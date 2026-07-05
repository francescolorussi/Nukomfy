"""Helpers for log message formatting: job/prompt id + machine reference."""

import uuid


def _is_prompt_id(s):
    # A ComfyUI prompt_id is a UUID - either the canonical hyphenated form
    # (ComfyUI native / current submits) or the bare 32-hex form (older
    # submits). An nfy_job_id is a short Base62 handle, never UUID-parseable.
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


def fmt_job(value):
    """Render a job reference for log messages.

    Returns `job <nfy_job_id>` whenever the Nukomfy job id is known,
    falling back to `prompt <full_prompt_id>` (untruncated) only when
    the entry is not in local history (e.g. action via admin gate on
    another user's job, or history cleared before a late server event).

    Accepts:
      - history entry dict - reads `nfy_job_id` then `prompt_id`;
      - UUID prompt_id string (hyphenated or bare hex) - looks up
        `nfy_job_id` in DB, falls back to the full prompt_id;
      - any other string - treated as `nfy_job_id`.
    """
    if isinstance(value, dict):
        jid = (value.get('nfy_job_id') or '').strip()
        if jid:
            return 'job ' + jid
        pid = (value.get('prompt_id') or '').strip()
        if pid:
            return _resolve_prompt_id(pid)
        return 'job ?'

    s = str(value or '').strip()
    if not s:
        return 'job ?'
    if _is_prompt_id(s):
        return _resolve_prompt_id(s)
    return 'job ' + s


def _resolve_prompt_id(prompt_id):
    try:
        from Nukomfy.data.db import find_nfy_job_id
        jid = find_nfy_job_id(prompt_id)
    except Exception:
        jid = ''
    return 'job ' + jid if jid else 'prompt ' + prompt_id


def fmt_machine(url, name=None):
    """Render a machine reference as `<name>` for log messages.

    Accepts the machine base URL or a full URL that starts with one
    (e.g. `http://host:port/api/queue` matches the `http://host:port`
    machine). When `name` is provided, an exact `(name + url)` match
    is preferred so callers that know which machine they are talking
    to get the right name even when several machines share the same
    URL. Falls back to the passed `name`, then to a privacy-biased
    URL-only lookup (hidden entries win ties so a co-tenant visible
    machine cannot leak through a log message), then to `?`.

    Never returns the URL itself; this keeps URLs out of logs and
    keeps screenshots/bug reports free of endpoint info.
    """
    if not url:
        return name or '?'
    url = str(url).rstrip('/')
    try:
        from Nukomfy.client.machines import machine_manager
        machines = list(machine_manager.machines)
    except Exception:
        machines = []

    def _url_matches(m):
        base = (m.url or '').rstrip('/')
        return bool(base) and (base == url or url.startswith(base + '/'))

    # 1. Caller-provided name + url exact match (preferred when known).
    if name:
        for m in machines:
            if (m.name or '') == name and _url_matches(m):
                return m.name or '?'
        # The caller already knows which machine this is; trust them
        # over the URL-only fallback (which could pick a sibling).
        return name

    # 2. URL-only scan. Hidden entries win so a visible co-tenant
    # cannot be leaked through a log message that only has the URL.
    url_matches = [m for m in machines if _url_matches(m)]
    hidden = next((m for m in url_matches if m.hidden_url), None)
    if hidden is not None:
        return hidden.name or '?'
    if url_matches:
        return url_matches[0].name or '?'
    return '?'
