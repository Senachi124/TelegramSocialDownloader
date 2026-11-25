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
import sqlite3
import sys
import time
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import admin_auth, local_socket
from . import core as c, links
from .admin import fanclub_session, feature_controls, views as layout
from .admin.actions import ACTIONS, ACCOUNT_RESUME_MAX_AGE, destination, manage, resume_accounts
from .admin.forms import FormError, parse_form

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
    def respond(self,status,body='',headers=(),mime='text/html; charset=utf-8', *, copy_script=False):
        raw=body.encode() if isinstance(body,str) else body
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','same-origin')
        policy = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        if copy_script:
            policy += f"; script-src 'sha256-{fanclub_session.COPY_HASH}'"
        self.send_header('Content-Security-Policy', policy)
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
        body = layout.form('login',layout.hidden_field('csrf',csrf),controls+layout.button('Sign in'))
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
            if route not in layout.PAGES:
                return self.respond(404,'Not found')
            cookies = self.cookies()
            signed_in = session(cookies.get('mb_session'))
            if not signed_in:
                return self.redirect(c.BASE+'login')
            hidden = layout.hidden_field('csrf',signed_in['csrf'])
            if route=='users':
                offset = parse_qs(path.query).get('offset',['0'])[0]
                if not re.fullmatch(r'[0-9]{1,7}',offset):
                    return self.respond(400,'Invalid page')
                body = layout.users(hidden,int(offset))
            elif route=='features':
                body = feature_controls.panel(hidden)
            elif route=='subscriptions':
                from . import push
                result = fanclub_session.read_result(cookies.get('mb_fc_test', ''), signed_in['csrf'])
                body = push.web_panel(hidden, result)
            elif route=='errors':
                from . import error_log
                body = layout.errors(error_log.recent())
            elif route=='accounts':
                platform = parse_qs(path.query).get('platform', [''])[0]
                body = layout.pools(hidden, selected_platform=platform)
            elif route=='keys':
                body = layout.keys(hidden)
            else:
                body = layout.connection(hidden)
            saved = ACTIONS.get(cookies.get('mb_update'))
            notice = saved[2] if saved and saved[0] == route else ''
            headers = [self.cookie('mb_update', '', 0)] if notice else []
            if route == 'subscriptions' and 'mb_fc_test' in cookies:
                headers.append(self.cookie('mb_fc_test', '', 0))
            return self.respond(200,layout.page(layout.PAGES[route], body, signed_in=True,
                                route=route, session_fields=hidden, notice=notice), headers,
                                copy_script=route=='subscriptions')
        except (OSError,ValueError,KeyError,sqlite3.Error):
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
        route = None
        signed_in = None
        form = {}
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
            cookies = self.cookies()
            content_type = self.headers.get('Content-Type','')
            if route=='login':
                return self._login(parse_form(content_type, self.rfile.read(length)),cookies)
            signed_in = session(cookies.get('mb_session',''))
            if not signed_in:
                return self.respond(401, layout.page('Session expired',
                    f'<p>Sign in to continue editing settings.</p><a href="{c.BASE}login">Sign in again</a>'))
            if route == 'account' and content_type.lower().startswith('multipart/form-data'):
                # Only the YouTube editor accepts files. Keep its return link if decoding fails.
                form = {'platform': 'youtube'}
            form = parse_form(content_type, self.rfile.read(length), allow_upload=route=='account')
            if not csrf_matches(form.get('csrf'),signed_in['csrf']):
                return self.respond(403,'Expired form')
            if route=='fanclub-test':
                result = fanclub_session.test_saved(form)
                feedback = fanclub_session.result_cookie(result, signed_in['csrf'])
                return self.redirect(c.BASE+'subscriptions#fanclub-connection', [self.cookie('mb_fc_test', feedback, 45)])
            if route=='logout':
                with c.db() as db:
                    db.execute('DELETE FROM sessions WHERE digest=?',(signed_in['digest'],))
                return self.redirect(c.BASE+'login',[self.cookie('mb_session','',0),self.cookie('mb_update','',0)])
            target = manage(route, form)
            if target is None:
                return self.respond(404,'Not found')
            return self.redirect(c.BASE+target, [self.cookie('mb_update', route.split('/', 1)[0], 45)])
        except (ValueError,KeyError,OverflowError) as error:
            message = str(error) if isinstance(error,FormError) else 'Invalid form or credential settings.'
            status = error.status if isinstance(error,FormError) else 400
            target = destination(route, form) if signed_in else 'login'
            panel = urlsplit(target).path
            label = 'Return to ' + layout.PAGES[panel] if panel in layout.PAGES else 'Sign in'
            body = f'<p role="alert">{escape(message)}</p><a href="{c.BASE}{escape(target,quote=True)}">{escape(label)}</a>'
            fields = layout.hidden_field('csrf',signed_in['csrf']) if signed_in else ''
            return self.respond(status,layout.page('Update failed',body,signed_in=bool(signed_in),
                                                   route=panel,session_fields=fields))
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
