"""Child profiles and child sessions (a child has no credentials: a signed-in parent opens a child session)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update

from app.config import get_settings
from app.deps import (
    ALL_ROLES,
    WRITE_ROLES,
    CurrentActor,
    DbSession,
    ParentActor,
    load_child,
    membership,
    require_elevation,
    require_guardian_verified,
)
from app.errors import ApiError, conflict
from app.models import Board, Child, ChildSession, Interest, Level
from app.schemas import ChildIn, ChildOut, ChildPatch, ChildSessionIn, ChildSessionOut
from app.security import create_child_token, now
from app.services import daily
from app.services.events import audit, emit

router = APIRouter(tags=["children"])


def _validate_refs(
    db, level_code: str | None, interests: list[str] | None, board_code: str | None = None, grade: str | None = None
) -> None:
    if level_code and db.get(Level, level_code) is None:
        raise ApiError(422, "validation_error", f"Unknown level {level_code}.")
    if board_code and db.get(Board, board_code) is None:
        raise ApiError(422, "validation_error", f"Unknown board {board_code}.")
    if grade is not None and not grade.strip():
        raise ApiError(422, "validation_error", "The grade cannot be blank.")
    if interests:
        known = set(db.scalars(select(Interest.code).where(Interest.code.in_(interests))))
        missing = sorted(set(interests) - known)
        if missing:
            raise ApiError(422, "validation_error", f"Unknown interests: {', '.join(missing)}.")


@router.post("/v1/families/{family_id}/children", response_model=ChildOut, status_code=201)
def create_child(family_id: uuid.UUID, body: ChildIn, actor: ParentActor, db: DbSession) -> Child:
    membership(db, actor, family_id, WRITE_ROLES)
    require_guardian_verified(db, family_id)
    _validate_refs(db, body.level_code, body.interests, body.board_code, body.grade)
    child = Child(
        family_id=family_id,
        display_name=body.display_name.strip(),
        birth_year=body.birth_year,
        level_code=body.level_code,
        board_code=body.board_code,
        grade=body.grade.strip() if body.grade else None,
        avatar=body.avatar,
        interests=body.interests,
        goals=body.goals,
    )
    db.add(child)
    db.flush()
    if daily.is_system_family(db, family_id):
        daily.auto_enrol(db, child)
    audit(
        db,
        "child.created",
        actor_user_id=actor.user_id,
        family_id=family_id,
        entity="child",
        entity_id=child.id,
        request_id=actor.request_id,
    )
    emit(db, "child.created", {"child_id": child.id, "family_id": family_id}, aggregate_id=child.id)
    return child


@router.get("/v1/families/{family_id}/children", response_model=list[ChildOut])
def list_children(family_id: uuid.UUID, actor: ParentActor, db: DbSession) -> list[Child]:
    membership(db, actor, family_id, ALL_ROLES)
    return list(
        db.scalars(
            select(Child).where(Child.family_id == family_id, Child.deleted_at.is_(None)).order_by(Child.created_at)
        )
    )


@router.get("/v1/children/{child_id}", response_model=ChildOut)
def get_child(child_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> Child:
    return load_child(db, actor, child_id)


@router.patch("/v1/children/{child_id}", response_model=ChildOut)
def patch_child(child_id: uuid.UUID, body: ChildPatch, actor: ParentActor, db: DbSession) -> Child:
    child = load_child(db, actor, child_id, WRITE_ROLES)
    if child.version != body.version:
        raise conflict("This profile was changed elsewhere. Reload and try again.")
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    _validate_refs(db, changes.get("level_code"), changes.get("interests"), changes.get("board_code"), changes.get("grade"))
    for k, v in changes.items():
        setattr(child, k, v.strip() if k in ("display_name", "grade") and v else v)
    if ("board_code" in changes or "grade" in changes) and daily.is_system_family(db, child.family_id):
        daily.auto_enrol(db, child)
    child.version += 1
    child.updated_at = now()
    audit(
        db,
        "child.updated",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        fields=sorted(changes),
        request_id=actor.request_id,
    )
    return child


@router.delete("/v1/children/{child_id}", status_code=204)
def delete_child(child_id: uuid.UUID, request: Request, actor: ParentActor, db: DbSession) -> Response:
    """Soft delete (the data is purged later by the retention job). Needs the parent PIN."""
    child = load_child(db, actor, child_id, WRITE_ROLES)
    require_elevation(request, actor)
    child.deleted_at = now()
    db.execute(
        update(ChildSession)
        .where(ChildSession.child_id == child.id, ChildSession.revoked_at.is_(None))
        .values(revoked_at=now())
    )
    audit(
        db,
        "child.deleted",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        request_id=actor.request_id,
    )
    emit(db, "child.deleted", {"child_id": child.id, "family_id": child.family_id}, aggregate_id=child.id)
    return Response(status_code=204)


@router.post("/v1/children/{child_id}/sessions", response_model=ChildSessionOut, status_code=201)
def open_child_session(child_id: uuid.UUID, body: ChildSessionIn, actor: ParentActor, db: DbSession) -> ChildSessionOut:
    """Child mode: the parent hands the device to the child. Returns a child-scoped token (no parent access)."""
    child = load_child(db, actor, child_id, ALL_ROLES)
    db.execute(
        update(ChildSession)
        .where(
            ChildSession.child_id == child.id,
            ChildSession.device_id == body.device_id,
            ChildSession.revoked_at.is_(None),
        )
        .values(revoked_at=now())
    )
    row = ChildSession(
        child_id=child.id,
        family_id=child.family_id,
        device_id=body.device_id,
        issued_by=actor.user_id,
        expires_at=now() + timedelta(minutes=get_settings().child_token_minutes),
    )
    db.add(row)
    db.flush()
    token, expires_at = create_child_token(child.id, child.family_id, row.id, body.device_id)
    row.expires_at = expires_at
    audit(
        db,
        "child.session_opened",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        request_id=actor.request_id,
    )
    return ChildSessionOut(child_token=token, expires_at=expires_at, child=ChildOut.model_validate(child))


@router.delete("/v1/children/{child_id}/sessions", status_code=204)
def close_child_sessions(child_id: uuid.UUID, request: Request, actor: ParentActor, db: DbSession) -> Response:
    """Leave child mode: revokes every open child session. Needs the parent PIN."""
    child = load_child(db, actor, child_id, ALL_ROLES)
    require_elevation(request, actor)
    db.execute(
        update(ChildSession)
        .where(ChildSession.child_id == child.id, ChildSession.revoked_at.is_(None))
        .values(revoked_at=now())
    )
    audit(
        db,
        "child.session_closed",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        request_id=actor.request_id,
    )
    return Response(status_code=204)
