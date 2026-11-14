"""AI pictures: the generate endpoint (against a fake CustomAI service), marking uploaded pictures as AI-made, access
rules, failure mapping and the optional daily limit. Nothing here reaches the real service or OpenAI."""

import base64

import httpx
import pytest

from app import config
from app.config import Settings
from app.services import ai_images
from tests.conftest import AI_KEY as KEY  # noqa: E402
from tests.test_educators import new_draft, valid_draft
from tests.test_images import png


def generate(user, activity_id, **body):
    return user.post(f"/v1/admin/activities/{activity_id}/images/generate", json={"description": "A red apple", **body})


def upload(user, activity_id, data, generation_id=None):
    params = {"activity_id": activity_id, **({"generation_id": generation_id} if generation_id else {})}
    return user.post("/v1/admin/assets", params=params, content=data, headers={"Content-Type": "image/png"})


def error_code(r) -> str:
    return r.json()["error"]["code"]


# ------------------------------------------------------------------------------------------------ the happy path
def test_educator_generates_a_picture_for_their_draft(make_admin, fake_ai):
    edu = make_admin("educator")
    a = new_draft(edu)
    r = generate(edu, a["id"], description="  Three red apples  ", style="diagram", n=2, avoid_text=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["images"]) == 2 and body["images"][0]["mime_type"] == "image/png"
    assert base64.b64decode(body["images"][0]["b64_json"]) == png((64, 48))
    assert body["generation_id"] and body["remaining_today"] is None  # no daily limit by default
    sent = fake_ai.sent[0]
    assert sent == {
        "description": "Three red apples",
        "style": "diagram",
        "size": "1024x1024",
        "quality": "medium",
        "n": 2,
        "output_format": "png",
        "avoid_text": True,
    }
    assert fake_ai.requests[0].headers["x-api-key"] == KEY
    # Nothing is stored until the picture is uploaded.
    assert edu.get(f"/v1/admin/activities/{a['id']}/assets").json() == []


def test_the_request_is_in_the_activity_log_with_what_was_asked(make_admin, fake_ai):
    admin, edu = make_admin("content_admin"), make_admin("educator")
    a = new_draft(edu)
    gen_id = generate(edu, a["id"]).json()["generation_id"]
    log = admin.get("/v1/admin/activity-log", params={"activity_id": a["id"], "action": "image_generated"}).json()
    assert len(log) == 1 and log[0]["detail"]["generation"] == gen_id and log[0]["detail"]["images"] == 1


# ------------------------------------------------------------------------------------------------ keeping a picture
def test_a_kept_generated_picture_is_marked_as_made_with_ai(make_admin, fake_ai):
    edu, admin = make_admin("educator"), make_admin("content_admin")
    a = new_draft(edu)
    gen_id = generate(edu, a["id"]).json()["generation_id"]
    kept = upload(edu, a["id"], png((64, 48)), gen_id)
    assert kept.status_code == 201, kept.text
    assert kept.json()["ai_generated"] is True
    plain = upload(edu, a["id"], png((50, 50), color=(0, 0, 200))).json()
    assert plain["ai_generated"] is False
    listed = {x["id"]: x["ai_generated"] for x in edu.get(f"/v1/admin/activities/{a['id']}/assets").json()}
    assert listed == {kept.json()["id"]: True, plain["id"]: False}
    added = admin.get("/v1/admin/activity-log", params={"activity_id": a["id"], "action": "image_added"}).json()
    assert sorted(bool(x["detail"].get("ai_generated")) for x in added) == [False, True]


def test_a_generation_id_must_belong_to_this_user_and_activity(make_admin, fake_ai):
    edu, other = make_admin("educator"), make_admin("educator")
    a, b, theirs = new_draft(edu), new_draft(edu), new_draft(other)
    gen_id = generate(edu, a["id"]).json()["generation_id"]
    assert upload(edu, b["id"], png(), gen_id).status_code == 422  # another activity
    assert upload(edu, a["id"], png(), "00000000-0000-4000-8000-000000000001").status_code == 422  # unknown
    other_gen = generate(other, theirs["id"]).json()["generation_id"]
    assert upload(edu, a["id"], png(), other_gen).status_code == 422  # someone else's
    assert edu.get(f"/v1/admin/activities/{a['id']}/assets").json() == []


# ------------------------------------------------------------------------------------------------ who may ask
def test_generating_follows_the_rules_for_editing_the_activity(make_admin, fake_ai):
    admin, edu, other = make_admin("content_admin"), make_admin("educator"), make_admin("educator")
    mine = new_draft(edu)
    assert generate(other, mine["id"]).status_code == 404  # not theirs
    assert generate(admin, mine["id"]).status_code == 200  # an admin may help on any draft
    sent_for_review = valid_draft(edu, "Counting socks")
    assert edu.post(f"/v1/admin/activities/{sent_for_review['id']}/validate").json()["ok"]
    assert edu.post(f"/v1/admin/activities/{sent_for_review['id']}/submit").status_code == 200
    r = generate(edu, sent_for_review["id"])
    assert r.status_code == 409 and error_code(r) == "activity_locked"
    assert len(fake_ai.requests) == 1  # the refused calls never reached the service


def test_generating_needs_a_staff_sign_in(client, make_admin, fake_ai):
    a = new_draft(make_admin("educator"))
    assert client.post(f"/v1/admin/activities/{a['id']}/images/generate", json={"description": "x"}).status_code == 401


def test_invalid_requests_are_422_and_cost_nothing(make_admin, fake_ai):
    edu = make_admin("educator")
    a = new_draft(edu)
    for change in (
        {"description": ""},
        {"description": "   "},
        {"description": "x" * 4001},
        {"n": 3},
        {"n": 0},
        {"style": "neon"},
        {"size": "10x10"},
        {"quality": "ultra"},
        {"surprise": 1},
    ):
        assert generate(edu, a["id"], **change).status_code == 422, change
    assert fake_ai.requests == []


# ------------------------------------------------------------------------------------------------ switch and capability
def test_while_switched_off_nothing_is_offered_or_sent(make_admin, monkeypatch):
    edu = make_admin("educator")
    a = new_draft(edu)
    assert "images.generate" not in edu.get("/v1/admin/me").json()["capabilities"]
    r = generate(edu, a["id"])
    assert r.status_code == 503 and error_code(r) == "ai_unavailable"


def test_the_capability_appears_when_switched_on(make_admin, ai_on):
    for role in ("educator", "content_admin", "super_admin"):
        assert "images.generate" in make_admin(role).get("/v1/admin/me").json()["capabilities"]


def test_settings_refuse_a_half_configured_switch(monkeypatch):
    base = {"_env_file": None, "ai_image_enabled": True}
    with pytest.raises(ValueError, match="AI_SERVICE_URL"):
        Settings(**base, ai_service_url="", ai_service_api_key="k" * 30)
    with pytest.raises(ValueError, match="AI_SERVICE_API_KEY"):
        Settings(**base, ai_service_url="https://ai.test", ai_service_api_key="short")
    with pytest.raises(ValueError, match="AI_MAX_CONCURRENT"):
        Settings(_env_file=None, ai_max_concurrent_generations=0)
    assert Settings(**base, ai_service_url="http://localhost:8100", ai_service_api_key="k" * 30).ai_image_enabled


# ------------------------------------------------------------------------------------------------ when the service says no
def test_a_refused_description_is_reported_to_the_educator(make_admin, fake_ai):
    edu = make_admin("educator")
    a = new_draft(edu)
    fake_ai.respond = lambda req: httpx.Response(
        422, json={"error": {"code": "image_rejected", "message": "OpenAI rejected the request: not allowed"}}
    )
    r = generate(edu, a["id"])
    assert r.status_code == 422 and error_code(r) == "ai_image_rejected"
    assert "not allowed" in r.json()["error"]["message"]
    log = make_admin("content_admin").get("/v1/admin/activity-log", params={"activity_id": a["id"]}).json()
    assert not [e for e in log if e["action"] == "image_generated"]  # a refused request is not recorded as a generation


def _json(status, body):
    return lambda req: httpx.Response(status, json=body)


def _raises(exc):
    def respond(req):
        raise exc

    return respond


BROKEN = {
    "service unavailable": _json(503, {"error": {"code": "image_service_busy", "message": "secret-detail-1"}}),
    "bad shared key": _json(401, {"error": {"code": "unauthorized", "message": "secret-detail-2"}}),
    "service rate limit": _json(429, {"error": {"code": "rate_limited", "message": "secret-detail-3"}}),
    "openai failing": _json(502, {"error": {"code": "image_service_error", "message": "secret-detail-4"}}),
    "disagreement": _json(422, {"error": {"code": "validation_error", "message": "secret-detail-5"}}),
    "crash": lambda req: httpx.Response(500, text="Traceback secret-detail-6"),
    "timeout": _raises(httpx.ReadTimeout("secret-detail-7")),
    "refused connection": _raises(httpx.ConnectError("secret-detail-8")),
    "not json": lambda req: httpx.Response(200, text="<html>hello</html>"),
    "no pictures": _json(200, {"images": []}),
    "too many pictures": _json(200, {"images": [{"mime_type": "image/png", "b64_json": "AAAA"}] * 3}),
    "wrong type": _json(200, {"images": [{"mime_type": "image/svg+xml", "b64_json": "AAAA"}]}),
    "not base64": _json(200, {"images": [{"mime_type": "image/png", "b64_json": "***not base64***"}]}),
    "missing field": _json(200, {"images": [{"mime_type": "image/png"}]}),
}


@pytest.mark.parametrize("name", list(BROKEN))
def test_any_service_failure_is_a_plain_unavailable_with_no_detail(make_admin, fake_ai, name):
    edu = make_admin("educator")
    a = new_draft(edu)
    fake_ai.respond = BROKEN[name]
    r = generate(edu, a["id"], n=2)
    assert r.status_code == 503 and error_code(r) == "ai_unavailable", name
    assert "secret-detail" not in r.text and "Traceback" not in r.text
    assert edu.get("/v1/admin/me").status_code == 200  # the failure did not poison the session


# ------------------------------------------------------------------------------------------------ the optional daily limit
def test_there_is_no_limit_unless_one_is_set(make_admin, fake_ai):
    edu = make_admin("educator")
    a = new_draft(edu)
    assert all(generate(edu, a["id"]).status_code == 200 for _ in range(6))


def test_a_daily_limit_counts_pictures_per_person(make_admin, fake_ai, monkeypatch):
    monkeypatch.setenv("AI_IMAGE_DAILY_LIMIT_PER_USER", "3")
    config.reset_settings_cache()
    edu, other = make_admin("educator"), make_admin("educator")
    a, b = new_draft(edu), new_draft(other)
    assert generate(edu, a["id"]).json()["remaining_today"] == 2
    assert generate(edu, a["id"], n=2).json()["remaining_today"] == 0
    r = generate(edu, a["id"])
    assert r.status_code == 429 and error_code(r) == "ai_limit_reached" and "3" in r.json()["error"]["message"]
    assert generate(other, b["id"], n=2).status_code == 200  # someone else's allowance is their own
    assert len(fake_ai.requests) == 3


def test_a_request_that_would_pass_the_limit_is_refused_whole(make_admin, fake_ai, monkeypatch):
    monkeypatch.setenv("AI_IMAGE_DAILY_LIMIT_PER_USER", "2")
    config.reset_settings_cache()
    edu = make_admin("educator")
    a = new_draft(edu)
    assert generate(edu, a["id"]).status_code == 200  # 1 of 2 used
    assert generate(edu, a["id"], n=2).status_code == 429  # would make 3
    assert generate(edu, a["id"]).status_code == 200


def test_the_concurrency_gate_follows_the_setting(monkeypatch):
    monkeypatch.setenv("AI_MAX_CONCURRENT_GENERATIONS", "2")
    config.reset_settings_cache()
    gate = ai_images._slot_gate()
    assert gate._value == 2 and ai_images._slot_gate() is gate
    monkeypatch.setenv("AI_MAX_CONCURRENT_GENERATIONS", "3")
    config.reset_settings_cache()
    assert ai_images._slot_gate()._value == 3
    monkeypatch.undo()
    config.reset_settings_cache()
