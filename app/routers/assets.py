"""Activity images: upload (staff), list, delete, and the signed link target used by the local storage backend.

Upload is the raw image as the request body (Content-Type image/jpeg|png|webp) with ?activity_id=...; the server
re-encodes it (see services/images.py) and answers with the asset id the editor puts into the activity.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import Text as SaText
from sqlalchemy import cast, func, select

from app import ratelimit
from app.config import get_settings
from app.deps import DbSession, StaffActor
from app.errors import ApiError, not_found
from app.models import ActivityVersion, Asset
from app.routers.admin import _require_editable, get_activity_for
from app.schemas import AssetOut
from app.security import now
from app.services.events import record_activity_event
from app.services.images import blob_path, collect_image_refs, process_image, sha256_hex
from app.services.storage import LocalStorage, get_storage, storage_ready

router = APIRouter(tags=["assets"])
ALLOWED_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _out(asset: Asset, request: Request) -> AssetOut:
    s = get_settings()
    out = AssetOut.model_validate(asset)
    out.url = get_storage().url(asset.blob_path, s.asset_url_ttl_seconds, str(request.base_url))
    return out


@router.post("/v1/admin/assets", response_model=AssetOut, status_code=201)
async def upload_asset(
    request: Request, actor: StaffActor, db: DbSession, activity_id: uuid.UUID = Query()
) -> AssetOut:
    s = get_settings()
    if not storage_ready(s):
        raise ApiError(503, "storage_unavailable")
    a = get_activity_for(db, actor, activity_id)
    _require_editable(actor, a)
    ratelimit.check(f"asset-upload:{actor.user_id}", 60, 3600)
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_UPLOAD_TYPES:
        raise ApiError(415, "asset_type_not_allowed")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > s.asset_max_upload_bytes:
        raise ApiError(413, "asset_too_large", f"Images may be up to {s.asset_max_upload_bytes // (1024 * 1024)} MB.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > s.asset_max_upload_bytes:
            raise ApiError(
                413, "asset_too_large", f"Images may be up to {s.asset_max_upload_bytes // (1024 * 1024)} MB."
            )
        chunks.append(chunk)
    data, width, height = process_image(b"".join(chunks))
    digest = sha256_hex(data)
    existing = db.scalar(
        select(Asset).where(Asset.activity_id == a.id, Asset.sha256 == digest, Asset.deleted_at.is_(None))
    )
    if existing is not None:
        return _out(existing, request)
    live = db.execute(
        select(func.count(), func.coalesce(func.sum(Asset.bytes), 0)).where(
            Asset.activity_id == a.id, Asset.deleted_at.is_(None)
        )
    ).one()
    if live[0] >= s.asset_max_per_activity or live[1] + len(data) > s.asset_max_bytes_per_activity:
        raise ApiError(413, "asset_too_large", "This activity has reached its image limit. Remove an image first.")
    path = blob_path(a.id, digest)
    get_storage().put(path, data, "image/webp")
    asset = Asset(
        activity_id=a.id,
        uploaded_by=actor.user_id,
        blob_path=path,
        content_type="image/webp",
        bytes=len(data),
        width=width,
        height=height,
        sha256=digest,
    )
    db.add(asset)
    db.flush()
    record_activity_event(db, a, actor.user_id, "image_added", coalesce_minutes=0, asset=str(asset.id), bytes=len(data))
    return _out(asset, request)


@router.get("/v1/admin/activities/{activity_id}/assets", response_model=list[AssetOut])
def list_assets(activity_id: uuid.UUID, request: Request, actor: StaffActor, db: DbSession) -> list[AssetOut]:
    a = get_activity_for(db, actor, activity_id)
    rows = db.scalars(
        select(Asset).where(Asset.activity_id == a.id, Asset.deleted_at.is_(None)).order_by(Asset.created_at)
    )
    return [_out(x, request) for x in rows]


@router.delete("/v1/admin/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: uuid.UUID, actor: StaffActor, db: DbSession) -> Response:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise not_found()
    a = get_activity_for(db, actor, asset.activity_id)
    _require_editable(actor, a)
    sid = str(asset.id)
    in_current = any(str(r["asset"]) == sid for r in collect_image_refs(a.definition))
    in_version = db.scalar(
        select(ActivityVersion.version)
        .where(ActivityVersion.activity_id == a.id, cast(ActivityVersion.definition, SaText).contains(sid))
        .limit(1)
    )
    if in_current or in_version is not None:
        raise ApiError(
            409, "asset_in_use", "Remove the image from the activity first (published versions keep theirs)."
        )
    asset.deleted_at = now()
    record_activity_event(db, a, actor.user_id, "image_removed", asset=sid)
    return Response(status_code=204)


@router.get("/v1/assets/local/{blob:path}", include_in_schema=False)
def serve_local(blob: str, exp: int, sig: str) -> Response:
    """Target of the signed links the local storage backend hands out (development and tests only)."""
    st = get_storage()
    if not isinstance(st, LocalStorage) or not st.verify(blob, exp, sig):
        raise not_found()
    data = st.get(blob)
    if data is None:
        raise not_found()
    return Response(data, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})
