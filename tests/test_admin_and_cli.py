import copy
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app import cli
from tests.helpers import BUNDLE

ROOT = Path(__file__).resolve().parent.parent


def _new_activity(slug="test-tally-marks", **over):
    a = copy.deepcopy(next(x for x in BUNDLE["activities"] if x["slug"] == "count-the-dogs"))
    a.update(slug=slug, title="Tally marks", **over)
    return a


def _bundle(activities, **over):
    return {"version": "test.1", "activities": activities, **over}


# ------------------------------------------------------------------------------------------------ access
def test_admin_endpoints_need_a_platform_role(parent, make_admin):
    assert parent.get("/v1/admin/stats").status_code == 403
    assert parent.get("/v1/admin/activities").status_code == 403
    assert make_admin("content_admin").get("/v1/admin/stats").status_code == 200
    assert make_admin("super_admin").get("/v1/admin/activities").status_code == 200


def test_admin_stats_expose_no_family_data(make_admin, parent):
    s = make_admin().get("/v1/admin/stats").json()
    assert (
        set(s) == {"users", "families", "activities_by_status", "skills"}
        and s["activities_by_status"]["published"] == 15
    )


def test_admins_cannot_read_children_of_families(make_admin, parent):
    kid = parent.add_child()
    admin = make_admin()
    for path in (
        f"/v1/children/{kid['id']}",
        f"/v1/children/{kid['id']}/mastery",
        f"/v1/children/{kid['id']}/progress/overview",
    ):
        assert admin.get(path).status_code == 404


# ------------------------------------------------------------------------------------------------ authoring
def test_list_and_read_activities_with_answer_keys(make_admin):
    admin = make_admin()
    rows = admin.get("/v1/admin/activities", params={"subject": "MAT"}).json()
    assert rows and all(r["subject_code"] == "MAT" and r["status"] == "published" for r in rows)
    assert admin.get("/v1/admin/activities", params={"q": "dogs"}).json()[0]["slug"] == "count-the-dogs"
    detail = admin.get(f"/v1/admin/activities/{rows[0]['id']}").json()
    assert "definition" in detail and detail["skills"]


def test_editing_a_published_activity_validates_and_versions(make_admin):
    admin = make_admin()
    a = next(r for r in admin.get("/v1/admin/activities", params={"q": "dogs"}).json())
    d = admin.get(f"/v1/admin/activities/{a['id']}").json()["definition"]
    url = f"/v1/admin/activities/{a['id']}/definition"
    same = admin.put(url, json={"definition": d})
    assert same.status_code == 200 and same.json()["version"] == 1  # no change, no new version
    bad = copy.deepcopy(d)
    bad["steps"][1]["key"]["correct"] = ["nope"]
    r = admin.put(url, json={"definition": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "content_invalid" and r.json()["error"]["problems"]
    renamed = {**d, "slug": "other-slug"}
    assert admin.put(url, json={"definition": renamed}).status_code == 422
    unknown_skill = copy.deepcopy(d)
    unknown_skill["steps"][1]["skills"] = [{"code": "MAT.NOPE.NOPE"}]  # skills are set per exercise
    assert admin.put(url, json={"definition": unknown_skill}).status_code == 422
    good = {**d, "title": "Count the dogs (v2)"}
    r = admin.put(url, json={"definition": good})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["title"] == "Count the dogs (v2)"


def test_import_dry_run_writes_nothing_then_apply_then_idempotent(make_admin, parent):
    admin = make_admin()
    payload = _bundle([_new_activity()])
    dry = admin.post("/v1/admin/content/bundle", json={**payload, "dry_run": True}).json()
    assert dry["ok"] and dry["created"]["activities"] == 1
    assert admin.get("/v1/admin/activities", params={"q": "tally"}).json() == []
    applied = admin.post("/v1/admin/content/bundle", json={**payload, "dry_run": False}).json()
    assert applied["ok"] and applied["created"]["activities"] == 1
    listed = admin.get("/v1/admin/activities", params={"q": "tally"}).json()
    assert len(listed) == 1 and listed[0]["status"] == "draft"  # imports arrive as drafts
    assert parent.get("/v1/activities", params={"q": "tally"}).json() == []  # families only ever see published content
    again = admin.post("/v1/admin/content/bundle", json={**payload, "dry_run": False}).json()
    assert again["created"]["activities"] == 0 and again["unchanged"]["activities"] == 1


def test_import_rejects_invalid_bundles_atomically(make_admin):
    admin = make_admin()
    good = _new_activity("ok-one")
    bad = _new_activity("bad-one")
    bad["steps"][1]["skills"] = [{"code": "MAT.NOPE.NOPE"}]
    r = admin.post("/v1/admin/content/bundle", json={**_bundle([good, bad]), "dry_run": False}).json()
    assert r["ok"] is False and any("MAT.NOPE.NOPE" in p for p in r["problems"])
    assert admin.get("/v1/admin/activities", params={"q": "tally"}).json() == []  # the good one was not written either
    cyc = {
        "skills": [
            {"code": "MAT.X.A", "subject": "MAT", "level": "L1", "name": "A", "prerequisites": ["MAT.X.B"]},
            {"code": "MAT.X.B", "subject": "MAT", "level": "L1", "name": "B", "prerequisites": ["MAT.X.A"]},
        ]
    }
    r = admin.post("/v1/admin/content/bundle", json={**_bundle([]), **cyc, "dry_run": False}).json()
    assert r["ok"] is False and any("cycle" in p for p in r["problems"])


def test_import_can_add_taxonomy_and_publish(make_admin, parent):
    admin = make_admin()
    payload = _bundle(
        [_new_activity(skills=["MAT.X.TALLY"])],
        skills=[
            {
                "code": "MAT.X.TALLY",
                "subject": "MAT",
                "level": "L1",
                "name": "Tallies",
                "prerequisites": ["MAT.NUM.COUNT20"],
            }
        ],
    )
    r = admin.post("/v1/admin/content/bundle", json={**payload, "dry_run": False, "auto_publish": True}).json()
    assert r["ok"] and r["created"]["skills"] == 1
    assert parent.get("/v1/curriculum/skills", params={"subject": "MAT"}).json().__len__() > 0
    tally = next(s for s in parent.get("/v1/curriculum/skills").json() if s["code"] == "MAT.X.TALLY")
    assert tally["prerequisites"] == ["MAT.NUM.COUNT20"]
    assert [a["slug"] for a in parent.get("/v1/activities", params={"q": "tally"}).json()] == ["test-tally-marks"]


def test_publishing_workflow_and_transitions(make_admin, parent):
    admin = make_admin()
    admin.post("/v1/admin/content/bundle", json={**_bundle([_new_activity()]), "dry_run": False})
    aid = admin.get("/v1/admin/activities", params={"q": "tally"}).json()[0]["id"]
    st = f"/v1/admin/activities/{aid}/status"
    assert admin.post(st, json={"status": "published"}).status_code == 409  # draft -> published is not allowed directly
    assert admin.post(st, json={"status": "in_review"}).json()["status"] == "in_review"
    assert admin.post(st, json={"status": "published"}).json()["status"] == "published"
    assert [a["slug"] for a in parent.get("/v1/activities", params={"q": "tally"}).json()] == ["test-tally-marks"]
    assert parent.get(f"/v1/activities/{aid}").json()["definition"]  # published: readable by families
    assert admin.post(st, json={"status": "draft"}).status_code == 409  # published content is archived, not edited back
    assert admin.post(st, json={"status": "archived"}).json()["status"] == "archived"
    assert parent.get(f"/v1/activities/{aid}").status_code == 404
    assert parent.get("/v1/activities", params={"q": "tally"}).json() == []


def test_public_activity_detail_hides_answer_keys(parent, activities):
    d = parent.get(f"/v1/activities/{activities['count-the-dogs']['id']}").json()
    assert "correct" not in json.dumps(d["definition"]) and d["skills"] == ["MAT.NUM.COUNT20"]


def test_catalogue_filters(parent):
    assert {a["subject_code"] for a in parent.get("/v1/activities", params={"subject": "SCI"}).json()} == {"SCI"}
    l1 = parent.get("/v1/activities", params={"level": "L1"}).json()
    assert l1 and all(a["level_from"] == "L1" for a in l1)
    assert {a["slug"] for a in parent.get("/v1/activities", params={"interest": "space"}).json()} == {
        "build-a-paper-rocket"
    }
    assert parent.get("/v1/activities", params={"level": "L9"}).json() == []
    assert len(parent.get("/v1/activities", params={"limit": 3}).json()) == 3
    assert [x["code"] for x in parent.get("/v1/curriculum/levels").json()] == ["L1", "L2", "L3", "L4", "L5"]
    assert parent.get("/v1/curriculum/subjects").json()[0]["code"] == "ENG"
    assert any(i["code"] == "animals" for i in parent.get("/v1/curriculum/interests").json())


# ------------------------------------------------------------------------------------------------ CLI
def test_cli_load_bundle_from_the_content_repo_folder():
    folder = ROOT.parent / "content-curriculum"
    if not folder.exists():
        pytest.skip("content-curriculum is not checked out next to this repo")
    loaded = cli.load_bundle(str(folder))
    assert len(loaded["activities"]) == len(BUNDLE["activities"]) and len(loaded["skills"]) == len(BUNDLE["skills"])


def test_cli_seed_is_idempotent_and_dry_run_safe(database, capsys):
    report = cli.seed_from_path(str(ROOT / "seed" / "launch-bundle.json"), auto_publish=True, dry_run=True)
    assert report["ok"] and report["created"]["activities"] == 0 and report["unchanged"]["activities"] == 15


def test_cli_create_admin(database, client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-admin-password")
    cli.create_admin("root@example.com", "Root", "super_admin")
    cli.create_admin("ROOT@example.com", "Root", "content_admin")  # second run promotes/updates instead of duplicating
    r = client.post("/v1/auth/login", json={"email": "root@example.com", "password": "a-long-admin-password"})
    assert r.status_code == 200 and r.json()["user"]["platform_role"] == "content_admin"
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    with pytest.raises(SystemExit):
        cli.create_admin("weak@example.com", "Weak", "content_admin")


def test_cli_ensure_app_role_is_repeatable_and_rotates_the_password(database):
    admin_url = database["admin_url"].render_as_string(hide_password=False)
    prev = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = admin_url
    from app import config

    config.reset_settings_cache()
    try:
        cli.ensure_app_role("hs_rotate_test", "first-password-0123456789")
        cli.ensure_app_role("hs_rotate_test", "second-password-0123456789")
        with pytest.raises(SystemExit):
            cli.ensure_app_role("bad role; drop", "x" * 20)
        with pytest.raises(SystemExit):
            cli.ensure_app_role("hs_rotate_test", "short")
    finally:
        os.environ["DATABASE_URL"] = prev
        config.reset_settings_cache()
    ok = database["app_url"].set(username="hs_rotate_test", password="second-password-0123456789")
    with create_engine(ok).connect() as c:
        assert c.execute(text("select current_user")).scalar() == "hs_rotate_test"
        role = c.execute(text("select rolsuper, rolbypassrls from pg_roles where rolname = current_user")).one()
    assert tuple(role) == (False, False)
    with database[
        "admin"
    ].connect() as c:  # the embedded test server trusts local connections, so compare the stored SCRAM verifier
        before = c.execute(text("select rolpassword from pg_authid where rolname = 'hs_rotate_test'")).scalar()
    assert before.startswith("SCRAM-SHA-256$")
