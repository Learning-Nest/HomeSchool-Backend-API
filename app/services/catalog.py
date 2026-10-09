"""Curriculum and activity catalogue writes: bundle import, definition updates, publishing and version snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import (
    Activity,
    ActivitySkill,
    ActivityVersion,
    Interest,
    Level,
    Skill,
    SkillPrerequisite,
    Subject,
)
from app.schemas import BundleIn, BundleReport
from app.security import now
from app.services.content import finalize, validate_activity
from app.services.images import asset_problems

ENTITIES = ("levels", "subjects", "interests", "skills", "activities")


def level_order(db: Session) -> list[str]:
    return list(db.scalars(select(Level.code).order_by(Level.sort_order)))


def known_skill_codes(db: Session) -> set[str]:
    return set(db.scalars(select(Skill.code)))


def snapshot(db: Session, activity: Activity) -> None:
    """Freeze the current definition under (activity_id, version). Idempotent."""
    db.execute(
        pg_insert(ActivityVersion)
        .values(activity_id=activity.id, version=activity.version, definition=activity.definition)
        .on_conflict_do_nothing(index_elements=["activity_id", "version"])
    )


def sync_activity_skills(db: Session, activity: Activity, skills: list[str]) -> None:
    db.execute(delete(ActivitySkill).where(ActivitySkill.activity_id == activity.id))
    for code in sorted(set(skills)):
        db.add(ActivitySkill(activity_id=activity.id, skill_code=code))


def apply_definition(a: Activity, d: dict[str, Any]) -> None:
    a.title = d["title"]
    a.summary = d.get("summary")
    a.subject_code = d["subject"]
    a.level_from = d["level_from"]
    a.level_to = d["level_to"]
    a.duration_min = d["duration_min"]
    a.materials = list(d.get("materials", []))
    a.interest_tags = list(d.get("interests", []))
    a.definition = d
    a.updated_at = now()


def definition_hash(definition: dict[str, Any]) -> str:
    """Stable fingerprint of a definition: 'validated' means this exact content passed validation."""
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_full(db: Session, definition: dict[str, Any], activity_id: uuid.UUID | None = None) -> list[str]:
    """Every check an activity must pass before review or publishing: schema, rules, live taxonomy and its images."""
    problems = validate_activity(definition, known_skill_codes(db), level_order(db))
    if not problems:
        problems += asset_problems(db, definition, activity_id)
    return problems


def publish(db: Session, a: Activity) -> None:
    """Validates against the live taxonomy, then marks the activity published and snapshots its version."""
    problems = validate_full(db, a.definition, a.id)
    if problems:
        raise ApiError(422, "content_invalid", "The activity cannot be published.", {"problems": problems})
    a.status = "published"
    a.published_at = a.published_at or now()
    snapshot(db, a)


_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    return base or "activity"


def unique_slug(db: Session, wanted: str) -> str:
    slug, n = wanted, 1
    while db.scalar(select(Activity.id).where(Activity.slug == slug)) is not None:
        n += 1
        slug = f"{wanted[: 76 - len(str(n))]}-{n}"
    return slug


def _lenient_check(db: Session, d: dict[str, Any]) -> None:
    """The minimum a DRAFT needs to be stored at all (the columns that must be filled). Everything else is checked by
    the Validate step, so an educator can save half-finished work."""
    problems: list[str] = []
    title = d.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        problems.append("title: required, up to 120 characters")
    if d.get("subject") not in set(db.scalars(select(Subject.code))):
        problems.append("subject: choose a subject")
    levels = level_order(db)
    for f in ("level_from", "level_to"):
        if d.get(f) not in levels:
            problems.append(f"{f}: choose a level")
    dur = d.get("duration_min")
    if not isinstance(dur, int) or isinstance(dur, bool) or not 1 <= dur <= 180:
        problems.append("duration_min: a whole number from 1 to 180")
    steps = d.get("steps")
    if not isinstance(steps, list) or not all(
        isinstance(s, dict) and isinstance(s.get("id"), str) and isinstance(s.get("type"), str) for s in steps
    ):
        problems.append("steps: must be a list of exercises that each have an id and a type")
    for f in ("materials", "interests"):
        if f in d and not (isinstance(d[f], list) and all(isinstance(x, str) for x in d[f])):
            problems.append(f"{f}: must be a list of text")
    if problems:
        raise ApiError(422, "content_invalid", "The draft cannot be saved yet.", {"problems": problems})


def _lenient_finalize(d: dict[str, Any]) -> dict[str, Any]:
    d = json.loads(json.dumps(d))
    d["schema_version"] = 2
    try:
        d = finalize(d)
    except Exception:  # half-built exercises: keep what the author sent
        d.setdefault("skills", [])
    d.setdefault("skills", [])
    return d


def update_definition(
    db: Session, a: Activity, definition: dict[str, Any], *, editor_id: uuid.UUID | None = None, strict: bool = True
) -> bool:
    """Replace the definition. Slug is immutable. A published activity gets a new version (old sessions keep the old one).

    strict=True (default) requires a fully valid activity. strict=False is for drafts only: the minimum needed to store
    it. Returns True when something changed."""
    if definition.get("slug") != a.slug:
        raise ApiError(
            422,
            "content_invalid",
            "The slug cannot be changed.",
            {"problems": ["slug: must equal the activity's slug"]},
        )
    if strict:
        definition = finalize(definition)  # v2: the top-level skills are derived from the exercises
        problems = validate_full(db, definition, a.id)
        if problems:
            raise ApiError(422, "content_invalid", "The activity failed validation.", {"problems": problems})
    else:
        if a.status != "draft":
            raise ApiError(409, "activity_locked", "Only a draft can be saved without full validation.")
        _lenient_check(db, definition)
        definition = _lenient_finalize(definition)
    if definition == a.definition:
        return False
    apply_definition(a, definition)
    known = known_skill_codes(db)
    sync_activity_skills(db, a, [c for c in definition.get("skills", []) if isinstance(c, str) and c in known])
    if editor_id is not None:
        a.last_edited_by, a.last_edited_at = editor_id, now()
    if a.status == "published":
        a.version += 1
        snapshot(db, a)
    return True


def create_activity(db: Session, definition: dict[str, Any], creator_id: uuid.UUID, source: str) -> Activity:
    """A new DRAFT written by a person. The slug is made from the title when the author did not pick one."""
    d = json.loads(json.dumps(definition))
    wanted = d.get("slug") if isinstance(d.get("slug"), str) and _SLUG_RE.match(d["slug"]) else None
    if wanted is None:
        wanted = slugify(d.get("title") if isinstance(d.get("title"), str) else "")
    d["slug"] = unique_slug(db, wanted)
    _lenient_check(db, d)
    d = _lenient_finalize(d)
    a = Activity(
        slug=d["slug"],
        title=d["title"],
        subject_code=d["subject"],
        level_from=d["level_from"],
        level_to=d["level_to"],
        duration_min=d["duration_min"],
        definition=d,
        status="draft",
        version=1,
        created_by=creator_id,
        last_edited_by=creator_id,
        last_edited_at=now(),
        source=source,
    )
    apply_definition(a, d)
    db.add(a)
    db.flush()
    known = known_skill_codes(db)
    sync_activity_skills(db, a, [c for c in d.get("skills", []) if isinstance(c, str) and c in known])
    return a


# ------------------------------------------------------------------------------------------------ bundle import
def _check_taxonomy(bundle: BundleIn, db: Session) -> tuple[list[str], set[str], list[str]]:
    problems: list[str] = []
    levels = list(dict.fromkeys(level_order(db) + [x["code"] for x in bundle.levels if "code" in x]))
    subjects = set(db.scalars(select(Subject.code))) | {x.get("code") for x in bundle.subjects}
    interests = set(db.scalars(select(Interest.code))) | {x.get("code") for x in bundle.interests}
    skills = known_skill_codes(db) | {x.get("code") for x in bundle.skills}
    for x in bundle.levels:
        if not x.get("code") or not x.get("name"):
            problems.append(f"level {x.get('code')!r}: code and name are required")
    for x in bundle.subjects:
        if not x.get("code") or not x.get("name"):
            problems.append(f"subject {x.get('code')!r}: code and name are required")
    for x in bundle.skills:
        code = x.get("code")
        if not code or not x.get("name"):
            problems.append(f"skill {code!r}: code and name are required")
            continue
        if x.get("subject") not in subjects:
            problems.append(f"skill {code}: unknown subject {x.get('subject')!r}")
        if x.get("level") not in levels:
            problems.append(f"skill {code}: unknown level {x.get('level')!r}")
        for p in x.get("prerequisites", []):
            if p not in skills:
                problems.append(f"skill {code}: unknown prerequisite {p!r}")
            if p == code:
                problems.append(f"skill {code}: cannot be its own prerequisite")
    # cycle check over the bundle's prerequisites
    graph = {x["code"]: list(x.get("prerequisites", [])) for x in bundle.skills if x.get("code")}
    state: dict[str, int] = {}

    def visit(n: str, trail: list[str]) -> None:
        if state.get(n) == 2 or n not in graph:
            return
        if state.get(n) == 1:
            problems.append("prerequisite cycle: " + " -> ".join([*trail, n]))
            return
        state[n] = 1
        for m in graph[n]:
            visit(m, [*trail, n])
        state[n] = 2

    for n in graph:
        visit(n, [])
    for a in bundle.activities:
        for p in validate_activity(finalize(a), skills, levels):
            problems.append(f"activity {a.get('slug')}: {p}")
        existing = db.scalar(select(Activity.id).where(Activity.slug == a.get("slug")))
        for p in asset_problems(db, a, existing):
            problems.append(f"activity {a.get('slug')}: {p}")
        if a.get("subject") not in subjects:
            problems.append(f"activity {a.get('slug')}: unknown subject {a.get('subject')!r}")
        for i in a.get("interests", []):
            if i not in interests:
                problems.append(f"activity {a.get('slug')}: unknown interest {i!r}")
    return problems, skills, levels


def import_bundle(db: Session, bundle: BundleIn) -> BundleReport:
    """Additive, idempotent import. Nothing is deleted. Activities arrive as drafts unless auto_publish is set."""
    created = dict.fromkeys(ENTITIES, 0)
    updated = dict.fromkeys(ENTITIES, 0)
    unchanged = dict.fromkeys(ENTITIES, 0)
    problems, _skills, levels = _check_taxonomy(bundle, db)
    if problems:
        return BundleReport(
            dry_run=bundle.dry_run,
            ok=False,
            problems=problems[:200],
            created=created,
            updated=updated,
            unchanged=unchanged,
        )

    nested = db.begin_nested()
    try:

        def upsert_simple(
            model, key_field: str, rows: list[dict], fields: list[str], counter: str, defaults: dict | None = None
        ) -> None:
            for i, row in enumerate(rows):
                values = {f: row.get(f, (defaults or {}).get(f)) for f in fields}
                if "sort_order" in fields and values.get("sort_order") is None:
                    values["sort_order"] = levels.index(row["code"]) + 1
                if "display_order" in fields and values.get("display_order") is None:
                    values["display_order"] = i + 1
                obj = db.get(model, row[key_field])
                if obj is None:
                    db.add(model(**{key_field: row[key_field], **values}))
                    created[counter] += 1
                elif any(getattr(obj, f) != v for f, v in values.items()):
                    for f, v in values.items():
                        setattr(obj, f, v)
                    updated[counter] += 1
                else:
                    unchanged[counter] += 1

        upsert_simple(Level, "code", bundle.levels, ["name", "indicative_age", "description", "sort_order"], "levels")
        upsert_simple(Subject, "code", bundle.subjects, ["name", "display_order", "scope"], "subjects")
        upsert_simple(Interest, "code", bundle.interests, ["name"], "interests")
        db.flush()

        for s in bundle.skills:
            obj = db.get(Skill, s["code"])
            values = {
                "subject_code": s["subject"],
                "level_code": s["level"],
                "name": s["name"],
                "typical_evidence": list(s.get("typical_evidence", [])),
            }
            if obj is None:
                db.add(Skill(code=s["code"], **values))
                created["skills"] += 1
            elif any(getattr(obj, f) != v for f, v in values.items()):
                for f, v in values.items():
                    setattr(obj, f, v)
                updated["skills"] += 1
            else:
                unchanged["skills"] += 1
        db.flush()
        for s in bundle.skills:
            current = set(
                db.scalars(select(SkillPrerequisite.prerequisite_code).where(SkillPrerequisite.skill_code == s["code"]))
            )
            wanted = set(s.get("prerequisites", []))
            if current != wanted:
                db.execute(delete(SkillPrerequisite).where(SkillPrerequisite.skill_code == s["code"]))
                for p in sorted(wanted):
                    db.add(SkillPrerequisite(skill_code=s["code"], prerequisite_code=p))
        db.flush()

        for d in map(finalize, bundle.activities):
            a = db.scalar(select(Activity).where(Activity.slug == d["slug"]))
            if a is None:
                a = Activity(
                    slug=d["slug"],
                    title=d["title"],
                    subject_code=d["subject"],
                    level_from=d["level_from"],
                    level_to=d["level_to"],
                    duration_min=d["duration_min"],
                    definition=d,
                    status="draft",
                    version=1,
                    bundle_version=bundle.version,
                )
                apply_definition(a, d)
                db.add(a)
                db.flush()
                sync_activity_skills(db, a, d["skills"])
                if bundle.auto_publish:
                    publish(db, a)
                created["activities"] += 1
            elif a.definition != d:
                update_definition(db, a, d)
                a.bundle_version = bundle.version
                if bundle.auto_publish and a.status == "draft":
                    publish(db, a)
                updated["activities"] += 1
            elif bundle.auto_publish and a.status == "draft":
                publish(db, a)
                updated["activities"] += 1
            else:
                unchanged["activities"] += 1
        db.flush()
        if bundle.dry_run:
            nested.rollback()
        else:
            nested.commit()
    except Exception:
        nested.rollback()
        raise
    return BundleReport(
        dry_run=bundle.dry_run, ok=True, problems=[], created=created, updated=updated, unchanged=unchanged
    )
