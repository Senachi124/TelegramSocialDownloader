"""Durable download jobs, bounded admission, and supported URL policy."""
from contextlib import contextmanager
import hashlib
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, urljoin

from . import config

DATA = config.data_dir()
ACTIVE = ('choice','queued','running','cooldown','sending','paused')
TWITTER_HOSTS = {'x.com','www.x.com','mobile.x.com','twitter.com','www.twitter.com','mobile.twitter.com','m.twitter.com'}
HOSTS = TWITTER_HOSTS | {'instagram.com','www.instagram.com','m.instagram.com','tiktok.com','www.tiktok.com','m.tiktok.com','vm.tiktok.com','vt.tiktok.com'}

class Connection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()

class RateLimited(ValueError):
    def __init__(self,until):self.until=until;super().__init__('平台限流，等待冷卻後續作')

def db():
    c = sqlite3.connect(DATA/'state.sqlite3', timeout=15,factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


@contextmanager
def db_keeper():
    """Keep WAL attachment alive without holding a cursor or read transaction.

    The worker owns this otherwise idle connection. Normal short transactions
    still use db(); durability and WAL checkpoint settings remain unchanged.
    """
    connection=sqlite3.connect((DATA/'state.sqlite3').resolve().as_uri()+'?mode=rw',uri=True,timeout=15)
    try:
        cursor=connection.execute('SELECT name FROM sqlite_master')
        try:cursor.fetchall()
        finally:cursor.close()
        yield connection
    finally:connection.close()


def is_busy(error):
    code=getattr(error,'sqlite_errorcode',None)
    return isinstance(error,sqlite3.OperationalError) and (
        (code is not None and code & 255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED))
        or str(error) in ('database is locked','database table is locked'))


def retry_write(callback,max_seconds=120):
    """Retry one DB-only transaction; callback must never perform external I/O.

    A successful callback can run again if commit itself encounters contention.
    Telegram sends, filesystem changes and credential calls must stay outside it.
    """
    deadline=time.monotonic()+max(0,max_seconds)
    while True:
        try:
            with db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                result=callback(connection)
            return result
        except sqlite3.OperationalError as error:
            remaining=deadline-time.monotonic()
            if not is_busy(error) or remaining<=0:raise
            logging.getLogger(__name__).warning('Local database write delayed (%s); retrying',type(error).__name__)
            time.sleep(min(1,remaining))

def initialize():
    if not DATA.is_dir():raise ValueError('Configured data directory is unavailable')
    (DATA/'media').mkdir(mode=0o700,exist_ok=True)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, update_key TEXT UNIQUE, chat INTEGER,
          url TEXT, platform TEXT, mode TEXT, state TEXT, message TEXT DEFAULT '', status_id INTEGER,
          retry_at REAL DEFAULT 0, created REAL, delivered REAL, cancel INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,job TEXT REFERENCES jobs(id), path TEXT,
          name TEXT,size INTEGER,state TEXT DEFAULT 'pending', telegram_id TEXT, UNIQUE(job,path));
        CREATE TABLE IF NOT EXISTS batch_requests(job TEXT PRIMARY KEY REFERENCES jobs(id),
          message_id INTEGER);
        CREATE TABLE IF NOT EXISTS batch_items(job TEXT NOT NULL REFERENCES jobs(id), position INTEGER NOT NULL,
          original_url TEXT NOT NULL, url TEXT NOT NULL, platform TEXT NOT NULL, mode TEXT NOT NULL,
          state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', error_notified INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(job,position));
        CREATE TABLE IF NOT EXISTS telegram_inbox(id INTEGER PRIMARY KEY,chat INTEGER NOT NULL,
          payload TEXT NOT NULL,received REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
          started REAL,finished REAL);
        CREATE INDEX IF NOT EXISTS telegram_inbox_order ON telegram_inbox(chat,state,id);
        CREATE TABLE IF NOT EXISTS media_budget(platform TEXT,item TEXT,time REAL,PRIMARY KEY(platform,item));
        CREATE INDEX IF NOT EXISTS media_budget_time ON media_budget(platform,time);
        ''')

def get_setting(key, default=None):
    with db() as c: row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default

def setting(key,value):
    setting_many({key:value})


def setting_many(values):
    """Skip unchanged settings and combine changed values into one transaction."""
    serialized=[(key,json.dumps(value)) for key,value in values.items()]
    if not serialized:return
    changed=[]
    with db() as connection:
        for key,value in serialized:
            row=connection.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            if row is None or row[0]!=value:changed.append((key,value))
    if not changed:return
    with db() as connection:
        connection.executemany('''INSERT INTO settings(key,value) VALUES (?,?)
          ON CONFLICT(key) DO UPDATE SET value=excluded.value
          WHERE settings.value IS NOT excluded.value''',changed)

def update(job, **fields):
    allowed={'state','message','status_id','retry_at','delivered','cancel','url','mode','platform'}
    if not fields or not set(fields)<=allowed:raise ValueError('Invalid update')
    with db() as c:c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),job))

def job_by_id(job):
    with db() as c: row=c.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
    return dict(row) if row else None

def validate_url(url):
    p=urlsplit(url)
    if (p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password
        or p.port not in (None,443) or any(ord(x)<33 for x in url) or len(url)>2048):
        raise ValueError('只接受 Instagram／TikTok／Twitter (X) HTTPS 連結')
    return urlunsplit(('https',p.hostname,p.path or '/',p.query,''))

def classify(url):
    p=urlsplit(validate_url(url)); path=p.path.strip('/')
    if p.hostname in TWITTER_HOSTS:
        from .twitter import post_id
        post_id(url)
        return 'twitter','post'
    platform='instagram' if p.hostname.endswith('instagram.com') else 'tiktok'
    if platform=='instagram':
        if re.fullmatch(r'(p|reel|reels|tv)/[A-Za-z0-9_-]+',path):return platform,'choice'
        if re.fullmatch(r'stories/[A-Za-z0-9._]+(?:/\d+)?',path):return platform,'stories'
        if re.fullmatch(r'[A-Za-z0-9._]{1,30}',path) and path not in {'explore','accounts','direct','stories','reels'}:return platform,'stories'
    else:
        if re.fullmatch(r'@[\w.-]+/(video|photo)/\d+',path):return platform,'choice'
        if re.fullmatch(r'@[\w.-]+(?:/stories(?:/\d+)?)?',path):return platform,'stories'
    return platform,'resolve'

def extract_urls(text):
    # Bare TikTok shares are accepted only at a text-token boundary. Keep HTTPS
    # links intact so a nested/lookalike URL cannot become a different request.
    urls=re.findall(r'https://[^\s<>]+|(?<![^\s<])(?:vt|vm)\.tiktok\.com/[^\s<>]+',text)
    return [url if url.startswith('https://') else 'https://'+url for url in urls]

def reserve_media(platform,item,at=None):
    """Reserve one new media download in a persistent rolling 60-second window.

    Network retries of the same job/file reuse its reservation within the window.
    Existing completed files never reach this function (gallery-dl skips them).
    """
    now=time.time() if at is None else at
    limit=int(get_setting(platform+'_media_limit',20))
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('DELETE FROM media_budget WHERE time<=?',(now-60,))
        if con.execute('SELECT 1 FROM media_budget WHERE platform=? AND item=?',(platform,item)).fetchone():return False
        row=con.execute('SELECT count(*),min(time) FROM media_budget WHERE platform=?',(platform,)).fetchone()
        if row[0]>=limit:raise RateLimited(row[1]+60)
        con.execute('INSERT INTO media_budget VALUES (?,?,?)',(platform,item,now))
    return True

def resolve(url):
    """Pinned public address on each HTTPS hop; no cookies on redirect requests."""
    for _ in range(6):
        url=validate_url(url); p=urlsplit(url)
        if classify(url)[1]!='resolve':return url
        addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):raise ValueError('連結目的地不允許')
        address=addresses[0][4][0]
        conn=http.client.HTTPSConnection(p.hostname,timeout=20)
        conn._create_connection=lambda destination,timeout,source_address=None:socket.create_connection((address,443),timeout)
        try:
            platform=classify(url)[0]
            conn.request('GET',p.path+('?' + p.query if p.query else ''),headers={'User-Agent':'Mozilla/5.0'})
            r=conn.getresponse()
            if r.status==429:
                value=r.getheader('Retry-After','900')
                try:until=time.time()+float(value)
                except ValueError:
                    try:until=parsedate_to_datetime(value).timestamp()
                    except Exception:until=time.time()+900
                until=max(time.time()+900,until)
                setting(platform+'_cooldown',until)
                raise RateLimited(until)
            if platform=='tiktok' and r.status in (401,403):
                from .tiktok import failure_message
                raise ValueError(failure_message('forbidden' if r.status==403 else 'login_required','post'))
            if r.status not in (301,302,303,307,308):raise ValueError('無法解析分享連結，請貼上完整網址')
            url=urljoin(url,r.getheader('Location',''))
        finally:conn.close()
    raise ValueError('分享連結重新導向次數過多')

def enqueue(key,chat,url):
    url=validate_url(url);platform,mode=classify(url)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not config.allowed(chat):raise ValueError('此帳號尚未獲准使用')
        old=c.execute('SELECT id FROM jobs WHERE update_key=?',(key,)).fetchone()
        if old:return old[0]
        count=c.execute("SELECT count(*) FROM jobs WHERE state IN ('choice','queued','running','cooldown','sending','paused')").fetchone()[0]
        if count>=100:raise ValueError('佇列已滿，請等待現有工作完成')
        job=secrets.token_hex(8)
        c.execute('INSERT INTO jobs(id,update_key,chat,url,platform,mode,state,created) VALUES (?,?,?,?,?,?,?,?)',
                  (job,key,chat,url,platform,mode,'choice' if mode=='choice' else 'queued',time.time()))
    return job

def enqueue_batch(key,chat,urls,message_id):
    """Create one durable task, including independently invalid links, atomically.

    Batch post links download the post; single-link enqueue retains its choice UI.
    Duplicate URLs remain distinct items in their original order.
    """
    if not urls:raise ValueError('請貼上下載連結')
    if len(urls)>50:raise ValueError('每個工作最多支援 50 個連結，請分成多則訊息')
    items=[]
    for original in urls:
        try:
            url=validate_url(original)
            platform,mode=classify(url)
            items.append((original,url,platform,'post' if mode=='choice' else mode,'pending',''))
        except ValueError:
            # URL parsing exceptions can include untrusted data; use a fixed message.
            items.append((original,original,'','','failed','連結格式無效或不受支援'))
    first=next((item for item in items if item[4]=='pending'),None)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        if not config.allowed(chat):raise ValueError('此帳號尚未獲准使用')
        old=con.execute('SELECT id FROM jobs WHERE update_key=?',(key,)).fetchone()
        if old:return old[0]
        count=con.execute("SELECT count(*) FROM jobs WHERE state IN ('choice','queued','running','cooldown','sending','paused')").fetchone()[0]
        if count>=100:raise ValueError('佇列已滿，請等待現有工作完成')
        job=secrets.token_hex(8)
        con.execute('INSERT INTO jobs(id,update_key,chat,url,platform,mode,state,created) VALUES (?,?,?,?,?,?,?,?)',
                    (job,key,chat,first[1] if first else '',first[2] if first else 'instagram','batch','queued',time.time()))
        con.execute('INSERT INTO batch_requests(job,message_id) VALUES (?,?)',(job,message_id))
        con.executemany('INSERT INTO batch_items(job,position,original_url,url,platform,mode,state,error) VALUES (?,?,?,?,?,?,?,?)',
                        [(job,n,*item) for n,item in enumerate(items)])
    return job



def digest(value):return hashlib.sha256(value.encode()).hexdigest()


def safe_file(path):
    p=Path(path)
    if p.is_symlink() or not p.resolve().is_relative_to((DATA/'media').resolve()):raise ValueError('Invalid file')
    return p

def cleanup():
    # Only confirmed deliveries expire. Bound each pass, skip expired rows, and
    # release the writer between files instead of holding it over the whole media walk.
    eligible="""FROM files f JOIN jobs j ON j.id=f.job
      WHERE f.state='sent' AND j.state='completed' AND j.delivered<?"""
    now=time.time()
    with db() as c:
        rows=c.execute('SELECT f.id '+eligible+' ORDER BY f.id LIMIT 16',(now-86400,)).fetchall()
    for row in rows:
        with db() as c:
            # Serialize the final eligibility check with job changes.
            # A failed BEGIN leaves the file untouched for a later pass.
            c.execute('BEGIN IMMEDIATE')
            now=time.time()
            current=c.execute('SELECT f.path '+eligible+' AND f.id=?',
                              (now-86400,row['id'])).fetchone()
            if current is None:continue
            safe_file(current['path']).unlink(missing_ok=True)
            c.execute("UPDATE files SET state='expired' WHERE id=?",(row['id'],))
    with db() as c:
        c.execute("UPDATE jobs SET state='cancelled',message='選擇已逾時' WHERE state='choice' AND created<?",(time.time()-86400,))
