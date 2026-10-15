"""Phase 2 light/host wakeup contract regression tests.

Only fake HTTP, temporary SQLite and local CLI; no host Hook or CrewQuay writes.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from patrol.components.collection.index import Sync
from patrol.components.config.index import Config
from patrol.components.persistence.index import Database, StaleLease
from patrol.components.promises.index import Promises
from patrol.components.queue.index import Queue
from patrol.components.runtime.index import Tick, main
from patrol.components.transport.index import Response, BudgetExceeded
from patrol.components.wakeup.index import Wakeups


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/"phase2.sqlite3")
        self.cfg=Config(database=str(self.db.path),allowed_chat_ids=["owner"],
                        max_pages_per_source=1,light_max_pages_per_source=1,
                        max_light_chats=2)

    def verified(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('actor','quill')")

    def message(self, key="message-1", created=1000, now=1000):
        Sync(self.db).page("messages","owner","",[
            {"messageId":key,"cursor":"1","senderId":"owner","content":"hello",
             "createdAt":created}],"1",now)

    def test_light_tick_fresh_message_creates_one_wakeup_without_post(self):
        calls=[]
        class Fake:
            requests=0
            def request(self,method,path,*args):
                calls.append((method,path))
                self.requests+=1
                if path=="/api/v1/agents/me":
                    return Response(200,{"id":"quill"})
                if "/chats/owner/messages?" in path:
                    return Response(200,{"items":[{"messageId":"real1","cursor":"1","senderId":"owner",
                        "content":"need review","createdAt":1000}],"nextCursor":"1","hasMore":False})
                return Response(200,{"items":[],"nextCursor":"0","hasMore":False})
        client=Fake()
        a=Tick(self.db,self.cfg,client=client,clock=lambda:1000).run(light=True)
        self.assertEqual(a["mode"],"light")
        self.assertEqual(a["host_wakeups"],0)
        self.assertEqual(a["wakeup_staged"],1,a)
        self.assertFalse(any(method=="POST" for method,_ in calls))
        self.assertFalse(any("/api/v1/tasks" in path or "/api/v1/projects" in path
                             for _,path in calls))
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM wakeup_requests")[0]["n"],1)
        b=Tick(self.db,self.cfg,client=client,clock=lambda:1060).run(light=True)
        self.assertEqual(b["wakeup_staged"],0,b)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM work_items WHERE type='human_action'")[0]["n"],1)

    def test_old_history_not_woken_even_if_high_score(self):
        self.verified()
        self.message(created=10)
        Tick(self.db,self.cfg,clock=lambda:1000).run(light=True)
        self.assertEqual(self.db.read("SELECT * FROM wakeup_requests"),[])

    def test_stage_once_blocked_high_priority_then_fenced_ack(self):
        q=Queue(self.db)
        ident=q.enqueue("human:fixture","human_action",{"fixture":True},1000,priority=90)
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='blocked' WHERE id=?",(ident,))
        w=Wakeups(self.db)
        self.assertEqual(w.stage(1000),1)
        self.assertEqual(w.stage(1001),0)
        self.assertEqual(len(w.export(1001)["requests"]),1)
        wid=w.export(1001)["requests"][0]["id"]
        receipt=w.claim(wid,"host-bridge",1001)
        self.assertFalse(receipt["host_called"])
        with self.assertRaises(StaleLease):
            w.claim(wid,"another",1002)
        with self.assertRaises(StaleLease):
            w.acknowledge(wid,"host-bridge",0,1002,"accepted",{"host_receipt":"ok"})
        done=w.acknowledge(wid,"host-bridge",receipt["lease_version"],1002,
                           "accepted",{"host_receipt":"mock-receipt"})
        self.assertFalse(done["business_work_completed"])
        self.assertEqual(w.stage(1005),0)
        self.assertEqual(w.export(1005)["requests"],[])
        self.assertEqual(self.db.read("SELECT status FROM work_items WHERE id=?",(ident,))[0]["status"],"blocked")

    def test_unknown_after_crash_is_not_blindly_dispatched(self):
        q=Queue(self.db)
        ident=q.enqueue("human:unknown","human_action",{"fixture":True},1000,priority=80)
        w=Wakeups(self.db,lease_seconds=10)
        self.assertEqual(w.stage(1000),1)
        wid=w.export(1000)["requests"][0]["id"]
        w.claim(wid,"bridge",1000)
        self.assertEqual(w.recover(1011),1)
        self.assertEqual(w.export(1011)["requests"][0]["status"],"unknown")
        with self.assertRaises(StaleLease):
            w.claim(wid,"bridge",1011)
        with self.assertRaises(ValueError):
            w.reconcile_unknown(wid,1011,False,{"claim":"unknown"})
        w.reconcile_unknown(wid,1011,True,{"host_proved_absent":"fixture"})
        receipt=w.claim(wid,"bridge",1012)
        result=w.acknowledge(wid,"bridge",receipt["lease_version"],1013,
                             "confirmed_failed",{"host_failure":"fixture"})
        self.assertEqual(result["status"],"retry_wait")
        with self.assertRaises(StaleLease):
            w.claim(wid,"bridge",1020)

    def test_completed_work_is_obsolete_before_dispatch(self):
        q=Queue(self.db)
        ident=q.enqueue("human:obsolete","human_action",{"fixture":True},1000,priority=99)
        w=Wakeups(self.db)
        w.stage(1000)
        wid=w.export(1000)["requests"][0]["id"]
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='done' WHERE id=?",(ident,))
        self.assertEqual(w.claim(wid,"bridge",1001)["status"],"obsolete")
        self.assertEqual(w.export(1001)["requests"],[])

    def test_near_due_promise_signal_is_deduped_and_progress_can_rekey(self):
        promises=Promises(self.db)
        ident=promises.create("quill","owner","p1","do thing",1000,1050,1100,due_at=3000)
        w=Wakeups(self.db)
        self.assertEqual(w.stage(1000),0)
        self.assertEqual(w.stage(1010),1)
        self.assertEqual(w.stage(1011),0)
        promises.progress(ident,1055,{"artifact":"new proof"},1100)
        # The same deadline should not generate a duplicate delivery.
        self.assertEqual(w.stage(1056),0)
        promises.progress(ident,1070,{"artifact":"newer proof"},1160)
        self.assertEqual(w.stage(1070),1)

    def test_readonly_cli_export_and_offline_light(self):
        self.verified()
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc=main(["--db",str(self.db.path),"tick","--light","--offline"])
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(buf.getvalue())["mode"],"light")
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            rc=main(["--db",str(self.db.path),"wakeup-export"])
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(out.getvalue())["mode"],"read_only_no_host_dispatch")

    def test_network_budget_failure_preserves_local_work_and_wakeup(self):
        self.verified()
        self.message(created=1000)
        class Fake:
            requests=0
            def request(self,method,path,*args):
                self.requests+=1
                raise BudgetExceeded("simulated request cap")
        result=Tick(self.db,self.cfg,client=Fake(),clock=lambda:1100).run(light=True)
        self.assertEqual(result["status"],"deferred")
        self.assertGreaterEqual(result["completed_work"],1)
        self.assertEqual(result["wakeup_staged"],1)
        self.assertEqual(self.db.read("SELECT status FROM work_items WHERE type='inspect_event'")[0]["status"],"done")
        self.assertEqual(len(self.db.read("SELECT * FROM wakeup_requests")),1)

    def test_low_score_does_not_wake_host(self):
        q=Queue(self.db)
        q.enqueue("human:low","human_action",{"fixture":True},1000,priority=20)
        self.assertEqual(Wakeups(self.db).stage(1000,threshold=70),0)


if __name__=="__main__":
    unittest.main()
