"""Persistent Telegram user-session and login-state storage in MongoDB with local fallback."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import motor.motor_asyncio
from pymongo.errors import PyMongoError

URI = (os.getenv("TELEGRAM_SESSION_DB_URI", "").strip() or os.getenv("TECH_VJ_DATABASE_URL", "").strip())
DB_NAME = os.getenv("TELEGRAM_SESSION_DB_NAME", "hj_groups_url_uploader").strip()
COLLECTION_NAME = os.getenv("TELEGRAM_SESSION_COLLECTION", "telegram_sessions").strip()
FALLBACK_FILE = Path(os.getenv("TELEGRAM_SESSION_FALLBACK_FILE", "/tmp/voroa_telegram_session_store.json").strip())

class SessionStore:
    def __init__(self) -> None:
        self._client = motor.motor_asyncio.AsyncIOMotorClient(
            URI,
            serverSelectionTimeoutMS=int(os.getenv("TELEGRAM_SESSION_DB_TIMEOUT_MS", "5000")),
            connectTimeoutMS=int(os.getenv("TELEGRAM_SESSION_DB_CONNECT_TIMEOUT_MS", "5000")),
        ) if URI else None
        self._collection = self._client[DB_NAME][COLLECTION_NAME] if self._client else None
        self._mongo_healthy = self._collection is not None

    @property
    def configured(self) -> bool:
        return self._collection is not None

    def _fallback_read(self) -> dict[str, Any]:
        try:
            value = json.loads(FALLBACK_FILE.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _fallback_write(self, value: dict[str, Any]) -> None:
        try:
            FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp = FALLBACK_FILE.with_suffix(FALLBACK_FILE.suffix + ".tmp")
            temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
            temp.replace(FALLBACK_FILE)
        except OSError:
            pass

    async def _mongo(self, operation):
        if self._collection is None or not self._mongo_healthy:
            return None, False
        try:
            return await operation(self._collection), True
        except (PyMongoError, OSError, TimeoutError) as exc:
            self._mongo_healthy = False
            print(f"[Voroa] MongoDB unavailable; using local session fallback: {type(exc).__name__}: {exc}", flush=True)
            return None, False

    async def get(self) -> Optional[str]:
        document, ok = await self._mongo(lambda c: c.find_one({"_id": "primary"}))
        if ok:
            value = (document or {}).get("session_string", "")
            return value.strip() or None
        value = self._fallback_read().get("primary", {}).get("session_string", "")
        return str(value).strip() or None

    async def set(self, session_string: str) -> None:
        value = session_string.strip()
        _, ok = await self._mongo(lambda c: c.update_one({"_id": "primary"}, {"$set": {"session_string": value}}, upsert=True))
        if not ok:
            data = self._fallback_read()
            data["primary"] = {"session_string": value}
            self._fallback_write(data)

    async def clear(self) -> None:
        await self._mongo(lambda c: c.delete_one({"_id": "primary"}))
        data = self._fallback_read()
        data.pop("primary", None)
        self._fallback_write(data)

    async def get_login(self, user_id: int) -> Optional[Dict[str, Any]]:
        key = f"login:{int(user_id)}"
        document, ok = await self._mongo(lambda c: c.find_one({"_id": key}))
        if not ok:
            document = self._fallback_read().get(key)
        if not document or not isinstance(document, dict):
            return None
        return {
            "phone": str(document.get("phone", "")).strip(),
            "phone_code_hash": str(document.get("phone_code_hash", "")).strip(),
            "session_string": str(document.get("session_string", "")).strip(),
        }

    async def set_login(self, user_id: int, phone: str, phone_code_hash: str, session_string: str) -> None:
        key = f"login:{int(user_id)}"
        payload = {"phone": phone.strip(), "phone_code_hash": phone_code_hash.strip(), "session_string": session_string.strip()}
        _, ok = await self._mongo(lambda c: c.update_one({"_id": key}, {"$set": payload}, upsert=True))
        if not ok:
            data = self._fallback_read()
            data[key] = payload
            self._fallback_write(data)

    async def clear_login(self, user_id: int) -> None:
        key = f"login:{int(user_id)}"
        await self._mongo(lambda c: c.delete_one({"_id": key}))
        data = self._fallback_read()
        data.pop(key, None)
        self._fallback_write(data)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()

session_store = SessionStore()
