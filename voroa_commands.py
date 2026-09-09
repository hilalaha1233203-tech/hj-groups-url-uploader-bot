"""HJ GROUPS OF FILES command and settings UI extensions."""
from __future__ import annotations

import html
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
import voroa_stable as v

_original_menu = v.menu

def extended_menu() -> ReplyKeyboardMarkup:
    base = _original_menu()
    rows = [list(row) for row in base.keyboard]
    rows.insert(-1, [KeyboardButton(text="⚙️ Settings")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=base.resize_keyboard, is_persistent=base.is_persistent, input_field_placeholder=base.input_field_placeholder)

v.menu = extended_menu

async def settings_response(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not v.authorized(uid):
        return
    destination = v.get_destination(uid) or "Not set"
    session = "Active" if v.load_session_string() else "Not logged in"
    await message.answer(
        "⚙️ <b>Settings</b>\n\n"
        f"🎯 Destination: <code>{html.escape(destination)}</code>\n"
        f"📱 Telegram session: <b>{session}</b>\n"
        f"📦 Bulk message limit: <b>{v.MAX_BULK_MESSAGES}</b>\n"
        f"📁 Transfer file limit: <b>{v.MAX_TRANSFER_FILES}</b>\n\n"
        "🎯 Use Destination to change the target.\n🔐 Use Login to change the Telegram account.",
        parse_mode="HTML", reply_markup=v.menu(),
    )

@v.dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    await settings_response(message)

@v.dp.message(v.F.text == "⚙️ Settings")
async def button_settings(message: Message) -> None:
    await settings_response(message)

@v.dp.message(Command("features"))
@v.dp.message(Command("latest"))
@v.dp.message(Command("latest_update"))
async def cmd_features(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not v.authorized(uid):
        return
    await message.answer(
        "🆕 <b>HJ GROUPS OF FILES — Latest Features</b>\n\n"
        "🔗 Single Telegram message link scan\n📦 Bulk message-range scan\n🎯 Per-user destination chat/channel\n"
        "🚀 Telegram-to-Telegram transfer\n📊 Live transfer status and progress\n🛑 Safe transfer cancellation\n"
        "⏳ FloodWait-aware retry handling\n📋 Current job/status view\n🔐 Telegram login with 2FA support\n"
        "📱 Session status\n⚙️ Settings with persistent destination",
        parse_mode="HTML", reply_markup=v.menu(),
    )

@v.dp.message(Command("scan"))
async def cmd_scan(message: Message) -> None: await v.button_scan(message)
@v.dp.message(Command("bulk"))
async def cmd_bulk(message: Message) -> None: await v.button_bulk(message)
@v.dp.message(Command("destination"))
async def cmd_destination(message: Message) -> None: await v.button_destination(message)
@v.dp.message(Command("job"))
async def cmd_job(message: Message) -> None: await v.button_job(message)
@v.dp.message(Command("login"))
async def cmd_login(message: Message) -> None: await v.button_login(message)
@v.dp.message(Command("session"))
async def cmd_session(message: Message) -> None: await v.button_session(message)
@v.dp.message(Command("logout"))
async def cmd_logout(message: Message) -> None: await v.button_logout(message)
@v.dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None: await v.button_cancel(message)

async def setup_commands() -> None:
    await v.bot.set_my_commands([
        v.BotCommand(command="start", description="Open HJ GROUPS OF FILES"),
        v.BotCommand(command="help", description="Show help"),
        v.BotCommand(command="features", description="Latest features"),
        v.BotCommand(command="latest", description="Show latest update"),
        v.BotCommand(command="settings", description="Open settings"),
        v.BotCommand(command="scan", description="Scan one Telegram link"),
        v.BotCommand(command="bulk", description="Scan a message range"),
        v.BotCommand(command="destination", description="Set destination"),
        v.BotCommand(command="job", description="Show current job"),
        v.BotCommand(command="login", description="Login Telegram account"),
        v.BotCommand(command="session", description="Show session status"),
        v.BotCommand(command="logout", description="Logout Telegram account"),
        v.BotCommand(command="cancel", description="Cancel current operation"),
    ])
