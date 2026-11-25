# Linux installation and GitHub Actions deployments

The bot, web console and credential broker run as systemd services. The optional push service accepts submissions from registered providers. GitHub hosts the source, tests and release distributions. Each installation owns its environment, credentials, media and SQLite state.

## Install a new instance

Use a Linux host with systemd, Python 3.11 or newer with venv support, FFmpeg, ffprobe, a TLS reverse proxy, and Deno for YouTube extraction. The bootstrap script uses the `www-data` group for access to the web socket. Create that group on distributions that use a different proxy identity and add the proxy worker to it.

On a trusted workstation, select a published release and download its source and wheel. The following commands use the GitHub CLI and a clean checkout. They select the latest published release.

```sh
repo=Senachi124/TelegramSocialDownloader
tag=$(gh api "repos/$repo/releases/latest" --jq .tag_name)
git clone --depth 1 --branch "$tag" "https://github.com/$repo.git" telegram-app
cd telegram-app
gh release download "$tag" --repo "$repo" --pattern '*.whl' --dir packages
wheel=$(find packages -name '*.whl' -type f -print -quit)
expected=$(gh api "repos/$repo/releases/tags/$tag" --jq '.assets[] | select(.name | endswith(".whl")) | .digest')
test "$expected" = "sha256:$(sha256sum "$wheel" | cut -d' ' -f1)"
```

On the host, from the verified checkout with its `packages` directory, run the installer once. It creates dedicated service accounts, installs the package in an isolated versioned directory, creates the host configuration and registers the systemd units. The installer uses binary Python packages under an unprivileged build account. Select a host architecture and Python version with wheels for the pinned dependencies.

```sh
sudo ./deploy/install.sh packages/*.whl "$(git rev-parse HEAD)"
/opt/telegram-social-downloader/current/.venv/bin/python -m media_bot.admin_auth
sudoedit /etc/telegram-social-downloader/.env
```

Copy the password verifier into `MEDIA_BOT_ADMIN_PASSWORD_HASH`. Set `MEDIA_BOT_OWNER_ID` to the numeric Telegram owner ID and `MEDIA_BOT_ORIGIN` to the external HTTPS origin. Set `MEDIA_BOT_BASE` for a path prefix such as `/telegram/` and update `health_path` in `/etc/telegram-social-deploy/config.json` to match, for example `/telegram/healthz`. Put the data directory on the intended filesystem before starting the units. Set `MEDIA_BOT_STORAGE_UUID` if the service needs a specific mount. The [configuration example](../.env.example) lists optional Telegram API settings.

Configure your TLS reverse proxy to forward the service prefix to the web socket. This nginx example serves the bot at the root of a dedicated HTTPS virtual host. The TLS virtual host and certificate are supplied by the operator.

```nginx
location / {
    proxy_pass http://unix:/run/telegram-social-web/web.sock;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Authorization "";
    client_max_body_size 300000;
    client_body_buffer_size 320k;
    proxy_buffering off;
    access_log off;
}
```

Start the broker and web console. Sign in through HTTPS and configure the BotFather token and platform accounts. Then start the worker and check the health endpoint.

```sh
sudo systemctl enable --now telegram-social-broker.service telegram-social-web.service
sudo systemctl enable --now telegram-social-worker.service
curl --fail --silent --unix-socket /run/telegram-social-web/web.sock http://localhost/healthz
```

Run the socket check as a user in the web socket group and adjust its URL to the configured base path.

## Connect GitHub Actions

Use the repository that owns the release workflow. An installation following a fork should use that fork's name. From the release checkout run the connection helper on the host. Replace the example repository and HTTPS URL with your own.

```sh
sudo python3 deploy/enable_actions.py \
  --repo OWNER/REPO \
  --health-url https://telegram.example.org/deployment-status \
  --config /etc/telegram-social-deploy/config.json
```

If your v1.0.1 or v1.0.2 controller was installed with `--preview-user`, run the connection helper above from v1.0.3 or newer before updating the application. This replaces the old controller and removes its release announcement helper.

On GitHub, configure the repository that will deploy your installation.

1. In a fork, open **Actions** and enable workflows. Put the release and deployment workflows on the default branch before creating the first version tag.
2. Open **Settings → Environments → production → Environment secrets**. Set the secret `DEPLOY_STATUS_URL` to the same HTTPS URL as the connection helper.
3. Open **Settings → Secrets and variables → Actions → Variables**. Set the repository variable `ENABLE_PRODUCTION_DEPLOY` to `true`.

The host polls GitHub for deployment requests from the release workflow and reports the installed release through that URL. Bot tokens, API keys and platform cookies stay on the host. The installation needs outbound HTTPS to GitHub and its release asset servers. GitHub-hosted runners need HTTPS access to the status URL.

For automatic updates, push a new version tag matching the package version. A successful tag release starts production deployment. To deploy an existing published release manually, open **Actions**, select the production deployment workflow, choose **Run workflow** on the default branch, enter the release tag and run it. Check the deployment job and the status endpoint for the deployed revision. For a service mounted under a prefix, include that prefix in `DEPLOY_STATUS_URL`.

## Optional subscription delivery

For Takaneko Fanclub updates, follow the [bundled provider installation](takaneko.md). The installer registers the provider and transport together.

Configure `MEDIA_BOT_PUSH_PROVIDERS` in the environment. Give each provider account access to the transport socket through the `telegram-push` group, restart its process so it picks up the new group, and configure its control socket for access by the bot. See the [provider protocol](push.md).

Add `"push": "telegram-social-push.service"` to the deployment configuration to include the transport in upgrades. Start the unit and restart the web console so it loads the provider map.

```sh
sudo systemctl enable --now telegram-social-push.service
sudo systemctl restart telegram-social-web.service
```

## Service paths

| Purpose | Fresh installation |
| --- | --- |
| Active release | `/opt/telegram-social-downloader/current` |
| Versioned environments | `/opt/telegram-social-downloader/releases/` |
| Service environment | `/etc/telegram-social-downloader/.env` |
| Upgrade configuration | `/etc/telegram-social-deploy/config.json` |
| Bot control state, downloads and temporary files | `/var/lib/telegram-social-downloader` |
| Broker-managed credentials | `/var/lib/telegram-social-credentials` |
| Privileged rollback records | `/var/backups/telegram-social-downloader` |

For an existing installation use its established paths, units and service identities in the upgrade configuration. Run the first-install script only for a fresh instance.
