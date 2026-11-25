"""Owner alerts with a durable, content-free send checkpoint."""
import hashlib
import json
import logging
import re
import shutil
import threading
import time

from . import core as c

INTERVAL = 5
RESERVE = 5 * 1024 ** 3
HEALTH_DELAY = 120
PLATFORMS = ('instagram', 'threads', 'tiktok', 'twitter', 'youtube', 'batch')
REASONS = {
    'account': ('cookie', 'login', '登入', '帳號', 'account'),
    'storage': ('storage', 'disk', 'ssd', '空間', '磁碟', '暫存'),
    'rate_limit': ('rate limit', '429', 'cooldown', '限流', '配額'),
    'size_limit': ('oversize', 'too large', '上限', '過大'),
    'unavailable': ('unavailable', 'not found', 'deleted', '找不到', '不存在', '無法觀看'),
    'access': ('forbidden', '403', '拒絕', '權限'),
    'network': ('timeout', 'network', '連線', '逾時'),
    'format': ('unsupported', 'format', '格式', '不支援'),
}
MESSAGES = {
    'account': 'The download account requires attention. Check its login and cookies.',
    'storage': 'The storage check failed or free space is low.',
    'rate_limit': 'The platform is limiting requests.',
    'size_limit': 'The media exceeds the configured size limit.',
    'unavailable': 'The source content is unavailable.',
    'access': 'The platform denied access.',
    'network': 'The platform connection failed or timed out.',
    'format': 'The downloader could not process this format.',
    'uncertain': 'Telegram delivery could not be confirmed. Check before retrying.',
    'failed': 'The download could not be completed.',
    'paused': 'Processing paused and requires attention.',
    'partial': 'Part of the request could not be completed.',
    'user': 'A new user registered with the bot.',
    'telegram': 'Telegram polling has been unhealthy for two minutes.',
    'storage_health': 'Available storage has been low for two minutes.',
    'recovered': 'The service has recovered.',
    'previous': 'An earlier operation requires attention.',
}


def safe_reason(message, state='failed', uncertain=False):
    if uncertain:
        return MESSAGES['uncertain']
    text = str(message or '').lower()
    code = next((code for code, words in REASONS.items() if any(w in text for w in words)), state)
    return MESSAGES.get(code, MESSAGES['failed'])


def initialize():
    # Build a CASE expression from fixed, trusted literals. Triggers also run
    # on connections opened outside Python. Never copy raw error text or URLs.
    conditions = []
    for code, words in REASONS.items():
        predicates = ' OR '.join(f"instr(lower(NEW.message),'{word}')>0" for word in words)
        conditions.append(f"WHEN {predicates} THEN '{code}'")
    reason = "CASE WHEN EXISTS(SELECT 1 FROM files WHERE job=NEW.id AND state='uncertain') THEN 'uncertain' " + ' '.join(conditions) + ' ELSE NEW.state END'
    platform = "CASE WHEN NEW.platform IN ('instagram','threads','tiktok','twitter','youtube') THEN NEW.platform ELSE 'batch' END"
    db_statements = f"""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS admin_alert_queue(
          id INTEGER PRIMARY KEY, reason TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS admin_alert_due ON admin_alert_queue(state,next_attempt,id);
        CREATE TABLE IF NOT EXISTS admin_alert_health(
          name TEXT PRIMARY KEY, bad INTEGER NOT NULL, since REAL NOT NULL, alerted INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS release_preview_outbox(
          id INTEGER PRIMARY KEY,event_key TEXT UNIQUE NOT NULL CHECK(event_key GLOB 'release-preview:*'),
          payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,
          next_attempt REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
          message_id TEXT,error TEXT NOT NULL DEFAULT '',finished REAL);
        DROP TRIGGER IF EXISTS admin_notice_new_job;
        DROP TRIGGER IF EXISTS admin_notice_job_state;
        DROP TRIGGER IF EXISTS admin_notice_new_user;
        DROP TRIGGER IF EXISTS admin_notice_new_tk;
        DROP TRIGGER IF EXISTS admin_notice_tk_state;
        CREATE TRIGGER IF NOT EXISTS admin_alert_job_transition AFTER UPDATE OF state ON jobs
          WHEN OLD.state IS NOT NEW.state AND NEW.state IN ('failed','partial','paused')
          AND COALESCE((SELECT value FROM settings WHERE key='failure_alerts'),'true')='true'
          BEGIN INSERT INTO admin_alert_queue(reason,platform) VALUES ({reason},{platform}); END;
        CREATE TRIGGER IF NOT EXISTS admin_alert_job_insert AFTER INSERT ON jobs
          WHEN NEW.state IN ('failed','partial','paused')
          AND COALESCE((SELECT value FROM settings WHERE key='failure_alerts'),'true')='true'
          BEGIN INSERT INTO admin_alert_queue(reason,platform) VALUES ({reason},{platform}); END;
        CREATE TRIGGER IF NOT EXISTS admin_alert_registration AFTER INSERT ON bot_users
          WHEN NEW.state != 'owner'
          BEGIN INSERT INTO admin_alert_queue(reason) VALUES ('user'); END;
    """
    with c.db() as db:
        db.executescript(db_statements)
        old = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'admin_notification_outbox' in old:
            db.execute("""INSERT OR IGNORE INTO release_preview_outbox(
              event_key,payload,state,created,next_attempt,attempts,message_id,error,finished)
              SELECT event_key,payload,state,created,next_attempt,attempts,message_id,error,finished
              FROM admin_notification_outbox WHERE event_key GLOB 'release-preview:*'""")
            db.execute("""INSERT INTO admin_alert_queue(reason,state,attempts,next_attempt)
                       SELECT 'previous',state,attempts,next_attempt FROM admin_notification_outbox
                       WHERE event_key NOT GLOB 'release-preview:*' AND state IN ('pending','uncertain')""")
            db.execute('DROP TABLE admin_notification_outbox')
        if 'admin_notification_dirty' in old:
            db.execute("""INSERT INTO admin_alert_queue(reason,platform)
                    SELECT j.state,CASE WHEN j.platform IN ('instagram','tiktok','twitter','threads','youtube')
                          THEN j.platform ELSE 'batch' END FROM admin_notification_dirty d
                    JOIN jobs j ON d.kind='job' AND d.item=j.id WHERE j.state IN ('failed','partial','paused')""")
            db.execute('DROP TABLE admin_notification_dirty')
        db.execute('DROP TABLE IF EXISTS admin_notification_subjects')


def _check_health(db, name, bad, now):
    row = db.execute('SELECT bad,since,alerted FROM admin_alert_health WHERE name=?',(name,)).fetchone()
    if row is None or row['bad'] != bad:
        if row and row['alerted'] and not bad:
            db.execute("INSERT INTO admin_alert_queue(reason) VALUES ('recovered')")
        db.execute('INSERT OR REPLACE INTO admin_alert_health VALUES (?,?,?,0)',(name,bad,now))
    elif bad and not row['alerted'] and now-row['since'] >= HEALTH_DELAY:
        db.execute('INSERT INTO admin_alert_queue(reason) VALUES (?)',(name,))
        db.execute('UPDATE admin_alert_health SET alerted=1 WHERE name=?',(name,))


def _send_preview(send, now):
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("""SELECT * FROM release_preview_outbox WHERE state='pending'
                           AND next_attempt<=? ORDER BY id LIMIT 1""",(now,)).fetchone()
        if not row:
            return False
        try:
            key = row['event_key']
            _, version, digest = key.split(':')
            if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):
                raise ValueError
            approval = db.execute('SELECT value FROM settings WHERE key=?',
                                  (f'release_announcement:{version}:{digest}',)).fetchone()
            record = json.loads(approval[0])
            recipient = db.execute("SELECT id FROM bot_users WHERE state='owner'").fetchone()[0]
            text = json.loads(row['payload'])['text']
            if (record['owner_id'] != recipient or record['text'] != text or record['preview_event'] != key or
                record['sha256'] != digest or hashlib.sha256(text.encode()).hexdigest() != digest):
                raise ValueError
        except (ValueError,KeyError,IndexError,AttributeError,TypeError):
            db.execute("""UPDATE release_preview_outbox SET state='blocked',error='invalid_preview',
                       finished=? WHERE id=?""",(now,row['id']))
            return True
        db.execute("""UPDATE release_preview_outbox SET attempts=attempts+1,state='uncertain',
                   error='awaiting_receipt' WHERE id=?""",(row['id'],))
    try:
        response = send('sendMessage',{'chat_id':recipient,'text':text,'disable_web_page_preview':True})
        receipt = response.get('message_id') if isinstance(response,dict) else None
        if type(receipt) is not int or receipt<=0:
            raise ValueError
        state, receipt, error, retry = 'sent', str(receipt), '', 0
    except Exception as exc:
        code = getattr(exc,'code',None)
        receipt = None
        if code in (401,409,429) and row['attempts'] < 7:
            delay = max(float(getattr(exc,'retry',0) or 0), min(3600,30 * 2**row['attempts']))
            state, error, retry = 'pending', f'telegram_rejected_{code}', now+delay
        elif type(code) is int and 400<=code<500:
            state, error, retry = 'blocked', f'telegram_rejected_{code}', 0
        else:
            state, error, retry = 'uncertain', 'delivery_result_unknown', 0
    c.retry_write(lambda db: db.execute("""UPDATE release_preview_outbox SET state=?,error=?,
                 message_id=?,next_attempt=?,finished=? WHERE id=?""",
                 (state,error,receipt,retry,None if state=='pending' else now,row['id'])))
    return True


def tick(send, now=None):
    now = time.time() if now is None else now
    if _send_preview(send, now):
        return
    enabled = c.get_setting('enabled', False)
    last_poll = c.get_setting('telegram_last_poll', None)
    try:
        low = shutil.disk_usage(c.DATA).free < RESERVE
    except OSError:
        low = True
    with c.db() as db:
        db.execute('BEGIN IMMEDIATE')
        owner = db.execute("SELECT id FROM bot_users WHERE state='owner'").fetchone()
        if not owner:
            return
        if c.get_setting('failure_alerts', True):
            _check_health(db, 'storage_health', low, now)
            _check_health(db, 'telegram', bool(enabled and isinstance(last_poll,(int,float)) and now-last_poll > 120), now)
        alert = db.execute("""SELECT * FROM admin_alert_queue WHERE state='pending'
                            AND next_attempt<=? ORDER BY id LIMIT 1""",(now,)).fetchone()
        if not alert:
            return
        db.execute("UPDATE admin_alert_queue SET state='uncertain',attempts=attempts+1 WHERE id=?",(alert['id'],))
    message = MESSAGES.get(alert['reason'], MESSAGES['previous'])
    if alert['platform'] == 'push':
        from .push import HEALTH
        message = 'Push · ' + HEALTH.get(alert['reason'], MESSAGES['previous'])
    elif alert['platform'] in PLATFORMS:
        message = f"{alert['platform'].capitalize()} · {message}"
    try:
        result = send('sendMessage', {'chat_id': owner['id'], 'text': message})
        if not isinstance(result, dict) or type(result.get('message_id')) is not int or result['message_id'] <= 0:
            return
    except Exception as exc:
        code = getattr(exc, 'code', None)
        if code in (401,409,429):
            delay = max(float(getattr(exc,'retry',0) or 0), min(3600, 30 * 2**min(alert['attempts'],7)))
            c.retry_write(lambda db: db.execute("UPDATE admin_alert_queue SET state='pending',next_attempt=? WHERE id=?",
                                               (now+delay,alert['id'])))
        elif type(code) is int and 400 <= code < 500:
            c.retry_write(lambda db: db.execute('DELETE FROM admin_alert_queue WHERE id=?',(alert['id'],)))
        return
    c.retry_write(lambda db: db.execute('DELETE FROM admin_alert_queue WHERE id=?',(alert['id'],)))


def run(send, stop=None):
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            tick(send)
        except Exception as error:
            logging.getLogger(__name__).warning('Owner alert delayed (%s)',type(error).__name__)
        stop.wait(INTERVAL)
