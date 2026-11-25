"""Authenticated service administration and expiring capability downloads."""
import argparse
import contextlib
import hmac
import http.server
import ipaddress
import json
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
from . import core as c, links
from .admin import views as layout
from .admin.actions import ACCOUNT_RESUME_MAX_AGE, FormError, manage, resume_accounts
from .admin.forms import parse_form

DEPLOYMENT_RECEIPT = Path('/var/lib/telegram-social-deploy/status.json')
TTL = 3 * 86400


def session(token,at=None):
    if not token:
        return None
    with c.db() as db:
        return db.execute('SELECT * FROM sessions WHERE digest=? AND expires>?',(c.digest(token),time.time() if at is None else at)).fetchone()

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
                receipt = DEPLOYMENT_RECEIPT
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
            target = manage(route, form)
            if target is None:
                return self.respond(404,'Not found')
            return self.redirect(c.BASE+target)
        except (ValueError,KeyError,OverflowError) as error:
            message = str(error) if isinstance(error,FormError) else 'Invalid form or credential settings.'
            return self.respond(400,layout.page('Update failed',f'<p role="alert">{escape(message)}</p><a href="{c.BASE}">Return to settings</a>',signed_in=True))
        except Exception:
            return self.respond(503,'The service is unavailable. Check the connection and try again.')

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
