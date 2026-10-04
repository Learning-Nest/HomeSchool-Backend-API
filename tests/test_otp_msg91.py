"""Unit tests for the MSG91 OTP delivery provider (app.services.otp._deliver_msg91).

This module mocks the network call and never touches the database, unlike most of this suite - the
conftest.py `_clean` fixture is autouse and depends on a real Postgres instance, which this pure-unit
module doesn't need, so it's shadowed below with a no-op of the same name.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ApiError
from app.services.otp import _deliver_msg91


@pytest.fixture(autouse=True)
def _clean():  # shadows conftest.py's DB-backed autouse fixture of the same name
    yield


def _settings(**overrides) -> Settings:
    base: dict = {
        "app_env": "test",
        "jwt_secret": SecretStr("test-only-jwt-secret-0123456789-abcdefghijklmnop"),
        "msg91_auth_key": SecretStr("super-secret-authkey"),
        "msg91_template_id": "tmpl-123",
        "otp_ttl_minutes": 10,
    }
    base.update(overrides)
    return Settings(**base)


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_deliver_msg91_builds_request_correctly_and_keeps_authkey_out_of_the_url():
    s = _settings()
    with patch(
        "app.services.otp.urllib.request.urlopen",
        return_value=_mock_response({"type": "success", "message": "req-id"}),
    ) as mock_urlopen:
        _deliver_msg91("+919876543210", "482915", s)

    req = mock_urlopen.call_args[0][0]
    assert req.get_method() == "POST"
    assert req.full_url == (
        "https://control.msg91.com/api/v5/otp?template_id=tmpl-123&mobile=919876543210&otp=482915&otp_expiry=10"
    )
    headers = dict(req.headers)
    assert headers["Authkey"] == "super-secret-authkey"
    assert "super-secret-authkey" not in req.full_url  # the secret goes in a header, never the URL


def test_deliver_msg91_error_response_raises_delivery_failed():
    s = _settings()
    with patch(
        "app.services.otp.urllib.request.urlopen",
        return_value=_mock_response({"type": "error", "message": "invalid template"}),
    ):
        with pytest.raises(ApiError) as exc:
            _deliver_msg91("+919876543210", "482915", s)
    assert exc.value.code == "delivery_failed"


def test_deliver_msg91_network_error_raises_delivery_failed():
    s = _settings()
    with patch(
        "app.services.otp.urllib.request.urlopen",
        side_effect=urllib.error.URLError("boom"),
    ):
        with pytest.raises(ApiError) as exc:
            _deliver_msg91("+919876543210", "482915", s)
    assert exc.value.code == "delivery_failed"


def test_deliver_msg91_malformed_json_response_raises_delivery_failed():
    s = _settings()
    bad_resp = MagicMock()
    bad_resp.read.return_value = b"not json"
    bad_resp.__enter__.return_value = bad_resp
    bad_resp.__exit__.return_value = False
    with patch("app.services.otp.urllib.request.urlopen", return_value=bad_resp):
        with pytest.raises(ApiError) as exc:
            _deliver_msg91("+919876543210", "482915", s)
    assert exc.value.code == "delivery_failed"
