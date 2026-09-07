from pathlib import Path

p = Path('telegram_bot.py')
s = p.read_text(encoding='utf-8')

s = s.replace(
    'from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError',
    'from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError, AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError',
    1,
)

s = s.replace(
    'PENDING = {}\nJOBS = {}',
    'PENDING = {}\nJOBS = {}\nQR_CLIENTS = {}',
    1,
)

old_start = '''async def qr_login_flow(message: Message, uid: int):\n    """QR login avoids Telegram's new-device code reuse/blocking path."""\n    async with LOGIN_LOCK:\n        try:\n            await rebuild("")\n            qr_login = await user_client.qr_login()'''
new_start = '''async def qr_login_flow(message: Message, uid: int):\n    """Start an isolated QR login client so other bot actions cannot replace its auth key."""\n    async with LOGIN_LOCK:\n        qr_client = None\n        keep_client = False\n        try:\n            qr_client = TelegramClient(StringSession(""), API_ID, API_HASH)\n            await qr_client.connect()\n            qr_login = await qr_client.qr_login()'''
if old_start not in s:
    raise SystemExit('qr start pattern not found')
s = s.replace(old_start, new_start, 1)

s = s.replace(
    '                await wait_task\n                session = user_client.session.save()\n                await session_store.set(session)\n                PENDING.pop(uid, None)\n                await message.answer("Telegram account login successful via QR. ✅", reply_markup=menu())\n            except SessionPasswordNeededError:\n                PENDING[uid] = "2fa_qr"',
    '                await wait_task\n                session = qr_client.session.save()\n                await session_store.set(session)\n                PENDING.pop(uid, None)\n                await message.answer("Telegram account login successful via QR. ✅", reply_markup=menu())\n            except SessionPasswordNeededError:\n                QR_CLIENTS[uid] = qr_client\n                keep_client = True\n                PENDING[uid] = "2fa_qr"',
    1,
)

s = s.replace(
    '            except Exception as exc:\n                await message.answer(f"QR login failed: {type(exc).__name__}: {exc}", reply_markup=menu())\n        except Exception as exc:\n            await message.answer(f"Could not start QR login: {type(exc).__name__}: {exc}", reply_markup=menu())',
    '            except (AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError) as exc:\n                QR_CLIENTS.pop(uid, None)\n                PENDING.pop(uid, None)\n                await message.answer("⚠️ The temporary Telegram QR session was invalidated. Press 🔐 Login and scan a new QR code.", reply_markup=menu())\n            except Exception as exc:\n                QR_CLIENTS.pop(uid, None)\n                PENDING.pop(uid, None)\n                await message.answer(f"QR login failed: {type(exc).__name__}: {exc}", reply_markup=menu())\n        except Exception as exc:\n            QR_CLIENTS.pop(uid, None)\n            PENDING.pop(uid, None)\n            await message.answer(f"Could not start QR login: {type(exc).__name__}: {exc}", reply_markup=menu())\n        finally:\n            if qr_client is not None and not keep_client:\n                try:\n                    if qr_client.is_connected():\n                        await qr_client.disconnect()\n                except Exception:\n                    pass',
    1,
)

old_2fa = '''    if PENDING.get(uid) == "2fa_qr":\n        try:\n            await user_client.sign_in(password=parts[1].strip())\n            await session_store.set(user_client.session.save())\n            PENDING.pop(uid, None)\n            await message.answer("Telegram account login successful via QR + 2FA. ✅", reply_markup=menu())\n        except Exception as exc:\n            await message.answer(f"2FA login failed: {type(exc).__name__}: {exc}", reply_markup=menu())\n        return'''
new_2fa = '''    if PENDING.get(uid) == "2fa_qr":\n        qr_client = QR_CLIENTS.get(uid)\n        if qr_client is None or not qr_client.is_connected():\n            QR_CLIENTS.pop(uid, None)\n            PENDING.pop(uid, None)\n            return await message.answer("⚠️ QR login session is no longer active. Press 🔐 Login and scan a new QR code.", reply_markup=menu())\n        try:\n            await qr_client.sign_in(password=parts[1].strip())\n            await session_store.set(qr_client.session.save())\n            PENDING.pop(uid, None)\n            QR_CLIENTS.pop(uid, None)\n            await message.answer("Telegram account login successful via QR + 2FA. ✅", reply_markup=menu())\n        except (AuthKeyUnregisteredError, AuthKeyInvalidError, SessionRevokedError):\n            PENDING.pop(uid, None)\n            QR_CLIENTS.pop(uid, None)\n            await message.answer("⚠️ Telegram invalidated the temporary QR session. Press 🔐 Login and scan a new QR code.", reply_markup=menu())\n        except Exception as exc:\n            await message.answer(f"2FA login failed: {type(exc).__name__}: {exc}", reply_markup=menu())\n        return'''
if old_2fa not in s:
    raise SystemExit('2fa pattern not found')
s = s.replace(old_2fa, new_2fa, 1)

old_cancel = '''    JOBS.pop(uid, None)\n    PENDING.pop(uid, None)\n    await session_store.clear_login(uid)'''
new_cancel = '''    JOBS.pop(uid, None)\n    PENDING.pop(uid, None)\n    qr_client = QR_CLIENTS.pop(uid, None)\n    if qr_client is not None:\n        try:\n            if qr_client.is_connected():\n                await qr_client.disconnect()\n        except Exception:\n            pass\n    await session_store.clear_login(uid)'''
s = s.replace(old_cancel, new_cancel, 1)

p.write_text(s, encoding='utf-8')
print('QR 2FA isolation patch applied')
