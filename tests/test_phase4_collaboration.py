"""Phase 4: platform collaboration uses typed, permission-scoped Outbox actions.

Temporary SQLite and fake API only; no production credentials or writes.
"""
import tempfile
import unittest
from pathlib import Path

from patrol.components.config.index import Config
from patrol.components.executor.index import ActionSender
from patrol.components.outbox.index import Outbox
from patrol.components.persistence.index import Database
from patrol.components.transport.index import Response


class FakePlatform:
    def __init__(self):
        self.calls=[]
        self.remote_id="msg_remote_one"

    def request(self,method,path,payload=None,idem_key=None):
        self.calls.append((method,path,payload,idem_key))
        if method=="GET" and path=="/api/v1/agents/me":
            return Response(200,{"id":"actor"})
        if method=="GET" and path=="/api/v1/tasks/task-4":
            return Response(200,{"id":"task-4","projectId":"project-1"})
        if method=="GET" and path=="/api/v1/rooms/room-1":
            return Response(200,{"roomId":"room-1","projectId":"project-1"})
        if method=="GET" and path=="/api/v1/tasks/unapproved":
            return Response(200,{"id":"unapproved","projectId":"project-2"})
        if method=="POST":
            if "/tasks/" in path:
                return Response(201,{"id":self.remote_id,"actorId":"actor"})
            if "/rooms/" in path:
                return Response(200,{"messageId":self.remote_id,"senderAgentId":"actor"})
            if "/projects/" in path:
                return Response(201,{"id":self.remote_id,"actorId":"actor"})
        if method=="GET" and "after=" in path and "limit=100" in path:
            if "/rooms/" in path:
                item={"messageId":self.remote_id,"senderAgentId":"actor",
                      "content":"Hello team","cursor":"5"}
            else:
                item={"id":self.remote_id,"actorId":"actor","content":"Hello team",
                      "cursor":5}
            return Response(200,{"items":[item],"nextCursor":"5","hasMore":False})
        return Response(404,{"error":"not found"})


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=Database(Path(temp.name)/"phase4.sqlite3")
        with self.db.transaction() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('actor','actor')")
        self.client=FakePlatform()
        self.config=Config(database=str(self.db.path),allow_writes=True,
                           allowed_chat_ids=["owner"],
                           project_ids=["project-1"],
                           allowed_project_action_ids=["project-1"])
        self.outbox=Outbox(self.db)

    def stage(self,action,target,logical=None):
        return self.outbox.stage(logical or action,"actor",action,target,
                                 {"content":"Hello team","mentions":[]},1000,
                                 producer_id="muse-worker")

    def test_project_task_thread_room_are_distinct_authorized_routes(self):
        for action,target,path in [
            ("project_message","project-1","/api/v1/projects/project-1/messages"),
            ("task_thread_reply","task-4","/api/v1/tasks/task-4/thread/messages"),
            ("room_message","room-1","/api/v1/rooms/room-1/messages"),
        ]:
            with self.subTest(action=action):
                self.client=FakePlatform()
                ident=self.stage(action,target)
                result=ActionSender(self.db,self.config,self.client,clock=lambda:1000).drain_once(
                    limit=1,action_id=ident,lane="collaboration")
                self.assertEqual(result["item_status"],"confirmed",result)
                posts=[x for x in self.client.calls if x[0]=="POST"]
                self.assertEqual(len(posts),1)
                self.assertEqual(posts[0][1],path)
                self.assertEqual(posts[0][2],{"content":"Hello team","mentions":[]})
                row=self.db.read("SELECT status,remote_id,attempts,result FROM outbox WHERE id=?",(ident,))[0]
                self.assertEqual(row["status"],"confirmed")
                self.assertEqual(row["remote_id"],"msg_remote_one")
                self.assertEqual(row["attempts"],1)
                self.assertIn("sha256",row["result"])
                again=ActionSender(self.db,self.config,self.client,clock=lambda:1001).drain_once(
                    limit=1,action_id=ident)
                self.assertEqual(again["item_status"],"confirmed")
                self.assertEqual(len([x for x in self.client.calls if x[0]=="POST"]),1)

    def test_read_only_project_scope_never_grants_write_scope(self):
        ident=self.stage("project_message","project-1")
        cfg=Config(database=str(self.db.path),allow_writes=True,
                   project_ids=["project-1"])
        report=ActionSender(self.db,cfg,self.client,clock=lambda:1000).drain_once(1,ident)
        self.assertEqual(report["blocked"],1,report)
        self.assertEqual(len([x for x in self.client.calls if x[0]=="POST"]),0)
        self.assertEqual(self.db.read("SELECT status FROM outbox WHERE id=?",(ident,))[0]["status"],"prepared")

    def test_task_scope_checked_from_authenticated_api_not_target_guess(self):
        ident=self.stage("task_thread_reply","unapproved")
        report=ActionSender(self.db,self.config,self.client,clock=lambda:1000).drain_once(1,ident)
        self.assertEqual(report["blocked"],1)
        self.assertTrue(any(x[1]=="/api/v1/tasks/unapproved" for x in self.client.calls))
        self.assertFalse(any(x[0]=="POST" for x in self.client.calls))

    def test_unknown_collaboration_result_gets_readback_only(self):
        ident=self.stage("task_thread_reply","task-4")
        with self.db.transaction() as c:
            c.execute("UPDATE outbox SET status='unknown',remote_id=? WHERE id=?",
                      ("msg_remote_one",ident))
        report=ActionSender(self.db,self.config,self.client,clock=lambda:1000).drain_once(1,ident)
        self.assertEqual(report["item_status"],"confirmed",report)
        self.assertFalse(any(x[0]=="POST" for x in self.client.calls))
        self.assertTrue(any("/thread?after=" in x[1] for x in self.client.calls))

    def test_collaboration_lanes_are_independent(self):
        ident=self.stage("project_message","project-1")
        report=ActionSender(self.db,self.config,self.client,clock=lambda:1000).drain_once(
            limit=3,lane="chat")
        self.assertEqual(report["outbox_attempts"],[])
        self.assertEqual(self.db.read("SELECT status FROM outbox WHERE id=?",(ident,))[0]["status"],"prepared")
        report=ActionSender(self.db,self.config,self.client,clock=lambda:1000).drain_once(
            limit=3,lane="collaboration")
        self.assertEqual(report["confirmed"],1,report)

    def test_payload_validation_and_actor_binding(self):
        with self.assertRaises(ValueError):
            self.outbox.stage("bad","actor","task_thread_reply","task-4",
                              {"content":"Hello team","mentions":["",42]},1000)
        with self.assertRaises(ValueError):
            self.outbox.stage("wrong","worker","project_message","project-1",
                              {"content":"Hello team","mentions":[]},1000)
        self.assertEqual(self.db.read("SELECT count(*) AS n FROM outbox")[0]["n"],0)


if __name__=="__main__":
    unittest.main()
