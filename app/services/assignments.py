"""Assignments of draft activities to educators: access rules, the lifecycle that follows the activity's status,
and the response shape. Written by an admin, worked on by an educator, completed when the activity is published."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Activity, ActivityAssignment, User
from app.schemas import AssignmentOut
from app.security import now
from app.services.events import record_activity_event

LIVE = ("open", "submitted")
IST = timezone(timedelta(hours=5, minutes=30))  # the product's audience; due dates are calendar days there


def today() -> date:
    return datetime.now(IST).date()


def live_assignment(db, activity_id: uuid.UUID) -> ActivityAssignment | None:
    return db.scalar(
        select(ActivityAssignment).where(
            ActivityAssignment.activity_id == activity_id, ActivityAssignment.status.in_(LIVE)
        )
    )


def can_reach(db, user_id: uuid.UUID, activity_id: uuid.UUID) -> bool:
    """An educator keeps access to an activity while it is assigned to them and afterwards if it was completed;
    a cancelled or reassigned assignment takes the access away."""
    return (
        db.scalar(
            select(ActivityAssignment.id)
            .where(
                ActivityAssignment.activity_id == activity_id,
                ActivityAssignment.educator_id == user_id,
                ActivityAssignment.status != "cancelled",
            )
            .limit(1)
        )
        is not None
    )


def assigned_activity_ids(user_id: uuid.UUID):
    """A subquery of the activity ids an educator can reach through assignments."""
    return select(ActivityAssignment.activity_id).where(
        ActivityAssignment.educator_id == user_id, ActivityAssignment.status != "cancelled"
    )


def is_overdue(row: ActivityAssignment) -> bool:
    return row.status == "open" and row.due_date is not None and row.due_date < today()


def outs(db, rows: list[ActivityAssignment]) -> list[AssignmentOut]:
    if not rows:
        return []
    acts = {a.id: a for a in db.scalars(select(Activity).where(Activity.id.in_({r.activity_id for r in rows})))}
    ids = {x for r in rows for x in (r.educator_id, r.assigned_by) if x}
    names = dict(db.execute(select(User.id, User.full_name).where(User.id.in_(ids))).all()) if ids else {}
    result = []
    for r in rows:
        a = acts[r.activity_id]
        result.append(
            AssignmentOut(
                id=r.id,
                activity_id=a.id,
                activity_slug=a.slug,
                activity_title=a.title,
                activity_status=a.status,
                educator_id=r.educator_id,
                educator_name=names.get(r.educator_id),
                assigned_by=r.assigned_by,
                assigned_by_name=names.get(r.assigned_by),
                requirements=r.requirements,
                due_date=r.due_date,
                status=r.status,
                overdue=is_overdue(r),
                review_note=a.review_note if r.status == "open" else None,
                created_at=r.created_at,
                updated_at=r.updated_at,
                submitted_at=r.submitted_at,
                completed_at=r.completed_at,
                cancelled_at=r.cancelled_at,
            )
        )
    return result


def close(db, row: ActivityAssignment, status: str, actor_id: uuid.UUID | None, activity: Activity, **detail) -> None:
    """Move a live assignment to completed or cancelled and log it on the activity."""
    row.status = status
    stamp = now()
    row.updated_at = stamp
    if status == "completed":
        row.completed_at = stamp
    else:
        row.cancelled_at = stamp
    record_activity_event(
        db,
        activity,
        actor_id,
        "assignment_completed" if status == "completed" else "assignment_cancelled",
        assignment_id=str(row.id),
        educator_id=str(row.educator_id),
        **detail,
    )


def on_status_change(db, activity: Activity, before: str, actor_id: uuid.UUID | None) -> None:
    """Keep the live assignment in step with the activity: submit -> submitted, sent back -> open again,
    published -> completed, archived -> cancelled."""
    row = live_assignment(db, activity.id)
    if row is None or before == activity.status:
        return
    after = activity.status
    if after == "in_review":
        row.status, row.submitted_at, row.updated_at = "submitted", now(), now()
    elif after == "draft":
        row.status, row.submitted_at, row.updated_at = "open", None, now()
    elif after == "published":
        close(db, row, "completed", actor_id, activity)
    elif after == "archived":
        close(db, row, "cancelled", actor_id, activity, reason="archived")
