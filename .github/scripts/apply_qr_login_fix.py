from pathlib import Path

BOT = Path("telegram_bot.py")
REQ = Path("requirements.txt")

text = BOT.read_text(encoding="utf-8")
if "QR_LOGIN_FIX_V1" in text:
    raise SystemExit("QR login patch already applied")

text = text.replace(
    "    ReplyKeyboardMarkup,\n    BotCommand,\n",
    "    ReplyKeyboardMarkup,\n    FSInputFile,\n    BotCommand,\n",
)
text = text.replace(
    "from playbook_client import PlaybookClient, PlaybookError\n",
    "from playbook_client import PlaybookClient, PlaybookError\nimport qrcode\n",
)
marker = "async def login_cmd(message: Message):\n"
start = text.index(marker)
end = text.index("@dp.message(Command(\"otp\"))", start)
new_login = '''async def qr_login_flow(message: Message, uid: int):\n    """QR login avoids Telegram's new-device code reuse/blocking path."""\n    async with LOGIN_LOCK:\n        try:\n            await rebuild("")\n            qr_login = await user_client.qr_login()\n            wait_task = asyncio.create_task(qr_login.wait(timeout=150))\n            qr_path = Path(tempfile.gettempdir()) / f"telegram_qr_{uid}.png"\n            img = qrcode.make(qr_login.url)\n            img.save(qr_path)\n            try:\n                await message.answer_photo(\n                    FSInputFile(qr_path),\n                    caption=(\n                        "🔐 Telegram QR Login\\n\\n"\n                        "1. Open Telegram on your phone.\\n"\n                        "2. Settings → Devices → Link Desktop Device.\\n"\n                        "3. Scan this QR code.\\n\\n"\n                        "The QR code expires automatically. Keep this chat open until login completes."\n                    ),\n                    reply_markup=menu(),\n                )\n            finally:\n                try:\n                    qr_path.unlink(missing_ok=True)\n                except OSError:\n                    pass\n\n            try:\n                await wait_task\n                session = user_client.session.save()\n                await session_store.set(session)\n                PENDING.pop(uid, None)\n                await message.answer("Telegram account login successful via QR. ✅", reply_markup=menu())\n            except SessionPasswordNeededError:\n                PENDING[uid] = "2fa_qr"\n                await message.answer(\n                    "✅ QR approved. Your Telegram account has 2FA enabled. Enter the 2FA password here or use /2fa your_password.",\n                    reply_markup=menu(),\n                )\n            except asyncio.TimeoutError:\n                await message.answer("⏱️ QR code expired. Press 🔐 Login to generate a new QR code.", reply_markup=menu())\n            except Exception as exc:\n                await message.answer(f"QR login failed: {type(exc).__name__}: {exc}", reply_markup=menu())\n        except Exception as exc:\n            await message.answer(f"Could not start QR login: {type(exc).__name__}: {exc}", reply_markup=menu())\n\n@dp.message(Command("login"))\nasync def login_cmd(message: Message):\n    uid = message.from_user.id if message.from_user else None\n    if not allowed(uid):\n        await message.answer("⛔ You are not authorized to use this bot.")\n        return\n    if not session_store.configured and not SESSION_STRING:\n        return await message.answer("Session storage is not configured.", reply_markup=menu())\n    await qr_login_flow(message, uid)\n\n# QR_LOGIN_FIX_V1\n\n'''
text = text[:start] + new_login + text[end:]

text = text.replace(
    "    parts = (message.text or \"\").split(maxsplit=1)\n    if len(parts) != 2:\n        PENDING[uid] = \"2fa\"\n        return await message.answer(\"Enter your Telegram 2FA password.\", reply_markup=menu())\n    pending = await session_store.get_login(uid)\n",
    "    parts = (message.text or \"\").split(maxsplit=1)\n    if len(parts) != 2:\n        PENDING[uid] = PENDING.get(uid) or \"2fa\"\n        return await message.answer(\"Enter your Telegram 2FA password.\", reply_markup=menu())\n    if PENDING.get(uid) == \"2fa_qr\":\n        try:\n            await user_client.sign_in(password=parts[1].strip())\n            await session_store.set(user_client.session.save())\n            PENDING.pop(uid, None)\n            await message.answer(\"Telegram account login successful via QR + 2FA. ✅\", reply_markup=menu())\n        except Exception as exc:\n            await message.answer(f\"2FA login failed: {type(exc).__name__}: {exc}\", reply_markup=menu())\n        return\n    pending = await session_store.get_login(uid)\n",
    1,
)
text = text.replace(
    "    if action == \"2fa\":\n        return await twofa_cmd(message.model_copy(update={\"text\": f\"/2fa {text}\"}))\n",
    "    if action in {\"2fa\", \"2fa_qr\"}:\n        return await twofa_cmd(message.model_copy(update={\"text\": f\"/2fa {text}\"}))\n",
)
BOT.write_text(text, encoding="utf-8")

req = REQ.read_text(encoding="utf-8")
if "qrcode" not in req.splitlines():
    REQ.write_text(req.rstrip() + "\nqrcode\n", encoding="utf-8")

print("QR login migration applied")
