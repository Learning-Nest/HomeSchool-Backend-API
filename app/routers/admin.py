"""Content authoring API.

Roles (users.platform_role): ``educator`` writes drafts and submits them; ``content_admin`` and ``super_admin`` also
review, publish and see everyone's work. Admins never see family or child data (RLS).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.deps import AdminActor, DbSession, StaffActor
from app.errors import ApiError, not_found
from app.models import Activity, ActivitySkill, Family, Skill, User
from app.schemas import (
    ActivityDraftIn,
    AdminActivityDetail,
    AdminActivitySummary,
    AdminDefinitionIn,
    AdminStats,
    AdminStatusIn,
    BundleIn,
    BundleReport,
    ProblemOut,
    StaffMeOut,
    ValidateIn,
    ValidationOut,
)
from app.security import now
from app.services import catalog
from app.services.events import audit, record_activity_event

router = APIRouter(prefix="/v1/admin", tags=["admin"])
TRANSITIONS = {
    "draft": {"in_review", "archived"},
    "in_review": {"draft", "published", "archived"},
    "published": {"archived"},
    "archived": {"draft"},
}
EDIT_COALESCE_MINUTES = 10  # autosaves within this window count as one 'edited' event


# ------------------------------------------------------------------------------------------------ helpers
def _names(db, ids) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(ids))).all())


def summaries(db, rows: list[Activity]) -> list[AdminActivitySummary]:
    names = _names(db, [x for a in rows for x in (a.created_by, a.last_edited_by, a.reviewed_by)])
    out = []
    for a in rows:
        s = AdminActivitySummary.model_validate(a)
        s.created_by_name = names.get(a.created_by)
        s.last_edited_by_name = names.get(a.last_edited_by)
        s.reviewed_by_name = names.get(a.reviewed_by)
        s.is_validated = a.validated_hash is not None and a.validated_hash == catalog.definition_hash(a.definition)
        out.append(s)
    return out


def _detail(db, a: Activity) -> AdminActivityDetail:
    codes = sorted(db.scalars(select(ActivitySkill.skill_code).where(ActivitySkill.activity_id == a.id)))
    base = summaries(db, [a])[0].model_dump()
    return AdminActivityDetail(**base, skills=codes, definition=a.definition)


def get_activity_for(db, actor, activity_id: uuid.UUID) -> Activity:
    """An admin reaches any activity; an educator only their own (anything else is a 404, never a 403)."""
    a = db.get(Activity, activity_id)
    if a is None or (not actor.is_content_admin and a.created_by != actor.user_id):
        raise not_found()
    return a


def _require_editable(actor, a: Activity) -> None:
    if not actor.is_content_admin and a.status != "draft":
        raise ApiError(409, "activity_locked", "Only a draft can be edited. Ask an admin to return it first.")


def _problem_outs(definition: dict, problems: list[str]) -> list[ProblemOut]:
    step_ids = {s.get("id") for s in definition.get("steps", []) if isinstance(s, dict)}
    out = []
    for p in problems:
        head, sep, rest = p.partition(": ")
        if sep and head in step_ids:
            out.append(ProblemOut(step=head, message=rest))
        elif sep and head.startswith("steps/") and head.split("/")[1].isdigit():
            idx = int(head.split("/")[1])
            steps = definition.get("steps", [])
            sid = steps[idx].get("id") if idx < len(steps) and isinstance(steps[idx], dict) else None
            out.append(ProblemOut(step=sid, message=p))
        else:
            out.append(ProblemOut(message=p))
    return out


# ------------------------------------------------------------------------------------------------ who am I
CAPABILITIES = {
    "educator": ["activities.write_own", "activities.validate", "activities.submit", "assets.upload"],
    "content_admin": [
        "activities.write_own",
        "activities.validate",
        "activities.submit",
        "assets.upload",
        "activities.read_all",
        "activities.review",
        "activities.publish",
        "bundle.import",
        "educators.view",
        "activity_log.view",
    ],
}
CAPABILITIES["super_admin"] = [*CAPABILITIES["content_admin"], "educators.manage"]


@router.get("/me", response_model=StaffMeOut)
def me(actor: StaffActor, db: DbSession) -> StaffMeOut:
    u = db.get(User, actor.user_id)
    return StaffMeOut(
        user_id=u.id,
        email=u.email,
        full_name=u.full_name,
        platform_role=actor.platform_role,
        capabilities=CAPABILITIES[actor.platform_role],
    )


@router.get("/stats", response_model=AdminStats)
def stats(actor: AdminActor, db: DbSession) -> AdminStats:
    by_status = dict(db.execute(select(Activity.status, func.count()).group_by(Activity.status)).all())
    return AdminStats(
        users=db.scalar(select(func.count()).select_from(User)) or 0,
        families=db.scalar(select(func.count()).select_from(Family)) or 0,
        activities_by_status=by_status,
        skills=db.scalar(select(func.count()).select_from(Skill)) or 0,
    )


# ------------------------------------------------------------------------------------------------ activities
@router.get("/activities", response_model=list[AdminActivitySummary])
def list_activities(
    actor: StaffActor,
    db: DbSession,
    status: str | None = None,
    subject: str | None = None,
    author: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AdminActivitySummary]:
    stmt = select(Activity)
    if not actor.is_content_admin:
        stmt = stmt.where(Activity.created_by == actor.user_id)  # educators: only their own work
    elif author:
        stmt = stmt.where(Activity.created_by == author)
    if status:
        stmt = stmt.where(Activity.status == status)
    if subject:
        stmt = stmt.where(Activity.subject_code == subject)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Activity.title).like(like), func.lower(Activity.slug).like(like)))
    rows = list(db.scalars(stmt.order_by(Activity.updated_at.desc()).limit(limit).offset(offset)))
    return summaries(db, rows)


@router.post("/activities", response_model=AdminActivityDetail, status_code=201)
def create_activity(body: ActivityDraftIn, actor: StaffActor, db: DbSession) -> AdminActivityDetail:
    """Creates a DRAFT. It needs only the basics (title, subject, levels, duration); Validate checks the rest."""
    source = "admin" if actor.is_content_admin else "educator"
    a = catalog.create_activity(db, body.definition, actor.user_id, source)
    record_activity_event(db, a, actor.user_id, "created", status_to="draft")
    audit(
        db,
        "content.activity_created",
        actor_user_id=actor.user_id,
        entity="activity",
        entity_id=a.id,
        request_id=actor.request_id,
    )
    return _detail(db, a)


@router.post("/activities/validate", response_model=ValidationOut)
def validate_draft(body: ValidateIn, actor: StaffActor, db: DbSession) -> ValidationOut:
    """Dry run of the full check. Saves nothing."""
    if body.activity_id is not None:
        get_activity_for(db, actor, body.activity_id)
    d = catalog.finalize(body.definition) if isinstance(body.definition, dict) else body.definition
    problems = catalog.validate_full(db, d, body.activity_id)
    return ValidationOut(ok=not problems, problems=_problem_outs(d, problems))


@router.get("/activities/{activity_id}", response_model=AdminActivityDetail)
def get_activity(activity_id: uuid.UUID, actor: StaffActor, db: DbSession) -> AdminActivityDetail:
    return _detail(db, get_activity_for(db, actor, activity_id))


@router.put("/activities/{activity_id}/definition", response_model=AdminActivityDetail)
def put_definition(
    activity_id: uuid.UUID,
    body: AdminDefinitionIn,
    actor: StaffActor,
    db: DbSession,
    strict: bool = Query(default=True, description="false = store a draft that is not valid yet (drafts only)"),
) -> AdminActivityDetail:
    a = get_activity_for(db, actor, activity_id)
    _require_editable(actor, a)
    changed = catalog.update_definition(db, a, body.definition, editor_id=actor.user_id, strict=strict)
    if changed:
        a.updated_at = now()
        record_activity_event(db, a, actor.user_id, "edited", coalesce_minutes=EDIT_COALESCE_MINUTES)
        audit(
            db,
            "content.definition_updated",
            actor_user_id=actor.user_id,
            entity="activity",
            entity_id=a.id,
            version=a.version,
            request_id=actor.request_id,
        )
    return _detail(db, a)


@router.post("/activities/{activity_id}/validate", response_model=ValidationOut)
def validate_saved(activity_id: uuid.UUID, actor: StaffActor, db: DbSession) -> ValidationOut:
    """Validates the SAVED content and records the result: Submit is allowed only while this exact content is valid."""
    a = get_activity_for(db, actor, activity_id)
    problems = catalog.validate_full(db, a.definition, a.id)
    a.last_validated_at = now()
    a.validated_hash = None if problems else catalog.definition_hash(a.definition)
    record_activity_event(db, a, actor.user_id, "validated", ok=not problems, problems=len(problems))
    return ValidationOut(
        ok=not problems, problems=_problem_outs(a.definition, problems), validated_at=a.last_validated_at
    )


@router.post("/activities/{activity_id}/submit", response_model=AdminActivityDetail)
def submit(activity_id: uuid.UUID, actor: StaffActor, db: DbSession) -> AdminActivityDetail:
    a = get_activity_for(db, actor, activity_id)
    if a.status != "draft":
        raise ApiError(409, "conflict", f"Only a draft can be submitted (this one is {a.status}).")
    if a.validated_hash is None or a.validated_hash != catalog.definition_hash(a.definition):
        raise ApiError(422, "validation_required")
    a.status, a.submitted_by, a.submitted_at, a.review_note = "in_review", actor.user_id, now(), None
    a.updated_at = now()
    record_activity_event(db, a, actor.user_id, "submitted", status_from="draft", status_to="in_review")
    audit(
        db,
        "content.status_changed",
        actor_user_id=actor.user_id,
        entity="activity",
        entity_id=a.id,
        status=a.status,
        request_id=actor.request_id,
    )
    return _detail(db, a)


@router.post("/activities/{activity_id}/status", response_model=AdminActivityDetail)
def set_status(activity_id: uuid.UUID, body: AdminStatusIn, actor: AdminActor, db: DbSession) -> AdminActivityDetail:
    a = db.get(Activity, activity_id)
    if a is None:
        raise not_found()
    if body.status == a.status:
        return _detail(db, a)
    if body.status not in TRANSITIONS[a.status]:
        raise ApiError(409, "conflict", f"Cannot move from {a.status} to {body.status}.")
    before = a.status
    if body.status == "published":
        catalog.publish(db, a)
    else:
        a.status = body.status
    if before == "in_review":  # a review decision: approve (publish), return (draft) or archive
        a.reviewed_by, a.reviewed_at = actor.user_id, now()
        a.review_note = body.note if body.status == "draft" else None
    a.updated_at = now()
    action = {"published": "published", "archived": "archived"}.get(
        body.status,
        "returned"
        if before == "in_review" and body.status == "draft"
        else "submitted"
        if body.status == "in_review"
        else "reopened",
    )
    record_activity_event(
        db,
        a,
        actor.user_id,
        action,
        status_from=before,
        status_to=a.status,
        **({"note": body.note} if body.note else {}),
    )
    audit(
        db,
        "content.status_changed",
        actor_user_id=actor.user_id,
        entity="activity",
        entity_id=a.id,
        status=a.status,
        request_id=actor.request_id,
    )
    return _detail(db, a)


@router.post("/content/bundle", response_model=BundleReport)
def import_bundle(body: BundleIn, actor: AdminActor, db: DbSession) -> BundleReport:
    report = catalog.import_bundle(db, body)
    if not body.dry_run and report.ok:
        audit(
            db,
            "content.bundle_imported",
            actor_user_id=actor.user_id,
            entity="bundle",
            entity_id=body.version,
            created=report.created,
            updated=report.updated,
            request_id=actor.request_id,
        )
    return report
