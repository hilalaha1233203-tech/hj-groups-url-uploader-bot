# HJ GROUPS Telegram Media Collector

This component uses an aiogram bot for the user interface and a Telethon user account for Telegram media access. The user account must have legitimate access to every source and destination chat.

## Workflow

1. Configure the Telegram Bot token and Telethon API credentials as environment variables.
2. Create a Telethon StringSession locally with `generate_telethon_session.py`.
3. Put the StringSession in the hosting provider's secret environment variable `TELEGRAM_SESSION_STRING`.
4. Add one or more allowed Telegram user IDs to `TELEGRAM_ALLOWED_USER_IDS`.
5. Set a default destination with `TELEGRAM_DESTINATION`, or configure per-user destinations with `TELEGRAM_USER_DESTINATIONS`.
6. Start `telegram_chat_exporter.py`.

## Bot commands

- `/start` — show help and current destination.
- `/setdestination @channel_or_chat_id` — set the current user's destination.
- Send a `t.me` message link — scan one media message.
- `/range @channel_or_message_link 25 100` — scan a message-ID range.
- `/select 25,31,44` — select individual files from the active range scan.
- `/cancel` — cancel the current selection.

After a scan, the bot offers Audio, Video, Photos, Documents, All files, individual selection, and Cancel. The user must confirm before processing.

## Captions

Each copied media item keeps its original caption when present and appends:

`📦 File Size: ...`

`@hjgroups_1`

## Large files

The collector uses Telethon/MTProto rather than the normal Bot API upload path. This is important for large Telegram media. Telegram's exact MTProto upload allowance depends on the account and current Telegram configuration; do not assume every account can send a 4 GB file.

The implementation does not intentionally bypass FloodWait or other Telegram limits. Failed media is logged and the remaining job continues.

## 60-minute bot-chat cleanup

Copies sent into the bot user's private chat through the Telethon account are scheduled for deletion after `TELEGRAM_BOT_DELETE_SECONDS` (default 3600). This is a best-effort timer and is not durable across a process restart.

## Security

Never commit any of these to GitHub:

- Bot token
- API hash
- Telethon session file
- Telethon StringSession
- OTP or 2FA password

Use the hosting provider's secret environment-variable store. The `.gitignore` already excludes `.env` and Telethon session files.

## Important Telegram restrictions

Protected content or chats that prohibit forwarding/saving may reject the operation. The collector does not attempt to bypass those restrictions.

## Render

`render.yaml` defines a Python background worker. Add the secret environment variables in Render, then deploy from this repository. For hosted operation, `TELEGRAM_SESSION_STRING` is preferred because it avoids storing a `.session` file on the worker filesystem.
