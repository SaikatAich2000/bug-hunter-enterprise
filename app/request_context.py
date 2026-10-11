"""Request IDs: one id per HTTP request, on the response and on every log line it causes.

The id comes from the client's ``X-Request-ID`` when it is a short, safe token (so a proxy's id
carries through), otherwise it is generated. Support can match a user's "reference" to the
server log, and the JSON log format includes it on every record.
"""
from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
HEADER = b"x-request-id"


def current_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` (empty outside a request) for formatters to use."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


class RequestIdMiddleware:
    """Pure ASGI: sets the request id for the duration of the request and echoes it back."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or ()).get(HEADER, b"").decode("latin-1")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        # Not reset afterwards: each request runs in its own task context, and the outermost
        # error handler (which runs after this middleware unwinds) still needs the id.
        _request_id.set(rid)

        async def send_with_id(message):
            if message.get("type") == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != HEADER]
                headers.append((HEADER, rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


def install_log_filter() -> None:
    """Attach the filter to the root handlers so every record carries ``request_id``."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())
