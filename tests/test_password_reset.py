"""Forgot-password (emailed temporary password), the forced password change, and the email guardian-code channel."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.services import email as email_service

PW = "correct-horse-battery"
NEW_PW = "a-brand-new-passphrase"


@pytest.fixture(autouse=True)
def _outbox():
    email_service.OUTBOX.clear()
    yield
    email_service.OUTBOX.clear()


def _temp_from_outbox() -> str:
    assert email_service.OUTBOX, "no email was sent"
    m = re.search(r"^ {4}(\S{12})$", email_service.OUTBOX[-1].body, re.MULTILINE)
    assert m, email_service.OUTBOX[-1].body
    return m.group(1)


def _login(client, email, password):
    return client.post("/v1/auth/login", json={"email": email, "password": password})


def _forgot(client, email):
    return client.post("/v1/auth/forgot-password", json={"email": email})


def _bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ------------------------------------------------------------------ forgot-password
def test_unknown_and_known_emails_get_the_same_answer_but_only_known_get_mail(client, make_parent):
    p = make_parent(verified=False, pin=False)
    known = _forgot(client, p.email)
    unknown = _forgot(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"status": "ok"}
    assert [m.to for m in email_service.OUTBOX] == [p.email]


def test_old_password_keeps_working_after_a_reset_request(client, make_parent):
    p = make_parent(verified=False, pin=False)
    _forgot(client, p.email)
    r = _login(client, p.email, PW)
    assert r.status_code == 200 and r.json()["temp_login"] is False


def test_temp_password_forces_a_change_before_anything_else(client, make_parent):
    p = make_parent(verified=False, pin=False)
    _forgot(client, p.email)
    temp = _temp_from_outbox()
    r = _login(client, p.email, temp + "x")
    assert r.status_code == 401  # near-miss is rejected
    r = _login(client, p.email, temp)
    assert r.status_code == 200 and r.json()["temp_login"] is True
    h = _bearer(r.json())
    blocked = client.get("/v1/families", headers=h)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "password_change_required"
    assert client.put("/v1/me/pin", headers=h, json={"pin": "2468"}).status_code == 403
    me = client.get("/v1/me", headers=h)
    assert me.status_code == 200 and me.json()["temp_login"] is True


def test_changing_the_password_clears_the_flag_and_signs_everything_else_out(client, make_parent):
    p = make_parent(verified=False, pin=False)
    old_refresh = p.tokens["refresh_token"]
    _forgot(client, p.email)
    temp = _temp_from_outbox()
    tokens = _login(client, p.email, temp).json()
    h = _bearer(tokens)

    short = client.post("/v1/auth/change-password", headers=h, json={"current_password": temp, "new_password": "short"})
    assert short.status_code == 422
    wrong = client.post(
        "/v1/auth/change-password", headers=h, json={"current_password": "nope", "new_password": NEW_PW}
    )
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_credentials"
    same = client.post("/v1/auth/change-password", headers=h, json={"current_password": temp, "new_password": temp})
    assert same.status_code == 422  # new == current

    ok = client.post("/v1/auth/change-password", headers=h, json={"current_password": temp, "new_password": NEW_PW})
    assert ok.status_code == 200 and ok.json()["temp_login"] is False
    h2 = _bearer(ok.json())
    assert client.get("/v1/families", headers=h2).status_code == 200
    assert client.get("/v1/me", headers=h2).json()["temp_login"] is False

    assert _login(client, p.email, NEW_PW).status_code == 200
    assert _login(client, p.email, PW).status_code == 401  # old password is gone
    assert _login(client, p.email, temp).status_code == 401  # temp password is single-use
    assert client.post("/v1/auth/refresh", json={"refresh_token": old_refresh}).status_code == 401
    assert client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_a_normal_user_can_change_their_password_with_the_current_one(client, make_parent):
    p = make_parent(verified=False, pin=False)
    r = p.post("/v1/auth/change-password", json={"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200
    assert _login(client, p.email, NEW_PW).status_code == 200


def test_expired_temp_password_is_rejected(client, make_parent, database):
    p = make_parent(verified=False, pin=False)
    _forgot(client, p.email)
    temp = _temp_from_outbox()
    with database["admin"].begin() as c:
        c.execute(text("update users set reset_expires_at = now() - interval '1 minute'"))
    assert _login(client, p.email, temp).status_code == 401
    assert _login(client, p.email, PW).status_code == 200


def test_logging_in_with_the_real_password_discards_a_pending_reset(client, make_parent):
    p = make_parent(verified=False, pin=False)
    _forgot(client, p.email)
    temp = _temp_from_outbox()
    assert _login(client, p.email, PW).status_code == 200
    assert _login(client, p.email, temp).status_code == 401


def test_temp_password_is_stored_hashed_and_a_new_request_replaces_it(client, make_parent, database):
    p = make_parent(verified=False, pin=False)
    _forgot(client, p.email)
    first = _temp_from_outbox()
    with database["admin"].connect() as c:
        stored = c.execute(text("select reset_hash from users")).scalar()
    assert stored.startswith("$argon2id$") and first not in stored
    _forgot(client, p.email)
    second = _temp_from_outbox()
    assert first != second
    assert _login(client, p.email, first).status_code == 401
    assert _login(client, p.email, second).status_code == 200


def test_forgot_password_is_rate_limited_without_revealing_the_account(client, make_parent):
    p = make_parent(verified=False, pin=False)
    for _ in range(5):
        assert _forgot(client, p.email).status_code == 202
    assert len(email_service.OUTBOX) == 3  # per-address cap: extra requests are answered but send nothing


def test_forgot_password_validates_the_email_shape(client):
    assert _forgot(client, "not-an-email").status_code == 422


# ------------------------------------------------------------------ email channel for guardian verification
def test_guardian_code_by_email_goes_to_the_account_address_and_verifies(make_parent, database):
    p = make_parent(verified=False, pin=False)
    r = p.post(f"/v1/families/{p.family_id}/guardian-verification", json={"channel": "email"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert (
        body["channel"] == "email"
        and body["destination_hint"].endswith("@example.com")
        and "***" in body["destination_hint"]
    )
    assert [m.to for m in email_service.OUTBOX] == [p.email]
    code = re.search(r"\b(\d{6})\b", email_service.OUTBOX[-1].body).group(1)
    assert code == body["dev_code"]
    ok = p.post(
        f"/v1/families/{p.family_id}/guardian-verification/confirm",
        json={
            "verification_id": body["verification_id"],
            "code": code,
            "declaration_accepted": True,
            "notice_version": "2026-09-draft",
        },
    )
    assert ok.status_code == 200
    with database["admin"].connect() as c:
        row = c.execute(text("select method, phone_last4 from guardian_verifications")).one()
    assert row.method == "email_declaration" and row.phone_last4 is None


def test_email_channel_ignores_any_address_in_the_request(make_parent):
    p = make_parent(verified=False, pin=False)
    r = p.post(
        f"/v1/families/{p.family_id}/guardian-verification", json={"channel": "email", "email": "attacker@example.com"}
    )
    assert r.status_code == 201
    assert [m.to for m in email_service.OUTBOX] == [p.email]


def test_sms_channel_still_requires_a_phone_and_email_does_not(make_parent):
    p = make_parent(verified=False, pin=False)
    url = f"/v1/families/{p.family_id}/guardian-verification"
    assert p.post(url, json={"channel": "sms"}).status_code == 422
    assert p.post(url, json={}).status_code == 422
    ok = p.post(url, json={"phone": "+919812345678"})
    assert ok.status_code == 201 and ok.json()["channel"] == "sms" and ok.json()["destination_hint"] == "******5678"
    assert email_service.OUTBOX == []
