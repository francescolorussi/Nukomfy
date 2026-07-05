"""Renders upstream Nuke nodes to disk for ComfyUI consumption.

Two-level cache identity:
  - The cache DIR is keyed by a frame-independent structural key
    (compute_fingerprint): a hash of each upstream node's class + non-default
    functional knobs via writeKnobs, which omits UI-only knobs (position,
    label, tile_color, ...) and serialises animation as curves. Stable across
    the current frame, viewer position and sessions; changes only on a real
    config change (path, grade, transform).
  - Per FRAME the sentinel stores the content op hash (frame_op_hash) plus the
    mtime+size of every source the frame depends on - the structural Read
    sources AND the file it really consumes (metadata-resolved, so a time-remap
    anywhere in the chain is honoured) - so only the frames whose content
    actually changed re-render. Reading a per-frame hash is a cheap validate.

Path layout (user-first, 6 levels under base):
    {base}/{user}/{project}/{workflow}/{input}/{struct_key}/

Sentinel schema v5 (`.nukomfy_input_cache.json`):
    schema, algo, algo_version, nuke_version, path, path_os,
    fingerprint (struct key), frames_state ({frame: {op_hash, src}} where src
    is a list of [mtime_ns, size]: one per structural Read source (expanded at
    the output frame) plus, when known, the file the frame really consumes
    (metadata-resolved, so a time-remap anywhere in the chain is honoured)),
    file_pattern, created_utc, last_used_utc, writing, in_use
"""

import contextlib
import datetime
import json
import logging
import os

import nuke  # type: ignore

from Nukomfy.core.identity import current_user
from Nukomfy.core.settings import settings

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache identity: a frame-independent structural key (names the folder) plus a
# per-frame content hash (drives the surgical reuse decision).
# ---------------------------------------------------------------------------
def compute_fingerprint(root_node):
    """16-hex structural key of *root_node* + upstream chain (names the folder).

    ``writeKnobs(WRITE_NON_DEFAULT_ONLY | TO_SCRIPT)`` omits UI-only knobs and
    serialises animation as curves, so the key is stable across frame, viewer
    and session and changes only on a real config change. Per-frame content is
    validated separately, so an imperfect group only ever spawns an extra
    variant, never a wrong reuse.
    """
    import hashlib
    parts = []
    seen, queue, budget = set(), [root_node], 0
    while queue and budget < 512:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        budget += 1
        try:
            cls = node.Class()
        except Exception:
            cls = '?'
        # Pass-through no-ops (Dot) carry no content; skip them so inserting or
        # removing one does not rotate the cache. Still walk their inputs.
        if cls not in _PASSTHROUGH_NOOPS:
            try:
                body = node.writeKnobs(
                    nuke.WRITE_NON_DEFAULT_ONLY | nuke.TO_SCRIPT)
            except Exception:
                body = ''
            parts.append('{}|{}'.format(cls, body or ''))
        try:
            for i in range(node.maxInputs()):
                queue.append(node.input(i))
        except Exception:
            pass
    blob = '\x01'.join(parts)
    return hashlib.sha1(blob.encode('utf-8', 'replace')).hexdigest()[:16]


# Pass-through nodes (no op hash, emit their input verbatim): skipped in the
# struct key and read through by frame_op_hash. Time-remapping no-ops
# (FrameHold/TimeOffset/Retime) are deliberately NOT listed - their per-frame
# identity is unreadable this way, so frame_op_hash returns None -> re-render.
_PASSTHROUGH_NOOPS = frozenset(('Dot',))


def frame_op_hash(node, frame, view, _depth=0):
    """16-hex per-frame content hash of *node* at *frame*, or None.

    metadata() forces a validate at that frame (no pixel render); the op hash
    for the frame is then read back from the node info. The parsed value is
    accepted only if it also appears in opHashes(), so a parse miss - or a
    future change to the info format - degrades to None (caller renders the
    frame) rather than to a wrong value. Never renders pixels.
    """
    import re
    if node is None or _depth > 32:
        return None
    try:
        node.metadata('input/filename', frame, view)
    except Exception:
        try:
            node.metadata('input/filename', frame)
        except Exception:
            pass
    try:
        info = nuke.showInfo(node)
    except Exception:
        info = ''
    m = re.search(
        r'Op for \{[^}]*\bframe=%d\b[^}]*\}[^\n]*?\bhash=([0-9a-fA-F]+)'
        % int(frame), info)
    if m:
        try:
            h = format(int(m.group(1), 16) & 0xFFFFFFFFFFFFFFFF, '016x')
        except ValueError:
            h = None
        if h:
            try:
                opset = set(format(int(x) & 0xFFFFFFFFFFFFFFFF, '016x')
                            for x in node.opHashes())
            except Exception:
                opset = set()
            if h in opset:
                return h
    # No per-frame op hash here: read through a pass-through no-op, else give up.
    try:
        passthrough = node.Class() in _PASSTHROUGH_NOOPS
    except Exception:
        passthrough = False
    if passthrough:
        try:
            inp = node.input(0)
        except Exception:
            inp = None
        return frame_op_hash(inp, frame, view, _depth + 1)
    return None


def _workflow_scope_name(gizmo_node):
    """Path scope for the cache: the WORKFLOW name (snapshotted on the gizmo),
    not the node name - so clones of the same workflow reuse the same cache
    regardless of their node name. Falls back to the node name if absent."""
    try:
        k = gizmo_node.knob('_nfy_wf_name')
        v = (k.value() or '').strip() if k is not None else ''
    except Exception:
        v = ''
    return v or gizmo_node.name()


def _write_format_hash(write_node):
    """16-hex hash of the Write's on-disk FORMAT config (file_type, compression,
    datatype, ...) - the write template's encoding, which determines the bytes on
    disk. Excludes the output-path knobs (file/proxy/create_directories): they
    point at the cache itself and are set by this writer, not the template."""
    import hashlib
    if write_node is None:
        return hashlib.sha1(b'').hexdigest()[:16]
    try:
        body = write_node.writeKnobs(nuke.WRITE_NON_DEFAULT_ONLY | nuke.TO_SCRIPT)
    except Exception:
        body = ''
    # Excluded because they change between two otherwise-identical Writes:
    # file/proxy (output path, set by this writer), create_directories (forced
    # by this writer), version (the Write's render counter, bumped every write).
    # in_colorspace/out_colorspace are dropped from the writeKnobs pass because
    # their non-default serialisation flips after the first validate (lazy OCIO
    # default), which would rotate the key on the first re-submit. Their VALUE
    # is stable, so the resolved value is folded in explicitly below: the key
    # stays stable across submits AND a genuine colorspace change still
    # invalidates the cache - the Write's colorspace is a byte-determining
    # operation the per-frame op hash cannot see (it hashes a node upstream of
    # the Write).
    _skip = ('file', 'proxy', 'create_directories', 'version',
             'in_colorspace', 'out_colorspace')
    kept = [ln for ln in (body or '').split('\n')
            if ln.strip().split(' ', 1)[0] not in _skip]
    for _kn in ('in_colorspace', 'out_colorspace'):
        _k = write_node.knob(_kn)
        if _k is not None:
            try:
                kept.append('{} {}'.format(_kn, _k.value()))
            except Exception:
                pass
    return hashlib.sha1('\n'.join(kept).encode('utf-8', 'replace')).hexdigest()[:16]


def _mix_fingerprints(*fingerprints):
    """Combine several 16-hex fingerprints into a single 16-hex value."""
    import hashlib
    return hashlib.sha1('|'.join(fingerprints).encode('utf-8')).hexdigest()[:16]


def _fp_path_segment(fingerprint):
    """Path segment derived from a fingerprint. 16 hex chars verbatim,
    or an empty string if the fingerprint is missing."""
    return fingerprint or ''


# ---------------------------------------------------------------------------
# Sentinel state file (schema v5)
# ---------------------------------------------------------------------------
SCHEMA_V5 = 'nukomfy.cache.v5'
_ALGO_NAME = 'nuke_native'
_ALGO_VERSION = 5
_STATE_FILENAME = '.nukomfy_input_cache.json'


def _nuke_version_string():
    """Best-effort Nuke version label for diagnostic field in the sentinel.

    Used only for logs / debugging if Foundry changes the hash algorithm
    in a future major release. Falls back to an empty string outside Nuke.
    """
    try:
        return str(nuke.env.get('NukeVersionString') or '')
    except Exception:
        return ''


def _state_file(output_dir):
    return os.path.join(output_dir, _STATE_FILENAME)


def _now_utc_iso():
    """Current UTC time as ISO8601 string (filesystem-independent age marker)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec='seconds')


def _load_state(output_dir):
    """Read sentinel JSON. Returns None on missing/invalid."""
    import Nukomfy.utils.fs_safe as fs_safe
    p = _state_file(output_dir)
    p_io = fs_safe._long_path(p)
    if os.path.isfile(p_io):
        try:
            with open(p_io, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _atomic_write(path, data):
    """Atomic JSON write with retry on Windows AV/lock contention.

    Raises OSError on permanent failure so the submit pipeline cascades
    the error instead of silently losing the sentinel update.
    """
    import Nukomfy.utils.fs_safe as fs_safe
    tmp = path + '.tmp'
    with open(fs_safe._long_path(tmp), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    if not fs_safe.atomic_replace(tmp, path):
        raise OSError('could not save the input cache state file: {}'.format(path))


def _save_state(output_dir, state):
    """Persist a full sentinel dict atomically."""
    import Nukomfy.utils.fs_safe as fs_safe
    if not fs_safe.makedirs(output_dir, action='save input cache state'):
        return
    _atomic_write(_state_file(output_dir), state)


def _new_sentinel(output_dir, fingerprint, file_pattern, frames_state=None):
    """Build a fresh sentinel dict (schema v5)."""
    from Nukomfy.client.path_substitution import current_os
    canonical = os.path.normpath(os.path.abspath(output_dir))
    return {
        'schema': SCHEMA_V5,
        'algo': _ALGO_NAME,
        'algo_version': _ALGO_VERSION,
        'nuke_version': _nuke_version_string(),
        'path': canonical,
        'path_os': current_os(),
        'fingerprint': fingerprint,
        'frames_state': dict(frames_state or {}),
        'file_pattern': file_pattern,
        'created_utc': _now_utc_iso(),
        'last_used_utc': _now_utc_iso(),
        'writing': None,
        'in_use': [],
    }


def _claim_writing(output_dir, fingerprint, file_pattern):
    """Mark the cache dir as 'currently being written' before render starts.

    Writes a minimal sentinel with `writing={started_utc}` so any concurrent
    submit (or post-crash retry) can detect the in-flight write. Preserves
    `in_use` if a sentinel already exists (legitimate refresh of a partially
    populated cache should not lose the running-job tracking).
    """
    state = _load_state(output_dir)
    if state and state.get('schema') == SCHEMA_V5:
        # Refresh path/fingerprint for partial rewrite, keep in_use.
        state['fingerprint'] = fingerprint
        state['file_pattern'] = file_pattern
        state['writing'] = {'started_utc': _now_utc_iso()}
        # frames_state retained - we'll add to it after render.
    else:
        state = _new_sentinel(output_dir, fingerprint, file_pattern)
        state['writing'] = {'started_utc': _now_utc_iso()}
    _save_state(output_dir, state)
    return state


def _clear_writing(output_dir):
    """Remove the writing marker (success or post-render error path)."""
    state = _load_state(output_dir)
    if not state:
        return
    if state.get('writing') is not None:
        state['writing'] = None
        state['last_used_utc'] = _now_utc_iso()
        _save_state(output_dir, state)


def _touch_last_used(output_dir):
    """Update only last_used_utc. Best-effort."""
    state = _load_state(output_dir)
    if not state:
        return
    state['last_used_utc'] = _now_utc_iso()
    try:
        _save_state(output_dir, state)
    except Exception as e:
        _log.warning(
            'Input cache touch failed for %s: %s. Cache may be purged early '
            'if last_used_utc cannot be refreshed before next TTL run.',
            _state_file(output_dir), e)


def _update_frames_state(output_dir, new_entries):
    """Merge per-frame {op_hash, src} entries into the sentinel."""
    state = _load_state(output_dir)
    if not state:
        return
    fs = state.get('frames_state') or {}
    fs.update({str(k): v for k, v in new_entries.items()})
    state['frames_state'] = fs
    state['last_used_utc'] = _now_utc_iso()
    _save_state(output_dir, state)


def add_in_use_entry(output_dir, persistable_url, machine_name, prompt_id):
    """Append a running-job entry to in_use (called PRE post_prompt).

    `persistable_url` is the URL formatted for at-rest storage (plain
    or obfuscated, via `Machine.to_persistable_url()`). `machine_name`
    is stored alongside so the read path can derive the deobfuscation
    key for hidden URLs.
    """
    state = _load_state(output_dir)
    if not state:
        return
    in_use = list(state.get('in_use') or [])
    in_use.append({
        'machine_url': persistable_url,
        'machine_name': machine_name,
        'prompt_id': prompt_id,
    })
    state['in_use'] = in_use
    _save_state(output_dir, state)


def remove_in_use_entry(output_dir, prompt_id):
    """Remove the entry matching prompt_id (terminal hook OR post fail rollback)."""
    state = _load_state(output_dir)
    if not state:
        return
    in_use = [e for e in (state.get('in_use') or [])
              if e.get('prompt_id') != prompt_id]
    state['in_use'] = in_use
    _save_state(output_dir, state)


def clear_in_use_for_prompt(prompt_id):
    """Sweep the current user's input cache branch and remove any in_use
    entry that matches *prompt_id*. Called from terminal-state hooks
    (job success/fail/cancel) and from delete_entry (job removed from
    local history).

    Best-effort: scans only the current OS user's branch, so the cost is
    bounded by how many cache variants this user has across all gizmos.
    Typical values: 5-50 sentinels. Acceptable.
    """
    if not prompt_id:
        return
    try:
        from Nukomfy.data.input_cache_cleanup import scan
        base = _resolve_base_path()
        for entry in scan(base):
            path = entry.get('path')
            if not path:
                continue
            try:
                state = _load_state(path)
                if not state:
                    continue
                in_use = state.get('in_use') or []
                new_in_use = [e for e in in_use
                              if e.get('prompt_id') != prompt_id]
                if len(new_in_use) != len(in_use):
                    state['in_use'] = new_in_use
                    _save_state(path, state)
            except Exception:
                pass
    except Exception:
        # A per-sentinel failure is swallowed above; this outer guard only
        # trips on a systemic failure (base path unresolved, scan raised).
        # Log it - db._clear_input_cache_in_use can't, our own except hides
        # it from that caller's try.
        _log.exception(
            'Input cache in-use sweep failed for prompt %s', prompt_id)


# ---------------------------------------------------------------------------
# Per-frame validation
# ---------------------------------------------------------------------------
def _stat_file(path):
    """Return (mtime_ns, size) of *path* or None if missing/error.

    `st_mtime_ns` rather than `st_mtime` (float) to keep equality
    comparisons exact across reload cycles.
    """
    import Nukomfy.utils.fs_safe as fs_safe
    try:
        st = os.stat(fs_safe._long_path(path))
        return [int(st.st_mtime_ns), int(st.st_size)]
    except OSError:
        return None


def _expand_source_template(template, frame):
    """Expand every frame placeholder (`#` padding or printf `%[N]d`) in a
    source `file` template for *frame*; unchanged if it has none."""
    import re
    t = re.sub(r'%(\d*)d',
               lambda m: str(frame).zfill(int(m.group(1) or 0)), template)
    return re.sub(r'#+', lambda m: str(frame).zfill(len(m.group(0))), t)


# Knobs naming an on-disk file a node READS. Nuke's op hash keys on the path,
# not the bytes, so an in-place overwrite is invisible to it and only the source
# mtime/size catches it - so we track every such source (Read plus
# OCIOFileTransform, Vectorfield, ...), on every leg of a multi-source input.
_SOURCE_FILE_KNOBS = ('file', 'vfield_file')


def _collect_source_templates(upstream_node):
    """Every file-source template feeding *upstream_node* (deduped, sorted).

    Descends into Group/Gizmo nodes along the chain feeding their output, so a
    Read baked inside a nested group is tracked too - the render pulls it and
    the op hash is blind to a same-path overwrite of its file. If a group's
    output can't be resolved the descent is skipped (never worse than a flat
    walk). Only the output-feeding chain is followed, so disconnected nodes
    inside a group are not spuriously tracked."""
    templates = []
    visited = set()

    def _walk(n, depth):
        if n is None or id(n) in visited or depth > 64:
            return
        visited.add(id(n))
        try:
            cls = n.Class()
        except Exception:
            cls = ''
        if cls not in ('Write', 'NukomfyWrite'):   # a Write's `file` is output
            for kname in _SOURCE_FILE_KNOBS:
                try:
                    k = n.knob(kname)
                    tpl = k.value() if k is not None else ''
                except Exception:
                    tpl = ''
                if tpl:
                    templates.append(tpl)
        # A Group/Gizmo exposes begin()/nodes(); walk its output-feeding chain.
        try:
            is_group = callable(getattr(n, 'begin', None)) \
                and callable(getattr(n, 'nodes', None))
        except Exception:
            is_group = False
        if is_group:
            try:
                n.begin()
                try:
                    _walk(nuke.toNode('Output1'), depth + 1)
                finally:
                    n.end()
            except Exception:
                pass
        try:
            for i in range(n.maxInputs()):
                _walk(n.input(i), depth + 1)
        except Exception:
            pass

    _walk(upstream_node, 0)
    return sorted(set(templates))


def _per_frame_source_states(source_templates, frames, consumed_paths=None):
    """Per-frame signature of the source files a frame depends on.

    Returns {frame_int: [[mtime_ns, size] | None, ...] | None}: one stat per
    structural source template (expanded at the OUTPUT frame), plus - when
    known - one stat for the file the frame REALLY consumes (metadata-resolved
    in _compute_op_hashes, so a time-remap anywhere in the chain is honoured).

    The consumed-path stat is APPENDED, never a replacement: it only ever adds
    a re-render trigger, so a same-path overwrite of a time-remapped source is
    caught without weakening the structural multi-source coverage. An in-place
    overwrite of ANY tracked file flips the frame's signature and forces its
    re-render.
    """
    consumed = consumed_paths or {}
    out = {}
    for f in frames:
        fi = int(f)
        stats = ([_stat_file(_expand_source_template(t, fi))
                  for t in source_templates] if source_templates else [])
        resolved = consumed.get(fi)
        if resolved:
            stats.append(_stat_file(resolved))
        out[fi] = stats or None
    return out


@contextlib.contextmanager
def _root_proxy_off():
    """Force the project (root) proxy off for the block, then restore it.

    Proxy is a project (root) setting - there is no per-render or per-Write
    override (nuke.executeMultiple takes no proxy argument), so Nuke's own render
    dialog toggles nuke.root().setProxy() and restores it in a finally. We do the
    same, using the documented Root.proxy()/setProxy() methods, so the cache is
    keyed and rendered at full resolution regardless of the viewer proxy toggle
    and root is never left changed."""
    root = None
    saved = None
    try:
        root = nuke.root()
        saved = root.proxy()
        root.setProxy(False)
    except Exception:
        root = saved = None
    try:
        yield
    finally:
        try:
            if root is not None and saved is not None:
                root.setProxy(saved)
        except Exception:
            pass


def _consumed_path(node, frame, view):
    """The file *node* actually reads at *frame*, time-remap resolved, or None.

    Nuke's 'input/filename' metadata is the resolved (frame-expanded) path of the
    dominant source pipe; querying it with an explicit frame forces a validate at
    that frame, so a FrameHold/Retime/TimeOffset anywhere upstream is honoured.
    None on a movie / errored Read (no per-frame filename) - the caller then
    relies on the structural source stat for that frame.
    """
    try:
        return node.metadata('input/filename', frame, view) or None
    except Exception:
        try:
            return node.metadata('input/filename', frame) or None
        except Exception:
            return None


def _compute_op_hashes(hash_node, frames, view, gizmo_node, in_gizmo):
    """Per-frame content hash of *hash_node*, plus the file each frame consumes.

    Probing runs inside the gizmo group when the node lives there; no pixels are
    rendered. Returns (op_hashes, consumed_paths):
      op_hashes:      {int frame: 16-hex | None}  (None -> render this frame)
      consumed_paths: {int frame: 'path' | None}  the file the frame actually
        reads, with any time-remap in the chain resolved. None when unreadable
        (movie / errored Read) -> caller falls back to the structural stat.
    """
    ops, paths = {}, {}
    if in_gizmo:
        gizmo_node.begin()
    try:
        for f in frames:
            fi = int(f)
            ops[fi] = frame_op_hash(hash_node, fi, view)
            paths[fi] = _consumed_path(hash_node, fi, view)
    finally:
        if in_gizmo:
            gizmo_node.end()
    return ops, paths


def _plan_cache_write(output_dir, fingerprint, requested_range,
                     source_templates, file_pattern, op_hashes,
                     consumed_paths=None):
    """Decide which frames need rendering for the current submit.

    op_hashes: {frame: 16-hex | None} per-frame content hash (None = unreadable
    -> render that frame). consumed_paths: {frame: path | None} the file each
    frame actually reads (time-remap resolved); its stat is folded into src.
    Returns (status, frames_to_render, frame_states):
        status: 'reuse_full' | 'partial' | 'fresh' | 'recover'
        frames_to_render: sorted list[int] (subset of requested_range)
        frame_states: {frame: {'op_hash', 'src'}} for the frames to render,
                      merged into the sentinel post-render.
    """
    import Nukomfy.utils.fs_safe as fs_safe
    requested = list(range(requested_range[0], requested_range[1] + 1))

    def _states(frames):
        srcs = _per_frame_source_states(source_templates, frames, consumed_paths)
        return {int(f): {'op_hash': op_hashes.get(int(f)), 'src': srcs.get(int(f))}
                for f in frames}

    state = _load_state(output_dir)

    # 1. Crash mid-write: writing != null -> orphan, rewrite all
    if state and state.get('writing') is not None:
        return ('recover', requested, _states(requested))

    # 2. No sentinel / wrong schema / struct-key mismatch -> fresh full render
    if not state or state.get('schema') != SCHEMA_V5 \
            or state.get('fingerprint') != fingerprint:
        return ('fresh', requested, _states(requested))

    # 3. Sentinel matches -> per-frame reuse decision
    frames_state = state.get('frames_state') or {}
    srcs = _per_frame_source_states(source_templates, requested, consumed_paths)
    to_render = []
    for f in requested:
        cached = frames_state.get(str(f))
        live_op = op_hashes.get(int(f))
        cur_src = srcs.get(int(f))
        cache_path = fs_safe._long_path(
            os.path.join(output_dir, _expand_pattern(file_pattern, f)))
        # A truncated / zero-byte file is not a valid frame (mirrors the
        # post-render verification); treat it as missing so it re-renders
        # instead of being served as a stale reuse.
        try:
            on_disk = (os.path.isfile(cache_path)
                       and os.path.getsize(cache_path) > 0)
        except OSError:
            on_disk = False
        if not on_disk:
            to_render.append(f)                    # missing or truncated
        elif not isinstance(cached, dict):
            to_render.append(f)                    # absent / legacy shape
        elif live_op is None or live_op != cached.get('op_hash'):
            to_render.append(f)                    # content changed / unreadable
        elif cur_src is not None and list(cur_src) != list(cached.get('src') or []):
            to_render.append(f)                    # source file overwritten on disk
        # else: reuse

    if not to_render:
        return ('reuse_full', [], {})
    frame_states = {int(f): {'op_hash': op_hashes.get(int(f)), 'src': srcs.get(int(f))}
                    for f in to_render}
    return ('partial', to_render, frame_states)


def _expand_pattern(pattern, frame):
    """Expand a `name_#####.ext` filename pattern for a specific *frame*."""
    import re
    m = re.search(r'#+', pattern)
    if not m:
        return pattern
    pad = len(m.group(0))
    return re.sub(r'#+', str(int(frame)).zfill(pad), pattern, count=1)


def _group_contiguous(frames):
    """[1,2,3,5,6,8] -> [(1,3,1), (5,6,1), (8,8,1)] for executeMultiple."""
    if not frames:
        return []
    sorted_f = sorted(set(int(f) for f in frames))
    segments = [[sorted_f[0], sorted_f[0]]]
    for f in sorted_f[1:]:
        if f == segments[-1][1] + 1:
            segments[-1][1] = f
        else:
            segments.append([f, f])
    return [(s[0], s[1], 1) for s in segments]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def _resolve_base_path():
    """Resolved+normalised input cache base path."""
    from Nukomfy.utils.path_utils import runtime_path
    raw = settings.default_input_cache_path
    return runtime_path(raw, fallback=raw)


def _safe(name):
    """Sanitize a path segment: alphanumerics + _ - only."""
    s = ''.join(c if c.isalnum() or c in '_-' else '_' for c in str(name or ''))
    return s or 'unknown'


def _safe_user():
    """Sanitized current OS username (allowlist [A-Za-z0-9_])."""
    return _safe(current_user())


def _build_output_dir(scope_name, input_name, fp_segment):
    """User-first cache dir layout.

    `{base}/{user}/{project}/{workflow}/{input}/{cache_key}/`

    `{workflow}` (not the node name) so clones of the same workflow share the
    cache. Each user has their own branch - multi-user safety by construction,
    cleanup-on-submit operates within a single branch, Clear All scopes to user.
    """
    from Nukomfy.utils.output_path import nk_file_stem
    base = _resolve_base_path()
    user = _safe_user()
    project = _safe(nk_file_stem())
    scope = _safe(scope_name)
    inp = _safe(input_name)
    return os.path.join(
        base, user, project, scope, inp, fp_segment or 'unknown'
    ).replace('\\', '/')


def _build_input_dir(scope_name, input_name):
    """Parent of all cache-key dirs for one (workflow, input) - used by cleanup."""
    from Nukomfy.utils.output_path import nk_file_stem
    base = _resolve_base_path()
    user = _safe_user()
    project = _safe(nk_file_stem())
    scope = _safe(scope_name)
    inp = _safe(input_name)
    return os.path.join(
        base, user, project, scope, inp
    ).replace('\\', '/')


def preview_input_cache_dirs(gizmo_node, input_ranges, machine=None):
    """Return the cache-key leaf dirs a submit from `gizmo_node` would actually
    REWRITE (plan fresh/partial/recover), machine-substituted and normalized.

    For the input-cache concurrency check. A leaf the submit would only reuse
    (plan reuse_full) is omitted: reusing a cache a running job still reads is
    not a conflict. The plan comes from write_input_cache(dry_run=True), so it
    keys on the exact same identity and per-frame decision as the real write.

    `input_ranges` is the submit's enabled inputs as (param, (first, last)) in
    gizmo-slot order (the list the submit path builds); the index is the gizmo
    input socket. Unconnected inputs are skipped.

    Returns a list of normalized strings (forward-slash, rstripped, casefolded)
    ready for set comparison.
    """
    out = []
    seen = set()
    for input_index, (p, frame_range) in enumerate(input_ranges or []):
        # Advisory check: a failure planning one input (e.g. a Read in the chain
        # in an error state when opHashes validates) must not abort the submit -
        # skip it, the write path and in_use sentinels still protect the cache.
        try:
            # An unconnected input has no cache to write and nothing to
            # conflict with; skip it silently (its "not connected" error is
            # an expected state, not a preview failure to log).
            if gizmo_node.input(input_index) is None:
                continue
            plan = write_input_cache(
                gizmo_node, input_index, p, frame_range, dry_run=True)
        except Exception:
            _log.exception(
                'input cache preview failed for input %d - skipping its '
                'conflict check', input_index)
            continue
        if not plan or plan.get('status') == 'reuse_full':
            continue
        d = plan['output_dir']
        if machine is not None:
            from Nukomfy.client.path_substitution import substitute_path
            d = substitute_path(d, machine)
        norm = d.replace('\\', '/').rstrip('/').casefold()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _build_file_pattern(scope_name, input_name, ext, is_sequence=True):
    """Filename template like 'project_workflow_input_#####.exr'.

    Always uses the `_#####` sequence pattern, even for single-frame
    submits. ComfyUI's NukomfyRead handles single-frame consumption via
    the `load_as_sequence=False, frame=N, frame_mode='single'` flags
    in `inject_input_paths` - the on-disk pattern itself stays uniform.

    This unification is critical for cache reuse across sub-modes: a
    submit with range [5, 10] and a later submit with single frame [7]
    can share the same cache dir because both write/read frames as
    `_00007.png`. Without it, single-mode writes would clobber the
    sequence cache's `file_pattern` and force a full re-render.
    """
    from Nukomfy.utils.output_path import nk_file_stem
    project = _safe(nk_file_stem())
    prefix = '{}_{}_{}'.format(project, _safe(scope_name), _safe(input_name))
    return '{}_#####.{}'.format(prefix, ext)


# ---------------------------------------------------------------------------
# Cleanup of obsolete cache-key dirs for the current (user, workflow, input)
# ---------------------------------------------------------------------------
def _recently_active(state, seconds=600):
    """True if this cache was written or touched within *seconds* - too fresh to
    reclaim as an orphan. A concurrent Nuke session may be mid-write (recent
    `writing` marker), or the session that just wrote it may not have registered
    its in_use claim yet (recent `last_used_utc`). The in_use check only protects
    caches whose running job is already recorded; this guards the windows before
    that."""
    now = datetime.datetime.now(datetime.timezone.utc)

    def _fresh(iso):
        if not iso:
            return False
        try:
            dt = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (now - dt).total_seconds() < seconds

    if _fresh(state.get('last_used_utc')):
        return True
    return _fresh((state.get('writing') or {}).get('started_utc'))


def cleanup_old_variants(scope_name, input_name, current_fp_segment):
    """Delete sibling cache-key dirs for the same (user, project, workflow, input)
    that are no longer in use. Gated by the user setting.

    For each candidate dir:
    - skip current fp (we're about to use it)
    - if `writing != null` -> orphan, eligible for delete (single-Nuke
      assumption: writing==true at the time of THIS submit means a
      previous Nuke crashed)
    - if any in_use entry has machine_url unreachable -> SKIP (keep, TTL
      will eventually purge)
    - if any in_use entry has machine reachable AND job alive -> SKIP
    - else (all entries stale or empty list) -> delete via selective_delete
    """
    if not getattr(settings, 'delete_unused_variants_on_submit', True):
        return

    parent = _build_input_dir(scope_name, input_name)
    import Nukomfy.utils.fs_safe as fs_safe
    parent_io = fs_safe._long_path(parent)
    if not os.path.isdir(parent_io):
        return

    try:
        entries = sorted(os.listdir(parent_io))
    except OSError:
        return

    for name in entries:
        if name == current_fp_segment:
            continue
        leaf = os.path.join(parent, name)
        leaf_io = fs_safe._long_path(leaf)
        if os.path.islink(leaf_io) or not os.path.isdir(leaf_io):
            continue

        state = _load_state(leaf)
        if not state or state.get('schema') \
                not in fs_safe._DELETABLE_INPUT_CACHE_SCHEMAS:
            # Unrecognised sentinel - sentinel-gated deletion protects, skip
            continue

        # Too fresh to reclaim: another Nuke session may be mid-write, or the
        # writer may not have registered its in_use claim yet. Skip; a genuine
        # crash orphan is older and gets cleaned on a later submit (or by TTL).
        if _recently_active(state):
            continue

        # Half-written orphan -> eligible for delete
        if state.get('writing') is not None:
            if not fs_safe.selective_delete_dir(
                    leaf, action='input cache cleanup (orphan)',
                    sentinel_kind='input_cache'):
                _log.warning(
                    'input cache variant not removed, left on disk: %s', leaf)
            continue

        # Iterate in_use entries. URL may be obfuscated for hidden
        # machines - deobfuscate transparently here using the snapshot
        # `machine_name` written alongside.
        from Nukomfy.utils.url_obfuscation import (
            deobfuscate_url, is_obfuscated)
        in_use = state.get('in_use') or []
        any_alive_or_unreachable = False
        for entry in in_use:
            stored = entry.get('machine_url') or ''
            mname = entry.get('machine_name') or ''
            prompt_id = entry.get('prompt_id') or ''
            if not prompt_id:
                continue
            machine_url = (deobfuscate_url(stored, mname)
                           if is_obfuscated(stored) else stored)
            if not machine_url:
                # A recorded claim we cannot resolve to a machine (corrupted or
                # undecodable obfuscation): fail closed - keep it, a job may
                # still be reading. The TTL purge reclaims it eventually.
                any_alive_or_unreachable = True
                break
            if _is_job_alive_or_unreachable(machine_url, prompt_id):
                any_alive_or_unreachable = True
                break

        if any_alive_or_unreachable:
            # Keep, TTL will eventually purge
            continue

        # All entries stale (or in_use empty) -> eligible for delete
        if not fs_safe.selective_delete_dir(
                leaf, action='input cache cleanup (unused variant)',
                sentinel_kind='input_cache'):
            _log.warning(
                'input cache variant not removed, left on disk: %s', leaf)


def _is_job_alive_or_unreachable(machine_url, prompt_id):
    """True if the job is alive on a reachable machine, OR the machine is
    unreachable (conservative - keep the cache).

    False only when the machine is reachable AND the job is definitively
    not there OR is in a terminal state (completed/failed/cancelled).
    """
    try:
        from Nukomfy.client.comfy_api import fetch_job_status
    except Exception:
        return True  # Conservative

    try:
        kind, item = fetch_job_status(machine_url, prompt_id)
    except Exception:
        return True

    if kind == 'unreachable':
        return True
    if kind == 'not_found':
        return False
    # 'ok' - check status
    if isinstance(item, dict):
        status = (item.get('nfy_status_str') or '').lower()
        if status in ('completed', 'failed', 'cancelled', 'error'):
            return False
        return True
    return True


def _input_cache_identity(gizmo_node, input_index, input_param):
    """Frame-independent identity of one gizmo input's cache.

    Returns ``(fingerprint, upstream, feed, write_node, feed_is_noop,
    write_name)`` or None if the input is not connected. The fingerprint keys
    the cache-key leaf dir; the resolved nodes are reused by write_input_cache
    for the per-frame hashing. Shared with preview_input_cache_dirs so the
    concurrency check keys on the exact leaf a write would produce -
    structural only, no render.

    The struct key (and per-frame content hash) key on the node FEEDING the
    Write, not the Write itself (a Write's op state changes after every
    render). No node name in the key, so clones of the same workflow share
    the cache.
    """
    from Nukomfy.gizmos.gizmo_builder import _safe_knob_name

    upstream = gizmo_node.input(input_index)
    if upstream is None:
        return None

    input_name = input_param.get('label', input_param.get('name', 'input'))
    write_name = 'Write_{}'.format(_safe_knob_name(input_name))
    gizmo_node.begin()
    try:
        write_node = nuke.toNode(write_name)
        feed = write_node.input(0) if write_node is not None else None
        feed_struct = compute_fingerprint(feed)
        # Validate before the no-op test: opHashes() is empty both for a genuine
        # pass-through AND for a not-yet-validated node, so without this the
        # feed-vs-upstream hash choice would flip between a fresh submit and a
        # later one and needlessly re-render an unchanged input. metadata()
        # validates without rendering; a genuine pass-through still reports empty.
        if feed is not None and feed.Class() != 'Input':
            try:
                feed.metadata('input/filename')
            except Exception:
                pass
        feed_is_noop = feed is None or feed.Class() == 'Input' \
            or not feed.opHashes()
        write_fmt = _write_format_hash(write_node)
    finally:
        gizmo_node.end()
    fingerprint = _mix_fingerprints(
        feed_struct, compute_fingerprint(upstream), write_fmt)
    return fingerprint, upstream, feed, write_node, feed_is_noop, write_name


# ---------------------------------------------------------------------------
# Main entry point: write_input_cache
# ---------------------------------------------------------------------------
def write_input_cache(gizmo_node, input_index, input_param,
                       frame_range, force=False, dry_run=False):
    """Render upstream content for one gizmo input.

    Pipeline:
      1. struct key (frame-independent) of the feed/upstream chain -> cache dir
      2. cleanup_old_variants for sibling dirs (gated by setting)
      3. per-frame content hashes (cheap validate, no render)
      4. plan: reuse_full / partial / fresh / recover
      5. reuse_full -> touch last_used, return
      6. else claim_writing -> executeMultiple -> store {op_hash, src} -> clear_writing

    Returns dict: output_dir, file_pattern, frame_range, was_cached,
                  full_path, fingerprint (16-hex struct key).

    dry_run=True stops after the plan (step 4) and returns just
    {output_dir, status} with no side effect (no cleanup, no render): the
    input-cache concurrency check uses it to tell a real rewrite from a reuse.
    """
    input_name = input_param.get('label', input_param.get('name', 'input'))

    # Frame-independent identity: the struct key plus the connected nodes the
    # per-frame hashing below reuses. Shared with the preview concurrency
    # check (via _input_cache_identity) so both key on the identical leaf.
    identity = _input_cache_identity(gizmo_node, input_index, input_param)
    if identity is None:
        raise RuntimeError(
            'Input "{}" is not connected to any node.'.format(input_name))
    (fingerprint, upstream, feed, write_node, feed_is_noop,
     write_name) = identity

    # Per-frame content hash: a real-op feed folds the whole chain (in-gizmo +
    # external) per frame; a no-op / Input feed -> hash the external upstream.
    if feed is not None and feed.Class() != 'Input' and not feed_is_noop:
        hash_node, hash_in_gizmo = feed, True
    else:
        hash_node, hash_in_gizmo = upstream, False

    # File sources to watch for in-place overwrites (the op hash is blind to
    # same-path byte changes). Walk the external upstream and the in-gizmo feed
    # chain too - the external walk can't cross the group boundary to reach a
    # source baked into the template. Watch the in-gizmo chain whenever the feed
    # is a real node (the Write renders through it regardless of which node we
    # hash), not only when the feed happens to be the hashed node.
    source_templates = _collect_source_templates(upstream)
    if feed is not None and feed.Class() != 'Input':
        gizmo_node.begin()
        try:
            source_templates = sorted(
                set(source_templates) | set(_collect_source_templates(feed)))
        finally:
            gizmo_node.end()

    scope = _workflow_scope_name(gizmo_node)
    fp_segment = _fp_path_segment(fingerprint)
    output_dir = _build_output_dir(scope, input_name, fp_segment)

    # View for the per-frame content hash (the cache scheme is view-agnostic;
    # pick a deterministic view so the hash is stable across sessions).
    try:
        _views = nuke.views()
        view = _views[0] if _views else 'main'
    except Exception:
        view = 'main'

    # 3. Detect format from internal Write node
    ext = 'exr'
    if write_node:
        ft = write_node.knob('file_type')
        if ft:
            ext = (ft.value() or '').strip() or 'exr'

    # Always use the sequence pattern (see _build_file_pattern docstring
    # for why). ComfyUI side honours `load_as_sequence` regardless.
    file_pattern = _build_file_pattern(scope, input_name, ext)
    full_path = os.path.join(output_dir, file_pattern).replace('\\', '/')

    # 4. Cleanup old cache-key dirs for this (workflow, input) (best-effort, gated)
    if not dry_run:
        try:
            cleanup_old_variants(scope, input_name, fp_segment)
        except Exception:
            _log.exception(
                'cleanup_old_variants failed for %s/%s - proceeding with submit',
                gizmo_node.name(), input_name)

    # 5. Per-frame content hashes (cheap validate, no pixel render), then plan.
    # Root proxy forced off so the reuse decision is taken at the same full
    # resolution the render writes at - a viewer proxy flip must not re-key the
    # cache.
    requested = list(range(frame_range[0], frame_range[1] + 1))
    with _root_proxy_off():
        op_hashes, consumed_paths = _compute_op_hashes(
            hash_node, requested, view, gizmo_node, hash_in_gizmo)
    if force:
        srcs = _per_frame_source_states(source_templates, requested, consumed_paths)
        status = 'fresh'
        frames_to_render = requested
        frame_states = {int(f): {'op_hash': op_hashes.get(int(f)),
                                 'src': srcs.get(int(f))} for f in requested}
    else:
        status, frames_to_render, frame_states = _plan_cache_write(
            output_dir, fingerprint, frame_range, source_templates,
            file_pattern, op_hashes, consumed_paths)

    if dry_run:
        # Concurrency check only: report what the write would do, no side effect.
        return {'output_dir': output_dir, 'status': status}

    if status == 'reuse_full':
        _touch_last_used(output_dir)
        return {
            'output_dir': output_dir,
            'file_pattern': file_pattern,
            'frame_range': list(frame_range),
            'was_cached': True,
            'full_path': full_path,
            'fingerprint': fingerprint,
        }

    # 6. Need to render - set up dir
    import Nukomfy.utils.fs_safe as fs_safe

    if status in ('fresh', 'recover'):
        # Fresh: dir may exist but with wrong fp/no sentinel/missing schema
        # Recover: writing != null, half-written orphan
        # Either way, wipe + recreate
        if os.path.exists(fs_safe._long_path(output_dir)):
            if not fs_safe.selective_delete_dir(
                    output_dir,
                    action='input cache fresh write' if status == 'fresh'
                    else 'input cache crash recovery',
                    sentinel_kind='input_cache'):
                # No sentinel or path-binding mismatch - sentinel-gated
                # deletion refuses. We can't safely take this dir over,
                # abort.
                raise RuntimeError(
                    'Cannot clean the old input cache directory: {}.\n\n'
                    'Delete it manually if you are sure it is safe to '
                    'remove.'.format(output_dir))
        if status == 'recover':
            _log.info(
                'Input cache recovered from incomplete write at %s',
                output_dir)

    if not fs_safe.makedirs(output_dir, action='input cache render'):
        raise RuntimeError(
            'Cannot create input cache directory: {}'.format(output_dir))

    # Claim writing BEFORE executing the Write node, so a crash mid-render
    # leaves a sentinel the next attempt can recognise.
    _claim_writing(output_dir, fingerprint, file_pattern)

    if write_node is None:
        _clear_writing(output_dir)
        raise RuntimeError(
            'Write node "{}" not found inside gizmo "{}".'.format(
                write_name, gizmo_node.name()))

    # Render only the frames that need it (incremental), with the root proxy
    # forced off (via _root_proxy_off) so the cache is always full resolution
    # regardless of the user's current proxy toggle.
    gizmo_node.begin()
    orig_path = None
    try:
        # The orig_path read lives inside the try so a failure here still
        # reaches the finally that owns gizmo_node.end() - otherwise begin()
        # would leak the group context.
        orig_path = write_node.knob('file').value()
        write_node.knob('file').setValue(full_path)
        write_node.knob('create_directories').setValue(True)

        segments = _group_contiguous(frames_to_render)
        if not segments:
            # Defensive: shouldn't happen (status != reuse_full means
            # frames_to_render is non-empty)
            pass
        else:
            with _root_proxy_off():
                try:
                    # Single executeMultiple call when API is available
                    nuke.executeMultiple((write_node,), segments)
                except (AttributeError, TypeError):
                    # Older Nuke: fallback to per-segment execute
                    for first, last, incr in segments:
                        nuke.execute(write_node, first, last, incr)
    except Exception:
        # Render failed - clear the writing marker so next attempt can
        # detect the crash and recover. Sentinel data already on disk
        # from _claim_writing. Path restore and gizmo_node.end() are owned
        # by the finally below; a second end() here would pop one group
        # level too many.
        try:
            _clear_writing(output_dir)
        except Exception:
            pass
        raise
    finally:
        try:
            if orig_path is not None:
                write_node.knob('file').setValue(orig_path)
        except Exception:
            pass
        gizmo_node.end()

    # Verify rendered files exist (per-frame check on the frames we
    # asked to render)
    missing = []
    for f in frames_to_render:
        cache_file = _expand_pattern(file_pattern, f)
        cp = fs_safe._long_path(os.path.join(output_dir, cache_file))
        # A truncated / zero-byte file (e.g. disk full where execute did not
        # raise) must count as missing, else it is recorded in frames_state and
        # later served as a valid reuse.
        try:
            present = os.path.isfile(cp) and os.path.getsize(cp) > 0
        except OSError:
            present = False
        if not present:
            missing.append(f)
    if missing:
        # Clear writing so next attempt can re-render. Caller sees error.
        _clear_writing(output_dir)
        raise RuntimeError(
            'Render incomplete: {} of {} frames missing for "{}".\n\n'
            'First missing: {}'.format(
                len(missing), len(frames_to_render), file_pattern,
                missing[0] if missing else '?'))

    # Update sentinel: merge per-frame {op_hash, src} for the rendered frames.
    _update_frames_state(output_dir, frame_states)
    _clear_writing(output_dir)

    return {
        'output_dir': output_dir,
        'file_pattern': file_pattern,
        'frame_range': list(frame_range),
        'was_cached': False,
        'full_path': full_path,
        'fingerprint': fingerprint,
    }
