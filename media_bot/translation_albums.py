"""Remember just enough Telegram metadata to resolve a reply to one album."""
import json
from pathlib import PurePath
import sqlite3
import threading
import time

from . import command_reply, core as c
from .translation_cache import MAX_IMAGE_BYTES
from .video import VIDEO_DEMUXERS


TTL = 24 * 60 * 60
QUIET_SECONDS = 1.5
MAX_WAIT_SECONDS = 4.5
MAX_MEMBERS = 10
_UNAVAILABLE = '無法確認這個相簿的完整內容。請把要處理的項目單獨轉發為一個相簿，再回覆該相簿。'
_IMAGE_SUFFIXES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                   '.webp': 'image/webp', '.gif': 'image/gif', '.heic': 'image/heic',
                   '.heif': 'image/heif', '.avif': 'image/avif', '.tif': 'image/tiff',
                   '.tiff': 'image/tiff', '.bmp': 'image/bmp', '.svg': 'image/svg+xml',
                   '.jxl': 'image/jxl', '.ico': 'image/x-icon'}


class AlbumUnavailable(ValueError):
    """No reliable membership is available for the selected album."""


def initialize(db):
    # The caller owns the transaction. A head and all its members expire together.
    db.execute('''CREATE TABLE IF NOT EXISTS translation_albums (
        id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, topic INTEGER NOT NULL,
        actor INTEGER NOT NULL, album TEXT NOT NULL, outgoing INTEGER NOT NULL,
        created REAL NOT NULL, last_member REAL NOT NULL, expected INTEGER NOT NULL,
        incomplete INTEGER NOT NULL DEFAULT 0, UNIQUE(chat,topic,actor,album,outgoing))''')
    db.execute('''CREATE TABLE IF NOT EXISTS translation_album_members (
        album_id INTEGER NOT NULL REFERENCES translation_albums(id) ON DELETE CASCADE,
        message INTEGER NOT NULL, kind TEXT NOT NULL, mime TEXT NOT NULL,
        file_id TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(album_id,message))''')
    db.execute('CREATE INDEX IF NOT EXISTS translation_albums_created ON translation_albums(created)')


# Reply resolution needs only a bounded, process-local index.
_lock = threading.RLock()
_cache = sqlite3.connect(':memory:', check_same_thread=False, isolation_level=None)
_cache.row_factory = sqlite3.Row
_cache.execute('PRAGMA foreign_keys=ON')
initialize(_cache)
_cache.execute('CREATE TABLE receipts(chat,topic,actor,message,expires,PRIMARY KEY(chat,topic,actor,message))')


def _bounded_cache(callback):
    def synchronized(*args, **kwargs):
        with _lock:
            return callback(*args, **kwargs)
    return synchronized


def _positive(value):
    return type(value) is int and 0 < value < 2**63


def _group_id(message):
    value = message.get('media_group_id')
    if isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable():
        return value
    return None


def _topic(message):
    if not message.get('is_topic_message'):
        return 0
    thread = message.get('message_thread_id')
    return thread if _positive(thread) else None


def _human_scope(db, message):
    sender, chat = message.get('from'), message.get('chat')
    if (not isinstance(sender, dict) or not isinstance(chat, dict) or sender.get('is_bot')
            or message.get('sender_chat') or message.get('via_bot') or message.get('is_automatic_forward')):
        return None
    actor, chat_id, topic = sender.get('id'), chat.get('id'), _topic(message)
    if not _positive(actor) or type(chat_id) is not int or not -2**63 < chat_id < 2**63 or topic is None:
        return None
    if chat.get('type') == 'private':
        if chat_id != actor:
            return None
    elif chat.get('type') not in ('group', 'supergroup') or chat_id >= 0:
        return None
    if not _eligible(db, actor, chat_id):
        return None
    return chat_id, topic, actor


def _eligible(db, actor, chat):
    row = db.execute('SELECT state FROM bot_users WHERE id=?', (actor,)).fetchone()
    if not row or row[0] not in ('owner', 'approved'):
        return False
    if chat == actor or (chat < 0 and row[0] == 'owner'):
        return True
    setting = db.execute("SELECT value FROM settings WHERE key='translation_groups'").fetchone()
    groups = json.loads(setting[0]) if setting else []
    return chat < 0 and isinstance(groups, list) and chat in groups


def _descriptor(item):
    file_id = item.get('file_id')
    size = item.get('file_size')
    if not isinstance(file_id, str) or not 0 < len(file_id) <= 1024:
        return {'file_id': '', 'file_size': 0}
    return {'file_id': file_id, 'file_size': size if _positive(size) else 0}


def image_spec(message):
    """Return a full photo or image document, excluding all video and thumbnail fields."""
    if any(key in message for key in ('video', 'video_note', 'animation', 'audio', 'voice', 'sticker')):
        return None
    photos = message.get('photo')
    if isinstance(photos, list) and photos:
        sizes = [p for p in photos if isinstance(p, dict) and _positive(p.get('width'))
                 and _positive(p.get('height'))]
        usable = [p for p in sizes if _positive(p.get('file_size')) and p['file_size'] <= MAX_IMAGE_BYTES
                  and _descriptor(p)['file_id']]
        best = max(usable or sizes, key=lambda p: p['width'] * p['height'], default={})
        return _descriptor(best), 'photos', 'image/jpeg'
    document = message.get('document')
    if not isinstance(document, dict):
        return None
    filename = document.get('file_name')
    suffix = PurePath(filename).suffix.lower() if isinstance(filename, str) else ''
    if suffix in VIDEO_DEMUXERS:
        return None
    mime = document.get('mime_type')
    mime = mime.partition(';')[0].strip().lower() if isinstance(mime, str) else ''
    if mime in ('image/jpg', 'image/pjpeg'):
        mime = 'image/jpeg'
    if mime in ('', 'application/octet-stream'):
        mime = _IMAGE_SUFFIXES.get(suffix, '')
    if not mime.startswith('image/') or not 6 < len(mime) <= 100 or not mime.isascii() or not mime.isprintable():
        return None
    return _descriptor(document), 'documents', mime


def _incoming_scope(db, message):
    group = _group_id(message)
    if not group or not _positive(message.get('message_id')):
        return None
    scope = _human_scope(db, message)
    return (*scope, group, 0) if scope else None


def _head(db, scope, now, expected):
    db.execute('''INSERT INTO translation_albums(chat,topic,actor,album,outgoing,created,last_member,expected)
                  VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(chat,topic,actor,album,outgoing) DO NOTHING''',
               (*scope, now, now, expected))
    return db.execute('''SELECT id FROM translation_albums
                         WHERE chat=? AND topic=? AND actor=? AND album=? AND outgoing=?''', scope).fetchone()[0]


def _member(db, head, message):
    spec = image_spec(message)
    item, kind, mime = spec if spec else ({'file_id': '', 'file_size': 0}, '', '')
    return db.execute('''INSERT INTO translation_album_members(album_id,message,kind,mime,file_id,size)
                         VALUES (?,?,?,?,?,?) ON CONFLICT(album_id,message) DO NOTHING''',
                      (head, message['message_id'], kind, mime, item['file_id'], item['file_size'])).rowcount


@_bounded_cache
def observe(db, message, now=None):
    """Observe authorized human album updates, including material the human forwards."""
    scope = _incoming_scope(db, message)
    if not scope:
        return
    now = time.time() if now is None else now
    db = _cache
    _bound_size()
    head = _head(db, scope, now, 0)
    present, count = db.execute('''SELECT count(CASE WHEN message=? THEN 1 END),count(*)
                                   FROM translation_album_members WHERE album_id=?''',
                                (message['message_id'], head)).fetchone()
    if present:
        return
    if count >= MAX_MEMBERS:
        db.execute('UPDATE translation_albums SET incomplete=1 WHERE id=?', (head,))
        return
    _member(db, head, message)
    db.execute('UPDATE translation_albums SET last_member=? WHERE id=?', (now, head))


@_bounded_cache
def invalidate(db, message, now=None):
    """An offset may advance after optional metadata failure. Disable just its group."""
    scope = _incoming_scope(db, message)
    if scope:
        now = time.time() if now is None else now
        db = _cache
        _bound_size()
        head = _head(db, scope, now, 0)
        db.execute('UPDATE translation_albums SET incomplete=1 WHERE id=?', (head,))


@_bounded_cache
def record_delivery(db, pairs, now=None):
    """Index one complete, acknowledged sendMediaGroup. Individual files prove routing only."""
    pairs = list(pairs)
    if not 1 <= len(pairs) <= MAX_MEMBERS:
        return
    group = _group_id(pairs[0][1])
    if not group and len(pairs) > 1:
        return
    seen = set()
    scope = None
    for file_id, message in pairs:
        message_id = message.get('message_id')
        if _group_id(message) != group or not _positive(message_id) or message_id in seen:
            return
        seen.add(message_id)
        row = db.execute('''SELECT j.chat,coalesce(j.group_chat_id,j.chat),coalesce(j.message_thread_id,0)
                            FROM files f JOIN jobs j ON j.id=f.job
                            WHERE f.id=? AND f.state='sent' AND f.telegram_id=?''',
                         (file_id, str(message_id))).fetchone()
        if not row:
            return
        actor, chat, topic = row
        address = message.get('chat')
        if address is not None and (not isinstance(address, dict) or address.get('id') != chat):
            return
        if message.get('is_topic_message') and _topic(message) != topic:
            return
        current = (chat, topic, actor, group, 1)
        if scope is None:
            if not _eligible(db, actor, chat):
                return
            scope = current
        elif current != scope:
            return
    now = time.time() if now is None else now
    db = _cache
    _bound_size()
    db.executemany('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?)',
                   [(*scope[:3], message_id, now + TTL) for message_id in seen])
    if not group:
        return
    head = _head(db, scope, now, len(pairs))
    for _, message in pairs:
        _member(db, head, message)


@_bounded_cache
def cleanup(db, now=None):
    db = _cache
    now = time.time() if now is None else now
    db.execute('DELETE FROM receipts WHERE expires<=?', (now,))
    db.execute('''DELETE FROM translation_albums WHERE id IN
                  (SELECT id FROM translation_albums WHERE created<=? ORDER BY created LIMIT 256)''', (now-TTL,))


@_bounded_cache
def _has_bot_receipt(db, scope, message_id):
    return _cache.execute('SELECT 1 FROM receipts WHERE chat=? AND topic=? AND actor=? AND message=? AND expires>?',
                          (*scope, message_id, time.time())).fetchone() is not None


def _bound_size():
    _cache.execute('DELETE FROM translation_albums WHERE id IN (SELECT id FROM translation_albums ORDER BY id DESC LIMIT -1 OFFSET 2047)')
    _cache.execute('DELETE FROM receipts WHERE rowid IN (SELECT rowid FROM receipts ORDER BY rowid DESC LIMIT -1 OFFSET 8192)')


@_bounded_cache
def _read(db, scope, message_id, now):
    db = _cache
    head = db.execute('''SELECT a.id,a.last_member,a.expected,a.incomplete
                         FROM translation_albums a JOIN translation_album_members m ON m.album_id=a.id
                         WHERE a.chat=? AND a.topic=? AND a.actor=? AND a.album=? AND a.outgoing=?
                         AND m.message=? AND a.created>?''', (*scope, message_id, now-TTL)).fetchone()
    if not head or head['incomplete']:
        raise AlbumUnavailable(_UNAVAILABLE)
    rows = db.execute('''SELECT kind,mime,file_id,size FROM translation_album_members
                         WHERE album_id=? ORDER BY message''', (head['id'],)).fetchall()
    if not head['expected']:
        delay = head['last_member'] + QUIET_SECONDS - now
        if delay > 0:
            return None, delay
    elif len(rows) != head['expected']:
        raise AlbumUnavailable(_UNAVAILABLE)
    if not 2 <= len(rows) <= MAX_MEMBERS:
        raise AlbumUnavailable(_UNAVAILABLE)
    return [({'file_id': r['file_id'], 'file_size': r['size']}, r['kind'], r['mime'])
            for r in rows if r['kind']], 0


def resolve(message, bot_username):
    """Resolve only a plain, same-chat reply. An unknown album never degrades to its anchor."""
    from .translation import command
    match = command(message)
    parent = command_reply.local_parent(message)
    if (not parent or not match or (message.get('text') or message.get('caption') or '')[match.end():].strip()
            or message.get('forward_origin') or 'quote' in message or message.get('photo') or message.get('document')):
        return None
    sender = parent.get('from')
    message_id = parent.get('message_id')
    if not isinstance(sender, dict) or not _positive(message_id) or parent.get('sender_chat') or parent.get('via_bot'):
        return None
    outgoing = sender.get('is_bot') is True
    if outgoing:
        if (parent.get('forward_origin') or not isinstance(sender.get('username'), str)
                or not sender['username'] or ('media_group_id' not in parent
                    and not any(key in parent for key in ('photo', 'document', 'video', 'animation', 'video_note')))):
            return None
        name = bot_username() if callable(bot_username) else bot_username
        if not isinstance(name, str) or name.lower() != sender['username'].lower():
            return None
    elif sender.get('id') != message.get('from', {}).get('id'):
        return None
    group = _group_id(parent)
    if not outgoing and 'media_group_id' not in parent:
        return None  # The normal self-reply path includes its caption.
    until = time.monotonic() + MAX_WAIT_SECONDS
    while True:
        with c.db() as db:
            db.execute('BEGIN')
            scope = _human_scope(db, message)
            if not scope or (parent.get('is_topic_message') and _topic(parent) != scope[1]):
                return None
            if 'media_group_id' not in parent:
                if not _has_bot_receipt(db, scope, message_id):
                    return None
                spec = image_spec(parent)
                return [spec] if spec else []
            if not group:
                raise AlbumUnavailable(_UNAVAILABLE)
            result, delay = _read(db, (*scope, group, int(outgoing)), message_id, time.time())
        if delay <= 0:
            return result
        if delay > until - time.monotonic():
            raise AlbumUnavailable('相簿仍在接收中，請稍後再回覆該相簿。')
        time.sleep(delay)
