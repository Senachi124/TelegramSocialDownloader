"""Check enabled Bot-owned IG sessions using the normal credential broker."""
from collections import Counter
import json
import re
import time

from requests.cookies import RequestsCookieJar

from . import accounts, core
from .instagram_policy import RequestWindow


def cookie_jar(values):
    jar = RequestsCookieJar()
    for name, value in values.items():
        jar.set(name, value, domain='.instagram.com', path='/', secure=True)
    return jar


IDENTITY_COOKIES = {'sessionid', 'ds_user_id', 'csrftoken', 'mid', 'ig_did', 'rur', 'datr'}


def flatten(jar, original):
    """Keep only cookies that the Bot's domain-wide flat store can represent."""
    result = {}
    narrow = set()
    for cookie in jar:
        if cookie.domain != '.instagram.com' or cookie.path != '/' or not cookie.domain_specified:
            narrow.add(cookie.name)
            continue
        if cookie.name in result or cookie.is_expired():
            raise ValueError('Ambiguous IG cookie')
        result[cookie.name] = cookie.value
    if narrow.intersection(set(original) | set(result) | IDENTITY_COOKIES):
        raise ValueError('Conflicting IG cookie scope')
    return result


def enabled_account(data, key):
    return next((account for account in accounts.pool(data, 'instagram')['items']
                 if account['id'] == key and account['enabled'] and account['cookies']), None)


def snapshot(data, key):
    account = enabled_account(data, key)
    return ({k: account[k] for k in ('id', 'revision', 'cookies')}
            if account else {})


def apply_result(data, request):
    """Called inside the serialized broker after network I/O has finished."""
    from .ig_cookie_refresh import expected_identity
    account = enabled_account(data, request['id'])
    if not account or account['revision'] != request['revision']:
        return 'superseded'
    cookies = request['cookies']
    if (not isinstance(cookies, dict) or not cookies.get('sessionid') or not cookies.get('csrftoken') or
            not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]+', name)
                    and isinstance(value, str) and len(value) <= 16384
                    and not re.search(r'[\x00-\x1f\x7f;]', value) for name, value in cookies.items())):
        raise ValueError('Invalid IG cookies')
    identity = expected_identity(cookie_jar(account['cookies']))
    if not identity or expected_identity(cookie_jar(cookies)) != identity:
        raise ValueError('IG account mismatch')
    if cookies == account['cookies']:
        return 'unchanged'
    account.update(cookies=cookies, revision=account['revision'] + 1, updated=time.time(),
                   last_result='success', last_result_at=time.time())
    accounts.sync_legacy(data)
    return 'updated'


def run():
    from .ig_cookie_refresh import check, expected_identity
    def rpc(path, payload=None):
        return core.rpc(core.SECRET_SOCKET, path, payload)
    limiter = RequestWindow(core.DATA / 'instagram-api-rate.sqlite3',
                            policy={'windowMinutes': 1, 'maxRequests': 10, 'minIntervalSeconds': 6})
    counts = Counter()
    for item in rpc('/accounts')['instagram']['items']:
        if not item['enabled'] or not item['cookie_set']:
            continue
        if core.get_setting('instagram_cooldown', 0) > time.time():
            counts['deferred'] += 1
            break
        try:
            limiter.reserve()
        except ValueError:
            counts['deferred'] += 1
            break
        row = rpc('/instagram-refresh/snapshot', {'id': item['id']})
        if not row:
            counts['superseded'] += 1
            continue
        original = row.pop('cookies')
        jar = cookie_jar(original)
        identity = expected_identity(jar)
        if not identity:
            counts['identity_unavailable'] += 1
            continue
        status, fresh = check(jar, identity)
        if status == 'rate_limited':
            limiter.block()
            counts['rate_limited'] += 1
            break
        if status == 'updated':
            try:
                row['cookies'] = flatten(fresh, original)
            except ValueError:
                counts['unsupported_scope'] += 1
                continue
            if not all(row['cookies'].get(key) for key in ('sessionid', 'csrftoken')):
                counts['missing_required_cookie'] += 1
                continue
            try:
                status = rpc('/instagram-refresh/result', row)['status']
            except ValueError:
                counts['broker_rejected'] += 1
                continue
        counts['upstream_error' if status == 'error' else status] += 1
    return dict(counts)


def main():
    try:
        counts = run()
    except Exception:
        raise SystemExit('IG session maintenance failed') from None
    print(json.dumps(counts, sort_keys=True))
    handled = {'updated', 'unchanged', 'superseded', 'rate_limited', 'deferred', 'login_required'}
    if set(counts) - handled:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
