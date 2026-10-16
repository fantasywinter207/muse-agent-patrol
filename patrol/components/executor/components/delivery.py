"""Bounded, credential-verified Outbox delivery; never generates decisions.

One verified CrewQuay actor performs all remote API writes. The Muse worker
that drafted an action is only an audit producer, not the platform principal.
"""
import hashlib
import json
import time
from urllib.parse import quote

from ...attention.index import Attention
from ...collection.index import Collector, Sync, event_time
from ...persistence.index import canonical
from ...outbox.index import Outbox
from ...transport.index import AuthError


class ActionSender:
    def __init__(self, db, config, client=None, clock=time.time):
        self.db=db
        self.config=config
        self.client=client
        self.clock=clock
        self.outbox=Outbox(db)
        self.sync=Sync(db,config.max_event_age_seconds,config.future_skew_seconds)
        self.attention=Attention(db,config)
        self.actor=None

    def can_send(self,row):
        if not self.config.allow_writes or not self.actor or self.actor!=row["actor"]:
            return False,"explicit write authorization and current matching actor required"
        if row["action"] in ("send","read") and row["target"] not in self.config.allowed_chat_ids:
            return False,"chat outside explicit authorized scope"
        if row["action"]=="read":
            state=self.sync.state("messages",row["target"])
            if state["covered_at"]<=0 or state["target"] is not None or int(row["payload"]["cursor"])>int(state["handled"]):
                return False,"read cursor is not covered by durably handled messages"
        if row["action"]=="ack":
            if len(row["payload"]["ids"])==1 and not self.config.ack_single_enabled:
                return False,"single ACK compatibility gate; retained without POST"
            # Every ACK id needs durable inbox coverage and explicit message dependencies.
            for seq in row["payload"]["ids"]:
                rows=self.db.read("SELECT payload,observed_at FROM events WHERE source='inbox' AND external_key=?",(str(seq),))
                if not rows:
                    return False,"inbox item not durable"
                item=json.loads(rows[0]["payload"])
                typ=item.get("event_type") or item.get("type")
                dependencies=self.config.ack_chat_map.get(typ)
                if dependencies is None:
                    return False,"explicit ACK dependency mapping required"
                for cid in dependencies:
                    state=self.sync.state("messages",cid)
                    if state["covered_at"]<rows[0]["observed_at"] or state["target"] is not None:
                        return False,"dependent messages not durably covered"
        return True,None

    def send(self,row):
        if row["action"]=="ack":
            path="/api/v1/inbox/ack"
        else:
            path="/api/v1/chats/"+quote(row["target"],safe="")+("/messages" if row["action"]=="send" else "/read")
        # Authorization is checked immediately before every network write.
        allowed,_=self.can_send(row)
        if not allowed:
            raise AuthError("authorization changed")
        return self.client.request("POST",path,row["payload"],row["idem_key"])

    def verify(self,row,response):
        # A read-only unknown-result reconciliation must still respect the
        # authorized chat scope, even though it never sends a POST.
        if row["action"]=="send" and row["target"] not in self.config.allowed_chat_ids:
            return None
        # Unknown operations without a stable remote id cannot safely be guessed from matching text.
        if response is None and row.get("remote_id"):
            response={"messageId":row["remote_id"]}
        if row["action"]=="send" and isinstance(response,dict):
            message=response.get("message",response)
            ident=message.get("messageId") or message.get("id")
            if not ident:
                return None
            # A collector may already have advanced beyond this success message.
            # Bounded replay may defer verification, but cannot confirm a text twin.
            after=row.get("verify_cursor","0")
            for _ in range(self.config.max_pages_per_source):
                r=self.client.request("GET","/api/v1/chats/"+quote(row["target"],safe="")+"/messages?after="+after+"&limit=100")
                if r.status!=200 or not isinstance(r.body,dict):
                    return None
                for item in r.body.get("items",[]):
                    if (item.get("messageId") or item.get("id"))==ident and item.get("senderId")==row["actor"] and item.get("content")==row["payload"]["content"]:
                        self.attention.observe(row["target"],"own:"+ident,event_time(item.get("createdAt"),self.clock()),self.clock(),own=True)
                        return {"id":ident,"source":"REST message readback","sha256":hashlib.sha256(canonical(item).encode()).hexdigest()}
                nxt=str(r.body.get("nextCursor",after))
                if int(nxt)<int(after):
                    return None
                self.outbox.verified_page(row["id"],nxt)
                if not r.body.get("hasMore") or nxt==after:
                    break
                after=nxt
        # ACK/read require an operator's evidence-backed resolution until an exact
        # endpoint-specific readback contract is supplied. Never replay them blindly.
        return None

    def drain_once(self, limit=3, action_id=None, lane="any"):
        """Deliver only bounded, due durable Outbox actions; NO full patrol.

        Authentication and platform identity are checked before network writes.
        No model calls, prompt interpretation, automatic authorization or
        credential printing occur here. A crash-ambiguous POST is reconciled,
        never blindly retried.
        """
        if type(limit) is not int or not 1<=limit<=50:
            raise ValueError("limit must be between 1 and 50")
        if action_id is not None and (not isinstance(action_id,str) or not action_id):
            raise ValueError("nonempty Outbox ID required")
        if lane not in ("any","chat","notifications"):
            raise ValueError("unsupported action lane")
        now=self.clock()
        report={"mode":"outbox_once","status":"idle","outbox_attempts":[],
                "confirmed":0,"unknown":0,"blocked":0,
                "remote_writes_allowed":bool(self.config.allow_writes)}
        if not self.config.allow_writes:
            report["status"]="authorization_failed"
            return report
        self.outbox.recover(now,self.config.lease_seconds)
        selection=" AND id=?" if action_id is not None else ""
        scope={"any":"","chat":" AND action='send'",
               "notifications":" AND action IN ('ack','read')"}[lane]
        params=(now,action_id) if action_id is not None else (now,)
        # Check due work before loading credentials or making an API request.
        due=self.db.read("""SELECT id FROM outbox WHERE
            status IN ('prepared','retry_wait','unknown') AND next_attempt_at<=?"""+
            selection+scope+" ORDER BY created_at LIMIT 1",params)
        if not due:
            if action_id:
                row=self.db.read("SELECT status FROM outbox WHERE id=?",(action_id,))
                report["item_status"]=row[0]["status"] if row else "not_found"
                if report["item_status"]=="not_found":
                    report["status"]="not_found"
            return report
        if self.client is None:
            report["status"]="authorization_failed"
            return report
        # An executor identity is the authenticated platform actor, never a
        # handoff-provided producer id. A DB bound to another actor is rejected.
        self.actor=Collector(self.sync,self.client.request,self.config).identity()
        report["verified_actor"]=self.actor
        report["outbox_attempts"]=self.outbox.process(
            self.clock(),self.can_send,self.send,self.verify,limit=limit,
            action_id=action_id,lane=lane)
        if action_id:
            row=self.db.read("SELECT status FROM outbox WHERE id=?",(action_id,))
            report["item_status"]=row[0]["status"] if row else "not_found"
        report["confirmed"]=sum(x.get("status")=="confirmed"
                                for x in report["outbox_attempts"])
        report["unknown"]=sum(x.get("status")=="unknown"
                              for x in report["outbox_attempts"])
        report["blocked"]=sum(x.get("status")=="blocked"
                              for x in report["outbox_attempts"])
        if (report["blocked"] or report["unknown"]
                or any(x["status"] not in ("confirmed","blocked") for x in report["outbox_attempts"])):
            report["status"]="degraded"
        elif report["outbox_attempts"]:
            report["status"]="ok"
        elif action_id and report.get("item_status")=="confirmed":
            report["status"]="ok"
        else:
            report["status"]="partial"
        return report
