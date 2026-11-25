"""Bounded operational errors. Persist only fixed reason/source codes and numeric status."""
import json
import logging
import re
import time

from . import core

LIMIT = 100
SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS admin_errors(
        id INTEGER PRIMARY KEY, observed REAL, source TEXT NOT NULL, reason TEXT NOT NULL, status INTEGER);
    CREATE TRIGGER IF NOT EXISTS admin_error_limit AFTER INSERT ON admin_errors
    BEGIN
        DELETE FROM admin_errors WHERE id <=
          (SELECT id FROM admin_errors ORDER BY id DESC LIMIT 1 OFFSET {LIMIT});
    END;
"""
SOURCES = {
    'instagram': 'Instagram downloads', 'twitter': 'X downloads', 'tiktok': 'TikTok downloads',
    'threads': 'Threads downloads', 'youtube': 'YouTube downloads', 'batch': 'Bot downloads',
    'takaneko': 'Takaneko provider', 'fanclub': 'Takaneko Fanclub', 'takaneko_media': 'Takaneko media',
    'takaneko_telegram': 'Takaneko → Telegram', 'push_telegram': 'Push → Telegram',
    'push': 'Notification provider', 'download_telegram': 'Downloads → Telegram',
}
HEALTH = {
    'account': 'The provider reported a login or credential access failure.',
    'storage': 'The provider storage check failed.',
    'rate_limit': 'The source rate-limited provider requests.',
    'access': 'The provider reported that the source denied access.',
    'network': 'The provider could not reach its source.',
    'format': 'The provider could not process the source response.',
    'paused': 'The provider reported that delivery has paused.',
    'uncertain': 'The provider has a Telegram send without a saved delivery receipt.',
}
FANCLUB = {
    'broker_unavailable': 'The credential broker did not confirm whether a Fanclub login is configured.',
    'fanclub_unauthorized': 'The Fanclub source rejected authorization (401).',
    'fanclub_forbidden': 'The Fanclub source denied access (403).',
    'fanclub_read': 'The provider could not read the Fanclub source.',
    'fanclub_poll': 'The provider could not complete the Fanclub inventory poll.',
    'fanclub_format': 'The provider could not prepare the Fanclub content.',
    'translation': 'The provider could not translate the Fanclub content.',
}
SEND = {
    'send_timeout': 'The Telegram send timed out before the Bot obtained a delivery receipt.',
    'send_connection': 'The Telegram send failed because a connection could not be established or maintained.',
    'send_receipt': 'The Bot did not receive a valid Telegram message ID.',
    'send_response': 'The Bot received an unrecognized Telegram response without a valid delivery receipt.',
    'send_failure': 'The Bot could not complete the Telegram send. No more specific cause was recorded.',
    'send_token': 'The Bot could not send because no Telegram bot token was configured.',
    'send_rejected': 'Telegram rejected the send request.',
    'download_receipt': 'The downloader has a Telegram send without a saved delivery receipt.',
}


def provider_http_status(reason):
    if isinstance(reason, str):
        return re.fullmatch(r'(fanclub_poll|fanclub_read|media_read)_http_([45][0-9]{2})', reason)


def _health_source(slug, reason):
    if reason in ('uncertain', 'paused') and slug == 'takaneko':
        return 'takaneko_telegram'
    if reason == 'uncertain':
        return 'push_telegram'
    if slug == 'takaneko':
        return 'fanclub' if reason in FANCLUB and reason not in ('broker_unavailable', 'translation') else 'takaneko'
    return 'push'


def record_health(db, slug, reason, observed):
    status = provider_http_status(reason) if slug == 'takaneko' else None
    if status:
        source = 'takaneko_media' if status[1] == 'media_read' else 'fanclub'
        db.execute('INSERT INTO admin_errors(observed,source,reason,status) VALUES (?,?,?,?)',
                   (observed, source, status[1] + '_status', int(status[2])))
        return
    if reason not in HEALTH and not (slug == 'takaneko' and reason in FANCLUB):
        return
    db.execute('INSERT INTO admin_errors(observed,source,reason) VALUES (?,?,?)',
               (observed, _health_source(slug, reason), reason))


def import_health(db):
    # Installation can start several processes concurrently. The caller owns
    # an immediate transaction covering the migration marker and observations.
    if not db.execute("INSERT OR IGNORE INTO settings VALUES ('admin_errors_imported_health','true')").rowcount:
        return
    for key, value in db.execute("SELECT key,value FROM settings WHERE key GLOB 'push_health.*'"):
        try:
            reason = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(reason, str):
            record_health(db, key.removeprefix('push_health.'), reason, None)


def record_send(slug, reason, status=None):
    """Diagnostics cannot delay a send checkpoint through a normal writer timeout."""
    source = 'takaneko_telegram' if slug == 'takaneko' else 'push_telegram'
    if reason in ('send_http', 'send_api'):
        if type(status) is not int or not 400 <= status <= 599:
            reason, status = 'send_failure', None
    elif reason not in SEND:
        reason, status = 'send_failure', None
    else:
        status = None
    try:
        with core.db() as db:
            db.execute('PRAGMA busy_timeout=100')
            db.execute('INSERT INTO admin_errors(observed,source,reason,status) VALUES (?,?,?,?)',
                       (time.time(), source, reason, status))
    except Exception as error:
        logging.getLogger(__name__).warning('Send diagnostic unavailable (%s)', type(error).__name__)


def recent():
    from .admin_notifications import MESSAGES, PLATFORMS

    with core.db() as db:
        rows = db.execute('SELECT observed,source,reason,status FROM admin_errors ORDER BY id DESC LIMIT ?',
                          (LIMIT,)).fetchall()
    result = []
    for observed, source, reason, status in rows:
        if source not in SOURCES:
            continue
        if source in PLATFORMS:
            message = ('Telegram delivery could not be confirmed.' if reason == 'uncertain'
                       else MESSAGES.get(reason, MESSAGES['failed']))
        elif reason in ('send_http', 'send_api') and type(status) is int and 400 <= status <= 599:
            label = 'HTTP status' if reason == 'send_http' else 'Bot API error code'
            message = f'The Telegram send returned {label} {status}.'
        elif reason in ('fanclub_read_status', 'fanclub_poll_status') and type(status) is int and 400 <= status <= 599:
            operation = 'inventory' if reason == 'fanclub_poll_status' else 'content'
            message = f'The Fanclub {operation} request failed (HTTP {status}).'
        elif reason == 'media_read_status' and type(status) is int and 400 <= status <= 599:
            message = f'The media request failed (HTTP {status}).'
        else:
            message = SEND.get(reason) or HEALTH.get(reason) or FANCLUB.get(reason)
        if message:
            result.append({'observed': observed, 'source': SOURCES[source], 'message': message})
    return result
