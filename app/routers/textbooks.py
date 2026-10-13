"""Boards, textbooks and lessons, and each child's progress through a textbook (see app/services/textbooks.py)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select

from app.deps import WRITE_ROLES, CurrentActor, DbSession, ParentActor, StaffActor, load_child
from app.errors import ApiError, not_found
from app.routers.admin import _require_editable, get_activity_for
from app.models import Activity, ActivityLesson, Board, Book, ChildBook, Lesson
from app.schemas import (
    ActivityLessonsIn,
    BoardOut,
    BookDetail,
    BookOut,
    BookPathOut,
    ChildBookOut,
    LessonOut,
    NextUpOut,
    PlacementOut,
    UnlockNextIn,
)
from app.security import now
from app.services import textbooks as tb
from app.services.events import audit

router = APIRouter(prefix="/v1", tags=["textbooks"])


# ------------------------------------------------------------------------------------------------ catalogue
@router.get("/curriculum/boards", response_model=list[BoardOut])
def boards(actor: CurrentActor, db: DbSession) -> list[Board]:
    return list(db.scalars(select(Board).order_by(Board.sort_order, Board.code)))


@router.get("/curriculum/books", response_model=list[BookOut])
def books(
    actor: CurrentActor,
    db: DbSession,
    board: str | None = None,
    grade: str | None = Query(default=None, max_length=8),
    subject: str | None = None,
) -> list[BookOut]:
    stmt = select(Book).where(Book.status == "active")
    if board:
        stmt = stmt.where(Book.board_code == board)
    if grade:
        stmt = stmt.where(Book.grade == grade)
    if subject:
        stmt = stmt.where(Book.subject_code == subject)
    return [tb.book_out(db, b) for b in db.scalars(stmt.order_by(Book.board_code, Book.grade, Book.subject_code, Book.title))]


@router.get("/curriculum/books/{book_code}", response_model=BookDetail)
def book_detail(book_code: str, actor: CurrentActor, db: DbSession) -> BookDetail:
    book = db.get(Book, book_code)
    if book is None or book.status != "active":
        raise not_found()
    counts = dict(
        db.execute(
            select(ActivityLesson.lesson_id, func.count())
            .join(Activity, Activity.id == ActivityLesson.activity_id)
            .where(Activity.status == "published")
            .group_by(ActivityLesson.lesson_id)
        ).all()
    )
    lessons = db.scalars(select(Lesson).where(Lesson.book_code == book.code).order_by(Lesson.position))
    return BookDetail(
        **tb.book_out(db, book).model_dump(),
        lessons=[
            LessonOut(position=lsn.position, label=lsn.label, title=lsn.title, activity_count=counts.get(lsn.id, 0))
            for lsn in lessons
        ],
    )


# ------------------------------------------------------------------------------------------------ child progress
def _enrolment(db, child_id: uuid.UUID, book_code: str) -> ChildBook:
    row = db.get(ChildBook, (child_id, book_code))
    if row is None:
        raise not_found()
    return row


@router.get("/children/{child_id}/books", response_model=list[ChildBookOut])
def child_books(child_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> list[ChildBookOut]:
    child = load_child(db, actor, child_id)
    out: list[ChildBookOut] = []
    for en in db.scalars(select(ChildBook).where(ChildBook.child_id == child.id).order_by(ChildBook.started_at)):
        path = tb.build_path(db, child.id, en, show_locked_activities=False)
        out.append(
            ChildBookOut(
                book=path.book,
                unlocked_through=en.unlocked_through,
                lessons_total=len(path.lessons),
                lessons_complete=sum(1 for lsn in path.lessons if lsn.state == "complete"),
                can_unlock_next=path.can_unlock_next,
            )
        )
    return out


@router.put("/children/{child_id}/books/{book_code}", response_model=BookPathOut)
def enrol(child_id: uuid.UUID, book_code: str, actor: ParentActor, db: DbSession) -> BookPathOut:
    """Starts a child on a textbook (idempotent): the first lesson with activities is unlocked."""
    child = load_child(db, actor, child_id, WRITE_ROLES)
    book = db.get(Book, book_code)
    if book is None or book.status != "active":
        raise not_found()
    en = db.get(ChildBook, (child.id, book.code))
    if en is None:
        en = ChildBook(
            family_id=child.family_id,
            child_id=child.id,
            book_code=book.code,
            unlocked_through=tb.first_lesson_position(db, book.code),
        )
        db.add(en)
        db.flush()
        audit(
            db,
            "child_book.enrolled",
            actor_user_id=actor.user_id,
            family_id=child.family_id,
            entity="child",
            entity_id=child.id,
            book=book.code,
            request_id=actor.request_id,
        )
    return tb.build_path(db, child.id, en, show_locked_activities=True)


@router.delete("/children/{child_id}/books/{book_code}", status_code=204)
def unenrol(child_id: uuid.UUID, book_code: str, actor: ParentActor, db: DbSession) -> Response:
    child = load_child(db, actor, child_id, WRITE_ROLES)
    en = _enrolment(db, child.id, book_code)
    db.delete(en)
    audit(
        db,
        "child_book.removed",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        book=book_code,
        request_id=actor.request_id,
    )
    return Response(status_code=204)


@router.get("/children/{child_id}/books/{book_code}/path", response_model=BookPathOut)
def book_path(child_id: uuid.UUID, book_code: str, actor: CurrentActor, db: DbSession) -> BookPathOut:
    """The textbook in order with each lesson's state. A child never receives the activities of a locked lesson."""
    child = load_child(db, actor, child_id)
    return tb.build_path(db, child.id, _enrolment(db, child.id, book_code), show_locked_activities=actor.is_parent)


@router.post("/children/{child_id}/books/{book_code}/unlock-next", response_model=BookPathOut)
def unlock_next(
    child_id: uuid.UUID, book_code: str, body: UnlockNextIn, actor: ParentActor, db: DbSession
) -> BookPathOut:
    """Unlocks the next lesson (one at a time). Needs every unlocked lesson finished unless `force` is true."""
    child = load_child(db, actor, child_id, WRITE_ROLES)
    en = _enrolment(db, child.id, book_code)
    path = tb.build_path(db, child.id, en, show_locked_activities=True)
    if path.next_lesson_position is None:
        raise ApiError(409, "conflict", "There are no more lessons to unlock.")
    unfinished = any(lsn.state == "open" for lsn in path.lessons)
    if unfinished and not body.force:
        raise ApiError(409, "lesson_incomplete")
    en.unlocked_through = path.next_lesson_position
    en.updated_at = now()
    audit(
        db,
        "child_book.unlocked",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="child",
        entity_id=child.id,
        book=book_code,
        lesson=path.next_lesson_position,
        forced=bool(body.force and unfinished),
        request_id=actor.request_id,
    )
    db.flush()
    return tb.build_path(db, child.id, en, show_locked_activities=True)


@router.get("/children/{child_id}/next-up", response_model=list[NextUpOut])
def child_next_up(
    child_id: uuid.UUID, actor: CurrentActor, db: DbSession, limit: int = Query(default=10, ge=1, le=50)
) -> list[NextUpOut]:
    """What the child can do now across their textbooks: unlocked, not yet passed, in textbook order."""
    child = load_child(db, actor, child_id)
    return tb.next_up(db, child.id, limit)


# ------------------------------------------------------------------------------------------------ admin: links
@router.get("/admin/activities/{activity_id}/lessons", response_model=list[PlacementOut])
def get_links(activity_id: uuid.UUID, actor: StaffActor, db: DbSession) -> list[PlacementOut]:
    get_activity_for(db, actor, activity_id)  # an educator reaches only their own activities (404 otherwise)
    return tb.placements_of(db, activity_id)


@router.put("/admin/activities/{activity_id}/lessons", response_model=list[PlacementOut])
def set_links(activity_id: uuid.UUID, body: ActivityLessonsIn, actor: StaffActor, db: DbSession) -> list[PlacementOut]:
    """Replaces the lessons an activity belongs to (an activity may belong to lessons of several books and boards).

    Admins may change any activity; an educator only their own, and only while it is a draft."""
    activity = get_activity_for(db, actor, activity_id)
    _require_editable(actor, activity)
    wanted: dict[uuid.UUID, int] = {}
    for ref in body.lessons:
        lesson = db.scalar(select(Lesson).where(Lesson.book_code == ref.book, Lesson.position == ref.lesson))
        if lesson is None:
            raise ApiError(422, "validation_error", f"Unknown lesson {ref.lesson} of book {ref.book!r}.")
        wanted[lesson.id] = ref.order
    for link in db.scalars(select(ActivityLesson).where(ActivityLesson.activity_id == activity.id)):
        if link.lesson_id in wanted:
            link.position = wanted.pop(link.lesson_id)
        else:
            db.delete(link)
    for lesson_id, order in wanted.items():
        db.add(ActivityLesson(activity_id=activity.id, lesson_id=lesson_id, position=order))
    db.flush()
    audit(
        db,
        "activity.lessons_set",
        actor_user_id=actor.user_id,
        entity="activity",
        entity_id=activity.id,
        count=len(body.lessons),
        request_id=actor.request_id,
    )
    return tb.placements_of(db, activity.id)
