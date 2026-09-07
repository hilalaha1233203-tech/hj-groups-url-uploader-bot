"""HJ GROUPS Telegram Media Collector with persistent button UI."""
from __future__ import annotations
import asyncio, json, os, re, tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telegram_session_store import session_store
from playbook_client import PlaybookClient, PlaybookError

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
    return (STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()

def set_destination(uid, dest):
    STATE[str(uid)] = dest.strip()
    save_state()

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
    await user_client.connect()

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
        return await status.edit_text("Set a destination first with 🎯 Destination.", reply_markup=menu())
    candidates = {msg.id: msg for msg in messages}
    JOBS[uid] = Job(uid, source, candidates, dest, set(candidates) if len(candidates) == 1 else set())
    await status.edit_text(
        f"🔎 Scan Complete\n\n📦 Files: {len(messages)}\n📦 Total size: {size(sum(media_size(msg) for msg in messages))}\n\nChoose what to process:",
        reply_markup=scan_menu(uid),
    )

async def send_destination(msg, dest):
    token = os.getenv("PLAYBOOK_API_TOKEN", "").strip()
    org = os.getenv("PLAYBOOK_ORG_SLUG", "").strip()
    if not token or not org:
        return await flood(lambda: user_client.send_file(dest, msg.media, caption=caption(msg)))
    temp_dir = Path(os.getenv("TELEGRAM_TEMP_DIR", tempfile.gettempdir()))
    temp_dir.mkdir(parents=True, exist_ok=True)
    name = getattr(getattr(msg, "file", None), "name", None) or f"telegram-{getattr(msg, 'id', 'media')}"
    path = temp_dir / name
    try:
        await user_client.download_media(msg, file=str(path))
        client = PlaybookClient(token=token, org_slug=org)
        asset_token = await client.upload_file(path, title=name)
        asset = {}
        for _ in range(30):
            asset = await client.get_asset(asset_token)
            if not asset.get("is_skeleton", False):
                break
            await asyncio.sleep(2)
        url = str(asset.get("display_url") or "").strip()
        if not url:
            raise PlaybookError(str(asset.get("source_error") or "Playbook asset has no display_url"))
        sent = await flood(lambda: user_client.send_file(dest, url, name=name, caption=caption(msg)))
        try:
            await client.delete_asset(asset_token)
        except Exception:
            pass
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
        for msg in selected:
            try:
                await send_destination(msg, job.destination)
                await send_copy(msg, job.owner_id)
                done += 1
            except Exception as exc:
                failed += 1
                print(f"File {getattr(msg, 'id', '?')} failed: {type(exc).__name__}: {exc}")
            await status.edit_text(f"⏳ Processing {done + failed}/{len(selected)} files...\n✅ Sent: {done}\n⚠️ Failed: {failed}")
        await status.edit_text(f"✅ Completed\n\nSent: {done}\nFailed: {failed}\n\nDestination: {job.destination}", reply_markup=menu())

@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid):
        return
    await message.answer(
        "HJ GROUPS Media Collector\n\nAll functions are available as buttons below.\n\n🔗 Scan Link — one media message\n📦 Bulk Range — multiple messages\n📋 Select Files — choose files\n🎯 Destination — delivery target\n🔐 Login / 📱 Session / 🚪 Logout — Telegram account controls",
        reply_markup=menu(),
    )

@dp.message(F.text == "ℹ️ Help")
async def help_button(message: Message):
    await start(message)

@dp.message(F.text == "🔗 Scan Link")
async def scan_button(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    PENDING[uid] = "link"
    await message.answer("🔗 Send the Telegram message link now.", reply_markup=menu())

@dp.message(F.text == "📦 Bulk Range")
async def bulk_button(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    PENDING[uid] = "range"
    await message.answer("📦 Send: @channel START_ID END_ID\nExample: @mychannel 100 150", reply_markup=menu())

@dp.message(F.text == "📋 Select Files")
async def select_button(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    PENDING[uid] = "select"
    await message.answer("📋 Send comma-separated message IDs.\nExample: 101,102,105", reply_markup=menu())

@dp.message(F.text == "🎯 Destination")
async def dest_button(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    PENDING[uid] = "destination"
    await message.answer("🎯 Send @username or chat ID.", reply_markup=menu())

@dp.message(F.text == "🔐 Login")
async def login_button(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    PENDING[uid] = "login"
    await message.answer("🔐 Send your Telegram phone number, e.g. +919876543210", reply_markup=menu())

@dp.message(F.text == "📱 Session")
async def session_button(message: Message):
    await session_cmd(message)

@dp.message(F.text == "🚪 Logout")
async def logout_button(message: Message):
    await logout_cmd(message)

@dp.message(F.text == "❌ Cancel")
async def cancel_button(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    JOBS.pop(uid, None)
    PENDING.pop(uid, None)
    await session_store.clear_login(uid)
    await message.answer("❌ Current action cancelled.", reply_markup=menu())

@dp.message(Command("login"))
async def login_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.answer("Usage: /login +91xxxxxxxxxx", reply_markup=menu())
    if not session_store.configured and not SESSION_STRING:
        return await message.answer("Session storage is not configured.", reply_markup=menu())
    phone = parts[1].strip()
    async with LOGIN_LOCK:
        try:
            # Always create a fresh unauthenticated Telethon session for the OTP flow.
            await rebuild("")
            sent = await user_client.send_code_request(phone)
            # Persist both the OTP state and the temporary MTProto session. This makes
            # /otp survive a worker restart/redeploy between the two messages.
            await session_store.set_login(uid, phone, sent.phone_code_hash, user_client.session.save())
            PENDING[uid] = "otp"
            await message.answer("OTP sent. Reply with /otp 12345", reply_markup=menu())
        except Exception as exc:
            await session_store.clear_login(uid)
            await message.answer(f"Login failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.message(Command("otp"))
async def otp_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.answer("Usage: /otp 12345", reply_markup=menu())
    pending = await session_store.get_login(uid)
    if not pending or not pending.get("phone") or not pending.get("phone_code_hash") or not pending.get("session_string"):
        return await message.answer("No login is waiting. Use 🔐 Login.", reply_markup=menu())
    try:
        await rebuild(pending["session_string"])
        await user_client.sign_in(pending["phone"], parts[1].strip(), phone_code_hash=pending["phone_code_hash"])
        await session_store.set(user_client.session.save())
        await session_store.clear_login(uid)
        PENDING.pop(uid, None)
        await message.answer("Telegram account login successful. ✅", reply_markup=menu())
    except SessionPasswordNeededError:
        PENDING[uid] = "2fa"
        await message.answer("2FA enabled. Reply with /2fa your_password", reply_markup=menu())
    except Exception as exc:
        await message.answer(f"OTP login failed: {type(exc).__name__}: {exc}", reply_markup=menu())

@dp.message(Command("2fa"))
async def twofa_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.answer("Usage: /2fa your_password", reply_markup=menu())
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
    if not allowed(uid): return
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
    if not allowed(uid): return
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
    if not allowed(uid): return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.answer("Usage: /setdestination @channel_or_chat_id", reply_markup=menu())
    set_destination(uid, parts[1])
    PENDING.pop(uid, None)
    await message.answer(f"✅ Destination saved: {parts[1]}", reply_markup=menu())

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    JOBS.pop(uid, None)
    PENDING.pop(uid, None)
    await session_store.clear_login(uid)
    await message.answer("❌ Current job cancelled.", reply_markup=menu())

@dp.message(Command("range"))
async def range_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    parts = (message.text or "").split()
    if len(parts) != 4:
        return await message.answer("Usage: /range @channel START_ID END_ID", reply_markup=menu())
    if not destination(uid):
        return await message.answer("Set a destination first with 🎯 Destination.", reply_markup=menu())
    try:
        start_id, end_id = int(parts[2]), int(parts[3])
        if start_id <= 0 or end_id < start_id:
            raise ValueError("Invalid message ID range.")
        count = end_id - start_id + 1
        if count > MAX_BULK_MESSAGES:
            raise ValueError(f"Maximum range is {MAX_BULK_MESSAGES} messages.")
        source = await user_client.get_entity(parts[1])
        status = await message.answer("🔎 Scanning messages...", reply_markup=menu())
        msgs = await user_client.get_messages(source, ids=list(range(start_id, end_id + 1)))
        await create_job(uid, source, [msg for msg in msgs if msg and getattr(msg, "media", None)], status)
    except (ValueError, RPCError) as exc:
        await message.answer(f"Could not scan range: {exc}", reply_markup=menu())

@dp.message(Command("select"))
async def select_cmd(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    job = JOBS.get(uid)
    parts = (message.text or "").split(maxsplit=1)
    if not job:
        return await message.answer("No active scan. Use 🔗 Scan Link or 📦 Bulk Range first.", reply_markup=menu())
    if len(parts) != 2:
        return await message.answer("Usage: /select 25,31,44", reply_markup=menu())
    try:
        ids = {int(value.strip()) for value in parts[1].split(",") if value.strip()}
    except ValueError:
        return await message.answer("Message IDs must be comma-separated numbers.", reply_markup=menu())
    missing = ids - set(job.candidates)
    if missing:
        return await message.answer("IDs not in current scan: " + ", ".join(map(str, sorted(missing)[:20])), reply_markup=menu())
    job.selected = ids
    await message.answer(
        f"Selected {len(ids)} file(s).\nTotal size: {size(sum(media_size(job.candidates[i]) for i in ids))}\n\nDestination: {job.destination}\n\nConfirm?",
        reply_markup=confirm_menu(uid),
    )

@dp.message(F.text)
async def text_handler(message: Message):
    uid = message.from_user.id if message.from_user else None
    if not allowed(uid): return
    text = (message.text or "").strip()
    action = PENDING.get(uid)
    if action == "login":
        PENDING.pop(uid, None)
        return await login_cmd(message.model_copy(update={"text": f"/login {text}"}))
    if action == "otp":
        return await otp_cmd(message.model_copy(update={"text": f"/otp {text}"}))
    if action == "2fa":
        return await twofa_cmd(message.model_copy(update={"text": f"/2fa {text}"}))
    if action == "destination":
        PENDING.pop(uid, None)
        return await setdest_cmd(message.model_copy(update={"text": f"/setdestination {text}"}))
    if action == "range":
        PENDING.pop(uid, None)
        return await range_cmd(message.model_copy(update={"text": f"/range {text}"}))
    if action == "select":
        PENDING.pop(uid, None)
        return await select_cmd(message.model_copy(update={"text": f"/select {text}"}))
    if not re.match(r"^https?://(?:www\.)?t\.me/", text):
        return await message.answer("Send a Telegram t.me message link or choose a button below.", reply_markup=menu())
    if not destination(uid):
        return await message.answer("Set a destination first with 🎯 Destination.", reply_markup=menu())
    status = await message.answer("🔎 Scanning message...", reply_markup=menu())
    try:
        peer, mid = parse_link(text)
        source = await user_client.get_entity(peer)
        msg = await user_client.get_messages(source, ids=mid)
        if not msg or not getattr(msg, "media", None):
            return await status.edit_text("That message does not contain downloadable media.", reply_markup=menu())
        await create_job(uid, source, [msg], status)
    except (ValueError, RPCError) as exc:
        await status.edit_text(f"Could not resolve that message: {exc}", reply_markup=menu())

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

async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise RuntimeError("Set Telegram API credentials.")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("Set TELEGRAM_ALLOWED_USER_IDS to at least one Telegram user ID.")
    session = await saved_session()
    if session:
        await rebuild(session)
        if await user_client.is_user_authorized():
            me = await user_client.get_me()
            print(f"Telethon connected as {getattr(me, 'username', None) or me.id}")
        else:
            print("Saved Telegram session is not authorized; use /login")
    else:
        print("Telethon user session not configured; use /login")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
