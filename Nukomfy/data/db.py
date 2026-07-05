"""Database layer (SQLite).

Persists local job history in `~/.nuke/nukomfy_history.db`. Public API
exposes CRUD functions plus two workflow-API blob helpers.

WAL mode enabled for concurrent read across panels; single-writer per
process serialised by `_lock` (RLock). Multi-Nuke-instance writers
serialised at SQLite level via `busy_timeout=5000`.

Connection model: one singleton `sqlite3.Connection` per process,
opened lazily on first call and reused for every CRUD.

Naming convention:
- Native columns owned by Nukomfy use the `nfy_` prefix in BOTH the
  SQL schema AND the dict keys returned to callers.
- Diagnostic metadata (workflow meta, environment, server snapshot)
  is stored as JSON columns grouped semantically - fewer columns,
  easier to extend (no ALTER TABLE for new metadata fields).
- Returned dict mirrors the schema 1:1: native columns are top-level
  keys, JSON columns deserialise to nested dicts under their name.
- Two un-prefixed columns: `id` (internal autoincrement) and
  `prompt_id` (Comfy server's universal key).
- Python kwargs of `record_submit` keep short un-prefixed names for
  ergonomics; the INSERT regroups them into the JSON columns.
- Workflow API JSON is stored in a BLOB column with a sibling flag
  column for compression. Excluded from `SELECT *` reads (loaded
  on-demand via `load_workflow_api(prompt_id)`) to keep history
  rebuilds light-weight.

Pre-release: NO migration code. If the existing DB schema differs from
the canonical version, `_connect` raises `SchemaMismatchError` and the
user deletes the local file. The history regenerates from the next
submit onwards.
"""

import datetime
import gzip
import json
import logging
import os
import sqlite3
import threading

from Nukomfy.utils.log_format import fmt_job

_log = logging.getLogger(__name__)

_DIR = os.path.join(os.path.expanduser('~'), '.nuke')
_DB_FILE = os.path.join(_DIR, 'nukomfy_history.db')

_MAX_ENTRIES_HARD = 5000

_TERMINAL_STATUSES = ('completed', 'failed', 'cancelled')

# Whitelist for `update_entry(**fields)`. Any kwarg outside this set
# raises ValueError loudly. Keys are the public dict-key names.
_UPDATABLE_COLUMNS = {
    'nfy_frame_range',
    'nfy_output_paths',
    'nfy_input_ranges',
    'nfy_seeds_used',
    'nfy_read_color',
    'nfy_range_resolved',
    'nfy_sent_at',  # used by orphan re-add to backfill timestamp from server
}

# Map of public dict-key name -> DB column name for fields stored as
# JSON TEXT in the DB but exposed as Python list/dict to callers.
_JSON_COL_MAP = {
    'nfy_frame_range':       'nfy_frame_range_json',
    'nfy_output_paths':      'nfy_output_paths_json',
    'nfy_input_ranges':      'nfy_input_ranges_json',
    'nfy_seeds_used':        'nfy_seeds_used_json',
    'nfy_execution_error':   'nfy_execution_error_json',
    'nfy_messages':          'nfy_messages_json',
    'nfy_preview_output':    'nfy_preview_output_json',
    'nfy_outputs':           'nfy_outputs_json',
    'nfy_workflow_meta':     'nfy_workflow_meta_json',
    'nfy_environment':       'nfy_environment_json',
    'nfy_server_snapshot':   'nfy_server_snapshot_json',
}

_SCHEMA_VERSION = 3

# Threshold above which the workflow API JSON payload is gzipped before
# storage. Below: stored raw (gzip header overhead ~20 bytes is wasted
# on tiny payloads). Above: ~70-90% size reduction on JSON.
_GZIP_THRESHOLD = 20 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    -- Identity / timing (native, queryable)
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id                TEXT    NOT NULL UNIQUE,
    nfy_job_id               TEXT    NOT NULL UNIQUE,
    nfy_submitted_by         TEXT    NOT NULL DEFAULT '',
    nfy_submitter_host       TEXT    NOT NULL DEFAULT '',
    nfy_machine_name         TEXT    NOT NULL,
    nfy_machine_url          TEXT    NOT NULL,
    nfy_workflow_name        TEXT    NOT NULL,
    nfy_nk_file              TEXT    NOT NULL DEFAULT '',
    nfy_node_name            TEXT    NOT NULL DEFAULT '',
    nfy_sent_at              TEXT    NOT NULL,
    nfy_batch_count          INTEGER NOT NULL DEFAULT 1,
    nfy_batch_index          INTEGER NOT NULL DEFAULT 1,
    nfy_read_color           INTEGER NOT NULL DEFAULT 0,

    -- Render config payload (lists/dicts as JSON)
    nfy_frame_range_json     TEXT    NOT NULL DEFAULT '[]',
    nfy_output_paths_json    TEXT    NOT NULL DEFAULT '[]',
    nfy_input_ranges_json    TEXT,
    nfy_seeds_used_json      TEXT,

    -- Diagnostic metadata (grouped JSON blobs, easier to extend)
    -- nfy_workflow_meta_json: workflow_uuid, workflow_author, workflow_version, params_spec
    -- nfy_environment_json:   os_submitter, nuke_version, nukomfy_version, input_cache
    -- nfy_server_snapshot_json: system_stats {os, comfyui_version, python_version, gpu, vram_total, ram_total}, server_version
    nfy_workflow_meta_json   TEXT,
    nfy_environment_json     TEXT,
    nfy_server_snapshot_json TEXT,

    -- Terminal state (set by persist_terminal_state)
    nfy_terminal_persisted   INTEGER NOT NULL DEFAULT 0
        CHECK (nfy_terminal_persisted IN (0, 1)),
    nfy_status_str           TEXT
        CHECK (nfy_status_str IS NULL OR nfy_status_str IN
               ('completed', 'failed', 'cancelled', 'unknown')),
    nfy_duration             REAL,
    nfy_outputs_count        INTEGER,
    nfy_execution_error_json TEXT,
    nfy_messages_json        TEXT,
    nfy_preview_output_json  TEXT,
    nfy_outputs_json         TEXT,

    -- Refresh-disk freeze flag
    nfy_range_resolved       INTEGER NOT NULL DEFAULT 0
        CHECK (nfy_range_resolved IN (0, 1)),

    -- Workflow API JSON snapshot of the payload posted to /prompt.
    -- Loaded on-demand via load_workflow_api(prompt_id). Excluded
    -- from SELECT * to keep history rebuilds light-weight.
    nfy_workflow_api            BLOB,
    nfy_workflow_api_compressed INTEGER NOT NULL DEFAULT 0
        CHECK (nfy_workflow_api_compressed IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_prompt_id ON jobs(prompt_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_nfy_job_id ON jobs(nfy_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_terminal_sent
    ON jobs(nfy_terminal_persisted, nfy_sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_sent_at ON jobs(nfy_sent_at DESC);
"""

_lock = threading.RLock()
_conn = None


def db_path():
    """Absolute path of the DB file."""
    return _DB_FILE


def _ensure_dir():
    """Create ~/.nuke if missing. Best-effort via fs_safe."""
    try:
        import Nukomfy.utils.fs_safe as fs_safe
        return fs_safe.makedirs_silent(_DIR)
    except Exception:
        try:
            os.makedirs(_DIR, exist_ok=True)
            return True
        except OSError:
            return False


class SchemaMismatchError(RuntimeError):
    """Raised when an existing DB has a schema version this build doesn't
    speak. Pre-release: no migration code, the user deletes the local DB
    and restarts (history is regenerated from the next submit onwards)."""


def _connect():
    """Return the singleton sqlite3.Connection.

    Fresh DB: creates the canonical schema. Existing DB with a different
    `user_version`: raises `SchemaMismatchError` with a clear message -
    pre-release builds carry no migration code by design.
    """
    global _conn
    if _conn is not None:
        return _conn
    if not _ensure_dir():
        raise RuntimeError(
            'Cannot create directory for history DB: %s' % _DIR)
    conn = sqlite3.connect(_DB_FILE, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA synchronous=NORMAL')
    current = conn.execute('PRAGMA user_version').fetchone()[0]
    if current == 0:
        conn.executescript(_SCHEMA)
        conn.execute('PRAGMA user_version=%d' % _SCHEMA_VERSION)
        conn.commit()
    elif current != _SCHEMA_VERSION:
        conn.close()
        raise SchemaMismatchError(
            'Nukomfy history DB at {} reports schema v{} but this '
            'build expects v{}. Delete the file (and its '
            '.db-wal/.db-shm siblings) and restart Nuke; history will '
            'be regenerated from the next submit.'.format(
                _DB_FILE, current, _SCHEMA_VERSION))
    _conn = conn
    return _conn


def close():
    """Close the singleton connection (used by tests)."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
        _conn = None


# ---------------------------------------------------------------------------
# Row <-> dict converters
# ---------------------------------------------------------------------------

_ROW_COLUMNS = (
    'id', 'prompt_id',
    'nfy_job_id', 'nfy_submitted_by', 'nfy_submitter_host',
    'nfy_machine_name', 'nfy_machine_url',
    'nfy_workflow_name', 'nfy_nk_file', 'nfy_node_name',
    'nfy_sent_at',
    'nfy_batch_count', 'nfy_batch_index', 'nfy_read_color',
    'nfy_frame_range_json', 'nfy_output_paths_json',
    'nfy_input_ranges_json', 'nfy_seeds_used_json',
    'nfy_workflow_meta_json', 'nfy_environment_json',
    'nfy_server_snapshot_json',
    'nfy_terminal_persisted', 'nfy_status_str', 'nfy_duration',
    'nfy_outputs_count',
    'nfy_execution_error_json', 'nfy_messages_json',
    'nfy_preview_output_json', 'nfy_outputs_json',
    'nfy_range_resolved',
)

# Explicit column list for SELECT - excludes the workflow-API BLOB +
# its compressed flag, which are loaded on-demand by load_workflow_api.
_SELECT_COLS_SQL = ', '.join(_ROW_COLUMNS)


def _row_to_dict(row):
    """Convert a DB row to the canonical entry dict.

    Mandatory fields always present; optional ones (terminal state,
    diagnostic JSON blobs, opt lists) omitted when NULL/0 so callers
    can use `entry.get('nfy_foo')` semantics.
    """
    if row is None:
        return None
    from Nukomfy.utils.url_obfuscation import deobfuscate_url, is_obfuscated
    r = dict(zip(_ROW_COLUMNS, row))
    stored_url = r['nfy_machine_url']
    machine_name = r['nfy_machine_name']
    # Transparent deobfuscation: callers always see the plain URL.
    # `machine_name` is the snapshot at submit time, used as the key
    # so the URL stays decodable even if the machine is later renamed.
    # `was_hidden` is derived from the storage prefix so the UI can
    # keep hiding the URL even if the machine has been removed from
    # Settings or its name has been reused on a new visible machine.
    was_hidden = is_obfuscated(stored_url)
    plain_url = (deobfuscate_url(stored_url, machine_name)
                 if was_hidden else stored_url)
    out = {
        'prompt_id': r['prompt_id'],
        'nfy_job_id': r['nfy_job_id'] or '',
        'nfy_submitted_by': r['nfy_submitted_by'] or '',
        'nfy_submitter_host': r['nfy_submitter_host'] or '',
        'nfy_machine_name': machine_name,
        'nfy_machine_url': plain_url,
        'nfy_machine_hidden_url': was_hidden,
        'nfy_workflow_name': r['nfy_workflow_name'],
        'nfy_sent_at': r['nfy_sent_at'],
        'nfy_frame_range': _safe_json_loads(r['nfy_frame_range_json'], []),
        'nfy_nk_file': r['nfy_nk_file'] or '',
        'nfy_node_name': r['nfy_node_name'] or '',
        'nfy_output_paths': _safe_json_loads(r['nfy_output_paths_json'], []),
        'nfy_batch_count': r['nfy_batch_count'],
        'nfy_batch_index': r['nfy_batch_index'],
        'nfy_read_color': r['nfy_read_color'] or 0,
    }
    if r['nfy_input_ranges_json'] is not None:
        out['nfy_input_ranges'] = _safe_json_loads(
            r['nfy_input_ranges_json'], [])
    if r['nfy_seeds_used_json'] is not None:
        out['nfy_seeds_used'] = _safe_json_loads(
            r['nfy_seeds_used_json'], [])
    # Diagnostic metadata (grouped JSON blobs)
    if r.get('nfy_workflow_meta_json') is not None:
        meta = _safe_json_loads(r['nfy_workflow_meta_json'], None)
        if meta:
            out['nfy_workflow_meta'] = meta
    if r.get('nfy_environment_json') is not None:
        env = _safe_json_loads(r['nfy_environment_json'], None)
        if env:
            out['nfy_environment'] = env
    if r.get('nfy_server_snapshot_json') is not None:
        snap = _safe_json_loads(r['nfy_server_snapshot_json'], None)
        if snap:
            out['nfy_server_snapshot'] = snap
    # Terminal state
    if r['nfy_terminal_persisted']:
        out['nfy_terminal_persisted'] = True
        if r['nfy_status_str'] is not None:
            out['nfy_status_str'] = r['nfy_status_str']
        if r['nfy_duration'] is not None:
            out['nfy_duration'] = r['nfy_duration']
        if r['nfy_execution_error_json'] is not None:
            out['nfy_execution_error'] = _safe_json_loads(
                r['nfy_execution_error_json'], None)
        if r['nfy_outputs_count'] is not None:
            out['nfy_outputs_count'] = r['nfy_outputs_count']
        if r['nfy_messages_json'] is not None:
            out['nfy_messages'] = _safe_json_loads(r['nfy_messages_json'], [])
        if r['nfy_preview_output_json'] is not None:
            out['nfy_preview_output'] = _safe_json_loads(
                r['nfy_preview_output_json'], None)
        if r['nfy_outputs_json'] is not None:
            out['nfy_outputs'] = _safe_json_loads(r['nfy_outputs_json'], None)
    if r['nfy_range_resolved']:
        out['nfy_range_resolved'] = True
    return out


def _safe_json_loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _trim_to_cap(conn):
    try:
        from Nukomfy.core.settings import settings
        cap = min(int(settings.local_history_max_entries), _MAX_ENTRIES_HARD)
    except Exception:
        cap = _MAX_ENTRIES_HARD
    cap = max(cap, 1)
    conn.execute(
        "DELETE FROM jobs WHERE id NOT IN ("
        "SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
        (cap,))


def _build_workflow_meta(workflow_uuid, workflow_author, workflow_version,
                         params_spec=None, write_templates=None):
    """Bundle the workflow_meta sub-fields into a dict (or None if all empty)."""
    m = {}
    if workflow_uuid:
        m['workflow_uuid'] = workflow_uuid
    if workflow_author:
        m['workflow_author'] = workflow_author
    if workflow_version:
        m['workflow_version'] = workflow_version
    if params_spec:
        m['params_spec'] = params_spec
    if write_templates:
        m['write_templates'] = write_templates
    return m or None


def _build_environment(os_submitter, nuke_version, nukomfy_version,
                      input_cache):
    """Bundle the environment sub-fields into a dict (or None if all empty)."""
    e = {}
    if os_submitter:
        e['os_submitter'] = os_submitter
    if nuke_version:
        e['nuke_version'] = nuke_version
    if nukomfy_version:
        e['nukomfy_version'] = nukomfy_version
    if input_cache:
        e['input_cache'] = input_cache
    return e or None


def _build_server_snapshot(system_stats, server_version):
    """Bundle the 2 server_snapshot sub-fields into a dict (or None if both empty)."""
    s = {}
    if system_stats:
        s['system_stats'] = system_stats
    if server_version:
        s['server_version'] = server_version
    return s or None


# ---------------------------------------------------------------------------
# Public CRUD functions
# ---------------------------------------------------------------------------

def _serialise_workflow_api(payload):
    """Encode workflow API payload for BLOB storage.

    Returns (blob, compressed_flag). gzip if raw size > _GZIP_THRESHOLD.
    Returns (None, 0) when payload is None or serialisation fails.
    """
    if payload is None:
        return (None, 0)
    try:
        raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError):
        _log.exception('Workflow API serialisation failed')
        return (None, 0)
    if len(raw) > _GZIP_THRESHOLD:
        return (gzip.compress(raw), 1)
    return (raw, 0)


def record_submit(prompt_id, machine_name, machine_url, workflow_name,
                  frame_range, nk_file, node_name, output_paths, batch_count,
                  seeds_used=None, input_ranges=None,
                  batch_index=1, nfy_job_id=None,
                  nfy_submitted_by=None, nfy_submitter_host=None,
                  read_color=0, sent_at=None,
                  # Capture extensions - splatted from collect_submit_capture
                  system_stats=None, server_version=None,
                  os_submitter=None, nuke_version=None, nukomfy_version=None,
                  input_cache=None,
                  workflow_author=None, workflow_uuid=None,
                  workflow_version=None, params_spec=None,
                  write_templates=None,
                  # Workflow API JSON snapshot stored in a BLOB column
                  workflow_api_payload=None):
    """Append a new submit entry to the history.

    Python kwargs keep the un-prefixed short names for ergonomics; the
    INSERT regroups the diagnostic metadata into 3 JSON columns
    (workflow_meta, environment, server_snapshot). The workflow API
    payload (the dict posted to /prompt) is stored in the BLOB column
    in the same atomic INSERT - gzipped if raw size exceeds the
    threshold.
    """
    workflow_meta = _build_workflow_meta(
        workflow_uuid, workflow_author, workflow_version, params_spec,
        write_templates)
    environment = _build_environment(
        os_submitter, nuke_version, nukomfy_version, input_cache)
    server_snapshot = _build_server_snapshot(system_stats, server_version)
    wf_blob, wf_flag = _serialise_workflow_api(workflow_api_payload)
    sql = (
        "INSERT INTO jobs ("
        "prompt_id, nfy_job_id, nfy_submitted_by, nfy_submitter_host, "
        "nfy_machine_name, nfy_machine_url, nfy_workflow_name, "
        "nfy_nk_file, nfy_node_name, nfy_sent_at, "
        "nfy_batch_count, nfy_batch_index, nfy_read_color, "
        "nfy_frame_range_json, nfy_output_paths_json, "
        "nfy_input_ranges_json, nfy_seeds_used_json, "
        "nfy_workflow_meta_json, nfy_environment_json, "
        "nfy_server_snapshot_json, "
        "nfy_workflow_api, nfy_workflow_api_compressed"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)"
    )
    params = (
        prompt_id,
        nfy_job_id or '',
        nfy_submitted_by or '',
        nfy_submitter_host or '',
        machine_name,
        machine_url,
        workflow_name,
        nk_file or '',
        node_name or '',
        sent_at or datetime.datetime.now().isoformat(timespec='microseconds'),
        int(batch_count or 1),
        int(batch_index or 1),
        int(read_color or 0),
        json.dumps(frame_range or []),
        json.dumps(output_paths or []),
        json.dumps(input_ranges) if input_ranges else None,
        json.dumps(seeds_used) if seeds_used else None,
        json.dumps(workflow_meta) if workflow_meta else None,
        json.dumps(environment) if environment else None,
        json.dumps(server_snapshot) if server_snapshot else None,
        wf_blob,
        wf_flag,
    )
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(sql, params)
            _trim_to_cap(conn)


def save_workflow_api(prompt_id, payload):
    """Persist the workflow API JSON snapshot for an existing entry.

    Standalone helper - useful when the workflow couldn't be passed at
    record_submit time. UPDATE WHERE prompt_id; no-op if the row
    doesn't exist. Idempotent: re-saving overwrites the previous BLOB.
    """
    if not prompt_id:
        return
    blob, flag = _serialise_workflow_api(payload)
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(
                "UPDATE jobs SET nfy_workflow_api=?, "
                "nfy_workflow_api_compressed=? WHERE prompt_id=?",
                (blob, flag, prompt_id))


def load_workflow_api(prompt_id):
    """Return the workflow API dict stored for prompt_id, or None.

    None if the row is absent OR the BLOB is NULL (entry was recorded
    without a payload). Decompresses transparently when the flag column
    indicates gzip.
    """
    if not prompt_id:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT nfy_workflow_api, nfy_workflow_api_compressed "
            "FROM jobs WHERE prompt_id=?",
            (prompt_id,)).fetchone()
    if not row or row[0] is None:
        return None
    blob, flag = row[0], row[1]
    try:
        raw = gzip.decompress(blob) if flag else blob
        return json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        _log.exception(
            'Workflow API decode failed for %s', fmt_job(prompt_id))
        return None


def get_history(limit=None, only_terminal=False):
    """Return history entries (most recent first)."""
    sql = "SELECT " + _SELECT_COLS_SQL + " FROM jobs"
    params = []
    if only_terminal:
        sql += " WHERE nfy_terminal_persisted=1"
    sql += " ORDER BY nfy_sent_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    with _lock:
        conn = _connect()
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_entry(prompt_id, **fields):
    """Update fields of an existing entry by prompt_id. No-op if missing.

    Strict whitelist: only columns in `_UPDATABLE_COLUMNS` accepted.
    Raises ValueError on any kwarg outside the whitelist.
    """
    if not fields:
        return
    bad = set(fields.keys()) - _UPDATABLE_COLUMNS
    if bad:
        raise ValueError(
            'update_entry: unknown/forbidden fields %s. '
            'Allowed: %s' % (sorted(bad), sorted(_UPDATABLE_COLUMNS)))
    sets = []
    params = []
    for k, v in fields.items():
        if k in _JSON_COL_MAP:
            sets.append('%s=?' % _JSON_COL_MAP[k])
            params.append(json.dumps(v) if v is not None else None)
        elif k == 'nfy_range_resolved':
            sets.append('nfy_range_resolved=?')
            params.append(1 if v else 0)
        elif k == 'nfy_read_color':
            sets.append('nfy_read_color=?')
            params.append(int(v or 0))
        else:
            sets.append('%s=?' % k)
            params.append(v)
    params.append(prompt_id)
    sql = "UPDATE jobs SET " + ', '.join(sets) + " WHERE prompt_id=?"
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(sql, params)


def delete_entry(prompt_id):
    """Remove a single entry by prompt_id."""
    with _lock:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM jobs WHERE prompt_id=?", (prompt_id,))
    _clear_input_cache_in_use(prompt_id)


def is_terminal_persisted(prompt_id):
    """Fast check: is this entry already frozen with terminal status?"""
    if not prompt_id:
        return False
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT nfy_terminal_persisted FROM jobs WHERE prompt_id=?",
            (prompt_id,)).fetchone()
    return bool(row[0]) if row else False


def find_nfy_job_id(prompt_id):
    """Return the nfy_job_id paired with prompt_id, or '' if unknown.

    Used by log helpers to render `job <id>` instead of `prompt <id>`
    whenever the entry exists in local history.
    """
    if not prompt_id:
        return ''
    try:
        with _lock:
            conn = _connect()
            row = conn.execute(
                "SELECT nfy_job_id FROM jobs WHERE prompt_id=?",
                (prompt_id,)).fetchone()
    except Exception:
        return ''
    return (row[0] if row else '') or ''


def persist_terminal_state(prompt_id, status_str, duration,
                           execution_error=None, outputs_count=None,
                           messages=None, preview_output=None,
                           outputs=None):
    """Freeze a history entry with terminal server data. Idempotent."""
    if not prompt_id or status_str not in _TERMINAL_STATUSES:
        return
    with _lock:
        conn = _connect()
        with conn:
            row = conn.execute(
                "SELECT nfy_terminal_persisted FROM jobs WHERE prompt_id=?",
                (prompt_id,)).fetchone()
            if not row or row[0]:
                return
            sets = ['nfy_status_str=?', 'nfy_duration=?',
                    'nfy_terminal_persisted=1']
            params = [status_str, duration]
            if execution_error is not None:
                sets.append('nfy_execution_error_json=?')
                params.append(json.dumps(execution_error))
            if outputs_count is not None:
                sets.append('nfy_outputs_count=?')
                params.append(outputs_count)
            if messages is not None:
                sets.append('nfy_messages_json=?')
                params.append(json.dumps(messages))
            if preview_output is not None:
                sets.append('nfy_preview_output_json=?')
                params.append(json.dumps(preview_output))
            if outputs is not None:
                sets.append('nfy_outputs_json=?')
                params.append(json.dumps(outputs))
            params.append(prompt_id)
            conn.execute(
                "UPDATE jobs SET " + ', '.join(sets)
                + " WHERE prompt_id=?", params)
    _clear_input_cache_in_use(prompt_id)


def persist_as_lost(prompt_id):
    """Freeze an entry as failed (lost to crash/timeout). Idempotent.

    Reached when the server is online but reports no trace of the pid
    (running, pending, or recent terminal listing) and no persistent
    history covers it either - i.e. the job is definitively gone with
    no completion. `failed` is the closest semantic: the work did not
    complete, with no user-initiated cancel. Symmetric to the manager
    custom_node's `boot_reconcile` for `running` pids interrupted by a
    server restart.
    """
    if not prompt_id:
        return
    with _lock:
        conn = _connect()
        with conn:
            conn.execute(
                "UPDATE jobs SET nfy_status_str='failed', "
                "nfy_duration=COALESCE(nfy_duration, 0), "
                "nfy_terminal_persisted=1 "
                "WHERE prompt_id=? AND nfy_terminal_persisted=0",
                (prompt_id,))


def clear_history():
    """Remove all entries."""
    with _lock:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM jobs")


def clear_terminal():
    """Remove only entries frozen with nfy_terminal_persisted=True."""
    with _lock:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM jobs WHERE nfy_terminal_persisted=1")


def bulk_update_ranges(updates):
    """Bulk persist nfy_frame_range + nfy_range_resolved updates."""
    if not updates:
        return
    with _lock:
        conn = _connect()
        with conn:
            for pid, fr, resolved in updates:
                conn.execute(
                    "UPDATE jobs SET nfy_frame_range_json=?, "
                    "nfy_range_resolved=? WHERE prompt_id=?",
                    (json.dumps(fr or []),
                     1 if resolved else 0,
                     pid))


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------

def _clear_input_cache_in_use(prompt_id):
    """Best-effort sweep of input cache sentinels."""
    if not prompt_id:
        return
    try:
        from Nukomfy.data.input_cache_writer import clear_in_use_for_prompt
        clear_in_use_for_prompt(prompt_id)
    except Exception:
        _log.exception(
            'Input cache in-use clear failed for %s', fmt_job(prompt_id))
