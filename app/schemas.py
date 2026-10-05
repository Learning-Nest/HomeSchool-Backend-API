"""Pydantic request/response models (the API contract is generated from these: see scripts/export_contracts.py)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

Role = Literal["owner", "guardian", "educator"]
Rating = Literal["trying", "with_help", "independent"]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------- auth / me
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)
    family_name: str | None = Field(default=None, max_length=120)
    timezone: str = "Asia/Kolkata"


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ForgotPasswordOut(BaseModel):
    status: Literal["ok"] = "ok"


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)  # the old password, or the emailed temporary one
    new_password: str = Field(min_length=10, max_length=128)


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(Out):
    id: uuid.UUID
    email: str
    full_name: str
    platform_role: str | None = None


class MembershipOut(BaseModel):
    family_id: uuid.UUID
    family_name: str
    role: Role
    guardian_verified: bool


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut
    memberships: list[MembershipOut]
    temp_login: bool = False  # signed in with an emailed temporary password: only a password change is allowed


class MeOut(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]
    pin_set: bool
    temp_login: bool = False


class PinSetIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4,6}$")
    current_pin: str | None = Field(default=None, pattern=r"^\d{4,6}$")


class PinVerifyIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4,6}$")


class PinResetIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    pin: str = Field(pattern=r"^\d{4,6}$")


class ElevationOut(BaseModel):
    elevation_token: str
    expires_in: int


# ---------------------------------------------------------------------------- families / consent
class FamilyOut(Out):
    id: uuid.UUID
    name: str
    timezone: str
    version: int


class GuardianStartIn(BaseModel):
    channel: Literal["sms", "email"] = "sms"  # email = send the code to the signed-in account's own email address
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{8,15}$")

    @model_validator(mode="after")
    def _phone_required_for_sms(self) -> GuardianStartIn:
        if self.channel == "sms" and not self.phone:
            raise ValueError("phone is required when channel is sms")
        return self


class GuardianStartOut(BaseModel):
    verification_id: uuid.UUID
    expires_in: int
    dev_code: str | None = None
    channel: Literal["sms", "email"] = "sms"
    destination_hint: str | None = None  # masked, e.g. "p***@gmail.com" or "******5678"


class GuardianConfirmIn(BaseModel):
    verification_id: uuid.UUID
    code: str = Field(pattern=r"^\d{6}$")
    declaration_accepted: bool
    notice_version: str


class ConsentIn(BaseModel):
    purpose: Literal["core_service", "media_capture", "ai_personalization", "product_analytics"]
    granted: bool
    notice_version: str


class ConsentOut(Out):
    purpose: str
    granted: bool
    notice_version: str
    recorded_at: datetime


# ---------------------------------------------------------------------------- children
class ChildIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)
    birth_year: int | None = Field(default=None, ge=2000, le=2100)
    level_code: str | None = Field(default=None, pattern=r"^L[1-5]$")
    avatar: str = Field(default="star", max_length=30)
    interests: list[str] = Field(default_factory=list, max_length=12)
    goals: list[str] = Field(default_factory=list, max_length=10)


class ChildPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    birth_year: int | None = Field(default=None, ge=2000, le=2100)
    level_code: str | None = Field(default=None, pattern=r"^L[1-5]$")
    avatar: str | None = Field(default=None, max_length=30)
    interests: list[str] | None = Field(default=None, max_length=12)
    goals: list[str] | None = Field(default=None, max_length=10)
    version: int


class ChildOut(Out):
    id: uuid.UUID
    family_id: uuid.UUID
    display_name: str
    birth_year: int | None
    level_code: str | None
    avatar: str
    interests: list[str]
    goals: list[str]
    version: int


class ChildSessionIn(BaseModel):
    device_id: str = Field(min_length=8, max_length=100)


class ChildSessionOut(BaseModel):
    child_token: str
    expires_at: datetime
    child: ChildOut


# ---------------------------------------------------------------------------- curriculum
class LevelOut(Out):
    code: str
    name: str
    indicative_age: str | None
    description: str | None


class SubjectOut(Out):
    code: str
    name: str
    display_order: int


class SkillOut(BaseModel):
    code: str
    subject_code: str
    level_code: str
    name: str
    prerequisites: list[str]


class ActivitySummary(Out):
    id: uuid.UUID
    slug: str
    title: str
    summary: str | None
    subject_code: str
    level_from: str
    level_to: str
    duration_min: int
    materials: list[str]
    interest_tags: list[str]
    version: int


class LibraryActivity(ActivitySummary):
    """An activity as the parent's Activity library shows it for one child: the catalogue fields plus what that
    child has already done with it."""

    times_done: int  # finished sessions (0 = never done)
    last_done_at: datetime | None
    planned_for: date | None  # the next day it is planned for this child, if any


class ActivityDetail(ActivitySummary):
    skills: list[str]
    definition: dict[str, Any]


# ---------------------------------------------------------------------------- planning
class PlanItemIn(BaseModel):
    activity_id: uuid.UUID
    scheduled_date: date
    position: int = Field(default=0, ge=0, le=1000)


class PlanItemPatch(BaseModel):
    scheduled_date: date | None = None
    position: int | None = Field(default=None, ge=0, le=1000)
    status: Literal["planned", "skipped"] | None = None
    version: int


class PlanItemOut(BaseModel):
    id: uuid.UUID
    child_id: uuid.UUID
    activity: ActivitySummary
    scheduled_date: date
    position: int
    status: str
    version: int


class PlanOut(BaseModel):
    child_id: uuid.UUID
    week_start: date
    items: list[PlanItemOut]


# ---------------------------------------------------------------------------- sessions
class SessionStartIn(BaseModel):
    child_id: uuid.UUID
    activity_id: uuid.UUID
    plan_item_id: uuid.UUID | None = None
    client_op_id: uuid.UUID


class AutosaveIn(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    hints_used: int = Field(default=0, ge=0, le=1000)
    parent_assist: bool = False


class SubmitIn(AutosaveIn):
    duration_sec: int = Field(default=0, ge=0, le=86400)
    occurred_at: datetime | None = None


class ReviewIn(BaseModel):
    ratings: dict[str, Rating]
    parent_assist: bool | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    child_id: uuid.UUID
    activity: ActivitySummary
    activity_definition: dict[str, Any]
    status: str
    answers: dict[str, Any]
    hints_used: int
    parent_assist: bool
    duration_sec: int
    result: dict[str, Any] | None
    version: int


# ---------------------------------------------------------------------------- progress
class ObservationIn(BaseModel):
    skill_code: str
    rating: Rating
    note: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None


class BaselineItem(BaseModel):
    skill_code: str
    rating: Rating


class BaselineIn(BaseModel):
    ratings: list[BaselineItem] = Field(min_length=1, max_length=60)


class ConsentPurposeList(BaseModel):
    consents: list[ConsentOut]


class MasteryOut(BaseModel):
    skill_code: str
    skill_name: str
    subject_code: str
    level_code: str
    status: str
    score: float
    confidence: float
    evidence_count: int
    distinct_days: int
    last_evidence_at: datetime | None
    needs_revisit: bool


class ObservationOut(BaseModel):
    mastery: MasteryOut


class BaselineOut(BaseModel):
    recorded: list[str]
    skipped: list[str]


class SubjectProgress(BaseModel):
    subject_code: str
    subject_name: str
    progress_pct: int | None
    started: int
    total: int


class OverviewOut(BaseModel):
    child_id: uuid.UUID
    range_start: date
    range_end: date
    activities: int
    minutes: int
    independent_pct: int | None
    new_skills: int
    streak: int
    week_done: int
    week_planned: int
    subjects: list[SubjectProgress]


class RecommendationOut(BaseModel):
    activity: ActivitySummary
    score: float
    reason: str


class DashboardOut(BaseModel):
    overview: OverviewOut
    today: list[PlanItemOut]
    recommendations: list[RecommendationOut]


class AdminActivitySummary(ActivitySummary):
    status: str
    updated_at: datetime


class AdminActivityDetail(AdminActivitySummary):
    skills: list[str]
    definition: dict[str, Any]


class AdminStats(BaseModel):
    users: int
    families: int
    activities_by_status: dict[str, int]
    skills: int


# ---------------------------------------------------------------------------- admin
class AdminStatusIn(BaseModel):
    status: Literal["draft", "in_review", "published", "archived"]


class AdminDefinitionIn(BaseModel):
    definition: dict[str, Any]


class BundleIn(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    levels: list[dict[str, Any]] = Field(default_factory=list)
    subjects: list[dict[str, Any]] = Field(default_factory=list)
    interests: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    activities: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = True
    auto_publish: bool = False

    @field_validator("version")
    @classmethod
    def _v(cls, v: str) -> str:
        return v.strip()


class BundleReport(BaseModel):
    dry_run: bool
    ok: bool
    problems: list[str]
    created: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]


class HistoryItem(BaseModel):
    session_id: uuid.UUID
    activity_title: str
    subject_code: str
    submitted_at: datetime
    duration_sec: int
    score: float | None
    parent_assist: bool
    needs_review: list[str]


# ---------------------------------------------------------------------------- scrum board
ScrumCategory = Literal["content", "dev", "design", "other"]
ScrumStatus = Literal["backlog", "todo", "in_progress", "review", "done"]


class ScrumBootstrapIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ScrumMemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    is_admin: bool = False


class ScrumMemberOut(Out):
    id: uuid.UUID
    name: str
    is_admin: bool
    created_at: datetime


class ScrumMemberCreatedOut(BaseModel):
    """Returned once, at creation time only — the access code is never retrievable again."""

    member: ScrumMemberOut
    access_code: str


class ScrumWhoamiOut(Out):
    id: uuid.UUID
    name: str
    is_admin: bool


class ScrumTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category: ScrumCategory = "dev"
    status: ScrumStatus = "backlog"
    assignee_name: str | None = Field(default=None, max_length=120)
    sprint: str | None = Field(default=None, max_length=120)


class ScrumTaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category: ScrumCategory | None = None
    status: ScrumStatus | None = None
    assignee_name: str | None = Field(default=None, max_length=120)
    sprint: str | None = Field(default=None, max_length=120)
    position: int | None = None


class ScrumTaskOut(Out):
    id: uuid.UUID
    title: str
    description: str | None
    category: ScrumCategory
    status: ScrumStatus
    assignee_name: str | None
    sprint: str | None
    position: int
    created_by_member_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
