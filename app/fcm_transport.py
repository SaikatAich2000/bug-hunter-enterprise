"""Firebase Cloud Messaging transport (optional external integration).

Lazily initialises the Firebase Admin SDK and multicasts notifications.
Never raises: failures are logged and a failed init disables push for the
process lifetime. Tests mock ``send()`` via ``app.push_service``.
"""
from __future__ import annotations

import base64
import json
import logging
import threading

from app.config import get_settings

logger = logging.getLogger("bug_hunter.push")

# Cached Firebase app; a hard init failure latches so sends stop retrying.
_state: dict = {"app": None, "init_failed": False}
_init_lock = threading.Lock()

# FCM error class names that indicate a permanently invalid token.
_DEAD_TOKEN_ERRORS = frozenset({"UnregisteredError", "SenderIdMismatchError"})
# Error substrings FCM uses for a token that will never work again.
_DEAD_TOKEN_MARKERS = (
    "not-registered",
    "registration-token-not-registered",
    "invalid-registration-token",
    "invalid-argument",
)
# send_each_for_multicast rejects a call with more than 500 tokens.
_MAX_TOKENS_PER_BATCH = 500
# FCM caps the whole payload at 4 KB; keep the text well under it.
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 1000


def _load_credential_json(raw: str) -> dict | None:
    """Parse FCM_CREDENTIALS_JSON as raw JSON or base64-encoded JSON."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        return json.loads(decoded)
    except ValueError:
        logger.exception("FCM_CREDENTIALS_JSON is not valid JSON or base64-encoded JSON.")
        return None


def _ensure_app():
    """Return the cached Firebase app, initialising on first call; None on failure."""
    if _state["app"] is not None:
        return _state["app"]
    if _state["init_failed"]:
        return None
    with _init_lock:
        if _state["app"] is not None:
            return _state["app"]
        if _state["init_failed"]:
            return None
        settings = get_settings()
        cred_source: str | dict
        if settings.FCM_CREDENTIALS_JSON:
            parsed = _load_credential_json(settings.FCM_CREDENTIALS_JSON)
            if parsed is None:
                _state["init_failed"] = True
                return None
            cred_source = parsed
        elif settings.FCM_CREDENTIALS_FILE:
            cred_source = settings.FCM_CREDENTIALS_FILE
        else:
            logger.warning("Web push enabled but no FCM_CREDENTIALS_JSON/FCM_CREDENTIALS_FILE is set.")
            _state["init_failed"] = True
            return None
        try:
            import firebase_admin
            from firebase_admin import credentials
            cred = credentials.Certificate(cred_source)
            _state["app"] = firebase_admin.initialize_app(cred, name="bug-hunter-push")
            logger.info("Firebase Admin initialised for web push.")
            return _state["app"]
        except Exception:  # noqa: BLE001 — any init failure just disables push
            logger.exception("Firebase Admin init failed; web push disabled this run.")
            _state["init_failed"] = True
            return None


def _is_dead_token(exc) -> bool:
    if exc is None:
        return False
    if type(exc).__name__ in _DEAD_TOKEN_ERRORS:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _DEAD_TOKEN_MARKERS)


def _absolute_link(url: str) -> str:
    """Resolve an app-relative deep link against APP_BASE_URL. FCM's webpush link
    must be absolute, so a relative one would otherwise be dropped entirely."""
    if not url or not url.startswith("/"):
        return url
    base = (get_settings().APP_BASE_URL or "").rstrip("/")
    return f"{base}{url}" if base else url


def _webpush_config(messaging, url: str):
    """WebpushConfig with a click-through link — https only; FCM rejects other
    schemes and would abort the whole multicast. data["url"] still carries it."""
    link = _absolute_link(url)
    if link.startswith("https://"):
        return messaging.WebpushConfig(
            fcm_options=messaging.WebpushFCMOptions(link=link),
        )
    return None


def _send_one_batch(
    messaging, app, batch: list[str], title: str, body: str, payload: dict, webpush,
    data_only: bool = False,
) -> tuple[int, list[str]]:
    """Sends one multicast batch. Returns (delivered_count, dead_tokens)."""
    # data["url"] is the deep link for Android and the web service worker.
    if data_only:
        # The Android app renders its own notification (and picks the notification channel
        # from data["channel"]); a "notification" payload would bypass that when backgrounded.
        message = messaging.MulticastMessage(
            tokens=batch, data=payload,
            android=messaging.AndroidConfig(priority="high"),
        )
    else:
        message = messaging.MulticastMessage(
            tokens=batch,
            notification=messaging.Notification(title=title, body=body),
            data=payload,
            webpush=webpush,
        )
    try:
        resp = messaging.send_each_for_multicast(message, app=app)
    except Exception:  # noqa: BLE001
        logger.exception("FCM multicast send failed for %d token(s)", len(batch))
        return 0, []

    if len(resp.responses) != len(batch):  # pragma: no cover - FCM returns 1:1
        # zip() would silently drop unmatched tokens; surface the mismatch.
        logger.warning(
            "FCM returned %d responses for %d tokens — response/token mismatch",
            len(resp.responses), len(batch),
        )
    delivered = 0
    dead: list[str] = []
    for tok, result in zip(batch, resp.responses):
        if result.success:
            delivered += 1
            continue
        if _is_dead_token(result.exception):
            dead.append(tok)
        else:
            logger.warning("FCM send to a token failed: %s", result.exception)
    return delivered, dead


def send(
    tokens, *, title: str, body: str, url: str = "", data: dict | None = None,
    channel: str = "", data_only: bool = False,
) -> list[str]:
    """Send one notification to many FCM tokens.

    ``channel`` (mentions / assignments / activity) is passed along as data for clients that
    sort notifications into channels; ``data_only`` sends no "notification" block (native Android).
    Returns the tokens FCM reported dead (for pruning); [] on failure, never raises.
    """
    tokens = list(tokens or [])
    if not tokens:
        return []
    # None url would raise in url.startswith(), breaking the never-raises contract.
    url = url or ""
    app = _ensure_app()
    if app is None:
        return []
    try:
        from firebase_admin import messaging
    except Exception:  # noqa: BLE001
        logger.exception("firebase_admin.messaging import failed")
        return []

    # FCM rejects a notification with an empty title outright.
    title = (title or get_settings().APP_NAME)[:_MAX_TITLE_CHARS]
    body = (body or "")[:_MAX_BODY_CHARS]
    payload = {"url": url or "/"}
    if data_only:
        payload.update({"title": title, "body": body, "deep_link": url or "/"})
    if channel:
        payload["channel"] = channel
    if data:
        payload.update({k: str(v) for k, v in data.items()})
    webpush = _webpush_config(messaging, url)

    dead: list[str] = []
    delivered = 0
    for start in range(0, len(tokens), _MAX_TOKENS_PER_BATCH):
        batch = tokens[start:start + _MAX_TOKENS_PER_BATCH]
        batch_delivered, batch_dead = _send_one_batch(
            messaging, app, batch, title, body, payload, webpush, data_only,
        )
        delivered += batch_delivered
        dead.extend(batch_dead)

    logger.info(
        "FCM send: %d/%d delivered, %d dead token(s) pruned.",
        delivered, len(tokens), len(dead),
    )
    return dead
