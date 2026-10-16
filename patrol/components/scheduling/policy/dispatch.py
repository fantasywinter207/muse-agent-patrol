"""SQLite-owned cooperative dispatch and lease-generation fencing.

Scheduling a work ID never grants a CrewQuay role or claims the original Queue.
Remote side effects live in Outbox and must be reconciled independently.
"""
import json
import math
from .scoring import priority, quantum, preemption
from ...persistence.index import canonical, new_id, StaleLease

KINDS=frozenset(("work","promise","social","research","maintenance"))
ACTIVE=("ready","suspended")


class Scheduler:
    def __init__(self, db, lease_seconds=120):
        if type(lease_seconds) is not int or lease_seconds<30:
            raise ValueError("scheduler lease_seconds must be >=30")
        self.db=db
        self.lease_seconds=lease_seconds

    @staticmethod
    def _ready(c):
        return [dict(r) for r in c.execute(
            """SELECT a.* FROM schedule_activities a
               LEFT JOIN work_items w ON w.id=a.work_id
               WHERE a.status IN ('ready','suspended')
                 AND (a.work_id IS NULL OR w.status IN
                      ('pending','retry_wait','blocked','needs_approval'))
               ORDER BY a.created_at,a.id""")]

    @staticmethod
    def _current(c):
        r=c.execute("SELECT * FROM schedule_activities WHERE status='running'").fetchone()
        return dict(r) if r else None

    @staticmethod
    def _best(rows, now):
        return max(rows,key=lambda r:(priority(r,now),-float(r["created_at"]),r["id"])) if rows else None

    @staticmethod
    def _public(row,now):
        if row is None:
            return None
        result={key:row[key] for key in (
            "id","logical_key","kind","work_id","status","base_priority",
            "weight","deadline","lease_owner","lease_version","lease_until",
            "slice_until","preempt_requested","preempt_mode","preempt_reason",
            "cpu_seconds","virtual_runtime","created_at","ready_since","last_updated"
        )}
        result["score"]=priority(row,now,running=row["status"]=="running")
        result["checkpoint"]=json.loads(row["checkpoint"]) if row["checkpoint"] else None
        return result

    def register(self,logical,kind,now,*,priority_score=50,weight=50,
                 deadline=None,switch_cost=4,work_id=None):
        if (not isinstance(logical,str) or not 1<=len(logical)<=160
                or kind not in KINDS or type(priority_score) is not int
                or not 0<=priority_score<=100 or type(weight) is not int
                or not 1<=weight<=100 or type(switch_cost) is not int
                or not 0<=switch_cost<=30
                or type(now) not in (int,float) or not math.isfinite(now)
                or (deadline is not None and
                    (type(deadline) not in (int,float) or not math.isfinite(deadline)))
                or (work_id is not None and
                    (not isinstance(work_id,str) or not work_id or kind!="work"))):
            raise ValueError("invalid bounded activity registration")
        with self.db.transaction() as c:
            if work_id and not c.execute("SELECT 1 FROM work_items WHERE id=?",(work_id,)).fetchone():
                raise ValueError("unknown local work; cannot create platform assignment")
            old=c.execute("SELECT * FROM schedule_activities WHERE logical_key=?",(logical,)).fetchone()
            if old:
                if (old["kind"],old["work_id"],old["base_priority"],old["weight"],
                    old["deadline"],old["switch_cost"]) != (
                        kind,work_id,priority_score,weight,deadline,switch_cost):
                    raise ValueError("immutable activity registration mismatch")
                return old["id"]
            ident=new_id()
            c.execute("""INSERT INTO schedule_activities
                (id,logical_key,kind,work_id,status,base_priority,weight,deadline,
                 switch_cost,created_at,ready_since,last_updated)
                VALUES(?,?,?,?,'ready',?,?,?,?,?,?,?)""",
                      (ident,logical,kind,work_id,priority_score,weight,deadline,
                       switch_cost,now,now,now))
            return ident

    def sync_work(self,now,limit=100):
        """Idempotently reflect local human-decision work; no model or claim."""
        if type(limit) is not int or not 1<=limit<=500:
            raise ValueError("sync limit must be 1..500")
        added=0
        with self.db.transaction() as c:
            rows=c.execute("""SELECT w.id,w.priority,w.created_at,w.start_by,w.due_at,
                         p.due_at AS promise_due
                         FROM work_items w LEFT JOIN promises p
                           ON p.work_item_id=w.id AND p.status!='done'
                         WHERE w.type='human_action' AND w.status IN
                           ('pending','retry_wait','blocked','needs_approval')
                         ORDER BY w.created_at LIMIT ?""",(limit,)).fetchall()
            for row in rows:
                key="work:"+row["id"]
                if c.execute("SELECT 1 FROM schedule_activities WHERE logical_key=?",(key,)).fetchone():
                    continue
                deadline=row["promise_due"] or row["due_at"] or row["start_by"]
                c.execute("""INSERT INTO schedule_activities
                  (id,logical_key,kind,work_id,status,base_priority,weight,
                   deadline,switch_cost,created_at,ready_since,last_updated)
                  VALUES(?,?, 'work',?,'ready',?,?,?,?,?,?,?)""",
                  (new_id(),key,row["id"],min(100,max(0,row["priority"])),
                   50,deadline,4,now,now,now))
                added+=1
            # External workers retain their own Queue fencing. A locally
            # completed work item invalidates an unleased scheduled copy,
            # but cannot undo anything already dispatched to Outbox.
            c.execute("""UPDATE schedule_activities SET status='completed',
                lease_owner=NULL,lease_until=NULL,slice_until=NULL,
                preempt_requested=0,preempt_mode=NULL,
                lease_version=lease_version+1,last_updated=?,
                preempt_reason='source work already completed'
                WHERE work_id IN (SELECT id FROM work_items WHERE status='done')
                AND status IN ('ready','suspended','running')""",(now,))
        return added

    @staticmethod
    def _charge(c,row,now):
        elapsed=max(0.0,min(float(now),float(row["lease_until"])) -
                    float(row["last_charged_at"] or row["last_started_at"] or now))
        if elapsed>0:
            c.execute("""UPDATE schedule_activities
                SET cpu_seconds=cpu_seconds+?,
                    virtual_runtime=virtual_runtime+?,
                    last_charged_at=? WHERE id=?""",
                (elapsed,elapsed*100.0/float(row["weight"]),now,row["id"]))
        return elapsed

    def _recover(self,c,now):
        active=self._current(c)
        if not active or active["lease_until"]>now:
            return 0
        self._charge(c,active,now)
        c.execute("""UPDATE schedule_activities SET status='suspended',
            lease_owner=NULL,lease_until=NULL,slice_until=NULL,
            lease_version=lease_version+1,ready_since=?,last_updated=?,
            preempt_requested=0,preempt_mode=NULL,
            preempt_reason='expired lease; reconcile Outbox before new effects'
            WHERE id=? AND status='running'""",(now,now,active["id"]))
        return 1

    def _assess_tx(self,c,now,mutate):
        current=self._current(c)
        contenders=self._ready(c)
        best=self._best(contenders,now)
        mode=preemption(current,best,now) if current else None
        if current and mode and mutate and not current["preempt_requested"]:
            c.execute("""UPDATE schedule_activities
                SET preempt_requested=1,preempt_mode=?,preempt_reason=?,
                    last_updated=? WHERE id=? AND status='running'""",
                (mode,"a higher-value eligible activity is ready",now,current["id"]))
            current=self._current(c)
        return {"running":self._public(current,now),
                "candidate":self._public(best,now),
                "preemption":current["preempt_mode"] if current and current["preempt_requested"] else mode,
                "advice":"checkpoint_at_safe_point" if current and
                         (current["preempt_requested"] or mode) else
                         "continue" if current else "ready_to_dispatch" if best else "idle",
                "remote_side_effects":"not managed by scheduler"}

    def reconcile(self,now,limit=100):
        """On a Patrol tick, refresh waiting activities and request a safe point.

        Does not claim local work or invoke a model. The host may react to the
        returned advice on its next wakeup. Never modifies Outbox rows.
        """
        added=self.sync_work(now,limit)
        with self.db.transaction() as c:
            expired=self._recover(c,now)
            decision=self._assess_tx(c,now,True)
        return {"work_registered":added,"expired_leases_recovered":expired,
                "decision":decision,"model_invoked":False,
                "remote_action_issued":False}

    def assess(self,now):
        """Read-only scheduling diagnosis; does not dispatch models."""
        with self.db.transaction() as c:
            return self._assess_tx(c,now,False)

    def dispatch(self,owner,now):
        """Grant one local time slice or request a safe-point preemption."""
        if not isinstance(owner,str) or not 1<=len(owner)<=128:
            raise ValueError("nonempty bounded worker owner required")
        with self.db.transaction() as c:
            expired=self._recover(c,now)
            view=self._assess_tx(c,now,True)
            if view["running"]:
                return {**view,"granted":False,"lease_recovered":expired}
            best=self._best(self._ready(c),now)
            if best is None:
                return {**view,"granted":False,"lease_recovered":expired}
            n=c.execute("""UPDATE schedule_activities SET status='running',
                lease_owner=?,lease_version=lease_version+1,
                lease_until=?,slice_until=?,last_started_at=?,
                last_charged_at=?,preempt_requested=0,preempt_mode=NULL,
                preempt_reason=NULL,last_updated=?
                WHERE id=? AND status IN ('ready','suspended')""",
                (owner,now+self.lease_seconds,now+quantum(best["weight"]),
                 now,now,now,best["id"])).rowcount
            if n!=1:
                raise StaleLease("another scheduler dispatched this activity")
            active=self._current(c)
            return {"granted":True,"activity":self._public(active,now),
                    "lease_recovered":expired,
                    "warning":"scheduler lease is not work-claim or CrewQuay authorization"}

    @staticmethod
    def _owned(c,ident,owner,generation,now):
        if type(generation) is not int or generation<=0 or not owner:
            raise StaleLease("current generation and worker identity required")
        row=c.execute("""SELECT * FROM schedule_activities
            WHERE id=? AND status='running' AND lease_owner=?
            AND lease_version=? AND lease_until>?""",
            (ident,owner,generation,now)).fetchone()
        if row is None:
            raise StaleLease("activity is no longer owned by this lease generation")
        return dict(row)

    def checkpoint(self,ident,owner,generation,now,data,*,voluntary=False):
        if (not isinstance(data,dict) or not data or
                len(canonical(data).encode("utf-8"))>32768):
            raise ValueError("nonempty checkpoint <=32KiB required")
        with self.db.transaction() as c:
            row=self._owned(c,ident,owner,generation,now)
            self._charge(c,row,now)
            row=dict(c.execute("SELECT * FROM schedule_activities WHERE id=?",
                               (ident,)).fetchone())
            contenders=self._best(self._ready(c),now)
            mode=preemption(row,contenders,now)
            suspended=bool(voluntary or row["preempt_requested"] or mode)
            if suspended:
                c.execute("""UPDATE schedule_activities SET
                    status='suspended',checkpoint=?,lease_owner=NULL,
                    lease_until=NULL,slice_until=NULL,
                    lease_version=lease_version+1,
                    preempt_requested=0,preempt_mode=NULL,
                    preempt_reason=?,ready_since=?,last_updated=?
                    WHERE id=?""",
                    (canonical(data),"voluntary yield" if voluntary else
                     row["preempt_mode"] or mode or "safe-point yield",
                     now,now,ident))
            else:
                c.execute("""UPDATE schedule_activities SET checkpoint=?,
                    lease_until=?,slice_until=?,last_updated=?
                    WHERE id=?""",
                    (canonical(data),now+self.lease_seconds,
                     now+quantum(row["weight"]),now,ident))
            new=dict(c.execute("SELECT * FROM schedule_activities WHERE id=?",
                               (ident,)).fetchone())
            return {"suspended":suspended,"activity":self._public(new,now),
                    "next_step":"dispatch_again" if suspended else "continue",
                    "warning":"previously staged remote effects remain in Outbox"}

    def complete(self,ident,owner,generation,now,evidence):
        if not isinstance(evidence,dict) or not evidence:
            raise ValueError("activity completion requires documented evidence")
        if len(canonical(evidence).encode("utf-8"))>32768:
            raise ValueError("evidence too large")
        with self.db.transaction() as c:
            row=self._owned(c,ident,owner,generation,now)
            if row["work_id"]:
                work=c.execute("SELECT status FROM work_items WHERE id=?",
                               (row["work_id"],)).fetchone()
                if not work or work["status"]!="done":
                    raise ValueError("linked local work must be completed with separate work-finish evidence")
            self._charge(c,row,now)
            c.execute("""UPDATE schedule_activities SET
                status='completed',checkpoint=?,lease_owner=NULL,
                lease_until=NULL,slice_until=NULL,lease_version=lease_version+1,
                preempt_requested=0,preempt_mode=NULL,last_updated=?
                WHERE id=?""",(canonical(evidence),now,ident))
            return {"completed":ident,"remote_action_issued":False}

    def cancel(self,ident,now,reason):
        if not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise ValueError("nonempty bounded cancellation reason required")
        with self.db.transaction() as c:
            n=c.execute("""UPDATE schedule_activities SET status='cancelled',
                lease_version=lease_version+1,preempt_reason=?,last_updated=?
                WHERE id=? AND status IN ('ready','suspended')""",
                (reason,now,ident)).rowcount
            if not n:
                raise ValueError("activity not cancellable while running or terminal")
            return {"cancelled":ident,"remote_action_issued":False}

    def snapshot(self,now,limit=50):
        if type(limit) is not int or not 1<=limit<=200:
            raise ValueError("snapshot limit must be 1..200")
        rows=self.db.read("""SELECT * FROM schedule_activities
            ORDER BY CASE status WHEN 'running' THEN 0
             WHEN 'ready' THEN 1 WHEN 'suspended' THEN 2 ELSE 3 END,
             created_at,id LIMIT ?""",(limit,))
        return {"activities":[self._public(r,now) for r in rows],
                "decision":self.assess(now),
                "model_invoked":False,"host_woken":False}
