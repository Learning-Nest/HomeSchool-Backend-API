"""Activity content: JSON Schema + semantic validation, and auto-scoring of answers.

Two document formats are stored side by side:

* v1 (no ``schema_version``): flat steps, activity-level skills. Schema: schemas/activity-content.schema.json,
  vendored at app/content/activity.schema.json.
* v2 (``schema_version: 2``): every step ("exercise") owns its skills, answer keys live only in ``step.key``, what the
  child sees lives in ``step.config``. Schema: schemas/activity-content.v2.schema.json, vendored at
  app/content/activity.v2.schema.json. The top-level ``skills`` is derived from the exercises by ``finalize``.

scripts/sync_contracts.py refreshes the vendored copies; a test fails if they drift. The app is never sent a v2 document:
``public_definition`` projects it back to the v1 wire shape, so the app needs no change.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "content" / "activity.schema.json"
SCHEMA_V2_PATH = Path(__file__).resolve().parent.parent / "content" / "activity.v2.schema.json"
SCHEMA_VERSION = 2
AUTO_TYPES = {"single_choice", "multi_choice", "numeric_input", "short_text", "sequence_order", "match_pairs"}
UNSCORED_TYPES = {"instruction", "media_prompt", "audio_record", "photo_evidence", "timer_task", "reflection"}
SKILL_RE = re.compile(r"^[A-Z]{3}\.[A-Z0-9]+\.[A-Z0-9]+$")


@lru_cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@lru_cache
def _validator_v2() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_V2_PATH.read_text(encoding="utf-8")))


def _where(path) -> str:
    return "/".join(str(p) for p in path) or "(root)"


def _explain(e) -> list[str]:
    """One readable line per problem. For a step that matches none of the step types, report the errors of the branch
    for its own type instead of dumping the whole step."""
    if e.validator == "oneOf" and e.context and isinstance(e.instance, dict) and "type" in e.instance:
        branches = e.schema.get("oneOf", [])
        k = next(
            (
                i
                for i, b in enumerate(branches)
                if b.get("properties", {}).get("type", {}).get("const") == e.instance["type"]
            ),
            None,
        )
        if k is None:
            return [f"{_where(e.path)}: unknown step type {e.instance['type']!r}"]
        subs = [c for c in e.context if c.schema_path and c.schema_path[0] == k]
        if subs:
            return [f"{_where(e.path)}/{_where(c.path)}: {c.message[:160]}".replace("/(root)", "") for c in subs]
    return [f"{_where(e.path)}: {e.message[:160]}"]


# ------------------------------------------------------------------------------------------------ v1 <-> v2
# Where each v1 step field lives in a v2 step. Everything else on a v1 step is a common field (id, type, prompt, ...).
_CONFIG_FIELDS = {
    "media_prompt": ("caption", "alt_text"),
    "single_choice": ("options",),
    "multi_choice": ("options",),
    "short_text": ("max_len",),
    "sequence_order": ("items",),
    "match_pairs": ("left", "right"),
    "audio_record": ("max_seconds", "consent_required"),
    "photo_evidence": ("optional", "consent_required"),
    "timer_task": ("duration_sec", "checklist"),
    "parent_checklist": ("rating_options",),
    "reflection": ("emoji_options",),
}
_KEY_FIELDS = {
    "single_choice": ("correct",),
    "multi_choice": ("correct",),
    "numeric_input": ("answer", "tolerance"),
    "short_text": ("accepted",),
    "sequence_order": ("correct_order",),
    "match_pairs": ("pairs",),
}
_SECRET_FIELDS = ("correct", "answer", "accepted", "correct_order", "pairs", "tolerance")


def schema_version(definition: dict[str, Any]) -> Any:
    return definition.get("schema_version") or 1


def _is_v2_step(step: dict[str, Any]) -> bool:
    return "scoring" in step or "key" in step or "config" in step or "enabled" in step


def _default_mode(step_type: str) -> str:
    return "auto" if step_type in AUTO_TYPES else ("parent_rubric" if step_type == "parent_checklist" else "none")


def _skill_refs(raw: Any) -> list[dict[str, Any]]:
    """Accepts v1 ['A.B.C'] and v2 [{'code': 'A.B.C', 'weight': 0.5}]; always returns v2 refs."""
    out = []
    for r in raw or []:
        out.append({"code": r, "weight": 1} if isinstance(r, str) else dict(r))
    return out


def upgrade_step(step: dict[str, Any], activity_skills: list[str] | None = None) -> dict[str, Any]:
    """v1 step -> v2 step. Idempotent: a v2 step comes back unchanged (as a copy)."""
    if _is_v2_step(step):
        return json.loads(json.dumps(step))
    t = step["type"]
    out: dict[str, Any] = {"id": step["id"], "type": t, "enabled": True}
    prompt = step.get("text") if t == "instruction" else step.get("prompt")
    if prompt is not None:
        out["prompt"] = prompt
    for k in ("audio_ref", "media_ref"):
        if k in step:
            out[k] = step[k]
    config = {k: step[k] for k in _CONFIG_FIELDS.get(t, ()) if k in step}
    if config:
        out["config"] = config
    key = {k: step[k] for k in _KEY_FIELDS.get(t, ()) if k in step}
    if key:
        out["key"] = key
    scoring: dict[str, Any] = {}
    if step.get("scored") is False and t != "parent_checklist":
        scoring["mode"] = "none"
    elif t in AUTO_TYPES and not (t == "short_text" and not step.get("accepted")):
        scoring["mode"] = "auto"
    elif t == "parent_checklist":
        scoring["mode"] = "parent_rubric"
    else:
        scoring["mode"] = "none"
    if "points" in step:
        scoring["points"] = step["points"]
    if "partial_credit" in step:
        scoring["partial_credit"] = step["partial_credit"]
    out["scoring"] = scoring
    if t == "parent_checklist":
        refs = [{"code": step["skill_code"], "weight": 1}]
    elif "skills" in step:
        refs = _skill_refs(step["skills"])
    elif scoring["mode"] == "auto":
        refs = _skill_refs(activity_skills)  # v1 rule: a scored step without its own skills counts for the activity's
    else:
        refs = []
    if refs:
        out["skills"] = refs
    if step.get("hint"):
        out["feedback"] = {"hints": [step["hint"]]}
    return out


def _v2(step: dict[str, Any], activity_skills: list[str] | None = None) -> dict[str, Any]:
    """Read-only view of a step in v2 shape (no copy when it already is v2)."""
    return step if _is_v2_step(step) else upgrade_step(step, activity_skills)


def derive_skills(definition: dict[str, Any]) -> list[str]:
    """Skills that the enabled, evidence-producing exercises credit, in first-use order."""
    seen: dict[str, None] = {}
    for step in definition.get("steps", []):
        if produces_evidence(step):
            for ref in _skill_refs(step.get("skills")):
                seen.setdefault(ref["code"], None)
    return list(seen)


def normalize(definition: dict[str, Any]) -> dict[str, Any]:
    """Any stored definition -> a v2-shaped copy for the scorer. A v1 document keeps its own top-level skills."""
    d = json.loads(json.dumps(definition))
    if schema_version(d) == SCHEMA_VERSION:
        for s in d["steps"]:
            s.setdefault("enabled", True)
        return d
    d["schema_version"] = SCHEMA_VERSION
    d["steps"] = [upgrade_step(s, d.get("skills")) for s in d["steps"]]
    return d


def finalize(definition: dict[str, Any]) -> dict[str, Any]:
    """Server-side canonical form, applied on every write: for v2 the top-level skills are re-derived from the
    exercises. v1 documents are returned unchanged."""
    if schema_version(definition) != SCHEMA_VERSION:
        return definition
    d = json.loads(json.dumps(definition))
    derived = derive_skills(d)
    d["skills"] = derived or list(d.get("skills") or [])
    return d


def produces_evidence(step: dict[str, Any]) -> bool:
    return is_scored(step) or needs_parent_review(step)


def needs_parent_review(step: dict[str, Any]) -> bool:
    s = _v2(step)
    if s["type"] != "parent_checklist" or not s.get("enabled", True):
        return False
    return (s.get("scoring", {}).get("mode") or _default_mode("parent_checklist")) != "none"


def is_scored(step: dict[str, Any]) -> bool:
    """A step is auto-scored if it is enabled, in an auto type, in 'auto' mode, and has a key to score against."""
    s = _v2(step)
    t = s["type"]
    if not s.get("enabled", True) or t not in AUTO_TYPES:
        return False
    if (s.get("scoring", {}).get("mode") or _default_mode(t)) != "auto":
        return False
    if t == "short_text":
        return bool((s.get("key") or {}).get("accepted"))
    return True


def step_skill_weights(step: dict[str, Any], activity_skills: list[str] | None = None) -> list[tuple[str, float]]:
    """(skill code, weight) pairs an exercise gives evidence for."""
    s = _v2(step, activity_skills)
    return [(r["code"], float(r.get("weight", 1))) for r in s.get("skills", [])]


def step_skills(step: dict[str, Any], activity_skills: list[str]) -> list[str]:
    return [code for code, _ in step_skill_weights(step, activity_skills)]


def _check_v2(activity: dict[str, Any], known_skills: set[str] | None) -> list[str]:
    problems: list[str] = []
    steps = activity["steps"]
    if not any(s.get("enabled", True) for s in steps):
        problems.append("at least one exercise must be enabled")
    for step in steps:
        sid, t = step["id"], step["type"]
        cfg, key = step.get("config", {}), step.get("key", {})
        mode = step.get("scoring", {}).get("mode")
        enabled = step.get("enabled", True)
        if mode == "auto" and t not in AUTO_TYPES:
            problems.append(f"{sid}: scoring.mode 'auto' is not available for {t}")
        if mode == "parent_rubric" and t != "parent_checklist":
            problems.append(f"{sid}: scoring.mode 'parent_rubric' is only for parent_checklist")
        if t == "parent_checklist" and mode == "auto":
            problems.append(f"{sid}: a parent_checklist cannot be auto-scored")
        refs = _skill_refs(step.get("skills"))
        codes = [r["code"] for r in refs]
        if len(codes) != len(set(codes)):
            problems.append(f"{sid}: skills lists a skill twice")
        for code in codes:
            if not SKILL_RE.match(code):
                problems.append(f"{sid}: invalid skill code {code}")
            elif known_skills is not None and code not in known_skills:
                problems.append(f"{sid}: unknown skill {code}")
        if enabled and produces_evidence(step) and not refs:
            problems.append(f"{sid}: a scored exercise needs at least one skill")
        if t in ("single_choice", "multi_choice"):
            opt_ids = [o["id"] for o in cfg.get("options", [])]
            if len(opt_ids) != len(set(opt_ids)):
                problems.append(f"{sid}: option ids must be unique")
            correct = key.get("correct", [])
            if is_scored(step) and not correct:
                problems.append(f"{sid}: a scored {t} needs key.correct (or set scoring.mode to 'none')")
            if not set(correct) <= set(opt_ids):
                problems.append(f"{sid}: key.correct refers to an unknown option")
        if t == "sequence_order":
            item_ids = [i["id"] for i in cfg.get("items", [])]
            order = key.get("correct_order", [])
            if len(item_ids) != len(set(item_ids)) or sorted(order) != sorted(item_ids):
                problems.append(f"{sid}: key.correct_order must list every item exactly once")
        if t == "match_pairs":
            left = {i["id"] for i in cfg.get("left", [])}
            right = {i["id"] for i in cfg.get("right", [])}
            for a, b in key.get("pairs", []):
                if a not in left or b not in right:
                    problems.append(f"{sid}: pair {a},{b} refers to an unknown item")
    return problems


def _validate_v2(activity: dict[str, Any], known_skills: set[str] | None, known_levels: list[str] | None) -> list[str]:
    problems = [line for e in _validator_v2().iter_errors(activity) for line in _explain(e)]
    problems = [
        "no exercise gives evidence: enable a scored exercise that has at least one skill"
        if p.startswith("skills:") and "non-empty" in p
        else p
        for p in problems
    ]
    if problems:
        return problems
    order = known_levels or ["L1", "L2", "L3", "L4", "L5"]
    if order.index(activity["level_from"]) > order.index(activity["level_to"]):
        problems.append("level_from is above level_to")
    ids = [s["id"] for s in activity["steps"]]
    if len(ids) != len(set(ids)):
        problems.append("step ids must be unique")
    problems += _check_v2(activity, known_skills)
    derived = derive_skills(activity)
    if not derived:
        problems.append("no exercise gives evidence: enable a scored exercise that has at least one skill")
    elif sorted(activity["skills"]) != sorted(derived):
        problems.append(
            f"skills must equal the skills of the enabled scored exercises ({', '.join(derived)}); "
            "the server re-derives them on save"
        )
    return problems


def validate_activity(
    activity: dict[str, Any], known_skills: set[str] | None = None, known_levels: list[str] | None = None
) -> list[str]:
    """Returns a list of problems (empty when valid). Dispatches on schema_version."""
    if isinstance(activity, dict) and activity.get("schema_version") not in (None, 1):
        if activity.get("schema_version") != SCHEMA_VERSION:
            return [f"schema_version: unsupported version {activity.get('schema_version')!r}"]
        return _validate_v2(activity, known_skills, known_levels)
    return _validate_v1(activity, known_skills, known_levels)


def _validate_v1(
    activity: dict[str, Any], known_skills: set[str] | None = None, known_levels: list[str] | None = None
) -> list[str]:
    """v1 documents."""
    problems = [line for e in _validator().iter_errors(activity) for line in _explain(e)]
    if problems:
        return problems
    order = known_levels or ["L1", "L2", "L3", "L4", "L5"]
    if order.index(activity["level_from"]) > order.index(activity["level_to"]):
        problems.append("level_from is above level_to")
    ids = [s["id"] for s in activity["steps"]]
    if len(ids) != len(set(ids)):
        problems.append("step ids must be unique")
    skills = set(activity["skills"])
    if known_skills is not None:
        for code in skills - known_skills:
            problems.append(f"unknown skill {code}")
    for step in activity["steps"]:
        sid, t = step["id"], step["type"]
        if t in ("single_choice", "multi_choice"):
            opt_ids = {o["id"] for o in step["options"]}
            correct = step.get("correct", [])
            if is_scored(step) and not correct:
                problems.append(f"{sid}: scored {t} needs 'correct' (or set scored=false)")
            if not set(correct) <= opt_ids:
                problems.append(f"{sid}: 'correct' refers to unknown option")
        if t == "sequence_order":
            item_ids = {i["id"] for i in step["items"]}
            if set(step["correct_order"]) != item_ids or len(step["correct_order"]) != len(item_ids):
                problems.append(f"{sid}: correct_order must list every item exactly once")
        if t == "match_pairs":
            left = {i["id"] for i in step["left"]}
            right = {i["id"] for i in step["right"]}
            for a, b in step["pairs"]:
                if a not in left or b not in right:
                    problems.append(f"{sid}: pair {a},{b} refers to an unknown item")
        if t == "parent_checklist":
            if not SKILL_RE.match(step["skill_code"]):
                problems.append(f"{sid}: invalid skill_code")
            elif known_skills is not None and step["skill_code"] not in known_skills:
                problems.append(f"{sid}: unknown skill {step['skill_code']}")
        for code in step.get("skills", []):
            if known_skills is not None and code not in known_skills:
                problems.append(f"{sid}: unknown skill {code}")
    return problems


def public_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """What the client receives: the v1 wire shape, with every answer key stripped.

    A v1 document only loses its key fields. A v2 document is projected back to v1: disabled exercises are dropped,
    ``config`` is flattened onto the step, the first hint becomes ``hint``, and ``key``, ``scoring``, ``skills`` per
    step, ``feedback``, ``title``, ``authoring`` and ``schema_version`` are not sent.
    """
    if schema_version(definition) != SCHEMA_VERSION:
        out = json.loads(json.dumps(definition))
        for step in out.get("steps", []):
            for key in _SECRET_FIELDS:
                step.pop(key, None)
        return out
    out = {k: v for k, v in definition.items() if k not in ("schema_version", "authoring", "steps")}
    out["steps"] = []
    for s in definition["steps"]:
        if not s.get("enabled", True):
            continue
        w: dict[str, Any] = {"id": s["id"], "type": s["type"]}
        if "prompt" in s:
            w["text" if s["type"] == "instruction" else "prompt"] = s["prompt"]
        for k in ("audio_ref", "media_ref", "image"):
            if k in s:
                w[k] = json.loads(json.dumps(s[k]))
        w.update(json.loads(json.dumps(s.get("config", {}))))
        hints = (s.get("feedback") or {}).get("hints") or []
        if hints:
            w["hint"] = hints[0]
        if s.get("scoring", {}).get("partial_credit") is not None:
            w["partial_credit"] = s["scoring"]["partial_credit"]
        if s["type"] == "parent_checklist":
            refs = _skill_refs(s.get("skills"))
            if refs:
                w["skill_code"] = refs[0]["code"]
        out["steps"].append(w)
    return json.loads(json.dumps(out))


def _scoring_view(step: dict[str, Any]) -> dict[str, Any]:
    """Flat v1-like view of a v2 step that the scorer below reads."""
    sc = step.get("scoring") or {}
    return {
        "type": step["type"],
        "partial_credit": sc.get("partial_credit"),
        **step.get("config", {}),
        **step.get("key", {}),
    }


def score_step(step: dict[str, Any], answer: Any) -> float | None:
    """0..1 for an auto-scored step; None when the step is not auto-scored or was not answered."""
    if answer is None or not is_scored(step):
        return None
    step = _scoring_view(_v2(step))
    t = step["type"]
    if t == "single_choice":
        return 1.0 if isinstance(answer, str) and [answer] == step.get("correct") else 0.0
    if t == "multi_choice":
        correct = set(step["correct"])
        given = set(answer) if isinstance(answer, list) else set()
        if step.get("partial_credit"):
            wrong = len(given - correct)
            return max(0.0, (len(given & correct) - wrong) / len(correct))
        return 1.0 if given == correct else 0.0
    if t == "numeric_input":
        try:
            return 1.0 if abs(float(answer) - float(step["answer"])) <= float(step.get("tolerance", 0)) else 0.0
        except (TypeError, ValueError):
            return 0.0
    if t == "short_text":
        accepted = [a.strip().lower() for a in step.get("accepted", [])]
        if not accepted:
            return None
        return 1.0 if isinstance(answer, str) and answer.strip().lower() in accepted else 0.0
    if t == "sequence_order":
        return 1.0 if answer == step["correct_order"] else 0.0
    if t == "match_pairs":
        expected = {tuple(p) for p in step["pairs"]}
        given = {tuple(p) for p in answer if isinstance(p, list) and len(p) == 2} if isinstance(answer, list) else set()
        if not expected:
            return None
        return max(0.0, (len(given & expected) - len(given - expected)) / len(expected))
    return None
