"""Audit log and transactional outbox writers (ADR-012). Both write in the caller's transaction."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActivityEvent, AuditLog, OutboxEvent


def audit(
    db: Session,
    action: str,
    *,
    actor_user_id=None,
    actor_child_id=None,
    family_id=None,
    entity: str | None = None,
    entity_id: Any = None,
    request_id: str | None = None,
    **meta: Any,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            actor_child_id=actor_child_id,
            family_id=family_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            meta=meta,
            request_id=request_id,
        )
    )


def emit(db: Session, event_type: str, payload: dict[str, Any], aggregate_id: Any = None) -> None:
    """Queue a domain event. Payloads carry identifiers only, never names or free text."""
    clean = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in payload.items()}
    db.add(
        OutboxEvent(
            event_type=event_type, aggregate_id=str(aggregate_id) if aggregate_id is not None else None, payload=clean
        )
    )


def record_activity_event(
    db: Session,
    activity,
    actor_id: uuid.UUID | None,
    action: str,
    *,
    status_from: str | None = None,
    status_to: str | None = None,
    coalesce_minutes: int = 0,
    **detail: Any,
) -> None:
    """Append a row to content.activity_events. Repeated 'edited' saves by the same person within `coalesce_minutes`
    are folded into one row (a counter) so autosave does not flood the log."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.security import now

    if coalesce_minutes and actor_id is not None:
        last = db.scalar(
            select(ActivityEvent)
            .where(ActivityEvent.activity_id == activity.id)
            .order_by(ActivityEvent.at.desc())
            .limit(1)
        )
        if (
            last is not None
            and last.actor_id == actor_id
            and last.action == action
            and last.at >= now() - timedelta(minutes=coalesce_minutes)
        ):
            last.at = now()
            last.version = activity.version
            last.detail = {**(last.detail or {}), **detail, "saves": int((last.detail or {}).get("saves", 1)) + 1}
            return
    db.add(
        ActivityEvent(
            activity_id=activity.id,
            actor_id=actor_id,
            action=action,
            version=activity.version,
            status_from=status_from,
            status_to=status_to,
            detail=detail,
        )
    )
