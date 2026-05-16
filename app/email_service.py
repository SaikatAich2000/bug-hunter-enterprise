"""Email service.

Three backends:
  - console   : just logs the email to stdout. Default; perfect for dev.
  - smtp      : sends via real SMTP server.
  - disabled  : no-op. For tests.

All public functions are designed to be called from a FastAPI
BackgroundTasks instance: they take pre-fetched primitives (not DB
sessions or ORM objects) so they don't blow up if called after the
request has finished and the session is closed.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable

from app.config import Settings, get_settings

logger = logging.getLogger("bug_hunter.email")


# ---------------------------------------------------------------------------
# Snapshot dataclasses (no SQLAlchemy objects past this point)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UserSnapshot:
    id: int
    name: str
    email: str

    @property
    def display(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass(frozen=True)
class BugSnapshot:
    id: int
    title: str
    project_name: str
    status: str
    priority: str
    environment: str
    description: str
    reporter: UserSnapshot | None
    assignees: tuple[UserSnapshot, ...]


# ---------------------------------------------------------------------------
# Low-level transport
# ---------------------------------------------------------------------------
def _send_smtp(settings: Settings, msg: EmailMessage) -> None:
    """Synchronous SMTP send. Called from a worker thread by FastAPI."""
    if not settings.SMTP_HOST:
        logger.warning("SMTP backend selected but SMTP_HOST is empty; dropping email.")
        return

    try:
        if settings.SMTP_USE_SSL:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT,
                timeout=settings.SMTP_TIMEOUT, context=ctx,
            ) as s:
                if settings.SMTP_USERNAME:
                    s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT,
                timeout=settings.SMTP_TIMEOUT,
            ) as s:
                s.ehlo()
                if settings.SMTP_USE_TLS:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if settings.SMTP_USERNAME:
                    s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        logger.info("SMTP email sent: subject=%r to=%s", msg["Subject"], msg["To"])
    except Exception:
        # Never let mailer failures break the API.
        logger.exception("Failed to send email via SMTP")


def _send_console(msg: EmailMessage) -> None:
    body = msg.get_content() if msg.is_multipart() is False else "(multipart)"
    logger.info(
        "[console-email]\n  From: %s\n  To: %s\n  Subject: %s\n  ----\n%s\n  ----",
        msg["From"], msg["To"], msg["Subject"], body.strip(),
    )


def _build(subject: str, to: list[str], body: str, settings: Settings) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def deliver(subject: str, to: list[str], body: str) -> None:
    """Public dispatch — pick the right backend based on settings."""
    settings = get_settings()
    if settings.EMAIL_BACKEND == "disabled":
        return
    to = sorted({addr.strip() for addr in to if addr and addr.strip()})
    if not to:
        return

    msg = _build(subject, to, body, settings)
    if settings.EMAIL_BACKEND == "smtp":
        _send_smtp(settings, msg)
    else:
        _send_console(msg)


# ---------------------------------------------------------------------------
# Recipient selection
# ---------------------------------------------------------------------------
def _recipients(bug: BugSnapshot, exclude_user_id: int | None) -> list[str]:
    """Reporter + all assignees, deduped, optionally minus the actor."""
    seen: dict[str, None] = {}
    candidates: list[UserSnapshot] = []
    if bug.reporter:
        candidates.append(bug.reporter)
    candidates.extend(bug.assignees)
    out: list[str] = []
    for u in candidates:
        if exclude_user_id is not None and u.id == exclude_user_id:
            continue
        if not u.email:
            continue
        key = u.email.lower()
        if key in seen:
            continue
        seen[key] = None
        out.append(u.email)
    return out


# ---------------------------------------------------------------------------
# Notification helpers — these are what routes call.
# Each takes only primitive data (no DB session, no ORM objects).
# ---------------------------------------------------------------------------
def _bug_link(bug_id: int) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}/#bug={bug_id}"


def _bug_meta_lines(bug: BugSnapshot) -> list[str]:
    return [
        f"Bug #{bug.id}: {bug.title}",
        f"Project:     {bug.project_name}",
        f"Status:      {bug.status}",
        f"Priority:    {bug.priority}",
        f"Environment: {bug.environment}",
        f"Reporter:    {bug.reporter.display if bug.reporter else '—'}",
        "Assignees:   " + (
            ", ".join(a.display for a in bug.assignees) if bug.assignees else "—"
        ),
    ]


def notify_bug_created(bug: BugSnapshot, actor_user_id: int | None) -> None:
    to = _recipients(bug, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[Bug Hunter] New bug #{bug.id}: {bug.title}"
    lines = ["A new bug has been reported.", ""]
    lines += _bug_meta_lines(bug)
    if bug.description:
        lines += ["", "Description:", bug.description]
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_bug_updated(
    bug: BugSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if not changes:
        return
    to = _recipients(bug, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[Bug Hunter] Bug #{bug.id} updated: {bug.title}"
    lines = [f"{actor_name} updated bug #{bug.id}.", "", "Changes:"]
    for field, old, new in changes:
        lines.append(f"  • {field}: {old or '(empty)'} → {new or '(empty)'}")
    lines += [""] + _bug_meta_lines(bug)
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_assignment(
    bug: BugSnapshot,
    newly_assigned: Iterable[UserSnapshot],
    actor_name: str,
) -> None:
    """Send a personalized 'you've been assigned' email to each new assignee."""
    for user in newly_assigned:
        if not user.email:
            continue
        subject = f"[Bug Hunter] You've been assigned to bug #{bug.id}: {bug.title}"
        lines = [
            f"Hi {user.name},",
            "",
            f"{actor_name} assigned you to a bug.",
            "",
        ]
        lines += _bug_meta_lines(bug)
        if bug.description:
            lines += ["", "Description:", bug.description]
        lines += ["", f"View: {_bug_link(bug.id)}"]
        deliver(subject, [user.email], "\n".join(lines))


def notify_comment_added(
    bug: BugSnapshot,
    comment_author_name: str,
    comment_author_id: int | None,
    comment_body: str,
) -> None:
    to = _recipients(bug, exclude_user_id=comment_author_id)
    if not to:
        return
    subject = f"[Bug Hunter] New comment on bug #{bug.id}: {bug.title}"
    lines = [
        f"{comment_author_name} commented on bug #{bug.id}:",
        "",
        comment_body,
        "",
        "---",
    ]
    lines += _bug_meta_lines(bug)
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines))


def notify_password_reset(email: str, name: str, reset_url: str) -> None:
    """Send the user a password-reset link."""
    if not email:
        return
    subject = "[Bug Hunter] Reset your password"
    body = "\n".join([
        f"Hi {name or 'there'},",
        "",
        "We received a request to reset your Bug Hunter password.",
        "Click the link below to choose a new one. The link is valid for 2 hours.",
        "",
        reset_url,
        "",
        "If you didn't request this, you can ignore this email — your password "
        "won't change unless someone uses the link.",
        "",
        "— Bug Hunter",
    ])
    deliver(subject, [email], body)


def notify_invitation(
    email: str,
    inviter_name: str,
    org_name: str,
    accept_url: str,
    role: str,
) -> None:
    """Send the invitee a link to join an organization."""
    if not email:
        return
    role_label = {"admin": "an admin", "manager": "a manager", "member": "a member"}.get(
        role, f"a {role}"
    )
    subject = f"[Bug Hunter] {inviter_name or 'Someone'} invited you to {org_name or 'Bug Hunter'}"
    body = "\n".join([
        "Hi,",
        "",
        f"{inviter_name or 'A colleague'} has invited you to join "
        f"\"{org_name}\" on Bug Hunter as {role_label}.",
        "",
        "Click the link below to set your name + password and join the team. "
        "The link is valid for 7 days.",
        "",
        accept_url,
        "",
        "If you weren't expecting this email, you can safely ignore it.",
        "",
        "— Bug Hunter",
    ])
    deliver(subject, [email], body)


def notify_email_change_code(
    new_email: str,
    user_name: str,
    code: str,
) -> None:
    """Send a 6-digit verification code to the NEW email address. Sent
    only to the new address (never the old) — we want to confirm the
    user actually controls the inbox they're switching to."""
    if not new_email:
        return
    subject = "[Bug Hunter] Confirm your new email address"
    body = "\n".join([
        f"Hi {user_name or 'there'},",
        "",
        "Use this 6-digit code to confirm your new Bug Hunter email address:",
        "",
        f"    {code}",
        "",
        "The code expires in 15 minutes. If you didn't request this, you can",
        "ignore this email — nothing will change unless the code is entered.",
        "",
        "— Bug Hunter",
    ])
    deliver(subject, [new_email], body)
