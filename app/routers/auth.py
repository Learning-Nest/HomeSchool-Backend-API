"""Sign-up, login, refresh-token rotation and logout."""

from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy import func, select

from app import ratelimit
from app.config import get_settings
from app.deps import DbSession, ParentActor
from app.errors import ApiError, unauthenticated
from app.models import Family, FamilyMembership, RefreshToken, User
from app.schemas import (
    ChangePasswordIn,
    ForgotPasswordIn,
    ForgotPasswordOut,
    LoginIn,
    RefreshIn,
    SignupIn,
    TokenOut,
)
from app.security import hash_secret, now, token_digest, verify_secret
from app.services import email as email_service
from app.services.auth import issue_tokens, new_temp_password
from app.services.events import audit, emit

log = logging.getLogger("app.auth")
router = APIRouter(prefix="/v1/auth", tags=["auth"])
_DUMMY_HASH: str | None = None


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


@router.post("/signup", response_model=TokenOut, status_code=201)
def signup(body: SignupIn, request: Request, db: DbSession) -> TokenOut:
    ratelimit.check(f"signup:{_client_ip(request)}", 10, 3600)
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ApiError(422, "validation_error", "Unknown timezone.") from None
    email = body.email.strip()
    if db.scalar(select(User.id).where(func.lower(User.email) == email.lower())):
        raise ApiError(409, "email_taken")
    user = User(email=email, password_hash=hash_secret(body.password), full_name=body.full_name.strip())
    db.add(user)
    db.flush()
    family = Family(
        name=(body.family_name or f"{body.full_name.strip()}'s family")[:120],
        timezone=body.timezone,
        created_by=user.id,
    )
    db.add(family)
    db.flush()
    db.add(FamilyMembership(family_id=family.id, user_id=user.id, role="owner"))
    db.flush()
    audit(
        db,
        "user.signup",
        actor_user_id=user.id,
        family_id=family.id,
        entity="user",
        entity_id=user.id,
        request_id=request.state.request_id,
    )
    emit(db, "user.signed_up", {"user_id": user.id, "family_id": family.id}, aggregate_id=user.id)
    out, _ = issue_tokens(db, user)
    return out


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: DbSession) -> TokenOut:
    global _DUMMY_HASH
    email = body.email.strip().lower()
    ratelimit.check(f"login:{_client_ip(request)}:{email}", 10, 300)
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        # Spend the same time as a real check so response timing does not reveal which emails exist.
        _DUMMY_HASH = _DUMMY_HASH or hash_secret("not-a-real-password")
        verify_secret(_DUMMY_HASH, body.password)
        raise ApiError(401, "invalid_credentials")
    password_ok = verify_secret(user.password_hash, body.password)
    temp_ok = not password_ok and _temp_password_matches(user, body.password)
    if not (password_ok or temp_ok) or user.status != "active":
        raise ApiError(401, "invalid_credentials")
    if temp_ok:
        # Signed in with the emailed temporary password: the server now refuses everything except changing the
        # password (see deps.get_actor). The temporary password stays valid until it expires or is replaced.
        user.must_change_password = True
        audit(
            db,
            "user.login_temp_password",
            actor_user_id=user.id,
            entity="user",
            entity_id=user.id,
            request_id=request.state.request_id,
        )
    else:
        user.reset_hash = user.reset_expires_at = None  # they remembered the password: drop any pending reset
        audit(
            db,
            "user.login",
            actor_user_id=user.id,
            entity="user",
            entity_id=user.id,
            request_id=request.state.request_id,
        )
    out, _ = issue_tokens(db, user)
    return out


def _temp_password_matches(user: User, candidate: str) -> bool:
    if not user.reset_hash or user.reset_expires_at is None or user.reset_expires_at <= now():
        return False
    return verify_secret(user.reset_hash, candidate)


def _email_temp_password(to: str, full_name: str, temp_password: str, valid_minutes: int) -> None:
    """Runs after the response is sent (so response time never reveals whether an address has an account)."""
    try:
        email_service.send_temp_password(to, full_name, temp_password, valid_minutes)
    except Exception:  # noqa: BLE001 - nothing useful to do in a background task except log (no secrets)
        log.error("forgot-password email could not be sent")


@router.post("/forgot-password", response_model=ForgotPasswordOut, status_code=202)
def forgot_password(
    body: ForgotPasswordIn, request: Request, background: BackgroundTasks, db: DbSession
) -> ForgotPasswordOut:
    """Emails a temporary password. Always answers the same 202, whether or not the address has an account."""
    email = body.email.strip().lower()
    ratelimit.check(f"forgot:{_client_ip(request)}", 10, 3600)
    try:
        ratelimit.check(f"forgot-email:{email}", 3, 3600)
    except ApiError:
        return ForgotPasswordOut()  # stay silent: a 429 here would also reveal that the account exists
    s = get_settings()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or user.status != "active":
        # Spend the time a real request spends hashing, so timing does not reveal which emails exist.
        hash_secret("not-a-real-password")
        return ForgotPasswordOut()
    temp = new_temp_password()
    user.reset_hash = hash_secret(temp)
    user.reset_expires_at = now() + timedelta(minutes=s.temp_password_minutes)
    audit(
        db,
        "auth.password_reset_requested",
        actor_user_id=user.id,
        entity="user",
        entity_id=user.id,
        request_id=request.state.request_id,
    )
    background.add_task(_email_temp_password, user.email, user.full_name, temp, s.temp_password_minutes)
    return ForgotPasswordOut()


@router.post("/change-password", response_model=TokenOut)
def change_password(body: ChangePasswordIn, actor: ParentActor, db: DbSession) -> TokenOut:
    """Sets a new password. `current_password` is the old password or the emailed temporary one.
    Every existing session is signed out and a fresh token pair is returned for this device."""
    ratelimit.check(f"chpw:{actor.user_id}", 10, 900)
    user = db.get(User, actor.user_id)
    known = verify_secret(user.password_hash, body.current_password) or _temp_password_matches(
        user, body.current_password
    )
    if not known:
        raise ApiError(401, "invalid_credentials")
    if body.new_password == body.current_password:
        raise ApiError(422, "validation_error", "Choose a password different from the current one.")
    user.password_hash = hash_secret(body.new_password)
    user.reset_hash = user.reset_expires_at = None
    user.must_change_password = False
    user.updated_at = now()
    for r in db.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))):
        r.revoked_at = now()
    audit(
        db,
        "auth.password_changed",
        actor_user_id=user.id,
        entity="user",
        entity_id=user.id,
        request_id=actor.request_id,
    )
    out, _ = issue_tokens(db, user)
    return out


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: DbSession) -> TokenOut:
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_digest(body.refresh_token)))
    if row is None:
        raise unauthenticated()
    if row.revoked_at is not None:
        # A rotated token was presented again: assume theft and revoke the whole chain. Commit before raising.
        for r in db.scalars(
            select(RefreshToken).where(RefreshToken.chain_id == row.chain_id, RefreshToken.revoked_at.is_(None))
        ):
            r.revoked_at = now()
        audit(
            db, "auth.refresh_reuse_detected", actor_user_id=row.user_id, entity="refresh_chain", entity_id=row.chain_id
        )
        db.commit()
        raise unauthenticated()
    if row.expires_at <= now():
        raise unauthenticated()
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise unauthenticated()
    out, new_row = issue_tokens(db, user, chain_id=row.chain_id)
    row.revoked_at = now()
    row.replaced_by = new_row.id
    return out


@router.post("/logout", status_code=204)
def logout(body: RefreshIn, db: DbSession) -> Response:
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_digest(body.refresh_token)))
    if row is not None:
        for r in db.scalars(
            select(RefreshToken).where(RefreshToken.chain_id == row.chain_id, RefreshToken.revoked_at.is_(None))
        ):
            r.revoked_at = now()
    return Response(status_code=204)
