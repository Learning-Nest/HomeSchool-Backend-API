"""Results and practice: grouping, best-of-attempts, trends, access, and the practice limits."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.services.progress import week_start_of
from tests.helpers import complete, correct_answers

KOLKATA = ZoneInfo("Asia/Kolkata")
MATHS_A, MATHS_B, ENGLISH = "count-the-dogs", "shape-hunt", "story-retell-in-3-pictures"
WITH_REVIEW = "kitchen-counting"  # one auto step and one parent checklist step
ONLY_REVIEW = "set-the-table"  # two parent checklist steps


def utcnow():
    return datetime.now(UTC)


def wrong(slug):
    return correct_answers(slug, wrong=set(correct_answers(slug)))


def done(parent, headers, child, activities, slug, *, right=True, at=None, **kw):
    r = complete(
        parent.client,
        headers,
        child["id"],
        activities[slug],
        None if right else wrong(slug),
        occurred_at=at or utcnow(),
        **kw,
    )
    assert r.status_code == 200, r.text
    return r.json()


def summary(parent, child, headers=None, **params):
    r = parent.get(f"/v1/children/{child['id']}/results/summary", params=params, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def set_policy(database, **cols):
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with database["admin"].begin() as conn:
        conn.execute(
            text(f"UPDATE content.release_policies SET {sets} WHERE scope_board = '' AND scope_grade = ''"), cols
        )


@pytest.fixture
def kid(parent):
    child = parent.add_child()
    return child, parent.child_headers(child["id"])


@pytest.mark.parametrize("group_by", ["subject", "week", "subject_week", "month", "lesson", "skill"])
def test_an_empty_history_gives_empty_buckets(parent, kid, group_by):
    child, _ = kid
    out = summary(parent, child, group_by=group_by)
    assert out["buckets"] == [] and out["group_by"] == group_by and out["pass_score"] == 0.6


def test_practising_again_raises_the_subject_score(parent, kid, activities):
    child, headers = kid
    now = utcnow()
    done(parent, headers, child, activities, MATHS_A, right=False, at=now - timedelta(hours=3))
    out = summary(parent, child, group_by="subject")
    (maths,) = out["buckets"]
    assert maths["key"] == "MAT" and maths["activities"] == 1 and maths["attempts"] == 1
    assert maths["avg_best_score"] < 0.6 and maths["needs_attention"] == 1

    done(parent, headers, child, activities, MATHS_A, right=True, at=now - timedelta(hours=2))
    (maths,) = summary(parent, child, group_by="subject")["buckets"]
    assert maths["activities"] == 1 and maths["attempts"] == 2  # same activity, so one in the mean
    assert maths["avg_best_score"] == 1.0 and maths["avg_latest_score"] == 1.0 and maths["needs_attention"] == 0

    (row,) = parent.get(f"/v1/children/{child['id']}/results/activities").json()
    assert row["attempts"] == 2 and row["best_score"] == 1.0 and row["latest_score"] == 1.0
    assert row["first_score"] < 0.6 and row["status"] == "passed" and not row["needs_attention"]

    att = parent.get(f"/v1/children/{child['id']}/results/activities/{activities[MATHS_A]['id']}").json()
    assert [a["score"] for a in att["attempts"]] == [1.0, att["first_score"]]  # newest first
    assert att["best_score"] == 1.0 and att["first_score"] < 0.6


def test_subjects_are_kept_apart(parent, kid, activities):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A)
    done(parent, headers, child, activities, MATHS_B, right=False)
    done(parent, headers, child, activities, ENGLISH)
    by_subject = {b["key"]: b for b in summary(parent, child, group_by="subject")["buckets"]}
    assert set(by_subject) == {"MAT", "ENG"}
    assert by_subject["MAT"]["activities"] == 2 and by_subject["MAT"]["needs_attention"] == 1
    assert 0 < by_subject["MAT"]["avg_best_score"] < 1 and by_subject["ENG"]["avg_best_score"] == 1.0
    assert by_subject["MAT"]["label"] and by_subject["MAT"]["label"] != "MAT"  # the subject's name
    only_maths = parent.get(f"/v1/children/{child['id']}/results/activities", params={"subject": "MAT"}).json()
    assert {r["activity"]["slug"] for r in only_maths} == {MATHS_A, MATHS_B}


def test_weeks_split_at_monday_midnight_in_the_family_time_zone(parent, kid, activities):
    child, headers = kid
    now = utcnow()
    this_monday = datetime.combine(week_start_of(now.astimezone(KOLKATA).date()), datetime.min.time(), KOLKATA)
    last_sunday_night = this_monday - timedelta(minutes=1)
    done(parent, headers, child, activities, MATHS_A, right=False, at=last_sunday_night)
    done(parent, headers, child, activities, MATHS_A, right=True, at=now)

    weeks = summary(parent, child, group_by="week")["buckets"]
    assert [b["week_start"] for b in weeks] == [
        this_monday.date().isoformat(),
        (this_monday.date() - timedelta(days=7)).isoformat(),
    ]  # newest first
    newer, older = weeks
    assert newer["avg_best_score"] == 1.0 and older["avg_best_score"] < 0.6
    assert newer["previous_score"] == older["avg_best_score"] and newer["trend"] == "up"
    assert older["previous_score"] is None and older["trend"] is None

    sw = summary(parent, child, group_by="subject_week", subject="MAT")["buckets"]
    assert [(b["subject_code"], b["week_start"]) for b in sw] == [("MAT", w["week_start"]) for w in weeks]
    assert sw[0]["trend"] == "up"

    in_last_week = parent.get(
        f"/v1/children/{child['id']}/results/activities",
        params={"week_start": older["week_start"], "subject": "MAT"},
    ).json()
    assert len(in_last_week) == 1 and in_last_week[0]["attempts"] == 1 and in_last_week[0]["best_score"] < 0.6


def test_month_lesson_and_skill_groupings(parent, kid, activities):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A)
    month = summary(parent, child, group_by="month")["buckets"]
    assert len(month) == 1 and month[0]["key"] == month[0]["month"] and len(month[0]["month"]) == 7
    lesson = summary(parent, child, group_by="lesson")["buckets"]
    assert [b["key"] for b in lesson] == ["other"]  # done outside any textbook path
    skills = summary(parent, child, group_by="skill")["buckets"]
    assert skills and all(b["skill_code"] == b["key"] and b["label"] for b in skills)
    assert all(b["avg_best_score"] == 1.0 for b in skills)


def test_a_bad_range_is_refused(parent, kid):
    child, _ = kid
    r = parent.get(f"/v1/children/{child['id']}/results/summary", params={"from": "2026-11-05", "to": "2026-11-01"})
    assert r.status_code == 422
    r = parent.get(f"/v1/children/{child['id']}/results/summary", params={"from": "2020-01-01", "to": "2026-11-01"})
    assert r.status_code == 422


def test_unscored_and_waiting_review_activities_are_counted_not_hidden(parent, kid, activities):
    child, headers = kid
    s = done(parent, headers, child, activities, WITH_REVIEW)
    assert s["result"]["needs_review"], "the checklist step is waiting for the parent"
    (row,) = parent.get(f"/v1/children/{child['id']}/results/activities").json()
    assert row["status"] == "waiting_review" and row["waiting_review"] == 1 and row["needs_attention"]
    (b,) = summary(parent, child, group_by="subject")["buckets"]
    assert b["waiting_review"] == 1 and b["activities"] == 1

    # the parent rates the checklist step: the reviewed score joins the auto score
    step = s["result"]["needs_review"][0]
    r = parent.post(f"/v1/sessions/{s['id']}/review", json={"ratings": {step: "independent"}})
    assert r.status_code == 200, r.text
    att = parent.get(f"/v1/children/{child['id']}/results/activities/{activities[WITH_REVIEW]['id']}").json()
    (a,) = att["attempts"]
    assert a["needs_review"] == [] and a["reviewed_score"] == 1.0 and a["score"] == 1.0
    (row,) = parent.get(f"/v1/children/{child['id']}/results/activities").json()
    assert row["status"] == "passed" and row["waiting_review"] == 0

    only = done(parent, headers, child, activities, ONLY_REVIEW)
    assert only["result"]["score"] is None  # nothing auto-scored: shown, but left out of the mean
    rows = {r["activity"]["slug"]: r for r in parent.get(f"/v1/children/{child['id']}/results/activities").json()}
    assert rows[ONLY_REVIEW]["status"] == "waiting_review" and rows[ONLY_REVIEW]["best_score"] is None
    r = parent.post(
        f"/v1/sessions/{only['id']}/review", json={"ratings": {i: "trying" for i in only["result"]["needs_review"]}}
    )
    assert r.status_code == 200
    rows = {r["activity"]["slug"]: r for r in parent.get(f"/v1/children/{child['id']}/results/activities").json()}
    assert rows[ONLY_REVIEW]["best_score"] == 0.3 and rows[ONLY_REVIEW]["status"] == "attempted"


def test_the_attention_filter_lists_what_needs_work(parent, kid, activities):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A)
    done(parent, headers, child, activities, MATHS_B, right=False)
    rows = parent.get(f"/v1/children/{child['id']}/results/activities", params={"attention": True}).json()
    assert [r["activity"]["slug"] for r in rows] == [MATHS_B]


def test_a_child_reads_their_own_results_only(parent, kid, activities, make_parent):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A)
    out = summary(parent, child, headers=headers, group_by="subject")
    assert out["buckets"][0]["key"] == "MAT"
    assert parent.get(f"/v1/children/{child['id']}/results/activities", headers=headers).status_code == 200

    sibling = parent.add_child("Mira")
    assert parent.get(f"/v1/children/{sibling['id']}/results/summary", headers=headers).status_code == 404
    stranger = make_parent()
    assert stranger.get(f"/v1/children/{child['id']}/results/summary").status_code == 404
    assert stranger.get(f"/v1/children/{child['id']}/results/activities").status_code == 404


def test_attempt_detail_shows_correct_answers_to_a_parent_only(parent, kid, activities, make_parent):
    child, headers = kid
    s = done(parent, headers, child, activities, MATHS_A, right=False)
    url = f"/v1/children/{child['id']}/results/attempts/{s['id']}"
    d = parent.get(url).json()
    assert d["session_id"] == s["id"] and d["score"] < 0.6 and d["activity"]["slug"] == MATHS_A
    scored = [e for e in d["exercises"] if e["scored"]]
    assert scored and all(e["correct"] is False and e["answer"] is not None for e in scored)
    assert all(e["correct_answer"] is not None for e in scored)
    assert all(e["skills"] for e in scored)
    choice = next(e for e in scored if e["type"] == "single_choice")
    assert choice["answer"] not in ("o1", "o2", "o3") and choice["correct_answer"] == ["3"]  # words, not option ids

    c = parent.get(url, headers=headers).json()
    assert [e["id"] for e in c["exercises"]] == [e["id"] for e in d["exercises"]]
    assert all(e["correct_answer"] is None for e in c["exercises"])  # a child token never sees the key
    assert all(e["correct"] is False for e in c["exercises"] if e["scored"])

    assert make_parent().get(url).status_code == 404
    other = parent.add_child("Mira")
    assert parent.get(f"/v1/children/{other['id']}/results/attempts/{s['id']}").status_code == 404


def test_attempt_detail_marks_checklist_steps_awaiting_review(parent, kid, activities):
    child, headers = kid
    s = done(parent, headers, child, activities, WITH_REVIEW)
    d = parent.get(f"/v1/children/{child['id']}/results/attempts/{s['id']}").json()
    waiting = [e for e in d["exercises"] if e["needs_review"]]
    assert [e["id"] for e in waiting] == s["result"]["needs_review"] and waiting[0]["rating"] is None
    parent.post(f"/v1/sessions/{s['id']}/review", json={"ratings": {waiting[0]["id"]: "with_help"}})
    d = parent.get(f"/v1/children/{child['id']}/results/attempts/{s['id']}").json()
    assert not any(e["needs_review"] for e in d["exercises"])
    assert [e["rating"] for e in d["exercises"] if e["id"] == waiting[0]["id"]] == ["with_help"]
    assert d["reviewed_score"] is not None and d["reviewed_score"] < d["score"] + 1


# ------------------------------------------------------------------------------------------------ practice
def practise(parent, child, slugs, activities, headers=None, **body):
    r = parent.post(
        f"/v1/children/{child['id']}/practice",
        json={"activity_ids": [activities[s]["id"] for s in slugs], **body},
        headers=headers or {},
    )
    assert r.status_code == 201, r.text
    return r.json()


def tomorrow():
    return (utcnow().astimezone(KOLKATA).date() + timedelta(days=1)).isoformat()


def test_practice_adds_revision_items_and_enforces_cooldown_and_duplicates(parent, kid, activities):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A, right=False)
    # finished today: the cooldown (one day) means it can be practised from tomorrow
    out = practise(parent, child, [MATHS_A], activities)
    assert out["items"] == [] and out["skipped"][0]["reason"] == "cooldown"

    out = practise(parent, child, [MATHS_A], activities, scheduled_date=tomorrow())
    (item,) = out["items"]
    assert item["source"] == "revision" and item["activity"]["slug"] == MATHS_A and item["scheduled_date"] == tomorrow()
    assert out["skipped"] == []

    again = practise(parent, child, [MATHS_A], activities, scheduled_date=tomorrow())
    assert again["items"] == [] and again["skipped"][0]["reason"] == "already_open"

    plan = parent.get(f"/v1/children/{child['id']}/plan", params={"week_start": tomorrow()}).json()
    assert any(i["source"] == "revision" for i in plan["items"])
    assert parent.delete(f"/v1/plan-items/{item['id']}").status_code == 204  # a parent can take it back


def test_practice_is_limited_to_what_was_done_or_is_related(parent, kid, activities):
    child, _ = kid  # has done nothing yet
    out = practise(parent, child, [MATHS_A], activities)
    assert out["items"] == [] and out["skipped"][0]["reason"] == "not_allowed"
    r = parent.post(
        f"/v1/children/{child['id']}/practice",
        json={"activity_ids": ["00000000-0000-4000-8000-000000000000"]},
    )
    assert r.status_code == 201 and r.json()["skipped"][0]["reason"] == "not_published"


def test_dates_for_practice_are_checked(parent, kid, activities):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A, right=False)
    ids = [activities[MATHS_A]["id"]]
    past = (utcnow().astimezone(KOLKATA).date() - timedelta(days=1)).isoformat()
    assert (
        parent.post(
            f"/v1/children/{child['id']}/practice", json={"activity_ids": ids, "scheduled_date": past}
        ).status_code
        == 422
    )
    r = parent.post(
        f"/v1/children/{child['id']}/practice",
        json={"activity_ids": ids, "scheduled_date": tomorrow()},
        headers=headers,
    )
    assert r.status_code == 422  # a child practises today only


def test_practice_has_an_open_cap(parent, kid, activities, database):
    child, headers = kid
    set_policy(database, revision_open_cap=1)
    for slug in (MATHS_A, MATHS_B):
        done(parent, headers, child, activities, slug, right=False)
    out = practise(parent, child, [MATHS_A, MATHS_B], activities, scheduled_date=tomorrow())
    assert len(out["items"]) == 1 and [s["reason"] for s in out["skipped"]] == ["cap_reached"]
    assert out["items"][0]["activity"]["slug"] == MATHS_A


def test_a_child_has_a_daily_practice_limit(parent, kid, activities, database):
    child, headers = kid
    set_policy(database, child_daily_practice_limit=1)
    old = utcnow() - timedelta(days=2)
    for slug in (MATHS_A, MATHS_B):
        done(parent, headers, child, activities, slug, right=False, at=old)
    out = practise(parent, child, [MATHS_A, MATHS_B], activities, headers=headers)
    assert len(out["items"]) == 1 and [s["reason"] for s in out["skipped"]] == ["daily_limit"]
    assert out["items"][0]["scheduled_date"] == utcnow().astimezone(KOLKATA).date().isoformat()
    # a parent is not held to the child's limit
    more = practise(parent, child, [MATHS_B], activities)
    assert len(more["items"]) == 1


def test_suggestions_start_with_what_scored_below_the_pass_mark(parent, kid, activities):
    child, headers = kid
    done(parent, headers, child, activities, MATHS_A)
    done(parent, headers, child, activities, MATHS_B, right=False)
    sug = parent.get(f"/v1/children/{child['id']}/practice/suggestions").json()
    assert sug and sug[0]["activity"]["slug"] == MATHS_B and sug[0]["reason"] == "below_pass_mark"
    assert sug[0]["best_score"] < 0.6 and sug[0]["open_in_plan"] is False
    assert len({s["activity"]["id"] for s in sug}) == len(sug)

    practise(parent, child, [MATHS_B], activities, scheduled_date=tomorrow())
    sug = parent.get(f"/v1/children/{child['id']}/practice/suggestions", params={"limit": 1}).json()
    assert len(sug) == 1 and sug[0]["open_in_plan"] is True
    assert parent.get(f"/v1/children/{child['id']}/practice/suggestions", headers=headers).status_code == 200


def test_a_session_started_from_a_practice_item_completes_it(parent, kid, activities):
    child, headers = kid
    old = utcnow() - timedelta(days=2)
    done(parent, headers, child, activities, MATHS_A, right=False, at=old)
    (item,) = practise(parent, child, [MATHS_A], activities)["items"]
    done(parent, headers, child, activities, MATHS_A, right=True, plan_item_id=item["id"])
    today = parent.get(f"/v1/children/{child['id']}/today").json()
    assert [(i["source"], i["status"]) for i in today] == [("revision", "completed")]
    (row,) = parent.get(f"/v1/children/{child['id']}/results/activities").json()
    assert row["attempts"] == 2 and row["best_score"] == 1.0 and row["first_score"] < 0.6
