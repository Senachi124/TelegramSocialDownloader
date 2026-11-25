"""Producer-side interfaces. Bot credentials remain inside the delivery service."""
from contextlib import contextmanager
import fcntl
import os
import pwd
import re
import threading

from media_bot.push_socket import exchange

from . import state


_sending = threading.local()


@contextmanager
def send_guard(attempt):
    path = state.CONTROL / ('.sending-' + attempt)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o600), 'rb') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        _sending.guard = guard
        try:
            yield
        finally:
            del _sending.guard


def send_finished(attempt):
    """A queued or running Bot transport keeps the lock even after a producer crash."""
    if not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{32}', attempt):
        return False
    path = state.CONTROL / ('.sending-' + attempt)
    try:
        with path.open('rb') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Tokens are never reused. An idle file cannot acquire a sender again.
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    except BlockingIOError:
        return False
    return True


class TelegramError(Exception):
    def __init__(self, code, retry=0):
        self.code, self.retry = code, retry


def _call(request, stream=None, timeout=10):
    return exchange(os.getenv('TAKANEKO_PUSH_SOCKET', '/run/telegram-social-push/transport.sock'),
                    pwd.getpwnam(os.getenv('TAKANEKO_BOT_USER', 'telegram-social')).pw_uid,
                    request, stream=stream, timeout=timeout,
                    send_guard=getattr(_sending, 'guard', None) if request['op'] == 'send' else None)


def file_limit():
    result = _call({'op': 'limits'})
    if result.get('ok') and type(result.get('file_limit')) is int:
        return result['file_limit']
    raise RuntimeError('Push transport unavailable')


def _check_access(operation, user_id):
    result = _call({'op': operation, 'user_id': user_id})
    if result.get('ok') is True:
        return True
    if result.get('code') == 403:
        return False
    raise RuntimeError('Push transport unavailable')


def allowed(user_id):
    return _check_access('allowed', user_id)


def review_allowed(user_id):
    return _check_access('review_allowed', user_id)


def telegram(method, data, files=None):
    if method == 'sendMessage':
        request = dict(op='send', kind='text', user_id=data['chat_id'], text=data['text'],
                       html=data.get('parse_mode') == 'HTML')
        result = _call(request, timeout=60)
    else:
        kind = method.removeprefix('send').lower()
        name, stream, _ = files[kind]
        request = dict(op='send', kind=kind, user_id=data['chat_id'], name=name)
        request.update({key: data[key] for key in ('height', 'width', 'duration') if key in data})
        result = _call(request, stream=stream, timeout=1860)
    if result.get('ok') and type(result.get('message_id')) is int:
        return result
    if isinstance(result.get('code'), int) and 400 <= result['code'] < 500:
        raise TelegramError(result['code'], result.get('retry_after', 0))
    raise RuntimeError('Uncertain push result')


def secret(operation, fields=None):
    result = exchange(os.getenv('TAKANEKO_SECRET_SOCKET', '/run/telegram-takaneko-broker/broker.sock'),
                      pwd.getpwnam(os.getenv('TAKANEKO_BROKER_USER', 'root')).pw_uid,
                      dict(op=operation, **(fields or {})), timeout=3 if operation == 'session_status' else 15)
    if result.get('ok') is not True:
        raise RuntimeError('Producer credentials unavailable')
    return result


def session_configured():
    try:
        configured = secret('session_status').get('session_configured')
    except (OSError, RuntimeError, ValueError):
        return None
    return configured if type(configured) is bool else None


def feature_control(enabled=None):
    from . import takaneko_feed as feed
    if enabled is False:
        feed.set_enabled(False)
    configured = session_configured()
    if enabled is True:
        if configured is None:
            raise RuntimeError('Fanclub session status unavailable')
        if configured is False:
            return {'ok': False, 'error': 'session_required'}
        feed.set_enabled(True)
    if enabled is None:
        enabled = feed.is_enabled()
    return {'ok': True, 'enabled': enabled, 'session_configured': configured}


def report_health(*, credentials_unavailable=False):
    from . import state as c
    if credentials_unavailable:
        return _call({'op': 'health', 'status': 'account'})
    # Send only stable reasons for open work, independent of article contents.
    with c.db() as db:
        db.create_function('send_finished', 1, send_finished)
        failed = db.execute("""SELECT p.error FROM tk_posts p WHERE p.error!=''
                              AND EXISTS(SELECT 1 FROM tk_deliveries d WHERE d.post_id=p.id
                              AND d.state IN ('pending','ready','paused')) ORDER BY p.retry_at DESC LIMIT 1""").fetchone()
        paused = db.execute("SELECT 1 FROM tk_deliveries WHERE state='paused' LIMIT 1").fetchone()
        uncertain = db.execute("""SELECT 1 FROM tk_deliveries d WHERE d.state='uncertain' AND NOT EXISTS
                                  (SELECT 1 FROM tk_review_attempts a WHERE a.delivery_id=d.id AND
                                   CASE WHEN a.active=1 THEN 1 ELSE NOT send_finished(a.attempt) END)
                                  LIMIT 1""").fetchone()
    if failed:
        reason = ('account' if '401' in failed[0] else 'access' if '403' in failed[0]
                  else 'network' if '讀取失敗' in failed[0] else 'format')
    elif c.get_setting('takaneko_poll_error'):
        reason = 'network'
    elif paused:
        reason = 'paused'
    elif uncertain:
        reason = 'uncertain'
    else:
        reason = 'ok'
    _call({'op':'health','status':reason})


def control(uid, request, fd):
    from . import state as c, takaneko_feed as feed
    if fd is not None:
        return {'ok': False}
    op = request.get('op')
    if op == 'health':
        with c.db() as db:
            db.execute('SELECT 1 FROM tk_subscriptions LIMIT 0')
        secret('health')
        return {'ok': True}
    if op == 'feature_status':
        return feature_control()
    if op == 'set_enabled':
        if type(request.get('enabled')) is not bool:
            return {'ok': False}
        return feature_control(request['enabled'])
    if op == 'set_session':
        secret('set_session', {'session': request.get('session')})
        return {'ok': True}
    if op == 'list':
        with c.db() as db:
            users = [row[0] for row in db.execute('SELECT user_id FROM tk_subscriptions WHERE enabled=1')]
        return {'ok': True, 'users': users, 'capabilities': ['set_session', 'set_enabled', 'review_deliveries']}
    if op == 'review_deliveries':
        return feed.review_deliveries(request.get('after', 0))
    if op == 'resolve_delivery':
        return feed.resolve_delivery(request.get('delivery_id'), request.get('position'),
                                     request.get('attempt'), request.get('decision'))
    user_id = request.get('user_id')
    if type(user_id) is not int or not 0 < user_id < 2**63:
        return {'ok': False}
    if op == 'set' and type(request.get('enabled')) is bool:
        feed.set_subscription(user_id, request['enabled'])
        return {'ok': True}
    if op == 'subscribed':
        return {'ok': True, 'enabled': feed.subscribed(user_id)}
    return {'ok': False}
