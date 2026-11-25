"""Manage the Takaneko provider, credential broker and capability link service."""
import argparse
import json
import os
import pwd
import signal
import sys
import threading

from . import state, takaneko_feed as feed, transport


def worker():
    from media_bot import push_socket

    state.validate_link_base()
    state.storage_guard()
    state.initialize()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    path = os.getenv('TAKANEKO_CONTROL_SOCKET', '/run/telegram-takaneko/control.sock')
    caller_uid = pwd.getpwnam(os.getenv('TAKANEKO_BOT_USER', 'telegram-social')).pw_uid

    def listen():
        try:
            push_socket.serve(path, {caller_uid}, transport.control)
        finally:
            if not stop.is_set():
                os._exit(1)

    threading.Thread(target=listen, daemon=True).start()
    feed.run(stop)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('worker', 'broker', 'links', 'credentials', 'enable', 'disable', 'pending'):
        commands.add_parser(name)
    commands.add_parser('retry').add_argument('delivery', type=int)
    resolve = commands.add_parser('resolve')
    resolve.add_argument('delivery', type=int)
    resolve.add_argument('result', choices=('sent', 'retry'))
    args = parser.parse_args()
    if args.command == 'worker':
        worker()
    elif args.command == 'broker':
        from . import secrets
        secrets.main()
    elif args.command == 'links':
        from . import links
        links.main()
    elif args.command == 'credentials':
        from .credentials import normalize_credentials
        if os.geteuid() != 0:
            parser.error('Run credentials as root and supply JSON on standard input')
        try:
            raw = sys.stdin.buffer.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError
            credentials = normalize_credentials(json.loads(raw))
            transport.secret('replace', {'credentials': credentials})
        except (ValueError, OSError, RuntimeError):
            parser.exit(1, 'Credential import failed. Check the JSON and broker status.\n')
        print('Provider credentials saved.')
    elif args.command in ('enable', 'disable'):
        try:
            result = transport.feature_control(args.command == 'enable')
        except (OSError, RuntimeError, ValueError):
            parser.exit(1, 'Fanclub session status unavailable. Check the credential broker.\n')
        if result.get('error') == 'session_required':
            parser.exit(1, 'Save a Fanclub session before enabling notifications.\n')
    elif args.command == 'retry':
        feed.retry_delivery(args.delivery)
    elif args.command == 'resolve':
        feed.resolve_uncertain(args.delivery, args.result)
    else:
        with state.db() as db:
            for row in db.execute("""SELECT id,state,error FROM tk_deliveries
                                     WHERE state IN ('paused','uncertain') ORDER BY id"""):
                print(*row)


if __name__ == '__main__':
    main()
