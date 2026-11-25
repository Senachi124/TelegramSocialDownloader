"""Serialized producer-only session access and atomic token rotation."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading

from media_bot.push_socket import _pack
from .credentials import normalize_credentials, normalize_session

ROOT = Path(os.getenv('TAKANEKO_CREDENTIALS_DIR', '/var/lib/telegram-takaneko-credentials'))
FILE = ROOT / 'credentials.json'
_lock = threading.Lock()


def save(data):
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, delete=False) as stream:
        path = Path(stream.name)
        try:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
            path.replace(FILE)
            directory = os.open(ROOT, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            path.unlink(missing_ok=True)


def session_digest(session):
    return hashlib.sha256(json.dumps(session, sort_keys=True).encode()).hexdigest()


def _credentials_reply(value, session):
    keys = sorted((item for item in value.get('openai_keys', []) if item.get('enabled') and item.get('key')),
                  key=lambda item: item['id'] != value.get('primary_key'))
    return {'ok': True, 'session': session, 'openai_keys': keys}


def request(uid, req, fd):
    if fd is not None:
        return {'ok': False}
    if req.get('op') == 'health':
        return {'ok': True}
    with _lock:
        try:
            value = json.loads(FILE.read_text())
        except FileNotFoundError:
            value = {}
        session = value.get('session', {})
        if req.get('op') == 'session_status':
            return {'ok': True, 'session_configured': bool(session)}
        if req.get('op') == 'get_session':
            return {'ok': True, 'session': session}
        if uid == 0 and req.get('op') == 'replace':
            save(normalize_credentials(req.get('credentials')))
            return {'ok': True}
        if req.get('op') == 'set_session':
            value['session'] = normalize_session(req.get('session'))
            save(value)
            return {'ok': True}
        if req.get('op') == 'get':
            return _credentials_reply(value, session)
        if req.get('op') in ('refresh', 'check_session'):
            probe = req['op'] == 'check_session'
            if not hmac.compare_digest(req.get('expected_session_digest', ''), session_digest(session)):
                return {'ok': True, 'current': False} if probe else {'ok': False}
            if probe and req.keys() == {'op', 'expected_session_digest'}:
                return {'ok': True, 'current': True}
            if not all(isinstance(req.get(k), str) and len(req[k]) <= 16384 for k in ('accessToken', 'refreshToken')):
                return {'ok': False}
            rotated = {k: req[k] for k in ('accessToken', 'refreshToken') if req[k]}
            merged = {**session, **rotated}
            try:
                normalize_session(merged)
                # A legacy file may contain additional fields. Check the actual
                # packet, including translation keys, before replacing the file.
                _pack(_credentials_reply(value, merged))
            except ValueError:
                return {'ok': False}
            value['session'] = merged
            save(value)
            return {'ok': True, 'current': True} if probe else {'ok': True}
    return {'ok': False}


def main():
    from media_bot.push_socket import serve
    path = os.getenv('TAKANEKO_SECRET_SOCKET', '/run/telegram-takaneko-broker/broker.sock')
    worker = pwd.getpwnam(os.getenv('TAKANEKO_WORKER_USER', 'telegram-takaneko')).pw_uid
    serve(path, {0, worker}, request)


if __name__ == '__main__':
    main()
