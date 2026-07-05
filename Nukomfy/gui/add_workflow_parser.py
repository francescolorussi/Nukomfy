"""Parses and enriches ComfyUI workflow JSON.

Parsing of ComfyUI workflow JSON (UI/API formats), enrichment via
/object_info HTTP fetch, plus the QThread worker that orchestrates
parse + enrich in background.

Internal module - public API surfaced via add_workflow.py.
"""

import json
import logging
import os
import re

from Nukomfy.utils.qt_compat import QtCore
from Nukomfy.workflows.workflow_converter import _VIRTUAL_NODE_TYPES

_log = logging.getLogger(__name__)

# Grace window after the first server replies, to let other fast servers
# answer before proceeding.
_FAST_SERVER_GRACE_S = 0.2
# A widget whose max_value reaches this huge range is treated as a seed.
_SEED_HUGE_RANGE_MIN = 2 ** 32


# NukomfyRead (ComfyUI) supports image sequences only - video containers
# would load as a single clip and break frame-indexed submission.
_TEMPLATE_IMAGE_FORMATS = frozenset({
    'exr', 'tiff', 'tif', 'png', 'jpg', 'jpeg',
    'dpx', 'hdr', 'tga', 'bmp', 'webp',
})

# file_type knob line inside a Write block body. Anchored to a knob line
# start so the word can't be matched inside another knob's quoted value.
_FILE_TYPE_RE = re.compile(r'(?m)^\s*file_type\s+(\S+)')


def _is_ident_char(ch):
    return ch.isalnum() or ch == '_'


def _ident_before(content, brace_idx):
    """Return (type_name, at_line_start) for the identifier preceding `{`.

    at_line_start is True only when the identifier begins at column 0 (the
    char before it is a newline or the start of the file). Nuke writes every
    top-level node declaration at column 0 and indents the children of a
    Group / expanded gizmo, so this flag tells a top-level node apart from a
    group child - and from a `clone $x Write {` prefix, whose type token is
    not at column 0.
    """
    j = brace_idx - 1
    while j >= 0 and content[j] in ' \t':
        j -= 1
    end = j + 1
    while j >= 0 and _is_ident_char(content[j]):
        j -= 1
    return content[j + 1:end], (j < 0 or content[j] == '\n')


def _parse_nk_blocks(content):
    """List of (type_name, body) for each TOP-LEVEL node block in a .nk.

    Top-level means the `Type {` declaration sits at column 0. Nuke indents
    the children of a Group / expanded gizmo (and writes `end_group` at
    column 0), so counting only column-0 declarations skips group internals
    without pairing `Group` with `end_group` - robust even when a group has
    no trailing end_group (e.g. a published LiveGroup) and against a
    `clone $x Write {` reference. Quote-, brace-depth- and backslash-aware
    so braces or keywords inside a knob value (TCL on `file`, animation
    curves, an `Input {` typed into a label) neither truncate a block nor
    fake one.
    """
    blocks = []
    depth = 0          # brace nesting inside the current node block
    in_str = False
    record = False     # is the open block a column-0 (top-level) node?
    type_name = None
    body_start = None
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c == '\\':                       # backslash escapes the next char
            i += 2
            continue
        if in_str:
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == '{':
            if depth == 0:
                type_name, record = _ident_before(content, i)
                body_start = i + 1
            depth += 1
        elif c == '}':
            if depth > 0:
                depth -= 1
                if depth == 0:
                    if record:
                        blocks.append((type_name, content[body_start:i]))
                    record = False
                    type_name = None
                    body_start = None
        i += 1
    return blocks


def _validate_nk_template_write_nodes(nk_path):
    """Return (ok: bool, reason: str) for a Write template file.

    A valid template is a self-contained chain delimited by exactly one
    Input node (the entry the gizmo feeds) and exactly one Write node
    (the terminal that caches the frames). Nodes between them (LUT,
    grade, OCIOFileTransform, ...) are recreated 1:1 at gizmo build.
    The Write's file_type must be in the image-sequence whitelist;
    video formats (mov/mp4/mxf/...) would break NukomfyRead on the
    ComfyUI side. Structural checks (Input->Write reachable, no broken
    external references) run at build time on the real pasted nodes.
    """
    if not nk_path or not os.path.isfile(nk_path):
        return False, 'File not found'
    try:
        with open(nk_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return False, 'Read error: {}'.format(e)

    blocks = _parse_nk_blocks(content)
    n_in = sum(1 for t, _ in blocks if t == 'Input')
    if n_in != 1:
        return False, 'Expected exactly 1 Input node, found {}'.format(n_in)

    writes = [body for t, body in blocks if t == 'Write']
    if len(writes) != 1:
        return False, 'Expected exactly 1 Write node, found {}'.format(len(writes))

    m = _FILE_TYPE_RE.search(writes[0])
    if not m:
        return False, 'Missing file_type in Write node'
    ft = m.group(1).strip().strip('"').lower()
    if ft not in _TEMPLATE_IMAGE_FORMATS:
        return False, 'Unsupported format: {} (only image sequences allowed)'.format(ft)

    return True, ''


def _write_templates_dir():
    """Return the path to the write templates folder."""
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_root, 'write_templates')


def _scan_write_templates(directory):
    """Return list of valid template filenames from a directory."""
    if not directory or not os.path.isdir(directory):
        return []
    result = []
    for f in sorted(os.listdir(directory)):
        if not f.lower().endswith('.nk'):
            continue
        full = os.path.join(directory, f)
        ok, reason = _validate_nk_template_write_nodes(full)
        if not ok:
            _log.warning('Ignored invalid write template %r: %s', f, reason)
            continue
        result.append(f)
    return result


def _list_write_templates(workflow_dir=None):
    """List templates from global and workflow-local dirs.

    Returns list of (display_name, filename, source) tuples.
    source is 'global' or 'workflow'.
    """
    result = []
    # Workflow-local templates first
    if workflow_dir:
        wf_tpl_dir = os.path.join(workflow_dir, 'write_templates')
        for f in _scan_write_templates(wf_tpl_dir):
            name = os.path.splitext(f)[0]
            result.append(('{} (workflow)'.format(name), f, 'workflow'))
    # Global templates
    for f in _scan_write_templates(_write_templates_dir()):
        name = os.path.splitext(f)[0]
        result.append(('{} (global)'.format(name), f, 'global'))
    return result


# ---------------------------------------------------------------------------
# Parsing App JSON -> list of exposed parameters
# ---------------------------------------------------------------------------
_FILE_PARAM_NAMES = {'filepath', 'file_path', 'directory', 'file'}

# Widgets managed automatically by the submit pipeline for
# NukomfyRead / NukomfyWrite nodes. These are never exposed in the
# gizmo - the Submit panel drives the frame range and the submit
# code injects file paths, frame_start, etc. file_path itself is
# routed separately via _AUTO_FILE_PATH_NODES (role=input/output).
_AUTO_MANAGED_WIDGETS = {
    'NukomfyRead': {
        'read_mode',   # forced to 'Custom Range' so Submit panel drives the range
        'first_frame',
        'last_frame',
    },
    'NukomfyWrite': {
        'frame_start',
        'create_directories',  # forced to True
    },
}

_SEED_CONTROLS = {'fixed', 'increment', 'decrement', 'randomize'}
_SEED_SEP_RE = re.compile(r'[^a-z0-9]+')


def _seed_name_has_token(name):
    # Python \b treats '_' as a word char, so \bseed\b would miss 'noise_seed'.
    return 'seed' in _SEED_SEP_RE.split((name or '').lower())

# Node types where file_path is always auto-detected even if not exposed
_AUTO_FILE_PATH_NODES = {
    'NukomfyRead':  'input',
    'NukomfyWrite': 'output',
}

# Node types provided by the Nukomfy Suite custom pack
# (https://github.com/francescolorussi/ComfyUI-Nukomfy-Suite).
# A workflow must contain at least one of these to be loadable,
# since Nukomfy's submit pipeline is built around them.
_NUKOMFY_NODE_TYPES = {'NukomfyRead', 'NukomfyWrite'}

# Sentinels passed through _ParamWorker.finished's error_msg field
# so _on_params_fetched can show the dedicated rich-text dialog
# instead of the generic plain-text warning.
_UNSUPPORTED_WORKFLOW_SENTINEL = '__UNSUPPORTED_WORKFLOW__'
_NUKOMFY_SUITE_NOT_INSTALLED_SENTINEL = '__NUKOMFY_SUITE_NOT_INSTALLED__'
_NO_NUKOMFY_WRITE_NODE_SENTINEL = '__NO_NUKOMFY_WRITE_NODE__'
_NO_NUKOMFY_WRITE_OUTPUT_SENTINEL = '__NO_NUKOMFY_WRITE_OUTPUT__'
def _build_flat_nodes_index(workflow_json):
    """Return a flat {id -> node} index across top-level nodes and every
    subgraph in definitions.subgraphs. Each node is stored under both its
    native id and str(id), because linearData entries can reference ids as
    strings while node dicts declare them as ints."""
    idx = {}

    def _add(nodes):
        for n in nodes or []:
            nid = n.get('id')
            if nid is None:
                continue
            idx[nid] = n
            idx[str(nid)] = n

    _add(workflow_json.get('nodes', []))
    for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []:
        _add(sg.get('nodes', []))
    return idx


def _classify_workflow_json(data):
    """Classify a parsed JSON object as a ComfyUI workflow.

    Returns one of:
      'ui'      - valid UI-format workflow (has nodes + links lists)
      'api'     - API-format workflow (flat dict of {id: {class_type, inputs}})
      'invalid' - JSON ok but not a ComfyUI workflow
    """
    if not isinstance(data, dict):
        return 'invalid'
    if isinstance(data.get('nodes'), list) and isinstance(data.get('links'), list):
        return 'ui'
    values = list(data.values())
    if values and all(
            isinstance(v, dict) and isinstance(v.get('class_type'), str)
            for v in values):
        return 'api'
    return 'invalid'


def _build_api_id_map(workflow_json):
    """Map UI-format internal node ids to the ids ComfyUI uses in API format.

    Top-level nodes map to ``str(id)``. Nodes inside a subgraph instance ``S``
    are inlined as ``"{S}:{internal_id}"``. Nested subgraphs are not supported
    - only one level of flattening.

    Returns ``{id_or_str: api_id_string}``. Both int and str keys are stored
    so callers don't need to normalise.
    """
    api_map = {}
    subgraphs_by_uuid = {
        sg.get('id'): sg
        for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []
        if sg.get('id')
    }
    for n in workflow_json.get('nodes', []) or []:
        nid = n.get('id')
        if nid is None:
            continue
        ntype = n.get('type', '')
        sg = subgraphs_by_uuid.get(ntype)
        if sg is None:
            api_map[nid] = str(nid)
            api_map[str(nid)] = str(nid)
        else:
            for inner in sg.get('nodes', []) or []:
                iid = inner.get('id')
                if iid is None:
                    continue
                api_id = '{}:{}'.format(nid, iid)
                api_map[iid] = api_id
                api_map[str(iid)] = api_id
    return api_map


def _build_subgraph_boundary_labels(workflow_json):
    """Map each subgraph-inner widget to the custom name of the subgraph
    input slot that drives it.

    When a workflow packs nodes into a subgraph and exposes an inner widget,
    the user can rename that boundary slot - an inner PrimitiveBoolean `value`
    surfaced as `enable_turbo_mode`, a CLIPTextEncode `text` surfaced as
    `prompt`. That rename lives on the subgraph instance boundary, but App
    Builder's linearData points straight at the inner `[node, widget]`, so the
    name is otherwise lost (the linearData branch of `_parse_app_inputs` would
    fall back to the raw widget name). Resolved here via the same
    linkIds -> internal link -> target walk the converter uses to inline
    subgraphs, then consumed as the default Gizmo Label so the two branches of
    `_parse_app_inputs` agree (the subgraphs branch already honours the slot
    label).

    Returns ``{(api_id, widget_name): label}`` keyed by the API-format inner id
    (`"outer:inner"`), so the caller can look it up with the api_id it already
    holds. Only slots with a custom label / localized_name are included, so a
    miss means "no rename - keep the widget name". One level of nesting,
    matching `_build_api_id_map`.
    """
    defs = {
        sg.get('id'): sg
        for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []
        if sg.get('id')
    }
    labels = {}
    for node in workflow_json.get('nodes', []) or []:
        sg = defs.get(node.get('type', ''))
        if sg is None:
            continue
        outer = node.get('id')
        inner_nodes = {n.get('id'): n for n in sg.get('nodes', []) or []}
        links_by_id = {
            lnk['id']: lnk for lnk in sg.get('links', []) or []
            if isinstance(lnk, dict) and 'id' in lnk
        }
        for inp in sg.get('inputs', []) or []:
            label = inp.get('label') or inp.get('localized_name')
            if not label:
                continue
            for link_id in inp.get('linkIds', []) or []:
                lnk = links_by_id.get(link_id)
                if not lnk:
                    continue
                tid = lnk.get('target_id')
                tslot = lnk.get('target_slot')
                inner = inner_nodes.get(tid)
                if inner is None:
                    continue
                inner_inputs = inner.get('inputs') or []
                if not isinstance(tslot, int) or not (0 <= tslot < len(inner_inputs)):
                    continue
                wname = inner_inputs[tslot].get('name')
                if wname:
                    labels[('{}:{}'.format(outer, tid), wname)] = label
    return labels


def _workflow_has_nukomfy_nodes(workflow_json):
    """True if the workflow contains at least one NukomfyRead/NukomfyWrite,
    searching top-level nodes and every subgraph."""
    def _scan(nodes):
        for node in nodes or []:
            if node.get('type', '') in _NUKOMFY_NODE_TYPES:
                return True
        return False

    if _scan(workflow_json.get('nodes', [])):
        return True
    for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []:
        if _scan(sg.get('nodes', [])):
            return True
    return False


def _floating_nukomfy_read_ids(workflow_json, api_id_map):
    """Return the API ids of NukomfyRead nodes whose IMAGE output is not
    connected to anything downstream. A floating NukomfyRead is irrelevant
    to the workflow's image graph even when its widgets are exposed in
    the App Builder - exposing it would produce orphan knobs with no
    runtime effect."""
    floating = set()

    def _scan(nodes):
        for n in nodes or []:
            if n.get('type') != 'NukomfyRead':
                continue
            outs = n.get('outputs') or []
            has_link = any(
                isinstance(o, dict) and o.get('links')
                for o in outs
            )
            if not has_link:
                nid = n.get('id')
                if nid is not None:
                    floating.add(api_id_map.get(nid, str(nid)))

    _scan(workflow_json.get('nodes', []))
    for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []:
        _scan(sg.get('nodes', []))
    return floating


def _node_has_any_connection(node):
    """True if node has at least 1 input link OR 1 output link in its scope.

    Workflow JSON stores connectivity inline on each node:
      - node['outputs'][slot]['links'] is a list of link_ids (possibly empty/null)
      - node['inputs'][slot]['link'] is a single link_id or null

    Connection check is symmetric: a link to/from a subgraph IO boundary
    slot still appears in the subgraph's links array, so this catches
    nodes that connect ONLY to the boundary correctly.
    """
    for out in node.get('outputs', []) or []:
        if isinstance(out, dict) and out.get('links'):
            return True
    for inp in node.get('inputs', []) or []:
        if isinstance(inp, dict) and inp.get('link') is not None:
            return True
    return False


def _live_node_ids_for_workflow(workflow_json, info_cache):
    """Reachability-from-output live set for a UI workflow.

    Resolves the workflow to API format (subgraphs / reroutes / get-set
    inlined) and returns the set of api ids that reach an output node,
    matching what ComfyUI actually executes. Returns None when it cannot
    be computed (converter failure or no object_info) so callers fall
    back to the structural orphan check.
    """
    if not info_cache:
        return None
    try:
        from Nukomfy.workflows.workflow_converter import (
            ui_to_api, live_api_node_ids)
        # Structural pass on a partial /object_info cache (only param-bearing
        # nodes are fetched), so unmapped non-param nodes are expected here.
        api = ui_to_api(workflow_json, info_cache, warn_unmapped=False)
        return live_api_node_ids(api, info_cache)
    except Exception:
        return None


def _classify_node_states(workflow_json, api_id_map, info_cache=None):
    """Classify nodes by their runtime/connection state.

    Returns {api_id -> 'bypassed' | 'muted' | 'disconnected'}.
    Active nodes are not in the map.

    'disconnected' means the node does not reach any output node in the
    resolved graph, so ComfyUI would prune it at execution and its
    widgets have no effect. Computed from `info_cache` (needs the
    `output_node` flag); it catches both nodes the converter drops AND
    nodes that survive conversion but feed only a dead branch. When
    `info_cache` is unavailable the check degrades to a structural
    orphan test (no input and no output link at all).

    Walks top-level + every subgraph in definitions.subgraphs. Excludes
    virtual node types (Reroute, SetNode, GetNode, PrimitiveNode, Note,
    MarkdownNote) - they are wiring/utility nodes, not workflow steps.

    Node modes (Comfy): 0 = ALWAYS, 2 = NEVER (muted), 4 = BYPASS.
    Bypass/mute take precedence over disconnected: an explicit user
    state change is more informative than the structural label. All
    three states get the same grey-and-disable treatment in the
    Workflow Creator tables (the Enabled checkbox is forced off).
    """
    state = {}
    live = _live_node_ids_for_workflow(workflow_json, info_cache)

    def _scan(nodes):
        for n in nodes or []:
            nid = n.get('id')
            if nid is None:
                continue
            api_id = api_id_map.get(nid, str(nid))
            mode = n.get('mode', 0) or 0
            if mode == 4:
                state[api_id] = 'bypassed'
                continue
            if mode == 2:
                state[api_id] = 'muted'
                continue
            ntype = n.get('type', '')
            if ntype in _VIRTUAL_NODE_TYPES:
                continue
            if live is not None:
                if api_id not in live:
                    state[api_id] = 'disconnected'
            elif not _node_has_any_connection(n):
                state[api_id] = 'disconnected'

    _scan(workflow_json.get('nodes', []))
    for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []:
        _scan(sg.get('nodes', []))
    return state


def _propagate_node_states(params, workflow_json, info_cache=None):
    """Tag every widget of a non-functional node (disconnected/bypassed/
    muted) with its `_node_state`, in a pass that runs AFTER enrichment.

    `_parse_app_inputs` only tags the widgets the user exposed in
    linearData. Widgets added later - non-exposed Read/Write widgets from
    `_add_nukomfy_nonexposed_widgets`, and the auto-added file_path of a
    floating NukomfyRead - never saw the node-state map and would stay
    toggleable. This second pass closes the gap so the editor greys+locks
    the whole node and the gizmo build drops all of its knobs (a
    non-functional node has no runtime effect). Idempotent: widgets that
    already carry a state are left untouched.
    """
    api_id_map = _build_api_id_map(workflow_json)
    state_map = _classify_node_states(workflow_json, api_id_map, info_cache)
    floating = _floating_nukomfy_read_ids(workflow_json, api_id_map)
    for p in params:
        if p.get('_node_state'):
            continue
        tnid = p.get('target_node_id')
        state = state_map.get(tnid) or state_map.get(str(tnid))
        if not state and (tnid in floating or str(tnid) in floating):
            state = 'disconnected'
        if state:
            p['_node_state'] = state


def _output_node_ids(workflow_json):
    """Node ids marked as app outputs in ComfyUI's App Builder.

    Reads extra.linearData.outputs, falling back to
    definitions.subgraphs[0].outputs when the linear data is empty.
    """
    out_ids = set()

    linear = workflow_json.get('extra', {}).get('linearData', {})
    for nid in linear.get('outputs', []) or []:
        out_ids.add(nid)

    if not out_ids:
        try:
            sg = workflow_json.get('definitions', {}).get('subgraphs', [{}])[0]
            links_by_id = {lnk['id']: lnk for lnk in sg.get('links', [])}
            for out in sg.get('outputs', []):
                for lid in out.get('linkIds', []):
                    lnk = links_by_id.get(lid)
                    if lnk:
                        origin = lnk.get('origin_id')
                        if origin is not None:
                            out_ids.add(origin)
        except Exception:
            pass

    return out_ids


def _marked_output_node_types(workflow_json):
    """Return the list of node 'type' strings for nodes marked as app outputs
    in ComfyUI's App Builder. Handles both extra.linearData.outputs and the
    definitions.subgraphs[0].outputs fallback."""
    nodes_by_id = _build_flat_nodes_index(workflow_json)
    out_ids = _output_node_ids(workflow_json)
    return [nodes_by_id.get(nid, {}).get('type', '') for nid in out_ids]


def _non_nukomfy_output_nodes(workflow_json):
    """Return a list of (node_type, node_title) for output-marked nodes
    whose type is NOT NukomfyWrite. These are nodes the user marked as app
    outputs in ComfyUI's App Builder but whose file output Nukomfy
    cannot capture - SaveImage, PreviewImage, and any other non-Nuke
    output-capable class. Used by the Workflow Creator to warn the user
    that those outputs won't be rendered to disk via the gizmo."""
    nodes_by_id = _build_flat_nodes_index(workflow_json)
    out_ids = _output_node_ids(workflow_json)

    result = []
    for nid in out_ids:
        node = nodes_by_id.get(nid, {})
        nt = node.get('type', '')
        if nt and nt != 'NukomfyWrite':
            result.append((nt, _node_title(node)))
    return result


def _build_widget_defaults(info, widgets_values):
    """Return `({widget_name: value}, seed_widgets_set, seed_controls_map)`
    for a node.

    Widgets immediately followed by a control_after_generate companion string
    (fixed/increment/decrement/randomize) are recorded in the seed set, with
    the companion value keyed by widget name in seed_controls_map.
    The companion is a client-side frontend feature that does not appear in
    /object_info, so this positional scan is the only way to detect it."""
    if not widgets_values:
        return {}, set(), {}

    # COMBO new format ('COMBO' + extra['options']) and DYNAMICCOMBO_V3
    # must count as widgets too - otherwise positional pairing of
    # widgets_values desyncs and seed/COMBO defaults end up bound to the
    # wrong widget name. Same for COLOR (hex string widget used by
    # Painter.bg_color, ColorToRGBInt.color, etc.) - omitting it would
    # skip its default extraction and cascade the desync for any widget
    # ordered after it in the same node.
    _WIDGET_TYPES = {'INT', 'FLOAT', 'STRING', 'BOOLEAN',
                     'COMBO', 'COMFY_DYNAMICCOMBO_V3', 'COLOR'}

    input_spec = info.get('input', {})
    input_order = info.get('input_order', {})

    def _is_widget(input_def):
        if not isinstance(input_def, (list, tuple)) or not input_def:
            return False
        t = input_def[0]
        if isinstance(t, list):
            return True  # COMBO old format
        return isinstance(t, str) and t.upper() in _WIDGET_TYPES

    # Build ordered list of ALL widget names from the input spec
    all_widgets = []
    for section in ('required', 'optional'):
        section_data = input_spec.get(section, {})
        if not isinstance(section_data, dict):
            continue
        # Use input_order for correct definition-order; fall back to dict keys
        ordered_names = input_order.get(section, list(section_data.keys()))
        for name in ordered_names:
            input_def = section_data.get(name)
            if _is_widget(input_def):
                all_widgets.append(name)

    # V3 master detection helpers. A DYNAMICCOMBO_V3 widget expands its
    # selected option's sub-inputs into widgets_values right after the
    # master value; the canonical order from /object_info does NOT list
    # these subs. A sub can itself be a V3 master (e.g. NukomfyWrite
    # file_type -> exr_compression -> exr_dw_compression_level), so the
    # subs must be walked DFS pre-order, capturing each value under its
    # full dotted path and advancing idx past every (nested) slot. Skip
    # socket-type subs (never serialized into widgets_values), matching
    # the snapshot consumer (_add_v3_subs_for_master / _walk).
    def _is_v3_master(spec):
        return (isinstance(spec, (list, tuple)) and spec
                and isinstance(spec[0], str)
                and spec[0].upper() == 'COMFY_DYNAMICCOMBO_V3')

    def _sub_is_widget(spec):
        if not isinstance(spec, (list, tuple)) or not spec:
            return False
        t = spec[0]
        if isinstance(t, list):
            return True  # COMBO old format (list of values)
        if not isinstance(t, str):
            return False
        tu = t.upper()
        return (tu in _WIDGET_TYPES
                or (tu.startswith('COMFY_') and 'COMBO' in tu))

    v3_master_specs = {}  # top-level widget_name -> spec
    for section in ('required', 'optional'):
        section_data = input_spec.get(section, {}) or {}
        if not isinstance(section_data, dict):
            continue
        for wn, wd in section_data.items():
            if _is_v3_master(wd):
                v3_master_specs[wn] = wd

    # Walk widgets_values in order, mapping each widget name
    result = {}
    seed_widgets = set()
    seed_controls = {}   # {widget_name: control_str from workflow}

    def _consume_v3_subs(master_full_name, master_spec, master_value, start):
        """DFS pre-order over the master's currently-selected option,
        recursing into nested V3 sub-masters. Captures each sub value
        under its full dotted path (master.sub[.subsub...]) and returns
        idx advanced past every (nested) dynamic sub-input slot."""
        i = start
        extra = (master_spec[1] if len(master_spec) > 1
                 and isinstance(master_spec[1], dict) else {})
        target = None
        for o in extra.get('options', []) or []:
            if (isinstance(o, dict)
                    and str(o.get('key', '')) == str(master_value)):
                target = o
                break
        if target is None:
            return i
        target_inputs = target.get('inputs', {}) or {}
        for sec in ('required', 'optional'):
            for sub_name, sub_spec in (
                    target_inputs.get(sec, {}) or {}).items():
                if not _sub_is_widget(sub_spec):
                    continue
                if i >= len(widgets_values):
                    return i
                full_name = '{}.{}'.format(master_full_name, sub_name)
                sub_value = widgets_values[i]
                result[full_name] = sub_value
                i += 1
                if _is_v3_master(sub_spec):
                    i = _consume_v3_subs(full_name, sub_spec, sub_value, i)
        return i

    # Dict-form widgets_values: the newer ComfyUI frontend can serialize a
    # node's widget values as a name->value mapping instead of a positional
    # list. Map directly by name. The positional seed-companion scan below
    # cannot apply to a mapping, so it is skipped; seeds on such nodes are
    # still detected name-based in _enrich_params via the /object_info
    # control_after_generate flag.
    if isinstance(widgets_values, dict):
        for wn in all_widgets:
            if wn in widgets_values:
                result[wn] = widgets_values[wn]
        for k, v in widgets_values.items():
            if isinstance(k, str) and '.' in k:  # dotted V3 sub-input
                result[k] = v
        return result, seed_widgets, seed_controls
    if not isinstance(widgets_values, list):
        return result, seed_widgets, seed_controls  # unknown form: nothing to map

    idx = 0
    for wn in all_widgets:
        if idx >= len(widgets_values):
            break
        value = widgets_values[idx]
        result[wn] = value
        idx += 1
        # A control_after_generate companion only ever follows the seed's
        # integer value. The same literal strings are legitimate option
        # values of unrelated combo widgets: consuming one of those would
        # mis-mark the previous widget as a seed and shift every later
        # default onto the wrong widget. (PrimitiveNode combos DO carry a
        # companion after a string value - handled in
        # _build_primitive_param, not here.)
        if (isinstance(value, int) and not isinstance(value, bool)
                and idx < len(widgets_values)
                and isinstance(widgets_values[idx], str)
                and widgets_values[idx] in _SEED_CONTROLS):
            seed_widgets.add(wn)
            seed_controls[wn] = widgets_values[idx]
            idx += 1
        # If this widget is a V3 master, walk its dynamic sub-inputs
        # (recursively for nested masters) so saved sub values land under
        # their full dotted path and idx clears every slot before the
        # next canonical widget.
        if wn in v3_master_specs:
            idx = _consume_v3_subs(wn, v3_master_specs[wn], value, idx)
    return result, seed_widgets, seed_controls


def _classify_param(p):
    """Return 'input'/'output'/'knob'. role='input'/'output' is reserved for
    our official Nuke I/O nodes (NukomfyRead/NukomfyWrite). File-like widgets on
    other custom nodes (e.g. VHS_LoadImages.directory) stay as 'knob'."""
    if p.get('_is_primitive'):
        return 'knob'
    is_string = (p.get('type', '').upper() == 'STRING')
    name_lc   = p.get('name', '').lower()
    is_file   = is_string and name_lc in _FILE_PARAM_NAMES
    is_nukomfy_io_node = p.get('node_type', '') in _AUTO_FILE_PATH_NODES
    if is_file and is_nukomfy_io_node:
        return 'output' if p.get('is_output', False) else 'input'
    return 'knob'


def _find_online_servers():
    """Probe enabled machines in parallel.

    Returns `(fast_urls, collect_remaining)`. `fast_urls` contains servers that
    responded within a 200 ms grace window after the first hit; `collect_remaining`
    blocks until the rest finish and returns any extra URLs found.
    """
    import time
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    from Nukomfy.client.machines import machine_manager

    machines = list(machine_manager.enabled_machines)
    if not machines:
        return [], lambda: []

    def _check(m):
        try:
            url = m.url.rstrip('/') + '/api/system_stats'
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as r:
                r.read()  # consume body so the socket closes with FIN, not RST
            return m.url.rstrip('/')
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
                except Exception: pass
            return None

    pool = ThreadPoolExecutor(max_workers=len(machines))
    pending = {pool.submit(_check, m) for m in machines}
    fast_urls = []
    grace_deadline = None

    while pending:
        timeout = None
        if grace_deadline is not None:
            timeout = max(0, grace_deadline - time.monotonic())
            if timeout <= 0:
                break
        done, pending = wait(pending, timeout=timeout,
                             return_when=FIRST_COMPLETED)
        for f in done:
            result = f.result()
            if result:
                fast_urls.append(result)
                if grace_deadline is None:
                    grace_deadline = time.monotonic() + _FAST_SERVER_GRACE_S

    # Return fast results now; provide a callable for the slow remainder
    remaining = list(pending)  # snapshot of still-running futures

    def _collect_remaining():
        extra = []
        for f in remaining:
            try:
                result = f.result()  # blocks until done (already running)
                if result:
                    extra.append(result)
            except Exception:
                pass
        pool.shutdown(wait=False)
        return extra

    if not remaining:
        pool.shutdown(wait=False)
    return fast_urls, _collect_remaining


def _add_nukomfy_nonexposed_widgets(params, info_cache, nukomfy_node_meta):
    """For each NukomfyRead/NukomfyWrite node, add every `/object_info` widget
    that isn't already in `params` with `enabled=False`.

    This lets the user opt-in to expose any Read/Write widget from the
    Workflow Creator, without having to edit linearData.inputs upstream.
    File-path widgets are skipped (handled by `_ensure_nukomfy_file_paths`),
    and auto-managed widgets (frame range, sequence, etc.) are skipped.
    """
    existing = {(p.get('target_node_id'),
                 p.get('widget_name', p.get('name', '')))
                for p in params}

    for node_id, meta in nukomfy_node_meta.items():
        nt   = meta['node_type']
        info = info_cache.get(nt, {})
        if not info:
            continue
        wv = meta['widgets_values']
        defaults_map, _seed_set, _seed_ctrl = _build_widget_defaults(info, wv)
        auto_mgd = _AUTO_MANAGED_WIDGETS.get(nt, set())
        is_output_node = (nt == 'NukomfyWrite')

        for section in ('required', 'optional'):
            inputs = info.get('input', {}).get(section, {})
            for wname, input_def in inputs.items():
                if wname in auto_mgd:
                    continue
                if wname.lower() in _FILE_PARAM_NAMES:
                    continue
                if (node_id, wname) in existing:
                    continue
                if not isinstance(input_def, (list, tuple)) or not input_def:
                    continue

                type_or_values = input_def[0]
                extra = (input_def[1] if len(input_def) > 1
                         and isinstance(input_def[1], dict) else {})

                # Skip socket-type inputs (IMAGE, MASK, LATENT, MODEL,
                # NUKOMFY_MULTILAYER, ...) that don't have a widget
                # representation. Accept scalar widgets (INT/FLOAT/STRING/
                # BOOLEAN), legacy list-type COMBOs, modern 'COMBO' string
                # with an `options` dict, and 'COMFY_DYNAMICCOMBO_V3'
                # master selectors.
                if isinstance(type_or_values, str) and type_or_values.upper() \
                        not in ('INT', 'FLOAT', 'STRING', 'BOOLEAN',
                                'COMBO', 'COMFY_DYNAMICCOMBO_V3'):
                    continue

                # forceInput widgets are sockets - not editable
                if isinstance(extra, dict) and extra.get('forceInput'):
                    continue

                # Modern IO.Schema widgets carry an explicit
                # `display_name` (human-readable label set by the node
                # author). Fall back to the snake_case `widget_name` for
                # legacy INPUT_TYPES nodes that have no display_name.
                # No algorithmic snake_case prettification (1:1 with ComfyUI)
                # because display_name is an explicit author choice, not
                # an automatic conversion.
                widget_label = (extra.get('display_name')
                                if isinstance(extra, dict) else None) or wname
                new_p = {
                    'name':           wname,
                    'label':          widget_label,
                    'target_node_id': node_id,
                    'node_type':      nt,
                    'node_title':     meta['node_title'],
                    'display_name':   info.get('display_name', ''),
                    'widget_name':    wname,
                    'default_value':  defaults_map.get(wname),
                    'is_output':      is_output_node,
                    'role':           'knob',
                    'enabled':        False,
                }

                if isinstance(type_or_values, list):
                    if (len(type_or_values) == 1
                            and isinstance(type_or_values[0], list)
                            and set(str(v) for v in type_or_values[0]) == {'True', 'False'}):
                        new_p['type'] = 'BOOLEAN'
                    else:
                        str_vals = [str(v) for v in type_or_values]
                        if set(str_vals) == {'True', 'False'}:
                            new_p['type'] = 'BOOLEAN'
                        else:
                            new_p['combo_values'] = str_vals
                            new_p['type'] = 'COMBO'
                elif isinstance(type_or_values, str):
                    ptype = type_or_values.upper()
                    if ptype == 'COMBO':
                        # Modern COMBO format: ['COMBO', {options: [...]}].
                        # The legacy [list, dict] format is handled by the
                        # isinstance(..., list) branch above.
                        opts = (extra.get('options', [])
                                if isinstance(extra, dict) else [])
                        new_p['type'] = 'COMBO'
                        new_p['combo_values'] = [str(v) for v in opts]
                    elif ptype == 'COMFY_DYNAMICCOMBO_V3':
                        # DynamicCombo V3 master: expose the selector as a
                        # COMBO with the option keys. The nested sub-inputs
                        # (master.sub) of the selected option are wired by
                        # the Workflow Editor live-update logic when the
                        # user changes the master value. Tagged so the
                        # save-time expansion can recognise this row as a
                        # top master and emit sub entries for every
                        # option, including the ones with no sub-inputs
                        # in the current workflow state (e.g. file_type
                        # set to a format with no extra widgets).
                        opts = (extra.get('options', [])
                                if isinstance(extra, dict) else [])
                        new_p['type'] = 'COMBO'
                        new_p['combo_values'] = [
                            str(o.get('key', '')) for o in opts
                            if isinstance(o, dict)]
                        new_p['_v3_is_dynamic_master'] = True
                    else:
                        new_p['type'] = ptype
                        if ptype in ('INT', 'FLOAT') and isinstance(extra, dict):
                            if 'min' in extra:
                                new_p['min_value'] = extra['min']
                            if 'max' in extra:
                                new_p['max_value'] = extra['max']
                        elif ptype == 'STRING' and isinstance(extra, dict):
                            if extra.get('multiline'):
                                new_p['multiline'] = True
                else:
                    new_p['type'] = 'STRING'

                if isinstance(extra, dict) and extra.get('tooltip'):
                    new_p['tooltip'] = extra['tooltip']

                # Fallback default from /object_info when widgets_values
                # didn't cover this widget
                if (new_p.get('default_value') is None
                        and isinstance(extra, dict) and 'default' in extra):
                    new_p['default_value'] = extra['default']

                # is_seed detection (standalone - no widgets_values companion)
                if (new_p.get('type', '').upper() == 'INT'
                        and _seed_name_has_token(wname)):
                    try:
                        has_huge_range = int(new_p.get('max_value') or 0) >= _SEED_HUGE_RANGE_MIN
                    except (TypeError, ValueError):
                        has_huge_range = False
                    if has_huge_range:
                        new_p['is_seed'] = True

                params.append(new_p)
                existing.add((node_id, wname))

        # Nested V3 sub-inputs (e.g. file_type.quality on NukomfyWrite
        # when file_type is jpeg, or
        # file_type.compression.dw_compression_level when compression
        # is DWAA). The main loop only sees top-level widgets - V3
        # subs live under options[].inputs.required / optional, and
        # any of those subs may itself be a V3 master with its own
        # nested sub-inputs. Walked recursively, scoped to each
        # master's current value; subsequent option swaps are handled
        # live by _refresh_v3_master_subs.
        #
        # We finalise type / combo_values / _v3_show_for_keys here
        # because this function runs AFTER _enrich_params (which owns
        # the standard V3 promotion); a placeholder with
        # _v3_master_candidate alone would never get promoted.
        _WIDGET_TYPE_NAMES = {'INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO',
                              'COMFY_DYNAMICCOMBO_V3'}

        def _add_v3_subs_for_master(master_path, master_def,
                                    master_current_value, ancestors):
            """Add V3 sub-inputs of `master_path` for the master's
            CURRENT VALUE only - the Workflow Editor table should
            mirror what the user picked, not show every alternative.
            The gizmo-side expansion (knobs for every option) happens
            later at save time in _KnobsTable.get_params.

            master_path is the dotted widget_name ('file_type' or
            'file_type.compression'). `ancestors` is a list of
            (ancestor_master_path, [allowed_keys]) carrying down so a
            nested sub (e.g. dw_compression_level) knows it depends on
            BOTH compression in {DWAA, DWAB} AND file_type == exr."""
            if (not isinstance(master_def, (list, tuple))
                    or len(master_def) < 2):
                return
            master_extra = (master_def[1]
                            if isinstance(master_def[1], dict) else {})
            options = master_extra.get('options', []) or []
            target_option = None
            for opt in options:
                if (isinstance(opt, dict)
                        and str(opt.get('key', ''))
                            == str(master_current_value)):
                    target_option = opt
                    break
            if target_option is None:
                return
            # Pre-compute show_for_keys per sub_name across every option
            # (mirrors V3 promotion).
            show_for_keys_by_sub = {}
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                key = str(opt.get('key', ''))
                if not key:
                    continue
                opt_inputs = opt.get('inputs', {}) or {}
                opt_sub_names = set()
                for sec in ('required', 'optional'):
                    opt_sub_names |= set(
                        ((opt_inputs.get(sec, {}) or {}).keys()))
                for sub_name in opt_sub_names:
                    show_for_keys_by_sub.setdefault(
                        sub_name, []).append(key)

            # Walk ONLY the current option's sub-inputs.
            target_inputs = target_option.get('inputs', {}) or {}
            for sec in ('required', 'optional'):
                sub_section = target_inputs.get(sec, {}) or {}
                for sub_name, sub_spec in sub_section.items():
                        full_name = '{}.{}'.format(master_path, sub_name)
                        is_dyn_master = (
                            isinstance(sub_spec, (list, tuple))
                            and len(sub_spec) >= 1
                            and isinstance(sub_spec[0], str)
                            and sub_spec[0].upper()
                                == 'COMFY_DYNAMICCOMBO_V3')
                        sub_show = list(
                            show_for_keys_by_sub.get(sub_name, []))
                        if (node_id, full_name) in existing:
                            continue
                        if (not isinstance(sub_spec, (list, tuple))
                                or not sub_spec):
                            continue
                        sub_type_or_values = sub_spec[0]
                        sub_extra = (sub_spec[1] if len(sub_spec) > 1
                                     and isinstance(sub_spec[1], dict)
                                     else {})
                        # Skip socket-type sub-inputs.
                        if (isinstance(sub_type_or_values, str)
                                and sub_type_or_values.upper()
                                    not in _WIDGET_TYPE_NAMES
                                and not (
                                    sub_type_or_values.upper().startswith('COMFY_')
                                    and 'COMBO'
                                        in sub_type_or_values.upper())):
                            continue
                        sub_label = (sub_extra.get('display_name')
                                     if isinstance(sub_extra, dict)
                                     else None) or sub_name
                        sub_p = {
                            'name':           full_name,
                            'label':          sub_label,
                            'target_node_id': node_id,
                            'node_type':      nt,
                            'node_title':     meta['node_title'],
                            'display_name':   info.get('display_name', ''),
                            'widget_name':    full_name,
                            'is_output':      is_output_node,
                            'role':           'knob',
                            'enabled':        False,
                            '_v3_master':     master_path,
                            '_v3_sub_name':   sub_name,
                            '_v3_show_for_keys': sub_show,
                        }
                        if is_dyn_master:
                            sub_p['_v3_is_dynamic_master'] = True
                        if ancestors:
                            sub_p['_v3_ancestor_conditions'] = [
                                list(a) for a in ancestors]
                        # Resolve type and constraints.
                        if isinstance(sub_type_or_values, list):
                            if (len(sub_type_or_values) == 1
                                    and isinstance(
                                        sub_type_or_values[0], list)
                                    and set(str(v)
                                            for v in sub_type_or_values[0])
                                        == {'True', 'False'}):
                                sub_p['type'] = 'BOOLEAN'
                            else:
                                str_vals = [str(v)
                                            for v in sub_type_or_values]
                                if set(str_vals) == {'True', 'False'}:
                                    sub_p['type'] = 'BOOLEAN'
                                else:
                                    sub_p['combo_values'] = str_vals
                                    sub_p['type'] = 'COMBO'
                        elif isinstance(sub_type_or_values, str):
                            sptype = sub_type_or_values.upper()
                            if (sptype in ('COMBO',
                                           'COMFY_DYNAMICCOMBO_V3')
                                    and isinstance(sub_extra, dict)
                                    and isinstance(
                                        sub_extra.get('options'), list)):
                                opts2 = sub_extra['options']
                                if opts2 and isinstance(opts2[0], dict):
                                    sub_p['combo_values'] = [
                                        str(o.get('key', ''))
                                        for o in opts2 if o.get('key')]
                                else:
                                    sub_p['combo_values'] = [
                                        str(v) for v in opts2]
                                sub_p['type'] = 'COMBO'
                            else:
                                sub_p['type'] = sptype
                            if (sptype in ('INT', 'FLOAT')
                                    and isinstance(sub_extra, dict)):
                                if 'min' in sub_extra:
                                    sub_p['min_value'] = sub_extra['min']
                                if 'max' in sub_extra:
                                    sub_p['max_value'] = sub_extra['max']
                                if 'display' in sub_extra:
                                    sub_p['_display_mode'] = (
                                        sub_extra['display'])
                                if (sptype in ('INT', 'FLOAT')
                                        and 'step' in sub_extra):
                                    sub_p['_step'] = sub_extra['step']
                            elif (sptype == 'STRING'
                                    and isinstance(sub_extra, dict)):
                                if sub_extra.get('multiline'):
                                    sub_p['multiline'] = True
                        # Default: workflow saved value first (via
                        # widgets_values for this sub), then sub_extra
                        # default.
                        saved_val = defaults_map.get(full_name)
                        if saved_val is not None:
                            sub_p['default_value'] = saved_val
                        elif (isinstance(sub_extra, dict)
                                and 'default' in sub_extra):
                            sub_p['default_value'] = sub_extra['default']
                        elif sub_p.get('combo_values'):
                            # No saved value and no schema default: a
                            # DynamicCombo/Combo defaults to its first
                            # option, matching the ComfyUI frontend.
                            sub_p['default_value'] = sub_p['combo_values'][0]
                        else:
                            sub_p['default_value'] = None
                        if (isinstance(sub_extra, dict)
                                and sub_extra.get('tooltip')):
                            sub_p['tooltip'] = sub_extra['tooltip']
                        params.append(sub_p)
                        existing.add((node_id, full_name))

                        # If this sub is itself a V3 master, recurse
                        # into its current option's tree carrying the
                        # new ancestor condition.
                        if is_dyn_master:
                            sub_current = sub_p.get('default_value')
                            if (sub_current is None
                                    and isinstance(sub_extra, dict)):
                                sub_current = sub_extra.get('default')
                            if sub_current is not None:
                                _add_v3_subs_for_master(
                                    full_name, sub_spec, sub_current,
                                    ancestors + [
                                        (master_path, sub_show)])

        for section in ('required', 'optional'):
            inputs = info.get('input', {}).get(section, {})
            for master_wname, master_def in inputs.items():
                if not isinstance(master_def, (list, tuple)) or not master_def:
                    continue
                if master_def[0] != 'COMFY_DYNAMICCOMBO_V3':
                    continue
                master_extra = (master_def[1] if len(master_def) > 1
                                and isinstance(master_def[1], dict) else {})
                current_value = defaults_map.get(master_wname)
                if current_value is None and isinstance(master_extra, dict):
                    current_value = master_extra.get('default')
                if current_value is None:
                    continue
                _add_v3_subs_for_master(
                    master_wname, master_def, current_value, [])


def _enrich_params(params, server_urls, subgraph_uuids=None):
    """Enrich params with COMBO values, min/max from ComfyUI servers.

    Tries all servers for each node type. Returns set of node types
    that could not be found on any server (missing nodes).

    `subgraph_uuids`: set of subgraph definition ids declared in
    `definitions.subgraphs`. These appear as `node.type` on subgraph
    instance wrappers but are frontend-only - the converter inlines
    them at ui_to_api time and ComfyUI's /object_info has no entry for
    them. Treated like _VIRTUAL_NODE_TYPES (skipped from /object_info
    lookup and never reported as missing).
    """
    import urllib.parse
    import urllib.request

    skip_types = set(_VIRTUAL_NODE_TYPES)
    if subgraph_uuids:
        skip_types |= set(subgraph_uuids)

    node_types = {p.get('node_type', '') for p in params
                  if p.get('node_type')
                  and p.get('node_type') not in skip_types}
    # Primitive params keep node_type='PrimitiveNode' for display, but
    # their constraints come from the linked targets - fetch each
    # target's /object_info too so we can intersect later. When a target
    # is a subgraph instance, _real_node_type holds the inner consumer's
    # class (e.g. 'KlingFirstLastFrameNode') - that's where /object_info
    # has the COMBO list / min / max. The wrapper UUID stays in
    # skip_types because it has no /object_info entry.
    for p in params:
        if not p.get('_is_primitive'):
            continue
        for t in p.get('_primitive_targets', []) or []:
            if not isinstance(t, dict):
                continue
            tt = t.get('_real_node_type') or t.get('node_type') or ''
            if tt and tt not in skip_types:
                node_types.add(tt)
    if not node_types:
        return set(), {}

    if isinstance(server_urls, str):
        server_urls = [server_urls]

    info_cache = {}
    missing_nodes = set()
    for nt in node_types:
        found = False
        for server_url in server_urls:
            try:
                url = '{}/object_info/{}'.format(
                    server_url, urllib.parse.quote(nt, safe=''))
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                info = data.get(nt, {})
                if info:
                    info_cache[nt] = info
                    found = True
                    break
            except Exception as e:
                if isinstance(e, urllib.error.HTTPError):
                    try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
                    except Exception: pass
                continue
        if not found:
            missing_nodes.add(nt)

    # Expand output-node placeholders: only add the file-path widget
    # (e.g. 'filepath') as an output param.  Other widgets of the output
    # node are NOT exposed - only the path matters.
    expanded = []
    for p in params:
        if not p.get('_expand_output'):
            expanded.append(p)
            continue
        nt = p.get('node_type', '')
        # Defensive skip: only Nuke-managed output types generate a
        # file-path placeholder. If a legacy placeholder survived on a
        # non-Nuke node (e.g. SaveImage), drop it silently - its widgets
        # (if exposed) are handled as regular knobs by _classify_param.
        if nt not in _AUTO_FILE_PATH_NODES:
            continue
        info = info_cache.get(nt, {})
        if not info:
            p['_missing_node'] = True
            p.pop('_expand_output', None)
            p.pop('_node_widgets_values', None)
            expanded.append(p)
            continue
        wv = p.get('_node_widgets_values', [])
        defaults, _seed_set, _seed_ctrl = _build_widget_defaults(info, wv)
        for wname, wval in defaults.items():
            if wname.lower() not in _FILE_PARAM_NAMES:
                continue
            new_p = {
                'name':           wname,
                'type':           'STRING',
                'label':          p.get('label') or wname,
                'target_node_id': p['target_node_id'],
                'node_type':      nt,
                'node_title':     p.get('node_title', ''),
                'display_name':   p.get('display_name', ''),
                'widget_name':    wname,
                'default_value':  wval,
                '_node_widgets_values': wv,
                'is_output':      True,
                'role':           'output',
            }
            expanded.append(new_p)
            break  # one file-path widget is enough
    params[:] = expanded

    # Capture widgets_values for NukomfyRead/NukomfyWrite nodes before they get
    # popped in the main loop below. Used afterwards to add non-exposed
    # widgets (those not in linearData.inputs) with enabled=False.
    # NukomfyWrite nodes are gated by `is_output=True`: an unmarked NukomfyWrite
    # isn't an output for the gizmo and must not contribute any knobs.
    nukomfy_write_output_ids = {p.get('target_node_id') for p in params
                             if p.get('node_type') == 'NukomfyWrite'
                             and p.get('is_output')
                             and p.get('target_node_id') is not None}
    nukomfy_node_meta = {}  # node_id -> {node_type, node_title, widgets_values}
    for p in params:
        nt = p.get('node_type', '')
        nid = p.get('target_node_id')
        if nid is None or nid in nukomfy_node_meta:
            continue
        if nt == 'NukomfyWrite' and nid not in nukomfy_write_output_ids:
            continue
        if nt in _NUKOMFY_NODE_TYPES:
            nukomfy_node_meta[nid] = {
                'node_type':      nt,
                'node_title':     p.get('node_title', ''),
                'widgets_values': p.get('_node_widgets_values', []),
            }

    # Build default-value maps per (node_type, widgets_values identity)
    # so we call _build_widget_defaults once per unique node instance
    _defaults_cache = {}  # (node_type, id(widgets_values)) -> {name: val}

    # Per-node widgets_values, captured before the main loop pops them:
    # the V3 synth-master pass further down needs the workflow's actual
    # values to default the synthesized master to the user's selection.
    _wv_by_node = {}
    for p in params:
        if p.get('_is_primitive'):
            continue
        nid = p.get('target_node_id')
        if nid is None or nid in _wv_by_node:
            continue
        wv = p.get('_node_widgets_values')
        if isinstance(wv, (list, dict)) and wv:
            _wv_by_node[nid] = wv

    for p in params:
        # Primitive params have node_type='PrimitiveNode' (skipped from
        # /object_info fetch). Their constraints come from the linked
        # targets - handled in a dedicated pass after this main loop.
        if p.get('_is_primitive'):
            continue
        nt = p.get('node_type', '')
        widget_name = p.get('widget_name', p.get('name', ''))
        info = info_cache.get(nt, {})
        p['display_name'] = info.get('display_name', '') if info else ''
        if not info:
            p['_missing_node'] = True
            p.pop('_node_widgets_values', None)
            continue

        # Extract default value from widgets_values using /object_info order
        wv = p.get('_node_widgets_values', [])
        cache_key = (nt, id(wv))
        if cache_key not in _defaults_cache:
            _defaults_cache[cache_key] = _build_widget_defaults(info, wv)
        defaults_map, seed_widgets_set, seed_controls_map = _defaults_cache[cache_key]
        if widget_name in defaults_map and p.get('default_value') is None:
            p['default_value'] = defaults_map[widget_name]
        if widget_name in seed_widgets_set:
            p['is_seed'] = True
            # Strong signal: the workflow JSON itself stores a
            # control_after_generate companion next to this widget's value.
            # Tracked so the safety gate downstream knows this was a Level-2
            # detection and won't drop it when name+huge_range fails (e.g.
            # ElevenLabsTextToSpeech.seed has max=2^31-1, below the gate's
            # huge_range threshold, but the companion in widgets_values is
            # unambiguous).
            p['_has_json_companion'] = True
            ctrl_default = seed_controls_map.get(widget_name)
            if ctrl_default and p.get('seed_control_default') is None:
                p['seed_control_default'] = ctrl_default
        p.pop('_node_widgets_values', None)

        # Enrich type, combo values, min/max
        # Detect pseudo-widgets: names exposed by the ComfyUI frontend
        # (upload buttons, folder pickers, etc.) that don't exist in the
        # API object_info. Example: VHS_LoadImages.'choose folder to upload'
        # is a frontend-only upload button - no schema, no API meaning,
        # no way for us to render it properly as a knob. Skip these.
        all_inputs = {}
        for section in ('required', 'optional', 'hidden'):
            all_inputs.update(info.get('input', {}).get(section, {}) or {})
        if widget_name and widget_name not in all_inputs:
            # Dotted V3 sub-input ('resize_type.crop') won't be in
            # all_inputs (which only has the master 'resize_type'). Don't
            # mark as pseudo-widget here - the V3 promotion pass below
            # validates it against the master's options and either keeps
            # it as a real knob or downgrades to STRING with a warning.
            if not p.get('_v3_master_candidate'):
                p['_skip'] = True
                p['_pseudo_widget'] = True
            continue
        for section in ('required', 'optional'):
            inputs = info.get('input', {}).get(section, {})
            if widget_name not in inputs:
                continue
            input_def = inputs[widget_name]
            if not isinstance(input_def, (list, tuple)) or not input_def:
                continue

            type_or_values = input_def[0]
            extra = input_def[1] if len(input_def) > 1 and isinstance(input_def[1], dict) else {}

            # forceInput widgets are sockets in ComfyUI - not editable
            if isinstance(extra, dict) and extra.get('forceInput'):
                p['_skip'] = True
                break

            # Tooltip from ComfyUI node definition (only if not already set)
            if not p.get('tooltip') and extra.get('tooltip'):
                p['tooltip'] = extra['tooltip']

            # Fallback default from /object_info when widgets_values didn't cover it
            if p.get('default_value') is None and isinstance(extra, dict) and 'default' in extra:
                p['default_value'] = extra['default']

            if isinstance(type_or_values, list):
                # Nested boolean form [[True, False]]
                if (len(type_or_values) == 1
                        and isinstance(type_or_values[0], list)
                        and set(str(v) for v in type_or_values[0]) == {'True', 'False'}):
                    p['type'] = 'BOOLEAN'
                else:
                    str_vals = [str(v) for v in type_or_values]
                    if set(str_vals) == {'True', 'False'}:
                        p['type'] = 'BOOLEAN'
                    else:
                        p['combo_values'] = str_vals
                        p['type'] = 'COMBO'
            elif isinstance(type_or_values, str):
                ptype = type_or_values.upper()
                # ComfyUI new-format COMBO + custom DYNAMICCOMBO_V3.
                # Old format: input_def = [['val1','val2',...], extra]
                # New format: input_def = ['COMBO', {'options':[...], ...}]
                # DYNAMICCOMBO_V3: ['COMFY_DYNAMICCOMBO_V3',
                #                   {'options':[{'key':'...', 'inputs':{...}}]}]
                # Both are treated as plain COMBO (the dynamic sub-inputs of
                # V3 stay in the workflow JSON; only the key is exposed).
                if (ptype in ('COMBO', 'COMFY_DYNAMICCOMBO_V3')
                        and isinstance(extra, dict)
                        and isinstance(extra.get('options'), list)):
                    opts = extra['options']
                    if opts and isinstance(opts[0], dict):
                        # DYNAMICCOMBO_V3 list-of-dicts shape
                        p['combo_values'] = [str(o.get('key', ''))
                                              for o in opts if o.get('key')]
                    else:
                        p['combo_values'] = [str(v) for v in opts]
                    p['type'] = 'COMBO'
                else:
                    p['type'] = ptype
                if ptype in ('INT', 'FLOAT') and isinstance(extra, dict):
                    if 'min' in extra:
                        p['min_value'] = extra['min']
                    if 'max' in extra:
                        p['max_value'] = extra['max']
                    # display="slider"|"knob"|"gradientslider" -> render as
                    # slider in the gizmo. Other display values
                    # (number/color/None) leave default spinbox.
                    if 'display' in extra:
                        p['_display_mode'] = extra['display']
                    # Capture `step` for INT and FLOAT. INT snaps to a
                    # multiple at commit (gizmo_callbacks); FLOAT uses it
                    # only to derive the editor spinbox decimals/increment
                    # - ComfyUI's FLOAT widget never snaps and neither does
                    # the gizmo (the snap path is INT-guarded).
                    if ptype in ('INT', 'FLOAT') and 'step' in extra:
                        p['_step'] = extra['step']
                    # ComfyUI explicit control_after_generate flag is the
                    # primary seed signal (more reliable than max>=2^32
                    # heuristic - many INT seeds use signed-int max=2^31-1).
                    if ptype == 'INT' and extra.get('control_after_generate'):
                        p['is_seed'] = True
                        p['_has_control_flag'] = True
                elif ptype == 'STRING' and isinstance(extra, dict):
                    if extra.get('multiline'):
                        p['multiline'] = True
                    ph = extra.get('placeholder')
                    if ph:
                        existing = p.get('tooltip', '')
                        p['tooltip'] = (existing + '\n' + ph) if existing else ph
            break

    # PrimitiveNode params: pull CONSTRAINTS from EACH linked target's
    # /object_info widget spec, then intersect (min/max for INT/FLOAT,
    # set intersection for COMBO values, OR for multiline). Mirrors
    # ComfyUI's implicit contract: the user sees one knob, the value
    # must be valid for every target the primitive feeds.
    # NOTE: knob `tooltip` is NOT pulled from any target - it lives on
    # the primitive itself (set by _build_primitive_param). On multi-link
    # picking a target's tooltip would be arbitrary.
    for p in params:
        if not p.get('_is_primitive'):
            continue
        ptype_upper = (p.get('type') or '').upper()
        targets = p.get('_primitive_targets', []) or []
        mins, maxs, combo_intersect, multiline_any = [], [], None, False
        for t in targets:
            # When the target is a subgraph instance, _real_* point at
            # the inner consumer class+widget - /object_info lives there.
            tt = t.get('_real_node_type') or t.get('node_type', '')
            wn = t.get('_real_widget_name') or t.get('widget_name', '')
            if not tt or not wn:
                continue
            info = info_cache.get(tt)
            if not info:
                continue
            # Resolves both standard widget names and dotted V3
            # sub-inputs (`master.sub`). PrimitiveNode -> Reroute ->
            # Subgraph -> KlingFirstLastFrameNode.model.resolution
            # produces combo_values=['1080p', '720p'] from the V3
            # master's option spec instead of falling back to the
            # current value alone.
            input_def = _resolve_widget_input_def(info, wn)
            if not isinstance(input_def, (list, tuple)) or not input_def:
                continue
            type_or_values = input_def[0]
            extra = (input_def[1]
                     if len(input_def) > 1 and isinstance(input_def[1], dict)
                     else {})
            if ptype_upper in ('INT', 'FLOAT'):
                if isinstance(extra, dict):
                    if 'min' in extra:
                        mins.append(extra['min'])
                    if 'max' in extra:
                        maxs.append(extra['max'])
            elif ptype_upper == 'COMBO':
                # Support both old format (type_or_values is list) and
                # new format ('COMBO' string + extra['options']).
                raw_vals = None
                if isinstance(type_or_values, list):
                    raw_vals = type_or_values
                elif (isinstance(type_or_values, str)
                      and type_or_values.upper() in ('COMBO',
                                                     'COMFY_DYNAMICCOMBO_V3')
                      and isinstance(extra, dict)
                      and isinstance(extra.get('options'), list)):
                    opts = extra['options']
                    if opts and isinstance(opts[0], dict):
                        raw_vals = [o.get('key', '') for o in opts
                                    if o.get('key')]
                    else:
                        raw_vals = opts
                if raw_vals is not None:
                    vals = set(str(v) for v in raw_vals)
                    combo_intersect = (vals if combo_intersect is None
                                       else combo_intersect & vals)
            elif ptype_upper == 'STRING':
                if isinstance(extra, dict) and extra.get('multiline'):
                    multiline_any = True
        if ptype_upper in ('INT', 'FLOAT'):
            if mins:
                p['min_value'] = max(mins)  # most restrictive
            if maxs:
                p['max_value'] = min(maxs)
        elif ptype_upper == 'COMBO':
            if combo_intersect:
                # Preserve order from the first target where possible
                # (handles both old list format and new
                # ['COMBO', {'options': [...]}] format).
                first = targets[0]
                first_tt = (first.get('_real_node_type')
                            or first.get('node_type', ''))
                first_info = info_cache.get(first_tt, {})
                first_widget_name = (first.get('_real_widget_name')
                                     or first.get('widget_name', ''))
                first_raw = _extract_combo_values(first_info, first_widget_name)
                ordered = []
                if first_raw:
                    for v in first_raw:
                        sv = str(v)
                        if sv in combo_intersect and sv not in ordered:
                            ordered.append(sv)
                p['combo_values'] = ordered or list(combo_intersect)
            else:
                # Empty intersection - fall back to first target's values
                # to keep the knob usable.
                first = targets[0]
                first_tt = (first.get('_real_node_type')
                            or first.get('node_type', ''))
                first_info = info_cache.get(first_tt, {})
                first_wn = (first.get('_real_widget_name')
                            or first.get('widget_name', ''))
                first_raw = _extract_combo_values(first_info, first_wn)
                if first_raw:
                    p['combo_values'] = [str(v) for v in first_raw]
        elif ptype_upper == 'STRING':
            if multiline_any:
                p['multiline'] = True

    # COMFY_DYNAMICCOMBO_V3 sub-input promotion.
    # Each linearData entry like ('node_id', 'master.sub') was tagged with
    # _v3_master_candidate during _parse_app_inputs. Now that /object_info
    # is fetched, validate that `master` really is a V3 widget on this
    # node, then derive the sub-input's type/range/options from
    # extra['options'][i]['inputs'][sub_name] and record `show_for_keys`
    # (set of master option keys that expose this sub) for runtime
    # visibility. Sub-inputs of socket type (IMAGE/MASK/LATENT) are
    # silently dropped - they aren't editable widgets.
    _WIDGET_TYPE_NAMES = {'INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO',
                          'COMFY_DYNAMICCOMBO_V3'}
    for p in params:
        master_name = p.get('_v3_master_candidate')
        if not master_name:
            continue
        sub_name = p.get('_v3_sub_name', '')
        nt = p.get('node_type', '')
        info = info_cache.get(nt) or {}
        master_spec = None
        for section in ('required', 'optional'):
            cand = (info.get('input', {}) or {}).get(section, {}) or {}
            if master_name in cand:
                master_spec = cand[master_name]
                break
        is_v3 = (
            isinstance(master_spec, (list, tuple))
            and master_spec
            and isinstance(master_spec[0], str)
            and master_spec[0].startswith('COMFY_')
            and 'COMBO' in master_spec[0]
        )
        if not is_v3:
            # Master not V3 - degrade to plain STRING knob, no constraints.
            # Already has type='STRING' from _parse_app_inputs fallback;
            # just clear the candidate markers so it goes through normal
            # save/load.
            p.pop('_v3_master_candidate', None)
            p.pop('_v3_sub_name', None)
            continue

        master_extra = (master_spec[1]
                        if len(master_spec) > 1 and isinstance(master_spec[1], dict)
                        else {})
        options = master_extra.get('options', []) or []
        show_for_keys = []
        sub_spec = None  # first option spec that declares this sub
        for opt in options:
            if not isinstance(opt, dict):
                continue
            key = str(opt.get('key', ''))
            if not key:
                continue
            opt_inputs = opt.get('inputs', {}) or {}
            opt_sub_names = set()
            for opt_section in ('required', 'optional'):
                opt_sub_names |= set(((opt_inputs.get(opt_section, {}) or {}).keys()))
            if sub_name in opt_sub_names:
                show_for_keys.append(key)
                if sub_spec is None:
                    for opt_section in ('required', 'optional'):
                        cand = (opt_inputs.get(opt_section, {}) or {})
                        if sub_name in cand:
                            sub_spec = cand[sub_name]
                            break

        if not show_for_keys or sub_spec is None:
            # Sub not found in any option - keep as STRING knob, drop V3
            # markers, log for diagnostics.
            try:
                _log.warning(
                    'sub-input %r not declared by any option of V3 '
                    'master %r on node %s - falling back to STRING knob.',
                    sub_name, master_name, nt)
            except Exception:
                pass
            p.pop('_v3_master_candidate', None)
            p.pop('_v3_sub_name', None)
            continue

        # Derive sub-input type from sub_spec (mirrors main loop logic).
        if not isinstance(sub_spec, (list, tuple)) or not sub_spec:
            # Defensive: malformed spec - skip silently.
            p['_skip'] = True
            continue
        sub_type_or_values = sub_spec[0]
        sub_extra = (sub_spec[1]
                     if len(sub_spec) > 1 and isinstance(sub_spec[1], dict)
                     else {})

        # Skip socket-type sub-inputs (IMAGE/MASK/LATENT/MODEL/CLIP/VAE/
        # CONDITIONING/etc.): they aren't editable widgets, so exposing
        # them as knobs would create orphan UI.
        if (isinstance(sub_type_or_values, str)
                and sub_type_or_values.upper() not in _WIDGET_TYPE_NAMES
                and not (sub_type_or_values.upper().startswith('COMFY_')
                         and 'COMBO' in sub_type_or_values.upper())):
            p['_skip'] = True
            continue

        if isinstance(sub_type_or_values, list):
            if (len(sub_type_or_values) == 1
                    and isinstance(sub_type_or_values[0], list)
                    and set(str(v) for v in sub_type_or_values[0]) == {'True', 'False'}):
                p['type'] = 'BOOLEAN'
            else:
                str_vals = [str(v) for v in sub_type_or_values]
                if set(str_vals) == {'True', 'False'}:
                    p['type'] = 'BOOLEAN'
                else:
                    p['combo_values'] = str_vals
                    p['type'] = 'COMBO'
        elif isinstance(sub_type_or_values, str):
            sptype = sub_type_or_values.upper()
            if (sptype in ('COMBO', 'COMFY_DYNAMICCOMBO_V3')
                    and isinstance(sub_extra, dict)
                    and isinstance(sub_extra.get('options'), list)):
                opts = sub_extra['options']
                if opts and isinstance(opts[0], dict):
                    p['combo_values'] = [str(o.get('key', ''))
                                         for o in opts if o.get('key')]
                else:
                    p['combo_values'] = [str(v) for v in opts]
                p['type'] = 'COMBO'
            else:
                p['type'] = sptype
            if sptype in ('INT', 'FLOAT') and isinstance(sub_extra, dict):
                if 'min' in sub_extra:
                    p['min_value'] = sub_extra['min']
                if 'max' in sub_extra:
                    p['max_value'] = sub_extra['max']
                # display='slider' on a sub-input must propagate to
                # the gizmo so it renders with the SLIDER flag
                # (mirrors the top-level V3 enrichment branch).
                if 'display' in sub_extra:
                    p['_display_mode'] = sub_extra['display']
                if sptype in ('INT', 'FLOAT') and 'step' in sub_extra:
                    p['_step'] = sub_extra['step']
            elif sptype == 'STRING' and isinstance(sub_extra, dict):
                if sub_extra.get('multiline'):
                    p['multiline'] = True

        if (p.get('default_value') is None
                and isinstance(sub_extra, dict) and 'default' in sub_extra):
            p['default_value'] = sub_extra['default']
        if not p.get('tooltip') and isinstance(sub_extra, dict) and sub_extra.get('tooltip'):
            p['tooltip'] = sub_extra['tooltip']

        # Persistent V3 metadata for the gizmo + submit-time strip.
        p['_v3_master'] = master_name
        p['_v3_sub_name'] = sub_name
        p['_v3_show_for_keys'] = list(show_for_keys)
        # Tag sub-inputs that are V3 masters themselves (e.g.
        # NukomfyWrite file_type.compression) so the Workflow Editor
        # installs the cascading rebuild hook on them too.
        if (isinstance(sub_type_or_values, str)
                and sub_type_or_values.upper() == 'COMFY_DYNAMICCOMBO_V3'):
            p['_v3_is_dynamic_master'] = True
        p.pop('_v3_master_candidate', None)

    # Detect-all for generic V3 clusters: a generic V3 sub exposed in
    # the App Builder WITHOUT its master leaves the cluster headless -
    # the master never becomes a param, so the editor would show an
    # orphan sub with no combo to drive it. Synthesize the top master
    # from /object_info so the sub-expansion below can fill the siblings
    # and the editor shows the full cluster. enabled=False: the master
    # was not explicitly exposed, so its visibility on the gizmo follows
    # that. NukomfyRead / NukomfyWrite keep their existing behavior.
    _v3_existing_keys = {
        (p.get('target_node_id'),
         p.get('widget_name') or p.get('name') or '')
        for p in params}
    _v3_synth_done = set()
    for p in list(params):
        master_name = p.get('_v3_master')
        if not master_name:
            continue
        nt = p.get('node_type', '')
        if nt in _NUKOMFY_NODE_TYPES:
            continue
        nid = p.get('target_node_id')
        top_master = master_name.split('.', 1)[0]
        key = (nid, top_master)
        if key in _v3_existing_keys or key in _v3_synth_done:
            continue
        info = info_cache.get(nt) or {}
        master_spec = None
        for section in ('required', 'optional'):
            cand = (info.get('input', {}) or {}).get(section, {}) or {}
            if top_master in cand:
                master_spec = cand[top_master]
                break
        if not (isinstance(master_spec, (list, tuple)) and master_spec
                and isinstance(master_spec[0], str)
                and master_spec[0].upper() == 'COMFY_DYNAMICCOMBO_V3'):
            continue
        master_extra = (master_spec[1] if len(master_spec) > 1
                        and isinstance(master_spec[1], dict) else {})
        options = master_extra.get('options', []) or []
        combo_values = [str(o.get('key', '')) for o in options
                        if isinstance(o, dict) and o.get('key')]
        # Default the synthesized master to its ACTUAL value in the
        # workflow (read from the node's widgets_values), so it reflects
        # what the user selected in ComfyUI - not merely the first option
        # that happens to show the exposed sub. This also keeps the load
        # filter correct (a non-None option). Fall back, only if the
        # workflow value is unavailable, to the exposed sub's option, the
        # spec default, then the first option.
        synth_default = None
        node_wv = _wv_by_node.get(nid) or []
        if node_wv:
            try:
                synth_default = _build_widget_defaults(
                    info, node_wv)[0].get(top_master)
            except Exception:
                synth_default = None
        if synth_default is None:
            sub_show = [str(k) for k in (p.get('_v3_show_for_keys') or [])]
            if sub_show:
                synth_default = sub_show[0]
            elif master_extra.get('default') is not None:
                synth_default = master_extra.get('default')
            elif combo_values:
                synth_default = combo_values[0]
        synth_master = {
            'name':                 top_master,
            'widget_name':          top_master,
            'label':                top_master,
            'type':                 'COMBO',
            'combo_values':         combo_values,
            'default_value':        synth_default,
            'target_node_id':       nid,
            'node_type':            nt,
            'node_title':           p.get('node_title', ''),
            'display_name':         p.get('display_name', ''),
            'is_output':            p.get('is_output', False),
            'role':                 'knob',
            'enabled':              False,
            '_v3_is_dynamic_master': True,
        }
        # Insert the synthesized master right before its first exposed
        # sub, so the cluster appears where the user exposed it (the DFS
        # reorder then pulls the remaining subs up to follow the master).
        # Appending at the end would push the whole cluster to the bottom
        # of the parameter list.
        try:
            _insert_at = params.index(p)
        except ValueError:
            _insert_at = len(params)
        params.insert(_insert_at, synth_master)
        _v3_synth_done.add(key)

    # Seed detection safety gate. Three pass conditions (any suffices):
    #   (a) ComfyUI explicit control_after_generate=True flag on the
    #       widget spec - most reliable, set in main loop above as
    #       p['_has_control_flag']=True.
    #   (b) Workflow JSON has a control_after_generate companion string
    #       ('fixed'/'increment'/'decrement'/'randomize') stored next to
    #       the widget value in widgets_values - detected positionally by
    #       _build_widget_defaults, marked via p['_has_json_companion'].
    #       Unambiguous signal: the user's workflow file itself records
    #       the seed control. Covers cases where /object_info lacks the
    #       explicit flag AND max < 2^32 (e.g. ElevenLabsTextToSpeech.seed
    #       max=2^31-1).
    #   (c) INT + max_value >= 2**32 + 'seed' as standalone token in
    #       widget_name. Name+range heuristic for legacy / synthesized
    #       params lacking both flag and companion.
    # Also apply a name-based fallback for workflows missing the companion.
    for p in params:
        ptype = (p.get('type') or '').upper()
        wname = p.get('widget_name') or p.get('name') or ''
        is_int = (ptype == 'INT')
        try:
            has_huge_range = int(p.get('max_value') or 0) >= _SEED_HUGE_RANGE_MIN
        except (TypeError, ValueError):
            has_huge_range = False
        has_seed_name = _seed_name_has_token(wname)
        has_control_flag = bool(p.get('_has_control_flag'))
        has_json_companion = bool(p.get('_has_json_companion'))
        # Fallback: no companion in JSON but name+type+range match
        if not p.get('is_seed') and is_int and has_huge_range and has_seed_name:
            p['is_seed'] = True
        # Safety: drop is_seed unless one of the three pass conditions holds.
        # Also drop seed_control_default - it's only meaningful when the
        # param survives as a seed (companion knob is created downstream).
        if p.get('is_seed') and not (
                has_control_flag
                or has_json_companion
                or (is_int and has_huge_range and has_seed_name)):
            p.pop('is_seed', None)
            p.pop('seed_control_default', None)

    # Mark file-path params with a dedicated type so the UI shows 'FILE'
    # instead of 'STRING' and gizmo_builder creates File_Knob directly.
    # Applies both to Nuke I/O (role='input'/'output') and to file-like
    # widgets landing in Model Parameters (e.g. VHS_LoadImages.directory).
    # No folder-vs-file distinction: Nuke's File_Knob is the only option -
    # the user picks the correct path manually.
    # Clear default_value for NukomfyRead/NukomfyWrite file paths - these are
    # path overrides in the gizmo, not meant to carry workflow values.
    for p in params:
        wname_lc = (p.get('widget_name') or p.get('name') or '').lower()
        is_str_type = (p.get('type', '').upper() == 'STRING')
        is_file_widget = is_str_type and wname_lc in _FILE_PARAM_NAMES
        is_nukomfy_io = p.get('role') in ('input', 'output')
        if is_nukomfy_io or is_file_widget:
            p['type'] = 'FILE'
            if p.get('node_type') in ('NukomfyRead', 'NukomfyWrite'):
                p.pop('default_value', None)

    # Apply /object_info `display_name` as the default Gizmo Label for
    # any param whose current label is still the snake_case widget_name
    # (i.e. the workflow author did not customize it in the App Builder).
    # Mirrors what the ComfyUI frontend shows on the node, so the
    # Workflow Creator stays consistent with what the author sees in
    # the source workflow. Works for both linearData-exposed widgets
    # (Format 1/2) and auto-added widgets (_add_nukomfy_nonexposed_widgets).
    # No algorithmic prettification because display_name
    # is an explicit author choice in the node schema.
    for p in params:
        nt = p.get('node_type', '')
        info = info_cache.get(nt)
        if not info:
            continue
        wname = p.get('widget_name') or p.get('name') or ''
        if not wname or p.get('label') != wname:
            continue
        input_def = _resolve_widget_input_def(info, wname)
        if not isinstance(input_def, (list, tuple)) or len(input_def) < 2:
            continue
        extra = input_def[1] if isinstance(input_def[1], dict) else {}
        dn = extra.get('display_name') if isinstance(extra, dict) else None
        if dn:
            p['label'] = dn

    # Remove auto-managed widgets (frame range, sequence, preview,
    # padding, etc.) for NukomfyRead/NukomfyWrite - the submit pipeline and
    # Settings handle these. Drop forceInput widgets (marked via _skip)
    # too - they are sockets.
    params[:] = [p for p in params
                 if not p.get('_skip')
                 and p.get('name', '') not in
                 _AUTO_MANAGED_WIDGETS.get(p.get('node_type', ''), set())]

    # Drop params from NukomfyWrite nodes that aren't marked as output.
    # An unmarked NukomfyWrite isn't consumed by the gizmo's submit pipeline,
    # so exposing any of its widgets (even ones the user manually added
    # to linearData.inputs) would produce orphaned knobs.
    params[:] = [p for p in params
                 if not (p.get('node_type') == 'NukomfyWrite'
                         and not p.get('is_output'))]

    # Add non-exposed widgets for NukomfyRead/NukomfyWrite nodes with enabled=False,
    # so the user can opt-in to expose them (or change their default) from
    # the Workflow Creator without having to edit the workflow upstream.
    _add_nukomfy_nonexposed_widgets(params, info_cache, nukomfy_node_meta)

    # Clamp any oversized ints in param dicts - PySide2 setData() crashes
    # on values outside signed int64 range (e.g. ComfyUI max_value=2^64-1)
    _I64_MIN, _I64_MAX = -(2**63), 2**63 - 1
    for p in params:
        for k, v in p.items():
            if isinstance(v, int) and not isinstance(v, bool):
                if v < _I64_MIN or v > _I64_MAX:
                    p[k] = max(_I64_MIN, min(_I64_MAX, v))

    # Normalize V3 sub-knob ordering. linearData preserves
    # the user's App Builder export order, so a non-V3 widget exposed
    # AFTER the master but interleaved with sub-inputs (e.g. scale_method
    # landing between resize_type.crop and resize_type.multiplier) gives
    # a confusing UI. Move every sub of a V3 master to a contiguous block
    # immediately after its master, preserving sub-to-sub relative order.
    # Generic across any V3 master, not specific to ResizeImageMaskNode.
    _v3_normalize_sub_ordering(params)

    return missing_nodes, info_cache


def _v3_normalize_sub_ordering(params):
    """In-place reordering: for every V3 master row, gather all rows with
    `_v3_master == master_widget_name` (same target_node_id) and place
    them as a contiguous block immediately after the master. Sub-to-sub
    relative order preserved. Non-V3 rows untouched apart from being
    pushed to make room for the block.

    A sub can itself be a V3 master (e.g. file_type.exr_compression under
    file_type, whose DWAA/DWAB options carry exr_dw_compression_level), so
    the block emission recurses: a nested master carries its own sub group
    with it. Without the recursion those grandchild rows are grouped under
    the intermediate master's key, never emitted, and dropped from params -
    losing their workflow-saved value (it gets re-synthesised downstream
    with the schema default instead)."""
    # Group subs by (target_node_id, master_widget_name).
    sub_groups = {}
    for p in params:
        master = p.get('_v3_master')
        if not master:
            continue
        nid = p.get('target_node_id')
        sub_groups.setdefault((nid, master), []).append(p)
    if not sub_groups:
        return

    # Build a new ordering: walk params in current order; when we hit a
    # master row, append it followed by its full sub group; skip subs in
    # place (they'll be inserted via their master).
    sub_id_set = set()
    for subs in sub_groups.values():
        for s in subs:
            sub_id_set.add(id(s))

    emitted = set()

    def _emit(p, out):
        # `emitted` guards against a grandchild being placed twice and
        # against pathological self-referential groups looping forever.
        pid = id(p)
        if pid in emitted:
            return
        emitted.add(pid)
        out.append(p)
        nid = p.get('target_node_id')
        wn = p.get('widget_name') or p.get('name') or ''
        for sub in sub_groups.get((nid, wn), []):
            _emit(sub, out)

    new_order = []
    for p in params:
        if id(p) in sub_id_set:
            continue  # placed as part of its master's block
        _emit(p, new_order)

    params[:] = new_order


# ---------------------------------------------------------------------------
# V3 sub-option expansion for the snapshot
# ---------------------------------------------------------------------------
def _expand_v3_subs_to_all_options(params, info_cache):
    """For every V3 master in `params`, append sub-input entries for
    each option OTHER than the master's current value. Entries for the
    current option were already produced by _enrich_params /
    _add_v3_subs_for_master; this fills the gap so the resulting list
    covers every option of every master.

    Default_value for non-current-option subs comes from
    `sub_extra['default']` (spec default from /object_info) because
    workflow.json's widgets_values only carries the current option's
    values.

    Used by the snapshot builder so Reset to Defaults can restore every
    option's values without a server fetch.

    First-seen wins for cross-option shared subs.
    """
    if not params or not info_cache:
        return

    existing = {
        (p.get('target_node_id'),
         p.get('widget_name', p.get('name', '')))
        for p in params}
    by_key = {
        (p.get('target_node_id'),
         p.get('widget_name', p.get('name', ''))): p
        for p in params}

    referenced_masters = set()
    for p in params:
        v3m = p.get('_v3_master', '')
        if v3m:
            referenced_masters.add(
                (p.get('target_node_id'), v3m.split('.', 1)[0]))

    master_entries = []
    for p in params:
        if p.get('_v3_master'):
            continue
        key = (p.get('target_node_id'),
               p.get('widget_name', p.get('name', '')))
        if (p.get('_v3_is_dynamic_master')
                or key in referenced_masters):
            master_entries.append(p)
            continue
        # A generic V3 master with no sub referencing it yet is still a
        # master: its current option may have no subs (e.g. a 'disabled'
        # default) or it was exposed without any of its subs. The Suite
        # path (_add_nukomfy_nonexposed_widgets) does not run for generic
        # nodes, so detect it structurally from object_info here and walk
        # it, so its subs are expanded for every option. Suite masters
        # keep their existing detection via their own path.
        nt = p.get('node_type', '')
        if nt in _NUKOMFY_NODE_TYPES:
            continue
        info = info_cache.get(nt, {}) or {}
        for _section in ('required', 'optional'):
            _spec = (info.get('input', {}) or {}).get(
                _section, {}).get(key[1])
            if (isinstance(_spec, (list, tuple)) and _spec
                    and isinstance(_spec[0], str)
                    and _spec[0].upper() == 'COMFY_DYNAMICCOMBO_V3'):
                master_entries.append(p)
                break

    _WIDGET_TYPE_NAMES = {'INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO',
                          'COMFY_DYNAMICCOMBO_V3'}

    # First-seen counter giving each generic V3 sub a node-order index
    # (object_info option/input order). The editor then lists the subs
    # exactly as they appear on the ComfyUI node, regardless of which
    # subs the user exposed first. Suite subs keep their own ordering.
    _order = [0]

    def _walk(master_p, master_path, master_def, ancestors):
        # Iterates EVERY option of master_def. Unlike
        # _add_v3_subs_for_master (current-value only), there is no
        # current_value parameter - the recursion must descend into
        # nested V3 masters even when their default is None, so
        # sub-of-sub entries (e.g. exr_dw_compression_level under
        # file_type.exr_compression) are captured for every option.
        if (not isinstance(master_def, (list, tuple))
                or len(master_def) < 2):
            return
        master_extra = (master_def[1]
                        if isinstance(master_def[1], dict) else {})
        options = master_extra.get('options', []) or []
        show_for_keys_by_sub = {}
        for opt in options:
            if not isinstance(opt, dict):
                continue
            opt_key = str(opt.get('key', ''))
            if not opt_key:
                continue
            opt_inputs = opt.get('inputs', {}) or {}
            sub_names_set = set()
            for sec in ('required', 'optional'):
                sub_names_set |= set(
                    ((opt_inputs.get(sec, {}) or {}).keys()))
            for sub_name in sub_names_set:
                show_for_keys_by_sub.setdefault(
                    sub_name, []).append(opt_key)

        node_id = master_p.get('target_node_id')
        nt = master_p.get('node_type', '')
        node_title = master_p.get('node_title', '')
        display_name = master_p.get('display_name', '')
        is_output_node = master_p.get('is_output', False)

        for opt in options:
            if not isinstance(opt, dict):
                continue
            opt_inputs = opt.get('inputs', {}) or {}
            for sec in ('required', 'optional'):
                sub_section = opt_inputs.get(sec, {}) or {}
                for sub_name, sub_spec in sub_section.items():
                    full_name = '{}.{}'.format(master_path, sub_name)
                    sub_key = (node_id, full_name)
                    is_dyn_master = (
                        isinstance(sub_spec, (list, tuple))
                        and len(sub_spec) >= 1
                        and isinstance(sub_spec[0], str)
                        and sub_spec[0].upper()
                            == 'COMFY_DYNAMICCOMBO_V3')
                    sub_show = list(
                        show_for_keys_by_sub.get(sub_name, []))
                    if sub_key in existing:
                        existing_sub = by_key.get(sub_key)
                        if (existing_sub is not None
                                and '_v3_sort_index' not in existing_sub):
                            existing_sub['_v3_sort_index'] = _order[0]
                            _order[0] += 1
                        if is_dyn_master:
                            _walk(
                                (existing_sub or master_p),
                                full_name, sub_spec,
                                ancestors + [(master_path, sub_show)])
                        continue
                    if (not isinstance(sub_spec, (list, tuple))
                            or not sub_spec):
                        continue
                    sub_type_or_values = sub_spec[0]
                    sub_extra = (sub_spec[1] if len(sub_spec) > 1
                                 and isinstance(sub_spec[1], dict)
                                 else {})
                    if (isinstance(sub_type_or_values, str)
                            and sub_type_or_values.upper()
                                not in _WIDGET_TYPE_NAMES
                            and not (
                                sub_type_or_values.upper().startswith(
                                    'COMFY_')
                                and 'COMBO'
                                    in sub_type_or_values.upper())):
                        continue
                    sub_label = (sub_extra.get('display_name')
                                 if isinstance(sub_extra, dict)
                                 else None) or sub_name
                    sub_p = {
                        'name':           full_name,
                        'label':          sub_label,
                        'target_node_id': node_id,
                        'node_type':      nt,
                        'node_title':     node_title,
                        'display_name':   display_name,
                        'widget_name':    full_name,
                        'is_output':      is_output_node,
                        'role':           'knob',
                        'enabled':        False,
                        '_v3_master':     master_path,
                        '_v3_sub_name':   sub_name,
                        '_v3_show_for_keys': sub_show,
                    }
                    sub_p['_v3_sort_index'] = _order[0]
                    _order[0] += 1
                    if is_dyn_master:
                        sub_p['_v3_is_dynamic_master'] = True
                    if ancestors:
                        sub_p['_v3_ancestor_conditions'] = [
                            list(a) for a in ancestors]
                    if isinstance(sub_type_or_values, list):
                        if (len(sub_type_or_values) == 1
                                and isinstance(
                                    sub_type_or_values[0], list)
                                and set(str(v)
                                        for v in sub_type_or_values[0])
                                    == {'True', 'False'}):
                            sub_p['type'] = 'BOOLEAN'
                        else:
                            str_vals = [str(v)
                                        for v in sub_type_or_values]
                            if set(str_vals) == {'True', 'False'}:
                                sub_p['type'] = 'BOOLEAN'
                            else:
                                sub_p['combo_values'] = str_vals
                                sub_p['type'] = 'COMBO'
                    elif isinstance(sub_type_or_values, str):
                        sptype = sub_type_or_values.upper()
                        if (sptype in ('COMBO', 'COMFY_DYNAMICCOMBO_V3')
                                and isinstance(sub_extra, dict)
                                and isinstance(
                                    sub_extra.get('options'), list)):
                            opts2 = sub_extra['options']
                            if opts2 and isinstance(opts2[0], dict):
                                sub_p['combo_values'] = [
                                    str(o.get('key', ''))
                                    for o in opts2 if o.get('key')]
                            else:
                                sub_p['combo_values'] = [
                                    str(v) for v in opts2]
                            sub_p['type'] = 'COMBO'
                        else:
                            sub_p['type'] = sptype
                        if (sptype in ('INT', 'FLOAT')
                                and isinstance(sub_extra, dict)):
                            if 'min' in sub_extra:
                                sub_p['min_value'] = sub_extra['min']
                            if 'max' in sub_extra:
                                sub_p['max_value'] = sub_extra['max']
                            if 'display' in sub_extra:
                                sub_p['_display_mode'] = (
                                    sub_extra['display'])
                            if (sptype in ('INT', 'FLOAT')
                                    and 'step' in sub_extra):
                                sub_p['_step'] = sub_extra['step']
                        elif (sptype == 'STRING'
                                and isinstance(sub_extra, dict)):
                            if sub_extra.get('multiline'):
                                sub_p['multiline'] = True
                    if (isinstance(sub_extra, dict)
                            and 'default' in sub_extra):
                        sub_p['default_value'] = sub_extra['default']
                    elif sub_p.get('combo_values'):
                        # A DynamicCombo/Combo sub with no schema default:
                        # mirror the ComfyUI frontend, which selects the
                        # first option. Without this a hidden sub injects
                        # nothing and the server falls back to its own
                        # default (e.g. NukomfyWrite file_type.exr_compression,
                        # the only Suite sub without a schema default, would
                        # otherwise write EXRs with no compression).
                        sub_p['default_value'] = sub_p['combo_values'][0]
                    else:
                        sub_p['default_value'] = None
                    if (isinstance(sub_extra, dict)
                            and sub_extra.get('tooltip')):
                        sub_p['tooltip'] = sub_extra['tooltip']
                    params.append(sub_p)
                    existing.add(sub_key)
                    by_key[sub_key] = sub_p
                    if is_dyn_master:
                        _walk(
                            sub_p, full_name, sub_spec,
                            ancestors + [(master_path, sub_show)])

    for master_p in master_entries:
        nt = master_p.get('node_type', '')
        info = info_cache.get(nt, {}) or {}
        if not info:
            continue
        master_path = master_p.get('widget_name',
                                    master_p.get('name', ''))
        master_def = None
        for section in ('required', 'optional'):
            cand = (info.get('input', {}) or {}).get(section, {}) or {}
            if master_path in cand:
                master_def = cand[master_path]
                break
        if master_def is None:
            continue
        # A top-level widget referenced by V3 subs is a dynamic master
        # even when it was not flagged at App-input parse time: only
        # App-exposed masters get _v3_is_dynamic_master there, so a
        # generic node combo (e.g. a Resize node's resize_type) reaches
        # here flag-less. Back-fill it so the snapshot is consistent and
        # the editor installs the live cascade hook on generic masters,
        # not just Suite ones.
        if (isinstance(master_def, (list, tuple)) and master_def
                and isinstance(master_def[0], str)
                and master_def[0].upper() == 'COMFY_DYNAMICCOMBO_V3'):
            master_p['_v3_is_dynamic_master'] = True
        _walk(master_p, master_path, master_def, [])


# ---------------------------------------------------------------------------
# Snapshot builder (snapshot-based Workflow Editor architecture)
# ---------------------------------------------------------------------------

_SNAPSHOT_SCHEMA_VERSION = 1

# Stripped from each widget when normalising to snapshot: user state
# (toggled / typed in the editor), UI-only helpers, or parse-time
# transient flags - none belong to the server-authoritative widget
# definition. The editor save flow reintroduces user state as
# `_overrides` entries when the user edits them.
_SNAPSHOT_STRIPPED_FIELDS = frozenset({
    'enabled',
    '_intent_enabled',
    'io_mode',
    'write_template',
    'write_template_source',
    '_v3_sort_index',
    '_has_control_flag',
    '_has_json_companion',
    '_missing_node',
})


def _sort_widgets_by_source_order(widgets, workflow_json, info_cache):
    """Order top-level widgets in place to mirror the ComfyUI source:
    nodes in data-flow order (sources -> sinks), and within a node the
    widgets in object_info declaration order - instead of the App
    Builder exposure order.

    V3 sub-inputs are NOT ordered here: they are parked at the tail and
    repositioned right after their master by
    _v3_subs_dfs_order_for_snapshot (which runs next and orders them by
    their _v3_sort_index). Stable sort: widgets with no resolvable rank
    or index keep their relative order, and a node absent from the
    resolved graph (e.g. disconnected, dropped at build) sorts last.
    """
    _BIG = 10 ** 9
    topo = {}
    try:
        from Nukomfy.workflows.workflow_converter import (
            ui_to_api, topological_node_order)
        # Structural pass on a partial /object_info cache: suppress the
        # unmapped-widget warning for the expected non-param nodes.
        topo = topological_node_order(ui_to_api(workflow_json, info_cache or {}, warn_unmapped=False))
    except Exception:
        topo = {}

    order_cache = {}

    def _oi_order(node_type):
        if node_type not in order_cache:
            inp = ((info_cache or {}).get(node_type) or {}).get('input') or {}
            names = (list((inp.get('required') or {}).keys())
                     + list((inp.get('optional') or {}).keys()))
            order_cache[node_type] = {n: i for i, n in enumerate(names)}
        return order_cache[node_type]

    def _key(w):
        if w.get('_v3_master'):
            return (1, _BIG, _BIG)
        nid = str(w.get('target_node_id', ''))
        wname = w.get('widget_name') or w.get('name') or ''
        return (0, topo.get(nid, _BIG),
                _oi_order(w.get('node_type', '')).get(wname, _BIG))

    widgets.sort(key=_key)


def _v3_subs_dfs_order_for_snapshot(widgets):
    """Reorder V3 subs in-place so each sits right after its direct
    master, in DFS top-down order, so the parser emits a deterministic
    order without depending on the UI table.
    """
    masters = set()
    for w in widgets:
        if w.get('_v3_master'):
            continue
        wn = w.get('widget_name', w.get('name', ''))
        masters.add((w.get('target_node_id'), wn))

    children_by_parent = {}
    sub_ids = set()
    for w in widgets:
        v3m = w.get('_v3_master', '')
        if not v3m:
            continue
        top = v3m.split('.', 1)[0]
        nid = w.get('target_node_id')
        if (nid, top) not in masters:
            continue
        children_by_parent.setdefault((nid, v3m), []).append(w)
        sub_ids.add(id(w))

    # Order each master's subs by their node-order index (assigned in
    # _expand_v3_subs_to_all_options from the object_info option/input
    # order) so the editor mirrors the ComfyUI node layout regardless of
    # exposure order. Applies to Suite and generic V3 alike.
    for _subs in children_by_parent.values():
        _subs.sort(key=lambda w: w.get('_v3_sort_index', 0))

    if not children_by_parent:
        return widgets

    def _dfs(nid, parent_path):
        out = []
        for sub in children_by_parent.get((nid, parent_path), []):
            out.append(sub)
            sub_path = sub.get('widget_name', sub.get('name', ''))
            out.extend(_dfs(nid, sub_path))
        return out

    new_widgets = []
    for w in widgets:
        if id(w) in sub_ids:
            continue
        new_widgets.append(w)
        if w.get('_v3_master'):
            continue
        wn = w.get('widget_name', w.get('name', ''))
        nid = w.get('target_node_id')
        if (nid, wn) in masters:
            new_widgets.extend(_dfs(nid, wn))
    widgets[:] = new_widgets
    return widgets


def _normalize_widget_for_snapshot(p):
    """Strip user-state fields from an enriched param. Server-derived
    fields are preserved as-is.

    `enabled` is kept for Suite (NukomfyRead/NukomfyWrite) params and for
    members of a generic V3 cluster (a master or a sub on a non-Suite
    node), so the editor's initial check state reflects the real App
    Builder exposure: parser-added widgets carry enabled=False while
    widgets the user actually exposed have no enabled key and default to
    True. Non-Suite, non-V3 widgets only reach the snapshot when the user
    exposed them, so they keep the default-exposed behavior (enabled
    stripped, defaults to True)."""
    strip = _SNAPSHOT_STRIPPED_FIELDS
    if (p.get('node_type') in _NUKOMFY_NODE_TYPES
            or p.get('_v3_master') or p.get('_v3_is_dynamic_master')):
        strip = _SNAPSHOT_STRIPPED_FIELDS - {'enabled'}
    return {k: v for k, v in p.items() if k not in strip}


def _hash_workflow_json_bytes(workflow_json_bytes):
    """SHA256 of the raw workflow.json bytes. Used by Sync mismatch
    detection: a byte-different source file means the snapshot needs
    re-syncing."""
    import hashlib
    h = hashlib.sha256(workflow_json_bytes).hexdigest()
    return 'sha256:' + h


def _hash_object_info(info_cache):
    """Deterministic SHA256 of the /object_info cache. Keys are sorted
    so the hash is stable across server response orderings."""
    import hashlib
    canonical = json.dumps(info_cache or {}, sort_keys=True,
                           separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _build_snapshot(workflow_json, workflow_json_bytes, info_cache,
                    subgraph_uuids=None, server_urls=None):
    """Produce the snapshot dict for `workflow_json`.

    Output::

        {
          "_snapshot_version": 1,
          "workflow_json_hash": "sha256:<hex>",
          "object_info_hash": "sha256:<hex>",
          "widgets": [ ... DFS-ordered, all V3 options expanded ]
        }

    `workflow_json_bytes`: raw bytes of the source file. Hashing the
    bytes (not a re-serialised dict) so Sync mismatch detection
    triggers on any byte change, including whitespace.

    No `server_url` or `synced_at` fields: the source server is private
    to the user's environment (workflows are often shared publicly) and
    the sync timestamp is already covered by git history of metadata.json.

    Returns ``(snapshot_dict, merged_info_cache, missing_types)``.
    """
    # Fresh parse + enrichment, then order V3 subs via
    # _v3_subs_dfs_order_for_snapshot (below). _v3_normalize_sub_ordering
    # groups V3 subs contiguously after their master during enrich, and
    # recurses so nested sub-of-sub entries survive. That recursion is
    # load-bearing: it keeps the workflow-saved value in params before
    # _expand_v3_subs_to_all_options re-synthesises missing subs with schema
    # defaults. The snapshot re-derives its own order below, so it does not
    # rely on that grouping for ordering.
    widgets = _parse_app_inputs(workflow_json)
    urls = list(server_urls) if server_urls else []
    missing, fresh_cache = _enrich_params(
        widgets, urls, subgraph_uuids)
    merged_cache = dict(info_cache or {})
    if fresh_cache:
        merged_cache.update(fresh_cache)
    _expand_v3_subs_to_all_options(widgets, merged_cache)

    # Default order = ComfyUI source order (node data-flow order + the
    # node's object_info widget order), not App Builder exposure order.
    # V3 subs are repositioned right after their master by the DFS pass
    # next (they are ordered there by their _v3_sort_index).
    _sort_widgets_by_source_order(widgets, workflow_json, merged_cache)
    _v3_subs_dfs_order_for_snapshot(widgets)

    # Tag widgets of non-functional nodes (disconnected/bypassed/muted)
    # after all widgets exist - including non-exposed Read/Write widgets
    # and floating-Read file paths added past the initial parse.
    # `merged_cache` gives the output_node flags the disconnected check
    # needs to mirror ComfyUI's reachability pruning.
    _propagate_node_states(widgets, workflow_json, merged_cache)

    snapshot_widgets = [_normalize_widget_for_snapshot(w) for w in widgets]

    return {
        '_snapshot_version': _SNAPSHOT_SCHEMA_VERSION,
        'workflow_json_hash': _hash_workflow_json_bytes(
            workflow_json_bytes),
        'object_info_hash': _hash_object_info(merged_cache),
        'widgets': snapshot_widgets,
    }, merged_cache, missing


# ---------------------------------------------------------------------------
# Background worker - fetch params from ComfyUI server
# ---------------------------------------------------------------------------
class _ParamWorker(QtCore.QThread):
    """Parse workflow JSON and enrich from running ComfyUI machines.

    Emits a snapshot dict (snapshot-based Workflow Editor architecture):
    the receiver composes effective params from snapshot + overrides
    and drives the cascade off the same snapshot.
    """
    # success, snapshot, error_msg, missing_nodes, non_nukomfy_outputs
    finished = QtCore.Signal(bool, object, str, object, object)

    def __init__(self, json_path, parent=None):
        super().__init__(parent)
        self._json_path = json_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        # 1. Find online servers (fast phase + handle for slow remainder)
        fast_urls, collect_remaining = _find_online_servers()
        if self._cancelled:
            return
        if not fast_urls:
            # No fast responders - wait for the slow ones
            fast_urls = collect_remaining()
        if self._cancelled:
            return
        if not fast_urls:
            from Nukomfy.client.machines import machine_manager
            if not machine_manager.enabled_machines:
                self.finished.emit(False, None,
                    'No machines configured.\n\n'
                    'Add at least one ComfyUI machine in\n'
                    'Settings \u2192 Machines.', [], [])
            else:
                self.finished.emit(False, None,
                    'No ComfyUI machine is currently online.\n\n'
                    'Make sure at least one machine from\n'
                    'Settings \u2192 Machines is running.', [], [])
            return

        # 2. Parse workflow JSON (read raw bytes for the source hash so
        # the snapshot detects byte-different upstream changes)
        try:
            with open(self._json_path, 'rb') as f:
                wf_bytes = f.read()
            wf = json.loads(wf_bytes.decode('utf-8'))
        except Exception as e:
            self.finished.emit(False, None,
                'Cannot read JSON:\n{}'.format(e), [], [])
            return
        if self._cancelled:
            return

        # Gate: workflow must use Nuke Nodes for file I/O
        if not _workflow_has_nukomfy_nodes(wf):
            self.finished.emit(False, None,
                _UNSUPPORTED_WORKFLOW_SENTINEL, [], [])
            return

        # Subgraph definition ids: see _enrich_params docstring.
        subgraph_uuids = {
            sg.get('id')
            for sg in wf.get('definitions', {}).get('subgraphs', []) or []
            if sg.get('id')
        }

        # 3. Build snapshot from fast servers (parse + enrich + expand
        # V3 subs for all options + DFS ordering, all inside
        # _build_snapshot).
        try:
            snapshot, info_cache, missing = _build_snapshot(
                wf, wf_bytes, info_cache=None,
                subgraph_uuids=subgraph_uuids,
                server_urls=fast_urls)
        except Exception as e:
            self.finished.emit(False, None,
                'Cannot parse workflow:\n{}'.format(e), [], [])
            return
        if self._cancelled:
            return

        # 4. If nodes missing, wait for slow servers and retry those types
        if missing:
            extra_urls = collect_remaining()
            new_urls = [u for u in extra_urls if u not in fast_urls]
            if new_urls and not self._cancelled:
                try:
                    snapshot2, info_cache2, missing2 = _build_snapshot(
                        wf, wf_bytes, info_cache=info_cache,
                        subgraph_uuids=subgraph_uuids,
                        server_urls=new_urls)
                    if snapshot2 and snapshot2.get('widgets'):
                        snapshot = snapshot2
                        info_cache = info_cache2
                        missing = missing2
                except Exception:
                    pass
        if self._cancelled:
            return

        if not snapshot or not snapshot.get('widgets'):
            self.finished.emit(False, None,
                'No exposed parameters detected.\n\n'
                'Parameters must be configured in the workflow\n'
                'using the App Builder in ComfyUI.', [], [])
            return

        # Gate B: workflow uses Nuke Nodes but server doesn't have them installed
        if missing & _NUKOMFY_NODE_TYPES:
            self.finished.emit(False, None,
                _NUKOMFY_SUITE_NOT_INSTALLED_SENTINEL, [], [])
            return

        # Gate C: at least one NukomfyWrite must be marked as app output.
        # Two distinct failure modes - different fix instructions for each.
        if not any(t == 'NukomfyWrite' for t in _marked_output_node_types(wf)):
            has_nukomfy_write = any(
                (n.get('type') or '') == 'NukomfyWrite'
                for n in (wf.get('nodes') or []))
            sentinel = (_NO_NUKOMFY_WRITE_OUTPUT_SENTINEL if has_nukomfy_write
                        else _NO_NUKOMFY_WRITE_NODE_SENTINEL)
            self.finished.emit(False, None, sentinel, [], [])
            return

        # Collect non-NukomfyWrite output nodes for a UI warning in the
        # creator (not a gate - NukomfyWrite is already present per gate C).
        non_nukomfy_outputs = _non_nukomfy_output_nodes(wf)

        self.finished.emit(True, snapshot, '',
                           sorted(missing), non_nukomfy_outputs)


def _number_duplicate_labels(items):
    """For params sharing the same label, append `_1`, `_2`... to ALL of them.

    Single occurrences keep their base label unchanged. Input and output
    params should be numbered separately (call this once per group).
    """
    groups = {}
    for p in items:
        groups.setdefault(p.get('label', ''), []).append(p)
    for lbl, grp in groups.items():
        if len(grp) > 1:
            for i, p in enumerate(grp, 1):
                p['label'] = '{}_{}'.format(lbl, i)


def _node_title(node):
    """Return the node title if set by the user, else ''.

    Used for disambiguating multiple nodes of the same type in UI tables
    and gizmo group labels. ComfyUI falls back to the type when no title
    is set, so treating 'title == type' as absent avoids noisy '(NukomfyRead)'
    suffixes on un-renamed nodes.
    """
    t = node.get('title', '')
    if t and t != node.get('type', ''):
        return t
    meta = node.get('_meta', node.get('properties', {}))
    if isinstance(meta, dict):
        mt = meta.get('title', '')
        if mt and mt != node.get('type', ''):
            return mt
    return ''


def _node_display_label(node_type, node_title, display_name=''):
    """Table-cell label: user title, else the ComfyUI display_name,
    else the class type as last resort.

    `display_name` comes from /object_info and matches what the ComfyUI
    frontend shows on each node by default (e.g. "Nuke Write" for the
    class "NukomfyWrite").
    """
    return node_title or display_name or node_type or ''


def _node_cell_tooltip(p):
    """Tooltip for a Node-column cell. A compressed column elides the
    cell label, so the tooltip carries the full label (title/display
    name) alongside the raw class and node id - both the friendly name
    and the technical identity stay reachable on hover. Falls back to
    '<class> (ID: <id>)' when the label is already just the class."""
    custom = p.get('_node_tooltip')
    if custom:
        return custom
    nid = p.get('target_node_id')
    if nid is None:
        return ''
    node_type = p.get('node_type', '')
    label = _node_display_label(node_type, p.get('node_title', ''),
                                p.get('display_name', ''))
    if label and label != node_type:
        return '{} - {} (ID: {})'.format(label, node_type, nid)
    return '{} (ID: {})'.format(node_type, nid)


def _ensure_nukomfy_file_paths(params, nodes_by_id, output_nodes):
    """Auto-add file_path params for NukomfyRead ('input') and NukomfyWrite output
    nodes ('output'). Labels default to the widget name ('file_path') -
    duplicates are numbered later by `_number_duplicate_labels`.

    Floating (image-output-unconnected) NukomfyReads are NOT skipped: their
    file_path is added so the node stays visible in the Inputs tab, then
    greyed+locked by `_propagate_node_states` (disconnected). A
    non-functional node never ships in the gizmo (Enabled forced off)."""
    # Collect node ids that already have a file_path param or an expand placeholder
    covered_nodes = set()
    for p in params:
        nid = p.get('target_node_id')
        wn = p.get('widget_name', p.get('name', ''))
        if wn.lower() in _FILE_PARAM_NAMES:
            covered_nodes.add(nid)
        if p.get('_expand_output') and nid:
            covered_nodes.add(nid)

    for node_id, node in nodes_by_id.items():
        node_type = node.get('type', '')
        if node_type not in _AUTO_FILE_PATH_NODES:
            continue

        expected_role = _AUTO_FILE_PATH_NODES[node_type]

        # For NukomfyWrite, only auto-add if the node is marked as output
        if expected_role == 'output' and node_id not in output_nodes:
            continue

        # Skip if this node already has a file_path param or placeholder
        if node_id in covered_nodes:
            continue

        wv = node.get('widgets_values', [])
        label = 'file_path'

        node_ttl = _node_title(node)
        if expected_role == 'output':
            # Add as _expand_output placeholder - _enrich_params will
            # resolve the actual widget name and default value
            params.append({
                'name':               '_output_placeholder',
                'type':               'STRING',
                'label':              label,
                'target_node_id':     node_id,
                'node_type':          node_type,
                'node_title':         node_ttl,
                'widget_name':        '',
                'default_value':      None,
                '_node_widgets_values': wv if isinstance(wv, (list, dict)) else [],
                'is_output':          True,
                '_expand_output':     True,
            })
        else:
            # NukomfyRead - add file_path directly as input
            params.append({
                'name':           'file_path',
                'type':           'STRING',
                'label':          label,
                'target_node_id': node_id,
                'node_type':      node_type,
                'node_title':     node_ttl,
                'widget_name':    'file_path',
                'default_value':  None,
                '_node_widgets_values': wv if isinstance(wv, (list, dict)) else [],
                'is_output':      False,
                'role':           'input',
            })


def _resolve_widget_input_def(info, widget_name):
    """Lookup a widget's input_def from /object_info, supporting dotted
    V3 sub-input notation (`master.sub`).

    For a standard widget name, returns the entry from
    `info.input.required[widget_name]` (or `optional`).

    For a dotted name `master.sub`: locates `master` as a V3 widget
    (`COMFY_DYNAMICCOMBO_V3`), then iterates `extra.options` and returns
    the sub's input_def from the first option that declares it. This
    handles PrimitiveNode -> Reroute -> Subgraph -> V3 sub-input chains
    that a direct lookup on the dotted key cannot resolve for combo_values.

    Returns None if the widget isn't found.
    """
    if not info or not widget_name:
        return None
    if '.' in widget_name:
        master, _, sub = widget_name.partition('.')
        for section in ('required', 'optional'):
            cand = (info.get('input', {}) or {}).get(section, {}) or {}
            if master not in cand:
                continue
            m_spec = cand[master]
            if not (isinstance(m_spec, (list, tuple)) and m_spec):
                return None
            if not (isinstance(m_spec[0], str)
                    and m_spec[0].startswith('COMFY_')
                    and 'COMBO' in m_spec[0]):
                return None
            m_extra = (m_spec[1]
                       if len(m_spec) > 1 and isinstance(m_spec[1], dict)
                       else {})
            for opt in m_extra.get('options', []) or []:
                if not isinstance(opt, dict):
                    continue
                opt_inputs = opt.get('inputs', {}) or {}
                for opt_section in ('required', 'optional'):
                    sec = opt_inputs.get(opt_section, {}) or {}
                    if sub in sec:
                        return sec[sub]
            return None
        return None
    for section in ('required', 'optional'):
        inputs = (info.get('input', {}).get(section, {}) or {})
        if widget_name in inputs:
            return inputs[widget_name]
    return None


def _extract_combo_values(info, widget_name):
    """Return the list of COMBO option values for a widget on a node.

    Handles both ComfyUI formats:
      - Old:  input_def = [['v1', 'v2', ...], extra]
      - New:  input_def = ['COMBO', {'options': ['v1', 'v2', ...], ...}]
      - V3:   input_def = ['COMFY_DYNAMICCOMBO_V3',
                           {'options': [{'key': '...', 'inputs': {...}}, ...]}]
    Also resolves dotted V3 sub-input notation (`master.sub`).

    Returns None if the widget isn't a recognized COMBO format.
    """
    td = _resolve_widget_input_def(info, widget_name)
    if not isinstance(td, (list, tuple)) or not td:
        return None
    type_or_values = td[0]
    extra = (td[1] if len(td) > 1 and isinstance(td[1], dict) else {})
    if isinstance(type_or_values, list):
        return type_or_values
    if (isinstance(type_or_values, str)
            and type_or_values.upper() in ('COMBO',
                                            'COMFY_DYNAMICCOMBO_V3')
            and isinstance(extra, dict)
            and isinstance(extra.get('options'), list)):
        opts = extra['options']
        if opts and isinstance(opts[0], dict):
            return [o.get('key', '') for o in opts if o.get('key')]
        return list(opts)
    return None


def _build_link_map(workflow_json):
    """Build {link_id: {source_id, source_slot, target_id, target_slot}}.

    Mirrors the link_map built by the workflow_converter.
    Workflow JSON stores links as arrays
    `[link_id, source_id, source_slot, target_id, target_slot, link_type]`.
    """
    link_map = {}
    for link in workflow_json.get('links', []) or []:
        if isinstance(link, list) and len(link) >= 5:
            link_id, source_id, source_slot, target_id, target_slot = link[:5]
            link_map[link_id] = {
                'source_id':   source_id,
                'source_slot': source_slot,
                'target_id':   target_id,
                'target_slot': target_slot,
            }
    return link_map


def _expand_through_reroute(target_id, target_slot, link_map, nodes_by_id,
                            visited=None):
    """Trace a downstream link chain through Reroute nodes.

    Reroute is a frontend-only passthrough with 1 input and N output links
    (fan-out). Returns the list of (real_target_id, real_target_slot)
    tuples reachable from the given (target_id, target_slot) by following
    every Reroute's outputs[0].links, recursively.

    For non-Reroute targets, returns [(target_id, target_slot)] unchanged.
    Cycle protection: visited Reroute ids are tracked across the chain.

    Mirror of trace_through_reroute in workflow_converter.py, but in
    the opposite direction (downstream from a source instead of upstream
    from a consumer).
    """
    if visited is None:
        visited = set()
    target_node = nodes_by_id.get(target_id)
    if not target_node:
        return [(target_id, target_slot)]
    if target_node.get('type') != 'Reroute':
        return [(target_id, target_slot)]
    rid = str(target_id)
    if rid in visited:
        return []
    visited.add(rid)

    real_targets = []
    outputs = target_node.get('outputs') or []
    if not outputs:
        return []
    for downstream_lid in outputs[0].get('links') or []:
        link = link_map.get(downstream_lid)
        if not link:
            continue
        next_tid = link.get('target_id')
        next_tslot = link.get('target_slot')
        if next_tid is None:
            continue
        real_targets.extend(_expand_through_reroute(
            next_tid, next_tslot, link_map, nodes_by_id, set(visited)))
    return real_targets


def _resolve_subgraph_inner_target(subgraph_def, outer_widget_name,
                                   subgraph_defs_by_uuid=None, visited=None):
    """Trace a subgraph instance's outer input slot to its real inner
    consumer(s).

    A subgraph instance node has `inputs[i] = {name: <outer_widget>}`
    that maps to the subgraph definition's `inputs[idx]` (matched by
    name). Each public input has a `linkIds` list referencing internal
    links in `subgraph_def.links` (list of dicts with id, target_id,
    target_slot). Each internal link points at an inner node whose
    `type` is the real ComfyUI class and `inputs[target_slot].name`
    is the real widget name on that class.

    If the inner consumer is itself a subgraph instance (nested
    subgraphs), recurse using `subgraph_defs_by_uuid` to keep walking
    down. Cycle protection via `visited`.

    Returns list of (real_class, real_widget_name) tuples. Empty when
    nothing resolves (orphan subgraph, malformed JSON, etc.).
    """
    if visited is None:
        visited = set()
    sg_id = subgraph_def.get('id')
    if sg_id and sg_id in visited:
        return []
    if sg_id:
        visited.add(sg_id)

    sg_inputs = subgraph_def.get('inputs') or []
    public_idx = None
    for idx, sg_input in enumerate(sg_inputs):
        if sg_input.get('name') == outer_widget_name:
            public_idx = idx
            break
    if public_idx is None:
        return []

    link_ids = sg_inputs[public_idx].get('linkIds') or []
    internal_links_by_id = {
        l.get('id'): l
        for l in subgraph_def.get('links') or []
        if isinstance(l, dict) and l.get('id') is not None
    }
    inner_nodes_by_id = {
        n.get('id'): n
        for n in subgraph_def.get('nodes') or []
        if n.get('id') is not None
    }
    sg_defs = subgraph_defs_by_uuid or {}

    results = []
    for link_id in link_ids:
        link = internal_links_by_id.get(link_id)
        if not isinstance(link, dict):
            continue
        target_id = link.get('target_id')
        target_slot = link.get('target_slot')
        if target_id is None:
            continue
        inner_node = inner_nodes_by_id.get(target_id) or {}
        inner_class = inner_node.get('type', '')
        if not inner_class:
            continue
        # Recurse if the inner node is itself a subgraph instance
        nested_def = sg_defs.get(inner_class)
        if nested_def is not None:
            nested_outer_inputs = inner_node.get('inputs') or []
            if (isinstance(target_slot, int)
                    and 0 <= target_slot < len(nested_outer_inputs)):
                nested_outer_name = nested_outer_inputs[target_slot].get('name', '')
                if nested_outer_name:
                    results.extend(_resolve_subgraph_inner_target(
                        nested_def, nested_outer_name,
                        sg_defs, set(visited)))
            continue
        # Real consumer
        inner_inputs = inner_node.get('inputs') or []
        inner_widget_name = ''
        if (isinstance(target_slot, int)
                and 0 <= target_slot < len(inner_inputs)):
            inner_widget_name = inner_inputs[target_slot].get('name', '') or ''
        if not inner_widget_name:
            inner_widget_name = outer_widget_name
        results.append((inner_class, inner_widget_name))
    return results


def _build_primitive_param(primitive_node, primitive_api_id,
                           link_map, nodes_by_id, api_id_map,
                           subgraph_names_by_uuid=None,
                           subgraph_defs_by_uuid=None):
    """Build a param record for a PrimitiveNode exposed in the App Builder.

    PrimitiveNode is a frontend-only meta-node hosting a primitive value
    (COMBO/INT/FLOAT/STRING/BOOLEAN). Its `outputs[0].name` carries the
    type, `outputs[0].widget.name` carries the destination widget name on
    the connected target(s), and `outputs[0].links` lists the link ids
    feeding all targets (multi-link by-design).

    The returned param keeps `target_node_id = primitive_id` (writeback
    happens via inject_primitive_values into widgets_values[0], which
    the converter then inlines into all targets). For multi-link,
    constraints are intersected across targets in _enrich_params.

    Returns None for orphan primitives (no target connected).
    """
    outputs = primitive_node.get('outputs', [])
    if not outputs:
        return None

    out0 = outputs[0]
    declared_widget_name = (out0.get('widget') or {}).get('name', '')
    if not declared_widget_name:
        return None

    # Resolve all linked targets, computing per-target widget name from
    # the actual connected slot when possible (more reliable than the
    # primitive's declared widget.name on edge-case workflows).
    # Reroute chain expansion: a PrimitiveNode -> Reroute -> Real target
    # configuration is common for visual organisation; each
    # Reroute (and Reroute-of-Reroute) is followed downstream to the
    # real consumer(s). Without this, target_node_type would be 'Reroute'
    # and widget_name would fall back to the primitive's declared name,
    # producing incorrect tooltips, missing constraints (no /object_info
    # for 'Reroute'), and a generic knob label.
    targets_meta = []
    for lid in out0.get('links', []) or []:
        link = link_map.get(lid)
        if not link:
            continue
        target_raw_id = link.get('target_id')
        if target_raw_id is None:
            continue
        target_slot = link.get('target_slot')

        for real_tid, real_tslot in _expand_through_reroute(
                target_raw_id, target_slot, link_map, nodes_by_id):
            target_node = nodes_by_id.get(real_tid, {})
            target_node_type = target_node.get('type', '')
            if not target_node_type:
                continue
            target_api_id = api_id_map.get(real_tid, str(real_tid))

            # Per-target widget name: walk target.inputs[target_slot] if
            # exposed, otherwise fall back to the primitive's declared name.
            wname = declared_widget_name
            target_inputs = target_node.get('inputs', []) or []
            if (isinstance(real_tslot, int)
                    and 0 <= real_tslot < len(target_inputs)):
                slot_def = target_inputs[real_tslot] or {}
                slot_name = slot_def.get('name')
                if slot_name:
                    wname = slot_name

            # Subgraph instance: resolve to the real inner consumer
            # class+widget so /object_info lookup in _enrich_params can
            # populate combo_values / min / max. Display fields stay on
            # the wrapper (UUID -> subgraph name in tooltip).
            sg_defs = subgraph_defs_by_uuid or {}
            real_pairs = []
            if target_node_type in sg_defs:
                real_pairs = _resolve_subgraph_inner_target(
                    sg_defs[target_node_type], wname, sg_defs)

            if real_pairs:
                # One target_meta entry per inner consumer. Display fields
                # share the wrapper's UUID + api_id so the user sees the
                # subgraph name; lookup fields point to the real class.
                for real_cls, real_wn in real_pairs:
                    targets_meta.append({
                        'node_id':           target_api_id,
                        'node_type':         target_node_type,
                        'widget_name':       wname,
                        '_real_node_type':   real_cls,
                        '_real_widget_name': real_wn,
                    })
            else:
                targets_meta.append({
                    'node_id':     target_api_id,
                    'node_type':   target_node_type,
                    'widget_name': wname,
                })

    if not targets_meta:
        return None

    primitive_outputs_name = out0.get('name', 'STRING')
    wv = primitive_node.get('widgets_values', []) or []
    default_value = wv[0] if wv else None
    primitive_title = _node_title(primitive_node)

    # Display: primitive's user-set title if any, otherwise the ComfyUI
    # default node name "Primitive". The target widget name is not used
    # as a fallback - that would misrepresent which node is exposed.
    display = primitive_title or 'Primitive'

    # Composed name: '_'-joined unique widget names of all targets,
    # preserving order of appearance. For multi-link to widgets with
    # different names (e.g. one target.width + one target.height) the
    # Original Name and Gizmo Label become 'width_height' so the user
    # sees what the primitive actually feeds. '_' is the safe Nuke knob
    # name separator.
    unique_widget_names = []
    for t in targets_meta:
        wn = t.get('widget_name', '')
        if wn and wn not in unique_widget_names:
            unique_widget_names.append(wn)
    composed_name = '_'.join(unique_widget_names) or declared_widget_name

    # Node-column tooltip: lists every target with class + id +
    # per-target widget, so the user can audit multi-link primitives
    # at a glance. NOT the same as `tooltip` (which is the auto-detected
    # widget tooltip from /object_info, populated later in _enrich_params).
    # When the target is a subgraph instance, replace the raw UUID type
    # with the subgraph definition's display name (e.g. "Generate Video 1")
    # so the user reads the structural label, not the uuid. A single
    # subgraph wrapper may have multiple inner-consumer entries in
    # targets_meta (one per real consumer); dedupe on (node_id,
    # node_type, widget_name) so the tooltip shows each wrapper once.
    sg_names = subgraph_names_by_uuid or {}
    target_lines = []
    seen_lines = set()
    for t in targets_meta:
        nt = t['node_type']
        nt_display = sg_names.get(nt) or nt
        line = '{} (ID: {}) · {}'.format(
            nt_display, t['node_id'], t['widget_name'])
        if line in seen_lines:
            continue
        seen_lines.add(line)
        target_lines.append(line)
    node_tooltip = 'PrimitiveNode (ID: {})\nTargets:\n  - {}'.format(
        primitive_api_id, '\n  - '.join(target_lines))

    record = {
        'name':                    composed_name,
        'type':                    primitive_outputs_name,
        # label = composed widget name (Original Name pattern, consistent
        # with how non-primitive params set their label). The primitive's
        # display label lives on node_title (Node column).
        'label':                   composed_name,
        'target_node_id':          primitive_api_id,
        # node_type='PrimitiveNode' so the Node column tooltip + tracking
        # reflect what the user actually exposed; per-target types live
        # in _node_tooltip + _primitive_targets.
        'node_type':               'PrimitiveNode',
        'node_title':              display,
        'widget_name':             composed_name,
        'default_value':           default_value,
        '_node_widgets_values':    wv,
        'is_output':               False,
        'role':                    'knob',
        'tooltip':                 '',
        '_node_tooltip':           node_tooltip,
        '_is_primitive':           True,
        '_primitive_targets':      targets_meta,
        '_primitive_outputs_name': primitive_outputs_name,
    }

    # Companion 'fixed'/'increment'/'decrement'/'randomize' at index 1 -
    # mark is_seed pre-emptively. Triple safety filter in _enrich_params
    # (INT + max>=2^32 + seed-name token) drops it for non-seed-like
    # primitives (polymorphic STRING / FLOAT cases). Also propagate the
    # workflow's control value as seed_control_default so gizmo_builder
    # picks it up as the initial control_after_generate state (instead
    # of hardcoded 'randomize').
    if len(wv) >= 2 and isinstance(wv[1], str) and wv[1] in _SEED_CONTROLS:
        record['is_seed'] = True
        record['seed_control_default'] = wv[1]

    return record


def _parse_app_inputs(workflow_json):
    """
    Return list of dicts for each exposed parameter.
    Each dict includes 'role': 'input'|'knob'|'output'.
    """
    # --- Format 1: extra.linearData ---
    # Activate Format 1 when either `inputs` OR `outputs` is populated.
    # This covers workflows that expose outputs in App Builder but no
    # inputs - Format 2 fallback only handles the inputs branch, which
    # would yield NukomfyRead/Write target_node_ids not api-qualified for
    # subgraph-inner nodes (silent file_path drop at submit) and an
    # empty Outputs tab. Format 1 handles both correctly via
    # `api_nodes_by_id`.
    linear = workflow_json.get('extra', {}).get('linearData', {})
    if linear.get('inputs') or linear.get('outputs'):
        nodes_by_id  = _build_flat_nodes_index(workflow_json)
        api_id_map   = _build_api_id_map(workflow_json)
        boundary_labels = _build_subgraph_boundary_labels(workflow_json)
        # Same node index but keyed by API id ("57:8" / "62"), so downstream
        # helpers can iterate and write target_node_id without re-resolving.
        api_nodes_by_id = {}
        for raw_id, node in nodes_by_id.items():
            if isinstance(raw_id, str):
                continue  # avoid double-walking the int+str entries
            api_id = api_id_map.get(raw_id)
            if api_id:
                api_nodes_by_id[api_id] = node
        # Normalize to strings - linearData may use either int or string ids
        # while node dicts declare them as ints. Also remap to API-format ids
        # (subgraph-qualified "outer:inner") so target_node_id matches the
        # flat layout produced by ui_to_api.
        output_nodes = {api_id_map.get(nid, str(nid))
                        for nid in linear.get('outputs', []) or []}

        link_map = _build_link_map(workflow_json)
        floating_nukomfy_reads = _floating_nukomfy_read_ids(workflow_json, api_id_map)
        # Bypassed/muted/disconnected nodes are NOT filtered - they
        # stay visible in the tables but get greyed and have their Enabled
        # checkbox forced off. This preserves the user's explicit App
        # Builder choices. `floating_nukomfy_reads` flags NukomfyReads whose
        # image output is unconnected so the loop below tags their exposed
        # widgets 'disconnected'; their auto-added file_path and non-exposed
        # widgets are tagged later by `_propagate_node_states`.
        node_state_map = _classify_node_states(workflow_json, api_id_map)
        # Map subgraph definition UUIDs -> their display name, so the
        # PrimitiveNode tooltip can show "Generate Video 1" instead of a
        # raw uuid for subgraph-instance targets reached via Reroute.
        # Also keep the full def by UUID so _build_primitive_param can
        # walk into the subgraph and resolve the real inner consumer
        # class+widget for /object_info enrichment (combo_values,
        # min/max). Without this the COMBO knob would only show the
        # current value because the subgraph UUID has no /object_info.
        subgraph_names_by_uuid = {
            sg.get('id'): sg.get('name') or sg.get('id')
            for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []
            if sg.get('id')
        }
        subgraph_defs_by_uuid = {
            sg.get('id'): sg
            for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []
            if sg.get('id')
        }

        result = []
        for entry in linear['inputs']:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                _log.warning('skipping malformed linearData input entry: %r',
                             entry)
                continue
            node_id, widget_name = entry[0], entry[1]
            node = nodes_by_id.get(node_id, {})
            api_id = api_id_map.get(node_id, str(node_id))
            node_state = node_state_map.get(api_id)
            if api_id in floating_nukomfy_reads and node_state is None:
                # NukomfyRead with no downstream consumer that's also not
                # already flagged by _classify_node_states - give it the
                # 'disconnected' label so the user sees why it's greyed.
                node_state = 'disconnected'

            # PrimitiveNode: derive type/widget/constraints from the
            # connected target(s) but keep target_node_id on the primitive
            # itself for pre-conversion writeback. Multi-link comes for free
            # via the converter's primitive_values inlining.
            if node.get('type') == 'PrimitiveNode':
                pp = _build_primitive_param(
                    node, api_id, link_map, nodes_by_id, api_id_map,
                    subgraph_names_by_uuid=subgraph_names_by_uuid,
                    subgraph_defs_by_uuid=subgraph_defs_by_uuid)
                if pp is not None:
                    if node_state:
                        pp['_node_state'] = node_state
                    result.append(pp)
                continue

            # A TOP-LEVEL widget that has since been connected to an input
            # (a Primitive, or any node, now drives it via a link) is no
            # longer an independent widget: ComfyUI feeds it from the link.
            # The connected source is separately exposable (a Primitive gets
            # its own row), so skip this stale App Builder entry - keeping it
            # would create a duplicate knob that silently overrides the
            # source at submit. NOT applied to subgraph-inner widgets (api_id
            # "outer:inner"): an inner widget is ALWAYS link-connected to the
            # subgraph boundary and IS the exposable input (the prompt text /
            # width / steps of a Text-to-Image subgraph), not a duplicate.
            if ':' not in str(api_id):
                _winp = next((i for i in node.get('inputs', [])
                              if i.get('name') == widget_name), None)
                if (_winp is not None and _winp.get('link') is not None
                        and _winp.get('widget')):
                    continue

            inp_type = 'STRING'
            for inp in node.get('inputs', []):
                if inp.get('name') == widget_name:
                    inp_type = inp.get('type', 'STRING')
                    break
            wv = node.get('widgets_values', [])
            # A subgraph-inner widget exposed under a renamed boundary slot
            # (e.g. an inner 'value' surfaced as 'enable_turbo_mode') takes
            # that name as its default Gizmo Label; a miss keeps the raw
            # widget name. Mirrors the subgraphs branch below, which already
            # reads the slot label.
            boundary_label = boundary_labels.get((api_id, widget_name))
            p = {
                'name':           widget_name,
                'type':           inp_type,
                'label':          boundary_label or widget_name,
                'target_node_id': api_id,
                'node_type':      node.get('type', ''),
                'node_title':     _node_title(node),
                'widget_name':    widget_name,
                'default_value':  None,
                '_node_widgets_values': wv if isinstance(wv, (list, dict)) else [],
                'is_output':      api_id in output_nodes,
                '_node_state':    node_state,  # None|'bypassed'|'muted'
            }
            # Dotted-path linearData entry like 'resize_type.crop' is a
            # sub-input of a COMFY_DYNAMICCOMBO_V3 master widget. Tag it for
            # later promotion in _enrich_params (after /object_info fetch
            # confirms `resize_type` is V3 on this node). The split is
            # speculative here; non-V3 dotted names fall back to plain
            # STRING via the existing pseudo-widget skip path.
            if isinstance(widget_name, str) and '.' in widget_name:
                master_name, _, sub_name = widget_name.partition('.')
                if master_name and sub_name:
                    p['_v3_master_candidate'] = master_name
                    p['_v3_sub_name'] = sub_name
                    # Default Gizmo Label for V3 sub-inputs uses just
                    # the sub name. The Original Name column already
                    # shows '↳ sub (master)' so the master prefix in
                    # the label would be redundant. Internal
                    # `name`/`widget_name` keep the dotted form for
                    # matching/save/load. A boundary rename, if present,
                    # still wins (it is the user's explicit name).
                    if not boundary_label:
                        p['label'] = sub_name
            p['role'] = _classify_param(p)
            result.append(p)

        # Auto-add placeholder for output nodes that don't already have a
        # file-path param (the output path).  Even if other widgets of the
        # output node are exposed (e.g. overwrite), we still need the
        # filepath - _enrich_params will discover it via /object_info.
        has_filepath = set()
        for p in result:
            if (str(p.get('target_node_id')) in output_nodes
                    and p.get('name', '').lower() in _FILE_PARAM_NAMES):
                has_filepath.add(str(p['target_node_id']))
        for out_id in output_nodes:
            if out_id in has_filepath:
                continue
            node = api_nodes_by_id.get(out_id)
            if not node:
                continue
            # Only Nuke-managed output types (NukomfyWrite) get a file-path
            # placeholder. SaveImage, PreviewImage, etc. are left alone -
            # their exposed widgets (if any) stay as regular knobs via
            # _classify_param. Coherent with plugin's NukomfyWrite-centric
            # output model.
            if node.get('type', '') not in _AUTO_FILE_PATH_NODES:
                continue
            wv = node.get('widgets_values', [])
            result.append({
                'name':               '_output_placeholder',
                'type':               'STRING',
                'label':              '',
                'target_node_id':     out_id,
                'node_type':          node.get('type', ''),
                'node_title':         _node_title(node),
                'widget_name':        '',
                'default_value':      None,
                '_node_widgets_values': wv if isinstance(wv, (list, dict)) else [],
                'is_output':          True,
                '_expand_output':     True,
                '_node_state':        node_state_map.get(out_id),
            })

        # Auto-detect file_path for NukomfyRead/NukomfyWrite nodes - pass the
        # API-keyed index so target_node_id values stay aligned.
        _ensure_nukomfy_file_paths(result, api_nodes_by_id, output_nodes)

        return result

    # --- Format 2: definitions.subgraphs ---
    try:
        sg = workflow_json.get('definitions', {}).get('subgraphs', [{}])[0]
        links_by_id = {lnk['id']: lnk for lnk in sg.get('links', [])}
        nodes_by_id = {n['id']: n for n in sg.get('nodes', [])}
    except Exception:
        return []

    # In Format 2, orphan filtering is implicit (only nodes with linkIds
    # reach the loop). Bypass/mute flag still useful for UI greying.
    def _fmt2_state(node):
        mode = node.get('mode', 0) or 0
        if mode == 4:
            return 'bypassed'
        if mode == 2:
            return 'muted'
        return None

    result = []
    for inp in sg.get('inputs', []):
        link_ids = inp.get('linkIds', [])
        if not link_ids:
            continue
        lnk = links_by_id.get(link_ids[0])
        if lnk is None:
            continue
        target_id = lnk.get('target_id')
        node = nodes_by_id.get(target_id, {})
        widget_name = inp.get('name', '')
        wv = node.get('widgets_values', [])
        p = {
            'name':           widget_name,
            'type':           inp.get('type', 'STRING'),
            'label':          inp.get('label') or inp.get('localized_name') or widget_name,
            'target_node_id': target_id,
            'node_type':      node.get('type', ''),
            'node_title':     _node_title(node),
            'widget_name':    widget_name,
            'default_value':  None,
            '_node_widgets_values': wv if isinstance(wv, (list, dict)) else [],
            'is_output':      False,
            '_node_state':    _fmt2_state(node),
        }
        p['role'] = _classify_param(p)
        result.append(p)

    for out in sg.get('outputs', []):
        link_ids = out.get('linkIds', [])
        if not link_ids:
            continue
        lnk = links_by_id.get(link_ids[0])
        if lnk is None:
            continue
        source_id = lnk.get('origin_id')
        node = nodes_by_id.get(source_id, {})
        widget_name = out.get('name', '')
        wv = node.get('widgets_values', [])
        p = {
            'name':           widget_name,
            'type':           out.get('type', 'STRING'),
            'label':          out.get('label') or out.get('localized_name') or widget_name,
            'target_node_id': source_id,
            'node_type':      node.get('type', ''),
            'node_title':     _node_title(node),
            'widget_name':    widget_name,
            'default_value':  None,
            '_node_widgets_values': wv if isinstance(wv, (list, dict)) else [],
            'is_output':      True,
            '_node_state':    _fmt2_state(node),
        }
        p['role'] = _classify_param(p)
        result.append(p)

    # Auto-detect file_path for NukomfyRead/NukomfyWrite nodes
    # Determine output node ids from subgraph outputs
    output_node_ids = set()
    for out in sg.get('outputs', []):
        link_ids = out.get('linkIds', [])
        for lid in link_ids:
            lnk = links_by_id.get(lid)
            if lnk:
                output_node_ids.add(lnk.get('origin_id'))
    _ensure_nukomfy_file_paths(result, nodes_by_id, output_node_ids)

    return result
