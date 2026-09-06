# HJ GROUPS Telegram Media Collector

## What it does

- Telethon personal account reads private chats, groups and channels that the account can access.
- aiogram bot provides the control UI.
- Message-link scan.
- Bulk range scan: `/range <chat> <start_id> <end_id>`.
- Audio / video / photo / document / all selection.
- Individual file selection with inline buttons.
- Per-user destination chat.
- Custom caption with original caption, file size and `@hjgroups_1`.
- Bot-chat copies are automatically deleted after 60 minutes by default.
- Telegram FloodWait is respected instead of bypassed.
- Large-file delivery uses Telethon MTProto rather than normal Bot API upload.

## Required environment variables

See `.env.telegram-media.example`.

Required:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_PHONE_NUMBER`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`

Optional:

- `TELEGRAM_DESTINATION`
- `TELEGRAM_BRANDING`
- `TELEGRAM_BOT_DELETE_SECONDS`
- `TELEGRAM_MAX_BULK_MESSAGES`
- `TELEGRAM_MAX_ACTIVE_JOBS`
- `TELEGRAM_SESSION_NAME`
- `TELEGRAM_STATE_FILE`

## First login

The first Telethon login may ask for the Telegram login code and 2FA password. Complete this on a trusted machine and keep the generated `.session` file private. Do not commit it to GitHub.

After the session exists, the process can run non-interactively on a persistent host.

## Bot usage

1. `/start`
2. `/setdestination -1001234567890` or `/setdestination @destinationchannel`
3. Send a message link such as `https://t.me/example/123` or a private-channel link available to the Telethon account.
4. For bulk processing: `/range @example 25 100`
5. Choose a media type or `Select individually`.
6. Confirm.

## Large files

The normal Bot API has a much smaller upload limit, so this project does not use Bot API upload for the actual large media copy. Telethon sends the media through MTProto to the destination and to the bot conversation. Actual maximum file size remains subject to Telegram's current account/API limits and source/destination permissions.

## Important Telegram restrictions

- The personal account must have access to the source chat.
- Protected content / forwarding-restricted messages may fail.
- Flood limits are respected; the application does not attempt to evade them.
- Do not use the project for unsolicited bulk messaging or other abusive activity.
