"""Independent, credential-free behavioral checks via public component indexes."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from patrol.components.persistence.index import Database
from patrol.components.config.index import Config
from patrol.components.collection.index import Sync, Collector
from patrol.components.attention.index import Attention
from patrol.components.outbox.index import Outbox
from patrol.components.runtime.index import Tick
from patrol.components.transport.index import Response
from patrol.components.migration.index import migrate_legacy
from patrol.components.promises.index import Promises


class IndependentChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root/'state.sqlite3')
        self.cfg = Config(database=str(self.db.path), allowed_chat_ids=['chat_demo'])

    def tearDown(self):
        self.tmp.cleanup()

    def test_active_poll_overrides_normal_scan_deadline(self):
        sync = Sync(self.db)
        requests = []
        def get(method, path):
            requests.append(path)
            return Response(200, {'items':[], 'nextCursor':'0', 'hasMore':False})
        collector = Collector(sync, get, self.cfg)
        collector.pages('messages','chat_demo','/api/v1/chats/chat_demo/messages',100)
        attention = Attention(self.db,self.cfg)
        attention.observe('chat_demo','new-meaningful',101,101)
        due = attention.due(161)
        self.assertEqual(due[0]['state'],'active')
        before = len(requests)
        collector.resources(161,chat_ids=['chat_demo'])
        calls = [p for p in requests[before:] if '/chat_demo/messages?' in p]
        print('ACTIVE_POLL',json.dumps({'active_at':161,'saved_message_next_at':sync.state('messages','chat_demo')['next_at'],'new_message_requests':calls}))
        self.assertEqual(len(calls),1,'active scan due but normal 600s deadline suppresses request')

    def test_unknown_outbox_finds_already_collected_remote_message(self):
        message={'id':'msg_1','cursor':'10','senderId':'actor','content':'hello','createdAt':100}
        class FakeClient:
            def __init__(self): self.paths=[]
            def request(self,method,path,*args):
                self.paths.append(path)
                after=int(parse_qs(urlsplit(path).query)['after'][0])
                return Response(200,{'items':[message] if after<10 else [],'nextCursor':'10','hasMore':False})
        client=FakeClient()
        sync=Sync(self.db)
        sync.page('messages','chat_demo','',[message],'10',101,next_at=701)
        out=Outbox(self.db)
        ident=out.stage('send-one','actor','send','chat_demo',{'content':'hello'},99)
        with self.db.transaction() as c:
            c.execute("UPDATE outbox SET status='unknown',remote_id='msg_1' WHERE id=?",(ident,))
        tick=Tick(self.db,self.cfg,client=client,clock=lambda:102)
        out.process(102,tick.can_send,tick.send,tick.verify)
        row=self.db.read('SELECT status,remote_id FROM outbox WHERE id=?',(ident,))[0]
        print('UNKNOWN_READBACK',json.dumps({'state':row,'GETs':client.paths,'durable_message_exists':True}))
        self.assertEqual(row['status'],'confirmed','known success is never found after received cursor already passed it')

    def test_identical_bytes_in_different_migration_sources_not_deduped(self):
        source=self.root/'legacy'; source.mkdir()
        value='{"lastProcessedCursor":"42","streamId":"s"}'
        (source/'state.json').write_text(value,encoding='utf-8')
        (source/'promises.jsonl').write_text(value,encoding='utf-8')
        result=migrate_legacy(self.db,source,100,stopped=True)
        print('MIGRATION_SOURCE_COLLISION',json.dumps({'result':result,'event_state':Sync(self.db).state('events','global')}))
        self.assertEqual(Sync(self.db).state('events','global')['received'],'42','state file skipped because another filename has same hash')

    def test_read_cursor_requires_durable_message_coverage(self):
        cfg=Config(database=str(self.db.path),allow_writes=True,allowed_chat_ids=['chat_demo'])
        tick=Tick(self.db,cfg)
        tick.actor='actor'
        out=Outbox(self.db)
        ident=out.stage('mark-read','actor','read','chat_demo',{'cursor':'999'},100)
        row=self.db.read('SELECT * FROM outbox WHERE id=?',(ident,))[0]
        row['payload']=json.loads(row['payload'])
        allowed,reason=tick.can_send(row)
        print('READ_WITHOUT_COVERAGE',json.dumps({'allowed':allowed,'reason':reason,'message_state':Sync(self.db).state('messages','chat_demo')}))
        self.assertFalse(allowed,'marks unseen 999 read although durable received/handled are both 0')

    def test_extension_does_not_accept_deadline_before_start(self):
        p=Promises(self.db)
        ident=p.create('worker','owner','source','deliver',100,110,120,due_at=200)
        rejected=False
        try:
            p.request_extension(ident,50,'need change',{'note':'e'},130)
            p.approve_extension(ident,50,'reviewer',{'approval':'e'},131)
        except ValueError:
            rejected=True
        row=self.db.read('SELECT start_by,original_due_at,due_at FROM promises WHERE id=?',(ident,))[0]
        print('INVALID_EXTENSION',json.dumps({'rejected':rejected,'row':row}))
        self.assertTrue(rejected,'extension bypasses create deadline >= start invariant')

    def test_fresh_ws_tick_reads_live_frames_after_rebuild(self):
        class FakeWS:
            def __init__(self):self.reads=0; self.closed=False
            def recv(self):
                self.reads+=1
                return json.dumps({'eventId':'live_11','cursor':'11','type':'task.updated','resourceId':'tsk_demo'}) if self.reads==1 else ''
            def close(self):self.closed=True
        class FakeClient:
            secret=staticmethod(lambda:'not-a-real-key')
            def request(self,method,path,*args):
                if path=='/api/v1/agents/me':return Response(200,{'id':'actor'})
                return Response(200,{'items':[],'nextCursor':'0','hasMore':False})
        ws=FakeWS()
        tick=Tick(self.db,self.cfg,FakeClient(),clock=lambda:100,ws_factory=lambda c,s:(ws,{'streamId':'s','waterline':'10'}))
        report=tick.run(websocket=True)
        print('FRESH_WS',json.dumps({'reads':ws.reads,'closed':ws.closed,'report':report,'live_events':self.db.read("SELECT external_key FROM events WHERE external_key='live_11'")}))
        self.assertGreater(ws.reads,0,'fresh/rebuilt WS ready has available live frame but closes without recv')


if __name__=='__main__':
    unittest.main(verbosity=2)
