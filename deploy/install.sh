#!/bin/bash
# Bootstrap a fresh systemd installation using an official release wheel.
set -euo pipefail
umask 022
[[ $(id -u) == 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ $# == 2 && $2 =~ ^[a-f0-9]{40}$ ]] || { echo "Usage: install.sh /path/to/release.whl FULL_RELEASE_COMMIT" >&2; exit 2; }
wheel=$(realpath -e -- "$1")
name=$(basename -- "$wheel")
[[ $name =~ ^telegram_social_downloader-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl$ ]] || { echo "Expected a Telegram Social Downloader release wheel." >&2; exit 2; }
version=${BASH_REMATCH[1]}
base=/opt/telegram-social-downloader
release=$base/releases/telegram-$version
unit_dir=/etc/systemd/system
source_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
data=/var/lib/telegram-social-downloader
secret_dir=/var/lib/telegram-social-credentials
env_dir=/etc/telegram-social-downloader
deploy_dir=/etc/telegram-social-deploy
for path in "$base/current" "$release" "$env_dir/.env" "$deploy_dir/config.json" "$unit_dir"/telegram-social-{worker,broker,push,web}.service; do
    if [[ -e $path || -L $path ]]; then
        echo "A service installation already exists at $path." >&2
        exit 1
    fi
done
for utility in systemctl python3 runuser ffmpeg ffprobe; do command -v "$utility" >/dev/null; done
getent group www-data >/dev/null || { echo "Create the reverse proxy group www-data first." >&2; exit 1; }
for name_group in telegram-social telegram-push telegram-credentials telegram-build; do
    getent group "$name_group" >/dev/null || groupadd --system "$name_group"
done
for account in telegram-social telegram-credentials telegram-build; do
    getent passwd "$account" >/dev/null || useradd --system --gid "$account" --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$account"
done
install -d -m 0755 "$base" "$base/releases"
install -d -o telegram-build -g telegram-build -m 0700 "$release"
# Build in the final location so venv script shebangs are valid after activation.
# The parent and current symlink remain root-owned.
install -m 0644 "$wheel" "$release/$name"
runuser -u telegram-build -- python3 -m venv "$release/.venv"
runuser -u telegram-build -- "$release/.venv/bin/python" -Im pip --disable-pip-version-check install --only-binary=:all: --no-cache-dir "$release/$name"
runuser -u telegram-build -- "$release/.venv/bin/python" -Im pip check
runuser -u telegram-build -- "$release/.venv/bin/python" -I -c \
 'import importlib.metadata as m,sys; assert m.version("telegram-social-downloader")==sys.argv[1]' "$version"
chown -hR root:root "$release"
chmod -R go-w "$release"
chmod 0755 "$release"
cat > "$release/DEPLOYMENT-MANIFEST.json" <<EOF
{"version":"$version","commit":"$2"}
EOF
install -d -o telegram-social -g telegram-social -m 0700 "$data"
install -d -o telegram-credentials -g telegram-credentials -m 0700 "$secret_dir"
install -d -m 0700 "$env_dir" "$deploy_dir" /var/backups/telegram-social-downloader
gid=$(getent group telegram-social | cut -d: -f3)
install -m 0600 /dev/null "$env_dir/.env"
cat > "$env_dir/.env" <<EOF
MEDIA_BOT_DATA=$data
MEDIA_BOT_SECRET_DIR=$secret_dir
MEDIA_BOT_SECRET_SOCKET=/run/telegram-social-broker/broker.sock
MEDIA_BOT_SECRET_SOCKET_GID=$gid
MEDIA_BOT_WEB_SOCKET=/run/telegram-social-web/web.sock
MEDIA_BOT_PUSH_SOCKET=/run/telegram-social-push/transport.sock
MEDIA_BOT_ORIGIN=https://telegram.example.org
MEDIA_BOT_BASE=/
MEDIA_BOT_ADMIN_USER=sena
MEDIA_BOT_ADMIN_PASSWORD_HASH=
MEDIA_BOT_OWNER_ID=
EOF
install -m 0600 /dev/null "$deploy_dir/config.json"
cat > "$deploy_dir/config.json" <<EOF
{
  "current": "$base/current",
  "releases": "$base/releases",
  "data": "$data",
  "backups": "/var/backups/telegram-social-downloader",
  "worker": "telegram-social-worker.service",
  "broker": "telegram-social-broker.service",
  "web": "telegram-social-web.service",
  "broker_socket": "/run/telegram-social-broker/broker.sock",
  "web_socket": "/run/telegram-social-web/web.sock",
  "health_path": "/healthz"
}
EOF
for unit in "$source_dir"/systemd/*.service; do
    install -m 0644 "$unit" "$unit_dir/"
done
ln -s "$release" "$base/current"
systemctl daemon-reload
echo "Installed $version. Set the origin, administrator verifier and owner ID in $env_dir/.env before enabling the units."
