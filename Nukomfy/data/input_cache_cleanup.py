"""Scanning and TTL/full purge of input cache directories.

Ownership is established by the presence (and schema validity) of the
`.nukomfy_input_cache.json` sentinel file inside a leaf directory. We
never delete a folder that doesn't carry this sentinel, so unrelated
user folders that happen to live under the same base path are safe.

Layout (user-first, 6 levels under base):
    {base}/{user}/{project}/{workflow}/{input}/{cache_key}/.nukomfy_input_cache.json

Defenses against touching something that isn't ours:
  1. `os.walk` skips symlinks (followlinks=False) - symlinked subdirs
     can't escape the walk into an unrelated tree.
  2. Each candidate leaf must contain `.nukomfy_input_cache.json`.
  3. The sentinel must be valid JSON AND match a recognised schema (the
     current one, or a deletable legacy schema for delete-only)
     AND carry all required fields AND pass path-binding (the sentinel's
     `path` field must resolve, after cross-OS path-substitution, to the
     directory the sentinel actually lives in).
  4. Every rmtree re-checks `_is_ours` immediately before deletion to
     close the TOCTOU gap between scan and delete.
  5. `_is_safe_rmtree_target` rejects catastrophic roots before any walk.

This module is deletion-only, so it recognises the current schema plus
deletable legacy schemas - caches from a previous plugin version stay
reclaimable instead of leaking on disk. No migration shim.

Age is read from `.nukomfy_input_cache.json:last_used_utc` (ISO8601 UTC).
JSON is the authoritative source - filesystem-independent (unlike mtime
which gets clobbered by copy/sync tools and SMB/NFS clock remapping).
"""

import datetime
import logging
import os

from Nukomfy.core.identity import current_user

_log = logging.getLogger(__name__)

_STATE_FILENAME = '.nukomfy_input_cache.json'

# Maximum depth to walk under base looking for sentinels. Must cover
# the longest expected layout (currently 6: user/project/workflow/input/key).
# Beyond this we abort to avoid runaway descent into user data.
_MAX_WALK_DEPTH = 8


def _load_state(state_path):
    import Nukomfy.utils.fs_safe as fs_safe
    dir_path = os.path.dirname(state_path)
    # Deletion-only module: tolerate older schemas so cleanup / Clear All can
    # still find and reclaim caches written by a previous plugin version.
    return fs_safe._load_sentinel(
        dir_path,
        fs_safe.SENTINEL_INPUT_CACHE,
        fs_safe._REQUIRED_INPUT_CACHE,
        fs_safe._DELETABLE_INPUT_CACHE_SCHEMAS)


def _is_ours(dir_path):
    """True iff *dir_path* carries a nukomfy input-cache sentinel (current or a
    deletable legacy schema) with matching path-binding. Deletion-only check."""
    import Nukomfy.utils.fs_safe as fs_safe
    return fs_safe._load_sentinel(
        dir_path,
        fs_safe.SENTINEL_INPUT_CACHE,
        fs_safe._REQUIRED_INPUT_CACHE,
        fs_safe._DELETABLE_INPUT_CACHE_SCHEMAS) is not None


def _parse_last_used(state_data):
    """Convert state dict's last_used_utc ISO string to UTC-aware datetime."""
    iso = state_data.get('last_used_utc')
    if not iso:
        return None
    try:
        s = iso.replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _dir_size(path):
    """Total bytes of files under *path*. Best-effort, no symlinks."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _safe_user():
    """Sanitized current user (mirrors input_cache_writer)."""
    u = current_user()
    return ''.join(c if c.isalnum() or c in '_-' else '_' for c in u)


def user_scope_root(base):
    """Absolute path of the current-user cache subtree that default-scope purges touch."""
    return os.path.join(os.path.abspath(base), _safe_user())


def _scan_walk(base, max_depth=_MAX_WALK_DEPTH):
    """Yield (sentinel_dir, depth) for every dir under *base* that contains
    a `.nukomfy_input_cache.json` file, depth-limited to *max_depth* levels.

    Symlinks are not followed. Pruning: once we hit a sentinel at depth N
    we don't descend further into that branch (sentinels mark leaves).
    """
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return
    base_depth = base.count(os.sep)
    for root, dirs, files in os.walk(base, followlinks=False):
        cur_depth = root.count(os.sep) - base_depth
        if cur_depth > max_depth:
            dirs[:] = []
            continue
        if _STATE_FILENAME in files:
            yield root, cur_depth
            dirs[:] = []  # Don't descend into a leaf
            continue


def scan(base, scope_user=None):
    """Enumerate input-cache leaf dirs under *base*.

    By default only walks `{base}/{user}/...` for the current OS user
    (user-first isolation). Pass `scope_user='all'` to scan every user
    branch (used by admin-style cleanup, not exposed in UI).

    Returns list of dicts: path, size_bytes, last_used_utc, project,
    scope, input_name, fp_segment.
    """
    import Nukomfy.utils.fs_safe as fs_safe
    if not fs_safe._is_safe_rmtree_target(base):
        _log.warning('input_cache scan refused unsafe base: %s', base)
        return []

    base_abs = os.path.abspath(base)
    if scope_user == 'all':
        roots = [base_abs]
    else:
        # Default: scope to current user
        user = _safe_user()
        roots = [os.path.join(base_abs, user)]

    results = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for leaf, depth in _scan_walk(root):
            state = _load_state(os.path.join(leaf, _STATE_FILENAME))
            if state is None:
                continue
            # Decompose path into segments relative to base for diagnostics
            rel = os.path.relpath(leaf, base_abs).replace('\\', '/')
            parts = rel.split('/')
            # Expected layout: user/project/workflow/input/cache_key -> 5 parts
            if len(parts) >= 5:
                user_seg, project, scope, input_name, fp_segment = parts[-5:]
            else:
                user_seg = parts[0] if parts else ''
                project = ''
                scope = ''
                input_name = ''
                fp_segment = parts[-1] if parts else ''
            results.append({
                'path': leaf.replace('\\', '/'),
                'size_bytes': _dir_size(leaf),
                'last_used_utc': _parse_last_used(state),
                'user': user_seg,
                'project': project,
                'scope': scope,
                'input_name': input_name,
                'fp_segment': fp_segment,
            })
    return results


def _prune_empty_ancestors(base, leaf_path):
    """After deleting a leaf, walk up removing any ancestor dirs that
    became empty. Stops at *base* (never removes base itself).
    """
    cur = os.path.dirname(leaf_path)
    base_abs = os.path.abspath(base)
    while True:
        cur_abs = os.path.abspath(cur)
        if cur_abs == base_abs or len(cur_abs) <= len(base_abs):
            break
        if not os.path.isdir(cur) or os.path.islink(cur):
            break
        try:
            if os.listdir(cur):
                break
            os.rmdir(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)


def purge_older_than(base, days, scope_user=None):
    """Delete every input-cache leaf whose last_used_utc is older than *days*.

    Entries with missing/unparseable last_used_utc are SKIPPED (conservative).

    By default only purges current-user leaves. Pass `scope_user='all'`
    for all users.

    Returns (n_deleted, bytes_freed).
    """
    if days <= 0:
        return (0, 0)

    import Nukomfy.utils.fs_safe as fs_safe
    cutoff = datetime.datetime.now(datetime.timezone.utc) \
        - datetime.timedelta(days=days)

    n_deleted = 0
    bytes_freed = 0
    for entry in scan(base, scope_user=scope_user):
        last_used = entry['last_used_utc']
        if last_used is None or last_used >= cutoff:
            continue
        path = entry['path']
        if not _is_ours(path):
            continue
        size = entry['size_bytes']
        if fs_safe.selective_delete_dir(
                path, action='input cache TTL purge',
                sentinel_kind='input_cache'):
            n_deleted += 1
            bytes_freed += size
            _prune_empty_ancestors(base, path)
        else:
            _log.warning('Input cache purge failed for %s', path)

    return (n_deleted, bytes_freed)


def purge_all(base, scope_user=None):
    """Delete every input-cache leaf identified as ours under *base*.

    Default scope: current user only (Clear My Input Cache).
    Pass `scope_user='all'` for everything.

    Returns (n_deleted, bytes_freed).
    """
    import Nukomfy.utils.fs_safe as fs_safe
    n_deleted = 0
    bytes_freed = 0
    for entry in scan(base, scope_user=scope_user):
        path = entry['path']
        if not _is_ours(path):
            continue
        size = entry['size_bytes']
        if fs_safe.selective_delete_dir(
                path, action='input cache full wipe',
                sentinel_kind='input_cache'):
            n_deleted += 1
            bytes_freed += size
            _prune_empty_ancestors(base, path)
        else:
            _log.warning('Input cache purge failed for %s', path)

    return (n_deleted, bytes_freed)
