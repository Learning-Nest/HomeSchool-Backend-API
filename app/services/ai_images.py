"""Asks the CustomAI service to draw a picture.

CustomAI is a private service: only this API can reach it, with a shared key kept in Key Vault. Nothing the service says
is passed on as written except the reason a description was refused; failures are logged here and the caller gets a
plain "not available" answer.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re

import httpx

from app.config import get_settings
from app.errors import ApiError

log = logging.getLogger("app.ai_images")

ALLOWED_MIME_TYPES = {"image/png", "image/webp", "image/jpeg"}
MAX_PICTURE_B64_CHARS = 14_000_000  # about 10 MB of picture; a normal one is 1 to 3 MB
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_slots: asyncio.Semaphore | None = None
_slots_size = 0


def _slot_gate() -> asyncio.Semaphore:
    """At most `ai_max_concurrent_generations` pictures in flight per replica, so a busy hour cannot use every worker."""
    global _slots, _slots_size
    size = get_settings().ai_max_concurrent_generations
    if _slots is None or _slots_size != size:
        _slots, _slots_size = asyncio.Semaphore(size), size
    return _slots


def make_client() -> httpx.AsyncClient:
    """The HTTP client for CustomAI. A separate function so tests can swap in a fake service."""
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.ai_service_url.rstrip("/"),
        headers={"X-API-Key": s.ai_service_api_key.get_secret_value()},
        timeout=httpx.Timeout(s.ai_request_timeout_seconds, connect=10.0),
    )


def _unavailable() -> ApiError:
    return ApiError(503, "ai_unavailable")


async def generate_pictures(
    *, description: str, style: str, size: str, quality: str, n: int, avoid_text: bool, request_id: str | None
) -> tuple[list[dict[str, str]], str | None]:
    """Returns ([{mime_type, b64_json}, ...], model name). Raises ApiError ai_image_rejected (the description was
    refused; the caller can change it) or ai_unavailable (anything else)."""
    payload = {
        "description": description,
        "style": style,
        "size": size,
        "quality": quality,
        "n": n,
        "output_format": "png",
        "avoid_text": avoid_text,
    }
    headers = {"X-Request-ID": request_id} if request_id and _REQUEST_ID.match(request_id) else {}
    try:
        async with _slot_gate():
            async with make_client() as client:
                r = await client.post("/v1/images/generate", json=payload, headers=headers)
    except httpx.HTTPError as e:  # timeouts, refused connections, a service that is still starting
        log.warning("CustomAI could not be reached (%s): %s", type(e).__name__, e)
        raise _unavailable() from e

    if r.status_code == 200:
        return _read_pictures(r, n)

    code, message = _read_error(r)
    if r.status_code == 422 and code == "image_rejected":
        raise ApiError(422, "ai_image_rejected", message or None)
    # 401 means the shared key is wrong, 429/502/503 mean CustomAI or OpenAI is struggling, 422 anything else means
    # the two services disagree about the request. None of that is the educator's doing.
    log.error("CustomAI answered %s (%s): %s", r.status_code, code, message)
    raise _unavailable()


def _read_error(r: httpx.Response) -> tuple[str | None, str | None]:
    try:
        err = r.json()["error"]
        return str(err.get("code")), str(err.get("message"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None


def _read_pictures(r: httpx.Response, wanted: int) -> tuple[list[dict[str, str]], str | None]:
    try:
        body = r.json()
        pictures = body["images"]
        if not isinstance(pictures, list) or not 1 <= len(pictures) <= wanted:
            raise ValueError(f"expected 1 to {wanted} pictures")
        out = []
        for p in pictures:
            mime, b64 = p["mime_type"], p["b64_json"]
            if mime not in ALLOWED_MIME_TYPES or not isinstance(b64, str) or not 0 < len(b64) <= MAX_PICTURE_B64_CHARS:
                raise ValueError("a picture is missing, too large or of an unexpected type")
            base64.b64decode(b64, validate=True)
            out.append({"mime_type": mime, "b64_json": b64})
        model = body.get("model")
        return out, model if isinstance(model, str) else None
    except (ValueError, KeyError, TypeError, binascii.Error) as e:
        log.error("CustomAI sent an answer this API cannot use: %s", e)
        raise _unavailable() from e
