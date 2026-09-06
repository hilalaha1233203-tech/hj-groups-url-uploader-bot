"""
HJ GROUPS Telegram Media Collector / Forwarder

Architecture
------------
* aiogram Bot API: UI, commands and inline buttons.
* Telethon user client: reads chats that the logged-in personal account can access
  and sends media to the configured destination and the bot chat.
* Both clients run concurrently on one asyncio event loop.

Supported workflow
------------------
1. Send a Telegram message link to the bot.
2. Or send a chat reference plus a message range, e.g. /range @channel 25 100.
3. The bot scans the requested messages and shows media-type totals.
4. Choose Audio / Video / Photo / Document / All / Select individually.
5. After confirmation, media is sent to the configured destination and also to
   the bot chat. Bot-chat copies are automatically deleted after 60 minutes.
6. Captions contain the original caption (when present), file size, and
   @hjgroups_1 on the last line.

Important limitations
---------------------
* The personal Telethon account must have access to the source chat.
* Protected content / chats that prohibit forwarding or saving can fail by design.
* Direct Telegram MTProto sending is used for large files; this avoids the
  normal Bot API 50 MB upload limit. Telegram's current MTProto upload limits
  are determined by the account's current Telegram configuration (including
  Premium). Do not treat 4 GB as a universal guarantee.
* No flood-limit bypass is attempted. FloodWait is respected and the job waits.
* Real credentials and the Telethon .session file must never be committed to GitHub.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE_NUMBER = os.getenv("TELEGRAM_PHONE_NUMBER", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Comma-separated Telegram user IDs allowed to use the bot.
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "0").split(",")
    if x.strip().isdigit() and int(x.strip()) > 0
}

DEFAULT_DESTINATION = os.getenv("TELEGRAM_DESTINATION", "")
BRANDING = os.getenv("TELEGRAM_BRANDING", "@hjgroups_1")
BOT_DELETE_SECONDS = int(os.getenv("TELEGRAM_BOT_DELETE_SECONDS", "3600"))
MAX_BULK_MESSAGES = int(os.getenv("TELEGRAM_MAX_BULK_MESSAGES", "500"))
STATE_FILE = Path(os.getenv("TELEGRAM_STATE_FILE", "telegram_media_state.json"))
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_user_session")
MAX_ACTIVE_JOBS = int(os.getenv("TELEGRAM_MAX_ACTIVE_JOBS", "1"))
JOB_SEMAPHORE = asyncio.Semaphore(MAX_ACTIVE_JOBS)

user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dataclass
class SelectionJob:
    owner_id: int
    source: Any
    message_ids: list[int]
    candidates: dict[int, Any]
    destination: str
    bot_message_id: int | None = None
    selected: set[int] | None = None


JOBS: dict[int, SelectionJob] = {}


def is_allowed(user_id: int | None) -> bool:
    return bool(user_id and user_id in ALLOWED_USER_IDS)


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data: dict[str, str]) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


USER_STATE = load_state()


def get_destination(user_id: int) -> str:
    return USER_STATE.get(str(user_id), DEFAULT_DESTINATION).strip()


def set_destination(user_id: int, destination: str) -> None:
    USER_STATE[str(user_id)] = destination.strip()
    save_state(USER_STATE)


def format_size(size: int | None) -> str:
    if not size:
        return "Unknown"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "Unknown"


def media_kind(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None

    document = getattr(media, "document", None)
    mime = (getattr(document, "mime_type", "") or "").lower()
    attrs = getattr(document, "attributes", []) or []

    has_audio = any(type(a).__name__ == "DocumentAttributeAudio" for a in attrs)
    has_video = any(type(a).__name__ == "DocumentAttributeVideo" for a in attrs)
    if has_audio or mime.startswith("audio/"):
        return "audio"
    if has_video or mime.startswith("video/"):
        return "video"
    if mime.startswith("image/") or type(media).__name__ == "MessageMediaPhoto":
        return "photo"
    if document is not None:
        return "document"
    return "other"


def media_size(message: Any) -> int:
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    return int(getattr(document, "size", 0) or 0)


def media_name(message: Any) -> str:
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    for attr in getattr(document, "attributes", []) or []:
        filename = getattr(attr, "file_name", None)
        if filename:
            return filename
    return f"telegram_{message.id}.{media_kind(message) or 'media'}"


def caption_for(message: Any) -> str:
    original = (getattr(message, "message", None) or "").strip()
    size = format_size(media_size(message))
    parts = []
    if original:
        parts.append(original)
    parts.append(f"📦 File Size: {size}")
    parts.append(BRANDING)
    return "\n\n".join(parts)[:1024]


def link_to_source(link: str) -> tuple[str, int]:
    """Parse common t.me message links into (peer reference, message id)."""
    value = link.strip()
    pattern = re.compile(r"https?://t\.me/(?:c/(\d+)|([A-Za-z0-9_]+)/)(\d+)(?:\?.*)?$")
    match = pattern.match(value)
    if not match:
        raise ValueError("Invalid Telegram message link. Use a t.me message link.")

    private_id, username, message_id_text = match.groups()
    message_id = int(message_id_text)
    peer = f"-100{private_id}" if private_id else f"@{username}"
    return peer, message_id


def build_scan_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Audio", callback_data=f"pick:{job_id}:audio"), InlineKeyboardButton(text="🎬 Video", callback_data=f"pick:{job_id}:video")],
            [InlineKeyboardButton(text="📷 Photos", callback_data=f"pick:{job_id}:photo"), InlineKeyboardButton(text="📄 Documents", callback_data=f"pick:{job_id}:document")],
            [InlineKeyboardButton(text="⬇️ All files", callback_data=f"pick:{job_id}:all")],
            [InlineKeyboardButton(text="☑️ Select individually", callback_data=f"select:{job_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{job_id}")],
        ]
    )


def build_confirm_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data=f"confirm:{job_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel:{job_id}")],
        ]
    )


def build_individual_keyboard(job: SelectionJob) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    selected = job.selected or set()
    for mid, msg in list(job.candidates.items())[:50]:
        mark = "☑️" if mid in selected else "⬜"
        label = f"{mark} #{mid} {media_kind(msg) or 'media'} {format_size(media_size(msg))}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"toggle:{id(job)}:{mid}")])
    rows.append([InlineKeyboardButton(text="✅ Download selected", callback_data=f"confirm_select:{id(job)}")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_obj:{id(job)}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scan_summary(messages: list[Any]) -> str:
    counts = {"audio": 0, "video": 0, "photo": 0, "document": 0, "other": 0}
    total = 0
    for msg in messages:
        kind = media_kind(msg)
        if kind:
            counts[kind] += 1
            total += media_size(msg)
    return (
        "🔎 Scan Complete\n\n"
        f"🎵 Audio: {counts['audio']}\n"
        f"🎬 Videos: {counts['video']}\n"
        f"📷 Photos: {counts['photo']}\n"
        f"📄 Documents: {counts['document']}\n"
        f"📦 Total size: {format_size(total)}\n\n"
        "Choose what to process:"
    )


async def resolve_message_link(link: str) -> tuple[Any, Any]:
    peer, message_id = link_to_source(link)
    entity = await user_client.get_entity(peer)
    message = await user_client.get_messages(entity, ids=message_id)
    if not message:
        raise ValueError("Message was not found or is not accessible by the user account.")
    return entity, message


async def scan_messages(source: Any, start_id: int, end_id: int) -> list[Any]:
    if end_id < start_id:
        raise ValueError("End message ID must be greater than or equal to start ID.")
    if end_id - start_id + 1 > MAX_BULK_MESSAGES:
        raise ValueError(f"Range is too large. Maximum is {MAX_BULK_MESSAGES} messages per job.")
    messages = await user_client.get_messages(source, ids=list(range(start_id, end_id + 1)))
    return [m for m in messages if m and getattr(m, "media", None)]


async def run_with_flood_wait(coro_factory):
    """Respect Telegram FloodWait instead of trying to bypass it."""
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 1))
            print(f"Telegram FloodWait: sleeping {wait_seconds}s")
            await asyncio.sleep(wait_seconds + 1)


async def send_media_to_destination(message: Any, destination: str) -> Any:
    return await run_with_flood_wait(
        lambda: user_client.send_file(
            destination,
            message.media,
            caption=caption_for(message),
            force_document=True,
        )
    )


async def send_media_to_bot_chat(message: Any, bot_chat_id: int) -> Any:
    """Send the large-file-capable copy to the bot conversation via MTProto."""
    sent = await run_with_flood_wait(
        lambda: user_client.send_file(
            bot_chat_id,
            message.media,
            caption=caption_for(message),
            force_document=True,
        )
    )
    asyncio.create_task(delete_later(sent))
    return sent


async def delete_later(message: Any) -> None:
    await asyncio.sleep(BOT_DELETE_SECONDS)
    try:
        await run_with_flood_wait(lambda: user_client.delete_messages(message.peer_id, [message.id]))
    except Exception as exc:
        print(f"Auto-delete failed: {type(exc).__name__}: {exc}")


async def process_job(job: SelectionJob, bot_chat_id: int, status_message: Message) -> None:
    async with JOB_SEMAPHORE:
        selected_ids = job.selected or set(job.candidates.keys())
        messages = [job.candidates[mid] for mid in job.candidates if mid in selected_ids]
        total = len(messages)
        done = 0
        await status_message.edit_text(f"⏳ Processing 0/{total} files...")

        for message in messages:
            try:
                await send_media_to_destination(message, job.destination)
                await send_media_to_bot_chat(message, bot_chat_id)
                done += 1
                await status_message.edit_text(f"⏳ Processing {done}/{total} files...")
            except RPCError as exc:
                print(f"Media {getattr(message, 'id', '?')} failed: {type(exc).__name__}: {exc}")
                await status_message.edit_text(
                    f"⚠️ Processed {done}/{total}. Message {getattr(message, 'id', '?')} failed: {type(exc).__name__}"
                )

        await status_message.edit_text(f"✅ Completed: {done}/{total} files\n\nDestination: {job.destination}")


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        return
    destination = get_destination(message.from_user.id)
    await message.answer(
        "HJ GROUPS Media Collector\n\n"
        "Send a Telegram message link to scan one message.\n"
        "Use /range <chat> <start_id> <end_id> for bulk selection.\n\n"
        f"Destination: {destination or 'not configured'}\n\n"
        "Commands:\n"
        "/setdestination <chat_id_or_username>\n"
        "/range <chat> <start_id> <end_id>\n"
        "/cancel"
    )


@dp.message(Command("setdestination"))
async def set_destination_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /setdestination @channel_or_chat_id")
        return
    destination = parts[1].strip()
    set_destination(message.from_user.id, destination)
    await message.answer(f"✅ Destination saved: {destination}")


@dp.message(Command("cancel"))
async def cancel_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        return
    JOBS.pop(message.from_user.id, None)
    await message.answer("❌ Current job cancelled.")


@dp.message(Command("range"))
async def range_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer("Usage: /range @channel_or_chat_id START_ID END_ID\nExample: /range @mychannel 25 100")
        return
    destination = get_destination(message.from_user.id)
    if not destination:
        await message.answer("Set a destination first with /setdestination")
        return
    try:
        source = await user_client.get_entity(parts[1])
        start_id = int(parts[2])
        end_id = int(parts[3])
        status = await message.answer("🔎 Scanning messages...")
        media_messages = await scan_messages(source, start_id, end_id)
        if not media_messages:
            await status.edit_text("No downloadable media messages were found in that range.")
            return
        job = SelectionJob(
            owner_id=message.from_user.id,
            source=source,
            message_ids=[m.id for m in media_messages],
            candidates={m.id: m for m in media_messages},
            destination=destination,
            bot_message_id=status.message_id,
            selected=set(),
        )
        JOBS[message.from_user.id] = job
        await status.edit_text(scan_summary(media_messages), reply_markup=build_scan_keyboard(message.from_user.id))
    except (ValueError, RPCError) as exc:
        await message.answer(f"Could not scan range: {exc}")


@dp.message(F.text)
async def text_handler(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        return
    text = (message.text or "").strip()
    if not text.startswith("http") or "t.me/" not in text:
        await message.answer("Send a Telegram message link, or use /range <chat> <start> <end>.")
        return
    destination = get_destination(message.from_user.id)
    if not destination:
        await message.answer("Set a destination first with /setdestination")
        return
    status = await message.answer("🔎 Scanning message...")
    try:
        source, source_message = await resolve_message_link(text)
        if not getattr(source_message, "media", None):
            await status.edit_text("That message does not contain downloadable media.")
            return
        job = SelectionJob(
            owner_id=message.from_user.id,
            source=source,
            message_ids=[source_message.id],
            candidates={source_message.id: source_message},
            destination=destination,
            bot_message_id=status.message_id,
            selected={source_message.id},
        )
        JOBS[message.from_user.id] = job
        await status.edit_text(scan_summary([source_message]), reply_markup=build_scan_keyboard(message.from_user.id))
    except (ValueError, RPCError) as exc:
        await status.edit_text(f"Could not resolve that message: {exc}")


@dp.callback_query(F.data.startswith("pick:"))
async def pick_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    parts = callback.data.split(":")
    job_id = int(parts[1])
    kind = parts[2]
    job = JOBS.get(job_id)
    if not job or job.owner_id != callback.from_user.id:
        await callback.answer("Job expired", show_alert=True)
        return
    if kind == "all":
        job.selected = set(job.candidates)
    else:
        job.selected = {mid for mid, msg in job.candidates.items() if media_kind(msg) == kind}
    if not job.selected:
        await callback.answer("No files of that type", show_alert=True)
        return
    total_size = sum(media_size(job.candidates[mid]) for mid in job.selected)
    await callback.message.edit_text(
        f"Ready to process {len(job.selected)} file(s).\n\n"
        f"Total size: {format_size(total_size)}\n\n"
        f"Destination: {job.destination}\n\nConfirm?",
        reply_markup=build_confirm_keyboard(job_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("select:"))
async def select_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    job_id = int(callback.data.split(":")[1])
    job = JOBS.get(job_id)
    if not job or job.owner_id != callback.from_user.id:
        await callback.answer("Job expired", show_alert=True)
        return
    job.selected = set()
    await callback.message.edit_text("Select individual files.\n\nSelected: 0", reply_markup=build_individual_keyboard(job))
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def toggle_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    _, object_id, message_id_text = callback.data.split(":")
    job = next((j for j in JOBS.values() if id(j) == int(object_id)), None)
    if not job or job.owner_id != callback.from_user.id:
        await callback.answer("Job expired", show_alert=True)
        return
    message_id = int(message_id_text)
    job.selected = job.selected or set()
    if message_id in job.selected:
        job.selected.remove(message_id)
    else:
        job.selected.add(message_id)
    await callback.message.edit_reply_markup(reply_markup=build_individual_keyboard(job))
    await callback.answer(f"Selected: {len(job.selected)}")


@dp.callback_query(F.data.startswith("confirm_select:"))
async def confirm_select_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    object_id = int(callback.data.split(":")[1])
    job = next((j for j in JOBS.values() if id(j) == object_id), None)
    if not job or job.owner_id != callback.from_user.id:
        await callback.answer("Job expired", show_alert=True)
        return
    if not job.selected:
        await callback.answer("Select at least one file", show_alert=True)
        return
    await callback.message.edit_text(
        f"Selected {len(job.selected)} file(s).\n\nDestination: {job.destination}\n\nConfirm?",
        reply_markup=build_confirm_keyboard(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    job_id = int(callback.data.split(":")[1])
    job = JOBS.get(job_id)
    if not job or job.owner_id != callback.from_user.id:
        await callback.answer("Job expired", show_alert=True)
        return
    await callback.answer("Started")
    await process_job(job, callback.from_user.id, callback.message)
    JOBS.pop(callback.from_user.id, None)


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    job_id = int(callback.data.split(":")[1])
    job = JOBS.get(job_id)
    if job and job.owner_id == callback.from_user.id:
        JOBS.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ Cancelled.")
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel_obj:"))
async def cancel_obj_callback(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Not authorized", show_alert=True)
        return
    object_id = int(callback.data.split(":")[1])
    job = next((j for j in JOBS.values() if id(j) == object_id), None)
    if job:
        JOBS.pop(job.owner_id, None)
    await callback.message.edit_text("❌ Cancelled.")
    await callback.answer()


async def run_telethon() -> None:
    await user_client.start(phone=PHONE_NUMBER)
    me = await user_client.get_me()
    print(f"Telethon connected as {getattr(me, 'username', None) or me.id}")
    await user_client.run_until_disconnected()


async def run_bot() -> None:
    print("aiogram polling started")
    await dp.start_polling(bot)


async def main() -> None:
    if not API_ID or not API_HASH or not PHONE_NUMBER or not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE_NUMBER and TELEGRAM_BOT_TOKEN.")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("Set TELEGRAM_ALLOWED_USER_IDS to one or more Telegram user IDs.")

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
