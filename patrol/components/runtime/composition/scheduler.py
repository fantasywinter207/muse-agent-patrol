import hashlib
import json
import time
from urllib.parse import quote
from ...attention.index import Attention
from ...collection.index import Collector, Sync, event_time
from ...outbox.index import Outbox
from ...persistence.index import canonical, new_id, Busy
from ...promises.index import Promises
from ...queue.index import Queue, MessageScores
from ...transport.index import BudgetExceeded, AuthError, connect_ready
from ...wakeup.index import Wakeups


class Tick:
    def __init__(self, db, config, client=None, clock=time.time, monotonic=time.monotonic, ws_factory=None):
        self.db=db
        self.config=config
        self.client=client
        self.clock=clock
        self.monotonic=monotonic
        self.ws_factory=ws_factory
        self.queue=Queue(db,config.lease_seconds)
        self.wakeups=Wakeups(db,config.lease_seconds)
        self.scores=MessageScores(db)
        self.promises=Promises(db)
        self.attention=Attention(db,config)
        self.outbox=Outbox(db)
        self.sync=Sync(db,config.max_event_age_seconds,config.future_skew_seconds)
        self.collector=Collector(self.sync,client.request,config) if client else None
        self.actor=None
        self.errors=[]
        self.request_deadline=0

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

    def execute(self, limit, local_only=False):
        done=0
        for _ in range(limit):
            if self.monotonic()>=self.request_deadline:
                break
            now=self.clock()
            # Reserve every fourth inspection slot for lower-ranked backlog;
            # if no background work is due, use the slot for ordinary work.
            lane="local_messages" if local_only else ("background" if _ % 4==3 else "any")
            item=self.queue.claim(self.run_id,now,lane=lane)
            if not local_only and not item and lane=="background":
                item=self.queue.claim(self.run_id,now)
            if not item:
                break
            if item["type"]!="inspect_event":
                self.queue.finish(item,now,status="blocked",reason="no authorized executor for "+item["type"])
                self.db.alert("missing_executor:"+item["id"],"blocked",{"work_id":item["id"],"type":item["type"]},now)
                continue
            try:
                rows=self.db.read("SELECT * FROM events WHERE id=?",(item["payload"]["event_id"],))
                if not rows:
                    raise ValueError("event missing")
                row=rows[0]
                value=json.loads(row["payload"])
                evidence={"event_id":row["id"],"payload_sha256":hashlib.sha256(row["payload"].encode()).hexdigest(),"kind":"durable_resource_snapshot"}
                if row["source"] in ("events","legacy_pending","live"):
                    if not self.collector:
                        self.queue.finish(item,now,status="retry_wait",reason="offline: REST confirmation required",retry_at=now+60)
                        continue
                    typ=value.get("type","")
                    tid=value.get("resourceId","")
                    cid=value.get("roomId")
                    if typ=="event.redacted":
                        evidence["kind"]="redacted_pointer"
                    elif cid and ("chat" in typ or "message" in typ or "room" in typ):
                        if not self.collector.pages("messages",cid,"/api/v1/chats/"+quote(cid,safe="")+"/messages",now,force=True):
                            self.queue.finish(item,self.clock(),status="retry_wait",reason="message pagination incomplete",retry_at=now+60)
                            continue
                        evidence["kind"]="REST messages persisted"
                    elif tid.startswith("tsk_"):
                        r=self.client.request("GET","/api/v1/tasks/"+quote(tid,safe=""))
                        if r.status not in (200,403,404):
                            raise RuntimeError("task refresh failed")
                        if r.status==200 and (not isinstance(r.body,dict) or r.body.get("id")!=tid):
                            raise ValueError("task resource identity mismatch")
                        evidence.update(http=r.status,kind="REST task refresh",resource_sha256=hashlib.sha256(canonical(r.body).encode()).hexdigest())
                    else:
                        # Unknown pointers must not be mislabeled as processed.
                        self.queue.finish(item,self.clock(),status="blocked",reason="unsupported resource pointer; explicit handler required")
                        continue
                elif row["source"]=="inbox" and (value.get("event_type") or value.get("type"))=="chat.message.created":
                    # An inbox notification is an untrusted pointer, not proof of
                    # an external message. Only verified REST messages produce human work.
                    cid=value.get("conversationId") or value.get("chatId") or value.get("roomId")
                    if not isinstance(cid,str) or not cid:
                        self.queue.finish(item,self.clock(),status="blocked",
                                          reason="chat notification lacks a verified conversation id")
                        self.db.alert("unmapped_inbox:"+str(row["id"]),"blocked",
                                      {"event_id":row["id"],"reason":"missing chat id"},now)
                        continue
                    if not self.collector:
                        self.queue.finish(item,now,status="retry_wait",
                                          reason="offline inbox pointer needs REST confirmation",retry_at=now+60)
                        continue
                    if not self.collector.pages("messages",cid,
                            "/api/v1/chats/"+quote(cid,safe="")+"/messages",now,force=True):
                        self.queue.finish(item,self.clock(),status="retry_wait",
                                          reason="inbox chat messages not yet fully fetched",retry_at=now+60)
                        continue
                    evidence["kind"]="inbox pointer reconciled by REST messages"
                elif row["source"]=="messages":
                    cid=row["scope"]
                    if not self.actor:
                        # An unverified actor cannot safely classify a message
                        # as someone else's. Preserve the inspector for a later tick.
                        self.queue.finish(item,now,status="retry_wait",
                                          reason="actor identity unavailable",retry_at=now+60)
                        continue
                    external=(value.get("senderId")!=self.actor
                              and value.get("senderKind")!="system"
                              and isinstance(value.get("content"),str)
                              and bool(value["content"].strip()))
                    # Already-ingested messages remain eligible even if queue
                    # congestion made inspection happen after the 15m window.
                    eligible=self.scores.first_receipt_fresh(
                        row["id"],self.config.max_event_age_seconds,
                        self.config.future_skew_seconds)
                    # Attention is still time-bound; old delayed work must not
                    # create a new 30-minute live watch window.
                    self.attention.observe(cid,row["external_key"],
                                           event_time(value.get("createdAt"),0),now,
                                           meaningful=external)
                    if eligible and external:
                        scored=self.db.read("SELECT COALESCE(muse_score,base_score) AS score FROM message_scores WHERE event_id=?",(row["id"],))
                        self.queue.enqueue("human:"+cid+":"+str(row["external_key"]),"human_action",
                                           {"event_id":row["id"],"reason":"fresh on first receipt; authorized agent decision required"},
                                           now,start_by=now+180,priority=scored[0]["score"] if scored else 25,
                                           event_id=row["id"])
                self.queue.finish(item,self.clock(),evidence=evidence)
                done+=1
            except (BudgetExceeded,AuthError):
                self.queue.finish(item,self.clock(),status="retry_wait",reason="request budget or authorization unavailable",retry_at=now+60)
                break
            except Exception:
                self.queue.finish(item,self.clock(),status="retry_wait",reason="resource inspection failed",retry_at=now+min(1800,30*2**min(item["attempts"],5)))
        self.sync.reconcile_handled()
        return done

    def run_light(self):
        """One bounded incremental turn: no outbox writes, WS, or full backfill.

        This is not a 60-second message delivery guarantee. Idle cursors can
        still be scheduled 600s apart and server event history may lag.
        """
        self.run_id=new_id()
        start=self.clock()
        self.request_deadline=self.monotonic()+self.config.tick_budget_seconds
        report={"run_id":self.run_id,"mode":"light","status":"ok","completed_work":0,
                "started_at_utc_epoch":start,"errors":[],"incomplete_sources":[],
                "outbox_attempts":[],"host_wakeups":0,"requests_used":0}
        with self.db.transaction() as c:
            c.execute("INSERT INTO patrol_runs(id,started_at,status) VALUES(?,?,'running')",
                      (self.run_id,start))
        try:
            self.queue.recover(start)
            wakeup_recovery=self.wakeups.recover(start,detail=True)
            report["wakeup_unknown_recovered"]=wakeup_recovery["unknown"]
            report["wakeup_obsolete_recovered"]=wakeup_recovery["obsolete"]
            self.attention.advance(start)
            report["promise_alerts"]=self.promises.watchdog(start)
            known=self.db.read("SELECT value FROM meta WHERE key='actor'")
            self.actor=known[0]["value"] if known else None
            # Local work has a reserved slot before any potentially exhausted GET.
            if known:
                report["completed_work"]+=self.execute(
                    max(1,self.config.max_work_per_tick//2),local_only=True)
            if self.collector:
                self.actor=self.collector.identity()
                if not known:
                    report["completed_work"]+=self.execute(
                        max(1,self.config.max_work_per_tick//2),local_only=True)
                now=self.clock()
                # When full patrol has established an event stream, poll its
                # waterline every light tick. Shallow replay catches new event
                # pointers even when idle chat cursors are sleeping. No guessed
                # bootstrap or waterline jump on stream mismatch.
                checkpoint=self.sync.state("events","global")
                if checkpoint["stream"] and checkpoint["stream"]!="!resync-required":
                    if checkpoint["failures"] and checkpoint["next_at"]>now:
                        report["incomplete_sources"].append("event_stream_backoff")
                    else:
                        response=self.client.request("GET","/api/v1/events?limit=1")
                        body=response.body
                        if (response.status==200 and isinstance(body,dict)
                                and body.get("streamId") and body.get("waterline") is not None):
                            if (checkpoint["stream"]==body["streamId"]
                                    and int(checkpoint["received"])<=int(body["waterline"])):
                                event_result=self.collector.events(str(body["waterline"]),
                                    body["streamId"],self.clock(),force=True)
                                report["event_replay"]=event_result
                                if event_result!="complete":
                                    report["incomplete_sources"].append("event_stream:"+event_result)
                            else:
                                report["incomplete_sources"].append("event_stream_requires_full_resync")
                        else:
                            report["incomplete_sources"].append("event_waterline_unavailable")
                else:
                    report["incomplete_sources"].append("event_stream_requires_full_bootstrap")
                # Only shallow chat discovery, capped per-source; never force
                # historical cursor resets or scan every resource in light mode.
                if self.sync.state("chats","global")["next_at"]<=now:
                    if not self.collector.pages("chats","global","/api/v1/chats",now):
                        report["incomplete_sources"].append("chats")
                targets=set(self.config.allowed_chat_ids)
                visible=self.db.read("SELECT value FROM meta WHERE key='visible_chats'")
                if visible:
                    targets.update(json.loads(visible[0]["value"]))
                due=self.attention.due(now)
                active={row["target"] for row in due if row["state"]!="idle"}
                targets.update(active)
                # Treat error backoff as binding; only due chat cursors.
                candidates=[]
                for cid in targets:
                    cp=self.sync.state("messages",cid)
                    if cp["next_at"]<=now:
                        candidates.append((0 if cid in active else 1,cp["next_at"],cid))
                candidates.sort()
                for _,__,cid in candidates[:self.config.max_light_chats]:
                    ok=self.collector.pages("messages",cid,
                        "/api/v1/chats/"+quote(cid,safe="")+"/messages",
                        self.clock(),interval=(self.config.active_scan_seconds
                           if cid in active else self.config.normal_scan_seconds))
                    if not ok:
                        report["incomplete_sources"].append("messages:"+cid)
                    elif cid in active:
                        self.attention.checked(cid,self.clock())
                if len(candidates)>self.config.max_light_chats:
                    report["incomplete_sources"].append("chat_scan_capacity")
                # Fetch a single page of notifications only when due. An old
                # inbox cursor is *not* skipped to the newest item.
                if self.sync.state("inbox","global")["next_at"]<=self.clock():
                    if not self.collector.pages("inbox","global","/api/v1/inbox",self.clock()):
                        report["incomplete_sources"].append("inbox")
                report["errors"].extend(self.collector.errors)
                # Streaming replay is handled by full tick until a verified
                # low-cost event-only endpoint/contract is available.
            report["completed_work"]+=self.execute(self.config.max_work_per_tick)
        except (BudgetExceeded,AuthError) as e:
            report["status"]="deferred" if isinstance(e,BudgetExceeded) else "authorization_failed"
            report["errors"].append({"code":type(e).__name__})
        except Busy:
            # Preserve the CLI's dedicated exit=75 semantics for SQLite contention.
            report["status"]="busy"
            raise
        except Exception as e:
            # The CLI must report a failed tick, never a misleading successful
            # 'partial' if an unexpected upstream payload escapes a collector.
            # Do not serialize error text: it may contain external content.
            report["status"]="failed"
            report["errors"].append({"code":"UNEXPECTED_TICK_ERROR",
                                     "error_type":type(e).__name__})
        finally:
            # These are local, idempotent SQLite transactions, even if HTTP failed.
            report["wakeup_staged"]=self.wakeups.stage(self.clock(),
                threshold=self.config.wakeup_min_score)
            report["wakeup_ready"]=len(self.db.read(
                "SELECT id FROM wakeup_requests WHERE status IN ('pending','retry_wait') AND next_attempt_at<=?",
                (self.clock(),)))
            report["ended_at_utc_epoch"]=self.clock()
            report["requests_used"]=getattr(self.client,"requests",0)
            report["queue"]=self.queue.status()
            if report["status"]=="ok" and report["errors"]:
                report["status"]="degraded"
            elif report["status"]=="ok" and report["incomplete_sources"]:
                report["status"]="partial"
            with self.db.transaction() as c:
                c.execute("UPDATE patrol_runs SET ended_at=?,status=?,report=? WHERE id=?",
                          (self.clock(),report["status"],canonical(report),self.run_id))
        return report

    def run(self, websocket=False, light=False):
        if light:
            return self.run_light()

        self.run_id=new_id()
        start=self.clock()
        self.request_deadline=self.monotonic()+self.config.tick_budget_seconds
        with self.db.transaction() as c:
            c.execute("INSERT INTO patrol_runs(id,started_at,status) VALUES(?,?,'running')",(self.run_id,start))
        ws=None
        report={"run_id":self.run_id,"started_at_utc_epoch":start,"completed_work":0,"alerts":[],"errors":[],"ws_connected":False}
        alert_waterline=self.db.read("SELECT COALESCE(MAX(rowid),0) AS n FROM alerts")[0]["n"]
        try:
            self.queue.recover(start)
            # Full tick is also an entrypoint for recovery: a production
            # executor must not depend on Light running to clear expired leases.
            wakeup_recovery=self.wakeups.recover(start,detail=True)
            report["wakeup_unknown_recovered"]=wakeup_recovery["unknown"]
            report["wakeup_obsolete_recovered"]=wakeup_recovery["obsolete"]
            # Existing v3 databases already contain messages; score them in
            # bounded batches without losing history or modifying remote cursors.
            report["messages_backfilled"]=self.scores.backfill(limit=1000)
            self.outbox.recover(start,self.config.lease_seconds)
            self.attention.advance(start)
            report["alerts"]=self.promises.watchdog(start)
            known=self.db.read("SELECT value FROM meta WHERE key='actor'")
            self.actor=known[0]["value"] if known else None
            due=self.attention.due(start)
            active_ids=[r["target"] for r in due if r["state"]!="idle"]
            collection_due=any(self.sync.state(source,"global")["next_at"]<=start
                               for source in ("inbox","chats","tasks","updates","events"))
            # Every per-chat/project checkpoint is a wake-up source, even when
            # the five global collection cursors are still sleeping.
            scope_due=self.db.read("SELECT 1 FROM sync_cursors WHERE source IN ('messages','projects') AND next_at<=? LIMIT 1",(start,))
            work_due=self.db.read("SELECT 1 FROM work_items WHERE type='inspect_event' AND status IN ('pending','retry_wait') AND next_action_at<=? LIMIT 1",(start,))
            writes_due=self.config.allow_writes and self.db.read("SELECT 1 FROM outbox WHERE status IN ('prepared','unknown','retry_wait') AND next_attempt_at<=? LIMIT 1",(start,))
            needs_network=bool(not known or collection_due or scope_due or active_ids or work_due or writes_due)
            # Reserve local work time before ANY network request if the DB
            # already has a previously verified actor. Identity verification
            # can consume/raise BudgetExceeded during an overloaded first tick.
            # A new DB still verifies the actor before inspecting messages.
            if self.collector and needs_network and not known:
                self.actor=self.collector.identity()
            report["completed_work"]+=self.execute(max(1,self.config.max_work_per_tick//2))
            if self.collector and needs_network and known:
                self.actor=self.collector.identity()
            if self.client and needs_network:
                report["outbox_attempts"]=self.outbox.process(self.clock(),self.can_send,self.send,self.verify)
                ready=None
                if websocket:
                    try:
                        ws,ready=(self.ws_factory or connect_ready)(self.config,self.client.secret)
                        report["ws_connected"]=True
                    except Exception:
                        self.errors.append({"code":"WS_UNAVAILABLE","fallback":"REST"})
                state=self.sync.state("events","global")
                if not ready:
                    if self.monotonic()<self.request_deadline:
                        r=self.client.request("GET","/api/v1/events?limit=1")
                        if r.status==200 and isinstance(r.body,dict) and r.body.get("streamId") and r.body.get("waterline") is not None:
                            ready=r.body
                        else:
                            self.errors.append({"code":"EVENT_WATERLINE_UNAVAILABLE","http":r.status})
                chat_ids=active_ids
                needs_resync=ready and (state["stream"]!=ready["streamId"] or int(state["received"])>int(ready["waterline"]))
                if needs_resync:
                    with self.db.transaction() as c:
                        c.execute("INSERT INTO meta VALUES('resync_since',?) ON CONFLICT(key) DO NOTHING",(str(self.clock()),))
                        self.collector.resync_since=float(c.execute("SELECT value FROM meta WHERE key='resync_since'").fetchone()[0])
                complete=self.collector.resources(self.clock(),force=bool(needs_resync),chat_ids=chat_ids)
                if not complete:
                    report.setdefault("incomplete_sources",[]).append("resource_snapshots")
                if ready:
                    if needs_resync:
                        if complete:
                            self.sync.reset(ready["streamId"],str(ready["waterline"]),self.clock())
                            with self.db.transaction() as c:
                                c.execute("DELETE FROM meta WHERE key='resync_since'")
                            report["resync"]="resources reloaded; cursor rebuilt"
                            report["resync_complete"]=True
                        else:
                            report["resync"]="incomplete; prior cursor retained"
                            report.setdefault("incomplete_sources",[]).append("resync")
                    else:
                        result=self.collector.events(ready["waterline"],ready["streamId"],self.clock())
                        report["event_replay"]=result
                        if result in ("incomplete","backoff","failed","resync"):
                            report.setdefault("incomplete_sources",[]).append("event_stream:"+result)
                        if result=="resync":
                            # Keep old checkpoint. A fresh connection/waterline plus full REST reload
                            # is required on the next tick, recorded explicitly rather than guessing.
                            with self.db.transaction() as c:
                                c.execute("UPDATE sync_cursors SET stream='!resync-required' WHERE source='events' AND scope='global'")
                            self.errors.append({"code":"RESYNC_REQUIRED_NEXT_TICK","checkpoint":"retained"})
                    if ws and (report.get("event_replay")=="complete" or report.get("resync_complete")):
                        for _ in range(64):
                            if self.monotonic()>=self.request_deadline-1:
                                break
                            try:
                                raw=ws.recv()
                            except Exception:
                                break
                            if not raw:
                                break
                            frame=json.loads(raw)
                            if frame.get("eventId") and frame.get("cursor"):
                                # Persist live pointers without jumping the replay checkpoint.
                                checkpoint=self.sync.state("events","global")
                                self.sync.page("events","global",ready["streamId"],[frame],checkpoint["received"],self.clock(),target=checkpoint["target"],next_at=checkpoint["next_at"])
                for row in due:
                    self.attention.checked(row["target"],self.clock())
                report["completed_work"]+=self.execute(self.config.max_work_per_tick)
                report["errors"].extend(self.collector.errors)
            report["errors"].extend(self.errors)
            report["wakeup_staged"]=self.wakeups.stage(self.clock(),threshold=self.config.wakeup_min_score)
            report["host_wakeups"]=0
            report["ended_at_utc_epoch"]=self.clock()
            report["queue"]=self.queue.status()
            report["status"]="degraded" if report["errors"] else "partial" if report.get("incomplete_sources") else "ok"
            return report
        except (BudgetExceeded,AuthError) as e:
            report["status"]="deferred" if isinstance(e,BudgetExceeded) else "authorization_failed"
            report["errors"].append({"code":type(e).__name__})
            return report
        except Busy:
            report["status"]="busy"
            raise
        except Exception as e:
            report["status"]="failed"
            report["errors"].append({"code":"UNEXPECTED_TICK_ERROR",
                                     "error_type":type(e).__name__})
            return report
        finally:
            if "wakeup_staged" not in report:
                # A deferred network tick can still have completed local work.
                report["wakeup_staged"]=self.wakeups.stage(
                    self.clock(),threshold=self.config.wakeup_min_score)
                report["host_wakeups"]=0
            if ws:
                ws.close()
            report["alerts"]=[{"kind":r["kind"],**json.loads(r["detail"])}
                              for r in self.db.read("SELECT kind,detail FROM alerts WHERE rowid>? ORDER BY rowid",(alert_waterline,))]
            with self.db.transaction() as c:
                c.execute("UPDATE patrol_runs SET ended_at=?,status=?,report=? WHERE id=?",(self.clock(),report.get("status","failed"),canonical(report),self.run_id))
