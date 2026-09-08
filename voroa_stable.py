"""Voroa Stable Telegram media transfer bot.

Uses aiogram for the bot UI and Telethon for the user-session Telegram API.
No monkey-patching, no nested runpy entrypoints, no MongoDB, and no full-file
buffering. Transfers are Telegram-to-Telegram through Telethon.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from telethon import TelegramClient, utils
from telethon.errors import (
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
    SessionRevokedError,
)
from telethon.sessions import StringSession

BUILD_TAG = "voroa-stable-2026-09-08-startup2"

API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
SESSION_FILE = Path(os.getenv("VOROA_SESSION_FILE", "/data/voroa_session.txt"))
STATE_FILE = Path(os.getenv("VOROA_STATE_FILE", "/data/voroa_state.json"))
DEFAULT_DESTINATION = os.getenv("TELEGRAM_DESTINATION", "").strip()
OWNER_ID = int(os.getenv("VOROA_OWNER_USER_ID", "0") or 0)
ALLOWED_IDS = {
    int(v.strip()) for v in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if v.strip().isdigit() and int(v.strip()) > 0
}
MAX_BULK_MESSAGES = max(1, int(os.getenv("TELEGRAM_MAX_BULK_MESSAGES", "500") or 500))
MAX_TRANSFER_FILES = max(1, int(os.getenv("VOROA_MAX_TRANSFER_FILES", "100") or 100))
SCAN_TIMEOUT = max(10, int(os.getenv("TELEGRAM_SCAN_TIMEOUT_SECONDS", "30") or 30))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_API_ID, TELEGRAM_API_HASH, or TELEGRAM_BOT_TOKEN.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
# Start with an empty Telethon session. A persisted session is loaded by the
# launcher before polling, so a malformed/stale env session cannot crash the
# process during module import before startup diagnostics can run.
user_client = TelegramClient(StringSession(), API_ID, API_HASH)

PENDING_INPUT: dict[int, str] = {}
JOBS: dict[int, "Job"] = {}
DESTINATIONS: dict[int, str] = {}
ACTIVE_TRANSFERS: dict[int, asyncio.Task] = {}
ACTIVE_JOBS: dict[int, "Job"] = {}
TRANSFER_CONTEXTS: dict[int, "TransferContext"] = {}
LOGIN_DATA: dict[int, dict[str, str]] = {}
STATE_LOCK = asyncio.Lock()
JOB_LOCK = asyncio.Lock()
LIFECYCLE_LOCK = asyncio.Lock()
CLIENT_LOCK = asyncio.Lock()


@dataclass
class Job:
    owner_id: int
    source: Any
    messages: list[Any]
    destination: str
    selected: list[Any]
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class TransferContext:
    uid: int
    job_id: str
    status: Message
    task: Optional[asyncio.Task] = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    status_tasks: set[asyncio.Task] = field(default_factory=set)


def menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 Scan Link"), KeyboardButton(text="📦 Bulk Range")],
            [KeyboardButton(text="🎯 Destination"), KeyboardButton(text="📋 Current Job")],
            [KeyboardButton(text="🔐 Login"), KeyboardButton(text="📱 Session")],
            [KeyboardButton(text="🚪 Logout"), KeyboardButton(text="❌ Cancel")],
            [KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose a function…",
    )


def inline_cancel(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛑 Cancel Transfer", callback_data=f"transfer_cancel:{uid}")]]
    )


def confirm_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm Transfer", callback_data=f"confirm:{job_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"job_cancel:{job_id}")],
        ]
    )


def kind(message: Any) -> str:
    media = getattr(message, "media", None)
    if media is None:
        return "text"
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return "photo"
    document = getattr(media, "document", None)
    if document is None:
        return "media"
    mime = (getattr(document, "mime_type", "") or "").lower()
    attrs = {type(a).__name__ for a in (getattr(document, "attributes", []) or [])}
    if "DocumentAttributeVideo" in attrs or mime.startswith("video/"):
        return "video"
    if "DocumentAttributeAudio" in attrs or mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "photo"
    return "document"


def file_size(message: Any) -> int:
    document = getattr(getattr(message, "media", None), "document", None)
    return int(getattr(document, "size", 0) or 0)


def human_size(value: int) -> str:
    if not value:
        return "Unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "Unknown"


def filename(message: Any) -> str:
    name = getattr(getattr(message, "file", None), "name", None) or f"telegram-{getattr(message, 'id', 'media')}"
    name = re.sub(r"[\\/:*?\"<>|\x00\r\n]+", "_", str(name)).strip(" .")
    return name[:200] or f"telegram-{getattr(message, 'id', 'media')}"


def clean_caption(message: Any) -> str:
    caption = (getattr(message, "message", None) or "").strip()
    extra = f"📦 File Size: {human_size(file_size(message))}"
    return (f"{caption}\n\n{extra}" if caption else extra)[:1024]


def authorized(uid: Optional[int]) -> bool:
    if not uid:
        return False
    uid = int(uid)
    if uid in ALLOWED_IDS:
        return True
    return bool(OWNER_ID and uid == OWNER_ID)


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(STATE_FILE)


STATE = read_state()
for key, value in STATE.get("destinations", {}).items():
    try:
        DESTINATIONS[int(key)] = str(value)
    except (TypeError, ValueError):
        continue


def get_destination(uid: int) -> str:
    return (DESTINATIONS.get(uid) or DEFAULT_DESTINATION).strip()


async def set_destination(uid: int, destination: str) -> str:
    value = destination.strip()
    if not value:
        raise ValueError("Destination cannot be empty.")
    async with STATE_LOCK:
        new_state = dict(STATE)
        destinations = dict(STATE.get("destinations", {}))
        destinations[str(uid)] = value
        new_state["destinations"] = destinations
        write_state(new_state)
        STATE.clear()
        STATE.update(new_state)
        DESTINATIONS[uid] = value
    return value


def save_session_string(value: str) -> None:
    value = value.strip()
    if not value:
        raise ValueError("Empty Telegram session.")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SESSION_FILE.with_suffix(SESSION_FILE.suffix + ".tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(SESSION_FILE)
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass


def load_session_string() -> str:
    try:
        value = SESSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return SESSION_STRING


async def answer_safe(message: Message, text: str, **kwargs: Any) -> Message:
    try:
        return await message.answer(text, **kwargs)
    except TelegramNetworkError as exc:
        print(f"[Voroa] Telegram send failed: {type(exc).__name__}: {exc}", flush=True)
        raise


async def edit_safe(message: Message, text: str, **kwargs: Any) -> Optional[Message]:
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        print(f"[Voroa] Status edit rejected: {exc}", flush=True)
        return None
    except TelegramNetworkError as exc:
        print(f"[Voroa] Status edit network error: {exc}", flush=True)
        return None
    except Exception as exc:
        print(f"[Voroa] Status edit error: {type(exc).__name__}: {exc}", flush=True)
        return None


def has_active_transfer(uid: int) -> bool:
    task = ACTIVE_TRANSFERS.get(uid)
    return task is not None and not task.done()


def transfer_is_active(uid: int, job_id: str, task: Optional[asyncio.Task] = None) -> bool:
    current = ACTIVE_TRANSFERS.get(uid)
    if current is None or current.done():
        return False
    if task is not None and current is not task:
        return False
    active_job = ACTIVE_JOBS.get(uid)
    return active_job is not None and active_job.job_id == job_id


async def ensure_user_client() -> None:
    session = load_session_string()
    if not session:
        raise RuntimeError("No Telegram user session. Use 🔐 Login first or set TELEGRAM_SESSION_STRING.")
    async with CLIENT_LOCK:
        if not user_client.is_connected():
            await asyncio.wait_for(user_client.connect(), timeout=SCAN_TIMEOUT)
        try:
            ok = await asyncio.wait_for(user_client.is_user_authorized(), timeout=SCAN_TIMEOUT)
        except (AuthKeyInvalidError, AuthKeyUnregisteredError, SessionRevokedError) as exc:
            raise RuntimeError("The saved Telegram session is invalid or revoked. Login again.") from exc
        if not ok:
            raise RuntimeError("The saved Telegram session is no longer authorized. Login again.")


async def rebuild_user_client(session: str) -> None:
    global user_client
    if has_any_active_transfer():
        raise RuntimeError("Cannot replace the Telegram session while a transfer is running.")
    async with LIFECYCLE_LOCK:
        if has_any_active_transfer():
            raise RuntimeError("Cannot replace the Telegram session while a transfer is running.")
        async with CLIENT_LOCK:
            if user_client.is_connected():
                await user_client.disconnect()
            try:
                candidate = TelegramClient(StringSession(session), API_ID, API_HASH)
            except (ValueError, TypeError, IndexError) as exc:
                raise RuntimeError("The configured Telegram session string is malformed. Login again.") from exc
            user_client = candidate
            await asyncio.wait_for(user_client.connect(), timeout=SCAN_TIMEOUT)


def has_any_active_transfer() -> bool:
    return any(task is not None and not task.done() for task in ACTIVE_TRANSFERS.values())


async def resolve_peer(value: str) -> Any:
    value = value.strip()
    await ensure_user_client()
    try:
        return await asyncio.wait_for(user_client.get_input_entity(value), timeout=SCAN_TIMEOUT)
    except (ValueError, TypeError, KeyError):
        if not value.lstrip("-").isdigit():
            return await asyncio.wait_for(user_client.get_entity(value), timeout=SCAN_TIMEOUT)
        target_id = int(value)
        async def search_dialogs() -> Any:
            async for dialog in user_client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                try:
                    if utils.get_peer_id(entity, add_mark=True) == target_id:
                        return entity
                except Exception:
                    continue
            return None
        entity = await asyncio.wait_for(search_dialogs(), timeout=SCAN_TIMEOUT * 2)
        if entity is None:
            raise RuntimeError(
                f"Telegram chat {value} is not accessible. The logged-in account must be a member of that chat/channel."
            )
        return entity


def parse_single_link(value: str) -> tuple[str, int]:
    match = re.fullmatch(
        r"https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/([0-9]+)(?:\?.*)?",
        value.strip(),
    )
    if not match:
        raise ValueError("Use a Telegram message link like https://t.me/c/123456789/42")
    private_id, username, mid = match.groups()
    peer = f"-100{private_id}" if private_id else f"@{username}"
    return peer, int(mid)


def parse_range(value: str) -> tuple[str, int, int]:
    match = re.fullmatch(
        r"https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/(\d+)\s*(?:-|to|\s)\s*(\d+)(?:\?.*)?",
        value.strip(), re.IGNORECASE,
    )
    if not match:
        raise ValueError("Use a range like https://t.me/c/123456789/100-110")
    private_id, username, start_text, end_text = match.groups()
    start_id, end_id = int(start_text), int(end_text)
    if start_id <= 0 or end_id < start_id:
        raise ValueError("Invalid message range.")
    if end_id - start_id + 1 > MAX_BULK_MESSAGES:
        raise ValueError(f"Maximum range is {MAX_BULK_MESSAGES} messages.")
    peer = f"-100{private_id}" if private_id else f"@{username}"
    return peer, start_id, end_id


async def get_messages_for_range(entity: Any, start_id: int, end_id: int) -> list[Any]:
    found: dict[int, Any] = {}
    for pos in range(start_id, end_id + 1, 100):
        ids = list(range(pos, min(end_id + 1, pos + 100)))
        rows = await user_client.get_messages(entity, ids=ids)
        if not isinstance(rows, list):
            rows = [rows]
        for message in rows:
            if message is not None:
                found[int(message.id)] = message
    return [found[mid] for mid in range(start_id, end_id + 1) if mid in found]


async def scan_source(peer: str, message_ids: list[int]) -> tuple[Any, list[Any]]:
    entity = await resolve_peer(peer)
    rows = await user_client.get_messages(entity, ids=message_ids)
    if not isinstance(rows, list):
        rows = [rows]
    mapping = {int(m.id): m for m in rows if m is not None}
    return entity, [mapping[mid] for mid in message_ids if mid in mapping]


async def refresh_message(job: Job, message_id: int) -> Any:
    rows = await user_client.get_messages(job.source, ids=[message_id])
    if not isinstance(rows, list):
        rows = [rows]
    message = next((m for m in rows if m is not None and int(m.id) == int(message_id)), None)
    if message is None or getattr(message, "media", None) is None:
        raise RuntimeError(f"Source message #{message_id} is unavailable or no longer contains media.")
    return message


async def register_status_task(ctx: TransferContext, task: asyncio.Task) -> None:
    ctx.status_tasks.add(task)

    def done_callback(done: asyncio.Task) -> None:
        ctx.status_tasks.discard(done)
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(done_callback)


async def cancel_owned_status_tasks(ctx: TransferContext) -> None:
    ctx.stop.set()
    current = asyncio.current_task()
    for task in list(ctx.status_tasks):
        if task is not current and not task.done():
            task.cancel()
    if ctx.status_tasks:
        await asyncio.gather(*list(ctx.status_tasks), return_exceptions=True)
        ctx.status_tasks.clear()


async def heartbeat(ctx: TransferContext, current: str, started: float) -> None:
    pulse = 0
    while not ctx.stop.is_set():
        pulse += 1
        elapsed = int(time.monotonic() - started)
        dots = "." * ((pulse % 3) + 1)
        if not transfer_is_active(ctx.uid, ctx.job_id, ctx.task):
            return
        await edit_safe(
            ctx.status,
            f"📄 <b>{html.escape(current)}</b>\n\n📤 Sending through Telegram{dots}\n⏱ Elapsed: <b>{elapsed}s</b>",
            parse_mode="HTML",
            reply_markup=inline_cancel(ctx.uid),
        )
        try:
            await asyncio.wait_for(ctx.stop.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def progress_callback_factory(ctx: TransferContext, current_name: str, started: float):
    state: dict[str, Any] = {"last": 0.0, "task": None}

    def callback(current: int, total: int) -> None:
        if ctx.stop.is_set() or not transfer_is_active(ctx.uid, ctx.job_id, ctx.task):
            return
        now = time.monotonic()
        if total <= 0 or now - state["last"] < 1.0:
            return
        state["last"] = now
        percent = max(0, min(100, int((current / total) * 100)))
        filled = percent // 10
        elapsed = int(now - started)
        text = (
            f"📄 <b>{html.escape(current_name)}</b>\n\n"
            f"[{percent:3d}%] {'█' * filled}{'░' * (10 - filled)}\n"
            f"⏱ Elapsed: <b>{elapsed}s</b>"
        )
        previous = state.get("task")
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            edit_safe(ctx.status, text, parse_mode="HTML", reply_markup=inline_cancel(ctx.uid)),
            name=f"voroa-status-{ctx.uid}-{ctx.job_id}",
        )
        state["task"] = task
        ctx.status_tasks.add(task)

        def done_callback(done: asyncio.Task) -> None:
            ctx.status_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done_callback)

    return callback, state


async def transfer_one(message: Any, destination: Any, ctx: TransferContext, index: int, total: int) -> None:
    name = filename(message)
    started = time.monotonic()
    beat = asyncio.create_task(heartbeat(ctx, name, started), name=f"voroa-heartbeat-{ctx.uid}-{ctx.job_id}")
    ctx.status_tasks.add(beat)
    try:
        await edit_safe(
            ctx.status,
            f"📄 <b>{html.escape(name)}</b>\n\n"
            f"📦 File {index}/{total}\n"
            f"💾 Size: <b>{human_size(file_size(message))}</b>\n\n"
            "📤 Preparing Telegram transfer…",
            parse_mode="HTML", reply_markup=inline_cancel(ctx.uid),
        )
        progress, progress_state = await progress_callback_factory(ctx, name, started)
        await user_client.send_file(
            destination,
            message.media,
            caption=clean_caption(message),
            force_document=(kind(message) == "document"),
            supports_streaming=True,
            progress_callback=progress,
        )
        pending = progress_state.get("task")
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await edit_safe(
            ctx.status,
            f"✅ <b>{html.escape(name)}</b>\n\nCompleted {index}/{total} • {human_size(file_size(message))}",
            parse_mode="HTML", reply_markup=inline_cancel(ctx.uid) if index < total else None,
        )
    finally:
        ctx.stop.set()
        if beat is not asyncio.current_task() and not beat.done():
            beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        ctx.status_tasks.discard(beat)
        await cancel_owned_status_tasks(ctx)


async def run_transfer(job: Job, ctx: TransferContext, uid: int) -> None:
    start_all = time.monotonic()
    done = 0
    failed: list[str] = []
    destination = await resolve_peer(job.destination)
    total = len(job.selected)
    task = asyncio.current_task()
    for index, selected in enumerate(job.selected, 1):
        if not transfer_is_active(uid, job.job_id, task):
            raise asyncio.CancelledError
        message_id = int(getattr(selected, "id", selected))
        try:
            fresh = await refresh_message(job, message_id)
            await transfer_one(fresh, destination, ctx, index, total)
            done += 1
        except FloodWaitError as exc:
            wait_for = max(1, int(getattr(exc, "seconds", 1))) + 1
            if ctx.stop.is_set() or task.cancelled():
                raise asyncio.CancelledError
            await edit_safe(ctx.status, f"⏳ Telegram rate limit. Retrying in <b>{wait_for}s</b>…", parse_mode="HTML", reply_markup=inline_cancel(uid))
            try:
                await asyncio.wait_for(ctx.stop.wait(), timeout=wait_for)
            except asyncio.TimeoutError:
                pass
            if ctx.stop.is_set() or task.cancelled():
                raise asyncio.CancelledError
            try:
                await transfer_one(fresh, destination, ctx, index, total)
                done += 1
            except Exception as retry_exc:
                failed.append(f"{filename(fresh)}: {retry_exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed.append(f"{filename(selected)}: {exc}")
    elapsed = int(time.monotonic() - start_all)
    if failed:
        await edit_safe(ctx.status, f"⚠️ <b>Transfer finished with errors</b>\n\nCompleted: <b>{done}/{total}</b>\nFailed: <b>{len(failed)}</b>\n⏱ {elapsed}s\n\n" + "\n".join(f"• {html.escape(x)}" for x in failed)[:3000], parse_mode="HTML", reply_markup=None)
    else:
        await edit_safe(ctx.status, f"✅ <b>Transfer complete</b>\n\nFiles: <b>{done}/{total}</b>\n⏱ {elapsed}s", parse_mode="HTML", reply_markup=None)


async def transfer_runner(uid: int, job: Job, status: Message) -> None:
    task = asyncio.current_task()
    ctx = TransferContext(uid=uid, job_id=job.job_id, status=status, task=task)
    TRANSFER_CONTEXTS[uid] = ctx
    ACTIVE_TRANSFERS[uid] = task
    ACTIVE_JOBS[uid] = job
    try:
        await run_transfer(job, ctx, uid)
    except asyncio.CancelledError:
        ctx.stop.set()
        await edit_safe(status, "🛑 <b>Transfer cancelled.</b>", parse_mode="HTML", reply_markup=None)
        raise
    except Exception as exc:
        await edit_safe(status, f"❌ <b>Transfer failed</b>\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML", reply_markup=None)
    finally:
        await cancel_owned_status_tasks(ctx)
        ACTIVE_TRANSFERS.pop(uid, None)
        ACTIVE_JOBS.pop(uid, None)
        TRANSFER_CONTEXTS.pop(uid, None)
        JOBS.pop(uid, None)


async def start_transfer(uid: int, job: Job, source_message: Message) -> bool:
    if has_active_transfer(uid) or has_any_active_transfer():
        await source_message.answer("⛔ Another transfer is already running. Cancel it first.", reply_markup=menu())
        return False
    status = await source_message.answer(
        f"🚀 <b>Transfer started</b>\n\n📦 Files: <b>{len(job.selected)}</b>\n🎯 Destination: <code>{html.escape(job.destination)}</code>\n\n⏳ Preparing transfer…",
        parse_mode="HTML", reply_markup=inline_cancel(uid),
    )
    task = asyncio.create_task(transfer_runner(uid, job, status), name=f"voroa-transfer-{uid}-{job.job_id}")
    ACTIVE_TRANSFERS[uid] = task
    return True


@dp.message(CommandStart())
async def command_start(message: Message) -> None:
    if not authorized(message.from_user.id):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    await message.answer("🤖 <b>Voroa</b> is ready. Choose a function below.", parse_mode="HTML", reply_markup=menu())


@dp.message(Command("help"))
async def command_help(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    await message.answer(
        "ℹ️ <b>Voroa Help</b>\n\n"
        "🔗 Scan Link — scan one Telegram message link.\n"
        "📦 Bulk Range — scan a message range.\n"
        "🎯 Destination — set the target chat/channel.\n"
        "📋 Current Job — inspect the pending transfer.\n"
        "🔐 Login — authenticate the Telegram user account.\n"
        "📱 Session — check the user session.\n"
        "❌ Cancel — cancel the current action/transfer.",
        parse_mode="HTML", reply_markup=menu(),
    )


@dp.message(F.text == "🔗 Scan Link")
async def button_scan(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    PENDING_INPUT[message.from_user.id] = "single"
    await message.answer("🔗 Send the Telegram message link.", reply_markup=menu())


@dp.message(F.text == "📦 Bulk Range")
async def button_bulk(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    PENDING_INPUT[message.from_user.id] = "range"
    await message.answer("📦 Send a range like:\nhttps://t.me/c/123456789/100-110", reply_markup=menu())


@dp.message(F.text == "🎯 Destination")
async def button_destination(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    PENDING_INPUT[message.from_user.id] = "destination"
    current = get_destination(message.from_user.id)
    await message.answer(
        f"🎯 Send destination chat/channel ID or @username.\n\nCurrent: <code>{html.escape(current or 'not set')}</code>",
        parse_mode="HTML", reply_markup=menu(),
    )


@dp.message(F.text == "📋 Current Job")
async def button_job(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    uid = message.from_user.id
    job = JOBS.get(uid) or ACTIVE_JOBS.get(uid)
    if not job:
        await message.answer("📋 No active job. Scan a link first.", reply_markup=menu())
        return
    total = sum(file_size(m) for m in job.selected if not isinstance(m, int))
    state_text = "transferring" if has_active_transfer(uid) else "awaiting confirmation"
    await message.answer(
        f"📋 <b>Current job</b>\n\nFiles: <b>{len(job.selected)}</b>\n"
        f"Total: <b>{human_size(total)}</b>\n"
        f"Destination: <code>{html.escape(job.destination)}</code>\n"
        f"State: <b>{state_text}</b>\nJob ID: <code>{job.job_id}</code>",
        parse_mode="HTML", reply_markup=None if has_active_transfer(uid) else confirm_keyboard(job.job_id),
    )


@dp.message(F.text == "🔐 Login")
async def button_login(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    if has_active_transfer(message.from_user.id):
        await message.answer("⛔ Login is blocked while a transfer is running.", reply_markup=menu())
        return
    PENDING_INPUT[message.from_user.id] = "phone"
    await message.answer("🔐 Send your Telegram phone number in international format.", reply_markup=menu())


@dp.message(F.text == "📱 Session")
async def button_session(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    try:
        await ensure_user_client()
        me = await user_client.get_me()
        name = " ".join(x for x in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if x).strip()
        await message.answer(
            "📱 <b>Session active</b>\n\n"
            f"Account: <b>{html.escape(name or 'Unknown')}</b>\nUser ID: <code>{me.id}</code>",
            parse_mode="HTML", reply_markup=menu(),
        )
    except Exception as exc:
        await message.answer(f"⚠️ <b>Session unavailable</b>\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML", reply_markup=menu())


@dp.message(F.text == "🚪 Logout")
async def button_logout(message: Message) -> None:
    if not authorized(message.from_user.id):
        return
    uid = message.from_user.id
    async with LIFECYCLE_LOCK:
        if has_active_transfer(uid) or has_any_active_transfer():
            await message.answer("⛔ Logout is blocked while a transfer is running.", reply_markup=menu())
            return
        async with CLIENT_LOCK:
            try:
                if user_client.is_connected():
                    await user_client.log_out()
            except Exception as exc:
                await message.answer(f"❌ Logout failed: {html.escape(str(exc))}", reply_markup=menu())
                return
            try:
                if user_client.is_connected():
                    await user_client.disconnect()
            except Exception:
                pass
            try:
                SESSION_FILE.unlink(missing_ok=True)
            except OSError as exc:
                await message.answer(f"⚠️ Session logged out, but local session cleanup failed: {html.escape(str(exc))}", reply_markup=menu())
                return
    LOGIN_DATA.pop(uid, None)
    await message.answer("🚪 Logged out successfully. Login again when needed.", reply_markup=menu())


@dp.message(F.text == "❌ Cancel")
async def button_cancel(message: Message) -> None:
    uid = message.from_user.id
    if not authorized(uid):
        return
    PENDING_INPUT.pop(uid, None)
    task = ACTIVE_TRANSFERS.get(uid)
    if task is not None and not task.done():
        task.cancel()
        await message.answer("🛑 Transfer cancellation requested.", reply_markup=menu())
        return
    JOBS.pop(uid, None)
    await message.answer("✅ Cancelled.", reply_markup=menu())


@dp.callback_query(F.data.startswith("job_cancel:"))
async def job_cancel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    if not authorized(uid):
        await callback.answer("Not authorized", show_alert=True)
        return
    job_id = callback.data.split(":", 1)[1]
    async with JOB_LOCK:
        active = ACTIVE_JOBS.get(uid)
        if active is not None and active.job_id == job_id:
            task = ACTIVE_TRANSFERS.get(uid)
            if task is not None and not task.done():
                task.cancel()
                await callback.answer("🛑 Cancelling transfer…")
                return
        pending = JOBS.get(uid)
        if pending is None or pending.job_id != job_id:
            await callback.answer("Job expired/stale.", show_alert=True)
            return
        JOBS.pop(uid, None)
    await callback.answer("Cancelled")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ Job cancelled.", reply_markup=menu())


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    if not authorized(uid):
        await callback.answer("Not authorized", show_alert=True)
        return
    job_id = callback.data.split(":", 1)[1]
    job = JOBS.get(uid)
    if job is None or job.job_id != job_id or not job.selected:
        await callback.answer("Job expired/stale. Scan again.", show_alert=True)
        return
    started = await start_transfer(uid, job, callback.message)
    if not started:
        return
    await callback.answer("🚀 Started")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("transfer_cancel:"))
async def transfer_cancel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    if not authorized(uid):
        await callback.answer("Not authorized", show_alert=True)
        return
    task = ACTIVE_TRANSFERS.get(uid)
    if task is None or task.done():
        await callback.answer("No active transfer", show_alert=True)
        return
    task.cancel()
    await callback.answer("🛑 Cancelling…")


@dp.message()
async def text_router(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not authorized(uid):
        return
    text = (message.text or "").strip()
    if not text:
        return
    mode = PENDING_INPUT.get(uid)
    if mode is None:
        return
    if mode == "single":
        PENDING_INPUT.pop(uid, None)
        try:
            peer, mid = parse_single_link(text)
            entity, rows = await scan_source(peer, [mid])
            media = [m for m in rows if getattr(m, "media", None) is not None]
            if not media:
                raise RuntimeError("That message has no media file.")
            destination = get_destination(uid)
            if not destination:
                raise RuntimeError("Set 🎯 Destination first.")
            job = Job(uid, entity, rows, destination, media[:1])
            async with JOB_LOCK:
                JOBS[uid] = job
            total = file_size(media[0])
            await message.answer(
                "🔎 <b>File found</b>\n\n"
                f"📄 <b>{html.escape(filename(media[0]))}</b>\n"
                f"💾 Size: <b>{human_size(total)}</b>\n"
                f"🎯 Destination: <code>{html.escape(destination)}</code>",
                parse_mode="HTML", reply_markup=confirm_keyboard(job.job_id),
            )
        except Exception as exc:
            await message.answer(f"❌ Scan failed\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML", reply_markup=menu())
        return
    if mode == "range":
        PENDING_INPUT.pop(uid, None)
        try:
            peer, start_id, end_id = parse_range(text)
            entity = await resolve_peer(peer)
            rows = await get_messages_for_range(entity, start_id, end_id)
            media = [m for m in rows if getattr(m, "media", None) is not None]
            if not media:
                raise RuntimeError("No media files were found in that range.")
            destination = get_destination(uid)
            if not destination:
                raise RuntimeError("Set 🎯 Destination first.")
            selected = media[:MAX_TRANSFER_FILES]
            job = Job(uid, entity, rows, destination, selected)
            async with JOB_LOCK:
                JOBS[uid] = job
            total = sum(file_size(m) for m in selected)
            await message.answer(
                "🔎 <b>Bulk range scanned</b>\n\n"
                f"📦 Files: <b>{len(selected)}</b>\n"
                f"💾 Total: <b>{human_size(total)}</b>\n"
                f"🎯 Destination: <code>{html.escape(destination)}</code>",
                parse_mode="HTML", reply_markup=confirm_keyboard(job.job_id),
            )
        except Exception as exc:
            await message.answer(f"❌ Bulk scan failed\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML", reply_markup=menu())
        return
    if mode == "destination":
        PENDING_INPUT.pop(uid, None)
        try:
            value = await set_destination(uid, text)
            await resolve_peer(value)
            await message.answer(f"✅ Destination saved: <code>{html.escape(value)}</code>", parse_mode="HTML", reply_markup=menu())
        except Exception as exc:
            await message.answer(f"❌ Destination rejected\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML", reply_markup=menu())
        return
    if mode == "phone":
        PENDING_INPUT.pop(uid, None)
        await login_phone(message, text)
        return
    if mode == "code":
        PENDING_INPUT.pop(uid, None)
        await login_code(message, text)
        return
    if mode == "password":
        PENDING_INPUT.pop(uid, None)
        await login_password(message, text)


async def login_phone(message: Message, phone: str) -> None:
    uid = message.from_user.id
    async with LIFECYCLE_LOCK:
        if has_any_active_transfer():
            await message.answer("⛔ Login is blocked while a transfer is running.", reply_markup=menu())
            return
        async with CLIENT_LOCK:
            if user_client.is_connected():
                await user_client.disconnect()
            await user_client.connect()
            try:
                result = await user_client.send_code_request(phone)
            except RPCError as exc:
                await message.answer(f"❌ Telegram login failed: {html.escape(str(exc))}", reply_markup=menu())
                return
            LOGIN_DATA[uid] = {"phone": phone, "phone_code_hash": result.phone_code_hash}
    PENDING_INPUT[uid] = "code"
    await message.answer("📨 Telegram code sent. Send the code here.", reply_markup=menu())


async def login_code(message: Message, code: str) -> None:
    uid = message.from_user.id
    data = LOGIN_DATA.get(uid)
    if not data:
        await message.answer("❌ Login session expired. Press 🔐 Login again.", reply_markup=menu())
        return
    try:
        await message.delete()
    except Exception:
        pass
    async with LIFECYCLE_LOCK:
        if has_any_active_transfer():
            await message.answer("⛔ Login is blocked while a transfer is running.", reply_markup=menu())
            return
        async with CLIENT_LOCK:
            try:
                await user_client.sign_in(data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
            except SessionPasswordNeededError:
                PENDING_INPUT[uid] = "password"
                await message.answer("🔐 Two-step verification is enabled. Send your Telegram 2FA password. The message will be deleted when possible.", reply_markup=menu())
                return
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
                LOGIN_DATA.pop(uid, None)
                await message.answer(f"❌ Invalid/expired code: {html.escape(str(exc))}", reply_markup=menu())
                return
            except Exception as exc:
                LOGIN_DATA.pop(uid, None)
                await message.answer(f"❌ Login failed: {html.escape(str(exc))}", reply_markup=menu())
                return
            await finalize_login(message, uid)


async def login_password(message: Message, password: str) -> None:
    uid = message.from_user.id
    if uid not in LOGIN_DATA:
        await message.answer("❌ Login session expired. Press 🔐 Login again.", reply_markup=menu())
        return
    try:
        await message.delete()
    except Exception:
        pass
    async with LIFECYCLE_LOCK:
        if has_any_active_transfer():
            await message.answer("⛔ Login is blocked while a transfer is running.", reply_markup=menu())
            return
        async with CLIENT_LOCK:
            try:
                await user_client.sign_in(password=password)
            except Exception as exc:
                await message.answer(f"❌ 2FA verification failed: {html.escape(str(exc))}", reply_markup=menu())
                return
            await finalize_login(message, uid)


async def finalize_login(message: Message, uid: int) -> None:
    try:
        string = StringSession.save(user_client.session)
        save_session_string(string)
    except Exception as exc:
        LOGIN_DATA.pop(uid, None)
        await message.answer(
            f"⚠️ Telegram login succeeded, but the session could not be saved persistently: {html.escape(str(exc))}",
            reply_markup=menu(),
        )
        return
    LOGIN_DATA.pop(uid, None)
    await message.answer(
        "✅ <b>Telegram session saved</b>\n\nYour Voroa session is ready. Set a destination and scan a link.",
        parse_mode="HTML", reply_markup=menu(),
    )


async def setup_commands() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Open Voroa"),
        BotCommand(command="help", description="Show help"),
    ])


async def on_startup() -> None:
    print(f"[Voroa] Stable build: {BUILD_TAG}", flush=True)
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"):
        print(f"[Voroa] {key}: {'set' if os.getenv(key) else 'MISSING'}", flush=True)
    print(f"[Voroa] Session: {'set' if load_session_string() else 'missing'}", flush=True)
    print(f"[Voroa] Destination: {'set' if DEFAULT_DESTINATION else 'per-user/interactive'}", flush=True)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:
        print(f"[Voroa] Webhook cleanup skipped: {type(exc).__name__}: {exc}", flush=True)
    await setup_commands()
    try:
        if load_session_string():
            await ensure_user_client()
            me = await user_client.get_me()
            print(f"[Voroa] Telegram user session OK: {getattr(me, 'id', 'unknown')}", flush=True)
    except Exception as exc:
        print(f"[Voroa] Telegram user session not ready: {type(exc).__name__}: {exc}", flush=True)
    print("[Voroa] Bot polling is ready.", flush=True)


async def on_shutdown() -> None:
    tasks = [task for task in ACTIVE_TRANSFERS.values() if task is not None and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for ctx in list(TRANSFER_CONTEXTS.values()):
        await cancel_owned_status_tasks(ctx)
    ACTIVE_TRANSFERS.clear()
    ACTIVE_JOBS.clear()
    TRANSFER_CONTEXTS.clear()
    try:
        if user_client.is_connected():
            await user_client.disconnect()
    except Exception:
        pass
    await bot.session.close()


async def main() -> None:
    await on_startup()
    try:
        await dp.start_polling(bot, handle_signals=True, polling_timeout=20, tasks_concurrency_limit=100)
    finally:
        await on_shutdown()


if __name__ == "__main__":
    asyncio.run(main())
