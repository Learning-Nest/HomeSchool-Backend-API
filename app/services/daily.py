"""The daily release: LearnNest, not the parent, decides which activities a child does each day.

How it works (all of it runs inside the request that asks for it, so no scheduler is needed):

* A child follows one *track* per textbook of the family's standard (board + grade). Tracks are `child_books` rows.
* Once per local day, `sync` decides whether to release a new *set*. A set is released when
    - the child has no set yet, or at least `release_threshold` (default 70 %) of the last set is done (finished or skipped),
    - and the child is not carrying too many open activities (`max_open_items`),
    - and the day is a school day and the child is not paused.
* The set is built from the published activities of each track in textbook order (lesson position, then the activity's
  order inside the lesson, then title), minus everything already given or already finished. Tracks are served in turn
  so every subject moves every day, until the set reaches `set_max_activities` or `set_minutes`.
* The engine works from "not yet given", not from a position marker, so an activity added later at an earlier order is
  picked up by children who have not yet received it.
* Finishing means submitting; parent grading never blocks the next set.

Only families whose `plan_mode` is 'system' take part; parent-mode families behave exactly as before.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityLesson,
    ActivitySession,
    AssignmentBatch,
    Book,
    Child,
    ChildBook,
    Family,
    Lesson,
    Plan,
    PlanItem,
    ReleasePolicy,
)
from app.schemas import DailyProgressOut
from app.security import now
from app.services.events import audit, emit
from app.services.progress import family_tz, week_start_of

OPEN_STATUSES = ("planned", "in_progress")
DONE_STATUSES = ("completed", "skipped")


@dataclass(frozen=True)
class Policy:
    release_threshold: float = 0.70
    set_minutes: int = 45
    set_max_activities: int = 5
    max_open_items: int = 10
    school_days: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)
    strong_score: float = 0.85
    revision_open_cap: int = 5
    revision_cooldown_days: int = 1
    child_daily_practice_limit: int = 3


def _policy_of(row: ReleasePolicy) -> Policy:
    return Policy(
        release_threshold=float(row.release_threshold),
        set_minutes=row.set_minutes,
        set_max_activities=row.set_max_activities,
        max_open_items=row.max_open_items,
        school_days=tuple(row.school_days or (1, 2, 3, 4, 5, 6, 7)),
        strong_score=float(row.strong_score),
        revision_open_cap=row.revision_open_cap,
        revision_cooldown_days=row.revision_cooldown_days,
        child_daily_practice_limit=row.child_daily_practice_limit,
    )


def effective_policy(db: Session, board: str | None, grade: str | None) -> Policy:
    """The most specific settings row: (board, grade), then (board, ''), then the default row ('', '')."""
    board, grade = board or "", grade or ""
    rows = {
        (r.scope_board, r.scope_grade): r
        for r in db.scalars(
            select(ReleasePolicy).where(
                ReleasePolicy.scope_board.in_(["", board]), ReleasePolicy.scope_grade.in_(["", grade])
            )
        )
    }
    for key in ((board, grade), (board, ""), ("", "")):
        if key in rows:
            return _policy_of(rows[key])
    return Policy()


def local_today(db: Session, family_id: uuid.UUID) -> date:
    return now().astimezone(family_tz(db, family_id)).date()


def plan_for(db: Session, child: Child, day: date) -> Plan:
    ws = week_start_of(day)
    plan = db.scalar(select(Plan).where(Plan.child_id == child.id, Plan.week_start == ws))
    if plan is None:
        plan = Plan(family_id=child.family_id, child_id=child.id, week_start=ws)
        db.add(plan)
        db.flush()
    return plan


# ------------------------------------------------------------------------------------------------ enrolment
def auto_enrol(db: Session, child: Child) -> list[str]:
    """Follows every active book of the child's board and grade (source 'system') and stops following system-enrolled
    books that no longer match. Parent-enrolled books are never touched. Returns the codes now followed by standard."""
    wanted = (
        [
            b.code
            for b in db.scalars(
                select(Book).where(
                    Book.board_code == child.board_code, Book.grade == child.grade, Book.status == "active"
                )
            )
        ]
        if child.board_code and child.grade
        else []
    )
    existing = {e.book_code: e for e in db.scalars(select(ChildBook).where(ChildBook.child_id == child.id))}
    for code in wanted:
        en = existing.get(code)
        if en is None:
            db.add(
                ChildBook(
                    family_id=child.family_id, child_id=child.id, book_code=code, unlocked_through=0, source="system"
                )
            )
        elif not en.active:
            en.active, en.updated_at = True, now()
    for code, en in existing.items():
        if en.source == "system" and code not in wanted and en.active:
            en.active, en.updated_at = False, now()
    db.flush()
    return wanted


# ------------------------------------------------------------------------------------------------ candidates
def _given_or_done(db: Session, child_id: uuid.UUID) -> set[uuid.UUID]:
    given = set(
        db.scalars(select(PlanItem.activity_id).where(PlanItem.child_id == child_id, PlanItem.source == "system"))
    )
    done = set(
        db.scalars(
            select(ActivitySession.activity_id).where(
                ActivitySession.child_id == child_id, ActivitySession.status == "submitted"
            )
        )
    )
    return given | done


def track_candidates(db: Session, book_code: str, excluded: set[uuid.UUID]) -> list[tuple[Lesson, int, Activity]]:
    """Published activities of a book in textbook order that are not excluded. One entry per activity."""
    rows = db.execute(
        select(Lesson, ActivityLesson.position, Activity)
        .join(ActivityLesson, ActivityLesson.lesson_id == Lesson.id)
        .join(Activity, Activity.id == ActivityLesson.activity_id)
        .where(Lesson.book_code == book_code, Activity.status == "published")
        .order_by(Lesson.position, ActivityLesson.position, Activity.title)
    ).all()
    out: list[tuple[Lesson, int, Activity]] = []
    seen: set[uuid.UUID] = set()
    for lsn, pos, act in rows:
        if act.id in excluded or act.id in seen:
            continue
        seen.add(act.id)
        out.append((lsn, pos, act))
    return out


def build_set(
    tracks: list[list[tuple[Lesson, int, Activity]]], policy: Policy
) -> list[tuple[Lesson, int, Activity]]:
    """Takes the next activity from each track in turn until the set is full. The first activity is always taken, so a
    child is never left with nothing because every activity is longer than the minute budget."""
    queues = [list(t) for t in tracks if t]
    chosen: list[tuple[Lesson, int, Activity]] = []
    minutes = 0
    while queues and len(chosen) < policy.set_max_activities:
        progressed = False
        for q in list(queues):
            if len(chosen) >= policy.set_max_activities:
                break
            lsn, pos, act = q[0]
            if chosen and minutes + act.duration_min > policy.set_minutes:
                queues.remove(q)  # this track's next activity no longer fits today
                continue
            q.pop(0)
            chosen.append((lsn, pos, act))
            minutes += act.duration_min
            progressed = True
            if not q:
                queues.remove(q)
        if not progressed:
            break
    return chosen


# ------------------------------------------------------------------------------------------------ progress of the last set
def _last_batch(db: Session, child_id: uuid.UUID, before: date | None = None) -> AssignmentBatch | None:
    stmt = select(AssignmentBatch).where(AssignmentBatch.child_id == child_id)
    if before is not None:
        stmt = stmt.where(AssignmentBatch.released_on < before)
    return db.scalar(stmt.order_by(AssignmentBatch.released_on.desc()).limit(1))


def _batch_counts(db: Session, child_id: uuid.UUID, batch: AssignmentBatch) -> tuple[int, int]:
    """(done, total) of a set. An item is done when finished or skipped, or when the child submitted that activity
    since the set was released (even if it was started from the library rather than from the plan)."""
    items = list(db.scalars(select(PlanItem).where(PlanItem.batch_id == batch.id)))
    if not items:
        return 0, 0
    submitted_since = set(
        db.scalars(
            select(ActivitySession.activity_id).where(
                ActivitySession.child_id == child_id,
                ActivitySession.status == "submitted",
                ActivitySession.submitted_at >= batch.created_at,
            )
        )
    )
    done = sum(1 for i in items if i.status in DONE_STATUSES or i.activity_id in submitted_since)
    return done, len(items)


def open_system_items(db: Session, child_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(PlanItem)
            .where(PlanItem.child_id == child_id, PlanItem.source == "system", PlanItem.status.in_(OPEN_STATUSES))
        )
        or 0
    )


def progress(db: Session, child: Child, policy: Policy) -> DailyProgressOut:
    batch = _last_batch(db, child.id)
    if batch is None:
        return DailyProgressOut(
            set_released_on=None,
            done=0,
            total=0,
            ratio=1.0,
            threshold=policy.release_threshold,
            more_needed=0,
            open_items=open_system_items(db, child.id),
        )
    done, total = _batch_counts(db, child.id, batch)
    ratio = round(done / total, 4) if total else 1.0
    need = math.ceil(round(policy.release_threshold * total, 6)) if total else 0
    return DailyProgressOut(
        set_released_on=batch.released_on,
        done=done,
        total=total,
        ratio=ratio,
        threshold=policy.release_threshold,
        more_needed=max(0, need - done),
        open_items=open_system_items(db, child.id),
    )


# ------------------------------------------------------------------------------------------------ the release
@dataclass
class SyncResult:
    reason: str
    items: list[PlanItem]
    policy: Policy


def _enrolled_books(db: Session, child_id: uuid.UUID) -> list[Book]:
    return list(
        db.scalars(
            select(Book)
            .join(ChildBook, ChildBook.book_code == Book.code)
            .where(ChildBook.child_id == child_id, ChildBook.active.is_(True), Book.status == "active")
            .order_by(Book.subject_code, Book.code)
        )
    )


def sync(db: Session, child: Child, *, actor_user_id=None, actor_child_id=None, request_id: str | None = None) -> SyncResult:
    """Releases today's set when the rules allow it. Safe to call as often as you like: one set at most per local day."""
    family = db.get(Family, child.family_id)
    policy = effective_policy(db, child.board_code, child.grade)
    if family is None or family.plan_mode != "system":
        return SyncResult("not_system_mode", [], policy)

    # Two devices syncing at once must not both release: the child row is the lock.
    db.execute(select(Child.id).where(Child.id == child.id).with_for_update())
    auto_enrol(db, child)  # follows books imported since the last sync, and drops books of an old standard
    today = local_today(db, child.family_id)

    todays = db.scalar(
        select(AssignmentBatch).where(AssignmentBatch.child_id == child.id, AssignmentBatch.released_on == today)
    )
    if todays is not None:
        items = list(db.scalars(select(PlanItem).where(PlanItem.batch_id == todays.id).order_by(PlanItem.position)))
        return SyncResult("already_released_today", items, policy)
    if child.paused_until is not None and today <= child.paused_until:
        return SyncResult("paused", [], policy)
    if today.isoweekday() not in policy.school_days:
        return SyncResult("not_a_school_day", [], policy)

    books = _enrolled_books(db, child.id)
    if not books:
        return SyncResult("no_books", [], policy)

    last = _last_batch(db, child.id)
    if last is not None:
        done, total = _batch_counts(db, child.id, last)
        if total and done < math.ceil(round(policy.release_threshold * total, 6)):
            return SyncResult("below_threshold", [], policy)
    if open_system_items(db, child.id) >= policy.max_open_items:
        return SyncResult("too_many_open", [], policy)

    excluded = _given_or_done(db, child.id)
    chosen = build_set([track_candidates(db, b.code, excluded) for b in books], policy)
    if not chosen:
        return SyncResult("no_content", [], policy)

    batch = AssignmentBatch(family_id=child.family_id, child_id=child.id, released_on=today, item_count=len(chosen))
    db.add(batch)
    db.flush()
    plan = plan_for(db, child, today)
    items: list[PlanItem] = []
    furthest: dict[str, int] = {}
    for i, (lsn, _pos, act) in enumerate(chosen):
        item = PlanItem(
            family_id=child.family_id,
            child_id=child.id,
            plan_id=plan.id,
            activity_id=act.id,
            scheduled_date=today,
            position=i,
            source="system",
            batch_id=batch.id,
            book_code=lsn.book_code,
            lesson_position=lsn.position,
            released_on=today,
        )
        db.add(item)
        items.append(item)
        furthest[lsn.book_code] = max(furthest.get(lsn.book_code, 0), lsn.position)
    for en in db.scalars(select(ChildBook).where(ChildBook.child_id == child.id, ChildBook.book_code.in_(list(furthest)))):
        en.unlocked_through = max(en.unlocked_through, furthest[en.book_code])
        en.last_release_on, en.updated_at = today, now()
    db.flush()
    audit(
        db,
        "plan.released",
        actor_user_id=actor_user_id,
        actor_child_id=actor_child_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        request_id=request_id,
        batch_id=str(batch.id),
        items=len(items),
    )
    emit(
        db,
        "plan.released",
        {"child_id": child.id, "family_id": child.family_id, "batch_id": batch.id, "items": len(items)},
        aggregate_id=batch.id,
    )
    return SyncResult("released", items, policy)


def is_system_family(db: Session, family_id: uuid.UUID) -> bool:
    return db.scalar(select(Family.plan_mode).where(Family.id == family_id)) == "system"
