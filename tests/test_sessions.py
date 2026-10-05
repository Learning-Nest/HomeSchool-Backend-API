import json
import uuid
from datetime import date, timedelta

from sqlalchemy import text

from app.security import now
from tests.helpers import DEFS, complete, correct_answers, start


def test_start_is_idempotent_and_hides_answer_keys(parent, activities):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    op = str(uuid.uuid4())
    r1 = start(parent.client, parent.headers, kid["id"], a["id"], op=op)
    r2 = start(parent.client, parent.headers, kid["id"], a["id"], op=op)
    assert r1.status_code == 201 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
    body = json.dumps(r1.json())
    assert '"correct"' not in body and '"answer"' not in body and r1.json()["status"] == "in_progress"
    assert r1.json()["activity_definition"]["steps"]


def test_full_session_flow_scores_on_the_server_and_builds_mastery(parent, activities):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    steps = [s for s in DEFS["count-the-dogs"]["steps"] if s["type"] == "single_choice"]
    s = start(parent.client, parent.headers, kid["id"], a["id"]).json()
    first = correct_answers("count-the-dogs")
    half = dict(list(first.items())[:2])
    r = parent.put(f"/v1/sessions/{s['id']}/autosave", json={"answers": half, "hints_used": 0, "parent_assist": False})
    assert r.status_code == 200 and r.json()["answers"] == half
    r = parent.post(f"/v1/sessions/{s['id']}/submit", json={"answers": first, "duration_sec": 240})
    assert r.status_code == 200
    res = r.json()["result"]
    assert r.json()["status"] == "submitted" and res["score"] == 1.0 and res["scored_steps"] == len(steps)
    assert res["independence"] == "independent" and res["skills"] == ["MAT.NUM.COUNT20"]
    m = {x["skill_code"]: x for x in parent.get(f"/v1/children/{kid['id']}/mastery").json()}["MAT.NUM.COUNT20"]
    # perfect answers on ONE day: high score and confidence, but not "secure" (needs two distinct days)
    assert (
        m["score"] == 1.0
        and m["evidence_count"] == len(steps)
        and m["distinct_days"] == 1
        and m["status"] == "developing"
    )
    # answering again cannot change anything after submission
    assert parent.put(f"/v1/sessions/{s['id']}/autosave", json={"answers": {}}).status_code == 409


def test_submit_is_idempotent(parent, activities, database):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    s = start(parent.client, parent.headers, kid["id"], a["id"]).json()
    body = {"answers": correct_answers("count-the-dogs"), "duration_sec": 100}
    r1 = parent.post(f"/v1/sessions/{s['id']}/submit", json=body)
    r2 = parent.post(
        f"/v1/sessions/{s['id']}/submit", json={**body, "answers": correct_answers("count-the-dogs", wrong={"s2"})}
    )
    assert r1.json()["result"] == r2.json()["result"] and r2.json()["version"] == r1.json()["version"]
    with database["admin"].connect() as c:
        assert c.execute(text("select count(*) from skill_evidence")).scalar() == 5


def test_hints_and_parent_help_lower_the_weight_of_evidence(parent, activities, database):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    assert complete(parent.client, parent.headers, kid["id"], a, hints=1).json()["result"]["independence"] == "prompted"
    with database["admin"].connect() as c:
        assert {tuple(r) for r in c.execute(text("select independence, weight from skill_evidence"))} == {
            ("prompted", 0.7)
        }
        c.execute(text("select 1"))
    kid2 = parent.add_child("K2", "L1")
    assert (
        complete(parent.client, parent.headers, kid2["id"], a, hints=3, assist=True).json()["result"]["independence"]
        == "assisted"
    )
    with database["admin"].connect() as c:
        assert ("assisted", 0.5) in {
            tuple(r) for r in c.execute(text("select independence, weight from skill_evidence"))
        }


def test_wrong_answers_give_emerging(parent, activities):
    kid = parent.add_child("K", "L1")
    steps = {s["id"] for s in DEFS["count-the-dogs"]["steps"] if s["type"] == "single_choice"}
    r = complete(
        parent.client,
        parent.headers,
        kid["id"],
        activities["count-the-dogs"],
        correct_answers("count-the-dogs", wrong=steps),
    )
    assert r.json()["result"]["score"] == 0.0
    m = parent.get(f"/v1/children/{kid['id']}/mastery").json()[0]
    assert m["status"] == "emerging" and m["score"] == 0.0


def test_unanswered_steps_produce_no_evidence(parent, activities, database):
    kid = parent.add_child("K", "L1")
    r = complete(parent.client, parent.headers, kid["id"], activities["count-the-dogs"], {"s2": "o2"})
    assert r.json()["result"]["scored_steps"] == 1
    with database["admin"].connect() as c:
        assert c.execute(text("select count(*) from skill_evidence")).scalar() == 1


def test_a_child_token_can_play_but_not_review(parent, activities):
    kid = parent.add_child("K", "L2")
    ch = parent.child_headers(kid["id"])
    a = activities["kitchen-counting"]
    r = complete(parent.client, ch, kid["id"], a)
    assert r.status_code == 200 and r.json()["result"]["needs_review"] == ["s3"]
    sid = r.json()["id"]
    assert (
        parent.client.post(
            f"/v1/sessions/{sid}/review", headers=ch, json={"ratings": {"s3": "independent"}}
        ).status_code
        == 403
    )
    assert parent.client.get(f"/v1/sessions/{sid}", headers=ch).status_code == 200
    # another child (even a sibling) cannot see it
    sib = parent.add_child("Sib", "L2")
    assert parent.client.get(f"/v1/sessions/{sid}", headers=parent.child_headers(sib["id"])).status_code == 404


def test_parent_review_adds_evidence_and_can_be_repeated(parent, activities, database):
    kid = parent.add_child("K", "L2")
    r = complete(parent.client, parent.headers, kid["id"], activities["kitchen-counting"])
    sid = r.json()["id"]
    assert (
        parent.post(f"/v1/sessions/{sid}/review", json={"ratings": {"s2": "independent"}}).status_code == 422
    )  # not a checklist step
    r = parent.post(f"/v1/sessions/{sid}/review", json={"ratings": {"s3": "with_help"}})
    assert (
        r.status_code == 200
        and r.json()["result"]["needs_review"] == []
        and r.json()["result"]["reviewed"] == {"s3": "with_help"}
    )
    skill = next(s for s in DEFS["kitchen-counting"]["steps"] if s["id"] == "s3")["skills"][0]["code"]
    m = {x["skill_code"]: x for x in parent.get(f"/v1/children/{kid['id']}/mastery").json()}
    # s2 now credits only MAT.NUM.COUNT20, so the review is the one piece of evidence for this skill
    assert m[skill]["score"] == 0.6 and m[skill]["evidence_count"] == 1
    parent.post(f"/v1/sessions/{sid}/review", json={"ratings": {"s3": "independent"}})  # the parent changes their mind
    m = {x["skill_code"]: x for x in parent.get(f"/v1/children/{kid['id']}/mastery").json()}
    assert m[skill]["score"] == 1.0 and m[skill]["evidence_count"] == 1  # replaced, not duplicated


def test_review_requires_a_submitted_session(parent, activities):
    kid = parent.add_child("K", "L2")
    s = start(parent.client, parent.headers, kid["id"], activities["kitchen-counting"]["id"]).json()
    assert parent.post(f"/v1/sessions/{s['id']}/review", json={"ratings": {"s3": "independent"}}).status_code == 409


def test_parent_assist_can_be_corrected_in_review(parent, activities, database):
    kid = parent.add_child("K", "L1")
    r = complete(parent.client, parent.headers, kid["id"], activities["count-the-dogs"])
    assert r.json()["parent_assist"] is False
    # the count-the-dogs activity has no checklist step, so the review only changes the assistance flag
    r = parent.post(f"/v1/sessions/{r.json()['id']}/review", json={"ratings": {}, "parent_assist": True})
    assert r.status_code == 200 and r.json()["parent_assist"] is True
    with database["admin"].connect() as c:
        assert {tuple(x) for x in c.execute(text("select independence, weight from skill_evidence"))} == {
            ("assisted", 0.5)
        }


def test_plan_item_lifecycle(parent, activities):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    today = now().date()
    item = parent.post(
        f"/v1/children/{kid['id']}/plan/items", json={"activity_id": a["id"], "scheduled_date": today.isoformat()}
    )
    assert item.status_code == 201 and item.json()["status"] == "planned"
    iid = item.json()["id"]
    assert [i["id"] for i in parent.get(f"/v1/children/{kid['id']}/today").json()] == [iid]
    s = start(parent.client, parent.headers, kid["id"], a["id"], plan_item_id=iid)
    assert s.status_code == 201
    assert parent.get(f"/v1/children/{kid['id']}/today").json()[0]["status"] == "in_progress"
    parent.post(
        f"/v1/sessions/{s.json()['id']}/submit", json={"answers": correct_answers("count-the-dogs"), "duration_sec": 60}
    )
    got = parent.get(f"/v1/children/{kid['id']}/today").json()[0]
    assert got["status"] == "completed"
    assert parent.delete(f"/v1/plan-items/{iid}").status_code == 409  # completed items stay
    assert (
        parent.patch(f"/v1/plan-items/{iid}", json={"status": "skipped", "version": got["version"]}).status_code == 409
    )


def test_plan_item_moves_between_weeks_and_checks_versions(parent, activities):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    day = date(2030, 1, 9)  # a Wednesday
    item = parent.post(
        f"/v1/children/{kid['id']}/plan/items", json={"activity_id": a["id"], "scheduled_date": day.isoformat()}
    ).json()
    wk = parent.get(f"/v1/children/{kid['id']}/plan", params={"week_start": "2030-01-10"}).json()
    assert wk["week_start"] == "2030-01-07" and [i["id"] for i in wk["items"]] == [item["id"]]
    moved = parent.patch(f"/v1/plan-items/{item['id']}", json={"scheduled_date": "2030-01-17", "version": 1})
    assert moved.status_code == 200 and moved.json()["version"] == 2
    assert (
        parent.patch(f"/v1/plan-items/{item['id']}", json={"scheduled_date": "2030-01-18", "version": 1}).status_code
        == 409
    )
    assert parent.get(f"/v1/children/{kid['id']}/plan", params={"week_start": "2030-01-07"}).json()["items"] == []
    assert len(parent.get(f"/v1/children/{kid['id']}/plan", params={"week_start": "2030-01-14"}).json()["items"]) == 1
    assert parent.delete(f"/v1/plan-items/{item['id']}").status_code == 204


def test_offline_timestamps_are_accepted_only_within_a_week(parent, activities, database):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    c, h = parent.client, parent.headers
    assert complete(c, h, kid["id"], a, occurred_at=now() - timedelta(days=10)).status_code == 422
    assert complete(c, h, kid["id"], a, occurred_at=now() + timedelta(hours=1)).status_code == 422
    two_days = now() - timedelta(days=2)
    ok = complete(c, h, kid["id"], a, occurred_at=two_days)
    assert ok.status_code == 200
    with database["admin"].connect() as conn:
        stamp = conn.execute(text("select occurred_at from skill_evidence limit 1")).scalar()
    assert abs((stamp - two_days).total_seconds()) < 2  # evidence carries the time it happened, not the sync time
    naive = c.post(
        "/v1/sessions",
        headers=h,
        json={"child_id": kid["id"], "activity_id": a["id"], "client_op_id": str(uuid.uuid4())},
    ).json()
    assert (
        c.post(
            f"/v1/sessions/{naive['id']}/submit", headers=h, json={"answers": {}, "occurred_at": "2026-09-01T10:00:00"}
        ).status_code
        == 422
    )


def test_answer_validation(parent, activities):
    kid = parent.add_child("K", "L1")
    s = start(parent.client, parent.headers, kid["id"], activities["count-the-dogs"]["id"]).json()
    assert parent.put(f"/v1/sessions/{s['id']}/autosave", json={"answers": {"nope": "x"}}).status_code == 422
    assert parent.put(f"/v1/sessions/{s['id']}/autosave", json={"answers": {"s2": "x" * 70000}}).status_code == 422


def test_start_rules(parent, make_parent, activities, make_admin):
    kid = parent.add_child("K", "L1")
    a = activities["count-the-dogs"]
    other = make_parent()
    assert start(other.client, other.headers, kid["id"], a["id"]).status_code == 404  # someone else's child
    assert start(parent.client, parent.headers, kid["id"], str(uuid.uuid4())).status_code == 422  # unknown activity
    op = str(uuid.uuid4())
    kid2 = parent.add_child("K2", "L1")
    assert start(parent.client, parent.headers, kid["id"], a["id"], op=op).status_code == 201
    assert (
        start(parent.client, parent.headers, kid2["id"], a["id"], op=op).status_code == 409
    )  # op id reused for another child
    other_item = parent.post(
        f"/v1/children/{kid2['id']}/plan/items",
        json={"activity_id": a["id"], "scheduled_date": now().date().isoformat()},
    ).json()
    assert start(parent.client, parent.headers, kid["id"], a["id"], plan_item_id=other_item["id"]).status_code == 422


def test_sessions_keep_the_version_they_started_with(parent, make_admin, activities):
    """Editing a published activity creates version 2; a session already started keeps showing version 1."""
    kid = parent.add_child("K", "L1")
    admin = make_admin()
    a = activities["count-the-dogs"]
    s1 = start(parent.client, parent.headers, kid["id"], a["id"]).json()
    detail = admin.get(f"/v1/admin/activities/{a['id']}").json()
    new_def = json.loads(json.dumps(detail["definition"]))
    new_def["steps"][0]["config"]["caption"] = "EDITED STEP TEXT"
    r = admin.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": new_def})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    again = parent.get(f"/v1/sessions/{s1['id']}").json()
    assert "EDITED STEP TEXT" not in json.dumps(again["activity_definition"]) and again["activity"]["version"] == 2
    s2 = start(parent.client, parent.headers, kid["id"], a["id"]).json()
    assert "EDITED STEP TEXT" in json.dumps(s2["activity_definition"])
