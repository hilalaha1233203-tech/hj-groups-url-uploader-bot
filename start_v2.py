"""Hardened Voroa entrypoint for the HJ GROUPS Telegram bot.

This entrypoint deliberately loads the existing telegram_bot.py only after
installing runtime compatibility guards. It keeps the existing feature set
while preventing the ReplyKeyboardMarkup/EditMessageText crash and improving
startup diagnostics, webhook/polling safety, global error handling, and
progress/session fallbacks.
"""
from __future__ import annotations

import asyncio
import os
import runpy
import time

from aiogram.types import ErrorEvent, Message, ReplyKeyboardMarkup
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser


# ---------------------------------------------------------------------------
# aiogram compatibility guard
# ---------------------------------------------------------------------------
# Message.edit_text accepts InlineKeyboardMarkup only. The project uses a
# persistent ReplyKeyboardMarkup as the normal chat menu, so never pass the
# reply keyboard while editing an existing message. The menu remains visible
# because it is attached to ordinary message.answer/send operations.
_original_edit_text = Message.edit_text


async def _safe_edit_text(self, text=None, **kwargs):
    if isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup):
        kwargs.pop("reply_markup", None)
    return await _original_edit_text(self, text=text, **kwargs)


Message.edit_text = _safe_edit_text


# Voroa uses the shorter environment variable names; the application uses
# explicit TELEGRAM_* names.
for target, source in {
    "TELEGRAM_API_ID": "API_ID",
    "TELEGRAM_API_HASH": "API_HASH",
    "TELEGRAM_BOT_TOKEN": "BOT_TOKEN",
}.items():
    if not os.getenv(target) and os.getenv(source):
        os.environ[target] = os.environ[source]


for name in (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "TELEGRAM_SESSION_DB_URI",
    "PLAYBOOK_API_TOKEN",
    "PLAYBOOK_ORG_SLUG",
):
    print(f"[Voroa] {name}: {'set' if os.getenv(name) else 'MISSING'}", flush=True)


# Load the established bot module after the compatibility guard is installed.
app = runpy.run_path("telegram_bot.py", run_name="hj_groups_app")
menu_fn = app["menu"]
session_store = app["session_store"]
user_client = app["user_client"]
original_resolve_message_peer = app["resolve_message_peer"]
original_saved_session = app["saved_session"]
original_make_progress_callback = app["make_progress_callback"]
original_send_destination = app["send_destination"]


# ---------------------------------------------------------------------------
# Global error boundary
# ---------------------------------------------------------------------------
# Handler exceptions must never silently break the user interaction. Errors
# are logged with a traceback and the user gets a safe retry message.
def install_global_error_handler():
    dp = app["dp"]
    bot = app["bot"]

    async def error_handler(event: ErrorEvent):
        exc = event.exception
        update = event.update
        print(
            f"[Voroa] HANDLER ERROR: {type(exc).__name__}: {exc}",
            flush=True,
        )
        message = update.message
        callback = update.callback_query
        if message is None and callback is not None:
            message = callback.message
        try:
            if callback is not None:
                await callback.answer("Something went wrong. Please retry.", show_alert=False)
        except Exception:
            pass
        try:
            if message is not None:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text="⚠️ Something went wrong while processing that action.\n\nPlease try again. The bot is still running.",
                    reply_markup=menu_fn(),
                )
        except Exception as send_exc:
            print(
                f"[Voroa] Error notification failed: {type(send_exc).__name__}: {send_exc}",
                flush=True,
            )

    dp.errors.register(error_handler)


install_global_error_handler()


# ---------------------------------------------------------------------------
# Durable progress updates
# ---------------------------------------------------------------------------
class _ProgressStatus:
    def __init__(self, inner):
        self.inner = inner
        self.progress_task = None

    async def _cancel_progress(self):
        task = self.progress_task
        if task is None or task.done() or task is asyncio.current_task():
            if task is not None and task.done():
                self.progress_task = None
            return
        self.progress_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def edit_text(self, text, **kwargs):
        await self._cancel_progress()
        return await self.inner.edit_text(text, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def patched_make_progress_callback(status, filename, stage, started_at):
    state = {"last": 0.0, "pending": None, "task": None}

    async def worker():
        try:
            while state["pending"] is not None:
                text = state["pending"]
                state["pending"] = None
                try:
                    if isinstance(status, _ProgressStatus):
                        await status.inner.edit_text(text, reply_markup=None)
                    else:
                        await status.edit_text(text, reply_markup=None)
                except Exception as exc:
                    print(f"[Voroa] Progress edit skipped: {type(exc).__name__}: {exc}", flush=True)
                if state["pending"] is not None:
                    await asyncio.sleep(0.8)
        except asyncio.CancelledError:
            raise
        finally:
            if state["task"] is asyncio.current_task():
                state["task"] = None
                if isinstance(status, _ProgressStatus) and status.progress_task is asyncio.current_task():
                    status.progress_task = None

    def callback(current, total):
        now = time.monotonic()
        if total and current >= total:
            return
        if total and now - state["last"] < 0.8:
            return
        state["last"] = now
        if total:
            percent = max(0.0, min(100.0, (current / total) * 100.0))
            elapsed = max(0.1, now - started_at)
            rate = current / elapsed if current > 0 else 0.0
            eta = ((total - current) / rate) if rate > 0 else None
            eta_text = "calculating…" if eta is None else (
                f"{int(round(eta))}s left" if eta < 60 else f"{int(eta // 60)}m {int(eta % 60)}s left"
            )
            filled = int(percent // 10)
            text = (
                f"📄 {filename}\n\n{stage}\n"
                f"[{int(percent):3d}%] {'█' * filled}{'░' * (10 - filled)}\n"
                f"⏳ {eta_text}"
            )
        else:
            text = f"📄 {filename}\n\n{stage}\n⏳ calculating…"
        state["pending"] = text
        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.create_task(worker())
            if isinstance(status, _ProgressStatus):
                status.progress_task = state["task"]

    return callback


async def patched_send_destination(msg, dest, status=None, index=1, total_files=1):
    if status is None:
        return await original_send_destination(msg, dest, status=None, index=index, total_files=total_files)
    wrapped = status if isinstance(status, _ProgressStatus) else _ProgressStatus(status)
    return await original_send_destination(
        msg,
        dest,
        status=wrapped,
        index=index,
        total_files=total_files,
    )


app["make_progress_callback"] = patched_make_progress_callback
app["send_destination"] = patched_send_destination


# ---------------------------------------------------------------------------
# Durable Telegram session + private peer cache
# ---------------------------------------------------------------------------
async def durable_saved_session():
    value = await original_saved_session()
    if value:
        return value
    try:
        local = session_store._local_get("primary")
        if isinstance(local, dict) and not local.get("_deleted"):
            local_value = str(local.get("session_string", "")).strip()
            if local_value:
                print("[Voroa] Restoring Telegram session from local mirror.", flush=True)
                return local_value
    except Exception as exc:
        print(f"[Voroa] Local session restore failed: {type(exc).__name__}: {exc}", flush=True)
    return None


async def peer_cache_get(peer):
    key = f"peer:{str(peer).strip()}"
    try:
        local = session_store._local_get(key)
        if isinstance(local, dict) and not local.get("_deleted"):
            return local
    except Exception:
        pass
    return None


async def peer_cache_set(peer, entity):
    peer_text = str(peer).strip()
    entity_id = (
        getattr(entity, "channel_id", None)
        or getattr(entity, "chat_id", None)
        or getattr(entity, "user_id", None)
        or getattr(entity, "id", None)
    )
    if entity_id is None:
        return
    access_hash = getattr(entity, "access_hash", None)
    kind = type(entity).__name__
    if isinstance(entity, InputPeerChannel):
        kind = "InputPeerChannel"
        entity_id = entity.channel_id
        access_hash = entity.access_hash
    elif isinstance(entity, InputPeerUser):
        kind = "InputPeerUser"
        entity_id = entity.user_id
        access_hash = entity.access_hash
    elif isinstance(entity, InputPeerChat):
        kind = "InputPeerChat"
        entity_id = entity.chat_id
        access_hash = None
    session_store._local_set(
        f"peer:{peer_text}",
        {
            "peer": peer_text,
            "kind": kind,
            "id": int(entity_id),
            "access_hash": int(access_hash) if access_hash is not None else None,
        },
    )


def cached_entity(cached):
    if not isinstance(cached, dict):
        return None
    kind = str(cached.get("kind", ""))
    entity_id = int(cached.get("id", 0) or 0)
    access_hash = cached.get("access_hash")
    if not entity_id:
        return None
    if kind in {"Channel", "InputPeerChannel"} and access_hash is not None:
        return InputPeerChannel(entity_id, int(access_hash))
    if kind in {"Chat", "InputPeerChat"}:
        return InputPeerChat(entity_id)
    if kind in {"User", "InputPeerUser"} and access_hash is not None:
        return InputPeerUser(entity_id, int(access_hash))
    return None


async def patched_resolve_message_peer(peer):
    cached = await peer_cache_get(peer)
    entity = cached_entity(cached)
    if entity is not None:
        try:
            resolved = await app["user_client"].get_input_entity(entity)
            print(f"[Voroa] Resolved {peer} from persistent Telegram peer cache.", flush=True)
            return resolved
        except Exception:
            pass
    resolved = await original_resolve_message_peer(peer)
    await peer_cache_set(peer, resolved)
    return resolved


app["saved_session"] = durable_saved_session
app["resolve_message_peer"] = patched_resolve_message_peer


# ---------------------------------------------------------------------------
# Runtime health command
# ---------------------------------------------------------------------------
async def status_command(message):
    uid = message.from_user.id if message.from_user else 0
    if not app["allowed"](uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    mongo = "✅ Connected" if session_store.healthy else "⚠️ Local fallback"
    try:
        session = await app["saved_session"]()
        telegram = "✅ Session saved" if session else "⚠️ No account session — use 🔐 Login"
    except Exception:
        telegram = "⚠️ Session check unavailable"
    destination = app["destination"](uid) or "⚠️ Not set"
    await message.answer(
        "🩺 HJ GROUPS Bot Status\n\n"
        f"🤖 Bot: ✅ Online\n"
        f"🗄️ Storage: {mongo}\n"
        f"📱 Telegram account: {telegram}\n"
        f"🎯 Destination: {destination}\n\n"
        "Use the buttons below to continue.",
        reply_markup=menu_fn(),
    )


app["dp"].message.register(status_command, app["Command"]("status")) if "Command" in app else None


async def run():
    bot = app["bot"]
    dp = app["dp"]
    try:
        # Make sure Telegram is in polling mode and not left in webhook mode
        # from an earlier deployment.
        await bot.delete_webhook(drop_pending_updates=False)
        me = await asyncio.wait_for(bot.get_me(), timeout=15)
        print(f"[Voroa] Bot API connection OK: @{getattr(me, 'username', None) or me.id}", flush=True)
        await app["main"]()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[Voroa] FATAL WORKER ERROR: {type(exc).__name__}: {exc}", flush=True)
        raise


if __name__ == "__main__":
    asyncio.run(run())
