#!/usr/bin/env python3
"""Add the bundled Takaneko provider to a standalone systemd installation."""
import argparse
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

import upgrade


BASE = Path('/opt/telegram-social-downloader/current')
CONTROL = Path('/var/lib/telegram-social-takaneko')
SECRETS = Path('/var/lib/telegram-takaneko-credentials')
CONFIG = Path('/etc/telegram-social-takaneko')
UNITS = Path('/etc/systemd/system')
TEMPLATES = Path(__file__).resolve().parent / 'takaneko'
WORKER = 'telegram-takaneko'
SOCKET = '/run/telegram-social-takaneko/control.sock'
PROVIDER_UNITS = {
    'takaneko_worker': 'telegram-social-takaneko.service',
    'takaneko_broker': 'telegram-social-takaneko-broker.service',
    'takaneko_links': 'telegram-social-takaneko-links.service',
}


def run(*args):
    subprocess.run(args, check=True)


def account():
    try:
        group = grp.getgrnam(WORKER)
    except KeyError:
        run('groupadd', '--system', WORKER)
        group = grp.getgrnam(WORKER)
    try:
        user = pwd.getpwnam(WORKER)
    except KeyError:
        run('useradd', '--system', '--gid', WORKER, '--no-create-home', '--home-dir', '/nonexistent',
            '--shell', '/usr/sbin/nologin', WORKER)
        user = pwd.getpwnam(WORKER)
    if not user.pw_uid or user.pw_gid != group.gr_gid:
        raise ValueError('Expected a dedicated telegram-takaneko service account')
    return user


def directory(path, uid, gid, mode):
    if not path.exists():
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        path.chmod(mode)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid or
            stat.S_IMODE(info.st_mode) != mode):
        raise ValueError('Unexpected ownership or permissions at ' + str(path))


def install_file(path, content, mode):
    """Keep existing local edits; verify before a retry after a partial first install."""
    if path.exists() or path.is_symlink():
        upgrade.trusted(path)
        if path.read_text() != content or stat.S_IMODE(path.stat().st_mode) != mode:
            raise ValueError('Existing configuration differs at ' + str(path))
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def storage_identity(path):
    # The input names the mounted directory that will own this provider's media and tmp.
    upgrade.trusted(path)
    if not path.is_dir() or not re.fullmatch(r'/[A-Za-z0-9_./-]*', str(path)) or '..' in path.parts:
        raise ValueError('Expected an absolute root-owned storage directory without spaces')
    mounts = json.loads(subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,UUID,FSROOT'], text=True))
    mount, = mounts['filesystems']
    if not re.fullmatch(r'[A-Za-z0-9-]+', mount['uuid'] or ''):
        raise ValueError('The selected filesystem must have a stable UUID')
    if shutil.disk_usage(path).free < 5 * 1024**3:
        raise ValueError('Takaneko media storage requires at least 5 GiB free')
    fsroot = Path(mount['fsroot']) / path.relative_to(mount['target'])
    if not re.fullmatch(r'/[A-Za-z0-9_./-]*', str(fsroot)):
        raise ValueError('Expected a simple filesystem root path')
    return mount['uuid'], fsroot


def link_base(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or
            parts.query or parts.fragment or not parts.path.endswith('/') or
            not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./-]*/', value)
            or '..' in Path(parts.path).parts):
        raise ValueError('Use an HTTPS download prefix ending in a slash')
    return value


def install(storage, url, cfg):
    uuid, fsroot = storage_identity(storage)
    url = link_base(url)
    if Path(cfg['current']) != BASE or cfg['web'] != 'telegram-social-web.service':
        raise ValueError('Use this installer with the standard Telegram Social Downloader service layout')
    python = BASE / '.venv/bin/python'
    run('runuser', '-u', 'telegram-social', '--', str(python), '-I', '-c', 'import takaneko_telegram')
    worker = account()
    directory(CONFIG, 0, 0, 0o755)
    directory(SECRETS, 0, 0, 0o700)
    directory(CONTROL, worker.pw_uid, worker.pw_gid, 0o700)
    media_root = storage/'telegram-social-takaneko'
    directory(media_root, 0, 0, 0o755)
    media = media_root/'media'
    tmp = media_root/'tmp'
    for path in (media, tmp):
        directory(path, worker.pw_uid, worker.pw_gid, 0o700)
    # Confirm traversal through the selected mount for the actual service identity.
    run('runuser', '-u', WORKER, '--', 'test', '-w', str(media))
    lock = CONFIG/'deploy.lock'
    install_file(lock, '', 0o644)
    provider_env = {
        'TAKANEKO_CONTROL': CONTROL, 'TAKANEKO_MEDIA': media, 'TMPDIR': tmp,
        'TAKANEKO_STORAGE_UUID': uuid, 'TAKANEKO_MEDIA_FSROOT': fsroot/media.relative_to(storage),
        'TAKANEKO_TMP_FSROOT': fsroot/tmp.relative_to(storage), 'TAKANEKO_LINK_BASE': url,
        'TAKANEKO_CONTROL_SOCKET': SOCKET, 'TAKANEKO_LINK_SOCKET': '/run/telegram-social-takaneko-links/web.sock',
        'TAKANEKO_SECRET_SOCKET': '/run/telegram-social-takaneko-broker/broker.sock',
        'TAKANEKO_CREDENTIALS_DIR': SECRETS, 'TAKANEKO_BROKER_USER': 'root',
        'TAKANEKO_BOT_USER': 'telegram-social', 'TAKANEKO_WORKER_USER': WORKER,
        'TAKANEKO_PUSH_SOCKET': '/run/telegram-social-push/transport.sock', 'TAKANEKO_DEPLOY_LOCK': lock,
    }
    install_file(CONFIG/'.env', ''.join(f'{k}={v}\n' for k,v in provider_env.items()), 0o600)
    for name in PROVIDER_UNITS.values():
        template = (TEMPLATES/(name+'.in')).read_text()
        install_file(UNITS/name, template.replace('@MEDIA@', str(media)).replace('@TMP@', str(tmp)), 0o644)
    push = 'telegram-social-push.service'
    registration = dict(**PROVIDER_UNITS, takaneko_control_socket=SOCKET, takaneko_data=str(CONTROL),
                        takaneko_deploy_lock=str(lock), push=push)
    for key, value in registration.items():
        if cfg.get(key) not in (None, value):
            raise ValueError('Existing deployment configuration differs at ' + key)
    dropin = f'[Service]\nEnvironment=MEDIA_BOT_TAKANEKO_UID={worker.pw_uid}\n'
    for unit in (push, cfg['web']):
        folder = UNITS/(unit+'.d')
        folder.mkdir(mode=0o755, exist_ok=True)
        upgrade.trusted(folder)
        install_file(folder/'takaneko.conf', dropin, 0o644)
    run('systemctl','daemon-reload')
    # A running transport may have a long file upload. Refuse to interrupt an
    # existing transport that still has to load the provider's registration.
    if upgrade.active(push):
        pid = upgrade.property_of(push, 'MainPID')
        with (Path('/proc')/pid/'environ').open('rb') as stream:
            registered = f'MEDIA_BOT_TAKANEKO_UID={worker.pw_uid}'.encode() in stream.read().split(b'\0')
        if not registered:
            raise RuntimeError('Drain other producers, stop the push service, then rerun the installer')
    controller = Path('/usr/local/lib/telegram-social-deploy/upgrade.py')
    if controller.exists():
        upgrade.trusted(controller)
        with tempfile.NamedTemporaryFile(dir=controller.parent, delete=False) as tmpfile:
            candidate = Path(tmpfile.name)
            try:
                tmpfile.write(Path(upgrade.__file__).read_bytes())
                tmpfile.flush()
                candidate.chmod(0o644)
                candidate.replace(controller)
            finally:
                candidate.unlink(missing_ok=True)
    # Register before starting daemons so even an interrupted activation is covered
    # by the next deployment's health checks and drain lock.
    cfg.update(registration)
    upgrade.atomic_json(Path(upgrade.CONFIG), cfg)
    run('systemctl', 'enable', '--now', push, PROVIDER_UNITS['takaneko_broker'],
        PROVIDER_UNITS['takaneko_links'], PROVIDER_UNITS['takaneko_worker'])
    run('systemctl','restart',cfg['web'])
    upgrade.check(cfg)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', required=True, type=Path, help='Existing mounted directory for media and temporary files')
    parser.add_argument('--link-base', required=True, help='Public HTTPS prefix routed to the Takaneko link socket')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root')
    cfg = upgrade.config(upgrade.CONFIG)
    backups = Path(cfg['backups'])
    upgrade.trusted(backups)
    with (backups/'upgrade.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (backups/'upgrade-in-progress.json').exists():
            parser.error('Complete or recover the current application upgrade first')
        install(args.storage, args.link_base, cfg)
    print('Takaneko is available under Subscriptions in the web console.')


if __name__ == '__main__':
    main()
