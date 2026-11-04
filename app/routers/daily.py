"""Daily release endpoints: sync (release today's set if the rules allow), status and pause."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.deps import WRITE_ROLES, CurrentActor, DbSession, ParentActor, load_child
from app.errors import ApiError
from app.models import Activity, Family
from app.routers.plans import outs_for
from app.schemas import DailyStatusOut, DailySyncOut, PauseIn
from app.services import daily
from app.services.events import audit

router = APIRouter(prefix="/v1", tags=["daily"])


def _mode(db, child) -> str:
    return db.scalar(select(Family.plan_mode).where(Family.id == child.family_id)) or "parent"


@router.post("/children/{child_id}/daily/sync", response_model=DailySyncOut)
def sync(child_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> DailySyncOut:
    """Releases today's set of activities when the rules allow it (see app/services/daily.py). Idempotent: asking again
    on the same day returns the same set. Child or parent token. Families in parent mode get reason `not_system_mode`."""
    child = load_child(db, actor, child_id)
    result = daily.sync(
        db, child, actor_user_id=actor.user_id, actor_child_id=actor.child_id, request_id=actor.request_id
    )
    rows = (
        db.execute(
            select(daily.PlanItem, Activity)
            .join(Activity, Activity.id == daily.PlanItem.activity_id)
            .where(daily.PlanItem.id.in_([i.id for i in result.items]))
            .order_by(daily.PlanItem.position)
        ).all()
        if result.items
        else []
    )
    return DailySyncOut(
        child_id=child.id,
        plan_mode=_mode(db, child),
        paused_until=child.paused_until,
        progress=daily.progress(db, child, result.policy),
        released=outs_for(db, rows, daily.local_today(db, child.family_id)),
        reason=result.reason,
    )


@router.get("/children/{child_id}/daily/status", response_model=DailyStatusOut)
def status(child_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> DailyStatusOut:
    """Progress through the latest set and what the next release needs (read only; never releases anything)."""
    child = load_child(db, actor, child_id)
    policy = daily.effective_policy(db, child.board_code, child.grade)
    return DailyStatusOut(
        child_id=child.id,
        plan_mode=_mode(db, child),
        paused_until=child.paused_until,
        progress=daily.progress(db, child, policy),
    )


@router.put("/children/{child_id}/daily/pause", response_model=DailyStatusOut)
def pause(child_id: uuid.UUID, body: PauseIn, actor: ParentActor, db: DbSession) -> DailyStatusOut:
    """Parent pauses the daily release up to and including a date (holiday, illness); null resumes it."""
    child = load_child(db, actor, child_id, WRITE_ROLES)
    today = daily.local_today(db, child.family_id)
    if body.paused_until is not None and body.paused_until < today:
        raise ApiError(422, "validation_error", "The pause must end today or later.")
    child.paused_until = body.paused_until
    audit(
        db,
        "child.daily_paused" if body.paused_until else "child.daily_resumed",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        until=str(body.paused_until) if body.paused_until else None,
        request_id=actor.request_id,
    )
    policy = daily.effective_policy(db, child.board_code, child.grade)
    return DailyStatusOut(
        child_id=child.id,
        plan_mode=_mode(db, child),
        paused_until=child.paused_until,
        progress=daily.progress(db, child, policy),
    )
