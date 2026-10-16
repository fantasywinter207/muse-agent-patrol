"""Regression for Phase 2 validation BUG-1: cursor regression is a failed
source, not a successful empty page, and must not abort the entire tick.

Fake REST, temporary SQLite only. No credentials, Hook, or network writes.
"""
import tempfile
import unittest
from pathlib import Path

from patrol.components.collection.index import Sync
from patrol.components.config.index import Config
from patrol.components.persistence.index import Database
from patrol.components.runtime.index import Tick
from patrol.components.transport.index import Response


class InvalidCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/"isolated.sqlite3")
        self.cfg=Config(database=str(self.db.path),allowed_chat_ids=["owner"],
                        max_pages_per_source=1,max_light_chats=1)

    def prepare_existing_message(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('actor','quill')")
        Sync(self.db).page("messages","owner","",[
            {"messageId":"owner1","cursor":"1","senderId":"owner",
             "content":"开始测试","createdAt":1000}],"1",1000)

    def test_light_regressive_empty_page_is_degraded_not_crash(self):
        self.prepare_existing_message()
        calls=[]
        class Fake:
            requests=0
            def request(self,method,path,*args):
                self.requests+=1
                calls.append(path)
                if path=="/api/v1/agents/me":
                    return Response(200,{"id":"quill"})
                if "/chats/owner/messages?" in path:
                    return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
                return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        report=Tick(self.db,self.cfg,client=Fake(),clock=lambda:2000).run(light=True)
        self.assertEqual(report["status"],"degraded",report)
        self.assertIn("messages:owner",report["incomplete_sources"])
        self.assertIn({"source":"messages","scope":"owner","code":"INVALID_PAGE_OR_CURSOR"},report["errors"])
        self.assertTrue(any(p.startswith("/api/v1/inbox?") for p in calls),
                        "bad chat page should not preempt other sources")
        checkpoint=Sync(self.db).state("messages","owner")
        self.assertEqual(checkpoint["received"],"1")
        self.assertEqual(checkpoint["failures"],1)
        self.assertGreater(checkpoint["next_at"],2000)
        self.assertEqual(self.db.read("SELECT status FROM patrol_runs")[0]["status"],"degraded")
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM events")[0]["n"],1)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM work_items WHERE type='human_action'")[0]["n"],1)
        self.assertEqual(report["wakeup_staged"],1)
        self.assertFalse(any(p.startswith("/api/v1/chats/owner/messages") and "POST" in p for p in calls))
        # Persisted backoff prevents a storm of repeated identical page requests.
        calls.clear()
        second=Tick(self.db,self.cfg,client=Fake(),clock=lambda:2001).run(light=True)
        self.assertNotIn("messages:owner",second["incomplete_sources"])
        self.assertFalse(any("/chats/owner/messages?" in p for p in calls))

    def test_full_regressive_empty_page_continues_other_sources(self):
        self.prepare_existing_message()
        calls=[]
        class Fake:
            requests=0
            def request(self,method,path,*args):
                self.requests+=1
                calls.append(path)
                if path=="/api/v1/agents/me":
                    return Response(200,{"id":"quill"})
                if path=="/api/v1/events?limit=1":
                    return Response(200,{"unexpected":"no-stream"})
                if "/chats/owner/messages?" in path:
                    return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
                return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        result=Tick(self.db,self.cfg,client=Fake(),clock=lambda:2000).run()
        self.assertEqual(result["status"],"degraded")
        self.assertIn({"source":"messages","scope":"owner","code":"INVALID_PAGE_OR_CURSOR"},
                      result["errors"])
        self.assertTrue(any(path.startswith("/api/v1/inbox?") for path in calls),calls)
        self.assertEqual(Sync(self.db).state("messages","owner")["received"],"1")
        self.assertEqual(self.db.read("SELECT status FROM patrol_runs")[0]["status"],"degraded")

    def test_light_invalid_event_cursor_keeps_checkpoint(self):
        self.prepare_existing_message()
        Sync(self.db).reset("known-stream","1",900)
        class Fake:
            requests=0
            def request(self,method,path,*args):
                self.requests+=1
                if path=="/api/v1/agents/me":
                    return Response(200,{"id":"quill"})
                if path=="/api/v1/events?limit=1":
                    return Response(200,{"streamId":"known-stream","waterline":"2"})
                if path.startswith("/api/v1/events?after="):
                    return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
                if "/chats/owner/messages?" in path:
                    return Response(200,{"items":[],"nextCursor":"1","hasMore":False})
                return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        result=Tick(self.db,self.cfg,client=Fake(),clock=lambda:2000).run(light=True)
        self.assertEqual(result["status"],"degraded",result)
        self.assertIn("event_stream:failed",result["incomplete_sources"])
        self.assertIn({"source":"events","scope":"global","code":"INVALID_EVENT_PAGE_OR_CURSOR"},
                      result["errors"])
        checkpoint=Sync(self.db).state("events","global")
        self.assertEqual(checkpoint["received"],"1")
        self.assertGreater(checkpoint["next_at"],2000)

    def test_light_unexpected_exception_is_honest_failed_report(self):
        self.prepare_existing_message()
        class Fake:
            requests=0
            def request(self,method,path,*args):
                self.requests+=1
                raise ValueError("malformed untrusted response with potentially sensitive contents")
        result=Tick(self.db,self.cfg,client=Fake(),clock=lambda:2000).run(light=True)
        self.assertEqual(result["status"],"failed",result)
        self.assertIn({"code":"UNEXPECTED_TICK_ERROR","error_type":"ValueError"},
                      result["errors"])
        self.assertNotIn("sensitive",str(result))
        self.assertEqual(result["wakeup_staged"],1)
        self.assertEqual(self.db.read("SELECT status FROM patrol_runs")[0]["status"],"failed")

    def test_full_unexpected_exception_is_honest_failed_report(self):
        self.prepare_existing_message()
        class Fake:
            requests=0
            def request(self,method,path,*args):
                raise ValueError("malformed simulated response")
        result=Tick(self.db,self.cfg,client=Fake(),clock=lambda:2000).run()
        self.assertEqual(result["status"],"failed")
        self.assertEqual(self.db.read("SELECT status FROM patrol_runs")[0]["status"],"failed")


if __name__=="__main__":
    unittest.main()
