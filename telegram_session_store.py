"""Persistent Voroa state in MongoDB.

Telegram sessions, destinations and user access are deployment-independent.  The
local /tmp fallback is intentionally NOT used for durable state: losing MongoDB
must never look like a successful save followed by silent data loss on restart.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import motor.motor_asyncio
from pymongo.errors import PyMongoError

URI = (os.getenv("TELEGRAM_SESSION_DB_URI", "").strip() or os.getenv("TECH_VJ_DATABASE_URL", "").strip())
DB_NAME = os.getenv("TELEGRAM_SESSION_DB_NAME", "hj_groups_url_uploader").strip()
COLLECTION_NAME = os.getenv("TELEGRAM_SESSION_COLLECTION", "telegram_sessions").strip()
TIMEOUT_MS = int(os.getenv("TELEGRAM_SESSION_DB_TIMEOUT_MS", "10000"))
CONNECT_TIMEOUT_MS = int(os.getenv("TELEGRAM_SESSION_DB_CONNECT_TIMEOUT_MS", "10000"))


class SessionStore:
    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._last_error = None
        self._build_client()

    def _build_client(self) -> None:
        if not URI:
            return
        self._client = motor.motor_asyncio.AsyncIOMotorClient(
            URI,
            serverSelectionTimeoutMS=TIMEOUT_MS,
            connectTimeoutMS=CONNECT_TIMEOUT_MS,
            retryWrites=True,
            appname="Voroa",
        )
        self._collection = self._client[DB_NAME][COLLECTION_NAME]

    @property
    def configured(self) -> bool:
        return bool(URI and self._collection is not None)

    async def ping(self) -> None:
        if not self.configured:
            raise RuntimeError("TELEGRAM_SESSION_DB_URI is not configured. Durable Telegram login/destination storage requires MongoDB.")
        try:
            await self._client.admin.command("ping")
            self._last_error = None
        except Exception as exc:
            self._last_error = exc
            raise RuntimeError(f"MongoDB is configured but unavailable: {type(exc).__name__}: {exc}") from exc

    async def _mongo(self, operation):
        if not self.configured:
            raise RuntimeError("MongoDB durable storage is not configured.")
        last = None
        for attempt in range(2):
            try:
                result = await operation(self._collection)
                self._last_error = None
                return result
            except (PyMongoError, OSError, TimeoutError) as exc:
                last = exc
                self._last_error = exc
                if attempt == 0:
                    try:
                        if self._client is not None:
                            self._client.close()
                    except Exception:
                        pass
                    self._build_client()
        raise RuntimeError(f"MongoDB operation failed: {type(last).__name__}: {last}") from last

    async def get(self) -> Optional[str]:
        document = await self._mongo(lambda c: c.find_one({"_id": "primary"}))
        value = (document or {}).get("session_string", "")
        return value.strip() or None

    async def set(self, session_string: str) -> None:
        value = session_string.strip()
        if not value:
            raise ValueError("Cannot persist an empty Telegram session.")
        await self._mongo(lambda c: c.update_one({"_id": "primary"}, {"$set": {"session_string": value, "updated_at": datetime.now(timezone.utc)}}, upsert=True))

    async def clear(self) -> None:
        await self._mongo(lambda c: c.delete_one({"_id": "primary"}))

    async def get_login(self, user_id: int) -> Optional[Dict[str, Any]]:
        key = f"login:{int(user_id)}"
        document = await self._mongo(lambda c: c.find_one({"_id": key}))
        if not document:
            return None
        return {
            "phone": str(document.get("phone", "")).strip(),
            "phone_code_hash": str(document.get("phone_code_hash", "")).strip(),
            "session_string": str(document.get("session_string", "")).strip(),
        }

    async def set_login(self, user_id: int, phone: str, phone_code_hash: str, session_string: str) -> None:
        key = f"login:{int(user_id)}"
        payload = {"phone": phone.strip(), "phone_code_hash": phone_code_hash.strip(), "session_string": session_string.strip(), "updated_at": datetime.now(timezone.utc)}
        await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))

    async def clear_login(self, user_id: int) -> None:
        await self._mongo(lambda c: c.delete_one({"_id": f"login:{int(user_id)}"}))

    async def get_destination(self, user_id: int) -> Optional[str]:
        document = await self._mongo(lambda c: c.find_one({"_id": f"destination:{int(user_id)}"}))
        value = (document or {}).get("destination", "")
        return str(value).strip() or None

    async def set_destination(self, user_id: int, destination: str) -> None:
        value = destination.strip()
        if not value:
            raise ValueError("Cannot persist an empty destination.")
        await self._mongo(lambda c: c.update_one({"_id": f"destination:{int(user_id)}"}, {"$set": {"destination": value, "updated_at": datetime.now(timezone.utc)}}, upsert=True))

    async def clear_destination(self, user_id: int) -> None:
        await self._mongo(lambda c: c.delete_one({"_id": f"destination:{int(user_id)}"}))

    async def get_access(self, user_id: int) -> Optional[dict[str, Any]]:
        document = await self._mongo(lambda c: c.find_one({"_id": f"access:{int(user_id)}"}))
        if not document:
            return None
        return {
            "user_id": int(user_id),
            "active": bool(document.get("active", True)),
            "expires_at": document.get("expires_at"),
            "role": str(document.get("role", "user")),
        }

    async def set_access(self, user_id: int, active: bool = True, expires_at: Optional[datetime] = None, role: str = "user") -> None:
        await self._mongo(lambda c: c.update_one(
            {"_id": f"access:{int(user_id)}"},
            {"$set": {"user_id": int(user_id), "active": bool(active), "expires_at": expires_at, "role": role, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        ))

    async def list_access(self) -> list[dict[str, Any]]:
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

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()


session_store = SessionStore()
