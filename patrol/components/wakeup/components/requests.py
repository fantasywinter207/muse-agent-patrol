"""Durable wakeup intent outbox. NEVER calls a model, Cron, Hook or remote API."""
import json
from ...persistence.index import canonical, new_id, StaleLease


class Wakeups:
    def __init__(self, db, lease_seconds=120):
        self.db=db
        self.lease_seconds=lease_seconds

    def stage(self, now, threshold=70, limit=50):
        if type(threshold) is not int or not 0<=threshold<=100 or type(limit) is not int or not 1<=limit<=200:
            raise ValueError("invalid wakeup threshold/limit")
        added=0
        with self.db.transaction() as c:
            # Only actual agent-decision work, not mere uninspected messages.
            # Blocked includes 'no executor': it still needs Muse judgement.
            works=c.execute("""SELECT w.id,w.priority,e.source
                FROM work_items w LEFT JOIN events e ON e.id=w.event_id
                WHERE w.type='human_action'
                  AND w.status IN ('pending','retry_wait','blocked','needs_approval')
                  AND w.priority>=? AND (w.event_id IS NULL OR e.source='messages')
                  AND NOT EXISTS(SELECT 1 FROM promises p WHERE p.work_item_id=w.id)
                ORDER BY w.priority DESC,w.created_at LIMIT ?""",(threshold,limit)).fetchall()
            for w in works:
                k="work:"+w["id"]
                n=c.execute("""INSERT INTO wakeup_requests(
                    id,logical_key,source_type,source_id,priority,reason,created_at,next_attempt_at)
                    VALUES(?,?,'work',?,?,?, ?,?)
                    ON CONFLICT(logical_key) DO NOTHING""",
                    (new_id(),k,w["id"],min(100,max(0,w["priority"])),
                     "authorized agent decision needed; not platform approval",now,now)).rowcount
                added+=n
            # Only due or near-due promises, never every dormant future promise.
            rows=c.execute("""SELECT id,next_action_at,status FROM promises
                WHERE status!='done' AND (next_action_at<=? OR
                    (first_action_at IS NULL AND start_by<=?) OR
                    (due_at IS NOT NULL AND due_at<=?))
                ORDER BY next_action_at LIMIT ?""",(now+120,now,now,limit)).fetchall()
            for p in rows:
                # A new next-action deadline creates a new logical signal after progress.
                k="promise:"+p["id"]+":"+str(p["next_action_at"])
                n=c.execute("""INSERT INTO wakeup_requests(
                    id,logical_key,source_type,source_id,priority,reason,created_at,next_attempt_at)
                    VALUES(?,?,'promise',?,100,?, ?,?)
                    ON CONFLICT(logical_key) DO NOTHING""",
                    (new_id(),k,p["id"],"next promise action due",now,now)).rowcount
                added+=n
        return added

    def _source_alive(self, c, row):
        if row["source_type"]=="work":
            return bool(c.execute("""SELECT 1 FROM work_items WHERE id=?
                AND type='human_action' AND status IN
                ('pending','retry_wait','blocked','needs_approval')""",
                (row["source_id"],)).fetchone())
        return bool(c.execute("""SELECT 1 FROM promises WHERE id=? AND status!='done'
                AND ('promise:'||id||':'||CAST(next_action_at AS TEXT))=?""",
                (row["source_id"],row["logical_key"])).fetchone())

    def export(self, now, limit=50):
        if type(limit) is not int or not 1<=limit<=200:
            raise ValueError("limit must be 1..200")
        rows=self.db.read("""SELECT id,logical_key,source_type,source_id,priority,reason,
            status,created_at,next_attempt_at,lease_until,attempts
            FROM wakeup_requests WHERE status IN ('pending','retry_wait','unknown','leased')
            ORDER BY CASE WHEN status IN ('pending','retry_wait') AND next_attempt_at<=? THEN 0 ELSE 1 END,
            priority DESC,created_at LIMIT ?""",(now,limit))
        for row in rows:
            row["claimable"]=row["status"] in ("pending","retry_wait") and row["next_attempt_at"]<=now
        return {"mode":"read_only_no_host_dispatch","generated_at":now,
                "warning":"A wakeup intention is not a host wakeup, worker execution, or authorization.",
                "requests":rows}

    def recover(self, now, detail=False):
        """Reconcile expired delivery leases against the CURRENT work state.

        Once the business work is completed (or a promise was closed/rekeyed),
        there is no remaining wakeup to deliver: retire the expired lease as
        obsolete. Otherwise, delivery may have occurred before a crash and
        remains unknown; NEVER silently retry that request.
        """
        totals={"unknown":0,"obsolete":0}
        with self.db.transaction() as c:
            rows=c.execute("""SELECT * FROM wakeup_requests
                WHERE status='leased' AND (lease_until IS NULL OR lease_until<=?)""",
                (now,)).fetchall()
            for row in rows:
                new_status="unknown" if self._source_alive(c,row) else "obsolete"
                count=c.execute("""UPDATE wakeup_requests SET status=?,
                    lease_owner=NULL,lease_until=NULL WHERE id=? AND status='leased'
                    AND (lease_until IS NULL OR lease_until<=?)""",
                    (new_status,row["id"],now)).rowcount
                totals[new_status]+=count
        return totals if detail else totals["unknown"]+totals["obsolete"]

    def claim(self, ident, owner, now):
        if not ident or not isinstance(owner,str) or not owner.strip():
            raise ValueError("specific ID and nonempty worker identity required")
        with self.db.transaction() as c:
            row=c.execute("SELECT * FROM wakeup_requests WHERE id=?",(ident,)).fetchone()
            if not row:
                raise ValueError("unknown wakeup request")
            if not self._source_alive(c,row):
                # Never report "obsolete" while an active delivery lease
                # remains persisted. An expired lease may be safely retired
                # because the underlying business work is already complete.
                if row["status"]=="obsolete":
                    return {"id":ident,"status":"obsolete","host_called":False}
                n=c.execute("""UPDATE wakeup_requests SET status='obsolete',
                    lease_owner=NULL,lease_until=NULL WHERE id=?
                    AND (status IN ('pending','retry_wait','unknown')
                         OR (status='leased' AND
                             (lease_until IS NULL OR lease_until<=?)))""",
                    (ident,now)).rowcount
                if n:
                    return {"id":ident,"status":"obsolete","host_called":False}
                raise StaleLease("active or confirmed wakeup cannot be retired by claim")
            n=c.execute("""UPDATE wakeup_requests SET status='leased',lease_owner=?,
                lease_until=?,lease_version=lease_version+1,attempts=attempts+1
                WHERE id=? AND status IN ('pending','retry_wait') AND next_attempt_at<=?""",
                (owner,now+self.lease_seconds,ident,now)).rowcount
            if n!=1:
                raise StaleLease("already delivered, unknown, not due, or leased")
            x=dict(c.execute("SELECT * FROM wakeup_requests WHERE id=?",(ident,)).fetchone())
            return {"id":x["id"],"lease_owner":x["lease_owner"],
                    "lease_version":x["lease_version"],"lease_until":x["lease_until"],
                    "source_type":x["source_type"],"source_id":x["source_id"],
                    "host_called":False}

    def acknowledge(self, ident, owner, version, now, result, evidence):
        if result not in ("accepted","confirmed_failed","unknown"):
            raise ValueError("invalid dispatch result")
        if not isinstance(evidence,dict) or not evidence:
            raise ValueError("host receipt or failure evidence required")
        dest={"accepted":"accepted","confirmed_failed":"retry_wait","unknown":"unknown"}[result]
        retry=now+60 if result=="confirmed_failed" else now
        with self.db.transaction() as c:
            n=c.execute("""UPDATE wakeup_requests SET status=?,dispatch_evidence=?,
                confirmed_at=?,next_attempt_at=?,lease_owner=NULL,lease_until=NULL
                WHERE id=? AND status='leased' AND lease_owner=?
                AND lease_version=? AND lease_until>?""",
                (dest,canonical(evidence),now,retry,ident,owner,version,now)).rowcount
            if n!=1:
                raise StaleLease("wakeup delivery lease expired or superseded")
        return {"id":ident,"status":dest,"business_work_completed":False}

    def reconcile_unknown(self, ident, now, proven_absent, evidence):
        if proven_absent is not True or not isinstance(evidence,dict) or not evidence:
            raise ValueError("explicit verified non-delivery proof required")
        with self.db.transaction() as c:
            n=c.execute("""UPDATE wakeup_requests SET status='retry_wait',next_attempt_at=?,
                dispatch_evidence=? WHERE id=? AND status='unknown'""",
                (now,canonical(evidence),ident)).rowcount
            if n!=1:
                raise ValueError("unknown delivery state required")
        return {"id":ident,"status":"retry_wait","host_called":False}
