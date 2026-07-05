"""Submit history (thin shim over Nukomfy.data.db).

Storage backend is SQLite at `~/.nuke/nukomfy_history.db`. The stateful
CRUD functions live in `Nukomfy.data.db` together with two BLOB helpers for
the workflow API JSON snapshot; this module re-exports them and keeps
the format/utility helpers + the disk-rescan logic local to this file.

No auto-migration code. If the schema in the DB doesn't match what this
build expects, `Nukomfy.db._connect` raises `SchemaMismatchError` with
instructions to delete the local file.
"""

import random
import string

# Re-export the stateful CRUD functions and workflow-API BLOB helpers
# so existing callers keep importing them from this module.
from Nukomfy.data.db import (
    record_submit,
    get_history,
    update_entry,
    delete_entry,
    is_terminal_persisted,
    persist_terminal_state,
    persist_as_lost,
    clear_history,
    clear_terminal,
    bulk_update_ranges,
    save_workflow_api,
    load_workflow_api,
    db_path,
)

__all__ = [
    # CRUD (re-exported from db)
    'record_submit', 'get_history', 'update_entry', 'delete_entry',
    'is_terminal_persisted', 'persist_terminal_state', 'persist_as_lost',
    'clear_history', 'clear_terminal',
    'save_workflow_api', 'load_workflow_api',
    'refresh_ranges_from_disk',
    # Utilities (local)
    'generate_job_id', 'history_path',
    'format_frame_range', 'resolve_ends_from_disk',
]


_JOB_ID_ALPHABET = string.ascii_letters + string.digits  # Base62
_JOB_ID_LEN = 7


def generate_job_id():
    """Return a short random Base62 id (~3.5T combinations).

    Used as `nfy_job_id` in submit extra_data and persisted alongside the
    server-assigned prompt_id. Shown to the user in dialog titles and
    table rows as a stable, human-friendly handle.
    """
    return ''.join(random.choices(_JOB_ID_ALPHABET, k=_JOB_ID_LEN))


def history_path():
    """Return the absolute path to the history DB file."""
    return db_path()


def format_frame_range(value):
    """Render a frame_range value (list of per-entry dicts) as a display
    string.

    Always renders as 'start-end'. If end is None (unresolved Range),
    renders as 'start-?'. Multi entries: 'name1: 1001-1100 | name2: 2000-2000'.
    """
    if not value or not isinstance(value, list):
        return ''
    parts = []
    for e in value:
        if not isinstance(e, dict):
            continue
        s = e.get('start')
        f = e.get('end')
        s_str = '?' if s is None else str(s)
        f_str = '?' if f is None else str(f)
        rng = '{}-{}'.format(s_str, f_str)
        if len(value) > 1:
            parts.append('{}: {}'.format(e.get('name', '?'), rng))
        else:
            parts.append(rng)
    return ' | '.join(parts)


def _scan_frame_range_from_disk(path_pattern):
    """Glob *path_pattern* and return (min, max) frame numbers.

    Supports both '%0Nd' (printf) and '#' (Nuke padded) patterns.
    Returns None if no files match or the pattern can't be parsed.
    """
    import glob
    import re
    if not path_pattern:
        return None
    norm = path_pattern.replace('\\', '/')
    m = re.search(r'%0(\d+)d', norm)
    if m:
        pad = int(m.group(1))
        prefix = norm[:m.start()]
        suffix = norm[m.end():]
    else:
        m = re.search(r'#+', norm)
        if not m:
            return None
        pad = m.end() - m.start()
        prefix = norm[:m.start()]
        suffix = norm[m.end():]
    # glob.escape prefix/suffix (bracket chars in a user output root would
    # otherwise be read as a glob char class); keep the intended [0-9] class.
    glob_pat = glob.escape(prefix) + '[0-9]' * pad + glob.escape(suffix)
    frame_re = re.compile(
        '^' + re.escape(prefix) + r'(\d{' + str(pad) + r'})'
        + re.escape(suffix) + '$')
    frames = []
    try:
        matches = glob.glob(glob_pat)
    except Exception:
        return None
    for f in matches:
        f_norm = f.replace('\\', '/')
        mm = frame_re.match(f_norm)
        if mm:
            try:
                frames.append(int(mm.group(1)))
            except ValueError:
                pass
    if not frames:
        return None
    return min(frames), max(frames)


def resolve_ends_from_disk(ranges, output_paths):
    """For entries with end=None, attempt to derive end by globbing the
    aligned output_path template. Returns a new list - does not mutate.

    No io_mode gating: if end is unknown and a path is available, try.
    Scan failures (no files, bad pattern, offline share) leave end=None,
    which displays as 'start-?'.
    """
    if not isinstance(ranges, list):
        return ranges
    resolved = []
    for idx, e in enumerate(ranges):
        if not isinstance(e, dict):
            resolved.append(e)
            continue
        new_e = dict(e)
        if (new_e.get('end') is None
                and output_paths
                and idx < len(output_paths)):
            scan = _scan_frame_range_from_disk(output_paths[idx])
            if scan is not None:
                _, max_f = scan
                new_e['end'] = max_f
        resolved.append(new_e)
    return resolved


def _written_frame_count(outputs):
    """Total frames written across all NukomfyWrite nodes, from the
    server-confirmed `nukomfy_written` payload (nfy_outputs). 0 if absent.
    """
    total = 0
    if isinstance(outputs, dict):
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for rec in node_out.get('nukomfy_written') or []:
                if not isinstance(rec, dict):
                    continue
                count = rec.get('count')
                if isinstance(count, int):
                    total += count
                else:
                    paths = rec.get('paths')
                    total += len(paths) if isinstance(paths, list) else 0
    return total


def _resolve_ends_from_count(ranges, outputs):
    """Fill the end of the single unresolved range entry from the
    server-confirmed write count. Mutates *ranges* in place; returns True
    when it changed something.

    Fallback for when the disk glob can't reach the files (output on a
    remote farm, or manager persistence off). io_write emits frames
    contiguously from frame_start, so end = start + count - 1. Acts only
    when exactly one entry is unresolved, to stay unambiguous on
    multi-output jobs.
    """
    if not isinstance(ranges, list):
        return False
    count = _written_frame_count(outputs)
    if count <= 0:
        return False
    pending = [it for it in ranges if isinstance(it, dict)
               and it.get('end') is None and isinstance(it.get('start'), int)]
    if len(pending) != 1:
        return False
    pending[0]['end'] = pending[0]['start'] + count - 1
    return True


def refresh_ranges_from_disk():
    """Scan output dirs for entries whose frame_range contains range outputs,
    and update their start/end based on actual files on disk.

    Entries with `range_resolved: True` are skipped (frozen). When an entry's
    render has completed (`duration > 0`), it is frozen after this pass so it
    is never rescanned again.

    Always returns the full list of entries (post-update). Callers pass this
    to `RenderDataStore.load_local_history(entries=...)` to avoid a second
    disk read in the same refresh cycle.
    """
    entries = get_history()
    updates = []  # [(prompt_id, frame_range, range_resolved), ...]
    for e in entries:
        if e.get('nfy_range_resolved'):
            continue
        fr = e.get('nfy_frame_range')
        if not isinstance(fr, list):
            continue
        output_paths = e.get('nfy_output_paths') or []
        changed_in_entry = False
        for idx, item in enumerate(fr):
            if not isinstance(item, dict):
                continue
            if item.get('io_mode') != 'Sequence':
                continue
            if idx >= len(output_paths):
                continue
            scan = _scan_frame_range_from_disk(output_paths[idx])
            if scan is None:
                continue
            s, f = scan
            if item.get('start') != s or item.get('end') != f:
                item['start'] = s
                item['end'] = f
                changed_in_entry = True
        # Count-based fallback: when the disk glob couldn't fill an end
        # (remote output unreachable, or manager persistence off so the
        # server didn't resolve), use the server-confirmed write count.
        if _resolve_ends_from_count(fr, e.get('nfy_outputs')):
            changed_in_entry = True
        # Freeze the entry once the render has completed.
        freeze = e.get('nfy_duration', 0) > 0
        if changed_in_entry or freeze:
            updates.append((e['prompt_id'], fr, freeze))
            if freeze:
                e['nfy_range_resolved'] = True
    if updates:
        bulk_update_ranges(updates)
    return entries
