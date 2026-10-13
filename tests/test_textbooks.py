"""Boards, textbooks, lessons: the import, catalogue filters, and a child's progress through a book (soft lock)."""

import pytest

from tests.helpers import complete, correct_answers

BOOK = "cbse-3-maths-test"
BOOK_ICSE = "icse-3-maths-test"


@pytest.fixture
def scored(activities):
    """Four launch activities that have auto-scored steps (so a wrong answer gives a low score)."""
    picks = [a for a in activities.values() if correct_answers(a["slug"])]
    assert len(picks) >= 4
    return picks[:4]


def bundle(scored, **over):
    a, b, c, d = (x["slug"] for x in scored)
    payload = {
        "version": "tb-1",
        "boards": [{"code": "CBSE", "name": "CBSE", "sort_order": 1}, {"code": "ICSE", "name": "ICSE", "sort_order": 2}],
        "books": [
            {
                "code": BOOK,
                "board": "CBSE",
                "grade": "3",
                "subject": "MAT",
                "title": "Maths Test Book",
                "publisher": "Test",
                "edition_year": 2024,
                "lessons": [
                    {"position": 1, "label": "1", "title": "Counting"},
                    {"position": 2, "label": "2", "title": "Adding"},
                    {"position": 3, "label": "3", "title": "An empty lesson"},
                    {"position": 4, "label": "4", "title": "Shapes"},
                ],
            },
            {
                "code": BOOK_ICSE,
                "board": "ICSE",
                "grade": "3",
                "subject": "MAT",
                "title": "ICSE Maths Test",
                "lessons": [{"position": 1, "title": "Numbers"}],
            },
        ],
        "lesson_links": [
            {"activity": a, "book": BOOK, "lesson": 1, "order": 20},
            {"activity": b, "book": BOOK, "lesson": 1, "order": 10},
            {"activity": c, "book": BOOK, "lesson": 2, "order": 10},
            {"activity": d, "book": BOOK, "lesson": 4, "order": 10},
            {"activity": a, "book": BOOK_ICSE, "lesson": 1, "order": 10},  # one activity, two boards
        ],
        "dry_run": False,
    }
    payload.update(over)
    return payload


@pytest.fixture
def loaded(make_admin, scored):
    admin = make_admin("content_admin")
    r = admin.post("/v1/admin/content/bundle", json=bundle(scored))
    assert r.status_code == 200 and r.json()["ok"], r.text
    return admin


def titles(path):
    return {lsn["position"]: [a["title"] for a in lsn["activities"]] for lsn in path["lessons"]}


# ------------------------------------------------------------------------------------------------ import
def test_import_is_idempotent_and_reports_counts(make_admin, scored):
    admin = make_admin("content_admin")
    dry = admin.post("/v1/admin/content/bundle", json=bundle(scored, dry_run=True)).json()
    assert dry["ok"] and dry["created"]["boards"] == 2 and dry["created"]["books"] == 2
    assert dry["created"]["lessons"] == 5 and dry["created"]["lesson_links"] == 5
    assert admin.get("/v1/curriculum/boards").json() == []  # a dry run changes nothing
    applied = admin.post("/v1/admin/content/bundle", json=bundle(scored)).json()
    assert applied["ok"] and applied["created"]["books"] == 2
    again = admin.post("/v1/admin/content/bundle", json=bundle(scored)).json()
    assert again["ok"] and again["created"]["books"] == 0 and again["unchanged"]["lessons"] == 5
    assert again["unchanged"]["lesson_links"] == 5


def test_import_rejects_bad_boards_books_and_links_without_changing_anything(make_admin, scored):
    admin = make_admin("content_admin")
    bad = bundle(scored)
    bad["boards"].append({"code": "cbse", "name": "lower case code"})
    bad["books"][0]["lessons"].append({"position": 2, "title": "duplicate position"})
    bad["books"].append({"code": "bad-board-book", "board": "NOPE", "grade": "1", "subject": "MAT", "title": "t", "lessons": []})
    bad["lesson_links"].append({"activity": "no-such-activity", "book": BOOK, "lesson": 1})
    bad["lesson_links"].append({"activity": scored[0]["slug"], "book": BOOK, "lesson": 99})
    r = admin.post("/v1/admin/content/bundle", json=bad).json()
    text = " ".join(r["problems"])
    assert not r["ok"]
    for needle in ("upper-case code", "used twice", "unknown board", "unknown activity", "no such lesson"):
        assert needle in text, text
    assert admin.get("/v1/curriculum/boards").json() == []


# ------------------------------------------------------------------------------------------------ catalogue
def test_catalogue_boards_books_lessons_and_filters(loaded, parent, activities, scored):
    assert [b["code"] for b in parent.get("/v1/curriculum/boards").json()] == ["CBSE", "ICSE"]
    cbse = parent.get("/v1/curriculum/books", params={"board": "CBSE", "grade": "3"}).json()
    assert [b["code"] for b in cbse] == [BOOK] and cbse[0]["lesson_count"] == 4
    detail = parent.get(f"/v1/curriculum/books/{BOOK}").json()
    assert [(x["position"], x["activity_count"]) for x in detail["lessons"]] == [(1, 2), (2, 1), (3, 0), (4, 1)]
    assert parent.get("/v1/curriculum/books/nope").status_code == 404

    in_cbse = {a["slug"] for a in parent.get("/v1/activities", params={"board": "CBSE", "limit": 100}).json()}
    in_icse = {a["slug"] for a in parent.get("/v1/activities", params={"board": "ICSE", "limit": 100}).json()}
    assert in_cbse == {x["slug"] for x in scored}
    assert in_icse == {scored[0]["slug"]}  # linked to both boards
    assert len(parent.get("/v1/activities", params={"limit": 100}).json()) == 16  # no filter: board-neutral too
    one_book = parent.get("/v1/activities", params={"book": BOOK_ICSE}).json()
    assert [a["slug"] for a in one_book] == [scored[0]["slug"]]

    places = parent.get(f"/v1/activities/{scored[0]['id']}").json()["placements"]
    assert sorted(p["board_code"] for p in places) == ["CBSE", "ICSE"]
    linked = {x["slug"] for x in scored}
    free = next(a for a in activities.values() if a["slug"] not in linked)
    assert parent.get(f"/v1/activities/{free['id']}").json()["placements"] == []  # board-neutral


# ------------------------------------------------------------------------------------------------ progress
def test_enrol_unlocks_only_the_first_lesson_and_hides_the_rest_from_the_child(loaded, parent, scored):
    child = parent.add_child()
    ch = parent.child_headers(child["id"])
    path = parent.put(f"/v1/children/{child['id']}/books/{BOOK}").json()
    assert path["unlocked_through"] == 1 and path["can_unlock_next"] is False
    states = {lsn["position"]: lsn["state"] for lsn in path["lessons"]}
    assert states == {1: "open", 2: "locked", 3: "locked", 4: "locked"}
    assert path["next_lesson_position"] == 2
    first = titles(path)[1]
    assert first == [scored[1]["title"], scored[0]["title"]]  # `order` 10 before 20, not alphabetical
    assert titles(path)[2] == [scored[2]["title"]] and path["lessons"][1]["activities"][0]["locked"] is True

    # the child sees lesson 1 only; locked lessons show a count but no activities
    child_path = client_get(ch, f"/v1/children/{child['id']}/books/{BOOK}/path", parent)
    assert titles(child_path)[1] == first and titles(child_path)[2] == [] and child_path["lessons"][1]["activity_count"] == 1
    up = client_get(ch, f"/v1/children/{child['id']}/next-up", parent)
    assert [x["title"] for x in up] == first and all(x["lesson_position"] == 1 for x in up)
    # enrolling again changes nothing
    again = parent.put(f"/v1/children/{child['id']}/books/{BOOK}").json()
    assert again["unlocked_through"] == 1


def client_get(headers, url, parent):
    r = parent.client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_lesson_is_complete_only_when_activities_pass_the_minimum_score(client, loaded, parent, scored):
    child = parent.add_child()
    ch = parent.child_headers(child["id"])
    parent.put(f"/v1/children/{child['id']}/books/{BOOK}")
    url = f"/v1/children/{child['id']}/books/{BOOK}"
    first, second = scored[1], scored[0]  # textbook order inside lesson 1

    wrong = set(correct_answers(first["slug"]))
    r = complete(client, ch, child["id"], first, correct_answers(first["slug"], wrong=wrong))
    assert r.status_code == 200, r.text
    path = parent.get(f"{url}/path").json()
    assert path["lessons"][0]["activities"][0]["status"] == "attempted"  # finished, but below the pass score
    assert path["lessons"][0]["activities"][0]["best_score"] < path["pass_score"]
    up = client_get(ch, f"/v1/children/{child['id']}/next-up", parent)
    assert [x["status"] for x in up][0] == "attempted"  # still on the child's list to try again

    assert complete(client, ch, child["id"], first).status_code == 200  # a good score passes
    assert complete(client, ch, child["id"], second).status_code == 200
    path = parent.get(f"{url}/path").json()
    assert path["lessons"][0]["state"] == "complete" and path["can_unlock_next"] is True
    assert path["lessons"][0]["activities"][0]["best_score"] == 1.0
    assert client_get(ch, f"/v1/children/{child['id']}/next-up", parent) == []  # nothing left until the parent adds more
    listing = parent.get(f"/v1/children/{child['id']}/books").json()
    assert listing[0]["lessons_complete"] == 1 and listing[0]["can_unlock_next"] is True


def test_parent_unlocks_one_lesson_at_a_time_and_empty_lessons_are_skipped(client, loaded, parent, scored):
    child = parent.add_child()
    ch = parent.child_headers(child["id"])
    parent.put(f"/v1/children/{child['id']}/books/{BOOK}")
    url = f"/v1/children/{child['id']}/books/{BOOK}"

    early = parent.post(f"{url}/unlock-next", json={})
    assert early.status_code == 409 and early.json()["error"]["code"] == "lesson_incomplete"

    for a in (scored[1], scored[0]):
        assert complete(client, ch, child["id"], a).status_code == 200
    path = parent.post(f"{url}/unlock-next", json={}).json()
    assert path["unlocked_through"] == 2 and path["lessons"][1]["state"] == "open"
    assert titles(path)[2] == [scored[2]["title"]]
    assert [x["title"] for x in client_get(ch, f"/v1/children/{child['id']}/next-up", parent)] == [scored[2]["title"]]

    assert complete(client, ch, child["id"], scored[2]).status_code == 200
    path = parent.post(f"{url}/unlock-next", json={}).json()
    assert path["unlocked_through"] == 4  # lesson 3 has no activities, so it is skipped
    assert complete(client, ch, child["id"], scored[3]).status_code == 200
    done = parent.post(f"{url}/unlock-next", json={})
    assert done.status_code == 409 and done.json()["error"]["code"] == "conflict"  # no more lessons


def test_force_lets_a_parent_skip_ahead(loaded, parent, scored):
    child = parent.add_child()
    parent.put(f"/v1/children/{child['id']}/books/{BOOK}")
    path = parent.post(f"/v1/children/{child['id']}/books/{BOOK}/unlock-next", json={"force": True}).json()
    assert path["unlocked_through"] == 2


def test_soft_lock_parents_can_still_plan_any_activity(loaded, parent, scored):
    child = parent.add_child()
    parent.put(f"/v1/children/{child['id']}/books/{BOOK}")
    r = parent.post(
        f"/v1/children/{child['id']}/plan/items", json={"activity_id": scored[3]["id"], "scheduled_date": "2026-10-20"}
    )
    assert r.status_code == 201  # lesson 4 is still locked for the child's textbook list, not for the planner


def test_access_and_roles(client, loaded, parent, make_parent, make_admin, scored):
    child = parent.add_child()
    ch = parent.child_headers(child["id"])
    parent.put(f"/v1/children/{child['id']}/books/{BOOK}")
    url = f"/v1/children/{child['id']}/books/{BOOK}"
    # a child cannot enrol, unlock or remove
    assert client.put(url, headers=ch).status_code == 403
    assert client.post(f"{url}/unlock-next", headers=ch, json={"force": True}).status_code == 403
    assert client.delete(url, headers=ch).status_code == 403
    # another family cannot see this child's progress
    other = make_parent()
    assert other.get(f"{url}/path").status_code == 404
    assert other.get(f"/v1/children/{child['id']}/books").status_code == 404
    assert other.put(url).status_code == 404
    # unknown book
    assert parent.put(f"/v1/children/{child['id']}/books/nope").status_code == 404
    # removing a book forgets the child's place in it
    assert parent.delete(url).status_code == 204
    assert parent.get(f"{url}/path").status_code == 404 and parent.get(f"/v1/children/{child['id']}/books").json() == []


def test_admin_can_replace_an_activitys_lessons(loaded, parent, make_admin, scored):
    a = scored[0]
    admin = make_admin("content_admin")
    assert parent.put(f"/v1/admin/activities/{a['id']}/lessons", json={"lessons": []}).status_code == 403
    r = admin.put(f"/v1/admin/activities/{a['id']}/lessons", json={"lessons": [{"book": BOOK, "lesson": 4, "order": 5}]})
    assert r.status_code == 200
    assert [(p["book_code"], p["lesson_position"], p["order"]) for p in r.json()] == [(BOOK, 4, 5)]  # the ICSE link was replaced
    bad = admin.put(f"/v1/admin/activities/{a['id']}/lessons", json={"lessons": [{"book": BOOK, "lesson": 42}]})
    assert bad.status_code == 422
    assert admin.get(f"/v1/admin/activities/{a['id']}/lessons").json() == r.json()
    assert admin.put(f"/v1/admin/activities/{a['id']}/lessons", json={"lessons": []}).json() == []


def test_child_books_is_protected_by_row_level_security(database):
    from sqlalchemy import text

    with database["admin"].connect() as c:
        row = c.execute(
            text("select relrowsecurity, relforcerowsecurity from pg_class where relname = 'child_books'")
        ).one()
        policies = c.execute(text("select policyname from pg_policies where tablename = 'child_books'")).scalars().all()
    assert tuple(row) == (True, True) and policies == ["family_isolation"]


def test_educator_places_only_their_own_drafts_in_lessons(loaded, make_admin):
    from tests.test_educators import new_draft, valid_draft

    edu, other = make_admin("educator"), make_admin("educator")
    a = new_draft(edu)
    url = f"/v1/admin/activities/{a['id']}/lessons"
    body = {"lessons": [{"book": BOOK, "lesson": 1, "order": 1}]}
    assert other.get(url).status_code == 404 and other.put(url, json=body).status_code == 404
    r = edu.put(url, json=body)
    assert r.status_code == 200 and [(p["book_code"], p["lesson_position"]) for p in r.json()] == [(BOOK, 1)]
    assert edu.get(url).json() == r.json()
    # once submitted for review the educator can no longer change it
    b = valid_draft(edu, "Placed later")
    assert edu.post(f"/v1/admin/activities/{b['id']}/validate").json()["ok"]
    assert edu.post(f"/v1/admin/activities/{b['id']}/submit").status_code == 200
    locked = edu.put(f"/v1/admin/activities/{b['id']}/lessons", json=body)
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "activity_locked"
