"""
Telegram Private Chat Exporter

- Frontend: aiogram Bot API bot
- Backend: Telethon user client
- Only ALLOWED_USER_ID can use the bot.
- Send a private chat ID or username to the bot; the bot fetches the
  latest 50 messages and returns them as a TXT file.

IMPORTANT:
Do NOT commit real API credentials, bot tokens, or a Telethon .session file
into a public GitHub repository. Put real values in environment variables
or a private/local configuration.
"""

import asyncio
import os
from datetime import timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from telethon import TelegramClient
from telethon.errors import RPCError

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# You may set these directly for local testing, but environment variables are
# safer, especially if this repository is public.
API_ID = int(os.getenv("TELEGRAM_API_ID", "12345678"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "PUT_YOUR_API_HASH_HERE")
PHONE_NUMBER = os.getenv("TELEGRAM_PHONE_NUMBER", "+910000000000")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "123456789"))

# Telethon session is persistent. The first local run may ask for the login
# code and, if enabled, your 2FA password. Never upload the resulting .session
# file to GitHub.
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "telegram_user_session")

OUTPUT_DIR = Path("exports")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# CLIENTS
# ---------------------------------------------------------------------------
# Telethon is the personal-account client (MTProto).
user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# aiogram is the Bot API client.
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Prevent two export requests from using the same output filename at once.
export_lock = asyncio.Lock()


def format_message(message) -> str:
    """Convert a Telethon message into readable plain text."""
    date_text = ""
    if message.date:
        date = message.date
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        date_text = date.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    sender = "Unknown"
    if message.sender:
        first = getattr(message.sender, "first_name", None) or ""
        last = getattr(message.sender, "last_name", None) or ""
        username = getattr(message.sender, "username", None)
        sender = " ".join(part for part in (first, last) if part).strip()
        if username:
            sender = f"{sender} (@{username})" if sender else f"@{username}"

    text = message.message or ""
    if not text and message.media:
        text = f"[Media: {type(message.media).__name__}]"
    if not text:
        text = "[No text]"

    return f"[{date_text}] Message ID: {message.id}\nSender: {sender}\n{text}\n"


async def export_last_50_messages(chat_reference: str) -> Path:
    """Fetch the latest 50 messages and save them to a TXT file."""
    # Telethon accepts usernames such as @username and numeric peer IDs.
    reference = chat_reference.strip()
    if not reference:
        raise ValueError("Empty chat ID/username")

    entity = await user_client.get_entity(reference)
    messages = await user_client.get_messages(entity, limit=50)

    # get_messages returns newest first. Reverse for chronological output.
    messages = list(reversed(messages))

    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in reference)
    safe_name = safe_name.lstrip("_")[:60] or "chat"
    output_path = OUTPUT_DIR / f"telegram_{safe_name}_{entity.id}.txt"

    lines = [
        f"Telegram chat export: {reference}",
        f"Chat ID: {entity.id}",
        "Messages: latest 50 (or fewer if the chat contains fewer messages)",
        "=" * 70,
        "",
    ]
    lines.extend(format_message(message) + "-" * 70 for message in messages)

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# BOT HANDLERS
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    """Respond only to the configured owner."""
    if message.from_user.id != ALLOWED_USER_ID:
        return

    await message.answer(
        "Send me a private Telegram chat ID or username.\n\n"
        "Example: @username\n"
        "Example: 123456789"
    )


@dp.message(F.text)
async def export_handler(message: Message) -> None:
    """Receive a chat reference, export it through Telethon, and send TXT."""
    # Security check: ignore everybody except the configured Telegram user.
    if not message.from_user or message.from_user.id != ALLOWED_USER_ID:
        return

    chat_reference = message.text.strip()
    status = await message.answer("Fetching the latest 50 messages...")

    async with export_lock:
        try:
            output_path = await export_last_50_messages(chat_reference)
        except (ValueError, RPCError) as exc:
            await status.edit_text(f"Could not export that chat: {exc}")
            return
        except Exception as exc:
            # Avoid exposing internal details to Telegram users.
            print(f"Export error: {type(exc).__name__}: {exc}")
            await status.edit_text("Export failed. Check the server logs.")
            return

        try:
            await message.answer_document(
                document=FSInputFile(output_path),
                caption=f"Latest 50 messages from: {chat_reference}",
            )
            await status.delete()
        finally:
            # The TXT is temporary and is deleted after delivery.
            output_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CONCURRENT ASYNCIO STARTUP
# ---------------------------------------------------------------------------
async def run_telethon() -> None:
    """Connect the personal user client and keep it alive."""
    # On the first run, Telethon may request the phone login code/2FA password.
    # After successful login it stores the session locally in SESSION_NAME.session.
    await user_client.start(phone=PHONE_NUMBER)
    print("Telethon user client connected.")

    # run_until_disconnected keeps the Telethon client alive without blocking
    # the aiogram polling task because it runs as its own asyncio task.
    await user_client.run_until_disconnected()


async def run_bot() -> None:
    """Start aiogram long polling."""
    print("aiogram bot polling started.")
    await dp.start_polling(bot)


async def main() -> None:
    """
    Run both Telegram clients in the SAME asyncio event loop.

    asyncio.create_task() schedules both long-running clients concurrently:

        Task 1 -> Telethon personal account (reads private chats)
        Task 2 -> aiogram Bot API (receives commands/messages)

    Neither client needs a second thread or process for this design.
    """
    if API_HASH == "PUT_YOUR_API_HASH_HERE":
        raise RuntimeError("Set TELEGRAM_API_HASH before starting the bot.")
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the bot.")

    telethon_task = asyncio.create_task(run_telethon(), name="telethon-user-client")
    bot_task = asyncio.create_task(run_bot(), name="aiogram-bot")

    try:
        # Wait until one client stops or fails.
        done, pending = await asyncio.wait(
            {telethon_task, bot_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Re-raise an exception from a completed task, if any.
        for task in done:
            task.result()
    finally:
        # Graceful shutdown for both clients.
        for task in (telethon_task, bot_task):
            if not task.done():
                task.cancel()

        await asyncio.gather(telethon_task, bot_task, return_exceptions=True)

        if user_client.is_connected():
            await user_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
