#!/usr/bin/env python3
"""Install a verified release wheel into an existing systemd installation."""
import argparse
import contextlib
from datetime import datetime, timezone
from email.parser import BytesParser
import fcntl
import grp
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile


CONFIG = '/etc/telegram-social-deploy/config.json'
MANIFEST = 'DEPLOYMENT-MANIFEST.json'


_NUMBER = r'(0|[1-9][0-9]*)'
_VERSION = re.compile(r'v' + r'\.'.join([_NUMBER]*3) + r'(?:\.dev' + _NUMBER + r')?')


def version(ref):
    match = _VERSION.fullmatch(ref)
    if not match:
        raise ValueError('Expected a release version or development build')
    major, minor, patch, dev = match.groups()
    # A stable release follows every development build of the same release.
    return int(major), int(minor), int(patch), dev is None, int(dev or 0)


def trusted(path):
    """Root-installed files/directories may also use the root group."""
    for item in (path, *path.parents):
        info = item.lstat()
        if (info.st_uid or info.st_mode & 0o002 or
                (info.st_mode & 0o020 and info.st_gid) or stat.S_ISLNK(info.st_mode)):
            raise ValueError('Unsafe ownership or permissions on ' + str(item))


def config(filename):
    filename = Path(filename)
    trusted(filename)
    values = json.loads(filename.read_text())
    provider_keys = ('takaneko_worker','takaneko_broker','takaneko_links','takaneko_data','takaneko_deploy_lock')
    if any(values.get(key) for key in provider_keys) and not all(values.get(key) for key in (*provider_keys, 'push')):
        raise ValueError('Takaneko deployment registration is incomplete')
    paths = ['current','releases','data','backups','web_socket']
    paths.extend(key for key in ('takaneko_data','takaneko_deploy_lock','takaneko_control_socket','takaneko_links_socket','takaneko_broker_socket') if values.get(key))
    for field in paths:
        if not Path(values[field]).is_absolute() or '..' in Path(values[field]).parts:
            raise ValueError('Invalid path for ' + field)
    for field in ('worker','web','broker','push','refresh_timer','refresh_service','takaneko_worker','takaneko_broker','takaneko_links'):
        if values.get(field) and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.(?:service|timer)',values[field]):
            raise ValueError('Invalid systemd unit for ' + field)
    if not str(values['health_path']).startswith('/'):
        raise ValueError('Invalid health path')
    return values


def wheel_info(wheel,tag):
    version(tag)
    filename = 'telegram_social_downloader-' + tag[1:] + '-py3-none-any.whl'
    if wheel.name != filename:
        raise ValueError('Unexpected wheel filename')
    with zipfile.ZipFile(wheel) as archive:
        metadata = 'telegram_social_downloader-' + tag[1:] + '.dist-info/METADATA'
        record = BytesParser().parsebytes(archive.read(metadata))
        if record['Name'] != 'telegram-social-downloader' or record['Version'] != tag[1:]:
            raise ValueError('Wheel distribution metadata mismatch')
        if any('@' in dep for dep in record.get_all('Requires-Dist',[])):
            raise ValueError('Release dependencies must use the package index')
    return hashlib.sha256(wheel.read_bytes()).hexdigest()


def atomic_json(path,value):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((json.dumps(value,indent=2)+'\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def spawn(args, **kwargs):
    return subprocess.run(args,check=True,stdin=subprocess.DEVNULL,**kwargs)


def systemctl(cmd,*units):
    spawn(['systemctl',cmd,*units],timeout=160)


def active(unit):
    return subprocess.run(['systemctl','is-active','--quiet',unit]).returncode == 0


def property_of(unit,name):
    return subprocess.check_output(['systemctl','show',unit,'-p',name,'--value'],text=True).strip()


def rollback_base(old, previous):
    if not version('v'+previous['version'])[3]:
        base = previous.get('trial_base') or {}
    else:
        base = {key: previous.get(key) for key in ('version','commit','wheel_sha256')}
        # Early installers kept the release wheel next to the manifest without its digest.
        if base['wheel_sha256'] is None:
            wheel = old / ('telegram_social_downloader-' + base['version'] + '-py3-none-any.whl')
            if wheel.is_file():
                trusted(wheel)
                base['wheel_sha256'] = wheel_info(wheel, 'v'+base['version'])
    if (not base.get('version') or not version('v'+base['version'])[3]
            or not re.fullmatch(r'[a-f0-9]{40}',base.get('commit') or '')
            or not re.fullmatch(r'[a-f0-9]{64}',base.get('wheel_sha256') or '')):
        raise ValueError('A trial requires verified stable release provenance for rollback')
    return base


def prepare(old,root,wheel,tag,sha,wheel_digest,base=None):
    target = root/('telegram-'+tag[1:]+'-'+sha[:12])
    if target.exists():
        trusted(target)
        manifest=json.loads((target/MANIFEST).read_text())
        if manifest.get('commit')!=sha or manifest.get('wheel_sha256')!=wheel_digest:
            raise ValueError('Existing release directory has different provenance')
        if base is not None and manifest.get('trial_base') != base:
            manifest['trial_base'] = base
            atomic_json(target/MANIFEST, manifest)
            (target/MANIFEST).chmod(0o644)
        return target
    previous_mask=os.umask(0o022)
    target.mkdir(mode=0o755)
    try:
        oldenv=old/'.venv'
        for name in ('.venv','systemd','tools'):
            if (old/name).exists():
                shutil.copytree(old/name,target/name,symlinks=True,ignore=shutil.ignore_patterns('__pycache__'))
        venv=target/'.venv'
        for binary in (venv/'bin').iterdir():
            if binary.is_symlink() or not binary.is_file():
                continue
            with binary.open('rb') as reader:
                prefix=reader.read(2)
            if prefix==b'#!':
                binary.write_bytes(binary.read_bytes().replace(os.fsencode(oldenv),os.fsencode(venv)))
        command=[str(venv/'bin/python'),'-Im','pip','--isolated','--disable-pip-version-check']
        # Binary-only resolution reuses existing pins while allowing a release to change them.
        spawn(command+['install','--only-binary=:all:','--index-url','https://pypi.org/simple',str(wheel)],
              cwd='/',timeout=600)
        spawn(command+['check'],cwd='/',timeout=60)
        manifest=json.loads((old/MANIFEST).read_text())
        if base is None:
            manifest.pop('trial_base', None)
        else:
            manifest['trial_base'] = base
        manifest.update(version=tag[1:],commit=sha,wheel_sha256=wheel_digest,branch='main')
        atomic_json(target/MANIFEST,manifest)
        (target/MANIFEST).chmod(0o644)
        return target
    except BaseException:
        shutil.rmtree(target)
        raise
    finally:
        os.umask(previous_mask)


# The operator never opens the service-owned database with root privileges.
DB_PROGRAM='''
import sqlite3,sys
from pathlib import Path
path=Path(sys.argv[1])/'state.sqlite3'
mode=sys.argv[2]
db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=10)
with db:
    if mode=='enabled':
        row=db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
        print(row[0] if row else 'false')
    elif mode=='set':
        db.execute("INSERT OR REPLACE INTO settings VALUES ('enabled',?)",(sys.argv[3],))
    elif mode=='snapshot':
        # The root process opens the protected output descriptor before dropping privileges.
        import shutil
        busy, _, _=db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if busy:
            raise RuntimeError('Database checkpoint busy')
        # All writers have stopped. Stream the checkpoint to the protected backup directly.
        with path.open('rb') as stream:
            shutil.copyfileobj(stream,sys.stdout.buffer)
    else:
        tables=dict(db.execute("SELECT name,1 FROM sqlite_master WHERE type='table'"))
        busy=any(table in tables and db.execute(query).fetchone() for table,query in [
           ('jobs',"SELECT 1 FROM jobs WHERE state IN ('running','sending') LIMIT 1"),
           ('telegram_inbox',"SELECT 1 FROM telegram_inbox WHERE state='working' LIMIT 1"),
           ('admin_pool_checks',"SELECT 1 FROM admin_pool_checks WHERE state='running' LIMIT 1")])
        print(int(busy))
db.close()
'''


def service_identity(unit):
    user=pwd.getpwnam(property_of(unit,'User'))
    if user.pw_uid==0:
        raise ValueError('The worker must run under a dedicated service user')
    def group_id(value):
        return int(value) if value.isdecimal() else grp.getgrnam(value).gr_gid

    primary = property_of(unit, 'Group')
    groups = property_of(unit, 'SupplementaryGroups').split()
    return dict(user=user.pw_uid, group=group_id(primary) if primary else user.pw_gid,
                extra_groups=[group_id(name) for name in groups])


def database(cfg,op,*args,output=None):
    result=spawn(['/usr/bin/python3','-I','-c',DB_PROGRAM,cfg['data'],op,*args],
        **service_identity(cfg['worker']),cwd='/',timeout=120,
        stdout=output if output is not None else subprocess.PIPE)
    return result.stdout.decode().strip() if result.stdout is not None else None


def free_of_work(cfg):
    if database(cfg,'busy')!='0':
        return False
    pid=property_of(cfg['worker'],'MainPID')
    if pid=='0':
        return False
    try:
        tasks=list((Path('/proc')/pid/'task').iterdir())
        named={}
        all_waiting=True
        for task in tasks:
            if (task/'children').read_text().strip():
                return False
            name=(task/'comm').read_text().strip()
            waiting=(task/'wchan').read_text().strip().startswith('futex')
            named[name]=waiting
            all_waiting &= waiting
        # Python 3.14 publishes thread names. On older Python, require every thread idle.
        if not all_waiting and not all(named.get(key) for key in ('telegram-transl','telegram-poll','admin-notificat')):
            return False
        if cfg.get('push'):
            pushpid=property_of(cfg['push'],'MainPID')
            if pushpid=='0' or len(list((Path('/proc')/pushpid/'task').iterdir()))!=1:
                return False
    except FileNotFoundError:
        return False
    return True


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self,path,peer_uid=None):
        super().__init__('localhost',timeout=3)
        self.path=path
        self.peer_uid=peer_uid
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        if self.peer_uid is not None:
            peer = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            if struct.unpack('3i', peer)[1] != self.peer_uid:
                raise PermissionError('Unexpected link server identity')


def request_json(sock,path):
    with contextlib.closing(UnixHTTP(sock)) as connection:
        connection.request('GET',path)
        response=connection.getresponse()
        if response.status!=200:
            raise RuntimeError('Health endpoint returned '+str(response.status))
        return json.loads(response.read(8192))


def units(cfg):
    order = ('broker','web','push','worker','takaneko_broker','takaneko_links','takaneko_worker')
    return [cfg[key] for key in order if cfg.get(key)]


@contextlib.contextmanager
def provider_guard(cfg):
    if not cfg.get('takaneko_worker'):
        yield
        return
    # The producer holds a shared lock during inventory and delivery. Wait while the
    # bot is still accepting sends, then keep the producer idle through rollback.
    path = Path(cfg['takaneko_deploy_lock'])
    trusted(path)
    if not path.is_file():
        raise ValueError('Expected a regular Takaneko deployment lock file')
    with path.open('rb') as lock:
        deadline = time.monotonic() + 150
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Takaneko operations still active')
                time.sleep(1)
        yield


# Use the installed client as the correct socket peer. Only boolean health or
# membership flags cross the deployment boundary. Never request credentials.
PROVIDER_PROBE = """
import sys
from media_bot.push_socket import exchange
path, uid, mode = sys.argv[1:]
request = {'op': 'subscribed', 'user_id': 1} if mode == 'membership' else {'op': 'health'}
reply = exchange(path, int(uid), request, timeout=3)
if mode == 'membership':
    ok = reply.get('ok') is True and type(reply.get('enabled')) is bool
elif mode == 'legacy-broker':
    ok = reply == {'ok': False}
else:
    ok = reply == {'ok': True}
raise SystemExit(0 if ok else 1)
"""


def provider_socket(cfg, key, unit, name):
    if cfg.get(key):
        return cfg[key]
    directory = property_of(unit, 'RuntimeDirectory')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', directory) or directory in ('.', '..'):
        raise ValueError('Register an explicit path for ' + key)
    return str(Path('/run') / directory / name)


def provider_probe(cfg, path, server, client, mode):
    expected = pwd.getpwnam(property_of(server, 'User')).pw_uid
    result = subprocess.run([str(Path(cfg['current'])/'.venv/bin/python'), '-IB', '-c', PROVIDER_PROBE,
                             path, str(expected), mode], **service_identity(client), cwd='/', timeout=6,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def provider_ready(cfg):
    if not cfg.get('takaneko_worker'):
        return True
    manifest = json.loads((Path(cfg['current'])/MANIFEST).read_text())
    legacy = manifest['version'] == '1.1.0'
    worker = cfg['takaneko_worker']
    control = provider_socket(cfg, 'takaneko_control_socket', worker, 'control.sock')
    if not provider_probe(cfg, control, worker, cfg['web'], 'membership' if legacy else 'health'):
        return False
    if legacy:
        # The first provider release supported neither readiness endpoint.
        # An unknown broker operation confirms the request loop and peer without returning stored credentials.
        broker = cfg['takaneko_broker']
        path = provider_socket(cfg, 'takaneko_broker_socket', broker, 'broker.sock')
        if not provider_probe(cfg, path, broker, worker, 'legacy-broker'):
            return False
    links = cfg['takaneko_links']
    path = provider_socket(cfg, 'takaneko_links_socket', links, 'web.sock')
    owner = pwd.getpwnam(property_of(links, 'User')).pw_uid
    with contextlib.closing(UnixHTTP(path, owner)) as connection:
        connection.request('GET', '/healthz')
        reply = connection.getresponse()
        if legacy:
            # Legacy links reject the unknown route and return 503 during a drain.
            return reply.status in (404, 503)
        return reply.status == 200 and json.loads(reply.read(8192)) == {'status': 'ok'}


def check(cfg):
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            pid=property_of(cfg['worker'],'MainPID')
            # Polling, translation and download threads start after database recovery.
            worker_ready=pid!='0' and len(list((Path('/proc')/pid/'task').iterdir())) >= 6
            if (worker_ready and all(active(u) for u in units(cfg)) and provider_ready(cfg) and
                    request_json(cfg['web_socket'],cfg['health_path']).get('status')=='ok' and
                    (not cfg.get('broker_socket') or
                     'token_set' in request_json(cfg['broker_socket'],'/status'))):
                return
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError,http.client.HTTPException):
            pass
        time.sleep(1)
    raise RuntimeError('Service readiness check failed')


def switch(current,target):
    with tempfile.TemporaryDirectory(dir=current.parent) as temp:
        link=Path(temp)/'current'
        link.symlink_to(target)
        link.replace(current)


def restore(cfg,journal):
    current=Path(cfg['current'])
    old=Path(journal['previous'])
    if current.resolve()!=old:
        systemctl('stop',*reversed(units(cfg)))
        switch(current,old)
    for unit in units(cfg):
        systemctl('start',unit)
    check(cfg)
    database(cfg,'set',journal['enabled'])
    if journal['timer_active']:
        systemctl('start',cfg['refresh_timer'])


@contextlib.contextmanager
def locked_config(filename):
    # Only the lock location is needed before locking. Registration and unit
    # changes from an installer/adoption are read after it releases the lock.
    path = Path(filename)
    trusted(path)
    backups = Path(json.loads(path.read_text())['backups'])
    if not backups.is_absolute() or '..' in backups.parts:
        raise ValueError('Invalid rollback directory')
    trusted(backups)
    if backups.stat().st_mode & 0o077:
        raise ValueError('Rollback directory must be root-only')
    with (backups/'upgrade.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cfg = config(path)
        if Path(cfg['backups']) != backups:
            raise RuntimeError('Rollback directory changed during registration')
        yield cfg


def upgrade(filename,wheel,tag,sha,*,trial=False,allow_trial_rollback=False):
    stable = version(tag)[3]
    if (trial and stable) or (not trial and not stable):
        raise ValueError('The deployment mode must match the wheel version')
    if not re.fullmatch(r'[a-f0-9]{40}',sha):
        raise ValueError('Invalid release commit')
    trusted(wheel)
    wheel_digest=wheel_info(wheel,tag)
    with locked_config(filename) as cfg:
        current=Path(cfg['current'])
        root=Path(cfg['releases'])
        backups=Path(cfg['backups'])
        trusted(root)
        trusted(current.parent)
        if not current.is_symlink() or current.resolve().parent!=root:
            raise ValueError('Active release is outside release root')
        journal_file=backups/'upgrade-in-progress.json'
        if journal_file.exists():
            with provider_guard(cfg):
                restore(cfg,json.loads(journal_file.read_text()))
            journal_file.unlink()
        old=current.resolve(strict=True)
        trusted(old)
        previous=json.loads((old/MANIFEST).read_text())
        if previous.get('commit')==sha and previous['version']==tag[1:]:
            if trial and previous.get('wheel_sha256') != wheel_digest:
                raise ValueError('The active trial has a different wheel digest')
            check(cfg)
            print('Requested version is already active.')
            return
        base = {'version':tag[1:], 'commit':sha, 'wheel_sha256':wheel_digest}
        returning = (stable and allow_trial_rollback and not version('v'+previous['version'])[3]
                     and previous.get('trial_base') == base)
        if not returning and version(tag)<=version('v'+previous['version']):
            raise ValueError('Release must advance the installed version')
        trial_base = rollback_base(old,previous) if trial else None
        target=prepare(old,root,wheel,tag,sha,wheel_digest,trial_base)
        freeze=cfg.get('freeze_file')
        if freeze and Path(freeze).exists():
            raise RuntimeError('Backup freeze in progress')
        check(cfg)
        enabled=database(cfg,'enabled')
        if enabled not in ('true','false'):
            raise ValueError('Unexpected bot admission state')
        timer_active=bool(cfg.get('refresh_timer') and active(cfg['refresh_timer']))
        journal=dict(previous=str(old),next=str(target),enabled=enabled,timer_active=timer_active)
        with provider_guard(cfg):
            atomic_json(journal_file,journal)
            try:
                if timer_active:
                    systemctl('stop',cfg['refresh_timer'])
                if cfg.get('refresh_service') and active(cfg['refresh_service']):
                    raise RuntimeError('Cookie refresh in progress')
                # Freeze administrative edits first, then let all previously admitted work finish.
                systemctl('stop',cfg['web'])
                database(cfg,'set','false')
                deadline=time.monotonic()+150
                while not free_of_work(cfg):
                    if time.monotonic()>deadline:
                        raise RuntimeError('Telegram operations still active')
                    time.sleep(1)
                if freeze and Path(freeze).exists():
                    raise RuntimeError('Backup freeze in progress')
                systemctl('stop',*reversed(units(cfg)))
                if database(cfg,'busy')!='0':
                    raise RuntimeError('Work started during the drain')
                stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                snapshots = [(cfg, stamp+'-'+tag+'.sqlite3')]
                if cfg.get('takaneko_worker'):
                    provider = dict(cfg, worker=cfg['takaneko_worker'], data=cfg['takaneko_data'])
                    snapshots.append((provider, stamp+'-'+tag+'-takaneko.sqlite3'))
                for service, name in snapshots:
                    with open(backups/name,'xb',opener=lambda p,flags: os.open(p,flags,0o600)) as dst:
                        database(service,'snapshot',output=dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                switch(current,target)
                for unit in units(cfg):
                    systemctl('start',unit)
                check(cfg)
                database(cfg,'set',enabled)
                if timer_active:
                    systemctl('start',cfg['refresh_timer'])
                journal_file.unlink()
                print(f'Activated {tag[1:]} at {sha}.')
            except BaseException:
                restore(cfg,journal)
                journal_file.unlink()
                raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=CONFIG)
    for name in ('wheel','sha'):
        parser.add_argument('--'+name,required=True)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--tag')
    source.add_argument('--trial-version')
    parser.add_argument('--allow-trial-rollback',action='store_true')
    args=parser.parse_args()
    if os.getuid()!=0:
        parser.error('The local upgrade controller requires root')
    ref=args.tag or 'v'+args.trial_version
    upgrade(args.config,Path(args.wheel).resolve(strict=True),ref,args.sha,trial=args.trial_version is not None,
            allow_trial_rollback=args.allow_trial_rollback)


if __name__=='__main__':
    main()
