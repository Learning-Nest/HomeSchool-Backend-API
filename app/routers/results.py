"""Results and practice: how a child did, grouped for reading, and putting activities back into the plan."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.deps import ALL_ROLES, CurrentActor, DbSession, load_child
from app.errors import ApiError, not_found
from app.models import Activity, ActivitySession
from app.routers.sessions import _definition
from app.schemas import (
    ActivityAttemptsOut,
    ActivitySummary,
    AttemptDetailOut,
    ExerciseResultOut,
    PracticeIn,
    PracticeOut,
    PracticeSuggestionOut,
    ResultActivityOut,
    ResultGroupBy,
    ResultSummaryOut,
)
from app.services import daily, results
from app.services import practice as practice_service
from app.services.content import is_scored, needs_parent_review, normalize, step_skills

router = APIRouter(prefix="/v1/children/{child_id}", tags=["results"])

_KEY_OF = {
    "single_choice": "correct",
    "multi_choice": "correct",
    "numeric_input": "answer",
    "short_text": "accepted",
    "sequence_order": "correct_order",
    "match_pairs": "pairs",
}


def _labels(items: Any) -> dict[str, str]:
    return {i["id"]: str(i.get("label") or i["id"]) for i in items or [] if isinstance(i, dict) and "id" in i}


def _readable(step: dict[str, Any], value: Any) -> Any:
    """Answers are stored as option ids; a person reading the result wants the words on the card."""
    cfg = step.get("config") or {}
    kind = step["type"]
    if value is None:
        return None
    if kind in ("single_choice", "multi_choice"):
        names = _labels(cfg.get("options"))
        return [names.get(v, v) for v in value] if isinstance(value, list) else names.get(value, value)
    if kind == "sequence_order":
        names = _labels(cfg.get("items"))
        return [names.get(v, v) for v in value] if isinstance(value, list) else value
    if kind == "match_pairs":
        left, right = _labels(cfg.get("left")), _labels(cfg.get("right"))
        if isinstance(value, list):
            return [[left.get(p[0], p[0]), right.get(p[1], p[1])] for p in value if isinstance(p, list) and len(p) == 2]
    return value


@router.get("/results/summary", response_model=ResultSummaryOut)
def results_summary(
    child_id: uuid.UUID,
    actor: CurrentActor,
    db: DbSession,
    group_by: ResultGroupBy = "subject",
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    subject: str | None = None,
    book: str | None = None,
) -> ResultSummaryOut:
    child = load_child(db, actor, child_id)
    return results.summary(db, child, group_by, from_, to, subject=subject, book=book)


@router.get("/results/activities", response_model=list[ResultActivityOut])
def results_activities(
    child_id: uuid.UUID,
    actor: CurrentActor,
    db: DbSession,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    week_start: date | None = None,
    subject: str | None = None,
    book: str | None = None,
    lesson: int | None = None,
    attention: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ResultActivityOut]:
    child = load_child(db, actor, child_id)
    today = daily.local_today(db, child.family_id)
    if week_start is not None:
        start = results.week_start_of(week_start)
        end = date.fromordinal(start.toordinal() + 6)
    else:
        d_start, _ = results.default_range(today)
        start, end = from_ or d_start, to or today
    if end < start or (end - start).days > results.MAX_RANGE_DAYS:
        raise ApiError(422, "validation_error", "The date range is not valid.")
    return results.activity_rows(
        db,
        child,
        start,
        end,
        subject=subject,
        book=book,
        lesson=lesson,
        attention=attention,
        limit=limit,
        offset=offset,
    )


@router.get("/results/activities/{activity_id}", response_model=ActivityAttemptsOut)
def results_activity_attempts(
    child_id: uuid.UUID, activity_id: uuid.UUID, actor: CurrentActor, db: DbSession
) -> ActivityAttemptsOut:
    child = load_child(db, actor, child_id)
    activity = db.get(Activity, activity_id)
    if activity is None:
        raise not_found()
    return results.activity_attempts(db, child, activity)


@router.get("/results/attempts/{session_id}", response_model=AttemptDetailOut)
def results_attempt(child_id: uuid.UUID, session_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> AttemptDetailOut:
    """Per-exercise result of one finished session. The correct answers are sent to a parent token only."""
    child = load_child(db, actor, child_id)
    s = db.get(ActivitySession, session_id)
    if s is None or s.child_id != child.id or s.status != "submitted":
        raise not_found()
    result: dict[str, Any] = s.result or {}
    defn = normalize(_definition(db, s))
    activity = db.get(Activity, s.activity_id)
    steps = result.get("steps") or {}
    reviewed = result.get("reviewed") or {}
    exercises: list[ExerciseResultOut] = []
    for st in defn["steps"]:
        if not st.get("enabled", True) or st["type"] == "instruction":
            continue
        sid = st["id"]
        scored = is_scored(st)
        sc = (steps.get(sid) or {}).get("score")
        fb = st.get("feedback") or {}
        correct = None if sc is None else sc >= 1.0
        key = (st.get("key") or {}).get(_KEY_OF.get(st["type"], ""))
        exercises.append(
            ExerciseResultOut(
                id=sid,
                type=st["type"],
                prompt=st.get("prompt"),
                scored=scored,
                score=sc,
                correct=correct,
                answer=_readable(st, s.answers.get(sid)),
                correct_answer=_readable(st, key) if actor.is_parent and scored else None,
                feedback=(fb.get("correct") if correct else fb.get("incorrect")) if correct is not None else None,
                skills=step_skills(st, defn.get("skills") or []),
                needs_review=needs_parent_review(st) and sid not in reviewed,
                rating=reviewed.get(sid),
            )
        )
    return AttemptDetailOut(
        session_id=s.id,
        activity=ActivitySummary.model_validate(activity),
        submitted_at=s.submitted_at,
        score=results.auto_score(result),
        reviewed_score=results.reviewed_score(result),
        duration_sec=s.duration_sec,
        hints_used=s.hints_used,
        parent_assist=s.parent_assist,
        exercises=exercises,
    )


@router.post("/practice", response_model=PracticeOut, status_code=201)
def practice(child_id: uuid.UUID, body: PracticeIn, actor: CurrentActor, db: DbSession) -> PracticeOut:
    child = load_child(db, actor, child_id, ALL_ROLES)
    return practice_service.create_practice(db, actor, child, body)


@router.get("/practice/suggestions", response_model=list[PracticeSuggestionOut])
def practice_suggestions(
    child_id: uuid.UUID, actor: CurrentActor, db: DbSession, limit: Annotated[int, Query(ge=1, le=30)] = 10
) -> list[PracticeSuggestionOut]:
    child = load_child(db, actor, child_id)
    return practice_service.suggestions(db, child, limit)
