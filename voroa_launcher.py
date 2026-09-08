"""Deployment-safe launcher for Voroa stable runtime."""
from __future__ import annotations

import asyncio
import os

from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from telethon import TelegramClient
from telethon.sessions import StringSession

import voroa_stable as v

MAX_RESTARTS = 20
RESTART_DELAY = 5
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))


async def health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = await asyncio.wait_for(reader.read(2048), timeout=5)
        parts = (request.split(b"\r\n", 1)[0] if request else b"").split(b" ", 2)
        path = parts[1].decode("ascii", "ignore") if len(parts) >= 2 else "/"
        if path == "/healthz":
            body = b'{"status":"ok","service":"voroa"}\n'
            content_type = b"application/json; charset=utf-8"
        else:
            body = b"Voroa is running.\n"
            content_type = b"text/plain; charset=utf-8"

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: " + content_type + b"\r\n"
            b"Cache-Control: no-store\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()
    except Exception as exc:
        print(f"[Voroa] Health request error: {type(exc).__name__}: {exc}", flush=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server() -> asyncio.AbstractServer:
    server = await asyncio.start_server(health_handler, HOST, PORT)
    print(f"[Voroa] Health server listening on {HOST}:{PORT}", flush=True)
    return server


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
    health_server = await start_health_server()
    try:
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
    finally:
        health_server.close()
        await health_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(run())
