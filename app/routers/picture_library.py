"""Picture library: find a picture an educator already made with the AI generator, and reuse it in an activity.

Reusing is free (no call to OpenAI). The picture is copied into the activity like any upload, so the activity's own
image limits apply and the picture is marked as made with AI.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app import ratelimit
from app.config import get_settings
from app.deps import DbSession, StaffActor
from app.errors import ApiError, not_found
from app.models import AiGeneration, Asset, LibraryPicture, User
from app.routers.admin import _require_editable, get_activity_for
from app.routers.assets import _out as asset_out
from app.schemas import AssetOut, LibraryPictureOut, LibrarySearchMode, LibrarySearchOut, LibraryUseIn
from app.services import picture_library
from app.services.events import record_activity_event
from app.services.images import blob_path
from app.services.storage import get_storage, storage_ready

router = APIRouter(tags=["picture-library"])


@router.get("/v1/admin/library/pictures", response_model=LibrarySearchOut)
def search_library(
    request: Request,
    actor: StaffActor,
    db: DbSession,
    q: str = Query(default="", max_length=200, description="Words describing the picture; empty lists the most used"),
    style: Literal["storybook", "flat", "diagram", "none"] | None = None,
    mode: LibrarySearchMode = "keyword",
    limit: int = Query(default=24, ge=1, le=48),
    offset: int = Query(default=0, ge=0),
) -> LibrarySearchOut:
    found = picture_library.search(db, mode, q, style=style, limit=limit + 1, offset=offset)
    page = found[:limit]
    names = {
        u.id: u.full_name
        for u in db.scalars(
            select(User).where(User.id.in_({f.picture.created_by for f in page if f.picture.created_by}))
        )
    }
    s = get_settings()
    storage = get_storage()
    base = str(request.base_url)
    items = [
        LibraryPictureOut(
            id=f.picture.id,
            description=f.picture.description,
            style=f.picture.style,
            width=f.picture.width,
            height=f.picture.height,
            bytes=f.picture.bytes,
            uses=f.picture.uses,
            created_at=f.picture.created_at,
            created_by_name=names.get(f.picture.created_by),
            url=storage.url(f.picture.blob_path, s.asset_url_ttl_seconds, base),
            score=f.score,
        )
        for f in page
    ]
    return LibrarySearchOut(mode=mode, items=items, has_more=len(found) > limit)


@router.post("/v1/admin/activities/{activity_id}/assets/from-library", response_model=AssetOut, status_code=201)
def use_library_picture(
    activity_id: uuid.UUID, body: LibraryUseIn, request: Request, actor: StaffActor, db: DbSession
) -> AssetOut:
    """Copies a library picture into the activity and answers with the new asset, ready to put in the definition."""
    s = get_settings()
    if not storage_ready(s):
        raise ApiError(503, "storage_unavailable")
    a = get_activity_for(db, actor, activity_id)
    _require_editable(actor, a)
    ratelimit.check(f"asset-upload:{actor.user_id}", 60, 3600)
    picture = db.get(LibraryPicture, body.picture_id)
    if picture is None:
        raise not_found()

    existing = db.scalar(
        select(Asset).where(Asset.activity_id == a.id, Asset.sha256 == picture.sha256, Asset.deleted_at.is_(None))
    )
    if existing is not None:
        return asset_out(existing, request)

    live = db.execute(
        select(func.count(), func.coalesce(func.sum(Asset.bytes), 0)).where(
            Asset.activity_id == a.id, Asset.deleted_at.is_(None)
        )
    ).one()
    if live[0] >= s.asset_max_per_activity or live[1] + picture.bytes > s.asset_max_bytes_per_activity:
        raise ApiError(413, "asset_too_large", "This activity has reached its image limit. Remove an image first.")

    data = get_storage().get(picture.blob_path)
    if data is None:
        raise ApiError(503, "storage_unavailable", "That picture could not be read. Try again in a moment.")
    path = blob_path(a.id, picture.sha256)
    get_storage().put(path, data, "image/webp")

    # The "made with AI" mark is a row in THIS activity (images = 0, so it never counts towards a daily limit), so it
    # lasts as long as the activity does, whatever happens to the activity the picture was first made in.
    mark = AiGeneration(
        activity_id=a.id,
        user_id=actor.user_id,
        description=picture.description,
        style=picture.style,
        size=picture.size,
        quality=picture.quality,
        images=0,
        avoid_text=False,
        model="library",
    )
    db.add(mark)
    db.flush()
    asset = Asset(
        activity_id=a.id,
        uploaded_by=actor.user_id,
        blob_path=path,
        content_type=picture.content_type,
        bytes=picture.bytes,
        width=picture.width,
        height=picture.height,
        sha256=picture.sha256,
        ai_generation_id=mark.id,
    )
    db.add(asset)
    picture.uses += 1
    db.flush()
    record_activity_event(
        db,
        a,
        actor.user_id,
        "image_added",
        coalesce_minutes=0,
        asset=str(asset.id),
        bytes=picture.bytes,
        ai_generated=True,
        library_picture=str(picture.id),
    )
    return asset_out(asset, request)
