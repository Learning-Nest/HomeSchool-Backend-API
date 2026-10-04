"""Token issuing and account helpers shared by the auth, me and family routers."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Family, FamilyMembership, GuardianVerification, RefreshToken, User
from app.schemas import MembershipOut, TokenOut, UserOut
from app.security import create_access_token, new_opaque_token, now

_TEMP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # no 0/O/1/l/I lookalikes


def new_temp_password(length: int = 12) -> str:
    """A random, easy-to-read-and-type temporary password (about 70 bits)."""
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))


def memberships_for(db: Session, user_id: uuid.UUID) -> list[MembershipOut]:
    rows = db.execute(
        select(FamilyMembership.role, Family.id, Family.name)
        .join(Family, Family.id == FamilyMembership.family_id)
        .where(FamilyMembership.user_id == user_id, Family.deleted_at.is_(None))
        .order_by(Family.created_at)
    ).all()
    if not rows:
        return []
    verified = set(
        db.scalars(
            select(GuardianVerification.family_id).where(
                GuardianVerification.family_id.in_([r.id for r in rows]),
                GuardianVerification.verified_at.is_not(None),
                GuardianVerification.declared_at.is_not(None),
            )
        )
    )
    return [
        MembershipOut(family_id=r.id, family_name=r.name, role=r.role, guardian_verified=r.id in verified) for r in rows
    ]


def issue_tokens(db: Session, user: User, chain_id: uuid.UUID | None = None) -> tuple[TokenOut, RefreshToken]:
    s = get_settings()
    token, digest = new_opaque_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=digest,
        chain_id=chain_id or uuid.uuid4(),
        expires_at=now() + timedelta(days=s.refresh_token_days),
    )
    db.add(row)
    db.flush()
    out = TokenOut(
        access_token=create_access_token(user.id),
        refresh_token=token,
        expires_in=s.access_token_minutes * 60,
        user=UserOut.model_validate(user),
        memberships=memberships_for(db, user.id),
        temp_login=user.must_change_password,
    )
    return out, row
