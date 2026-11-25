"""Durable jobs, bounded admission, private capability links, and URL policy."""
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
from urllib.parse import parse_qs, urlsplit, urlunsplit, urljoin
from . import config
from .limits import LINK_TTL_SECONDS, DIRECT_RETENTION_SECONDS

DATA = config.data_dir()
ORIGIN = os.environ.get('MEDIA_BOT_ORIGIN', '').rstrip('/')
BASE = '/' + os.environ.get('MEDIA_BOT_BASE', '').strip('/')
if BASE != '/':
    BASE += '/'
SECRET_SOCKET = os.environ.get('MEDIA_BOT_SECRET_SOCKET', str(DATA / 'run' / 'broker.sock'))
ACTIVE = ('choice','queued','running','cooldown','sending','paused')
TWITTER_HOSTS = {'x.com','www.x.com','mobile.x.com','twitter.com','www.twitter.com','mobile.twitter.com','m.twitter.com'}
THREADS_HOSTS = {'threads.com','www.threads.com','threads.net','www.threads.net'}
YOUTUBE_HOSTS = {'youtube.com','www.youtube.com','m.youtube.com','youtu.be'}
HOSTS = TWITTER_HOSTS | THREADS_HOSTS | YOUTUBE_HOSTS | {'instagram.com','www.instagram.com','m.instagram.com','tiktok.com','www.tiktok.com','m.tiktok.com','vm.tiktok.com','vt.tiktok.com'}

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
    c.execute('PRAGMA secure_delete=ON')
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

def update_album_index(db, operation, *args):
    """An incomplete optional index write must not undo a delivery receipt or inbox offset."""
    db.execute('SAVEPOINT album_index')
    try:
        operation(db, *args)
    except Exception as error:
        db.execute('ROLLBACK TO album_index')
        logging.getLogger(__name__).warning('Telegram album index unavailable (%s)', type(error).__name__)
        return False
    finally:
        db.execute('RELEASE album_index')
    return True

def initialize():
    from . import pool_checks, access

    if not DATA.is_dir():raise ValueError('Configured data directory is unavailable')
    (DATA/'media').mkdir(exist_ok=True)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, update_key TEXT UNIQUE, chat INTEGER,
          url TEXT, platform TEXT, mode TEXT, state TEXT, message TEXT DEFAULT '', status_id INTEGER,
          bridge_id TEXT, retry_at REAL DEFAULT 0, created REAL, delivered REAL, cancel INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,job TEXT REFERENCES jobs(id), path TEXT,
          name TEXT,size INTEGER,state TEXT DEFAULT 'pending', telegram_id TEXT, UNIQUE(job,path));
        CREATE TABLE IF NOT EXISTS batch_requests(job TEXT PRIMARY KEY REFERENCES jobs(id),
          message_id INTEGER);
        CREATE TABLE IF NOT EXISTS batch_items(job TEXT NOT NULL REFERENCES jobs(id), position INTEGER NOT NULL,
          original_url TEXT NOT NULL, url TEXT NOT NULL, platform TEXT NOT NULL, mode TEXT NOT NULL,
          state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', error_notified INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(job,position));
        CREATE TABLE IF NOT EXISTS links(digest TEXT PRIMARY KEY,file INTEGER REFERENCES files(id),expires REAL);
        CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY,expires REAL,csrf TEXT);
        CREATE TABLE IF NOT EXISTS attempts(address TEXT,time REAL);
        CREATE TABLE IF NOT EXISTS telegram_inbox(id INTEGER PRIMARY KEY,chat INTEGER NOT NULL,
          payload TEXT NOT NULL,received REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
          started REAL,finished REAL);
        CREATE INDEX IF NOT EXISTS telegram_inbox_order ON telegram_inbox(chat,state,id);
        CREATE TABLE IF NOT EXISTS media_budget(platform TEXT,item TEXT,time REAL,PRIMARY KEY(platform,item));
        CREATE INDEX IF NOT EXISTS media_budget_time ON media_budget(platform,time);
        CREATE TABLE IF NOT EXISTS bot_users(id INTEGER PRIMARY KEY,name TEXT NOT NULL DEFAULT '',
          username TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'pending'
          CHECK(state IN ('owner','approved','pending','rejected','revoked')),
          requested REAL NOT NULL,last_seen REAL NOT NULL,notice_at REAL NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS bot_owner ON bot_users(state) WHERE state='owner';
        DROP TABLE IF EXISTS access_events;
        ''')
        # Both the web and worker processes initialize after deployment.
        c.execute('BEGIN IMMEDIATE')
        columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        if 'account_id' not in columns:c.execute("ALTER TABLE jobs ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
        if 'preferences' not in columns:
            c.execute("ALTER TABLE jobs ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")
        # jobs.chat remains the requesting user's ID for permissions and preferences.
        for column in ('group_chat_id', 'original_message_id', 'message_thread_id'):
            if column not in columns:
                c.execute(f'ALTER TABLE jobs ADD COLUMN {column} INTEGER')
        columns={r[1] for r in c.execute('PRAGMA table_info(files)')}
        if 'telegram_media_kind' not in columns:
            c.execute("ALTER TABLE files ADD COLUMN telegram_media_kind TEXT NOT NULL DEFAULT ''")
        if 'force_document' not in columns:
            c.execute('ALTER TABLE files ADD COLUMN force_document INTEGER NOT NULL DEFAULT 0')
        if 'supports_streaming' not in columns:
            c.execute('ALTER TABLE files ADD COLUMN supports_streaming INTEGER NOT NULL DEFAULT 0')
        if 'link_retention_until' not in columns:
            c.execute('ALTER TABLE files ADD COLUMN link_retention_until REAL NOT NULL DEFAULT 0')
        # Older releases used a different name for the digest column.
        if 'token' in {r[1] for r in c.execute('PRAGMA table_info(links)')}:
            c.execute('ALTER TABLE links RENAME COLUMN token TO digest')
        if 'bridge_id' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
            c.execute('ALTER TABLE jobs ADD COLUMN bridge_id TEXT')
        c.execute('DROP TABLE IF EXISTS translation_album_members')
        c.execute('DROP TABLE IF EXISTS translation_albums')
        c.execute('CREATE TABLE IF NOT EXISTS media_expiry(path TEXT PRIMARY KEY, expires REAL NOT NULL)')
        c.execute("INSERT OR IGNORE INTO settings VALUES ('enabled','true')")
        pool_checks.initialize(c)
    if os.environ.get('MEDIA_BOT_ALLOWED_USERS'):
        with db() as connection:
            connection.executemany("""INSERT OR IGNORE INTO bot_users(id,state,requested,last_seen)
                                   VALUES (?,'approved',?,?)""",
                                   [(user,time.time(),time.time()) for user in config._allowed_users()])
    owner = os.environ.get('MEDIA_BOT_OWNER_ID')
    if owner:
        with db() as connection:
            current = connection.execute("SELECT id FROM bot_users WHERE state='owner'").fetchone()
        if not current or current['id'] != int(owner):
            access.set_owner(int(owner))
    from . import bot_ui, admin_notifications
    bot_ui.initialize()
    admin_notifications.initialize()
    from . import retention
    retention.migrate()

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
    allowed={'state','message','status_id','bridge_id','retry_at','delivered','cancel','url','mode','platform','account_id'}
    if not fields or not set(fields)<=allowed:raise ValueError('Invalid update')
    with db() as c:c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),job))

def job_by_id(job):
    with db() as c: row=c.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
    return dict(row) if row else None

def validate_url(url):
    p=urlsplit(url)
    if (p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password
        or p.port not in (None,443) or any(ord(x)<33 for x in url) or len(url)>2048):
        raise ValueError('只接受 Instagram／TikTok／Twitter (X)／Threads／YouTube HTTPS 連結')
    if p.hostname in YOUTUBE_HOSTS:
        if p.hostname!='youtu.be' and p.path=='/watch':
            ids=parse_qs(p.query,keep_blank_values=True).get('v',[])
            video_id=ids[0] if len(ids)==1 else ''
        else:
            prefix='/' if p.hostname=='youtu.be' else '/shorts/'
            match=re.fullmatch(prefix+r'([A-Za-z0-9_-]{11})/?',p.path)
            video_id=match[1] if match else ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
            raise ValueError('請貼上單條 YouTube 影片或 Shorts 連結')
        return 'https://www.youtube.com/watch?v='+video_id
    return urlunsplit(('https',p.hostname,p.path or '/','' if p.hostname in THREADS_HOSTS else p.query,''))

def classify(url):
    p=urlsplit(validate_url(url)); path=p.path.strip('/')
    if p.hostname in YOUTUBE_HOSTS:return 'youtube','post'
    if p.hostname in THREADS_HOSTS:
        if re.fullmatch(r'share/[A-Za-z0-9_-]{1,128}',path):return 'threads','resolve'
        from .threads import post_id
        post_id(url)
        return 'threads','post'
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
    from .message_links import candidates
    return candidates(text)


def extract_message_urls(message):
    from .message_links import extract
    if isinstance(message.get('text'),str):
        text,entities=message['text'],message.get('entities',[])
    else:
        text,entities=message.get('caption',''),message.get('caption_entities',[])
    return extract(text,entities,validate_url,classify)


def reserve_media(platform,item,at=None):
    """Reserve one new media download in a persistent rolling 60-second window.

    Network retries of the same job/file reuse its reservation within the window.
    Existing completed files never reach this function (gallery-dl skips them).
    """
    now=time.time() if at is None else at
    item=digest(item)
    limit=int(get_setting(platform+'_media_limit',20))
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('DELETE FROM media_budget WHERE time<=?',(now-60,))
        if con.execute('SELECT 1 FROM media_budget WHERE platform=? AND item=?',(platform,item)).fetchone():return False
        row=con.execute('SELECT count(*),min(time) FROM media_budget WHERE platform=?',(platform,)).fetchone()
        if row[0]>=limit:raise RateLimited(row[1]+60)
        con.execute('INSERT INTO media_budget VALUES (?,?,?)',(platform,item,now))
    return True

class ResolutionDeferred(Exception):
    """The next redirect hop belongs to a disabled download platform."""
    def __init__(self, url, platform):
        super().__init__(platform)
        self.url = url
        self.platform = platform


def resolve(url):
    """Pinned public address on each HTTPS hop; no cookies on redirect requests."""
    from . import features
    threads_share=urlsplit(validate_url(url)).hostname in THREADS_HOSTS
    for _ in range(6):
        url=validate_url(url); p=urlsplit(url)
        if threads_share and p.hostname not in THREADS_HOSTS:raise ValueError('Threads 分享連結目的地不允許')
        platform,mode=classify(url)
        if mode!='resolve':return url
        if not features.download_enabled(platform):
            raise ResolutionDeferred(url, platform)
        addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):raise ValueError('連結目的地不允許')
        address=addresses[0][4][0]
        conn=http.client.HTTPSConnection(p.hostname,timeout=20)
        conn._create_connection=lambda destination,timeout,source_address=None:socket.create_connection((address,443),timeout)
        try:
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

def _serialized_preferences(value):
    if value is None:return '{}'
    try:
        if not isinstance(value,dict):raise ValueError()
        result=json.dumps(value,ensure_ascii=False,allow_nan=False)
        if len(result.encode('utf-8'))>16384:raise ValueError()
        return result
    except (ValueError,TypeError):raise ValueError('下載偏好格式無效') from None


def _group_destination(group_chat_id, message_id, thread_id):
    if group_chat_id is None:
        return None, None, None
    if type(group_chat_id) is not int or group_chat_id >= 0 or type(message_id) is not int or message_id <= 0:
        raise ValueError('群組或原訊息無效')
    if thread_id is not None and (type(thread_id) is not int or thread_id <= 0):
        raise ValueError('群組話題無效')
    return group_chat_id, message_id, thread_id


def enqueue(key,chat,url,*,preferences=None,group_chat_id=None,original_message_id=None,message_thread_id=None):
    from . import access, features
    url=validate_url(url);platform,mode=classify(url)
    snapshot=_serialized_preferences(preferences)
    destination=_group_destination(group_chat_id, original_message_id, message_thread_id)
    notice=''
    if preferences is not None:
        from .preferences import route
        mode,notice=route(platform,mode,url,preferences.get(platform,{}))
    if group_chat_id is not None and mode=='choice':
        mode='post'
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not access.allowed(chat,c):raise ValueError('此帳號尚未獲准使用')
        old=c.execute('SELECT id FROM jobs WHERE update_key=?',(key,)).fetchone()
        if old:return old[0]
        features.require_downloads((platform,), c)
        count=c.execute("SELECT count(*) FROM jobs WHERE state IN ('choice','queued','running','cooldown','sending','paused')").fetchone()[0]
        if count>=100:raise ValueError('佇列已滿，請等待現有工作完成')
        job=secrets.token_hex(8)
        c.execute('''INSERT INTO jobs(id,update_key,chat,url,platform,mode,state,created,preferences,message,
                  group_chat_id,original_message_id,message_thread_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (job,key,chat,url,platform,mode,'choice' if mode=='choice' else 'queued',time.time(),snapshot,notice,*destination))
        if notice:
            c.execute('INSERT INTO settings(key,value) VALUES (?,?)',('preference_notice:'+job,json.dumps([str(notice)[:500]])))
    return job

def enqueue_batch(key,chat,urls,message_id,*,preferences=None,group_chat_id=None,message_thread_id=None):
    """Create one durable task, including independently invalid links, atomically.

    Batch post links download the post; single-link enqueue retains its choice UI.
    Duplicate URLs remain distinct items in their original order.
    """
    from . import access, features
    if not urls:raise ValueError('請貼上下載連結')
    if len(urls)>50:raise ValueError('每個工作最多支援 50 個連結，請分成多則訊息')
    snapshot=_serialized_preferences(preferences)
    destination=_group_destination(group_chat_id, message_id, message_thread_id)
    items=[];notices=[]
    for position,original in enumerate(urls,1):
        try:
            url=validate_url(original)
            platform,mode=classify(url)
            if preferences is not None:
                from .preferences import route
                mode,notice=route(platform,mode,url,preferences.get(platform,{}))
                if notice:notices.append(f'第 {position} 個連結：'+str(notice)[:500])
            items.append((original,url,platform,'post' if mode=='choice' else mode,'pending',''))
        except ValueError:
            # URL parsing exceptions can include untrusted data; use a fixed message.
            items.append((original,original,'','','failed','連結格式無效或不受支援'))
    first=next((item for item in items if item[4]=='pending'),None)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        if not access.allowed(chat,con):raise ValueError('此帳號尚未獲准使用')
        old=con.execute('SELECT id FROM jobs WHERE update_key=?',(key,)).fetchone()
        if old:return old[0]
        features.require_downloads((item[2] for item in items if item[4]=='pending'), con)
        count=con.execute("SELECT count(*) FROM jobs WHERE state IN ('choice','queued','running','cooldown','sending','paused')").fetchone()[0]
        if count>=100:raise ValueError('佇列已滿，請等待現有工作完成')
        job=secrets.token_hex(8)
        con.execute('''INSERT INTO jobs(id,update_key,chat,url,platform,mode,state,created,preferences,
                    group_chat_id,original_message_id,message_thread_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (job,key,chat,first[1] if first else '',first[2] if first else 'instagram','batch','queued',time.time(),snapshot,*destination))
        if notices:
            con.execute('INSERT INTO settings(key,value) VALUES (?,?)',('preference_notice:'+job,json.dumps(notices)))
        con.execute('INSERT INTO batch_requests(job,message_id) VALUES (?,?)',(job,message_id))
        con.executemany('INSERT INTO batch_items(job,position,original_url,url,platform,mode,state,error) VALUES (?,?,?,?,?,?,?,?)',
                        [(job,n,*item) for n,item in enumerate(items)])
    return job

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self,path,timeout=30):super().__init__('localhost',timeout=timeout);self.path=path
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect(self.path)

def rpc(socket_path,path,payload=None):
    c=UnixHTTP(socket_path)
    try:
        c.request('POST' if payload is not None else 'GET',path,json.dumps(payload) if payload is not None else None,{'Content-Type':'application/json'})
        r=c.getresponse();data=json.loads(r.read(2*1024*1024))
        if r.status>=400:raise ValueError(data.get('error','內部服務暫時不可用'))
        return data
    finally:c.close()

def digest(value):return hashlib.sha256(value.encode()).hexdigest()

def safe_file(path):
    p=Path(path)
    root=(DATA/'media').resolve()
    if p.resolve()!=p or p==root or not p.is_relative_to(root):raise ValueError('Invalid file')
    return p

def cleanup():
    from . import retention
    retention.sweep()
