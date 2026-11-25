"""Command-line entry points for the Telegram service."""
import argparse
import importlib
import logging
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', metavar='PATH', help='load a private service environment file')
    parser.add_argument('--check', action='store_true', help='validate configuration without contacting Telegram')
    parser.add_argument('component', nargs='?', default='worker',
                        choices=('worker', 'web', 'broker', 'push', 'refresh-cookies'))
    args = parser.parse_args()
    from . import config
    os.umask(0o077)
    try:
        if args.env_file:
            config.load_environment(args.env_file)
        config.validate_service(args.component)
    except (OSError, config.ConfigurationError) as error:
        parser.exit(2, f'Configuration error: {error}\n')
    if args.check:
        print('Configuration is valid.')
        return
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    module = 'instagram_cookie_maintenance' if args.component == 'refresh-cookies' else args.component
    component = importlib.import_module('.' + module, __package__)
    if module == 'web':
        component.main([])  # The dispatcher already consumed the service arguments.
    else:
        component.main()


if __name__ == '__main__':
    main()
