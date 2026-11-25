"""Owner alerts with a durable, content-free send checkpoint."""
import logging
import shutil
import threading
import time

from . import core as c, error_log

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


def _migrate_pending_push_alerts(db):
    if not db.execute("SELECT 1 FROM admin_alert_queue WHERE state='pending' AND platform='push' LIMIT 1").fetchone():
        return
    # Previous releases stored a provider health key in the same transaction
    # as the unscoped alert. Keep attempted notification checkpoints intact.
    has_takaneko = db.execute("SELECT 1 FROM settings WHERE key='push_health.takaneko'").fetchone()
    if has_takaneko:
        other = db.execute("""SELECT 1 FROM settings WHERE key GLOB 'push_health.*'
                              AND key!='push_health.takaneko' LIMIT 1""").fetchone()
        if other:
            db.execute("DELETE FROM admin_alert_queue WHERE state='pending' AND platform='push' AND reason='uncertain'")
        else:
            db.execute("DELETE FROM admin_alert_queue WHERE state='pending' AND platform='push'")
    db.execute("UPDATE admin_alert_queue SET platform='push_provider' WHERE state='pending' AND platform='push'")


def initialize():
    # Build a CASE expression from fixed, trusted literals. Triggers also run
    # on connections opened outside Python. Never copy raw error text or URLs.
    conditions = []
    for code, words in REASONS.items():
        predicates = ' OR '.join(f"instr(lower(NEW.message),'{word}')>0" for word in words)
        conditions.append(f"WHEN {predicates} THEN '{code}'")
    reason = "CASE WHEN EXISTS(SELECT 1 FROM files WHERE job=NEW.id AND state='uncertain') THEN 'uncertain' " + ' '.join(conditions) + ' ELSE NEW.state END'
    platform = "CASE WHEN NEW.platform IN ('instagram','threads','tiktok','twitter','youtube') THEN NEW.platform ELSE 'batch' END"
    # These fixed conditions must precede the general "access denied" match.
    uncertain = "EXISTS(SELECT 1 FROM files WHERE job=NEW.id AND state='uncertain')"
    rejected = "NEW.message='Telegram 拒絕傳送，檔案保留待重試'"
    diagnostic_source = f"CASE WHEN {uncertain} OR {rejected} THEN 'download_telegram' ELSE {platform} END"
    diagnostic_reason = (f"CASE WHEN {uncertain} THEN 'download_receipt' WHEN {rejected} THEN 'send_rejected' "
                         + ' '.join(conditions) + ' ELSE NEW.state END')
    diagnostic = ("INSERT INTO admin_errors(observed,source,reason) "
                  f"VALUES (strftime('%s','now'),{diagnostic_source},{diagnostic_reason});")
    db_statements = f"""
        BEGIN IMMEDIATE;
        {error_log.SCHEMA}
        CREATE TABLE IF NOT EXISTS admin_alert_queue(
          id INTEGER PRIMARY KEY, reason TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS admin_alert_due ON admin_alert_queue(state,next_attempt,id);
        CREATE TABLE IF NOT EXISTS admin_alert_health(
          name TEXT PRIMARY KEY, bad INTEGER NOT NULL, since REAL NOT NULL, alerted INTEGER NOT NULL);
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
        CREATE TRIGGER IF NOT EXISTS admin_error_job_transition AFTER UPDATE OF state ON jobs
          WHEN OLD.state IS NOT NEW.state AND NEW.state IN ('failed','partial','paused')
          BEGIN {diagnostic} END;
        CREATE TRIGGER IF NOT EXISTS admin_error_job_insert AFTER INSERT ON jobs
          WHEN NEW.state IN ('failed','partial','paused')
          BEGIN {diagnostic} END;
    """
    with c.db() as db:
        db.executescript(db_statements)
        error_log.import_health(db)
        _migrate_pending_push_alerts(db)
        old = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Retain previous delivery checkpoints separately from live queues and authorizations.
        if 'release_preview_outbox' in old or 'admin_notification_outbox' in old:
            db.execute("""CREATE TABLE IF NOT EXISTS retired_release_receipts(
                       event_key TEXT PRIMARY KEY, state TEXT, message_id TEXT)""")
            for table in ('release_preview_outbox', 'admin_notification_outbox'):
                if table in old:
                    db.execute(f"""INSERT OR IGNORE INTO retired_release_receipts
                               SELECT event_key,state,message_id FROM {table}
                               WHERE event_key GLOB 'release-preview:*' AND state!='pending'""")
            db.execute('DROP TABLE IF EXISTS release_preview_outbox')
        if db.execute("SELECT 1 FROM settings WHERE key GLOB 'release_announcement:*' LIMIT 1").fetchone():
            db.execute('CREATE TABLE IF NOT EXISTS retired_release_announcements(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("""INSERT OR IGNORE INTO retired_release_announcements
                       SELECT key,value FROM settings WHERE key GLOB 'release_announcement:*'""")
            db.execute("DELETE FROM settings WHERE key GLOB 'release_announcement:*'")
        if 'admin_notification_outbox' in old:
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


def tick(send, now=None):
    now = time.time() if now is None else now
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
        _migrate_pending_push_alerts(db)
        alert = db.execute("""SELECT * FROM admin_alert_queue WHERE state='pending'
                            AND next_attempt<=? ORDER BY id LIMIT 1""",(now,)).fetchone()
        if not alert:
            return
        db.execute("UPDATE admin_alert_queue SET state='uncertain',attempts=attempts+1 WHERE id=?",(alert['id'],))
    message = MESSAGES.get(alert['reason'], MESSAGES['previous'])
    if alert['platform'] in ('push', 'push_provider'):
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
