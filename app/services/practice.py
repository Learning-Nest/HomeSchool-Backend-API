"""Practice again: a family puts activities back into the plan to improve a score.

A practice item is a plan item with source 'revision'. It shows up in Today with a "Practice" tag and never takes part
in the 70 % release test or in the engine's "already given" list, so practising never changes what is released next.
Parents and children may both ask for it, within limits set in the release policy (see `daily.Policy`):

* `revision_open_cap`: how many practice items may be open at once;
* `revision_cooldown_days`: how long after an activity was last finished it may be queued again;
* `child_daily_practice_limit`: how many practice items a child's own token may add per day.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import Actor
from app.errors import ApiError
from app.models import (
    Activity,
    ActivityLesson,
    ActivitySession,
    ActivitySkill,
    Child,
    PlanItem,
    SkillMastery,
)
from app.schemas import (
    ActivitySummary,
    PracticeIn,
    PracticeOut,
    PracticeSkipped,
    PracticeSuggestionOut,
)
from app.services import daily
from app.services.events import audit
from app.services.progress import family_tz, local_day
from app.services.results import effective_score

MAX_DAYS_AHEAD = 14


def _attempted(db: Session, child_id: uuid.UUID) -> dict[uuid.UUID, tuple[ActivitySession, float | None]]:
    """For every activity the child has finished: its latest session and its best score."""
    latest: dict[uuid.UUID, ActivitySession] = {}
    best: dict[uuid.UUID, float | None] = {}
    for s in db.scalars(
        select(ActivitySession)
        .where(ActivitySession.child_id == child_id, ActivitySession.status == "submitted")
        .order_by(ActivitySession.submitted_at)
    ):
        latest[s.activity_id] = s
        sc = effective_score(s.result)
        if sc is not None and (best.get(s.activity_id) is None or sc > best[s.activity_id]):  # type: ignore[operator]
            best[s.activity_id] = sc
        best.setdefault(s.activity_id, None)
    return {aid: (latest[aid], best[aid]) for aid in latest}


def _eligible(db: Session, attempted: set[uuid.UUID]) -> set[uuid.UUID]:
    """What may be practised: what the child has done, what sits in the same lesson, and what trains the same skills."""
    if not attempted:
        return set()
    ids = set(attempted)
    lessons = select(ActivityLesson.lesson_id).where(ActivityLesson.activity_id.in_(attempted))
    ids |= set(db.scalars(select(ActivityLesson.activity_id).where(ActivityLesson.lesson_id.in_(lessons))))
    skills = select(ActivitySkill.skill_code).where(ActivitySkill.activity_id.in_(attempted))
    ids |= set(db.scalars(select(ActivitySkill.activity_id).where(ActivitySkill.skill_code.in_(skills))))
    return ids


def _open_activity_ids(db: Session, child_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        db.scalars(
            select(PlanItem.activity_id).where(PlanItem.child_id == child_id, PlanItem.status.in_(daily.OPEN_STATUSES))
        )
    )


def create_practice(db: Session, actor: Actor, child: Child, body: PracticeIn) -> PracticeOut:
    from app.routers.plans import outs_for  # the plan screens' output shape

    policy = daily.effective_policy(db, child.board_code, child.grade)
    tz = family_tz(db, child.family_id)
    today = daily.local_today(db, child.family_id)
    day = body.scheduled_date or today
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ApiError(
            422, "validation_error", f"Practice can be planned from today up to {MAX_DAYS_AHEAD} days ahead."
        )
    if not actor.is_parent and day != today:
        raise ApiError(422, "validation_error", "A child can add practice for today only.")

    attempted = _attempted(db, child.id)
    allowed = _eligible(db, set(attempted))
    open_ids = _open_activity_ids(db, child.id)
    open_revision = (
        db.scalar(
            select(func.count())
            .select_from(PlanItem)
            .where(
                PlanItem.child_id == child.id,
                PlanItem.source == "revision",
                PlanItem.status.in_(daily.OPEN_STATUSES),
            )
        )
        or 0
    )
    created_today = (
        db.scalar(
            select(func.count())
            .select_from(PlanItem)
            .where(
                PlanItem.child_id == child.id, PlanItem.source == "revision", PlanItem.created_at >= _day_start(day, tz)
            )
        )
        or 0
        if today == day
        else 0
    )
    published = {
        a.id: a
        for a in db.scalars(select(Activity).where(Activity.id.in_(body.activity_ids), Activity.status == "published"))
    }
    skipped: list[PracticeSkipped] = []
    made: list[tuple[PlanItem, Activity]] = []
    position = (
        db.scalar(
            select(func.coalesce(func.max(PlanItem.position), -1)).where(
                PlanItem.child_id == child.id, PlanItem.scheduled_date == day
            )
        )
        or 0
    )
    seen: set[uuid.UUID] = set()
    for aid in body.activity_ids:
        if aid in seen:
            continue
        seen.add(aid)
        activity = published.get(aid)
        reason = None
        if activity is None:
            reason = "not_published"
        elif aid not in allowed:
            reason = "not_allowed"
        elif aid in open_ids:
            reason = "already_open"
        elif aid in attempted and day < local_day(attempted[aid][0].submitted_at, tz) + timedelta(
            days=policy.revision_cooldown_days
        ):
            reason = "cooldown"
        elif not actor.is_parent and created_today + len(made) >= policy.child_daily_practice_limit:
            reason = "daily_limit"
        elif open_revision + len(made) >= policy.revision_open_cap:
            reason = "cap_reached"
        if reason:
            skipped.append(PracticeSkipped(activity_id=aid, reason=reason))  # type: ignore[arg-type]
            continue
        assert activity is not None
        last = attempted[aid][0] if aid in attempted else None
        position += 1
        item = PlanItem(
            family_id=child.family_id,
            child_id=child.id,
            plan_id=daily.plan_for(db, child, day).id,
            activity_id=aid,
            scheduled_date=day,
            position=position,
            source="revision",
            book_code=last.book_code if last else None,
            lesson_position=last.lesson_position if last else None,
        )
        db.add(item)
        db.flush()
        made.append((item, activity))
        audit(
            db,
            "plan.practice_added",
            actor_user_id=actor.user_id,
            actor_child_id=actor.child_id,
            family_id=child.family_id,
            entity="plan_item",
            entity_id=item.id,
            request_id=actor.request_id,
        )
    return PracticeOut(items=outs_for(db, made, today), skipped=skipped)


def _day_start(day, tz):
    from app.services.progress import day_bounds

    return day_bounds(day, tz)


def suggestions(db: Session, child: Child, limit: int = 10) -> list[PracticeSuggestionOut]:
    """Worth practising: activities finished below the pass mark, then activities for the weakest skills, then the other
    activities of the same lessons."""
    pass_score = get_settings().lesson_pass_score
    attempted = _attempted(db, child.id)
    open_ids = _open_activity_ids(db, child.id)
    picked: dict[uuid.UUID, tuple[str, float | None]] = {}

    weak = sorted(
        ((aid, best) for aid, (_s, best) in attempted.items() if best is not None and best < pass_score),
        key=lambda t: t[1],  # type: ignore[arg-type,return-value]
    )
    for aid, best in weak:
        picked[aid] = ("below_pass_mark", best)

    weak_skills = [
        r.skill_code
        for r in db.scalars(
            select(SkillMastery)
            .where(SkillMastery.child_id == child.id, SkillMastery.score < pass_score)
            .order_by(SkillMastery.score)
            .limit(5)
        )
    ]
    if weak_skills:
        by_skill: dict[uuid.UUID, int] = defaultdict(int)
        for aid, _code in db.execute(
            select(ActivitySkill.activity_id, ActivitySkill.skill_code).where(ActivitySkill.skill_code.in_(weak_skills))
        ).all():
            by_skill[aid] += 1
        # untried first, then the longest ago; more weak skills trained first
        for aid in sorted(by_skill, key=lambda a: (a in attempted, -by_skill[a], str(a))):
            picked.setdefault(aid, ("weak_skill", attempted[aid][1] if aid in attempted else None))

    if weak:
        lessons = select(ActivityLesson.lesson_id).where(ActivityLesson.activity_id.in_([a for a, _ in weak]))
        for aid in db.scalars(
            select(ActivityLesson.activity_id)
            .where(ActivityLesson.lesson_id.in_(lessons))
            .order_by(ActivityLesson.position)
        ):
            picked.setdefault(aid, ("same_lesson", attempted[aid][1] if aid in attempted else None))

    if not picked:
        return []
    acts = {
        a.id: a
        for a in db.scalars(select(Activity).where(Activity.id.in_(list(picked)), Activity.status == "published"))
    }
    out: list[PracticeSuggestionOut] = []
    for aid, (reason, best) in picked.items():
        if aid not in acts:
            continue
        out.append(
            PracticeSuggestionOut(
                activity=ActivitySummary.model_validate(acts[aid]),
                reason=reason,  # type: ignore[arg-type]
                best_score=best,
                open_in_plan=aid in open_ids,
            )
        )
        if len(out) >= limit:
            break
    return out
