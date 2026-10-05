"""The parent's Activity library for one child: pick a subject, see the activities at that child's level, and see
which of them the child has already done or has planned."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from app.security import now

from .helpers import complete


def _today():
    return now().astimezone(ZoneInfo("Asia/Kolkata")).date()


def _library(parent, kid, **params):
    r = parent.get(f"/v1/children/{kid['id']}/library", params={"limit": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _plan(parent, kid, activity, day):
    r = parent.post(
        f"/v1/children/{kid['id']}/plan/items", json={"activity_id": activity["id"], "scheduled_date": day.isoformat()}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_library_shows_only_the_chosen_subject_at_the_childs_level(parent, activities):
    kid = parent.add_child("K", "L2")
    rows = _library(parent, kid, subject="MAT")
    assert rows, "the launch content has maths activities for L2"
    assert {r["subject_code"] for r in rows} == {"MAT"}
    assert all(r["level_from"] <= "L2" <= r["level_to"] for r in rows)
    # exactly what the plain catalogue returns for that subject and level
    expected = {
        a["slug"] for a in parent.get("/v1/activities", params={"subject": "MAT", "level": "L2", "limit": 100}).json()
    }
    assert {r["slug"] for r in rows} == expected
    # a different level gives a different (level-appropriate) list
    other = _library(parent, parent.add_child("K3", "L4"), subject="MAT")
    assert all(r["level_from"] <= "L4" <= r["level_to"] for r in other)


def test_library_marks_what_the_child_has_already_done(parent, activities):
    kid = parent.add_child("K", "L2")
    sibling = parent.add_child("Sib", "L2")
    done = activities["kitchen-counting"]
    complete(parent.client, parent.headers, kid["id"], done)

    rows = {r["slug"]: r for r in _library(parent, kid, subject=done["subject_code"])}
    assert rows["kitchen-counting"]["times_done"] == 1
    assert rows["kitchen-counting"]["last_done_at"] is not None
    assert all(r["times_done"] == 0 and r["last_done_at"] is None for s, r in rows.items() if s != "kitchen-counting")

    # doing it again counts again
    complete(parent.client, parent.headers, kid["id"], done)
    again = {r["slug"]: r for r in _library(parent, kid, subject=done["subject_code"])}
    assert again["kitchen-counting"]["times_done"] == 2

    # it is per child: the sibling has done nothing
    assert all(r["times_done"] == 0 for r in _library(parent, sibling, subject=done["subject_code"]))


def test_library_lists_activities_not_done_yet_before_done_ones(parent, activities):
    kid = parent.add_child("K", "L2")
    subject = activities["kitchen-counting"]["subject_code"]
    before = [r["slug"] for r in _library(parent, kid, subject=subject)]
    assert len(before) >= 2
    first = before[0]  # alphabetical by title when nothing is done
    complete(parent.client, parent.headers, kid["id"], activities[first])

    after = _library(parent, kid, subject=subject)
    assert after[-1]["slug"] == first and after[-1]["times_done"] == 1
    assert [r["times_done"] for r in after] == sorted(r["times_done"] for r in after)


def test_library_shows_when_an_activity_is_planned(parent, activities):
    kid = parent.add_child("K", "L2")
    a = activities["kitchen-counting"]
    today = _today()
    _plan(parent, kid, a, today + timedelta(days=3))
    _plan(parent, kid, a, today + timedelta(days=1))
    rows = {r["slug"]: r for r in _library(parent, kid, subject=a["subject_code"])}
    assert rows["kitchen-counting"]["planned_for"] == (today + timedelta(days=1)).isoformat()
    assert all(r["planned_for"] is None for s, r in rows.items() if s != "kitchen-counting")


def test_a_finished_or_past_plan_is_not_shown_as_planned(parent, activities):
    kid = parent.add_child("K", "L2")
    a = activities["kitchen-counting"]
    item = _plan(parent, kid, a, _today())
    complete(parent.client, parent.headers, kid["id"], a, plan_item_id=item["id"])  # completes the plan item
    rows = {r["slug"]: r for r in _library(parent, kid, subject=a["subject_code"])}
    assert rows["kitchen-counting"]["planned_for"] is None
    assert rows["kitchen-counting"]["times_done"] == 1


def test_library_search_narrows_within_the_subject(parent, activities):
    kid = parent.add_child("K", "L2")
    a = activities["kitchen-counting"]
    rows = _library(parent, kid, subject=a["subject_code"], q="kitchen")
    assert [r["slug"] for r in rows] == ["kitchen-counting"]


def test_a_child_without_a_level_sees_every_level(parent, activities):
    r = parent.post(f"/v1/families/{parent.family_id}/children", json={"display_name": "NoLevel"})
    assert r.status_code == 201, r.text
    kid = r.json()
    everything = {a["slug"] for a in parent.get("/v1/activities", params={"subject": "MAT", "limit": 100}).json()}
    assert {x["slug"] for x in _library(parent, kid, subject="MAT")} == everything


def test_library_is_private_and_a_child_can_read_their_own(make_parent, activities):
    a, b = make_parent(), make_parent()
    kid = a.add_child("K", "L2")
    assert b.get(f"/v1/children/{kid['id']}/library", params={"subject": "MAT"}).status_code == 404
    own = a.client.get(
        f"/v1/children/{kid['id']}/library", params={"subject": "MAT"}, headers=a.child_headers(kid["id"])
    )
    assert own.status_code == 200
