"""Upload claim bytes to Pinata and read them back through an IPFS gateway."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests


PINATA_UPLOAD_URL = "https://uploads.pinata.cloud/v3/files"
DEFAULT_IPFS_GATEWAY = "https://gateway.pinata.cloud/ipfs"


class IPFSError(RuntimeError):
    """Raised when IPFS configuration or an IPFS request fails."""


class IPFSClient:
    def __init__(
        self,
        *,
        pinata_jwt: str | None = None,
        gateway: str = DEFAULT_IPFS_GATEWAY,
        session: Any | None = None,
    ) -> None:
        self.pinata_jwt = pinata_jwt
        self.gateway = gateway.rstrip("/")
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls, *, require_upload: bool = False) -> "IPFSClient":
        pinata_jwt = os.environ.get("PINATA_JWT")
        if require_upload and not pinata_jwt:
            raise IPFSError(
                "PINATA_JWT is required to upload the synthetic claim to IPFS"
            )
        return cls(
            pinata_jwt=pinata_jwt,
            gateway=os.environ.get("IPFS_GATEWAY", DEFAULT_IPFS_GATEWAY),
        )

    def upload_bytes(
        self,
        payload: bytes,
        *,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload public bytes through Pinata and return their IPFS CID."""
        if not self.pinata_jwt:
            raise IPFSError("PINATA_JWT is required for IPFS uploads")
        if not payload:
            raise IPFSError("Refusing to upload an empty IPFS payload")

        # Keep only the file name. A caller cannot use ../ to create a path.
        safe_filename = Path(filename).name
        try:
            response = self.session.post(
                PINATA_UPLOAD_URL,
                headers={"Authorization": f"Bearer {self.pinata_jwt}"},
                data={"network": "public", "name": safe_filename},
                files={"file": (safe_filename, payload, content_type)},
                timeout=60,
            )
            response.raise_for_status()
            cid = response.json()["data"]["cid"]
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            raise IPFSError(f"Pinata upload failed: {exc}") from exc

        if not isinstance(cid, str) or not cid:
            raise IPFSError("Pinata upload response did not contain a CID")
        return cid

    @staticmethod
    def target_from_pointer(pointer: str) -> str:
        """Take the CID and optional subpath from a safe ipfs:// pointer."""
        parsed = urlsplit(pointer)
        if parsed.scheme != "ipfs":
            raise IPFSError(f"Unsupported data pointer: {pointer!r}")

        target = f"{parsed.netloc}{parsed.path}".lstrip("/")
        if not target or target.startswith(".") or "/../" in f"/{target}/":
            raise IPFSError(f"Invalid IPFS data pointer: {pointer!r}")
        return target

    def gateway_url(self, pointer: str) -> str:
        target = self.target_from_pointer(pointer)
        return f"{self.gateway}/{quote(target, safe='/')}"

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        """Download an IPFS file through the configured web gateway."""
        if attempts < 1:
            raise ValueError("attempts must be at least 1")

        url = self.gateway_url(pointer)
        last_error: Exception | None = None
        # A new CID can take a moment to appear at the gateway. Retry with a
        # short increasing delay instead of failing immediately.
        for attempt in range(attempts):
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.content
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)

        raise IPFSError(f"IPFS download failed for {pointer}: {last_error}")
