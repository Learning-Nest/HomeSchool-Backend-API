"""AI pictures: an educator describes a picture, the CustomAI service draws it, and the editor shows the result.

Nothing is stored by this call except a record of the request. The picture only becomes part of the activity when the
editor uploads it (POST /v1/admin/assets?generation_id=...), which re-encodes it, applies the usual image limits and
marks the asset as made with AI.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from fastapi import APIRouter
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import DbSession, StaffActor
from app.errors import ApiError
from app.models import AiGeneration
from app.routers.admin import _require_editable, get_activity_for
from app.schemas import AiGeneratedImage, AiImageGenerateIn, AiImageGenerateOut
from app.services import ai_images
from app.services.assignments import IST, today
from app.services.events import audit, record_activity_event

router = APIRouter(tags=["ai-images"])


@router.post("/v1/admin/activities/{activity_id}/images/generate", response_model=AiImageGenerateOut)
async def generate_images(
    activity_id: uuid.UUID, body: AiImageGenerateIn, actor: StaffActor, db: DbSession
) -> AiImageGenerateOut:
    """Makes one or two pictures from a description. Anyone who may edit the activity may ask."""
    s = get_settings()
    if not s.ai_image_enabled:
        raise ApiError(503, "ai_unavailable", "Picture generation is not switched on yet.")
    a = get_activity_for(db, actor, activity_id)
    _require_editable(actor, a)

    limit = s.ai_image_daily_limit_per_user  # 0 means no limit
    used = 0
    if limit:
        since = datetime.combine(today(), time.min, tzinfo=IST)
        used = db.scalar(
            select(func.coalesce(func.sum(AiGeneration.images), 0)).where(
                AiGeneration.user_id == actor.user_id, AiGeneration.created_at >= since
            )
        )
        if used + body.n > limit:
            raise ApiError(
                429, "ai_limit_reached", f"You can make {limit} generated pictures a day. Try again tomorrow."
            )

    pictures, model = await ai_images.generate_pictures(
        description=body.description,
        style=body.style,
        size=body.size,
        quality=body.quality,
        n=body.n,
        avoid_text=body.avoid_text,
        request_id=actor.request_id,
    )
    gen = AiGeneration(
        activity_id=a.id,
        user_id=actor.user_id,
        description=body.description,
        style=body.style,
        size=body.size,
        quality=body.quality,
        images=len(pictures),
        avoid_text=body.avoid_text,
        model=model,
    )
    db.add(gen)
    db.flush()
    record_activity_event(
        db, a, actor.user_id, "image_generated", generation=str(gen.id), images=len(pictures), style=body.style
    )
    audit(
        db,
        "ai.image_generated",
        actor_user_id=actor.user_id,
        entity="activity",
        entity_id=a.id,
        generation_id=str(gen.id),
        images=len(pictures),
        request_id=actor.request_id,
    )
    return AiImageGenerateOut(
        generation_id=gen.id,
        images=[AiGeneratedImage(**p) for p in pictures],
        remaining_today=max(0, limit - used - len(pictures)) if limit else None,
    )
