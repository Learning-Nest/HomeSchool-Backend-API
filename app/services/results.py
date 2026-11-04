"""Results: how a child did, aggregated by subject, week, subject + week, month, lesson or skill.

Everything is computed from the child's submitted sessions in the family's time zone (a few thousand rows at most, so
the grouping runs in Python and every grouping shares one code path).

Rules (kept in one place so the app, the engine and the page never disagree):

* An attempt's *score* is the parent-reviewed score when the parent has rated checklist steps (`reviewed_score`: the auto
  scores and the ratings averaged together), otherwise the auto-scored mean stored on the session (`result.score`).
  An attempt with neither is *unscored* (finishing is enough).
* A bucket's score is the mean of each activity's **best** score in it, so practising again raises it. The mean of the
  latest scores is reported next to it.
* A week is Monday to Sunday by the submission date in the family's time zone, as in the planner.
* The trend compares a bucket with the previous one: the older bucket of the same series for week, subject + week and
  month; the previous period of the same length for subject, lesson and skill.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ApiError
from app.models import Activity, ActivitySession, Child, Lesson, Skill, Subject
from app.schemas import (
    ActivityAttemptsOut,
    ActivitySummary,
    AttemptOut,
    ResultActivityOut,
    ResultBucket,
    ResultGroupBy,
    ResultSummaryOut,
)
from app.services import daily
from app.services import mastery as m
from app.services.progress import day_bounds, family_tz, local_day, week_start_of

MAX_RANGE_DAYS = 400
DEFAULT_WEEKS = 8
TREND_DELTA = 0.05
TIME_GROUPS = ("week", "subject_week", "month")


# ------------------------------------------------------------------------------------------------ scores
def auto_score(result: dict[str, Any] | None) -> float | None:
    s = (result or {}).get("score")
    return None if s is None else float(s)


def reviewed_score(result: dict[str, Any] | None) -> float | None:
    """The auto scores and the parent's ratings averaged together; None until the parent has rated something."""
    result = result or {}
    reviewed = result.get("reviewed") or {}
    ratings = [m.RATING_VALUES[r] for r in reviewed.values() if r in m.RATING_VALUES]
    if not ratings:
        return None
    steps = [float(v["score"]) for v in (result.get("steps") or {}).values() if v.get("score") is not None]
    values = steps + ratings
    return round(sum(values) / len(values), 4)


def effective_score(result: dict[str, Any] | None) -> float | None:
    r = reviewed_score(result)
    return r if r is not None else auto_score(result)


def _skill_scores(result: dict[str, Any] | None) -> dict[str, float]:
    per: dict[str, list[float]] = defaultdict(list)
    for v in ((result or {}).get("steps") or {}).values():
        if v.get("score") is None:
            continue
        for code in v.get("skills") or []:
            per[code].append(float(v["score"]))
    return {code: sum(vals) / len(vals) for code, vals in per.items()}


# ------------------------------------------------------------------------------------------------ loading
@dataclass
class Attempt:
    session_id: uuid.UUID
    activity_id: uuid.UUID
    submitted_at: datetime
    day: date
    week: date
    month: str
    score: float | None
    auto: float | None
    reviewed: float | None
    duration_sec: int
    duration_min: int
    hints: int
    assist: bool
    needs_review: list[str]
    book_code: str | None
    lesson_position: int | None
    subject_code: str
    skills: dict[str, float] = field(default_factory=dict)


def load_attempts(
    db: Session,
    child: Child,
    start: date,
    end: date,
    *,
    subject: str | None = None,
    book: str | None = None,
    lesson: int | None = None,
    activity_id: uuid.UUID | None = None,
) -> list[Attempt]:
    tz = family_tz(db, child.family_id)
    lo, hi = day_bounds(start, tz), day_bounds(end + timedelta(days=1), tz)
    q = (
        select(ActivitySession, Activity.subject_code, Activity.duration_min)
        .join(Activity, Activity.id == ActivitySession.activity_id)
        .where(
            ActivitySession.child_id == child.id,
            ActivitySession.status == "submitted",
            ActivitySession.submitted_at >= lo,
            ActivitySession.submitted_at < hi,
        )
        .order_by(ActivitySession.submitted_at)
    )
    if subject:
        q = q.where(Activity.subject_code == subject)
    if book:
        q = q.where(ActivitySession.book_code == book)
    if lesson is not None:
        q = q.where(ActivitySession.lesson_position == lesson)
    if activity_id is not None:
        q = q.where(ActivitySession.activity_id == activity_id)
    out: list[Attempt] = []
    for s, subject_code, duration_min in db.execute(q).all():
        day = local_day(s.submitted_at, tz)
        r = s.result or {}
        out.append(
            Attempt(
                session_id=s.id,
                activity_id=s.activity_id,
                submitted_at=s.submitted_at,
                day=day,
                week=week_start_of(day),
                month=f"{day.year:04d}-{day.month:02d}",
                score=effective_score(r),
                auto=auto_score(r),
                reviewed=reviewed_score(r),
                duration_sec=s.duration_sec,
                duration_min=duration_min,
                hints=s.hints_used,
                assist=s.parent_assist,
                needs_review=list(r.get("needs_review") or []),
                book_code=s.book_code,
                lesson_position=s.lesson_position,
                subject_code=subject_code,
                skills=_skill_scores(r),
            )
        )
    return out


# ------------------------------------------------------------------------------------------------ buckets
def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _trend(now_score: float | None, before: float | None) -> str | None:
    if now_score is None or before is None:
        return None
    d = now_score - before
    return "up" if d > TREND_DELTA else "down" if d < -TREND_DELTA else "flat"


def _keys(group_by: str, a: Attempt) -> list[tuple[str, float | None]]:
    """(bucket key, the score this attempt contributes to it)."""
    if group_by == "subject":
        return [(a.subject_code, a.score)]
    if group_by == "week":
        return [(a.week.isoformat(), a.score)]
    if group_by == "subject_week":
        return [(f"{a.subject_code}|{a.week.isoformat()}", a.score)]
    if group_by == "month":
        return [(a.month, a.score)]
    if group_by == "lesson":
        return [
            (
                f"{a.book_code}|{a.lesson_position}" if a.book_code and a.lesson_position is not None else "other",
                a.score,
            )
        ]
    if group_by == "skill":
        return [(code, sc) for code, sc in a.skills.items()]
    raise ValueError(group_by)


@dataclass
class _Agg:
    activities: int
    attempts: int
    scored: int
    best: float | None
    latest: float | None
    minutes: int
    independent_pct: int | None
    hints: int
    waiting: int
    attention: int


def _aggregate(units: dict[uuid.UUID, list[tuple[Attempt, float | None]]], pass_score: float) -> _Agg:
    best: list[float] = []
    latest: list[float] = []
    waiting = attempts = hints = 0
    minutes = 0.0
    independent = 0
    for items in units.values():
        items.sort(key=lambda t: t[0].submitted_at)
        scores = [s for _, s in items if s is not None]
        if scores:
            best.append(max(scores))
            latest.append(scores[-1])
        if items[-1][0].needs_review:
            waiting += 1
        for a, _ in items:
            attempts += 1
            hints += a.hints
            minutes += min(a.duration_sec / 60.0, 3 * a.duration_min)
            independent += 0 if a.assist else 1
    return _Agg(
        activities=len(units),
        attempts=attempts,
        scored=len(best),
        best=_mean(best),
        latest=_mean(latest),
        minutes=round(minutes),
        independent_pct=round(100 * independent / attempts) if attempts else None,
        hints=hints,
        waiting=waiting,
        attention=sum(1 for b in best if b < pass_score),
    )


def _group(group_by: str, attempts: list[Attempt]) -> dict[str, dict[uuid.UUID, list[tuple[Attempt, float | None]]]]:
    groups: dict[str, dict[uuid.UUID, list[tuple[Attempt, float | None]]]] = defaultdict(lambda: defaultdict(list))
    for a in attempts:
        for key, sc in _keys(group_by, a):
            groups[key][a.activity_id].append((a, sc))
    return groups


def snap_start(group_by: str, start: date) -> date:
    """Time groupings start on a whole week or month so the first bucket is never partial."""
    if group_by in ("week", "subject_week"):
        return week_start_of(start)
    if group_by == "month":
        return start.replace(day=1)
    return start


def default_range(today: date) -> tuple[date, date]:
    return week_start_of(today) - timedelta(weeks=DEFAULT_WEEKS - 1), today


def summary(
    db: Session,
    child: Child,
    group_by: ResultGroupBy,
    start: date | None,
    end: date | None,
    *,
    subject: str | None = None,
    book: str | None = None,
) -> ResultSummaryOut:
    today = daily.local_today(db, child.family_id)
    d_start, _d_end = default_range(today)
    start, end = start or d_start, end or today
    if end < start:
        raise ApiError(422, "validation_error", "`from` must not be after `to`.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ApiError(422, "validation_error", f"The range can be at most {MAX_RANGE_DAYS} days.")
    start = snap_start(group_by, start)
    pass_score = get_settings().lesson_pass_score
    policy = daily.effective_policy(db, child.board_code, child.grade)

    span = (end - start).days + 1
    prev_start = start - timedelta(days=span)
    everything = load_attempts(db, child, prev_start, end, subject=subject, book=book)
    current = [a for a in everything if a.day >= start]

    subjects = {s.code: s for s in db.scalars(select(Subject))}
    skills = {s.code: s for s in db.scalars(select(Skill))} if group_by == "skill" else {}
    lessons: dict[tuple[str, int], str] = {}
    if group_by == "lesson":
        books = {a.book_code for a in current if a.book_code}
        if books:
            lessons = {
                (b, p): t
                for b, p, t in db.execute(
                    select(Lesson.book_code, Lesson.position, Lesson.title).where(Lesson.book_code.in_(books))
                ).all()
            }

    groups = _group(group_by, current)
    aggs = {key: _aggregate(units, pass_score) for key, units in groups.items()}

    previous: dict[str, float | None] = {}
    if group_by in TIME_GROUPS:
        series: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
        for key, units in _group(group_by, everything).items():
            sk = key.split("|")[0] if group_by == "subject_week" else ""
            series[sk].append((key, _aggregate(units, pass_score).best))
        for items in series.values():
            items.sort(key=lambda t: t[0].split("|")[-1])
            for i, (key, _best) in enumerate(items):
                previous[key] = items[i - 1][1] if i else None
    else:
        before = [a for a in everything if a.day < start]
        previous = {key: _aggregate(units, pass_score).best for key, units in _group(group_by, before).items()}

    first_session: dict[str, Attempt] = {}
    for a in current:
        for key, _sc in _keys(group_by, a):
            first_session.setdefault(key, a)

    buckets: list[ResultBucket] = []
    for key, g in aggs.items():
        meta: dict[str, Any] = {"label": key}
        sample = first_session[key]
        if group_by == "subject":
            meta.update(subject_code=key, label=subjects[key].name if key in subjects else key)
        elif group_by == "week":
            meta.update(week_start=sample.week, label=key)
        elif group_by == "subject_week":
            meta.update(
                subject_code=sample.subject_code,
                week_start=sample.week,
                label=subjects[sample.subject_code].name if sample.subject_code in subjects else sample.subject_code,
            )
        elif group_by == "month":
            meta.update(month=key, label=key)
        elif group_by == "lesson":
            if key == "other":
                meta.update(label="Other")
            else:
                meta.update(
                    book_code=sample.book_code,
                    lesson_position=sample.lesson_position,
                    label=lessons.get(
                        (sample.book_code or "", sample.lesson_position or 0), f"Lesson {sample.lesson_position}"
                    ),
                    subject_code=sample.subject_code,
                )
        elif group_by == "skill":
            sk = skills.get(key)
            meta.update(skill_code=key, label=sk.name if sk else key, subject_code=sk.subject_code if sk else None)
        buckets.append(
            ResultBucket(
                key=key,
                activities=g.activities,
                attempts=g.attempts,
                scored_activities=g.scored,
                avg_best_score=g.best,
                avg_latest_score=g.latest,
                minutes=g.minutes,
                independent_pct=g.independent_pct,
                hints_used=g.hints,
                waiting_review=g.waiting,
                needs_attention=g.attention,
                previous_score=previous.get(key),
                trend=_trend(g.best, previous.get(key)),
                **meta,
            )
        )

    order = {code: s.display_order for code, s in subjects.items()}
    if group_by == "subject":
        buckets.sort(key=lambda b: (order.get(b.subject_code or "", 999), b.key))
    elif group_by == "week":
        buckets.sort(key=lambda b: b.week_start or date.min, reverse=True)
    elif group_by == "subject_week":
        buckets.sort(key=lambda b: (-(b.week_start or date.min).toordinal(), order.get(b.subject_code or "", 999)))
    elif group_by == "month":
        buckets.sort(key=lambda b: b.month or "", reverse=True)
    elif group_by == "lesson":
        buckets.sort(key=lambda b: (b.book_code is None, b.book_code or "", b.lesson_position or 0))
    else:
        buckets.sort(key=lambda b: (order.get(b.subject_code or "", 999), b.key))
    return ResultSummaryOut(
        child_id=child.id,
        group_by=group_by,
        range_start=start,
        range_end=end,
        pass_score=pass_score,
        strong_score=policy.strong_score,
        buckets=buckets,
    )


# ------------------------------------------------------------------------------------------------ per activity
def _status(best: float | None, waiting: bool, pass_score: float) -> str:
    if waiting:
        return "waiting_review"
    if best is None:
        return "unscored"
    return "passed" if best >= pass_score else "attempted"


def activity_rows(
    db: Session,
    child: Child,
    start: date,
    end: date,
    *,
    subject: str | None = None,
    book: str | None = None,
    lesson: int | None = None,
    attention: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[ResultActivityOut]:
    pass_score = get_settings().lesson_pass_score
    by_activity: dict[uuid.UUID, list[Attempt]] = defaultdict(list)
    for a in load_attempts(db, child, start, end, subject=subject, book=book, lesson=lesson):
        by_activity[a.activity_id].append(a)
    rows: list[tuple[uuid.UUID, list[Attempt], float | None, bool]] = []
    for aid, items in by_activity.items():
        scores = [a.score for a in items if a.score is not None]
        best = max(scores) if scores else None
        waiting = bool(items[-1].needs_review)
        needs = waiting or (best is not None and best < pass_score)
        if attention and not needs:
            continue
        rows.append((aid, items, best, waiting))
    rows.sort(key=lambda r: r[1][-1].submitted_at, reverse=True)
    rows = rows[offset : offset + limit]
    activities = {
        a.id: a for a in db.scalars(select(Activity).where(Activity.id.in_([r[0] for r in rows] or [uuid.uuid4()])))
    }
    out: list[ResultActivityOut] = []
    for aid, items, best, waiting in rows:
        scored = [a.score for a in items if a.score is not None]
        last = items[-1]
        out.append(
            ResultActivityOut(
                activity=ActivitySummary.model_validate(activities[aid]),
                attempts=len(items),
                first_score=scored[0] if scored else None,
                latest_score=scored[-1] if scored else None,
                best_score=best,
                last_done_at=last.submitted_at,
                status=_status(best, waiting, pass_score),
                needs_attention=waiting or (best is not None and best < pass_score),
                waiting_review=sum(1 for a in items if a.needs_review),
                book_code=last.book_code,
                lesson_position=last.lesson_position,
                last_session_id=last.session_id,
            )
        )
    return out


def activity_attempts(db: Session, child: Child, activity: Activity) -> ActivityAttemptsOut:
    tomorrow = daily.local_today(db, child.family_id) + timedelta(days=1)
    items = load_attempts(db, child, date(2000, 1, 1), tomorrow, activity_id=activity.id)
    scored = [a.score for a in items if a.score is not None]
    return ActivityAttemptsOut(
        activity=ActivitySummary.model_validate(activity),
        first_score=scored[0] if scored else None,
        latest_score=scored[-1] if scored else None,
        best_score=max(scored) if scored else None,
        attempts=[
            AttemptOut(
                session_id=a.session_id,
                submitted_at=a.submitted_at,
                score=a.auto,
                reviewed_score=a.reviewed,
                duration_sec=a.duration_sec,
                hints_used=a.hints,
                parent_assist=a.assist,
                needs_review=a.needs_review,
            )
            for a in reversed(items)
        ],
    )
