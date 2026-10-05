"""Shared helpers: answer keys come from the seed bundle (the API never returns them)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

BUNDLE = json.loads(
    (Path(__file__).resolve().parent.parent / "seed" / "launch-bundle.json").read_text(encoding="utf-8")
)
DEFS = {a["slug"]: a for a in BUNDLE["activities"]}  # format v2 (what the seed ships)
# The v1 launch set, frozen: proves old documents still validate, upgrade, score and render the same.
V1_BUNDLE = json.loads(
    (Path(__file__).resolve().parent / "vectors" / "launch-bundle.v1.json").read_text(encoding="utf-8")
)
V1_DEFS = {a["slug"]: a for a in V1_BUNDLE["activities"]}


def correct_answers(slug: str, wrong: set[str] = frozenset()) -> dict:
    """Right answers for every auto-scored step of the activity (steps listed in `wrong` get a wrong answer)."""
    out: dict = {}
    defs = DEFS if slug in DEFS else V1_DEFS
    for st in defs[slug]["steps"]:
        t = st["type"]
        if st.get("scored") is False or (st.get("scoring") or {}).get("mode") == "none" or st.get("enabled") is False:
            continue
        flat = {**st, **st.get("config", {}), **st.get("key", {})}  # v1 steps are already flat
        if t == "single_choice":
            good = flat["correct"][0]
            bad = next(o["id"] for o in flat["options"] if o["id"] != good)
            out[st["id"]] = bad if st["id"] in wrong else good
        elif t == "sequence_order":
            out[st["id"]] = list(reversed(flat["correct_order"])) if st["id"] in wrong else flat["correct_order"]
        elif t == "match_pairs":
            pairs = flat["pairs"]
            out[st["id"]] = (
                [[a, pairs[(i + 1) % len(pairs)][1]] for i, (a, _b) in enumerate(pairs)] if st["id"] in wrong else pairs
            )
        elif t == "numeric_input":
            out[st["id"]] = flat["answer"] + 100 if st["id"] in wrong else flat["answer"]
    return out


def start(client, headers, child_id, activity_id, plan_item_id=None, op=None):
    return client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "child_id": child_id,
            "activity_id": activity_id,
            "plan_item_id": plan_item_id,
            "client_op_id": op or str(uuid.uuid4()),
        },
    )


def complete(
    client,
    headers,
    child_id,
    activity,
    answers=None,
    *,
    hints=0,
    assist=False,
    duration=300,
    occurred_at=None,
    plan_item_id=None,
):
    """Start and submit a session. Returns the submit response."""
    s = start(client, headers, child_id, activity["id"], plan_item_id)
    assert s.status_code == 201, s.text
    body = {
        "answers": answers if answers is not None else correct_answers(activity["slug"]),
        "hints_used": hints,
        "parent_assist": assist,
        "duration_sec": duration,
    }
    if occurred_at:
        body["occurred_at"] = occurred_at.isoformat()
    return client.post(f"/v1/sessions/{s.json()['id']}/submit", headers=headers, json=body)
