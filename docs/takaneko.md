# Takaneko Fanclub subscriptions

Each installation can poll the Fanclub using its operator's account and push new articles, original text and media to approved Telegram subscribers. Subscribers use their existing Bot accounts. A dedicated worker maintains the Fanclub session through its credential broker and records deliveries in its own database. The application wheel includes all three Takaneko components.

## Enable the provider

1. Complete the [standalone Linux installation](deployment.md), configure the Bot token, and start the Bot worker, broker and web console. Use the checkout for the release installed on the host.
2. Choose the mounted filesystem for Fanclub media and temporary downloads. Pass the exact root-owned mount point of a filesystem separate from the system root. A bind mount of a directory on that separate filesystem also works. The mount needs a stable filesystem UUID, traversable parent directories and at least 5 GiB free. Pick the HTTPS download prefix on your reverse proxy.
3. Run the provider installer with those two values. The following example uses a volume mounted at `/srv/media`.

   ```sh
   sudo python3 deploy/install_takaneko.py \
     --storage /srv/media \
     --link-base https://telegram.example.org/takaneko-download/
   ```

   The installer creates a dedicated service account, enables the local push transport and starts the provider's worker, broker and link server. It adds the provider to the web console and the GitHub Actions deployment configuration. If you already run another push provider, drain its active sends and stop the transport before first enabling this integration. The installer starts it again.
4. Route the exact path from `--link-base` to the provider link socket. Preserve the full path. For the example above, add this location to your HTTPS virtual host and reload nginx after checking its configuration.

   ```nginx
   location ^~ /takaneko-download/ {
       proxy_pass http://unix:/run/telegram-social-takaneko-links/web.sock;
       proxy_buffering off;
       access_log off;
   }
   ```

## Connect a Fanclub account

Sign in to the Bot web console. Open **Subscriptions → takaneko**, enter the Fanclub session and choose **Save Fanclub session**. You can supply a browser-exported JSON array containing the cookies valid for the Fanclub API, or a JSON object with an `accessToken`, `refreshToken` or `cookies` array. The credential broker stores the operator's session and handles refresh-token rotation. The web console does not display stored values.

In **Users & groups**, approve each Telegram user who should receive updates. In **Subscriptions**, choose **Subscribe** next to that user. Open **Features** and choose **Enable** beside **Takaneko Fanclub** to start polling and delivery. A new installation starts paused. You can leave it paused without adding a session. The first complete poll establishes the starting inventory. Subsequent polls normally run every five minutes and enqueue newly discovered posts for current subscribers. Delivery rechecks both Bot access and the subscription.

Choose **Pause** in **Features** to suspend polling and delivery while keeping the Fanclub session, subscriptions and pending deliveries. Choose **Enable** to resume. To remove one recipient, choose **Unsubscribe** in **Subscriptions**. This cancels eligible outstanding deliveries and revokes the user's download links.

The provider sends original text by default. To enable translation, import the full provider credential object using a private JSON file. An example schema is shown below. Use your own session and key. An import replaces the provider credentials, including the active Fanclub session and translation-key list.

```json
{
  "session": {"refreshToken": "<your Fanclub refresh token>"},
  "openai_keys": [{"id": "primary", "key": "<your OpenAI API key>", "enabled": true}],
  "primary_key": "primary"
}
```

```sh
sudo env TAKANEKO_SECRET_SOCKET=/run/telegram-social-takaneko-broker/broker.sock \
  /opt/telegram-social-downloader/current/.venv/bin/python -IB -m takaneko_telegram credentials \
  < /path/to/private/credentials.json
```

## Services and operations

| Component | Purpose |
| --- | --- |
| `telegram-social-takaneko.service` | Polls new articles, prepares media, manages subscriptions and delivery checkpoints |
| `telegram-social-takaneko-broker.service` | Stores credentials and serializes session updates |
| `telegram-social-takaneko-links.service` | Serves time-limited downloads after validating the capability and current access |
| `telegram-social-push.service` | Checks the requesting provider and recipient and sends through the Bot |

To inspect delivery problems, run the provider CLI under its service account with its own environment:

```sh
sudo systemd-run --quiet --wait --pipe --collect \
  -p User=telegram-takaneko -p Group=telegram-push \
  -p SupplementaryGroups=telegram-takaneko \
  -p EnvironmentFile=/etc/telegram-social-takaneko/.env \
  /opt/telegram-social-downloader/current/.venv/bin/python -IB -m takaneko_telegram pending
```

Substitute `retry ID` for `pending` to retry a definite rejection. For an uncertain send, first check the actual Telegram result and use `resolve ID sent` or `resolve ID retry`. The provider checkpoints an attempted send before contacting Telegram so a restart does not silently duplicate it.

The provider's settings and SQLite delivery state live in `/var/lib/telegram-social-takaneko`; credentials live in the root-only `/var/lib/telegram-takaneko-credentials`. Its unit environment is `/etc/telegram-social-takaneko/.env`. The selected volume contains `telegram-social-takaneko/media` and `telegram-social-takaneko/tmp`. Include these locations in your host backup. During GitHub Actions upgrades the controller drains active provider work, checkpoints the provider and Bot databases, and switches all components to the same release. Pending work resumes from the service-owned database.

When upgrading an existing v1.1.0 installation, refresh the host deployment controller from the verified v1.1.1 checkout. Use the connection command in [Connect GitHub Actions](deployment.md#connect-github-actions) with your existing repository, branch and status URL. Do this before requesting the deployment. The controller checks both the installed v1.1.0 services and the new version after activation. For later releases that change the host controller, repeat this step with that release's checkout.
