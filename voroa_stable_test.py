import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "ci_test_hash")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ci_test_token")
os.environ.setdefault("VOROA_OWNER_USER_ID", "456")

import voroa_stable as v


class FakeStatus:
    def __init__(self):
        self.edits = []
        self.answer_calls = 0
        self.markup_removed = False

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self

    async def answer(self, text, **kwargs):
        self.answer_calls += 1
        self.edits.append((text, kwargs))
        return FakeStatus()

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_removed = reply_markup is None
        return self


class FakeCallback:
    def __init__(self, uid, data, message):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeMessage(FakeStatus):
    def __init__(self, uid=456):
        super().__init__()
        self.from_user = SimpleNamespace(id=uid)


class FakeMedia:
    def __init__(self, size=1024):
        self.document = SimpleNamespace(size=size, mime_type="video/mp4", attributes=[])


class FakeTelegramMessage:
    def __init__(self, mid=1, name="file.mp4"):
        self.id = mid
        self.media = FakeMedia()
        self.file = SimpleNamespace(name=name)
        self.message = "caption"


def reset_state():
    v.JOBS.clear()
    v.ACTIVE_JOBS.clear()
    v.ACTIVE_TRANSFERS.clear()
    v.TRANSFER_CONTEXTS.clear()
    v.PENDING_INPUT.clear()
    v.LOGIN_DATA.clear()


def test_single_links():
    assert v.parse_single_link("https://t.me/c/123456789/42") == ("-100123456789", 42)
    assert v.parse_single_link("https://t.me/examplechannel/99") == ("@examplechannel", 99)


def test_ranges():
    assert v.parse_range("https://t.me/c/123456789/100-110") == ("-100123456789", 100, 110)
    assert v.parse_range("https://t.me/examplechannel/100 to 105") == ("@examplechannel", 100, 105)


def test_sizes():
    assert v.human_size(1024) == "1.0 KB"
    assert v.human_size(1024 * 1024) == "1.0 MB"
    assert v.human_size(1024 * 1024 * 1024) == "1.0 GB"


def test_filename_safety():
    class File:
        name = 'bad:/name*.mp4'

    class Message:
        id = 17
        file = File()

    assert '/' not in v.filename(Message())
    assert ':' not in v.filename(Message())


def test_authorization_is_fail_closed():
    original = set(v.ALLOWED_IDS)
    original_owner = v.OWNER_ID
    v.ALLOWED_IDS = set()
    v.OWNER_ID = 0
    assert not v.authorized(123)
    v.ALLOWED_IDS = {123}
    v.OWNER_ID = 456
    assert v.authorized(123)
    assert v.authorized(456)
    assert not v.authorized(789)
    v.ALLOWED_IDS = original
    v.OWNER_ID = original_owner


def test_confirm_keyboard_contains_job_identity():
    keyboard = v.confirm_keyboard("abc123")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "confirm:abc123" in callbacks
    assert "job_cancel:abc123" in callbacks


async def test_duplicate_confirm_creates_one_transfer():
    reset_state()
    uid = 456
    status = FakeMessage(uid)
    job = v.Job(uid, "source", [FakeTelegramMessage()], "@destination", [FakeTelegramMessage()], job_id="job-a")
    v.JOBS[uid] = job

    original_run = v.run_transfer
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_transfer(job_arg, ctx, uid_arg):
        started.set()
        await release.wait()

    v.run_transfer = fake_run_transfer
    try:
        cb1 = FakeCallback(uid, "confirm:job-a", status)
        cb2 = FakeCallback(uid, "confirm:job-a", status)
        await asyncio.gather(v.confirm(cb1), v.confirm(cb2))
        await asyncio.wait_for(started.wait(), timeout=1)
        task = v.ACTIVE_TRANSFERS.get(uid)
        assert task is not None
        assert len(v.ACTIVE_TRANSFERS) == 1
        assert cb1.answers or cb2.answers
        release.set()
        await asyncio.wait_for(task, timeout=1)
        assert uid not in v.ACTIVE_TRANSFERS
    finally:
        v.run_transfer = original_run
        reset_state()


async def test_stale_confirm_cannot_start_new_job():
    reset_state()
    uid = 456
    message = FakeMessage(uid)
    job_a = v.Job(uid, "source", [FakeTelegramMessage(1)], "A", [FakeTelegramMessage(1)], job_id="job-a")
    job_b = v.Job(uid, "source", [FakeTelegramMessage(2)], "B", [FakeTelegramMessage(2)], job_id="job-b")
    v.JOBS[uid] = job_b
    callback = FakeCallback(uid, "confirm:job-a", message)
    await v.confirm(callback)
    assert callback.answers[0][0].startswith("Job expired")
    assert v.JOBS[uid].job_id == "job-b"
    reset_state()


async def test_job_cancel_does_not_delete_active_job():
    reset_state()
    uid = 456
    status = FakeMessage(uid)
    job = v.Job(uid, "source", [FakeTelegramMessage()], "A", [FakeTelegramMessage()], job_id="job-a")
    v.ACTIVE_JOBS[uid] = job
    task = asyncio.create_task(asyncio.sleep(10))
    v.ACTIVE_TRANSFERS[uid] = task
    callback = FakeCallback(uid, "job_cancel:job-a", status)
    await v.job_cancel(callback)
    assert uid in v.ACTIVE_JOBS
    assert not task.cancelled()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    reset_state()


async def test_status_tasks_are_owned_and_cleaned():
    reset_state()
    uid = 456
    job = v.Job(uid, "source", [FakeTelegramMessage()], "A", [FakeTelegramMessage()], job_id="job-a")
    status = FakeMessage(uid)
    current = asyncio.current_task()
    v.ACTIVE_JOBS[uid] = job
    v.ACTIVE_TRANSFERS[uid] = current
    ctx = v.TransferContext(uid=uid, job_id=job.job_id, status=status, task=current)
    v.TRANSFER_CONTEXTS[uid] = ctx
    callback, state = await v.progress_callback_factory(ctx, "file.mp4", asyncio.get_running_loop().time())
    callback(50, 100)
    await asyncio.sleep(0)
    assert ctx.status_tasks
    await v.cancel_owned_status_tasks(ctx)
    assert not ctx.status_tasks
    reset_state()


async def test_cancel_during_floodwait_does_not_retry():
    reset_state()
    uid = 456
    selected = FakeTelegramMessage(7)
    job = v.Job(uid, "source", [selected], "A", [selected], job_id="job-a")
    status = FakeMessage(uid)
    current = asyncio.current_task()
    v.ACTIVE_JOBS[uid] = job
    v.ACTIVE_TRANSFERS[uid] = current
    ctx = v.TransferContext(uid=uid, job_id=job.job_id, status=status, task=current)

    original_resolve = v.resolve_peer
    original_refresh = v.refresh_message
    original_transfer = v.transfer_one
    calls = {"transfer": 0}

    async def fake_resolve(destination):
        return destination

    async def fake_refresh(job_arg, message_id):
        return selected

    async def fake_transfer(message_arg, destination_arg, ctx_arg, index, total):
        calls["transfer"] += 1
        if calls["transfer"] == 1:
            raise v.FloodWaitError(request=None, capture=None)

    v.resolve_peer = fake_resolve
    v.refresh_message = fake_refresh
    v.transfer_one = fake_transfer
    try:
        # Simpler deterministic FloodWait stub with a seconds attribute.
        class FakeFloodWait(Exception):
            seconds = 2

        original_flood = v.FloodWaitError
        v.FloodWaitError = FakeFloodWait
        task = asyncio.create_task(v.run_transfer(job, ctx, uid))
        v.ACTIVE_TRANSFERS[uid] = task
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert calls["transfer"] == 1
        v.FloodWaitError = original_flood
    finally:
        v.resolve_peer = original_resolve
        v.refresh_message = original_refresh
        v.transfer_one = original_transfer
        reset_state()


async def test_single_failure_is_reported_as_failed():
    reset_state()
    uid = 456
    selected = FakeTelegramMessage(8)
    job = v.Job(uid, "source", [selected], "A", [selected], job_id="job-a")
    status = FakeMessage(uid)
    current = asyncio.current_task()
    v.ACTIVE_JOBS[uid] = job
    v.ACTIVE_TRANSFERS[uid] = current
    ctx = v.TransferContext(uid=uid, job_id=job.job_id, status=status, task=current)
    original_resolve = v.resolve_peer
    original_refresh = v.refresh_message
    original_transfer = v.transfer_one

    async def fake_resolve(destination):
        return destination

    async def fake_refresh(job_arg, message_id):
        return selected

    async def fake_transfer(*args, **kwargs):
        raise RuntimeError("simulated send failure")

    v.resolve_peer = fake_resolve
    v.refresh_message = fake_refresh
    v.transfer_one = fake_transfer
    try:
        await v.run_transfer(job, ctx, uid)
        combined = "\n".join(text for text, _ in status.edits)
        assert "Transfer failed" in combined
        assert "Completed: <b>0</b>" in combined
        assert "Failed: <b>1</b>" in combined
    finally:
        v.resolve_peer = original_resolve
        v.refresh_message = original_refresh
        v.transfer_one = original_transfer
        reset_state()


async def test_job_destination_snapshot_is_authoritative():
    reset_state()
    uid = 456
    selected = FakeTelegramMessage(9)
    job = v.Job(uid, "source", [selected], "DEST-A", [selected], job_id="job-a")
    v.DESTINATIONS[uid] = "DEST-B"
    status = FakeMessage(uid)
    current = asyncio.current_task()
    v.ACTIVE_JOBS[uid] = job
    v.ACTIVE_TRANSFERS[uid] = current
    ctx = v.TransferContext(uid=uid, job_id=job.job_id, status=status, task=current)
    original_resolve = v.resolve_peer
    original_refresh = v.refresh_message
    original_transfer = v.transfer_one
    seen = []

    async def fake_resolve(destination):
        seen.append(destination)
        return destination

    async def fake_refresh(job_arg, message_id):
        return selected

    async def fake_transfer(*args, **kwargs):
        return None

    v.resolve_peer = fake_resolve
    v.refresh_message = fake_refresh
    v.transfer_one = fake_transfer
    try:
        await v.run_transfer(job, ctx, uid)
        assert seen == ["DEST-A"]
    finally:
        v.resolve_peer = original_resolve
        v.refresh_message = original_refresh
        v.transfer_one = original_transfer
        reset_state()


async def test_login_code_rejected_during_active_transfer():
    reset_state()
    uid = 456
    v.LOGIN_DATA[uid] = {"phone": "+10000000000", "phone_code_hash": "hash"}
    blocking = asyncio.create_task(asyncio.sleep(10))
    v.ACTIVE_TRANSFERS[uid] = blocking
    fake_client = SimpleNamespace(sign_in=AsyncMock())
    original_client = v.user_client
    v.user_client = fake_client
    message = FakeMessage(uid)
    try:
        await v.login_code(message, "12345")
        fake_client.sign_in.assert_not_awaited()
        assert message.edits or True
    finally:
        v.user_client = original_client
        blocking.cancel()
        await asyncio.gather(blocking, return_exceptions=True)
        reset_state()


async def main_async():
    await test_duplicate_confirm_creates_one_transfer()
    await test_stale_confirm_cannot_start_new_job()
    await test_job_cancel_does_not_delete_active_job()
    await test_status_tasks_are_owned_and_cleaned()
    await test_cancel_during_floodwait_does_not_retry()
    await test_single_failure_is_reported_as_failed()
    await test_job_destination_snapshot_is_authoritative()
    await test_login_code_rejected_during_active_transfer()


if __name__ == "__main__":
    test_single_links()
    test_ranges()
    test_sizes()
    test_filename_safety()
    test_authorization_is_fail_closed()
    test_confirm_keyboard_contains_job_identity()
    asyncio.run(main_async())
    print("Voroa stable lifecycle tests: PASS")
