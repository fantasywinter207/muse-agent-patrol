"""Post-fix boundary checks with fake HTTP only."""
import json
import tempfile
import unittest
from pathlib import Path
from patrol.components.persistence.index import Database
from patrol.components.collection.index import Sync, Collector
from patrol.components.config.index import Config
from patrol.components.transport.index import Response
from patrol.components.attention.index import Attention
from patrol.components.promises.index import Promises
from patrol.components.runtime.index import Tick

class Followup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=Database(self.root/'db.sqlite3');self.cfg=Config(database=str(self.db.path))
    def tearDown(self):self.tmp.cleanup()

    def test_active_interval_does_not_override_retry_after(self):
        sync=Sync(self.db);calls=[]
        def fake(m,p):
            calls.append(p)
            if '/chat_demo/messages?' in p:return Response(429,{},retry_after=3600)
            return Response(200,{'items':[],'nextCursor':'0','hasMore':False})
        collector=Collector(sync,fake,self.cfg)
        collector.pages('messages','chat_demo','/api/v1/chats/chat_demo/messages',100)
        before=sync.state('messages','chat_demo');n=len(calls)
        collector.resources(161,chat_ids=['chat_demo'])
        repeated=[p for p in calls[n:] if '/chat_demo/messages?' in p]
        print('ACTIVE_RETRY_AFTER',json.dumps({'before':before,'request_time':161,'repeat_requests':repeated,'after':sync.state('messages','chat_demo')}))
        self.assertEqual(repeated,[],'active scan bypassed persisted Retry-After 3600 seconds')

    def test_visible_chat_envelope_is_included_in_resource_reconciliation(self):
        calls=[]
        def fake(m,p):
            calls.append(p)
            if p.startswith('/api/v1/chats?'):
                return Response(200,{'items':[{'conversationId':'chat_visible','cursor':'1'}],'nextCursor':'1','hasMore':False})
            return Response(200,{'items':[],'nextCursor':'0','hasMore':False})
        collector=Collector(Sync(self.db),fake,self.cfg)
        collector.resources(100)
        chat_calls=[p for p in calls if '/chat_visible/messages?' in p]
        print('VISIBLE_CHAT_ENVELOPE',json.dumps({'all_GETs':calls,'visible_chat_message_GETs':chat_calls}))
        self.assertTrue(chat_calls,'supported envelope form persisted chat but skipped its messages')

    def test_nine_minute_old_message_still_activates_attention(self):
        # A normal ten-minute idle poll must not lose a legitimate message
        # just because it arrived near the beginning of the interval.
        now = 700
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES('actor','my-agent')")
        Sync(self.db).page("messages","chat_demo","",
            [{"messageId":"m1","cursor":"1","senderId":"remote","content":"hello","createdAt":now-540}],
            "1",now)
        Tick(self.db,self.cfg,clock=lambda:now).run()
        row=self.db.read("SELECT * FROM attention_sessions WHERE target='chat_demo'")[0]
        self.assertEqual(row["state"],"active")
        self.assertGreater(row["active_until"],now)

    def test_promise_next_action_cannot_be_scheduled_tomorrow(self):
        promises=Promises(self.db)
        with self.assertRaises(ValueError):
            promises.create("worker","owner","tomorrow","task",100,110,100+86400)
        ident=promises.create("worker","owner","timely","task",100,110,120,due_at=300)
        with self.assertRaises(ValueError):
            promises.progress(ident,130,{"artifact":"real-progress"},130+86400)
        with self.assertRaises(ValueError):
            promises.progress(ident,130,{"started":True},140)
        promises.progress(ident,130,{"artifact":"real-progress"},140)
        self.assertEqual(self.db.read("SELECT next_action_at FROM promises WHERE id=?",(ident,))[0]["next_action_at"],140)

    def test_external_reason_cannot_defer_start_until_tomorrow(self):
        promises=Promises(self.db)
        with self.assertRaises(ValueError):
            promises.create("worker","owner","late-start","task",100,1000,150,
                            due_at=2000,objective_reason="dependency holds delivery")
        # Start immediately to document the dependency, even if delivery is far away.
        ident=promises.create("worker","owner","early-start","task",100,110,120,
                              due_at=2000,objective_reason="dependency holds delivery")
        self.assertTrue(ident)

    def test_normal_deadline_cannot_slip_to_tomorrow_without_reason(self):
        promises=Promises(self.db)
        with self.assertRaises(ValueError):
            promises.create("worker","owner","unexplained","task",100,110,120,due_at=100+86400,kind="normal")
        ident=promises.create("worker","owner","explained","task",100,110,120,due_at=100+86400,
                              kind="complex",objective_reason="documented long-running external dependency")
        self.assertTrue(ident)

    def test_chat_collection_prioritizes_oldest_due_scope(self):
        cfg=Config(database=str(self.db.path),allowed_chat_ids=["chat_a","chat_z"])
        sync=Sync(self.db)
        sync.page("messages","chat_a","",[],"0",0,next_at=1200)
        sync.page("messages","chat_z","",[],"0",0,next_at=200)
        calls=[]
        def fake(method,path):
            calls.append(path)
            return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        self.assertTrue(Collector(sync,fake,cfg).resources(1200))
        chats=[path for path in calls if "/messages?" in path]
        self.assertEqual(len(chats),2)
        self.assertIn("chat_z",chats[0])

    def test_per_chat_due_wakes_even_when_global_sources_sleep(self):
        cfg=Config(database=str(self.db.path),allowed_chat_ids=["chat_demo"])
        sync=Sync(self.db)
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES('actor','fixture-agent')")
        for source in ("inbox","chats","tasks","updates","events"):
            sync.page(source,"global","",[],"0",90,next_at=1000)
        sync.page("messages","chat_demo","",[],"0",90,next_at=100)
        paths=[]
        class Client:
            def request(self,method,path,*args):
                paths.append(path)
                if path=="/api/v1/agents/me":
                    return Response(200,{"id":"fixture-agent"})
                if path=="/api/v1/events?limit=1":
                    return Response(200,{"streamId":"stream","waterline":"0"})
                return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        report=Tick(self.db,cfg,Client(),clock=lambda:200).run()
        self.assertIn(report["status"],("ok","partial"))
        self.assertTrue(any("/chats/chat_demo/messages?" in p for p in paths),paths)

    def test_incomplete_snapshot_is_not_reported_as_ok(self):
        cfg=Config(database=str(self.db.path),max_pages_per_source=1)
        class Client:
            def request(self,method,path,*args):
                if path=="/api/v1/agents/me":
                    return Response(200,{"id":"fixture-agent"})
                if path=="/api/v1/events?limit=1":
                    return Response(200,{"streamId":"stream","waterline":"0"})
                if path.startswith("/api/v1/inbox?"):
                    return Response(200,{"items":[{"seq":1}],"nextCursor":"1","hasMore":True})
                return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        result=Tick(self.db,cfg,Client(),clock=lambda:100).run()
        self.assertEqual(result["status"],"partial",result)
        self.assertIn("resource_snapshots",result["incomplete_sources"])


if __name__=='__main__':unittest.main(verbosity=2)
