"""Production regression: a completed work item must not leave a stale
wakeup delivery lease permanently 'leased'.

Temporary SQLite only. No service credentials, remote writes, or host wakeups.
"""
import tempfile
import unittest
from pathlib import Path

from patrol.components.config.index import Config
from patrol.components.persistence.index import Database, StaleLease
from patrol.components.promises.index import Promises
from patrol.components.queue.index import Queue
from patrol.components.runtime.index import Tick
from patrol.components.wakeup.index import Wakeups


class ExpiredWakeupLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/"production-shape.sqlite3")
        self.cfg=Config(database=str(self.db.path))
        self.wakeups=Wakeups(self.db,lease_seconds=10)
        self.queue=Queue(self.db)

    def leased_work(self, logical="human:owner:fixture", priority=90):
        work_id=self.queue.enqueue(logical,"human_action",
                                   {"message":"fixture"},1000,priority=priority)
        self.assertEqual(self.wakeups.stage(1000),1)
        rows=self.wakeups.export(1000)["requests"]
        self.assertEqual(len(rows),1)
        wakeup_id=rows[0]["id"]
        receipt=self.wakeups.claim(wakeup_id,"host-A",1000)
        self.assertEqual(receipt["lease_version"],1)
        return work_id,wakeup_id,receipt

    def complete(self, work_id):
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='done' WHERE id=?",(work_id,))

    def record(self, wakeup_id):
        return self.db.read("SELECT * FROM wakeup_requests WHERE id=?",(wakeup_id,))[0]

    def test_expired_lease_with_completed_work_is_persistently_obsolete(self):
        work_id,wakeup_id,_=self.leased_work()
        self.complete(work_id)
        self.assertEqual(self.wakeups.recover(1011,detail=True),
                         {"unknown":0,"obsolete":1})
        state=self.record(wakeup_id)
        self.assertEqual(state["status"],"obsolete")
        self.assertIsNone(state["lease_owner"])
        self.assertIsNone(state["lease_until"])
        self.assertEqual(state["attempts"],1)
        self.assertEqual(self.wakeups.export(1011)["requests"],[])
        self.assertEqual(self.wakeups.stage(1012),0)
        self.assertEqual(self.wakeups.claim(wakeup_id,"host-B",1012)["status"],
                         self.record(wakeup_id)["status"])
        self.assertEqual(self.wakeups.recover(1013),0)

    def test_claim_also_retires_expired_lease_without_prior_recovery_tick(self):
        work_id,wakeup_id,_=self.leased_work()
        self.complete(work_id)
        result=self.wakeups.claim(wakeup_id,"host-B",1011)
        self.assertEqual(result["status"],"obsolete")
        self.assertEqual(self.record(wakeup_id)["status"],"obsolete")
        self.assertEqual(self.wakeups.export(1011)["requests"],[])

    def test_active_lease_is_not_cancelled_when_business_finishes(self):
        work_id,wakeup_id,receipt=self.leased_work()
        self.complete(work_id)
        with self.assertRaises(StaleLease):
            self.wakeups.claim(wakeup_id,"host-B",1005)
        self.assertEqual(self.record(wakeup_id)["status"],"leased")
        self.assertEqual(self.wakeups.recover(1005),0)
        delivered=self.wakeups.acknowledge(wakeup_id,"host-A",
                    receipt["lease_version"],1006,"accepted",
                    {"host_receipt":"already replied"})
        self.assertEqual(delivered["status"],"accepted")
        self.assertEqual(self.wakeups.recover(1011),0)
        self.assertEqual(self.record(wakeup_id)["status"],"accepted")

    def test_expired_lease_with_unfinished_work_remains_unknown(self):
        _,wakeup_id,_=self.leased_work()
        self.assertEqual(self.wakeups.recover(1011,detail=True),
                         {"unknown":1,"obsolete":0})
        self.assertEqual(self.record(wakeup_id)["status"],"unknown")
        with self.assertRaises(StaleLease):
            self.wakeups.claim(wakeup_id,"host-B",1012)
        self.assertEqual(self.wakeups.stage(1013),0)

    def test_completed_work_with_previous_unknown_is_retired_on_claim(self):
        work_id,wakeup_id,_=self.leased_work()
        self.wakeups.recover(1011)
        self.assertEqual(self.record(wakeup_id)["status"],"unknown")
        self.complete(work_id)
        self.assertEqual(self.wakeups.claim(wakeup_id,"host-B",1012)["status"],"obsolete")
        self.assertEqual(self.record(wakeup_id)["status"],"obsolete")

    def test_full_tick_recovers_expired_lease_after_completion(self):
        work_id,wakeup_id,_=self.leased_work()
        self.complete(work_id)
        report=Tick(self.db,self.cfg,clock=lambda:1011).run()
        self.assertEqual(report["wakeup_obsolete_recovered"],1)
        self.assertEqual(report["wakeup_unknown_recovered"],0)
        self.assertEqual(self.record(wakeup_id)["status"],"obsolete")

    def test_light_tick_recovers_expired_lease_after_completion(self):
        work_id,wakeup_id,_=self.leased_work()
        self.complete(work_id)
        report=Tick(self.db,self.cfg,clock=lambda:1011).run(light=True)
        self.assertEqual(report["wakeup_obsolete_recovered"],1)
        self.assertEqual(report["wakeup_unknown_recovered"],0)
        self.assertEqual(self.record(wakeup_id)["status"],"obsolete")

    def test_closed_promise_retires_expired_wakeup(self):
        promises=Promises(self.db)
        promise_id=promises.create("quill","owner","promise-1",
                        "deliver update",1000,1050,1130,due_at=3000)
        self.assertEqual(self.wakeups.stage(1010),1)
        wakeup_id=self.wakeups.export(1010)["requests"][0]["id"]
        self.wakeups.claim(wakeup_id,"host-A",1010)
        promises.complete(promise_id,1012,{"proof":"finished"})
        self.assertEqual(self.wakeups.recover(1021,detail=True),
                         {"unknown":0,"obsolete":1})
        self.assertEqual(self.record(wakeup_id)["status"],"obsolete")


if __name__=="__main__":
    unittest.main()
