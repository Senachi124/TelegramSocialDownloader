"""Classify download limits and retain the cause of the shared Instagram cooldown."""
import json
import re

from .. import core

DEADLINE = 'instagram_cooldown'
DETAIL = 'instagram_cooldown_detail'


def _upstream(reason):
    return (isinstance(reason, str) and
            (reason == 'upstream_restriction' or re.fullmatch(r'http_[45][0-9]{2}', reason) is not None))


def result_reason(result):
    if result.get('wait_reason') == 'quota':
        return 'local_media_quota'
    diagnostic = result.get('instagram_diagnostic') or {}
    if diagnostic.get('kind') == 'local_request_budget':
        return 'local_request_budget'
    cause = result.get('instagram_limit_reason')
    if _upstream(cause):
        return cause
    status = diagnostic.get('status')
    if status == 429 or 429 in (result.get('http_errors') or ()):
        return 'http_429'
    if diagnostic.get('kind') == 'rate_limited':
        return 'upstream_restriction'
    if type(status) is int and 400 <= status <= 599 and result.get('retry_at'):
        return f'http_{status}'
    return 'unknown'


def _read(db):
    settings = dict(db.execute('SELECT key,value FROM settings WHERE key IN (?,?)', (DEADLINE, DETAIL)))
    until = json.loads(settings.get(DEADLINE, '0'))
    try:
        detail = json.loads(settings.get(DETAIL, '{}'))
    except (TypeError, ValueError):
        return until, 'unknown'
    # An older version or a manual reset may have replaced just the deadline.
    if isinstance(detail, dict) and until == detail.get('until') and _upstream(detail.get('reason')):
        return until, detail['reason']
    return until, 'unknown'


def active_cooldown():
    with core.db() as db:
        return _read(db)


def extend_cooldown(until, reason):
    """Keep deadline and provenance together, preserving a concurrent longer block."""
    reason = reason if _upstream(reason) else 'unknown'

    def update(db):
        current = _read(db)
        if current[0] >= until:
            return current
        db.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', (
            (DEADLINE, json.dumps(until)), (DETAIL, json.dumps({'until': until, 'reason': reason})),
        ))
        return until, reason

    return core.retry_write(update)
