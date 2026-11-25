"""Authenticated service administration and expiring capability downloads."""
import argparse
import contextlib
import hmac
import http.server
import ipaddress
import os
import re
import secrets
import socketserver
import sys
import time
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import admin_auth, local_socket
from . import access, accounts, core as c, links, pool_checks, web_layout as layout

TTL = 3 * 86400
ACCOUNT_RESUME_MAX_AGE = 30 * 60


def session(token,at=None):
    if not token:
        return None
    with c.db() as db:
        return db.execute('SELECT * FROM sessions WHERE digest=? AND expires>?',(c.digest(token),time.time() if at is None else at)).fetchone()

def parse_form(content_type,raw,*,allow_upload=False):
    """Bounded, in-memory import; never persist filenames, form bodies or secrets."""
    if not content_type.lower().startswith('multipart/form-data'):
        try:return {k:v[-1] for k,v in parse_qs(raw.decode('utf-8')).items()}
        except UnicodeError:raise ValueError('Supply a UTF-8 encoded form') from None
    if not allow_upload:raise ValueError('This endpoint does not accept uploads')
    from email.parser import BytesParser
    from email.policy import default
    try:
        message=BytesParser(policy=default).parsebytes(
            ('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode('ascii')+raw)
        if message.defects or not message.is_multipart():raise ValueError()
        fields={};upload=None
        for index,part in enumerate(message.iter_parts()):
            if index>=12 or part.is_multipart() or part.defects:raise ValueError()
            name=part.get_param('name',header='content-disposition')
            if part.get_content_disposition()!='form-data' or not name or name in fields:raise ValueError()
            data=part.get_payload(decode=True)
            if not isinstance(data,bytes) or len(data)>131072:raise ValueError()
            value=data.decode('utf-8-sig')
            if part.get_filename() is not None:
                if name!='youtube_cookie_file' or upload is not None:raise ValueError()
                upload=value
            else:
                if name not in ('csrf','platform','action','id','label','cookie','policy'):raise ValueError()
                fields[name]=value
        if fields.get('platform')!='youtube':raise ValueError()
        if upload and upload.strip():
            if fields.get('cookie','').strip():raise ValueError('Choose either an upload or pasted cookies')
            fields['cookie']=upload
        return fields
    except (UnicodeError,TypeError):raise ValueError('Supply a UTF-8 cookies.txt file') from None
    except ValueError as error:
        if str(error)=='Choose either an upload or pasted cookies':raise
        raise ValueError('Invalid cookie import (128 KiB maximum)') from None


def resume_accounts(platform,account_id=''):
    # Age is measured from the original request, never the Cookie update or
    # last retry. One write transaction prevents expired jobs becoming runnable.
    cutoff = time.time() - ACCOUNT_RESUME_MAX_AGE
    eligible = """platform=? AND state='paused' AND cancel=0
          AND (account_id='' OR account_id=?)
          AND NOT EXISTS(SELECT 1 FROM files WHERE job=jobs.id AND state='uncertain')"""
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""UPDATE jobs SET state='cancelled',cancel=1,retry_at=0,
          message='工作建立已超過 30 分鐘，更新帳號後自動撤銷；如仍需要請重新傳送連結。'
          WHERE """ + eligible + ' AND created<?', (platform,account_id,cutoff))
        db.execute("""UPDATE jobs SET state='queued' WHERE """ + eligible + """
          AND created>=?
          AND (mode='batch' OR NOT EXISTS(SELECT 1 FROM files WHERE job=jobs.id))""",
          (platform,account_id,cutoff))

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def setup(self):super().setup();self.connection.settimeout(30)
    def respond(self,status,body='',headers=(),mime='text/html; charset=utf-8'):
        raw=body.encode() if isinstance(body,str) else body
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('X-Content-Type-Options','nosniff')
        for k,v in headers:self.send_header(k,v)
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)
    def cookie(self,name,value,age=TTL):return ('Set-Cookie',f'{name}={value}; Path={c.BASE}; Max-Age={age}; Secure; HttpOnly; SameSite=Strict')
    def cookies(self):
        jar=SimpleCookie()
        with contextlib.suppress(Exception):jar.load(self.headers.get('Cookie',''))
        return {k:v.value for k,v in jar.items()}
    def redirect(self,path,headers=()):self.respond(303,headers=[('Location',path),*headers])
    def form(self, message=''):
        csrf = secrets.token_urlsafe(32)
        controls = layout.field('username','Username',attrs='required autocomplete="username"')
        controls += layout.field('password','Password','password',attrs='required autocomplete="current-password"')
        body = '<section>'+layout.form('login',layout.hidden_field('csrf',csrf),controls+layout.button('Sign in'))+'</section>'
        if message:
            body = f'<p role="alert">{escape(message)}</p>'+body
        self.respond(200,layout.page('Sign in',body),[self.cookie('mb_login_csrf',csrf,600)])

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path = urlsplit(self.path)
            if not path.path.startswith(c.BASE):
                return self.respond(404,'Not found')
            route = path.path[len(c.BASE):]
            if route=='deployment-status':
                receipt = Path('/var/lib/telegram-social-deploy/status.json')
                if not receipt.is_file():
                    return self.respond(404,'No deployment receipt')
                state = json.loads(receipt.read_text())
                return self.respond(200,json.dumps({key: state[key] for key in ('deployment','sha','state')}),mime='application/json')
            if route=='healthz':
                return self.respond(200,'{"status":"ok"}',mime='application/json')
            if route=='login':
                return self.form()
            if re.fullmatch(r'download/[A-Za-z0-9_-]{43}',route):
                return links.serve_download(self,route.split('/')[1],self.command=='HEAD')
            panels = {'':'Bot connection','users':'Users and groups','subscriptions':'Subscriptions','accounts':'Account pools','keys':'API credentials'}
            if route not in panels:
                return self.respond(404,'Not found')
            signed_in = session(self.cookies().get('mb_session'))
            if not signed_in:
                return self.redirect(c.BASE+'login')
            hidden = layout.hidden_field('csrf',signed_in['csrf'])
            if route=='users':
                offset = parse_qs(path.query).get('offset',['0'])[0]
                if not re.fullmatch(r'[0-9]{1,7}',offset):
                    return self.respond(400,'Invalid page')
                body = layout.users(hidden,int(offset))
            elif route=='subscriptions':
                from . import push
                body = push.web_panel(hidden)
            elif route=='accounts':
                body = layout.pools(hidden)
            elif route=='keys':
                body = layout.keys(hidden)
            else:
                body = layout.connection(hidden)
            body += layout.form('logout',hidden,layout.button('Sign out'))
            return self.respond(200,layout.page(panels[route],body,signed_in=True))
        except (OSError,ValueError,KeyError):
            return self.respond(503,'Service settings are temporarily unavailable.')

    def _rate_key(self):
        if isinstance(self.client_address,tuple):
            return self.client_address[0]
        # Only Unix socket peers can be a trusted local reverse proxy.
        try:
            return str(ipaddress.ip_address(self.headers.get('X-Real-IP','')))
        except ValueError:
            return 'local'

    def _login(self, form, cookies):
        if not csrf_matches(form.get('csrf'),cookies.get('mb_login_csrf')):
            return self.respond(403,'Expired sign-in form')
        now = time.time()
        address = self._rate_key()
        with c.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE time<?',(now-900,))
            if db.execute('SELECT COUNT(*) FROM attempts WHERE address=?',(address,)).fetchone()[0]>=8:
                return self.respond(429,'Try again in fifteen minutes')
            db.execute('INSERT INTO attempts VALUES (?,?)',(address,now))
        if not admin_auth.verify(form.get('username',''),form.get('password','')):
            return self.form('Username or password was incorrect.')
        token = secrets.token_urlsafe(32)
        with c.db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)',(c.digest(token),now+TTL,secrets.token_urlsafe(32)))
            db.execute('DELETE FROM attempts WHERE address=?',(address,))
        return self.redirect(c.BASE,[self.cookie('mb_session',token),self.cookie('mb_login_csrf','',0)])

    def do_POST(self):
        try:
            if self.headers.get('Origin')!=c.ORIGIN:
                return self.respond(403,'Origin mismatch')
            length = int(self.headers.get('Content-Length','0'))
            if not 0<length<=300000:
                return self.respond(413,'Invalid form size')
            path = urlsplit(self.path).path
            if not path.startswith(c.BASE):
                return self.respond(404,'Not found')
            route = path[len(c.BASE):]
            form = parse_form(self.headers.get('Content-Type',''), self.rfile.read(length), allow_upload=route=='account')
            cookies = self.cookies()
            if route=='login':
                return self._login(form,cookies)
            signed_in = session(cookies.get('mb_session',''))
            if not signed_in:
                return self.respond(401,'Sign in again')
            if not csrf_matches(form.get('csrf'),signed_in['csrf']):
                return self.respond(403,'Expired form')
            if route=='logout':
                with c.db() as db:
                    db.execute('DELETE FROM sessions WHERE digest=?',(signed_in['digest'],))
                return self.redirect(c.BASE+'login',[self.cookie('mb_session','',0)])
            target = self._manage(route,form)
            if target is None:
                return self.respond(404,'Not found')
            return self.redirect(c.BASE+target)
        except (ValueError,KeyError,OverflowError) as error:
            message = str(error) if isinstance(error,FormError) else 'Invalid form or credential settings.'
            return self.respond(400,layout.page('Update failed',f'<p role="alert">{escape(message)}</p><a href="{c.BASE}">Return to settings</a>',signed_in=True))
        except Exception:
            return self.respond(503,'The service is unavailable. Check the connection and try again.')

    def _manage(self,route,form):
        if route=='settings':
            changes = {p+'_media_limit':int(form[p+'_media_limit']) for p in accounts.PLATFORMS}
            if not all(1<=v<=1000 for v in changes.values()):
                raise FormError('Limits must be between 1 and 1000 media items per minute.')
            if form.get('token'):
                c.rpc(c.SECRET_SOCKET,'/settings',{'token':form['token']})
            changes.update(enabled=form.get('enabled')=='1',failure_alerts=form.get('failure_alerts')=='1')
            c.setting_many(changes)
            return ''
        if route=='check':
            from .worker import telegram
            telegram('getMe',{})
            hook = telegram('getWebhookInfo',{})
            c.setting('telegram_health','Webhook configured; remove it to poll' if hook.get('url') else 'Connected')
            return ''
        if route=='access':
            access.decide(int(form.get('user_id','0')),form.get('action',''))
            return 'users'
        if route=='admission':
            c.setting('auto_approve_users',form.get('enabled')=='1')
            return 'users'
        if route=='groups':
            values = [v.strip() for v in form.get('groups','').split(',') if v.strip()]
            if len(values)>200 or any(not re.fullmatch(r'-[1-9][0-9]{0,15}',v) for v in values):
                raise FormError('Supply negative numeric Telegram group IDs.')
            c.setting('translation_groups',list(dict.fromkeys(map(int,values))))
            return 'users'
        if route=='account':
            result = c.rpc(c.SECRET_SOCKET,'/accounts',{k:form.get(k,'') for k in ('platform','action','id','label','cookie','policy')})
            if form.get('action') in ('save','enable','primary'):
                resume_accounts(form['platform'],result.get('id',''))
            return 'accounts'
        if route.startswith('pool-check/'):
            platform = route[len('pool-check/'):]
            if platform not in accounts.PLATFORMS:
                return None
            try:
                pool_checks.start(platform,form.get('url',''))
            except ValueError as exc:
                if str(exc) in pool_checks.FORM_ERRORS.values():
                    raise FormError(str(exc)) from None
                raise
            return 'accounts'
        if route=='api-key':
            c.rpc(c.SECRET_SOCKET,'/translation/keys',{k:form.get(k,'') for k in ('id','label','key','action')})
            return 'keys'
        if route=='local-api':
            c.rpc(c.SECRET_SOCKET,'/local-api',{k:form.get(k,'') for k in ('api_id','api_hash')})
            return 'keys'
        if route=='subscriptions':
            from . import push
            push.web_post(form)
            return 'subscriptions'
        return None

class FormError(ValueError):
    """A form error with a fixed user-facing message."""


def csrf_matches(actual,expected):
    return (isinstance(actual,str) and isinstance(expected,str) and len(actual)==43
            and re.fullmatch('[A-Za-z0-9_-]{43}',actual) is not None and hmac.compare_digest(actual,expected))


def main(argv=()):
    parser=argparse.ArgumentParser()
    parser.add_argument('--hash-password',action='store_true')
    if parser.parse_args(argv).hash_password:
        return admin_auth.main()
    admin_auth._verifier()
    c.initialize()
    socket_path=os.environ.get('MEDIA_BOT_WEB_SOCKET')
    if socket_path:
        class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
            daemon_threads=True
        path=Path(socket_path)
        if not path.is_absolute() or path.parent.resolve()!=path.parent:
            raise ValueError('Invalid web socket path')
        with local_socket.reserve(path), UnixServer(str(path),Handler) as server:
            os.chmod(path,0o660)
            server.serve_forever()
        return
    else:
        server=http.server.ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('MEDIA_BOT_WEB_PORT','8091'))),Handler)
    with server:
        server.serve_forever()


if __name__=='__main__':
    main(sys.argv[1:])
