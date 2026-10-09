"""Educator role: drafts, ownership, validation, submit/review, authorship fields, and the admin 'who did what' views."""

import copy
import uuid

from sqlalchemy import text

from app.services import email as email_service
from tests.helpers import DEFS

MIN_DRAFT = {
    "title": "Counting socks",
    "subject": "MAT",
    "level_from": "L1",
    "level_to": "L1",
    "duration_min": 8,
    "steps": [{"id": "s1", "type": "single_choice", "prompt": "How many socks?"}],
}


def valid_definition(title="Counting socks", slug=None):
    d = copy.deepcopy(DEFS["count-the-dogs"])
    d["title"] = title
    d["slug"] = slug or "will-be-replaced"
    return d


def new_draft(user, definition=None, **kw):
    r = user.post("/v1/admin/activities", json={"definition": definition or MIN_DRAFT}, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def valid_draft(user, title="Counting socks"):
    a = new_draft(user, {**MIN_DRAFT, "title": title})
    d = valid_definition(title, a["slug"])
    r = user.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": d})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ access
def test_roles_and_capabilities(parent, make_admin):
    edu = make_admin("educator")
    assert parent.get("/v1/admin/me").status_code == 403
    me = edu.get("/v1/admin/me").json()
    assert me["platform_role"] == "educator" and "activities.submit" in me["capabilities"]
    assert "activities.publish" not in me["capabilities"]
    assert "educators.manage" in make_admin("super_admin").get("/v1/admin/me").json()["capabilities"]
    assert "educators.manage" not in make_admin("content_admin").get("/v1/admin/me").json()["capabilities"]


def test_educators_cannot_use_admin_only_endpoints(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    assert edu.get("/v1/admin/stats").status_code == 403
    assert edu.get("/v1/admin/educators").status_code == 403
    assert edu.get("/v1/admin/activity-log").status_code == 403
    assert edu.post("/v1/admin/content/bundle", json={"version": "x", "dry_run": True}).status_code == 403
    assert edu.post(f"/v1/admin/activities/{a['id']}/status", json={"status": "published"}).status_code == 403
    assert edu.post("/v1/admin/educators", json={"email": "x@example.com", "full_name": "X"}).status_code == 403


# ------------------------------------------------------------------------------------------------ drafts and ownership
def test_create_draft_records_who_and_when(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    assert a["status"] == "draft" and a["source"] == "educator" and a["slug"] == "counting-socks"
    assert a["created_by"] == edu.user_id and a["created_by_name"] == "Admin"
    assert a["last_edited_by"] == edu.user_id and a["created_at"] and a["last_edited_at"]
    again = new_draft(edu)
    assert again["slug"] == "counting-socks-2"  # unique slug from the same title
    assert new_draft(make_admin("content_admin"))["source"] == "admin"


def test_draft_needs_only_the_basics(make_admin):
    edu = make_admin("educator")
    r = edu.post("/v1/admin/activities", json={"definition": {**MIN_DRAFT, "subject": "ZZZ"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "content_invalid"
    assert any("subject" in p for p in r.json()["error"]["problems"])


def test_educator_sees_only_their_own_activities(make_admin):
    one, two, admin = make_admin("educator"), make_admin("educator"), make_admin("content_admin")
    a = new_draft(one)
    assert two.get(f"/v1/admin/activities/{a['id']}").status_code == 404
    assert (
        two.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": a["definition"]}).status_code == 404
    )
    assert [x["id"] for x in one.get("/v1/admin/activities").json()] == [a["id"]]
    assert two.get("/v1/admin/activities").json() == []
    mine = admin.get("/v1/admin/activities", params={"author": one.user_id}).json()
    assert [x["id"] for x in mine] == [a["id"]] and mine[0]["created_by"] == one.user_id
    assert len(admin.get("/v1/admin/activities").json()) > 1


def test_lenient_save_for_drafts_strict_save_validates(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    d = {
        **a["definition"],
        "steps": [{"id": "s1", "type": "single_choice", "prompt": "Pick", "config": {"options": []}}],
    }
    path = f"/v1/admin/activities/{a['id']}/definition"
    assert edu.put(path, json={"definition": d}).status_code == 422  # strict is the default
    r = edu.put(path, params={"strict": "false"}, json={"definition": d})
    assert r.status_code == 200 and r.json()["is_validated"] is False
    assert edu.put(path, params={"strict": "false"}, json={"definition": {**d, "slug": "other"}}).status_code == 422


def test_validate_reports_problems_by_exercise(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    r = edu.post(f"/v1/admin/activities/{a['id']}/validate")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["problems"]
    dry = edu.post("/v1/admin/activities/validate", json={"definition": valid_definition("x", a["slug"])})
    assert dry.json()["ok"] is True  # a dry run saves and records nothing
    assert edu.get(f"/v1/admin/activities/{a['id']}").json()["last_validated_at"] is not None


# ------------------------------------------------------------------------------------------------ submit and review
def test_submit_needs_a_passing_validation_of_the_current_content(make_admin):
    edu = make_admin("educator")
    a = valid_draft(edu)
    sub = f"/v1/admin/activities/{a['id']}/submit"
    r = edu.post(sub)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_required"
    assert edu.post(f"/v1/admin/activities/{a['id']}/validate").json()["ok"] is True
    assert edu.get(f"/v1/admin/activities/{a['id']}").json()["is_validated"] is True
    d = copy.deepcopy(a["definition"])
    d["title"] = "Counting socks, edited"
    assert edu.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": d}).status_code == 200
    assert edu.get(f"/v1/admin/activities/{a['id']}").json()["is_validated"] is False
    assert edu.post(sub).json()["error"]["code"] == "validation_required"  # an edit clears the validation


def test_review_cycle_return_edit_resubmit_publish(make_admin):
    edu, admin = make_admin("educator"), make_admin("content_admin")
    a = valid_draft(edu)
    aid = a["id"]
    assert edu.post(f"/v1/admin/activities/{aid}/validate").json()["ok"]
    r = edu.post(f"/v1/admin/activities/{aid}/submit")
    assert r.status_code == 200 and r.json()["status"] == "in_review" and r.json()["submitted_at"]
    locked = edu.put(f"/v1/admin/activities/{aid}/definition", json={"definition": a["definition"]})
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "activity_locked"
    back = admin.post(f"/v1/admin/activities/{aid}/status", json={"status": "draft", "note": "Add a hint"})
    assert back.json()["status"] == "draft" and back.json()["review_note"] == "Add a hint"
    assert back.json()["reviewed_by"] == admin.user_id
    assert edu.get(f"/v1/admin/activities/{aid}").json()["review_note"] == "Add a hint"
    edu.post(f"/v1/admin/activities/{aid}/validate")
    again = edu.post(f"/v1/admin/activities/{aid}/submit")
    assert again.status_code == 200 and again.json()["review_note"] is None
    pub = admin.post(f"/v1/admin/activities/{aid}/status", json={"status": "published"})
    assert pub.status_code == 200 and pub.json()["status"] == "published" and pub.json()["reviewed_by_name"]
    assert edu.put(f"/v1/admin/activities/{aid}/definition", json={"definition": a["definition"]}).status_code == 409


# ------------------------------------------------------------------------------------------------ admin views
def test_admin_sees_each_educators_work(make_admin):
    edu, other, admin = make_admin("educator"), make_admin("educator"), make_admin("content_admin")
    a = valid_draft(edu)
    edu.post(f"/v1/admin/activities/{a['id']}/validate")
    edu.post(f"/v1/admin/activities/{a['id']}/submit")
    admin.post(f"/v1/admin/activities/{a['id']}/status", json={"status": "draft", "note": "again"})
    new_draft(other)
    rows = {r["id"]: r for r in admin.get("/v1/admin/educators").json()}
    mine = rows[edu.user_id]
    assert (mine["activities"], mine["drafts"], mine["submissions"], mine["returned"]) == (1, 1, 1, 1)
    assert mine["last_active_at"] and rows[other.user_id]["activities"] == 1
    detail = admin.get(f"/v1/admin/educators/{edu.user_id}/activity").json()
    assert [x["id"] for x in detail["activities"]] == [a["id"]]
    actions = [e["action"] for e in detail["events"]]
    assert {"created", "edited", "validated", "submitted"} <= set(actions)
    assert all(e["actor_id"] == edu.user_id for e in detail["events"])
    log = admin.get("/v1/admin/activity-log", params={"action": "returned"}).json()
    assert [e["action"] for e in log] == ["returned"] and log[0]["actor_id"] == admin.user_id
    csv_resp = admin.get("/v1/admin/activity-log", params={"format": "csv", "actor_id": edu.user_id})
    assert csv_resp.headers["content-type"].startswith("text/csv") and "submitted" in csv_resp.text
    assert admin.get(f"/v1/admin/educators/{admin.user_id}/activity").status_code == 404  # not an educator


def test_edits_in_a_row_are_one_log_entry(make_admin):
    edu, admin = make_admin("educator"), make_admin("content_admin")
    a = valid_draft(edu)
    path = f"/v1/admin/activities/{a['id']}/definition"
    for n in range(3):
        d = copy.deepcopy(a["definition"])
        d["summary"] = f"version {n}"
        assert edu.put(path, json={"definition": d}).status_code == 200
    edited = admin.get("/v1/admin/activity-log", params={"action": "edited", "activity_id": a["id"]}).json()
    assert len(edited) == 1 and edited[0]["detail"]["saves"] >= 3


# ------------------------------------------------------------------------------------------------ account management
def test_super_admin_creates_and_deactivates_educators(client, make_admin):
    boss, plain = make_admin("super_admin"), make_admin("content_admin")
    email = f"new-{uuid.uuid4().hex[:6]}@example.com"
    assert plain.post("/v1/admin/educators", json={"email": email, "full_name": "Nia"}).status_code == 403
    email_service.OUTBOX.clear()
    r = boss.post("/v1/admin/educators", json={"email": email, "full_name": "Nia Teacher"})
    assert r.status_code == 201 and r.json()["email_sent"] and not r.json()["existing_account"]
    temp = email_service.OUTBOX[-1].body.split("    ")[1].split("\n")[0].strip()
    login = client.post("/v1/auth/login", json={"email": email, "password": temp})
    assert login.status_code == 200 and login.json()["temp_login"] is True
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/v1/admin/me", headers=headers).json()["error"]["code"] == "password_change_required"
    new_pw = "a-much-better-password"
    ok = client.post(
        "/v1/auth/change-password", headers=headers, json={"current_password": temp, "new_password": new_pw}
    )
    assert ok.status_code in (200, 204), ok.text
    relog = client.post("/v1/auth/login", json={"email": email, "password": new_pw}).json()
    me = client.get("/v1/admin/me", headers={"Authorization": f"Bearer {relog['access_token']}"})
    assert me.status_code == 200 and me.json()["platform_role"] == "educator"
    uid = r.json()["educator"]["id"]
    off = boss.patch(f"/v1/admin/educators/{uid}", json={"active": False})
    assert off.status_code == 200 and off.json()["active"] is False
    assert client.post("/v1/auth/login", json={"email": email, "password": new_pw}).status_code == 401
    assert boss.patch(f"/v1/admin/educators/{uid}", json={"active": True}).json()["active"] is True
    assert client.post("/v1/auth/login", json={"email": email, "password": new_pw}).status_code == 200


def test_existing_parent_account_can_be_upgraded_and_admins_cannot(make_admin, parent):
    boss = make_admin("super_admin")
    r = boss.post("/v1/admin/educators", json={"email": parent.email, "full_name": "Parent"})
    assert r.status_code == 201 and r.json()["existing_account"] is True and not r.json()["email_sent"]
    assert parent.get("/v1/admin/me").json()["platform_role"] == "educator"
    other_admin = make_admin("content_admin")
    assert boss.post("/v1/admin/educators", json={"email": other_admin.email, "full_name": "A"}).status_code == 409


def test_events_survive_when_users_are_removed(database, make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    with database["admin"].begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))
    with database["admin"].begin() as conn:
        n = conn.execute(text("select count(*) from content.activities where id = :i"), {"i": a["id"]}).scalar()
    assert n == 1  # no foreign keys from content to users
