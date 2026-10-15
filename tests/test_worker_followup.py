"""Post-fix boundary checks with fake HTTP only."""
import json
import tempfile
import unittest
from pathlib import Path
from patrol.components.persistence.index import Database
from patrol.components.collection.index import Sync, Collector
from patrol.components.config.index import Config
from patrol.components.transport.index import Response

class Followup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=Database(self.root/'db.sqlite3');self.cfg=Config(database=str(self.db.path))
    def tearDown(self):self.tmp.cleanup()

    def test_active_interval_does_not_override_retry_after(self):
        sync=Sync(self.db);calls=[]
        def fake(m,p):
            calls.append(p)
            if '/chat_demo/messages?' in p:return Response(429,{},retry_after=3600)
            return Response(200,{'items':[],'nextCursor':'0','hasMore':False})
        collector=Collector(sync,fake,self.cfg)
        collector.pages('messages','chat_demo','/api/v1/chats/chat_demo/messages',100)
        before=sync.state('messages','chat_demo');n=len(calls)
        collector.resources(161,chat_ids=['chat_demo'])
        repeated=[p for p in calls[n:] if '/chat_demo/messages?' in p]
        print('ACTIVE_RETRY_AFTER',json.dumps({'before':before,'request_time':161,'repeat_requests':repeated,'after':sync.state('messages','chat_demo')}))
        self.assertEqual(repeated,[],'active scan bypassed persisted Retry-After 3600 seconds')

    def test_visible_chat_envelope_is_included_in_resource_reconciliation(self):
        calls=[]
        def fake(m,p):
            calls.append(p)
            if p.startswith('/api/v1/chats?'):
                return Response(200,{'items':[{'conversationId':'chat_visible','cursor':'1'}],'nextCursor':'1','hasMore':False})
            return Response(200,{'items':[],'nextCursor':'0','hasMore':False})
        collector=Collector(Sync(self.db),fake,self.cfg)
        collector.resources(100)
        chat_calls=[p for p in calls if '/chat_visible/messages?' in p]
        print('VISIBLE_CHAT_ENVELOPE',json.dumps({'all_GETs':calls,'visible_chat_message_GETs':chat_calls}))
        self.assertTrue(chat_calls,'supported envelope form persisted chat but skipped its messages')

if __name__=='__main__':unittest.main(verbosity=2)
