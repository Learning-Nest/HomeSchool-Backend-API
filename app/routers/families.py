"""Families, guardian verification (OTP + declaration) and consent records."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter
from sqlalchemy import select

from app import ratelimit
from app.config import get_settings
from app.deps import WRITE_ROLES, DbSession, ParentActor, membership
from app.errors import ApiError
from app.models import Consent, Family, FamilyMembership, GuardianVerification, User
from app.schemas import (
    ConsentIn,
    ConsentOut,
    ConsentPurposeList,
    FamilyOut,
    GuardianConfirmIn,
    GuardianStartIn,
    GuardianStartOut,
)
from app.security import now
from app.services import email as email_service
from app.services import otp
from app.services.events import audit, emit

router = APIRouter(prefix="/v1/families", tags=["families"])


@router.get("", response_model=list[FamilyOut])
def list_families(actor: ParentActor, db: DbSession) -> list[Family]:
    q = (
        select(Family)
        .join(FamilyMembership, FamilyMembership.family_id == Family.id)
        .where(FamilyMembership.user_id == actor.user_id, Family.deleted_at.is_(None))
        .order_by(Family.created_at)
    )
    return list(db.scalars(q))


@router.post("/{family_id}/guardian-verification", response_model=GuardianStartOut, status_code=201)
def start_verification(
    family_id: uuid.UUID, body: GuardianStartIn, actor: ParentActor, db: DbSession
) -> GuardianStartOut:
    membership(db, actor, family_id, WRITE_ROLES)
    ratelimit.check(f"otp:{actor.user_id}", 5, 3600)
    s = get_settings()
    code = otp.new_code()
    by_email = body.channel == "email"
    # The email channel always goes to the signed-in account's own address - never to one supplied in the request.
    account_email = db.get(User, actor.user_id).email if by_email else None
    v = GuardianVerification(
        user_id=actor.user_id,
        family_id=family_id,
        method="email_declaration" if by_email else "otp_declaration",
        phone_last4=None if by_email else body.phone[-4:],
        otp_expires_at=now() + timedelta(minutes=s.otp_ttl_minutes),
    )
    db.add(v)
    db.flush()
    v.otp_hash = otp.digest(v.id, code)
    if by_email:
        otp.deliver_email(account_email, code)
    else:
        otp.deliver(body.phone, code)
    audit(
        db,
        "guardian.otp_sent",
        actor_user_id=actor.user_id,
        family_id=family_id,
        entity="guardian_verification",
        entity_id=v.id,
        request_id=actor.request_id,
        channel=body.channel,
    )
    return GuardianStartOut(
        verification_id=v.id,
        expires_in=s.otp_ttl_minutes * 60,
        dev_code=code if s.expose_dev_otp else None,
        channel=body.channel,
        destination_hint=email_service.mask_email(account_email) if by_email else "******" + body.phone[-4:],
    )


@router.post("/{family_id}/guardian-verification/confirm", status_code=200)
def confirm_verification(family_id: uuid.UUID, body: GuardianConfirmIn, actor: ParentActor, db: DbSession) -> dict:
    membership(db, actor, family_id, WRITE_ROLES)
    s = get_settings()
    v = db.scalar(
        select(GuardianVerification).where(
            GuardianVerification.id == body.verification_id,
            GuardianVerification.family_id == family_id,
            GuardianVerification.user_id == actor.user_id,
        )
    )
    if v is None:
        raise ApiError(422, "otp_invalid")
    if v.verified_at is not None:
        return {"verified": True}
    if not body.declaration_accepted:
        raise ApiError(422, "validation_error", "The guardian declaration must be accepted.")
    if body.notice_version != s.declaration_notice_version:
        raise ApiError(422, "validation_error", "The privacy notice version is out of date. Reload and try again.")
    if v.attempts >= s.otp_max_attempts or v.otp_expires_at is None or v.otp_expires_at <= now():
        raise ApiError(422, "otp_invalid")
    if not otp.matches(v.id, body.code, v.otp_hash):
        v.attempts += 1
        audit(
            db,
            "guardian.otp_failed",
            actor_user_id=actor.user_id,
            family_id=family_id,
            entity="guardian_verification",
            entity_id=v.id,
        )
        db.commit()
        raise ApiError(422, "otp_invalid")
    v.verified_at = v.declared_at = now()
    v.declaration_notice_version = body.notice_version
    v.otp_hash = None
    db.add(
        Consent(
            family_id=family_id,
            user_id=actor.user_id,
            purpose="core_service",
            granted=True,
            notice_version=body.notice_version,
        )
    )
    audit(
        db,
        "guardian.verified",
        actor_user_id=actor.user_id,
        family_id=family_id,
        entity="guardian_verification",
        entity_id=v.id,
        notice_version=body.notice_version,
        request_id=actor.request_id,
    )
    emit(db, "guardian.verified", {"family_id": family_id, "user_id": actor.user_id}, aggregate_id=family_id)
    return {"verified": True}


def _latest_consents(db, family_id: uuid.UUID) -> list[Consent]:
    rows = db.scalars(
        select(Consent).where(Consent.family_id == family_id).order_by(Consent.recorded_at.desc(), Consent.id)
    )
    latest: dict[str, Consent] = {}
    for r in rows:
        latest.setdefault(r.purpose, r)
    return sorted(latest.values(), key=lambda c: c.purpose)


@router.get("/{family_id}/consents", response_model=ConsentPurposeList)
def get_consents(family_id: uuid.UUID, actor: ParentActor, db: DbSession) -> ConsentPurposeList:
    membership(db, actor, family_id)
    return ConsentPurposeList(consents=[ConsentOut.model_validate(c) for c in _latest_consents(db, family_id)])


@router.put("/{family_id}/consents", response_model=ConsentPurposeList)
def put_consent(family_id: uuid.UUID, body: ConsentIn, actor: ParentActor, db: DbSession) -> ConsentPurposeList:
    """Consent is append-only: every change is a new row, and the latest row per purpose is the current state."""
    membership(db, actor, family_id, WRITE_ROLES)
    if body.purpose == "core_service" and not body.granted:
        raise ApiError(
            422, "validation_error", "The core service consent can only be withdrawn by deleting the account."
        )
    db.add(
        Consent(
            family_id=family_id,
            user_id=actor.user_id,
            purpose=body.purpose,
            granted=body.granted,
            notice_version=body.notice_version,
        )
    )
    audit(
        db,
        "consent.recorded",
        actor_user_id=actor.user_id,
        family_id=family_id,
        entity="consent",
        purpose=body.purpose,
        granted=body.granted,
        request_id=actor.request_id,
    )
    db.flush()
    return ConsentPurposeList(consents=[ConsentOut.model_validate(c) for c in _latest_consents(db, family_id)])
