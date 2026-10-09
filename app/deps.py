"""Request dependencies: authentication, family membership, child access, PIN elevation, guardian verification."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, set_rls_identity
from app.errors import ApiError, forbidden, not_found, unauthenticated
from app.models import Child, ChildSession, FamilyMembership, GuardianVerification, User
from app.security import decode_token, now

# The DB dependency's exit code (commit) runs right after the endpoint returns and BEFORE the response is sent,
# so a failed commit becomes a proper error response and the client never sees a 2xx for data that was not saved.
DbSession = Annotated[Session, Depends(get_db, scope="function")]
_bearer = HTTPBearer(auto_error=False)

# After signing in with an emailed temporary password the account can do exactly two things: read /v1/me (so the app
# can tell what is going on) and set a new password. Everything else answers 403 password_change_required.
_PASSWORD_CHANGE_EXEMPT = {("GET", "/v1/me"), ("POST", "/v1/auth/change-password")}

WRITE_ROLES = {"owner", "guardian"}
ALL_ROLES = {"owner", "guardian", "educator"}


@dataclass
class Actor:
    kind: str  # "parent" | "child"
    user_id: uuid.UUID | None = None
    child_id: uuid.UUID | None = None
    family_id: uuid.UUID | None = None  # set for child actors
    platform_role: str | None = None
    session_id: uuid.UUID | None = None
    request_id: str | None = None

    @property
    def is_parent(self) -> bool:
        return self.kind == "parent"

    @property
    def is_content_admin(self) -> bool:
        return self.platform_role in ("content_admin", "super_admin")


def get_actor(
    request: Request, db: DbSession, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Actor:
    if creds is None:
        raise unauthenticated()
    try:
        claims = decode_token(creds.credentials)
        sub = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise unauthenticated() from None
    request_id = getattr(request.state, "request_id", None)
    typ = claims.get("typ")
    if typ == "access":
        user = db.get(User, sub)
        if user is None or user.status != "active":
            raise unauthenticated()
        set_rls_identity(db, user_id=user.id)
        if user.must_change_password and (request.method, request.url.path) not in _PASSWORD_CHANGE_EXEMPT:
            raise ApiError(403, "password_change_required")
        return Actor(kind="parent", user_id=user.id, platform_role=user.platform_role, request_id=request_id)
    if typ == "child":
        try:
            family_id, session_id = uuid.UUID(claims["fam"]), uuid.UUID(claims["sid"])
        except (KeyError, ValueError):
            raise unauthenticated() from None
        set_rls_identity(db, family_id=family_id)
        cs = db.get(ChildSession, session_id)
        if (
            cs is None
            or cs.child_id != sub
            or cs.family_id != family_id
            or cs.revoked_at is not None
            or cs.expires_at <= now()
        ):
            raise unauthenticated()
        child = db.get(Child, sub)
        if child is None or child.deleted_at is not None:
            raise unauthenticated()
        return Actor(kind="child", child_id=sub, family_id=family_id, session_id=session_id, request_id=request_id)
    raise unauthenticated()


CurrentActor = Annotated[Actor, Depends(get_actor)]


def require_parent(actor: CurrentActor) -> Actor:
    if not actor.is_parent:
        raise forbidden("This action is for a parent account.")
    return actor


ParentActor = Annotated[Actor, Depends(require_parent)]


def require_platform_admin(actor: ParentActor) -> Actor:
    if actor.platform_role not in ("content_admin", "super_admin"):
        raise forbidden("Platform admin access is required.")
    return actor


AdminActor = Annotated[Actor, Depends(require_platform_admin)]

# Platform roles. `educator` writes drafts and submits them; the two admin roles also review and publish.
# (Not to be confused with the FAMILY role "educator" in family_memberships.)
ADMIN_ROLES = ("content_admin", "super_admin")
STAFF_ROLES = ("educator", *ADMIN_ROLES)


def require_content_staff(actor: ParentActor) -> Actor:
    if actor.platform_role not in STAFF_ROLES:
        raise forbidden("Content staff access is required.")
    return actor


StaffActor = Annotated[Actor, Depends(require_content_staff)]


def require_super_admin(actor: ParentActor) -> Actor:
    if actor.platform_role != "super_admin":
        raise forbidden("Super admin access is required.")
    return actor


SuperAdminActor = Annotated[Actor, Depends(require_super_admin)]


def membership(db: Session, actor: Actor, family_id: uuid.UUID, roles: set[str] = ALL_ROLES) -> FamilyMembership:
    """The parent's membership in the family, or 404 (never reveal that a family exists) / 403 when the role is too low."""
    row = db.scalar(
        select(FamilyMembership).where(
            FamilyMembership.family_id == family_id, FamilyMembership.user_id == actor.user_id
        )
    )
    if row is None:
        raise not_found()
    if row.role not in roles:
        raise forbidden("Your role in this family does not allow this.")
    return row


def load_child(
    db: Session, actor: Actor, child_id: uuid.UUID, roles: set[str] = ALL_ROLES, parent_only: bool = False
) -> Child:
    """Loads a child the actor may access. Parents need a membership with one of `roles`; a child token only reaches itself."""
    if actor.is_parent:
        child = db.scalar(select(Child).where(Child.id == child_id, Child.deleted_at.is_(None)))
        if child is None:
            raise not_found()
        membership(db, actor, child.family_id, roles)
        return child
    if parent_only:
        raise forbidden("This action is for a parent account.")
    if child_id != actor.child_id:
        raise not_found()
    child = db.scalar(select(Child).where(Child.id == child_id, Child.deleted_at.is_(None)))
    if child is None:
        raise not_found()
    return child


def require_elevation(request: Request, actor: Actor) -> None:
    """Sensitive actions need a fresh parent-PIN elevation token in X-Elevation-Token (5 minutes)."""
    token = request.headers.get("X-Elevation-Token")
    if not token:
        raise ApiError(403, "elevation_required")
    try:
        claims = decode_token(token, expected_type="elev")
    except jwt.PyJWTError:
        raise ApiError(403, "elevation_required") from None
    if claims.get("sub") != str(actor.user_id):
        raise ApiError(403, "elevation_required")


def require_guardian_verified(db: Session, family_id: uuid.UUID) -> None:
    ok = db.scalar(
        select(GuardianVerification.id)
        .where(
            GuardianVerification.family_id == family_id,
            GuardianVerification.verified_at.is_not(None),
            GuardianVerification.declared_at.is_not(None),
        )
        .limit(1)
    )
    if ok is None:
        raise ApiError(403, "guardian_verification_required")
