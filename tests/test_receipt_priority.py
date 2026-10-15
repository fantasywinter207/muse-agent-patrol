"""Regressions for first-receipt freshness and pre-network reserved work.

Temporary SQLite and fake HTTP only; never authenticates or sends to CrewQuay.
"""
import json
import tempfile
import unittest
from pathlib import Path

from patrol.components.config.index import Config
from patrol.components.collection.index import Sync
from patrol.components.persistence.index import Database
from patrol.components.queue.index import MessageScores
from patrol.components.runtime.index import Tick
from patrol.components.transport.index import BudgetExceeded


class ReceiptAndBudget(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/"isolated.sqlite3")
        self.config=Config(database=str(self.db.path))

    def actor(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta VALUES('actor','quill')")

    def ingest(self, created, seen=1000, message_id="owner1", content="开始测试"):
        Sync(self.db).page("messages","owner-chat","",[
            {"messageId":message_id,"cursor":"1","senderId":"owner",
             "senderKind":"user","createdAt":created,"content":content}
        ],"1",seen)
        return self.db.read("SELECT id FROM events WHERE external_key=?",(message_id,))[0]["id"]

    def test_delayed_inspection_still_emits_human_work_without_renewing_attention(self):
        self.actor()
        event_id=self.ingest(990)
        initial=self.db.read("SELECT fresh_at_receipt,observed_at FROM message_receipts WHERE event_id=?",(event_id,))[0]
        self.assertEqual(initial,{"fresh_at_receipt":1,"observed_at":1000})
        result=Tick(self.db,self.config,clock=lambda:2300).run()
        inspector=self.db.read("SELECT status FROM work_items WHERE event_id=? AND type='inspect_event'",(event_id,))[0]
        self.assertEqual(inspector["status"],"done")
        work=self.db.read("SELECT type,status,event_id FROM work_items WHERE type='human_action'")
        self.assertEqual(len(work),1)
        self.assertEqual(work[0]["event_id"],event_id)
        self.assertIn(work[0]["status"],("pending","blocked"))
        self.assertEqual(self.db.read("SELECT * FROM attention_sessions"),[],
                         "delayed inspection cannot reactivate an old attention window")
        self.assertGreaterEqual(result["completed_work"],1)

    def test_old_message_is_not_exempted_by_late_scoring(self):
        self.actor()
        event_id=self.ingest(20)
        MessageScores(self.db).rate(event_id,99,"quill","fixture scoring",{"analysis":"fixture"},1001)
        self.assertEqual(self.db.read("SELECT fresh_at_receipt FROM message_receipts")[0]["fresh_at_receipt"],0)
        Tick(self.db,self.config,clock=lambda:2300).run()
        self.assertEqual(self.db.read("SELECT * FROM work_items WHERE type='human_action'"),[])

    def test_duplicate_collection_cannot_change_original_first_receipt(self):
        self.actor()
        event_id=self.ingest(990)
        self.ingest(990,seen=7000)
        rows=self.db.read("SELECT * FROM message_receipts WHERE event_id=?",(event_id,))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["fresh_at_receipt"],1)
        self.assertEqual(rows[0]["observed_at"],1000)

    def test_existing_scored_event_repairs_receipt_using_first_observed_at(self):
        self.actor()
        event_id=self.ingest(990)
        with self.db.transaction() as c:
            c.execute("DELETE FROM message_receipts WHERE event_id=?",(event_id,))
        self.assertTrue(MessageScores(self.db).first_receipt_fresh(event_id))
        rows=self.db.read("SELECT observed_at,fresh_at_receipt FROM message_receipts WHERE event_id=?",(event_id,))
        self.assertEqual(rows,[{"observed_at":1000,"fresh_at_receipt":1}])
        Tick(self.db,self.config,clock=lambda:2300).run()
        self.assertEqual(len(self.db.read("SELECT * FROM work_items WHERE type='human_action'")),1)

    def test_network_budget_exhaustion_cannot_preempt_reserved_local_work(self):
        self.actor()
        event_id=self.ingest(990)
        class BudgetClient:
            def __init__(self):
                self.calls=[]
            def request(self,method,path,*args):
                self.calls.append((method,path))
                raise BudgetExceeded("simulated network budget already exhausted")
        client=BudgetClient()
        result=Tick(self.db,self.config,client=client,clock=lambda:2300).run()
        self.assertEqual(result["status"],"deferred")
        self.assertGreaterEqual(result["completed_work"],1)
        self.assertEqual(self.db.read("SELECT status FROM work_items WHERE event_id=? AND type='inspect_event'",(event_id,))[0]["status"],"done")
        self.assertEqual(len(self.db.read("SELECT * FROM work_items WHERE type='human_action'")),1)
        self.assertEqual(client.calls,[("GET","/api/v1/agents/me")],
                         "local persisted work must run before any network request")

    def test_missing_actor_cannot_silently_consume_message(self):
        event_id=self.ingest(990)
        Tick(self.db,self.config,clock=lambda:1001).run()
        row=self.db.read("SELECT status FROM work_items WHERE event_id=? AND type='inspect_event'",(event_id,))[0]
        self.assertEqual(row["status"],"retry_wait")
        self.assertEqual(self.db.read("SELECT * FROM work_items WHERE type='human_action'"),[])
        self.assertFalse(self.db.read("SELECT 1 FROM attention_sessions"))


if __name__=="__main__":
    unittest.main()
