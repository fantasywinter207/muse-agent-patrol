import hashlib
import json
from datetime import datetime
from urllib.parse import quote, urlencode
from ...persistence.index import canonical, cursor
from ...queue.index import enqueue_tx
from ...transport.index import BudgetExceeded, AuthError


def event_time(value, fallback):
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        return float(value)
    if isinstance(value,str):
        try:
            dt=datetime.fromisoformat(value.replace("Z","+00:00"))
            if dt.tzinfo is not None:
                return dt.timestamp()
        except ValueError:
            pass
    return fallback


class Sync:
    def __init__(self, db):
        self.db=db

    def state(self, source, scope):
        rows=self.db.read("SELECT * FROM sync_cursors WHERE source=? AND scope=?",(source,scope))
        return rows[0] if rows else {"source":source,"scope":scope,"stream":"","received":"0","handled":"0","target":None,"next_at":0,"failures":0,"covered_at":0}

    def page(self, source, scope, stream, items, next_cursor, now, target=None, next_at=None):
        next_cursor=cursor(next_cursor)
        with self.db.transaction() as c:
            old=c.execute("SELECT * FROM sync_cursors WHERE source=? AND scope=?",(source,scope)).fetchone()
            if old and old["stream"]!=stream:
                raise ValueError("stream reset requires a successful resource resync")
            if old and int(next_cursor)<int(old["received"]):
                raise ValueError("stale page cannot regress cursor")
            for item in items:
                if not isinstance(item,dict):
                    raise ValueError("invalid event object")
                external=item.get("eventId") or item.get("messageId") or item.get("seq") or item.get("updateSeq")
                if external is None:
                    external=hashlib.sha256(canonical(item).encode()).hexdigest()
                # Snapshot status/version changes get distinct keys.
                if source in ("tasks","projects"):
                    external=str(item.get("id",""))+":"+hashlib.sha256(canonical(item).encode()).hexdigest()
                ec=item.get("cursor")
                if ec is not None:
                    ec=cursor(str(ec))
                c.execute("""INSERT INTO events(source,scope,stream,external_key,cursor,payload,observed_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,scope,stream,external_key) DO NOTHING""",
                    (source,scope,stream,str(external),ec,canonical(item),now))
                row=c.execute("SELECT id FROM events WHERE source=? AND scope=? AND stream=? AND external_key=?",(source,scope,stream,str(external))).fetchone()
                enqueue_tx(c,"event:"+str(row[0]),"inspect_event",{"event_id":row[0]},now,event_id=row[0],start_by=now+180)
            c.execute("""INSERT INTO sync_cursors(source,scope,stream,received,target,next_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source,scope) DO UPDATE SET received=excluded.received,
                target=excluded.target,next_at=excluded.next_at,failures=0""",
                (source,scope,stream,next_cursor,target,now if next_at is None else next_at))
            if target is None:
                c.execute("UPDATE sync_cursors SET covered_at=? WHERE source=? AND scope=?",(now,source,scope))
            self._handled(c,source,scope,stream)

    @staticmethod
    def _handled(c, source, scope, stream):
        # No higher handled watermark while any earlier event's work is uncompleted.
        pending=c.execute("""SELECT 1 FROM events e JOIN work_items w ON w.event_id=e.id
            WHERE e.source=? AND e.scope=? AND e.stream=? AND w.status!='done' LIMIT 1""",(source,scope,stream)).fetchone()
        if not pending:
            c.execute("UPDATE sync_cursors SET handled=received WHERE source=? AND scope=?",(source,scope))

    def reconcile_handled(self):
        with self.db.transaction() as c:
            for row in c.execute("SELECT source,scope,stream FROM sync_cursors").fetchall():
                self._handled(c,*row)

    def failure(self, source, scope, now, retry_after=0):
        old=self.state(source,scope)
        failures=min(old["failures"]+1,10)
        # Bounded backoff persisted across ticks; no blocking retry loop.
        delay=max(retry_after,min(1800,15*2**failures))
        with self.db.transaction() as c:
            c.execute("""INSERT INTO sync_cursors(source,scope,next_at,failures) VALUES(?,?,?,?)
                ON CONFLICT(source,scope) DO UPDATE SET next_at=excluded.next_at,failures=excluded.failures""",(source,scope,now+delay,failures))

    def reset(self, stream, waterline, now):
        waterline=cursor(waterline)
        with self.db.transaction() as c:
            c.execute("""INSERT INTO sync_cursors(source,scope,stream,received,handled,next_at)
                VALUES('events','global',?,?,?,?) ON CONFLICT(source,scope) DO UPDATE SET stream=excluded.stream,
                received=excluded.received,handled=excluded.handled,target=NULL,next_at=excluded.next_at,failures=0""",
                (stream,waterline,waterline,now))


class Collector:
    def __init__(self, sync, request, config):
        self.sync=sync
        self.request=request
        self.config=config
        self.errors=[]
        self.resync_since=None

    def identity(self):
        r=self.request("GET","/api/v1/agents/me")
        if r.status!=200 or not isinstance(r.body,dict) or not r.body.get("id"):
            raise AuthError("identity could not be verified")
        actor=r.body["id"]
        if self.config.actor_id and actor!=self.config.actor_id:
            raise AuthError("authenticated actor differs from configured actor")
        with self.sync.db.transaction() as c:
            old=c.execute("SELECT value FROM meta WHERE key='actor'").fetchone()
            if old and old[0]!=actor:
                raise AuthError("database is bound to a different actor")
            c.execute("INSERT INTO meta VALUES('actor',?) ON CONFLICT(key) DO NOTHING",(actor,))
        return actor

    def pages(self, source, scope, path, now, force=False, interval=None):
        state=self.sync.state(source,scope)
        if force and self.resync_since is not None and state["covered_at"]>=self.resync_since:
            return True
        if not force and state["next_at"]>now:
            return True
        after=state["received"]
        interval=interval or self.config.normal_scan_seconds
        for _ in range(self.config.max_pages_per_source):
            try:
                response=self.request("GET",path+("&" if "?" in path else "?")+urlencode({"after":after,"limit":100}))
            except BudgetExceeded:
                return False
            if response.status!=200:
                self.sync.failure(source,scope,now,response.retry_after)
                self.errors.append({"source":source,"status":response.status})
                return False
            b=response.body
            if isinstance(b,list):
                items=b
                nxt=after
                more=False
                if source=="chats":
                    ids=[v.get("conversationId") or v.get("id") for v in items if isinstance(v,dict)]
                    with self.sync.db.transaction() as c:
                        c.execute("INSERT INTO meta VALUES('visible_chats',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical([v for v in ids if v]),))
            elif isinstance(b,dict) and isinstance(b.get("items"),list):
                items=b["items"]
                nxt=str(b.get("nextCursor",after))
                # Inbox/thread pages can omit hasMore: continue to a terminal page.
                more=bool(b["hasMore"]) if "hasMore" in b else bool(items) and int(nxt)>int(after)
            else:
                raise ValueError("unrecognized collection response; cursor retained")
            cursor(nxt)
            if more and int(nxt)<=int(after):
                raise ValueError("pagination did not advance")
            self.sync.page(source,scope,"",items,nxt,now,target="incomplete" if more else None,next_at=now if more else now+interval)
            if not more:
                return True
            after=nxt
        return False

    def resources(self, now, force=False, chat_ids=None):
        complete=True
        for source,path in (("inbox","/api/v1/inbox"),("chats","/api/v1/chats"),("tasks","/api/v1/tasks?mine=true"),("updates","/api/v1/updates")):
            complete=self.pages(source,"global",path,now,force) and complete
        ids=set(self.config.allowed_chat_ids)
        if chat_ids:
            ids.update(chat_ids)
        # Include every currently visible chat, not merely the last-message projection.
        current=self.sync.db.read("SELECT value FROM meta WHERE key='visible_chats'")
        if current:
            ids.update(json.loads(current[0]["value"]))
        for cid in sorted(ids):
            complete=self.pages("messages",cid,"/api/v1/chats/"+quote(cid,safe="")+"/messages",now,force or bool(chat_ids and cid in chat_ids),
                                self.config.active_scan_seconds if chat_ids and cid in chat_ids else None) and complete
        for pid in self.config.project_ids:
            for suffix in ("","/progress","/artifacts"):
                snapshot=self.sync.state("projects",pid+suffix)
                if (force and self.resync_since is not None and snapshot["covered_at"]>=self.resync_since) or (not force and snapshot["next_at"]>now):
                    continue
                r=self.request("GET","/api/v1/projects/"+quote(pid,safe="")+suffix)
                if r.status!=200:
                    complete=False
                    self.errors.append({"source":"project","status":r.status})
                else:
                    body=r.body
                    items=body if isinstance(body,list) else [body]
                    self.sync.page("projects",pid+suffix,"",items,"0",now,next_at=now+self.config.normal_scan_seconds)
        return complete

    def events(self, waterline, stream, now):
        waterline=cursor(str(waterline))
        state=self.sync.state("events","global")
        if state["stream"]!=stream or int(state["received"])>int(waterline):
            return "resync"
        if state["next_at"]>now:
            return "backoff"
        target=state["target"] or waterline
        after=state["received"]
        for _ in range(self.config.max_pages_per_source):
            try:
                r=self.request("GET","/api/v1/events?"+urlencode({"after":after,"to":target,"limit":100}))
            except BudgetExceeded:
                return "incomplete"
            if r.status in (410,422):
                return "resync"
            if r.status!=200:
                self.sync.failure("events","global",now,r.retry_after)
                self.errors.append({"source":"events","status":r.status})
                return "failed"
            b=r.body
            if not isinstance(b,dict) or not isinstance(b.get("items"),list):
                raise ValueError("invalid event page")
            nxt=str(b.get("nextCursor",target if not b.get("hasMore") else after))
            cursor(nxt)
            if int(nxt)>int(target) or (b.get("hasMore") and int(nxt)<=int(after)):
                raise ValueError("invalid event coverage")
            done=not b.get("hasMore")
            self.sync.page("events","global",stream,b["items"],target if done else nxt,now,target=None if done else target,
                           next_at=now+self.config.normal_scan_seconds if done else now)
            if done:
                return "complete"
            after=nxt
        return "incomplete"
