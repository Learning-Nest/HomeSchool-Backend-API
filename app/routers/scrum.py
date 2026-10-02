"""Internal Scrum/Kanban board for the people building the app (not an app-facing feature).

Auth is a single per-person opaque access code in the `X-Scrum-Code` header (see security.new_opaque_token /
token_digest, the same pattern used for refresh tokens: a long random value, only its sha256 digest stored, so
the raw code is never recoverable after creation). There is no password, no JWT and no family/child scoping here
-- `POST /v1/scrum/bootstrap` creates the first admin once (while the table is empty); every member after that is
added by an existing admin via `POST /v1/scrum/members`.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select

from app.deps import DbSession
from app.errors import ApiError, forbidden, not_found, unauthenticated
from app.models import ScrumMember, ScrumTask
from app.ratelimit import check as rate_check
from app.schemas import (
    ScrumBootstrapIn,
    ScrumMemberCreatedOut,
    ScrumMemberIn,
    ScrumMemberOut,
    ScrumTaskIn,
    ScrumTaskOut,
    ScrumTaskPatch,
    ScrumWhoamiOut,
)
from app.security import now, token_digest

router = APIRouter(prefix="/v1/scrum", tags=["scrum"])

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L -- easier to read back over chat/WhatsApp


def _generate_access_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def get_scrum_member(
    request: Request, db: DbSession, x_scrum_code: Annotated[str | None, Header()] = None
) -> ScrumMember:
    client_host = request.client.host if request.client else "unknown"
    rate_check(f"scrum_auth:{client_host}", limit=30, window_seconds=60)
    if not x_scrum_code:
        raise unauthenticated("Missing X-Scrum-Code header.")
    member = db.scalar(select(ScrumMember).where(ScrumMember.access_code_digest == token_digest(x_scrum_code)))
    if member is None:
        raise unauthenticated("Invalid access code.")
    return member


CurrentMember = Annotated[ScrumMember, Depends(get_scrum_member)]


def require_admin(member: CurrentMember) -> ScrumMember:
    if not member.is_admin:
        raise forbidden("Only a board admin can do this.")
    return member


AdminMember = Annotated[ScrumMember, Depends(require_admin)]


# ---------------------------------------------------------------------------- members / access
@router.post("/bootstrap", response_model=ScrumMemberCreatedOut, status_code=201)
def bootstrap_admin(body: ScrumBootstrapIn, request: Request, db: DbSession) -> ScrumMemberCreatedOut:
    """Creates the first admin, once. Refuses once any member exists -- re-run POST /members instead."""
    client_host = request.client.host if request.client else "unknown"
    rate_check(f"scrum_bootstrap:{client_host}", limit=5, window_seconds=3600)
    if db.scalar(select(ScrumMember.id).limit(1)) is not None:
        raise ApiError(409, "conflict", "The board already has members -- ask an existing admin to add you.")
    code = _generate_access_code()
    row = ScrumMember(name=body.name.strip(), access_code_digest=token_digest(code), is_admin=True)
    db.add(row)
    db.flush()
    return ScrumMemberCreatedOut(member=ScrumMemberOut.model_validate(row), access_code=code)


@router.post("/members", response_model=ScrumMemberCreatedOut, status_code=201)
def add_member(body: ScrumMemberIn, admin: AdminMember, db: DbSession) -> ScrumMemberCreatedOut:
    code = _generate_access_code()
    row = ScrumMember(name=body.name.strip(), access_code_digest=token_digest(code), is_admin=body.is_admin)
    db.add(row)
    db.flush()
    return ScrumMemberCreatedOut(member=ScrumMemberOut.model_validate(row), access_code=code)


@router.get("/members", response_model=list[ScrumMemberOut])
def list_members(admin: AdminMember, db: DbSession) -> list[ScrumMember]:
    return list(db.scalars(select(ScrumMember).order_by(ScrumMember.created_at)))


@router.delete("/members/{member_id}", status_code=204)
def remove_member(member_id: uuid.UUID, admin: AdminMember, db: DbSession) -> Response:
    row = db.get(ScrumMember, member_id)
    if row is None:
        raise not_found()
    if row.id == admin.id:
        raise ApiError(409, "conflict", "You can't remove your own access.")
    db.delete(row)
    return Response(status_code=204)


@router.get("/whoami", response_model=ScrumWhoamiOut)
def whoami(member: CurrentMember) -> ScrumMember:
    return member


# ---------------------------------------------------------------------------- tasks
@router.get("/tasks", response_model=list[ScrumTaskOut])
def list_tasks(member: CurrentMember, db: DbSession) -> list[ScrumTask]:
    return list(db.scalars(select(ScrumTask).order_by(ScrumTask.status, ScrumTask.position, ScrumTask.created_at)))


@router.post("/tasks", response_model=ScrumTaskOut, status_code=201)
def create_task(body: ScrumTaskIn, member: CurrentMember, db: DbSession) -> ScrumTask:
    top = db.scalar(
        select(ScrumTask.position)
        .where(ScrumTask.status == body.status)
        .order_by(ScrumTask.position.desc())
        .limit(1)
    )
    row = ScrumTask(
        title=body.title.strip(),
        description=body.description,
        category=body.category,
        status=body.status,
        assignee_name=(body.assignee_name or "").strip() or None,
        sprint=(body.sprint or "").strip() or None,
        position=(top + 1) if top is not None else 0,
        created_by_member_id=member.id,
    )
    db.add(row)
    db.flush()
    return row


@router.patch("/tasks/{task_id}", response_model=ScrumTaskOut)
def patch_task(task_id: uuid.UUID, body: ScrumTaskPatch, member: CurrentMember, db: DbSession) -> ScrumTask:
    row = db.get(ScrumTask, task_id)
    if row is None:
        raise not_found()
    changes = body.model_dump(exclude_unset=True)
    moving_column = "status" in changes and changes["status"] != row.status
    for k, v in changes.items():
        if k in ("title", "assignee_name", "sprint") and isinstance(v, str):
            v = v.strip() or None
        setattr(row, k, v)
    if moving_column and "position" not in changes:
        top = db.scalar(
            select(ScrumTask.position)
            .where(ScrumTask.status == row.status, ScrumTask.id != row.id)
            .order_by(ScrumTask.position.desc())
            .limit(1)
        )
        row.position = (top + 1) if top is not None else 0
    row.updated_at = now()
    return row


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: uuid.UUID, member: CurrentMember, db: DbSession) -> Response:
    row = db.get(ScrumTask, task_id)
    if row is None:
        raise not_found()
    db.delete(row)
    return Response(status_code=204)
