"""System capture helpers.

Collects diagnostic snapshot data at submit time:
- Submitter side: Nuke version, Nukomfy plugin version, OS
- Server side: system_stats snapshot (OS, ComfyUI version, GPU, RAM)
- Render side: input cache directory (where .nk + cache files are
  written) - only when an actual cache is in use.
- Write templates: per-input mapping of (input -> template name + scope).

All getters fail soft and return None - callers pass the result
directly to `record_submit(**capture)` which accepts None for every
optional field.
"""

import logging
import os
import platform

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Submitter-side capture (no network)
# ---------------------------------------------------------------------------

def get_nuke_version():
    """Return Nuke version string (e.g. '16.0v8'). None if Nuke not loaded."""
    try:
        import nuke  # type: ignore
        return getattr(nuke, 'NUKE_VERSION_STRING', None)
    except Exception:
        return None


def get_nukomfy_version():
    """Return the Nukomfy plugin version string."""
    try:
        from Nukomfy.version import __version__
        return __version__
    except Exception:
        return None


def get_os_submitter():
    """Return display OS name (Windows/macOS/Linux) for the submitting host.

    Normalised at capture so both Detail renderers (Nuke + Suite web)
    print the stored value verbatim - the single source of truth lives
    here, not in either renderer.
    """
    try:
        from Nukomfy.client.machines import os_display_name
        return os_display_name(platform.platform()) or None
    except Exception:
        return None


def extract_write_templates(gizmo_params, workflow_path):
    """Resolve which `.nk` write templates were used per gizmo input.

    Returns a list of dicts ``[{'input': name, 'template': tpl_name,
    'scope': 'workflow'|'global'}, ...]``. Empty list when no enabled
    input declared a write template (workflow used a generic Write
    node). An entry gains ``'missing': True`` when the configured
    template was not found at gizmo build time and a default Write node
    was used instead (recorded on the gizmo by ``_build_internal_graph``).

    Scope honours the user's explicit choice in the Workflow Creator
    (``write_template_source``). When that field is absent, scope is
    derived by checking whether the template file exists under
    ``<workflow_dir>/write_templates/<tpl_name>``.
    """
    if not gizmo_params:
        return []
    workflow_dir = (os.path.dirname(workflow_path)
                    if workflow_path else None)
    out = []
    for p in gizmo_params:
        if p.get('role') != 'input':
            continue
        if not p.get('enabled', True):
            continue
        tpl = p.get('write_template') or ''
        if not tpl:
            continue
        explicit_src = (p.get('write_template_source') or '').strip()
        if explicit_src in ('workflow', 'global'):
            scope = explicit_src
        else:
            scope = 'global'
            if workflow_dir:
                try:
                    wf_tpl = os.path.join(
                        workflow_dir, 'write_templates', tpl)
                    if os.path.isfile(wf_tpl):
                        scope = 'workflow'
                except (OSError, TypeError):
                    pass
        entry = {
            'input': p.get('label') or p.get('name') or '',
            'template': tpl,
            'scope': scope,
        }
        if p.get('write_template_missing'):
            entry['missing'] = True
        out.append(entry)
    return out


def get_input_cache(write_results=None):
    """Return the input cache directories used by this submit.

    The Nukomfy cache holds the rendered .nk + read sequences referenced
    by the workflow's NukomfyRead nodes. Returns the deduplicated list of
    `output_dir` values across every write result - one entry per gizmo
    input that wrote a cache. Returns None when no cache was written
    (e.g. workflow without NukomfyRead nodes), so the field doesn't appear
    in the Detail tab for such jobs.
    """
    if not write_results:
        return None
    paths = []
    seen = set()
    for wr in write_results:
        if isinstance(wr, dict):
            output_dir = wr.get('output_dir')
            if output_dir and output_dir not in seen:
                paths.append(output_dir)
                seen.add(output_dir)
    return paths or None


# ---------------------------------------------------------------------------
# Server-side capture (HTTP, may block briefly)
# ---------------------------------------------------------------------------

def fetch_machine_snapshot(machine, timeout=2.0):
    """Fetch a system_stats snapshot from the target machine at submit time.

    Returns `(stats_dict, server_version_str)` or `(None, None)` on
    failure / timeout. `stats_dict` is the raw `/system_stats` payload
    structure (os, comfyui_ver, python_ver, gpu, vram, ram) - same
    shape `machines.check_machine` already produces.

    This call blocks the submit thread by up to `timeout` seconds. Keep
    short (default 2s) so a slow/unresponsive server never stalls the
    actual job submission. Caller's POST `/api/prompt` follows after.
    """
    if machine is None:
        return None, None
    try:
        import urllib.request
        import json as _json
        url = machine.url.rstrip('/') + '/api/system_stats'
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Nukomfy'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read().decode())
        sys_info = data.get('system', {}) or {}
        devices = data.get('devices', []) or []
        gpu_name = ''
        vram_total = 0
        if devices:
            d = devices[0]
            gpu_name = d.get('name', '') or ''
            vram_total = d.get('vram_total', 0) or 0
        # OS + GPU normalised here (not in the renderers) so the persisted
        # snapshot is already display-ready - same convention check_machine
        # already follows for the Machines panel.
        from Nukomfy.client.machines import os_display_name, _clean_gpu_name
        snapshot = {
            'os': os_display_name(sys_info.get('os', '')),
            'comfyui_version': sys_info.get('comfyui_version', ''),
            'python_version': sys_info.get('python_version', ''),
            'gpu': _clean_gpu_name(gpu_name) if gpu_name else '',
            'vram_total': vram_total,
            'ram_total': sys_info.get('ram_total', 0) or 0,
        }
        return snapshot, snapshot['comfyui_version'] or None
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
            except Exception: pass
        from Nukomfy.utils.log_format import fmt_machine
        from Nukomfy.utils.url_obfuscation import scrub_url_in_text
        url = getattr(machine, 'url', None)
        mname = getattr(machine, 'name', None)
        _log.debug('fetch_machine_snapshot failed for %s: %s',
                   fmt_machine(url, mname), scrub_url_in_text(str(e), url))
        return None, None


def collect_submit_capture(machine, submit_wf, write_results=None,
                           snapshot_timeout=2.0):
    """One-shot collector: returns dict of kwargs ready for record_submit.

    Convenience wrapper around the individual getters. Every value is
    optional - failures degrade gracefully to None and are filtered out
    so the caller can splat the dict via `**capture` cleanly.
    """
    stats, server_ver = fetch_machine_snapshot(
        machine, timeout=snapshot_timeout)
    # Keys here MUST match the Python kwarg names of `db.record_submit`
    # (un-prefixed, ergonomic). The DB layer stores them in `nfy_*`
    # columns internally; the prefix only appears in the dict returned
    # by `get_history` and on the HTTP wire in `extra_data`.
    capture = {
        'system_stats': stats,
        'server_version': server_ver,
        'os_submitter': get_os_submitter(),
        'nuke_version': get_nuke_version(),
        'nukomfy_version': get_nukomfy_version(),
        'input_cache': get_input_cache(write_results),
    }
    return {k: v for k, v in capture.items() if v is not None}
