"""Reliable Playbook asset uploader for Telegram media staging."""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any, Callable

import httpx

BASE_URL = os.getenv("PLAYBOOK_API_BASE_URL", "https://api.playbook.com/v1").rstrip("/")
TOKEN = os.getenv("PLAYBOOK_API_TOKEN", "").strip()
ORG_SLUG = os.getenv("PLAYBOOK_ORG_SLUG", "").strip()
TIMEOUT = float(os.getenv("PLAYBOOK_HTTP_TIMEOUT", "120"))


class PlaybookError(RuntimeError):
    pass


class ProgressFileStream(httpx.AsyncByteStream):
    def __init__(self, path: Path, total: int, callback: Callable[[int, int], None] | None = None, chunk_size: int = 1024 * 1024) -> None:
        self.path = path
        self.total = total
        self.callback = callback
        self.chunk_size = chunk_size

    async def __aiter__(self):
        sent = 0
        with self.path.open("rb") as stream:
            while True:
                chunk = stream.read(self.chunk_size)
                if not chunk:
                    break
                yield chunk
                sent += len(chunk)
                if self.callback:
                    self.callback(sent, self.total)


class PlaybookClient:
    def __init__(self, token: str = TOKEN, org_slug: str = ORG_SLUG) -> None:
        if not token or not org_slug:
            raise PlaybookError("PLAYBOOK_API_TOKEN and PLAYBOOK_ORG_SLUG are required")
        self.token = token
        self.org_slug = org_slug
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{BASE_URL}/{self.org_slug}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                response = await client.request(method, url, headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            raise PlaybookError(f"Playbook network error: {exc}") from exc
        if response.status_code >= 400:
            raise PlaybookError(f"Playbook API {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PlaybookError("Playbook returned a non-JSON response") from exc

    async def upload_file(self, file_path: str | Path, title: str | None = None, progress_callback=None) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise PlaybookError(f"File does not exist: {path}")

        size = path.stat().st_size
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        file_title = title or path.name
        payload = {"asset": {"title": file_title, "media_type": media_type, "size": size}}
        prepared = await self._request("POST", "assets/upload_prepare", json=payload)
        data = prepared.get("data") or {}

        signed_id = data.get("signed_gcs_id")
        provider = str(data.get("storage_provider") or "").lower()
        if not signed_id:
            raise PlaybookError("Playbook did not return signed_gcs_id")

        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            if provider == "gcs":
                upload_url = data.get("upload_url")
                if not upload_url:
                    raise PlaybookError("Playbook GCS response did not include upload_url")

                init_headers = {"Content-Type": media_type, "x-goog-resumable": "start"}
                encrypted = data.get("encrypted_organization_metadata")
                extension = data.get("file_extension")
                if encrypted:
                    init_headers["x-goog-meta-encrypted-organization-metadata"] = str(encrypted)
                if extension:
                    init_headers["x-goog-meta-extension"] = str(extension)

                try:
                    init = await client.post(upload_url, headers=init_headers)
                    init.raise_for_status()
                except httpx.HTTPError as exc:
                    raise PlaybookError(f"Playbook GCS session initialization failed: {exc}") from exc

                session_url = init.headers.get("Location")
                if not session_url:
                    raise PlaybookError("Playbook GCS upload did not return a resumable session URL")

                # Playbook's GCS signed-upload flow requires this exact resumable
                # byte upload shape; the byte PUT is not a normal media MIME PUT.
                upload_headers = {
                    "Content-Type": "text/plain",
                    "Content-Range": f"bytes 0-{max(size - 1, 0)}/{size}",
                }
                try:
                    upload = await client.put(
                        session_url,
                        headers=upload_headers,
                        content=ProgressFileStream(path, size, progress_callback),
                    )
                    if upload.status_code not in {200, 201}:
                        upload.raise_for_status()
                except httpx.HTTPError as exc:
                    raise PlaybookError(f"Playbook GCS file upload failed: {exc}") from exc

            elif provider == "backblaze" and data.get("multipart_upload_id") and data.get("parts"):
                multipart_id = str(data["multipart_upload_id"])
                part_size = int(data.get("part_size") or 0)
                if part_size <= 0:
                    raise PlaybookError("Playbook Backblaze multipart response has invalid part_size")

                uploaded = 0
                with path.open("rb") as stream:
                    for part in data["parts"]:
                        part_number = int(part["part_number"])
                        part_url = part.get("url")
                        if not part_url:
                            raise PlaybookError(f"Playbook missing URL for multipart part {part_number}")
                        stream.seek((part_number - 1) * part_size)
                        chunk = stream.read(part_size)
                        try:
                            response = await client.put(part_url, content=chunk)
                            response.raise_for_status()
                        except httpx.HTTPError as exc:
                            raise PlaybookError(f"Playbook Backblaze part {part_number} upload failed: {exc}") from exc
                        uploaded += len(chunk)
                        if progress_callback:
                            progress_callback(min(uploaded, size), size)

                completed_payload = {
                    "asset": {
                        "signed_gcs_id": str(signed_id),
                        "multipart_upload_id": multipart_id,
                        "title": file_title,
                    }
                }
                completed = await self._request("POST", "assets/upload_complete", json=completed_payload)
                asset = completed.get("data") or completed
                token = asset.get("token")
                if not token:
                    raise PlaybookError("Playbook completed multipart upload without an asset token")
                if progress_callback:
                    progress_callback(size, size)
                return str(token)

            else:
                upload_url = data.get("upload_url")
                if not upload_url:
                    raise PlaybookError("Playbook upload response did not include upload_url")

                # Backblaze single-part upload requires the signed metadata headers
                # returned by upload_prepare, verbatim.
                headers = {"Content-Type": media_type}
                extension = data.get("file_extension")
                encrypted = data.get("encrypted_organization_metadata")
                if extension:
                    headers["x-amz-meta-extension"] = str(extension)
                if encrypted:
                    headers["x-amz-meta-encrypted-organization-metadata"] = str(encrypted)
                try:
                    upload = await client.put(
                        upload_url,
                        headers=headers,
                        content=ProgressFileStream(path, size, progress_callback),
                    )
                    upload.raise_for_status()
                except httpx.HTTPError as exc:
                    raise PlaybookError(f"Playbook Backblaze upload failed: {exc}") from exc

        completed = await self._request(
            "POST",
            "assets/upload_complete",
            json={
                "asset": {
                    "signed_gcs_id": str(signed_id),
                    "title": file_title,
                    "media_type": media_type,
                    "size": size,
                }
            },
        )
        asset = completed.get("data") or completed
        token = asset.get("token")
        if not token:
            raise PlaybookError("Playbook upload completed without an asset token")
        if progress_callback:
            progress_callback(size, size)
        return str(token)

    async def get_asset(self, asset_token: str) -> dict[str, Any]:
        token = str(asset_token).strip()
        if not token:
            raise PlaybookError("Asset token is required")
        response = await self._request("GET", f"assets/{token}")
        return response.get("data") or response

    async def delete_asset(self, asset_token: str) -> None:
        token = str(asset_token).strip()
        if not token:
            return
        await self._request("DELETE", f"assets/{token}")


__all__ = ["PlaybookClient", "PlaybookError"]
