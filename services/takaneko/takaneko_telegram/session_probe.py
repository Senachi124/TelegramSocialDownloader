"""One bounded check of the saved Fanclub login. Only a fixed exit code leaves the process."""
import signal
import sys

from . import transport
from .credentials import normalize_session
from .secrets import session_digest
from .takaneko_api import Client, FanclubConnectionError, FanclubError, FanclubLoginRejected

# Reserve ordinary interpreter failures (e.g. 1 or 2) for "unavailable".
RESULTS = {
    20: 'connected', 21: 'no_session', 22: 'unauthorized', 23: 'forbidden',
    24: 'rate_limited', 25: 'network', 26: 'invalid', 27: 'upstream',
    28: 'unavailable', 29: 'changed',
}


class SessionChanged(Exception):
    pass


def _compare(session, **tokens):
    result = transport.secret('check_session', {'expected_session_digest': session_digest(session), **tokens})
    if result.get('current') is False:
        raise SessionChanged
    if result.get('current') is not True:
        raise RuntimeError('Session comparison unavailable')


def check():
    try:
        saved = transport.secret('get_session')['session']
        if saved == {}:
            return 'no_session'
        def checkpoint(snapshot):
            nonlocal saved
            if saved != snapshot:
                # Persist a rotation before checking the count. A later timeout
                # or invalid count cannot discard an already-issued refresh token.
                tokens = {k: snapshot.get(k, '') for k in ('accessToken', 'refreshToken')}
                try:
                    normalize_session({k: v for k, v in tokens.items() if v})
                except ValueError:
                    raise FanclubError('Invalid rotated Fanclub login') from None
                _compare(saved, **tokens)
                saved = snapshot

        try:
            Client(saved, api_timeout=2.5).check_session(checkpoint)
            result = 'connected'
        except FanclubConnectionError:
            result = 'network'
        except FanclubLoginRejected:
            result = 'unauthorized'
        except FanclubError as error:
            if error.status is None:
                result = 'invalid'
            else:
                result = {401: 'unauthorized', 403: 'forbidden', 429: 'rate_limited'}.get(error.status, 'upstream')
        # The administrator can replace a session while this request is
        # running. Return an outcome only for the login the broker still holds.
        _compare(saved)
        return result
    except SessionChanged:
        return 'changed'
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return 'unavailable'


if __name__ == '__main__':
    # Also bound an orphaned probe if its supervising worker exits abruptly.
    signal.alarm(8)
    outcome = check()
    sys.exit(next(code for code, value in RESULTS.items() if value == outcome))
