"""Minimal Playbook API client for temporary Telegram media staging.

The token is read from PLAYBOOK_API_TOKEN and is never persisted by this module.
The client uses Playbook's signed upload flow and deletes the asset only when the
caller confirms the downstream Telegram send succeeded.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx


BASE_URL = os.getenv("PLAYBOOK_API_BASE_URL", "https://api.playbook.com/v1").rstrip("/")
TOKEN = os.getenv("PLAYBOOK_API_TOKEN", "").strip()
ORG_SLUG = os.getenv("PLAYBOOK_ORG_SLUG", "").strip()
TIMEOUT = float(os.getenv("PLAYBOOK_HTTP_TIMEOUT", "120"))


class PlaybookError(RuntimeError):
    pass


class PlaybookClient:
    def __init__(self, token: str = TOKEN, org_slug: str = ORG_SLUG) -> None:
        if not token or not org_slug:
            raise PlaybookError("PLAYBOOK_API_TOKEN and PLAYBOOK_ORG_SLUG are required")
        self.token = token
        self.org_slug = org_slug
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{BASE_URL}/{self.org_slug}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise PlaybookError(f"Playbook API {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        return response.json()

    async def upload_file(self, file_path: str | Path, title: str | None = None) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise PlaybookError(f"File does not exist: {path}")
        size = path.stat().st_size
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = {"asset": {"title": title or path.name, "media_type": media_type, "size": size}}
        prepared = await self._request("POST", "assets/upload_prepare", json=payload)
        data = prepared.get("data") or {}
        upload_url = data.get("upload_url")
        signed_id = data.get("signed_gcs_id")
        provider = data.get("storage_provider")
        if not upload_url or not signed_id:
            raise PlaybookError("Playbook did not return upload credentials")

        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            if provider == "gcs":
                init_headers = {"Content-Type": media_type, "x-goog-resumable": "start"}
                encrypted = data.get("encrypted_organization_metadata")
                extension = data.get("file_extension")
                if encrypted:
                    init_headers["x-goog-meta-encrypted-organization-metadata"] = encrypted
                if extension:
                    init_headers["x-goog-meta-extension"] = extension
                with path.open("rb") as stream:
                    init = await client.post(upload_url, headers=init_headers)
                init.raise_for_status()
                session_url = init.headers.get("Location")
                if not session_url:
                    raise PlaybookError("Playbook GCS upload did not return a session URL")
                with path.open("rb") as stream:
                    upload = await client.put(session_url, headers={"Content-Type": media_type}, content=stream)
                upload.raise_for_status()
            elif provider == "backblaze" and data.get("multipart_upload_id") and data.get("parts"):
                with path.open("rb") as stream:
                    for part in data["parts"]:
                        part_number = int(part["part_number"])
                        part_size = int(data["part_size"])
                        stream.seek((part_number - 1) * part_size)
                        chunk = stream.read(part_size)
                        response = await client.put(part["url"], content=chunk)
                        response.raise_for_status()
            else:
                headers = {"Content-Type": media_type}
                for key in ("x-amz-meta-extension", "x-amz-meta-encrypted-organization-metadata"):
                    value = data.get(key.replace("x-amz-meta-", ""))
                    if value:
                        headers[key] = value
                with path.open("rb") as stream:
                    upload = await client.put(upload_url, headers=headers, content=stream)
                upload.raise_for_status()

        completed = await self._request(
            "POST",
            "assets/upload_complete",
            json={"asset": {"signed_gcs_id": signed_id, "title": title or path.name, "media_type": media_type, "size": size}},
        )
        asset = completed.get("data") or {}
        token = asset.get("token")
        if not token:
            raise PlaybookError("Playbook upload completed without an asset token")
        return str(token)

    async def get_asset(self, asset_token: str) -> dict[str, Any]:
        response = await self._request("GET", f"assets/{asset_token}")
        return response.get("data") or response

    async def delete_asset(self, asset_token: str) -> None:
        await self._request("DELETE", f"assets/{asset_token}")


__all__ = ["PlaybookClient", "PlaybookError"]
