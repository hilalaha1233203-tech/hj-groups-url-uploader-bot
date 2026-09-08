"""Deployment-safe launcher for Voroa stable runtime."""
from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from telethon import TelegramClient
from telethon.sessions import StringSession

import voroa_stable as v

MAX_RESTARTS = 20
RESTART_DELAY = 5


async def prepare() -> None:
    session = v.load_session_string()
    if session and session != v.SESSION_STRING:
        old = v.user_client
        try:
            if old.is_connected():
                await old.disconnect()
        except Exception:
            pass
        v.user_client = TelegramClient(StringSession(session), v.API_ID, v.API_HASH)

    me = await v.bot.get_me()
    print(f"[Voroa] Bot API OK: @{getattr(me, 'username', 'unknown')}", flush=True)
    webhook = await v.bot.get_webhook_info()
    if getattr(webhook, "url", ""):
        print("[Voroa] Removing active webhook before polling.", flush=True)
        await v.bot.delete_webhook(drop_pending_updates=False)


async def run() -> None:
    await prepare()
    await v.on_startup()
    restarts = 0
    try:
        while True:
            try:
                print("[Voroa] Starting long polling.", flush=True)
                await v.dp.start_polling(
                    v.bot,
                    handle_signals=True,
                    polling_timeout=20,
                    tasks_concurrency_limit=100,
                    close_bot_session=False,
                )
                print("[Voroa] Polling stopped cleanly.", flush=True)
                break
            except TelegramConflictError as exc:
                restarts += 1
                print(
                    f"[Voroa] POLLING CONFLICT #{restarts}: {exc}. "
                    "Only one process may poll this bot token.",
                    flush=True,
                )
                if restarts >= MAX_RESTARTS:
                    raise RuntimeError(
                        "Telegram polling conflict persisted. Stop the other process using this bot token."
                    ) from exc
                await asyncio.sleep(RESTART_DELAY)
            except TelegramNetworkError as exc:
                restarts += 1
                print(f"[Voroa] Telegram network error #{restarts}: {exc}", flush=True)
                if restarts >= MAX_RESTARTS:
                    raise
                await asyncio.sleep(min(30, RESTART_DELAY * restarts))
            except Exception as exc:
                restarts += 1
                print(f"[Voroa] Polling crashed #{restarts}: {type(exc).__name__}: {exc}", flush=True)
                if restarts >= MAX_RESTARTS:
                    raise
                await asyncio.sleep(min(30, RESTART_DELAY * restarts))
    finally:
        await v.on_shutdown()


if __name__ == "__main__":
    asyncio.run(run())
