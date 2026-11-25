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


def describe_part(item):
    return (f"Delivery {item['delivery_id']}, recipient {item['user_id']}, "
            f"part {item['position'] + 1}/{item['total']} ({item['kind']})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('worker', 'broker', 'links', 'credentials', 'enable', 'disable', 'pending'):
        commands.add_parser(name)
    commands.add_parser('retry').add_argument('delivery', type=int)
    resolve = commands.add_parser('resolve')
    resolve.add_argument('delivery', type=int)
    resolve.add_argument('result', choices=('sent', 'retry'))
    resolve.add_argument('--position', type=int, help='Zero-based position printed by pending')
    resolve.add_argument('--attempt', help='Attempt token printed by pending')
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
        if args.position is None or args.attempt is None:
            if args.position is not None or args.attempt is not None or not sys.stdin.isatty():
                parser.error('Supply both --position and --attempt from pending.')
            try:
                item = feed.current_review(args.delivery)
            except ValueError as error:
                parser.exit(1, f'{error}\n')
            print(describe_part(item))
            if input(f"After checking the recipient's chat, type {args.result} to confirm: ") != args.result:
                parser.exit(1, 'Decision cancelled.\n')
            args.position, args.attempt = item['position'], item['attempt']
        result = feed.resolve_delivery(args.delivery, args.position, args.attempt, args.result)
        if not result['ok']:
            parser.exit(1, 'Delivery could not be resolved: ' + result['error'] + '\n')
    else:
        with state.db() as db:
            for row in db.execute("SELECT id,state,error FROM tk_deliveries WHERE state='paused' ORDER BY id"):
                print(*row)
        after = 0
        while after is not None:
            page = feed.review_deliveries(after)
            for item in page['items']:
                print(describe_part(item), f"position={item['position']} attempt={item['attempt']}")
            after = page['next_after']


if __name__ == '__main__':
    main()
