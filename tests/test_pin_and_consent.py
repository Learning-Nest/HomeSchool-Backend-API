import pytest
from sqlalchemy import text


def test_pin_required_before_verify(make_parent):
    p = make_parent(pin=False)
    r = p.post("/v1/me/pin/verify", json={"pin": "1234"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "pin_not_set"


def test_pin_validation(make_parent):
    p = make_parent(pin=False)
    for bad in ("123", "1234567", "12ab", ""):
        assert p.put("/v1/me/pin", json={"pin": bad}).status_code == 422


def test_pin_verify_returns_short_lived_elevation_token(parent):
    r = parent.post("/v1/me/pin/verify", json={"pin": "2468"})
    assert r.status_code == 200 and r.json()["expires_in"] == 300


def test_pin_is_hashed(parent, database):
    with database["admin"].connect() as c:
        h = c.execute(text("select pin_hash from parent_pins")).scalar()
    assert h.startswith("$argon2id$") and "2468" not in h


def test_changing_pin_needs_the_current_pin(parent):
    r = parent.put("/v1/me/pin", json={"pin": "1357"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "pin_already_set"
    assert parent.put("/v1/me/pin", json={"pin": "1357", "current_pin": "0000"}).status_code == 401
    assert parent.put("/v1/me/pin", json={"pin": "1357", "current_pin": "2468"}).status_code == 204
    assert parent.post("/v1/me/pin/verify", json={"pin": "1357"}).status_code == 200


def test_lockout_tiers(parent, database):
    def fail():
        return parent.post("/v1/me/pin/verify", json={"pin": "0000"})

    for i in range(4):
        r = fail()
        assert r.status_code == 401 and r.json()["error"]["attempts_remaining"] == 4 - i
    r = fail()  # 5th failure: 15 minutes
    assert (
        r.status_code == 429
        and r.json()["error"]["code"] == "pin_locked"
        and 890 <= r.json()["error"]["retry_after_seconds"] <= 900
    )
    # the correct PIN is refused while locked, and the lock is durable (committed before the error)
    assert parent.post("/v1/me/pin/verify", json={"pin": "2468"}).status_code == 429
    with database["admin"].begin() as c:
        c.execute(text("update parent_pins set locked_until = now() - interval '1 second'"))
    assert fail().status_code == 429  # attempt 6 after the lock expired: locked again straight away
    with database["admin"].begin() as c:
        c.execute(text("update parent_pins set locked_until = null, failed_attempts = 9"))
    assert 3500 <= fail().json()["error"]["retry_after_seconds"] <= 3600  # 10th failure: 1 hour
    with database["admin"].begin() as c:
        c.execute(text("update parent_pins set locked_until = null, failed_attempts = 14"))
    assert fail().json()["error"]["retry_after_seconds"] == 86400  # 15th failure: 24 hours


def test_success_resets_the_failure_counter(parent, database):
    for _ in range(3):
        parent.post("/v1/me/pin/verify", json={"pin": "0000"})
    assert parent.post("/v1/me/pin/verify", json={"pin": "2468"}).status_code == 200
    with database["admin"].connect() as c:
        assert c.execute(text("select failed_attempts from parent_pins")).scalar() == 0


def test_forgotten_pin_can_be_reset_with_the_password(parent, database):
    for _ in range(5):
        parent.post("/v1/me/pin/verify", json={"pin": "0000"})
    assert parent.post("/v1/me/pin/reset", json={"password": "wrong-password", "pin": "7777"}).status_code == 401
    assert parent.post("/v1/me/pin/reset", json={"password": "correct-horse-battery", "pin": "7777"}).status_code == 204
    assert parent.post("/v1/me/pin/verify", json={"pin": "7777"}).status_code == 200


# ------------------------------------------------------------------------------------------------ guardian + consent
def test_child_profiles_need_a_verified_guardian(make_parent):
    p = make_parent(verified=False)
    r = p.post(f"/v1/families/{p.family_id}/children", json={"display_name": "Mia"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "guardian_verification_required"
    p.verify_guardian()
    assert p.post(f"/v1/families/{p.family_id}/children", json={"display_name": "Mia"}).status_code == 201
    me = p.get("/v1/me").json()
    assert me["memberships"][0]["guardian_verified"] is True


def test_wrong_otp_is_rejected_and_attempts_are_limited(make_parent):
    p = make_parent(verified=False)
    started = p.post(f"/v1/families/{p.family_id}/guardian-verification", json={"phone": "+919812345678"}).json()
    body = {
        "verification_id": started["verification_id"],
        "declaration_accepted": True,
        "notice_version": "2026-09-draft",
    }
    wrong = "000000" if started["dev_code"] != "000000" else "111111"
    for _ in range(5):
        r = p.post(f"/v1/families/{p.family_id}/guardian-verification/confirm", json={**body, "code": wrong})
        assert r.status_code == 422 and r.json()["error"]["code"] == "otp_invalid"
    # after 5 failures even the right code no longer works: request a new one
    r = p.post(f"/v1/families/{p.family_id}/guardian-verification/confirm", json={**body, "code": started["dev_code"]})
    assert r.status_code == 422


def test_declaration_must_be_accepted_and_notice_current(make_parent):
    p = make_parent(verified=False)
    s = p.post(f"/v1/families/{p.family_id}/guardian-verification", json={"phone": "+919812345678"}).json()
    base = {"verification_id": s["verification_id"], "code": s["dev_code"]}
    assert (
        p.post(
            f"/v1/families/{p.family_id}/guardian-verification/confirm",
            json={**base, "declaration_accepted": False, "notice_version": "2026-09-draft"},
        ).status_code
        == 422
    )
    assert (
        p.post(
            f"/v1/families/{p.family_id}/guardian-verification/confirm",
            json={**base, "declaration_accepted": True, "notice_version": "old"},
        ).status_code
        == 422
    )


def test_otp_is_stored_hashed_and_dev_code_hidden_unless_enabled(make_parent, database, monkeypatch):
    p = make_parent(verified=False)
    s = p.post(f"/v1/families/{p.family_id}/guardian-verification", json={"phone": "+919812345678"}).json()
    with database["admin"].connect() as c:
        row = c.execute(text("select otp_hash, phone_last4 from guardian_verifications")).one()
    assert s["dev_code"] not in row.otp_hash and row.phone_last4 == "5678"


def test_verification_confirmation_records_core_consent(parent):
    consents = {c["purpose"]: c for c in parent.get(f"/v1/families/{parent.family_id}/consents").json()["consents"]}
    assert consents["core_service"]["granted"] is True and consents["core_service"]["notice_version"] == "2026-09-draft"


def test_optional_consents_are_append_only_and_core_cannot_be_withdrawn(parent, database):
    url = f"/v1/families/{parent.family_id}/consents"
    on = {"purpose": "media_capture", "granted": True, "notice_version": "2026-09-draft"}
    assert parent.put(url, json=on).status_code == 200
    latest = {c["purpose"]: c for c in parent.put(url, json={**on, "granted": False}).json()["consents"]}
    assert latest["media_capture"]["granted"] is False
    with database["admin"].connect() as c:
        assert (
            c.execute(text("select count(*) from consents where purpose = 'media_capture'")).scalar() == 2
        )  # history kept
    assert (
        parent.put(
            url, json={"purpose": "core_service", "granted": False, "notice_version": "2026-09-draft"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("path", ["/v1/families/{f}/consents"])
def test_other_families_are_invisible(make_parent, path):
    a, b = make_parent(), make_parent()
    assert b.get(path.format(f=a.family_id)).status_code == 404
