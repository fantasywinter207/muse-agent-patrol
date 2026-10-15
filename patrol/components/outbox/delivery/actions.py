import hashlib
import json
from ...persistence.index import canonical, new_id, cursor


class Outbox:
    """Discovery never stages writes; actions need an explicit local operator."""
    def __init__(self, db):
        self.db=db

    def stage(self, logical, actor, action, target, payload, now):
        if action not in ("send","ack","read") or not actor or not logical:
            raise ValueError("unsupported or unattributed action")
        if action=="send" and (set(payload)!={"content"} or not isinstance(payload["content"],str) or not 1<=len(payload["content"])<=8000):
            raise ValueError("send payload must contain only content")
        if action=="ack" and (set(payload)!={"ids"} or not isinstance(payload["ids"],list) or not 1<=len(payload["ids"])<=100 or any(type(n)is not int or n<=0 or n>9007199254740991 for n in payload["ids"])):
            raise ValueError("invalid ACK payload")
        if action=="read":
            if set(payload)!={"cursor"}:
                raise ValueError("invalid read payload")
            cursor(payload["cursor"])
        encoded=canonical(payload)
        sha=hashlib.sha256(encoded.encode()).hexdigest()
        ident=new_id()
        with self.db.transaction() as c:
            row=c.execute("SELECT * FROM outbox WHERE logical_key=?",(logical,)).fetchone()
            if row:
                if (row["actor"],row["action"],row["target"],row["payload_hash"])!=(actor,action,target,sha):
                    raise ValueError("logical action has different immutable request")
                return row["id"]
            c.execute("""INSERT INTO outbox(id,actor,action,target,logical_key,idem_key,payload,payload_hash,status,created_at,next_attempt_at)
                VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)""",(ident,actor,action,target,logical,new_id(),encoded,sha,now,now))
            return ident

    def recover(self, now, lease_seconds):
        with self.db.transaction() as c:
            c.execute("""UPDATE outbox SET status='unknown',error='sender interrupted; reconcile remote result',
                dispatch_owner=NULL WHERE status='sending' AND dispatch_at<=?""",(now-lease_seconds,))

    def process(self, now, can_send, send, verify, limit=3):
        report=[]
        for row in self.db.read("SELECT * FROM outbox WHERE status IN ('prepared','retry_wait','unknown') AND next_attempt_at<=? ORDER BY created_at LIMIT ?",(now,limit)):
            row["payload"]=json.loads(row["payload"])
            if row["status"]=="unknown":
                result=verify(row,None)
                if result:
                    self._confirmed(row["id"],result)
                else:
                    with self.db.transaction() as c:
                        c.execute("UPDATE outbox SET next_attempt_at=? WHERE id=? AND status='unknown'",(now+300,row["id"]))
                continue
            allowed,reason=can_send(row)
            if not allowed:
                self.db.alert("outbox_block:"+row["id"],"outbox_blocked",{"id":row["id"],"reason":reason},now)
                continue
            token=new_id()
            with self.db.transaction() as c:
                won=c.execute("""UPDATE outbox SET status='sending',dispatch_owner=?,dispatch_at=?,attempts=attempts+1
                    WHERE id=? AND status IN ('prepared','retry_wait') AND next_attempt_at<=?""",(token,now,row["id"],now)).rowcount
            if not won:
                continue
            try:
                response=send(row)
            except Exception:
                response=None
            if response and 200<=response.status<300:
                # Persist the remote identity BEFORE readback so a crash can reconcile it.
                if isinstance(response.body,dict):
                    message=response.body.get("message",response.body)
                    remote=message.get("messageId") or message.get("id")
                    if remote:
                        with self.db.transaction() as c:
                            c.execute("UPDATE outbox SET remote_id=? WHERE id=? AND dispatch_owner=?",(str(remote),row["id"],token))
                try:
                    result=verify(row,response.body)
                except Exception:
                    result=None
                if result:
                    self._confirmed(row["id"],result,token)
                else:
                    self._result(row["id"],token,"unknown",now+300,"success response unconfirmed")
            elif response and response.status in (400,401,403,404,409,422):
                self._result(row["id"],token,"permanent_failed",now,"HTTP "+str(response.status))
            else:
                self._result(row["id"],token,"unknown",now+300,"ambiguous remote result")
            report.append({"id":row["id"],"action":row["action"]})
        return report

    def _confirmed(self, ident, evidence, token=None):
        with self.db.transaction() as c:
            sql="UPDATE outbox SET status='confirmed',result=?,remote_id=?,dispatch_owner=NULL,error=NULL WHERE id=?"
            args=[canonical(evidence),str(evidence.get("id","")),ident]
            if token:
                sql+=" AND dispatch_owner=? AND status='sending'"
                args.append(token)
            else:
                sql+=" AND status='unknown'"
            c.execute(sql,args)

    def _result(self, ident, token, status, next_at, error):
        with self.db.transaction() as c:
            c.execute("""UPDATE outbox SET status=?,next_attempt_at=?,error=?,dispatch_owner=NULL
                WHERE id=? AND dispatch_owner=? AND status='sending'""",(status,next_at,error,ident,token))

    def resolve(self, ident, evidence, now, confirmed=False, proven_absent=False):
        if not isinstance(evidence,dict) or not evidence or confirmed==proven_absent:
            raise ValueError("choose one evidence-backed resolution")
        with self.db.transaction() as c:
            n=c.execute("""UPDATE outbox SET status=?,result=?,next_attempt_at=?,error=NULL
                WHERE id=? AND status='unknown'""",("confirmed" if confirmed else "prepared",canonical(evidence),now,ident)).rowcount
            if n!=1:
                raise ValueError("unknown Outbox item required")

