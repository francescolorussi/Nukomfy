"""Canonical path resolution for UI preview and runtime.

Single source of truth for "what path will the system actually use".
Used by Settings preview fields, dialogs that cite paths, and runtime
call sites that resolve user-typed path settings.

Chain (fixed order):
    raw -> resolve_path (Nuke TCL subst) -> abspath -> normpath

Does NOT expand `~` or environment variables ($VAR / %VAR%) outside TCL.
Users who need home/env access write `[getenv HOME]/foo` (Tcl syntax),
which `resolve_path` already evaluates.

Never touches the filesystem (no `os.path.exists`, no drive validation),
so it's safe to call on every `textChanged` signal in preview labels.
"""

import os

from Nukomfy.core.settings import resolve_path


def canonical_path(raw):
    """Normalise *raw* into the path the system will actually use.

    Returns a ``(ok, value, reason)`` triple:

    - ``(True, normalised_path, '')`` on success.
    - ``(False, raw, 'Empty input')`` when raw is blank.
    - ``(False, raw, 'Invalid TCL expression')`` when raw has
      ``[...]`` / ``$...`` that did not substitute (or substituted to
      empty).
    - ``(False, raw, 'Path is not absolute')`` when the resolved path
      is relative (``foo/bar``, ``~/x``, ``./y``).
    - ``(False, raw, 'Path normalisation failed')`` on the rare
      ``ValueError``/``TypeError`` from ``abspath``/``normpath``
      (malformed string).

    On success the second element is the path after
    ``resolve_path -> abspath -> normpath``. Drive-relative paths like
    ``\\foo`` (Windows) are absolutised against the current drive,
    ``..`` segments are collapsed, separators normalised.
    """
    raw = (raw or '').strip()
    if not raw:
        return (False, raw, 'Empty input')

    if '[' in raw or '$' in raw:
        resolved = resolve_path(raw)
        # resolve_path normalises `\` -> `/` before tcl subst; compare in
        # the normalised form so a backslash-only diff doesn't mask a
        # TCL evaluation failure.
        if not resolved or resolved == raw.replace('\\', '/'):
            return (False, raw, 'Invalid TCL expression')
    else:
        resolved = raw

    if not os.path.isabs(resolved):
        return (False, raw, 'Path is not absolute')

    try:
        return (True, os.path.normpath(os.path.abspath(resolved)), '')
    except (ValueError, TypeError):
        return (False, raw, 'Path normalisation failed')


def display_path(raw):
    """Return the path the system will use, in OS-native separators.

    Thin wrapper around :func:`canonical_path` for sites that just want
    a string to show. On error returns the raw input unchanged - the
    caller is expected to also call ``canonical_path`` if it needs to
    distinguish valid from invalid.
    """
    ok, value, _reason = canonical_path(raw)
    if not ok:
        return value
    if os.sep == '/':
        return value
    return value.replace('/', os.sep)


def runtime_path(raw, fallback=None):
    """Return the canonical absolute path for runtime use.

    Same chain as :func:`canonical_path` but returns a single string,
    falling back to *fallback* (or to a best-effort
    ``abspath(normpath(resolve_path(raw)))`` of *raw* if no fallback)
    when validation fails. Used by runtime call sites that must
    produce *some* path even on malformed input - UI fields gate on
    ``canonical_path`` first, so by the time runtime sees the value
    it's already user-validated in the common case.

    Validation failures are intentionally silent: UI fields gate on
    ``canonical_path`` first (which surfaces the error inline as the
    user types), so the fallback is reached only for already-surfaced
    or non-UI input. Logging here would fire on every keystroke of an
    in-progress path edit.
    """
    ok, value, _reason = canonical_path(raw)
    if ok:
        return value
    if fallback is not None:
        return fallback
    # Last-resort best effort: try to normalise whatever we have so
    # downstream `fs_safe._is_safe_rmtree_target` still sees something
    # comparable to its own normalisation.
    raw = (raw or '').strip()
    if not raw:
        return raw
    try:
        candidate = resolve_path(raw) or raw
        return os.path.normpath(os.path.abspath(candidate))
    except (ValueError, TypeError):
        return raw
