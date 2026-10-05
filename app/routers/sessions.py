"""Activity sessions: start (idempotent), autosave, submit (idempotent, scored on the server), parent review."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import ALL_ROLES, Actor, CurrentActor, DbSession, ParentActor, load_child
from app.errors import ApiError, conflict, not_found
from app.models import Activity, ActivitySession, ActivityVersion, Child, PlanItem, SkillEvidence
from app.schemas import ActivitySummary, AutosaveIn, ReviewIn, SessionOut, SessionStartIn, SubmitIn
from app.security import now
from app.services import catalog
from app.services import mastery as m
from app.services.content import (
    is_scored,
    needs_parent_review,
    normalize,
    public_definition,
    score_step,
    step_skill_weights,
)
from app.services.events import audit, emit
from app.services.progress import add_evidence, recompute_mastery

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
MAX_ANSWER_BYTES = 64 * 1024


def _definition(db, s: ActivitySession) -> dict[str, Any]:
    d = db.scalar(
        select(ActivityVersion.definition).where(
            ActivityVersion.activity_id == s.activity_id, ActivityVersion.version == s.activity_version
        )
    )
    if d is None:  # published before snapshots existed: freeze now
        a = db.get(Activity, s.activity_id)
        catalog.snapshot(db, a)
        d = a.definition
    return d


def _out(db, s: ActivitySession) -> SessionOut:
    a = db.get(Activity, s.activity_id)
    return SessionOut(
        id=s.id,
        child_id=s.child_id,
        activity=ActivitySummary.model_validate(a),
        activity_definition=public_definition(_definition(db, s)),
        status=s.status,
        answers=s.answers,
        hints_used=s.hints_used,
        parent_assist=s.parent_assist,
        duration_sec=s.duration_sec,
        result=s.result,
        version=s.version,
    )


def _load(db, actor: Actor, session_id: uuid.UUID) -> tuple[ActivitySession, Child]:
    s = db.get(ActivitySession, session_id)
    if s is None:
        raise not_found()
    return s, load_child(db, actor, s.child_id, ALL_ROLES)


def _check_answers(defn: dict[str, Any], answers: dict[str, Any]) -> None:
    ids = {st["id"] for st in defn["steps"] if st.get("enabled", True)}
    unknown = sorted(set(answers) - ids)
    if unknown:
        raise ApiError(422, "validation_error", f"Unknown step ids: {', '.join(unknown)}.")
    if len(json.dumps(answers)) > MAX_ANSWER_BYTES:
        raise ApiError(422, "validation_error", "Answers are too large.")


@router.post("", response_model=SessionOut, status_code=201)
def start(body: SessionStartIn, response: Response, actor: CurrentActor, db: DbSession) -> SessionOut:
    child = load_child(db, actor, body.child_id)
    existing = db.scalar(
        select(ActivitySession).where(
            ActivitySession.family_id == child.family_id, ActivitySession.client_op_id == body.client_op_id
        )
    )
    if existing is not None:
        if existing.child_id != child.id:
            raise conflict("This client_op_id was used for another child.")
        response.status_code = 200
        return _out(db, existing)
    activity = db.scalar(select(Activity).where(Activity.id == body.activity_id, Activity.status == "published"))
    if activity is None:
        raise ApiError(422, "validation_error", "The activity does not exist or is not published.")
    if body.plan_item_id is not None:
        item = db.get(PlanItem, body.plan_item_id)
        if item is None or item.child_id != child.id or item.activity_id != activity.id:
            raise ApiError(422, "validation_error", "The plan item does not match this child and activity.")
    catalog.snapshot(db, activity)
    s = ActivitySession(
        family_id=child.family_id,
        child_id=child.id,
        activity_id=activity.id,
        activity_version=activity.version,
        plan_item_id=body.plan_item_id,
        client_op_id=body.client_op_id,
    )
    try:
        with db.begin_nested():
            db.add(s)
            db.flush()
    except IntegrityError:  # two devices raced on the same client_op_id: return the winner
        existing = db.scalar(
            select(ActivitySession).where(
                ActivitySession.family_id == child.family_id, ActivitySession.client_op_id == body.client_op_id
            )
        )
        if existing is None:
            raise
        response.status_code = 200
        return _out(db, existing)
    if body.plan_item_id is not None and item.status == "planned":
        item.status = "in_progress"
        item.version += 1
    return _out(db, s)


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: uuid.UUID, actor: CurrentActor, db: DbSession) -> SessionOut:
    s, _ = _load(db, actor, session_id)
    return _out(db, s)


@router.put("/{session_id}/autosave", response_model=SessionOut)
def autosave(session_id: uuid.UUID, body: AutosaveIn, actor: CurrentActor, db: DbSession) -> SessionOut:
    s, _ = _load(db, actor, session_id)
    if s.status != "in_progress":
        raise conflict("This session is already submitted.")
    _check_answers(_definition(db, s), body.answers)
    s.answers = {**s.answers, **body.answers}
    s.hints_used = max(s.hints_used, body.hints_used)
    s.parent_assist = s.parent_assist or body.parent_assist
    s.version += 1
    return _out(db, s)


@router.post("/{session_id}/submit", response_model=SessionOut)
def submit(session_id: uuid.UUID, body: SubmitIn, actor: CurrentActor, db: DbSession) -> SessionOut:
    s, child = _load(db, actor, session_id)
    if s.status == "submitted":  # idempotent: a retried submit returns the stored result
        return _out(db, s)
    defn = normalize(_definition(db, s))
    _check_answers(defn, body.answers)
    answers = {**s.answers, **body.answers}
    hints = max(s.hints_used, body.hints_used)
    assist = s.parent_assist or body.parent_assist
    stamp = now()
    occurred = body.occurred_at or stamp
    if occurred.tzinfo is None:
        raise ApiError(422, "validation_error", "occurred_at must include a timezone.")
    if occurred < stamp - timedelta(days=7):
        raise ApiError(422, "validation_error", "occurred_at is more than 7 days in the past.")
    if occurred > stamp + timedelta(minutes=5):
        raise ApiError(422, "validation_error", "occurred_at is in the future.")
    occurred = min(occurred, stamp)

    independence = m.independence_for_session(hints, assist)
    affected: set[str] = set()
    step_results: dict[str, dict[str, Any]] = {}
    for step in defn["steps"]:
        if not is_scored(step):
            continue
        sc = score_step(step, answers.get(step["id"]))
        if sc is None:
            continue
        weights = step_skill_weights(step, defn["skills"])
        step_results[step["id"]] = {"score": round(sc, 4), "skills": [code for code, _ in weights]}
        for code, factor in weights:
            add_evidence(
                db,
                family_id=child.family_id,
                child_id=child.id,
                skill_code=code,
                source="activity",
                value=sc,
                independence=independence,
                occurred_at=occurred,
                session_id=s.id,
                step_id=step["id"],
                weight_factor=factor,
            )
            affected.add(code)
    scores = [r["score"] for r in step_results.values()]
    needs_review = [st["id"] for st in defn["steps"] if needs_parent_review(st)]
    s.answers, s.hints_used, s.parent_assist = answers, hints, assist
    s.status, s.submitted_at, s.duration_sec = "submitted", occurred, body.duration_sec
    s.result = {
        "score": round(sum(scores) / len(scores), 4) if scores else None,
        "scored_steps": len(scores),
        "steps": step_results,
        "needs_review": needs_review,
        "independence": independence,
        "skills": sorted(affected),
    }
    s.version += 1
    if s.plan_item_id is not None:
        item = db.get(PlanItem, s.plan_item_id)
        if item is not None and item.status != "skipped":
            item.status, item.version = "completed", item.version + 1
    recompute_mastery(db, child.id, child.family_id, affected)
    audit(
        db,
        "session.submitted",
        actor_user_id=actor.user_id,
        actor_child_id=actor.child_id,
        family_id=child.family_id,
        entity="activity_session",
        entity_id=s.id,
        request_id=actor.request_id,
    )
    emit(
        db,
        "session.submitted",
        {"session_id": s.id, "child_id": child.id, "family_id": child.family_id, "activity_id": s.activity_id},
        aggregate_id=s.id,
    )
    return _out(db, s)


@router.post("/{session_id}/review", response_model=SessionOut)
def review(session_id: uuid.UUID, body: ReviewIn, actor: ParentActor, db: DbSession) -> SessionOut:
    """Parent ratings for parent_checklist steps. Only a parent can create this evidence, never a child token."""
    s, child = _load(db, actor, session_id)
    if s.status != "submitted":
        raise conflict("Submit the session before reviewing it.")
    defn = normalize(_definition(db, s))
    checklist = {st["id"]: st for st in defn["steps"] if needs_parent_review(st)}
    bad = sorted(set(body.ratings) - set(checklist))
    if bad:
        raise ApiError(422, "validation_error", f"Not parent checklist steps: {', '.join(bad)}.")
    affected: set[str] = set()
    for sid, rating in body.ratings.items():
        for code, factor in step_skill_weights(checklist[sid], defn["skills"]):
            add_evidence(
                db,
                family_id=child.family_id,
                child_id=child.id,
                skill_code=code,
                source="activity",
                value=m.RATING_VALUES[rating],
                independence="independent",
                occurred_at=s.submitted_at,
                session_id=s.id,
                step_id=sid,
                weight_factor=factor,
            )
            affected.add(code)
    if body.parent_assist is not None and body.parent_assist != s.parent_assist:
        s.parent_assist = body.parent_assist
        independence = m.independence_for_session(s.hints_used, s.parent_assist)
        by_id = {st["id"]: st for st in defn["steps"]}
        for ev in db.scalars(
            select(SkillEvidence).where(
                SkillEvidence.session_id == s.id, SkillEvidence.step_id.not_in(list(checklist) or [""])
            )
        ):
            weights = dict(step_skill_weights(by_id[ev.step_id], defn["skills"])) if ev.step_id in by_id else {}
            factor = weights.get(ev.skill_code, 1.0)
            ev.independence, ev.weight = independence, m.evidence_weight(ev.source, independence) * factor
            affected.add(ev.skill_code)
    result = dict(s.result or {})
    reviewed = {**result.get("reviewed", {}), **body.ratings}
    result["reviewed"] = reviewed
    result["needs_review"] = [i for i in checklist if i not in reviewed]
    s.result = result
    s.version += 1
    recompute_mastery(db, child.id, child.family_id, affected)
    audit(
        db,
        "session.reviewed",
        actor_user_id=actor.user_id,
        family_id=child.family_id,
        entity="activity_session",
        entity_id=s.id,
        request_id=actor.request_id,
    )
    return _out(db, s)
