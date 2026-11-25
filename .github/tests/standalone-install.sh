#!/usr/bin/env bash
# Exercise the release wheel and public installers on a disposable Ubuntu Actions VM.
set -euo pipefail
: "${RUNNER_TEMP:?}"
sudo ./deploy/install.sh dist/*.whl "$GITHUB_SHA"
sudo python3 - <<'PY'
import hashlib
from pathlib import Path
import secrets

env = Path('/etc/telegram-social-downloader/.env')
salt = secrets.token_bytes(16)
verifier = hashlib.scrypt(secrets.token_bytes(32), salt=salt, n=16384, r=8, p=1, dklen=64)
with env.open('a') as stream:
    stream.write(f'MEDIA_BOT_ADMIN_PASSWORD_HASH=scrypt${salt.hex()}${verifier.hex()}\n')
    stream.write('MEDIA_BOT_OWNER_ID=123456\n')
PY
sudo systemctl start telegram-social-broker.service telegram-social-web.service telegram-social-worker.service
image="$RUNNER_TEMP/fanclub-media.ext4"
truncate -s 8G "$image"
mkfs.ext4 -q -F "$image"
sudo install -d -m 0755 /srv/fanclub-storage
sudo mount -o loop "$image" /srv/fanclub-storage
sudo udevadm settle
sudo python3 deploy/install_takaneko.py --storage /srv/fanclub-storage --link-base https://telegram.example.org/takaneko-download/
python=/opt/telegram-social-downloader/current/.venv/bin/python
sudo -u telegram-social -g telegram-push "$python" -I - <<'PY'
import pwd
import time
from media_bot.push_socket import exchange

path = '/run/telegram-social-takaneko/control.sock'
# Verify the public service responds before any Fanclub session is supplied.
from pathlib import Path
for attempt in range(30):
    try:
        uid = Path(path).stat().st_uid
        assert uid not in (0, pwd.getpwnam('telegram-social').pw_uid)
        reply = exchange(path, uid, {'op': 'list'})
    except (FileNotFoundError, ConnectionRefusedError):
        time.sleep(1)
    else:
        assert reply['ok'] and reply['users'] == [] and 'set_session' in reply['capabilities']
        break
else:
    raise RuntimeError('Takaneko control socket did not start')
PY
sudo curl --fail --silent --unix-socket /run/telegram-social-web/web.sock http://localhost/healthz
sudo systemctl is-active telegram-social-{push,takaneko-broker,takaneko-links,takaneko}.service
