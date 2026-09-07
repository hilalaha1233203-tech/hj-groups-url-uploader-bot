from pathlib import Path
import re

bot_path = Path('telegram_bot.py')
store_path = Path('telegram_session_store.py')
bot = bot_path.read_text(encoding='utf-8')
store = store_path.read_text(encoding='utf-8')

# 1) Persistent destination storage in the same store used by the Telegram session.
marker = '''    async def close(self) -> None:\n'''
if 'async def get_destination(self, user_id: int)' not in store:
    addition = '''    async def get_destination(self, user_id: int) -> Optional[str]:\n        key = f"destination:{int(user_id)}"\n        document, ok = await self._mongo(lambda c: c.find_one({"_id": key}))\n        if not ok:\n            document = self._fallback_read().get(key)\n        if not document or not isinstance(document, dict):\n            return None\n        value = str(document.get("destination", "")).strip()\n        return value or None\n\n    async def set_destination(self, user_id: int, destination: str) -> None:\n        key = f"destination:{int(user_id)}"\n        value = destination.strip()\n        payload = {"destination": value}\n        _, ok = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))\n        if not ok:\n            data = self._fallback_read()\n            data[key] = payload\n            self._fallback_write(data)\n\n    async def clear_destination(self, user_id: int) -> None:\n        key = f"destination:{int(user_id)}"\n        await self._mongo(lambda c: c.delete_one({"_id": key}))\n        data = self._fallback_read()\n        data.pop(key, None)\n        self._fallback_write(data)\n\n'''
    if marker not in store:
        raise SystemExit('session store insertion marker not found')
    store = store.replace(marker, addition + marker, 1)

# 2) Imports / in-memory destination cache / helper functions.
bot = bot.replace('import asyncio, json, os, re, tempfile\n', 'import asyncio, json, os, re, tempfile, time\n', 1)
bot = bot.replace('JOBS = {}\nQR_CLIENTS = {}\n', 'JOBS = {}\nQR_CLIENTS = {}\nDESTINATIONS = {}\n', 1)
bot = bot.replace(
'''def destination(uid):\n    return (STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()\n\ndef set_destination(uid, dest):\n    STATE[str(uid)] = dest.strip()\n    save_state()\n''',
'''def destination(uid):\n    return (DESTINATIONS.get(str(uid)) or STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()\n\nasync def load_destination(uid):\n    cached = DESTINATIONS.get(str(uid))\n    if cached:\n        return cached\n    try:\n        saved = await session_store.get_destination(uid)\n    except Exception as exc:\n        print(f"[Voroa] Destination load failed: {type(exc).__name__}: {exc}", flush=True)\n        saved = None\n    value = (saved or STATE.get(str(uid)) or ENV_DESTINATIONS.get(str(uid)) or DEFAULT_DESTINATION).strip()\n    if value:\n        DESTINATIONS[str(uid)] = value\n    return value\n\nasync def set_destination(uid, dest):\n    value = dest.strip()\n    DESTINATIONS[str(uid)] = value\n    STATE[str(uid)] = value\n    save_state()\n    try:\n        await session_store.set_destination(uid, value)\n    except Exception as exc:\n        print(f"[Voroa] Destination persistence failed: {type(exc).__name__}: {exc}", flush=True)\n    return value\n''', 1)

# 3) Human-friendly progress/ETA helpers.
marker = 'def scan_menu(uid):\n'
if 'def format_eta(seconds):' not in bot:
    helpers = '''def safe_filename(msg):\n    name = getattr(getattr(msg, "file", None), "name", None)\n    if name:\n        return str(name)\n    return f"telegram-{getattr(msg, 'id', 'media')}"\n\ndef format_eta(seconds):\n    if seconds is None or seconds < 0:\n        return "calculating…"\n    seconds = int(round(seconds))\n    if seconds < 1:\n        return "0s left"\n    if seconds < 60:\n        return f"{seconds}s left"\n    minutes, secs = divmod(seconds, 60)\n    return f"{minutes}m {secs}s left"\n\ndef make_progress_callback(status, filename, stage, started_at):\n    state = {"last": 0.0}\n    def callback(current, total):\n        now = time.monotonic()\n        if total and current < total and now - state["last"] < 0.8:\n            return\n        state["last"] = now\n        if total:\n            percent = max(0.0, min(100.0, (current / total) * 100.0))\n            elapsed = max(0.1, now - started_at)\n            rate = current / elapsed if current > 0 else 0.0\n            eta = ((total - current) / rate) if rate > 0 else None\n            text = (f"📄 {filename}\\n\\n"\n                    f"{stage}\\n"\n                    f"[{int(percent):3d}%] {'█' * int(percent // 10)}{'░' * (10 - int(percent // 10))}\\n"\n                    f"⏳ {format_eta(eta)}")\n        else:\n            text = f"📄 {filename}\\n\\n{stage}\\n⏳ calculating…"\n        asyncio.create_task(status.edit_text(text, reply_markup=menu()))\n    return callback\n\n'''
    if marker not in bot:
        raise SystemExit('scan menu marker not found')
    bot = bot.replace(marker, helpers + marker, 1)

# 4) Replace send_destination with progress-aware stages and filenames.
send_start = bot.index('async def send_destination(msg, dest):')
send_end = bot.index('\nasync def send_copy(msg, uid):', send_start)
new_send = '''async def send_destination(msg, dest, status=None, index=1, total_files=1):\n    token = os.getenv("PLAYBOOK_API_TOKEN", "").strip()\n    org = os.getenv("PLAYBOOK_ORG_SLUG", "").strip()\n    filename = safe_filename(msg)\n    started_at = time.monotonic()\n\n    async def update(text):\n        if status is not None:\n            try:\n                await status.edit_text(text, reply_markup=menu())\n            except Exception:\n                pass\n\n    if status is not None:\n        await update(f"📄 {filename}\\n\\n🚀 Starting file {index}/{total_files}\\n⏳ calculating…")\n\n    if not token or not org:\n        callback = make_progress_callback(status, filename, "📤 Sending to destination…", started_at) if status is not None else None\n        kwargs = {"caption": caption(msg)}\n        if callback:\n            kwargs["progress_callback"] = callback\n        sent = await flood(lambda: user_client.send_file(dest, msg.media, **kwargs))\n        if status is not None:\n            await update(f"✅ {filename}\\n\\n📤 Sent successfully\\n⏱️ {int(time.monotonic() - started_at)}s")\n        return sent\n\n    temp_dir = Path(os.getenv("TELEGRAM_TEMP_DIR", tempfile.gettempdir()))\n    temp_dir.mkdir(parents=True, exist_ok=True)\n    path = temp_dir / filename\n    try:\n        download_cb = make_progress_callback(status, filename, "📥 Downloading from Telegram…", started_at) if status is not None else None\n        download_kwargs = {"file": str(path)}\n        if download_cb:\n            download_kwargs["progress_callback"] = download_cb\n        await user_client.download_media(msg, **download_kwargs)\n\n        client = PlaybookClient(token=token, org_slug=org)\n        upload_started = time.monotonic()\n        await update(f"📄 {filename}\\n\\n☁️ Uploading to temporary storage…\\n⏳ calculating…")\n        asset_token = await client.upload_file(\n            path,\n            title=filename,\n            progress_callback=(make_progress_callback(status, filename, "☁️ Uploading to temporary storage…", upload_started) if status is not None else None),\n        )\n        asset = {}\n        for poll in range(30):\n            asset = await client.get_asset(asset_token)\n            if not asset.get("is_skeleton", False):\n                break\n            if status is not None:\n                elapsed = int(time.monotonic() - upload_started)\n                await update(f"📄 {filename}\\n\\n☁️ Processing upload…\\n⏳ {max(1, 60 - elapsed)}s left (estimate)")\n            await asyncio.sleep(2)\n        url = str(asset.get("display_url") or "").strip()\n        if not url:\n            raise PlaybookError(str(asset.get("source_error") or "Playbook asset has no display_url"))\n        await update(f"📄 {filename}\\n\\n📤 Sending to destination…\\n⏳ finalizing…")\n        sent = await flood(lambda: user_client.send_file(dest, url, name=filename, caption=caption(msg)))\n        try:\n            await client.delete_asset(asset_token)\n        except Exception:\n            pass\n        if status is not None:\n            await update(f"✅ {filename}\\n\\n📤 Sent successfully\\n⏱️ {int(time.monotonic() - started_at)}s")\n        return sent\n    finally:\n        try:\n            path.unlink(missing_ok=True)\n        except OSError:\n            pass\n'''
bot = bot[:send_start] + new_send + bot[send_end:]

# 5) Process display: filename, per-file stages, progress-safe edits.
proc_start = bot.index('async def process(job, status):')
proc_end = bot.index('\n@dp.message(CommandStart())', proc_start)
new_proc = '''async def process(job, status):\n    async with SEM:\n        selected = [job.candidates[mid] for mid in job.candidates if mid in job.selected]\n        done = failed = 0\n        total_files = len(selected)\n        for index, msg in enumerate(selected, 1):\n            filename = safe_filename(msg)\n            try:\n                await status.edit_text(f"📄 {filename}\\n\\n🚀 Starting {index}/{total_files}\\n⏳ calculating…", reply_markup=menu())\n                await send_destination(msg, job.destination, status=status, index=index, total_files=total_files)\n                await send_copy(msg, job.owner_id)\n                done += 1\n            except Exception as exc:\n                failed += 1\n                print(f"File {getattr(msg, 'id', '?')} failed: {type(exc).__name__}: {exc}", flush=True)\n                await status.edit_text(f"❌ {filename}\\n\\nFailed: {type(exc).__name__}: {exc}\\n\\n✅ Sent: {done} | ⚠️ Failed: {failed}", reply_markup=menu())\n                continue\n            await status.edit_text(f"✅ {filename}\\n\\nCompleted {index}/{total_files}\\n✅ Sent: {done}\\n⚠️ Failed: {failed}", reply_markup=menu())\n        await status.edit_text(f"✅ Completed\\n\\n📦 Files processed: {total_files}\\n✅ Sent: {done}\\n⚠️ Failed: {failed}\\n\\nDestination: {job.destination}", reply_markup=menu())\n'''
bot = bot[:proc_start] + new_proc + bot[proc_end:]

# 6) Destination command uses persistent store and no longer depends on ephemeral JSON alone.
bot = bot.replace(
'''    set_destination(uid, parts[1])\n    PENDING.pop(uid, None)\n    await message.answer(f"✅ Destination saved: {parts[1]}", reply_markup=menu())\n''',
'''    value = await set_destination(uid, parts[1])\n    PENDING.pop(uid, None)\n    await message.answer(f"✅ Permanent destination saved: {value}\\n\\nYou can change it anytime with 🎯 Destination.", reply_markup=menu())\n''', 1)

# 7) All destination checks/load before scans.
bot = bot.replace(
'''    if not destination(uid):\n        return await message.answer("Set a destination first with 🎯 Destination.", reply_markup=menu())\n''',
'''    await load_destination(uid)\n    if not destination(uid):\n        return await message.answer("Set a destination first with 🎯 Destination.", reply_markup=menu())\n''',
    2,
)

# 8) Short, explicit scan pipeline with a hard wall-clock timeout and heartbeat text.
scan_start = bot.index('    status = await message.answer("🔎 Scanning bulk messages..." if bulk else "🔎 Scanning message...", reply_markup=menu())')
scan_end = bot.index('\n@dp.callback_query(F.data.startswith("pick:"))', scan_start)
scan_block = '''    status = await message.answer("🔎 Scanning bulk messages..." if bulk else "🔎 Scanning message...", reply_markup=menu())\n    try:\n        await ensure_user_client()\n        if bulk:\n            peer, start_id, end_id = bulk\n            await status.edit_text(f"🔎 Resolving Telegram channel…\\n📦 Range: {start_id}–{end_id}", reply_markup=menu())\n            source = await asyncio.wait_for(resolve_message_peer(peer), timeout=SCAN_TIMEOUT_SECONDS)\n            await status.edit_text(f"🔎 Fetching messages {start_id}–{end_id}…", reply_markup=menu())\n            msgs = await asyncio.wait_for(\n                user_client.get_messages(source, ids=list(range(start_id, end_id + 1))),\n                timeout=SCAN_TIMEOUT_SECONDS,\n            )\n            media_msgs = [msg for msg in msgs if msg and getattr(msg, "media", None)]\n            await create_job(uid, source, media_msgs, status)\n            return\n\n        peer, mid = parse_link(text)\n        await status.edit_text(f"🔎 Resolving Telegram channel…\\n📌 Message: {mid}", reply_markup=menu())\n        source = await asyncio.wait_for(resolve_message_peer(peer), timeout=SCAN_TIMEOUT_SECONDS)\n        await status.edit_text(f"🔎 Fetching message {mid}…", reply_markup=menu())\n        msg = await asyncio.wait_for(user_client.get_messages(source, ids=mid), timeout=SCAN_TIMEOUT_SECONDS)\n        if not msg or not getattr(msg, "media", None):\n            return await status.edit_text("That message does not contain downloadable media.", reply_markup=menu())\n        await create_job(uid, source, [msg], status)\n    except asyncio.TimeoutError:\n        await status.edit_text("⏱️ Scan timed out after 35 seconds.\\n\\nCheck that the logged-in Telegram account can open this channel and try again.", reply_markup=menu())\n    except (ValueError, RPCError) as exc:\n        await status.edit_text(f"Could not resolve that message: {exc}", reply_markup=menu())\n    except Exception as exc:\n        print(f"Scan failed: {type(exc).__name__}: {exc}", flush=True)\n        await status.edit_text(f"❌ Scan failed: {type(exc).__name__}: {exc}", reply_markup=menu())\n'''
bot = bot[:scan_start] + scan_block + bot[scan_end:]

# 9) Startup loads persistent destinations for all allowed users.
needle = '''    await configure_command_menu()\n    try:\n        session = await saved_session()\n'''
replacement = '''    await configure_command_menu()\n    for uid in ALLOWED_USER_IDS:\n        await load_destination(uid)\n    try:\n        session = await saved_session()\n'''
if needle not in bot:
    raise SystemExit('main startup marker not found')
bot = bot.replace(needle, replacement, 1)

bot_path.write_text(bot, encoding='utf-8')
store_path.write_text(store, encoding='utf-8')

# Static assertions before committing.
assert 'async def get_destination(self, user_id: int)' in store
assert 'async def set_destination(self, user_id: int, destination: str)' in store
assert 'DESTINATIONS = {}' in bot
assert 'Permanent destination saved' in bot
assert 'format_eta(seconds)' in bot
assert 'progress_callback' in bot
assert 'Scan timed out after 35 seconds' in bot
assert 'source = await asyncio.wait_for(resolve_message_peer(peer)' in bot
print('Voroa UX upgrade patch applied successfully.')
