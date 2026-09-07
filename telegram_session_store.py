"""Persistent Telegram user-session and login-state storage in MongoDB."""
from __future__ import annotations
import os
from typing import Any, Dict, Optional
import motor.motor_asyncio

URI = (os.getenv("TELEGRAM_SESSION_DB_URI", "").strip() or os.getenv("TECH_VJ_DATABASE_URL", "").strip())
DB_NAME = os.getenv("TELEGRAM_SESSION_DB_NAME", "hj_groups_url_uploader").strip()
COLLECTION_NAME = os.getenv("TELEGRAM_SESSION_COLLECTION", "telegram_sessions").strip()

class SessionStore:
    def __init__(self) -> None:
        self._client = motor.motor_asyncio.AsyncIOMotorClient(URI) if URI else None
        self._collection = self._client[DB_NAME][COLLECTION_NAME] if self._client else None

    @property
    def configured(self) -> bool:
        return self._collection is not None

    async def get(self) -> Optional[str]:
        if self._collection is None:
            return None
        document = await self._collection.find_one({"_id": "primary"})
        value = (document or {}).get("session_string", "")
        return value.strip() or None

    async def set(self, session_string: str) -> None:
        if self._collection is None:
            raise RuntimeError("Set TELEGRAM_SESSION_DB_URI (or TECH_VJ_DATABASE_URL) before /login.")
        await self._collection.update_one(
            {"_id": "primary"},
            {"$set": {"session_string": session_string.strip()}},
            upsert=True,
        )

    async def clear(self) -> None:
        if self._collection is None:
            return
        await self._collection.delete_one({"_id": "primary"})

    async def get_login(self, user_id: int) -> Optional[Dict[str, Any]]:
        if self._collection is None:
            return None
        document = await self._collection.find_one({"_id": f"login:{int(user_id)}"})
        if not document:
            return None
        return {
            "phone": str(document.get("phone", "")).strip(),
            "phone_code_hash": str(document.get("phone_code_hash", "")).strip(),
            "session_string": str(document.get("session_string", "")).strip(),
        }

    async def set_login(self, user_id: int, phone: str, phone_code_hash: str, session_string: str) -> None:
        if self._collection is None:
            raise RuntimeError("Set TELEGRAM_SESSION_DB_URI (or TECH_VJ_DATABASE_URL) before /login.")
        await self._collection.update_one(
            {"_id": f"login:{int(user_id)}"},
            {"$set": {
                "phone": phone.strip(),
                "phone_code_hash": phone_code_hash.strip(),
                "session_string": session_string.strip(),
            }},
            upsert=True,
        )

    async def clear_login(self, user_id: int) -> None:
        if self._collection is None:
            return
        await self._collection.delete_one({"_id": f"login:{int(user_id)}"})

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()

session_store = SessionStore()
