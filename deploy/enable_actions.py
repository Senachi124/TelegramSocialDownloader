"""Register this host to receive deployments from its public GitHub repository."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',required=True)
    parser.add_argument('--branch',default='main')
    parser.add_argument('--config',type=Path,default=Path('/etc/telegram-social-deploy/config.json'))
    parser.add_argument('--health-url',required=True, help='Public HTTPS deployment-status URL')
    args=parser.parse_args()
    if os.getuid()!=0 or args.config!=Path('/etc/telegram-social-deploy/config.json') or not args.config.is_file():
        parser.error('Run as root after installing the service and its deployment configuration')
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',args.repo):
        parser.error('Expected OWNER/REPOSITORY')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',args.branch):
        parser.error('Expected a simple default branch name')
    url=urlsplit(args.health_url)
    if url.scheme!='https' or not url.hostname or url.username or url.password or not url.path.endswith('/deployment-status') or url.query or url.fragment:
        parser.error('Expected a public HTTPS deployment-status URL')
    root=Path('/usr/local/lib/telegram-social-deploy')
    root.mkdir(mode=0o755,parents=True,exist_ok=True)
    for name in ('actions_receiver.py','upgrade.py'):
        shutil.copyfile(Path(__file__).with_name(name),root/name)
        (root/name).chmod(0o644)
    (root/'preview.py').unlink(missing_ok=True)
    state=Path('/var/lib/telegram-social-deploy')
    state.mkdir(mode=0o755,exist_ok=True)
    state.chmod(0o755)
    settings=args.config.with_name('actions.json')
    settings.write_text(json.dumps(dict(repository=args.repo,branch=args.branch,
        enabled_at=int(time.time()))))
    settings.chmod(0o600)
    units=Path('/etc/systemd/system')
    (units/'telegram-social-deploy.service').write_text(
        '[Unit]\nDescription=Install verified Telegram application releases\nAfter=network-online.target\n'
        '[Service]\nType=oneshot\nUMask=0022\nExecStart=/usr/bin/python3 -IB '+str(root/'actions_receiver.py')+'\nTimeoutStartSec=20min\n')
    (units/'telegram-social-deploy.timer').write_text(
        '[Unit]\nDescription=Check for Telegram application deployments\n'
        '[Timer]\nOnActiveSec=5s\nOnUnitInactiveSec=105s\nAccuracySec=3s\n'
        '[Install]\nWantedBy=timers.target\n')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','telegram-social-deploy.timer'],check=True)
    print('Set production environment secret DEPLOY_STATUS_URL to the supplied URL and repository variable ENABLE_PRODUCTION_DEPLOY to true.')

if __name__=='__main__':
    main()
