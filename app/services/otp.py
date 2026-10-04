"""Guardian OTP generation and delivery.

Providers: console (writes the code to the application log: dev/test/nonprod only, refused in prod by config),
webhook (POSTs {"to", "message"} to your SMS gateway over HTTPS), msg91 (sends via MSG91's v5 Send OTP API,
see docs.msg91.com/otp), disabled (always fails). deliver_email() is the separate email channel (SMTP, see
app/services/email.py) and is independent of OTP_PROVIDER.

Every provider only delivers the code - generation (new_code) and verification (matches) always happen here,
locally, regardless of provider. For msg91 specifically this matters: we never call MSG91's own Verify OTP
API, so MSG91 never becomes a second source of truth for whether a code is correct, and the (much more
frequent) verify request path never depends on an external call.

OTP_STATIC_TEST_CODE (dev/nonprod only, never prod): when set, every generated code is this fixed value
instead of a random one, so a group of testers can all be told the same code out of band rather than each
needing real SMS delivery. Delivery (console/webhook/msg91/disabled) still runs as configured; only
generation is affected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid

from app.config import Settings, get_settings
from app.errors import ApiError
from app.services import email as email_service

log = logging.getLogger("app.otp")


def new_code() -> str:
    s = get_settings()
    if s.otp_static_test_code:
        return s.otp_static_test_code
    return f"{secrets.randbelow(10**6):06d}"


def digest(verification_id: uuid.UUID, code: str) -> str:
    return hashlib.sha256(f"{verification_id}:{code}".encode()).hexdigest()


def matches(verification_id: uuid.UUID, code: str, stored: str | None) -> bool:
    return bool(stored) and hmac.compare_digest(digest(verification_id, code), stored)


def deliver_email(address: str, code: str) -> None:
    """Email the code to `address` (the account's own email). Generation/verification stay local, as for SMS."""
    email_service.send_guardian_code(address, code, get_settings().otp_ttl_minutes)


def deliver(phone: str, code: str) -> None:
    s = get_settings()
    message = f"Your HomeSchool verification code is {code}. It expires in {s.otp_ttl_minutes} minutes."
    if s.otp_provider == "console":
        log.warning("OTP (console provider) for ****%s: %s", phone[-4:], code)
        return
    if s.otp_provider == "webhook" and s.otp_webhook_url:
        body = json.dumps({"to": phone, "message": message}).encode()
        req = urllib.request.Request(
            s.otp_webhook_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {s.otp_webhook_token.get_secret_value()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 (https enforced by config)
                if resp.status >= 300:
                    raise ApiError(503, "delivery_failed")
        except (urllib.error.URLError, TimeoutError):
            log.error("OTP webhook delivery failed")
            raise ApiError(503, "delivery_failed") from None
        return
    if s.otp_provider == "msg91":
        _deliver_msg91(phone, code, s)
        return
    raise ApiError(503, "delivery_failed", "Verification codes cannot be sent right now.")


def _deliver_msg91(phone: str, code: str, s: Settings) -> None:
    """Send `code` as an SMS via MSG91's v5 Send OTP API (POST https://control.msg91.com/api/v5/otp).

    We pass our own already-generated `code` via the API's `otp` query parameter instead of letting MSG91
    generate one, so MSG91 is used purely for SMS delivery - verification still happens locally via
    `matches()` above, exactly as for every other provider.

    `template_id` must be a pre-approved MSG91 template containing the ##OTP## placeholder (registering
    that template, and TRAI DLT sender/template registration, are account-side prerequisites - see the
    "Register for TRAI DLT" task - not something this function can do).

    MSG91's API takes mobile/otp/template_id as URL query parameters (their contract, confirmed against
    MSG91's own docs - not a choice made here); `authkey` goes in a request header, never the URL. We never
    log the constructed URL, since it would contain the phone number and the OTP in plain text.
    """
    query = urllib.parse.urlencode(
        {
            "template_id": s.msg91_template_id,
            "mobile": phone.lstrip("+"),  # MSG91 expects "<country code><number>", no leading +
            "otp": code,
            "otp_expiry": s.otp_ttl_minutes,
        }
    )
    req = urllib.request.Request(
        f"https://control.msg91.com/api/v5/otp?{query}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "authkey": s.msg91_auth_key.get_secret_value(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 (https enforced; see docstring)
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        log.error("MSG91 OTP delivery failed (request error)")
        raise ApiError(503, "delivery_failed") from None
    if payload.get("type") != "success":
        log.error("MSG91 OTP delivery failed: %s", payload.get("message"))
        raise ApiError(503, "delivery_failed")
