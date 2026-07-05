"""HTTP client for the ComfyUI-Nukomfy-Suite custom node.

Wraps the /nukomfy/* surface with a tiny per-URL TTL cache so probing every
machine on every refresh does not flood the network. Uses stdlib urllib only,
matching the zero-dependency policy of comfy_api.py.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from logging import getLogger
from typing import Any, Literal

from Nukomfy.core.identity import header_user

_log = getLogger(__name__)

_TIMEOUT = 5.0
_POST_TIMEOUT = 10.0
_PING_TTL = 60.0  # seconds
_RATE_LIMIT_FALLBACK_SECONDS = 300  # default cooldown when a 429 omits retry_after
_UA = "Nukomfy"

# {url: (timestamp, payload_or_None)}
_ping_cache: dict[str, tuple[float, dict | None]] = {}


def _ping_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/nukomfy/manager/ping"


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "X-Nukomfy-User": header_user(),
    }


def _http_get_json(url: str, timeout: float = _TIMEOUT) -> dict | None:
    req = urllib.request.Request(url, headers=_default_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                resp.read()
                return None
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Drain the body before discarding: an unread response makes urllib
        # close the socket with a TCP RST, which ComfyUI logs as
        # "_call_connection_lost ... WinError 10054" on every poll. A 404 here
        # is the normal case when the Suite is not installed on a machine.
        try:
            e.read()
        except Exception:
            pass
        return None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            UnicodeError):
        # UnicodeError backstops a non-latin-1 header value slipping past
        # header_user(): fail the request gracefully instead of crashing.
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _http_post_json(
    url: str,
    body: dict,
    timeout: float = _POST_TIMEOUT,
) -> tuple[int, dict | None]:
    """POST a JSON body. Returns (status_code, parsed_response_or_None).

    On network failure returns (0, None) so the caller can distinguish a
    connection error from any HTTP status the server returned.
    """
    headers = _default_headers()
    headers["Content-Type"] = "application/json"
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:
            raw = b""
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            UnicodeError):
        # UnicodeError backstops a non-latin-1 header value slipping past
        # header_user(): fail the request gracefully instead of crashing.
        return 0, None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    return status, (data if isinstance(data, dict) else None)


def ping(base_url: str, *, force: bool = False) -> dict | None:
    """Probe /nukomfy/manager/ping with a 60 s in-process cache.

    Returns the parsed payload or None when the custom node is not
    installed / not reachable. Payload shape::

        {
            "version": str,
            "has_password": bool,
            "password_changed_at": str,           # ISO-8601 or ""
            "availability": "available" | "unavailable",
            "has_persistent_history": bool,       # capability flag
            "history_schema_version": int,        # DB schema version
            "history_enabled": bool,              # config flag (live)
        }

    The last three fields are capability flags the manager omits when it
    does not expose persistent history; callers treat them as missing.
    """
    if not base_url:
        return None
    now = time.monotonic()
    cached = _ping_cache.get(base_url)
    if cached and not force and (now - cached[0]) < _PING_TTL:
        return cached[1]
    payload = _http_get_json(_ping_url(base_url))
    _ping_cache[base_url] = (now, payload)
    return payload


def is_installed(base_url: str, *, force: bool = False) -> bool:
    """True iff a ping to base_url returns a valid Nukomfy Manager payload."""
    info = ping(base_url, force=force)
    return bool(info and isinstance(info.get("version"), str))


def has_password(base_url: str, *, force: bool = False) -> bool | None:
    """Whether the manager on base_url has an admin password configured.

    Returns None when the custom node is not installed (caller distinguishes
    the no-node case from the no-password case).
    """
    info = ping(base_url, force=force)
    if not info:
        return None
    return bool(info.get("has_password"))


def availability(base_url: str, *, force: bool = False) -> str | None:
    """Current Availability value (\"available\"/\"unavailable\") or None."""
    info = ping(base_url, force=force)
    if not info:
        return None
    value = info.get("availability")
    return value if value in ("available", "unavailable") else None


def rate_limit_status(base_url: str) -> tuple[bool, int]:
    """Whether the calling IP is currently rate-limited on base_url.

    Returns ``(blocked, retry_after_seconds)``. ``blocked`` is False when
    the endpoint is unreachable so the caller can fall back to the normal
    auth flow (server-side will still 429 if the limit fires anyway).
    """
    if not base_url:
        return False, 0
    url = f"{base_url.rstrip('/')}/nukomfy/admin/rate_limit_status"
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return False, 0
    blocked = bool(payload.get("rate_limited"))
    retry = payload.get("retry_after_seconds", 0)
    if not isinstance(retry, int) or retry < 0:
        retry = 0
    return blocked, retry


def clear_cache(base_url: str | None = None) -> None:
    """Invalidate the ping cache for one URL or all URLs."""
    if base_url is None:
        _ping_cache.clear()
    else:
        _ping_cache.pop(base_url, None)


AuthResult = Literal["ok", "wrong", "rate_limited", "no_password", "error"]


def auth(base_url: str, password: str) -> tuple[AuthResult, int]:
    """Verify an admin password against the manager on base_url.

    Returns ``(result, retry_after_seconds)``. ``retry_after_seconds`` is 0
    unless ``result == "rate_limited"``, in which case it carries the
    server-reported cooldown so the UI can surface a countdown.

    Result codes:
      - "ok": password accepted
      - "wrong": 401 wrong password
      - "rate_limited": 429 too many failed attempts from this IP
      - "no_password": 400 manager has no password configured yet
      - "error": network failure or unexpected response
    """
    if not base_url or not isinstance(password, str):
        return "error", 0
    url = f"{base_url.rstrip('/')}/nukomfy/admin/auth"
    status, body = _http_post_json(url, {"password": password})
    if status == 0:
        return "error", 0
    if status == 200 and body and body.get("ok"):
        return "ok", 0
    if status == 401:
        return "wrong", 0
    if status == 429:
        retry = _RATE_LIMIT_FALLBACK_SECONDS
        if isinstance(body, dict):
            value = body.get("retry_after_seconds")
            if isinstance(value, int) and value > 0:
                retry = value
        return "rate_limited", retry
    if status == 400 and body and body.get("reason") == "no_password_set":
        return "no_password", 0
    return "error", 0


SetPasswordResult = Literal["ok", "wrong_current", "weak", "error"]


def set_password(
    base_url: str,
    new_password: str,
    current_password: str | None = None,
) -> SetPasswordResult:
    """Set or change the admin password on the manager.

    Pass current_password=None for the first-time set (no password configured
    yet). Otherwise pass the current password for verification.
    """
    if not base_url or not isinstance(new_password, str):
        return "error"
    url = f"{base_url.rstrip('/')}/nukomfy/admin/set_password"
    body: dict[str, Any] = {"new": new_password}
    if current_password is not None:
        body["current"] = current_password
    status, resp = _http_post_json(url, body)
    if status == 0:
        return "error"
    if status == 200 and resp and resp.get("ok"):
        # The ping cache might still report has_password=false; invalidate it.
        clear_cache(base_url)
        return "ok"
    if status == 401:
        return "wrong_current"
    if status == 400 and resp and resp.get("reason") == "weak_password":
        return "weak"
    return "error"


ForceActionResult = Literal[
    "ok", "not_running", "wrong_password", "rate_limited", "no_password",
    "missing_prompt_id", "error",
]


def force_abort(
    base_url: str,
    password: str,
    prompt_id: str,
    affected_user: str | None = None,
    nfy_job_id: str | None = None,
) -> ForceActionResult:
    """Admin-gated force abort of a running job on base_url.

    Bypasses the client-side cross-user block by re-verifying the admin
    password server-side. Silent no-op if prompt_id is not currently running
    (mirrors ComfyUI's native /api/interrupt semantics).

    ``nfy_job_id`` is the short Base62 id Nukomfy assigns at submit time;
    when supplied it appears in the server activity log instead of the
    long ComfyUI prompt_id.
    """
    return _force_action(base_url, password, prompt_id, affected_user,
                         nfy_job_id, "/nukomfy/admin/force_abort")


def force_remove(
    base_url: str,
    password: str,
    prompt_id: str,
    affected_user: str | None = None,
    nfy_job_id: str | None = None,
) -> ForceActionResult:
    """Admin-gated force removal of a pending job from queue on base_url.

    Bypasses the client-side cross-user block. Silent no-op if prompt_id is
    not in the pending queue (mirrors /api/queue?delete=[...] semantics).

    See ``force_abort`` for the ``nfy_job_id`` parameter.
    """
    return _force_action(base_url, password, prompt_id, affected_user,
                         nfy_job_id, "/nukomfy/admin/force_remove")


def _force_action(
    base_url: str,
    password: str,
    prompt_id: str,
    affected_user: str | None,
    nfy_job_id: str | None,
    path: str,
) -> ForceActionResult:
    if not base_url or not isinstance(password, str) or not isinstance(prompt_id, str):
        return "error"
    url = f"{base_url.rstrip('/')}{path}"
    body: dict[str, Any] = {"password": password, "prompt_id": prompt_id}
    if affected_user:
        body["affected_user"] = affected_user
    if nfy_job_id:
        body["nfy_job_id"] = nfy_job_id
    status, resp = _http_post_json(url, body)
    if status == 0:
        return "error"
    if status == 200 and resp and resp.get("ok"):
        # force_abort reports whether it actually marked the running job in
        # `action_performed`: False means the prompt was not the running one
        # (or the in-process mark failed), so no abort took effect and the
        # optimistic "Aborting…" row must clear instead of staying armed.
        # force_remove omits the field, so an absent value keeps "ok".
        if resp.get("action_performed") is False:
            return "not_running"
        return "ok"
    if status == 401:
        return "wrong_password"
    if status == 429:
        return "rate_limited"
    if status == 400 and isinstance(resp, dict):
        reason = resp.get("reason")
        if reason == "no_password_set":
            return "no_password"
        if reason == "missing_prompt_id":
            return "missing_prompt_id"
    return "error"


RebootResult = Literal["ok", "wrong_password", "rate_limited", "no_password", "error"]


def reboot(base_url: str, password: str) -> RebootResult:
    """Trigger an admin-gated reboot on the target ComfyUI machine.

    The server returns the auth/dispatch result immediately, then schedules
    the actual restart after the response is flushed. The caller is
    responsible for polling availability afterwards.
    """
    if not base_url or not isinstance(password, str):
        return "error"
    url = f"{base_url.rstrip('/')}/nukomfy/admin/reboot"
    status, body = _http_post_json(url, {"password": password})
    if status == 0:
        return "error"
    if status == 200 and body and body.get("ok"):
        return "ok"
    if status == 401:
        return "wrong_password"
    if status == 429:
        return "rate_limited"
    if status == 400 and body and body.get("reason") == "no_password_set":
        return "no_password"
    return "error"


def has_persistent_history(base_url: str, *, force: bool = False) -> bool:
    """Whether base_url advertises the persistent history capability.

    Derived from the ping payload (no separate cache - the ping wrapper
    already memoises the result for 60 s). Returns False when the manager
    is not installed or pre-dates the capability bump.
    """
    payload = ping(base_url, force=force)
    return bool(payload and payload.get("has_persistent_history"))


def get_persistent_history(
    base_url: str,
    *,
    limit: int | None = None,
    offset: int = 0,
    since_iso: str | None = None,
    statuses: list[str] | None = None,
    query: str | None = None,
) -> dict | None:
    """GET /nukomfy/jobs/history (no auth).

    Returns the parsed ``{"ok": True, "entries": [...]}`` payload or None
    on any network/parse error. Entries carry the same shape as a row
    produced by Nukomfy/data/db.py:get_history() so the store can merge
    them with locally-persisted entries by ``prompt_id``. ``statuses`` and
    ``query`` map to the server-side status filter and text search.
    """
    if not base_url:
        return None
    params: dict[str, str] = {}
    if limit is not None:
        params["limit"] = str(limit)
    if offset:
        params["offset"] = str(offset)
    if since_iso:
        params["since"] = since_iso
    if statuses:
        params["status"] = ",".join(statuses)
    if query:
        params["q"] = query
    url = f"{base_url.rstrip('/')}/nukomfy/jobs/history"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return _http_get_json(url)


def get_persistent_history_one(base_url: str, prompt_id: str) -> dict | None:
    """GET /nukomfy/jobs/history/{prompt_id} (no auth).

    Returns the parsed ``{"ok": True, "entry": {...}}`` payload, or None on
    404 / network / parse error. The entry has the same shape as a single
    element of ``get_persistent_history``'s ``entries`` list, so callers can
    feed it through the identical persistent-terminal merge path.
    """
    if not base_url or not prompt_id:
        return None
    quoted = urllib.parse.quote(str(prompt_id), safe="")
    url = f"{base_url.rstrip('/')}/nukomfy/jobs/history/{quoted}"
    return _http_get_json(url)


def get_persistent_workflow_api(base_url: str, prompt_id: str) -> dict | None:
    """GET /nukomfy/jobs/history/{prompt_id}/workflow_api (no auth).

    Returns the workflow API graph dict persisted server-side at submit,
    or None on 404 / network / parse error / empty payload. Fallback source
    for the Job dialog's Workflow views when ComfyUI's native
    ``/api/jobs/{prompt_id}`` no longer has the job - the native history
    does not survive a server restart, the persistent one does.
    """
    if not base_url or not prompt_id:
        return None
    quoted = urllib.parse.quote(str(prompt_id), safe="")
    url = f"{base_url.rstrip('/')}/nukomfy/jobs/history/{quoted}/workflow_api"
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return None
    wf = payload.get("workflow_api")
    return wf if isinstance(wf, dict) and wf else None
