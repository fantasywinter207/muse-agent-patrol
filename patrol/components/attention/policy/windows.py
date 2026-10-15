class Attention:
    def __init__(self, db, config):
        self.db = db
        self.active_window = config.active_window_seconds
        self.cooldown_window = config.cooldown_window_seconds
        self.own_window = config.own_reply_window_seconds
        self.max_age = config.max_event_age_seconds
        self.future_skew = config.future_skew_seconds
        self.intervals = {"active":config.active_scan_seconds,"cooldown":config.cooldown_scan_seconds,"idle":config.normal_scan_seconds}

    def observe(self, target, event_key, event_time, now, own=False, meaningful=True):
        if not meaningful or event_time > now+self.future_skew or event_time < now-self.max_age:
            return False
        event_time = min(event_time, now)
        with self.db.transaction() as c:
            used=c.execute("INSERT INTO attention_consumed VALUES(?,?) ON CONFLICT(target,event_key) DO NOTHING",(target,event_key)).rowcount
            if not used:
                return False
            old=c.execute("SELECT * FROM attention_sessions WHERE target=?",(target,)).fetchone()
            if own and old and old["own_until"] > 0:
                return False
            until=max(old["active_until"] if old else 0,event_time+(self.own_window if own else self.active_window))
            own_until=event_time+self.own_window if own else 0
            c.execute("""INSERT INTO attention_sessions VALUES(?,'active',?,?,?,?,?)
                ON CONFLICT(target) DO UPDATE SET state='active',active_until=excluded.active_until,
                cooldown_until=excluded.cooldown_until,next_check_at=MIN(attention_sessions.next_check_at,excluded.next_check_at),
                last_activity_at=MAX(attention_sessions.last_activity_at,excluded.last_activity_at),own_until=excluded.own_until""",
                (target,until,until+self.cooldown_window,now,event_time,own_until))
            return True

    def advance(self, now):
        with self.db.transaction() as c:
            c.execute("UPDATE attention_sessions SET state='idle' WHERE cooldown_until<=?",(now,))
            c.execute("UPDATE attention_sessions SET state='cooldown' WHERE active_until<=? AND cooldown_until>?",(now,now))

    def due(self, now):
        self.advance(now)
        return self.db.read("SELECT * FROM attention_sessions WHERE next_check_at<=? ORDER BY next_check_at",(now,))

    def checked(self, target, now):
        with self.db.transaction() as c:
            row=c.execute("SELECT state FROM attention_sessions WHERE target=?",(target,)).fetchone()
            if row:
                c.execute("UPDATE attention_sessions SET next_check_at=? WHERE target=?",(now+self.intervals[row[0]],target))
