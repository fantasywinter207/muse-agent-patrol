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

    def claim(self, owner, now):
        with self.db.transaction() as c:
            row = c.execute("""SELECT * FROM work_items WHERE status IN ('pending','retry_wait') AND next_action_at<=?
                ORDER BY CASE WHEN start_by<=? THEN 1 ELSE 0 END DESC,
                (priority + CAST((? - created_at)/300 AS INTEGER)) DESC, created_at LIMIT 1""",
                            (now, now, now)).fetchone()
            if not row:
                return None
            c.execute("""UPDATE work_items SET status='running',lease_owner=?,lease_until=?,
                         lease_version=lease_version+1,attempts=attempts+1 WHERE id=?""",
                      (owner, now+self.lease_seconds, row["id"]))
            result = dict(c.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone())
            result["payload"] = json.loads(result["payload"])
            return result

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
