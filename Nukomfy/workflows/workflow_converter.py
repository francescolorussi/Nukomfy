"""UI to API workflow conversion for ComfyUI graphs.

Modified version of
https://github.com/SethRobinson/comfyui-workflow-to-api-converter-endpoint
"""

import json
import logging

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Frontend-only node types - editor widgets with no server-side implementation.
_VIRTUAL_NODE_TYPES = frozenset({
    'Note', 'MarkdownNote', 'Reroute', 'PrimitiveNode',
    'SetNode', 'GetNode',
})

# LiteGraph node modes that the frontend skips: NEVER (muted) and BYPASS.
# ALWAYS=0, ON_EVENT=1, NEVER=2, ON_TRIGGER=3, BYPASS=4.
_SKIPPED_NODE_MODES = frozenset({2, 4})

# Seed clamping: input accepts up to uint64 max; random range matches the
# frontend's 2^50 (ComfyUI_frontend widgets.js addValueControlWidget).
_SEED_MAX_INPUT = 2**64 - 1
_SEED_MAX_RANDOM = 2**50


# ---------------------------------------------------------------------------
# Fetch /object_info from the ComfyUI server
# ---------------------------------------------------------------------------
def fetch_object_info(server_url, node_types):
    """Return `{node_type: object_info}` for `node_types` from a ComfyUI server.
    Tries bulk /object_info first, falls back to per-type fetches."""
    import urllib.parse
    import urllib.request

    try:
        url = '{}/object_info'.format(server_url.rstrip('/'))
        req = urllib.request.Request(url,
                                     headers={'User-Agent': 'Nukomfy'})
        with urllib.request.urlopen(req, timeout=15) as r:
            all_info = json.loads(r.read().decode())
        return {nt: all_info.get(nt, {}) for nt in node_types}
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
            except Exception: pass

    cache = {}
    for nt in node_types:
        url = '{}/object_info/{}'.format(
            server_url.rstrip('/'), urllib.parse.quote(nt, safe=''))
        try:
            req = urllib.request.Request(url,
                                         headers={'User-Agent': 'Nukomfy'})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode())
            cache[nt] = data.get(nt, data)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
                except Exception: pass
            cache[nt] = {}
    return cache


def live_api_node_ids(api_workflow, object_info):
    """Ids of the nodes in `api_workflow` that actually reach an output.

    ComfyUI executes a node only if it is an OUTPUT_NODE or an ancestor
    (through input links) of one. `ui_to_api` keeps any node that has a
    connected output, so a node feeding only a dead branch survives the
    conversion yet is still pruned at execution. This walks the resolved
    API graph backwards from every output node and returns exactly the
    nodes that influence a result - both the converter-dropped ones
    (already absent here) and the ones present-but-unreachable.

    `object_info` is the {class_type: info} map; the `output_node` flag
    is read from it. Returns the live id set, or None when no output
    node is found (caller must NOT then treat every node as dead).
    """
    if not isinstance(api_workflow, dict) or not api_workflow:
        return None
    object_info = object_info or {}
    adj_rev = {}          # node id -> ids feeding its inputs
    seeds = []
    for nid, node in api_workflow.items():
        if not isinstance(node, dict):
            continue
        cls = node.get('class_type', '')
        if (object_info.get(cls) or {}).get('output_node'):
            seeds.append(nid)
        for v in (node.get('inputs') or {}).values():
            # API link refs are ["<src_node_id>", slot]; widget values
            # are scalars. The string node id discriminates a link from
            # a plain 2-element list value (e.g. an int pair).
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                adj_rev.setdefault(nid, []).append(v[0])
    if not seeds:
        return None
    live = set()
    stack = list(seeds)
    while stack:
        x = stack.pop()
        if x in live:
            continue
        live.add(x)
        stack.extend(adj_rev.get(x, []))
    return live


def topological_node_order(api_workflow):
    """Return `{node_id: rank}` in data-flow order on the resolved API
    graph: source nodes (no upstream) first, sinks last, so callers can
    lay out a node's controls in pipeline reading order.

    Kahn's algorithm. Ties (independent branches ready at the same time)
    are broken by the node id's natural order (numeric when possible),
    so the result is deterministic. Any node left over by a cycle - the
    graph should be a DAG - is appended in that same tie-break order.
    """
    if not isinstance(api_workflow, dict) or not api_workflow:
        return {}

    def _tie_break_key(nid):
        try:
            return (0, int(nid))
        except (ValueError, TypeError):
            return (1, str(nid))

    preds = {nid: set() for nid in api_workflow}
    succ = {nid: [] for nid in api_workflow}
    for nid, node in api_workflow.items():
        if not isinstance(node, dict):
            continue
        for v in (node.get('inputs') or {}).values():
            if (isinstance(v, list) and len(v) == 2
                    and isinstance(v[0], str)
                    and v[0] in api_workflow and v[0] != nid):
                preds[nid].add(v[0])
    for nid, ps in preds.items():
        for p in ps:
            succ[p].append(nid)

    indeg = {nid: len(ps) for nid, ps in preds.items()}
    ready = sorted([n for n, d in indeg.items() if d == 0], key=_tie_break_key)
    rank = {}
    r = 0
    while ready:
        nid = ready.pop(0)
        rank[nid] = r
        r += 1
        newly = False
        for s in succ.get(nid, []):
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
                newly = True
        if newly:
            ready.sort(key=_tie_break_key)
    for nid in sorted(api_workflow.keys(), key=_tie_break_key):
        if nid not in rank:
            rank[nid] = r
            r += 1
    return rank


# ===========================================================================
# UI -> API conversion.
# Field access adapted to `widget_orders` dict (from HTTP /object_info).
# ===========================================================================
def _is_subgraph_uuid(node_type):
    """Check if a node type is a subgraph UUID.
    Subgraphs are identified by UUID format (e.g., "b43bb7e6-178c-4f1a-b014-ac4d6a50fca2")
    """
    if not node_type or not isinstance(node_type, str):
        return False
    # UUIDs are 36 characters with dashes at positions 8, 13, 18, 23
    if len(node_type) == 36 and node_type.count('-') == 4:
        parts = node_type.split('-')
        if len(parts) == 5 and all(len(p) in [8, 4, 4, 4, 12] for i, p in enumerate(parts) if i == 0 or i == 4 or len(p) == 4):
            return True
    return False


def _expand_subgraph(subgraph_node_id, subgraph_def, workflow_links):
    """Expand a subgraph into individual nodes.

    Args:
        subgraph_node_id: The ID of the subgraph node in the main workflow
        subgraph_def: The subgraph definition from definitions.subgraphs
        workflow_links: The links from the main workflow

    Returns:
        Tuple of (expanded_nodes, expanded_links, input_slot_to_internal, internal_to_output_slot)
    """
    expanded_nodes = []
    expanded_links = []

    internal_nodes = subgraph_def.get('nodes', [])
    internal_links = subgraph_def.get('links', [])

    # Find the maximum link ID in the existing workflow links to avoid conflicts
    max_link_id = 0
    for link in workflow_links:
        if isinstance(link, (list, tuple)) and len(link) > 0:
            link_id = link[0]
            if isinstance(link_id, int) and link_id > max_link_id:
                max_link_id = link_id

    # Create a remapping for internal link IDs to avoid conflicts
    link_id_remap = {}
    next_link_id = max_link_id + 1
    for link in internal_links:
        if isinstance(link, dict):
            old_id = link.get('id')
            if old_id is not None:
                link_id_remap[old_id] = next_link_id
                next_link_id += 1

    # Build a mapping of internal link IDs to link data
    internal_link_map = {}
    for link in internal_links:
        if isinstance(link, dict):
            link_id = link.get('id')
            internal_link_map[link_id] = link

    subgraph_inputs = subgraph_def.get('inputs', [])
    subgraph_outputs = subgraph_def.get('outputs', [])

    input_slot_to_internal = {}   # slot_index -> [(target_node_id, target_slot), ...]
    internal_to_output_slot = {}  # (source_node_id, source_slot) -> slot_index

    for idx, input_def in enumerate(subgraph_inputs):
        input_link_ids = input_def.get('linkIds', [])
        targets = []
        for link_id in input_link_ids:
            if link_id in internal_link_map:
                link = internal_link_map[link_id]
                target_id = link.get('target_id')
                target_slot = link.get('target_slot')
                targets.append((target_id, target_slot))
        if targets:
            input_slot_to_internal[idx] = targets

    for idx, output_def in enumerate(subgraph_outputs):
        output_link_ids = output_def.get('linkIds', [])
        for link_id in output_link_ids:
            if link_id in internal_link_map:
                link = internal_link_map[link_id]
                origin_id = link.get('origin_id')
                origin_slot = link.get('origin_slot')
                internal_to_output_slot[(origin_id, origin_slot)] = idx

    # Create expanded nodes with prefixed IDs
    for node in internal_nodes:
        internal_id = node.get('id')
        expanded_node = node.copy()
        expanded_node['id'] = f"{subgraph_node_id}:{internal_id}"

        if 'inputs' in expanded_node:
            updated_inputs = []
            for input_info in expanded_node['inputs']:
                input_link = input_info.get('link')
                if input_link in internal_link_map:
                    link_data = internal_link_map[input_link]
                    if link_data.get('origin_id') == -10:
                        input_copy = input_info.copy()
                        input_copy['link'] = None
                        updated_inputs.append(input_copy)
                    else:
                        input_copy = input_info.copy()
                        if input_link in link_id_remap:
                            input_copy['link'] = link_id_remap[input_link]
                        updated_inputs.append(input_copy)
                else:
                    updated_inputs.append(input_info)
            expanded_node['inputs'] = updated_inputs

        expanded_nodes.append(expanded_node)

    # Create expanded links (internal only, skipping input/output placeholder nodes)
    for link in internal_links:
        if isinstance(link, dict):
            origin_id = link.get('origin_id')
            target_id = link.get('target_id')
            if origin_id in [-10, -20] or target_id in [-10, -20]:
                continue
            old_link_id = link.get('id')
            new_link_id = link_id_remap.get(old_link_id, old_link_id)
            expanded_link = [
                new_link_id,
                f"{subgraph_node_id}:{origin_id}",
                link.get('origin_slot'),
                f"{subgraph_node_id}:{target_id}",
                link.get('target_slot'),
                link.get('type')
            ]
            expanded_links.append(expanded_link)

    return expanded_nodes, expanded_links, input_slot_to_internal, internal_to_output_slot


def _subgraph_widget_targets(subgraph_def):
    """Map each widget-backed input of a subgraph to the inner widgets it feeds.

    Returns ``{input_name: [(inner_node, widget_name), ...]}`` in definition
    order, listing only the inputs that reach at least one inner widget: the
    ones the frontend promotes onto the instance node, which shows the first
    target.
    """
    inner_nodes = {n.get('id'): n for n in subgraph_def.get('nodes', []) or []}
    links = {
        lnk.get('id'): lnk for lnk in subgraph_def.get('links', []) or []
        if isinstance(lnk, dict)
    }
    targets = {}
    for sg_input in subgraph_def.get('inputs', []) or []:
        feeds = []
        for link_id in sg_input.get('linkIds', []) or []:
            link = links.get(link_id) or {}
            inner = inner_nodes.get(link.get('target_id'))
            slot = link.get('target_slot')
            inner_inputs = (inner or {}).get('inputs') or []
            if not isinstance(slot, int) or not 0 <= slot < len(inner_inputs):
                continue
            widget_name = (inner_inputs[slot].get('widget') or {}).get('name')
            if widget_name:
                feeds.append((inner, widget_name))
        if feeds:
            targets[sg_input.get('name')] = feeds
    return targets


def _promoted_host_values(host_node, subgraph_def, include_linked=False):
    """Return ``{(inner_id, widget_name): value}`` for the promoted widgets
    whose value lives on the subgraph instance node.

    From frontend 1.46.3 the instance stores a promoted widget's value in
    `widgets_values` (one entry per widget-backed input, in definition order),
    the inner widget keeps a stale copy, and the frontend sends the instance
    value to every inner widget the input feeds; files from older frontends
    leave the list empty or mirror the inner values. Inputs linked on the
    instance are left to the link unless `include_linked` is set (a nested
    instance's input is linked from the enclosing one), and a length mismatch
    yields {} rather than a guessed alignment.
    """
    values = host_node.get('widgets_values')
    targets = _subgraph_widget_targets(subgraph_def)
    if not isinstance(values, list) or not values or len(values) != len(targets):
        return {}
    linked = set() if include_linked else {
        inp.get('name') for inp in host_node.get('inputs', []) or []
        if inp.get('link') is not None
    }
    result = {}
    for (input_name, feeds), value in zip(targets.items(), values):
        if value is None or input_name in linked:
            continue
        for inner, widget_name in feeds:
            result[(inner.get('id'), widget_name)] = value
    return result


def _promoted_socket_feeds(host_node, subgraph_def):
    """Return ``[(socket_id, input_name, widget_id, widget_name)]``: the inner
    sockets fed by a promoted input left unlinked on the instance, each with
    the first inner widget that input feeds.

    The frontend sends such a socket the input's value, which that widget
    receives too: the instance's, or in older files the widget's own.
    """
    widgets = _subgraph_widget_targets(subgraph_def)
    linked = {
        inp.get('name') for inp in host_node.get('inputs', []) or []
        if inp.get('link') is not None
    }
    result = []
    for sg_input in subgraph_def.get('inputs', []) or []:
        name = sg_input.get('name')
        if name not in widgets or name in linked:
            continue
        first, widget_name = widgets[name][0]
        link_ids = set(sg_input.get('linkIds', []) or [])
        for inner in subgraph_def.get('nodes', []) or []:
            for inp in inner.get('inputs', []) or []:
                if inp.get('link') in link_ids and not inp.get('widget'):
                    result.append((inner.get('id'), inp.get('name'),
                                   first.get('id'), widget_name))
    return result


def _promoted_widget_chain(subgraph_def, input_name, subgraph_defs):
    """Return ``[(path, node, widget_name), ...]``: the first inner widget a
    promoted input feeds and, while that is the input of a nested instance,
    the first widget that one feeds, down to the widget the frontend shows
    for the input. ``path`` is the node's id path inside the instance
    ('3', '3:7'); the list is empty when the input feeds no widget.
    """
    chain = []
    path = ''
    for _ in range(10):     # ui_to_api expands ten levels
        feeds = _subgraph_widget_targets(subgraph_def).get(input_name)
        if not feeds:
            break
        node, input_name = feeds[0]
        path = '{}:{}'.format(path, node.get('id')) if path else str(node.get('id'))
        chain.append((path, node, input_name))
        node_type = node.get('type')
        subgraph_def = (subgraph_defs.get(node_type)
                        if isinstance(node_type, str) else None)
        if subgraph_def is None:
            break
    return chain


def _param_key(p):
    """The (node id, input) a param's own value is written to."""
    return (str(p.get('target_node_id', '')),
            p.get('widget_name', p.get('name', '')))


def param_value_targets(workflow_json, gizmo_params):
    """Return ``{(node_id, widget_name): [(node_id, input_name), ...]}``: the
    inputs of the prompt a param's value is written to, for each param whose
    value the frontend gives to inputs other than its own target.

    An input promoted on an instance in the root graph and left unlinked gives
    its value to each inner widget and socket it feeds, down through nested
    instances. A param on that input, or on the first inner widget it feeds
    and on down through nested instances (_promoted_widget_chain: what the
    parser builds from an App Builder entry, and what the frontend upgrades a
    legacy entry to), carries its value to all of them; one on another of
    them keeps its own. An inner widget no subgraph input feeds is
    one widget of its definition, shared by all of its instances. An input
    another param is on keeps that param's value.

    Left out, as the parser gives them the value saved on the inner node
    rather than the one the frontend holds: the NukomfyRead / NukomfyWrite
    widgets it adds by itself, the sub-inputs of a DynamicCombo, the own value
    of a nested instance (the frontend drops App Builder entries on it).
    """
    defs = {
        sg.get('id'): sg
        for sg in (workflow_json.get('definitions') or {}).get('subgraphs') or []
        if sg.get('id')
    }
    if not defs:
        return {}

    def definition(node):
        node_type = node.get('type')
        return defs.get(node_type) if isinstance(node_type, str) else None

    source_of = {}      # input -> the root instance input it takes the value of
    members = {}        # root instance input -> the inputs that take its value
    heads = {}          # root instance input -> the keys that stand for it
    definition_of = {}  # instance path -> its definition
    instances = {}      # definition id -> its instance paths

    # fed: for a nested instance, the source of each input the enclosing
    # instance passes down; None for an instance in the root graph.
    def walk(path, sg, host, fed, depth):
        if depth >= 10:     # ui_to_api expands ten levels
            return
        definition_of[path] = sg
        instances.setdefault(sg.get('id'), []).append(path)
        nodes = {n.get('id'): n for n in sg.get('nodes') or []}
        links = {
            lnk.get('id'): lnk for lnk in sg.get('links') or []
            if isinstance(lnk, dict)
        }
        linked = {
            inp.get('name') for inp in host.get('inputs') or []
            if inp.get('link') is not None
        }
        backed = _subgraph_widget_targets(sg)
        own = {}
        for sg_input in sg.get('inputs') or []:
            name = sg_input.get('name')
            if fed is not None:
                source = fed.get(name)
            elif name in backed and name not in linked:
                source = (path, name)
                heads[source] = {source} | {
                    ('{}:{}'.format(path, inner_path), widget)
                    for inner_path, _, widget in _promoted_widget_chain(sg, name, defs)}
            else:
                continue
            if source is None:
                continue
            own[name] = source
            source_of[(path, name)] = source
            for link_id in sg_input.get('linkIds') or []:
                link = links.get(link_id) or {}
                inner = nodes.get(link.get('target_id'))
                slot = link.get('target_slot')
                if (inner is None or definition(inner) is not None
                        or inner.get('type') == 'Reroute'
                        or not isinstance(slot, int)
                        or not 0 <= slot < len(inner.get('inputs') or [])):
                    continue
                key = ('{}:{}'.format(path, inner.get('id')),
                       inner['inputs'][slot].get('name'))
                if key not in source_of:
                    source_of[key] = source
                    members.setdefault(source, []).append(key)
        sg_inputs = sg.get('inputs') or []
        for node in sg.get('nodes') or []:
            nested = definition(node)
            if nested is None:
                continue
            nested_fed = {}
            for inp in node.get('inputs') or []:
                link = links.get(inp.get('link')) or {}
                slot = link.get('origin_slot')
                if (link.get('origin_id') == -10 and isinstance(slot, int)
                        and 0 <= slot < len(sg_inputs)):
                    nested_fed[inp.get('name')] = own.get(
                        sg_inputs[slot].get('name'))
            walk('{}:{}'.format(path, node.get('id')), nested, node,
                 nested_fed, depth + 1)

    for node in workflow_json.get('nodes') or []:
        sg = definition(node)
        if sg is not None:
            walk(str(node.get('id')), sg, node, None, 0)

    params = [p for p in gizmo_params
              if not p.get('_is_primitive') and p.get('role', 'knob') == 'knob']
    taken = {_param_key(p) for p in params}
    result = {}
    for p in params:
        if (p.get('node_type') in ('NukomfyRead', 'NukomfyWrite')
                or p.get('_v3_master')):
            continue
        key = _param_key(p)
        source = source_of.get(key)
        if source is not None:
            if key not in heads.get(source, ()):
                continue
            targets = members.get(source, [])
        else:
            path, _, inner_id = key[0].rpartition(':')
            sg = definition_of.get(path) or {}
            node = next((n for n in sg.get('nodes') or []
                         if str(n.get('id')) == inner_id), None)
            if node is None or definition(node) is not None or any(
                    inp.get('name') == key[1] and inp.get('link') is not None
                    for inp in node.get('inputs') or []):
                continue
            targets = [('{}:{}'.format(other, inner_id), key[1])
                       for other in instances[sg.get('id')]]
        targets = [t for t in targets if t == key or t not in taken]
        if targets and targets != [key]:
            result[key] = targets
    return result


def is_api_format(workflow):
    """Check if a workflow is already in API format.
    API format has node IDs as keys with 'class_type' and 'inputs'.
    Non-API format has 'nodes', 'links', etc.
    """
    if 'nodes' in workflow and 'links' in workflow:
        return False

    for key, value in workflow.items():
        if key in ['prompt', 'extra_data', 'client_id']:
            continue
        if isinstance(value, dict) and 'class_type' in value:
            return True

    return False


def ui_to_api(workflow_json, widget_orders, warn_unmapped=True):
    """Convert a non-API (UI-format) workflow to API format.

    Args:
        workflow_json: UI-format workflow with `nodes`, `links`, optionally
            `definitions.subgraphs`.
        widget_orders: dict {node_type: object_info_dict} as returned by
            fetch_object_info. Missing entries -> best-effort fallback
            (positional widget mapping, no default injection for that type).
        warn_unmapped: when True (submit path), log a warning for any node
            whose widget values cannot be mapped for lack of /object_info -
            a genuine "node type unknown to the server" signal. The creator's
            internal structural passes fetch /object_info only for the
            param-bearing nodes, so they pass False to avoid false positives.

    Returns:
        API format workflow ready for execution: {node_id: {class_type, inputs, _meta}}.
    """
    if is_api_format(workflow_json):
        return workflow_json

    workflow_nodes = workflow_json.get('nodes', [])
    links = workflow_json.get('links', [])

    # Extract subgraph definitions
    subgraph_defs = {}
    definitions = workflow_json.get('definitions', {})
    for subgraph in definitions.get('subgraphs', []):
        subgraph_id = subgraph.get('id')
        if subgraph_id:
            subgraph_defs[subgraph_id] = subgraph

    # Expand subgraphs into individual nodes (recursive, max_iterations guard)
    subgraph_input_mappings = {}
    subgraph_output_mappings = {}
    subgraph_slot_to_input_idx = {}
    # Expanded inner node id -> {widget_name: value held by its instance}.
    promoted_values = {}
    # (socket id, input name, first widget id, widget name) for each inner
    # socket fed by a promoted input.
    promoted_sockets = []
    # Expanded instance id -> the types of its inputs, by slot.
    instance_input_types = {}

    max_iterations = 10
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        expanded_nodes = []
        found_subgraph = False

        for node in workflow_nodes:
            node_type = node.get('type')
            node_id = node.get('id')

            # A muted or bypassed instance stays one skipped node, as in the
            # frontend: none of its inner nodes are sent, and a bypass passes
            # its inputs through (trace_through_bypassed).
            if (_is_subgraph_uuid(node_type) and node_type in subgraph_defs
                    and node.get('mode', 0) not in _SKIPPED_NODE_MODES):
                found_subgraph = True
                subgraph_def = subgraph_defs[node_type]
                sg_nodes, sg_links, input_map, output_map = _expand_subgraph(
                    node_id, subgraph_def, links
                )
                for (inner_id, widget_name), value in _promoted_host_values(
                        node, subgraph_def).items():
                    promoted_values.setdefault(
                        f"{node_id}:{inner_id}", {})[widget_name] = value
                for socket_id, name, widget_id, widget_name in (
                        _promoted_socket_feeds(node, subgraph_def)):
                    promoted_sockets.append((f"{node_id}:{socket_id}", name,
                                             f"{node_id}:{widget_id}", widget_name))

                expanded_nodes.extend(sg_nodes)
                links.extend(sg_links)

                subgraph_input_mappings[str(node_id)] = input_map
                subgraph_output_mappings[str(node_id)] = output_map

                outer_inputs = node.get('inputs', [])
                subgraph_inputs = subgraph_def.get('inputs', [])
                instance_input_types[str(node_id)] = [
                    inp.get('type') for inp in outer_inputs]

                subgraph_input_name_to_idx = {}
                for idx, sg_input in enumerate(subgraph_inputs):
                    input_name = sg_input.get('name')
                    if input_name:
                        subgraph_input_name_to_idx[input_name] = idx

                slot_mapping = {}
                for outer_slot, outer_input in enumerate(outer_inputs):
                    outer_name = outer_input.get('name')
                    if outer_name and outer_name in subgraph_input_name_to_idx:
                        slot_mapping[outer_slot] = subgraph_input_name_to_idx[outer_name]

                subgraph_slot_to_input_idx[str(node_id)] = slot_mapping
            else:
                expanded_nodes.append(node)

        workflow_nodes = expanded_nodes

        if not found_subgraph:
            break

    if iteration >= max_iterations:
        _log.warning(
            'Workflow subgraph expansion hit max iterations (%d) - possible circular references',
            max_iterations)

    # Helper: recursively resolve subgraph outputs
    def resolve_subgraph_output(node_id_str, slot, depth=0):
        if depth > 100:
            _log.warning(
                'Workflow subgraph output recursion exceeded 100 levels at node %s',
                node_id_str)
            return (node_id_str, slot)
        if node_id_str in subgraph_output_mappings:
            output_map = subgraph_output_mappings[node_id_str]
            for (internal_node, internal_slot), out_slot in output_map.items():
                if out_slot == slot:
                    new_node_id = f"{node_id_str}:{internal_node}"
                    return resolve_subgraph_output(new_node_id, internal_slot, depth + 1)
        return (node_id_str, slot)

    # Helper: recursively resolve subgraph inputs (first target only)
    def resolve_subgraph_input(node_id_str, slot, depth=0):
        if depth > 100:
            _log.warning(
                'Workflow subgraph input recursion exceeded 100 levels at node %s',
                node_id_str)
            return (node_id_str, slot)
        if node_id_str in subgraph_input_mappings:
            input_map = subgraph_input_mappings[node_id_str]
            subgraph_input_idx = slot
            if node_id_str in subgraph_slot_to_input_idx:
                slot_mapping = subgraph_slot_to_input_idx[node_id_str]
                if slot in slot_mapping:
                    subgraph_input_idx = slot_mapping[slot]
            if subgraph_input_idx in input_map:
                targets = input_map[subgraph_input_idx]
                if targets:
                    internal_node, internal_slot = targets[0]
                    new_node_id = f"{node_id_str}:{internal_node}"
                    return resolve_subgraph_input(new_node_id, internal_slot, depth + 1)
        return (node_id_str, slot)

    # Helper: recursively resolve subgraph inputs - ALL targets
    def resolve_subgraph_input_all(node_id_str, slot, depth=0):
        if depth > 100:
            _log.warning(
                'Workflow subgraph input recursion exceeded 100 levels at node %s',
                node_id_str)
            return [(node_id_str, slot)]
        if node_id_str in subgraph_input_mappings:
            input_map = subgraph_input_mappings[node_id_str]
            subgraph_input_idx = slot
            if node_id_str in subgraph_slot_to_input_idx:
                slot_mapping = subgraph_slot_to_input_idx[node_id_str]
                if slot in slot_mapping:
                    subgraph_input_idx = slot_mapping[slot]
            if subgraph_input_idx in input_map:
                targets = input_map[subgraph_input_idx]
                results = []
                for internal_node, internal_slot in targets:
                    new_node_id = f"{node_id_str}:{internal_node}"
                    results.extend(resolve_subgraph_input_all(new_node_id, internal_slot, depth + 1))
                return results if results else [(node_id_str, slot)]
        return [(node_id_str, slot)]

    # Update links to handle subgraph inputs and outputs
    node_input_updates = {}
    # Link entering an expanded instance -> the type of the instance input:
    # the frontend resolves what the link carries with that type, not with
    # the type of the inner input it is rewired to.
    boundary_types = {}

    updated_links = []
    for link in links:
        if len(link) >= 6:
            link_id = link[0]
            source_id = link[1]
            source_slot = link[2]
            target_id = link[3]
            target_slot = link[4]
            link_type = link[5] if len(link) > 5 else None

            source_id_str = str(source_id)
            source_id, source_slot = resolve_subgraph_output(source_id_str, source_slot)

            target_id_str = str(target_id)
            all_targets = resolve_subgraph_input_all(target_id_str, target_slot)
            instance_types = instance_input_types.get(target_id_str) or []
            if isinstance(target_slot, int) and 0 <= target_slot < len(instance_types):
                boundary_types[link_id] = instance_types[target_slot]

            for resolved_target_id, resolved_target_slot in all_targets:
                if resolved_target_id != target_id_str:
                    if resolved_target_id not in node_input_updates:
                        node_input_updates[resolved_target_id] = {}
                    node_input_updates[resolved_target_id][resolved_target_slot] = link_id

            resolved_target_id, resolved_target_slot = all_targets[0]
            target_id = resolved_target_id
            target_slot = resolved_target_slot

            updated_links.append([link_id, source_id, source_slot, target_id, target_slot, link_type])

    links = updated_links

    # Update the expanded nodes' inputs to reference the external link IDs
    for node in workflow_nodes:
        node_id_str = str(node.get('id'))
        if node_id_str in node_input_updates and 'inputs' in node:
            slot_to_link = node_input_updates[node_id_str]
            inputs = node.get('inputs', [])
            for i, input_info in enumerate(inputs):
                if i in slot_to_link:
                    input_info['link'] = slot_to_link[i]

    # Build link map
    link_map = {}
    nodes_with_connected_outputs = set()

    for link in links:
        if len(link) >= 6:
            link_id = link[0]
            source_id = link[1]
            source_slot = link[2]
            target_id = link[3]
            target_slot = link[4]
            link_type = link[5] if len(link) > 5 else None
            link_map[link_id] = {
                'source_id': source_id,
                'source_slot': source_slot,
                'target_id': target_id,
                'target_slot': target_slot,
                'type': link_type
            }
            nodes_with_connected_outputs.add(source_id)

    # First pass: identify virtual/routing nodes and excluded nodes
    primitive_values = {}
    nodes_to_exclude = set()
    bypassed_nodes = set()

    set_node_sources = {}   # variable_name -> (source_node_id, source_slot)
    get_node_vars = {}      # node_id (str) -> variable_name
    reroute_sources = {}    # node_id_str -> (source_node_id, source_slot)

    node_by_id = {}
    for node in workflow_nodes:
        node_by_id[str(node.get('id'))] = node

    for node in workflow_nodes:
        node_id = node.get('id')
        node_type = node.get('type')
        node_mode = node.get('mode', 0)

        if node_mode == 4:
            bypassed_nodes.add(str(node_id))

        if node_type == 'PrimitiveNode':
            node_id_str = str(node_id)
            widget_values = node.get('widgets_values')
            if widget_values and len(widget_values) > 0:
                primitive_values[node_id_str] = widget_values[0]

        elif node_type == 'SetNode':
            widget_values = node.get('widgets_values')
            if widget_values and len(widget_values) > 0:
                var_name = widget_values[0]
                node_inputs = node.get('inputs', [])
                if node_inputs:
                    for input_info in node_inputs:
                        input_link = input_info.get('link')
                        if input_link is not None and input_link in link_map:
                            link_data = link_map[input_link]
                            set_node_sources[var_name] = (link_data['source_id'], link_data['source_slot'])
                            break

        elif node_type == 'GetNode':
            widget_values = node.get('widgets_values')
            if widget_values and len(widget_values) > 0:
                var_name = widget_values[0]
                get_node_vars[str(node_id)] = var_name

        elif node_type == 'Reroute':
            node_inputs = node.get('inputs', [])
            if node_inputs:
                input_link = node_inputs[0].get('link')
                if input_link is not None and input_link in link_map:
                    link_data = link_map[input_link]
                    reroute_sources[str(node_id)] = (link_data['source_id'], link_data['source_slot'])

        # Decide whether this node should be excluded from the API format
        outputs = node.get('outputs', [])
        has_connected_output = False
        for output in outputs:
            output_links = output.get('links', [])
            if output_links and len(output_links) > 0:
                has_connected_output = True
                break

        if node_type == 'LoadImageOutput':
            nodes_to_exclude.add(str(node_id))
        elif not outputs or not has_connected_output:
            # widget_orders carries the /object_info data (no in-process NODE_CLASS_MAPPINGS).
            node_info = widget_orders.get(node_type) or {}
            is_output_node = bool(node_info.get('output_node'))

            # Fallback: if node info isn't available but node has connected inputs,
            # it's likely an output node that should be kept
            has_connected_input = False
            if not is_output_node and not node_info:
                node_inputs = node.get('inputs', [])
                for input_info in node_inputs:
                    if input_info.get('link') is not None:
                        has_connected_input = True
                        break

            if not is_output_node and not has_connected_input:
                nodes_to_exclude.add(str(node_id))

    # Helper: trace through GetNode/SetNode pairs
    def trace_through_get_set_nodes(source_node_id, source_slot, visited=None):
        if visited is None:
            visited = set()
        source_node_id_str = str(source_node_id)
        if source_node_id_str in visited:
            return (source_node_id, source_slot)
        visited.add(source_node_id_str)

        if source_node_id_str in get_node_vars:
            var_name = get_node_vars[source_node_id_str]
            if var_name in set_node_sources:
                actual_source_id, actual_source_slot = set_node_sources[var_name]
                return trace_through_get_set_nodes(actual_source_id, actual_source_slot, visited)

        return (source_node_id, source_slot)

    # Helper: trace through bypassed nodes. The input passed on is the one the
    # frontend picks (_bypass_input_index on _frontend_inputs); when it is
    # unlinked, or there is none, the source stays the bypassed node and the
    # socket gets nothing. Upstream, the target type becomes the picked
    # input's, or the instance input's where the link enters a subgraph.
    def trace_through_bypassed(source_node_id, source_slot, target_type, visited=None):
        if visited is None:
            visited = set()
        if source_node_id in visited:
            return (source_node_id, source_slot)
        visited.add(source_node_id)

        if source_node_id not in bypassed_nodes:
            return (source_node_id, source_slot)

        node = node_by_id.get(str(source_node_id), {})
        inputs = _frontend_inputs(node, widget_orders,
                                  subgraph_defs.get(node.get('type')))
        index = _bypass_input_index(inputs, node.get('outputs') or [],
                                    source_slot, target_type)
        if not 0 <= index < len(inputs):
            return (source_node_id, source_slot)
        input_link = inputs[index].get('link')
        if input_link is None or input_link not in link_map:
            return (source_node_id, source_slot)

        link_data = link_map[input_link]
        upstream_source_id, upstream_source_slot = trace_through_get_set_nodes(
            link_data['source_id'], link_data['source_slot']
        )
        upstream_source_id, upstream_source_slot = trace_through_reroute(
            upstream_source_id, upstream_source_slot
        )
        # A PrimitiveNode gives its value to the widget it feeds only: the
        # frontend resolves nothing through it, so nothing passes the bypass.
        if str(upstream_source_id) in primitive_values:
            return (source_node_id, source_slot)
        return trace_through_bypassed(
            upstream_source_id, upstream_source_slot,
            boundary_types.get(input_link, inputs[index].get('type')), visited
        )

    # Helper: trace through Reroute nodes
    def trace_through_reroute(source_node_id, source_slot, visited=None):
        if visited is None:
            visited = set()
        source_node_id_str = str(source_node_id)
        if source_node_id_str in visited:
            return (source_node_id, source_slot)
        visited.add(source_node_id_str)

        if source_node_id_str in reroute_sources:
            actual_source_id, actual_source_slot = reroute_sources[source_node_id_str]
            return trace_through_reroute(actual_source_id, actual_source_slot, visited)

        return (source_node_id, source_slot)

    # Build API format prompt
    api_prompt = {}
    linked_inputs = {}   # node id -> names of the inputs set from a link

    for node in workflow_nodes:
        node_id = str(node.get('id'))
        node_type = node.get('type')

        if not node_type:
            continue

        node_mode = node.get('mode', 0)
        if node_mode == 2:
            continue
        elif node_mode == 4:
            continue

        if node_type in _VIRTUAL_NODE_TYPES:
            continue

        if node_id in nodes_to_exclude:
            continue

        api_node = {
            'inputs': {},
            'class_type': node_type
        }

        # _meta.title comes from widget_orders[t]['display_name'] (from /object_info).
        if 'title' in node:
            api_node['_meta'] = {'title': node['title']}
        else:
            display_name = (widget_orders.get(node_type) or {}).get('display_name')
            api_node['_meta'] = {'title': display_name or node_type}

        # Process inputs (connections via links)
        link_inputs = {}
        primitive_inputs = {}
        specs = _input_specs(node_type, widget_orders)
        node_inputs = node.get('inputs', [])

        if node_inputs:
            for input_info in node_inputs:
                input_name = input_info.get('name')
                input_link = input_info.get('link')

                if input_link is not None and input_link in link_map:
                    link_data = link_map[input_link]
                    source_node_id = link_data['source_id']
                    source_slot = link_data['source_slot']

                    actual_source_id, actual_source_slot = trace_through_get_set_nodes(
                        source_node_id, source_slot
                    )
                    actual_source_id, actual_source_slot = trace_through_reroute(
                        actual_source_id, actual_source_slot
                    )

                    if str(actual_source_id) in bypassed_nodes:
                        actual_source_id, actual_source_slot = trace_through_bypassed(
                            actual_source_id, actual_source_slot,
                            boundary_types.get(input_link, input_info.get('type'))
                        )
                        if str(actual_source_id) in bypassed_nodes:
                            continue

                    actual_source_id, actual_source_slot = trace_through_get_set_nodes(
                        actual_source_id, actual_source_slot
                    )
                    actual_source_id, actual_source_slot = trace_through_reroute(
                        actual_source_id, actual_source_slot
                    )
                    actual_source_id, actual_source_slot = resolve_subgraph_output(
                        str(actual_source_id), actual_source_slot
                    )

                    source_node_id_str = str(actual_source_id)

                    if source_node_id_str in primitive_values:
                        primitive_inputs[input_name] = _prompt_widget_value(
                            primitive_values[source_node_id_str], specs.get(input_name))
                    elif actual_source_id in nodes_to_exclude:
                        pass
                    elif actual_source_id in bypassed_nodes:
                        _log.warning(
                            "Workflow bypassed node %s for input '%s' could not be resolved - connection skipped",
                            source_node_id, input_name)
                    else:
                        link_inputs[input_name] = [str(actual_source_id), actual_source_slot]

        ordered_inputs = _get_ordered_inputs(node_type, widget_orders)

        widget_values = node.get('widgets_values')
        widget_inputs = {}

        if widget_values is not None:
            if isinstance(widget_values, dict):
                for key, value in widget_values.items():
                    if key in ['videopreview', 'preview']:
                        continue
                    if key not in link_inputs:
                        widget_inputs[key] = value

            elif isinstance(widget_values, list):
                filtered_values = _filter_control_values(widget_values)
                widget_mappings = _get_widget_mappings(node_type, node, widget_orders,
                                                       filtered_values)

                if _uses_dict_path(filtered_values, widget_mappings, specs):
                    _process_dict_widget_values(widget_values, widget_inputs, link_inputs)
                else:
                    if widget_mappings:
                        for i, value in enumerate(filtered_values):
                            if i < len(widget_mappings):
                                widget_name = widget_mappings[i]
                                # Skip widgets fed by a PrimitiveNode -
                                # primitive_inputs holds the inlined value
                                # and must win over the target's stale
                                # widgets_values entry below.
                                if (widget_name
                                        and widget_name not in link_inputs
                                        and widget_name not in primitive_inputs):
                                    widget_inputs[widget_name] = _prompt_widget_value(
                                        value, specs.get(widget_name))
                        # Dicts past the declared widgets belong to widgets
                        # only the frontend knows (LoRA rows next to combos).
                        extra = filtered_values[len(widget_mappings):]
                        if any(isinstance(value, dict) for value in extra):
                            _process_dict_widget_values(extra, widget_inputs, link_inputs)
                    else:
                        if filtered_values and warn_unmapped:
                            _log.warning(
                                "Workflow node type '%s' (id %s) unknown - widget values not mapped",
                                node_type, node_id)

        # A promoted subgraph widget takes the value its instance holds, as in
        # the frontend's prompt; a link or a PrimitiveNode still wins.
        for name, value in promoted_values.get(node_id, {}).items():
            if name not in link_inputs and name not in primitive_inputs:
                widget_inputs[name] = _prompt_widget_value(value)

        default_inputs = _get_default_inputs(node_type, widget_inputs, primitive_inputs, link_inputs, widget_orders)

        # Build inputs in the correct order (widgets + primitives + defaults first, links after)
        if ordered_inputs:
            for input_name in ordered_inputs:
                if input_name in widget_inputs:
                    api_node['inputs'][input_name] = widget_inputs[input_name]
                elif input_name in primitive_inputs:
                    api_node['inputs'][input_name] = primitive_inputs[input_name]
                elif input_name in default_inputs:
                    api_node['inputs'][input_name] = default_inputs[input_name]

            for input_name in ordered_inputs:
                if input_name in link_inputs and input_name not in api_node['inputs']:
                    api_node['inputs'][input_name] = link_inputs[input_name]

            for key, value in widget_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value
            for key, value in primitive_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value
            for key, value in default_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value
            for key, value in link_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value
        else:
            for key, value in widget_inputs.items():
                api_node['inputs'][key] = value
            for key, value in primitive_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value
            for key, value in default_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value
            for key, value in link_inputs.items():
                if key not in api_node['inputs']:
                    api_node['inputs'][key] = value

        _normalize_combo_values(node_type, api_node['inputs'], widget_orders)

        if node_type == 'CustomCombo':
            _set_custom_combo_index(node, api_node['inputs'], 'choice' in link_inputs)

        api_prompt[node_id] = api_node
        linked_inputs[node_id] = list(link_inputs)

    # As in the frontend's prompt, a socket fed by a promoted input gets the
    # input's value too: the instance's, or else what its first widget gets.
    # A link that reaches the socket through the instances wins. An array is
    # boxed, as the frontend does, so it is not read as a link.
    for node_id, name, widget_id, widget_name in promoted_sockets:
        if node_id not in api_prompt:
            continue
        socket = next((inp for inp in node_by_id[node_id].get('inputs') or []
                       if inp.get('name') == name), {})
        if socket.get('link') is not None:
            continue
        value = promoted_values.get(widget_id, {}).get(
            widget_name,
            api_prompt.get(widget_id, {}).get('inputs', {}).get(widget_name))
        if value is not None:
            api_prompt[node_id]['inputs'][name] = (
                {'__value__': value} if isinstance(value, list) else value)

    # As in the frontend's prompt, an input whose source is not sent (a muted
    # node or subgraph) is left out instead of linking to a missing node.
    for node_id, names in linked_inputs.items():
        inputs = api_prompt[node_id]['inputs']
        for name in names:
            source = inputs.get(name)
            if isinstance(source, list) and source[0] not in api_prompt:
                del inputs[name]

    return api_prompt


def _is_valid_connection(type_a, type_b):
    """Return whether two slot types connect, as LiteGraph.isValidConnection.

    An empty or '*' type fits any, a comma-separated type fits when one of
    its members does, and names compare case-insensitively.
    """
    if not type_a or not type_b or '*' in (type_a, type_b) or type_a == type_b:
        return True
    type_a, type_b = str(type_a).lower(), str(type_b).lower()
    if ',' not in type_a and ',' not in type_b:
        return type_a == type_b
    return any(_is_valid_connection(a, b)
               for a in type_a.split(',') for b in type_b.split(','))


def _bypass_input_index(inputs, outputs, slot, target_type):
    """Return the index of the input a bypassed node passes to `slot`, or -1.

    Same choice as the frontend's ExecutableNodeDTO._getBypassSlotIndex, for
    a target socket of `target_type`: links are not looked at, so an
    unlinked pick passes nothing.
    """
    if target_type in ('*', ''):
        return slot if len(inputs) > slot else 0
    output_type = outputs[slot].get('type') if slot < len(outputs) else None
    if slot < len(inputs):
        opposite_type = inputs[slot].get('type')
        if (_is_valid_connection(opposite_type, output_type)
                and _is_valid_connection(opposite_type, target_type)):
            return slot
    for index, input_info in enumerate(inputs):
        if input_info.get('type') == target_type:
            return index
    for index, input_info in enumerate(inputs):
        input_type = input_info.get('type')
        if (_is_valid_connection(input_type, output_type)
                and _is_valid_connection(input_type, target_type)):
            return index
    return -1


def _frontend_inputs(node, widget_orders, subgraph_def=None):
    """Return the node's inputs in the order the frontend holds them.

    Loading a file, the frontend rebuilds each node's inputs from its
    definition: sockets, then widget inputs (a dynamic combo's own inputs
    right after it), then the inputs the definition lacks; a subgraph
    instance's from its subgraph's inputs. Export leaves the unlinked widget
    inputs out of the file and the bypass rule picks by position, so they
    are put back here, unlinked. Without a definition the file's list is
    used.
    """
    inputs = node.get('inputs') or []
    if subgraph_def is not None:
        by_name = {input_info.get('name'): input_info for input_info in inputs}
        return [by_name.get(slot.get('name'))
                or {'name': slot.get('name'), 'type': slot.get('type'), 'link': None}
                for slot in subgraph_def.get('inputs') or []]
    node_info = widget_orders.get(node.get('type')) or {}
    specs = {}
    for section in ('required', 'optional'):
        specs.update((node_info.get('input') or {}).get(section) or {})
    if not specs:
        return inputs
    # /object_info lists the inputs in input_order, the frontend's order.
    rank = {name: index for index, name in enumerate(specs)}
    names = [str(input_info.get('name')) for input_info in inputs]
    groups = {name.split('.')[0] for name in names if '.' in name}
    # A defined input the file lacks is taken as a widget input left out by
    # Export (a socket added to the definition after the file was saved
    # would be misplaced). An autogrow input appears only as its dotted
    # group, a dynamic combo also under its own name.
    missing = []
    for name in rank:
        if name not in names and (
                name not in groups or _classify_widget_spec(specs[name])[1]):
            spec_type = specs[name][0]
            missing.append({'name': name, 'link': None,
                            'type': spec_type if isinstance(spec_type, str) else 'COMBO'})
    widget_bases = {stub['name'] for stub in missing} | {
        name if name in rank else name.split('.')[0]
        for name, input_info in zip(names, inputs) if input_info.get('widget')}

    def position(item):
        index, input_info = item
        name = str(input_info.get('name'))
        base = name if name in rank else name.split('.')[0]
        if base not in rank:
            return (2, 0, False, index)
        return (1 if base in widget_bases else 0, rank[base], name != base, index)

    ranked = [(-1, stub) for stub in missing] + list(enumerate(inputs))
    return [input_info for _, input_info in sorted(ranked, key=position)]


def _input_specs(node_type, widget_orders):
    """Return ``{input_name: spec}`` for the node type's declared inputs."""
    node_input = (widget_orders.get(node_type) or {}).get('input') or {}
    specs = {}
    for section in ('required', 'optional'):
        specs.update(node_input.get(section) or {})
    return specs


def _uses_dict_path(values, mappings, specs):
    """Return whether a node's widgets_values go to _process_dict_widget_values.

    That path serves nodes whose widgets exist only in the frontend (a LoRA
    stack's rows), so a list holding a dict takes it when /object_info
    declares no widget with a slot, or when a dict falls on a plain widget's
    slot (a layout the positional reading does not know). Otherwise the list
    is read by position, object-valued widgets included.
    """
    if not any(isinstance(value, dict) for value in values):
        return False
    objects, declared = set(), False
    for name, spec in specs.items():
        slots = _object_widget_slots(name, spec, [])
        if slots is not None:
            objects.add(name)
        declared = declared or bool(slots) or _classify_widget_spec(spec)[0]
    if not declared:
        return True
    return any(isinstance(value, dict) and index < len(mappings)
               and mappings[index] and mappings[index] not in objects
               for index, value in enumerate(values))


def _process_dict_widget_values(widget_values, widget_inputs, link_inputs):
    """Process widget values that contain dictionaries.
    These are self-describing widgets that contain their configuration as dicts.
    """
    lora_counter = 0

    for value in widget_values:
        if isinstance(value, dict):
            if not value:
                continue
            elif 'type' in value:
                widget_name = value.get('type')
                if widget_name and widget_name not in link_inputs:
                    widget_inputs[widget_name] = value
            elif 'lora' in value:
                lora_counter += 1
                widget_name = f'lora_{lora_counter}'
                if widget_name not in link_inputs:
                    clean_value = {k: v for k, v in value.items() if k != 'strengthTwo' or v is not None}
                    widget_inputs[widget_name] = clean_value
        elif isinstance(value, str):
            if value == '':
                widget_inputs['\u2795 Add Lora'] = value


_SEED_CONTROL_VALUES = ('fixed', 'increment', 'decrement', 'randomize')


def _filter_control_values(widget_values):
    """Drop control_after_generate companions from a positional widget list.

    The frontend serializes the companion string immediately after the
    seed's integer value, and /object_info does not list it as a widget,
    so it must be removed before positional name mapping. Only a keyword
    directly preceded by an int is a companion: the same literal strings
    are legitimate option values of unrelated combo widgets, and dropping
    one of those shifts every later value onto the wrong widget."""
    filtered = []
    prev = None
    for value in widget_values:
        if (value in _SEED_CONTROL_VALUES
                and isinstance(prev, int) and not isinstance(prev, bool)):
            # Consumed companion: reset so a keyword-valued combo right
            # after it is kept.
            prev = None
            continue
        filtered.append(value)
        prev = value
    return filtered


def _get_ordered_inputs(node_type, widget_orders):
    """Get the ordered list of all inputs (widgets + connections) for a node type.
    Reads order from widget_orders[t]['input_order'] (from /object_info).
    """
    node_info = widget_orders.get(node_type) or {}

    # Primary: widget_orders[t]['input_order']
    if 'input_order' in node_info:
        input_order = node_info['input_order']
        input_names = []
        for section in ['required', 'optional']:
            if section in input_order:
                input_names.extend(input_order[section])
        if input_names:
            return input_names

    # Secondary fallback: derive order from widget_orders[t]['input'] sections
    if 'input' in node_info:
        input_types = node_info['input']
        input_names = []
        for section in ['required', 'optional']:
            if section in input_types and isinstance(input_types[section], dict):
                for input_name in input_types[section].keys():
                    input_names.append(input_name)
        if input_names:
            return input_names

    return []


def _declared_widget_type(input_spec):
    """Return the type that decides whether an input is a widget.

    A multi-type input ("FLOAT,INT") names its widget type in `widgetType`,
    as io.MultiType does. A single type is kept even when it carries a
    `widgetType`: Painter.mask is a STRING value drawn by a custom widget.
    """
    input_type = input_spec[0]
    options = input_spec[1] if len(input_spec) > 1 and isinstance(input_spec[1], dict) else {}
    if isinstance(input_type, str) and ',' in input_type and options.get('widgetType'):
        return options['widgetType']
    return input_type


def _classify_widget_spec(input_spec):
    """Return (is_widget, is_dynamic_combo) for an /object_info input spec.

    COLOR is a serialized hex-string widget (Painter.bg_color,
    ColorToRGBInt.color) and takes a widgets_values slot, matching the
    parser's _WIDGET_TYPES. Other uppercase widget types stay classified as
    sockets here: the object-valued ones (CURVE, BOUNDING_BOX, ...) are
    placed by _object_widget_slots, which knows how many slots each fills.
    """
    if not (isinstance(input_spec, (list, tuple)) and len(input_spec) >= 1):
        return False, False
    input_type = _declared_widget_type(input_spec)
    if isinstance(input_type, (list, tuple)):
        return True, False
    if input_type in ['INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO', 'COLOR']:
        return True, False
    if isinstance(input_type, str) and input_type.startswith('COMFY_') and 'COMBO' in input_type:
        return True, True
    # Custom widget types carry lowercase letters; "*" (any type) has none
    # and is a socket.
    if isinstance(input_type, str) and input_type != input_type.upper():
        return True, False
    return False, False


# Widget types the frontend builds for a structured value (an object or a
# list) and saves in widgets_values like any other widget. Left out: WEBCAM
# and AUDIO_RECORD, whose value is captured in the browser, and RANGE, which
# no node uses.
_OBJECT_WIDGET_TYPES = frozenset({
    'BOUNDING_BOX', 'BOUNDING_BOXES', 'COLORS', 'COMPOSITOR', 'CURVE',
    'LOAD_3D', 'VIDEO_EDIT'})


def _object_widget_slots(input_name, input_spec, widget_names, node_type=None):
    """Return the widgets_values slots an object-valued widget fills, or None.

    The frontend adds helper widgets that are saved but not sent (None in the
    result): BOUNDING_BOX is followed by its x, y, width and height, LOAD_3D
    is preceded by three buttons when the node has a model_file widget
    (`widget_names` holds the widgets placed so far), except on
    Load3DAdvanced, which the frontend gives its own widget. IMAGECOMPARE is
    not saved at all. None for any other input, and for a socket-only one.
    """
    if not (isinstance(input_spec, (list, tuple)) and input_spec
            and isinstance(input_spec[0], str)):
        return None
    input_type = input_spec[0]
    options = input_spec[1] if len(input_spec) > 1 and isinstance(input_spec[1], dict) else {}
    if options.get('forceInput'):
        return None
    if input_type == 'IMAGECOMPARE':
        return []
    if input_type not in _OBJECT_WIDGET_TYPES:
        return None
    if input_type == 'BOUNDING_BOX':
        return [input_name, None, None, None, None]
    if (input_type == 'LOAD_3D' and 'model_file' in widget_names
            and node_type != 'Load3DAdvanced'):
        return [None, None, None, input_name]
    return [input_name]


def _prompt_widget_value(value, input_spec=None):
    """Return a widget value as the frontend's prompt carries it.

    A list would read as a link, so it is boxed; a curve widget's value
    carries a type marker. The server unwraps both. A value promoted from a
    subgraph input (no `input_spec`) only has its lists boxed, as in the
    frontend.
    """
    if (value is not None and isinstance(input_spec, (list, tuple))
            and input_spec and input_spec[0] == 'CURVE'):
        return {'__type__': 'CURVE', '__value__': value}
    if isinstance(value, list):
        return {'__value__': value}
    return value


def _get_dynamic_combo_sub_inputs(input_name, input_spec, widget_values, current_idx):
    """Get sub-input names for a COMFY_DYNAMICCOMBO_V3 input based on the selected option.

    `widget_values` is the control-filtered widget list and `current_idx` the
    index of the combo's own value in it. The frontend serializes the active
    option's sub-widgets depth-first right after the combo value; socket-type
    subs (e.g. IMAGE) become node inputs instead and never appear in
    widgets_values, so only widget subs take a positional slot. A sub can
    itself be a dynamic combo (e.g. NukomfyWrite file_type -> exr_compression
    -> exr_dw_compression_level) and recurses the same way.

    Returns (sub_input_names, next_idx): dot-notation names (e.g.
    ['resize_type.width', 'resize_type.height']) and the index of the first
    positional slot after the combo's whole group.
    """
    next_idx = current_idx + 1
    if not isinstance(input_spec, (list, tuple)) or len(input_spec) < 2:
        return [], next_idx

    input_type = input_spec[0]
    if not isinstance(input_type, str) or not input_type.startswith('COMFY_') or 'COMBO' not in input_type:
        return [], next_idx

    spec_options = input_spec[1] if len(input_spec) > 1 else {}
    if not isinstance(spec_options, dict):
        return [], next_idx

    options = spec_options.get('options', [])
    if not options or current_idx >= len(widget_values):
        return [], next_idx

    selected_value = widget_values[current_idx]

    for option in options:
        if isinstance(option, dict) and option.get('key') == selected_value:
            sub_inputs = option.get('inputs', {})
            if not isinstance(sub_inputs, dict):
                return [], next_idx
            sub_input_names = []
            for section in ['required', 'optional']:
                section_specs = sub_inputs.get(section)
                if not isinstance(section_specs, dict):
                    continue
                for sub_name, sub_spec in section_specs.items():
                    is_widget, is_dynamic = _classify_widget_spec(sub_spec)
                    if not is_widget:
                        continue
                    full_name = f"{input_name}.{sub_name}"
                    sub_input_names.append(full_name)
                    if is_dynamic:
                        nested_names, next_idx = _get_dynamic_combo_sub_inputs(
                            full_name, sub_spec, widget_values, next_idx)
                        sub_input_names.extend(nested_names)
                    else:
                        next_idx += 1
            return sub_input_names, next_idx

    return [], next_idx


def _get_widget_mappings(node_type, node, widget_orders, filtered_values):
    """Get widget name mappings for a given node type.
    Returns a list of widget names in the order they appear in widgets_values.
    Reads widget order from widget_orders[t]['input'] (from /object_info).

    `filtered_values` is the node's widgets_values after
    _filter_control_values: the caller maps it positionally onto the
    returned names, so dynamic-combo option lookups and the fallback
    length guards must index/measure that same list, not the raw one
    (the raw list still holds the control_after_generate companions).
    """
    # Looked up by type, as the frontend does: "Node name for S&R" is a
    # user-editable alias (for %Name.widget% in file names), not a class.
    node_info = widget_orders.get(node_type) or {}

    # Primary: widget_orders[t]['input']
    if 'input' in node_info:
        try:
            input_def = node_info['input']
            widget_names = []
            widget_idx = 0

            for section in ['required', 'optional']:
                if section in input_def:
                    for input_name, input_spec in input_def[section].items():
                        slots = _object_widget_slots(input_name, input_spec, widget_names,
                                                     node_type)
                        if slots is not None:
                            widget_names.extend(slots)
                            widget_idx += len(slots)
                            continue

                        is_widget, is_dynamic_combo = _classify_widget_spec(input_spec)

                        if is_widget:
                            widget_names.append(input_name)

                            if is_dynamic_combo and filtered_values:
                                sub_inputs, widget_idx = _get_dynamic_combo_sub_inputs(
                                    input_name, input_spec, filtered_values, widget_idx
                                )
                                widget_names.extend(sub_inputs)
                            else:
                                widget_idx += 1

            if widget_names:
                return widget_names
        except Exception:
            pass

    # Fallback: try to infer widget mappings from the workflow and widget values
    if not filtered_values:
        return []

    # Try ue_properties.widget_ue_connectable
    properties = node.get('properties')
    ue_properties = properties.get('ue_properties') if isinstance(properties, dict) else None
    widget_ue_connectable = (ue_properties.get('widget_ue_connectable', {})
                             if isinstance(ue_properties, dict) else {})

    if widget_ue_connectable and isinstance(widget_ue_connectable, dict):
        widget_names = list(widget_ue_connectable.keys())
        if widget_names and len(widget_names) >= len(filtered_values):
            return widget_names[:len(filtered_values)]

    all_inputs = []
    connected_inputs = set()
    widget_flagged_inputs = []

    for input_info in node.get('inputs') or []:
        input_name = input_info.get('name')
        if input_name:
            all_inputs.append(input_name)
            if input_info.get('link') is not None:
                connected_inputs.add(input_name)
            if input_info.get('widget'):
                widget_flagged_inputs.append(input_name)

    if widget_flagged_inputs:
        if len(filtered_values) > len(widget_flagged_inputs):
            potential_widgets = [inp for inp in all_inputs
                               if inp not in connected_inputs and inp not in widget_flagged_inputs]
            return widget_flagged_inputs + potential_widgets[:len(filtered_values) - len(widget_flagged_inputs)]
        return widget_flagged_inputs

    unconnected = [inp for inp in all_inputs if inp not in connected_inputs]
    if unconnected and len(unconnected) >= len(filtered_values):
        return unconnected[:len(filtered_values)]

    return []


def _get_default_inputs(node_type, widget_inputs, primitive_inputs, link_inputs, widget_orders):
    """Get default values for required/optional inputs that aren't already set.
    Reads defaults from widget_orders[t]['input'] (from /object_info).
    """
    default_inputs = {}

    node_info = widget_orders.get(node_type) or {}
    if 'input' not in node_info:
        return default_inputs

    try:
        input_types = node_info['input']

        for section in ['required', 'optional']:
            if section not in input_types:
                continue

            for input_name, input_spec in input_types[section].items():
                if input_name in widget_inputs or input_name in primitive_inputs or input_name in link_inputs:
                    continue

                if isinstance(input_spec, (list, tuple)) and len(input_spec) >= 1:
                    input_type = input_spec[0]
                    spec_options = input_spec[1] if len(input_spec) >= 2 else {}

                    if isinstance(spec_options, dict) and 'default' in spec_options:
                        default_inputs[input_name] = _prompt_widget_value(
                            spec_options['default'], input_spec)
                    elif isinstance(input_type, list) and len(input_type) > 0:
                        default_inputs[input_name] = input_type[0]
                    elif input_type == 'COMBO' and isinstance(spec_options, dict) and 'options' in spec_options:
                        options = spec_options['options']
                        if isinstance(options, list) and len(options) > 0:
                            default_inputs[input_name] = options[0]
                    elif input_type == 'IMAGECOMPARE':
                        # Never saved: the prompt carries the widget's
                        # initial value.
                        default_inputs[input_name] = _prompt_widget_value(['', ''])
    except Exception:
        pass

    return default_inputs


def _normalize_combo_values(node_type, inputs, widget_orders):
    """Normalize combo widget values against the node's allowed options.
    Reads allowed options from widget_orders[t]['input'] (from /object_info).
    """
    node_info = widget_orders.get(node_type) or {}
    if 'input' not in node_info:
        return

    try:
        input_types = node_info['input']
    except Exception:
        return

    for section in ['required', 'optional']:
        if section not in input_types:
            continue

        for input_name, input_spec in input_types[section].items():
            if input_name not in inputs:
                continue

            value = inputs[input_name]
            if not isinstance(value, str):
                continue

            if not isinstance(input_spec, (list, tuple)) or len(input_spec) < 1:
                continue

            allowed = input_spec[0]
            if not isinstance(allowed, (list, tuple)):
                continue

            if value in allowed:
                continue

            for option in allowed:
                if isinstance(option, str) and value.lower() == option.lower():
                    inputs[input_name] = option
                    break


def _set_custom_combo_index(node, inputs, choice_linked):
    """Add CustomCombo's hidden `index` input: the chosen option's position.

    The server outputs `index` as INDEX (0 when absent), and /object_info
    lists only `choice`: the options are frontend widgets saved after the
    choice and the index, `[choice, index, option1, ...]`. As in the
    frontend, the position is that of the value sent as `choice`, or of the
    node's own value when `choice` is linked; -1 when it is not an option.
    """
    # The raw list: the control-value filter would drop an option named
    # 'fixed' that follows the integer index.
    values = node.get('widgets_values')
    if not isinstance(values, list) or not values:
        return
    choice = values[0] if choice_linked else inputs.get('choice')
    options = values[2:]
    inputs['index'] = options.index(choice) if choice in options else -1


# ===========================================================================
# Gizmo-specific injection helpers
# ===========================================================================
def inject_primitive_values(workflow_json, gizmo_params, gizmo_node):
    """Write current gizmo knob values into PrimitiveNode widgets_values
    BEFORE ui_to_api runs. The converter then inlines them into all
    connected target nodes (multi-link gratis via primitive_values dict).

    For non-primitive params, this is a no-op (handled later by
    inject_knob_values on the API workflow). For PrimitiveNode params,
    writing here is required because the primitive itself is stripped
    from the API workflow during conversion (target_node_id won't be a
    valid api_workflow key).

    Per-batch seed override for primitive seeds happens in
    apply_seed_control via the param's _primitive_targets list.

    Modifies workflow_json in place.
    """
    nodes = workflow_json.get('nodes', []) or []
    nodes_by_id = {str(n.get('id')): n for n in nodes}
    for sg in workflow_json.get('definitions', {}).get('subgraphs', []) or []:
        for n in sg.get('nodes', []) or []:
            nodes_by_id[str(n.get('id'))] = n

    for p in gizmo_params:
        if not p.get('_is_primitive'):
            continue

        if p.get('enabled', True):
            # Exposed: live knob value.
            knob_name = p.get('_knob_name', '')
            if not knob_name:
                continue
            knob = gizmo_node.knob(knob_name)
            if knob is None:
                continue
            value = knob.value()
        else:
            # Hidden: Workflow Creator default. A PrimitiveNode is inlined
            # away during conversion, so its id is not a key in the API
            # workflow and inject_param_defaults can't reach it. Apply the
            # default here (pre-conversion) so a hidden primitive honours
            # the edited default, same as a hidden normal param.
            value = p.get('default_value')
            if value is None:
                continue

        ptype = (p.get('type') or '').upper()
        if ptype == 'INT':
            try:
                value = int(str(value).strip() or '0')
            except (ValueError, TypeError):
                try:
                    value = int(float(value))
                except (ValueError, TypeError):
                    value = 0
        elif ptype == 'FLOAT':
            try:
                value = float(value)
            except (ValueError, TypeError):
                pass
        elif ptype == 'BOOLEAN':
            value = bool(value)

        prim_id = str(p.get('target_node_id', ''))
        node = nodes_by_id.get(prim_id)
        if node is None:
            continue
        wv = node.get('widgets_values')
        if not isinstance(wv, list) or not wv:
            continue
        wv[0] = value


def normalize_v3_sub_enabled(gizmo_params, gizmo_node):
    """Reconcile each V3 sub's `enabled` flag with whether it actually
    materialised a knob on the gizmo.

    A Suite V3 sub (NukomfyRead / NukomfyWrite) always materialises a
    settable knob when its master is exposed (gizmo_builder._kept), but
    the stored `enabled` flag only tracks the master option that was
    current when the workflow was last saved in the Workflow Editor.
    When the artist later switches the master ON THE GIZMO (e.g.
    file_type exr->dpx), the new option's subs stay enabled=False, so
    inject_knob_values would skip their knobs and inject_param_defaults
    would send the spec default instead of the artist's value (and the
    job Detail would list them as Hidden). The knob's existence - not
    the stale flag - is the source of truth for "this sub is on the
    gizmo", so promote it to enabled here.

    Generic (non-Suite) V3 subs left unchecked have NO knob (dropped at
    build time), so the `knob is not None` guard leaves them untouched:
    their intentional opt-out survives. Inactive-option subs get
    promoted too, but strip_v3_inactive_subs removes them from the wire
    afterwards, exactly as for an all-exposed gizmo.

    Modifies gizmo_params in place. Idempotent.
    """
    if gizmo_node is None:
        return
    for p in gizmo_params:
        if p.get('enabled', True):
            continue
        if not p.get('_v3_master'):
            continue
        kn = p.get('_knob_name', '')
        if kn and gizmo_node.knob(kn) is not None:
            p['enabled'] = True


def param_targets(p, targets):
    """The (node id, input) pairs a param's value is written to: its own, or
    those param_value_targets lists for it."""
    key = _param_key(p)
    return (targets or {}).get(key, [key])


def inject_knob_values(api_workflow, gizmo_params, gizmo_node, targets=None):
    """Replace values in the API workflow with current knob values from the gizmo.

    Reads each enabled param's _knob_name from the gizmo node and sets
    api_workflow[target_node_id]["inputs"][widget_name] = value, and the
    same on the other inputs `targets` (param_value_targets) lists for it.

    Modifies api_workflow in place.
    """
    for p in gizmo_params:
        if p.get('role') == 'separator':
            continue
        if not p.get('enabled', True):
            continue

        knob_name = p.get('_knob_name', '')
        if not knob_name:
            continue

        knob = gizmo_node.knob(knob_name)
        if knob is None:
            continue

        ptype = p.get('type', '').upper()
        if ptype == 'COLOR':
            # Color_Knob / AColor_Knob store per-channel floats in 0.0-1.0
            # range. ComfyUI expects "#RRGGBB" (Color_Knob) or "#RRGGBBAA"
            # (AColor_Knob) lowercase hex. isinstance check is more reliable
            # than try/except on value(3): some Nuke versions silently return
            # 0 for out-of-range channel access instead of raising, which
            # would produce a spurious "#RRGGBB00" alpha byte on Color_Knob.
            try:
                # nuke import is module-level via gizmo_callbacks dependency;
                # access AColor_Knob via attribute lookup with safe fallback.
                import nuke as _nuke  # type: ignore
                _AColor = getattr(_nuke, 'AColor_Knob', None)
                has_alpha = _AColor is not None and isinstance(knob, _AColor)
                r = max(0.0, min(1.0, float(knob.value(0))))
                g = max(0.0, min(1.0, float(knob.value(1))))
                b = max(0.0, min(1.0, float(knob.value(2))))
                if has_alpha:
                    a = max(0.0, min(1.0, float(knob.value(3))))
                    value = '#{:02x}{:02x}{:02x}{:02x}'.format(
                        int(round(r * 255)), int(round(g * 255)),
                        int(round(b * 255)), int(round(a * 255)))
                else:
                    value = '#{:02x}{:02x}{:02x}'.format(
                        int(round(r * 255)), int(round(g * 255)),
                        int(round(b * 255)))
            except Exception:
                value = '#000000'
        else:
            value = knob.value()
            if ptype == 'INT':
                try:
                    value = int(str(value).strip() or '0')
                except (ValueError, TypeError):
                    try:
                        value = int(float(value))
                    except (ValueError, TypeError):
                        value = 0
            elif ptype == 'FLOAT':
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    pass
            elif ptype == 'BOOLEAN':
                value = bool(value)

        for nid, wname in param_targets(p, targets):
            if nid in api_workflow:
                api_workflow[nid]['inputs'][wname] = value


def strip_v3_inactive_subs(api_workflow, gizmo_params, gizmo_node):
    """Remove `master.sub` keys for sub-inputs that are currently
    HIDDEN in the gizmo. Sub visibility is the AND of every entry in
    the gizmo's `_v3_visibility_map`: a nested sub (e.g.
    file_type.compression.dw_compression_level) is visible iff both
    file_type=exr AND compression in [DWAA, DWAB]. Hidden subs are
    stripped from api_workflow so ComfyUI doesn't receive stale keys
    from non-active options.

    Covers both exposed subs (knobs, via the visibility map) and subs
    of a non-exposed master (no knob, injected as defaults by
    inject_param_defaults): the master/ancestor option is resolved from
    the live knob when exposed, else from the param `default_value`.

    Modifies api_workflow in place.
    """
    if gizmo_node is None:
        return

    from Nukomfy.workflows._payload import decode_payload
    v3_map_k = gizmo_node.knob('_v3_visibility_map')
    v3_map = {}
    if v3_map_k is not None and v3_map_k.value():
        v3_map = decode_payload(v3_map_k.value(), default={}) or {}

    # First pass - exposed V3 subs (those registered as knobs in the
    # visibility map): strip a sub when any gating master is off its
    # active option. Skipped when the gizmo exposed no V3 sub at all
    # (empty map) - but the second pass below MUST still run, because
    # subs of a non-exposed master are never in the visibility map yet
    # were injected by inject_param_defaults: otherwise a NukomfyWrite
    # with an unexposed file_type leaves every option's subs on the wire.
    processed_kns = set()
    if v3_map:
        # Build {sub_kn: [(master_kn, [allowed_values]), ...]}.
        sub_conditions = {}
        for master_kn, sub_map in v3_map.items():
            if not isinstance(sub_map, dict):
                continue
            for sub_kn, allowed in sub_map.items():
                sub_conditions.setdefault(sub_kn, []).append(
                    (master_kn, list(allowed or [])))

        # Map sub_kn -> (nid, widget_name) via gizmo_params. widget_name
        # is the dotted path that appears as a key in api_workflow.inputs.
        kn_to_target = {}
        for p in gizmo_params:
            if p.get('role') == 'separator':
                continue
            if not p.get('_v3_master'):
                continue
            kn = p.get('_knob_name', '')
            if not kn:
                continue
            kn_to_target[kn] = (
                str(p.get('target_node_id', '')),
                p.get('widget_name') or p.get('name') or '')

        for sub_kn, conditions in sub_conditions.items():
            processed_kns.add(sub_kn)
            # Evaluate AND across every master that gates this sub.
            visible = True
            for master_kn, allowed in conditions:
                mk = gizmo_node.knob(master_kn)
                if mk is None:
                    continue
                if mk.value() not in allowed:
                    visible = False
                    break
            if visible:
                continue
            # Sub is hidden - strip its key from api_workflow.
            target = kn_to_target.get(sub_kn)
            if not target:
                continue
            nid, widget_name = target
            if not nid or not widget_name:
                continue
            if nid not in api_workflow:
                continue
            inputs_dict = api_workflow[nid].get('inputs', {}) or {}
            if widget_name in inputs_dict:
                del inputs_dict[widget_name]

    # Second pass: sub V3 that are NOT in the gizmo's
    # `_v3_visibility_map` because the user did not expose them in
    # the Workflow Editor (enabled=False, no knob in the gizmo).
    # `inject_param_defaults` injected their default into the
    # payload, so we must still evaluate their option-key membership
    # against the current master value and drop the key if the sub
    # belongs to a non-active option. Without this pass the server
    # would receive stale `master.sub` keys for non-Suite V3 masters
    # (e.g. ResizeImageMaskNode `resize_type.multiplier` when the
    # current resize_type is `scale dimensions`).
    # The master (or an ancestor) may itself be unexposed: its current
    # value then lives only in the param's `default_value` (exactly what
    # inject_param_defaults wrote on the wire), not in any knob - and a
    # nested master is itself a sub, so it never has a top-level knob.
    # Resolve every widget's EFFECTIVE value up front (knob value when
    # exposed, else default_value) so masters/ancestors are resolvable
    # regardless of exposure. Built before any deletion and read from
    # knobs/params (never the mutating api_workflow), so delete order is
    # irrelevant. A None value means "undeterminable": leave the sub
    # rather than strip on a guess (matches the prior behaviour when the
    # master had no knob).
    value_by_widget = {}
    for p in gizmo_params:
        if p.get('role') == 'separator':
            continue
        wn = p.get('widget_name') or p.get('name') or ''
        nid_p = str(p.get('target_node_id', ''))
        if not nid_p or not wn:
            continue
        val = None
        kn_p = p.get('_knob_name', '')
        if kn_p:
            kobj = gizmo_node.knob(kn_p)
            if kobj is not None:
                val = kobj.value()
        if val is None:
            val = p.get('default_value')
        value_by_widget[(nid_p, wn)] = val

    for p in gizmo_params:
        if p.get('role') == 'separator':
            continue
        master_wn = p.get('_v3_master', '')
        if not master_wn:
            continue
        if p.get('_knob_name', '') in processed_kns:
            continue
        nid_p = str(p.get('target_node_id', ''))
        widget_name_p = p.get('widget_name') or p.get('name') or ''
        if not nid_p or not widget_name_p or nid_p not in api_workflow:
            continue
        visible_p = True
        show_keys_p = list(p.get('_v3_show_for_keys', []) or [])
        if show_keys_p:
            mval = value_by_widget.get((nid_p, master_wn))
            if mval is not None and mval not in show_keys_p:
                visible_p = False
        if visible_p:
            for anc in (p.get('_v3_ancestor_conditions') or []):
                if not (isinstance(anc, (list, tuple))
                        and len(anc) == 2):
                    continue
                anc_path, anc_keys = anc[0], list(anc[1] or [])
                aval = value_by_widget.get((nid_p, anc_path))
                if aval is not None and aval not in anc_keys:
                    visible_p = False
                    break
        if visible_p:
            continue
        inputs_dict_p = api_workflow[nid_p].get('inputs', {}) or {}
        if widget_name_p in inputs_dict_p:
            del inputs_dict_p[widget_name_p]


def _coerce_value(value, ptype):
    ptype = (ptype or '').upper()
    if ptype == 'INT':
        try:
            return int(str(value).strip() or '0')
        except (ValueError, TypeError):
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return 0
    if ptype == 'FLOAT':
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if ptype == 'BOOLEAN':
        return bool(value)
    return value


def inject_param_defaults(api_workflow, gizmo_params, targets=None):
    """Apply Workflow Creator `default_value` for unchecked params.

    Enabled params are handled by inject_knob_values (reading from the
    gizmo knob). Unchecked params have no knob, so without this pass the
    value ultimately sent would come from the source `workflow.json`'s
    `widgets_values`, ignoring any default the user set in the Workflow
    Creator. File path params (role input/output) are skipped - their
    values are resolved at submit time by inject_input_paths /
    inject_output_params. `targets` as in inject_knob_values.

    Modifies api_workflow in place.
    """
    for p in gizmo_params:
        if p.get('role') == 'separator':
            continue
        if p.get('enabled', True):
            continue
        if p.get('role') in ('input', 'output'):
            continue
        dv = p.get('default_value')
        if dv is None:
            continue
        value = _coerce_value(dv, p.get('type', ''))
        for nid, wname in param_targets(p, targets):
            if nid and wname and nid in api_workflow:
                api_workflow[nid]['inputs'][wname] = value


def _seed_max_for(param):
    """The seed param's real max, capped to the global uint64 input limit."""
    try:
        return min(int(param.get('max_value')), _SEED_MAX_INPUT)
    except (TypeError, ValueError):
        return _SEED_MAX_INPUT


def random_seed_value(param):
    """Fresh random seed for `param`, matching the ComfyUI frontend range
    (2^50) capped to the widget's real max."""
    import random
    return random.randint(0, min(_SEED_MAX_RANDOM, _seed_max_for(param)))


def apply_seed_control(api_workflow, gizmo_params, gizmo_node,
                       batch_idx, base_seeds, targets=None):
    """Apply seed control_after_generate logic to a single batch iteration.

    Reads each is_seed param's `{knob}_control` Enumeration_Knob on the
    gizmo, computes the effective seed from `base_seeds[knob_name]` and
    `batch_idx`, and writes it into
    `api_workflow[target_node_id]['inputs'][widget_name]`.

    ComfyUI semantics: the render uses the value currently shown in the
    widget, and the control advances it only AFTER queueing. So the first
    batch iteration always uses the gizmo's value - for randomize too -
    and only later iterations generate fresh values. The caller writes
    the NEXT value back to the gizmo once the batch is queued.

    Returns {knob_name: used_value}.

    Randomize range matches ComfyUI frontend (2^50) but is capped to the
    seed widget's real max. Manual input, increment and decrement are all
    clamped to [0, min(2^64-1, real max)]. `targets` as in
    inject_knob_values.
    """
    used = {}
    for p in gizmo_params:
        if not p.get('is_seed') or not p.get('enabled', True):
            continue
        knob_name = p.get('_knob_name', '')
        if not knob_name:
            continue
        ctrl_knob = gizmo_node.knob('{}_control'.format(knob_name))
        if ctrl_knob is None:
            continue
        mode = ctrl_knob.value()
        base = base_seeds.get(knob_name, 0)
        # Respect the widget's real max so signed-int seeds (e.g.
        # max=2^31-1) never get a value the server would reject.
        seed_max = _seed_max_for(p)
        if mode == 'randomize':
            value = base if batch_idx == 0 else random_seed_value(p)
        elif mode == 'increment':
            value = base + batch_idx
        elif mode == 'decrement':
            value = base - batch_idx
        else:  # 'fixed' or unknown
            value = base
        value = max(0, min(int(value), seed_max))

        # PrimitiveNode is inlined into ALL connected targets - write
        # the per-batch seed to each target node's input. The primitive
        # itself is not in api_workflow (target_node_id would no-op).
        if p.get('_is_primitive'):
            for t in p.get('_primitive_targets', []) or []:
                tid = str(t.get('node_id', ''))
                twn = t.get('widget_name', '')
                if tid and twn and tid in api_workflow:
                    api_workflow[tid]['inputs'][twn] = value
        else:
            for nid, wname in param_targets(p, targets):
                if nid in api_workflow:
                    api_workflow[nid]['inputs'][wname] = value
        used[knob_name] = value
    return used


def _nuke_pattern_to_printf(path):
    """Convert Nuke frame pattern (#####) to printf format (%05d).

    ComfyUI expects printf-style patterns like %04d, not Nuke's #### style.
    """
    import re
    def _replace(m):
        return '%0{}d'.format(len(m.group()))
    return re.sub(r'#+', _replace, path)


def inject_input_paths(api_workflow, input_params, write_results, machine=None):
    """Inject input file paths (in place) and auto-fill NukomfyRead frame
    widgets. `write_results` must match `input_params` order."""
    for param, result in zip(input_params, write_results):
        nid = str(param.get('target_node_id', ''))
        wname = param.get('widget_name', param.get('name', ''))
        path = result.get('full_path', '')

        if nid not in api_workflow or not path:
            continue

        node_inputs = api_workflow[nid]['inputs']
        node_type = api_workflow[nid].get('class_type', '')

        printf_path = _nuke_pattern_to_printf(path)
        if machine:
            from Nukomfy.client.path_substitution import (
                substitute_path, maybe_long_prefix_for_target)
            printf_path = substitute_path(printf_path, machine)
            printf_path = maybe_long_prefix_for_target(printf_path, machine)

        node_inputs[wname] = printf_path

        if node_type == 'NukomfyRead':
            frange = result.get('frame_range', [1, 1])
            # Force Custom Range so the Submit Panel's frame_range always
            # drives the read, overriding whatever read_mode was saved in
            # the workflow. NukomfyRead handles single-frame natively when
            # first_frame == last_frame, no literal-path substitution needed.
            node_inputs['read_mode'] = 'Custom Range'
            node_inputs['first_frame'] = frange[0]
            node_inputs['last_frame'] = frange[1]


def inject_output_params(api_workflow, output_params, output_starts,
                         output_path_info, machine=None):
    """Auto-fill NukomfyWrite path + pipeline-managed widgets (in place).

    `output_starts` is a list of ints aligned with `output_params` - each is
    the `frame_start` value for that output's NukomfyWrite node.
    Reads `file_type` from the workflow to pick the path extension; the
    frame padding is read from Settings and embedded into `file_path`
    directly as the `%0Nd` token (NukomfyWrite reads the padding width
    from the token in the path, there is no separate frame_padding knob).
    `output_path_info` provides output_dir, output_name, version,
    workflow_name, workflow_alias, workflow_uuid, workflow_categories,
    workflow_models - all derivable from the gizmo so the path matches
    what the gizmo's preview / Read Outputs reconstruct.
    """
    from Nukomfy.utils.output_path import build_output_path, clamp_padding
    from Nukomfy.core.settings import settings as _settings

    padding = clamp_padding(_settings.frame_padding)

    for idx, param in enumerate(output_params):
        nid = str(param.get('target_node_id', ''))
        if nid not in api_workflow:
            continue
        node_type = api_workflow[nid].get('class_type', '')
        if node_type != 'NukomfyWrite':
            continue

        node_inputs = api_workflow[nid]['inputs']

        # file_type is the DynamicCombo selector ('exr'/'jpeg'/'tga'/...).
        # Nested format-specific keys (file_type.compression, file_type.
        # datatype, ...) remain in node_inputs as the workflow author
        # configured them.
        ext = node_inputs.get('file_type', 'exr')

        output_names = output_path_info.get('output_names', [])
        if idx < len(output_names) and output_names[idx]:
            name = output_names[idx]
        else:
            name = output_path_info.get('output_name', 'output')
        base = output_path_info['output_dir'].rstrip('/')
        workflow_name = output_path_info.get('workflow_name', 'output')
        workflow_alias = output_path_info.get('workflow_alias')
        nk_file = output_path_info.get('nk_file', 'Untitled')
        node_name = output_path_info.get('node_name')
        frame_pat = '%0{}d'.format(padding)

        full_path = build_output_path(
            base, nk_file, workflow_name, name,
            output_path_info['version'], frame_pat, ext,
            node_name=node_name,
            workflow_alias=workflow_alias,
            output_index=idx + 1,
            workflow_uuid=output_path_info.get('workflow_uuid'),
            workflow_categories=output_path_info.get('workflow_categories'),
            workflow_models=output_path_info.get('workflow_models'))
        if machine:
            from Nukomfy.client.path_substitution import (
                substitute_path, maybe_long_prefix_for_target)
            full_path = substitute_path(full_path, machine)
            full_path = maybe_long_prefix_for_target(full_path, machine)

        node_inputs['file_path'] = full_path
        node_inputs['frame_start'] = (
            output_starts[idx] if idx < len(output_starts) else 1)
        node_inputs['create_directories'] = True
