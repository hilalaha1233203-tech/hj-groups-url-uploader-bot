from pathlib import Path
p = Path('telegram_bot.py')
s = p.read_text(encoding='utf-8')
start = s.index('def make_progress_callback(status, filename, stage, started_at):')
end = s.index('\ndef scan_menu(uid):', start)
new = '''def make_progress_callback(status, filename, stage, started_at):\n    state = {"last": 0.0, "task": None}\n    def callback(current, total):\n        now = time.monotonic()\n        if total and current >= total:\n            return\n        if total and now - state["last"] < 0.8:\n            return\n        state["last"] = now\n        if total:\n            percent = max(0.0, min(100.0, (current / total) * 100.0))\n            elapsed = max(0.1, now - started_at)\n            rate = current / elapsed if current > 0 else 0.0\n            eta = ((total - current) / rate) if rate > 0 else None\n            filled = int(percent // 10)\n            text = (f"📄 {filename}\\n\\n{stage}\\n"\n                    f"[{int(percent):3d}%] {'█' * filled}{'░' * (10 - filled)}\\n"\n                    f"⏳ {format_eta(eta)}")\n        else:\n            text = f"📄 {filename}\\n\\n{stage}\\n⏳ calculating…"\n        previous = state.get("task")\n        if previous is not None and not previous.done():\n            previous.cancel()\n        state["task"] = asyncio.create_task(status.edit_text(text, reply_markup=menu()))\n    return callback\n\n'''
s = s[:start] + new + s[end+1:]
p.write_text(s, encoding='utf-8')
assert 'current >= total' in s
assert 'previous.cancel()' in s
print('Progress race hardening applied.')
