"""Weekly plans. A plan is one row per child and week (Monday start); items are scheduled activities."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.deps import ALL_ROLES, CurrentActor, DbSession, ParentActor, load_child
from app.errors import ApiError, conflict, not_found
from app.models import Activity, Child, Lesson, Plan, PlanItem
from app.schemas import ActivitySummary, PlanItemIn, PlanItemOut, PlanItemPatch, PlanOut
from app.security import now
from app.services import daily
from app.services.events import audit
from app.services.progress import family_tz, week_start_of

router = APIRouter(prefix="/v1", tags=["plans"])


def item_out(
    item: PlanItem, activity: Activity, lesson_title: str | None = None, carry_over: bool = False
) -> PlanItemOut:
    return PlanItemOut(
        id=item.id,
        child_id=item.child_id,
        activity=ActivitySummary.model_validate(activity),
        scheduled_date=item.scheduled_date,
        position=item.position,
        status=item.status,
        version=item.version,
        source=item.source,
        released_on=item.released_on,
        book_code=item.book_code,
        lesson_position=item.lesson_position,
        lesson_title=lesson_title,
        carry_over=carry_over,
    )


def lesson_titles(db, items: list[PlanItem]) -> dict[tuple[str, int], str]:
    pairs = {(i.book_code, i.lesson_position) for i in items if i.book_code and i.lesson_position is not None}
    if not pairs:
        return {}
    rows = db.execute(
        select(Lesson.book_code, Lesson.position, Lesson.title).where(Lesson.book_code.in_({b for b, _ in pairs}))
    ).all()
    return {(b, p): t for b, p, t in rows if (b, p) in pairs}


def outs_for(db, rows, today: date | None = None) -> list[PlanItemOut]:
    """PlanItemOut for (PlanItem, Activity) rows, with lesson titles and the carry-over flag."""
    titles = lesson_titles(db, [i for i, _ in rows])
    return [
        item_out(
            i,
            a,
            titles.get((i.book_code, i.lesson_position)) if i.book_code else None,
            carry_over=bool(today and i.scheduled_date < today and i.status in daily.OPEN_STATUSES),
        )
        for i, a in rows
    ]


def items_between(db, child_id: uuid.UUID, start: date, end_exclusive: date) -> list[PlanItemOut]:
    rows = db.execute(
        select(PlanItem, Activity)
        .join(Activity, Activity.id == PlanItem.activity_id)
        .where(PlanItem.child_id == child_id, PlanItem.scheduled_date >= start, PlanItem.scheduled_date < end_exclusive)
        .order_by(PlanItem.scheduled_date, PlanItem.position, PlanItem.created_at)
    ).all()
    return outs_for(db, rows)


def today_items(db, child_id: uuid.UUID, today: date) -> list[PlanItemOut]:
    """Today's items plus anything the system released earlier (or a practice item) that is still open."""
    rows = db.execute(
        select(PlanItem, Activity)
        .join(Activity, Activity.id == PlanItem.activity_id)
        .where(
            PlanItem.child_id == child_id,
            (PlanItem.scheduled_date == today)
            | (
                (PlanItem.scheduled_date < today)
                & PlanItem.source.in_(("system", "revision"))
                & PlanItem.status.in_(daily.OPEN_STATUSES)
            ),
        )
        .order_by(PlanItem.scheduled_date, PlanItem.position, PlanItem.created_at)
    ).all()
    return outs_for(db, rows, today)


def require_parent_planning(db, child: Child) -> None:
    """Families whose activities are chosen by LearnNest cannot plan by hand."""
    if daily.is_system_family(db, child.family_id):
        raise ApiError(403, "plan_managed")


def _plan_for(db, child: Child, day: date) -> Plan:
    ws = week_start_of(day)
    plan = db.scalar(select(Plan).where(Plan.child_id == child.id, Plan.week_start == ws))
    if plan is None:
        plan = Plan(family_id=child.family_id, child_id=child.id, week_start=ws)
        db.add(plan)
        db.flush()
    return plan


@router.get("/children/{child_id}/plan", response_model=PlanOut)
def get_plan(child_id: uuid.UUID, actor: CurrentActor, db: DbSession, week_start: date | None = None) -> PlanOut:
    child = load_child(db, actor, child_id)
    ws = week_start_of(week_start or now().astimezone(family_tz(db, child.family_id)).date())
    return PlanOut(child_id=child.id, week_start=ws, items=items_between(db, child.id, ws, ws + timedelta(days=7)))


@router.get("/children/{child_id}/today", response_model=list[PlanItemOut])
def today(child_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> list[PlanItemOut]:
    child = load_child(db, actor, child_id)
    d = now().astimezone(family_tz(db, child.family_id)).date()
    return today_items(db, child.id, d)


@router.post("/children/{child_id}/plan/items", response_model=PlanItemOut, status_code=201)
def add_item(child_id: uuid.UUID, body: PlanItemIn, actor: ParentActor, db: DbSession) -> PlanItemOut:
    child = load_child(db, actor, child_id, ALL_ROLES)
    require_parent_planning(db, child)
    activity = db.scalar(select(Activity).where(Activity.id == body.activity_id, Activity.status == "published"))
    if activity is None:
        raise ApiError(422, "validation_error", "The activity does not exist or is not published.")
    plan = _plan_for(db, child, body.scheduled_date)
    item = PlanItem(
        family_id=child.family_id,
        child_id=child.id,
        plan_id=plan.id,
        activity_id=activity.id,
        scheduled_date=body.scheduled_date,
        position=body.position,
    )
    db.add(item)
    db.flush()
    audit(
        db,
        "plan.item_added",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="plan_item",
        entity_id=item.id,
        request_id=actor.request_id,
    )
    return item_out(item, activity)


def _load_item(db, actor, item_id: uuid.UUID) -> tuple[PlanItem, Activity, Child]:
    row = db.execute(
        select(PlanItem, Activity).join(Activity, Activity.id == PlanItem.activity_id).where(PlanItem.id == item_id)
    ).first()
    if row is None:
        raise not_found()
    child = load_child(db, actor, row[0].child_id, ALL_ROLES)
    return row[0], row[1], child


@router.patch("/plan-items/{item_id}", response_model=PlanItemOut)
def patch_item(item_id: uuid.UUID, body: PlanItemPatch, actor: ParentActor, db: DbSession) -> PlanItemOut:
    item, activity, child = _load_item(db, actor, item_id)
    if item.version != body.version:
        raise conflict("This plan item was changed elsewhere. Reload and try again.")
    if item.status == "completed":
        raise conflict("A completed item cannot be changed.")
    if daily.is_system_family(db, child.family_id) and (
        item.source == "parent" or body.scheduled_date is not None or body.position is not None
    ):
        raise ApiError(403, "plan_managed")  # in system mode a parent may only skip or un-skip released items
    if body.scheduled_date is not None and body.scheduled_date != item.scheduled_date:
        item.scheduled_date = body.scheduled_date
        item.plan_id = _plan_for(db, child, body.scheduled_date).id
    if body.position is not None:
        item.position = body.position
    if body.status is not None:
        item.status = body.status
    item.version += 1
    item.updated_at = now()
    return item_out(item, activity)


@router.delete("/plan-items/{item_id}", status_code=204)
def delete_item(item_id: uuid.UUID, actor: ParentActor, db: DbSession) -> Response:
    item, _activity, child = _load_item(db, actor, item_id)
    if daily.is_system_family(db, child.family_id) and item.source != "revision":
        raise ApiError(403, "plan_managed")  # only a practice item can be taken back
    if item.status in ("completed", "in_progress"):
        raise conflict("Only planned or skipped items can be removed.")
    db.delete(item)
    return Response(status_code=204)
