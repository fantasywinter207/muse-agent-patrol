"""Phase 6.1: deterministic real SQLite scheduler and Outbox fencing tests."""
import tempfile
import unittest
from pathlib import Path

from patrol.components.persistence.index import Database, StaleLease
from patrol.components.scheduling.index import Scheduler
from patrol.components.outbox.index import Outbox
from patrol.components.queue.index import Queue
from patrol.components.config.index import Config
from patrol.components.runtime.index import Tick


class PreemptiveSchedulerTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db=Database(Path(directory.name)/"schedule.sqlite3")
        self.scheduler=Scheduler(self.db,lease_seconds=120)
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('actor','muse')")

    def add(self,key,kind="social",now=1000,score=50,weight=50,deadline=None):
        return self.scheduler.register(key,kind,now,priority_score=score,
                                       weight=weight,deadline=deadline)

    def test_deadline_beats_high_social_score_without_fixed_priority_lanes(self):
        self.add("social","social",score=75)
        task=self.add("urgent","work",score=45,deadline=1015)
        d=self.scheduler.dispatch("worker-1",1000)
        self.assertTrue(d["granted"])
        self.assertEqual(d["activity"]["id"],task)
        self.assertEqual(d["activity"]["lease_version"],1)
        self.assertLessEqual(d["activity"]["slice_until"],d["activity"]["lease_until"])

    def test_preemption_requires_cooperative_checkpoint_and_fences_old_generation(self):
        social=self.add("chat","social",score=60)
        first=self.scheduler.dispatch("muse-a",1000)
        self.assertEqual(first["activity"]["id"],social)
        generation=first["activity"]["lease_version"]
        self.add("task","work",now=1001,score=50,deadline=1020)
        decision=self.scheduler.dispatch("muse-b",1001)
        self.assertFalse(decision["granted"])
        self.assertEqual(decision["advice"],"checkpoint_at_safe_point")
        self.assertIn(decision["preemption"],("urgent_safe_point","medium_safe_point"))
        state=self.scheduler.snapshot(1001)["activities"]
        self.assertTrue(next(x for x in state if x["id"]==social)["preempt_requested"])
        with self.assertRaises(StaleLease):
            Outbox(self.db).stage("reply-too-late","muse","send","chat",
                {"content":"must not send"},1001,
                schedule_context={"id":social,"owner":"muse-a",
                                  "generation":generation})
        paused=self.scheduler.checkpoint(social,"muse-a",generation,1002,
                                          {"phase":"chat","safe_point":"model_finished"})
        self.assertTrue(paused["suspended"])
        self.assertEqual(paused["activity"]["checkpoint"]["phase"],"chat")
        with self.assertRaises(StaleLease):
            self.scheduler.checkpoint(social,"muse-a",generation,1003,{"phase":"stale"})
        next_task=self.scheduler.dispatch("muse-b",1003)
        self.assertTrue(next_task["granted"])
        self.assertEqual(next_task["activity"]["kind"],"work")
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM outbox")[0]["n"],0)

    def test_staged_action_remains_durable_after_safe_point_yield(self):
        social=self.add("chat","social",score=60)
        claimed=self.scheduler.dispatch("muse-a",1000)["activity"]
        guard={"id":social,"owner":"muse-a","generation":claimed["lease_version"]}
        ident=Outbox(self.db).stage("a-first","muse","send","chat",
                                    {"content":"authorized payload"},1001,
                                    producer_id="muse-a",schedule_context=guard)
        self.scheduler.checkpoint(social,"muse-a",claimed["lease_version"],1002,
                                   {"sent_intent":ident},voluntary=True)
        row=self.db.read("SELECT status FROM outbox WHERE id=?",(ident,))[0]
        self.assertEqual(row["status"],"prepared")
        audit=self.db.read("SELECT * FROM scheduled_outbox WHERE outbox_id=?",(ident,))
        self.assertEqual(audit[0]["lease_generation"],claimed["lease_version"])
        with self.assertRaises(StaleLease):
            Outbox(self.db).stage("another","muse","send","chat",
                                    {"content":"late"},1003,schedule_context=guard)

    def test_resume_preserves_checkpoint_and_increments_fence(self):
        ident=self.add("research","research",score=50)
        first=self.scheduler.dispatch("a",1000)["activity"]
        self.scheduler.checkpoint(ident,"a",first["lease_version"],1015,
                                  {"step":2,"summary":"partial"},voluntary=True)
        next_run=self.scheduler.dispatch("b",1016)["activity"]
        self.assertEqual(next_run["id"],ident)
        self.assertGreater(next_run["lease_version"],first["lease_version"])
        self.assertEqual(next_run["checkpoint"]["step"],2)
        with self.assertRaises(StaleLease):
            self.scheduler.complete(ident,"a",first["lease_version"],1017,{"proof":"stale"})
        done=self.scheduler.complete(ident,"b",next_run["lease_version"],1017,
                                      {"summary":"finished"})
        self.assertEqual(done["completed"],ident)
        self.assertFalse(done["remote_action_issued"])

    def test_recovered_expired_lease_never_silently_reuses_old_generation(self):
        ident=self.add("long-research","research")
        old=self.scheduler.dispatch("old",1000)["activity"]
        replacement=self.scheduler.dispatch("new",1120)
        self.assertTrue(replacement["granted"],replacement)
        self.assertEqual(replacement["lease_recovered"],1)
        self.assertEqual(replacement["activity"]["id"],ident)
        self.assertGreater(replacement["activity"]["lease_version"],old["lease_version"]+1)
        with self.assertRaises(StaleLease):
            self.scheduler.checkpoint(ident,"old",old["lease_version"],1121,
                                      {"state":"old"})
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM outbox")[0]["n"],0)

    def test_weighted_fairness_charges_real_slice_and_stays_bounded(self):
        low=self.add("small-weight",weight=10)
        r=self.scheduler.dispatch("a",1000)["activity"]
        self.assertEqual(r["id"],low)
        after=self.scheduler.checkpoint(low,"a",r["lease_version"],1030,
                                         {"state":"slice"})
        self.assertGreater(after["activity"]["virtual_runtime"],150)
        self.assertAlmostEqual(after["activity"]["cpu_seconds"],30)
        self.assertTrue(20<=after["activity"]["slice_until"]-1030<=120)

    def test_aging_can_raise_old_low_priority_activity(self):
        older=self.add("old",now=0,score=47)
        newer=self.add("new",now=2800,score=60)
        picked=self.scheduler.dispatch("a",2800)["activity"]
        self.assertEqual(picked["id"],older)
        self.assertEqual(picked["kind"],"social")

    def test_quantum_expiry_without_competitor_does_not_pause(self):
        ident=self.add("solo",score=40)
        first=self.scheduler.dispatch("muse",1000)["activity"]
        safe=self.scheduler.checkpoint(ident,"muse",first["lease_version"],1080,
                                        {"phase":"next turn"})
        self.assertFalse(safe["suspended"])
        self.assertEqual(safe["activity"]["status"],"running")
        self.assertEqual(safe["activity"]["lease_version"],first["lease_version"])

    def test_idempotent_register_and_immutable_keys(self):
        ident=self.add("key",score=42)
        self.assertEqual(self.add("key",score=42),ident)
        with self.assertRaises(ValueError):
            self.add("key",score=43)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM schedule_activities")[0]["n"],1)

    def test_linked_local_work_requires_independent_completion_evidence(self):
        work=Queue(self.db).enqueue("human:task","human_action",
                                    {"reason":"task needed"},1000,priority=82,start_by=1050)
        self.assertEqual(self.scheduler.sync_work(1001),1)
        self.assertEqual(self.scheduler.sync_work(1002),0)
        selected=self.scheduler.dispatch("worker",1003)["activity"]
        self.assertEqual(selected["work_id"],work)
        with self.assertRaises(ValueError):
            self.scheduler.complete(selected["id"],"worker",
                                    selected["lease_version"],1004,{"done":True})
        # The scheduler does not claim or finish the original work itself.
        self.assertEqual(self.db.read("SELECT status FROM work_items WHERE id=?",(work,))[0]["status"],"pending")
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='done' WHERE id=?",(work,))
        self.assertEqual(self.scheduler.complete(selected["id"],"worker",
                         selected["lease_version"],1004,{"work_proof":"separate"} )["completed"],selected["id"])

    def test_cancel_only_nonrunning_and_assess_does_not_change_owner(self):
        ident=self.add("unused")
        before=self.scheduler.assess(1000)
        self.assertEqual(before["advice"],"ready_to_dispatch")
        self.scheduler.cancel(ident,1001,"no longer relevant")
        self.assertEqual(self.scheduler.assess(1001)["advice"],"idle")
        active=self.add("active",now=1002)
        claimed=self.scheduler.dispatch("worker",1002)["activity"]
        with self.assertRaises(ValueError):
            self.scheduler.cancel(active,1003,"cannot kill remote work")
        self.assertEqual(claimed["status"],"running")

    def test_tick_imports_work_but_does_not_invoke_external_model(self):
        Queue(self.db).enqueue("human:auto","human_action",
                              {"reason":"owner work"},1000,priority=82)
        report=Tick(self.db,Config(database=str(self.db.path)),
                    clock=lambda:1000,monotonic=lambda:0).run(light=True)
        self.assertEqual(report["scheduler"]["work_registered"],1)
        self.assertFalse(report["scheduler"]["model_invoked"])
        self.assertFalse(report["scheduler"]["remote_action_issued"])
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM schedule_activities")[0]["n"],1)
        self.assertEqual(self.db.read("SELECT status FROM work_items WHERE logical_key='human:auto'")[0]["status"],"pending")


if __name__=="__main__":
    unittest.main()
