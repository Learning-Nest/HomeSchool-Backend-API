"""Picture library: pictures people choose to use are kept, found by their description and reused for free."""

import base64

import pytest
from sqlalchemy import select

from app.db import get_factory
from app.models import AiGeneration, Asset, LibraryPicture
from app.services import picture_library
from app.services.picture_library import query_tokens
from tests.test_ai_images import error_code, generate, upload
from tests.test_educators import new_draft
from tests.test_images import png


def make(user, description, color=(200, 30, 30), **options):
    """Generates a picture for a new draft and uploads it, which puts it in the library. Returns (activity, asset)."""
    a = new_draft(user)
    gen = generate(user, a["id"], description=description, **options)
    assert gen.status_code == 200, gen.text
    data = base64.b64decode(gen.json()["images"][0]["b64_json"])
    asset = upload(user, a["id"], data, gen.json()["generation_id"])
    assert asset.status_code == 201, asset.text
    return a, asset.json()


def library(user, **params):
    r = user.get("/v1/admin/library/pictures", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def reuse(user, activity_id, picture_id):
    return user.post(f"/v1/admin/activities/{activity_id}/assets/from-library", json={"picture_id": picture_id})


@pytest.fixture
def varied(fake_ai):
    """The fake service returns a different picture each time, so each description gets its own library entry."""
    counter = {"n": 0}
    original = fake_ai.respond

    def respond(request):
        counter["n"] += 1
        fake_ai.picture = base64.b64encode(
            png((64 + counter["n"], 48), color=(counter["n"] * 20 % 255, 80, 90))
        ).decode()
        return original(request)

    fake_ai.respond = respond
    return fake_ai


# ------------------------------------------------------------------------------------------------ filling the library
def test_a_used_ai_picture_joins_the_library_but_an_unused_one_does_not(make_admin, varied):
    edu = make_admin("educator")
    a = new_draft(edu)
    gen = generate(edu, a["id"], description="Three red apples in a wooden bowl", n=2, style="flat")
    assert gen.status_code == 200
    assert library(edu)["items"] == []  # generated, but nobody chose one yet

    asset = upload(edu, a["id"], base64.b64decode(gen.json()["images"][0]["b64_json"]), gen.json()["generation_id"])
    assert asset.status_code == 201
    items = library(edu)["items"]
    assert len(items) == 1
    pic = items[0]
    assert pic["description"] == "Three red apples in a wooden bowl" and pic["style"] == "flat"
    assert pic["uses"] == 1 and pic["created_by_name"] and pic["url"] and pic["score"] is None


def test_a_plain_upload_never_goes_into_the_library(make_admin, varied):
    edu = make_admin("educator")
    a = new_draft(edu)
    assert upload(edu, a["id"], png((50, 50))).status_code == 201
    assert library(edu)["items"] == []


def test_the_same_picture_is_kept_once(make_admin, varied):
    edu = make_admin("educator")
    a, asset = make(edu, "A red apple")
    gen = generate(edu, a["id"], description="Another try")
    # The same bytes come back from an upload, which the activity already holds: nothing new for the library either.
    first = base64.b64decode(varied.picture)
    assert upload(edu, a["id"], first, gen.json()["generation_id"]).status_code == 201
    assert len(library(edu)["items"]) == 2  # the first picture and this new one, never a duplicate of either
    again = upload(edu, a["id"], first, gen.json()["generation_id"])
    assert again.status_code == 201 and len(library(edu)["items"]) == 2


# ------------------------------------------------------------------------------------------------ finding pictures
def test_keyword_search_finds_by_description_and_ranks(make_admin, varied):
    edu = make_admin("educator")
    make(edu, "Three red apples in a wooden bowl")
    make(edu, "A green apple on a branch")
    make(edu, "A diagram of the water cycle")

    apples = library(edu, q="red apples")
    assert [i["description"] for i in apples["items"]] == ["Three red apples in a wooden bowl"]
    assert apples["mode"] == "keyword" and apples["items"][0]["score"] == 1.0

    both = library(edu, q="apple")
    assert {i["description"] for i in both["items"]} == {
        "Three red apples in a wooden bowl",
        "A green apple on a branch",
    }

    assert library(edu, q="volcano")["items"] == []
    assert len(library(edu)["items"]) == 3  # no query lists everything


def test_search_ignores_filler_words_and_special_characters(make_admin, varied):
    edu = make_admin("educator")
    make(edu, "100% fresh_water lake")
    assert library(edu, q="a picture of the lake")["items"][0]["description"] == "100% fresh_water lake"
    assert library(edu, q="100%")["items"]  # % is just a character, not a wildcard
    assert library(edu, q="5%")["items"] == []  # a wildcard would have matched everything
    assert library(edu, q="x_y")["items"] == []  # and so would an underscore
    assert library(edu, q="fresh_water")["items"]


def test_style_filter_paging_and_most_used_first(make_admin, varied):
    edu = make_admin("educator")
    make(edu, "Cat one", style="flat")
    make(edu, "Cat two", style="diagram")
    popular_activity, _ = make(edu, "Cat three", style="flat")
    cat_three = next(i for i in library(edu)["items"] if i["description"] == "Cat three")
    other = new_draft(edu)
    assert reuse(edu, other["id"], cat_three["id"]).status_code == 201

    assert [i["description"] for i in library(edu, q="cat", style="flat")["items"]][0] == "Cat three"
    assert {i["style"] for i in library(edu, style="flat")["items"]} == {"flat"}
    page1 = library(edu, limit=2)
    page2 = library(edu, limit=2, offset=2)
    assert len(page1["items"]) == 2 and page1["has_more"] is True
    assert len(page2["items"]) == 1 and page2["has_more"] is False


def test_everyone_on_staff_can_search_and_validation(make_admin, varied):
    edu, other, admin = make_admin("educator"), make_admin("educator"), make_admin("content_admin")
    make(edu, "A lighthouse")
    for user in (other, admin):
        assert library(user, q="lighthouse")["items"][0]["description"] == "A lighthouse"
    assert other.get("/v1/admin/library/pictures", params={"mode": "similar"}).status_code == 422
    assert other.get("/v1/admin/library/pictures", params={"style": "neon"}).status_code == 422
    assert other.get("/v1/admin/library/pictures", params={"limit": 500}).status_code == 422


def test_search_needs_a_staff_sign_in(client, make_admin):
    assert client.get("/v1/admin/library/pictures").status_code == 401


def test_capability_follows_the_ai_switch(make_admin, ai_on):
    edu = make_admin("educator")
    assert "images.library" in edu.get("/v1/admin/me").json()["capabilities"]


# ------------------------------------------------------------------------------------------------ using a picture
def test_reusing_a_picture_copies_it_into_the_activity_and_marks_it_as_ai(make_admin, varied):
    edu, other = make_admin("educator"), make_admin("educator")
    make(edu, "A lighthouse at night")
    pic = library(other, q="lighthouse")["items"][0]

    target = new_draft(other)
    before = len(varied.requests)
    r = reuse(other, target["id"], pic["id"])
    assert r.status_code == 201, r.text
    asset = r.json()
    assert asset["activity_id"] == target["id"] and asset["ai_generated"] is True and asset["url"]
    assert len(varied.requests) == before  # no new picture was paid for
    assert other.get(f"/v1/admin/activities/{target['id']}/assets").json()[0]["id"] == asset["id"]
    assert library(other, q="lighthouse")["items"][0]["uses"] == 2

    again = reuse(other, target["id"], pic["id"])
    assert again.status_code == 201 and again.json()["id"] == asset["id"]  # not added twice
    assert library(other, q="lighthouse")["items"][0]["uses"] == 2

    log = other.get(f"/v1/admin/activities/{target['id']}").json()
    assert log["id"] == target["id"]


def test_reusing_does_not_count_towards_a_daily_limit(make_admin, varied, monkeypatch):
    from app import config

    monkeypatch.setenv("AI_IMAGE_DAILY_LIMIT_PER_USER", "1")
    config.reset_settings_cache()
    edu = make_admin("educator")
    make(edu, "A lighthouse")  # uses the one picture allowed today
    pic = library(edu)["items"][0]
    for _ in range(3):
        assert reuse(edu, new_draft(edu)["id"], pic["id"]).status_code == 201
    a = new_draft(edu)
    assert generate(edu, a["id"], description="One more").status_code == 429


def test_the_reused_marker_survives_the_original_activity_being_removed(make_admin, varied):
    edu = make_admin("educator")
    first, _ = make(edu, "A lighthouse")
    pic = library(edu)["items"][0]
    target = new_draft(edu)
    asset = reuse(edu, target["id"], pic["id"]).json()
    with get_factory()() as db:
        original = db.scalar(select(AiGeneration).where(AiGeneration.activity_id == first["id"]))
        db.delete(original)
        db.commit()
        assert db.get(Asset, asset["id"]).ai_generated is True


def test_reuse_follows_the_normal_edit_rules(make_admin, varied):
    edu, other = make_admin("educator"), make_admin("educator")
    make(edu, "A lighthouse")
    pic = library(edu)["items"][0]
    mine = new_draft(edu)
    theirs = new_draft(other)
    assert reuse(edu, theirs["id"], pic["id"]).status_code == 404  # someone else's draft
    assert reuse(edu, mine["id"], "00000000-0000-0000-0000-000000000000").status_code == 404
    assert reuse(edu, mine["id"], "not-a-uuid").status_code == 422
    bad = edu.post(f"/v1/admin/activities/{mine['id']}/assets/from-library", json={"picture_id": pic["id"], "x": 1})
    assert bad.status_code == 422


def test_reuse_respects_the_activity_image_limit(make_admin, varied, monkeypatch):
    from app import config

    monkeypatch.setenv("ASSET_MAX_PER_ACTIVITY", "1")
    config.reset_settings_cache()
    edu = make_admin("educator")
    make(edu, "First picture")
    make(edu, "Second picture")
    first, second = (library(edu, q=w)["items"][0] for w in ("First", "Second"))
    target = new_draft(edu)
    assert reuse(edu, target["id"], first["id"]).status_code == 201
    r = reuse(edu, target["id"], second["id"])
    assert r.status_code == 413 and error_code(r) == "asset_too_large"


def test_reuse_when_the_stored_picture_is_missing(make_admin, varied):
    from app.services.storage import get_storage

    edu = make_admin("educator")
    make(edu, "A lighthouse")
    pic = library(edu)["items"][0]
    with get_factory()() as db:
        get_storage().delete(db.get(LibraryPicture, pic["id"]).blob_path)
    r = reuse(edu, new_draft(edu)["id"], pic["id"])
    assert r.status_code == 503 and error_code(r) == "storage_unavailable"


# ------------------------------------------------------------------------------------------------ the search seam
def test_query_tokens():
    assert query_tokens("A picture of the red apples!") == ["red", "apple"]
    assert query_tokens("boss glass") == ["boss", "glass"]  # "ss" endings are not plurals
    assert query_tokens("the of a") == []
    assert len(query_tokens(" ".join(f"word{i}" for i in range(30)))) == picture_library.MAX_QUERY_TOKENS


def test_a_new_search_method_plugs_in_without_touching_the_rest(make_admin, varied, monkeypatch):
    class Newest:
        def find(self, db, query, *, style, limit, offset):
            rows = db.scalars(select(LibraryPicture).order_by(LibraryPicture.created_at.desc()).limit(limit))
            return [picture_library.Found(p, 0.5) for p in rows]

    edu = make_admin("educator")
    make(edu, "A lighthouse")
    monkeypatch.setitem(picture_library.SEARCH_METHODS, "keyword", Newest())
    assert library(edu, q="anything at all")["items"][0]["score"] == 0.5
