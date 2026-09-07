"""Persistent Voroa state with MongoDB-first storage and durable local mirroring."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Callable, Awaitable

import motor.motor_asyncio
from pymongo.errors import PyMongoError

URI = (os.getenv("TELEGRAM_SESSION_DB_URI", "").strip() or os.getenv("TECH_VJ_DATABASE_URL", "").strip())
DB_NAME = os.getenv("TELEGRAM_SESSION_DB_NAME", "hj_groups_url_uploader").strip()
COLLECTION_NAME = os.getenv("TELEGRAM_SESSION_COLLECTION", "telegram_sessions").strip()
TIMEOUT_MS = int(os.getenv("TELEGRAM_SESSION_DB_TIMEOUT_MS", "10000"))
CONNECT_TIMEOUT_MS = int(os.getenv("TELEGRAM_SESSION_DB_CONNECT_TIMEOUT_MS", "10000"))
# Voroa service filesystems are ephemeral. Use /data when a persistent disk is
# attached; operators can override this with TELEGRAM_LOCAL_STATE_FILE.
LOCAL_FILE = Path(os.getenv("TELEGRAM_LOCAL_STATE_FILE", "/data/voroa_session_store.json"))


class SessionStore:
    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._last_error: Optional[Exception] = None
        self._mongo_healthy = False
        self._local: dict[str, dict[str, Any]] = {}
        self._load_local()
        self._build_client()

    def _load_local(self) -> None:
        try:
            value = json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self._local = value
        except (OSError, ValueError, TypeError):
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

    def _local_get(self, key: str) -> Optional[dict[str, Any]]:
        value = self._local.get(key)
        return dict(value) if isinstance(value, dict) else None

    def _local_set(self, key: str, value: dict[str, Any]) -> None:
        self._local[key] = dict(value)
        self._save_local()

    def _local_delete(self, key: str) -> None:
        self._local.pop(key, None)
        self._save_local()

    async def ping(self) -> bool:
        if not self.configured:
            self._mongo_healthy = False
            print("[Voroa] MongoDB not configured; using local fallback storage.", flush=True)
            return False
        for attempt in range(1, 4):
            try:
                await self._client.admin.command("ping")
                self._last_error = None
                self._mongo_healthy = True
                print(f"[Voroa] MongoDB connected (attempt {attempt}).", flush=True)
                return True
            except Exception as exc:
                self._last_error = exc
                self._mongo_healthy = False
                if attempt < 3:
                    await __import__("asyncio").sleep(min(2 * attempt, 5))
                else:
                    print(
                        f"[Voroa] MongoDB unavailable after {attempt} attempts; local mirror is active: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
        return False

    async def _mongo(self, operation: Callable[[Any], Awaitable[Any]]):
        if not self.configured:
            return None
        try:
            result = await operation(self._collection)
            self._last_error = None
            self._mongo_healthy = True
            return result
        except (PyMongoError, OSError, TimeoutError) as exc:
            self._last_error = exc
            self._mongo_healthy = False
            print(f"[Voroa] MongoDB operation unavailable; using local mirror: {type(exc).__name__}: {exc}", flush=True)
            return None

    async def get(self) -> Optional[str]:
        document = await self._mongo(lambda c: c.find_one({"_id": "primary"}))
        if isinstance(document, dict):
            value = document.get("session_string", "")
            return str(value).strip() or None
        if self._mongo_healthy:
            return None
        local = self._local_get("primary")
        value = (local or {}).get("session_string", "")
        return str(value).strip() or None

    async def set(self, session_string: str) -> None:
        value = session_string.strip()
        if not value:
            raise ValueError("Cannot persist an empty Telegram session.")
        payload = {"session_string": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        result = await self._mongo(lambda c: c.update_one({"_id": "primary"}, {"$set": payload}, upsert=True))
        self._local_set("primary", payload)
        if result is None and self.configured:
            print("[Voroa] Telegram session saved only to local mirror because MongoDB is unavailable.", flush=True)

    async def clear(self) -> None:
        result = await self._mongo(lambda c: c.delete_one({"_id": "primary"}))
        self._local_delete("primary")
        if result is None and self.configured and self._mongo_healthy is False:
            print("[Voroa] Telegram primary session removed from local mirror; MongoDB delete pending availability.", flush=True)

    async def get_login(self, user_id: int) -> Optional[Dict[str, Any]]:
        key = f"login:{int(user_id)}"
        document = await self._mongo(lambda c: c.find_one({"_id": key}))
        if not isinstance(document, dict):
            if self._mongo_healthy:
                return None
            document = self._local_get(key)
        if not document:
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
        result = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        self._local_set(key, payload)
        if result is None and self.configured:
            print(f"[Voroa] Login state {user_id} saved only to local mirror because MongoDB is unavailable.", flush=True)

    async def clear_login(self, user_id: int) -> None:
        key = f"login:{int(user_id)}"
        result = await self._mongo(lambda c: c.delete_one({"_id": key}))
        self._local_delete(key)
        if result is None and self.configured and not self._mongo_healthy:
            print(f"[Voroa] Login state {user_id} removed locally; MongoDB delete is unavailable.", flush=True)

    async def get_destination(self, user_id: int) -> Optional[str]:
        key = f"destination:{int(user_id)}"
        document = await self._mongo(lambda c: c.find_one({"_id": key}))
        if not isinstance(document, dict):
            if self._mongo_healthy:
                return None
            document = self._local_get(key)
        value = (document or {}).get("destination", "")
        return str(value).strip() or None

    async def set_destination(self, user_id: int, destination: str) -> None:
        value = destination.strip()
        if not value:
            raise ValueError("Cannot persist an empty destination.")
        key = f"destination:{int(user_id)}"
        payload = {"destination": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        result = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        self._local_set(key, payload)
        if result is None and self.configured:
            print(f"[Voroa] Destination {user_id} saved only to local mirror because MongoDB is unavailable.", flush=True)

    async def clear_destination(self, user_id: int) -> None:
        key = f"destination:{int(user_id)}"
        result = await self._mongo(lambda c: c.delete_one({"_id": key}))
        self._local_delete(key)
        if result is None and self.configured and not self._mongo_healthy:
            print(f"[Voroa] Destination {user_id} removed locally; MongoDB delete is unavailable.", flush=True)

    async def get_access(self, user_id: int) -> Optional[dict[str, Any]]:
        key = f"access:{int(user_id)}"
        document = await self._mongo(lambda c: c.find_one({"_id": key}))
        if not isinstance(document, dict):
            if self._mongo_healthy:
                return None
            document = self._local_get(key)
        if not document:
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
        result = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        self._local_set(key, payload)
        if result is None and self.configured:
            print(f"[Voroa] Access state {user_id} saved only to local mirror because MongoDB is unavailable.", flush=True)

    async def list_access(self) -> list[dict[str, Any]]:
        if self._mongo_healthy and self._collection is not None:
            try:
                cursor = self._collection.find({"_id": {"$regex": r"^access:"}})
                rows = []
                async for document in cursor:
                    rows.append({
                        "user_id": int(document.get("user_id")),
                        "active": bool(document.get("active", True)),
                        "expires_at": document.get("expires_at"),
                        "role": str(document.get("role", "user")),
                    })
                return rows
            except (PyMongoError, OSError, TimeoutError) as exc:
                self._last_error = exc
                self._mongo_healthy = False
                print(f"[Voroa] Access listing fell back to local mirror: {type(exc).__name__}: {exc}", flush=True)
        rows = []
        for document in self._local.values():
            if not isinstance(document, dict) or "user_id" not in document:
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
