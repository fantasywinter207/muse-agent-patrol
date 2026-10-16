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
        # An immutable, already-staged action may be retrieved even after its
        # original scheduler generation has been fenced by a safe-point yield.
        duplicate=Outbox(self.db).stage("a-first","muse","send","chat",
                                       {"content":"authorized payload"},1003,
                                       schedule_context=guard)
        self.assertEqual(duplicate,ident)
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
        self.assertEqual(after["activity"]["slice_until"],r["slice_until"])

    def test_repeated_checkpoints_do_not_extend_a_competitive_slice(self):
        first_id=self.add("session-a",score=52,weight=50)
        second_id=self.add("session-b",score=52,weight=50)
        active=self.scheduler.dispatch("worker-a",1000)["activity"]
        original_end=active["slice_until"]
        for at in (1010,1020,1030,1040):
            saved=self.scheduler.checkpoint(active["id"],"worker-a",
                         active["lease_version"],at,{"progress":at})
            self.assertFalse(saved["suspended"])
            self.assertEqual(saved["activity"]["slice_until"],original_end)
        decision=self.scheduler.dispatch("worker-b",original_end)
        self.assertEqual(decision["advice"],"checkpoint_at_safe_point")
        yielded=self.scheduler.checkpoint(active["id"],"worker-a",
                     active["lease_version"],original_end+0.01,
                     {"progress":"yield"})
        self.assertTrue(yielded["suspended"])
        next_activity=self.scheduler.dispatch("worker-b",original_end+0.02)["activity"]
        self.assertIn(next_activity["id"],(first_id,second_id))
        self.assertNotEqual(next_activity["id"],active["id"])

    def test_time_slice_fits_a_short_configured_lease(self):
        short=Scheduler(self.db,lease_seconds=30)
        self.add("short-lease",weight=100)
        active=short.dispatch("worker",1000)["activity"]
        self.assertLess(active["slice_until"],active["lease_until"])

    def test_retry_wait_and_blocked_work_cannot_dispatch_early(self):
        work=Queue(self.db).enqueue("human:delayed","human_action",
                                    {"reason":"retry"},1000,priority=95)
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='retry_wait',next_action_at=2000 "
                      "WHERE id=?",(work,))
        self.scheduler.sync_work(1000)
        self.assertIsNone(self.scheduler.assess(1001)["candidate"])
        self.assertFalse(self.scheduler.dispatch("worker",1001)["granted"])
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='blocked' WHERE id=?",(work,))
        self.assertIsNone(self.scheduler.assess(2100)["candidate"])
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='retry_wait' WHERE id=?",(work,))
        self.assertTrue(self.scheduler.dispatch("worker",2100)["granted"])

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

    def test_equal_priority_activities_rotate_after_slice(self):
        a=self.add("social-a",score=52,weight=50)
        b=self.add("social-b",score=52,weight=50)
        first=self.scheduler.dispatch("worker-a",1000)["activity"]
        self.assertIn(first["id"],(a,b))
        # Competing equal-value work cannot force an immediate context switch.
        early=self.scheduler.dispatch("worker-b",1001)
        self.assertFalse(early["granted"])
        self.assertEqual(early["advice"],"continue")
        end=first["slice_until"]
        gate=self.scheduler.dispatch("worker-b",end)
        self.assertFalse(gate["granted"])
        self.assertEqual(gate["advice"],"checkpoint_at_safe_point")
        result=self.scheduler.checkpoint(first["id"],"worker-a",first["lease_version"],
                                         end+0.01,{"safe_point":"slice boundary"})
        self.assertTrue(result["suspended"])
        second=self.scheduler.dispatch("worker-b",end+0.02)["activity"]
        self.assertNotEqual(second["id"],first["id"])

    def test_external_work_completion_fences_ongoing_activity(self):
        work=Queue(self.db).enqueue("human:finished-elsewhere","human_action",
                                    {"reason":"work"},1000,priority=75)
        self.scheduler.sync_work(1000)
        active=self.scheduler.dispatch("owner",1001)["activity"]
        with self.db.transaction() as c:
            c.execute("UPDATE work_items SET status='done' WHERE id=?",(work,))
        self.scheduler.sync_work(1002)
        state=self.db.read("SELECT status,lease_version FROM schedule_activities WHERE id=?",
                           (active["id"],))[0]
        self.assertEqual(state["status"],"completed")
        self.assertGreater(state["lease_version"],active["lease_version"])
        with self.assertRaises(StaleLease):
            Outbox(self.db).stage("late","muse","send","chat",{"content":"stale"},1002,
                                 schedule_context={"id":active["id"],
                                   "owner":"owner","generation":active["lease_version"]})

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

    def test_reconciliation_uses_latest_muse_score_not_old_work_priority(self):
        with self.db.transaction() as c:
            c.execute("""INSERT INTO events(source,scope,stream,external_key,
                         payload,observed_at) VALUES
                         ('messages','owner','','msg1','{}',1000)""")
            event_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("""INSERT INTO message_scores(event_id,base_score,baseline_at)
                         VALUES(?,?,?)""",(event_id,25,1000))
        ident=Queue(self.db).enqueue("human:score-updated","human_action",
                                     {"event_id":event_id},1000,
                                     priority=25,event_id=event_id)
        self.assertEqual(self.scheduler.sync_work(1001),1)
        a=self.db.read("SELECT base_priority FROM schedule_activities WHERE work_id=?",
                       (ident,))[0]
        self.assertEqual(a["base_priority"],25)
        with self.db.transaction() as c:
            c.execute("UPDATE message_scores SET muse_score=92 WHERE event_id=?",
                      (event_id,))
        self.assertEqual(self.scheduler.sync_work(1002),0)
        b=self.db.read("SELECT base_priority FROM schedule_activities WHERE work_id=?",
                       (ident,))[0]
        self.assertEqual(b["base_priority"],92)

    def test_bounded_work_import_advances_past_old_already_registered_rows(self):
        for i in range(5):
            Queue(self.db).enqueue("human:backlog-"+str(i),"human_action",
                                   {"index":i},1000+i,priority=70)
        self.assertEqual(self.scheduler.sync_work(1100,limit=2),2)
        self.assertEqual(self.scheduler.sync_work(1101,limit=2),2)
        self.assertEqual(self.scheduler.sync_work(1102,limit=2),1)
        self.assertEqual(self.scheduler.sync_work(1103,limit=2),0)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM schedule_activities")[0]["n"],5)

    def test_tick_imports_work_but_does_not_invoke_external_model(self):
        Queue(self.db).enqueue("human:auto","human_action",
                              {"reason":"owner work"},1000,priority=82)
        report=Tick(self.db,Config(database=str(self.db.path)),
                    clock=lambda:1000,monotonic=lambda:0).run(light=True)
        self.assertEqual(report["scheduler"]["work_registered"],1)
        self.assertFalse(report["scheduler"]["model_invoked"])
        self.assertFalse(report["scheduler"]["remote_action_issued"])
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM schedule_activities")[0]["n"],1)
        self.assertEqual(self.db.read("SELECT status FROM work_items WHERE logical_key='human:auto'")[0]["status"],"blocked")


if __name__=="__main__":
    unittest.main()
