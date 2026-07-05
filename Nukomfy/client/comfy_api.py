"""Blocking HTTP functions for interacting with ComfyUI machines.

Uses urllib.request only (zero external dependencies).
All functions are blocking - call from background threads.
"""

import json
import logging
import urllib.parse
import urllib.request
import urllib.error

from Nukomfy.client.manager_client import (
    get_persistent_history,
    get_persistent_history_one,
    has_persistent_history,
)
from Nukomfy.core.identity import header_user
from Nukomfy.utils.log_format import fmt_machine
from Nukomfy.utils.url_obfuscation import scrub_url_in_text

_log = logging.getLogger(__name__)

_TIMEOUT = 10       # seconds - GET requests (queue, history, system_stats)
_TIMEOUT_SUBMIT = 30  # seconds - POST /prompt (validation + model loading)
# Timeout for the status/refresh probe (queue reachability). Generous on
# purpose: a slow-but-alive machine must have time to answer (no false
# offline). UI responsiveness is decoupled - handled by the refresh
# button's soft deadline (gui/_auto_refresh.SOFT_READY_MS) - so this does
# NOT gate how long the user waits. NOT used by the submit POST or the
# submit-time concurrency pre-flight (those keep _TIMEOUT/_TIMEOUT_SUBMIT).
# Shared with machines.check_queue and the Render Manager unified fetch.
STATUS_PROBE_TIMEOUT = 8

# Max per-tick targeted single-pid persistent-history fetches for own
# awaiting jobs that both the /api/jobs listing and the bounded persistent
# window rolled off (e.g. a server-restart terminal pushed below
# `history_limit`). Bounds fan-out for a pathological awaiting backlog; the
# overflow falls through to the existing lost-detection path.
_AWAITING_BACKFILL_CAP = 25


def _url(base, path):
    return base.rstrip('/') + path


def _extract_model_name(workflow_nodes):
    """Extract model/checkpoint name from workflow node dict.

    Searches for loader/checkpoint nodes and returns the filename
    of the first model input found (without directory prefix).
    """
    for node in workflow_nodes.values():
        if not isinstance(node, dict):
            continue
        ct = node.get('class_type', '')
        # `ct` is a ComfyUI class_type string (e.g. CheckpointLoaderSimple,
        # NunchakuFluxLoader). Substring check uses raw Comfy nomenclature,
        # NOT our nfy_* convention.
        if 'Loader' in ct or 'checkpoint' in ct.lower():
            for k, v in node.get('inputs', {}).items():
                if isinstance(v, str) and ('ckpt' in k or 'model' in k or 'unet' in k):
                    return v.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
    return ''

def _normalise_ts(ts):
    """Convert a timestamp to seconds.  ComfyUI uses milliseconds."""
    if not ts:
        return 0
    if ts > 1e12:
        return ts / 1000.0
    return float(ts)


def _sanitize_output_paths(raw):
    """Filter untrusted output_paths coming back from the server.

    The list originates in our own extra_data at submit time, but the
    server echoes it back over HTTP - a compromised server, a third-party
    client hitting the same endpoint, or in-flight corruption could
    inject malformed entries. Downstream consumers (glob, isdir,
    fromUserText) expect well-formed absolute path strings.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, str):
            _log.warning('Ignored malformed output_path: non-string type=%s',
                         type(entry).__name__)
            continue
        if not entry:
            continue
        if len(entry) > 4096:
            _log.warning('Ignored malformed output_path: too long len=%d',
                         len(entry))
            continue
        if '\x00' in entry:
            _log.warning('Ignored malformed output_path: null byte path=%r',
                         entry[:200])
            continue
        segments = entry.replace('\\', '/').split('/')
        if '..' in segments:
            _log.warning('Ignored malformed output_path: path traversal path=%r',
                         entry[:200])
            continue
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Queue / Status
# ---------------------------------------------------------------------------
def _extract_input_cache_dirs(workflow):
    """Extract parent directories of NukomfyRead `file_path` entries.

    The paths are the ones the REMOTE machine sees (post path_substitution
    applied by inject_input_paths), always forward-slash. Returned as a
    deduplicated list of normalized/casefolded strings, suitable for
    set-matching against a new submit's predicted cache dirs.
    """
    if not isinstance(workflow, dict):
        return []
    seen = set()
    out = []
    import os as _os
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get('class_type') != 'NukomfyRead':
            continue
        fp = node.get('inputs', {}).get('file_path') if isinstance(
            node.get('inputs'), dict) else None
        if not isinstance(fp, str) or not fp:
            continue
        try:
            d = _os.path.dirname(fp).replace('\\', '/').rstrip('/').casefold()
        except Exception:
            continue
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def _parse_queue_item(item):
    """Parse a /queue entry tuple into a usable dict.

    Each queue entry is: [job_number, prompt_id, workflow, extra_data, outputs_to_execute]

    `queue_position = item[0]` is the server-side monotonic submit counter
    (or priority tuple). Captured here so client views can sort by it -
    iterating ComfyUI's pending list yields heap order, which for ties is
    not the FIFO execution order (next-to-run can land mid-list).
    """
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return {}
    queue_position = item[0] if len(item) > 0 else 0
    prompt_id = item[1] if len(item) > 1 else ''
    workflow = item[2] if len(item) > 2 else {}
    extra = item[3] if len(item) > 3 else {}

    if not isinstance(workflow, dict):
        workflow = {}
    if not isinstance(extra, dict):
        extra = {}

    model_name = _extract_model_name(workflow)

    return {
        'prompt_id': prompt_id,
        'queue_position': queue_position,
        # Server-side abort flag written into extra_data by the Suite's
        # /nukomfy/abort (or admin force_abort). Drives the "Aborting…"
        # row state, which thus survives a panel reopen / Nuke restart.
        'nfy_aborting': bool(extra.get('nfy_aborting')),
        'nfy_job_id': extra.get('nfy_job_id', ''),
        'nfy_submitted_by': extra.get('nfy_submitted_by', ''),
        'nfy_submitter_host': extra.get('nfy_submitter_host', ''),
        'create_time': _normalise_ts(extra.get('create_time', 0)),
        'model': model_name,
        'nfy_workflow_name': extra.get('nfy_workflow_name', ''),
        'node_count': len(workflow),
        'nfy_nk_file': extra.get('nfy_nk_file', ''),
        'nfy_node_name': extra.get('nfy_node_name', ''),
        'nfy_frame_range': extra.get('nfy_frame_range', []),
        'nfy_output_paths': _sanitize_output_paths(extra.get('nfy_output_paths', [])),
        'input_cache_dirs': _extract_input_cache_dirs(workflow),
        'nfy_input_ranges': extra.get('nfy_input_ranges', []),
        'nfy_output_ranges': extra.get('nfy_output_ranges', []),
        'nfy_batch_count': extra.get('nfy_batch_count', 1),
        'nfy_batch_index': extra.get('nfy_batch_index', 1),
        # Cross-user enrichment fields
        'nfy_machine_name': extra.get('nfy_machine_name', ''),
        'nfy_machine_url': extra.get('nfy_machine_url', ''),
        'nfy_sent_at': extra.get('nfy_sent_at', ''),
        'nfy_seeds_used': extra.get('nfy_seeds_used', []),
        'nfy_workflow_meta': extra.get('nfy_workflow_meta') or None,
        'nfy_environment': extra.get('nfy_environment') or None,
        'nfy_server_snapshot': extra.get('nfy_server_snapshot') or None,
    }


def check_queue_status(base_url, timeout=_TIMEOUT):
    """Fetch /queue and return `{status, running, pending, running_jobs,
    pending_jobs, error}`. Status is one of idle/rendering/queued/offline.

    `timeout` defaults to _TIMEOUT, safe for the submit-time concurrency
    pre-flight (which must not skip its check on a slow but live machine).
    Refresh callers pass STATUS_PROBE_TIMEOUT, generous enough to avoid
    false-offlining a slow machine; UI responsiveness is decoupled via the
    refresh button's soft deadline."""
    try:
        url = _url(base_url, '/api/queue')
        req = urllib.request.Request(url, headers={'User-Agent': 'Nukomfy'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())

        running = data.get('queue_running', [])
        pending = data.get('queue_pending', [])

        if running:
            status = 'rendering'
        elif pending:
            status = 'queued'
        else:
            status = 'idle'

        return {
            'status': status,
            'running': len(running),
            'pending': len(pending),
            'running_jobs': [_parse_queue_item(j) for j in running],
            'pending_jobs': [_parse_queue_item(j) for j in pending],
            'error': None,
        }
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
            except Exception: pass
        return {
            'status': 'offline',
            'running': 0,
            'pending': 0,
            'running_jobs': [],
            'pending_jobs': [],
            'error': str(e),
        }


def is_reachable(base_url, timeout=_TIMEOUT):
    """True if the ComfyUI server answers a native, Suite-independent
    request. Uses GET /api/prompt (returns only the queue_remaining count),
    the lightest core endpoint, with no dependency on any custom node.
    Used to tell an offline host apart from one that is merely missing a
    custom node."""
    try:
        url = _url(base_url, '/api/prompt')
        req = urllib.request.Request(url, headers={'User-Agent': 'Nukomfy'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
            except Exception: pass
        return False


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------
def post_prompt(base_url, api_workflow, client_id=None, extra_data=None,
                prompt_id=None):
    """POST /prompt. Returns `{prompt_id, number}` or raises RuntimeError.

    `client_id` is the WebSocket session id (per ComfyUI protocol - server
    routes execution events back to the WS connected with the same
    `?clientId=`). It is NOT a user identity and must NOT be confused with
    `nfy_submitted_by`. The user identity travels in `extra_data` via the
    `nfy_submitted_by` / `nfy_submitter_host` fields.

    When `prompt_id` is supplied, the server echoes it back in the response,
    so the client-generated id is the one that appears in the queue listing
    and in the progress broadcast - the bar and the job row share one key.
    """
    url = _url(base_url, '/api/prompt')
    body = {'prompt': api_workflow}
    if client_id:
        body['client_id'] = client_id
    if extra_data:
        body['extra_data'] = extra_data
    if prompt_id:
        body['prompt_id'] = prompt_id

    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'Nukomfy'})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SUBMIT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        error_body = ''
        try:
            error_body = e.read().decode('utf-8', errors='ignore')
            error_data = json.loads(error_body)
            msg = error_data.get('error', {}).get('message', 'Unknown error')
            node_errors = error_data.get('node_errors', {})
            if node_errors:
                details = []
                for node_name, val in node_errors.items():
                    for err in val.get('errors', []):
                        details.append('{}: {} - {}'.format(
                            node_name, err.get('details', ''), err.get('message', '')))
                msg += '\n\n' + '\n'.join(details)
            raise RuntimeError(msg)
        except (json.JSONDecodeError, KeyError):
            raise RuntimeError('HTTP {}: {}'.format(e.code, error_body[:500]))
    except Exception as e:
        raise RuntimeError(str(e))



# ---------------------------------------------------------------------------
# History / Monitoring
# ---------------------------------------------------------------------------
def _get_json(url):
    """GET helper - returns parsed JSON or None."""
    req = urllib.request.Request(url, headers={'User-Agent': 'Nukomfy'})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Drain the error body, else urllib closes with a TCP RST that the
        # server logs as "_call_connection_lost ... WinError 10054" every poll.
        try:
            e.read()
        except Exception:
            pass
        _log.debug('GET %s HTTP %s', fmt_machine(url), e.code)
        return None
    except Exception as e:
        _log.debug('GET %s failed: %s', fmt_machine(url), scrub_url_in_text(str(e), url))
        return None


def _get_json_ex(url):
    """Like `_get_json` but returns a status tuple so callers can tell
    a definitive 404 apart from a network/timeout error.

    Returns:
        ('ok', parsed_json)       - 200 with valid JSON
        ('not_found', None)       - server reachable, responded 404
        ('unreachable', None)     - network error, timeout, DNS, non-JSON,
                                    or any other failure where we can't
                                    trust that the resource doesn't exist
    """
    req = urllib.request.Request(url, headers={'User-Agent': 'Nukomfy'})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return ('ok', json.loads(r.read().decode()))
    except urllib.error.HTTPError as e:
        # Drain the error body, else urllib closes with a TCP RST that the
        # server logs as "_call_connection_lost ... WinError 10054" every poll.
        try:
            e.read()
        except Exception:
            pass
        if e.code == 404:
            return ('not_found', None)
        _log.debug('GET %s HTTP %s', fmt_machine(url), e.code)
        return ('unreachable', None)
    except Exception as e:
        _log.debug('GET %s failed: %s', fmt_machine(url), scrub_url_in_text(str(e), url))
        return ('unreachable', None)


def _parse_job_detail(detail):
    """Parse a single /api/jobs/{id} response into our item dict."""
    if not isinstance(detail, dict):
        return None

    # Timestamps (all native, in ms)
    create_time = _normalise_ts(detail.get('create_time', 0))
    start_time = _normalise_ts(detail.get('execution_start_time', 0))
    end_time = _normalise_ts(detail.get('execution_end_time', 0))
    duration = max(0, end_time - start_time) if start_time and end_time else 0

    # Status - normalise to canonical taxonomy. Prefer `/api/jobs` top-level
    # `status` (canonical JobStatus enum) over `execution_status.status_str`
    # (legacy - for interrupted jobs it reports `error`, which would wrongly
    # map to `failed`). The legacy field is used only as a fallback for very
    # old ComfyUI servers missing the top-level field.
    exec_status = detail.get('execution_status', {}) or {}
    # `exec_status` is the server-side `execution_status` dict - uses
    # ComfyUI's native key `status_str`, NOT our `nfy_*` convention.
    status_str = detail.get('status', '') or exec_status.get('status_str', '')
    status_str = {'success': 'completed', 'error': 'failed',
                  'interrupted': 'cancelled',
                  'in_progress': 'running'}.get(status_str, status_str)
    job_status = detail.get('status', '')

    # Workflow data (extra_data has nfy_submitted_by, workflow_name, ...)
    wf = detail.get('workflow', {}) or {}
    extra = wf.get('extra_data', {}) or {}
    prompt = wf.get('prompt', {}) or {}

    model_name = _extract_model_name(prompt)

    return {
        'prompt_id': detail.get('id', ''),
        'nfy_job_id': extra.get('nfy_job_id', ''),
        'nfy_submitted_by': extra.get('nfy_submitted_by', ''),
        'nfy_submitter_host': extra.get('nfy_submitter_host', ''),
        'nfy_status_str': status_str,
        'completed': job_status == 'completed',
        'create_time': create_time,
        'model': model_name,
        'nfy_workflow_name': extra.get('nfy_workflow_name', ''),
        'node_count': len(prompt),
        'nfy_duration': duration,
        # Server-side fields - read with ComfyUI's native key names,
        # output under our nfy_* convention for downstream consumption.
        'nfy_messages': exec_status.get('messages', []),
        'nfy_execution_error': detail.get('execution_error'),
        'nfy_outputs_count': detail.get('outputs_count', 0),
        'nfy_nk_file': extra.get('nfy_nk_file', ''),
        'nfy_node_name': extra.get('nfy_node_name', ''),
        'nfy_frame_range': extra.get('nfy_frame_range', []),
        'nfy_output_paths': _sanitize_output_paths(extra.get('nfy_output_paths', [])),
        'input_cache_dirs': _extract_input_cache_dirs(prompt),
        'nfy_input_ranges': extra.get('nfy_input_ranges', []),
        'nfy_output_ranges': extra.get('nfy_output_ranges', []),
        'nfy_batch_count': extra.get('nfy_batch_count', 1),
        'nfy_batch_index': extra.get('nfy_batch_index', 1),
        # Cross-user enrichment fields (read from extra_data, populated
        # by submit_panel for ALL submits)
        'nfy_machine_name': extra.get('nfy_machine_name', ''),
        'nfy_machine_url': extra.get('nfy_machine_url', ''),
        'nfy_sent_at': extra.get('nfy_sent_at', ''),
        'nfy_seeds_used': extra.get('nfy_seeds_used', []),
        'nfy_workflow_meta': extra.get('nfy_workflow_meta') or None,
        'nfy_environment': extra.get('nfy_environment') or None,
        'nfy_server_snapshot': extra.get('nfy_server_snapshot') or None,
    }


def fetch_workflow_api(base_url, prompt_id):
    """Fetch ONLY the workflow API JSON (the graph dict posted to
    /prompt) for a given prompt_id, via `/api/jobs/{prompt_id}`.

    Lazy cross-user fallback: when the local BLOB persisted at submit
    time is missing - typically because the job was submitted by another
    user - the Detail dialog uses this to populate the Workflow (API)
    tab + the Submitted parameters value lookups on-demand. Not cached
    in the RenderDataStore; the dialog memoizes fetched graphs in its
    own small per-dialog cache, so the fetch is paid at most once per
    job per dialog lifetime.

    Returns the workflow dict (`{node_id: {class_type, inputs}, ...}`)
    or None when the server responds 404 or can't be reached.
    """
    if not prompt_id:
        return None
    base = base_url.rstrip('/')
    raw = _get_json('{}/api/jobs/{}'.format(
        base, urllib.parse.quote(str(prompt_id), safe='')))
    if not isinstance(raw, dict):
        return None
    wf = raw.get('workflow') or {}
    if not isinstance(wf, dict):
        return None
    prompt = wf.get('prompt')
    return prompt if isinstance(prompt, dict) and prompt else None


def fetch_job_status(base_url, prompt_id):
    """Fetch a single job detail via `/api/jobs/{prompt_id}`, distinguishing
    404 from network failure so the reconciler can freeze definitively-lost
    jobs separately from ones pending retry.

    Returns (kind, item_or_none):
        ('ok', parsed_dict)  - job found on the server
        ('not_found', None)  - server reachable, no such prompt_id
                               (rolled off server history window or
                               server was restarted mid-render)
        ('unreachable', None) - network error; state is unknown, retry
                                later
    """
    if not prompt_id:
        return ('not_found', None)
    base = base_url.rstrip('/')
    status, raw = _get_json_ex('{}/api/jobs/{}'.format(
        base, urllib.parse.quote(str(prompt_id), safe='')))
    if status != 'ok':
        return (status, None)
    item = _parse_job_detail(raw)
    return ('ok', item) if item else ('not_found', None)


def _fetch_history_outputs(base, prompt_id):
    """Return the per-node `outputs` map from ComfyUI core `/history/{id}`.

    Independent of the Suite manager job-history toggle: `/history` is
    core ComfyUI in-memory state, available whenever the job is still in
    the server's recent window. Each node's value carries its `ui` payload
    (e.g. a NukomfyWrite node's `nukomfy_written`). Returns None when the
    job is absent or the response is malformed.
    """
    if not prompt_id:
        return None
    raw = _get_json('{}/history/{}'.format(
        base, urllib.parse.quote(str(prompt_id), safe='')))
    if not isinstance(raw, dict):
        return None
    entry = raw.get(prompt_id)
    if not isinstance(entry, dict):
        return None
    outs = entry.get('outputs')
    return outs if isinstance(outs, dict) else None


def _fetch_awaiting_terminals(base_url, pids, terminal_states):
    """Parallel single-pid persistent fetch for `pids`; return the entries
    the server reports as terminal (non-terminal entries and errors dropped).

    Recovers own awaiting jobs that rolled off both the bounded /api/jobs
    listing and the persistent-history window - notably server-restart
    terminals, whose reason lives only in the manager's persistent store.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not pids:
        return []

    def _one(pid):
        payload = get_persistent_history_one(base_url, pid)
        if not (payload and payload.get('ok')):
            return None
        entry = payload.get('entry')
        if (isinstance(entry, dict)
                and entry.get('nfy_status_str') in terminal_states):
            return entry
        return None

    out = []
    with ThreadPoolExecutor(max_workers=min(len(pids), 6)) as pool:
        for f in as_completed([pool.submit(_one, p) for p in pids]):
            try:
                entry = f.result()
            except Exception as e:
                _log.debug('awaiting backfill fetch failed: %s',
                           scrub_url_in_text(str(e), base_url))
                continue
            if entry:
                out.append(entry)
    return out


def fetch_live_progress(base_url):
    """GET /nukomfy/progress - live {prompt_id: {fraction, tooltip}} snapshot.

    Lets the Render Manager reseed progress bars it holds no live WS value
    for (Nuke restart, a second viewer, a Queue rebuild during a long node
    that emits no progress). Read-only, no auth - the same values already
    broadcast to every WS listener.

    Returns (status, data):
      ('ok', {pid: {...}})  - endpoint answered (data may be empty)
      ('not_found', None)   - reachable but the route is absent (a Suite
                              without the endpoint); caller falls back to
                              the hatched no-data cell
      ('unreachable', None) - network error; caller keeps its prior bars
    """
    status, payload = _get_json_ex(_url(base_url, '/nukomfy/progress'))
    if status == 'ok':
        return ('ok', payload if isinstance(payload, dict) else {})
    return (status, None)


def fetch_all_for_machine(base_url, known_terminal_ids=None,
                          display_limit=None, my_awaiting_ids=None):
    """Single-machine unified fetch for the Render Manager.

    Batches the calls needed by `_RenderDataStore.ingest_machine` into
    one worker run:
      1. `GET /api/queue` - running + pending (via `check_queue_status`)
      2. `GET /api/jobs` - full recent history listing (no server-side
         cap; needed for MyJobs awaiting reconciliation across users)
      3. `GET /api/jobs/{id}` in parallel - detail fetch is **bounded**:
         (a) for non-known pids in the top-`display_limit` of the listing
             (these are the ones that will populate the History sub-table),
             plus
         (b) for any pid in `my_awaiting_ids` regardless of position
             (so MyJobs can reconcile awaiting jobs that fell below the
             display window).
         Pids already in `known_terminal_ids` are skipped - their detail
         is either in the in-memory cache or fully persisted on disk.

    Args:
        base_url: machine URL (e.g. 'http://127.0.0.1:8188').
        known_terminal_ids: set of prompt_ids that already have full
            detail (cache in-memory plus locally-persisted). The worker
            skips detail fetch for these.
        display_limit: max number of non-known terminal pids fetched
            for the History sub-table. Pass None for unbounded fetch
            (default - backwards-compatible). Typical caller passes
            `settings.history_limit`.
        my_awaiting_ids: set of own prompt_ids still awaiting (i.e. in
            local history with `terminal_persisted=False`). These get
            detail fetched even when outside `display_limit` so MyJobs
            can promote them to terminal state. Pass None or empty if
            no awaiting reconciliation is needed.

    Returns dict - superset of `check_queue_status` output:
        {
            'status': 'idle'|'rendering'|'queued'|'offline',
            'running': int, 'pending': int,
            'running_jobs': [...], 'pending_jobs': [...],
            'recent_terminal_ids': [prompt_id, ...],
            'new_terminals': [parsed_detail_dict, ...],
            'error': str | None,
        }

    `recent_terminal_ids` mirrors the full set of terminal prompt_ids
    currently in the server's recent listing - used by the store to prune
    cached entries that have rolled off the server window. `new_terminals`
    is the subset for which detail was just fetched.

    Never raises: all errors are captured into `error` and the dict is
    still well-formed so callers don't have to defensively check types.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    result = check_queue_status(base_url, timeout=STATUS_PROBE_TIMEOUT)
    result['recent_terminal_ids'] = []
    result['new_terminals'] = []

    if result.get('error'):
        return result

    # Live progress snapshot (reseeds bars with no live WS value). Gated on
    # a non-empty running set so idle machines skip the extra GET, and kept
    # best-effort: a failure here must not disturb the queue/listing result.
    # `progress_endpoint_ok` lets the panel tell a Suite that lacks the route
    # (-> fall back to hatched) apart from a transient network error (-> keep
    # the prior bars).
    if result.get('running_jobs'):
        try:
            p_status, p_data = fetch_live_progress(base_url)
            if p_status == 'ok':
                result['live_progress'] = p_data
                result['progress_endpoint_ok'] = True
            elif p_status == 'not_found':
                result['progress_endpoint_ok'] = False
        except Exception:
            pass

    known = set(known_terminal_ids or ())
    awaiting = set(my_awaiting_ids or ())
    base = base_url.rstrip('/')
    listing = _get_json('{}/api/jobs'.format(base))
    if not listing or not isinstance(listing.get('jobs'), list):
        return result

    jobs = listing['jobs']
    terminal_set = {'completed', 'failed', 'cancelled'}
    result['recent_terminal_ids'] = [
        j['id'] for j in jobs
        if isinstance(j, dict)
        and j.get('status') in terminal_set
        and j.get('id')
    ]

    # Capability-gated server-side persistent history fetch. Purely
    # additive: pids already covered by the in-listing detail fetch
    # below take that path literally. The server payload is consumed
    # only for pids the listing has rolled off - other users, entries
    # missing after a server restart or beyond the in-memory window,
    # and our own awaiting jobs that the client must promote to
    # terminal state without a /api/jobs/{id} fallback.
    #
    # Filter rationale:
    #  - `pid in result['recent_terminal_ids']`: skip - the listing
    #    path handles this pid (Pass 1 for History display, or Pass 2
    #    for awaiting reconcile). Capturing here would duplicate the
    #    same entry across `new_terminals` and `persistent_terminals`.
    #  - Status not terminal: skip - the cache contract is recent
    #    terminals only (matches the `recent_terminal_ids` filter on
    #    /api/jobs). Pending/running jobs belong to the live queue path.
    persistent_terminal_states = {'completed', 'failed', 'cancelled'}
    persistent_entries = []
    if has_persistent_history(base_url):
        payload = get_persistent_history(
            base_url, limit=display_limit if display_limit else 10)
        if payload and payload.get('ok'):
            for entry in payload.get('entries') or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get('nfy_status_str') not in persistent_terminal_states:
                    continue
                pid = entry.get('prompt_id')
                if not pid or pid in result['recent_terminal_ids']:
                    continue
                persistent_entries.append(entry)

        # Targeted gap-fill for own awaiting pids that both /api/jobs and the
        # bounded persistent window rolled off (e.g. a server-restart terminal
        # pushed below `history_limit`): recover them via the unbounded
        # single-pid endpoint before the lost-detection freezes them
        # reason-less. Feeds the existing persistent-terminal merge path.
        live_ids = (
            {j.get('prompt_id') for j in (result.get('running_jobs') or [])}
            | {j.get('prompt_id') for j in (result.get('pending_jobs') or [])})
        covered = (set(result['recent_terminal_ids']) | known | live_ids
                   | {e.get('prompt_id') for e in persistent_entries})
        need = [pid for pid in awaiting if pid and pid not in covered]
        if len(need) > _AWAITING_BACKFILL_CAP:
            _log.debug('awaiting backfill capped: %d of %d',
                       _AWAITING_BACKFILL_CAP, len(need))
            need = need[:_AWAITING_BACKFILL_CAP]
        persistent_entries.extend(_fetch_awaiting_terminals(
            base_url, need, persistent_terminal_states))
    result['persistent_terminals'] = persistent_entries

    # Bounded fetch list build - listing is server-ordered by recency.
    # Pass 1: walk the listing, take the first `display_limit` non-known
    # pids for the History sub-table. Pids in `awaiting` are reserved for
    # pass 2 so they don't consume the display budget.
    fetch_set = set()
    fetch_order = []
    display_taken = 0
    for pid in result['recent_terminal_ids']:
        if pid in known or pid in awaiting:
            continue
        if display_limit is None or display_taken < display_limit:
            fetch_set.add(pid)
            fetch_order.append(pid)
            display_taken += 1
        else:
            break  # listing is recency-ordered; later pids are older
    # Pass 2: own awaiting pids regardless of position (MyJobs reconcile).
    for pid in result['recent_terminal_ids']:
        if pid in awaiting and pid not in fetch_set and pid not in known:
            fetch_set.add(pid)
            fetch_order.append(pid)

    if not fetch_order:
        return result

    def _fetch_one(pid):
        item = _parse_job_detail(_get_json('{}/api/jobs/{}'.format(
            base, urllib.parse.quote(str(pid), safe=''))))
        # `/api/jobs/{id}` omits the per-node outputs map (only a scalar
        # count). The core `/history/{id}` endpoint carries it and is
        # independent of the manager persistence toggle - enrich completed
        # jobs with the server-confirmed write outputs.
        if item and item.get('nfy_status_str') == 'completed':
            outs = _fetch_history_outputs(base, pid)
            if outs:
                item['nfy_outputs'] = outs
        return item

    details = []
    with ThreadPoolExecutor(max_workers=min(len(fetch_order), 6)) as pool:
        futures = [pool.submit(_fetch_one, pid) for pid in fetch_order]
        for f in as_completed(futures):
            try:
                item = f.result()
            except Exception as e:
                _log.debug('detail fetch failed: %s', scrub_url_in_text(str(e), base))
                continue
            if item:
                details.append(item)

    result['new_terminals'] = details
    return result


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------
def abort(base_url, prompt_id=None):
    """POST /nukomfy/abort - mark the running job aborting and interrupt it.

    Returns one of three strings:
      'ok'          - the job was running and is now aborting.
      'not_running' - the server reports the prompt is not the running one
                      (HTTP 409): it finished between the caller's snapshot
                      and this POST. Distinct from a machine failure.
      'error'       - the machine was unreachable or returned an error.

    The Suite endpoint sets `nfy_aborting` on the running item - visible to
    every client through /api/queue - before interrupting, so "Aborting…"
    survives a panel reopen and a Nuke restart. Callers must not persist
    `cancelled` locally on this result: the server stays the source of
    truth for the terminal state.
    """
    url = _url(base_url, '/nukomfy/abort')
    body = {}
    if prompt_id:
        body['prompt_id'] = prompt_id
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'Nukomfy',
                 'X-Nukomfy-User': header_user()},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
        except Exception: pass
        if e.code == 409:
            return 'not_running'
        _log.debug('abort %s failed: HTTP %s', fmt_machine(url), e.code)
        return 'error'
    except Exception as e:
        _log.debug('abort %s failed: %s', fmt_machine(url), scrub_url_in_text(str(e), url))
        return 'error'
    # A 2xx means the POST reached the server and the abort was accepted
    # (the Suite marks and interrupts before responding). Parsing the body
    # is best-effort: a 2xx with an unparseable body (e.g. a buffering
    # proxy) must not be misread as an unreachable machine.
    try:
        ok = json.loads(raw).get('ok')
    except (ValueError, AttributeError):
        ok = True  # unparseable 2xx body: the POST was still accepted
    return 'ok' if ok else 'error'


def delete_from_queue(base_url, prompt_ids):
    """POST /api/queue - remove pending items by prompt_id. Returns True on
    2xx, else False. The caller uses the result to confirm the optimistic
    remove (un-greys the row and lets the user retry on failure)."""
    url = _url(base_url, '/api/queue')
    body = json.dumps({'delete': prompt_ids}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'Nukomfy'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            ok = 200 <= r.status < 300
            r.read()  # drain -> clean FIN close, not RST (WinError 10054)
            return ok
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
            except Exception: pass
        _log.debug('delete_from_queue %s failed: %s', fmt_machine(url), scrub_url_in_text(str(e), url))
        return False


_TERMINAL_JOB_STATUSES = frozenset(
    ('completed', 'failed', 'cancelled'))
