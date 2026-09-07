"""HJ GROUPS Telegram Media Collector with persistent button UI."""
from __future__ import annotations
import asyncio, json, os, re, tempfile, time
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
            [KeyboardButton(text="🔐 Login"), KeyboardButton(text="📱 Session")],
            [KeyboardButton(text="🔗 Scan Link"), KeyboardButton(text="📦 Bulk Range")],
            [KeyboardButton(text="🎯 Destination"), KeyboardButton(text="📋 Select Files")],
            [KeyboardButton(text="❌ Cancel"), KeyboardButton(text="🚪 Logout")],
            [KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose a function or send a Telegram link",
    )

def allowed(uid):
    return bool(uid and uid in ALLOWED_USER_IDS)

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
    try:
        saved = await session_store.get_destination(uid)
    except Exception as exc:
        print(f"[Voroa] Destination load failed: {type(exc).__name__}: {exc}", flush=True)
        saved = None
    value = (saved or STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()
    if value:
        DESTINATIONS[str(uid)] = value
    return value

async def set_destination(uid, dest):
    value = dest.strip()
    DESTINATIONS[str(uid)] = value
    STATE[str(uid)] = value
    save_state()
    try:
        await session_store.set_destination(uid, value)
    except Exception as exc:
        print(f"[Voroa] Destination persistence failed: {type(exc).__name__}: {exc}", flush=True)
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
    # Accept: t.me/c/123/471-480 and t.me/c/123/471 480
    match = re.match(
        r"^https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/(\d+)\s*(?:-|\s)\s*(\d+)(?:\?.*)?$",
        value.strip(),
    )
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

async def resolve_message_peer(peer):
    # For t.me/c/<internal-id>/<message-id>, Telegram gives only the marked chat ID.
    # Telethon can resolve a negative -100... ID from its entity cache. If the entity
    # is not cached, walk dialogs until the matching channel/chat is found.
    try:
        return await asyncio.wait_for(user_client.get_input_entity(peer), timeout=SCAN_TIMEOUT_SECONDS)
    except (ValueError, TypeError):
        target_id = int(peer) if str(peer).lstrip("-").isdigit() else None
        if target_id is None:
            return await asyncio.wait_for(user_client.get_entity(peer), timeout=SCAN_TIMEOUT_SECONDS)
        async def find_in_dialogs():
            async for dialog in user_client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is not None and getattr(entity, "id", None) == abs(target_id) and getattr(dialog, "id", None) == target_id:
                    return entity
            return None
        entity = await asyncio.wait_for(find_in_dialogs(), timeout=SCAN_TIMEOUT_SECONDS)
        if entity is None:
            raise ValueError(f"Telegram channel/chat {peer} is not in the logged-in account's dialogs. Make sure the account has access to that channel.")
        return entity

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
        [InlineKeyboardButton(text="☑️ Select individually", callback_data=f"individual:{uid}")],
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
    await status.edit_text(
        f"🔎 Scan Complete\n\n📦 Files: {len(messages)}\n📦 Total size: {size(sum(media_size(msg) for msg in messages))}\n\nChoose what to process:",
        reply_markup=scan_menu(uid),
    )

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
        asset_token = await client.upload_file(
            path,
            title=filename,
            progress_callback=(make_progress_callback(status, filename, "☁️ Uploading to temporary storage…", upload_started) if status is not None else None),
        )
        asset = {}
        for poll in range(30):
            asset = await client.get_asset(asset_token)
            if not asset.get("is_skeleton", False):
                break
            if status is not None:
                elapsed = int(time.monotonic() - upload_started)
                await update(f"📄 {filename}\n\n☁️ Processing upload…\n⏳ {max(1, 60 - elapsed)}s left (estimate)")
            await asyncio.sleep(2)
        url = str(asset.get("display_url") or "").strip()
        if not url:
            raise PlaybookError(str(asset.get("source_error") or "Playbook asset has no display_url"))
        await update(f"📄 {filename}\n\n📤 Sending to destination…\n⏳ finalizing…")
        sent = await flood(lambda: user_client.send_file(dest, url, name=filename, caption=caption(msg)))
        try:
            await client.delete_asset(asset_token)
        except Exception:
            pass
        if status is not None:
            await update(f"✅ {filename}\n\n📤 Sent successfully\n⏱️ {int(time.monotonic() - started_at)}s")
        return sent
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

async def send_copy(msg, uid):
    sent = await flood(lambda: user_client.send_file(uid, msg.media, caption=caption(msg)))
    if BOT_DELETE_SECONDS:
        async def later():
            await asyncio.sleep(BOT_DELETE_SECONDS)
            try:
                await user_client.delete_messages(sent.peer_id, [sent.id])
            except Exception:
                pass
        task = asyncio.create_task(later())
        DELETE_TASKS.add(task)
        task.add_done_callback(DELETE_TASKS.discard)

async def process(job, status):
    async with SEM:
        selected = [job.candidates[mid] for mid in job.candidates if mid in job.selected]
        done = failed = 0
        total_files = len(selected)
        for index, msg in enumerate(selected, 1):
            filename = safe_filename(msg)
            try:
                await status.edit_text(f"📄 {filename}\n\n🚀 Starting {index}/{total_files}\n⏳ calculating…", reply_markup=menu())
                await send_destination(msg, job.destination, status=status, index=index, total_files=total_files)
                await send_copy(msg, job.owner_id)
                done += 1
            except Exception as exc:
                failed += 1
                print(f"File {getattr(msg, 'id', '?')} failed: {type(exc).__name__}: {exc}", flush=True)
                await status.edit_text(f"❌ {filename}\n\nFailed: {type(exc).__name__}: {exc}\n\n✅ Sent: {done} | ⚠️ Failed: {failed}", reply_markup=menu())
                continue
            await status.edit_text(f"✅ {filename}\n\nCompleted {index}/{total_files}\n✅ Sent: {done}\n⚠️ Failed: {failed}", reply_markup=menu())
        await status.edit_text(f"✅ Completed\n\n📦 Files processed: {total_files}\n✅ Sent: {done}\n⚠️ Failed: {failed}\n\nDestination: {job.destination}", reply_markup=menu())

@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    await message.answer(
        "HJ GROUPS Media Collector\n\nChoose a function from the buttons below or send a Telegram message link directly.",
        reply_markup=menu(),
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    await message.answer(
        "HJ GROUPS Media Collector\n\n🔗 Scan Link — send a Telegram message link. Bulk links like .../471-480 or .../471 480 are supported.\n📦 Bulk Range — send: @channel START_ID END_ID.\n📋 Select Files — use after a scan, then send: 25,31,44.\n🎯 Destination — send @username or chat ID.\n🔐 Login — send your Telegram phone number.\n📱 Session — check the saved session.\n🚪 Logout — remove the saved Telegram session.\n❌ Cancel — cancel the current job or pending input.",
        reply_markup=menu(),
    )

async def qr_login_flow(message: Message, uid: int):
    """Start an isolated QR login client so other bot actions cannot replace its auth key."""
    async with LOGIN_LOCK:
        qr_client = None
        keep_client = False
        try:
            qr_client = TelegramClient(StringSession(""), API_ID, API_HASH)
            await qr_client.connect()
            qr_login = await qr_client.qr_login()
            wait_task = asyncio.create_task(qr_login.wait(timeout=150))
            qr_path = Path(tempfile.gettempdir()) / f"telegram_qr_{uid}.png"
            img = qrcode.make(qr_login.url)
            img.save(qr_path)
            try:
                await message.answer_photo(
                    FSInputFile(qr_path),
                    caption=(
                        "🔐 Telegram QR Login\n\n"
                        "1. Open Telegram on your phone.\n"
                        "2. Settings → Devices → Link Desktop Device.\n"
                        "3. Scan this QR code.\n\n"
                        "The QR code expires automatically. Keep this chat open until login completes."
                    ),
                    reply_markup=menu(),
                )
            finally:
                try:
                    qr_path.unlink(missing_ok=True)
                except OSError:
                    pass

            try:
                await wait_task
                session = qr_client.session.save()
                await session_store.set(session)
                PENDING.pop(uid, None)
                await message.answer("Telegram account login successful via QR. ✅", reply_markup=menu())
            except SessionPasswordNeededError:
                QR_CLIENTS[uid] = qr_client
                keep_client = True
                PENDING[uid] = "2fa_qr"
                await message.answer(
                    "✅ QR approved. Your Telegram account has 2FA enabled. Enter the 2FA password here or use /2fa your_password.",
                    reply_markup=menu(),
                )
            except asyncio.TimeoutError:
                await message.answer("⏱️ QR code expired. Press 🔐 Login to generate a new QR code.", reply_markup=menu())
            except (AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError) as exc:
                QR_CLIENTS.pop(uid, None)
                PENDING.pop(uid, None)
                await message.answer("⚠️ The temporary Telegram QR session was invalidated. Press 🔐 Login and scan a new QR code.", reply_markup=menu())
            except Exception as exc:
                QR_CLIENTS.pop(uid, None)
                PENDING.pop(uid, None)
                await message.answer(f"QR login failed: {type(exc).__name__}: {exc}", reply_markup=menu())
        except Exception as exc:
            QR_CLIENTS.pop(uid, None)
            PENDING.pop(uid, None)
            await message.answer(f"Could not start QR login: {type(exc).__name__}: {exc}", reply_markup=menu())
        finally:
            if qr_client is not None and not keep_client:
                try:
                    if qr_client.is_connected():
                        await qr_client.disconnect()
                except Exception:
                    pass

@dp.message(Command("login"))
async def login_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    if not session_store.configured and not SESSION_STRING:
        return await message.answer("Session storage is not configured.", reply_markup=menu())
    await qr_login_flow(message, uid)

# QR_LOGIN_FIX_V1

@dp.message(Command("otp"))
async def otp_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        PENDING[uid] = "otp"
        return await message.answer("Enter the OTP number, for example 12345.", reply_markup=menu())
    pending = await session_store.get_login(uid)
    if not pending or not pending.get("phone") or not pending.get("phone_code_hash") or not pending.get("session_string"):
        return await message.answer("No login is waiting. Use 🔐 Login first.", reply_markup=menu())
    try:
        await rebuild(pending["session_string"])
        await user_client.sign_in(pending["phone"], parts[1].strip(), phone_code_hash=pending["phone_code_hash"])
        await session_store.set(user_client.session.save())
        await session_store.clear_login(uid)
        PENDING.pop(uid, None)
        await message.answer("Telegram account login successful. ✅", reply_markup=menu())
    except SessionPasswordNeededError:
        PENDING[uid] = "2fa"
        await message.answer("2FA enabled. Enter the Telegram 2FA password.", reply_markup=menu())
    except Exception as exc:
        await message.answer(f"OTP login failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.message(Command("2fa"))
async def twofa_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        PENDING[uid] = PENDING.get(uid) or "2fa"
        return await message.answer("Enter your Telegram 2FA password.", reply_markup=menu())
    if PENDING.get(uid) == "2fa_qr":
        qr_client = QR_CLIENTS.get(uid)
        if qr_client is None or not qr_client.is_connected():
            QR_CLIENTS.pop(uid, None)
            PENDING.pop(uid, None)
            return await message.answer("⚠️ QR login session is no longer active. Press 🔐 Login and scan a new QR code.", reply_markup=menu())
        try:
            await qr_client.sign_in(password=parts[1].strip())
            await session_store.set(qr_client.session.save())
            PENDING.pop(uid, None)
            QR_CLIENTS.pop(uid, None)
            await message.answer("Telegram account login successful via QR + 2FA. ✅", reply_markup=menu())
        except (AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError):
            PENDING.pop(uid, None)
            QR_CLIENTS.pop(uid, None)
            await message.answer("⚠️ Telegram invalidated the temporary QR session. Press 🔐 Login and scan a new QR code.", reply_markup=menu())
        except Exception as exc:
            await message.answer(f"2FA login failed: {type(exc).__name__}: {exc}", reply_markup=menu())
        return
    pending = await session_store.get_login(uid)
    if not pending or not pending.get("session_string"):
        return await message.answer("No 2FA login is waiting. Use 🔐 Login first.", reply_markup=menu())
    try:
        await rebuild(pending["session_string"])
        await user_client.sign_in(password=parts[1])
        await session_store.set(user_client.session.save())
        await session_store.clear_login(uid)
        PENDING.pop(uid, None)
        await message.answer("Telegram account login successful. ✅", reply_markup=menu())
    except Exception as exc:
        await message.answer(f"2FA login failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.message(Command("logout"))
async def logout_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    try:
        if user_client.is_connected():
            await user_client.log_out()
    finally:
        await session_store.clear()
        await session_store.clear_login(uid)
    PENDING.pop(uid, None)
    await message.answer("Telegram account logged out and saved session removed. ✅", reply_markup=menu())

@dp.message(Command("session"))
async def session_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    session = await saved_session()
    if not session:
        return await message.answer("No Telegram user session. Use 🔐 Login.", reply_markup=menu())
    try:
        if not user_client.is_connected():
            await rebuild(session)
        if await user_client.is_user_authorized():
            me = await user_client.get_me()
            await message.answer(f"Session active: {getattr(me, 'username', None) or me.id} ✅", reply_markup=menu())
        else:
            await message.answer("Session is not authorized. Use 🔐 Login.", reply_markup=menu())
    except Exception as exc:
        await message.answer(f"Session check failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.message(Command("setdestination"))
async def setdest_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        PENDING[uid] = "destination"
        return await message.answer("🎯 Send the destination username or chat ID, for example @mychannel or -1001234567890.", reply_markup=menu())
    value = await set_destination(uid, parts[1])
    PENDING.pop(uid, None)
    await message.answer(f"✅ Permanent destination saved: {value}\n\nYou can change it anytime with 🎯 Destination.", reply_markup=menu())

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    JOBS.pop(uid, None)
    PENDING.pop(uid, None)
    qr_client = QR_CLIENTS.pop(uid, None)
    if qr_client is not None:
        try:
            if qr_client.is_connected():
                await qr_client.disconnect()
        except Exception:
            pass
    await session_store.clear_login(uid)
    await message.answer("❌ Current job cancelled.", reply_markup=menu())

@dp.message(Command("range"))
async def range_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        PENDING[uid] = "range"
        return await message.answer("📦 Send the range as: @channel START_ID END_ID", reply_markup=menu())
    await load_destination(uid)
    if not destination(uid):
        return await message.answer("Set a destination first with 🎯 Destination.", reply_markup=menu())
    try:
        start_id, end_id = int(parts[2]), int(parts[3])
        if start_id <= 0 or end_id < start_id:
            raise ValueError("Invalid message ID range.")
        count = end_id - start_id + 1
        if count > MAX_BULK_MESSAGES:
            raise ValueError(f"Maximum range is {MAX_BULK_MESSAGES} messages.")
        await ensure_user_client()
        source = await asyncio.wait_for(user_client.get_entity(parts[1]), timeout=SCAN_TIMEOUT_SECONDS)
        status = await message.answer("🔎 Scanning messages...", reply_markup=menu())
        msgs = await asyncio.wait_for(user_client.get_messages(source, ids=list(range(start_id, end_id + 1))), timeout=SCAN_TIMEOUT_SECONDS)
        await create_job(uid, source, [msg for msg in msgs if msg and getattr(msg, "media", None)], status)
    except asyncio.TimeoutError:
        await message.answer("⏱️ Telegram did not respond within the scan timeout. Please try the range again.", reply_markup=menu())
    except (ValueError, RPCError) as exc:
        await message.answer(f"Could not scan range: {exc}", reply_markup=menu())
    except Exception as exc:
        print(f"Range scan failed: {type(exc).__name__}: {exc}", flush=True)
        await message.answer(f"❌ Range scan failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.message(Command("select"))
async def select_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        await message.answer("⛔ You are not authorized to use this bot.")
        return
    job = JOBS.get(uid)
    parts = (message.text or "").split(maxsplit=1)
    if not job:
        return await message.answer("No active scan. Use 🔗 Scan Link or 📦 Bulk Range first.", reply_markup=menu())
    if len(parts) != 2:
        PENDING[uid] = "select"
        return await message.answer("📋 Send the file message IDs as: 25,31,44", reply_markup=menu())
    try:
        ids = {int(value.strip()) for value in parts[1].split(",") if value.strip()}
    except ValueError:
        return await message.answer("Message IDs must be comma-separated numbers.", reply_markup=menu())
    missing = ids - set(job.candidates)
    if missing:
        return await message.answer("IDs not in current scan: " + ", ".join(map(str, sorted(missing)[:20])), reply_markup=menu())
    job.selected = ids
    PENDING.pop(uid, None)
    await message.answer(
        f"Selected {len(ids)} file(s).\nTotal size: {size(sum(media_size(job.candidates[i]) for i in ids))}\n\nDestination: {job.destination}\n\nConfirm?",
        reply_markup=confirm_menu(uid),
    )

@dp.message(F.text == "🔐 Login")
async def button_login(message: Message):
    return await login_cmd(message.model_copy(update={"text": "/login"}))

@dp.message(F.text == "📱 Session")
async def button_session(message: Message):
    return await session_cmd(message.model_copy(update={"text": "/session"}))

@dp.message(F.text == "🔗 Scan Link")
async def button_scan(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        return
    PENDING[uid] = "scan"
    await message.answer("🔗 Send a Telegram link. For bulk use .../471-480 or .../471 480.", reply_markup=menu())

@dp.message(F.text == "📦 Bulk Range")
async def button_range(message: Message):
    return await range_cmd(message.model_copy(update={"text": "/range"}))

@dp.message(F.text == "🎯 Destination")
async def button_destination(message: Message):
    return await setdest_cmd(message.model_copy(update={"text": "/setdestination"}))

@dp.message(F.text == "📋 Select Files")
async def button_select(message: Message):
    return await select_cmd(message.model_copy(update={"text": "/select"}))

@dp.message(F.text == "❌ Cancel")
async def button_cancel(message: Message):
    return await cancel_cmd(message.model_copy(update={"text": "/cancel"}))

@dp.message(F.text == "🚪 Logout")
async def button_logout(message: Message):
    return await logout_cmd(message.model_copy(update={"text": "/logout"}))

@dp.message(F.text == "ℹ️ Help")
async def button_help(message: Message):
    return await help_cmd(message.model_copy(update={"text": "/help"}))

@dp.message(F.text)
async def text_handler(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        return
    text = (message.text or "").strip()
    action = PENDING.get(uid)
    if action == "phone":
        return await login_cmd(message.model_copy(update={"text": f"/login {text}"}))
    if action == "otp":
        return await otp_cmd(message.model_copy(update={"text": f"/otp {text}"}))
    if action in {"2fa", "2fa_qr"}:
        return await twofa_cmd(message.model_copy(update={"text": f"/2fa {text}"}))
    if action == "destination":
        return await setdest_cmd(message.model_copy(update={"text": f"/setdestination {text}"}))
    if action == "range":
        return await range_cmd(message.model_copy(update={"text": f"/range {text}"}))
    if action == "select":
        return await select_cmd(message.model_copy(update={"text": f"/select {text}"}))
    if action == "scan":
        PENDING.pop(uid, None)
        if not re.match(r"^https?://(?:www\.)?t\.me/", text):
            return await message.answer("Send a valid Telegram t.me message link.", reply_markup=menu())
    if not re.match(r"^https?://(?:www\.)?t\.me/", text):
        return await message.answer("Send a Telegram t.me message link or use the buttons below.", reply_markup=menu())
    await load_destination(uid)
    if not destination(uid):
        return await message.answer("Set a destination first with 🎯 Destination.", reply_markup=menu())

    bulk = parse_bulk_link(text)
    status = await message.answer("🔎 Scanning bulk messages..." if bulk else "🔎 Scanning message...", reply_markup=menu())
    try:
        await ensure_user_client()
        if bulk:
            peer, start_id, end_id = bulk
            await status.edit_text(f"🔎 Resolving Telegram channel…\n📦 Range: {start_id}–{end_id}", reply_markup=menu())
            source = await asyncio.wait_for(resolve_message_peer(peer), timeout=SCAN_TIMEOUT_SECONDS)
            await status.edit_text(f"🔎 Fetching messages {start_id}–{end_id}…", reply_markup=menu())
            msgs = await asyncio.wait_for(
                user_client.get_messages(source, ids=list(range(start_id, end_id + 1))),
                timeout=SCAN_TIMEOUT_SECONDS,
            )
            media_msgs = [msg for msg in msgs if msg and getattr(msg, "media", None)]
            await create_job(uid, source, media_msgs, status)
            return

        peer, mid = parse_link(text)
        await status.edit_text(f"🔎 Resolving Telegram channel…\n📌 Message: {mid}", reply_markup=menu())
        source = await asyncio.wait_for(resolve_message_peer(peer), timeout=SCAN_TIMEOUT_SECONDS)
        await status.edit_text(f"🔎 Fetching message {mid}…", reply_markup=menu())
        msg = await asyncio.wait_for(user_client.get_messages(source, ids=mid), timeout=SCAN_TIMEOUT_SECONDS)
        if not msg or not getattr(msg, "media", None):
            return await status.edit_text("That message does not contain downloadable media.", reply_markup=menu())
        await create_job(uid, source, [msg], status)
    except asyncio.TimeoutError:
        await status.edit_text("⏱️ Scan timed out after 35 seconds.\n\nCheck that the logged-in Telegram account can open this channel and try again.", reply_markup=menu())
    except (ValueError, RPCError) as exc:
        await status.edit_text(f"Could not resolve that message: {exc}", reply_markup=menu())
    except Exception as exc:
        print(f"Scan failed: {type(exc).__name__}: {exc}", flush=True)
        await status.edit_text(f"❌ Scan failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.callback_query(F.data.startswith("pick:"))
async def pick(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return await callback.answer("Not authorized", show_alert=True)
    _, owner, typ = callback.data.split(":", 2)
    job = JOBS.get(int(owner))
    if not job or int(owner) != uid: return await callback.answer("Job expired", show_alert=True)
    job.selected = set(job.candidates) if typ == "all" else {mid for mid, msg in job.candidates.items() if kind(msg) == typ}
    if not job.selected: return await callback.answer("No files of that type", show_alert=True)
    await callback.message.edit_text(f"Ready to process {len(job.selected)} file(s).\n\nTotal size: {size(sum(media_size(job.candidates[i]) for i in job.selected))}\n\nDestination: {job.destination}\n\nConfirm?", reply_markup=confirm_menu(uid))
    await callback.answer()

@dp.callback_query(F.data.startswith("individual:"))
async def individual(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return await callback.answer("Not authorized", show_alert=True)
    job = JOBS.get(int(callback.data.split(":", 1)[1]))
    if not job or job.owner_id != uid: return await callback.answer("Job expired", show_alert=True)
    job.selected = set()
    await callback.message.edit_text("Select individual files.\n\nSelected: 0", reply_markup=individual_menu(job))
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle:"))
async def toggle(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return await callback.answer("Not authorized", show_alert=True)
    _, owner, mid_text = callback.data.split(":", 2)
    mid = int(mid_text)
    job = JOBS.get(int(owner))
    if not job or job.owner_id != uid or mid not in job.candidates: return await callback.answer("Job expired", show_alert=True)
    if mid in job.selected: job.selected.remove(mid)
    else: job.selected.add(mid)
    await callback.message.edit_reply_markup(reply_markup=individual_menu(job))
    await callback.answer(f"Selected: {len(job.selected)}")

@dp.callback_query(F.data.startswith("individual_confirm:"))
async def individual_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    job = JOBS.get(int(callback.data.split(":", 1)[1]))
    if not allowed(uid): return await callback.answer("Not authorized", show_alert=True)
    if not job or job.owner_id != uid: return await callback.answer("Job expired", show_alert=True)
    if not job.selected: return await callback.answer("Select at least one file", show_alert=True)
    await callback.message.edit_text(f"Selected {len(job.selected)} file(s).\nTotal size: {size(sum(media_size(job.candidates[i]) for i in job.selected))}\n\nDestination: {job.destination}\n\nConfirm?", reply_markup=confirm_menu(uid))
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm:"))
async def confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    job = JOBS.get(int(callback.data.split(":", 1)[1]))
    if not allowed(uid): return await callback.answer("Not authorized", show_alert=True)
    if not job or job.owner_id != uid: return await callback.answer("Job expired", show_alert=True)
    if not job.selected: return await callback.answer("Nothing selected", show_alert=True)
    await callback.answer("Started")
    try:
        await process(job, callback.message)
    finally:
        JOBS.pop(uid, None)

@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return await callback.answer("Not authorized", show_alert=True)
    owner = int(callback.data.split(":", 1)[1])
    if owner == uid:
        JOBS.pop(uid, None)
        PENDING.pop(uid, None)
        await session_store.clear_login(uid)
    await callback.message.edit_text("❌ Cancelled.", reply_markup=menu())
    await callback.answer()

async def configure_command_menu():
    commands = [
        BotCommand(command="start", description="Open main menu"),
        BotCommand(command="help", description="Show help"),
        BotCommand(command="login", description="Login Telegram account"),
        BotCommand(command="otp", description="Submit Telegram OTP"),
        BotCommand(command="2fa", description="Submit Telegram 2FA"),
        BotCommand(command="session", description="Check saved session"),
        BotCommand(command="setdestination", description="Set delivery destination"),
        BotCommand(command="range", description="Scan a message range"),
        BotCommand(command="select", description="Select scanned files"),
        BotCommand(command="cancel", description="Cancel current job"),
        BotCommand(command="logout", description="Logout Telegram account"),
    ]
    await bot.set_my_commands(commands)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    print("Telegram command menu configured.", flush=True)

async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise RuntimeError("Set Telegram API credentials.")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("Set TELEGRAM_ALLOWED_USER_IDS to at least one Telegram user ID.")
    await configure_command_menu()
    for uid in ALLOWED_USER_IDS:
        await load_destination(uid)
    try:
        session = await saved_session()
        if session:
            await rebuild(session)
            if await user_client.is_user_authorized():
                me = await user_client.get_me()
                print(f"Telethon connected as {getattr(me, 'username', None) or me.id}", flush=True)
            else:
                print("Saved Telegram session is not authorized; use /login", flush=True)
        else:
            print("Telethon user session not configured; use /login", flush=True)
        await dp.start_polling(bot)
    finally:
        for task in list(DELETE_TASKS):
            task.cancel()
        if DELETE_TASKS:
            await asyncio.gather(*DELETE_TASKS, return_exceptions=True)
        try:
            if user_client.is_connected():
                await user_client.disconnect()
        finally:
            try:
                await session_store.close()
            finally:
                await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
