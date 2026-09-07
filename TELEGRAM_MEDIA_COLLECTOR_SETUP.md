# HJ GROUPS Telegram Media Collector

## What it does

- Telethon personal account reads private chats, groups and channels that the account can access.
- aiogram bot provides the control UI with persistent buttons.
- Message-link scan, including private `t.me/c/.../...` links.
- Bulk range scan: `/range <chat> <start_id> <end_id>`.
- Audio / video / photo / document / all selection.
- Individual file selection with inline buttons.
- Per-user destination chat.
- Custom caption with original caption, file size and `@hjgroups_1`.
- Bot-chat copies are automatically deleted after 60 minutes by default.
- Telegram FloodWait is respected instead of bypassed.
- Large-file delivery uses Telethon MTProto rather than normal Bot API upload.
- Optional Playbook temporary staging is used for the download → temporary storage → Telegram delivery path.

## Required environment variables

See `.env.telegram-media.example`.

Required for the Voroa deployment:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_BOT_TOKEN`
- `VOROA_OWNER_USER_ID`
- `TELEGRAM_SESSION_DB_URI`
- `PLAYBOOK_API_TOKEN`
- `PLAYBOOK_ORG_SLUG`

`TELEGRAM_ALLOWED_USER_IDS` is optional. It can contain additional pre-authorized users; the single owner is controlled by `VOROA_OWNER_USER_ID`.

## Persistence

MongoDB is the primary persistent store for the Telethon session, temporary login state, owner/VIP access records and per-user destinations. A local mirror is also maintained at `/data/voroa_session_store.json` by default when the Voroa service provides persistent disk storage.

If MongoDB is temporarily unavailable, writes are mirrored locally. When MongoDB becomes reachable again, locally mirrored records that are missing in MongoDB are restored without overwriting existing MongoDB documents.

Do not commit a Telethon `.session` file, Telegram session string, BotFather token, MongoDB URI or Playbook token to GitHub.

## Owner and VIP access

There is exactly one owner account. Set its Telegram user ID in `VOROA_OWNER_USER_ID`.

- Owner: all normal bot functions plus `/grant`, `/revoke`, `/users`.
- VIP: normal bot functions only.
- `/grant USER_ID` gives permanent VIP access.
- `/grant USER_ID DAYS` gives time-limited VIP access.
- `/revoke USER_ID` disables access.
- `/id` shows the caller's Telegram user ID and current role.

## First login

Use the `🔐 Login` button to start QR login. On the phone that is already logged into Telegram, open `Settings → Devices → Link Desktop Device` and scan the QR code sent by the bot. If the Telegram account uses 2FA, enter the 2FA password when prompted.

The resulting Telethon session is saved to MongoDB and mirrored locally. The bot can then restart without asking for another login while that session remains authorized.

## Bot usage

1. `/start`
2. `/setdestination -1001234567890` or `/setdestination @destinationchannel`
3. Send a message link such as `https://t.me/example/123` or a private-channel link available to the Telethon account.
4. For bulk processing: `/range @example 25 100`
5. Choose a media type or `Select individually`.
6. Confirm.

The same actions are available through the persistent reply-keyboard buttons.

## Large files

The normal Bot API has a smaller upload limit than MTProto workflows. This project therefore uses Telethon for the actual media transfer. When Playbook staging is enabled, the local download is uploaded as a temporary Playbook asset, the resulting asset URL is sent to the Telegram destination, and the temporary Playbook asset is deleted after successful delivery. Actual maximum file size remains subject to Telegram's current account/API limits and source/destination permissions.

## Important Telegram restrictions

- The personal account must have access to the source chat.
- Protected content / forwarding-restricted messages may fail.
- Private `t.me/c/...` message links are resolved against the logged-in Telegram account's accessible dialogs.
- Flood limits are respected; the application does not attempt to evade them.
- Do not use the project for unsolicited bulk messaging or other abusive activity.
