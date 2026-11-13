"""Assignments: an admin hands a draft to an educator with written requirements; the educator is emailed, works on the
draft, and the assignment follows the activity (submitted, sent back, published, archived)."""

from datetime import date, timedelta

from sqlalchemy import text

from app.errors import ApiError
from app.services import assignments as assignment_service
from app.services import email as email_service
from tests.test_educators import MIN_DRAFT, new_draft, valid_draft

REQ = "Write six exercises on counting to 20. Include one hands-on task and a hint for every question."


def assign(admin, activity_id, educator, requirements=REQ, **extra):
    return admin.post(
        f"/v1/admin/activities/{activity_id}/assignments",
        json={"educator_id": educator.user_id, "requirements": requirements, **extra},
    )


def tomorrow() -> str:
    return (assignment_service.today() + timedelta(days=1)).isoformat()


# ------------------------------------------------------------------------------------------------ assigning
def test_admin_assigns_a_draft_and_the_educator_is_emailed(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    assert edu.get(f"/v1/admin/activities/{a['id']}").status_code == 404  # not theirs yet
    email_service.OUTBOX.clear()
    r = assign(admin, a["id"], edu, due_date=tomorrow())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email_sent"] is True
    asg = body["assignment"]
    assert asg["status"] == "open" and asg["requirements"] == REQ and asg["due_date"] == tomorrow()
    assert asg["activity_id"] == a["id"] and asg["activity_title"] == a["title"] and asg["overdue"] is False
    assert asg["educator_id"] == edu.user_id and asg["assigned_by"] == admin.user_id
    mail = email_service.OUTBOX[-1]
    assert mail.to == edu.email and a["title"] in mail.subject and "counting to 20" in mail.body
    assert "password" not in mail.body.lower()
    # The educator now reaches the draft, lists it, and can edit it.
    assert edu.get(f"/v1/admin/activities/{a['id']}").status_code == 200
    assert a["id"] in [x["id"] for x in edu.get("/v1/admin/activities").json()]
    d = {**a["definition"], "summary": "Counting practice"}
    assert edu.put(f"/v1/admin/activities/{a['id']}/definition?strict=false", json={"definition": d}).status_code == 200
    # Admin lists show who holds it.
    row = next(x for x in admin.get("/v1/admin/activities").json() if x["id"] == a["id"])
    assert row["assigned_to"] == edu.user_id and row["assignment_id"] == asg["id"]


def test_assignment_is_logged_on_the_activity(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    assert assign(admin, a["id"], edu).status_code == 201
    log = admin.get("/v1/admin/activity-log", params={"activity_id": a["id"], "action": "assigned"}).json()
    assert len(log) == 1 and log[0]["detail"]["educator_id"] == edu.user_id


def test_email_failure_does_not_block_the_assignment(make_admin, monkeypatch):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)

    def boom(*args, **kwargs):
        raise ApiError(503, "delivery_failed")

    monkeypatch.setattr(email_service, "send_activity_assignment", boom)
    r = assign(admin, a["id"], edu)
    assert r.status_code == 201 and r.json()["email_sent"] is False
    assert r.json()["assignment"]["status"] == "open"


def test_only_admins_assign(make_admin):
    admin, edu, other = make_admin("content_admin"), make_admin("educator"), make_admin("educator")
    a = new_draft(admin)
    assert assign(edu, a["id"], other).status_code == 403
    assert assign(make_admin("super_admin"), a["id"], other).status_code == 201


def test_assignment_validation(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    assert assign(admin, a["id"], edu, requirements="   ").status_code == 422
    assert assign(admin, a["id"], edu, requirements="x" * 4001).status_code == 422
    yesterday = (assignment_service.today() - timedelta(days=1)).isoformat()
    assert assign(admin, a["id"], edu, due_date=yesterday).status_code == 422
    assert assign(admin, a["id"], admin).status_code == 404  # an admin is not an educator
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        admin.post(
            f"/v1/admin/activities/{missing}/assignments", json={"educator_id": edu.user_id, "requirements": REQ}
        ).status_code
        == 404
    )
    # A published activity is not a draft.
    published = admin.get("/v1/admin/activities", params={"status": "published", "limit": 1}).json()[0]
    r = assign(admin, published["id"], edu)
    assert r.status_code == 409


def test_cannot_assign_to_a_disabled_educator(make_admin):
    admin, sup, edu = make_admin("content_admin"), make_admin("super_admin"), make_admin("educator")
    a = new_draft(admin)
    assert sup.patch(f"/v1/admin/educators/{edu.user_id}", json={"active": False}).status_code == 200
    assert assign(admin, a["id"], edu).status_code == 409


def test_assigning_twice_to_the_same_educator_is_a_conflict(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    assert assign(admin, a["id"], edu).status_code == 201
    assert assign(admin, a["id"], edu).status_code == 409


def test_reassigning_moves_access_and_keeps_history(make_admin):
    admin, first, second = make_admin("content_admin"), make_admin("educator"), make_admin("educator")
    a = new_draft(admin)
    one = assign(admin, a["id"], first).json()["assignment"]
    two = assign(admin, a["id"], second, requirements="Different brief").json()["assignment"]
    assert two["educator_id"] == second.user_id and two["requirements"] == "Different brief"
    assert first.get(f"/v1/admin/activities/{a['id']}").status_code == 404
    assert second.get(f"/v1/admin/activities/{a['id']}").status_code == 200
    assert first.get(f"/v1/admin/assignments/{one['id']}").json()["status"] == "cancelled"
    history = admin.get("/v1/admin/assignments", params={"activity_id": a["id"]}).json()
    assert {h["status"] for h in history} == {"open", "cancelled"}


# ------------------------------------------------------------------------------------------------ lifecycle
def test_assignment_follows_the_activity(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = valid_draft(admin, "Counting socks")
    asg = assign(admin, a["id"], edu).json()["assignment"]
    assert edu.post(f"/v1/admin/activities/{a['id']}/validate").status_code == 200
    assert edu.post(f"/v1/admin/activities/{a['id']}/submit").status_code == 200
    assert edu.get(f"/v1/admin/assignments/{asg['id']}").json()["status"] == "submitted"
    # Sent back: the assignment is open again and carries the reviewer's note.
    r = admin.post(f"/v1/admin/activities/{a['id']}/status", json={"status": "draft", "note": "Add a hint to s2."})
    assert r.status_code == 200
    back = edu.get(f"/v1/admin/assignments/{asg['id']}").json()
    assert back["status"] == "open" and back["review_note"] == "Add a hint to s2." and back["submitted_at"] is None
    assert edu.post(f"/v1/admin/activities/{a['id']}/validate").json()["ok"] is True
    assert edu.post(f"/v1/admin/activities/{a['id']}/submit").status_code == 200
    assert admin.post(f"/v1/admin/activities/{a['id']}/status", json={"status": "published"}).status_code == 200
    done = edu.get(f"/v1/admin/assignments/{asg['id']}").json()
    assert done["status"] == "completed" and done["completed_at"] and done["activity_status"] == "published"
    # The educator can still read their finished work, but a published activity is locked for them.
    assert edu.get(f"/v1/admin/activities/{a['id']}").status_code == 200
    d = edu.get(f"/v1/admin/activities/{a['id']}").json()["definition"]
    assert edu.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": d}).status_code == 409
    # A completed assignment can no longer be changed or cancelled.
    assert admin.patch(f"/v1/admin/assignments/{asg['id']}", json={"requirements": "late change"}).status_code == 409
    assert admin.post(f"/v1/admin/assignments/{asg['id']}/cancel").status_code == 409


def test_archiving_the_activity_cancels_the_assignment(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    asg = assign(admin, a["id"], edu).json()["assignment"]
    assert admin.post(f"/v1/admin/activities/{a['id']}/status", json={"status": "archived"}).status_code == 200
    assert admin.get(f"/v1/admin/assignments/{asg['id']}").json()["status"] == "cancelled"
    assert edu.get(f"/v1/admin/activities/{a['id']}").status_code == 404


# ------------------------------------------------------------------------------------------------ edit and cancel
def test_admin_edits_requirements_and_due_date(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    asg = assign(admin, a["id"], edu, due_date=tomorrow()).json()["assignment"]
    r = admin.patch(f"/v1/admin/assignments/{asg['id']}", json={"requirements": "  New wording  "})
    assert r.status_code == 200 and r.json()["requirements"] == "New wording" and r.json()["due_date"] == tomorrow()
    r = admin.patch(f"/v1/admin/assignments/{asg['id']}", json={"due_date": None})
    assert r.json()["due_date"] is None and r.json()["requirements"] == "New wording"
    assert admin.patch(f"/v1/admin/assignments/{asg['id']}", json={"requirements": " "}).status_code == 422
    assert edu.patch(f"/v1/admin/assignments/{asg['id']}", json={"requirements": "mine"}).status_code == 403


def test_cancel_removes_the_educators_access(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(admin)
    asg = assign(admin, a["id"], edu).json()["assignment"]
    assert edu.post(f"/v1/admin/assignments/{asg['id']}/cancel").status_code == 403
    r = admin.post(f"/v1/admin/assignments/{asg['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["cancelled_at"]
    assert edu.get(f"/v1/admin/activities/{a['id']}").status_code == 404
    assert admin.post(f"/v1/admin/assignments/{asg['id']}/cancel").status_code == 409
    # The draft can be assigned again afterwards.
    assert assign(admin, a["id"], edu).status_code == 201


# ------------------------------------------------------------------------------------------------ listing
def test_educators_see_only_their_assignments(make_admin):
    admin, one, two = make_admin("content_admin"), make_admin("educator"), make_admin("educator")
    a, b = new_draft(admin), new_draft(admin)
    mine = assign(admin, a["id"], one).json()["assignment"]
    theirs = assign(admin, b["id"], two).json()["assignment"]
    assert [x["id"] for x in one.get("/v1/admin/assignments").json()] == [mine["id"]]
    assert [x["id"] for x in one.get("/v1/admin/assignments", params={"educator_id": two.user_id}).json()] == [
        mine["id"]
    ]
    assert one.get(f"/v1/admin/assignments/{theirs['id']}").status_code == 404
    both = {x["id"] for x in admin.get("/v1/admin/assignments").json()}
    assert both == {mine["id"], theirs["id"]}
    only_two = admin.get("/v1/admin/assignments", params={"educator_id": two.user_id}).json()
    assert [x["id"] for x in only_two] == [theirs["id"]]
    assert two.get(f"/v1/admin/activities/{a['id']}").status_code == 404  # an assignment opens only its own draft


def test_overdue_and_status_filters(make_admin, database):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    late, ontime, none = new_draft(admin), new_draft(admin), new_draft(admin)
    late_asg = assign(admin, late["id"], edu, due_date=tomorrow()).json()["assignment"]
    assign(admin, ontime["id"], edu, due_date=tomorrow())
    assign(admin, none["id"], edu)
    with database["admin"].begin() as conn:  # time passes: the due date is now yesterday
        conn.execute(
            text("update content.activity_assignments set due_date = :d where id = :i"),
            {"d": date.today() - timedelta(days=3), "i": late_asg["id"]},
        )
    overdue = admin.get("/v1/admin/assignments", params={"overdue": "true"}).json()
    assert [x["id"] for x in overdue] == [late_asg["id"]] and overdue[0]["overdue"] is True
    listed = admin.get("/v1/admin/assignments", params={"live": "true"}).json()
    assert listed[0]["id"] == late_asg["id"]  # nearest due date first; no date last
    assert listed[-1]["due_date"] is None
    admin.post(f"/v1/admin/assignments/{late_asg['id']}/cancel")
    assert admin.get("/v1/admin/assignments", params={"overdue": "true"}).json() == []
    assert len(admin.get("/v1/admin/assignments", params={"status": "cancelled"}).json()) == 1


def test_capabilities_mention_assignments(make_admin):
    assert "assignments.view_own" in make_admin("educator").get("/v1/admin/me").json()["capabilities"]
    assert "assignments.manage" in make_admin("content_admin").get("/v1/admin/me").json()["capabilities"]
    assert "assignments.manage" not in make_admin("educator").get("/v1/admin/me").json()["capabilities"]


# ------------------------------------------------------------------------------------------------ bulk assignment
def bulk(admin, ids, educator, requirements=REQ, **extra):
    return admin.post(
        "/v1/admin/assignments/bulk",
        json={"activity_ids": ids, "educator_id": educator.user_id, "requirements": requirements, **extra},
    )


def test_bulk_assignment_hands_over_every_draft_and_sends_one_email(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    drafts = [new_draft(admin) for _ in range(3)]
    ids = [d["id"] for d in drafts]
    email_service.OUTBOX.clear()
    r = bulk(admin, ids, edu, due_date=tomorrow())
    assert r.status_code == 200, r.text
    assert r.json() == {"assigned": 3, "skipped": [], "email_sent": True}
    mails = [m for m in email_service.OUTBOX if m.to == edu.email]
    assert len(mails) == 1  # one email for the whole batch
    assert "3 draft activities" in mails[0].body and "counting to 20" in mails[0].body
    assert "3" in mails[0].subject and "password" not in mails[0].body.lower()
    # Every draft carries the same brief and due date, and the educator reaches all of them.
    mine = edu.get("/v1/admin/assignments").json()
    assert sorted(x["activity_id"] for x in mine) == sorted(ids)
    assert {x["requirements"] for x in mine} == {REQ} and {x["due_date"] for x in mine} == {tomorrow()}
    for i in ids:
        assert edu.get(f"/v1/admin/activities/{i}").status_code == 200
    # Each activity's log shows it, and the batch itself is audited once.
    log = admin.get("/v1/admin/activity-log", params={"activity_id": ids[0], "action": "assigned"}).json()
    assert len(log) == 1 and log[0]["detail"]["educator_id"] == edu.user_id


def test_bulk_assignment_skips_what_cannot_be_assigned_and_counts_only_the_rest(make_admin):
    admin, edu, other = make_admin("content_admin"), make_admin("educator"), make_admin("educator")
    fresh, held_by_other, already_mine = new_draft(admin), new_draft(admin), new_draft(admin)
    assert assign(admin, held_by_other["id"], other).status_code == 201
    assert assign(admin, already_mine["id"], edu).status_code == 201
    published = admin.get("/v1/admin/activities", params={"status": "published", "limit": 1}).json()[0]
    missing = "00000000-0000-4000-8000-000000000001"
    email_service.OUTBOX.clear()
    ids = [fresh["id"], held_by_other["id"], already_mine["id"], published["id"], missing, fresh["id"]]
    r = bulk(admin, ids, edu)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] == 2 and body["email_sent"] is True  # the repeated id counts once
    reasons = {x["activity_id"]: x["reason"] for x in body["skipped"]}
    assert reasons == {already_mine["id"]: "already_assigned", published["id"]: "not_draft", missing: "not_found"}
    mails = [m for m in email_service.OUTBOX if m.to == edu.email]
    assert len(mails) == 1 and "2 draft activities" in mails[0].body
    # The one held by someone else moved, keeping history.
    assert other.get(f"/v1/admin/activities/{held_by_other['id']}").status_code == 404
    assert edu.get(f"/v1/admin/activities/{held_by_other['id']}").status_code == 200


def test_bulk_assignment_with_nothing_to_assign_sends_no_email(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    mine = new_draft(admin)
    assert assign(admin, mine["id"], edu).status_code == 201
    email_service.OUTBOX.clear()
    r = bulk(admin, [mine["id"]], edu)
    assert r.status_code == 200 and r.json()["assigned"] == 0 and r.json()["email_sent"] is False
    assert not [m for m in email_service.OUTBOX if m.to == edu.email]


def test_bulk_assignment_for_one_draft_reads_naturally(make_admin):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    email_service.OUTBOX.clear()
    assert bulk(admin, [new_draft(admin)["id"]], edu).json()["assigned"] == 1
    assert "1 draft activity" in email_service.OUTBOX[-1].body


def test_bulk_assignment_checks_the_educator_the_date_and_the_caller(make_admin):
    admin, sup, edu = make_admin("content_admin"), make_admin("super_admin"), make_admin("educator")
    a = new_draft(admin)
    yesterday = (assignment_service.today() - timedelta(days=1)).isoformat()
    assert bulk(admin, [a["id"]], edu, due_date=yesterday).status_code == 422
    assert bulk(admin, [a["id"]], admin).status_code == 404  # an admin is not an educator
    assert sup.patch(f"/v1/admin/educators/{edu.user_id}", json={"active": False}).status_code == 200
    assert bulk(admin, [a["id"]], edu).status_code == 409
    other = make_admin("educator")
    assert bulk(other, [a["id"]], other).status_code == 403  # educators cannot assign
    assert bulk(admin, [], other).status_code == 422
    assert bulk(admin, [a["id"]], other, requirements="   ").status_code == 422


def test_bulk_assignment_survives_a_mail_failure(make_admin, monkeypatch):
    admin, edu = make_admin("content_admin"), make_admin("educator")

    def broken(*args, **kwargs):
        raise ApiError(503, "unavailable", "mail down")

    monkeypatch.setattr(email_service, "send_bulk_activity_assignment", broken)
    r = bulk(admin, [new_draft(admin)["id"]], edu)
    assert r.status_code == 200 and r.json()["assigned"] == 1 and r.json()["email_sent"] is False


# ------------------------------------------------------------------------------------------------ list filters
def test_activity_list_filters_by_level_and_allows_a_thousand_rows(make_admin):
    admin = make_admin("content_admin")
    l1 = new_draft(admin, {**MIN_DRAFT, "title": "Level one only"})
    l23 = new_draft(admin, {**MIN_DRAFT, "title": "Levels two to three", "level_from": "L2", "level_to": "L3"})

    def ids(level):
        r = admin.get("/v1/admin/activities", params={"level": level, "limit": 1001})
        assert r.status_code == 200, r.text
        return {x["id"] for x in r.json()}

    assert l1["id"] in ids("L1") and l23["id"] not in ids("L1")
    assert l23["id"] in ids("L2") and l1["id"] not in ids("L2")  # a range matches every level inside it
    assert l23["id"] in ids("L3")
    assert admin.get("/v1/admin/activities", params={"level": "L99"}).status_code == 422
    assert admin.get("/v1/admin/activities", params={"limit": 1002}).status_code == 422
