"""Activity images: validate and re-encode uploads, and attach download links to an activity sent to the app.

An activity definition refers to an image as ``{"asset": "<uuid>", "alt": "..."}`` (anywhere a step, option or item may
carry one). Before the definition is sent to a parent or child, ``attach_images`` swaps each reference for its
download details and adds a top-level ``images`` manifest the app prefetches when the activity loads.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError
from app.models import Asset
from app.services.content import public_definition
from app.services.storage import Storage, get_storage

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_SOURCE_PIXELS = 25_000_000
OUTPUT_TYPE = "image/webp"


def process_image(data: bytes) -> tuple[bytes, int, int]:
    """Decode an upload, drop metadata, scale down and re-encode as WebP. Returns (bytes, width, height)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    s = get_settings()
    if len(data) > s.asset_max_upload_bytes:
        raise ApiError(413, "asset_too_large", f"Images may be up to {s.asset_max_upload_bytes // (1024 * 1024)} MB.")
    if not data:
        raise ApiError(422, "validation_error", "The image is empty.")
    try:
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        if fmt not in ALLOWED_FORMATS:
            raise ApiError(415, "asset_type_not_allowed")
        if im.width * im.height > MAX_SOURCE_PIXELS:
            raise ApiError(413, "asset_too_large", "The image has too many pixels.")
        im.seek(0)  # first frame of an animated image
        im = ImageOps.exif_transpose(im)  # honour phone rotation, then metadata is dropped on save
        im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
    except ApiError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ApiError(415, "asset_type_not_allowed", "That file is not a readable JPEG, PNG or WebP image.") from None
    im.thumbnail((s.asset_max_side_px, s.asset_max_side_px))
    out = io.BytesIO()
    im.save(out, format="WEBP", quality=82, method=4)
    return out.getvalue(), im.width, im.height


def blob_path(activity_id: uuid.UUID, sha256: str) -> str:
    return f"{activity_id}/{sha256}.webp"


def collect_image_refs(node: Any) -> list[dict[str, Any]]:
    """Every ``image`` object ({asset, alt}) anywhere inside a definition, in document order."""
    found: list[dict[str, Any]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            img = x.get("image")
            if isinstance(img, dict) and "asset" in img:
                found.append(img)
            for k, v in x.items():
                if k != "image":
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(node)
    return found


def asset_ids_in(definition: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(r["asset"]) for r in collect_image_refs(definition)))


def asset_problems(db: Session, definition: dict[str, Any], activity_id: uuid.UUID | None) -> list[str]:
    """Problems with the images a definition refers to: unknown, deleted, or uploaded for a different activity."""
    ids = asset_ids_in(definition)
    if not ids:
        return []
    if activity_id is None:
        return [f"image {i}: images can only be added to an activity that already exists" for i in ids]
    rows = {}
    try:
        uuids = [uuid.UUID(i) for i in ids]
    except ValueError:
        return ["image: asset must be a uuid"]
    for a in db.scalars(select(Asset).where(Asset.id.in_(uuids))):
        rows[str(a.id)] = a
    out = []
    for i in ids:
        a = rows.get(i)
        if a is None or a.deleted_at is not None:
            out.append(f"image {i}: not found (upload it again)")
        elif a.activity_id != activity_id:
            out.append(f"image {i}: belongs to a different activity")
    return out


def attach_images(db: Session, storage: Storage, wire: dict[str, Any], base_url: str = "") -> dict[str, Any]:
    """Resolve image references in a client-facing definition. Adds ``images`` (the manifest) when there are any."""
    refs = collect_image_refs(wire)
    if not refs:
        return wire
    s = get_settings()
    ids = list(dict.fromkeys(uuid.UUID(str(r["asset"])) for r in refs))
    assets = {str(a.id): a for a in db.scalars(select(Asset).where(Asset.id.in_(ids), Asset.deleted_at.is_(None)))}
    manifest = []
    seen = set()
    for r in refs:
        a = assets.get(str(r["asset"]))
        if a is None:
            r["missing"] = True  # the app falls back to the text label
            continue
        r["sha256"], r["width"], r["height"] = a.sha256, a.width, a.height
        if a.id not in seen:
            seen.add(a.id)
            manifest.append(
                {
                    "id": str(a.id),
                    "url": storage.url(a.blob_path, s.asset_url_ttl_seconds, base_url),
                    "sha256": a.sha256,
                    "bytes": a.bytes,
                    "width": a.width,
                    "height": a.height,
                    "content_type": a.content_type,
                }
            )
    wire["images"] = manifest
    return wire


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wire_definition(db: Session, definition: dict[str, Any]) -> dict[str, Any]:
    """What a parent or child app receives for an activity: answer keys removed, image links resolved."""
    return attach_images(db, get_storage(), public_definition(definition), get_settings().public_base_url)
