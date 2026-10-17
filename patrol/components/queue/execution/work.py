import json
from ...persistence.index import canonical, new_id, StaleLease


def enqueue_tx(c, logical, kind, payload, now, priority=0, start_by=None, due_at=None, event_id=None):
    ident = new_id()
    c.execute("""INSERT INTO work_items(id,event_id,logical_key,type,payload,status,priority,created_at,start_by,next_action_at,due_at)
                 VALUES(?,?,?,?,?,'pending',?,?,?,?,?) ON CONFLICT(logical_key) DO NOTHING""",
              (ident, event_id, logical, kind, canonical(payload), priority, now, start_by, now, due_at))
    return c.execute("SELECT id FROM work_items WHERE logical_key=?", (logical,)).fetchone()[0]


class Queue:
    def __init__(self, database, lease_seconds=120):
        self.db = database
        self.lease_seconds = lease_seconds

    def enqueue(self, logical, kind, payload, now, **kwargs):
        with self.db.transaction() as c:
            return enqueue_tx(c, logical, kind, payload, now, **kwargs)

    def recover(self, now):
        with self.db.transaction() as c:
            # Local work may resume. Remote effects are independently held in Outbox.
            c.execute("""UPDATE work_items SET status='pending',lease_owner=NULL,lease_until=NULL,
                         next_action_at=?,block_reason='expired local lease; reconcile Outbox before any remote effect'
                         WHERE status='running' AND lease_until<=?""", (now, now))

    def claim(self, owner, now, lane="any"):
        """Highest scored ready work, reserving optional slots for background.

        A capped aging bonus prevents old tasks from dominating fresh high scores.
        The scheduler dedicates every fourth slot to lower-priority backlog.
        """
        if lane not in ("any","background","local_messages"):
            raise ValueError("invalid queue lane")
        background_filter="""
            AND NOT EXISTS (
                SELECT 1 FROM message_scores ms WHERE ms.event_id=work_items.event_id
                AND COALESCE(ms.muse_score,ms.base_score)>=70)
        """ if lane=="background" else (
            """ AND type='inspect_event' AND EXISTS (
                SELECT 1 FROM events e WHERE e.id=work_items.event_id
                 AND e.source IN ('messages','agent_events'))
            """ if lane=="local_messages" else "")
        with self.db.transaction() as c:
            row = c.execute("""SELECT * FROM work_items WHERE status IN ('pending','retry_wait')
                AND next_action_at<=? """+background_filter+"""
                ORDER BY (
                    CASE WHEN EXISTS (
                        SELECT 1 FROM events e
                        WHERE e.id=work_items.event_id AND e.source='messages'
                    ) THEN COALESCE((
                        SELECT COALESCE(ms.muse_score,ms.base_score)
                        FROM message_scores ms WHERE ms.event_id=work_items.event_id
                    ),20)
                    ELSE priority END
                    + MIN(20,MAX(0,CAST((? - created_at)/600 AS INTEGER)))
                ) DESC, created_at ASC LIMIT 1""",(now,now)).fetchone()
            if not row:
                return None
            c.execute("""UPDATE work_items SET status='running',lease_owner=?,lease_until=?,
                         lease_version=lease_version+1,attempts=attempts+1 WHERE id=?""",
                      (owner, now+self.lease_seconds, row["id"]))
            result = dict(c.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone())
            result["payload"] = json.loads(result["payload"])
            return result

    def claim_specific(self, ident, owner, now, authorization_evidence):
        """Explicit local handoff. Evidence is an operator assertion, not platform approval."""
        if not owner or not ident or not isinstance(authorization_evidence,dict) or not authorization_evidence:
            raise ValueError("owner, work id, and documented operator authorization required")
        with self.db.transaction() as c:
            n=c.execute("""UPDATE work_items SET status='running',lease_owner=?,lease_until=?,
                lease_version=lease_version+1,attempts=attempts+1
                WHERE id=? AND type='human_action' AND status IN
                    ('pending','retry_wait','blocked','needs_approval')
                AND next_action_at<=?""",(owner,now+self.lease_seconds,ident,now)).rowcount
            if n!=1:
                raise ValueError("human work unavailable or already leased")
            row=dict(c.execute("SELECT * FROM work_items WHERE id=?",(ident,)).fetchone())
            # Append-only evidence of the claimed local handoff, including lease generation.
            # This is not a CrewQuay permission check or remote task claim.
            detail={"work_id":ident,"owner":owner,"lease_version":row["lease_version"],
                    "operator_evidence":authorization_evidence}
            c.execute("INSERT INTO alerts(logical_key,kind,detail,created_at) VALUES(?,?,?,?)",
                      ("handoff:"+ident+":"+str(row["lease_version"]),"local_handoff",canonical(detail),now))
            row["payload"]=json.loads(row["payload"])
            return row

    def renew(self, item, now, evidence):
        """Renew a live local lease only with new nontrivial progress evidence."""
        if not isinstance(evidence,dict) or not evidence or (
                set(evidence).issubset({"started","working","in_progress","status"})
                and all(isinstance(v,(str,bool)) for v in evidence.values())):
            raise ValueError("lease renewal requires evidence of actual progress")
        serialized=canonical(evidence)
        with self.db.transaction() as c:
            row=c.execute("""SELECT evidence FROM work_items WHERE id=? AND type='human_action'
                AND status='running' AND lease_owner=? AND lease_version=? AND lease_until>?""",
                (item["id"],item["lease_owner"],item["lease_version"],now)).fetchone()
            if not row:
                raise StaleLease("cannot renew expired or superseded lease")
            if row["evidence"]==serialized:
                raise ValueError("repeating the same evidence cannot prolong a lease")
            c.execute("""UPDATE work_items SET lease_until=?,evidence=?,
                first_action_at=COALESCE(first_action_at,?),last_progress_at=?
                WHERE id=? AND lease_owner=? AND lease_version=? AND status='running'""",
                (now+self.lease_seconds,serialized,now,now,
                 item["id"],item["lease_owner"],item["lease_version"]))
            detail={"work_id":item["id"],"lease_version":item["lease_version"],
                    "owner":item["lease_owner"],"progress":evidence}
            c.execute("INSERT INTO alerts(logical_key,kind,detail,created_at) VALUES(?,?,?,?)",
                ("work_progress:"+new_id(),"local_work_progress",canonical(detail),now))
            return {"lease_until":now+self.lease_seconds,"last_progress_at":now}

    def finish(self, item, now, evidence=None, status="done", reason=None, retry_at=None):
        if status not in ("done", "blocked", "needs_approval", "retry_wait"):
            raise ValueError("invalid work result")
        if status == "done" and (not isinstance(evidence,dict) or not evidence):
            raise ValueError("completion requires evidence")
        with self.db.transaction() as c:
            n = c.execute("""UPDATE work_items SET status=?,evidence=?,block_reason=?,next_action_at=?,
                first_action_at=CASE WHEN ? IS NOT NULL THEN COALESCE(first_action_at,?) ELSE first_action_at END,
                last_progress_at=CASE WHEN ? IS NOT NULL THEN ? ELSE last_progress_at END,
                lease_owner=NULL,lease_until=NULL WHERE id=? AND status='running'
                AND lease_owner=? AND lease_version=? AND lease_until>?""",
                (status, canonical(evidence) if evidence else None, reason, retry_at or now,
                 1 if evidence else None, now, 1 if evidence else None, now, item["id"], item["lease_owner"], item["lease_version"], now)).rowcount
            if n != 1:
                raise StaleLease("expired or superseded worker cannot finish")

    def resume(self, ident, now, evidence):
        if not evidence:
            raise ValueError("resume requires evidence of resolved condition")
        with self.db.transaction() as c:
            c.execute("""UPDATE work_items SET status='pending',next_action_at=?,block_reason=NULL
                         WHERE id=? AND status IN ('blocked','needs_approval')""", (now, ident))

    def status(self):
        return self.db.read("SELECT status,count(*) AS count FROM work_items GROUP BY status")
