"""Blob storage for activity images.

* ``local``: files under STORAGE_LOCAL_DIR, served through ``GET /v1/assets/local/...`` with a signed, expiring link.
  For development and tests only (a deployed environment refuses uploads on this backend, see routers/assets.py).
* ``azure``: a private blob container. The API uses its managed identity (no account key anywhere) and hands out
  read-only user-delegation SAS links that expire (ASSET_URL_TTL_SECONDS).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.config import Settings, get_settings
from app.security import now


class Storage(Protocol):
    def put(self, path: str, data: bytes, content_type: str) -> None: ...

    def get(self, path: str) -> bytes | None: ...

    def delete(self, path: str) -> None: ...

    def url(self, path: str, ttl_seconds: int, base_url: str = "") -> str: ...


def _sign(secret: str, path: str, expires: int) -> str:
    return hmac.new(secret.encode(), f"{path}:{expires}".encode(), hashlib.sha256).hexdigest()


class LocalStorage:
    def __init__(self, root: str, secret: str):
        self.root = Path(root)
        self.secret = secret

    def _file(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("bad path")
        return p

    def put(self, path: str, data: bytes, content_type: str) -> None:
        p = self._file(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, path: str) -> bytes | None:
        p = self._file(path)
        return p.read_bytes() if p.is_file() else None

    def delete(self, path: str) -> None:
        p = self._file(path)
        if p.is_file():
            p.unlink()

    def url(self, path: str, ttl_seconds: int, base_url: str = "") -> str:
        expires = int(now().timestamp()) + ttl_seconds
        return f"{base_url.rstrip('/')}/v1/assets/local/{quote(path)}?exp={expires}&sig={_sign(self.secret, path, expires)}"

    def verify(self, path: str, expires: int, sig: str) -> bool:
        return expires >= int(now().timestamp()) and hmac.compare_digest(sig, _sign(self.secret, path, expires))


class AzureBlobStorage:
    """Private container + user-delegation SAS. The Azure SDK is imported lazily so local runs do not need it."""

    def __init__(self, account_url: str, container: str, client_id: str | None = None):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.account_url = account_url.rstrip("/")
        self.container = container
        self._svc = BlobServiceClient(
            self.account_url, credential=DefaultAzureCredential(managed_identity_client_id=client_id or None)
        )
        self._lock = threading.Lock()
        self._key = None
        self._key_expiry = None

    def put(self, path: str, data: bytes, content_type: str) -> None:
        from azure.storage.blob import ContentSettings

        self._svc.get_blob_client(self.container, path).upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(
                content_type=content_type, cache_control="private, max-age=31536000, immutable"
            ),
        )

    def get(self, path: str) -> bytes | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._svc.get_blob_client(self.container, path).download_blob().readall()
        except ResourceNotFoundError:
            return None

    def delete(self, path: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._svc.get_blob_client(self.container, path).delete_blob()
        except ResourceNotFoundError:
            pass

    def _delegation_key(self):
        with self._lock:
            n = now()
            if self._key is None or self._key_expiry - n < timedelta(minutes=30):
                self._key_expiry = n + timedelta(hours=6)
                self._key = self._svc.get_user_delegation_key(n - timedelta(minutes=5), self._key_expiry)
            return self._key

    def url(self, path: str, ttl_seconds: int, base_url: str = "") -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        n = now()
        sas = generate_blob_sas(
            account_name=self._svc.account_name,
            container_name=self.container,
            blob_name=path,
            user_delegation_key=self._delegation_key(),
            permission=BlobSasPermissions(read=True),
            start=n - timedelta(minutes=5),
            expiry=n + timedelta(seconds=ttl_seconds),
        )
        return f"{self.account_url}/{self.container}/{quote(path)}?{sas}"


_cache: dict[str, Storage] = {}


def make_storage(s: Settings) -> Storage:
    if s.storage_backend == "azure":
        if not s.storage_account_url:
            raise RuntimeError("STORAGE_ACCOUNT_URL must be set when STORAGE_BACKEND=azure")
        return AzureBlobStorage(s.storage_account_url, s.storage_container, s.azure_client_id)
    return LocalStorage(s.storage_local_dir, s.jwt_secret.get_secret_value())


def get_storage() -> Storage:
    s = get_settings()
    key = f"{s.storage_backend}|{s.storage_account_url}|{s.storage_container}|{s.storage_local_dir}"
    if key not in _cache:
        _cache.clear()
        _cache[key] = make_storage(s)
    return _cache[key]


def storage_ready(s: Settings | None = None) -> bool:
    """False when uploads would be lost: a deployed environment still on the local backend."""
    s = s or get_settings()
    if s.storage_backend == "azure":
        return bool(s.storage_account_url)
    return not s.is_deployed
