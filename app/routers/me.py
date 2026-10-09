"""Current user and the parent PIN (elevation gate for sensitive actions)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Response
from sqlalchemy import select

from app import ratelimit
from app.config import get_settings
from app.deps import DbSession, ParentActor
from app.errors import ApiError
from app.models import ParentPin, User
from app.schemas import ElevationOut, MeOut, PinResetIn, PinSetIn, PinVerifyIn, UserOut
from app.security import create_elevation_token, hash_secret, now, verify_secret
from app.services.auth import memberships_for
from app.services.events import audit

router = APIRouter(prefix="/v1/me", tags=["me"])


def lock_seconds(failed: int) -> int:
    """Lockout tiers (22_CHILD_ACCESS_RBAC): 5 failures -> 15 min, 10 -> 1 h, 15 -> 24 h. Applied on every failure from the 5th."""
    if failed >= 15:
        return 24 * 3600
    if failed >= 10:
        return 3600
    if failed >= 5:
        return 15 * 60
    return 0


def check_pin(db, user_id, pin: str) -> None:
    """Verifies the PIN with lockout. Failed attempts are committed before the error is raised."""
    row = db.get(ParentPin, user_id)
    if row is None:
        raise ApiError(409, "pin_not_set")
    if row.locked_until and row.locked_until > now():
        raise ApiError(
            429, "pin_locked", extra={"retry_after_seconds": int((row.locked_until - now()).total_seconds()) + 1}
        )
    if verify_secret(row.pin_hash, pin):
        row.failed_attempts = 0
        row.locked_until = None
        return
    row.failed_attempts += 1
    secs = lock_seconds(row.failed_attempts)
    row.locked_until = now() + timedelta(seconds=secs) if secs else None
    audit(db, "pin.failed", actor_user_id=user_id, entity="parent_pin", entity_id=user_id, attempts=row.failed_attempts)
    remaining = max(0, 5 - row.failed_attempts) if not secs else 0
    db.commit()
    if secs:
        raise ApiError(429, "pin_locked", extra={"retry_after_seconds": secs})
    raise ApiError(401, "pin_invalid", extra={"attempts_remaining": remaining})


@router.get("", response_model=MeOut)
def me(actor: ParentActor, db: DbSession) -> MeOut:
    user = db.get(User, actor.user_id)
    return MeOut(
        user=UserOut.model_validate(user),
        memberships=memberships_for(db, user.id),
        pin_set=db.get(ParentPin, user.id) is not None,
        temp_login=user.must_change_password,
    )


@router.put("/pin", status_code=204)
def set_pin(body: PinSetIn, actor: ParentActor, db: DbSession) -> Response:
    existing = db.get(ParentPin, actor.user_id)
    if existing is not None:
        if not body.current_pin:
            raise ApiError(409, "pin_already_set", "A parent PIN is already set. Send current_pin to change it.")
        check_pin(db, actor.user_id, body.current_pin)
        existing.pin_hash = hash_secret(body.pin)
        existing.updated_at = now()
    else:
        db.add(ParentPin(user_id=actor.user_id, pin_hash=hash_secret(body.pin)))
    audit(
        db,
        "pin.set",
        actor_user_id=actor.user_id,
        entity="parent_pin",
        entity_id=actor.user_id,
        request_id=actor.request_id,
    )
    return Response(status_code=204)


@router.post("/pin/verify", response_model=ElevationOut)
def verify_pin(body: PinVerifyIn, actor: ParentActor, db: DbSession) -> ElevationOut:
    check_pin(db, actor.user_id, body.pin)
    audit(
        db,
        "pin.verified",
        actor_user_id=actor.user_id,
        entity="parent_pin",
        entity_id=actor.user_id,
        request_id=actor.request_id,
    )
    return ElevationOut(
        elevation_token=create_elevation_token(actor.user_id), expires_in=get_settings().elevation_minutes * 60
    )


@router.post("/pin/reset", status_code=204)
def reset_pin(body: PinResetIn, actor: ParentActor, db: DbSession) -> Response:
    """Forgot the PIN: re-authenticate with the account password and set a new one (clears any lockout)."""
    ratelimit.check(f"pinreset:{actor.user_id}", 5, 900)
    user = db.scalar(select(User).where(User.id == actor.user_id))
    if not verify_secret(user.password_hash, body.password):
        raise ApiError(401, "invalid_credentials")
    row = db.get(ParentPin, actor.user_id)
    if row is None:
        db.add(ParentPin(user_id=actor.user_id, pin_hash=hash_secret(body.pin)))
    else:
        row.pin_hash, row.failed_attempts, row.locked_until, row.updated_at = hash_secret(body.pin), 0, None, now()
    audit(
        db,
        "pin.reset",
        actor_user_id=actor.user_id,
        entity="parent_pin",
        entity_id=actor.user_id,
        request_id=actor.request_id,
    )
    return Response(status_code=204)
