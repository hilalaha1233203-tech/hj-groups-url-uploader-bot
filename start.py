import asyncio
import os
import runpy

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

# The standalone Telegram application contains the complete bot UI and
# persistent reply-keyboard menu.
app = runpy.run_path("telegram_bot.py", run_name="hj_groups_app")

# The Telegram/Playbook transfer callbacks can fire many times per second.
# The old implementation scheduled and then cancelled edit tasks on every
# callback, so a real transfer could appear stuck on "Starting".  Use a
# coalescing progress writer instead: only one editor runs, and it always
# publishes the newest progress value.  The wrapper also cancels any pending
# progress update before a direct stage/final-status edit so an old progress
# value can never overwrite the final result.
menu_fn = app["menu"]
original_make_progress_callback = app["make_progress_callback"]
original_send_destination = app["send_destination"]


class _ProgressStatus:
    def __init__(self, inner):
        self.inner = inner
        self.progress_task = None

    async def _cancel_progress(self):
        task = self.progress_task
        if task is None or task.done() or task is asyncio.current_task():
            if task is not None and task.done():
                self.progress_task = None
            return
        self.progress_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def edit_text(self, text, **kwargs):
        await self._cancel_progress()
        return await self.inner.edit_text(text, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def patched_make_progress_callback(status, filename, stage, started_at):
    state = {"last": 0.0, "pending": None, "task": None}

    async def worker():
        try:
            while state["pending"] is not None:
                text = state["pending"]
                state["pending"] = None
                try:
                    await status.inner.edit_text(text, reply_markup=menu_fn()) if isinstance(status, _ProgressStatus) else await status.edit_text(text, reply_markup=menu_fn())
                except Exception:
                    pass
                if state["pending"] is not None:
                    await asyncio.sleep(0.8)
        except asyncio.CancelledError:
            raise
        finally:
            if state["task"] is asyncio.current_task():
                state["task"] = None
                if isinstance(status, _ProgressStatus) and status.progress_task is asyncio.current_task():
                    status.progress_task = None

    def callback(current, total):
        import time
        now = time.monotonic()
        if total and current >= total:
            return
        if total and now - state["last"] < 0.8:
            return
        state["last"] = now
        if total:
            percent = max(0.0, min(100.0, (current / total) * 100.0))
            elapsed = max(0.1, now - started_at)
            rate = current / elapsed if current > 0 else 0.0
            eta = ((total - current) / rate) if rate > 0 else None
            filled = int(percent // 10)
            if eta is None:
                eta_text = "calculating…"
            else:
                eta_s = int(round(eta))
                if eta_s < 1:
                    eta_text = "0s left"
                elif eta_s < 60:
                    eta_text = f"{eta_s}s left"
                else:
                    mins, secs = divmod(eta_s, 60)
                    eta_text = f"{mins}m {secs}s left"
            text = f"📄 {filename}\n\n{stage}\n[{int(percent):3d}%] {'█' * filled}{'░' * (10 - filled)}\n⏳ {eta_text}"
        else:
            text = f"📄 {filename}\n\n{stage}\n⏳ calculating…"
        state["pending"] = text
        if state["task"] is None or state["task"].done():
            state["task"] = asyncio.create_task(worker())
            if isinstance(status, _ProgressStatus):
                status.progress_task = state["task"]

    return callback


async def patched_send_destination(msg, dest, status=None, index=1, total_files=1):
    if status is None:
        return await original_send_destination(msg, dest, status=None, index=index, total_files=total_files)
    wrapped = status if isinstance(status, _ProgressStatus) else _ProgressStatus(status)
    return await original_send_destination(msg, dest, status=wrapped, index=index, total_files=total_files)


app["make_progress_callback"] = patched_make_progress_callback
app["send_destination"] = patched_send_destination
main = app["main"]

if __name__ == "__main__":
    asyncio.run(main())
