"""Persistent Voroa state with MongoDB-first storage and durable local mirroring."""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Callable, Awaitable

import motor.motor_asyncio
from pymongo.errors import PyMongoError

URI = (os.getenv("TELEGRAM_SESSION_DB_URI", "").strip() or os.getenv("TECH_VJ_DATABASE_URL", "").strip())
DB_NAME = os.getenv("TELEGRAM_SESSION_DB_NAME", "hj_groups_url_uploader").strip()
COLLECTION_NAME = os.getenv("TELEGRAM_SESSION_COLLECTION", "telegram_sessions").strip()
TIMEOUT_MS = max(1000, int(os.getenv("TELEGRAM_SESSION_DB_TIMEOUT_MS", "2500")))
CONNECT_TIMEOUT_MS = max(1000, int(os.getenv("TELEGRAM_SESSION_DB_CONNECT_TIMEOUT_MS", "2500")))
MONGO_RETRY_BACKOFF_SECONDS = max(5, int(os.getenv("TELEGRAM_SESSION_DB_RETRY_BACKOFF_SECONDS", "30")))
LOCAL_FILE = Path(os.getenv("TELEGRAM_LOCAL_STATE_FILE", "/data/telegram_media_state.json"))
# Keep the dedicated fallback path backwards-compatible with previous Voroa
# releases, while using the same file when the explicit state path is absent.
LEGACY_LOCAL_FILE = Path(os.getenv("TELEGRAM_LOCAL_SESSION_FILE", "/data/voroa_session_store.json"))


class SessionStore:
    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._last_error: Optional[Exception] = None
        self._mongo_healthy = False
        self._mongo_retry_at = 0.0
        self._local: dict[str, dict[str, Any]] = {}
        self._load_local()
        self._build_client()

    def _load_local(self) -> None:
        loaded = False
        for path in (LOCAL_FILE, LEGACY_LOCAL_FILE):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    self._local.update(value)
                    loaded = True
                    break
            except (OSError, ValueError, TypeError):
                continue
        if not loaded:
            self._local = {}

    def _save_local(self) -> None:
        try:
            LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_FILE.write_text(json.dumps(self._local, indent=2, default=str), encoding="utf-8")
        except (OSError, TypeError) as exc:
            print(f"[Voroa] Local state write failed: {type(exc).__name__}: {exc}", flush=True)

    def _build_client(self) -> None:
        if not URI:
            self._client = None
            self._collection = None
            self._mongo_healthy = False
            return
        self._client = motor.motor_asyncio.AsyncIOMotorClient(
            URI,
            serverSelectionTimeoutMS=TIMEOUT_MS,
            connectTimeoutMS=CONNECT_TIMEOUT_MS,
            socketTimeoutMS=TIMEOUT_MS,
            retryWrites=True,
            appname="Voroa",
        )
        self._collection = self._client[DB_NAME][COLLECTION_NAME]
        self._mongo_healthy = False

    @property
    def configured(self) -> bool:
        return bool(URI and self._collection is not None)

    @property
    def healthy(self) -> bool:
        return self._mongo_healthy

    def _mongo_attempt_allowed(self) -> bool:
        return self.configured and (self._mongo_healthy or time.monotonic() >= self._mongo_retry_at)

    def _mark_mongo_failed(self, exc: Exception) -> None:
        self._last_error = exc
        self._mongo_healthy = False
        self._mongo_retry_at = time.monotonic() + MONGO_RETRY_BACKOFF_SECONDS

    def _mark_mongo_healthy(self) -> None:
        self._last_error = None
        self._mongo_healthy = True
        self._mongo_retry_at = 0.0

    def _local_get(self, key: str) -> Optional[dict[str, Any]]:
        value = self._local.get(key)
        return dict(value) if isinstance(value, dict) else None

    def _local_set(self, key: str, value: dict[str, Any]) -> None:
        self._local[key] = dict(value)
        self._save_local()

    def _local_delete(self, key: str) -> None:
        self._local.pop(key, None)
        self._save_local()

    def _local_tombstone(self, key: str) -> None:
        self._local[key] = {
            "_deleted": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_local()

    @staticmethod
    def _timestamp(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

    async def _sync_local_to_mongo(self) -> None:
        """Replay local writes/deletes after MongoDB recovery without losing newer state."""
        if not self._mongo_healthy or self._collection is None or not self._local:
            return
        for key, payload in list(self._local.items()):
            if not isinstance(payload, dict):
                continue
            try:
                remote = await self._collection.find_one({"_id": key})
                local_ts = self._timestamp(payload.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)
                remote_ts = self._timestamp((remote or {}).get("updated_at")) if isinstance(remote, dict) else None
                if payload.get("_deleted"):
                    if remote is not None and (remote_ts is None or local_ts >= remote_ts):
                        await self._collection.delete_one({"_id": key})
                    self._local.pop(key, None)
                elif remote is None:
                    document = dict(payload)
                    document["_id"] = key
                    await self._collection.insert_one(document)
                    self._local.pop(key, None)
                elif remote_ts is None or local_ts > remote_ts:
                    replacement = dict(payload)
                    replacement.pop("_id", None)
                    await self._collection.update_one({"_id": key}, {"$set": replacement})
                    self._local.pop(key, None)
                else:
                    self._local.pop(key, None)
            except (PyMongoError, OSError, TimeoutError) as exc:
                self._mark_mongo_failed(exc)
                print(f"[Voroa] Local-to-Mongo sync paused: {type(exc).__name__}: {exc}", flush=True)
                self._save_local()
                return
        self._save_local()

    async def ping(self) -> bool:
        if not self.configured:
            self._mongo_healthy = False
            print("[Voroa] MongoDB not configured; using local fallback storage.", flush=True)
            return False
        # Startup must never spend ~30 seconds retrying a dead database before
        # Telegram polling can begin. One short probe is enough; later calls can
        # retry after the backoff period without blocking command handlers.
        if self._mongo_healthy:
            return True
        if not self._mongo_attempt_allowed():
            return False
        try:
            await asyncio.wait_for(self._client.admin.command("ping"), timeout=TIMEOUT_MS / 1000)
            self._mark_mongo_healthy()
            await self._sync_local_to_mongo()
            print("[Voroa] MongoDB connected.", flush=True)
            return True
        except Exception as exc:
            self._mark_mongo_failed(exc)
            print(
                f"[Voroa] MongoDB unavailable; local mirror is active: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    async def _mongo(self, operation: Callable[[Any], Awaitable[Any]]):
        if not self.configured or not self._mongo_attempt_allowed():
            return None
        try:
            result = await asyncio.wait_for(operation(self._collection), timeout=TIMEOUT_MS / 1000)
            self._mark_mongo_healthy()
            return result
        except (PyMongoError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
            self._mark_mongo_failed(exc)
            print(f"[Voroa] MongoDB operation unavailable; using local mirror: {type(exc).__name__}: {exc}", flush=True)
            return None

    async def get(self) -> Optional[str]:
        # When Mongo is unhealthy, local state is authoritative for responsiveness.
        if not self._mongo_healthy:
            local = self._local_get("primary")
            if (local or {}).get("_deleted"):
                return None
            value = (local or {}).get("session_string", "")
            return str(value).strip() or None
        document = await self._mongo(lambda c: c.find_one({"_id": "primary"}))
        if isinstance(document, dict):
            value = document.get("session_string", "")
            return str(value).strip() or None
        return None

    async def set(self, session_string: str) -> None:
        value = session_string.strip()
        if not value:
            raise ValueError("Cannot persist an empty Telegram session.")
        payload = {"session_string": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        # Always persist locally first so login succeeds even if Atlas is down.
        self._local_set("primary", payload)
        result = await self._mongo(lambda c: c.update_one({"_id": "primary"}, {"$set": payload}, upsert=True))
        if result is None and self.configured:
            print("[Voroa] Telegram session saved to local mirror; MongoDB sync pending recovery.", flush=True)

    async def clear(self) -> None:
        # Local state is updated immediately; Mongo delete is best-effort.
        result = await self._mongo(lambda c: c.delete_one({"_id": "primary"}))
        if result is None and self.configured:
            self._local_tombstone("primary")
            print("[Voroa] Telegram session delete queued for MongoDB recovery.", flush=True)
        else:
            self._local_delete("primary")

    async def get_login(self, user_id: int) -> Optional[Dict[str, Any]]:
        key = f"login:{int(user_id)}"
        if not self._mongo_healthy:
            document = self._local_get(key)
        else:
            document = await self._mongo(lambda c: c.find_one({"_id": key}))
            if not isinstance(document, dict) and not self._mongo_healthy:
                document = self._local_get(key)
        if not document or document.get("_deleted"):
            return None
        return {
            "phone": str(document.get("phone", "")).strip(),
            "phone_code_hash": str(document.get("phone_code_hash", "")).strip(),
            "session_string": str(document.get("session_string", "")).strip(),
        }

    async def set_login(self, user_id: int, phone: str, phone_code_hash: str, session_string: str) -> None:
        key = f"login:{int(user_id)}"
        payload = {
            "phone": phone.strip(),
            "phone_code_hash": phone_code_hash.strip(),
            "session_string": session_string.strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._local_set(key, payload)
        result = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        if result is None and self.configured:
            print(f"[Voroa] Login state {user_id} saved to local mirror; MongoDB sync pending recovery.", flush=True)

    async def clear_login(self, user_id: int) -> None:
        key = f"login:{int(user_id)}"
        result = await self._mongo(lambda c: c.delete_one({"_id": key}))
        if result is None and self.configured:
            self._local_tombstone(key)
            print(f"[Voroa] Login state {user_id} delete queued for MongoDB recovery.", flush=True)
        else:
            self._local_delete(key)

    async def get_destination(self, user_id: int) -> Optional[str]:
        key = f"destination:{int(user_id)}"
        if not self._mongo_healthy:
            document = self._local_get(key)
        else:
            document = await self._mongo(lambda c: c.find_one({"_id": key}))
            if not isinstance(document, dict) and not self._mongo_healthy:
                document = self._local_get(key)
        if not document or document.get("_deleted"):
            return None
        value = document.get("destination", "")
        return str(value).strip() or None

    async def set_destination(self, user_id: int, destination: str) -> None:
        value = destination.strip()
        if not value:
            raise ValueError("Cannot persist an empty destination.")
        key = f"destination:{int(user_id)}"
        payload = {"destination": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        self._local_set(key, payload)
        result = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        if result is None and self.configured:
            print(f"[Voroa] Destination {user_id} saved to local mirror; MongoDB sync pending recovery.", flush=True)

    async def clear_destination(self, user_id: int) -> None:
        key = f"destination:{int(user_id)}"
        result = await self._mongo(lambda c: c.delete_one({"_id": key}))
        if result is None and self.configured:
            self._local_tombstone(key)
            print(f"[Voroa] Destination {user_id} delete queued for MongoDB recovery.", flush=True)
        else:
            self._local_delete(key)

    async def get_access(self, user_id: int) -> Optional[dict[str, Any]]:
        key = f"access:{int(user_id)}"
        if not self._mongo_healthy:
            document = self._local_get(key)
        else:
            document = await self._mongo(lambda c: c.find_one({"_id": key}))
            if not isinstance(document, dict) and not self._mongo_healthy:
                document = self._local_get(key)
        if not document or document.get("_deleted"):
            return None
        return {
            "user_id": int(user_id),
            "active": bool(document.get("active", True)),
            "expires_at": document.get("expires_at"),
            "role": str(document.get("role", "user")),
        }

    async def set_access(self, user_id: int, active: bool = True, expires_at: Optional[datetime] = None, role: str = "user") -> None:
        key = f"access:{int(user_id)}"
        payload = {
            "user_id": int(user_id),
            "active": bool(active),
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
            "role": role,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._local_set(key, payload)
        result = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        if result is None and self.configured:
            print(f"[Voroa] Access state {user_id} saved to local mirror; MongoDB sync pending recovery.", flush=True)

    async def list_access(self) -> list[dict[str, Any]]:
        if self._mongo_healthy and self._collection is not None:
            try:
                cursor = self._collection.find({"_id": {"$regex": r"^access:"}})
                rows = []
                async for document in cursor:
                    if document.get("_deleted"):
                        continue
                    rows.append({
                        "user_id": int(document.get("user_id")),
                        "active": bool(document.get("active", True)),
                        "expires_at": document.get("expires_at"),
                        "role": str(document.get("role", "user")),
                    })
                return rows
            except (PyMongoError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
                self._mark_mongo_failed(exc)
                print(f"[Voroa] Access listing fell back to local mirror: {type(exc).__name__}: {exc}", flush=True)
        rows = []
        for document in self._local.values():
            if not isinstance(document, dict) or "user_id" not in document or document.get("_deleted"):
                continue
            rows.append({
                "user_id": int(document.get("user_id")),
                "active": bool(document.get("active", True)),
                "expires_at": document.get("expires_at"),
                "role": str(document.get("role", "user")),
            })
        return rows

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()


session_store = SessionStore()
