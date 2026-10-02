"""Reusable OpenAPI response documentation for Sonar python:S8415.

Every FastAPI route that raises :class:`HTTPException` documents those
responses with one of these maps. Maps are shared **only** where the raised
status codes (and their user-facing meaning) are genuinely identical; a route
with a unique response documents it inline instead.

The values are plain descriptions — the app's error contract carries details in
the JSON ``detail`` body and the ``X-Error-Code`` header, which FastAPI emits
from the raised exception itself.
"""

from __future__ import annotations

from typing import Final


def _doc(*codes: int) -> dict[str, dict[str, str]]:
    return {str(c): {"description": ""} for c in codes}


#: Wrong credentials / not signed in.
AUTH_401: Final = _doc(401)
#: Current-password mismatch or same-password change.
PASSWORD_CHANGE_400: Final = _doc(400)
#: Enumeration-safe 404 for unknown reset accounts (non-safe deployments only).
FORGOT_PASSWORD_404: Final = _doc(404)
#: Invalid/expired reset token.
RESET_TOKEN_400: Final = _doc(400)
#: Entity does not exist (or is scoped away from the caller).
NOT_FOUND_404: Final = _doc(404)
#: Stale version / conflicting concurrent edit / duplicate.
CONFLICT_409: Final = _doc(409)
#: Not found + conflict (rename/transition races).
NOT_FOUND_CONFLICT_409: Final = _doc(404, 409)
#: Not found + validation.
NOT_FOUND_VALIDATION_422: Final = _doc(404, 422)
#: Not found + forbidden.
NOT_FOUND_FORBIDDEN_403: Final = _doc(404, 403)
#: Not found + conflict + validation.
NOT_FOUND_CONFLICT_VALIDATION_422: Final = _doc(404, 409, 422)
#: Malformed request rejected before/around body validation.
BAD_REQUEST_400: Final = _doc(400)
#: Malformed request + entity not visible (user delete: self/last-admin are 400).
BAD_REQUEST_NOT_FOUND_404: Final = _doc(400, 404)
XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file_200(description: str, *mimes: str) -> dict[str, dict]:
    """A 200 whose body is a file, so clients don't expect JSON."""
    schema = {"type": "string", "format": "binary"}
    return {"200": {"description": description, "content": {m: {"schema": schema} for m in mimes}}}


#: Excel workbook download.
XLSX_FILE_200: Final = _file_200("Excel workbook", XLSX_MIME)
#: Report export in the requested format.
XLSX_OR_CSV_FILE_200: Final = _file_200("Excel workbook or CSV file", XLSX_MIME, "text/csv")
#: Stored attachment bytes (served with the file's own safe content type).
ATTACHMENT_FILE_200: Final = _file_200("The attachment's content", "application/octet-stream")
#: Unexpected server-side failure (downstream/infrastructure).
SERVER_ERROR_500: Final = _doc(500)
#: Export failures: bad report request, oversize export, workbook build failure.
EXPORT_ERRORS: Final = _doc(400, 413, 500)
#: Malformed request + caller lacks the required role.
BAD_REQUEST_FORBIDDEN_403: Final = _doc(400, 403)
#: Malformed request + caller lacks the required role + entity not visible.
BAD_REQUEST_FORBIDDEN_NOT_FOUND_404: Final = _doc(400, 403, 404)
#: Malformed request + forbidden grant + duplicate + entity not visible (user update).
BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404: Final = _doc(400, 403, 404, 409)
#: Config/version conflict + missing project work-item/repository + bad branch input.
GIT_CONFIG_CONFLICT_NOT_FOUND_VALIDATION_422: Final = _doc(404, 409, 422)
#: Removal guards: forbidden toggle + missing record + version conflict + bad ref.
GIT_REMOVE_FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_422: Final = _doc(403, 404, 409, 422)
#: Branch create/list/preview/discovery: missing scope + conflict + bad input.
GIT_BRANCH_CONFLICT_NOT_FOUND_VALIDATION_422: Final = _doc(404, 409, 422)

# --- Additional endpoint combinations (Sonar S8415) ------------------------
#: Codes an endpoint family raises: 400, 403, 404, 413, 429.
BAD_REQUEST_FORBIDDEN_NOT_FOUND_PAYLOAD_TOO_LARGE_429: Final = _doc(400, 403, 404, 413, 429)
#: Codes an endpoint family raises: 400, 403, 404, 422.
BAD_REQUEST_FORBIDDEN_NOT_FOUND_422: Final = _doc(400, 403, 404, 422)
#: Codes an endpoint family raises: 400, 403, 422.
BAD_REQUEST_FORBIDDEN_422: Final = _doc(400, 403, 422)
#: Codes an endpoint family raises: 400, 404, 409, 422.
BAD_REQUEST_NOT_FOUND_CONFLICT_422: Final = _doc(400, 404, 409, 422)
#: Codes an endpoint family raises: 400, 413.
BAD_REQUEST_413: Final = _doc(400, 413)
#: Codes an endpoint family raises: 400, 422.
BAD_REQUEST_422: Final = _doc(400, 422)
#: Codes an endpoint family raises: 403, 404, 409, 422, 503.
FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_503: Final = _doc(403, 404, 409, 422, 503)
#: Codes an endpoint family raises: 403, 404, 429.
FORBIDDEN_NOT_FOUND_429: Final = _doc(403, 404, 429)
#: Codes an endpoint family raises: 404, 500.
NOT_FOUND_500: Final = _doc(404, 500)
