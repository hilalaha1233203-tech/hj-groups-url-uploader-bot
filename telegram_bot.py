"""HJ GROUPS Telegram Media Collector with persistent button UI."""
from __future__ import annotations
import asyncio, json, os, re, tempfile, time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    FSInputFile,
    BotCommand,
    MenuButtonCommands,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError, AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError
from telegram_session_store import session_store
from playbook_client import PlaybookClient, PlaybookError
import qrcode

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
ALLOWED_USER_IDS = {int(v.strip()) for v in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if v.strip().isdigit() and int(v.strip()) > 0}
EXPLICIT_OWNER_ID = int(os.getenv("VOROA_OWNER_USER_ID", "0") or 0)
DEFAULT_DESTINATION = os.getenv("TELEGRAM_DESTINATION", "").strip()
BRANDING = os.getenv("TELEGRAM_BRANDING", "@hjgroups_1").strip()
BOT_DELETE_SECONDS = max(0, int(os.getenv("TELEGRAM_BOT_DELETE_SECONDS", "3600")))
MAX_BULK_MESSAGES = max(1, int(os.getenv("TELEGRAM_MAX_BULK_MESSAGES", "500")))
MAX_ACTIVE_JOBS = max(1, int(os.getenv("TELEGRAM_MAX_ACTIVE_JOBS", "1")))
SCAN_TIMEOUT_SECONDS = max(10, int(os.getenv("TELEGRAM_SCAN_TIMEOUT_SECONDS", "35")))
STATE_FILE = Path(os.getenv("TELEGRAM_STATE_FILE", "telegram_media_state.json"))

ENV_DESTINATIONS = {}
for item in os.getenv("TELEGRAM_USER_DESTINATIONS", "").split("|"):
    if ":" in item:
        uid, dest = item.split(":", 1)
        if uid.strip().isdigit() and dest.strip():
            ENV_DESTINATIONS[uid.strip()] = dest.strip()

user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
SEM = asyncio.Semaphore(MAX_ACTIVE_JOBS)
DELETE_TASKS = set()
LOGIN_LOCK = asyncio.Lock()
PENDING = {}
JOBS = {}
QR_CLIENTS = {}
DESTINATIONS = {}
ACCESS_CACHE: dict[int, dict[str, Any]] = {}
OWNER_USER_ID = EXPLICIT_OWNER_ID or (min(ALLOWED_USER_IDS) if ALLOWED_USER_IDS else 0)

@dataclass
class Job:
    owner_id: int
    source: Any
    candidates: dict[int, Any]
    destination: str
    selected: set[int]

def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 Scan Link"), KeyboardButton(text="📦 Bulk Range")],
            [KeyboardButton(text="🎯 Destination"), KeyboardButton(text="📋 Select Files")],
            [KeyboardButton(text="🔐 Login"), KeyboardButton(text="📱 Session")],
            [KeyboardButton(text="🚪 Logout"), KeyboardButton(text="❌ Cancel")],
            [KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose a function…",
    )

def allowed(uid):
    if not uid:
        return False
    uid = int(uid)
    if OWNER_USER_ID and uid == OWNER_USER_ID:
        return True
    record = ACCESS_CACHE.get(uid)
    if not record or not record.get("active", False):
        return False
    expires_at = record.get("expires_at")
    if expires_at is None:
        return True
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < expires_at

async def load_access_state():
    global OWNER_USER_ID
    rows = await session_store.list_access()
    for row in rows:
        ACCESS_CACHE[int(row["user_id"])] = row
        if row.get("role") == "owner":
            OWNER_USER_ID = int(row["user_id"])
    if not OWNER_USER_ID:
        raise RuntimeError("No Voroa owner is configured. Set VOROA_OWNER_USER_ID or TELEGRAM_ALLOWED_USER_IDS.")
    await session_store.set_access(OWNER_USER_ID, active=True, expires_at=None, role="owner")
    ACCESS_CACHE[OWNER_USER_ID] = {"user_id": OWNER_USER_ID, "active": True, "expires_at": None, "role": "owner"}
    for uid in ALLOWED_USER_IDS:
        if uid == OWNER_USER_ID:
            continue
        existing = ACCESS_CACHE.get(uid)
        if not existing:
            await session_store.set_access(uid, active=True, expires_at=None, role="user")
            ACCESS_CACHE[uid] = {"user_id": uid, "active": True, "expires_at": None, "role": "user"}

def load_state():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

STATE = load_state()

def save_state():
    try:
        STATE_FILE.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
    except OSError:
        pass

def destination(uid):
    return (DESTINATIONS.get(str(uid)) or STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()

async def load_destination(uid):
    cached = DESTINATIONS.get(str(uid))
    if cached:
        return cached
    saved = await session_store.get_destination(uid)
    if saved:
        DESTINATIONS[str(uid)] = saved
        return saved
    value = (STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid) or "") or DEFAULT_DESTINATION).strip()
    if value:
        await session_store.set_destination(uid, value)
        DESTINATIONS[str(uid)] = value
    return value

async def set_destination(uid, dest):
    value = dest.strip()
    await session_store.set_destination(uid, value)
    DESTINATIONS[str(uid)] = value
    STATE[str(uid)] = value
    save_state()
    return value

def size(n):
    if not n:
        return "Unknown"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "Unknown"

def kind(m):
    media = getattr(m, "media", None)
    if media is None:
        return None
    if type(media).__name__ == "MessageMediaPhoto":
        return "photo"
    doc = getattr(media, "document", None)
    if doc is None:
        return "other"
    mime = (getattr(doc, "mime_type", "") or "").lower()
    names = {type(a).__name__ for a in (getattr(doc, "attributes", []) or [])}
    if "DocumentAttributeAudio" in names or mime.startswith("audio/"):
        return "audio"
    if "DocumentAttributeVideo" in names or mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "photo"
    return "document"

def media_size(m):
    return int(getattr(getattr(getattr(m, "media", None), "document", None), "size", 0) or 0)

def caption(m):
    parts = []
    original = (getattr(m, "message", None) or "").strip()
    if original:
        parts.append(original)
    parts.append(f"📦 File Size: {size(media_size(m))}")
    if BRANDING:
        parts.append(BRANDING)
    return "\n\n".join(parts)[:1024]

def parse_link(value):
    match = re.match(r"^https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/([0-9]+)(?:\?.*)?$", value.strip())
    if not match:
        raise ValueError("Invalid Telegram message link.")
    private, username, mid = match.groups()
    return (f"-100{private}" if private else f"@{username}"), int(mid)

def parse_bulk_link(value):
    match = re.match(r"^https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/(\d+)\s*(?:-|\s)\s*(\d+)(?:\?.*)?$", value.strip())
    if not match:
        return None
    private, username, start_text, end_text = match.groups()
    start_id, end_id = int(start_text), int(end_text)
    if start_id <= 0 or end_id < start_id:
        raise ValueError("Invalid Telegram message ID range.")
    count = end_id - start_id + 1
    if count > MAX_BULK_MESSAGES:
        raise ValueError(f"Maximum range is {MAX_BULK_MESSAGES} messages.")
    peer = f"-100{private}" if private else f"@{username}"
    return peer, start_id, end_id

async def flood(fn: Callable[[], Awaitable[Any]]):
    while True:
        try:
            return await fn()
        except FloodWaitError as exc:
            await asyncio.sleep(max(1, int(getattr(exc, "seconds", 1))) + 1)

async def saved_session():
    return SESSION_STRING or await session_store.get()

async def rebuild(session):
    global user_client
    if user_client.is_connected():
        await user_client.disconnect()
    user_client = TelegramClient(StringSession(session), API_ID, API_HASH)
    await asyncio.wait_for(user_client.connect(), timeout=SCAN_TIMEOUT_SECONDS)

async def ensure_user_client():
    session = await saved_session()
    if not session:
        raise RuntimeError("No saved Telegram session. Press 🔐 Login first.")
    if not user_client.is_connected():
        await rebuild(session)
    authorized = await asyncio.wait_for(user_client.is_user_authorized(), timeout=SCAN_TIMEOUT_SECONDS)
    if not authorized:
        raise RuntimeError("Telegram session is no longer authorized. Press 🔐 Login first.")

def safe_filename(msg):
    name = getattr(getattr(msg, "file", None), "name", None)
    if name:
        return str(name)
    return f"telegram-{getattr(msg, 'id', 'media')}"

def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "calculating…"
    seconds = int(round(seconds))
    if seconds < 1:
        return "0s left"
    if seconds < 60:
        return f"{seconds}s left"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s left"

def make_progress_callback(status, filename, stage, started_at):
    state = {"last": 0.0, "task": None}
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
            filled = int(percent // 10)
            text = (f"📄 {filename}\n\n{stage}\n"
                    f"[{int(percent):3d}%] {'█' * filled}{'░' * (10 - filled)}\n"
                    f"⏳ {format_eta(eta)}")
        else:
            text = f"📄 {filename}\n\n{stage}\n⏳ calculating…"
        previous = state.get("task")
        if previous is not None and not previous.done():
            previous.cancel()
        state["task"] = asyncio.create_task(status.edit_text(text, reply_markup=menu()))
    return callback

def scan_menu(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Audio", callback_data=f"pick:{uid}:audio"), InlineKeyboardButton(text="🎬 Video", callback_data=f"pick:{uid}:video")],
        [InlineKeyboardButton(text="📷 Photos", callback_data=f"pick:{uid}:photo"), InlineKeyboardButton(text="📄 Documents", callback_data=f"pick:{uid}:document")],
        [InlineKeyboardButton(text="⬇️ All files", callback_data=f"pick:{uid}:all")],
        [InlineKeyboardButton(text="☑️ Select individually", callback_data=f"individual:{uid}"),],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{uid}")],
    ])

def confirm_menu(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm:{uid}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{uid}")],
    ])

def individual_menu(job):
    rows = []
    for mid, msg in list(job.candidates.items())[:50]:
        label = f"{'☑️' if mid in job.selected else '⬜'} #{mid} {kind(msg) or 'media'} {size(media_size(msg))}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"toggle:{job.owner_id}:{mid}")])
    rows += [
        [InlineKeyboardButton(text="✅ Continue", callback_data=f"individual_confirm:{job.owner_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{job.owner_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def create_job(uid, source, messages, status):
    if not messages:
        return await status.edit_text("No downloadable media messages were found.", reply_markup=menu())
    dest = destination(uid)
    if not dest:
        return await status.edit_text("Set a destination first with /setdestination or 🎯 Destination.", reply_markup=menu())
    candidates = {msg.id: msg for msg in messages}
    JOBS[uid] = Job(uid, source, candidates, dest, set(candidates) if len(candidates) == 1 else set())
    await status.edit_text(f"🔎 Scan Complete\n\n📦 Files: {len(messages)}\n📦 Total size: {size(sum(media_size(msg) for msg in messages))}\n\nChoose what to process:", reply_markup=scan_menu(uid))

async def send_destination(msg, dest, status=None, index=1, total_files=1):
    token = os.getenv("PLAYBOOK_API_TOKEN", "").strip()
    org = os.getenv("PLAYBOOK_ORG_SLUG", "").strip()
    filename = safe_filename(msg)
    started_at = time.monotonic()
    async def update(text):
        if status is not None:
            try:
                await status.edit_text(text, reply_markup=menu())
            except Exception:
                pass
    if status is not None:
        await update(f"📄 {filename}\n\n🚀 Starting file {index}/{total_files}\n⏳ calculating…")
    if not token or not org:
        callback = make_progress_callback(status, filename, "📤 Sending to destination…", started_at) if status is not None else None
        kwargs = {"caption": caption(msg)}
        if callback:
            kwargs["progress_callback"] = callback
        sent = await flood(lambda: user_client.send_file(dest, msg.media, **kwargs))
        if status is not None:
            await update(f"✅ {filename}\n\n📤 Sent successfully\n⏱️ {int(time.monotonic() - started_at)}s")
        return sent
    temp_dir = Path(os.getenv("TELEGRAM_TEMP_DIR", tempfile.gettempdir()))
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / filename
    try:
        download_cb = make_progress_callback(status, filename, "📥 Downloading from Telegram…", started_at) if status is not None else None
        download_kwargs = {"file": str(path)}
        if download_cb:
            download_kwargs["progress_callback"] = download_cb
        await user_client.download_media(msg, **download_kwargs)
        client = PlaybookClient(token=token, org_slug=org)
        upload_started = time.monotonic()
        await update(f"📄 {filename}\n\n☁️ Uploading to temporary storage…\n⏳ calculating…")
        asset_token = await client.upload_file(path, title=filename, progress_callback=(make_progress_callback(status, filename, "☁️ Uploading to temporary storage…", upload_started) if status is not None else None))
        asset = {}
        for poll in range(30):
            asset = await client.get_asset(asset_token)
            display_url = asset.get("display_url") or asset.get("url") or asset.get("download_url")
            status_name = str(asset.get("status") or asset.get("state") or "").lower()
            if display_url and status_name not in {"processing", "pending", "uploading"}:
                break
            await update(f"📄 {filename}\n\n☁️ Processing temporary file…\n⏳ {30 - poll}s")
            await asyncio.sleep(1)
        display_url = asset.get("display_url") or asset.get("url") or asset.get("download_url")
        if not display_url:
            raise PlaybookError("Playbook asset did not become downloadable in time")
        await flood(lambda: user_client.send_file(dest, display_url, caption=caption(msg)))
        try:
            await client.delete_asset(asset_token)
        except Exception:
            pass
        if status is not None:
            await update(f"✅ {filename}\n\n📤 Sent successfully\n⏱️ {int(time.monotonic() - started_at)}s")
        return asset
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

async def process(job, status):
    selected = [job.candidates[mid] for mid in job.selected if mid in job.candidates]
    success = 0
    failed = 0
    total = len(selected)
    for index, message in enumerate(selected, 1):
        try:
            await send_destination(message, job.destination, status=status, index=index, total_files=total)
            success += 1
        except Exception as exc:
            failed += 1
            if status is not None:
                await status.edit_text(f"❌ {safe_filename(message)}\n\n{type(exc).__name__}: {exc}", reply_markup=menu())
            await asyncio.sleep(0.2)
    JOBS.pop(job.owner_id, None)
    await status.edit_text(f"✅ Completed\n\n📤 Sent: {success}\n❌ Failed: {failed}\n📦 Total: {total}", reply_markup=menu())

# Handlers below this point are intentionally kept unchanged from the working bot implementation.
