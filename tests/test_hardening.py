"""Additional regressions found during implementation hardening; no live API."""
import json
import unittest
from pathlib import Path
from test_patrol import Fixture
from patrol.components.collection.index import Sync, Collector
from patrol.components.config.index import Config
from patrol.components.migration.index import migrate_legacy
from patrol.components.persistence.index import Database
from patrol.components.promises.index import Promises
from patrol.components.runtime.index import Tick
from patrol.components.transport.index import Response


class Hardening(Fixture):
    def test_recursive_credential_migration_denied_before_any_import(self):
        for n,obj in enumerate((
            {"api_key":"SYNTHETIC_MARKER"},
            {"settings":{"credentials":{"apiKey":"SYNTHETIC_MARKER"}}},
            {"list":[{"Access-Token":"SYNTHETIC_MARKER"}]},
            {"log":"Authorization: Bearer SYNTHETIC_MARKER"},
        )):
            folder=self.root/("old"+str(n))
            folder.mkdir()
            (folder/"state.json").write_text(json.dumps({"lastProcessedCursor":"42",**obj}),encoding="utf-8")
            db=Database(self.root/("migration"+str(n)+".sqlite3"))
            result=migrate_legacy(db,folder,100,True)
            self.assertFalse(result["imported"])
            self.assertEqual(db.read("SELECT * FROM legacy_records"),[])
            self.assertEqual(db.read("SELECT * FROM migrations"),[])

    def test_idle_tick_does_not_read_credentials_or_network(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES('actor','fixture-actor')")
        for source in ("inbox","chats","tasks","updates","events"):
            Sync(self.db).page(source,"global","",[],"0",90,next_at=1000)
        class NeverClient:
            def request(self,*args):
                raise AssertionError("idle must not do HTTP")
        report=Tick(self.db,self.config,NeverClient(),clock=lambda:100).run()
        self.assertEqual(report["status"],"ok")

    def test_inbox_without_hasmore_follows_nextcursor_until_empty(self):
        calls=[]
        def request(method,path):
            calls.append(path)
            return Response(200,{"items":[{"seq":1}] if len(calls)==1 else [],"nextCursor":1,"unreadCount":1})
        sync=Sync(self.db)
        c=Collector(sync,request,self.config)
        self.assertTrue(c.pages("inbox","global","/api/v1/inbox",100))
        self.assertEqual(len(calls),2)
        self.assertIsNone(sync.state("inbox","global")["target"])

    def test_nonfinite_deadline_rejected(self):
        for value in (float("nan"),float("inf"),True):
            with self.assertRaises(ValueError):
                Promises(self.db).create("w","o","m","x",100,105,110,due_at=value)

    def test_unrelated_identity_cannot_approve_extension(self):
        p=Promises(self.db)
        ident=p.create("w","owner","m","x",100,105,110,due_at=120)
        p.request_extension(ident,150,"dependency",{"fixture":True},115)
        with self.assertRaises(ValueError):
            p.approve_extension(ident,150,"unrelated",{"fixture":True},116)

    def test_manual_promise_completion_finishes_linked_work(self):
        p=Promises(self.db)
        ident=p.create("w","o","m","x",100,105,110,due_at=120)
        p.complete(ident,115,{"artifact":"verified-fixture"})
        self.assertEqual(self.db.read("SELECT status FROM work_items")[0]["status"],"done")

    def test_completed_queue_keeps_handled_monotonic(self):
        sync=Sync(self.db)
        sync.page("events","global","s",[{"eventId":"e1","cursor":"101","type":"event.redacted"}],"101",100)
        report=Tick(self.db,self.config,clock=lambda:100).run()
        # Offline resource pointers remain pending, never falsely handled.
        self.assertEqual(sync.state("events","global")["handled"],"0")
        self.assertEqual(sync.state("events","global")["received"],"101")


if __name__=="__main__":
    unittest.main()
