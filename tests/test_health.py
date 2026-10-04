import pytest
from pydantic import ValidationError

from app.config import Settings


def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_client_config_is_public(client):
    body = client.get("/v1/config").json()
    assert body["min_app_version"] and "declaration_notice_version" in body


def test_error_shape_and_request_id(client):
    r = client.get("/v1/me", headers={"X-Request-Id": "abc-123"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    assert r.json()["error"]["request_id"] == "abc-123"
    assert r.headers["X-Request-Id"] == "abc-123"
    assert r.headers["Cache-Control"] == "no-store"


def test_validation_error_shape(client):
    r = client.post("/v1/auth/signup", json={"email": "not-an-email"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def _prod(**kw):
    base = dict(
        app_env="prod",
        jwt_secret="x" * 40,
        db_password="p" * 20,
        db_sslmode="require",
        cors_origins="https://admin.example.com",
        otp_provider="webhook",
        otp_webhook_url="https://sms.example.com/send",
        email_provider="smtp",
        smtp_host="smtp.example.com",
        email_from="HomeSchool <no-reply@example.com>",
        expose_dev_otp=False,
        database_url=None,
    )
    return Settings(_env_file=None, **{**base, **kw})


def test_prod_settings_accept_a_good_configuration():
    assert _prod().is_deployed


@pytest.mark.parametrize(
    "bad",
    [
        {"jwt_secret": "change-me"},
        {"jwt_secret": "short"},
        {"expose_dev_otp": True},
        {"cors_origins": "*"},
        {"db_password": ""},
        {"db_sslmode": "disable"},
        {"otp_provider": "console"},
        {"email_provider": "console"},
        {"smtp_host": None},
        {"smtp_username": "user", "smtp_password": ""},
        {"otp_webhook_url": "http://insecure.example.com"},
    ],
)
def test_prod_settings_reject_unsafe_values(bad):
    with pytest.raises(ValidationError):
        _prod(**bad)


def test_nonprod_allows_console_otp_but_not_dev_secrets():
    common = dict(
        _env_file=None,
        app_env="nonprod",
        db_password="p" * 20,
        db_sslmode="require",
        cors_origins="https://t.example.com",
        expose_dev_otp=False,
        database_url=None,
    )
    Settings(jwt_secret="y" * 40, otp_provider="console", **common)
    with pytest.raises(ValidationError):
        Settings(jwt_secret="dev-secret-change-me", **common)


def test_planned_endpoints_answer_501_and_never_shadow_real_ones(client):
    from app.main import create_app
    from app.routers.planned import PLANNED

    assert len(PLANNED) > 60
    for method, path in PLANNED[:5]:
        r = client.request(method, path.replace("{session_id}", "x"))
        assert r.status_code == 501 and r.json()["error"]["code"] == "not_implemented"
    seen: dict[tuple[str, str], int] = {}
    for route in create_app().routes:
        for m in getattr(route, "methods", None) or []:
            if m != "HEAD":
                seen[(m, route.path)] = seen.get((m, route.path), 0) + 1
    assert [k for k, n in seen.items() if n > 1] == []  # a stub must be deleted when its endpoint is implemented


def test_unknown_paths_and_methods_use_the_error_shape(client):
    r = client.get("/v1/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.delete("/healthz")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
