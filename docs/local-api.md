# Local Telegram Bot API

The worker uses the official Telegram endpoint by default. To use a local server, build the pinned binary with the optional **Build pinned Local Bot API** workflow or install the upstream binary. Run it on loopback in local mode, under supervision. Follow Telegram's bot migration procedure when switching an existing bot between the official and local endpoints.

Set the Telegram application API ID and hash in the console. The broker writes them to a private `local-api.env` in the credential directory. Supply that file to the API daemon. Changes to its credentials take effect when the daemon restarts.

Point `MEDIA_BOT_API_BASE` at the loopback listener, for example `http://127.0.0.1:8081`. Set `MEDIA_BOT_LOCAL_FILE_UPLOADS=1` if the daemon shares the media paths. Place its download cache under the configured data root in `local-api-files`. Configure the bot's public HTTPS origin and base path so recipients can use expiring download links.
