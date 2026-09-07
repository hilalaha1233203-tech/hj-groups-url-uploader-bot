"""Persistent button UI for the HJ GROUPS Telegram bot."""

from __future__ import annotations

from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

BTN_LOGIN = "🔐 Login"
BTN_OTP = "🔢 Enter OTP"
BTN_2FA = "🔒 Enter 2FA"
BTN_SESSION = "📡 Session"
BTN_LOGOUT = "🚪 Logout"
BTN_DESTINATION = "🎯 Set Destination"
BTN_SCAN = "🔎 Scan Link"
BTN_RANGE = "📦 Bulk Range"
BTN_SELECT = "☑️ Select Files"
BTN_CANCEL = "❌ Cancel"
BTN_HELP = "ℹ️ Help"


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LOGIN), KeyboardButton(text=BTN_SESSION)],
            [KeyboardButton(text=BTN_DESTINATION), KeyboardButton(text=BTN_SCAN)],
            [KeyboardButton(text=BTN_RANGE), KeyboardButton(text=BTN_SELECT)],
            [KeyboardButton(text=BTN_OTP), KeyboardButton(text=BTN_2FA)],
            [KeyboardButton(text=BTN_LOGOUT), KeyboardButton(text=BTN_CANCEL)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an action…",
    )


class MenuMiddleware(BaseMiddleware):
    def __init__(self, ns: dict[str, Any]) -> None:
        self.ns = ns
        self.modes: dict[int, str] = {}

    def _allowed(self, user_id: int | None) -> bool:
        return bool(user_id and self.ns["is_allowed"](user_id))

    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)
        user_id = event.from_user.id if event.from_user else None
        if not self._allowed(user_id):
            return await handler(event, data)
        text = event.text.strip()
        ns = self.ns

        if text == "/start" or text.startswith("/start "):
            await event.answer(
                "HJ GROUPS Media Collector\n\nChoose an action from the buttons below.",
                reply_markup=main_keyboard(),
            )
            return None

        if text == BTN_HELP:
            await event.answer(
                "HJ GROUPS Media Collector\n\n"
                "🔐 Login → connect Telegram account\n"
                "🔢 Enter OTP → complete Telegram login\n"
                "🔒 Enter 2FA → complete 2FA login\n"
                "📡 Session → check connected account\n"
                "🚪 Logout → remove saved Telegram session\n"
                "🎯 Set Destination → choose delivery chat\n"
                "🔎 Scan Link → scan one Telegram message\n"
                "📦 Bulk Range → scan a message-ID range\n"
                "☑️ Select Files → select files from the current scan\n"
                "❌ Cancel → cancel current action/job",
                reply_markup=main_keyboard(),
            )
            return None

        if text == BTN_LOGIN:
            self.modes[user_id] = "login_phone"
            await event.answer("📱 Send your Telegram phone number, for example:\n+919585049379", reply_markup=main_keyboard())
            return None
        if text == BTN_OTP:
            self.modes[user_id] = "otp"
            await event.answer("🔢 Send the OTP you received from Telegram.", reply_markup=main_keyboard())
            return None
        if text == BTN_2FA:
            self.modes[user_id] = "2fa"
            await event.answer("🔒 Send your Telegram 2FA password.", reply_markup=main_keyboard())
            return None

        if text == BTN_SESSION:
            self.modes.pop(user_id, None)
            session = await ns["get_saved_session"]()
            if not session:
                await event.answer("No Telegram user session. Tap 🔐 Login first.", reply_markup=main_keyboard())
                return None
            try:
                if not ns["user_client"].is_connected():
                    await ns["rebuild_user_client"](session)
                if await ns["user_client"].is_user_authorized():
                    me = await ns["user_client"].get_me()
                    await event.answer(f"✅ Session active: {getattr(me, 'username', None) or me.id}", reply_markup=main_keyboard())
                else:
                    await event.answer("⚠️ Session is not authorized. Tap 🔐 Login.", reply_markup=main_keyboard())
            except Exception as exc:
                await event.answer(f"Session check failed: {type(exc).__name__}: {exc}", reply_markup=main_keyboard())
            return None

        if text == BTN_LOGOUT:
            self.modes.pop(user_id, None)
            try:
                if ns["user_client"].is_connected():
                    await ns["user_client"].log_out()
            finally:
                await ns["session_store"].clear()
            await event.answer("🚪 Telegram account logged out and saved session removed.", reply_markup=main_keyboard())
            return None

        if text == BTN_DESTINATION:
            self.modes[user_id] = "destination"
            current = ns["get_destination"](user_id)
            await event.answer(f"🎯 Send the destination @username or chat ID.\nCurrent: {current or 'not configured'}", reply_markup=main_keyboard())
            return None
        if text == BTN_SCAN:
            self.modes[user_id] = "scan"
            await event.answer("🔎 Send a Telegram message link, for example:\nhttps://t.me/channel/123", reply_markup=main_keyboard())
            return None
        if text == BTN_RANGE:
            self.modes[user_id] = "range"
            await event.answer("📦 Send bulk range as:\n@channel START_ID END_ID\nExample: @mychannel 100 200", reply_markup=main_keyboard())
            return None
        if text == BTN_SELECT:
            self.modes[user_id] = "select"
            await event.answer("☑️ Send message IDs separated by commas.\nExample: 101,102,108", reply_markup=main_keyboard())
            return None
        if text == BTN_CANCEL:
            self.modes.pop(user_id, None)
            ns["JOBS"].pop(user_id, None)
            await event.answer("❌ Current action/job cancelled.", reply_markup=main_keyboard())
            return None

        mode = self.modes.get(user_id)
        if mode is None or text.startswith("/"):
            return await handler(event, data)

        if mode == "login_phone":
            async with ns["LOGIN_LOCK"]:
                try:
                    await ns["user_client"].connect()
                    sent = await ns["user_client"].send_code_request(text)
                    ns["LOGIN_PHONE"] = text
                    ns["LOGIN_CODE_HASH"] = sent.phone_code_hash
                    self.modes[user_id] = "otp"
                    await event.answer("✅ OTP sent. Tap 🔢 Enter OTP and send the code.", reply_markup=main_keyboard())
                except Exception as exc:
                    await event.answer(f"Login failed: {type(exc).__name__}: {exc}", reply_markup=main_keyboard())
            return None

        if mode == "otp":
            async with ns["LOGIN_LOCK"]:
                try:
                    if not ns["LOGIN_PHONE"] or not ns["LOGIN_CODE_HASH"]:
                        self.modes.pop(user_id, None)
                        await event.answer("No login is waiting. Tap 🔐 Login first.", reply_markup=main_keyboard())
                        return None
                    await ns["user_client"].sign_in(ns["LOGIN_PHONE"], text, phone_code_hash=ns["LOGIN_CODE_HASH"])
                    await ns["session_store"].set(ns["user_client"].session.save())
                    ns["LOGIN_PHONE"] = None
                    ns["LOGIN_CODE_HASH"] = None
                    self.modes.pop(user_id, None)
                    await event.answer("✅ Telegram account login successful.", reply_markup=main_keyboard())
                except ns["SessionPasswordNeededError"]:
                    self.modes[user_id] = "2fa"
                    await event.answer("🔒 2FA is enabled. Tap 🔒 Enter 2FA and send your password.", reply_markup=main_keyboard())
                except Exception as exc:
                    await event.answer(f"OTP login failed: {type(exc).__name__}: {exc}", reply_markup=main_keyboard())
            return None

        if mode == "2fa":
            try:
                await ns["user_client"].sign_in(password=text)
                await ns["session_store"].set(ns["user_client"].session.save())
                ns["LOGIN_PHONE"] = None
                ns["LOGIN_CODE_HASH"] = None
                self.modes.pop(user_id, None)
                await event.answer("✅ Telegram account login successful.", reply_markup=main_keyboard())
            except Exception as exc:
                await event.answer(f"2FA login failed: {type(exc).__name__}: {exc}", reply_markup=main_keyboard())
            return None

        if mode == "destination":
            ns["set_destination"](user_id, text)
            self.modes.pop(user_id, None)
            await event.answer(f"✅ Destination saved: {text}", reply_markup=main_keyboard())
            return None

        if mode == "scan":
            self.modes.pop(user_id, None)
            if not ns["get_destination"](user_id):
                await event.answer("Set 🎯 Destination first.", reply_markup=main_keyboard())
                return None
            status = await event.answer("🔎 Scanning message…", reply_markup=main_keyboard())
            try:
                source, source_message = await ns["resolve_source_and_message"](text)
                if not getattr(source_message, "media", None):
                    await status.edit_text("That message does not contain downloadable media.")
                    return None
                await ns["create_job_from_messages"](user_id, source, [source_message], status)
            except (ValueError, ns["RPCError"]) as exc:
                await status.edit_text(f"Could not resolve that message: {exc}")
            return None

        if mode == "range":
            self.modes.pop(user_id, None)
            parts = text.split()
            if len(parts) != 3:
                await event.answer("Format: @channel START_ID END_ID", reply_markup=main_keyboard())
                return None
            if not ns["get_destination"](user_id):
                await event.answer("Set 🎯 Destination first.", reply_markup=main_keyboard())
                return None
            try:
                source = await ns["user_client"].get_entity(parts[0])
                status = await event.answer("🔎 Scanning messages…", reply_markup=main_keyboard())
                messages = await ns["scan_range"](source, int(parts[1]), int(parts[2]))
                await ns["create_job_from_messages"](user_id, source, messages, status)
            except (ValueError, ns["RPCError"]) as exc:
                await event.answer(f"Could not scan range: {exc}", reply_markup=main_keyboard())
            return None

        if mode == "select":
            self.modes.pop(user_id, None)
            job = ns["JOBS"].get(user_id)
            if not job:
                await event.answer("No active scan. Use 🔎 Scan Link or 📦 Bulk Range first.", reply_markup=main_keyboard())
                return None
            try:
                ids = {int(value.strip()) for value in text.split(",") if value.strip()}
            except ValueError:
                await event.answer("Message IDs must be comma-separated numbers.", reply_markup=main_keyboard())
                return None
            missing = ids - set(job.candidates)
            if missing:
                await event.answer("IDs not in current scan: " + ", ".join(map(str, sorted(missing)[:20])), reply_markup=main_keyboard())
                return None
            job.selected = ids
            total_size = sum(ns["media_size"](job.candidates[mid]) for mid in ids)
            await event.answer(
                f"Selected {len(ids)} file(s).\nTotal size: {ns['format_size'](total_size)}\n\n"
                f"Destination: {job.destination}\n\nConfirm using the inline Confirm button on the scan message.",
                reply_markup=main_keyboard(),
            )
            return None

        return await handler(event, data)


def install(ns: dict[str, Any]) -> None:
    ns["dp"].message.outer_middleware(MenuMiddleware(ns))
    ns["MAIN_KEYBOARD"] = main_keyboard()
