"""Filesystem helpers with user-visible error reporting.

Two surfaces:

- User-action helper (makedirs) pops a Qt dialog on
  failure that names the operation, the path, the OS error, and the
  likely causes (permissions, drive unreachable, file open elsewhere,
  disk full). It returns True on success and False on failure so the
  caller can short-circuit the user's flow cleanly.

- Silent helpers (*_silent) only log. Use these for bookkeeping writes
  the user never explicitly triggered (app config, favourites, history,
  UI state). They still return True/False so callers can skip the
  subsequent `open()` when the directory wasn't created.

The popup uses QtWidgets.QMessageBox so it inherits the parent window's
modality. Pass the calling widget as `parent` - fall back to None for
non-GUI contexts (the dialog becomes a standalone top-level window).

Sentinel-gated safe_delete helpers
==================================

`safe_delete_dir` is the uniform wrapper for any directory deletion
that targets user-configured paths. It enforces, in order:

  1. `_is_safe_rmtree_target` (root/home/system gate).
  2. Reject symlinks at the directory itself.
  3. Sentinel ownership check: the directory MUST contain a
     Nukomfy-written sentinel (`.nukomfy_input_cache.json` for input cache or
     `.nukomfy_output.json` for output) whose internal `path` field
     matches the directory's actual canonical location. A sentinel
     copied/moved into an unrelated folder fails the path-binding and
     the delete is refused.
  4. `shutil.rmtree`.

Callers that don't have a sentinel context get a graceful skip +
warning - never a delete.
"""

import json
import logging
import os
import re
import shutil
import stat
import time

from Nukomfy.workflows.workflow_loader import WORKFLOW_JSON, METADATA_JSON

_log = logging.getLogger(__name__)

# Sentinel filenames recognised as Nukomfy ownership claims.
SENTINEL_INPUT_CACHE = '.nukomfy_input_cache.json'
SENTINEL_OUTPUT      = '.nukomfy_output.json'

# Schema discriminators (the `schema` field inside the sentinel JSON).
# Input cache: reuse requires the current schema exactly. Deletion tolerates
# older schemas too (see _DELETABLE_INPUT_CACHE_SCHEMAS) so cleanup / Clear All
# can still reclaim caches written by a previous version - no migration shim.
_SCHEMA_INPUT_CACHE = 'nukomfy.cache.v5'
_SCHEMA_OUTPUT      = 'nukomfy.output.v1'

# Schemas whose folders may still be DELETED (never reused) by cleanup/purge,
# so obsolete-version caches stay reclaimable instead of leaking on disk.
_DELETABLE_INPUT_CACHE_SCHEMAS = frozenset(
    ('nukomfy.cache.v5', 'nukomfy.cache.v4', 'nukomfy.cache.v3'))

# Required fields per schema. The `path` field is what binds a sentinel
# to its physical location - without it a moved sentinel would falsely
# vouch for any folder it lands in.
# Input cache v5: `fingerprint` is the 16-hex frame-independent struct key
# (names the folder). `frames_state` maps each frame to its content op hash +
# per-source mtime/size (all Read sources in the chain) for surgical re-render.
# `algo`/`algo_version`/`nuke_version` document the hashing for diagnostics.
# `writing`/`in_use` cover ownership and concurrency.
_REQUIRED_INPUT_CACHE = frozenset((
    'schema', 'algo', 'algo_version', 'nuke_version',
    'path', 'path_os', 'fingerprint', 'frames_state',
    'file_pattern', 'created_utc', 'last_used_utc', 'writing', 'in_use'))
_REQUIRED_OUTPUT      = frozenset((
    'schema', 'path', 'path_os', 'created_utc', 'file_patterns'))


def _strip_long_path_for_display(s):
    """Remove the Windows long-path adapter prefix (\\\\?\\ and the UNC
    variant \\\\?\\UNC\\) from a string so user-facing popups don't show
    the internal syscall form. Handles both raw-form and the
    double-escaped form that appears when an OSError __str__ embeds
    repr() of a path. POSIX inputs pass through untouched.
    """
    if not s or not isinstance(s, str):
        return s
    # Order matters: strip the UNC variants before the bare prefix so
    # the share root is preserved as \\server\share.
    repls = (
        ('\\\\\\\\?\\\\UNC\\\\', '\\\\\\\\'),  # OSError repr-escaped UNC
        ('\\\\\\\\?\\\\', ''),                  # OSError repr-escaped bare
        ('\\\\?\\UNC\\', '\\\\'),               # raw UNC
        ('\\\\?\\', ''),                        # raw bare
        ('//?/UNC/', '//'),                     # forward-slash UNC
        ('//?/', ''),                           # forward-slash bare
    )
    for old, new in repls:
        s = s.replace(old, new)
    return s


def _format_body(verb, path, exc):
    return (
        '{} failed:\n{}\n\n'
        'Error: {}'
    ).format(verb,
             _strip_long_path_for_display(path),
             _strip_long_path_for_display(str(exc)))


def to_short_path_win(path):
    """Return the Windows 8.3 short form of *path* (e.g. ``TEST_L~2``).
    POSIX passthrough.

    Some consumers (notably Nuke's Read/Write nodes) don't support
    paths over the legacy MAX_PATH (260) limit - the long-path adapter
    ``\\\\?\\`` only works for direct WinAPI file handles, not for
    apps that open the path through their own framework. The 8.3 short
    form sidesteps the limit by aliasing each path component to a
    sub-260 alias that NTFS resolves natively.

    ``GetShortPathNameW`` only works on paths that actually exist, so
    when *path* contains a frame placeholder (``%04d``, ``####``) we
    shorten only the dirname (which exists) and re-attach the basename
    untouched. This keeps the implementation usable for both concrete
    files and frame-range patterns.

    Returns the input unchanged on POSIX, on shortening failure, or
    if 8.3 generation is disabled on the volume (``fsutil 8dot3name``).
    The short form is a per-volume alias and NOT a canonical identifier
    - callers must keep using the long path for everything except the
    final hand-off to Nuke knobs / external tools that share Nuke's
    limitation.
    """
    if os.name != 'nt' or not path or not isinstance(path, str):
        return path

    # Strip the long-path prefix if the caller already added it
    # (gizmo runtime_path resolution does). Done before the length
    # gate so short paths that happen to carry the prefix come out
    # clean too - Nuke's Read knob doesn't like `//?/` literals.
    # Order matters: UNC variants must match before the bare prefix
    # so the share root is preserved rather than truncated.
    if path.startswith('\\\\?\\UNC\\'):
        path = '\\\\' + path[8:]
    elif path.startswith('//?/UNC/'):
        path = '//' + path[8:]
    elif path.startswith('\\\\?\\'):
        path = path[4:]
    elif path.startswith('//?/'):
        path = path[4:]

    # Only shorten when the path actually needs it. Sub-MAX_PATH paths
    # work natively in Nuke and Explorer; rewriting them to 8.3 form
    # is gratuitous noise (e.g. `Untitled` -> `Untitled` is stable but
    # `zzz_thisismyoutput2` -> `ZZZ_TH~1` is ugly with no benefit).
    # Threshold sits a few chars below MAX_PATH so we still trigger
    # before consumers actually trip the limit.
    if len(path) <= 248:
        return path
    try:
        import ctypes
        from ctypes import wintypes

        # Normalise to backslashes so split semantics are unambiguous
        # and UNC roots are handled by os.path. Caller's path may use
        # forward slashes (Nuke convention).
        norm = os.path.normpath(path)
        drive, rest = os.path.splitdrive(norm)
        rest = rest.lstrip(os.sep)
        if not rest:
            return path
        parts = rest.split(os.sep)

        # WIN32_FIND_DATAW is the only structure we need.
        MAX_PATH = 260
        class WIN32_FIND_DATAW(ctypes.Structure):
            _fields_ = [
                ('dwFileAttributes', wintypes.DWORD),
                ('ftCreationTime', wintypes.FILETIME),
                ('ftLastAccessTime', wintypes.FILETIME),
                ('ftLastWriteTime', wintypes.FILETIME),
                ('nFileSizeHigh', wintypes.DWORD),
                ('nFileSizeLow', wintypes.DWORD),
                ('dwReserved0', wintypes.DWORD),
                ('dwReserved1', wintypes.DWORD),
                ('cFileName', wintypes.WCHAR * MAX_PATH),
                ('cAlternateFileName', wintypes.WCHAR * 14),
            ]
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        FindFirstFileW = k32.FindFirstFileW
        FindFirstFileW.argtypes = [wintypes.LPCWSTR,
                                   ctypes.POINTER(WIN32_FIND_DATAW)]
        FindFirstFileW.restype = wintypes.HANDLE
        FindClose = k32.FindClose
        FindClose.argtypes = [wintypes.HANDLE]
        FindClose.restype = wintypes.BOOL

        def _alt_name(full_query):
            data = WIN32_FIND_DATAW()
            h = FindFirstFileW(full_query, ctypes.byref(data))
            if h == INVALID_HANDLE_VALUE or h == 0:
                return None
            try:
                alt = data.cAlternateFileName
                # cAlternateFileName is empty when the component is
                # already 8.3-conformant - caller falls back to cFileName.
                return alt if alt else data.cFileName
            finally:
                FindClose(h)

        # Walk components, accumulating a short path. Each per-step
        # query stays well under MAX_PATH because the resolved prefix
        # shrinks as we go. Concrete drive root is the start.
        cur = drive + os.sep
        out = drive
        for comp in parts:
            query = os.path.join(cur, comp)
            short = _alt_name(query)
            if short is None:
                # Component doesn't exist (frame placeholder, missing
                # file) - keep the original name.
                short = comp
            out = out + os.sep + short
            cur = os.path.join(cur, comp)
        return out
    except Exception as e:
        _log.warning('to_short_path_win: exception for %s: %s', path, e)
        return path


def _long_path(path):
    """Windows long-path adapter: prefix ``\\\\?\\`` so syscalls work past
    the legacy MAX_PATH (260) limit. POSIX passthrough.

    The prefix disables Windows path normalisation, so ``path`` MUST be
    fully resolved upstream - we run ``abspath+normpath`` here to be
    safe. Idempotent: pre-prefixed paths are returned unchanged. Returns
    the input untouched on empty/non-string/exception so callers can
    rely on the result being a passable path.
    """
    if os.name != 'nt':
        return path
    if not path or not isinstance(path, str):
        return path
    # Already prefixed in any of the 4 accepted forms - leave it.
    # Without these guards, a forward-slash prefix would slip past the
    # check and the ``\\\\`` test below would mis-classify it as UNC,
    # producing a malformed double-prefix like ``\\\\?\\UNC\\?\\C:\\…``.
    if (path.startswith('\\\\?\\')
            or path.startswith('//?/')):
        return path
    try:
        absp = os.path.abspath(os.path.normpath(path))
    except (ValueError, TypeError):
        return path
    # `normpath` may have produced a prefixed path if the input started
    # with `//?/` and got folded to `\\?\`. Re-check after normalisation.
    if absp.startswith('\\\\?\\'):
        return absp
    if absp.startswith('\\\\'):
        return '\\\\?\\UNC\\' + absp[2:]
    return '\\\\?\\' + absp


def _show_popup(parent, title_prefix, action, verb, path, exc):
    title = title_prefix
    if action:
        title = '{} ({})'.format(title_prefix, action)
    try:
        from Nukomfy.gui import _dialogs
        _dialogs.critical(parent, title, _format_body(verb, path, exc))
    except Exception:
        _log.exception('Failed to show fs_safe popup')


def _show_info_popup(parent, title_prefix, action, path, body):
    """Neutral (non-error) popup for a deliberate sentinel-gated refusal.

    A delete that declines because the ownership marker is absent is the
    safety feature working, not a failure - show it with the information
    icon and plain wording, never the '<verb> failed / Error:' critical
    template that _show_popup uses for real OSErrors.
    """
    title = title_prefix
    if action:
        title = '{} ({})'.format(title_prefix, action)
    text = '{}\n\n{}'.format(_strip_long_path_for_display(path), body)
    try:
        from Nukomfy.gui import _dialogs
        _dialogs.inform(parent, title, text)
    except Exception:
        _log.exception('Failed to show fs_safe info popup')


def makedirs(path, parent=None, action=None):
    """Create a directory tree. Show popup + log on failure."""
    try:
        os.makedirs(_long_path(path), exist_ok=True)
        return True
    except OSError as e:
        _log.exception('makedirs failed: %s', path)
        _show_popup(parent, 'Cannot create folder', action,
                    'Creating folder', path, e)
        return False


def dangerous_root_kind(norm):
    """Classify an already-normalised path as a catastrophic delete/cache root.

    Returns ``'drive'`` | ``'home'`` | ``'system'`` when *norm* is a
    filesystem/drive root, the user's home directory, or a known OS system
    root (exact match only - subdirs are the user's choice); otherwise None.

    *norm* must already be normalised by the caller (e.g. ``normpath`` +
    ``abspath``, or ``canonical_path``): this does identity comparisons only,
    it does not normalise. Single source of truth for the rmtree-safety rule
    shared by ``_is_safe_rmtree_target`` and the Settings cache-path gate.
    """
    # Windows paths are case-insensitive: compare in folded (lowercase)
    # form so case differences in user-supplied paths don't matter.
    norm_cmp = norm.lower() if os.name == 'nt' else norm
    # Filesystem root or drive root (path is its own parent).
    if os.path.dirname(norm) == norm:
        return 'drive'
    # The user's home directory itself.
    try:
        home = os.path.normpath(os.path.expanduser('~'))
        home_cmp = home.lower() if os.name == 'nt' else home
        if norm_cmp == home_cmp:
            return 'home'
    except Exception:
        pass
    # Known system roots (exact match only - subdirs are user's choice).
    dangerous = []
    if os.name == 'nt':
        for var in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)',
                    'ProgramData', 'WINDIR'):
            v = os.environ.get(var)
            if v:
                dangerous.append(os.path.normpath(v).lower())
    else:
        dangerous = ['/etc', '/usr', '/bin', '/sbin', '/lib', '/boot',
                     '/sys', '/proc', '/dev', '/var', '/opt']
    for d in dangerous:
        if norm_cmp == d:
            return 'system'
    return None


def _is_safe_rmtree_target(path):
    """Refuse delete on filesystem roots, home dir, and known system roots.

    Defence-in-depth guard: callers must still ensure their own ownership
    checks (sentinel files, prefix checks). This only rejects paths that
    are provably catastrophic regardless of intent.

    Match is by *identity* (exact normalised path), not by prefix - so
    user-chosen subdirs like ``/var/cache/nukomfy`` or ``D:\\cache`` pass.
    """
    try:
        norm = os.path.normpath(os.path.abspath(str(path)))
    except (ValueError, TypeError):
        return False
    return dangerous_root_kind(norm) is None


def makedirs_silent(path):
    """Create a directory tree. Log on failure, no popup. Returns bool."""
    try:
        os.makedirs(_long_path(path), exist_ok=True)
        return True
    except OSError:
        _log.exception('makedirs failed: %s', path)
        return False


def _on_rmtree_error(func, path, exc_info):
    """shutil.rmtree onerror handler: clear readonly bit and retry once.

    Windows: a readonly file (git checkout, AV quarantine, copy from CD)
    makes the unlink syscall fail with PermissionError. Clearing
    IWRITE|IWUSR via os.chmod and re-invoking the original func is the
    canonical recipe shown in Python's shutil.rmtree documentation.

    POSIX: re-raises the original exception. POSIX permits file unlink
    based on the directory's write bit, not the file's mode, so chmod
    here would not help.
    """
    if os.name != 'nt':
        if exc_info and exc_info[1]:
            raise exc_info[1]
        raise OSError('rmtree failed at {}'.format(path))
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IWUSR)
        func(path)
    except OSError:
        if exc_info and exc_info[1]:
            raise exc_info[1]
        raise


def _force_remove(path_io):
    """os.remove with a single readonly-clear retry on Windows.

    Mirrors _on_rmtree_error: a readonly file (AV quarantine, copy from
    read-only media) makes the unlink syscall fail with PermissionError
    on Windows; clearing the write bit and retrying is the canonical fix.
    POSIX unlink depends on the directory's write bit, not the file mode,
    so the retry there would not help - re-raise. Raises if it still
    fails so the caller can record a partial-failure error.
    """
    try:
        os.remove(path_io)
    except PermissionError:
        if os.name != 'nt':
            raise
        os.chmod(path_io, stat.S_IWRITE | stat.S_IWUSR)
        os.remove(path_io)


def atomic_replace(tmp_path, dst_path, retries=5, backoff_step=0.05):
    """Replace dst_path with tmp_path atomically, with retry on Windows.

    On POSIX os.replace is atomic at the kernel level and the loop exits
    on the first attempt. On Windows the destination can be transiently
    locked by AV scanners, OneDrive/Dropbox sync, or the search indexer;
    linear backoff (0.05s, 0.10s, ...) covers the typical contention
    window. Both arguments are wrapped with _long_path() so paths past
    the legacy MAX_PATH (260 chars) succeed.

    Returns True on success, False after retries are exhausted. The tmp
    file is removed on permanent failure so the caller's directory
    doesn't accumulate stale `.tmp` leftovers. Errors are logged; no
    exception is raised (callers needing raise wrap the return value).
    """
    tmp_io = _long_path(tmp_path)
    dst_io = _long_path(dst_path)
    for attempt in range(retries):
        try:
            os.replace(tmp_io, dst_io)
            return True
        except PermissionError as e:
            # Transient Windows lock (AV, sync, indexer): retry with linear
            # backoff, then give up after the last attempt.
            if attempt < retries - 1:
                time.sleep(backoff_step * (attempt + 1))
                continue
            _log.error('atomic_replace failed after %d retries: %s (%s)',
                       retries, dst_path, e)
        except OSError as e:
            # Any other OSError (cross-device, disk full, missing tmp) will
            # not clear on retry - fail fast. The docstring promises this
            # never raises, so log and fall through to the tmp cleanup.
            _log.error('atomic_replace failed: %s (%s)', dst_path, e)
        # Permanent failure: drop the tmp so callers don't accumulate stale
        # .tmp leftovers, then report False.
        try:
            os.remove(tmp_io)
        except OSError:
            pass
        return False
    return False


# ---------------------------------------------------------------------------
# Sentinel-based ownership for safe deletion
# ---------------------------------------------------------------------------
def _norm_for_compare(path):
    """Canonicalise a path for case/separator-stable equality."""
    if not path:
        return ''
    try:
        n = os.path.normpath(os.path.abspath(str(path)))
    except (ValueError, TypeError):
        return ''
    return n.lower() if os.name == 'nt' else n


def _path_binding_matches(declared, declared_os, actual_dir):
    """True iff sentinel-declared path resolves to actual_dir on this OS.

    Cross-OS resolution goes through the user's Nuke path-substitution
    table (the same one used for NukomfyRead/NukomfyWrite remap on remote
    machines). Without a matching rule, cross-OS sentinels fail closed.
    """
    if not declared:
        return False

    try:
        from Nukomfy.client.path_substitution import (
            translate_path_between, current_os, normalize_os)
        local_os = current_os()
        src_os = normalize_os(declared_os) if declared_os else local_os
        if src_os and src_os != local_os:
            try:
                translated = translate_path_between(
                    declared, src_os, local_os, _bypass_enabled_flag=True)
            except Exception:
                _log.exception(
                    'path_substitution translate_path_between failed for %s',
                    declared)
                translated = declared
        else:
            translated = declared
    except Exception:
        # path_substitution unavailable (no Nuke / module error) - fall
        # back to literal compare. Same-OS sentinels still work; cross-OS
        # ones fail closed, which is the safe default.
        _log.exception(
            'path_substitution unavailable, falling back to literal '
            'sentinel compare')
        translated = declared

    return _norm_for_compare(translated) == _norm_for_compare(actual_dir)


def _load_sentinel(dir_path, sentinel_name, required_fields, expected_schema):
    """Read+validate a sentinel JSON inside *dir_path*.

    Returns the parsed dict on success, None on:
      - dir_path not a real directory (or symlink)
      - sentinel missing or itself a symlink
      - JSON parse error
      - missing required fields
      - schema discriminator mismatch
      - path-binding mismatch (sentinel.path != actual dir, after
        cross-OS translation via path_substitution)
    """
    dir_io = _long_path(dir_path)
    if os.path.islink(dir_io) or not os.path.isdir(dir_io):
        return None
    p = os.path.join(dir_path, sentinel_name)
    p_io = _long_path(p)
    if os.path.islink(p_io) or not os.path.isfile(p_io):
        return None
    try:
        with open(p_io, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not required_fields.issubset(data.keys()):
        return None
    if isinstance(expected_schema, (set, frozenset)):
        if data.get('schema') not in expected_schema:
            return None
    elif data.get('schema') != expected_schema:
        return None
    if not _path_binding_matches(data.get('path'),
                                 data.get('path_os'),
                                 dir_path):
        _log.warning(
            'Sentinel path-binding mismatch: declared=%s (os=%s), actual=%s '
            '- refusing ownership claim',
            data.get('path'), data.get('path_os'), dir_path)
        return None
    return data


def write_output_sentinel(dir_path, workflow_id='', gizmo_name='',
                          file_patterns=None):
    """Write `.nukomfy_output.json` inside *dir_path*, claiming ownership.

    Idempotent: an existing sentinel is overwritten with refreshed
    timestamps. The directory must already exist (caller should
    `makedirs` first). The sentinel embeds the canonical path of the
    directory + the writing OS so cross-OS reads can validate via
    Nuke's path-substitution table.

    *file_patterns* is the list of basename templates Nukomfy will
    write into this folder (e.g. ['render_v001.####.exr']). At cleanup
    time, only files matching one of these patterns are deleted -
    foreign files the user added (notes, references, color charts) are
    preserved. Pass `[]` only when the caller doesn't know yet (the
    folder will be wiped wholesale on next overwrite).

    Returns True on success, False on filesystem error.
    """
    import datetime

    if not os.path.isdir(_long_path(dir_path)):
        _log.error('write_output_sentinel: dir does not exist: %s', dir_path)
        return False

    try:
        from Nukomfy.client.path_substitution import current_os
        path_os = current_os()
    except Exception:
        path_os = ''

    canonical = os.path.normpath(os.path.abspath(dir_path))
    iso_now = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec='seconds')
    data = {
        'schema': _SCHEMA_OUTPUT,
        'path': canonical,
        'path_os': path_os,
        'created_utc': iso_now,
        'workflow_id': workflow_id or '',
        'gizmo': gizmo_name or '',
        'file_patterns': list(file_patterns or []),
    }

    final = os.path.join(dir_path, SENTINEL_OUTPUT)
    tmp = final + '.tmp'
    try:
        with open(_long_path(tmp), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except OSError:
        _log.exception('write_output_sentinel write tmp failed: %s', tmp)
        return False
    return atomic_replace(tmp, final)


def is_input_cache_sentinel_valid(dir_path):
    """True iff *dir_path* carries a valid input-cache sentinel."""
    return _load_sentinel(dir_path, SENTINEL_INPUT_CACHE,
                          _REQUIRED_INPUT_CACHE,
                          _SCHEMA_INPUT_CACHE) is not None


def is_output_sentinel_valid(dir_path):
    """True iff *dir_path* carries a valid output sentinel."""
    return _load_sentinel(dir_path, SENTINEL_OUTPUT,
                          _REQUIRED_OUTPUT,
                          _SCHEMA_OUTPUT) is not None


def assert_output_ownership(path, parent=None, action=None):
    """Ownership precondition for ANY destructive action on *path*
    (Delete or Overwrite). On missing/invalid sentinel, raises the
    SAME popup that :func:`selective_delete_dir` raises in the same
    situation - title, verb and body are identical so the user sees a
    uniform refusal regardless of which action they picked.

    Returns True if a valid output sentinel is present (caller may
    proceed), False otherwise (popup already shown, caller should
    abort).
    """
    if not os.path.exists(_long_path(path)):
        # Brand-new dir, nothing to validate yet - caller will create it.
        return True
    if is_output_sentinel_valid(path):
        return True
    _log.warning(
        'assert_output_ownership refused (no valid Nukomfy sentinel): %s',
        path)
    body = ('This folder doesn\'t carry a Nukomfy ownership marker '
            '(sentinel missing, moved, or corrupted).\n\n'
            'Nukomfy will not delete it.')
    _show_info_popup(parent, 'Folder not deleted', action, path, body)
    return False


def is_any_nukomfy_sentinel_valid(dir_path):
    """True iff *dir_path* carries either kind of valid Nukomfy sentinel."""
    return (is_input_cache_sentinel_valid(dir_path)
            or is_output_sentinel_valid(dir_path))


def is_workflow_folder(dir_path):
    """True iff *dir_path* looks like a Nukomfy workflow library folder.

    Workflow folders predate the explicit `.nukomfy_*.json` sentinels -
    their implicit ownership marker is the pair of files Nukomfy writes
    when the user creates a workflow: `metadata.json` (with a
    `workflow_id` field) and `workflow.json`. Both must be present and
    `metadata.json` must parse as a JSON object carrying `workflow_id`
    so a foreign folder that happens to contain a `metadata.json` from
    some other tool isn't claimed.

    Returns True if the dir is a real (non-symlink) directory containing
    these two files with a valid metadata schema.
    """
    dir_io = _long_path(dir_path)
    if os.path.islink(dir_io) or not os.path.isdir(dir_io):
        return False
    meta_path = os.path.join(dir_path, METADATA_JSON)
    wf_path = os.path.join(dir_path, WORKFLOW_JSON)
    meta_io = _long_path(meta_path)
    wf_io = _long_path(wf_path)
    if os.path.islink(meta_io) or not os.path.isfile(meta_io):
        return False
    if os.path.islink(wf_io) or not os.path.isfile(wf_io):
        return False
    try:
        with open(meta_io, 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    return 'workflow_id' in meta


def safe_delete_dir(path, parent=None, action=None,
                    sentinel_kind='any'):
    """Delete *path* iff it carries a valid Nukomfy sentinel.

    *sentinel_kind* selects which sentinel to require:
        'input_cache' -> only `.nukomfy_input_cache.json`
        'output'      -> only `.nukomfy_output.json`
        'any'         -> either of the two above qualifies
        'workflow'    -> workflow library folder (metadata.json +
                       workflow.json), used by workflow rename cleanup

    Returns:
        True  -> deleted, or path didn't exist (idempotent)
        False -> refused (catastrophic root, symlink, missing sentinel,
                path-binding mismatch, OSError). Caller logs/handles.

    On refusal a popup names the offending path and the reason - the
    user knows immediately why their cleanup didn't proceed.
    """
    if not os.path.exists(_long_path(path)):
        return True

    if not _is_safe_rmtree_target(path):
        _log.error('safe_delete_dir refused unsafe path: %s', path)
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path,
                    'Refused: path is a filesystem/system root.')
        return False

    if os.path.islink(_long_path(path)):
        _log.error('safe_delete_dir refused symlink: %s', path)
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path,
                    'Refused: path is a symbolic link. Nukomfy never '
                    'follows symlinks for deletion.')
        return False

    if sentinel_kind == 'input_cache':
        ok = is_input_cache_sentinel_valid(path)
    elif sentinel_kind == 'output':
        ok = is_output_sentinel_valid(path)
    elif sentinel_kind == 'workflow':
        ok = is_workflow_folder(path)
    else:
        ok = is_any_nukomfy_sentinel_valid(path)

    if not ok:
        _log.warning(
            'safe_delete_dir refused (no valid Nukomfy sentinel): %s', path)
        body = ('This folder doesn\'t carry a Nukomfy ownership marker '
                '(sentinel missing, moved, or corrupted).\n\n'
                'Nukomfy will not delete it.')
        _show_info_popup(parent, 'Folder not deleted', action, path, body)
        return False

    try:
        shutil.rmtree(_long_path(path), onerror=_on_rmtree_error)
        return True
    except OSError as e:
        _log.exception('safe_delete_dir rmtree failed: %s', path)
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path, e)
        return False


def _pattern_to_regex(pattern):
    """Convert a Nukomfy frame pattern with `#` placeholders to a regex.

    Each consecutive run of `#` becomes `\\d{N}` matching exactly N
    digits. Other characters are escaped literally.

    Examples:
        'frame_v001.####.exr' -> r'^frame_v001\\.\\d{4}\\.exr$'
        'name_v001.exr'       -> r'^name_v001\\.exr$' (no frame placeholder)
    """
    if not pattern:
        return None
    parts = []
    i = 0
    while i < len(pattern):
        if pattern[i] == '#':
            j = i
            while j < len(pattern) and pattern[j] == '#':
                j += 1
            parts.append(r'\d{' + str(j - i) + '}')
            i = j
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile('^' + ''.join(parts) + '$')


def selective_delete_dir(path, parent=None, action=None,
                         sentinel_kind='output', delete_basenames=None):
    """Delete only files in *path* matching the sentinel's recorded
    file patterns. Foreign files (notes, references, anything the user
    added that doesn't match Nukomfy's naming) are preserved.

    Same safety gates as :func:`safe_delete_dir`:
        1. catastrophic root rejection
        2. symlink rejection
        3. valid Nukomfy sentinel + path-binding match required

    Matching strategy:
        - Default: pattern-based, patterns come from the sentinel itself:
            input_cache -> ``file_pattern`` (single string)
            output      -> ``file_patterns`` (list of strings)
        - If `delete_basenames` (set[str]) is provided, deletes only
          files whose basename is in that exact set. Overrides the
          sentinel's pattern matching for the matching step (sentinel
          ownership precondition still required). Used by submit_panel
          to surgically delete only the colliding files in a Single/Range
          submit, preserving Nukomfy files outside the submit's scope.

    Files matching the strategy above, plus any Nukomfy sentinel file
    (`.nukomfy_*.json`), are removed. Everything else stays.
    Subdirectories are also preserved (Nukomfy never writes nested
    output structures itself).

    The caller normally re-writes the sentinel right after with the
    new render's patterns - so the folder ends up with the new render's
    files + the user's foreign files + the refreshed sentinel.

    Deletion order is frames first, sentinel(s) last: if a frame is
    locked and cannot be removed, the ownership marker survives and the
    folder stays eligible for a future purge instead of becoming an
    untracked orphan. When nothing foreign remains the emptied dir is
    removed too.

    Returns True on success / dir doesn't exist, False on refusal.
    """
    if not os.path.exists(_long_path(path)):
        return True

    if not _is_safe_rmtree_target(path):
        _log.error('selective_delete_dir refused unsafe path: %s', path)
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path,
                    'Refused: path is a filesystem/system root.')
        return False

    if os.path.islink(_long_path(path)):
        _log.error('selective_delete_dir refused symlink: %s', path)
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path,
                    'Refused: path is a symbolic link.')
        return False

    # Load + path-binding sentinel
    if sentinel_kind == 'input_cache':
        # Deletion tolerates older schemas (reuse does not) so obsolete-version
        # caches stay reclaimable by cleanup / Clear All.
        data = _load_sentinel(path, SENTINEL_INPUT_CACHE,
                              _REQUIRED_INPUT_CACHE,
                              _DELETABLE_INPUT_CACHE_SCHEMAS)
        patterns = [data['file_pattern']] if data and data.get('file_pattern') else []
    elif sentinel_kind == 'output':
        data = _load_sentinel(path, SENTINEL_OUTPUT,
                              _REQUIRED_OUTPUT,
                              _SCHEMA_OUTPUT)
        patterns = list(data.get('file_patterns') or []) if data else []
    else:
        _log.error('selective_delete_dir: unsupported sentinel_kind=%s',
                     sentinel_kind)
        return False

    if data is None:
        _log.warning(
            'selective_delete_dir refused (no valid Nukomfy sentinel): %s',
            path)
        body = ('This folder doesn\'t carry a Nukomfy ownership marker '
                '(sentinel missing, moved, or corrupted).\n\n'
                'Nukomfy will not delete it.')
        _show_info_popup(parent, 'Folder not deleted', action, path, body)
        return False

    nukomfy_sentinels = (SENTINEL_INPUT_CACHE, SENTINEL_OUTPUT)

    # If delete_basenames provided, override pattern-based matching with
    # literal set membership. Sentinel ownership check above still
    # applies, but the file selection inside the dir is driven by the
    # caller's explicit set instead of the sentinel's stored patterns.
    if delete_basenames is not None:
        delete_set = set(delete_basenames)
        def _name_matches(name):
            return name in delete_set
    else:
        regexes = [r for r in (_pattern_to_regex(p) for p in patterns) if r]
        def _name_matches(name):
            return any(r.match(name) for r in regexes)

    try:
        names = sorted(os.listdir(_long_path(path)))
    except OSError as e:
        _log.exception('selective_delete_dir listdir failed: %s', path)
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path, e)
        return False

    # Single classification pass. Split into Nukomfy frames, the Nukomfy
    # sentinel(s), and foreign entries (anything else, including any
    # subdirectory, all preserved). Sentinels are deleted LAST so a
    # partial failure on a locked frame leaves the ownership marker in
    # place: the folder stays eligible for a future purge instead of
    # becoming an untracked orphan.
    frame_matches = []
    sentinel_matches = []
    foreign = []
    for name in names:
        full = os.path.join(path, name)
        full_io = _long_path(full)
        if os.path.isdir(full_io) and not os.path.islink(full_io):
            # Subdirs are never written by Nukomfy at this level, so
            # they're treated as foreign and preserved.
            foreign.append(name + '/')
            continue
        if name in nukomfy_sentinels:
            sentinel_matches.append(full)
        elif _name_matches(name):
            frame_matches.append(full)
        else:
            foreign.append(name)

    def _report(errs):
        _log.error('selective_delete_dir partial failure on %s: %s',
                   path, errs[:5])
        _show_popup(parent, 'Cannot remove folder', action,
                    'Removing folder', path,
                    'Failed to delete {} file(s): {}'.format(
                        len(errs),
                        ', '.join(n for n, _e in errs[:5])))

    # Frames first: attempt every frame, then bail BEFORE touching any
    # sentinel if one failed (e.g. a file still locked by a running job).
    errors = []
    for full in frame_matches:
        try:
            _force_remove(_long_path(full))
        except OSError as e:
            errors.append((os.path.basename(full), str(e)))
    if errors:
        _report(errors)
        return False

    # Sentinel(s) last, now that every frame is gone.
    for full in sentinel_matches:
        try:
            _force_remove(_long_path(full))
        except OSError as e:
            errors.append((os.path.basename(full), str(e)))
    if errors:
        _report(errors)
        return False

    # Nothing foreign to preserve -> drop the now-empty dir. A lingering
    # empty shell (rmdir race) is harmless: no sentinel, no data, not an
    # orphan.
    if not foreign:
        try:
            os.rmdir(_long_path(path))
        except OSError:
            _log.warning(
                'selective_delete_dir emptied but could not rmdir: %s', path)

    return True


def safe_delete_file(path, expected_basename=None, parent=None, action=None):
    """Delete a single file iff its basename matches *expected_basename*.

    Used for fixed-name deletions like `gizmo_logo.png`: the basename
    check is a cheap sanity gate against a caller passing the wrong
    path. Symlinks are refused.

    Returns True on success / file-not-present, False on refusal/error.
    """
    if not os.path.exists(_long_path(path)):
        return True
    if os.path.islink(_long_path(path)):
        _log.error('safe_delete_file refused symlink: %s', path)
        _show_popup(parent, 'Cannot delete file', action,
                    'Deleting file', path,
                    'Refused: path is a symbolic link.')
        return False
    if expected_basename is not None:
        actual = os.path.basename(os.path.normpath(path))
        if actual.lower() != expected_basename.lower():
            _log.error('safe_delete_file basename mismatch: %s != %s',
                         actual, expected_basename)
            _show_popup(parent, 'Cannot delete file', action,
                        'Deleting file', path,
                        'Refused: basename mismatch (expected {}).'.format(
                            expected_basename))
            return False
    try:
        os.remove(_long_path(path))
        return True
    except OSError as e:
        _log.exception('safe_delete_file failed: %s', path)
        _show_popup(parent, 'Cannot delete file', action,
                    'Deleting file', path, e)
        return False
