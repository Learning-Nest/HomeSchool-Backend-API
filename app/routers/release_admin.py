"""Release settings and content coverage for the daily release (admin console).

Content admins read; only a super admin changes settings. None of this touches family data.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import and_, func, select

from app.deps import AdminActor, DbSession, SuperAdminActor
from app.errors import ApiError, not_found
from app.models import Activity, ActivityLesson, Board, Book, Lesson, ReleasePolicy
from app.schemas import (
    CoverageBookOut,
    CoverageLessonOut,
    CoverageOut,
    ReleasePolicyIn,
    ReleasePolicyOut,
    ReleasePreviewActivity,
    ReleasePreviewDay,
    ReleasePreviewIn,
    ReleasePreviewOut,
)
from app.security import now
from app.services import daily
from app.services.events import audit

router = APIRouter(prefix="/v1/admin", tags=["release"])
DEFAULT_TARGET = 3  # activities a lesson should have before it counts as covered


def _out(row: ReleasePolicy) -> ReleasePolicyOut:
    return ReleasePolicyOut(
        scope_board=row.scope_board,
        scope_grade=row.scope_grade,
        release_threshold=float(row.release_threshold),
        set_minutes=row.set_minutes,
        set_max_activities=row.set_max_activities,
        max_open_items=row.max_open_items,
        school_days=list(row.school_days or []),
        strong_score=float(row.strong_score),
        revision_open_cap=row.revision_open_cap,
        revision_cooldown_days=row.revision_cooldown_days,
        child_daily_practice_limit=row.child_daily_practice_limit,
        updated_at=row.updated_at,
    )


@router.get("/release-policies", response_model=list[ReleasePolicyOut])
def list_policies(_actor: AdminActor, db: DbSession) -> list[ReleasePolicyOut]:
    rows = db.scalars(select(ReleasePolicy).order_by(ReleasePolicy.scope_board, ReleasePolicy.scope_grade))
    return [_out(r) for r in rows]


@router.put("/release-policies", response_model=ReleasePolicyOut)
def put_policy(
    body: ReleasePolicyIn,
    actor: SuperAdminActor,
    db: DbSession,
    board: Annotated[str, Query(max_length=20)] = "",
    grade: Annotated[str, Query(max_length=20)] = "",
) -> ReleasePolicyOut:
    """Creates or replaces the settings for a scope: both empty = the default, a board alone = that board, or both."""
    if grade and not board:
        raise ApiError(422, "validation_error", "Choose a board when you choose a grade.")
    if board and db.get(Board, board) is None:
        raise ApiError(422, "validation_error", f"Unknown board {board}.")
    row = db.get(ReleasePolicy, (board, grade))
    values = {
        "release_threshold": Decimal(str(body.release_threshold)),
        "set_minutes": body.set_minutes,
        "set_max_activities": body.set_max_activities,
        "max_open_items": body.max_open_items,
        "school_days": body.school_days,
        "strong_score": Decimal(str(body.strong_score)),
        "revision_open_cap": body.revision_open_cap,
        "revision_cooldown_days": body.revision_cooldown_days,
        "child_daily_practice_limit": body.child_daily_practice_limit,
        "updated_at": now(),
    }
    if row is None:
        row = ReleasePolicy(scope_board=board, scope_grade=grade, **values)
        db.add(row)
    else:
        for k, v in values.items():
            setattr(row, k, v)
    db.flush()
    audit(
        db,
        "release_policy.updated",
        actor_user_id=actor.user_id,
        entity="release_policy",
        entity_id=f"{board}/{grade}",
        request_id=actor.request_id,
        threshold=body.release_threshold,
        set_minutes=body.set_minutes,
        set_max_activities=body.set_max_activities,
    )
    return _out(row)


@router.delete("/release-policies", status_code=204)
def delete_policy(
    actor: SuperAdminActor,
    db: DbSession,
    board: Annotated[str, Query(max_length=20)] = "",
    grade: Annotated[str, Query(max_length=20)] = "",
) -> Response:
    """Removes an override so the scope falls back to the next broader settings. The default row stays."""
    if not board and not grade:
        raise ApiError(422, "validation_error", "The default settings cannot be removed.")
    row = db.get(ReleasePolicy, (board, grade))
    if row is None:
        raise not_found()
    db.delete(row)
    audit(
        db,
        "release_policy.removed",
        actor_user_id=actor.user_id,
        entity="release_policy",
        entity_id=f"{board}/{grade}",
        request_id=actor.request_id,
    )
    return Response(status_code=204)


@router.get("/coverage", response_model=CoverageOut)
def coverage(
    _actor: AdminActor,
    db: DbSession,
    board: str | None = None,
    grade: str | None = None,
    target_per_lesson: Annotated[int, Query(ge=1, le=20)] = DEFAULT_TARGET,
) -> CoverageOut:
    """How much published content each book has, lesson by lesson, and how many school days it lasts."""
    books_q = (
        select(Book).where(Book.status == "active").order_by(Book.board_code, Book.grade, Book.subject_code, Book.code)
    )
    if board:
        books_q = books_q.where(Book.board_code == board)
    if grade:
        books_q = books_q.where(Book.grade == grade)
    books = list(db.scalars(books_q))
    if not books:
        return CoverageOut(target_per_lesson=target_per_lesson, books=[])
    rows = db.execute(
        select(
            Lesson.book_code,
            Lesson.position,
            Lesson.title,
            func.count(Activity.id),
            func.coalesce(func.sum(Activity.duration_min), 0),
        )
        .outerjoin(ActivityLesson, ActivityLesson.lesson_id == Lesson.id)
        .outerjoin(Activity, and_(Activity.id == ActivityLesson.activity_id, Activity.status == "published"))
        .where(Lesson.book_code.in_([b.code for b in books]))
        .group_by(Lesson.id, Lesson.book_code, Lesson.position, Lesson.title)
        .order_by(Lesson.book_code, Lesson.position)
    ).all()
    by_book: dict[str, list[CoverageLessonOut]] = {}
    for code, pos, title, n, minutes in rows:
        by_book.setdefault(code, []).append(
            CoverageLessonOut(position=pos, title=title, published_activities=n, minutes=int(minutes))
        )
    standard_size: dict[tuple[str, str], int] = {}
    for b in books:
        standard_size[(b.board_code, b.grade)] = standard_size.get((b.board_code, b.grade), 0) + 1

    out: list[CoverageBookOut] = []
    for b in books:
        lessons = by_book.get(b.code, [])
        policy = daily.effective_policy(db, b.board_code, b.grade)
        per_day = max(1, policy.set_max_activities // standard_size[(b.board_code, b.grade)])
        total = sum(ls.published_activities for ls in lessons)
        weak = [ls for ls in lessons if ls.published_activities < target_per_lesson]
        out.append(
            CoverageBookOut(
                code=b.code,
                board_code=b.board_code,
                grade=b.grade,
                subject_code=b.subject_code,
                title=b.title,
                lessons=len(lessons),
                lessons_with_activities=sum(1 for ls in lessons if ls.published_activities),
                lessons_below_target=len(weak),
                published_activities=total,
                minutes=sum(ls.minutes for ls in lessons),
                days_of_content=math.ceil(total / per_day) if total else 0,
                weak_lessons=weak[:10],
            )
        )
    return CoverageOut(target_per_lesson=target_per_lesson, books=out)


@router.post("/release-preview", response_model=ReleasePreviewOut)
def release_preview(body: ReleasePreviewIn, _actor: AdminActor, db: DbSession) -> ReleasePreviewOut:
    """What a brand-new child of this board and grade would get over the next days, assuming every set is finished."""
    if db.get(Board, body.board_code) is None:
        raise ApiError(422, "validation_error", f"Unknown board {body.board_code}.")
    books = list(
        db.scalars(
            select(Book)
            .where(Book.board_code == body.board_code, Book.grade == body.grade, Book.status == "active")
            .order_by(Book.subject_code, Book.code)
        )
    )
    policy = daily.effective_policy(db, body.board_code, body.grade)
    given: set = set()
    days: list[ReleasePreviewDay] = []
    for n in range(1, body.days + 1):
        chosen = daily.build_set([daily.track_candidates(db, b.code, given) for b in books], policy)
        if not chosen:
            break
        given |= {a.id for _l, _p, a in chosen}
        days.append(
            ReleasePreviewDay(
                day=n,
                activities=[
                    ReleasePreviewActivity(
                        slug=a.slug,
                        title=a.title,
                        book_code=lsn.book_code,
                        lesson_position=lsn.position,
                        order=pos,
                        duration_min=a.duration_min,
                    )
                    for lsn, pos, a in chosen
                ],
                minutes=sum(a.duration_min for _l, _p, a in chosen),
            )
        )
    remaining = sum(len(daily.track_candidates(db, b.code, given)) for b in books)
    return ReleasePreviewOut(
        board_code=body.board_code,
        grade=body.grade,
        books=[b.code for b in books],
        days=days,
        remaining_activities=remaining,
    )
