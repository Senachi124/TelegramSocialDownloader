"""Optional loopback capability downloads; raw tokens never enter the database/logs."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import secrets
import stat
import threading
import time
from urllib.parse import quote, urlsplit
from . import config, core as c, limits


def configuration():
    base=os.environ.get('MEDIA_BOT_DOWNLOAD_BASE_URL','').rstrip('/')
    if not base:return '',None
    try:
        parsed=urlsplit(base)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None,443)
                or any(ord(ch)<33 for ch in base)):
            raise ValueError()
        port=int(os.environ.get('MEDIA_BOT_DOWNLOAD_PORT','8090'))
        if not 1<=port<=65535:raise ValueError()
    except ValueError:
        raise config.ConfigurationError('Download base requires HTTPS and a valid local port') from None
    return base,port


def enabled():return bool(configuration()[0])


def create(file_id):
    base,_=configuration()
    if not base:raise ValueError('Download URL is not configured')
    token=secrets.token_urlsafe(32);expires=time.time()+limits.LINK_TTL_SECONDS
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT f.path,f.state,j.chat,j.cancel FROM files f JOIN jobs j ON j.id=f.job WHERE f.id=?',(file_id,)).fetchone()
        if not row or row['cancel'] or row['state'] in ('expired','too_large') or not config.allowed(row['chat']):
            raise ValueError('File unavailable')
        path=c.safe_file(row['path'])
        if not path.is_file() or path.stat().st_size>limits.MAX_DOWNLOAD_BYTES:
            raise ValueError('File unavailable')
        db.execute('INSERT INTO links(token,file,expires) VALUES (?,?,?)',(c.digest(token),file_id,expires))
        db.execute('UPDATE files SET link_retention_until=max(link_retention_until,?) WHERE id=?',(expires,file_id))
    return base+'/'+token


def byte_range(value,size):
    if value is None:return 0,size-1,False
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
    if not match or not any(match.groups()) or size<=0:raise ValueError()
    left,right=match.groups()
    if left:
        start=int(left);end=min(int(right),size-1) if right else size-1
    else:
        length=int(right)
        if length<=0:raise ValueError()
        start=max(0,size-length);end=size-1
    if start>end or start>=size:raise ValueError()
    return start,end,True


class Handler(BaseHTTPRequestHandler):
    server_version='Download'
    sys_version=''
    def setup(self):
        super().setup();self.connection.settimeout(20)
    def log_message(self,*args):pass
    def do_GET(self):self.transfer(False)
    def do_HEAD(self):self.transfer(True)
    def transfer(self,head):
        # Proxy maps the configured external prefix to / on this loopback server.
        match=re.fullmatch(r'/([A-Za-z0-9_-]{43})',self.path)
        if not match:self.send_error(404);return
        with c.db() as db:
            row=db.execute('''SELECT f.path,f.name,f.state,j.chat,j.cancel,l.expires
                FROM links l JOIN files f ON f.id=l.file JOIN jobs j ON j.id=f.job
                WHERE l.token=? AND l.expires>?''',(c.digest(match[1]),time.time())).fetchone()
        if not row or row['cancel'] or row['state']=='expired' or not config.allowed(row['chat']):
            self.send_error(404);return
        try:
            path=c.safe_file(row['path']).resolve(strict=True)
            descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        except (ValueError,OSError):self.send_error(404);return
        with os.fdopen(descriptor,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>limits.MAX_DOWNLOAD_BYTES:
                self.send_error(404);return
            try:start,end,partial=byte_range(self.headers.get('Range'),info.st_size)
            except ValueError:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{info.st_size}')
                self.send_header('Content-Length','0');self.end_headers();return
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type','application/octet-stream')
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(row['name'],safe=''))
            self.send_header('Content-Length',str(max(0,end-start+1)))
            self.send_header('Accept-Ranges','bytes')
            self.send_header('Cache-Control','private, no-store')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('X-Content-Type-Options','nosniff')
            if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{info.st_size}')
            self.end_headers()
            if head:return
            stream.seek(start);remaining=end-start+1
            try:
                while remaining>0 and time.time()<row['expires']:
                    chunk=stream.read(min(256*1024,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)
            except (OSError,TimeoutError):pass


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address):
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(address,Handler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()
    def handle_error(self,*args):pass


@contextmanager
def serving():
    _,port=configuration()
    if port is None:
        yield;return
    server=Server(('127.0.0.1',port))
    thread=threading.Thread(target=server.serve_forever,daemon=True,name='download-links')
    thread.start()
    try:yield
    finally:server.shutdown();server.server_close();thread.join(2)
