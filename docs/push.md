# Notification producers

Each producer runs under its own Unix account. It owns its subscriptions, inventory, queue and media. Install the package in the producer environment to use `media_bot.push_socket`. Give the bot and producer accounts access to a shared socket group. Each socket needs its own daemon-owned parent directory with mode 0750, group ownership or setgid inheritance for the shared group, and mode 0660 on the socket.

Configure the transport and producer control sockets in the bot's environment, then start the push component alongside the worker and web console.

```dotenv
MEDIA_BOT_PUSH_SOCKET=/run/telegram-push/transport.sock
MEDIA_BOT_PUSH_PROVIDERS='{"news":{"uid":2001,"control":"/run/news/control.sock"}}'
```

The producer implements three control operations with `push_socket.serve(path, {bot_uid}, handler)`. Its handler receives the authenticated peer UID, a request dictionary and an optional read-only file descriptor. Reject attachments on control operations.

| Request | Result |
| --- | --- |
| `{"op":"list"}` | `{"ok":true,"users":[101,102]}` |
| `{"op":"set","user_id":101,"enabled":true}` | Commit the change, then return `{"ok":true}`. |
| `{"op":"subscribed","user_id":101}` | `{"ok":true,"enabled":true}` |

Administrators manage these subscriptions from the web console. The bot checks the user's access and the producer's subscription before each send. A service outage blocks delivery.

Use `push_socket.exchange(path, bot_uid, request, stream=None, timeout=15)` for push requests. For a media upload, pass an open read-only regular file as `stream` and allow a timeout suitable for its size.

| Request | Result |
| --- | --- |
| `{"op":"limits"}` | Current `file_limit` in bytes |
| `{"op":"allowed","user_id":101}` | Current recipient eligibility |
| `{"op":"send","kind":"text","user_id":101,"text":"New article","html":false}` | Telegram message ID |
| `{"op":"send","kind":"photo","user_id":101,"name":"cover.jpg"}` | Telegram message ID; also supports video, audio, animation and document |
| `{"op":"health","status":"network"}` | Updates provider health. Failures appear in the admin Error log. |

Health values are `ok`, `account`, `storage`, `rate_limit`, `access`, `network`, `uncertain`, `format` and `paused`. The socket verifies peer credentials in both directions.

Takaneko reports errors to **Error log**. Other providers can also notify the owner when failure alerts are enabled. Older versions queued push alerts without a provider name. On upgrade, pending alerts attributed solely to Takaneko are retired. If both Takaneko and other providers reported health, the upgrade retires ambiguous missing-receipt reminders and preserves the other pending alerts.

Checkpoint an attempted delivery in the producer before the send. A successful send returns `{"ok":true,"message_id":123}`. A confirmed rejection returns an HTTP-style 4xx `code` and may include `retry_after`. A timeout, malformed reply, or other failure after dispatch leaves delivery uncertain. Resolve it before retrying.

The bundled Takaneko provider advertises optional control operations in the `capabilities` array of its list response.

| Capability | Console operation |
| --- | --- |
| `set_session` | Save a Fanclub session object or browser cookie array. The reply contains only the operation result. |
| `test_session` | Check the saved Fanclub session against the authenticated notification count endpoint and return its connection status. |
| `set_enabled` | Switch Takaneko polling and delivery on or off. |

Other producers can implement the subscription operations independently of these capabilities.
