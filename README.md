# Voroa Telegram Transfer Bot

Voroa is a clean Telegram media transfer bot built specifically for HJ GROUPS.

## Architecture

- `voroa_launcher.py` is the production launcher.
- `voroa_stable.py` contains the bot runtime.
- aiogram handles the bot UI, commands, buttons, and callbacks.
- Telethon uses a Telegram user session for source access and Telegram-to-Telegram media transfer.
- Core transfer does not depend on MongoDB, Playbook, or a separate downloader service.
- Transfers pass Telegram media to Telethon directly; Voroa does not buffer the entire file in RAM.

## Main workflow

1. Start the bot.
2. Set or confirm the destination.
3. Scan one Telegram message link or a message-ID range.
4. Review the files and total size.
5. Press `Confirm Transfer`.
6. Voroa creates a unique job and starts exactly one background transfer task.
7. Source media are refreshed by message ID before sending.
8. A `Cancel Transfer` button is available while the job is active.

## Lifecycle safety

- Confirmation and cancellation callbacks are bound to a unique job ID, so stale Telegram buttons cannot operate on a newer job.
- Active job cleanup is identity-safe and cannot remove a newer transfer's state.
- Login, logout, and Telegram session replacement are blocked while a transfer is active because the runtime uses one shared Telethon client.
- Progress and heartbeat status tasks belong to the transfer lifecycle and are cleaned up before transfer state is removed.
- Flood-wait sleeps are cancellation-aware and do not resume a retry after cancellation.
- A single failed file is reported as a failed transfer rather than a normal success.
- Saved destinations are snapshotted into the job and are authoritative for that job.
- Access control is fail-closed: configure `TELEGRAM_ALLOWED_USER_IDS` and/or `VOROA_OWNER_USER_ID`.

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
- access-control IDs
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

The CI workflow compiles and imports only the current Voroa production files; the old VJ URL uploader runtime is intentionally not part of the production path.

## Telegram access

The logged-in Telegram user account must have legitimate access to both the source and destination chats. Voroa does not attempt to bypass protected-content or other Telegram restrictions.
