"""Test harness: a real PostgreSQL (embedded via pgserver, or TEST_DATABASE_URL in CI), real migrations, and the API
connecting as the same least-privilege role that production uses, so Row-Level Security is actually enforced."""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
APP_ROLE = "homeschool_app_test"
APP_PASSWORD = "test-app-role-password-0123456789"
TEST_ENV = {
    "APP_ENV": "test",
    "JWT_SECRET": "test-only-jwt-secret-0123456789-abcdefghijklmnop",
    "EXPOSE_DEV_OTP": "true",
    "OTP_PROVIDER": "console",
    "EMAIL_PROVIDER": "console",  # a developer's .env.local must never make the test suite send real email
    "LOG_LEVEL": "WARNING",
    "LOG_JSON": "false",
    "STORAGE_BACKEND": "local",
    "STORAGE_LOCAL_DIR": tempfile.mkdtemp(prefix="hs-assets-"),
}


def _admin_url() -> tuple[str, object | None]:
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        return url.replace("postgresql://", "postgresql+psycopg://", 1) if "+psycopg" not in url else url, None
    import pgserver

    tmp = tempfile.mkdtemp(prefix="hs-pg-")
    server = pgserver.get_server(tmp, cleanup_mode="stop")
    uri = server.get_uri()  # postgresql://postgres:@/postgres?host=/tmp/...
    return uri.replace("postgresql://", "postgresql+psycopg://", 1), server


@pytest.fixture(scope="session")
def database():
    from app import config, db

    admin_url, server = _admin_url()
    dbname = f"hs_{secrets.token_hex(4)}"
    base = make_url(admin_url)
    with create_engine(base, isolation_level="AUTOCOMMIT").connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin_db_url = base.set(database=dbname)
    os.environ.update(TEST_ENV)
    os.environ["DATABASE_URL"] = admin_db_url.render_as_string(hide_password=False)
    os.environ["APP_DB_PASSWORD"] = APP_PASSWORD
    config.reset_settings_cache()
    db.dispose_engine()

    from app import cli

    cli.migrate()
    cli.ensure_app_role(APP_ROLE, APP_PASSWORD)

    admin_engine = create_engine(admin_db_url)
    app_url = admin_db_url.set(username=APP_ROLE, password=APP_PASSWORD)
    os.environ["DATABASE_URL"] = app_url.render_as_string(hide_password=False)
    config.reset_settings_cache()
    db.dispose_engine()
    cli.seed_from_path(str(ROOT / "seed" / "launch-bundle.json"), auto_publish=True)
    yield {"admin": admin_engine, "app_url": app_url, "admin_url": admin_db_url}
    db.dispose_engine()
    admin_engine.dispose()
    if server is not None:
        server.cleanup()


def _content_is_pristine(conn) -> bool:
    row = conn.execute(
        text("""select (select count(*) from content.activities), (select coalesce(max(version), 1) from content.activities),
                                      (select count(*) from content.skills), (select count(*) from content.activities where status <> 'published')""")
    ).one()
    return tuple(row) == (16, 1, 51, 0)


@pytest.fixture(autouse=True)
def _clean(database):
    """Every test starts with no families/children and the launch content exactly as seeded (admin tests edit content)."""
    from app import cli, ratelimit

    ratelimit.reset()
    with database["admin"].begin() as conn:
        conn.execute(text("TRUNCATE users, audit_log, outbox_events CASCADE"))
        conn.execute(text("TRUNCATE content.boards CASCADE"))  # books, lessons, activity links and child_books follow
        conn.execute(text("TRUNCATE content.release_policies"))  # back to the default row
        conn.execute(text("INSERT INTO content.release_policies (scope_board, scope_grade) VALUES ('', '')"))
        pristine = _content_is_pristine(conn)
        if not pristine:
            conn.execute(text("TRUNCATE content.activities CASCADE"))
            conn.execute(text("DELETE FROM content.skills WHERE code LIKE 'MAT.X.%'"))
    if not pristine:
        cli.seed_from_path(str(ROOT / "seed" / "launch-bundle.json"), auto_publish=True)
    yield


@pytest.fixture(scope="session")
def client(database):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(), base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------- helpers used by many tests
class Parent:
    def __init__(self, client, email: str, tokens: dict):
        self.client = client
        self.email = email
        self.tokens = tokens
        self.user_id = tokens["user"]["id"]
        self.family_id = tokens["memberships"][0]["family_id"]
        self.headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    def req(self, method: str, url: str, **kw):
        headers = {**self.headers, **kw.pop("headers", {})}
        return self.client.request(method, url, headers=headers, **kw)

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, **kw):
        return self.req("POST", url, **kw)

    def put(self, url, **kw):
        return self.req("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.req("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.req("DELETE", url, **kw)

    def verify_guardian(self) -> None:
        r = self.post(f"/v1/families/{self.family_id}/guardian-verification", json={"phone": "+919812345678"})
        assert r.status_code == 201, r.text
        body = r.json()
        r = self.post(
            f"/v1/families/{self.family_id}/guardian-verification/confirm",
            json={
                "verification_id": body["verification_id"],
                "code": body["dev_code"],
                "declaration_accepted": True,
                "notice_version": "2026-09-draft",
            },
        )
        assert r.status_code == 200, r.text

    def set_pin(self, pin: str = "2468") -> None:
        assert self.put("/v1/me/pin", json={"pin": pin}).status_code == 204

    def elevation(self, pin: str = "2468") -> dict:
        r = self.post("/v1/me/pin/verify", json={"pin": pin})
        assert r.status_code == 200, r.text
        return {"X-Elevation-Token": r.json()["elevation_token"]}

    def add_child(self, name: str = "Aarav", level: str = "L2", **extra) -> dict:
        r = self.post(
            f"/v1/families/{self.family_id}/children", json={"display_name": name, "level_code": level, **extra}
        )
        assert r.status_code == 201, r.text
        return r.json()

    def child_headers(self, child_id: str, device: str = "device-0001") -> dict:
        r = self.post(f"/v1/children/{child_id}/sessions", json={"device_id": device})
        assert r.status_code == 201, r.text
        return {"Authorization": f"Bearer {r.json()['child_token']}"}


@pytest.fixture
def make_parent(client):
    counter = {"n": 0}

    def _make(verified: bool = True, pin: bool = True) -> Parent:
        counter["n"] += 1
        email = f"parent{counter['n']}-{uuid.uuid4().hex[:6]}@example.com"
        r = client.post(
            "/v1/auth/signup",
            json={"email": email, "password": "correct-horse-battery", "full_name": f"Parent {counter['n']}"},
        )
        assert r.status_code == 201, r.text
        p = Parent(client, email, r.json())
        if verified:
            p.verify_guardian()
        if pin:
            p.set_pin()
        return p

    return _make


@pytest.fixture
def parent(make_parent) -> Parent:
    return make_parent()


@pytest.fixture
def activities(client, parent):
    """Published launch activities by slug (as summary dicts)."""
    rows = parent.get("/v1/activities", params={"limit": 100}).json()
    return {a["slug"]: a for a in rows}


@pytest.fixture
def make_admin(database, client):
    def _make(role: str = "content_admin") -> Parent:
        email = f"admin-{uuid.uuid4().hex[:6]}@example.com"
        r = client.post(
            "/v1/auth/signup", json={"email": email, "password": "correct-horse-battery", "full_name": "Admin"}
        )
        assert r.status_code == 201
        with database["admin"].begin() as conn:
            conn.execute(
                text("update users set platform_role = :r where lower(email) = :e"), {"r": role, "e": email.lower()}
            )
        r = client.post("/v1/auth/login", json={"email": email, "password": "correct-horse-battery"})
        return Parent(client, email, r.json())

    return _make


# ---------------------------------------------------------------------------- the AI picture service (fake)
AI_KEY = "ai-service-key-" + "k" * 24


@pytest.fixture
def ai_on(monkeypatch):
    """The AI switch on, pointing at a made-up address, with settings re-read around the test."""
    from app import config

    monkeypatch.setenv("AI_IMAGE_ENABLED", "true")
    monkeypatch.setenv("AI_SERVICE_URL", "https://ai.test")
    monkeypatch.setenv("AI_SERVICE_API_KEY", AI_KEY)
    config.reset_settings_cache()
    yield
    monkeypatch.undo()
    config.reset_settings_cache()


class FakeAi:
    """Stands in for the CustomAI service. Replace `respond` to make it misbehave."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        from tests.test_images import png

        self.picture = base64.b64encode(png((64, 48))).decode()

    def respond(self, request: httpx.Request) -> httpx.Response:
        n = int(json.loads(request.content)["n"])
        return httpx.Response(
            200,
            json={
                "request_id": "r",
                "model": "fake-model",
                "images": [{"mime_type": "image/png", "b64_json": self.picture} for _ in range(n)],
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    @property
    def sent(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def fake_ai(ai_on, monkeypatch):
    from app.config import get_settings
    from app.services import ai_images

    fake = FakeAi()

    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handle),
            base_url=get_settings().ai_service_url,
            headers={"X-API-Key": get_settings().ai_service_api_key.get_secret_value()},
        )

    monkeypatch.setattr(ai_images, "make_client", make_client)
    return fake
