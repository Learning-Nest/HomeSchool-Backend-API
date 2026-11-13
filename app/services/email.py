"""Transactional email: guardian verification codes and forgot-password temporary passwords.

Providers: console (writes the message to the application log: dev/test/nonprod only, refused in prod by config),
smtp (any SMTP server: Gmail/Workspace app password, Zoho, Amazon SES SMTP, SendGrid, Brevo, Azure Communication
Services... configured with SMTP_HOST / SMTP_PORT / SMTP_SECURITY / SMTP_USERNAME / SMTP_PASSWORD / EMAIL_FROM),
disabled (always fails).

Message bodies contain secrets (a one-time code or a temporary password) so they are never logged by the smtp
provider and the console provider is only for local development.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import get_settings
from app.errors import ApiError

log = logging.getLogger("app.email")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str


# Test hook: with APP_ENV=test and the console provider every message is also kept here so tests can read the
# code/password that would have been emailed. Never populated in any other environment.
OUTBOX: list[Message] = []


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def send_email(to: str, subject: str, body: str) -> None:
    """Delivers one plain-text message. Raises ApiError(503, delivery_failed) when it cannot be sent."""
    s = get_settings()
    if s.email_provider == "console":
        log.warning("EMAIL (console provider) to=%s subject=%r\n%s", mask_email(to), subject, body)
        if s.app_env == "test":
            OUTBOX.append(Message(to, subject, body))
        return
    if s.email_provider == "smtp":
        _send_smtp(Message(to, subject, body))
        return
    raise ApiError(503, "delivery_failed", "Email cannot be sent right now.")


def _send_smtp(m: Message) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.email_from
    msg["To"] = m.to
    msg["Subject"] = m.subject
    msg.set_content(m.body)
    try:
        if s.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                s.smtp_host, s.smtp_port, timeout=10, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10)
        with server:
            if s.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password.get_secret_value())
            server.send_message(msg)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # Log only the exception type: SMTP error text can echo the recipient or credentials.
        log.error("SMTP delivery failed (%s)", type(exc).__name__)
        raise ApiError(503, "delivery_failed") from None


def send_guardian_code(to: str, code: str, ttl_minutes: int) -> None:
    brand = get_settings().email_brand
    send_email(
        to,
        f"Your {brand} verification code",
        f"Your {brand} verification code is {code}.\n\n"
        f"It expires in {ttl_minutes} minutes. If you did not ask for this code, you can ignore this email.\n",
    )


def send_temp_password(to: str, full_name: str, temp_password: str, valid_minutes: int) -> None:
    send_email(
        to,
        f"Your temporary {get_settings().email_brand} password",
        f"Hello {full_name},\n\n"
        f"Your temporary password is:\n\n    {temp_password}\n\n"
        f"Sign in with it within {valid_minutes} minutes and the app will ask you to choose a new password. "
        "Your existing password keeps working until then.\n\n"
        "If you did not ask for this, you can ignore this email - nobody can use the temporary password "
        "without it, and your account is unchanged.\n",
    )


def send_admin_password_reset(to: str, full_name: str, temp_password: str, valid_hours: int) -> None:
    """An administrator started a password reset for this person. The temporary password is the only secret."""
    brand = get_settings().email_brand
    send_email(
        to,
        f"Reset your {brand} password",
        f"Hello {full_name},\n\n"
        f"An administrator has started a password reset for your {brand} account. Your temporary password is:\n\n"
        f"    {temp_password}\n\n"
        f"Sign in with your email address and this password within {valid_hours} hours; you will be asked to choose "
        "a new password straight away. Your existing password keeps working until then.\n\n"
        "If you were not expecting this, you can ignore this email.\n",
    )


def send_educator_invite(to: str, full_name: str, temp_password: str, valid_days: int) -> None:
    brand = get_settings().email_brand
    send_email(
        to,
        f"Your {brand} educator account",
        f"Hello {full_name},\n\n"
        f"An educator account has been created for you on {brand}. Your temporary password is:\n\n"
        f"    {temp_password}\n\n"
        f"Sign in with your email address and this password within {valid_days} days; you will be asked to choose "
        "a new password straight away.\n\n"
        "If you were not expecting this, you can ignore this email.\n",
    )


def send_activity_assignment(
    to: str, full_name: str, activity_title: str, requirements: str, due_date, assigned_by: str
) -> None:
    """Tells an educator that an admin handed them a draft. No link and no secrets: they sign in as usual and open
    "My assignments". The title is flattened to one line because it goes into the subject."""
    brand = get_settings().email_brand
    title = " ".join(activity_title.split()) or "an activity"
    due = f"Due: {due_date.strftime('%d %B %Y')}\n\n" if due_date else ""
    send_email(
        to,
        f"New {brand} assignment: {title[:80]}",
        f"Hello {full_name},\n\n"
        f"{assigned_by} has assigned you the draft activity “{title}”.\n\n"
        f"{due}What is needed:\n\n{requirements}\n\n"
        f"Sign in to the {brand} educator console and open “My assignments” to start. "
        "When you are done, check the activity and send it for review.\n",
    )


def send_bulk_activity_assignment(
    to: str, full_name: str, count: int, requirements: str, due_date, assigned_by: str
) -> None:
    """One email for a whole batch of drafts handed to an educator at once: how many, the shared brief and due date."""
    brand = get_settings().email_brand
    noun = "draft activity" if count == 1 else "draft activities"
    due = f"Due: {due_date.strftime('%d %B %Y')}\n\n" if due_date else ""
    send_email(
        to,
        f"{count} new {brand} {'assignment' if count == 1 else 'assignments'}",
        f"Hello {full_name},\n\n"
        f"{assigned_by} has assigned you {count} {noun}.\n\n"
        f"{due}What is needed (the same for each):\n\n{requirements}\n\n"
        f"Sign in to the {brand} educator console and open “My assignments” to see them. "
        "When you are done with each one, check it and send it for review.\n",
    )
