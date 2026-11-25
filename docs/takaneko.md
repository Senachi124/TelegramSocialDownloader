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

Sign in to the bot web console and open **Takaneko subscriptions**. If you already have login JSON, paste it into **Fanclub login (JSON)** and choose **Save Fanclub session**.

To prepare the JSON from your signed-in Fanclub tab:

1. Choose **Copy script** in the DevTools helper. Open the Console in your signed-in [official Fanclub tab](https://takanekofc.com/) and run the script. It opens an export dialog in that tab.
2. For a refresh token, open **Application → Local Storage** in DevTools, select the Fanclub origin and copy the value of `refreshToken`. For cookies, open **Network**, select a request to `https://api.takanekofc.com/auth/refresh` and copy the login cookie as `name=value`. Separate multiple pairs with a semicolon.
3. In the dialog choose **Refresh token** or **API cookies**, paste the selected value and choose **Copy login JSON**. The helper formats only the value you paste. Return to **Takaneko subscriptions**, paste the JSON and save it.
4. Choose **Test connection** to check the saved login immediately. The provider makes an authenticated request for the notification count. This works with subscriptions paused and does not queue a delivery.

| What you have | What to paste | How it works |
| --- | --- | --- |
| Refresh token | `{"refreshToken":"<your refresh token>"}` | The provider requests an access token and saves a replacement refresh token when the Fanclub issues one. This is the preferred option for ongoing subscriptions. |
| Browser cookies | A browser-exported JSON cookie array, or `{"cookies":[…]}` with that array | Cookies alone work if the Fanclub accepts them at its API refresh endpoint. An export containing only preferences or analytics cookies cannot renew a login. |
| Access token (JWT) only | `{"accessToken":"<your access token>"}` | The provider can use this token until it expires. You then need to import fresh login data. |

If your refresh token is stored in browser local storage, a cookie export will omit it. Saving replaces the stored Fanclub login. The test reports whether the Fanclub accepts that login at the time of the check.

In **Users & groups**, approve each Telegram user who should receive updates. In **Takaneko subscriptions**, choose **Subscribe** next to that user. Open **Features** and choose **Enable** beside **Takaneko Fanclub** to start polling and delivery. A new installation starts paused. You can leave it paused without adding a session. The first complete poll establishes the starting inventory. Subsequent polls normally run every five minutes and enqueue newly discovered posts for current subscribers. Delivery rechecks both Bot access and the subscription.

Choose **Pause** in **Features** to suspend polling and delivery while keeping the Fanclub session, subscriptions and pending deliveries. Choose **Enable** to resume. To remove one recipient, choose **Unsubscribe** in **Takaneko subscriptions**. This cancels eligible outstanding deliveries and revokes the user's download links.

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

### Read delivery errors

Open **Error log** in the admin console to see the source and error message. A Fanclub login or polling error occurs while the provider is reading Fanclub. A missing Telegram receipt occurs later, during delivery to a subscriber. Telegram may already have accepted that send. The worker holds the affected delivery and continues with other eligible subscribers.

Use **Takaneko subscriptions** to update the Fanclub session or manage recipients.

### Service commands

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

Substitute `retry ID` for `pending` to retry a definite rejection. For a missing receipt, `pending` lists the recipient, part, position and attempt. After checking that part with the recipient, use `resolve ID sent --position POSITION --attempt TOKEN` or `resolve ID retry --position POSITION --attempt TOKEN`. Copy the zero-based position and attempt from the same row. If another operator has already handled it, run `pending` again and check the current part before making a new decision.

The provider's settings and SQLite delivery state live in `/var/lib/telegram-social-takaneko`; credentials live in the root-only `/var/lib/telegram-takaneko-credentials`. Its unit environment is `/etc/telegram-social-takaneko/.env`. The selected volume contains `telegram-social-takaneko/media` and `telegram-social-takaneko/tmp`. Include these locations in your host backup. During GitHub Actions upgrades the controller drains active provider work, checkpoints the provider and Bot databases, and switches all components to the same release. Pending work resumes from the service-owned database.

When upgrading an existing v1.1.0 installation, refresh the host deployment controller from the verified v1.1.1 checkout. Use the connection command in [Connect GitHub Actions](deployment.md#connect-github-actions) with your existing repository, branch and status URL. Do this before requesting the deployment. The controller checks both the installed v1.1.0 services and the new version after activation. For later releases that change the host controller, repeat this step with that release's checkout.
