"""Activity format v2: exercises own their skills; v1 documents keep working through an in-memory upgrade."""

import copy
import json

import pytest
from sqlalchemy import text

from app.services import content
from tests.helpers import BUNDLE, DEFS, V1_DEFS, correct_answers, start

KNOWN = {s["code"] for s in BUNDLE["skills"]}
OPTS = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}]


def v2_activity(slug="mixed-practice", **over):
    """One activity with five kinds of exercise: info, auto-scored (two skills, one weighted), disabled, and rated."""
    d = {
        "schema_version": 2,
        "slug": slug,
        "title": "Mixed practice",
        "subject": "MAT",
        "level_from": "L1",
        "level_to": "L2",
        "duration_min": 20,
        "skills": ["MAT.NUM.COUNT20", "MAT.NUM.ADD10", "MAT.NUM.COMPARE", "MAT.MEAS.UNITS"],
        "tags": ["demo"],
        "authoring": {"notes": "internal only", "educator_reviewed": True},
        "steps": [
            {"id": "intro", "type": "instruction", "title": "Intro", "prompt": "Let's count and add."},
            {
                "id": "q1",
                "type": "single_choice",
                "title": "Count",
                "prompt": "How many?",
                "config": {"options": copy.deepcopy(OPTS)},
                "key": {"correct": ["b"]},
                "scoring": {"mode": "auto"},
                "skills": [{"code": "MAT.NUM.COUNT20", "weight": 1}],
                "feedback": {"hints": ["Count slowly.", "Use your fingers."]},
            },
            {
                "id": "q2",
                "type": "numeric_input",
                "prompt": "3 + 4 = ?",
                "key": {"answer": 7},
                "scoring": {"mode": "auto"},
                "skills": [{"code": "MAT.NUM.ADD10", "weight": 0.5}, {"code": "MAT.NUM.COMPARE"}],
            },
            {
                "id": "old",
                "type": "single_choice",
                "enabled": False,
                "prompt": "Retired question",
                "config": {"options": copy.deepcopy(OPTS)},
                "key": {"correct": ["a"]},
                "skills": [{"code": "MAT.NUM.SUB10"}],
            },
            {
                "id": "chk",
                "type": "parent_checklist",
                "prompt": "Did they measure with a ruler?",
                "config": {"rating_options": ["trying", "with_help", "independent"]},
                "skills": [{"code": "MAT.MEAS.UNITS", "weight": 0.8}],
            },
        ],
    }
    d.update(over)
    return d


# ------------------------------------------------------------------------------------------------ pure functions
def test_v2_activity_is_valid_and_its_skills_are_derived():
    d = v2_activity()
    assert content.validate_activity(d, KNOWN) == []
    assert content.derive_skills(d) == ["MAT.NUM.COUNT20", "MAT.NUM.ADD10", "MAT.NUM.COMPARE", "MAT.MEAS.UNITS"]
    stale = v2_activity(skills=["MAT.NUM.COUNT20"])
    assert any("skills must equal" in p for p in content.validate_activity(stale, KNOWN))
    assert content.validate_activity(content.finalize(stale), KNOWN) == []  # the server fixes it on save
    assert "MAT.NUM.SUB10" not in content.finalize(stale)["skills"]  # a disabled exercise credits nothing


def test_every_v1_launch_activity_upgrades_cleanly_and_idempotently():
    for slug, a in V1_DEFS.items():
        up = content.normalize(a)
        assert up["schema_version"] == 2 and up["skills"] == a["skills"], slug
        assert content.normalize(up) == up, slug  # idempotent
        assert content.upgrade_step(up["steps"][0]) == up["steps"][0]
        assert content.validate_activity(content.finalize(up), KNOWN) == [], slug
        # the app sees exactly what it saw before (minus fields the v1 wire never needed)
        v1 = content.public_definition(a)["steps"]
        v2 = content.public_definition(up)["steps"]
        for s1, s2 in zip(v1, v2, strict=True):
            extra = {k: s1[k] for k in ("scored", "points", "skills") if k in s1}
            assert {k: v for k, v in s1.items() if k not in extra} == s2, (slug, s1["id"])


def test_v1_and_upgraded_steps_score_the_same():
    for slug, a in V1_DEFS.items():
        up = content.normalize(a)
        for wrong in (set(), {s["id"] for s in a["steps"]}):
            answers = correct_answers(slug, wrong=wrong)
            for s1, s2 in zip(a["steps"], up["steps"], strict=True):
                assert content.is_scored(s1) == content.is_scored(s2), (slug, s1["id"])
                assert content.score_step(s1, answers.get(s1["id"])) == content.score_step(s2, answers.get(s1["id"]))
                assert content.step_skills(s1, a["skills"]) == content.step_skills(s2, a["skills"])


def test_v1_scored_step_without_skills_counts_for_the_activity_skills():
    a = copy.deepcopy(V1_DEFS["count-the-dogs"])
    up = content.normalize(a)
    scored = [s for s in up["steps"] if content.is_scored(s)]
    assert scored and all([r["code"] for r in s["skills"]] == a["skills"] for s in scored)
    assert all("skills" not in s for s in up["steps"] if not content.produces_evidence(s))


def test_public_definition_of_v2_is_the_legacy_wire_shape_without_secrets():
    pub = content.public_definition(v2_activity())
    wire = json.dumps(pub)
    for secret in ('"key"', '"correct"', '"answer"', '"scoring"', "authoring", "schema_version", "Retired question"):
        assert secret not in wire
    assert [s["id"] for s in pub["steps"]] == ["intro", "q1", "q2", "chk"]  # the disabled exercise is gone
    intro, q1, q2, chk = pub["steps"]
    assert intro == {"id": "intro", "type": "instruction", "text": "Let's count and add."}
    assert q1 == {"id": "q1", "type": "single_choice", "prompt": "How many?", "options": OPTS, "hint": "Count slowly."}
    assert q2 == {"id": "q2", "type": "numeric_input", "prompt": "3 + 4 = ?"}
    assert chk["skill_code"] == "MAT.MEAS.UNITS" and chk["rating_options"] == ["trying", "with_help", "independent"]
    assert pub["skills"] and pub["slug"] == "mixed-practice"


def test_score_v2_steps_and_modes():
    d = v2_activity()
    q1, q2 = d["steps"][1], d["steps"][2]
    assert content.score_step(q1, "b") == 1.0 and content.score_step(q1, "a") == 0.0
    assert content.score_step(q2, 7) == 1.0 and content.score_step(q2, 8) == 0.0
    assert content.score_step({**q1, "scoring": {"mode": "none"}}, "b") is None
    assert content.score_step({**q1, "enabled": False}, "b") is None
    multi = {
        "id": "m",
        "type": "multi_choice",
        "config": {"options": copy.deepcopy(OPTS)},
        "key": {"correct": ["a", "b"]},
        "scoring": {"partial_credit": True},
    }
    assert content.score_step(multi, ["a"]) == 0.5
    assert content.score_step({**multi, "scoring": {}}, ["a"]) == 0.0
    assert content.needs_parent_review(d["steps"][4]) and not content.needs_parent_review(q1)
    muted = {**d["steps"][4], "scoring": {"mode": "none"}}
    assert not content.needs_parent_review(muted) and not content.produces_evidence(muted)


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda d: d["steps"][1]["key"].update(correct=["zzz"]), "unknown option"),
        (lambda d: d["steps"][1].pop("skills"), "needs at least one skill"),
        (lambda d: d["steps"][0].update(scoring={"mode": "auto"}), "not available for instruction"),
        (lambda d: d["steps"][1].update(scoring={"mode": "parent_rubric"}), "only for parent_checklist"),
        (lambda d: d["steps"][2]["skills"].append({"code": "MAT.NUM.ADD10"}), "twice"),
        (lambda d: d["steps"][2]["skills"].append({"code": "MAT.NOPE.NOPE"}), "unknown skill"),
        (lambda d: d["steps"][1]["config"]["options"].append({"id": "a", "label": "dup"}), "unique"),
        (lambda d: d["steps"][1].update(correct=["b"]), "correct"),  # answer keys belong in `key`
        (lambda d: d["steps"][1]["skills"][0].update(weight=0), "0"),
        (lambda d: d["steps"][1]["skills"][0].update(weight=1.5), "1.5"),
        (lambda d: d.update(schema_version=3), "unsupported version"),
        (lambda d: d["steps"][2].update(id="q1"), "unique"),
        (lambda d: d["steps"][1]["key"].update(correct=[]), "needs key.correct"),
        (lambda d: d["steps"][1]["feedback"]["hints"].extend(["3", "4"]), "hints"),
    ],
)
def test_v2_validation_catches_mistakes(mutate, expect):
    d = v2_activity()
    mutate(d)
    problems = content.validate_activity(d, KNOWN)
    assert problems and any(expect in p for p in problems), problems


def test_all_exercises_disabled_or_unscored_is_rejected():
    d = v2_activity()
    for s in d["steps"]:
        s["enabled"] = False
    assert any("enabled" in p for p in content.validate_activity(content.finalize(d), KNOWN))
    d = v2_activity()
    d["steps"] = [d["steps"][0]]
    assert any("no exercise gives evidence" in p for p in content.validate_activity(content.finalize(d), KNOWN))


def test_sequence_and_pairs_keys_are_checked_in_v2():
    d = v2_activity()
    d["steps"].append(
        {
            "id": "seq",
            "type": "sequence_order",
            "prompt": "Order",
            "config": {"items": [{"id": "1", "label": "1"}, {"id": "2", "label": "2"}]},
            "key": {"correct_order": ["1", "1"]},
            "skills": [{"code": "MAT.NUM.COUNT20"}],
        }
    )
    assert any("correct_order" in p for p in content.validate_activity(d, KNOWN))
    d["steps"][-1] = {
        "id": "mp",
        "type": "match_pairs",
        "prompt": "Match",
        "config": {"left": [{"id": "l", "label": "l"}, {"id": "l2", "label": "l2"}], "right": copy.deepcopy(OPTS[:2])},
        "key": {"pairs": [["l", "zz"]]},
        "skills": [{"code": "MAT.NUM.COUNT20"}],
    }
    assert any("unknown item" in p for p in content.validate_activity(d, KNOWN))


def test_vendored_v2_schema_matches_api_contracts_when_available():
    sibling = content.SCHEMA_V2_PATH.parent.parent.parent.parent / "api-contracts" / "schemas"
    sibling = sibling / "activity-content.v2.schema.json"
    if not sibling.exists():
        pytest.skip("api-contracts is not checked out next to this repo")
    assert sibling.read_text(encoding="utf-8") == content.SCHEMA_V2_PATH.read_text(encoding="utf-8"), (
        "run scripts/sync_contracts.py"
    )


# ------------------------------------------------------------------------------------------------ through the API
def _publish(admin, activity):
    body = {"version": "v2.test", "activities": [activity], "dry_run": False, "auto_publish": True}
    r = admin.post("/v1/admin/content/bundle", json=body)
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()


def _activity_id(admin, q):
    return admin.get("/v1/admin/activities", params={"q": q}).json()[0]["id"]


def test_import_finalizes_skills_and_is_idempotent(make_admin):
    admin = make_admin()
    r = _publish(admin, v2_activity(skills=["MAT.NUM.COUNT20"]))  # stale list: the server derives the real one
    assert r["created"]["activities"] == 1
    aid = _activity_id(admin, "mixed")
    detail = admin.get(f"/v1/admin/activities/{aid}").json()
    assert detail["skills"] == sorted(content.derive_skills(v2_activity()))
    assert detail["definition"]["skills"] == content.derive_skills(v2_activity())
    again = _publish(admin, v2_activity(skills=["MAT.NUM.COUNT20"]))
    assert again["unchanged"]["activities"] == 1 and again["updated"]["activities"] == 0


def test_a_bundle_with_a_bad_v2_activity_is_rejected(make_admin):
    bad = v2_activity()
    bad["steps"][1]["key"]["correct"] = ["nope"]
    r = (
        make_admin()
        .post("/v1/admin/content/bundle", json={"version": "x", "activities": [bad], "dry_run": False})
        .json()
    )
    assert r["ok"] is False and any("unknown option" in p for p in r["problems"])


def test_session_flow_credits_each_exercise_only_its_own_skills(make_admin, parent, database):
    admin = make_admin()
    _publish(admin, v2_activity())
    aid = _activity_id(admin, "mixed")
    kid = parent.add_child("K", "L1")
    s = start(parent.client, parent.headers, kid["id"], aid).json()
    # the app gets the legacy shape: no keys, no disabled exercise, hint flattened
    wire = json.dumps(s["activity_definition"])
    assert '"key"' not in wire and "Retired" not in wire and "internal only" not in wire
    assert [x["id"] for x in s["activity_definition"]["steps"]] == ["intro", "q1", "q2", "chk"]
    assert s["activity_definition"]["steps"][1]["hint"] == "Count slowly."

    r = parent.post(f"/v1/sessions/{s['id']}/submit", json={"answers": {"q1": "b", "q2": 9}, "duration_sec": 90})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["steps"] == {
        "q1": {"score": 1.0, "skills": ["MAT.NUM.COUNT20"]},
        "q2": {"score": 0.0, "skills": ["MAT.NUM.ADD10", "MAT.NUM.COMPARE"]},
    }
    assert res["score"] == 0.5 and res["scored_steps"] == 2 and res["needs_review"] == ["chk"]
    assert res["skills"] == ["MAT.NUM.ADD10", "MAT.NUM.COMPARE", "MAT.NUM.COUNT20"]
    with database["admin"].connect() as c:
        rows = {
            (r[0], r[1]): (r[2], r[3])
            for r in c.execute(text("select step_id, skill_code, value, weight from skill_evidence"))
        }
    # weight = base (1.0 for activities) x independence (1.0) x the exercise's weight for that skill
    assert rows == {
        ("q1", "MAT.NUM.COUNT20"): (1.0, 1.0),
        ("q2", "MAT.NUM.ADD10"): (0.0, 0.5),
        ("q2", "MAT.NUM.COMPARE"): (0.0, 1.0),
    }
    # the parent rates the checklist: only its own skill gets evidence, scaled by its weight
    r = parent.post(f"/v1/sessions/{s['id']}/review", json={"ratings": {"chk": "independent"}})
    assert r.status_code == 200 and r.json()["result"]["needs_review"] == []
    with database["admin"].connect() as c:
        got = c.execute(text("select skill_code, value, weight from skill_evidence where step_id = 'chk'")).all()
    assert [tuple(g) for g in got] == [("MAT.MEAS.UNITS", 1.0, 0.8)]


def test_answers_for_disabled_or_unknown_exercises_are_rejected(make_admin, parent):
    admin = make_admin()
    _publish(admin, v2_activity())
    kid = parent.add_child("K", "L1")
    s = start(parent.client, parent.headers, kid["id"], _activity_id(admin, "mixed")).json()
    assert parent.put(f"/v1/sessions/{s['id']}/autosave", json={"answers": {"old": "a"}}).status_code == 422
    assert parent.put(f"/v1/sessions/{s['id']}/autosave", json={"answers": {"q1": "a"}}).status_code == 200


def test_editing_a_published_v2_activity_creates_a_version_and_old_sessions_keep_theirs(make_admin, parent):
    admin = make_admin()
    _publish(admin, v2_activity())
    aid = _activity_id(admin, "mixed")
    kid = parent.add_child("K", "L1")
    old = start(parent.client, parent.headers, kid["id"], aid).json()
    d = admin.get(f"/v1/admin/activities/{aid}").json()["definition"]
    d["steps"][1]["key"]["correct"] = ["c"]  # the editor changes the answer key
    d["steps"][2]["skills"] = [{"code": "MAT.NUM.ADD10"}]  # ... and drops a skill from an exercise
    d["skills"] = ["whatever"]  # the editor cannot set the derived list: the server rewrites it
    r = admin.put(f"/v1/admin/activities/{aid}/definition", json={"definition": d})
    assert r.status_code == 200, r.text
    assert r.json()["definition"]["skills"] == ["MAT.NUM.COUNT20", "MAT.NUM.ADD10", "MAT.MEAS.UNITS"]
    assert r.json()["version"] == 2
    new = start(parent.client, parent.headers, kid["id"], aid).json()
    ok = parent.post(f"/v1/sessions/{old['id']}/submit", json={"answers": {"q1": "b"}})
    assert ok.json()["result"]["steps"]["q1"]["score"] == 1.0  # scored with the version the session started on
    ok2 = parent.post(f"/v1/sessions/{new['id']}/submit", json={"answers": {"q1": "b"}})
    assert ok2.json()["result"]["steps"]["q1"]["score"] == 0.0


def test_a_v1_and_a_v2_activity_live_side_by_side(make_admin, parent, activities):
    admin = make_admin()
    _publish(admin, v2_activity())
    kid = parent.add_child("K", "L1")
    dogs = activities["count-the-dogs"]
    s = start(parent.client, parent.headers, kid["id"], dogs["id"]).json()
    r = parent.post(f"/v1/sessions/{s['id']}/submit", json={"answers": correct_answers("count-the-dogs")})
    assert r.json()["result"]["score"] == 1.0 and r.json()["result"]["skills"] == ["MAT.NUM.COUNT20"]
    assert all(set(v) == {"score", "skills"} for v in r.json()["result"]["steps"].values())


# Exercises added on purpose after the migration (the migration itself adds none).
ADDED_AFTER_MIGRATION = {"weather-diary": {"s4"}}
# Whole activities written after the migration (they have no v1 original to compare with).
NEW_ACTIVITIES_AFTER_MIGRATION = {"shape-hunt"}


def test_the_migrated_launch_set_is_wire_identical_and_scores_the_same_as_v1():
    """The content migration must not change what the app receives or how an answer is scored."""
    assert set(DEFS) - NEW_ACTIVITIES_AFTER_MIGRATION == set(V1_DEFS)
    for slug, new in ((k, v) for k, v in DEFS.items() if k in V1_DEFS):
        old = V1_DEFS[slug]
        assert new["schema_version"] == 2 and content.validate_activity(new, KNOWN) == [], slug
        added = ADDED_AFTER_MIGRATION.get(slug, set())
        kept = [s for s in new["steps"] if s["id"] not in added]
        assert {s["id"] for s in new["steps"]} - {s["id"] for s in kept} == added, slug
        v1 = content.public_definition(old)["steps"]
        v2 = [s for s in content.public_definition(new)["steps"] if s["id"] not in added]
        for s1, s2 in zip(v1, v2, strict=True):
            assert {k: v for k, v in s1.items() if k not in ("scored", "points", "skills")} == s2, (slug, s1["id"])
        for wrong in (set(), {s["id"] for s in old["steps"]}):
            answers = correct_answers(slug, wrong=wrong)
            for s1, s2 in zip(old["steps"], kept, strict=True):
                assert content.score_step(s1, answers.get(s1["id"])) == content.score_step(s2, answers.get(s1["id"]))
        # skills may only shrink, except where an exercise was added on purpose
        assert set(new["skills"]) <= set(old["skills"]) or added


def test_weather_diary_rates_time_sequencing_through_a_parent_checklist():
    steps = {s["id"]: s for s in DEFS["weather-diary"]["steps"]}
    assert content.needs_parent_review(steps["s4"]) and not content.is_scored(steps["s4"])
    assert content.step_skills(steps["s4"], []) == ["SOC.TIME.SEQ"]
    assert DEFS["weather-diary"]["skills"] == ["SCI.ENV.WEATHER", "SOC.TIME.SEQ"]


def test_api_contracts_activity_v2_vectors_when_available():
    base = content.SCHEMA_V2_PATH.parent.parent.parent.parent / "api-contracts" / "test-vectors" / "activity-v2"
    if not base.exists():
        pytest.skip("api-contracts is not checked out next to this repo")
    stored = json.loads((base / "mixed-practice.stored.json").read_text(encoding="utf-8"))
    assert content.validate_activity(stored, KNOWN) == []
    assert content.public_definition(stored) == json.loads(
        (base / "mixed-practice.wire.json").read_text(encoding="utf-8")
    )
    for case in json.loads((base / "invalid-cases.json").read_text(encoding="utf-8")):
        problems = content.validate_activity(case["document"], KNOWN)
        assert any(case["expect_problem_contains"] in p for p in problems), (case["name"], problems)
