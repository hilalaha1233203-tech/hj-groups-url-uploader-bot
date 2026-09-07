import asyncio
import os
import runpy

from telethon import utils
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

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
# Use a coalescing progress writer so fast callback bursts do not cancel every
# visible edit before Telegram can display it.
menu_fn = app["menu"]
original_make_progress_callback = app["make_progress_callback"]
original_send_destination = app["send_destination"]
original_saved_session = app["saved_session"]
original_resolve_message_peer = app["resolve_message_peer"]
session_store = app["session_store"]
user_client = app["user_client"]


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
                    if isinstance(status, _ProgressStatus):
                        await status.inner.edit_text(text, reply_markup=menu_fn())
                    else:
                        await status.edit_text(text, reply_markup=menu_fn())
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


async def _durable_saved_session():
    """Prefer MongoDB, but never lose a session because a healthy Mongo has no primary record."""
    value = await original_saved_session()
    if value:
        return value
    try:
        local = session_store._local_get("primary")
        if isinstance(local, dict) and not local.get("_deleted"):
            local_value = str(local.get("session_string", "")).strip()
            if local_value:
                print("[Voroa] Restoring Telegram session from local mirror.", flush=True)
                if getattr(session_store, "healthy", False) and getattr(session_store, "_collection", None) is not None:
                    try:
                        payload = dict(local)
                        payload.pop("_id", None)
                        await session_store._collection.update_one(
                            {"_id": "primary"},
                            {"$set": payload},
                            upsert=True,
                        )
                    except Exception as exc:
                        print(f"[Voroa] Local session restore could not backfill MongoDB: {type(exc).__name__}: {exc}", flush=True)
                return local_value
    except Exception as exc:
        print(f"[Voroa] Local session fallback failed: {type(exc).__name__}: {exc}", flush=True)
    return None


async def _peer_cache_get(peer):
    key = f"peer:{str(peer).strip()}"
    try:
        local = session_store._local_get(key)
        if isinstance(local, dict) and not local.get("_deleted"):
            return local
    except Exception:
        pass
    try:
        if getattr(session_store, "healthy", False) and getattr(session_store, "_collection", None) is not None:
            remote = await session_store._collection.find_one({"_id": key})
            if isinstance(remote, dict) and not remote.get("_deleted"):
                return remote
    except Exception as exc:
        print(f"[Voroa] Peer cache read failed for {peer}: {type(exc).__name__}: {exc}", flush=True)
    return None


async def _peer_cache_set(peer, entity):
    peer_text = str(peer).strip()
    access_hash = getattr(entity, "access_hash", None)
    entity_id = getattr(entity, "channel_id", None) or getattr(entity, "chat_id", None) or getattr(entity, "user_id", None) or getattr(entity, "id", None)
    if entity_id is None:
        return
    kind = type(entity).__name__
    if isinstance(entity, InputPeerChannel):
        kind = "InputPeerChannel"
        access_hash = entity.access_hash
        entity_id = entity.channel_id
    elif isinstance(entity, InputPeerUser):
        kind = "InputPeerUser"
        access_hash = entity.access_hash
        entity_id = entity.user_id
    elif isinstance(entity, InputPeerChat):
        kind = "InputPeerChat"
        entity_id = entity.chat_id
        access_hash = None
    payload = {"peer": peer_text, "kind": kind, "id": int(entity_id), "access_hash": int(access_hash) if access_hash is not None else None}
    key = f"peer:{peer_text}"
    try:
        session_store._local_set(key, payload)
    except Exception:
        pass
    try:
        if getattr(session_store, "healthy", False) and getattr(session_store, "_collection", None) is not None:
            await session_store._collection.update_one({"_id": key}, {"$set": payload}, upsert=True)
    except Exception as exc:
        print(f"[Voroa] Peer cache write failed for {peer_text}: {type(exc).__name__}: {exc}", flush=True)


def _cached_entity(cached):
    if not isinstance(cached, dict):
        return None
    kind = str(cached.get("kind", ""))
    entity_id = int(cached.get("id", 0) or 0)
    access_hash = cached.get("access_hash")
    if not entity_id:
        return None
    if kind in {"Channel", "InputPeerChannel"} and access_hash is not None:
        return InputPeerChannel(entity_id, int(access_hash))
    if kind in {"Chat", "InputPeerChat"}:
        return InputPeerChat(entity_id)
    if kind in {"User", "InputPeerUser"} and access_hash is not None:
        return InputPeerUser(entity_id, int(access_hash))
    return None


async def patched_resolve_message_peer(peer):
    # A known private channel/chat should be resolved from its persisted access
    # hash first. This avoids a slow dialog scan after a redeploy and preserves
    # access even when Telegram's in-memory entity cache was rebuilt.
    cached = await _peer_cache_get(peer)
    cached_entity = _cached_entity(cached)
    if cached_entity is not None:
        try:
            resolved = await user_client.get_input_entity(cached_entity)
            print(f"[Voroa] Resolved {peer} from persistent Telegram peer cache.", flush=True)
            return resolved
        except Exception as exc:
            print(f"[Voroa] Cached peer for {peer} is no longer valid: {type(exc).__name__}: {exc}", flush=True)
    try:
        entity = await original_resolve_message_peer(peer)
        await _peer_cache_set(peer, entity)
        return entity
    except Exception:
        # Preserve the original resolver's precise error and do not fabricate
        # access to a private channel the logged-in account cannot reach.
        raise


app["make_progress_callback"] = patched_make_progress_callback
app["send_destination"] = patched_send_destination
app["saved_session"] = _durable_saved_session
app["resolve_message_peer"] = patched_resolve_message_peer
main = app["main"]

if __name__ == "__main__":
    asyncio.run(main())
