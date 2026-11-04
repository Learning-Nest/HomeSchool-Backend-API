"""The daily release: LearnNest releases a set of activities every day once enough of the last set is done."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.services import daily
from tests.helpers import complete

MAT = "cbse-3-mat-daily"
ENG = "cbse-3-eng-daily"
DAY0 = date(2026, 11, 2)  # a Monday


@pytest.fixture
def today(monkeypatch):
    """A controllable 'local today' for the engine."""
    state = {"d": DAY0}
    monkeypatch.setattr(daily, "local_today", lambda db, family_id: state["d"])
    return state


def set_policy(database, **cols):
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with database["admin"].begin() as conn:
        conn.execute(
            text(f"UPDATE content.release_policies SET {sets} WHERE scope_board = '' AND scope_grade = ''"), cols
        )


def go_system(database, parent):
    with database["admin"].begin() as conn:
        conn.execute(text("UPDATE families SET plan_mode = 'system' WHERE id = :f"), {"f": parent.family_id})


@pytest.fixture
def slugs(activities):
    """Eight published launch activities, in a fixed order."""
    return sorted(activities)[:8]


def bundle(slugs, extra_links=()):
    a = slugs
    links = [
        {"activity": a[0], "book": MAT, "lesson": 1, "order": 10},
        {"activity": a[1], "book": MAT, "lesson": 1, "order": 20},
        {"activity": a[2], "book": MAT, "lesson": 2, "order": 10},
        {"activity": a[3], "book": MAT, "lesson": 3, "order": 10},
        {"activity": a[4], "book": ENG, "lesson": 1, "order": 10},
        {"activity": a[5], "book": ENG, "lesson": 1, "order": 20},
        {"activity": a[6], "book": ENG, "lesson": 2, "order": 10},
        {"activity": a[7], "book": ENG, "lesson": 2, "order": 20},
        *extra_links,
    ]
    return {
        "version": "daily-1",
        "boards": [{"code": "CBSE", "name": "CBSE", "sort_order": 1}],
        "books": [
            {
                "code": MAT,
                "board": "CBSE",
                "grade": "3",
                "subject": "MAT",
                "title": "Maths",
                "lessons": [{"position": i, "title": f"M{i}"} for i in (1, 2, 3)],
            },
            {
                "code": ENG,
                "board": "CBSE",
                "grade": "3",
                "subject": "ENG",
                "title": "English",
                "lessons": [{"position": i, "title": f"E{i}"} for i in (1, 2)],
            },
        ],
        "lesson_links": links,
        "dry_run": False,
    }


@pytest.fixture
def world(make_admin, make_parent, database, slugs, today):
    """A system-mode family with a child on CBSE grade 3 and two books; one set holds at most 4 activities."""
    admin = make_admin("content_admin")
    r = admin.post("/v1/admin/content/bundle", json=bundle(slugs))
    assert r.status_code == 200 and r.json()["ok"], r.text
    parent = make_parent()
    go_system(database, parent)
    set_policy(database, set_max_activities=4, set_minutes=600)
    child = parent.add_child(board_code="CBSE", grade="3")
    return parent, child, parent.child_headers(child["id"]), admin


def sync(parent, child, headers=None):
    r = parent.client.post(f"/v1/children/{child['id']}/daily/sync", headers=headers or parent.headers)
    assert r.status_code == 200, r.text
    return r.json()


def finish(parent, headers, child, activities, items):
    for it in items:
        r = complete(parent.client, headers, child["id"], activities[it["activity"]["slug"]], plan_item_id=it["id"])
        assert r.status_code == 200, r.text


def test_parent_mode_families_are_untouched(make_parent, slugs, make_admin):
    admin = make_admin("content_admin")
    assert admin.post("/v1/admin/content/bundle", json=bundle(slugs)).json()["ok"]
    parent = make_parent()
    child = parent.add_child(board_code="CBSE", grade="3")
    out = sync(parent, child)
    assert out["reason"] == "not_system_mode" and out["released"] == [] and out["plan_mode"] == "parent"
    assert parent.get(f"/v1/families/{parent.family_id}/children").json()[0]["board_code"] == "CBSE"


def test_first_release_takes_the_next_activity_of_each_book_in_turn(world, slugs):
    parent, child, _h, _a = world
    out = sync(parent, child)
    assert out["reason"] == "released" and out["plan_mode"] == "system"
    got = [(i["book_code"], i["lesson_position"], i["activity"]["slug"]) for i in out["released"]]
    # ENG and MAT alternate (subject order ENG, MAT); inside a book: lesson, then `order`
    assert got == [(ENG, 1, slugs[4]), (MAT, 1, slugs[0]), (ENG, 1, slugs[5]), (MAT, 1, slugs[1])]
    assert all(i["source"] == "system" and i["status"] == "planned" for i in out["released"])
    assert out["released"][0]["lesson_title"] == "E1" and out["progress"]["total"] == 4
    assert out["progress"]["done"] == 0 and out["progress"]["more_needed"] == 3


def test_asking_again_on_the_same_day_returns_the_same_set(world):
    parent, child, _h, _a = world
    first = sync(parent, child)
    again = sync(parent, child)
    assert again["reason"] == "already_released_today"
    assert [i["id"] for i in again["released"]] == [i["id"] for i in first["released"]]
    today_items = parent.get(f"/v1/children/{child['id']}/today").json()
    assert len(today_items) == 4


def test_a_new_set_needs_70_percent_of_the_last_one(world, today, activities):
    parent, child, headers, _a = world
    first = sync(parent, child)["released"]
    today["d"] += timedelta(days=1)
    assert sync(parent, child)["reason"] == "below_threshold"
    finish(parent, headers, child, activities, first[:2])  # 2 of 4 = 50 %
    st = parent.get(f"/v1/children/{child['id']}/daily/status").json()["progress"]
    assert (st["done"], st["total"], st["more_needed"]) == (2, 4, 1) and st["ratio"] == 0.5
    assert sync(parent, child)["reason"] == "below_threshold"
    finish(parent, headers, child, activities, first[2:3])  # 3 of 4 = 75 %
    out = sync(parent, child)
    assert out["reason"] == "released"
    new = {i["activity"]["slug"] for i in out["released"]}
    assert new.isdisjoint({i["activity"]["slug"] for i in first})  # never the same activity twice
    assert len(new) == 4  # the rest of both books
    assert any(i["carry_over"] for i in parent.get(f"/v1/children/{child['id']}/today").json())  # the unfinished one


def test_a_skipped_activity_counts_toward_the_threshold(world, today, activities):
    parent, child, headers, _a = world
    first = sync(parent, child)["released"]
    finish(parent, headers, child, activities, first[:2])
    skip = parent.patch(f"/v1/plan-items/{first[2]['id']}", json={"status": "skipped", "version": first[2]["version"]})
    assert skip.status_code == 200, skip.text
    today["d"] += timedelta(days=1)
    assert sync(parent, child)["reason"] == "released"  # 2 done + 1 skipped = 3 of 4


def test_at_most_one_set_per_day_even_when_everything_is_done(world, today, activities):
    parent, child, headers, _a = world
    first = sync(parent, child)["released"]
    finish(parent, headers, child, activities, first)
    assert sync(parent, child)["reason"] == "already_released_today"
    today["d"] += timedelta(days=1)
    assert sync(parent, child)["reason"] == "released"


def test_too_many_open_activities_pause_new_releases(world, today, database, activities):
    parent, child, headers, _a = world
    first = sync(parent, child)["released"]
    finish(parent, headers, child, activities, first[:1])  # threshold met, 3 still open
    set_policy(database, max_open_items=3, release_threshold=0.2)
    today["d"] += timedelta(days=1)
    assert sync(parent, child)["reason"] == "too_many_open"


def test_pause_school_days_no_books_and_no_content(world, today, database, make_parent):
    parent, child, _h, _a = world
    today["d"] = DAY0 + timedelta(days=5)  # Saturday
    set_policy(database, school_days="{1,2,3,4,5}")
    assert sync(parent, child)["reason"] == "not_a_school_day"
    today["d"] = DAY0 + timedelta(days=7)  # Monday
    r = parent.put(f"/v1/children/{child['id']}/daily/pause", json={"paused_until": str(DAY0 + timedelta(days=8))})
    assert r.status_code == 200 and r.json()["paused_until"] == str(DAY0 + timedelta(days=8))
    assert sync(parent, child)["reason"] == "paused"
    assert parent.put(f"/v1/children/{child['id']}/daily/pause", json={"paused_until": None}).status_code == 200
    assert sync(parent, child)["reason"] == "released"
    # a child with no board and grade follows nothing
    other = parent.add_child("Mira")
    assert sync(parent, other)["reason"] == "no_books"
    # a standard without published activities has nothing to release
    empty = parent.add_child("Zoya", board_code="CBSE", grade="9")
    assert sync(parent, empty)["reason"] == "no_books"
    assert parent.put(f"/v1/children/{child['id']}/daily/pause", json={"paused_until": "2020-01-01"}).status_code == 422


def test_an_activity_added_later_at_an_earlier_order_is_released_next(world, today, activities, slugs):
    parent, child, headers, admin = world
    first = sync(parent, child)["released"]
    finish(parent, headers, child, activities, first)
    # a ninth activity goes in front of lesson 2 of the maths book
    extra = next(s for s in sorted(activities) if s not in slugs)
    r = admin.post(
        "/v1/admin/content/bundle", json=bundle(slugs, [{"activity": extra, "book": MAT, "lesson": 2, "order": 5}])
    )
    assert r.json()["ok"], r.text
    today["d"] += timedelta(days=1)
    out = sync(parent, child)
    maths = [i["activity"]["slug"] for i in out["released"] if i["book_code"] == MAT]
    assert maths[:2] == [extra, slugs[2]]


def test_parents_cannot_plan_or_unlock_by_hand_in_system_mode(world, activities):
    parent, child, _h, _a = world
    first = sync(parent, child)["released"]
    act = next(iter(activities.values()))
    r = parent.post(
        f"/v1/children/{child['id']}/plan/items", json={"activity_id": act["id"], "scheduled_date": "2026-11-03"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_managed"
    item = first[0]
    moved = parent.patch(
        f"/v1/plan-items/{item['id']}", json={"scheduled_date": "2026-11-09", "version": item["version"]}
    )
    assert moved.status_code == 403 and moved.json()["error"]["code"] == "plan_managed"
    assert parent.delete(f"/v1/plan-items/{item['id']}").status_code == 403
    assert parent.put(f"/v1/children/{child['id']}/books/{MAT}").status_code == 403
    assert parent.delete(f"/v1/children/{child['id']}/books/{MAT}").status_code == 403
    assert parent.post(f"/v1/children/{child['id']}/books/{MAT}/unlock-next", json={}).status_code == 403
    assert parent.get(f"/v1/children/{child['id']}/plan").status_code == 200  # viewing is fine


def test_the_child_can_sync_and_only_their_own_data(world, make_parent):
    parent, child, headers, _a = world
    out = sync(parent, child, headers)
    assert out["reason"] == "released" and len(out["released"]) == 4
    other_child = parent.add_child("Mira", board_code="CBSE", grade="3")
    r = parent.client.post(f"/v1/children/{other_child['id']}/daily/sync", headers=headers)
    assert r.status_code == 404
    stranger = make_parent()
    assert stranger.post(f"/v1/children/{child['id']}/daily/sync").status_code == 404


def test_board_and_grade_decide_the_books_and_changing_them_stops_the_old_ones(world, today):
    parent, child, _h, _a = world
    books = {b["book"]["code"] for b in parent.get(f"/v1/children/{child['id']}/books").json()}
    assert books == {MAT, ENG}
    r = parent.patch(f"/v1/children/{child['id']}", json={"grade": "4", "version": child["version"]})
    assert r.status_code == 200 and r.json()["grade"] == "4"
    assert parent.get(f"/v1/children/{child['id']}/books").json() == []
    assert sync(parent, child)["reason"] == "no_books"
    bad = parent.patch(f"/v1/children/{child['id']}", json={"board_code": "NOPE", "version": r.json()["version"]})
    assert bad.status_code == 422


def test_the_release_uses_the_most_specific_settings_row(database, world, today):
    parent, child, _h, _a = world
    with database["admin"].begin() as conn:
        conn.execute(
            text(
                "INSERT INTO content.release_policies (scope_board, scope_grade, set_max_activities, set_minutes) VALUES ('CBSE', '3', 2, 600)"
            )
        )
    out = sync(parent, child)
    assert len(out["released"]) == 2


def test_the_minute_budget_stops_a_set_but_never_leaves_it_empty(database, world):
    parent, child, _h, _a = world
    set_policy(database, set_minutes=5)  # every launch activity is longer than this
    out = sync(parent, child)
    assert len(out["released"]) == 1


def test_practice_and_parent_items_do_not_use_up_system_activities(world, activities):
    parent, child, _h, _a = world
    out = sync(parent, child)
    assert {i["source"] for i in parent.get(f"/v1/children/{child['id']}/today").json()} == {"system"}
    assert out["progress"]["open_items"] == 4


def test_practice_is_the_one_way_a_system_family_adds_activities(world, today, activities):
    parent, child, headers, _a = world
    real_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()  # sessions are stamped with the real clock
    today["d"] = real_today
    tomorrow = (real_today + timedelta(days=1)).isoformat()
    first = sync(parent, child)["released"]
    finish(parent, headers, child, activities, first[:1])
    done_activity = first[0]["activity"]
    base = f"/v1/children/{child['id']}"
    # a parent still cannot plan by hand
    r = parent.post(f"{base}/plan/items", json={"activity_id": done_activity["id"], "scheduled_date": tomorrow})
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_managed"

    # but can queue practice for something already done (from tomorrow, after the cooldown)
    out = parent.post(
        f"{base}/practice", json={"activity_ids": [done_activity["id"]], "scheduled_date": tomorrow}
    ).json()
    (item,) = out["items"]
    assert item["source"] == "revision"
    status = parent.get(f"{base}/daily/status").json()["progress"]
    assert (status["done"], status["total"]) == (1, 4)  # practice never changes the 70 % test

    planned = parent.get(f"{base}/plan", params={"week_start": tomorrow}).json()["items"]
    assert item["id"] in [i["id"] for i in planned]
    assert parent.delete(f"/v1/plan-items/{item['id']}").status_code == 204  # practice can be taken back
    system_item = first[1]["id"]
    assert parent.delete(f"/v1/plan-items/{system_item}").status_code == 403  # released items cannot


# ------------------------------------------------------------------------------------------------ admin: settings, coverage, preview
def test_release_settings_are_read_by_admins_and_changed_by_a_super_admin(world, make_admin):
    _p, _c, _h, content_admin = world
    boss = make_admin("super_admin")
    rows = content_admin.get("/v1/admin/release-policies").json()
    default = next(r for r in rows if r["scope_board"] == "" and r["scope_grade"] == "")
    assert default["release_threshold"] == 0.7 and default["school_days"] == [1, 2, 3, 4, 5, 6, 7]

    body = {k: default[k] for k in default if k not in ("scope_board", "scope_grade", "updated_at")}
    body.update(release_threshold=0.8, set_max_activities=2)
    assert (
        content_admin.put("/v1/admin/release-policies", params={"board": "CBSE", "grade": "3"}, json=body).status_code
        == 403
    )
    r = boss.put("/v1/admin/release-policies", params={"board": "CBSE", "grade": "3"}, json=body)
    assert r.status_code == 200 and r.json()["release_threshold"] == 0.8 and r.json()["scope_grade"] == "3"
    assert len(content_admin.get("/v1/admin/release-policies").json()) == len(rows) + 1

    assert boss.put("/v1/admin/release-policies", params={"grade": "3"}, json=body).status_code == 422
    assert boss.put("/v1/admin/release-policies", params={"board": "NOPE"}, json={**body}).status_code == 422
    assert boss.put("/v1/admin/release-policies", json={**body, "release_threshold": 0}).status_code == 422
    assert boss.put("/v1/admin/release-policies", json={**body, "school_days": [0, 9]}).status_code == 422

    assert boss.delete("/v1/admin/release-policies").status_code == 422  # the default stays
    assert boss.delete("/v1/admin/release-policies", params={"board": "CBSE", "grade": "3"}).status_code == 204
    assert boss.delete("/v1/admin/release-policies", params={"board": "CBSE", "grade": "3"}).status_code == 404


def test_the_most_specific_release_settings_apply_to_a_child(world, today, make_admin):
    parent, child, _h, _a = world
    boss = make_admin("super_admin")
    body = {**boss.get("/v1/admin/release-policies").json()[0], "set_max_activities": 2}
    for k in ("scope_board", "scope_grade", "updated_at"):
        body.pop(k)
    boss.put("/v1/admin/release-policies", params={"board": "CBSE", "grade": "3"}, json=body)
    assert len(sync(parent, child)["released"]) == 2


def test_coverage_shows_lessons_and_days_of_content(world):
    _p, _c, _h, admin = world
    out = admin.get("/v1/admin/coverage", params={"board": "CBSE", "grade": "3"}).json()
    assert out["target_per_lesson"] == 3
    books = {b["code"]: b for b in out["books"]}
    mat = books[MAT]
    assert (mat["lessons"], mat["lessons_with_activities"], mat["published_activities"]) == (3, 3, 4)
    assert mat["lessons_below_target"] == 3 and mat["days_of_content"] == 2  # 2 a day: two books share the set of 4
    assert [(w["position"], w["published_activities"]) for w in mat["weak_lessons"]] == [(1, 2), (2, 1), (3, 1)]
    assert mat["minutes"] > 0
    strict = admin.get("/v1/admin/coverage", params={"board": "CBSE", "grade": "3", "target_per_lesson": 1}).json()
    assert {b["code"]: b["lessons_below_target"] for b in strict["books"]}[MAT] == 0
    assert admin.get("/v1/admin/coverage", params={"board": "CBSE", "grade": "9"}).json()["books"] == []


def test_release_preview_matches_what_a_child_would_get(world):
    parent, child, _h, admin = world
    out = admin.post("/v1/admin/release-preview", json={"board_code": "CBSE", "grade": "3", "days": 5}).json()
    assert sorted(out["books"]) == [ENG, MAT]
    assert [len(d["activities"]) for d in out["days"]] == [4, 4]  # eight activities, then nothing left
    assert out["remaining_activities"] == 0
    real = [i["activity"]["slug"] for i in sync(parent, child)["released"]]
    assert [a["slug"] for a in out["days"][0]["activities"]] == real
    assert out["days"][0]["minutes"] == sum(a["duration_min"] for a in out["days"][0]["activities"])
    assert admin.post("/v1/admin/release-preview", json={"board_code": "NOPE", "grade": "3"}).status_code == 422
