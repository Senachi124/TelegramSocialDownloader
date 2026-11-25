"""Verify the configured data filesystem and temporary directory before transfers."""
import json
import os
from pathlib import Path
import shutil
import subprocess

from .config import data_dir

RESERVE_BYTES = 5 * 1024**3


class StorageError(ValueError):
    pass


def check(*, download=False, data=None, verify_identity=True):
    try:
        root = data_dir()
        if data is not None and Path(data) != root:
            raise StorageError('Unexpected data directory')
        tmp = Path(os.environ['TMPDIR'])
        if not tmp.is_relative_to(root / 'tmp'):
            raise StorageError('Temporary directory must be inside the configured data tree')
        device = root.stat().st_dev
        for path in (root, root / 'media', tmp):
            if path.resolve(strict=True) != path or not path.is_dir() or path.stat().st_dev != device:
                raise StorageError('Service storage is unavailable')
            expected_uuid = os.environ.get('MEDIA_BOT_STORAGE_UUID')
            if verify_identity and expected_uuid:
                output = subprocess.check_output(
                    ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,UUID,FSROOT'],
                    timeout=10, stderr=subprocess.DEVNULL)
                mount, = json.loads(output)['filesystems']
                mount_path = Path(mount['fsroot']) / path.relative_to(mount['target'])
                expected = Path(os.environ.get('MEDIA_BOT_FILESYSTEM_ROOT', str(root))) / path.relative_to(root)
                if mount.get('uuid') != expected_uuid or mount_path != expected:
                    raise StorageError('Configured filesystem identity or root does not match')
        if download and shutil.disk_usage(root).free < RESERVE_BYTES:
            raise StorageError('Storage reserve reached')
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, StorageError):
            raise
        raise StorageError('Service storage is unavailable') from error


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', required=True)
    parser.add_argument('--download', action='store_true', help='also check the transfer reserve')
    args = parser.parse_args()
    try:
        check(download=args.download)
    except StorageError as error:
        parser.exit(1, str(error) + '\n')
    print('Service storage verified.')


if __name__ == '__main__':
    main()
