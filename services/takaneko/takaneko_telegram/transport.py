"""Producer-side interfaces. Bot credentials remain inside the delivery service."""
import os
import pwd

from media_bot.push_socket import exchange


class TelegramError(Exception):
    def __init__(self, code, retry=0):
        self.code, self.retry = code, retry


def _call(request, stream=None, timeout=10):
    return exchange(os.getenv('TAKANEKO_PUSH_SOCKET', '/run/telegram-social-push/transport.sock'),
                    pwd.getpwnam(os.getenv('TAKANEKO_BOT_USER', 'telegram-social')).pw_uid,
                    request, stream=stream, timeout=timeout)


def file_limit():
    result = _call({'op': 'limits'})
    if result.get('ok') and type(result.get('file_limit')) is int:
        return result['file_limit']
    raise RuntimeError('Push transport unavailable')


def allowed(user_id):
    return _call({'op': 'allowed', 'user_id': user_id}).get('ok') is True


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
                      dict(op=operation, **(fields or {})))
    if result.get('ok') is not True:
        raise RuntimeError('Producer credentials unavailable')
    return result


def report_health():
    from . import state as c
    # Send only stable reasons for open work, independent of article contents.
    with c.db() as db:
        failed = db.execute("""SELECT p.error FROM tk_posts p WHERE p.error!=''
                              AND EXISTS(SELECT 1 FROM tk_deliveries d WHERE d.post_id=p.id
                              AND d.state IN ('pending','ready','paused')) ORDER BY p.retry_at DESC LIMIT 1""").fetchone()
        paused = db.execute("SELECT 1 FROM tk_deliveries WHERE state='paused' LIMIT 1").fetchone()
        uncertain = db.execute("SELECT 1 FROM tk_deliveries WHERE state='uncertain' LIMIT 1").fetchone()
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
    if op == 'set_session':
        secret('set_session', {'session': request.get('session')})
        return {'ok': True}
    if op == 'list':
        with c.db() as db:
            users = [row[0] for row in db.execute('SELECT user_id FROM tk_subscriptions WHERE enabled=1')]
        return {'ok': True, 'users': users, 'capabilities': ['set_session']}
    user_id = request.get('user_id')
    if type(user_id) is not int or not 0 < user_id < 2**63:
        return {'ok': False}
    if op == 'set' and type(request.get('enabled')) is bool:
        feed.set_subscription(user_id, request['enabled'])
        return {'ok': True}
    if op == 'subscribed':
        return {'ok': True, 'enabled': feed.subscribed(user_id)}
    return {'ok': False}
