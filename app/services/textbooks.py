"""Textbook progress: which lessons a child may see, what counts as finished, and what is next.

Rules (decided with the product owner):
* Soft lock - a child's own views (`next-up`, the book path) only offer activities of unlocked lessons, in textbook
  order; a parent can still open and plan any activity from the library.
* An activity counts as passed when the child submitted it and either it has no scored steps or the best score is at
  least `settings.lesson_pass_score`. A lesson is complete when all its published activities are passed.
* A parent unlocks one lesson at a time. Lessons without published activities are skipped, never block anyone.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Activity, ActivityLesson, ActivitySession, Book, ChildBook, Lesson
from app.schemas import (
    BookOut,
    BookPathOut,
    NextUpOut,
    PathActivityOut,
    PathLessonOut,
    PlacementOut,
)

ACTIVITY_STATES = ("not_started", "attempted", "passed")


def book_out(db: Session, book: Book) -> BookOut:
    n = db.scalar(select(func.count()).select_from(Lesson).where(Lesson.book_code == book.code)) or 0
    return BookOut(
        code=book.code,
        board_code=book.board_code,
        grade=book.grade,
        subject_code=book.subject_code,
        title=book.title,
        publisher=book.publisher,
        edition_year=book.edition_year,
        level_code=book.level_code,
        lesson_count=n,
    )


def placement_filter(stmt: Any, board: str | None, book: str | None) -> Any:
    """Restricts an Activity select to activities linked to a lesson of the given board and/or book."""
    if not board and not book:
        return stmt
    sub = (
        select(ActivityLesson.activity_id)
        .join(Lesson, Lesson.id == ActivityLesson.lesson_id)
        .join(Book, Book.code == Lesson.book_code)
        .where(Book.status == "active")
    )
    if board:
        sub = sub.where(Book.board_code == board)
    if book:
        sub = sub.where(Book.code == book)
    return stmt.where(Activity.id.in_(sub))


def placements_of(db: Session, activity_id: uuid.UUID) -> list[PlacementOut]:
    rows = db.execute(
        select(Book, Lesson, ActivityLesson.position)
        .join(Lesson, Lesson.book_code == Book.code)
        .join(ActivityLesson, ActivityLesson.lesson_id == Lesson.id)
        .where(ActivityLesson.activity_id == activity_id, Book.status == "active")
        .order_by(Book.board_code, Book.grade, Book.code, Lesson.position)
    ).all()
    return [
        PlacementOut(
            board_code=b.board_code,
            book_code=b.code,
            book_title=b.title,
            grade=b.grade,
            lesson_position=lsn.position,
            lesson_title=lsn.title,
            order=pos,
        )
        for b, lsn, pos in rows
    ]


def activity_states(db: Session, child_id: uuid.UUID, activity_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, float | None]]:
    """Per activity: ('not_started' | 'attempted' | 'passed', best score or None)."""
    if not activity_ids:
        return {}
    pass_score = get_settings().lesson_pass_score
    best: dict[uuid.UUID, tuple[bool, float | None]] = {}
    for aid, result in db.execute(
        select(ActivitySession.activity_id, ActivitySession.result).where(
            ActivitySession.child_id == child_id,
            ActivitySession.status == "submitted",
            ActivitySession.activity_id.in_(activity_ids),
        )
    ):
        score = (result or {}).get("score")
        passed = score is None or score >= pass_score  # no scored steps: finishing is enough
        prev_passed, prev_best = best.get(aid, (False, None))
        if score is None:
            top = prev_best
        else:
            top = score if prev_best is None else max(prev_best, score)
        best[aid] = (prev_passed or passed, top)
    out: dict[uuid.UUID, tuple[str, float | None]] = {}
    for aid in activity_ids:
        if aid not in best:
            out[aid] = ("not_started", None)
        else:
            passed, top = best[aid]
            out[aid] = ("passed" if passed else "attempted", top)
    return out


def build_path(db: Session, child_id: uuid.UUID, enrolment: ChildBook, *, show_locked_activities: bool) -> BookPathOut:
    book = db.get(Book, enrolment.book_code)
    lessons = list(db.scalars(select(Lesson).where(Lesson.book_code == book.code).order_by(Lesson.position)))
    by_lesson: dict[uuid.UUID, list[Activity]] = {}
    for lesson_id, activity in db.execute(
        select(ActivityLesson.lesson_id, Activity)
        .join(Activity, Activity.id == ActivityLesson.activity_id)
        .join(Lesson, Lesson.id == ActivityLesson.lesson_id)
        .where(Lesson.book_code == book.code, Activity.status == "published")
        .order_by(ActivityLesson.position, Activity.title)
    ):
        by_lesson.setdefault(lesson_id, []).append(activity)
    states = activity_states(db, child_id, [a.id for acts in by_lesson.values() for a in acts])

    out: list[PathLessonOut] = []
    unlocked_all_complete = True
    next_position: int | None = None
    for lsn in lessons:
        acts = by_lesson.get(lsn.id, [])
        locked = lsn.position > enrolment.unlocked_through
        complete = all(states[a.id][0] == "passed" for a in acts)  # no activities = nothing to do = complete
        if locked:
            state = "locked"
            if acts and next_position is None:
                next_position = lsn.position
        else:
            state = "complete" if complete else "open"
            if not complete:
                unlocked_all_complete = False
        visible = show_locked_activities or not locked
        out.append(
            PathLessonOut(
                position=lsn.position,
                label=lsn.label,
                title=lsn.title,
                state=state,
                activity_count=len(acts),
                activities=[
                    PathActivityOut(
                        id=a.id,
                        slug=a.slug,
                        title=a.title,
                        duration_min=a.duration_min,
                        status=states[a.id][0],
                        best_score=states[a.id][1],
                        locked=locked,
                    )
                    for a in acts
                ]
                if visible
                else [],
            )
        )
    return BookPathOut(
        book=book_out(db, book),
        unlocked_through=enrolment.unlocked_through,
        pass_score=get_settings().lesson_pass_score,
        next_lesson_position=next_position,
        can_unlock_next=unlocked_all_complete and next_position is not None,
        lessons=out,
    )


def first_lesson_position(db: Session, book_code: str) -> int:
    """Where a new enrolment starts: the first lesson that has a published activity (0 when there is none yet)."""
    return (
        db.scalar(
            select(func.min(Lesson.position))
            .join(ActivityLesson, ActivityLesson.lesson_id == Lesson.id)
            .join(Activity, Activity.id == ActivityLesson.activity_id)
            .where(Lesson.book_code == book_code, Activity.status == "published")
        )
        or 0
    )


def next_up(db: Session, child_id: uuid.UUID, limit: int) -> list[NextUpOut]:
    """Unlocked, not-yet-passed activities across the child's books, in textbook order."""
    out: list[NextUpOut] = []
    enrolments = db.scalars(select(ChildBook).where(ChildBook.child_id == child_id).order_by(ChildBook.started_at))
    for en in enrolments:
        book = db.get(Book, en.book_code)
        if book is None or book.status != "active":
            continue
        rows = db.execute(
            select(Lesson, Activity)
            .join(ActivityLesson, ActivityLesson.lesson_id == Lesson.id)
            .join(Activity, Activity.id == ActivityLesson.activity_id)
            .where(Lesson.book_code == book.code, Lesson.position <= en.unlocked_through, Activity.status == "published")
            .order_by(Lesson.position, ActivityLesson.position, Activity.title)
        ).all()
        states = activity_states(db, child_id, [a.id for _, a in rows])
        for lsn, a in rows:
            status = states[a.id][0]
            if status == "passed":
                continue
            out.append(
                NextUpOut(
                    **{
                        "id": a.id,
                        "slug": a.slug,
                        "title": a.title,
                        "summary": a.summary,
                        "subject_code": a.subject_code,
                        "level_from": a.level_from,
                        "level_to": a.level_to,
                        "duration_min": a.duration_min,
                        "materials": list(a.materials or []),
                        "interest_tags": list(a.interest_tags or []),
                        "version": a.version,
                    },
                    book_code=book.code,
                    lesson_position=lsn.position,
                    lesson_title=lsn.title,
                    status=status,
                )
            )
            if len(out) >= limit:
                return out
    return out
