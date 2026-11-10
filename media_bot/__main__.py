"""Start the background worker; importing this package never starts polling."""
import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description='Telegram Instagram, TikTok and X download worker')
    parser.add_argument('--check', action='store_true', help='validate runtime inputs without network access')
    args = parser.parse_args()
    from . import config
    try:
        config.validate()
    except config.ConfigurationError as error:
        parser.exit(2, str(error) + '\n')
    if args.check:
        print('Runtime configuration is valid; no network requests made.')
        return
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    from . import worker
    try:
        worker.main()
    except KeyboardInterrupt:
        pass
    except Exception:
        parser.exit(1, 'Worker stopped; inspect runtime inputs and available storage.\n')


if __name__ == '__main__':
    main()
