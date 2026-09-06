"""HJ GROUPS Telegram Media Collector / Forwarder.

Uses aiogram for the bot UI and Telethon (MTProto) for reading/sending media.
The Telethon account must legitimately have access to the source and destination.
No flood-limit bypassing is attempted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE_NUMBER = os.getenv("TELEGRAM_PHONE_NUMBER", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_user_session")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
ALLOWED_USER_IDS = {
    int(value.strip()) for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit() and int(value.strip()) > 0
}
DEFAULT_DESTINATION = os.getenv("TELEGRAM_DESTINATION", "").strip()
BRANDING = os.getenv("TELEGRAM_BRANDING", "@hjgroups_1").strip()
BOT_DELETE_SECONDS = max(0, int(os.getenv("TELEGRAM_BOT_DELETE_SECONDS", "3600")))
MAX_BULK_MESSAGES = max(1, int(os.getenv("TELEGRAM_MAX_BULK_MESSAGES", "500")))
MAX_ACTIVE_JOBS = max(1, int(os.getenv("TELEGRAM_MAX_ACTIVE_JOBS", "1")))
STATE_FILE = Path(os.getenv("TELEGRAM_STATE_FILE", "telegram_media_state.json"))

ENV_DESTINATIONS: dict[str, str] = {}
for item in os.getenv("TELEGRAM_USER_DESTINATIONS", "").split("|"):
    if ":" in item:
        uid, destination = item.split(":", 1)
        if uid.strip().isdigit() and destination.strip():
            ENV_DESTINATIONS[uid.strip()] = destination.strip()

# StringSession is recommended for hosted deployment because it avoids storing
# a .session file on the server. A file session remains supported for local use.
if SESSION_STRING:
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
JOB_SEMAPHORE = asyncio.Semaphore(MAX_ACTIVE_JOBS)
DELETE_TASKS: set[asyncio.Task] = set()


@dataclass
class SelectionJob:
    owner_id: int
    source: Any
    candidates: dict[int, Any]
    destination: str
    selected: set[int]
    status_message_id: int | None = None


JOBS: dict[int, SelectionJob] = {}


def is_allowed(user_id: int | None) -> bool:
    return bool(user_id and user_id in ALLOWED_USER_IDS)


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_state(data: dict[str, str]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Could not persist destination state: {exc}")


USER_STATE = load_state()


def get_destination(user_id: int) -> str:
    return (USER_STATE.get(str(user_id)) or ENV_DESTINATIONS.get(str(user_id)) or DEFAULT_DESTINATION).strip()


def set_destination(user_id: int, destination: str) -> None:
    USER_STATE[str(user_id)] = destination.strip()
    save_state(USER_STATE)


def format_size(size: int | None) -> str:
    if not size:
        return "Unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "Unknown"


def media_kind(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    if type(media).__name__ == "MessageMediaPhoto":
        return "photo"
    document = getattr(media, "document", None)
    if document is None:
        return "other"
    mime = (getattr(document, "mime_type", "") or "").lower()
    names = {type(attr).__name__ for attr in (getattr(document, "attributes", []) or [])}
    if "DocumentAttributeAudio" in names or mime.startswith("audio/"):
        return "audio"
    if "DocumentAttributeVideo" in names or mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "photo"
    return "document"


def media_size(message: Any) -> int:
    document = getattr(getattr(message, "media", None), "document", None)
    return int(getattr(document, "size", 0) or 0)


def caption_for(message: Any) -> str:
    original = (getattr(message, "message", None) or "").strip()
    parts = [original] if original else []
    parts.append(f"📦 File Size: {format_size(media_size(message))}")
    if BRANDING:
        parts.append(BRANDING)
    return "\n\n".join(parts)[:1024]


def parse_message_link(value: str) -> tuple[str, int]:
    pattern = re.compile(r"^https?://(?:www\.)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]{3,}))/([0-9]+)(?:\?.*)?$")
    match = pattern.match(value.strip())
    if not match:
        raise ValueError("Invalid Telegram message link. Use a t.me/.../message_id link.")
    private_id, username, message_id = match.groups()
    return (f"-100{private_id}" if private_id else f"@{username}"), int(message_id)


async def with_flood_wait(factory: Callable[[], Awaitable[Any]]) -> Any:
    while True:
        try:
            return await factory()
        except FloodWaitError as exc:
            seconds = max(1, int(getattr(exc, "seconds", 1)))
            print(f"Telegram FloodWait: waiting {seconds}s")
            await asyncio.sleep(seconds + 1)


async def resolve_source_and_message(link: str) -> tuple[Any, Any]:
    peer, message_id = parse_message_link(link)
    source = await user_client.get_entity(peer)
    message = await user_client.get_messages(source, ids=message_id)
    if not message:
        raise ValueError("Message was not found or is not accessible by the Telethon account.")
    return source, message


async def scan_range(source: Any, start_id: int, end_id: int) -> list[Any]:
    if start_id <= 0 or end_id <= 0:
        raise ValueError("Message IDs must be positive.")
    if end_id < start_id:
        raise ValueError("End message ID must be greater than or equal to start ID.")
    if end_id - start_id + 1 > MAX_BULK_MESSAGES:
        raise ValueError(f"Maximum range is {MAX_BULK_MESSAGES} messages per job.")
    messages = await user_client.get_messages(source, ids=list(range(start_id, end_id + 1)))
    return [message for message in messages if message and getattr(message, "media", None)]


def summary(messages: list[Any]) -> str:
    counts = {"audio": 0, "video": 0, "photo": 0, "document": 0, "other": 0}
    total_size = 0
    for message in messages:
        kind = media_kind(message) or "other"
        counts[kind] = counts.get(kind, 0) + 1
        total_size += media_size(message)
    return (
        "🔎 Scan Complete\n\n"
        f"🎵 Audio: {counts['audio']}\n"
        f"🎬 Video: {counts['video']}\n"
        f"📷 Photos: {counts['photo']}\n"
        f"📄 Documents: {counts['document']}\n"
        f"📦 Total size: {format_size(total_size)}\n\n"
        "Choose what to process:"
    )


def scan_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Audio", callback_data=f"pick:{user_id}:audio"), InlineKeyboardButton(text="🎬 Video", callback_data=f"pick:{user_id}:video")],
        [InlineKeyboardButton(text="📷 Photos", callback_data=f"pick:{user_id}:photo"), InlineKeyboardButton(text="📄 Documents", callback_data=f"pick:{user_id}:document")],
        [InlineKeyboardButton(text="⬇️ All files", callback_data=f"pick:{user_id}:all")],
        [InlineKeyboardButton(text="☑️ Select individually", callback_data=f"individual:{user_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{user_id}")],
    ])


def confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm:{user_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{user_id}")],
    ])


def individual_keyboard(job: SelectionJob) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for message_id, media_message in list(job.candidates.items())[:50]:
        mark = "☑️" if message_id in job.selected else "⬜"
        label = f"{mark} #{message_id} {media_kind(media_message) or 'media'} {format_size(media_size(media_message))}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"toggle:{job.owner_id}:{message_id}")])
    if len(job.candidates) > 50:
        rows.append([InlineKeyboardButton(text="ℹ️ More: use /select 101,102,...", callback_data=f"noop:{job.owner_id}")])
    rows.append([InlineKeyboardButton(text="✅ Continue", callback_data=f"individual_confirm:{job.owner_id}")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{job.owner_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def schedule_delete(sent_message: Any) -> None:
    await asyncio.sleep(BOT_DELETE_SECONDS)
    try:
        await with_flood_wait(lambda: user_client.delete_messages(sent_message.peer_id, [sent_message.id]))
    except Exception as exc:
        print(f"Bot-chat auto-delete failed: {type(exc).__name__}: {exc}")


def track_delete_task(task: asyncio.Task) -> None:
    DELETE_TASKS.add(task)
    task.add_done_callback(DELETE_TASKS.discard)


async def send_to_destination(message: Any, destination: str) -> Any:
    return await with_flood_wait(lambda: user_client.send_file(
        destination, message.media, caption=caption_for(message)
    ))


async def send_to_bot_chat(message: Any, bot_chat_id: int) -> Any:
    sent = await with_flood_wait(lambda: user_client.send_file(
        bot_chat_id, message.media, caption=caption_for(message)
    ))
    if BOT_DELETE_SECONDS > 0:
        track_delete_task(asyncio.create_task(schedule_delete(sent)))
    return sent


async def process_job(job: SelectionJob, status_message: Message) -> None:
    async with JOB_SEMAPHORE:
        selected = [job.candidates[mid] for mid in job.candidates if mid in job.selected]
        total, done, failed = len(selected), 0, 0
        await status_message.edit_text(f"⏳ Processing 0/{total} files...")
        for media_message in selected:
            message_id = getattr(media_message, "id", "?")
            try:
                await send_to_destination(media_message, job.destination)
                await send_to_bot_chat(media_message, job.owner_id)
                done += 1
            except Exception as exc:
                failed += 1
                print(f"Message {message_id} failed: {type(exc).__name__}: {exc}")
            await status_message.edit_text(
                f"⏳ Processing {done + failed}/{total} files...\n"
                f"✅ Sent: {done}\n⚠️ Failed: {failed}"
            )
        minutes = BOT_DELETE_SECONDS // 60
        await status_message.edit_text(
            f"✅ Completed\n\nSent: {done}\nFailed: {failed}\n\n"
            f"Destination: {job.destination}\nBot-chat copies auto-delete after {minutes} minutes."
        )


async def create_job_from_messages(user_id: int, source: Any, messages: list[Any], status: Message) -> None:
    if not messages:
        await status.edit_text("No downloadable media messages were found.")
        return
    destination = get_destination(user_id)
    if not destination:
        await status.edit_text("Set a destination first with /setdestination <chat_id_or_username>")
        return
    candidates = {message.id: message for message in messages}
    JOBS[user_id] = SelectionJob(
        owner_id=user_id, source=source, candidates=candidates, destination=destination,
        selected=set(candidates) if len(candidates) == 1 else set(), status_message_id=status.message_id,
    )
    await status.edit_text(summary(messages), reply_markup=scan_keyboard(user_id))


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        return
    destination = get_destination(user_id)
    await message.answer(
        "HJ GROUPS Media Collector\n\n"
        "Send a Telegram message link to scan one media message.\n"
        "For bulk: /range <chat_or_link> <start_id> <end_id>\n\n"
        f"Destination: {destination or 'not configured'}\n\n"
        "Commands:\n"
        "/setdestination <chat_id_or_username>\n"
        "/range <chat_or_message_link> <start_id> <end_id>\n"
        "/select <message_id,message_id,...>\n"
        "/cancel"
    )


@dp.message(Command("setdestination"))
async def set_destination_handler(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /setdestination @channel_or_chat_id")
        return
    set_destination(user_id, parts[1].strip())
    await message.answer(f"✅ Destination saved: {parts[1].strip()}")


@dp.message(Command("cancel"))
async def cancel_handler(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        return
    JOBS.pop(user_id, None)
    await message.answer("❌ Current job cancelled.")


@dp.message(Command("range"))
async def range_handler(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer("Usage: /range @channel_or_link START_ID END_ID")
        return
    if not get_destination(user_id):
        await message.answer("Set a destination first with /setdestination <chat_id_or_username>")
        return
    try:
        source_ref = parts[1]
        if source_ref.startswith("http"):
            peer, _ = parse_message_link(source_ref)
            source = await user_client.get_entity(peer)
        else:
            source = await user_client.get_entity(source_ref)
        status = await message.answer("🔎 Scanning messages...")
        messages = await scan_range(source, int(parts[2]), int(parts[3]))
        await create_job_from_messages(user_id, source, messages, status)
    except (ValueError, RPCError) as exc:
        await message.answer(f"Could not scan range: {exc}")


@dp.message(Command("select"))
async def select_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        return
    job = JOBS.get(user_id)
    if not job:
        await message.answer("No active scan. Send a link or use /range first.")
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) != 2:
        await message.answer("Usage: /select 25,31,44")
        return
    try:
        ids = {int(value.strip()) for value in raw[1].split(",") if value.strip()}
    except ValueError:
        await message.answer("Message IDs must be comma-separated numbers.")
        return
    missing = ids - set(job.candidates)
    if missing:
        await message.answer("IDs not in current scan: " + ", ".join(map(str, sorted(missing)[:20])))
        return
    job.selected = ids
    total_size = sum(media_size(job.candidates[mid]) for mid in ids)
    await message.answer(
        f"Selected {len(ids)} file(s).\nTotal size: {format_size(total_size)}\n\n"
        f"Destination: {job.destination}\n\nConfirm?", reply_markup=confirm_keyboard(user_id)
    )


@dp.message(F.text)
async def link_handler(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_allowed(user_id):
        return
    text = (message.text or "").strip()
    if not re.match(r"^https?://(?:www\.)?t\.me/", text):
        await message.answer("Send a Telegram t.me message link, or use /range <chat> <start> <end>.")
        return
    if not get_destination(user_id):
        await message.answer("Set a destination first with /setdestination <chat_id_or_username>")
        return
    status = await message.answer("🔎 Scanning message...")
    try:
        source, source_message = await resolve_source_and_message(text)
        if not getattr(source_message, "media", None):
            await status.edit_text("That message does not contain downloadable media.")
            return
        await create_job_from_messages(user_id, source, [source_message], status)
    except (ValueError, RPCError) as exc:
        await status.edit_text(f"Could not resolve that message: {exc}")


@dp.callback_query(F.data.startswith("pick:"))
async def pick_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return
    _, owner_text, kind = callback.data.split(":", 2)
    owner_id = int(owner_text)
    job = JOBS.get(owner_id)
    if not job or owner_id != user_id:
        await callback.answer("Job expired", show_alert=True)
        return
    job.selected = set(job.candidates) if kind == "all" else {
        mid for mid, media_message in job.candidates.items() if media_kind(media_message) == kind
    }
    if not job.selected:
        await callback.answer("No files of that type", show_alert=True)
        return
    total_size = sum(media_size(job.candidates[mid]) for mid in job.selected)
    await callback.message.edit_text(
        f"Ready to process {len(job.selected)} file(s).\n\nTotal size: {format_size(total_size)}\n\n"
        f"Destination: {job.destination}\n\nConfirm?", reply_markup=confirm_keyboard(user_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("individual:"))
async def individual_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return
    owner_id = int(callback.data.split(":", 1)[1])
    job = JOBS.get(owner_id)
    if not job or owner_id != user_id:
        await callback.answer("Job expired", show_alert=True)
        return
    job.selected = set()
    await callback.message.edit_text("Select individual files.\n\nSelected: 0", reply_markup=individual_keyboard(job))
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return
    _, owner_text, message_text = callback.data.split(":", 2)
    owner_id, message_id = int(owner_text), int(message_text)
    job = JOBS.get(owner_id)
    if not job or owner_id != user_id or message_id not in job.candidates:
        await callback.answer("Job expired", show_alert=True)
        return
    if message_id in job.selected:
        job.selected.remove(message_id)
    else:
        job.selected.add(message_id)
    await callback.message.edit_reply_markup(reply_markup=individual_keyboard(job))
    await callback.answer(f"Selected: {len(job.selected)}")


@dp.callback_query(F.data.startswith("individual_confirm:"))
async def individual_confirm_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return
    owner_id = int(callback.data.split(":", 1)[1])
    job = JOBS.get(owner_id)
    if not job or owner_id != user_id:
        await callback.answer("Job expired", show_alert=True)
        return
    if not job.selected:
        await callback.answer("Select at least one file", show_alert=True)
        return
    total_size = sum(media_size(job.candidates[mid]) for mid in job.selected)
    await callback.message.edit_text(
        f"Selected {len(job.selected)} file(s).\nTotal size: {format_size(total_size)}\n\n"
        f"Destination: {job.destination}\n\nConfirm?", reply_markup=confirm_keyboard(user_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return
    owner_id = int(callback.data.split(":", 1)[1])
    job = JOBS.get(owner_id)
    if not job or owner_id != user_id:
        await callback.answer("Job expired", show_alert=True)
        return
    if not job.selected:
        await callback.answer("Nothing selected", show_alert=True)
        return
    await callback.answer("Started")
    try:
        await process_job(job, callback.message)
    finally:
        JOBS.pop(user_id, None)


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return
    owner_id = int(callback.data.split(":", 1)[1])
    if owner_id == user_id:
        JOBS.pop(user_id, None)
    await callback.message.edit_text("❌ Cancelled.")
    await callback.answer()


@dp.callback_query(F.data.startswith("noop:"))
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer("Use /select 101,102,... for IDs beyond the first 50.", show_alert=True)


async def run_telethon() -> None:
    if SESSION_STRING:
        await user_client.start()
    else:
        await user_client.start(phone=PHONE_NUMBER)
    if not await user_client.is_user_authorized():
        raise RuntimeError("Telethon account is not authorized. Create a session first.")
    me = await user_client.get_me()
    print(f"Telethon connected as {getattr(me, 'username', None) or me.id}")
    await user_client.run_until_disconnected()


async def run_bot() -> None:
    print("aiogram polling started")
    await dp.start_polling(bot)


async def main() -> None:
    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN.")
    if not SESSION_STRING and not PHONE_NUMBER:
        raise RuntimeError("Set TELEGRAM_SESSION_STRING for hosting or TELEGRAM_PHONE_NUMBER for local first login.")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("Set TELEGRAM_ALLOWED_USER_IDS to at least one Telegram user ID.")
    telethon_task = asyncio.create_task(run_telethon(), name="telethon-user-client")
    bot_task = asyncio.create_task(run_bot(), name="aiogram-bot")
    try:
        await asyncio.gather(telethon_task, bot_task)
    finally:
        for task in (telethon_task, bot_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(telethon_task, bot_task, return_exceptions=True)
        if user_client.is_connected():
            await user_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
