import tempfile
import unittest
from pathlib import Path
from patrol.components.outbox.index import Outbox
from patrol.components.persistence.index import Database
from patrol.components.transport.index import Response


class Rejections(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=Database(Path(temp.name)/"outbox.sqlite3")
        self.o=Outbox(self.db)
        self.oid=self.o.stage("social:one","muse","send","chat_one",
                              {"content":"hello"},1000)

    def test_rate_limit_reuses_same_key(self):
        keys=[]
        def send(row):
            keys.append(row["idem_key"])
            return Response(429,{},retry_after=90)
        self.o.process(1000,lambda r:(True,None),send,lambda r,v:None)
        first=self.db.read("SELECT * FROM outbox WHERE id=?",(self.oid,))[0]
        self.assertEqual(first["status"],"retry_wait")
        self.assertEqual(first["next_attempt_at"],1090)
        self.o.process(1089,lambda r:(True,None),send,lambda r,v:None)
        self.assertEqual(len(keys),1)
        self.o.process(1090,lambda r:(True,None),send,lambda r,v:None)
        self.assertEqual(keys,[first["idem_key"]]*2)

    def test_duplicate_response_does_not_resend(self):
        calls=[]
        def send(row):
            calls.append(row["id"])
            return Response(409,{"error":{"code":"DUPLICATE_SOCIAL_CONTENT"}})
        self.o.process(1000,lambda r:(True,None),send,lambda r,v:None)
        state=self.db.read("SELECT status,error FROM outbox WHERE id=?",(self.oid,))[0]
        self.assertEqual(state["status"],"permanent_failed")
        self.assertIn("DUPLICATE_SOCIAL_CONTENT",state["error"])
        self.o.process(2000,lambda r:(True,None),send,lambda r,v:None)
        self.assertEqual(len(calls),1)


if __name__=="__main__":
    unittest.main()
