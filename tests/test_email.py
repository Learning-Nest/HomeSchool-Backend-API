"""Unit tests for the email service (app.services.email). The SMTP connection is mocked and no database is used,
so the DB-backed autouse `_clean` fixture from conftest.py is shadowed with a no-op."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ApiError
from app.services import email as email_service


@pytest.fixture(autouse=True)
def _clean():  # shadows conftest.py's DB-backed autouse fixture of the same name
    yield


def _use(monkeypatch, **overrides) -> None:
    base: dict = {
        "_env_file": None,
        "app_env": "test",
        "jwt_secret": SecretStr("test-only-jwt-secret-0123456789-abcdefghijklmnop"),
        "email_provider": "smtp",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "mailer",
        "smtp_password": SecretStr("smtp-secret"),
        "email_from": "HomeSchool <no-reply@example.com>",
    }
    base.update(overrides)
    s = Settings(**base)
    monkeypatch.setattr(email_service, "get_settings", lambda: s)


def _fake_smtp(monkeypatch, cls_name="SMTP") -> MagicMock:
    server = MagicMock()
    server.__enter__.return_value = server
    factory = MagicMock(return_value=server)
    monkeypatch.setattr(email_service.smtplib, cls_name, factory)
    factory.server = server
    return factory


def test_smtp_starttls_login_and_message_shape(monkeypatch):
    _use(monkeypatch)
    factory = _fake_smtp(monkeypatch)
    email_service.send_guardian_code("asha@example.com", "123456", 10)
    factory.assert_called_once_with("smtp.example.com", 587, timeout=10)
    factory.server.starttls.assert_called_once()
    factory.server.login.assert_called_once_with("mailer", "smtp-secret")
    msg = factory.server.send_message.call_args.args[0]
    assert msg["To"] == "asha@example.com" and msg["From"] == "HomeSchool <no-reply@example.com>"
    assert "123456" in msg.get_content() and "10 minutes" in msg.get_content()


def test_smtp_ssl_mode_uses_smtp_ssl_and_no_starttls(monkeypatch):
    _use(monkeypatch, smtp_port=465, smtp_security="ssl")
    factory = _fake_smtp(monkeypatch, "SMTP_SSL")
    email_service.send_email("a@example.com", "Hi", "body")
    assert factory.call_args.args[:2] == ("smtp.example.com", 465)
    factory.server.starttls.assert_not_called()
    factory.server.send_message.assert_called_once()


def test_no_login_when_no_username(monkeypatch):
    _use(monkeypatch, smtp_username=None, smtp_password=SecretStr(""), smtp_security="none", smtp_port=25)
    factory = _fake_smtp(monkeypatch)
    email_service.send_email("a@example.com", "Hi", "body")
    factory.server.login.assert_not_called()
    factory.server.starttls.assert_not_called()


@pytest.mark.parametrize(
    "exc", [smtplib.SMTPAuthenticationError(535, b"bad"), ConnectionRefusedError(), TimeoutError()]
)
def test_delivery_failures_become_503_delivery_failed(monkeypatch, caplog, exc):
    _use(monkeypatch)
    factory = _fake_smtp(monkeypatch)
    factory.server.send_message.side_effect = exc
    with pytest.raises(ApiError) as e:
        email_service.send_email("a@example.com", "Hi", "TOP-SECRET-BODY")
    assert e.value.status == 503 and e.value.code == "delivery_failed"
    assert "TOP-SECRET-BODY" not in caplog.text and "smtp-secret" not in caplog.text


def test_disabled_provider_always_fails(monkeypatch):
    _use(monkeypatch, email_provider="disabled")
    with pytest.raises(ApiError) as e:
        email_service.send_email("a@example.com", "Hi", "body")
    assert e.value.code == "delivery_failed"


def test_console_provider_fills_test_outbox(monkeypatch):
    _use(monkeypatch, email_provider="console")
    email_service.OUTBOX.clear()
    email_service.send_temp_password("a@example.com", "Asha", "Abcdefgh2345", 60)
    assert len(email_service.OUTBOX) == 1 and "Abcdefgh2345" in email_service.OUTBOX[0].body
    email_service.OUTBOX.clear()


def test_mask_email():
    assert email_service.mask_email("pawel@gmail.com") == "p***@gmail.com"
    assert email_service.mask_email("nonsense") == "***"


def test_smtp_requires_host_and_from():
    with pytest.raises(ValueError):
        Settings(_env_file=None, email_provider="smtp", smtp_host="smtp.example.com")
    with pytest.raises(ValueError):
        Settings(_env_file=None, email_provider="smtp", email_from="a@example.com")
