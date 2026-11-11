"""Synthetic queue, delivery and loopback capability-server regression tests."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from test_runtime import _TEST_ROOT
from media_bot import core as c,config,limits,links,preferences,worker as w


class Features(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='features-',dir=_TEST_ROOT);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for patcher in (patch.object(c,'DATA',self.root),patch.dict(os.environ,{
                'MEDIA_BOT_DATA':str(self.root),'MEDIA_BOT_ALLOWED_USERS':'123,456',
                'MEDIA_BOT_API_BASE':'http://127.0.0.1:8081','MEDIA_BOT_DOWNLOAD_BASE_URL':'https://downloads.example.test/files',
                'MEDIA_BOT_DOWNLOAD_PORT':'8090'})):
            patcher.start();self.addCleanup(patcher.stop)
        c.initialize();w.STOP.clear()
    def job(self,key='test',url='https://x.com/example/status/123',prefs=None):
        return c.job_by_id(c.enqueue(key,123,url,preferences=prefs))
    def file(self,job,size=8):
        root=self.root/'media'/job['id'];root.mkdir(parents=True,exist_ok=True)
        path=root/'fixture.bin'
        with path.open('wb') as stream:stream.truncate(size)
        w.add_files(job,[dict(path=str(path),name=path.name,size=size)])
        with c.db() as db:return dict(db.execute('SELECT * FROM files WHERE job=?',(job['id'],)).fetchone())
    def test_endpoint_validation_and_runtime_help(self):
        for endpoint in ('http://localhost:8081','http://192.0.2.1:8081','http://127.0.0.1:8081/path','https://api.telegram.org.evil.test'):
            with patch.dict(os.environ,MEDIA_BOT_API_BASE=endpoint),self.assertRaises(ValueError):limits.telegram_api_base()
        self.assertIn('500 MB',w.help_text_for_runtime());self.assertIn('2000 MB',w.help_text_for_runtime())
        self.assertIn('2 小時',w.help_text_for_runtime())
        with patch.dict(os.environ,MEDIA_BOT_API_BASE='https://api.telegram.org',MEDIA_BOT_DOWNLOAD_BASE_URL=''):
            self.assertIn('49 MiB',w.help_text_for_runtime());self.assertIn('尚未設定',w.help_text_for_runtime())
    def test_snapshot_defaults_no_prompt_and_ownership(self):
        prefs=preferences.load(123)
        job=self.job(url='https://instagram.com/p/example/',prefs=prefs)
        preferences.save(123,'instagram','content','stories_all')
        self.assertEqual(job['mode'],'post');self.assertEqual(json.loads(job['preferences']),prefs)
        self.assertEqual(preferences.load(456)['instagram']['content'],'post_all')
        again=self.job(url='https://instagram.com/p/example/',prefs=preferences.load(123))
        self.assertEqual(again['preferences'],job['preferences'])
        with patch.object(w,'telegram',return_value={'message_id':3}),patch.object(w,'choice') as choice:
            w.handle_update({'update_id':9,'message':{'chat':{'id':456,'type':'private'},'from':{'id':456},'text':'https://instagram.com/p/example/'}})
        choice.assert_not_called()
        with self.assertRaises(PermissionError):preferences.save(999,'instagram','content','post_all')
    def test_batch_notice_is_atomic(self):
        key=c.enqueue_batch('batch',123,['https://instagram.com/stories/example/1/','https://youtu.be/Abc123_X-yZ'],1,preferences=preferences.load(123))
        self.assertIn('第 1 個連結',preferences.notice_text(key))
        with c.db() as db:self.assertEqual([r[0] for r in db.execute('SELECT mode FROM batch_items WHERE job=? ORDER BY position',(key,))],['stories','post'])
    def test_size_boundaries_receipt_and_optional_link(self):
        for size,method in ((500_000_000,'sendDocument'),(500_000_001,'sendMessage')):
            job=self.job(str(size));file=self.file(job,size)
            with patch.object(w,'telegram',return_value={'message_id':7}) as send:
                w.deliver(job)
            self.assertTrue(any(call.args[0]==method for call in send.call_args_list))
            with c.db() as db:self.assertEqual(db.execute('SELECT state FROM files WHERE id=?',(file['id'],)).fetchone()[0],'sent')
        job=self.job('disabled');file=self.file(job,500_000_001)
        with patch.dict(os.environ,MEDIA_BOT_DOWNLOAD_BASE_URL=''),patch.object(w,'telegram',return_value={'message_id':7}):w.deliver(job)
        self.assertEqual(c.job_by_id(job['id'])['state'],'partial')
        with c.db() as db:self.assertEqual(db.execute('SELECT state FROM files WHERE id=?',(file['id'],)).fetchone()[0],'too_large')
        self.assertTrue(Path(file['path']).exists())
    def test_oversize_cancels_and_timeout_never_resends(self):
        job=self.job('oversize');file=self.file(job,2_000_000_001)
        with patch.object(w,'telegram',return_value={'message_id':1}):w.deliver(job)
        self.assertEqual(c.job_by_id(job['id'])['state'],'cancelled');self.assertTrue(Path(file['path']).exists())
        job=self.job('uncertain');self.file(job,500_000_000)
        def send(method,*args,**kwargs):
            if method=='sendDocument':raise TimeoutError()
            return {'message_id':1}
        with patch.object(w,'telegram',side_effect=send) as api:w.deliver(job);w.deliver(job)
        self.assertEqual(sum(call.args[0]=='sendDocument' for call in api.call_args_list),1)
    def test_local_path_upload_validation(self):
        job=self.job();file=self.file(job)
        response=Mock();response.json.return_value={'ok':True,'result':{'message_id':1}}
        session=Mock();session.post.return_value=response
        with patch.dict(os.environ,MEDIA_BOT_LOCAL_FILE_UPLOADS='1'),patch.object(config,'token',return_value='123:synthetic'),patch.object(w._transport,'session',session,create=True):
            with Path(file['path']).open('rb') as stream:w.telegram('sendDocument',{'chat_id':123},{'document':('file',stream,'application/octet-stream')})
            kwargs=session.post.call_args.kwargs
            self.assertEqual(kwargs['json']['document'],file['path']);self.assertFalse(kwargs['allow_redirects']);self.assertFalse(session.trust_env)
            outside=self.root/'outside';outside.write_text('fixture')
            with outside.open('rb') as stream:self.assertIsNone(w._local_file_payload({}, {'document':('file',stream,'application/octet-stream')}))
    def test_capability_server_ranges_expiry_revocation_and_no_plain_tokens(self):
        job=self.job();file=self.file(job);Path(file['path']).write_bytes(b'abcdefgh')
        url=links.create(file['id']);token=url.rsplit('/',1)[-1]
        with c.db() as db:
            row=db.execute('SELECT token,expires FROM links').fetchone()
            self.assertEqual(row['token'],c.digest(token));self.assertNotEqual(row['token'],token)
            self.assertAlmostEqual(row['expires']-time.time(),7200,delta=3)
        server=links.Server(('127.0.0.1',0));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,2);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(method='GET',headers=None,path=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            try:
                connection.request(method,path or '/'+token,headers=headers or {})
                response=connection.getresponse();return response.status,response.read(),dict(response.getheaders())
            finally:connection.close()
        self.assertEqual(request()[0:2],(200,b'abcdefgh'))
        self.assertEqual(request('HEAD')[0:2],(200,b''))
        self.assertEqual(request(headers={'Range':'bytes=2-4'})[0:2],(206,b'cde'))
        self.assertEqual(request(headers={'Range':'bytes=-2'})[0:2],(206,b'gh'))
        self.assertEqual(request(headers={'Range':'bytes=20-'})[0],416)
        self.assertEqual(request(headers={'Range':'bytes=0-1,3-4'})[0],416)
        self.assertEqual(request(path='/../state.sqlite3')[0],404)
        with patch.object(limits,'MAX_DOWNLOAD_BYTES',7):
            self.assertEqual(request()[0],404)
            with self.assertRaises(ValueError):links.create(file['id'])
        with patch.dict(os.environ,MEDIA_BOT_ALLOWED_USERS='456'):self.assertEqual(request()[0],404)
        with c.db() as db:db.execute('UPDATE links SET expires=?',(time.time()-1,))
        self.assertEqual(request()[0],404)
    def test_retention_preserves_direct_and_uncertain_files(self):
        records=[]
        for name,state,linked in [('link','sent',True),('direct','sent',False),('uncertain','uncertain',True)]:
            job=self.job(name);file=self.file(job);records.append(file)
            if linked:links.create(file['id'])
            c.update(job['id'],state='completed' if state=='sent' else 'partial',delivered=time.time()-7500)
            with c.db() as db:
                db.execute('UPDATE files SET state=?,link_retention_until=? WHERE id=?',(state,time.time()-1 if linked else 0,file['id']))
                db.execute('UPDATE links SET expires=? WHERE file=?',(time.time()-1,file['id']))
        c.cleanup()
        self.assertEqual([Path(f['path']).exists() for f in records],[False,True,True])

if __name__=='__main__':unittest.main()
