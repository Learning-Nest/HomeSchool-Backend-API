"""SQLAlchemy models. The authoritative DDL (including RLS policies) is in migrations/versions/0001_initial.py."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    platform_role: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    # Forgot-password: a separate, expiring temporary password. The real password keeps working until the user
    # changes it, so a stranger who merely types your email address cannot lock you out.
    reset_hash: Mapped[str | None] = mapped_column(Text)
    reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    chain_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Family(Base):
    __tablename__ = "families"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Kolkata", server_default="Asia/Kolkata")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class FamilyMembership(Base):
    __tablename__ = "family_memberships"
    __table_args__ = (UniqueConstraint("family_id", "user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(Text)  # owner | guardian | educator
    created_at: Mapped[datetime] = _now()


class GuardianVerification(Base):
    __tablename__ = "guardian_verifications"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    method: Mapped[str] = mapped_column(Text, default="otp_declaration", server_default="otp_declaration")
    phone_last4: Mapped[str | None] = mapped_column(Text)
    otp_hash: Mapped[str | None] = mapped_column(Text)
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    declaration_notice_version: Mapped[str | None] = mapped_column(Text)
    declared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Consent(Base):
    __tablename__ = "consents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(Text)
    granted: Mapped[bool] = mapped_column(Boolean)
    notice_version: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = _now()


class Child(Base):
    __tablename__ = "children"
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    display_name: Mapped[str] = mapped_column(Text)
    birth_year: Mapped[int | None] = mapped_column(Integer)
    level_code: Mapped[str | None] = mapped_column(Text)
    avatar: Mapped[str] = mapped_column(Text, default="star", server_default="star")
    interests: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    goals: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ParentPin(Base):
    __tablename__ = "parent_pins"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    pin_hash: Mapped[str] = mapped_column(Text)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


class ChildSession(Base):
    __tablename__ = "child_sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    child_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    device_id: Mapped[str] = mapped_column(Text)
    issued_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ----------------------------------------------------------------------------- content schema
class Level(Base):
    __tablename__ = "levels"
    __table_args__ = {"schema": "content"}
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    indicative_age: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = {"schema": "content"}
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer)
    scope: Mapped[str | None] = mapped_column(Text)


class Interest(Base):
    __tablename__ = "interests"
    __table_args__ = {"schema": "content"}
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = {"schema": "content"}
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_code: Mapped[str] = mapped_column(ForeignKey("content.subjects.code"))
    level_code: Mapped[str] = mapped_column(ForeignKey("content.levels.code"))
    name: Mapped[str] = mapped_column(Text)
    typical_evidence: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")


class SkillPrerequisite(Base):
    __tablename__ = "skill_prerequisites"
    __table_args__ = {"schema": "content"}
    skill_code: Mapped[str] = mapped_column(ForeignKey("content.skills.code", ondelete="CASCADE"), primary_key=True)
    prerequisite_code: Mapped[str] = mapped_column(
        ForeignKey("content.skills.code", ondelete="CASCADE"), primary_key=True
    )


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = {"schema": "content"}
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    subject_code: Mapped[str] = mapped_column(ForeignKey("content.subjects.code"))
    level_from: Mapped[str] = mapped_column(ForeignKey("content.levels.code"))
    level_to: Mapped[str] = mapped_column(ForeignKey("content.levels.code"))
    duration_min: Mapped[int] = mapped_column(Integer)
    materials: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    interest_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    status: Mapped[str] = mapped_column(Text, default="draft", server_default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    definition: Mapped[dict] = mapped_column(JSONB)
    bundle_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivityVersion(Base):
    """Immutable snapshot of a published definition. Sessions point at (activity_id, version), never at the live row."""

    __tablename__ = "activity_versions"
    __table_args__ = {"schema": "content"}
    activity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content.activities.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class ActivitySkill(Base):
    __tablename__ = "activity_skills"
    __table_args__ = {"schema": "content"}
    activity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content.activities.id", ondelete="CASCADE"), primary_key=True
    )
    skill_code: Mapped[str] = mapped_column(ForeignKey("content.skills.code"), primary_key=True)


# ----------------------------------------------------------------------------- planning, sessions, progress
class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("child_id", "week_start"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    child_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"))
    week_start: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class PlanItem(Base):
    __tablename__ = "plan_items"
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    child_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"))
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    activity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content.activities.id"))
    scheduled_date: Mapped[date] = mapped_column(Date)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(Text, default="planned", server_default="planned")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ActivitySession(Base):
    __tablename__ = "activity_sessions"
    __table_args__ = (UniqueConstraint("family_id", "client_op_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    child_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"))
    activity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content.activities.id"))
    activity_version: Mapped[int] = mapped_column(Integer)
    plan_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plan_items.id", ondelete="SET NULL"))
    client_op_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="in_progress", server_default="in_progress")
    started_at: Mapped[datetime] = _now()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    hints_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parent_assist: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    answers: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    result: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class SkillEvidence(Base):
    __tablename__ = "skill_evidence"
    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    child_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"))
    skill_code: Mapped[str] = mapped_column(ForeignKey("content.skills.code"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("activity_sessions.id", ondelete="SET NULL"))
    step_id: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)  # activity | observation | assessment | baseline
    value: Mapped[float] = mapped_column(Float)
    base_weight: Mapped[float] = mapped_column(Float)
    independence: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class SkillMastery(Base):
    __tablename__ = "skill_mastery"
    child_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"), primary_key=True)
    skill_code: Mapped[str] = mapped_column(ForeignKey("content.skills.code"), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text)
    evidence_count: Mapped[int] = mapped_column(Integer)
    distinct_days: Mapped[int] = mapped_column(Integer)
    last_evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_developing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_child_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    family_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


# ---------------------------------------------------------------------------------------------------------------
# Scrum board (internal team tool, unrelated to family/child data) -----------------------------------------------
# Not covered by Row-Level Security: these tables hold no family-scoped data, just an internal task board for the
# people building the app. Access is controlled entirely in app/routers/scrum.py via a per-person access code.
class ScrumMember(Base):
    __tablename__ = "scrum_members"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    access_code_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = _now()


class ScrumTask(Base):
    __tablename__ = "scrum_tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, default="dev", server_default="dev")
    status: Mapped[str] = mapped_column(Text, default="backlog", server_default="backlog")
    assignee_name: Mapped[str | None] = mapped_column(Text)
    sprint: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scrum_members.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
