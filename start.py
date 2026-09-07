import asyncio
import os
import runpy

from aiogram import BaseMiddleware, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, BotCommand

# Voroa uses the shorter environment variable names. Map them to the names
# expected by the Python application.
aliases = {
    "TELEGRAM_API_ID": "API_ID",
    "TELEGRAM_API_HASH": "API_HASH",
    "TELEGRAM_BOT_TOKEN": "BOT_TOKEN",
}
for target, source in aliases.items():
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

# Import the real application without triggering its __main__ block.
app = runpy.run_path("telegram_chat_exporter.py", run_name="hj_groups_app")
dp = app["dp"]
bot = app["bot"]
main = app["main"]
is_allowed = app["is_allowed"]


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Login Telegram", callback_data="ui:login"), InlineKeyboardButton(text="📡 Session Status", callback_data="ui:session")],
        [InlineKeyboardButton(text="📥 Download / Forward", callback_data="ui:download"), InlineKeyboardButton(text="🔗 Set Destination", callback_data="ui:destination")],
        [InlineKeyboardButton(text="🔎 Scan Messages", callback_data="ui:scan"), InlineKeyboardButton(text="📦 Bulk Range", callback_data="ui:bulk")],
        [InlineKeyboardButton(text="📋 Select Messages", callback_data="ui:select"), InlineKeyboardButton(text="❌ Cancel Job", callback_data="ui:cancel")],
        [InlineKeyboardButton(text="🚪 Logout", callback_data="ui:logout"), InlineKeyboardButton(text="ℹ️ Help", callback_data="ui:help")],
    ])


def help_text() -> str:
    return (
        "🤖 <b>HJ Groups Downloader</b>\n\n"
        "All bot functions are available below as buttons.\n\n"
        "📥 Send a Telegram message link to download/forward media.\n"
        "📦 Bulk Range scans a message-ID range.\n"
        "📋 Select Messages lets you choose individual files."
    )


class StartMenuMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.text:
            command = event.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command == "/start" and is_allowed(event.from_user.id if event.from_user else None):
                await event.answer(help_text(), parse_mode="HTML", reply_markup=main_keyboard())
                return
        return await handler(event, data)


# The original application already owns all command handlers. This middleware
# only replaces the /start presentation with the button-first interface.
dp.message.outer_middleware(StartMenuMiddleware())


@dp.callback_query(F.data.startswith("ui:"))
async def button_menu(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not is_allowed(user_id):
        await callback.answer("Not authorized", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    prompts = {
        "login": "🔐 <b>Telegram Login</b>\n\nSend:\n<code>/login +91xxxxxxxxxx</code>\n\nThen:\n<code>/otp 12345</code>\n\nIf 2FA is enabled:\n<code>/2fa your_password</code>",
        "session": "📡 Send <code>/session</code> to check the connected Telegram account.",
        "download": "📥 Send a Telegram message link, for example:\n<code>https://t.me/channel/123</code>",
        "destination": "🔗 Send:\n<code>/setdestination @channel_or_chat_id</code>",
        "scan": "🔎 Send a Telegram message link. The bot will scan the media and show selection buttons.",
        "bulk": "📦 Send:\n<code>/range @channel START_ID END_ID</code>",
        "select": "📋 After a scan, send:\n<code>/select 101,102,103</code>\n\nYou can also use the individual selection buttons shown by the scan.",
        "cancel": "❌ Send <code>/cancel</code> or use the Cancel button shown on the active job.",
        "logout": "🚪 Send <code>/logout</code> to remove the saved Telegram user session.",
        "help": help_text(),
    }
    await callback.message.answer(prompts[action], parse_mode="HTML", reply_markup=main_keyboard())


async def set_commands() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Open button menu"),
        BotCommand(command="login", description="Login Telegram account"),
        BotCommand(command="otp", description="Enter Telegram OTP"),
        BotCommand(command="2fa", description="Enter Telegram 2FA password"),
        BotCommand(command="session", description="Check Telegram session"),
        BotCommand(command="setdestination", description="Set destination chat"),
        BotCommand(command="range", description="Scan a message range"),
        BotCommand(command="select", description="Select message IDs"),
        BotCommand(command="cancel", description="Cancel current job"),
        BotCommand(command="logout", description="Logout Telegram account"),
    ])


async def runner() -> None:
    await set_commands()
    await main()


if __name__ == "__main__":
    asyncio.run(runner())
