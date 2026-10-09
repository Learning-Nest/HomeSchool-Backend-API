"""Admin views of educators and their work: who they are, what they wrote, and a filterable activity log.

Only content_admin and super_admin read these; creating or deactivating an educator needs super_admin.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select, update

from app.deps import AdminActor, DbSession, SuperAdminActor
from app.errors import ApiError, not_found
from app.models import Activity, ActivityEvent, RefreshToken, User
from app.routers.admin import summaries
from app.schemas import (
    ActivityEventOut,
    EducatorActivityOut,
    EducatorCreateIn,
    EducatorCreateOut,
    EducatorOut,
    EducatorPatchIn,
)
from app.security import hash_secret, now
from app.services import email as email_service
from app.services.auth import new_temp_password
from app.services.events import audit

router = APIRouter(prefix="/v1/admin", tags=["admin"])
INVITE_VALID_DAYS = 7


def educator_outs(db, users: list[User]) -> list[EducatorOut]:
    ids = [u.id for u in users]
    if not ids:
        return []
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for author, status, n in db.execute(
        select(Activity.created_by, Activity.status, func.count())
        .where(Activity.created_by.in_(ids))
        .group_by(Activity.created_by, Activity.status)
    ):
        counts.setdefault(author, {})[status] = n
    submissions = dict(
        db.execute(
            select(ActivityEvent.actor_id, func.count())
            .where(ActivityEvent.actor_id.in_(ids), ActivityEvent.action == "submitted")
            .group_by(ActivityEvent.actor_id)
        ).all()
    )
    returned = dict(
        db.execute(
            select(Activity.created_by, func.count())
            .select_from(ActivityEvent)
            .join(Activity, Activity.id == ActivityEvent.activity_id)
            .where(Activity.created_by.in_(ids), ActivityEvent.action == "returned")
            .group_by(Activity.created_by)
        ).all()
    )
    last = dict(
        db.execute(
            select(ActivityEvent.actor_id, func.max(ActivityEvent.at))
            .where(ActivityEvent.actor_id.in_(ids))
            .group_by(ActivityEvent.actor_id)
        ).all()
    )
    out = []
    for u in users:
        c = counts.get(u.id, {})
        out.append(
            EducatorOut(
                id=u.id,
                email=u.email,
                full_name=u.full_name,
                active=u.status == "active",
                platform_role=u.platform_role,
                created_at=u.created_at,
                activities=sum(c.values()),
                drafts=c.get("draft", 0),
                in_review=c.get("in_review", 0),
                published=c.get("published", 0),
                submissions=submissions.get(u.id, 0),
                returned=returned.get(u.id, 0),
                last_active_at=last.get(u.id),
            )
        )
    return out


def _educator(db, educator_id: uuid.UUID) -> User:
    u = db.get(User, educator_id)
    if u is None or u.platform_role != "educator":
        raise not_found()
    return u


def event_outs(db, rows) -> list[ActivityEventOut]:
    """rows: (ActivityEvent, Activity) pairs."""
    names = (
        dict(
            db.execute(
                select(User.id, User.full_name).where(User.id.in_({e.actor_id for e, _ in rows if e.actor_id}))
            ).all()
        )
        if rows
        else {}
    )
    return [
        ActivityEventOut(
            id=e.id,
            at=e.at,
            action=e.action,
            actor_id=e.actor_id,
            actor_name=names.get(e.actor_id),
            activity_id=a.id,
            activity_slug=a.slug,
            activity_title=a.title,
            version=e.version,
            status_from=e.status_from,
            status_to=e.status_to,
            detail=e.detail or {},
        )
        for e, a in rows
    ]


def _event_query(actor_id, action, activity_id, date_from, date_to):
    stmt = select(ActivityEvent, Activity).join(Activity, Activity.id == ActivityEvent.activity_id)
    if actor_id:
        stmt = stmt.where(ActivityEvent.actor_id == actor_id)
    if action:
        stmt = stmt.where(ActivityEvent.action == action)
    if activity_id:
        stmt = stmt.where(ActivityEvent.activity_id == activity_id)
    if date_from:
        stmt = stmt.where(ActivityEvent.at >= date_from)
    if date_to:
        stmt = stmt.where(
            ActivityEvent.at < date_to + timedelta(days=1)
            if date_to.hour == date_to.minute == 0
            else ActivityEvent.at <= date_to
        )
    return stmt.order_by(ActivityEvent.at.desc())


@router.get("/educators", response_model=list[EducatorOut])
def list_educators(actor: AdminActor, db: DbSession) -> list[EducatorOut]:
    users = list(db.scalars(select(User).where(User.platform_role == "educator").order_by(User.full_name)))
    return educator_outs(db, users)


@router.get("/educators/{educator_id}/activity", response_model=EducatorActivityOut)
def educator_activity(
    educator_id: uuid.UUID,
    actor: AdminActor,
    db: DbSession,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> EducatorActivityOut:
    u = _educator(db, educator_id)
    acts = list(
        db.scalars(select(Activity).where(Activity.created_by == u.id).order_by(Activity.updated_at.desc()).limit(500))
    )
    rows = list(db.execute(_event_query(u.id, None, None, date_from, date_to).limit(limit)).all())
    return EducatorActivityOut(
        educator=educator_outs(db, [u])[0], activities=summaries(db, acts), events=event_outs(db, rows)
    )


@router.get("/activity-log", response_model=list[ActivityEventOut])
def activity_log(
    actor: AdminActor,
    db: DbSession,
    actor_id: uuid.UUID | None = None,
    action: str | None = Query(default=None, max_length=40),
    activity_id: uuid.UUID | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    format: str = Query(default="json", pattern="^(json|csv)$"),
):
    rows = list(
        db.execute(_event_query(actor_id, action, activity_id, date_from, date_to).limit(limit).offset(offset)).all()
    )
    events = event_outs(db, rows)
    if format == "json":
        return events
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "who", "action", "activity", "slug", "version", "from", "to", "detail"])
    for e in events:
        w.writerow(
            [
                e.at.isoformat(),
                e.actor_name or "",
                e.action,
                e.activity_title,
                e.activity_slug,
                e.version or "",
                e.status_from or "",
                e.status_to or "",
                e.detail or "",
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="activity-log.csv"'},
    )


@router.post("/educators", response_model=EducatorCreateOut, status_code=201)
def create_educator(body: EducatorCreateIn, actor: SuperAdminActor, db: DbSession) -> EducatorCreateOut:
    email = body.email.strip()
    user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
    existing = user is not None
    sent = False
    if user is None:
        temp = new_temp_password()
        user = User(
            email=email,
            full_name=body.full_name.strip(),
            # The real password is unknowable; the person signs in with the emailed temporary one and must replace it.
            password_hash=hash_secret(new_temp_password(24)),
            platform_role="educator",
            reset_hash=hash_secret(temp),
            reset_expires_at=now() + timedelta(days=INVITE_VALID_DAYS),
            must_change_password=False,
        )
        db.add(user)
        db.flush()
        try:
            email_service.send_educator_invite(user.email, user.full_name, temp, INVITE_VALID_DAYS)
            sent = True
        except ApiError:
            sent = False
    else:
        if user.platform_role is not None:
            raise ApiError(409, "conflict", "This account already has a platform role.")
        user.platform_role = "educator"
    audit(
        db,
        "educator.created",
        actor_user_id=actor.user_id,
        entity="user",
        entity_id=user.id,
        existing_account=existing,
        request_id=actor.request_id,
    )
    return EducatorCreateOut(educator=educator_outs(db, [user])[0], existing_account=existing, email_sent=sent)


@router.patch("/educators/{educator_id}", response_model=EducatorOut)
def patch_educator(educator_id: uuid.UUID, body: EducatorPatchIn, actor: SuperAdminActor, db: DbSession) -> EducatorOut:
    u = _educator(db, educator_id)
    u.status = "active" if body.active else "disabled"
    u.updated_at = now()
    if not body.active:  # sign them out everywhere; their history stays
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == u.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now())
        )
    audit(
        db,
        "educator.deactivated" if not body.active else "educator.reactivated",
        actor_user_id=actor.user_id,
        entity="user",
        entity_id=u.id,
        request_id=actor.request_id,
    )
    return educator_outs(db, [u])[0]
