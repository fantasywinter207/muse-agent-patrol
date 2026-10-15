import json
from ...persistence.index import canonical, new_id
from ...queue.index import enqueue_tx


class Promises:
    def __init__(self, db):
        self.db=db

    def create(self, actor, recipient, source_message_id, text, now, start_by, next_action_at,
               due_at=None, work_id=None, kind="human_action", payload=None, objective_reason=None):
        if not actor or not recipient or not source_message_id or not text:
            raise ValueError("promise provenance required")
        if start_by is None or next_action_at is None or start_by<now or next_action_at<now:
            raise ValueError("explicit start_by and next_action_at required")
        if due_at is not None and due_at<start_by:
            raise ValueError("deadline precedes start")
        if kind=="simple" and due_at and due_at-now>900 and not objective_reason:
            raise ValueError("simple target over 15m requires objective explanation")
        ident=new_id()
        with self.db.transaction() as c:
            old=c.execute("SELECT id FROM promises WHERE source_message_id=?",(source_message_id,)).fetchone()
            if old:
                return old[0]
            if work_id is None:
                work_id=enqueue_tx(c,"promise:"+source_message_id,kind,payload or {"source_message_id":source_message_id},now,start_by=start_by,due_at=due_at)
            elif not c.execute("SELECT id FROM work_items WHERE id=?",(work_id,)).fetchone():
                raise ValueError("unknown work item")
            c.execute("""INSERT INTO promises(id,work_item_id,actor,recipient,source_message_id,text,created_at,
                start_by,original_due_at,due_at,next_action_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (ident,work_id,actor,recipient,source_message_id,text,now,start_by,due_at,due_at,next_action_at))
            self._history(c,ident,"created",now,{"reason":objective_reason,"due_at":due_at})
            return ident

    @staticmethod
    def _history(c, ident, action, now, detail):
        c.execute("INSERT INTO promise_history(promise_id,action,at,detail) VALUES(?,?,?,?)",(ident,action,now,canonical(detail)))

    def progress(self, ident, now, evidence, next_action_at):
        if not isinstance(evidence,dict) or not evidence or next_action_at<=now:
            raise ValueError("progress requires structured evidence and next action")
        with self.db.transaction() as c:
            if not c.execute("SELECT id FROM promises WHERE id=? AND status!='done'",(ident,)).fetchone():
                raise ValueError("unknown or completed promise")
            c.execute("""UPDATE promises SET first_action_at=COALESCE(first_action_at,?),last_progress_at=?,
                progress_evidence=?,next_action_at=? WHERE id=?""",(now,now,canonical(evidence),next_action_at,ident))
            self._history(c,ident,"progress",now,evidence)

    def request_extension(self, ident, due_at, reason, evidence, now):
        if not reason or not evidence:
            raise ValueError("extension needs reason and evidence")
        with self.db.transaction() as c:
            row=c.execute("SELECT * FROM promises WHERE id=? AND status!='done'",(ident,)).fetchone()
            if not row:
                raise ValueError("unknown promise")
            self._history(c,ident,"extension_requested",now,{"old_due_at":row["due_at"],"new_due_at":due_at,"reason":reason,"evidence":evidence})

    def approve_extension(self, ident, due_at, approved_by, approval_evidence, now):
        with self.db.transaction() as c:
            row=c.execute("SELECT * FROM promises WHERE id=? AND status!='done'",(ident,)).fetchone()
            if not row or approved_by==row["actor"] or not approved_by or not approval_evidence:
                raise ValueError("independent approval identity and evidence required")
            request=c.execute("SELECT detail FROM promise_history WHERE promise_id=? AND action='extension_requested' ORDER BY id DESC LIMIT 1",(ident,)).fetchone()
            if not request or json.loads(request[0])["new_due_at"]!=due_at:
                raise ValueError("matching extension request required")
            self._history(c,ident,"extension_approved",now,{"old_due_at":row["due_at"],"new_due_at":due_at,"approved_by":approved_by,"approval_evidence":approval_evidence})
            c.execute("UPDATE promises SET due_at=? WHERE id=?",(due_at,ident))

    def complete(self, ident, now, evidence):
        if not isinstance(evidence,dict) or not evidence:
            raise ValueError("completion requires structured evidence")
        with self.db.transaction() as c:
            if not c.execute("SELECT id FROM promises WHERE id=? AND status!='done'",(ident,)).fetchone():
                raise ValueError("unknown or completed promise")
            self._history(c,ident,"completed",now,evidence)
            c.execute("UPDATE promises SET status='done',completed_evidence=? WHERE id=?",(canonical(evidence),ident))

    def watchdog(self, now):
        notices=[]
        for row in self.db.read("SELECT * FROM promises WHERE status!='done'"):
            tags=[]
            if row["first_action_at"] is None and now>=row["start_by"]:
                tags.append("start_overdue")
            if now>=row["next_action_at"]:
                tags.append("stalled")
            if row["due_at"] is not None and now>=row["due_at"]:
                tags.append("overdue")
            with self.db.transaction() as c:
                for tag in tags:
                    interval=row["next_action_at"] if tag=="stalled" else (row["due_at"] if tag=="overdue" else row["start_by"])
                    key="promise:"+row["id"]+":"+tag+":"+str(interval)
                    detail={"promise_id":row["id"],"work_id":row["work_item_id"],"state":tag}
                    added=c.execute("INSERT INTO alerts VALUES(?,?,?,?) ON CONFLICT(logical_key) DO NOTHING",(key,tag,canonical(detail),now)).rowcount
                    if added:
                        notices.append(detail)
                        self._history(c,row["id"],tag,now,detail)
                    c.execute("UPDATE work_items SET priority=MAX(priority,100) WHERE id=? AND status IN ('pending','retry_wait')",(row["work_item_id"],))
                if tags:
                    c.execute("UPDATE promises SET status=? WHERE id=?",("overdue" if "overdue" in tags else tags[0],row["id"]))
        return notices

