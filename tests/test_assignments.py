"""Assignments: an admin hands a draft to an educator with written requirements; the educator is emailed, works on the
draft, and the assignment follows the activity (submitted, sent back, published, archived)."""

from datetime import date, timedelta

from sqlalchemy import text

from app.errors import ApiError
from app.services import assignments as assignment_service
from app.services import email as email_service
from tests.test_educators import new_draft, valid_draft

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
