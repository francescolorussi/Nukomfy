"""Applies Nuke's path substitution table when submitting cross-OS renders.

Reads Nuke's built-in path substitution table (Preferences > Path
Substitutions) and applies it when submitting renders to a remote
ComfyUI machine running a different OS.

OS strings are unified across the codebase:
  - ComfyUI /system_stats returns sys.platform verbatim (verified at
    server.py:865 in upstream ComfyUI master): 'win32', 'darwin',
    'linux', or 'linux2' on legacy Python.
  - Local Nuke Python reads sys.platform: same values.
  - Both sides are normalized via normalize_os() before comparison.

Exotic platforms (FreeBSD, AIX, Cygwin) fall through to OS_UNKNOWN and
substitute_path() returns the original path unchanged - graceful no-op.

normalize_os() is idempotent: it accepts both the raw sys.platform
values and its own canonical outputs ('Windows'/'OSX'/'Linux'), because
machine.info['os'] is already normalized when fetched (see check_machine
in machines.py).
"""

import sys

from Nukomfy.core.settings import settings

OS_WINDOWS = 'Windows'
OS_LINUX   = 'Linux'
OS_MACOS   = 'OSX'
OS_UNKNOWN = ''


def normalize_os(raw):
    """Canonicalize an OS string from sys.platform or /system_stats (win32/
    darwin/linux/linux2 or already-canonical Windows/Linux/OSX)."""
    if not raw:
        return OS_UNKNOWN
    k = str(raw).strip().lower()
    if k == 'win32' or k == 'windows':
        return OS_WINDOWS
    if k == 'darwin' or k == 'osx' or k == 'macos':
        return OS_MACOS
    if k.startswith('linux'):
        return OS_LINUX
    return OS_UNKNOWN


def current_os():
    """Canonical OS of the machine running this code."""
    return normalize_os(sys.platform)


def get_machine_os(machine):
    """Canonical OS of a target machine, or OS_UNKNOWN."""
    if machine and machine.info:
        return normalize_os(machine.info.get('os', ''))
    return OS_UNKNOWN


def get_nuke_substitutions():
    """Return (windows, osx, linux) triples from Nuke's Path Substitutions.

    The `platformPathRemaps` knob name is undocumented; `.value()` returns 0.0
    and only `.toScript()` yields the actual semicolon-separated data (3 entries
    per row, column order win/osx/linux, empty string or '-' = no mapping).
    The Preferences UI displays columns as OSX/Windows/Linux but the serialized
    order from toScript() is always Windows/OSX/Linux regardless of UI order.
    Source: https://www.mail-archive.com/nuke-python@support.thefoundry.co.uk/msg04407.html
    """
    try:
        import nuke
    except ImportError:
        return []

    prefs = nuke.toNode('preferences')
    if not prefs:
        return []

    knob = prefs.knob('platformPathRemaps')
    if not knob:
        return []

    try:
        raw = knob.toScript()
    except Exception:
        return []
    if not raw:
        return []

    parts = [p.strip() for p in raw.split(';')]
    # Drop a trailing empty token from the terminating ';'
    if parts and parts[-1] == '':
        parts = parts[:-1]

    rows = []
    for i in range(0, len(parts) - 2, 3):
        win, osx, lin = parts[i], parts[i + 1], parts[i + 2]
        rows.append((win, osx, lin))
    return rows


_OS_COL = {OS_WINDOWS: 0, OS_MACOS: 1, OS_LINUX: 2}


def _normalise_cell(value):
    """Treat empty string or '-' (Nuke UI placeholder) as no-mapping."""
    s = (value or '').replace('\\', '/').rstrip('/')
    if s.strip() == '-':
        return ''
    return s


def translate_path_between(path, src_os, tgt_os, _pairs=None,
                           _bypass_enabled_flag=False):
    """Translate `path` from `src_os` to `tgt_os` via Nuke's substitution table.

    Lower-level than :func:`substitute_path`: takes explicit OS strings
    instead of a Machine object. Used by sentinel path-binding to
    interpret a sentinel written on machine A from the perspective of
    machine B that's now reading it. Same matching rules as
    `substitute_path` (boundary-safe, case-insensitive on Windows/macOS
    sources).

    `_bypass_enabled_flag=True` lets safety-critical callers (sentinel
    validation) translate even when the user has disabled the global
    `path_substitution_enabled` setting - refusing to translate would
    falsely reject legitimate cross-OS sentinels and we'd skip a
    cleanup the user expects. Substitution-table itself is still empty
    when no rules exist, so `_bypass_enabled_flag` only matters when
    the user has rules but flipped the master switch off.
    """
    if not path or not src_os or not tgt_os:
        return path
    if src_os == tgt_os:
        return path
    if not _bypass_enabled_flag and not settings.path_substitution_enabled:
        return path

    src_col = _OS_COL.get(src_os)
    tgt_col = _OS_COL.get(tgt_os)
    if src_col is None or tgt_col is None:
        return path

    rows = _pairs if _pairs is not None else get_nuke_substitutions()
    if not rows:
        return path

    norm_path     = path.replace('\\', '/')
    target_is_win = (tgt_os == OS_WINDOWS)
    # Windows is always case-insensitive; macOS's default filesystem
    # (APFS/HFS+) is case-insensitive too. Match those case-insensitively so a
    # rule stored with different casing than the real path still translates
    # (drive-letter AND UNC on Windows). Linux is case-sensitive - match exact.
    source_case_insensitive = src_os in (OS_WINDOWS, OS_MACOS)

    for row in rows:
        if len(row) < 3:
            continue
        source = _normalise_cell(row[src_col])
        target = _normalise_cell(row[tgt_col])
        if not source or not target:
            continue

        if source_case_insensitive:
            match = norm_path.lower().startswith(source.lower())
        else:
            match = norm_path.startswith(source)
        if not match:
            continue

        # Match at path boundary only (no partial directory names)
        plen = len(source)
        if plen < len(norm_path) and norm_path[plen] != '/':
            continue

        result = target + norm_path[plen:]
        if target_is_win:
            result = result.replace('/', '\\')
        return result

    return path


def substitute_path(path, machine, _pairs=None, _local_os=None):
    """Translate `path` for `machine` via Nuke's platform substitutions.
    Returns the substituted path, or the original if no rule applies."""
    if not path or not machine:
        return path
    if not settings.path_substitution_enabled:
        return path

    local_os  = _local_os if _local_os is not None else current_os()
    target_os = get_machine_os(machine)

    return translate_path_between(path, local_os, target_os, _pairs=_pairs)


def maybe_long_prefix_for_target(path, machine):
    r"""Prefix `path` with the Windows long-path adapter when the
    target machine runs Windows and the path exceeds the legacy MAX_PATH
    (260 chars). Lets the server-side ComfyUI process open files past
    260 chars without relying on the LongPathsEnabled registry flag.

    No-op when:
      - target is Linux/macOS (PATH_MAX is ~4 KB / ~1 KB, not realistic)
      - path is short enough (< 260)
      - path is already prefixed (idempotent)

    The prefix disables Windows path normalisation, so forward slashes
    are converted to backslashes on the way in (the prefix only takes
    effect with backslashes). Drive paths use ``\\?\``, UNC paths use
    ``\\?\UNC\``.
    """
    if not path or not isinstance(path, str):
        return path
    if get_machine_os(machine) != OS_WINDOWS:
        return path
    if path.startswith('\\\\?\\') or path.startswith('//?/'):
        return path
    if len(path) <= 259:
        return path
    win_path = path.replace('/', '\\')
    if win_path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + win_path[2:]
    return '\\\\?\\' + win_path