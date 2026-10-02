"""Email service with console / smtp / disabled backends.

Public functions run from FastAPI BackgroundTasks: they take pre-fetched
primitives (no DB sessions/ORM) so they work after the request session closes.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable

from app.config import Settings, get_settings

logger = logging.getLogger("bug_hunter.email")

# Redact ?token=/&token= values so a DEBUG log can't be replayed.
_TOKEN_IN_URL_RE = re.compile(r"(?i)([?&]token=)[^&\s]+")


def _redact_secrets_for_log(text: str) -> str:
    return _TOKEN_IN_URL_RE.sub(r"\1[REDACTED]", text)

# Shared label for the description block in notification bodies.
_DESC_LABEL = "Description:"


# --- Snapshot dataclasses (no SQLAlchemy objects past this point) ---
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
    """Snapshot of one work-item row for the email layer.

    Defaults ("Bug" / None) keep pre-migration rows and older callers correct.
    """
    id: int
    title: str
    project_name: str
    status: str
    priority: str
    environment: str
    description: str
    reporter: UserSnapshot | None
    assignees: tuple[UserSnapshot, ...]
    item_type: str = "Bug"
    event_name: str | None = None
    # The organization's own From address (Organization.email_from_override), if any.
    from_address: str | None = None


# --- Low-level transport ---
def _send_smtp(settings: Settings, msg: EmailMessage) -> bool:
    """Synchronous SMTP send. Called from a worker thread by FastAPI."""
    if not settings.SMTP_HOST:
        logger.warning("SMTP backend selected but SMTP_HOST is empty; dropping email.")
        return False   # nothing sent → report failure, like the except path below

    try:
        # Explicit TLS settings make the security posture obvious.
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        if settings.SMTP_USE_SSL:
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
                    s.starttls(context=ctx)
                    s.ehlo()
                if settings.SMTP_USERNAME:
                    s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                s.send_message(msg)
        logger.info("SMTP email sent: subject=%r to=%s", msg["Subject"], msg["To"])
        return True
    except (smtplib.SMTPException, OSError):
        # Narrow catch: programming errors still raise; callers see False.
        logger.exception("Failed to send email via SMTP")
        return False


def _send_console(msg: EmailMessage) -> None:
    # Body may contain a live reset link or PII — gate it behind DEBUG.
    logger.info(
        "[console-email] to=%s subject=%r (body suppressed — set log level "
        "DEBUG to include it)",
        msg["To"], msg["Subject"],
    )
    if logger.isEnabledFor(logging.DEBUG):
        body = msg.get_content() if msg.is_multipart() is False else "(multipart)"
        logger.debug("[console-email] body for %r:\n%s", msg["Subject"],
                     _redact_secrets_for_log(body.strip()))


def _header_safe(value: str) -> str:
    """Strip CR/LF — header-injection defense; also keeps a newline from 500ing the send."""
    return value.replace("\r", " ").replace("\n", " ")


def _build(
    subject: str, to: list[str], body: str, settings: Settings, from_address: str | None = None,
) -> EmailMessage:
    sender = from_address or settings.EMAIL_FROM
    msg = EmailMessage()
    msg["From"] = _header_safe(sender)
    if len(to) == 1:
        msg["To"] = _header_safe(to[0])
    else:
        # Bcc hides recipients from each other; smtplib delivers via the envelope.
        msg["To"] = _header_safe(sender)
        msg["Bcc"] = ", ".join(_header_safe(addr) for addr in to)
    msg["Subject"] = _header_safe(subject)
    msg.set_content(body)
    return msg


def deliver(
    subject: str, to: list[str], body: str, from_address: str | None = None,
) -> bool:
    """Dispatch to the configured backend. ``from_address`` overrides EMAIL_FROM (an
    organization's own sender).

    True = handed to a backend (and accepted, for SMTP); False = skipped or failed."""
    settings = get_settings()
    if settings.EMAIL_BACKEND == "disabled":
        return False
    to = sorted({addr.strip() for addr in to if addr and addr.strip()})
    if not to:
        return False

    msg = _build(subject, to, body, settings, from_address)
    if settings.EMAIL_BACKEND == "smtp":
        return _send_smtp(settings, msg)
    _send_console(msg)
    return True


def _digest_owns_work_item_email() -> bool:
    """True when the daily digest job (not immediate `notify_*` sends) owns
    work-item emails. Transactional emails (password reset) never consult this."""
    return get_settings().EMAIL_DIGEST_ENABLED


# --- Recipient selection ---
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


# Notification helpers — these are what routes call.
# Each takes only primitive data (no DB session, no ORM objects).
def _bug_link(bug_id: int) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}/#bug={bug_id}"


def _item_label(bug: BugSnapshot) -> str:
    """The grammatical noun for the item: 'bug' / 'requirement' / 'task'."""
    return (bug.item_type or "Bug").lower()


def _bug_meta_lines(bug: BugSnapshot) -> list[str]:
    """Plain-text metadata block reused across every notification email."""
    label_cap = (bug.item_type or "Bug").capitalize()
    lines = [
        f"{label_cap} #{bug.id}: {bug.title}",
        f"Type:        {bug.item_type or 'Bug'}",
    ]
    if bug.event_name:
        lines.append(f"Event:       {bug.event_name}")
    lines += [
        f"Project:     {bug.project_name}",
        f"Status:      {bug.status}",
        f"Priority:    {bug.priority}",
        f"Environment: {bug.environment}",
        f"Reporter:    {bug.reporter.display if bug.reporter else '—'}",
        "Assignees:   " + (
            ", ".join(a.display for a in bug.assignees) if bug.assignees else "—"
        ),
    ]
    return lines


def notify_bug_created(bug: BugSnapshot, actor_user_id: int | None) -> None:
    if _digest_owns_work_item_email():
        return
    to = _recipients(bug, exclude_user_id=actor_user_id)
    if not to:
        return
    label = _item_label(bug)
    subject = f"[{get_settings().APP_NAME}] New {label} #{bug.id}: {bug.title}"
    lines = [f"A new {label} has been created.", ""]
    lines += _bug_meta_lines(bug)
    if bug.description:
        lines += ["", _DESC_LABEL, bug.description]
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines), bug.from_address)


def notify_bug_updated(
    bug: BugSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    if not changes:
        return
    to = _recipients(bug, exclude_user_id=actor_user_id)
    if not to:
        return
    label = _item_label(bug)
    label_cap = (bug.item_type or "Bug").capitalize()
    subject = f"[{get_settings().APP_NAME}] {label_cap} #{bug.id} updated: {bug.title}"
    lines = [f"{actor_name} updated {label} #{bug.id}.", "", "Changes:"]
    for field, old, new in changes:
        lines.append(f"  • {field}: {old or '(empty)'} → {new or '(empty)'}")
    lines += [""] + _bug_meta_lines(bug)
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines), bug.from_address)


def notify_assignment(
    bug: BugSnapshot,
    newly_assigned: Iterable[UserSnapshot],
    actor_name: str,
) -> None:
    """Send a personalized 'you've been assigned' email to each new assignee."""
    if _digest_owns_work_item_email():
        return
    label = _item_label(bug)
    for user in newly_assigned:
        if not user.email:
            continue
        subject = f"[{get_settings().APP_NAME}] You've been assigned to {label} #{bug.id}: {bug.title}"
        lines = [
            f"Hi {user.name},",
            "",
            f"{actor_name} assigned you to a {label}.",
            "",
        ]
        lines += _bug_meta_lines(bug)
        if bug.description:
            lines += ["", _DESC_LABEL, bug.description]
        lines += ["", f"View: {_bug_link(bug.id)}"]
        deliver(subject, [user.email], "\n".join(lines), bug.from_address)


def notify_comment_added(
    bug: BugSnapshot,
    comment_author_name: str,
    comment_author_id: int | None,
    comment_body: str,
) -> None:
    if _digest_owns_work_item_email():
        return
    to = _recipients(bug, exclude_user_id=comment_author_id)
    if not to:
        return
    label = _item_label(bug)
    subject = f"[{get_settings().APP_NAME}] New comment on {label} #{bug.id}: {bug.title}"
    lines = [
        f"{comment_author_name} commented on {label} #{bug.id}:",
        "",
        comment_body,
        "",
        "---",
    ]
    lines += _bug_meta_lines(bug)
    lines += ["", f"View: {_bug_link(bug.id)}"]
    deliver(subject, to, "\n".join(lines), bug.from_address)


# --- Event notifications (separate channel from per-item assignment emails) ---
@dataclass(frozen=True)
class EventSnapshot:
    """Event snapshot for emails; ORM-free so BackgroundTasks can use it."""
    id: int
    name: str
    description: str
    scheduled_for: str | None
    managers: tuple[UserSnapshot, ...]
    from_address: str | None = None


def _event_recipients(ev: EventSnapshot, exclude_user_id: int | None) -> list[str]:
    """Recipients of an event email: every manager except the actor."""
    out: list[str] = []
    seen: dict[str, None] = {}
    for u in ev.managers:
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


def _event_meta_lines(ev: EventSnapshot) -> list[str]:
    return [
        f"Event #{ev.id}: {ev.name}",
        f"Scheduled: {ev.scheduled_for or '—'}",
        "Managers:  " + (
            ", ".join(m.display for m in ev.managers) if ev.managers else "—"
        ),
    ]


def _event_link(event_id: int) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}/#event={event_id}"


def notify_event_created(
    ev: EventSnapshot,
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    to = _event_recipients(ev, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[{get_settings().APP_NAME}] New event #{ev.id}: {ev.name}"
    lines = [
        f"{actor_name} created a new event you're managing.",
        "",
    ]
    lines += _event_meta_lines(ev)
    if ev.description:
        lines += ["", _DESC_LABEL, ev.description]
    lines += ["", f"View: {_event_link(ev.id)}"]
    deliver(subject, to, "\n".join(lines), ev.from_address)


def notify_event_updated(
    ev: EventSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    if not changes:
        return
    to = _event_recipients(ev, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[{get_settings().APP_NAME}] Event #{ev.id} updated: {ev.name}"
    lines = [f"{actor_name} updated event #{ev.id}.", "", "Changes:"]
    for field, old, new in changes:
        lines.append(f"  • {field}: {old or '(empty)'} → {new or '(empty)'}")
    lines += [""] + _event_meta_lines(ev)
    lines += ["", f"View: {_event_link(ev.id)}"]
    deliver(subject, to, "\n".join(lines), ev.from_address)


def notify_event_deleted(
    ev: EventSnapshot,
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if _digest_owns_work_item_email():
        return
    to = _event_recipients(ev, exclude_user_id=actor_user_id)
    if not to:
        return
    subject = f"[{get_settings().APP_NAME}] Event #{ev.id} deleted: {ev.name}"
    lines = [
        f"{actor_name} deleted event #{ev.id}: {ev.name}.",
        "",
        "Any items that belonged to this event are preserved as standalone work items.",
    ]
    deliver(subject, to, "\n".join(lines), ev.from_address)


def notify_password_reset(
    email: str, name: str, reset_url: str, from_address: str | None = None,
) -> None:
    """Send the user a password-reset link."""
    if not email:
        return
    app_name = get_settings().APP_NAME
    subject = f"[{app_name}] Reset your password"
    body = "\n".join([
        f"Hi {name or 'there'},",
        "",
        f"We received a request to reset your {app_name} password.",
        "Click the link below to choose a new one. The link is valid for 2 hours.",
        "",
        reset_url,
        "",
        "If you didn't request this, you can ignore this email — your password "
        "won't change unless someone uses the link.",
        "",
        f"— {app_name}",
    ])
    # Transactional: log a clear error on failure — the user only sees the
    # generic "if an account exists" response.
    if not deliver(subject, [email], body, from_address) and get_settings().EMAIL_BACKEND != "disabled":
        logger.error("Password-reset email was NOT delivered for a reset request.")


def notify_invitation(
    email: str,
    inviter_name: str,
    org_name: str,
    accept_url: str,
    role: str,
    from_address: str | None = None,
) -> None:
    """Send the invitee a link to join an organization (valid 7 days)."""
    if not email:
        return
    app_name = get_settings().APP_NAME
    role_label = {"admin": "an admin", "manager": "a manager", "user": "a member"}.get(role, f"a {role}")
    inviter = inviter_name or "A colleague"
    subject = f"[{app_name}] {inviter} invited you to {org_name or app_name}"
    body = "\n".join([
        "Hi,",
        "",
        f"{inviter} has invited you to join \"{org_name}\" on {app_name} as {role_label}.",
        "",
        "Open the link below to set your name and password and join the team. "
        "The link is valid for 7 days.",
        "",
        accept_url,
        "",
        "If you weren't expecting this email, you can safely ignore it.",
        "",
        f"— {app_name}",
    ])
    if not deliver(subject, [email], body, from_address) and get_settings().EMAIL_BACKEND != "disabled":
        logger.error("Invitation email was NOT delivered.")


def notify_email_change_code(
    new_email: str, user_name: str, code: str, from_address: str | None = None,
) -> None:
    """Send the 6-digit confirmation code to the NEW address only, so the change proves
    control of the inbox being switched to."""
    if not new_email:
        return
    app_name = get_settings().APP_NAME
    subject = f"[{app_name}] Confirm your new email address"
    body = "\n".join([
        f"Hi {user_name or 'there'},",
        "",
        f"Use this 6-digit code to confirm your new {app_name} email address:",
        "",
        f"    {code}",
        "",
        "The code expires in 15 minutes. If you didn't request this, ignore this email;",
        "nothing changes unless the code is entered.",
        "",
        f"— {app_name}",
    ])
    if not deliver(subject, [new_email], body, from_address) and get_settings().EMAIL_BACKEND != "disabled":
        logger.error("Email-change code was NOT delivered.")
