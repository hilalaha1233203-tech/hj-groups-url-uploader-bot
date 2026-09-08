# Voroa Telegram Transfer Bot

Voroa is a clean Telegram media transfer bot built specifically for HJ GROUPS.

## Architecture

- `voroa_launcher.py` is the only production launcher.
- `voroa_stable.py` contains the bot runtime.
- aiogram handles the bot UI, commands, buttons, and callbacks.
- Telethon uses a Telegram user session for source access and Telegram-to-Telegram media transfer.
- Core transfer does not depend on MongoDB, Playbook, or a separate downloader service.

## Main workflow

1. Start the bot.
2. Set or confirm the destination.
3. Scan one Telegram message link or a message-ID range.
4. Review the files and total size.
5. Press `Confirm Transfer`.
6. Voroa starts the transfer as a background task and shows a live status message.
7. A `Cancel Transfer` button is available while the job is active.

## Supported controls

- Scan Link
- Bulk Range
- Destination
- Current Job
- Login
- Session status
- Logout
- Cancel
- Help

## Configuration

Copy `.env.example` into your deployment configuration and provide:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_SESSION_STRING` or a persistent session file
- optional access-control IDs
- optional default destination

Never commit a bot token, API hash, Telegram session, OTP, or 2FA password.

## Run locally

```bash
python -m pip install -r requirements.txt
python voroa_launcher.py
```

## Docker

The included `Dockerfile` starts `voroa_launcher.py` directly.

## Tests

```bash
python voroa_stable_test.py
python smoke_test.py
```

The CI workflow compiles and imports only the current Voroa production files. The previous VJ URL uploader runtime, plugins, database, Playbook client, and legacy entrypoints are intentionally not part of the repository anymore.

## Telegram access

The logged-in Telegram user account must have legitimate access to both the source and destination chats. Voroa does not attempt to bypass protected-content or other Telegram restrictions.
