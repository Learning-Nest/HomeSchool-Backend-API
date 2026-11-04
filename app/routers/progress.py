"""Progress: mastery, observations, baseline, overview, dashboard, recommendations and history."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import ALL_ROLES, WRITE_ROLES, CurrentActor, DbSession, ParentActor, load_child
from app.errors import ApiError
from app.models import Activity, ActivitySession, Skill, SkillEvidence
from app.routers.plans import today_items
from app.schemas import (
    BaselineIn,
    BaselineOut,
    DashboardOut,
    HistoryItem,
    MasteryOut,
    ObservationIn,
    ObservationOut,
    OverviewOut,
    RecommendationOut,
)
from app.security import now
from app.services import mastery as m
from app.services import progress as pg
from app.services.events import audit

router = APIRouter(prefix="/v1/children/{child_id}", tags=["progress"])


def _clamp_time(value):
    stamp = now()
    if value is None:
        return stamp
    if value.tzinfo is None:
        raise ApiError(422, "validation_error", "occurred_at must include a timezone.")
    if value < stamp - timedelta(days=7) or value > stamp + timedelta(minutes=5):
        raise ApiError(422, "validation_error", "occurred_at must be within the last 7 days.")
    return min(value, stamp)


@router.post("/observations", response_model=ObservationOut, status_code=201)
def observation(child_id: uuid.UUID, body: ObservationIn, actor: ParentActor, db: DbSession) -> ObservationOut:
    """A parent's or educator's rating of one skill (trying 0.3 / with help 0.6 / on their own 1.0)."""
    child = load_child(db, actor, child_id, ALL_ROLES)
    skill = db.get(Skill, body.skill_code)
    if skill is None:
        raise ApiError(422, "validation_error", "Unknown skill.")
    pg.add_evidence(
        db,
        family_id=child.family_id,
        child_id=child.id,
        skill_code=skill.code,
        source="observation",
        value=m.RATING_VALUES[body.rating],
        independence="independent",
        occurred_at=_clamp_time(body.occurred_at),
        note=body.note,
    )
    pg.recompute_mastery(db, child.id, child.family_id, {skill.code})
    audit(
        db,
        "progress.observation",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="skill",
        entity_id=skill.code,
        request_id=actor.request_id,
    )
    row = next(r for r in pg.child_mastery(db, child.id) if r.skill_code == skill.code)
    return ObservationOut(mastery=row)


@router.post("/baseline", response_model=BaselineOut, status_code=201)
def baseline(child_id: uuid.UUID, body: BaselineIn, actor: ParentActor, db: DbSession) -> BaselineOut:
    """Starting-point checklist. Skills that already have any evidence are skipped so history is never overwritten."""
    child = load_child(db, actor, child_id, WRITE_ROLES)
    known = set(db.scalars(select(Skill.code).where(Skill.code.in_([i.skill_code for i in body.ratings]))))
    unknown = sorted({i.skill_code for i in body.ratings} - known)
    if unknown:
        raise ApiError(422, "validation_error", f"Unknown skills: {', '.join(unknown)}.")
    have = set(
        db.scalars(
            select(SkillEvidence.skill_code).where(
                SkillEvidence.child_id == child.id, SkillEvidence.skill_code.in_(known)
            )
        )
    )
    recorded, skipped = [], []
    stamp = now()
    for item in body.ratings:
        if item.skill_code in have or item.skill_code in recorded:
            skipped.append(item.skill_code)
            continue
        pg.add_evidence(
            db,
            family_id=child.family_id,
            child_id=child.id,
            skill_code=item.skill_code,
            source="baseline",
            value=m.RATING_VALUES[item.rating],
            independence="independent",
            occurred_at=stamp,
        )
        recorded.append(item.skill_code)
    pg.recompute_mastery(db, child.id, child.family_id, set(recorded))
    audit(
        db,
        "progress.baseline",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        recorded=len(recorded),
        request_id=actor.request_id,
    )
    return BaselineOut(recorded=recorded, skipped=skipped)


@router.get("/mastery", response_model=list[MasteryOut])
def mastery(child_id: uuid.UUID, actor: ParentActor, db: DbSession, subject: str | None = None) -> list[MasteryOut]:
    child = load_child(db, actor, child_id, ALL_ROLES)
    return pg.child_mastery(db, child.id, subject)


@router.get("/progress/overview", response_model=OverviewOut)
def overview(
    child_id: uuid.UUID,
    actor: ParentActor,
    db: DbSession,
    range_start: date | None = None,
    range_end: date | None = None,
) -> OverviewOut:
    """Defaults to the current week (Monday to Sunday, family timezone)."""
    child = load_child(db, actor, child_id, ALL_ROLES)
    today = now().astimezone(pg.family_tz(db, child.family_id)).date()
    start = range_start or pg.week_start_of(today)
    end = range_end or start + timedelta(days=6)
    if end < start or (end - start).days > 366:
        raise ApiError(422, "validation_error", "Invalid date range.")
    return pg.overview(db, child, start, end)


@router.get("/recommendations", response_model=list[RecommendationOut])
def recommendations(
    child_id: uuid.UUID,
    actor: ParentActor,
    db: DbSession,
    limit: int = Query(default=5, ge=1, le=20),
    max_minutes: int | None = Query(default=None, ge=5, le=240),
) -> list[RecommendationOut]:
    child = load_child(db, actor, child_id, ALL_ROLES)
    return pg.recommendations(db, child, limit, max_minutes)


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(child_id: uuid.UUID, actor: ParentActor, db: DbSession) -> DashboardOut:
    child = load_child(db, actor, child_id, ALL_ROLES)
    tz = pg.family_tz(db, child.family_id)
    today = now().astimezone(tz).date()
    ws = pg.week_start_of(today)
    return DashboardOut(
        overview=pg.overview(db, child, ws, ws + timedelta(days=6)),
        today=today_items(db, child.id, today),
        recommendations=pg.recommendations(db, child, 3),
    )


@router.get("/history", response_model=list[HistoryItem])
def history(
    child_id: uuid.UUID,
    actor: CurrentActor,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[HistoryItem]:
    child = load_child(db, actor, child_id)
    rows = db.execute(
        select(ActivitySession, Activity)
        .join(Activity, Activity.id == ActivitySession.activity_id)
        .where(ActivitySession.child_id == child.id, ActivitySession.status == "submitted")
        .order_by(ActivitySession.submitted_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        HistoryItem(
            session_id=s.id,
            activity_title=a.title,
            subject_code=a.subject_code,
            submitted_at=s.submitted_at,
            duration_sec=s.duration_sec,
            score=(s.result or {}).get("score"),
            parent_assist=s.parent_assist,
            needs_review=(s.result or {}).get("needs_review", []),
        )
        for s, a in rows
    ]
