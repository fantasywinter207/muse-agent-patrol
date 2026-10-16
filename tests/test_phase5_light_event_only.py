"""Phase 5 event-only Light Tick tests with fake HTTP + temp SQLite."""
import tempfile
import unittest
from pathlib import Path

from patrol.components.config.index import Config
from patrol.components.persistence.index import Database
from patrol.components.runtime.index import Tick
from patrol.components.transport.index import Response


def event(seq=1,kind="chat.message.created",created="1970-01-01T00:16:40Z",
          scope_type="chat",scope_id="owner",message_id="msg1",sender="owner"):
    e={"eventId":"ev"+str(seq),"cursor":str(seq),"type":kind,
       "resourceId":message_id,"createdAt":created}
    if kind in ("chat.message.created","chat.mentioned","room.message.created",
                "room.mentioned","message.created"):
        e["message"]={"messageId":message_id,"scopeType":scope_type,
                     "scopeId":scope_id,"senderId":sender,"senderKind":"agent",
                     "content":"Project collaboration","createdAt":created}
    return e


def page(items,after="0",to="1",has_more=False,stream="stream1",
         deleted="0",next_cursor=None):
    return {"items":items,"streamId":stream,"deletedThrough":deleted,
            "waterline":to,"to":to,"hasMore":has_more,
            "nextCursor":next_cursor if next_cursor is not None else to}


class Fake:
    def __init__(self,replies,actor="muse"):
        self.replies=list(replies)
        self.calls=[]
        self.requests=0
        self.actor=actor

    def request(self,method,path,*args):
        self.requests+=1
        self.calls.append((method,path))
        if path=="/api/v1/agents/me":
            return Response(200,{"id":self.actor})
        if not path.startswith("/api/v1/agent/events?"):
            raise AssertionError("Legacy/light resource request forbidden: "+path)
        if not self.replies:
            raise AssertionError("Unexpected extra feed call")
        return self.replies.pop(0)


class EventOnlyLightTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db=Database(Path(tmp.name)/"light.sqlite3")
        self.cfg=Config(database=str(self.db.path),
                        allowed_chat_ids=["owner"],light_max_pages_per_source=1,
                        max_work_per_tick=12)
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('actor','muse')")

    def tick(self,replies,now=1000):
        fake=Fake(replies)
        report=Tick(self.db,self.cfg,client=fake,clock=lambda:now).run(light=True)
        self.assertEqual({m for m,p in fake.calls},{"GET"})
        self.assertFalse(any("/chats/" in p or "/inbox" in p or "/projects/" in p or
                             "/api/v1/events" in p or "/tasks/" in p for _,p in fake.calls))
        return report,fake.calls

    def test_chat_task_project_room_messages_share_feed_and_score(self):
        scenarios=[("chat","owner","chat.message.created"),
                   ("task","task4","message.created"),
                   ("project","proj1","message.created"),
                   ("room","room1","room.message.created")]
        for i,(scope_type,scope,kind) in enumerate(scenarios,1):
            msg=event(seq=i,kind=kind,scope_type=scope_type,scope_id=scope,
                      message_id="m"+str(i))
            response=page([msg],after=str(i-1),to=str(i))
            result,_=self.tick([Response(200,response)],now=1000+i)
            self.assertEqual(result["status"],"ok",result)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM message_receipts")[0]["n"],4)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM wakeup_requests")[0]["n"],4)
        scopes={x["scope"] for x in self.db.read("SELECT scope FROM events WHERE source='messages'")}
        self.assertEqual(scopes,{"owner","task:task4","project:proj1","room1"})
        self.assertEqual(self.db.read("SELECT received FROM sync_cursors WHERE source='agent_events'")[0]["received"],"4")

    def test_event_pointer_and_message_atomically_deduped(self):
        e=event()
        one=Response(200,page([e]))
        a,_=self.tick([one],now=1000)
        self.assertEqual(a["wakeup_staged"],1,a)
        again,_=self.tick([Response(200,page([],to="1"))],now=1100)
        self.assertEqual(again["wakeup_staged"],0,again)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM message_scores")[0]["n"],1)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM work_items WHERE type='human_action'")[0]["n"],1)

    def test_fresh_generic_task_event_requires_muse_decision(self):
        e=event(kind="task.ready")
        a,_=self.tick([Response(200,page([e]))])
        self.assertEqual(a["wakeup_staged"],1,a)
        work=self.db.read("SELECT * FROM work_items WHERE type='human_action'")
        self.assertEqual(len(work),1)
        self.assertEqual(work[0]["priority"],75)

    def test_redacted_and_stale_event_do_not_falsely_wake(self):
        old=event(seq=1,kind="task.ready",created="1970-01-01T00:00:10Z")
        red={"eventId":"ev2","type":"event.redacted","cursor":"2"}
        a,_=self.tick([Response(200,page([old,red],to="2"))])
        self.assertEqual(a["wakeup_staged"],0,a)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM work_items WHERE type='human_action'")[0]["n"],1)

    def test_malformed_cursor_or_message_keeps_checkpoint(self):
        for payload in [
            page([event()],to="1",has_more=True,next_cursor="0"),
            page([dict(event(),contextUnavailable=True,message=None)]),
        ]:
            a,_=self.tick([Response(200,payload)],now=1000)
            self.assertIn(a["status"],("degraded","partial"),a)
            state=self.db.read("SELECT received FROM sync_cursors WHERE source='agent_events'")
            self.assertFalse(state and state[0]["received"]!="0")
            self.assertEqual(self.db.read("SELECT count(*) AS n FROM message_receipts")[0]["n"],0)
            # Reset backoff only for isolated second malformed case.
            with self.db.transaction() as c:
                c.execute("UPDATE sync_cursors SET failures=0,next_at=0 WHERE source='agent_events'")

    def test_expired_server_history_never_skip_to_live(self):
        result,_=self.tick([Response(410,{"error":"RESYNC_REQUIRED"})])
        self.assertIn("agent_events:resync_required",result["incomplete_sources"])
        self.assertNotEqual(result["status"],"ok")
        self.assertEqual(self.db.read("SELECT received FROM sync_cursors WHERE source='agent_events'")[0]["received"],"0")

    def test_page_limit_does_not_advance_without_follow_up(self):
        e=event()
        a,_=self.tick([Response(200,page([e],to="3",has_more=True,next_cursor="1"))])
        self.assertEqual(a["event_replay"],"incomplete")
        state=self.db.read("SELECT * FROM sync_cursors WHERE source='agent_events'")[0]
        self.assertEqual(state["received"],"1")
        self.assertEqual(state["target"],"3")
        b,calls=self.tick([Response(200,page([],to="3",next_cursor="3"))],now=1060)
        self.assertEqual(b["event_replay"],"complete")
        self.assertTrue(any("to=3" in path for _,path in calls))

    def test_changed_stream_never_resets_checkpoint(self):
        self.tick([Response(200,page([event()]))])
        a,_=self.tick([Response(200,page([],to="1",stream="unexpected"))],now=1060)
        self.assertIn("agent_events:resync_required",a["incomplete_sources"])
        self.assertEqual(self.db.read("SELECT received FROM sync_cursors WHERE source='agent_events'")[0]["received"],"1")


if __name__=="__main__":
    unittest.main()
