"""Hardened Voroa entrypoint for the HJ GROUPS Telegram bot."""
from __future__ import annotations

import asyncio
import os
import runpy
import time

from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

BUILD_TAG = "voroa-transfer-ui-2026-09-08"
ACTIVE_TRANSFER_TASKS = {}
ACTIVE_TRANSFER_STATUS = {}

# aiogram Message.edit_text only accepts inline keyboards. The project uses a
# reply keyboard as its persistent chat menu, so remove that markup only for
# edits while keeping it on normal message.answer calls.
_original_edit_text = Message.edit_text


async def _safe_edit_text(self, text=None, **kwargs):
    if isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup):
        kwargs.pop("reply_markup", None)
    return await _original_edit_text(self, text=text, **kwargs)


Message.edit_text = _safe_edit_text

for target, source in {
    "TELEGRAM_API_ID": "API_ID",
    "TELEGRAM_API_HASH": "API_HASH",
    "TELEGRAM_BOT_TOKEN": "BOT_TOKEN",
}.items():
    if not os.getenv(target) and os.getenv(source):
        os.environ[target] = os.environ[source]

print(f"[Voroa] Hardened worker build: {BUILD_TAG}", flush=True)

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

app = runpy.run_path("telegram_bot.py", run_name="hj_groups_app")
menu_fn = app["menu"]
session_store = app["session_store"]
original_resolve_message_peer = app["resolve_message_peer"]
original_saved_session = app["saved_session"]
original_make_progress_callback = app["make_progress_callback"]
original_send_destination = app["send_destination"]
original_process = app["process"]


def install_global_error_handler():
    dp = app["dp"]
    bot = app["bot"]

    async def error_handler(event: ErrorEvent):
        exc = event.exception
        update = event.update
        print(f"[Voroa] HANDLER ERROR: {type(exc).__name__}: {exc}", flush=True)
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
            print(f"[Voroa] Error notification failed: {type(send_exc).__name__}: {send_exc}", flush=True)

    dp.errors.register(error_handler)


install_global_error_handler()


class _ProgressStatus:
    def __init__(self, inner, uid=None):
        self.inner = inner
        self.uid = int(uid) if uid else None
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
        # During an active transfer, use an inline cancel action. The normal
        # persistent reply keyboard remains available in the chat itself.
        if self.uid:
            kwargs["reply_markup"] = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛑 Cancel Transfer", callback_data=f"transfer_cancel:{self.uid}")]
                ]
            )
        else:
            kwargs.pop("reply_markup", None)
        try:
            return await self.inner.edit_text(text, **kwargs)
        except Exception as exc:
            print(f"[Voroa] Transfer status edit failed: {type(exc).__name__}: {exc}", flush=True)
            return None

    def __getattr__(self, name):
        return getattr(self.inner, name)


def patched_make_progress_callback(status, filename, stage, started_at):
    uid = getattr(status, "uid", None)
    state = {"last": 0.0, "pending": None, "task": None}

    async def worker():
        try:
            while state["pending"] is not None:
                text = state["pending"]
                state["pending"] = None
                try:
                    if isinstance(status, _ProgressStatus):
                        await status.inner.edit_text(
                            text,
                            reply_markup=(
                                InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [InlineKeyboardButton(text="🛑 Cancel Transfer", callback_data=f"transfer_cancel:{status.uid}")]
                                    ]
                                )
                                if status.uid
                                else None
                            ),
                        )
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
    wrapped = None if status is None else (
        status if isinstance(status, _ProgressStatus) else _ProgressStatus(status)
    )
    return await original_send_destination(msg, dest, status=wrapped, index=index, total_files=total_files)


async def patched_process(job, status):
    uid = getattr(job, "owner_id", None)
    wrapped = status if isinstance(status, _ProgressStatus) else _ProgressStatus(status, uid=uid)
    return await original_process(job, wrapped)


async def _run_transfer(job, callback_message, uid):
    status = None
    try:
        status = await callback_message.answer(
            "🚀 <b>Transfer started</b>\n\n"
            f"📦 Files: <b>{len(job.selected)}</b>\n"
            f"🎯 Destination: <code>{job.destination}</code>\n\n"
            "⏳ Preparing transfer…",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛑 Cancel Transfer", callback_data=f"transfer_cancel:{uid}")]
                ]
            ),
            parse_mode="HTML",
        )
        ACTIVE_TRANSFER_STATUS[uid] = status
        await app["process"](job, status)
    except asyncio.CancelledError:
        print(f"[Voroa] Transfer cancelled by user: {uid}", flush=True)
        if status is not None:
            try:
                await status.edit_text("🛑 <b>Transfer cancelled</b>\n\nThe active transfer was stopped safely.", parse_mode="HTML")
            except Exception:
                pass
        raise
    except Exception as exc:
        print(f"[Voroa] TRANSFER ERROR user={uid}: {type(exc).__name__}: {exc}", flush=True)
        if status is not None:
            try:
                await status.edit_text(
                    f"❌ <b>Transfer failed</b>\n\n<code>{type(exc).__name__}: {exc}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    finally:
        ACTIVE_TRANSFER_TASKS.pop(uid, None)
        ACTIVE_TRANSFER_STATUS.pop(uid, None)
        app["JOBS"].pop(uid, None)


async def patched_confirm(callback):
    uid = callback.from_user.id
    if not app["allowed"](uid):
        return await callback.answer("Not authorized", show_alert=True)
    try:
        owner = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        return await callback.answer("Invalid transfer request", show_alert=True)
    job = app["JOBS"].get(owner)
    if not job or owner != uid:
        return await callback.answer("Job expired. Please scan again.", show_alert=True)
    if not job.selected:
        return await callback.answer("Nothing selected", show_alert=True)
    old_task = ACTIVE_TRANSFER_TASKS.get(uid)
    if old_task is not None and not old_task.done():
        return await callback.answer("A transfer is already running.", show_alert=True)
    await callback.answer("🚀 Started")
    task = asyncio.create_task(_run_transfer(job, callback.message, uid), name=f"voroa-transfer-{uid}")
    ACTIVE_TRANSFER_TASKS[uid] = task


async def transfer_cancel_callback(callback):
    uid = callback.from_user.id
    if not app["allowed"](uid):
        return await callback.answer("Not authorized", show_alert=True)
    task = ACTIVE_TRANSFER_TASKS.get(uid)
    if task is None or task.done():
        return await callback.answer("No active transfer.", show_alert=True)
    await callback.answer("🛑 Cancelling transfer…")
    task.cancel()


async def patched_cancel_message(message):
    uid = message.from_user.id if message.from_user else None
    if not app["allowed"](uid):
        return await message.answer("⛔ You are not authorized to use this bot.")
    task = ACTIVE_TRANSFER_TASKS.get(uid)
    if task is not None and not task.done():
        task.cancel()
        return await message.answer("🛑 Transfer cancellation requested.", reply_markup=menu_fn())
    return await app["cancel_cmd"](message)


# Replace the registered callback handlers with hardened transfer versions.
dp = app["dp"]
for handler in getattr(dp.callback_query, "handlers", []):
    callback = getattr(handler, "callback", None)
    name = getattr(callback, "__name__", "")
    if name == "confirm":
        handler.callback = patched_confirm
    elif name == "cancel_callback":
        # Leave the original job-cancel action intact; transfer-specific cancel
        # uses a dedicated callback_data prefix below.
        pass
for handler in getattr(dp.message, "handlers", []):
    callback = getattr(handler, "callback", None)
    name = getattr(callback, "__name__", "")
    if name in {"cancel_cmd", "button_cancel"}:
        handler.callback = patched_cancel_message

# Dedicated inline cancel callback has no overlap with the existing cancel:
# handlers, so it can safely be registered after the existing routes.
@dp.callback_query(app["F"].data.startswith("transfer_cancel:"))
async def _transfer_cancel_router(callback):
    await transfer_cancel_callback(callback)


app["make_progress_callback"] = patched_make_progress_callback
app["send_destination"] = patched_send_destination
app["process"] = patched_process
app["saved_session"] = durable_saved_session if "durable_saved_session" in globals() else original_saved_session
app["resolve_message_peer"] = patched_resolve_message_peer if "patched_resolve_message_peer" in globals() else original_resolve_message_peer


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
            value = str(local.get("session_string", "")).strip()
            if value:
                print("[Voroa] Restoring Telegram session from local mirror.", flush=True)
                return value
    except Exception as exc:
        print(f"[Voroa] Local session restore failed: {type(exc).__name__}: {exc}", flush=True)
    return None


async def peer_cache_get(peer):
    try:
        value = session_store._local_get(f"peer:{str(peer).strip()}")
        return value if isinstance(value, dict) and not value.get("_deleted") else None
    except Exception:
        return None


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


async def peer_cache_set(peer, entity):
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
        kind, entity_id, access_hash = "InputPeerChannel", entity.channel_id, entity.access_hash
    elif isinstance(entity, InputPeerUser):
        kind, entity_id, access_hash = "InputPeerUser", entity.user_id, entity.access_hash
    elif isinstance(entity, InputPeerChat):
        kind, entity_id, access_hash = "InputPeerChat", entity.chat_id, None
    session_store._local_set(
        f"peer:{str(peer).strip()}",
        {"peer": str(peer).strip(), "kind": kind, "id": int(entity_id), "access_hash": access_hash},
    )


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


async def run():
    bot = app["bot"]
    try:
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
