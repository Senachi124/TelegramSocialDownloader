"""Apply a verified release requested by the configured repository's production workflow."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from urllib.request import Request, urlopen

CONTROL = Path('/etc/telegram-social-deploy')
STATE = Path('/var/lib/telegram-social-deploy')
TOOLS = Path(__file__).resolve().parent
TAG = re.compile(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')
SHA = re.compile(r'[a-f0-9]{40}')
DEV_WHEEL = re.compile(r'telegram_social_downloader-((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}\.dev(?:0|[1-9][0-9]*))-py3-none-any\.whl')
LIMIT = 32*1024*1024


def fetch(url, limit):
    req = Request(url, headers={'User-Agent':'telegram-social-deploy/1', 'Accept':'application/json'})
    with urlopen(req, timeout=30) as response:
        raw = response.read(limit+1)
    if len(raw)>limit:
        raise ValueError('Remote file exceeded its size limit')
    return raw


def write_status(identity, sha, state):
    path = STATE/'status.json'
    with tempfile.NamedTemporaryFile(dir=STATE, delete=False, mode='w') as stream:
        temp=Path(stream.name)
        json.dump(dict(deployment=identity,sha=sha,state=state),stream)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(),0o644)
    temp.replace(path)


def select(requests, previous, since):
    for item in requests:
        if (item['id'] > previous and item['task']=='telegram-upgrade'
                and item['creator']['login']=='github-actions[bot]'
                and item['environment']=='telegram-app'
                and datetime.fromisoformat(item['created_at'].replace('Z','+00:00')).timestamp()>=since):
            return item


def staged_distribution(sha):
    directory = STATE / 'trials'
    path = directory / (sha + '.zip')
    for item, check_type in ((STATE, stat.S_ISDIR), (directory, stat.S_ISDIR), (path, stat.S_ISREG)):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not check_type(info.st_mode):
            raise ValueError('Unsafe trial staging path')
    if not 0 < info.st_size <= LIMIT:
        raise ValueError('Invalid trial distribution size')
    return path.read_bytes()


def trial_wheel(host, sha, api):
    runs = api('actions/workflows/release.yml/runs?event=push&status=success&head_sha='+sha)
    run = next((r for r in runs['workflow_runs'] if r['head_sha'] == sha
                and r['head_branch'] == host['branch'] and r['event'] == 'push'
                and r['conclusion'] == 'success' and r['path'] == '.github/workflows/release.yml'
                and r['head_repository']['full_name'].lower() == host['repository'].lower()), None)
    if run is None:
        raise ValueError('Default-branch CI has not passed for the trial commit')
    artifacts = api(f"actions/runs/{run['id']}/artifacts")
    artifact, = [a for a in artifacts['artifacts'] if a['name'] == 'distribution' and not a['expired']]
    raw = staged_distribution(sha)
    if artifact.get('digest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('Staged archive differs from the verified CI artifact')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        item, = [i for i in archive.infolist() if i.filename.endswith('.whl')]
        match = DEV_WHEEL.fullmatch(item.filename)
        if not match or not 0 < item.file_size <= LIMIT:
            raise ValueError('Expected one development wheel in the CI artifact')
        return item.filename, archive.read(item), ['--trial-version', match[1]]


def release_wheel(host, tag, sha, api):
    if api('commits/'+tag)['sha']!=sha:
        raise ValueError('Release tag differs from requested revision')
    comparison=api('compare/'+sha+'...'+host['branch'])
    if comparison['status'] not in ('ahead','identical'):
        raise ValueError('Release commit must belong to the deployment branch')
    ci=api('actions/workflows/release.yml/runs?event=push&status=success&head_sha='+sha)
    if not any(row['head_sha']==sha and row['head_branch']==tag for row in ci['workflow_runs']):
        raise ValueError('Release checks have not passed for this tag')
    release=api('releases/tags/'+tag)
    if release['tag_name']!=tag or release['draft'] or release['prerelease']:
        raise ValueError('Expected a published stable release')
    name='telegram_social_downloader-'+tag[1:]+'-py3-none-any.whl'
    asset,=[row for row in release['assets'] if row['name']==name]
    url='https://github.com/'+host['repository']+'/releases/download/'+tag+'/'+name
    raw=fetch(url,32*1024*1024)
    if asset.get('digest')!='sha256:'+hashlib.sha256(raw).hexdigest():
        raise ValueError('Release asset digest mismatch')
    return name, raw, ['--tag', tag]


def deploy(host, request, api):
    sha = request['sha']
    payload = request['payload']
    trial = payload.get('trial') is True
    tag = payload.get('tag', '')
    run_id = str(payload['run_id'])
    if not SHA.fullmatch(sha) or not run_id.isdecimal():
        raise ValueError('Invalid deployment request')
    if (trial and tag) or (not trial and not TAG.fullmatch(tag)):
        raise ValueError('Invalid deployment version')
    run = api('actions/runs/' + run_id)
    if (run['path'] != '.github/workflows/deploy.yml' or run['head_branch'] != host['branch']
            or run['event'] not in ('workflow_dispatch','workflow_run') or run['status'] != 'in_progress'
            or run['head_repository']['full_name'].lower() != host['repository'].lower()):
        raise ValueError('Deployment workflow identity mismatch')
    if trial:
        if (run['event'] != 'workflow_dispatch' or run['head_sha'] != sha
                or run.get('display_title') != 'Trial ' + sha):
            raise ValueError('A trial requires a manual dispatch at the requested commit')
        name, raw, source = trial_wheel(host, sha, api)
    else:
        name, raw, source = release_wheel(host, tag, sha, api)
        if run['event'] == 'workflow_dispatch' and run.get('display_title') == 'Release ' + tag:
            source.append('--allow-trial-rollback')
    with tempfile.TemporaryDirectory(prefix='install-',dir=STATE) as tmp:
        wheel=Path(tmp)/name
        wheel.write_bytes(raw)
        subprocess.run([sys.executable,TOOLS/'upgrade.py','--config',CONTROL/'config.json',
                        '--wheel',wheel,*source,'--sha',sha],check=True)


def main():
    host=json.loads((CONTROL/'actions.json').read_text())
    def api(path):
        return json.loads(fetch('https://api.github.com/repos/'+host['repository']+'/'+path,4*1024*1024))
    receipt=STATE/'status.json'
    prior=json.loads(receipt.read_text()) if receipt.exists() else {}
    # Recheck interrupted operations by their original ID. Installing the same release is idempotent.
    previous=prior.get('deployment',0) - int(prior.get('state')=='running')
    item=select(api('deployments?environment=telegram-app&task=telegram-upgrade&per_page=10'),
                previous,max(host['enabled_at'],datetime.now(timezone.utc).timestamp()-25*60))
    if item is None:
        return
    write_status(item['id'],item['sha'],'running')
    try:
        deploy(host,item,api)
    except Exception:
        write_status(item['id'],item['sha'],'failure')
        raise
    write_status(item['id'],item['sha'],'success')


if __name__=='__main__':
    main()
