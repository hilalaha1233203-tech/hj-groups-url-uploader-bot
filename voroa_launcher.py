"""Production launcher for the Voroa Telegram worker."""
from __future__ import annotations

import asyncio
import os

from aiogram.exceptions import TelegramNetworkError

STARTUP_RETRY_LIMIT = max(1, int(os.getenv("VOROA_STARTUP_RETRY_LIMIT", "12") or 12))
RESTART_DELAY = max(2, int(os.getenv("VOROA_RESTART_DELAY_SECONDS", "5") or 5))
ENABLE_HEALTH_SERVER = os.getenv("VOROA_ENABLE_HEALTH_SERVER", "0").strip().lower() in {"1", "true", "yes", "on"}
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000") or 10000)

async def health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = await asyncio.wait_for(reader.read(2048), timeout=5)
        first_line = request.split(b"\r\n", 1)[0] if request else b""
        parts = first_line.split(b" ", 2)
        path = parts[1].decode("ascii", "ignore") if len(parts) >= 2 else "/"
        body = b'{"status":"ok","service":"voroa"}\n' if path == "/healthz" else b"Voroa is running.\n"
        content_type = b"application/json; charset=utf-8" if path == "/healthz" else b"text/plain; charset=utf-8"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type + b"\r\nCache-Control: no-store\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except Exception as exc:
        print(f"[Voroa] Health request error: {type(exc).__name__}: {exc}", flush=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def start_health_server() -> asyncio.AbstractServer | None:
    if not ENABLE_HEALTH_SERVER:
        print("[Voroa] Health server disabled (Background Worker mode).", flush=True)
        return None
    server = await asyncio.start_server(health_handler, HOST, PORT)
    print(f"[Voroa] Health server listening on {HOST}:{PORT}", flush=True)
    return server

async def load_runtime():
    os.environ["TELEGRAM_SESSION_STRING"] = ""
    import voroa_stable as v
    # Register optional command/UI extensions before startup registers commands.
    import voroa_commands as commands
    v.setup_commands = commands.setup_commands
    return v

async def prepare(v) -> None:
    session = v.load_session_string()
    if session:
        await v.rebuild_user_client(session)
    me = await v.bot.get_me()
    print(f"[Voroa] Bot API OK: @{getattr(me, 'username', 'unknown')}", flush=True)
    webhook = await v.bot.get_webhook_info()
    if getattr(webhook, "url", ""):
        print("[Voroa] Removing active webhook before polling.", flush=True)
        await v.bot.delete_webhook(drop_pending_updates=False)

async def startup_once(v) -> None:
    await prepare(v)
    await v.on_startup()

async def run() -> None:
    v = await load_runtime()
    health_server = await start_health_server()
    try:
        startup_attempt = 0
        while True:
            try:
                startup_attempt += 1
                print(f"[Voroa] Startup attempt #{startup_attempt}", flush=True)
                await startup_once(v)
                print("[Voroa] Startup checks passed.", flush=True)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[Voroa] Startup failed #{startup_attempt}: {type(exc).__name__}: {exc}", flush=True)
                if startup_attempt >= STARTUP_RETRY_LIMIT:
                    raise RuntimeError(f"Voroa startup failed after {STARTUP_RETRY_LIMIT} attempts. Check Telegram credentials, bot token, and deployment environment variables.") from exc
                delay = min(60, RESTART_DELAY * startup_attempt)
                print(f"[Voroa] Retrying startup in {delay}s.", flush=True)
                await asyncio.sleep(delay)
        while True:
            try:
                print("[Voroa] Starting long polling.", flush=True)
                await v.dp.start_polling(v.bot, handle_signals=True, polling_timeout=20, tasks_concurrency_limit=100, close_bot_session=False)
                print("[Voroa] Polling stopped cleanly; restarting in 3s.", flush=True)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except TelegramNetworkError as exc:
                print(f"[Voroa] Telegram network error: {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(RESTART_DELAY)
            except Exception as exc:
                print(f"[Voroa] Polling crashed: {type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(RESTART_DELAY)
    finally:
        try:
            await v.on_shutdown()
        finally:
            if health_server is not None:
                health_server.close()
                await health_server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("[Voroa] Shutdown requested.", flush=True)
