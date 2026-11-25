"""Durable, ordered Takaneko notifications. Historical inventory is never replayed.

External sends are checkpointed as uncertain BEFORE I/O. Only a definite rejection
can be retried automatically; a crash or ambiguous transport needs human review.
"""
import contextlib
import fcntl
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import state as c, transport
from media_bot import limits
from media_bot import download_size

POLL_SECONDS = 300
EXCLUDED_POSTS = frozenset(filter(None, os.getenv('TAKANEKO_EXCLUDED_POSTS', '').split(',')))


def require_allowed_post(post_id):
    if post_id in EXCLUDED_POSTS:raise ValueError('此舊文章已永久取消通知')


_guard_state=threading.local()
_AUTH_LOCK=threading.Lock()
_LOG=logging.getLogger(__name__)
_telegram_cooldown=0.0


def initialize():
    with c.db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS tk_subscriptions(user_id INTEGER PRIMARY KEY,
          enabled INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tk_posts(id TEXT PRIMARY KEY,summary TEXT NOT NULL,state TEXT NOT NULL,
          discovered REAL NOT NULL,plan TEXT,error TEXT NOT NULL DEFAULT '',retry_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tk_deliveries(id INTEGER PRIMARY KEY,post_id TEXT NOT NULL REFERENCES tk_posts(id),
          user_id INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
          created REAL NOT NULL,error TEXT NOT NULL DEFAULT '',retry_at REAL NOT NULL DEFAULT 0,
          test_key TEXT NOT NULL DEFAULT '',UNIQUE(post_id,user_id,test_key));
        CREATE TABLE IF NOT EXISTS tk_parts(delivery_id INTEGER REFERENCES tk_deliveries(id),position INTEGER,
          payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',telegram_id TEXT,
          PRIMARY KEY(delivery_id,position));
        CREATE TABLE IF NOT EXISTS tk_review_attempts(
          delivery_id INTEGER PRIMARY KEY REFERENCES tk_deliveries(id) ON DELETE CASCADE,
          position INTEGER NOT NULL, attempt TEXT NOT NULL, active INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS tk_uncertain_deliveries ON tk_deliveries(id) WHERE state='uncertain';
        CREATE INDEX IF NOT EXISTS tk_active_review_attempts ON tk_review_attempts(delivery_id) WHERE active=1;
        CREATE TRIGGER IF NOT EXISTS tk_finish_review_attempt AFTER UPDATE OF state ON tk_parts
          WHEN OLD.state='uncertain' AND NEW.state!='uncertain'
          BEGIN
            DELETE FROM tk_review_attempts WHERE delivery_id=OLD.delivery_id AND position=OLD.position;
          END;
        CREATE TABLE IF NOT EXISTS tk_links(digest TEXT PRIMARY KEY,delivery_id INTEGER NOT NULL REFERENCES tk_deliveries(id),
          path TEXT NOT NULL,name TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tk_link_retention(path TEXT PRIMARY KEY,post_id TEXT NOT NULL,
          expires REAL NOT NULL);
        ''')
        # Give unresolved pre-upgrade checkpoints a stable identity. Reopening
        # the database must never replace a token or release a live sender.
        db.execute("""INSERT OR IGNORE INTO tk_review_attempts(delivery_id,position,attempt,active)
                      SELECT d.id, MIN(p.position), lower(hex(randomblob(16))), 0
                      FROM tk_deliveries d JOIN tk_parts p ON p.delivery_id=d.id
                      WHERE d.state='uncertain' AND p.state='uncertain'
                      GROUP BY d.id HAVING COUNT(*)=1""")
        db.execute("INSERT OR IGNORE INTO settings VALUES ('takaneko_enabled','false')")


def subscribed(user_id, db=None):
    if db is None:
        with c.db() as db:return subscribed(user_id,db)
    row=db.execute('SELECT enabled FROM tk_subscriptions WHERE user_id=?',(user_id,)).fetchone()
    return bool(row and row[0])


def set_subscription(user_id, enabled):
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO tk_subscriptions VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',
                   (user_id,int(bool(enabled)),time.time()))
        if not enabled:
            db.execute("UPDATE tk_deliveries SET state='cancelled' WHERE user_id=? AND state IN ('pending','ready','paused')",(user_id,))
            db.execute('DELETE FROM tk_links WHERE delivery_id IN (SELECT id FROM tk_deliveries WHERE user_id=?)',(user_id,))


def is_enabled(db=None):
    if db is None:
        with c.db() as db:
            return is_enabled(db)
    row = db.execute("SELECT value FROM settings WHERE key='takaneko_enabled'").fetchone()
    return bool(row and row[0] == 'true')


def set_enabled(enabled):
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if enabled == is_enabled(db):
            return
        db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_enabled', ?)", (json.dumps(enabled),))
        if enabled:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_next_poll', '0')")


def record_inventory(posts):
    """Commit only a COMPLETE API inventory. No partial baseline can enable sending."""
    if not isinstance(posts,list) or any(not isinstance(p,dict) or not p.get('id') for p in posts):
        raise ValueError('文章清單無效')
    if len({p['id'] for p in posts})!=len(posts):raise ValueError('文章清單重複，暫停初始化')
    # Validate/serialize before taking the writer lock, including known posts.
    prepared=[(post,json.dumps(post,ensure_ascii=False)) for post in sorted(posts,key=lambda p:(p.get('published',0),p['id']))]
    now=time.time()
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not is_enabled(db):
            return
        known={row[0] for row in db.execute('SELECT id FROM tk_posts')}
        initial=not db.execute("SELECT 1 FROM settings WHERE key='takaneko_initialized' AND value='true'").fetchone()
        users=[r[0] for r in db.execute("SELECT user_id FROM tk_subscriptions WHERE enabled=1")]
        for post,serialized in prepared:
            if post['id'] in EXCLUDED_POSTS:continue
            if post['id'] in known:continue
            added=db.execute('INSERT OR IGNORE INTO tk_posts(id,summary,state,discovered) VALUES (?,?,?,?)',
                             (post['id'],serialized,'baseline' if initial else 'pending',now)).rowcount
            if added and not initial:
                for user_id in users:
                    db.execute('INSERT INTO tk_deliveries(post_id,user_id,created) VALUES (?,?,?)',(post['id'],user_id,now))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_initialized','true')")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_last_poll',?)",(json.dumps(now),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_poll_error','\"\"')")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_error','\"\"')")


def credentials():return transport.secret('get')


def test_session():
    from . import session_probe

    try:
        with deployment_guard() as available:
            if not available or not _AUTH_LOCK.acquire(blocking=False):
                return 'busy'
            try:
                # Bound DNS, slow reads, and the connection as a single operation.
                # The child gets the saved login directly from the credential broker.
                process = subprocess.run([sys.executable, '-IB', '-m', 'takaneko_telegram.session_probe'],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         timeout=9, check=False)
                return session_probe.RESULTS.get(process.returncode, 'unavailable')
            finally:
                _AUTH_LOCK.release()
    except (OSError, subprocess.TimeoutExpired):
        return 'unavailable'


def persist_session(client, original):
    updated=getattr(client,'session_snapshot',None)
    if callable(updated):updated=updated()
    if not isinstance(updated,dict):updated=getattr(client,'session',None)
    if not isinstance(updated,dict):return
    if any(updated.get(k)!=original.get(k) for k in ('accessToken','refreshToken')):
        from .secrets import session_digest
        transport.secret('refresh',dict(expected_session_digest=session_digest(original),
              accessToken=updated.get('accessToken',''),refreshToken=updated.get('refreshToken','')))


def poll_once(client=None):
    from .takaneko_api import Client
    with _AUTH_LOCK:
        if not is_enabled():
            return 0
        original={}
        if client is None:
            original=credentials().get('session',{})
            if not original:
                return 0
            client=Client(original)
        try:posts=client.list_posts()
        finally:
            if original:persist_session(client,original)
    record_inventory(posts)
    return len(posts)


def storage_guard():
    # Recheck identity during long transfers; do not merely trust a directory.
    refresh=time.monotonic()>=getattr(_guard_state,'until',0)
    c.storage_guard(verify_identity=refresh)
    if refresh:
        _guard_state.until=time.monotonic()+3


def post_directory(post_id):
    return c.MEDIA/hashlib.sha256(post_id.encode()).hexdigest()


def atomic_json(path,value):
    pending=path.with_suffix(path.suffix+'.pending')
    with pending.open('w') as out:
        json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
    pending.replace(path)


def embedded_video(url, directory, position):
    from .takaneko_api import canonical_video_url
    url=canonical_video_url(url)
    storage_guard()
    output=directory/f'video-{position}'
    receipt=directory/f'video-{position}.json'
    if receipt.exists():
        path=c.safe_file(json.loads(receipt.read_text())['path'])
        if path.is_file() and path.stat().st_size:
            download_size.check(path.stat().st_size)
            return path
    command=[sys.executable,'-m','takaneko_telegram.embedded_runner']
    # stdout/stderr stay in service storage and are never logged (URLs can be signed).
    log=directory/f'video-{position}.log'
    with log.open('w+') as out:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=out,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
        process.stdin.write(json.dumps({'url':url,'output':str(output)}));process.stdin.close()
        started=time.monotonic()
        try:
            while process.poll() is None:
                time.sleep(.2)
                for candidate in directory.glob(output.name+'*'):
                    if candidate.is_file():download_size.check(candidate.stat().st_size)
                if time.monotonic()-started>900:raise ValueError('影片下載逾時，保留進度')
                storage_guard()
            if process.returncode:raise ValueError('影片來源暫時無法下載，保留進度')
            out.seek(0);lines=out.read(32768).splitlines()
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    if not lines:raise ValueError('影片下載未完成')
    result=json.loads(lines[-1])
    if result.get('too_large'):raise download_size.DownloadTooLarge()
    if not result.get('ok'):raise ValueError('影片來源暫時無法下載，保留進度')
    path=c.safe_file(result['path'])
    if path.parent!=directory or not path.is_file() or not path.stat().st_size:raise ValueError('影片檔案無效')
    download_size.check(path.stat().st_size)
    atomic_json(receipt,{'path':str(path)})
    log.unlink(missing_ok=True)
    return path


def prepare_post(post, config=None):
    require_allowed_post(post['id'])
    from .takaneko_api import Client
    from .takaneko_content import article_blocks
    config=config or credentials()
    storage_guard()
    directory=post_directory(post['id']);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    client=Client(config.get('session',{}))
    source=directory/'source.json'
    if source.exists():detail=json.loads(source.read_text())
    else:
        with _AUTH_LOCK:
            original=credentials().get('session',{})
            client=Client(original)
            try:detail=client.detail(json.loads(post['summary']))
            finally:persist_session(client,original)
        atomic_json(source,detail)
        (directory/'source.html').write_text(detail.get('html',''))
    translated=directory/'article-layout-v3.json'
    if translated.exists():blocks=json.loads(translated.read_text())
    else:
        blocks=article_blocks(detail,config)
        atomic_json(translated,blocks)
    media_directory=directory/'layout-v3'
    media_directory.mkdir(exist_ok=True,mode=0o700)
    plan=[]
    for i,block in enumerate(blocks):
        if block['kind']=='text':
            plan.append({'kind':'text','html':block.get('html',block.get('text',''))});continue
        url=block['url'];host=urlsplit(url).hostname
        if block['kind']=='video' and host in {'player.vimeo.com','vimeo.com','www.vimeo.com','youtube.com','www.youtube.com','www.youtube-nocookie.com','youtu.be'}:
            path=embedded_video(url,media_directory,i)
        else:
            suffix=Path(urlsplit(url).path).suffix.lower()
            if suffix not in {'.jpg','.jpeg','.png','.gif','.webp','.mp4','.mov','.mp3','.pdf'}:suffix='.bin'
            target=media_directory/f'media-{i}{suffix}'
            receipt=media_directory/f'media-{i}.json'
            if not receipt.exists() or not target.is_file() or target.stat().st_size!=json.loads(receipt.read_text()).get('size'):
                storage_guard();client.download(url,target,storage_guard)
                atomic_json(receipt,{'size':target.stat().st_size})
            path=target
        plan.append({'kind':block['kind'],'path':str(path),'name':path.name,'size':path.stat().st_size})
    if not plan:raise ValueError('文章沒有可傳送內容，需檢查來源')
    with c.db() as db:
        db.execute("UPDATE tk_posts SET plan=?,state='ready',error='' WHERE id=?",(json.dumps(plan,ensure_ascii=False),post['id']))


def link(delivery_id, part):
    token=secrets.token_urlsafe(32)
    expires=time.time()+limits.LINK_TTL_SECONDS
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        post=db.execute('SELECT post_id FROM tk_deliveries WHERE id=?',(delivery_id,)).fetchone()
        if not post:raise ValueError('找不到通知工作')
        db.execute('INSERT INTO tk_links VALUES (?,?,?,?,?)',(c.digest(token),delivery_id,part['path'],part['name'],expires))
        db.execute('INSERT INTO tk_link_retention VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET expires=max(expires,excluded.expires)',
                   (part['path'],post['post_id'],expires))
    return c.LINK_BASE+token


def cleanup_links():
    """Expire newly linked media only after every shared-post delivery is confirmed."""
    eligible='''FROM tk_link_retention r WHERE r.expires<=?
      AND NOT EXISTS(SELECT 1 FROM tk_links l WHERE l.path=r.path AND l.expires>?)
      AND NOT EXISTS(SELECT 1 FROM tk_deliveries d WHERE d.post_id=r.post_id AND d.state!='sent')
      AND NOT EXISTS(SELECT 1 FROM tk_parts t JOIN tk_deliveries d ON d.id=t.delivery_id
                     WHERE d.post_id=r.post_id AND t.state!='sent')'''
    now=time.time()
    with c.db() as db:rows=db.execute('SELECT r.path '+eligible+' LIMIT 16',(now,now)).fetchall()
    for row in rows:
        with c.db() as db:
            db.execute('BEGIN IMMEDIATE');now=time.time()
            current=db.execute('SELECT r.path,r.post_id '+eligible+' AND r.path=?',(now,now,row['path'])).fetchone()
            if not current:continue
            c.safe_file(current['path']).unlink(missing_ok=True)
            # An explicit later replay rebuilds missing media instead of using stale paths.
            db.execute('UPDATE tk_posts SET plan=NULL WHERE id=?',(current['post_id'],))
            db.execute('DELETE FROM tk_link_retention WHERE path=?',(current['path'],))
            db.execute('DELETE FROM tk_links WHERE path=? AND expires<=?',(current['path'],now))


def download_link(token):
    with c.db() as db:
        row=db.execute('SELECT l.*,d.user_id FROM tk_links l JOIN tk_deliveries d ON d.id=l.delivery_id WHERE l.digest=? AND l.expires>?',
                       (c.digest(token),time.time())).fetchone()
        if not row or not subscribed(row['user_id'],db):return None
    if not transport.allowed(row['user_id']):return None
    try:
        path=c.safe_file(row['path'])
        if not path.is_file():return None
        return dict(path=path,name=row['name'],size=path.stat().st_size)
    except (ValueError,OSError):return None


def enabled_for(delivery):
    return delivery['post_id'] not in EXCLUDED_POSTS and is_enabled() and subscribed(delivery['user_id'])


def send_part(delivery,part,send):
    from .transport import TelegramError
    from media_bot import video
    data={'chat_id':delivery['user_id']}
    if part['kind']=='text':
        return send('sendMessage',{**data,'text':part['html'],'parse_mode':'HTML','link_preview_options':{'is_disabled':True}})
    path=c.safe_file(part['path'])
    if part['size']>transport.file_limit():
        reason='檔案大於 500 MB，改提供下載連結。' if part['size']>limits.MAX_TELEGRAM_BYTES else '目前傳送介面限制，改提供下載連結。'
        return send('sendMessage',{**data,'text':part['name']+'\n'+reason+'\n檔案及連結保留 2 小時：'+link(delivery['id'],part)})
    kind=part['kind']
    if kind=='photo' and (part['size']>=9*1024**2 or path.suffix.lower() not in {'.jpg','.jpeg','.png'}):kind='document'
    if kind=='video':
        attributes=video.attributes(path)
        if attributes:data.update(attributes)
        else:kind='document'
    with path.open('rb') as stream:
        file=(part['name'],stream,mimetypes.guess_type(part['name'])[0] or 'application/octet-stream')
        try:return send('send'+kind.title(),data,{kind:file})
        except TelegramError as error:
            if error.code!=400 or kind=='document':raise
            # This part already owns a send checkpoint. Complete its fallback
            # while a feature pause holds subsequent parts at their claim boundary.
            if not subscribed(delivery['user_id']):raise
            stream.seek(0)
            return send('sendDocument',{'chat_id':delivery['user_id']},{'document':file})


def text_first_plan(plan):
    """Join safe text, split only at Telegram limits, then retain media order."""
    from .takaneko_content import split_html
    text='\n\n'.join(part['html'] for part in plan if part['kind']=='text')
    return ([{'kind':'text','html':html} for html in split_html(text)] +
            [part for part in plan if part['kind']!='text'])


def _locked(error):return c.is_busy(error)


def _retry_write(operation):return c.retry_write(operation)


def _save_receipt(delivery_id,position,message_id):
    """Never repeat a confirmed Telegram request when its receipt needs retry."""
    def save(db):
        db.execute("UPDATE tk_parts SET state='sent',telegram_id=? WHERE delivery_id=? AND position=?",
                   (str(message_id),delivery_id,position))
        db.execute("UPDATE tk_deliveries SET state='ready',error='' WHERE id=? AND state='uncertain'",(delivery_id,))
    _retry_write(save)


def _save_rejection(delivery_id,position,code,retry_at):
    def save(db):
        if code==429:
            previous=db.execute("SELECT value FROM settings WHERE key='takaneko_telegram_cooldown'").fetchone()
            until=max(json.loads(previous[0]) if previous else 0,retry_at)
            db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_telegram_cooldown',?)",(json.dumps(until),))
        db.execute("UPDATE tk_parts SET state='pending' WHERE delivery_id=? AND position=?",(delivery_id,position))
        db.execute("UPDATE tk_deliveries SET state=?,error=?,retry_at=? WHERE id=? AND state='uncertain'",
                   ('ready' if code==429 else 'paused','Telegram 拒絕傳送 '+str(code),retry_at,delivery_id))
    _retry_write(save)


def _flush_cooldown():
    global _telegram_cooldown
    if not _telegram_cooldown:return
    def save(db):
        previous=db.execute("SELECT value FROM settings WHERE key='takaneko_telegram_cooldown'").fetchone()
        until=max(json.loads(previous[0]) if previous else 0,_telegram_cooldown)
        db.execute("INSERT OR REPLACE INTO settings VALUES ('takaneko_telegram_cooldown',?)",(json.dumps(until),))
    _retry_write(save)
    _telegram_cooldown=0.0


def deliver(delivery,send=None):
    global _telegram_cooldown
    from .transport import telegram,TelegramError
    send=send or telegram
    if not enabled_for(delivery):return
    # Probe producer and Bot access before checkpointing a send attempt.
    if send is telegram:
        try:
            retry_delay = 0 if transport.allowed(delivery['user_id']) else 300
        except (OSError, ValueError, RuntimeError):
            retry_delay = 30
        if retry_delay:
            with c.db() as db:
                db.execute('''UPDATE tk_deliveries SET retry_at=?
                              WHERE id=? AND state IN ('pending','ready')''',
                           (time.time() + retry_delay, delivery['id']))
            return
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM tk_deliveries WHERE id=?',(delivery['id'],)).fetchone()
        if not row or row[0] not in ('pending','ready'):return
        post=db.execute('SELECT plan FROM tk_posts WHERE id=?',(delivery['post_id'],)).fetchone()
        if not post or not post['plan']:return
        # Freeze the final layout once per delivery. Existing partial/uncertain
        # deliveries keep their exact checkpoints across layout deployments.
        if not db.execute('SELECT 1 FROM tk_parts WHERE delivery_id=? LIMIT 1',(delivery['id'],)).fetchone():
            for i,part in enumerate(text_first_plan(json.loads(post['plan']))):
                db.execute('INSERT INTO tk_parts(delivery_id,position,payload) VALUES (?,?,?)',(delivery['id'],i,json.dumps(part)))
        parts=[dict(r) for r in db.execute('SELECT * FROM tk_parts WHERE delivery_id=? ORDER BY position',(delivery['id'],))]
        if any(json.loads(part['payload']).get('size',0)>limits.MAX_DOWNLOAD_BYTES for part in parts):
            db.execute("UPDATE tk_deliveries SET state='cancelled',error='單一檔案超過 2000 MB，已取消並保留現有資料' WHERE id=?",(delivery['id'],))
            return
    for part in parts:
        if part['state']=='sent':continue
        if part['state']=='uncertain':return
        if not enabled_for(delivery):return
        with c.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not is_enabled(db) or not subscribed(delivery['user_id'],db):return
            state=db.execute('SELECT state FROM tk_deliveries WHERE id=?',(delivery['id'],)).fetchone()[0]
            if state not in ('pending','ready'):return
            claimed = db.execute("""UPDATE tk_parts SET state='uncertain'
                                    WHERE delivery_id=? AND position=? AND state='pending'""",
                                 (delivery['id'], part['position'])).rowcount
            if not claimed:return
            attempt = secrets.token_hex(16)
            db.execute('INSERT OR REPLACE INTO tk_review_attempts VALUES (?,?,?,1)',
                       (delivery['id'], part['position'], attempt))
            db.execute("UPDATE tk_deliveries SET state='uncertain',error='傳送結果待確認' WHERE id=?",(delivery['id'],))
        checkpoint_saved = False
        try:
            with transport.send_guard(attempt):
                result=send_part(delivery,json.loads(part['payload']),send)
            if not isinstance(result,dict) or not result.get('message_id'):raise ValueError('Missing Telegram receipt')
            _save_receipt(delivery['id'],part['position'],result['message_id'])
            checkpoint_saved = True
        except TelegramError as error:
            # Gateway/server errors may have happened AFTER acceptance. Fail closed.
            if error.code>=500 or error.code<400:return
            retry_at=time.time()+max(5,error.retry)
            if error.code==429:_telegram_cooldown=max(_telegram_cooldown,retry_at)
            _save_rejection(delivery['id'],part['position'],error.code,retry_at)
            checkpoint_saved = True
            if error.code==429:_telegram_cooldown=0.0
            return
        except Exception:return  # Persisted uncertainty, never replay automatically.
        finally:
            # Normally both processes have released their descriptors already.
            # On socket timeout, the remote descriptor keeps this send fenced.
            transport.send_finished(attempt)
            if not checkpoint_saved:
                _retry_write(lambda db: db.execute(
                    'UPDATE tk_review_attempts SET active=0 WHERE delivery_id=? AND attempt=?',
                    (delivery['id'], attempt)))
    with c.db() as db:db.execute("UPDATE tk_deliveries SET state='sent',error='' WHERE id=? AND state='ready'",(delivery['id'],))


def retry_delivery(delivery_id):
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM tk_deliveries WHERE id=?',(delivery_id,)).fetchone()
        if not row or not subscribed(row['user_id'],db):raise ValueError('用戶未訂閱或無存取權限')
        require_allowed_post(row['post_id'])
        if row['state']=='uncertain' or db.execute("SELECT 1 FROM tk_parts WHERE delivery_id=? AND state='uncertain'",(delivery_id,)).fetchone():
            raise ValueError('請先確認 Telegram 中結果未明的訊息')
        if row['state']=='sent':raise ValueError('已送達，不會重送；測試請使用歷史文章測試功能')
        db.execute("UPDATE tk_deliveries SET state='pending',retry_at=0,error='' WHERE id=?",(delivery_id,))
        db.execute('UPDATE tk_posts SET retry_at=0,error=? WHERE id=?',('',row['post_id']))


REVIEW_PAGE_SIZE = 20


def _review_rows(db, after, through, limit):
    # Project only operator-visible metadata from the frozen layout.
    db.create_function('send_finished', 1, transport.send_finished)
    exclusions = ','.join('?' for _ in EXCLUDED_POSTS)
    exclude_posts = f'AND d.post_id NOT IN ({exclusions})' if exclusions else ''
    query = f"""SELECT d.id AS delivery_id, d.user_id, d.created, p.position, a.attempt,
                       (SELECT COUNT(*) FROM tk_parts WHERE delivery_id=d.id) AS total,
                       CASE WHEN json_valid(p.payload) THEN
                         CASE WHEN json_extract(p.payload,'$.kind') IN
                              ('text','photo','video','document','audio','animation')
                              THEN json_extract(p.payload,'$.kind') ELSE 'attachment' END
                         ELSE 'attachment' END AS kind
                FROM tk_review_attempts a
                JOIN tk_deliveries d ON d.id=a.delivery_id
                JOIN tk_parts p ON p.delivery_id=d.id AND p.position=a.position
                JOIN tk_subscriptions s ON s.user_id=d.user_id AND s.enabled=1
                WHERE d.id>? AND d.id<=? AND a.active=0
                  AND d.state='uncertain' AND p.state='uncertain' AND p.telegram_id IS NULL
                  AND CASE WHEN a.active=0 THEN send_finished(a.attempt) ELSE 0 END
                  AND NOT EXISTS (SELECT 1 FROM tk_parts other WHERE other.delivery_id=d.id
                                  AND other.position!=p.position AND other.state='uncertain')
                  {exclude_posts}
                ORDER BY d.id LIMIT ?"""
    return [dict(row) for row in db.execute(query, (after, through, *EXCLUDED_POSTS, limit))]


def review_deliveries(after=0):
    if type(after) is not int or not 0 <= after < 2**63:
        return {'ok': False, 'error': 'invalid'}
    with c.db() as db:
        rows = _review_rows(db, after, 2**63 - 1, REVIEW_PAGE_SIZE + 1)
    items = rows[:REVIEW_PAGE_SIZE]
    return {'ok': True, 'items': items, 'next_after': items[-1]['delivery_id'] if len(rows) > len(items) else None}


def resolve_delivery(delivery_id, position, attempt, decision):
    if (type(delivery_id) is not int or not 0 < delivery_id < 2**63
            or type(position) is not int or not 0 <= position < 2**63
            or not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{32}', attempt)
            or decision not in ('sent', 'retry')):
        return {'ok': False, 'error': 'invalid'}
    with c.db() as db:
        rows = _review_rows(db, delivery_id - 1, delivery_id, 1)
    if not rows or rows[0]['position'] != position or rows[0]['attempt'] != attempt:
        return {'ok': False, 'error': 'stale'}
    # Check Bot access over the socket before taking a writer lock. Recheck
    # the provider subscription and the exact attempt inside the transaction.
    try:
        if not transport.review_allowed(rows[0]['user_id']):
            return {'ok': False, 'error': 'stale'}
    except (OSError, RuntimeError, ValueError):
        return {'ok': False, 'error': 'unavailable'}
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if _review_rows(db, delivery_id - 1, delivery_id, 1) != rows:
            return {'ok': False, 'error': 'stale'}
        db.execute('UPDATE tk_parts SET state=? WHERE delivery_id=? AND position=?',
                   ('sent' if decision == 'sent' else 'pending', delivery_id, position))
        db.execute("UPDATE tk_deliveries SET state='ready',error='',retry_at=0 WHERE id=?", (delivery_id,))
    return {'ok': True, 'decision': decision}


def current_review(delivery_id):
    if type(delivery_id) is not int or not 0 < delivery_id < 2**63:
        raise ValueError('Invalid delivery ID.')
    with c.db() as db:
        rows = _review_rows(db, delivery_id - 1, delivery_id, 1)
    if not rows:
        raise ValueError('No reviewable delivery part.')
    return rows[0]


def process_once():
    if not is_enabled():return
    _flush_cooldown()
    with c.db() as db:
        # Also fence stale/restored rows before selection, retaining receipts.
        for post_id in EXCLUDED_POSTS:
            db.execute("UPDATE tk_deliveries SET state='cancelled',error='',retry_at=0 WHERE post_id=? AND state IN ('pending','ready','paused')",(post_id,))
        # Cancel revoked/unsubscribed work without deleting checkpoints or originals.
        invalid="""state IN ('pending','ready','paused') AND user_id NOT IN
          (SELECT user_id FROM tk_subscriptions WHERE enabled=1)"""
        if db.execute('SELECT 1 FROM tk_deliveries WHERE '+invalid+' LIMIT 1').fetchone():
            db.execute("UPDATE tk_deliveries SET state='cancelled' WHERE "+invalid)
        posts=[dict(r) for r in db.execute("""SELECT p.* FROM tk_posts p WHERE p.plan IS NULL AND p.retry_at<=?
          AND EXISTS (SELECT 1 FROM tk_deliveries d WHERE d.post_id=p.id AND d.state IN ('pending','ready'))
          ORDER BY p.discovered,p.id LIMIT 1""",(time.time(),))]
    for post in posts:
        try:prepare_post(post)
        except download_size.DownloadTooLarge:
            message='媒體下載超過 2000 MB，已取消並保留未完成資料'
            with c.db() as db:
                db.execute("UPDATE tk_posts SET state='cancelled',error=? WHERE id=?",(message,post['id']))
                db.execute("UPDATE tk_deliveries SET state='cancelled',error=? WHERE post_id=? AND state IN ('pending','ready','paused')",(message,post['id']))
            c.setting('takaneko_error',message)
        except Exception as error:
            from .takaneko_api import FanclubError, FanclubHTTPError
            from .takaneko_content import TranslationError
            if isinstance(error, FanclubHTTPError):
                message = error.status_message
            elif isinstance(error,FanclubError) and error.status==403:
                message='來源拒絕存取（403）；已跳過，保留待重試'
            elif isinstance(error,FanclubError) and error.status==401:
                message='來源登入失效（401）；已跳過，請更新登入憑證'
            elif isinstance(error,FanclubError):
                message='來源讀取失敗；已跳過，保留待重試'
            elif isinstance(error,TranslationError):
                message='OpenAI 翻譯失敗；已跳過，保留待重試'
            else:
                message='文章或媒體處理失敗；已跳過，保留待重試'
            with c.db() as db:db.execute("UPDATE tk_posts SET error=?,retry_at=? WHERE id=?",(message,time.time()+300,post['id']))
            c.setting('takaneko_error',message)
    if time.time()<max(_telegram_cooldown,c.get_setting('takaneko_telegram_cooldown',0)):return
    with c.db() as db:
        # Each article owns its checkpoints. An unavailable/paused/uncertain
        # older article must not hold later ready articles for the same user.
        rows=[dict(r) for r in db.execute("""SELECT d.* FROM tk_deliveries d JOIN tk_posts p ON p.id=d.post_id
          WHERE d.state IN ('pending','ready') AND p.plan IS NOT NULL AND d.retry_at<=?
          ORDER BY d.retry_at,d.id LIMIT 20""",(time.time(),))]
    for row in rows:
        if time.time()<max(_telegram_cooldown,c.get_setting('takaneko_telegram_cooldown',0)):break
        deliver(row)


def _report_error(key,message,error):
    # Diagnostics must remain available when the database itself is unavailable.
    # Never log exception text: upstream exceptions may contain private data.
    from .takaneko_api import FanclubHTTPError
    if isinstance(error, FanclubHTTPError):
        message = error.status_message
    _LOG.warning('Takaneko %s failed (%s); will retry',key,type(error).__name__)
    try:c.setting(key,message)
    except Exception:_LOG.warning('Takaneko error status unavailable; retry loop remains active')


@contextlib.contextmanager
def deployment_guard():
    path = os.getenv('TAKANEKO_DEPLOY_LOCK')
    if not path:
        yield True
        return
    with open(path, 'rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def poll_loop(stop=None):
    stop=stop or threading.Event()
    while not stop.is_set():
        try:
            with deployment_guard() as available:
                if available and is_enabled():
                    now = time.time()
                    if now >= c.get_setting('takaneko_next_poll',0):
                        c.setting('takaneko_next_poll',now+POLL_SECONDS)
                        poll_once()
        except Exception as error:
            _report_error('takaneko_poll_error','Takaneko 清單暫時不可用；將自動重試',error)
        stop.wait(2)


def process_cycle(last_health):
    cleanup_links()
    if not is_enabled():
        return last_health
    configured = transport.session_configured()
    if configured is False:
        return last_health
    if configured:
        process_once()
    if time.monotonic() - last_health >= 30 and is_enabled():
        transport.report_health(credentials_unavailable=configured is None)
        if configured:
            c.setting_many({'takaneko_process_error': '', 'takaneko_last_process': time.time()})
        return time.monotonic()
    return last_health


def _release_abandoned_sends():
    # Called only by the singleton sender, outside its delivery loop. This also
    # recovers if a previous attempt exhausted the database writer retries.
    with c.db() as db:
        if db.execute('SELECT 1 FROM tk_review_attempts WHERE active=1 LIMIT 1').fetchone():
            db.execute('UPDATE tk_review_attempts SET active=0 WHERE active=1')


def run(stop=None):
    # Hold the singleton lock until BOTH loops have stopped. A supervisor can
    # safely replace a failed feed without leaving a duplicate inventory thread.
    with (c.CONTROL/'takaneko.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        # Only the owner of the sender singleton can know a previous process
        # has exited. Its uncheckpointed sends now require manual review.
        _release_abandoned_sends()
        for path in c.CONTROL.glob('.sending-*'):
            transport.send_finished(path.name.removeprefix('.sending-'))
        stop=stop or threading.Event()
        poller=threading.Thread(target=poll_loop,args=(stop,),daemon=True)
        poller.start()
        last_health=0
        try:
            while not stop.is_set():
                try:
                    if not poller.is_alive():
                        _LOG.warning('Takaneko inventory thread stopped; restarting')
                        poller=threading.Thread(target=poll_loop,args=(stop,),daemon=True)
                        poller.start()
                    with deployment_guard() as available:
                        if not available:
                            stop.wait(2)
                            continue
                        _release_abandoned_sends()
                        last_health = process_cycle(last_health)
                except Exception as error:
                    _report_error('takaneko_process_error','通知處理暫時不可用；將自動重試，進度已保留',error)
                stop.wait(2)
        finally:
            stop.set()
            poller.join()
