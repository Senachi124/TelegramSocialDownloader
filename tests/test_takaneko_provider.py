import fcntl
import os
import json
from pathlib import Path

import tempfile
import unittest
from unittest.mock import patch

from takaneko_telegram import state, takaneko_feed as feed, transport


class ProducerIntegrationTests(unittest.TestCase):
    def test_existing_sent_and_uncertain_work_never_sent_again(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state,'CONTROL', Path(tmp)):
            state.initialize()
            feed.set_enabled(True)
            feed.set_subscription(42,True)
            feed.record_inventory([{'id':'baseline'}])
            feed.record_inventory([{'id':'baseline'},{'id':'delivered'},{'id':'unknown'}])
            plan=json.dumps([{'kind':'text','html':'fixture'}])
            with state.db() as db:
                db.execute("UPDATE tk_posts SET plan=? WHERE id!='baseline'",(plan,))
                db.execute("UPDATE tk_deliveries SET state=CASE post_id WHEN 'delivered' THEN 'sent' ELSE 'uncertain' END")
                for d in db.execute('SELECT * FROM tk_deliveries'):
                    db.execute('INSERT INTO tk_parts VALUES (?,0,?,?,?)',
                               (d['id'],json.dumps({'kind':'text','html':'fixture'}),d['state'],
                                '123' if d['state']=='sent' else None))
                items = list(db.execute('SELECT * FROM tk_deliveries'))
            calls=[]
            for row in items:
                feed.deliver(dict(row), lambda *args: calls.append(args))
            with patch.object(feed,'deliver') as attempt:
                feed.process_once()
                attempt.assert_not_called()
            self.assertEqual(calls,[])

    def test_unavailable_recipients_do_not_starve_the_next_page(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, 'CONTROL', Path(tmp)):
            state.initialize()
            feed.set_enabled(True)
            for user in range(1, 23):
                feed.set_subscription(user, True)
            feed.record_inventory([])
            feed.record_inventory([{'id': 'story'}])
            with state.db() as db:
                plan = json.dumps([{'kind': 'text', 'html': 'fixture'}])
                db.execute('UPDATE tk_posts SET plan=?', (plan,))
            def eligible(uid):
                if uid == 1:
                    raise ConnectionRefusedError()
                return uid == 22
            with patch.object(transport, 'allowed', side_effect=eligible), patch.object(
                    transport, 'telegram', return_value={'message_id': 77}) as send:
                feed.process_once()
                feed.process_once()
                feed.process_once()
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1]['chat_id'], 22)
            with state.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM tk_deliveries WHERE state='pending' AND retry_at>0").fetchone()[0], 21)
                self.assertEqual(db.execute("SELECT count(*) FROM tk_parts WHERE state='sent'").fetchone()[0], 1)

    def test_health_requires_database_and_broker_but_never_reads_credentials(self):
        from takaneko_telegram import secrets
        with tempfile.TemporaryDirectory() as root, patch.object(state, 'CONTROL', Path(root)):
            state.initialize()
            with patch.object(secrets, 'FILE') as private_file, patch.object(
                    transport, 'secret', side_effect=lambda op: secrets.request(7, {'op': op}, None)):
                self.assertEqual(transport.control(11, {'op': 'health'}, None), {'ok': True})
                private_file.read_text.assert_not_called()
            with patch.object(transport, 'secret', side_effect=ConnectionRefusedError), self.assertRaises(ConnectionRefusedError):
                transport.control(11, {'op': 'health'}, None)

    def test_links_health_works_while_delivery_is_drained(self):
        from io import BytesIO
        from unittest.mock import Mock
        from takaneko_telegram import links
        with tempfile.TemporaryDirectory() as root, patch.object(state, 'CONTROL', Path(root)), patch.object(
                feed, 'deployment_guard', side_effect=AssertionError), patch.object(state, 'storage_guard') as guard:
            state.initialize()
            request = Mock(path='/healthz', wfile=BytesIO())
            links.Handler.do_GET(request)
            request.send_response.assert_called_once_with(200)
            self.assertEqual(json.loads(request.wfile.getvalue()), {'status': 'ok'})
            guard.side_effect = RuntimeError('mount unavailable')
            links.Handler.do_GET(request)
            request.send_error.assert_called_once_with(503)

    def test_live_link_requires_both_current_grants(self):
        import time
        with tempfile.TemporaryDirectory() as tmp, patch.object(state,'CONTROL', Path(tmp)), patch.object(state,'MEDIA',Path(tmp)):
            state.initialize()
            feed.set_enabled(True)
            feed.set_subscription(8,True)
            feed.record_inventory([])
            feed.record_inventory([{'id':'story'}])
            media=Path(tmp)/'image';media.write_bytes(b'part')
            with state.db() as db:
                row = db.execute('SELECT id FROM tk_deliveries').fetchone()
                db.execute('INSERT INTO tk_links VALUES (?,?,?,?,?)',(state.digest('fixture'),row[0],str(media),'image',time.time()+60))
            with patch.object(transport,'allowed', return_value=False):
                self.assertIsNone(feed.download_link('fixture'))
            with patch.object(transport,'allowed', return_value=True):
                self.assertEqual(feed.download_link('fixture')['path'],media)
                feed.set_subscription(8,False)
                self.assertIsNone(feed.download_link('fixture'))

    def test_producer_attachment_reaches_bot_without_shared_media_access(self):
        import os
        import socket
        import threading
        from media_bot import push_socket
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MEDIA_BOT_DATA':tmp}):
            from media_bot import push
            root=Path(tmp)
            file=root/'photo.jpg';file.write_bytes(b'fixture-image')
            uid=os.getuid()
            sockets=[]
            workers=[]
            def start(name,func):
                path=str(root/name)
                server=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
                sockets.append(server)
                server.bind(path)
                server.listen(1)
                def once():
                    client,_=server.accept()
                    push_socket._serve_one(client,{uid},func,threading.Semaphore(0))
                thread=threading.Thread(target=once,daemon=True)
                thread.start();workers.append(thread)
                return path
            seen=[]
            def send(method,data,files):
                seen.append((method,data['chat_id'],files['photo'][1].read()))
                return {'message_id':21,'private':'filtered'}
            try:
                with patch.object(state,'CONTROL',root), patch.object(push.access,'allowed',return_value=True), patch.object(push.core,'get_setting',return_value=True):
                    state.initialize()
                    feed.set_subscription(81,True)
                    control_path=start('control',transport.control)
                    entry={'uid':uid,'control':control_path}
                    bot_path=start('transport',lambda peer,req,fd:push.dispatch(entry,req,fd,send=send))
                    def call(request,stream=None,timeout=10):
                        return push_socket.exchange(bot_path,uid,request,stream=stream,timeout=timeout)
                    with patch.object(transport,'_call',side_effect=call), file.open('rb') as stream:
                        reply=transport.telegram('sendPhoto',{'chat_id':81},{'photo':('photo.jpg',stream,'image/jpeg')})
                        self.assertEqual(reply,{'ok':True,'message_id':21})
                        self.assertEqual(seen,[('sendPhoto',81,b'fixture-image')])
            finally:
                for server in sockets:server.close()
                for thread in workers:thread.join(2)

    def test_credentials_rotate_without_stale_overwrite(self):
        from takaneko_telegram import secrets
        with tempfile.TemporaryDirectory() as tmp, patch.object(secrets,'ROOT',Path(tmp)), patch.object(secrets,'FILE',Path(tmp)/'credentials.json'):
            initial={'session':{'refreshToken':'one'},'primary_key':'a','openai_keys':[{'enabled':True,'id':'a','key':'fixture'}]}
            secrets.save(initial)
            req={'op':'refresh','expected_session_digest':secrets.session_digest(initial['session']),'expected_refresh_token':'one','refreshToken':'two','accessToken':'access'}
            self.assertTrue(secrets.request(100,req,None)['ok'])
            self.assertFalse(secrets.request(100,req,None)['ok'])
            self.assertFalse(secrets.request(100,{'op':'replace','credentials':initial},None)['ok'])
            self.assertTrue(secrets.request(0,{'op':'replace','credentials':initial},None)['ok'])
            self.assertEqual(json.loads(secrets.FILE.read_text()),initial)

    def test_browser_cookie_update_preserves_keys_and_fences_cookie_only_refresh(self):
        from takaneko_telegram import secrets
        with tempfile.TemporaryDirectory() as tmp, patch.object(secrets,'ROOT',Path(tmp)), patch.object(secrets,'FILE',Path(tmp)/'credentials.json'):
            cookie = dict(name='session',value='fixture',path='/',domain='.takanekofc.com')
            stored = {'session':{'cookies':[cookie]},'openai_keys':[{'enabled':True,'id':'first','key':'fixture-key'}],'primary_key':'first'}
            secrets.save(stored)
            with patch.object(transport,'secret',side_effect=lambda op, fields:secrets.request(101,{'op':op,**fields},None)):
                update = {'op':'set_session','session':[dict(cookie,value='replacement'),dict(cookie,domain='.unrelated.test')]}
                self.assertEqual(transport.control(100,update,None),{'ok':True})
                self.assertEqual(json.loads(secrets.FILE.read_text()),{**stored,'session':{'cookies':[dict(cookie,value='replacement')]}})
                with self.assertRaises(ValueError):
                    transport.control(100,{'op':'set_session','session':[dict(cookie,domain='.unrelated.test')]},None)
            reply = secrets.request(101, dict(op='refresh', expected_session_digest=secrets.session_digest(stored['session']),
                 expected_refresh_token='', accessToken='stale', refreshToken='stale'), None)
            self.assertEqual(reply,{'ok':False})
            self.assertEqual(secrets.FILE.stat().st_mode & 0o777,0o600)

    def test_fresh_authorized_install_delivers_new_article_without_translation_keys(self):
        from takaneko_telegram import takaneko_api
        with tempfile.TemporaryDirectory() as tmp, patch.object(state,'CONTROL',Path(tmp)), patch.object(state,'MEDIA',Path(tmp)):
            state.initialize()
            feed.set_enabled(True)
            self.assertIn('set_session',transport.control(100,{'op':'list'},None)['capabilities'])
            feed.set_subscription(101,True)
            feed.record_inventory([{'id':'previous'}])
            feed.record_inventory([{'id':'previous'},{'id':'current'}])
            sent=[]
            def send(method,data):
                sent.append((method,data))
                return {'message_id':123}
            with patch.object(feed,'storage_guard'), patch.object(feed,'credentials',return_value={'session':{'accessToken':'synthetic'},'openai_keys':[]}), patch.object(takaneko_api.Client,'detail',return_value={'id':'current','title':'Fixture','html':'<p>Original fixture</p>'}), patch.object(transport,'allowed',return_value=True), patch.object(transport,'telegram',side_effect=send):
                feed.process_once()
                feed.process_once()
            self.assertEqual(len(sent),1)
            self.assertEqual(sent[0][1]['chat_id'],101)
            self.assertIn('Original fixture',sent[0][1]['text'])
            with state.db() as db:
                self.assertEqual(tuple(db.execute('SELECT post_id,state FROM tk_deliveries').fetchone()),('current','sent'))

    def test_deployment_exclusive_lock_defers_provider_work(self):
        with tempfile.NamedTemporaryFile() as lock, patch.dict(os.environ,{'TAKANEKO_DEPLOY_LOCK':lock.name}):
            fcntl.flock(lock,fcntl.LOCK_EX)
            with feed.deployment_guard() as available:
                self.assertFalse(available)
            fcntl.flock(lock,fcntl.LOCK_UN)
            with feed.deployment_guard() as available:
                self.assertTrue(available)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_volume_identity_checks_media_and_temp_against_separate_bind_roots(self):
        with tempfile.TemporaryDirectory() as root:
            media=Path(root)/'media'
            tmp=Path(root)/'tmp'
            media.mkdir()
            tmp.mkdir()
            var={'TMPDIR':str(tmp),'TAKANEKO_STORAGE_UUID':'volume','TAKANEKO_MEDIA_FSROOT':'/owned/media','TAKANEKO_TMP_FSROOT':'/owned/tmp'}
            def mount(argv, **kwargs):
                target=argv[argv.index('--target')+1]
                return json.dumps({'filesystems':[{'target':target,'uuid':'volume','fsroot':'/owned/'+Path(target).name}]})
            with patch.object(state,'MEDIA',media), patch.object(state,'TMP',tmp), patch.dict(os.environ,var), patch.object(state.subprocess,'check_output',side_effect=mount):
                with patch.object(state.shutil,'disk_usage') as usage:
                    usage.return_value.free=6*1024**3
                    state.storage_guard()
                    with patch.dict(os.environ,{'TAKANEKO_TMP_FSROOT':'/wrong'}), self.assertRaises(RuntimeError):
                        state.storage_guard()
                    usage.return_value.free=1
                    with self.assertRaises(RuntimeError):
                        state.storage_guard()
