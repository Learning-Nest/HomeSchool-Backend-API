"""Activity images: upload and re-encode, references in a definition, download links in what the app receives."""

import copy
import io
import uuid

import pytest
from PIL import Image

from app.config import get_settings
from app.services import content
from app.services.storage import LocalStorage
from tests.helpers import DEFS
from tests.test_educators import MIN_DRAFT, new_draft, valid_definition


def png(size=(1600, 1200), color=(200, 30, 30), mode="RGB", fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format=fmt)
    return buf.getvalue()


def upload(user, activity_id, data, ctype="image/png"):
    return user.post(
        "/v1/admin/assets", params={"activity_id": activity_id}, content=data, headers={"Content-Type": ctype}
    )


def with_images(definition, option_asset, step_asset=None):
    """count-the-dogs with a picture on every option of s2 (and optionally on step s2)."""
    d = copy.deepcopy(definition)
    step = next(s for s in d["steps"] if s["id"] == "s2")
    for o in step["config"]["options"]:
        o["image"] = {"asset": option_asset, "alt": f"{o['label']} dogs"}
    if step_asset:
        step["image"] = {"asset": step_asset, "alt": "A park"}
    return d


# ------------------------------------------------------------------------------------------------ upload
def test_upload_reencodes_to_a_small_webp(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    r = upload(edu, a["id"], png())
    assert r.status_code == 201, r.text
    asset = r.json()
    assert asset["content_type"] == "image/webp" and max(asset["width"], asset["height"]) == 1024
    assert asset["width"] == 1024 and asset["height"] == 768 and asset["bytes"] < 20_000
    assert asset["url"] and len(asset["sha256"]) == 64
    again = upload(edu, a["id"], png())
    assert again.json()["id"] == asset["id"]  # the same picture is stored once
    listed = edu.get(f"/v1/admin/activities/{a['id']}/assets").json()
    assert [x["id"] for x in listed] == [asset["id"]]


def test_upload_strips_metadata_and_handles_rotation_and_alpha(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu)
    img = Image.new("RGB", (60, 30), (0, 128, 0))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees
    exif[0x010F] = "Secret Camera Co"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    out = upload(edu, a["id"], buf.getvalue(), "image/jpeg").json()
    assert (out["width"], out["height"]) == (30, 60)  # orientation applied
    blob = edu.get(out["url"]).content
    assert b"Secret Camera Co" not in blob and Image.open(io.BytesIO(blob)).format == "WEBP"
    assert upload(edu, a["id"], png(mode="RGBA", color=(0, 0, 0, 0)), "image/png").status_code == 201


@pytest.mark.parametrize(
    "data,ctype,status",
    [
        (b"not an image at all", "image/png", 415),
        (png(), "application/pdf", 415),
        (b"GIF89a" + b"\x00" * 20, "image/png", 415),
        (b"", "image/png", 422),
    ],
)
def test_upload_rejects_bad_files(make_admin, data, ctype, status):
    edu = make_admin("educator")
    a = new_draft(edu)
    assert upload(edu, a["id"], data, ctype).status_code == status


def test_upload_limits(make_admin, monkeypatch):
    edu = make_admin("educator")
    a = new_draft(edu)
    s = get_settings()
    monkeypatch.setattr(s, "asset_max_upload_bytes", 500)
    r = upload(edu, a["id"], png(size=(300, 300), color=(1, 2, 3)) + b"0" * 1000)
    assert r.status_code == 413 and r.json()["error"]["code"] == "asset_too_large"
    monkeypatch.undo()
    monkeypatch.setattr(s, "asset_max_per_activity", 1)
    assert upload(edu, a["id"], png(color=(1, 1, 1))).status_code == 201
    assert upload(edu, a["id"], png(color=(2, 2, 2))).json()["error"]["code"] == "asset_too_large"


def test_upload_permissions(parent, make_admin):
    one, two = make_admin("educator"), make_admin("educator")
    a = new_draft(one)
    assert upload(parent, a["id"], png()).status_code == 403
    assert upload(two, a["id"], png()).status_code == 404  # not their activity
    asset = upload(one, a["id"], png()).json()
    assert two.get(f"/v1/admin/activities/{a['id']}/assets").status_code == 404
    assert two.delete(f"/v1/admin/assets/{asset['id']}").status_code == 404
    admin = make_admin("content_admin")
    assert upload(admin, a["id"], png(color=(9, 9, 9))).status_code == 201


def test_deployed_environments_refuse_uploads_without_cloud_storage(make_admin, monkeypatch):
    edu = make_admin("educator")
    a = new_draft(edu)
    monkeypatch.setattr(get_settings(), "app_env", "nonprod")
    r = upload(edu, a["id"], png())
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"


# ------------------------------------------------------------------------------------------------ references
def test_definition_with_images_validates_and_checks_the_asset(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu, {**MIN_DRAFT, "title": "Dogs with pictures"})
    asset = upload(edu, a["id"], png()).json()
    good = with_images(valid_definition("Dogs with pictures", a["slug"]), asset["id"])
    assert content.validate_activity(good) == []
    ok = edu.post("/v1/admin/activities/validate", json={"definition": good, "activity_id": a["id"]}).json()
    assert ok["ok"], ok
    missing = with_images(good, "11111111-1111-4111-8111-111111111111")
    bad = edu.post("/v1/admin/activities/validate", json={"definition": missing, "activity_id": a["id"]}).json()
    assert not bad["ok"] and any("not found" in p["message"] for p in bad["problems"])
    no_alt = copy.deepcopy(good)
    no_alt["steps"][1]["config"]["options"][0]["image"] = {"asset": asset["id"]}
    assert content.validate_activity(no_alt)
    other = new_draft(edu, {**MIN_DRAFT, "title": "Other"})
    foreign = edu.post("/v1/admin/activities/validate", json={"definition": good, "activity_id": other["id"]}).json()
    assert any("different activity" in p["message"] for p in foreign["problems"])


def test_images_cannot_arrive_through_a_bundle_for_a_new_activity(make_admin):
    admin = make_admin("content_admin")
    d = with_images(DEFS["count-the-dogs"], "11111111-1111-4111-8111-111111111111")
    d["slug"] = "bundle-with-pictures"
    r = admin.post("/v1/admin/content/bundle", json={"version": "t.1", "activities": [d], "dry_run": True}).json()
    assert r["ok"] is False and any("image" in p for p in r["problems"])


def test_delete_asset_only_when_unused(make_admin):
    edu = make_admin("educator")
    a = new_draft(edu, {**MIN_DRAFT, "title": "Delete test"})
    asset = upload(edu, a["id"], png()).json()
    d = with_images(valid_definition("Delete test", a["slug"]), asset["id"])
    assert edu.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": d}).status_code == 200
    r = edu.delete(f"/v1/admin/assets/{asset['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "asset_in_use"
    plain = valid_definition("Delete test", a["slug"])
    assert edu.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": plain}).status_code == 200
    assert edu.delete(f"/v1/admin/assets/{asset['id']}").status_code == 204
    assert edu.get(f"/v1/admin/activities/{a['id']}/assets").json() == []
    again = edu.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": d})
    assert again.status_code == 422  # the deleted image can no longer be referenced


# ------------------------------------------------------------------------------------------------ what the app receives
def publish_with_images(make_admin, picture_step=True):
    edu, admin = make_admin("educator"), make_admin("content_admin")
    a = new_draft(edu, {**MIN_DRAFT, "title": "Dogs with pictures"})
    one = upload(edu, a["id"], png(color=(255, 0, 0))).json()
    two = upload(edu, a["id"], png(color=(0, 0, 255))).json()
    d = with_images(valid_definition("Dogs with pictures", a["slug"]), one["id"], two["id"] if picture_step else None)
    assert edu.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": d}).status_code == 200
    assert edu.post(f"/v1/admin/activities/{a['id']}/validate").json()["ok"]
    assert edu.post(f"/v1/admin/activities/{a['id']}/submit").status_code == 200
    assert admin.post(f"/v1/admin/activities/{a['id']}/status", json={"status": "published"}).status_code == 200
    return a, one, two


def test_app_gets_a_manifest_and_links_that_download(client, parent, make_admin):
    a, one, two = publish_with_images(make_admin)
    r = parent.get(f"/v1/activities/{a['id']}")
    assert r.status_code == 200
    wire = r.json()["definition"]
    manifest = {m["id"]: m for m in wire["images"]}
    assert set(manifest) == {one["id"], two["id"]}
    step = next(s for s in wire["steps"] if s["id"] == "s2")
    assert step["image"]["sha256"] == two["sha256"] and step["image"]["alt"] == "A park"
    assert all(o["image"]["asset"] == one["id"] and o["image"]["width"] for o in step["options"])
    assert "correct" not in step and "key" not in step  # answers still never leave the server
    got = client.get(manifest[one["id"]]["url"])  # anyone holding the signed link can fetch it
    assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
    assert len(got.content) == manifest[one["id"]]["bytes"]


def test_session_responses_carry_the_same_manifest(parent, make_admin):
    a, one, two = publish_with_images(make_admin)
    kid = parent.add_child("K", "L1")
    r = parent.post(
        "/v1/sessions",
        json={"child_id": kid["id"], "activity_id": a["id"], "plan_item_id": None, "client_op_id": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    defn = r.json()["activity_definition"]
    assert {m["id"] for m in defn["images"]} == {one["id"], two["id"]}


def test_activities_without_images_have_no_manifest(parent, activities):
    d = parent.get(f"/v1/activities/{activities['count-the-dogs']['id']}").json()["definition"]
    assert "images" not in d


def test_published_versions_keep_their_pictures_when_the_draft_changes(parent, make_admin):
    a, one, _ = publish_with_images(make_admin)
    admin = make_admin("content_admin")
    plain = valid_definition("Dogs with pictures", a["slug"])
    assert admin.put(f"/v1/admin/activities/{a['id']}/definition", json={"definition": plain}).status_code == 200
    assert admin.get(f"/v1/admin/activities/{a['id']}").json()["version"] == 2
    # the frozen version 1 still refers to the first image, so deleting it is refused
    assert admin.delete(f"/v1/admin/assets/{one['id']}").json()["error"]["code"] == "asset_in_use"


# ------------------------------------------------------------------------------------------------ storage layer
def test_signed_local_links_cannot_be_forged_or_reused_after_expiry(client, tmp_path):
    st = LocalStorage(str(tmp_path), "secret")
    st.put("abc/def.webp", b"data", "image/webp")
    assert st.get("abc/def.webp") == b"data"
    url = st.url("abc/def.webp", 60)
    exp = int(url.split("exp=")[1].split("&")[0])
    sig = url.split("sig=")[1]
    assert st.verify("abc/def.webp", exp, sig)
    assert not st.verify("abc/other.webp", exp, sig) and not st.verify("abc/def.webp", exp, "0" * 64)
    assert not st.verify("abc/def.webp", exp - 120, sig)
    with pytest.raises(ValueError):
        st.put("../escape.webp", b"x", "image/webp")
    assert client.get("/v1/assets/local/abc/def.webp", params={"exp": exp, "sig": "bad"}).status_code == 404
