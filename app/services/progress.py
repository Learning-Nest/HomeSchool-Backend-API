"""Evidence, mastery recomputation, progress overview and rule-based recommendations (24_PROGRESS_RULES)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivitySession,
    ActivitySkill,
    Child,
    Family,
    PlanItem,
    Skill,
    SkillEvidence,
    SkillMastery,
    SkillPrerequisite,
    Subject,
)
from app.schemas import ActivitySummary, MasteryOut, OverviewOut, RecommendationOut, SubjectProgress
from app.security import now
from app.services import mastery as m
from app.services.streak import compute_streak

REVISIT_DAYS = 30
RECOMMEND_WEIGHTS = {"goal": 0.40, "gap": 0.30, "interest": 0.20, "variety": 0.10}
RECENT_DAYS_EXCLUDED = 14


def family_tz(db: Session, family_id: uuid.UUID) -> ZoneInfo:
    name = db.scalar(select(Family.timezone).where(Family.id == family_id)) or "Asia/Kolkata"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Kolkata")


def local_day(ts: datetime, tz: ZoneInfo) -> date:
    return ts.astimezone(tz).date()


def week_start_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def day_bounds(d: date, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz)


# ------------------------------------------------------------------------------------------------ evidence + mastery
def add_evidence(
    db: Session,
    *,
    family_id: uuid.UUID,
    child_id: uuid.UUID,
    skill_code: str,
    source: str,
    value: float,
    independence: str,
    occurred_at: datetime,
    session_id: uuid.UUID | None = None,
    step_id: str | None = None,
    note: str | None = None,
    weight_factor: float = 1.0,
) -> None:
    """weight_factor is the exercise's weight for this skill (activity format v2); 1.0 for everything else."""
    weight = m.evidence_weight(source, independence) * weight_factor
    values = dict(
        family_id=family_id,
        child_id=child_id,
        skill_code=skill_code,
        session_id=session_id,
        step_id=step_id,
        source=source,
        value=value,
        base_weight=m.BASE_WEIGHTS[source],
        independence=independence,
        weight=weight,
        note=note,
        occurred_at=occurred_at,
    )
    if session_id is not None:
        # One row per (session, step, skill): a re-review updates the row instead of adding a second one.
        stmt = (
            pg_insert(SkillEvidence)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["session_id", "step_id", "skill_code"],
                index_where=SkillEvidence.session_id.is_not(None),
                set_={"value": value, "independence": independence, "weight": weight, "note": note},
            )
        )
        db.execute(stmt)
    else:
        db.add(SkillEvidence(**values))


def recompute_mastery(
    db: Session, child_id: uuid.UUID, family_id: uuid.UUID, skill_codes: set[str] | list[str]
) -> None:
    """Rebuild skill_mastery for the given skills by replaying the ordered evidence log (idempotent)."""
    db.flush()
    tz = family_tz(db, family_id)
    for code in sorted(set(skill_codes)):
        rows = db.execute(
            select(SkillEvidence.value, SkillEvidence.weight, SkillEvidence.occurred_at)
            .where(SkillEvidence.child_id == child_id, SkillEvidence.skill_code == code)
            .order_by(SkillEvidence.occurred_at, SkillEvidence.created_at, SkillEvidence.id)
        ).all()
        row = db.get(SkillMastery, (child_id, code))
        if not rows:
            if row is not None:
                db.delete(row)
            continue
        items = [
            m.Item(value=r.value, weight=r.weight, day=local_day(r.occurred_at, tz), occurred_at=r.occurred_at)
            for r in rows
        ]
        states = m.replay(items)
        final = states[-1]
        first_dev = next(
            (items[i].occurred_at for i, st in enumerate(states) if st.status in ("developing", "secure")), None
        )
        values = dict(
            family_id=family_id,
            score=final.score,
            confidence=final.confidence,
            status=final.status,
            evidence_count=final.n,
            distinct_days=final.distinct_days,
            last_evidence_at=items[-1].occurred_at,
            first_developing_at=first_dev,
            updated_at=now(),
        )
        if row is None:
            db.add(SkillMastery(child_id=child_id, skill_code=code, **values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
    db.flush()


def mastery_out(row: SkillMastery, skill: Skill) -> MasteryOut:
    revisit = bool(row.last_evidence_at and now() - row.last_evidence_at > timedelta(days=REVISIT_DAYS))
    return MasteryOut(
        skill_code=skill.code,
        skill_name=skill.name,
        subject_code=skill.subject_code,
        level_code=skill.level_code,
        status=row.status,
        score=round(row.score, 4),
        confidence=round(row.confidence, 4),
        evidence_count=row.evidence_count,
        distinct_days=row.distinct_days,
        last_evidence_at=row.last_evidence_at,
        needs_revisit=revisit,
    )


def child_mastery(db: Session, child_id: uuid.UUID, subject: str | None = None) -> list[MasteryOut]:
    q = (
        select(SkillMastery, Skill)
        .join(Skill, Skill.code == SkillMastery.skill_code)
        .where(SkillMastery.child_id == child_id)
    )
    if subject:
        q = q.where(Skill.subject_code == subject)
    return [
        mastery_out(r, s) for r, s in db.execute(q.order_by(Skill.subject_code, Skill.level_code, Skill.code)).all()
    ]


# ------------------------------------------------------------------------------------------------ overview
def _levels_upto(db: Session, level_code: str | None) -> list[str] | None:
    from app.models import Level

    if not level_code:
        return None
    order = list(db.scalars(select(Level.code).order_by(Level.sort_order)))
    return order[: order.index(level_code) + 1] if level_code in order else None


def overview(db: Session, child: Child, range_start: date, range_end: date) -> OverviewOut:
    tz = family_tz(db, child.family_id)
    lo, hi = day_bounds(range_start, tz), day_bounds(range_end + timedelta(days=1), tz)
    sess = db.execute(
        select(ActivitySession.duration_sec, ActivitySession.parent_assist, Activity.duration_min)
        .join(Activity, Activity.id == ActivitySession.activity_id)
        .where(
            ActivitySession.child_id == child.id,
            ActivitySession.status == "submitted",
            ActivitySession.submitted_at >= lo,
            ActivitySession.submitted_at < hi,
        )
    ).all()
    minutes = sum(min(r.duration_sec / 60.0, 3 * r.duration_min) for r in sess)
    independent = round(100 * sum(1 for r in sess if not r.parent_assist) / len(sess)) if sess else None
    new_skills = (
        db.scalar(
            select(func.count())
            .select_from(SkillMastery)
            .where(
                SkillMastery.child_id == child.id,
                SkillMastery.first_developing_at >= lo,
                SkillMastery.first_developing_at < hi,
            )
        )
        or 0
    )

    today = now().astimezone(tz).date()
    ws = week_start_of(today)
    week_items = db.execute(
        select(PlanItem.status).where(
            PlanItem.child_id == child.id,
            PlanItem.scheduled_date >= ws,
            PlanItem.scheduled_date < ws + timedelta(days=7),
            PlanItem.status != "skipped",
        )
    ).all()

    subjects = []
    levels = _levels_upto(db, child.level_code)
    for subj in db.scalars(select(Subject).order_by(Subject.display_order)):
        sq = select(Skill.code).where(Skill.subject_code == subj.code)
        if levels:
            sq = sq.where(Skill.level_code.in_(levels))
        codes = list(db.scalars(sq))
        scores = (
            list(
                db.scalars(
                    select(SkillMastery.score).where(
                        SkillMastery.child_id == child.id, SkillMastery.skill_code.in_(codes)
                    )
                )
            )
            if codes
            else []
        )
        subjects.append(
            SubjectProgress(
                subject_code=subj.code,
                subject_name=subj.name,
                started=len(scores),
                total=len(codes),
                progress_pct=round(100 * sum(scores) / len(scores)) if scores else None,
            )
        )
    return OverviewOut(
        child_id=child.id,
        range_start=range_start,
        range_end=range_end,
        activities=len(sess),
        minutes=round(minutes),
        independent_pct=independent,
        new_skills=new_skills,
        streak=streak_for(db, child, tz),
        week_done=sum(1 for r in week_items if r.status == "completed"),
        week_planned=len(week_items),
        subjects=subjects,
    )


def streak_for(db: Session, child: Child, tz: ZoneInfo) -> int:
    today = now().astimezone(tz).date()
    since = today - timedelta(days=120)
    done = {
        local_day(ts, tz)
        for ts in db.scalars(
            select(ActivitySession.submitted_at).where(
                ActivitySession.child_id == child.id,
                ActivitySession.status == "submitted",
                ActivitySession.submitted_at >= day_bounds(since, tz),
            )
        )
    }
    planned = set(
        db.scalars(
            select(PlanItem.scheduled_date).where(
                PlanItem.child_id == child.id,
                PlanItem.status != "skipped",
                PlanItem.scheduled_date >= since,
                PlanItem.scheduled_date <= today,
            )
        )
    )
    return compute_streak(today, done, planned)


# ------------------------------------------------------------------------------------------------ recommendations
def recommendations(
    db: Session, child: Child, limit: int = 5, max_minutes: int | None = None
) -> list[RecommendationOut]:
    from app.models import Level

    order = list(db.scalars(select(Level.code).order_by(Level.sort_order)))
    rank = {c: i for i, c in enumerate(order)}
    cutoff = now() - timedelta(days=RECENT_DAYS_EXCLUDED)
    recent_done = set(
        db.scalars(
            select(ActivitySession.activity_id).where(
                ActivitySession.child_id == child.id,
                ActivitySession.status == "submitted",
                ActivitySession.submitted_at >= cutoff,
            )
        )
    )
    last_subjects = list(
        db.scalars(
            select(Activity.subject_code)
            .join(ActivitySession, ActivitySession.activity_id == Activity.id)
            .where(ActivitySession.child_id == child.id, ActivitySession.status == "submitted")
            .order_by(ActivitySession.submitted_at.desc())
            .limit(3)
        )
    )

    mastery = {r.skill_code: r for r in db.scalars(select(SkillMastery).where(SkillMastery.child_id == child.id))}
    skills = {s.code: s for s in db.scalars(select(Skill))}
    prereqs: dict[str, list[str]] = {}
    for pr in db.scalars(select(SkillPrerequisite)):
        prereqs.setdefault(pr.skill_code, []).append(pr.prerequisite_code)
    act_skills: dict[uuid.UUID, list[str]] = {}
    for a_id, code in db.execute(select(ActivitySkill.activity_id, ActivitySkill.skill_code)).all():
        act_skills.setdefault(a_id, []).append(code)
    child_rank = rank.get(child.level_code) if child.level_code else None

    def prereq_ok(code: str) -> bool:
        for p in prereqs.get(code, []):
            r = mastery.get(p)
            if r is not None and r.status in ("developing", "secure"):
                continue
            # Never assessed and below the child's level: assumed known until evidence says otherwise.
            if r is None and child_rank is not None and rank.get(skills[p].level_code, 99) < child_rank:
                continue
            return False
        return True

    q = select(Activity).where(Activity.status == "published")
    if max_minutes:
        q = q.where(Activity.duration_min <= max_minutes)
    goals, interests = set(child.goals or []), set(child.interests or [])
    out: list[RecommendationOut] = []
    for a in db.scalars(q):
        if a.id in recent_done:
            continue
        if child_rank is not None and not (rank[a.level_from] <= child_rank <= rank[a.level_to]):
            continue
        codes = act_skills.get(a.id, [])
        if not all(c in mastery or prereq_ok(c) for c in codes):
            continue
        goal = 1.0 if (a.subject_code in goals or goals & set(codes)) else 0.0
        gaps = {
            c: (
                0.0
                if c in mastery and mastery[c].status == "secure"
                else (1.0 - mastery[c].score if c in mastery else 1.0)
            )
            for c in codes
        }
        gap = sum(gaps.values()) / len(gaps) if gaps else 0.0
        common = interests & set(a.interest_tags or [])
        interest = 1.0 if common else 0.0
        variety = 1.0 if a.subject_code not in last_subjects else 0.0
        score = (
            RECOMMEND_WEIGHTS["goal"] * goal
            + RECOMMEND_WEIGHTS["gap"] * gap
            + RECOMMEND_WEIGHTS["interest"] * interest
            + RECOMMEND_WEIGHTS["variety"] * variety
        )
        if goal:
            reason = "Because it supports one of your goals"
        elif gap >= 0.5 and gaps:
            top = max(gaps, key=gaps.get)
            reason = f"Because you are working on: {skills[top].name.lower()}"
        elif common:
            reason = "Because it matches interests: " + ", ".join(sorted(common))
        else:
            reason = "A change of subject" if variety else "Good practice"
        out.append(RecommendationOut(activity=ActivitySummary.model_validate(a), score=round(score, 4), reason=reason))
    out.sort(key=lambda r: (-r.score, r.activity.title))
    return out[:limit]
