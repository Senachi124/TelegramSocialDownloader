import http.client
import http.server
import json
import os
import re
import socketserver
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        data = self.root/'data'
        data.mkdir()
        salt = b'test-salt-012345'
        from media_bot import admin_auth
        verifier = 'scrypt$'+salt.hex()+'$'+admin_auth.hash_password('correct test password',salt)
        self.enterContext(patch.dict(os.environ, {
            'MEDIA_BOT_ADMIN_PASSWORD_HASH':verifier, 'MEDIA_BOT_DATA':str(data),
            'MEDIA_BOT_ORIGIN':'https://service.example', 'MEDIA_BOT_BASE':'/bot/',
            'MEDIA_BOT_SECRET_DIR':str(self.root/'private'), 'MEDIA_BOT_OWNER_ID':'90001',
            'TMPDIR':str(data/'tmp')
        }, clear=True))
        from media_bot import core, broker, web
        self.c = core
        self.broker = broker
        self.web = web
        socket = str(self.root/'credentials.sock')
        for name,value in [('DATA',data),('BASE','/bot/'),('ORIGIN','https://service.example'),('SECRET_SOCKET',socket)]:
            self.enterContext(patch.object(core,name,value))
        core.initialize()
        broker.bootstrap()
        self.serve(socketserver.UnixStreamServer(socket,broker.Handler))
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
        self.port = httpd.server_port
        self.serve(httpd)

    def serve(self, server):
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.addCleanup(close)
        thread.start()

    def request(self,route,*,form=None,cookie='',origin='https://service.example'):
        conn = http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        headers = {'Cookie':cookie,'Origin':origin,'Content-Type':'application/x-www-form-urlencoded'}
        conn.request('POST' if form is not None else 'GET','/bot/'+route,urlencode(form) if form is not None else None,headers)
        response = conn.getresponse()
        data = (response.status,response.read().decode(),response.headers)
        conn.close()
        return data

    def login(self):
        code,body,headers = self.request('login')
        self.assertEqual(code,200)
        csrf = re.search('name="csrf" value="([^"]+)"',body)[1]
        cookie = headers.get('Set-Cookie').split(';',1)[0]
        form = {'username':'sena','password':'correct test password','csrf':csrf}
        self.assertEqual(self.request('login',form=form)[0],403)
        status,_,headers = self.request('login',form=form,cookie=cookie)
        self.assertEqual(status,303)
        session = headers.get_all('Set-Cookie')[0]
        for field in ('Secure','HttpOnly','SameSite=Strict','Max-Age=259200','Path=/bot/'):
            self.assertIn(field,session)
        cookie = session.split(';',1)[0]
        status,body,_ = self.request('',cookie=cookie)
        self.assertEqual(status,200)
        return cookie,re.search('name="csrf" value="([^"]+)"',body)[1]

    def test_provider_session_import_requires_admin_and_csrf(self):
        from media_bot import push
        entry = {'control': '/run/test-provider.sock', 'uid': 2000}
        self.enterContext(patch.object(push, 'providers', return_value={'takaneko': entry}))
        provider = self.enterContext(patch.object(push, 'control', return_value={
            'ok': True, 'users': [], 'capabilities': ['set_session']
        }))
        form = {'provider': 'takaneko', 'action': 'set_session', 'session': '{"accessToken":"sample-secret"}'}
        self.assertEqual(self.request('subscriptions', form=form)[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.request('subscriptions', form=form, cookie=cookie)[0], 403)
        provider.assert_not_called()
        self.assertEqual(self.request('subscriptions', form=dict(form, csrf=csrf), cookie=cookie)[0], 303)
        provider.assert_called_once_with(entry, 'set_session', session={'accessToken': 'sample-secret'})
        status, body, _ = self.request('subscriptions', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('Save Fanclub session', body)
        self.assertNotIn('sample-secret', body)

    def test_feature_switches_are_authenticated_independent_and_persistent(self):
        from media_bot import features
        form = {'feature': 'download_twitter', 'enabled': '0'}
        self.assertEqual(self.request('features')[0], 303)
        self.assertEqual(self.request('features', form=form)[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.request('features', cookie=cookie, form=form)[0], 403)
        form['csrf'] = csrf
        self.assertEqual(self.request('features', cookie=cookie, form=form, origin='https://wrong.example')[0], 403)
        status, body, headers = self.request('features', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        self.assertIn('href="/bot/features" aria-current="page"', body)
        self.assertIn('Not installed', body)
        for label in features.LABELS.values():
            self.assertIn(f'aria-label="Pause {label}"', body)
        for _ in range(2):
            status, _, headers = self.request('features', cookie=cookie, form=form)
            self.assertEqual(status, 303)
            self.assertEqual(headers['Location'], '/bot/features#download_twitter')
        self.assertEqual([key for key, value in features.snapshot().items() if not value], ['download_twitter'])
        body = self.request('features', cookie=cookie)[1]
        self.assertIn('aria-label="Enable Twitter / X downloads"', body)
        self.assertIn('aria-label="Pause TikTok downloads"', body)
        for invalid in [dict(feature='enabled'), dict(feature='download_other'), dict(enabled='yes'),
                        dict(feature='takaneko', enabled='1')]:
            self.assertEqual(self.request('features', cookie=cookie, form={**form, **invalid})[0], 400)
        self.assertEqual(self.request('features', cookie=cookie, form={**form, 'feature': 'translation'})[0], 303)
        self.assertEqual(self.request('features', cookie=cookie, form={**form, 'enabled': '1'})[0], 303)
        with self.c.db() as db:
            self.assertEqual([key for key, value in features.snapshot(db).items() if not value], ['translation'])

    def test_fanclub_switch_reflects_and_updates_provider_state(self):
        from media_bot import push
        entry = {'control': '/run/test-provider.sock', 'uid': 2000}
        self.enterContext(patch.object(push, 'providers', return_value={'takaneko': entry}))
        control = self.enterContext(patch.object(push, 'control'))
        cookie, csrf = self.login()
        form = {'csrf': csrf, 'feature': 'takaneko', 'enabled': '1'}
        control.return_value = {'ok': True, 'enabled': False, 'session_configured': False}
        status, body, _ = self.request('features', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('Add a Fanclub session in Subscriptions', body)
        self.assertNotIn('aria-label="Enable Takaneko Fanclub"', body)
        control.assert_called_once_with(entry, 'feature_status')
        control.return_value['session_configured'] = True
        self.assertIn('aria-label="Enable Takaneko Fanclub"', self.request('features', cookie=cookie)[1])
        control.return_value['enabled'] = True
        status, _, headers = self.request('features', cookie=cookie, form=form)
        self.assertEqual((status, headers['Location']), (303, '/bot/features#takaneko'))
        control.assert_called_with(entry, 'set_enabled', enabled=True)
        control.return_value['session_configured'] = None
        self.assertIn('aria-label="Pause Takaneko Fanclub"', self.request('features', cookie=cookie)[1])
        control.side_effect = push.FanclubSessionRequired()
        self.assertEqual(self.request('features', cookie=cookie, form=form)[0], 400)
        control.side_effect = OSError('test socket unavailable')
        status, body, _ = self.request('features', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('Unavailable', body)
        self.assertIn('aria-label="Pause TikTok downloads"', body)
        self.assertEqual(self.request('features', cookie=cookie, form=form)[0], 503)
        control.side_effect = None
        for reply in ({'ok': True}, {'ok': True, 'enabled': False, 'session_configured': True}):
            control.return_value = reply
            self.assertEqual(self.request('features', cookie=cookie, form=form)[0], 503)

    def test_login_access_pools_api_and_subscriptions(self):
        from media_bot import access
        self.assertEqual(self.request('users')[0],303)
        member = access.observe({'id':405,'first_name':'A & B'})
        self.assertEqual(member['state'],'pending')
        cookie,csrf = self.login()
        for path in ('','users','accounts','subscriptions','keys'):
            self.assertEqual(self.request(path,cookie=cookie)[0],200,path)
        form={'csrf':csrf,'user_id':'405','action':'approve'}
        self.assertEqual(self.request('access',cookie=cookie,form=form,origin='https://wrong.example')[0],403)
        self.assertEqual(self.request('access',cookie=cookie,form={**form,'csrf':''})[0],403)
        self.assertFalse(access.allowed(405))
        self.assertEqual(self.request('access',cookie=cookie,form=form)[0],303)
        self.assertTrue(access.allowed(405))
        form={'csrf':csrf,'action':'save','platform':'tiktok','label':'Test pool','cookie':'sessionid=test-canary-cookie'}
        self.assertEqual(self.request('account',cookie=cookie,form=form)[0],303)
        status,body,_=self.request('accounts',cookie=cookie)
        self.assertEqual(status,200)
        self.assertIn('Test pool',body)
        self.assertNotIn('canary-cookie',body)
        key='sk-test-placeholder'
        self.assertEqual(self.request('api-key',cookie=cookie,form={'csrf':csrf,'action':'save','label':'Test key','key':key})[0],303)
        self.assertNotIn(key,self.request('keys',cookie=cookie)[1])
        self.assertEqual(self.c.rpc(self.c.SECRET_SOCKET,'/translation/keys')['keys'],[key])
        self.assertEqual(self.request('local-api',cookie=cookie,form={'csrf':csrf,'api_id':'12345','api_hash':'1'*32})[0],303)
        self.assertEqual((self.root/'private'/'local-api.env').stat().st_mode & 0o777,0o600)
        self.assertEqual(self.request('logout',cookie=cookie,form={'csrf':csrf})[0],303)
        self.assertEqual(self.request('users',cookie=cookie)[0],303)

    def test_public_deployment_receipt(self):
        receipt = self.root/'status.json'
        with patch.object(self.web, 'DEPLOYMENT_RECEIPT', receipt):
            self.assertEqual(self.request('deployment-status')[0], 404)
            expected = {'deployment': 111, 'sha': 'b'*40, 'state': 'success'}
            receipt.write_text(json.dumps(dict(expected, extra='private')))
            status, body, _ = self.request('deployment-status?deployment=111')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), expected)

    def test_expired_session_and_removed_routes(self):
        cookie,csrf = self.login()
        for path in ('history','admin-media/1','llm-traces','unknown'):
            self.assertEqual(self.request(path,cookie=cookie)[0],404)
        with self.c.db() as db:
            db.execute('UPDATE sessions SET expires=?',(time.time()-1,))
        self.assertEqual(self.request('users',cookie=cookie)[0],303)
        self.assertEqual(self.request('admission',form={'csrf':csrf,'enabled':'1'},cookie=cookie)[0],401)

    def test_failure_alert_survives_retirement_without_content_or_duplicate_sends(self):
        from media_bot import admin_notifications as alerts
        with self.c.db() as db:
            db.execute("""INSERT INTO jobs(id,chat,url,platform,state,message) VALUES
                       ('job',90001,'https://example.invalid/test-canary','instagram','queued','')""")
            db.execute("UPDATE jobs SET state='failed',message='cookie test-canary' WHERE id='job'")
            db.execute("DELETE FROM jobs WHERE id='job'")
            queue = list(map(dict,db.execute('SELECT * FROM admin_alert_queue')))
            self.assertEqual([q['reason'] for q in queue],['account'])
            self.assertNotIn('test-canary',json.dumps(queue))
        messages=[]
        alerts.tick(lambda _,message:messages.append(message) or {'message_id':123})
        self.assertEqual(len(messages),1)
        self.assertIn('cookies',messages[0]['text'])
        self.assertNotIn('test-canary',messages[0]['text'])
        alerts.tick(lambda *_:self.fail('Alert was sent twice'))
        with self.c.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM admin_alert_queue').fetchone()[0],0)

    def test_pool_check_requires_operator_input_and_clears_finished_urls(self):
        from media_bot import pool_checks
        url='https://www.instagram.com/p/test-canary/'
        with self.c.db() as db:
            db.execute("INSERT INTO jobs(id,chat,url,mode,platform,state) VALUES ('test',90001,?,'post','instagram','queued')",(url,))
        with self.assertRaises(ValueError):
            pool_checks.start('instagram','')
        pool_checks.start('instagram',url)
        with patch.object(pool_checks,'_run',return_value=('passed','Verified.')):
            self.assertTrue(pool_checks.run_next('instagram'))
        with self.c.db() as db:
            self.assertEqual(db.execute('SELECT url FROM admin_pool_checks').fetchone()[0],'')
        with self.assertRaises(ValueError):
            pool_checks.start('instagram','')

    def test_upgrade_retires_announcements_and_keeps_operational_alerts(self):
        from media_bot import admin_notifications as alerts
        key = 'release_announcement:1.0.2:test'
        self.c.setting(key, {'broadcast_authorized': True})
        with self.c.db() as db:
            db.execute('CREATE TABLE release_preview_outbox(event_key TEXT, state TEXT, message_id TEXT)')
            db.executemany('INSERT INTO release_preview_outbox VALUES (?,?,?)', [
                ('release-preview:sent', 'sent', '123'),
                ('release-preview:waiting', 'pending', None),
                ('release-preview:unknown', 'uncertain', None)])
            db.execute("""CREATE TABLE admin_notification_outbox(event_key TEXT, state TEXT,
                       message_id TEXT, attempts INTEGER, next_attempt REAL)""")
            db.executemany('INSERT INTO admin_notification_outbox VALUES (?,?,?,?,?)', [
                ('release-preview:legacy', 'pending', None, 0, 0),
                ('job:legacy', 'pending', None, 1, 0)])
        alerts.initialize()
        alerts.initialize()
        with self.c.db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='release_preview_outbox'").fetchone())
            receipts = db.execute('SELECT state,message_id FROM retired_release_receipts ORDER BY state')
            self.assertEqual([tuple(row) for row in receipts], [('sent', '123'), ('uncertain', None)])
        sent = []
        def send(method, message):
            sent.append(message['text'])
            return {'message_id': 1}
        alerts.tick(send)
        alerts.tick(send)
        self.assertEqual(sent, [alerts.MESSAGES['previous']])

    def test_each_account_pool_and_key_rotation(self):
        cookie,csrf = self.login()
        form=lambda route,fields:self.request(route,form={**fields,'csrf':csrf},cookie=cookie)[0]
        cookie_line='.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tunit-test-example\n'
        samples={'instagram':'sessionid=example; csrftoken=csrf-example',
                 'tiktok':'sessionid=example','twitter':'auth_token=example; ct0=csrf-example',
                 'threads':'sessionid=example; csrftoken=csrf-example','youtube':cookie_line}
        for platform,raw in samples.items():
            label=platform+' test'
            fields={'platform':platform,'label':label,'action':'save','cookie':raw}
            if platform=='youtube':
                from requests_toolbelt import MultipartEncoder
                fields={**fields,'cookie':'','csrf':csrf,'youtube_cookie_file':('cookies.txt',raw,'text/plain')}
                payload=MultipartEncoder(fields)
                conn=http.client.HTTPConnection('127.0.0.1',self.port)
                conn.request('POST','/bot/account',payload.to_string(),
                             {'Content-Type':payload.content_type,'Origin':self.c.ORIGIN,'Cookie':cookie})
                res=conn.getresponse()
                self.assertEqual(res.status,303)
                res.read();conn.close()
            else:
                self.assertEqual(form('account',fields),303)
            pool=self.c.rpc(self.c.SECRET_SOCKET,'/accounts')[platform]
            account,=pool['items']
            key=account['id']
            self.assertNotIn('cookies',account)
            self.assertEqual(pool['primary'],key)
            self.assertEqual(form('account',{**fields,'id':key,'label':'Updated','cookie':raw}),303)
            self.assertEqual(form('account',{'platform':platform,'action':'policy','policy':'round_robin'}),303)
            selected=self.c.rpc(self.c.SECRET_SOCKET,'/select',{'platform':platform})
            self.assertEqual(selected['revision'],2)
            for action in ('disable','enable','primary','disable','delete'):
                self.assertEqual(form('account',{'platform':platform,'id':key,'action':action}),303)
            self.assertEqual(self.c.rpc(self.c.SECRET_SOCKET,'/accounts')[platform]['items'],[])
        self.assertEqual(form('api-key',{'action':'save','label':'First','key':'sk-example-first'}),303)
        self.assertEqual(form('api-key',{'action':'save','label':'Second','key':'sk-example-second'}),303)
        _,second=self.c.rpc(self.c.SECRET_SOCKET,'/translation/status')['keys']
        for action in ('primary','disable','enable','primary'):
            self.assertEqual(form('api-key',{'id':second['id'],'action':action}),303)
        self.assertEqual(self.c.rpc(self.c.SECRET_SOCKET,'/translation/keys')['keys'][0],'sk-example-second')
        self.assertEqual(self.request('keys',cookie=cookie)[1].count('sk-example'),0)

    def test_capability_head_range_revocation_and_expiry(self):
        from media_bot import links
        media=self.c.DATA/'media'/'range-file'
        media.write_bytes(b'0123456789')
        with self.c.db() as db:
            db.execute("INSERT INTO jobs(id,chat,state) VALUES ('range-job',90001,'completed')")
            key=db.execute("INSERT INTO files(job,path,name,size,state) VALUES (?,?,?,?,?)",
                           ('range-job',str(media),'fixture',10,'sent')).lastrowid
        route=links.create(key).split('/bot/')[1]
        def fetch(method,rng=None):
            conn=http.client.HTTPConnection('127.0.0.1',self.port)
            conn.request(method,'/bot/'+route,headers={'Range':rng} if rng else {})
            response=conn.getresponse()
            answer=response.status,response.read(),response.headers
            conn.close()
            return answer
        for method in ('HEAD','GET'):
            code,body,headers=fetch(method,'bytes=2-4')
            self.assertEqual((code,headers['Content-Range'],headers['Accept-Ranges']), (206,'bytes 2-4/10','bytes'))
            self.assertEqual(body,b'234' if method=='GET' else b'')
        self.assertEqual(fetch('GET','bytes=12-')[0],416)
        self.assertEqual(fetch('GET','bytes=-3')[1],b'789')
        with self.c.db() as db:
            db.execute("UPDATE jobs SET cancel=1 WHERE id='range-job'")
        self.assertEqual(fetch('GET')[0],404)
        with self.c.db() as db:
            db.execute("UPDATE jobs SET cancel=0 WHERE id='range-job'")
            db.execute('UPDATE links SET expires=0')
        self.assertEqual(fetch('GET')[0],404)
