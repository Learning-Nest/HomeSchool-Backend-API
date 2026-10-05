"""Read-only curriculum and activity catalogue (published content only; answer keys are never returned)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from app.deps import CurrentActor, DbSession, load_child
from app.errors import not_found
from app.models import (
    Activity,
    ActivitySession,
    ActivitySkill,
    Interest,
    Level,
    PlanItem,
    Skill,
    SkillPrerequisite,
    Subject,
)
from app.schemas import ActivityDetail, ActivitySummary, LevelOut, LibraryActivity, SkillOut, SubjectOut
from app.security import now
from app.services.content import public_definition
from app.services.progress import family_tz

router = APIRouter(prefix="/v1", tags=["curriculum"])


@router.get("/curriculum/levels", response_model=list[LevelOut])
def levels(actor: CurrentActor, db: DbSession) -> list[Level]:
    return list(db.scalars(select(Level).order_by(Level.sort_order)))


@router.get("/curriculum/subjects", response_model=list[SubjectOut])
def subjects(actor: CurrentActor, db: DbSession) -> list[Subject]:
    return list(db.scalars(select(Subject).order_by(Subject.display_order)))


@router.get("/curriculum/interests")
def interests(actor: CurrentActor, db: DbSession) -> list[dict]:
    return [{"code": i.code, "name": i.name} for i in db.scalars(select(Interest).order_by(Interest.name))]


@router.get("/curriculum/skills", response_model=list[SkillOut])
def skills(actor: CurrentActor, db: DbSession, subject: str | None = None, level: str | None = None) -> list[SkillOut]:
    q = select(Skill).order_by(Skill.subject_code, Skill.level_code, Skill.code)
    if subject:
        q = q.where(Skill.subject_code == subject)
    if level:
        q = q.where(Skill.level_code == level)
    rows = list(db.scalars(q))
    pre: dict[str, list[str]] = {}
    for p in db.scalars(select(SkillPrerequisite)):
        pre.setdefault(p.skill_code, []).append(p.prerequisite_code)
    return [
        SkillOut(
            code=s.code,
            subject_code=s.subject_code,
            level_code=s.level_code,
            name=s.name,
            prerequisites=sorted(pre.get(s.code, [])),
        )
        for s in rows
    ]


@router.get("/activities", response_model=list[ActivitySummary])
def list_activities(
    actor: CurrentActor,
    db: DbSession,
    subject: str | None = None,
    level: str | None = None,
    interest: str | None = None,
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[Activity]:
    stmt = select(Activity).where(Activity.status == "published")
    if subject:
        stmt = stmt.where(Activity.subject_code == subject)
    if level:
        lv = {c: o for c, o in db.execute(select(Level.code, Level.sort_order)).all()}
        if level not in lv:
            return []
        stmt = stmt.where(
            select(Level.sort_order).where(Level.code == Activity.level_from).scalar_subquery() <= lv[level],
            select(Level.sort_order).where(Level.code == Activity.level_to).scalar_subquery() >= lv[level],
        )
    if interest:
        stmt = stmt.where(Activity.interest_tags.any(interest))
    if q:
        stmt = stmt.where(
            func.to_tsvector("simple", Activity.title + " " + func.coalesce(Activity.summary, "")).op("@@")(
                func.plainto_tsquery("simple", q)
            )
        )
    return list(db.scalars(stmt.order_by(Activity.subject_code, Activity.title).limit(limit).offset(offset)))


@router.get("/children/{child_id}/library", response_model=list[LibraryActivity])
def child_library(
    child_id: uuid.UUID,
    actor: CurrentActor,
    db: DbSession,
    subject: str | None = None,
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[LibraryActivity]:
    """The Activity library for one child: published activities in the given subject whose level range includes the
    child's level (all levels when the child has none set), each with how many times the child has finished it, when
    last, and the next day it is planned. Activities the child has not done come first."""
    child = load_child(db, actor, child_id)
    stmt = select(Activity).where(Activity.status == "published")
    if subject:
        stmt = stmt.where(Activity.subject_code == subject)
    if child.level_code:
        lv = {c: o for c, o in db.execute(select(Level.code, Level.sort_order)).all()}
        if child.level_code not in lv:
            return []
        stmt = stmt.where(
            select(Level.sort_order).where(Level.code == Activity.level_from).scalar_subquery() <= lv[child.level_code],
            select(Level.sort_order).where(Level.code == Activity.level_to).scalar_subquery() >= lv[child.level_code],
        )
    if q:
        stmt = stmt.where(
            func.to_tsvector("simple", Activity.title + " " + func.coalesce(Activity.summary, "")).op("@@")(
                func.plainto_tsquery("simple", q)
            )
        )

    done = {
        aid: (n, last)
        for aid, n, last in db.execute(
            select(ActivitySession.activity_id, func.count(), func.max(ActivitySession.submitted_at))
            .where(ActivitySession.child_id == child.id, ActivitySession.status == "submitted")
            .group_by(ActivitySession.activity_id)
        ).all()
    }
    today = now().astimezone(family_tz(db, child.family_id)).date()
    planned = {
        aid: day
        for aid, day in db.execute(
            select(PlanItem.activity_id, func.min(PlanItem.scheduled_date))
            .where(
                PlanItem.child_id == child.id,
                PlanItem.status.in_(("planned", "in_progress")),
                PlanItem.scheduled_date >= today,
            )
            .group_by(PlanItem.activity_id)
        ).all()
    }

    never_done_first = case((Activity.id.in_(list(done)), 1), else_=0) if done else None
    order = [never_done_first] if never_done_first is not None else []
    rows = db.scalars(stmt.order_by(*order, Activity.title).limit(limit).offset(offset))
    out: list[LibraryActivity] = []
    for a in rows:
        n, last = done.get(a.id, (0, None))
        out.append(
            LibraryActivity(
                **ActivitySummary.model_validate(a).model_dump(),
                times_done=n,
                last_done_at=last,
                planned_for=planned.get(a.id),
            )
        )
    return out


@router.get("/activities/{activity_id}", response_model=ActivityDetail)
def get_activity(activity_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> ActivityDetail:
    a = db.scalar(select(Activity).where(Activity.id == activity_id, Activity.status == "published"))
    if a is None:
        raise not_found()
    codes = sorted(db.scalars(select(ActivitySkill.skill_code).where(ActivitySkill.activity_id == a.id)))
    return ActivityDetail(
        **ActivitySummary.model_validate(a).model_dump(), skills=codes, definition=public_definition(a.definition)
    )
