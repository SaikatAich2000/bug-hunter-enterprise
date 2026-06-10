"""Firebase Cloud Messaging — HTTP v1 sender.

We avoid the Firebase Admin SDK because it pulls gRPC + protobuf into a
deployment image that doesn't need them. Instead we:

  1. Parse the service-account JSON once at startup (`FIREBASE_SA_JSON`).
  2. Mint a short-lived OAuth2 access token for the FCM scope using
     `google.oauth2.service_account`. The library handles JWT signing,
     refresh, and caching — we just call `credentials.token`.
  3. POST one HTTP v1 request per device token to
     `https://fcm.googleapis.com/v1/projects/<id>/messages:send` using
     the existing httpx dependency.

Stale-token handling: FCM returns `UNREGISTERED` or `INVALID_ARGUMENT`
for tokens the device has retired (uninstall, data clear, app rotation).
We delete those rows so the table doesn't grow unbounded.

Recipient-side preferences are checked here, not at the route layer, so
every notify-* call site automatically respects the user's opt-out
without each caller having to remember.

Channels:
  - 'mentions'    → bh_mentions
  - 'assignments' → bh_assignments
  - 'activity'    → bh_activity   (default; anything unrecognised falls through)

When FIREBASE_PROJECT_ID or FIREBASE_SA_JSON is blank, every public
function short-circuits to a no-op. This lets dev / test environments
run without Firebase credentials configured.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Iterable

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import DeviceToken, NotificationPreference, User

logger = logging.getLogger("bug_hunter.push")

# FCM HTTP v1 base URL — concatenate <project_id>/messages:send.
_FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Stale-token error markers in the HTTP v1 response body. Either of these
# means the token will never deliver again — clean it up so the database
# doesn't grow forever.
_STALE_ERROR_STATUSES = {"NOT_FOUND", "UNREGISTERED", "INVALID_ARGUMENT"}

CHANNEL_MENTIONS = "mentions"
CHANNEL_ASSIGNMENTS = "assignments"
CHANNEL_ACTIVITY = "activity"
_VALID_CHANNELS = {CHANNEL_MENTIONS, CHANNEL_ASSIGNMENTS, CHANNEL_ACTIVITY}


@dataclass(frozen=True)
class PushMessage:
    """Immutable view of a single push, pre-flight.

    Mirrors the snapshot pattern in `email_service.py` — primitive data
    only so background tasks can fire after the request session closes.
    """
    title: str
    body: str
    channel: str
    # Optional deep link. We standardise on the `app://bughunter/...`
    # scheme so the Android client's NavHost picks it up via the
    # existing intent filter. Example: 'app://bughunter/bug/42'.
    deep_link: str | None = None
    # Optional collapse key — the Android service uses this as the
    # notification tag, so the latest push for the same item replaces
    # earlier ones. Example: 'bug:42'.
    tag: str | None = None


# ---------------------------------------------------------------------------
# Credentials — parsed once, refreshed by google-auth on demand.
# ---------------------------------------------------------------------------
class _CredentialCache:
    """Thread-safe wrapper around the service-account Credentials object.

    `google.oauth2.service_account.Credentials` already does its own
    refresh, but the underlying Credentials object is NOT thread-safe
    when multiple workers call `.refresh()` concurrently — we wrap it in
    a lock to be safe under uvicorn worker concurrency.

    When the service-account JSON is missing or unparseable, this stays
    in a 'disabled' state and `access_token()` returns None — every push
    call then short-circuits without raising.
    """

    def __init__(self, settings: Settings) -> None:
        self._lock = threading.Lock()
        self._credentials = None
        self._project_id = settings.FIREBASE_PROJECT_ID.strip()
        self._enabled = bool(self._project_id) and bool(settings.FIREBASE_SA_JSON.strip())
        if not self._enabled:
            return
        try:
            from google.oauth2 import service_account
            info = json.loads(settings.FIREBASE_SA_JSON)
            self._credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[_FCM_SCOPE],
            )
        except Exception:
            logger.exception(
                "Failed to parse FIREBASE_SA_JSON; FCM push will be disabled. "
                "Check the env var contains the entire service-account JSON, "
                "single-line and unescaped."
            )
            self._enabled = False
            self._credentials = None

    @property
    def enabled(self) -> bool:
        return self._enabled and self._credentials is not None

    @property
    def project_id(self) -> str:
        return self._project_id

    def access_token(self) -> str | None:
        """Mint or reuse a cached access token. Returns None when the
        push subsystem is disabled — callers should no-op silently."""
        if not self.enabled:
            return None
        with self._lock:
            try:
                from google.auth.transport.requests import Request
                creds = self._credentials
                if creds is None:
                    return None
                if not creds.valid:
                    creds.refresh(Request())
                return creds.token
            except Exception:
                logger.exception("FCM access token refresh failed")
                return None


_credential_cache: _CredentialCache | None = None
_cache_lock = threading.Lock()


def _get_cache() -> _CredentialCache:
    """Lazy initialiser so importing this module doesn't touch Firebase.
    Tests can monkeypatch `app.push_service._credential_cache` directly."""
    global _credential_cache  # noqa: PLW0603 — singleton init
    with _cache_lock:
        if _credential_cache is None:
            _credential_cache = _CredentialCache(get_settings())
        return _credential_cache


def reset_credentials_cache() -> None:
    """Drop the cached credentials. Tests call this between cases that
    flip FIREBASE_* env vars; production never needs it."""
    global _credential_cache  # noqa: PLW0603
    with _cache_lock:
        _credential_cache = None


# ---------------------------------------------------------------------------
# Preferences gating
# ---------------------------------------------------------------------------
def _channel_enabled_for_user(db: Session, user_id: int, channel: str) -> bool:
    pref = db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    # Missing row = all-on (see app/models.py NotificationPreference).
    if pref is None:
        return True
    if channel == CHANNEL_MENTIONS:
        return pref.mentions
    if channel == CHANNEL_ASSIGNMENTS:
        return pref.assignments
    # Anything else maps to activity (lowest importance).
    return pref.activity


def _normalise_channel(channel: str) -> str:
    return channel if channel in _VALID_CHANNELS else CHANNEL_ACTIVITY


# ---------------------------------------------------------------------------
# Wire-level send
# ---------------------------------------------------------------------------
def _build_payload(token: str, msg: PushMessage) -> dict:
    """The HTTP v1 'message' object. We only use the `data` field — see
    BugHunterFcmService for why notification payloads are intentionally
    avoided (they bypass our render path when the app is backgrounded)."""
    data: dict[str, str] = {
        "title": msg.title,
        "body": msg.body,
        "channel": msg.channel,
    }
    if msg.deep_link:
        data["deep_link"] = msg.deep_link
    if msg.tag:
        data["tag"] = msg.tag
    return {
        "message": {
            "token": token,
            "data": data,
        }
    }


def _is_stale_token_error(response: httpx.Response) -> bool:
    """Detect the 'this token is dead, delete it' responses from FCM."""
    if response.status_code in (404, 410):
        return True
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error", {}) if isinstance(body, dict) else {}
    status_text = (error.get("status") or "").upper()
    if status_text in _STALE_ERROR_STATUSES:
        return True
    # HTTP v1 nests the FCM error under details[].errorCode for some
    # token-not-found cases — walk it defensively.
    for detail in error.get("details", []) or []:
        if not isinstance(detail, dict):
            continue
        if (detail.get("errorCode") or "").upper() in _STALE_ERROR_STATUSES:
            return True
    return False


def _send_one(
    client: httpx.Client,
    url: str,
    access_token: str,
    token: str,
    msg: PushMessage,
) -> httpx.Response | None:
    """Single POST. Returns the response on a network round-trip, None on
    transport error. Caller decides what to do with the result."""
    try:
        return client.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=_build_payload(token, msg),
        )
    except httpx.HTTPError:
        logger.exception("FCM HTTP error sending push to token=%s…", token[:12])
        return None


# ---------------------------------------------------------------------------
# Public API — called from notify_* hooks in app/push_notify.py
# ---------------------------------------------------------------------------
def send_to_user(
    db: Session,
    user_id: int,
    *,
    title: str,
    body: str,
    channel: str = CHANNEL_ACTIVITY,
    deep_link: str | None = None,
    tag: str | None = None,
) -> int:
    """Fan out one logical push to every registered token for the user.

    Returns the number of tokens we successfully POSTed for (post-prefs,
    post-stale-cleanup). Zero is a legitimate result — no tokens / opted
    out — and never an error condition.

    Stale-token rows are deleted in this function so the next caller
    isn't burdened with cleanup. We commit the deletion synchronously to
    keep the database tidy even if the HTTP send loop is partially
    completed.
    """
    cache = _get_cache()
    if not cache.enabled:
        # Push disabled — silent no-op. Avoids spamming the log with
        # "we tried but config is empty" lines on every notify call.
        return 0

    channel = _normalise_channel(channel)
    if not _channel_enabled_for_user(db, user_id, channel):
        return 0

    tokens = db.scalars(
        select(DeviceToken).where(DeviceToken.user_id == user_id)
    ).all()
    if not tokens:
        return 0

    access_token = cache.access_token()
    if access_token is None:
        return 0

    settings = get_settings()
    url = _FCM_ENDPOINT.format(project_id=cache.project_id)
    msg = PushMessage(
        title=title, body=body, channel=channel,
        deep_link=deep_link, tag=tag,
    )

    stale_ids: list[int] = []
    sent_count = 0
    with httpx.Client(timeout=settings.FIREBASE_HTTP_TIMEOUT_SECONDS) as client:
        for row in tokens:
            response = _send_one(client, url, access_token, row.token, msg)
            if response is None:
                continue
            if response.status_code == 200:
                sent_count += 1
                continue
            if _is_stale_token_error(response):
                stale_ids.append(row.id)
                continue
            # Other 4xx / 5xx — log but keep the row. Could be a
            # transient FCM regional outage; retrying on the next push
            # is the right behaviour.
            logger.warning(
                "FCM push failed: status=%d token=%s… body=%s",
                response.status_code, row.token[:12], response.text[:200],
            )

    if stale_ids:
        db.execute(delete(DeviceToken).where(DeviceToken.id.in_(stale_ids)))
        db.commit()
        logger.info("FCM: deleted %d stale tokens for user_id=%d", len(stale_ids), user_id)

    return sent_count


def send_to_users(
    db: Session,
    user_ids: Iterable[int],
    *,
    title: str,
    body: str,
    channel: str = CHANNEL_ACTIVITY,
    deep_link: str | None = None,
    tag: str | None = None,
) -> int:
    """Fan out the same push to multiple users. Returns the total
    number of tokens we POSTed for, summed across all users. Caller is
    responsible for dedupe — passing the same user_id twice will send
    them two notifications."""
    total = 0
    for uid in user_ids:
        total += send_to_user(
            db, uid,
            title=title, body=body, channel=channel,
            deep_link=deep_link, tag=tag,
        )
    return total
