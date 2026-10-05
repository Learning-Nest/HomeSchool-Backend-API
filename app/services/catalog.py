"""Curriculum and activity catalogue writes: bundle import, definition updates, publishing and version snapshots."""

from __future__ import annotations

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


def publish(db: Session, a: Activity) -> None:
    """Validates against the live taxonomy, then marks the activity published and snapshots its version."""
    problems = validate_activity(a.definition, known_skill_codes(db), level_order(db))
    if problems:
        raise ApiError(422, "content_invalid", "The activity cannot be published.", {"problems": problems})
    a.status = "published"
    a.published_at = a.published_at or now()
    snapshot(db, a)


def update_definition(db: Session, a: Activity, definition: dict[str, Any]) -> None:
    """Replace the definition. Slug is immutable. A published activity gets a new version (old sessions keep the old one)."""
    if definition.get("slug") != a.slug:
        raise ApiError(
            422,
            "content_invalid",
            "The slug cannot be changed.",
            {"problems": ["slug: must equal the activity's slug"]},
        )
    definition = finalize(definition)  # v2: the top-level skills are derived from the exercises
    problems = validate_activity(definition, known_skill_codes(db), level_order(db))
    if problems:
        raise ApiError(422, "content_invalid", "The activity failed validation.", {"problems": problems})
    if definition == a.definition:
        return
    apply_definition(a, definition)
    sync_activity_skills(db, a, definition["skills"])
    if a.status == "published":
        a.version += 1
        snapshot(db, a)


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
