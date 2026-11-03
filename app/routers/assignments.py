"""Assignments: an admin hands an existing draft to an educator with written requirements and an optional due date.

Admins (content_admin, super_admin) create, edit, reassign and cancel; educators only read their own assignments
(and work on the activity through the normal activity endpoints, which an assignment opens up to them).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.deps import AdminActor, DbSession, StaffActor
from app.errors import ApiError, not_found
from app.models import Activity, ActivityAssignment, User
from app.schemas import AssignmentCreateIn, AssignmentCreateOut, AssignmentOut, AssignmentPatchIn, AssignmentStatus
from app.security import now
from app.services import assignments as svc
from app.services import email as email_service
from app.services.events import audit, record_activity_event

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _assignment_for(db, actor, assignment_id: uuid.UUID) -> ActivityAssignment:
    row = db.get(ActivityAssignment, assignment_id)
    if row is None or (not actor.is_content_admin and row.educator_id != actor.user_id):
        raise not_found()  # an educator never learns that someone else's assignment exists
    return row


@router.post("/activities/{activity_id}/assignments", response_model=AssignmentCreateOut, status_code=201)
def assign_activity(
    activity_id: uuid.UUID, body: AssignmentCreateIn, actor: AdminActor, db: DbSession
) -> AssignmentCreateOut:
    """Assigns a draft to an educator. If it is already assigned to someone else, that assignment is cancelled
    (and kept as history) and replaced; assigning it again to the same person is a 409: edit the assignment instead."""
    activity = db.get(Activity, activity_id)
    if activity is None:
        raise not_found()
    if activity.status != "draft":
        raise ApiError(409, "conflict", f"Only a draft can be assigned (this one is {activity.status}).")
    educator = db.get(User, body.educator_id)
    if educator is None or educator.platform_role != "educator":
        raise ApiError(404, "not_found", "That person is not an educator.")
    if educator.status != "active":
        raise ApiError(409, "conflict", "This educator is disabled. Re-enable them before assigning work.")
    if body.due_date is not None and body.due_date < svc.today():
        raise ApiError(422, "validation_error", "The due date cannot be in the past.")
    current = svc.live_assignment(db, activity.id)
    if current is not None:
        if current.educator_id == educator.id:
            raise ApiError(
                409, "conflict", "This activity is already assigned to that educator. Edit the assignment instead."
            )
        svc.close(db, current, "cancelled", actor.user_id, activity, reason="reassigned")
        db.flush()  # free the one-live-assignment slot before inserting the replacement
    row = ActivityAssignment(
        activity_id=activity.id,
        educator_id=educator.id,
        assigned_by=actor.user_id,
        requirements=body.requirements,
        due_date=body.due_date,
    )
    db.add(row)
    db.flush()
    admin = db.get(User, actor.user_id)
    record_activity_event(
        db,
        activity,
        actor.user_id,
        "assigned",
        assignment_id=str(row.id),
        educator_id=str(educator.id),
        educator_name=educator.full_name,
        **({"due_date": body.due_date.isoformat()} if body.due_date else {}),
    )
    audit(
        db,
        "assignment.created",
        actor_user_id=actor.user_id,
        entity="assignment",
        entity_id=row.id,
        activity_id=str(activity.id),
        educator_id=str(educator.id),
        request_id=actor.request_id,
    )
    try:
        email_service.send_activity_assignment(
            educator.email,
            educator.full_name,
            activity.title,
            row.requirements,
            row.due_date,
            admin.full_name if admin else "An administrator",
        )
        sent = True
    except ApiError:
        sent = False
    return AssignmentCreateOut(assignment=svc.outs(db, [row])[0], email_sent=sent)


@router.get("/assignments", response_model=list[AssignmentOut])
def list_assignments(
    actor: StaffActor,
    db: DbSession,
    status: AssignmentStatus | None = None,
    live: bool = Query(default=False, description="true = only open and submitted"),
    educator_id: uuid.UUID | None = None,
    overdue: bool = False,
    activity_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[AssignmentOut]:
    """Admins: everyone's assignments (filter by educator, status, overdue). Educators: only their own."""
    stmt = select(ActivityAssignment)
    if not actor.is_content_admin:
        stmt = stmt.where(ActivityAssignment.educator_id == actor.user_id)
    elif educator_id:
        stmt = stmt.where(ActivityAssignment.educator_id == educator_id)
    if status:
        stmt = stmt.where(ActivityAssignment.status == status)
    if live:
        stmt = stmt.where(ActivityAssignment.status.in_(svc.LIVE))
    if activity_id:
        stmt = stmt.where(ActivityAssignment.activity_id == activity_id)
    if overdue:
        stmt = stmt.where(
            ActivityAssignment.status == "open",
            ActivityAssignment.due_date.is_not(None),
            ActivityAssignment.due_date < svc.today(),
        )
    # Live work first, then the nearest due date, then the newest.
    rows = list(
        db.scalars(
            stmt.order_by(
                ActivityAssignment.status.in_(svc.LIVE).desc(),
                ActivityAssignment.due_date.asc().nulls_last(),
                ActivityAssignment.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    )
    return svc.outs(db, rows)


@router.get("/assignments/{assignment_id}", response_model=AssignmentOut)
def get_assignment(assignment_id: uuid.UUID, actor: StaffActor, db: DbSession) -> AssignmentOut:
    return svc.outs(db, [_assignment_for(db, actor, assignment_id)])[0]


@router.patch("/assignments/{assignment_id}", response_model=AssignmentOut)
def patch_assignment(
    assignment_id: uuid.UUID, body: AssignmentPatchIn, actor: AdminActor, db: DbSession
) -> AssignmentOut:
    row = _assignment_for(db, actor, assignment_id)
    if row.status not in svc.LIVE:
        raise ApiError(409, "conflict", f"A {row.status} assignment cannot be changed.")
    sent = body.model_fields_set
    if "requirements" in sent and body.requirements is not None:
        row.requirements = body.requirements
    if "due_date" in sent:
        if body.due_date is not None and body.due_date < svc.today():
            raise ApiError(422, "validation_error", "The due date cannot be in the past.")
        row.due_date = body.due_date
    row.updated_at = now()
    activity = db.get(Activity, row.activity_id)
    record_activity_event(db, activity, actor.user_id, "assignment_updated", assignment_id=str(row.id))
    audit(
        db,
        "assignment.updated",
        actor_user_id=actor.user_id,
        entity="assignment",
        entity_id=row.id,
        request_id=actor.request_id,
    )
    return svc.outs(db, [row])[0]


@router.post("/assignments/{assignment_id}/cancel", response_model=AssignmentOut)
def cancel_assignment(assignment_id: uuid.UUID, actor: AdminActor, db: DbSession) -> AssignmentOut:
    """Withdraws the assignment. The activity stays as it is; the educator loses access to it."""
    row = _assignment_for(db, actor, assignment_id)
    if row.status not in svc.LIVE:
        raise ApiError(409, "conflict", f"A {row.status} assignment cannot be cancelled.")
    activity = db.get(Activity, row.activity_id)
    svc.close(db, row, "cancelled", actor.user_id, activity, reason="withdrawn")
    audit(
        db,
        "assignment.cancelled",
        actor_user_id=actor.user_id,
        entity="assignment",
        entity_id=row.id,
        request_id=actor.request_id,
    )
    return svc.outs(db, [row])[0]
