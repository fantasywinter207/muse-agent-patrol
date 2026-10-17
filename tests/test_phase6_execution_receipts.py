"""Host acceptance versus real worker execution: SQLite-only acceptance tests."""
import tempfile
import unittest
from pathlib import Path

from patrol.components.persistence.index import Database, StaleLease
from patrol.components.queue.index import Queue
from patrol.components.scheduling.index import Scheduler
from patrol.components.wakeup.index import Wakeups


class WakeupExecutionReceiptsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db=Database(Path(tmp.name)/"worker.sqlite3")
        self.queue=Queue(self.db)
        self.schedule=Scheduler(self.db)
        self.wake=Wakeups(self.db)

    def accepted_work(self):
        ident=self.queue.enqueue("human:execution","human_action",
                                 {"reason":"real worker needed"},1000,priority=85)
        self.schedule.sync_work(1000)
        self.assertEqual(self.wake.stage(1000,threshold=70),1)
        wake=self.wake.claim_next("hatch",1000)
        self.assertEqual(wake["source_id"],ident)
        ack=self.wake.acknowledge(wake["id"],"hatch",wake["lease_version"],
                                   1001,"accepted",{"host_receipt":"accepted"})
        self.assertEqual(ack["status"],"accepted")
        return ident,wake

    def test_acceptance_without_start_is_alerted_but_never_replayed(self):
        ident,wake=self.accepted_work()
        self.assertEqual(self.wake.execution_health(1200)["new_alerts"],0)
        health=self.wake.execution_health(1302)
        self.assertEqual(health["accepted_without_start"],1)
        self.assertEqual(health["new_alerts"],1)
        self.assertEqual(self.wake.execution_health(1303)["new_alerts"],0)
        self.assertEqual(self.db.read(
            "SELECT status FROM wakeup_requests WHERE id=?",(wake["id"],))[0]["status"],"accepted")
        self.assertIsNone(self.wake.claim_next("hatch",1304))
        self.assertEqual(self.wake.stage(1304),0)

    def test_started_progress_finished_need_correct_worker_and_generation(self):
        wid,wake=self.accepted_work()
        running=self.schedule.dispatch("muse",1002)["activity"]
        actid=running["id"]
        gen=running["lease_version"]
        with self.assertRaises(StaleLease):
            self.wake.record_execution(wake["id"],wake["lease_version"],
                    actid,gen,"wrong-worker","started",1003,{"proof":"invalid"})
        with self.assertRaises(StaleLease):
            self.wake.record_execution(wake["id"],wake["lease_version"]+1,
                    actid,gen,"muse","started",1003,{"proof":"invalid"})
        start=self.wake.record_execution(wake["id"],wake["lease_version"],
                    actid,gen,"muse","started",1003,{"host_session":"verified"})
        self.assertEqual(start["phase"],"started")
        self.assertFalse(start["remote_action_confirmed"])
        claim=self.queue.claim_specific(wid,"muse",1004,{"approved_by":"operator"})
        self.assertEqual(claim["status"],"running")
        progressed=self.wake.record_execution(wake["id"],wake["lease_version"],
                    actid,gen,"muse","progress",1005,{"task_step":"in flight"})
        self.assertEqual(progressed["phase"],"progress")
        self.queue.finish(claim,1006,evidence={"artifact":"verified"})
        self.schedule.complete(actid,"muse",gen,1007,{"work_proof":"done"})
        done=self.wake.record_execution(wake["id"],wake["lease_version"],
                    actid,gen,"muse","finished",1008,{"completion":"verified"})
        self.assertEqual(done["phase"],"finished")
        self.assertEqual(self.wake.execution_health(2000)["new_alerts"],0)
        self.assertEqual(self.db.read(
            "SELECT status FROM wakeup_execution_receipts WHERE wakeup_id=?",
            (wake["id"],))[0]["status"],"finished")

    def test_expired_start_gets_alert_without_false_finish(self):
        wid,wake=self.accepted_work()
        running=self.schedule.dispatch("muse",1002)["activity"]
        self.wake.record_execution(wake["id"],wake["lease_version"],
                running["id"],running["lease_version"],"muse","started",1003,
                {"host_session":"verified"})
        health=self.wake.execution_health(1304)
        self.assertEqual(health["started_without_progress"],1)
        self.assertEqual(health["new_alerts"],1)
        with self.assertRaises(StaleLease):
            self.wake.record_execution(wake["id"],wake["lease_version"],
                running["id"],running["lease_version"],"muse","progress",1304,
                {"proof":"stale generation"})
        self.assertEqual(self.db.read(
            "SELECT status FROM wakeup_execution_receipts WHERE wakeup_id=?",
            (wake["id"],))[0]["status"],"started")


if __name__=="__main__":
    unittest.main()
