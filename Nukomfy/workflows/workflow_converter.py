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

    max_iterations = 10
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        expanded_nodes = []
        found_subgraph = False

        for node in workflow_nodes:
            node_type = node.get('type')
            node_id = node.get('id')

            if _is_subgraph_uuid(node_type) and node_type in subgraph_defs:
                found_subgraph = True
                subgraph_def = subgraph_defs[node_type]
                sg_nodes, sg_links, input_map, output_map = _expand_subgraph(
                    node_id, subgraph_def, links
                )

                expanded_nodes.extend(sg_nodes)
                links.extend(sg_links)

                subgraph_input_mappings[str(node_id)] = input_map
                subgraph_output_mappings[str(node_id)] = output_map

                outer_inputs = node.get('inputs', [])
                subgraph_inputs = subgraph_def.get('inputs', [])

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

    # Helper: trace through bypassed nodes
    def trace_through_bypassed(source_node_id, source_slot, visited=None):
        if visited is None:
            visited = set()
        if source_node_id in visited:
            return (source_node_id, source_slot)
        visited.add(source_node_id)

        if source_node_id not in bypassed_nodes:
            return (source_node_id, source_slot)

        for node in workflow_nodes:
            if str(node.get('id')) == str(source_node_id):
                node_inputs = node.get('inputs', [])
                node_outputs = node.get('outputs', [])

                output_type = None
                if node_outputs and source_slot < len(node_outputs):
                    output_type = node_outputs[source_slot].get('type')

                if node_inputs:
                    linked_input = None
                    fallback_linked_input = None

                    for idx, input_info in enumerate(node_inputs):
                        input_link = input_info.get('link')
                        input_type = input_info.get('type')

                        if input_link is not None and input_link in link_map:
                            if fallback_linked_input is None:
                                fallback_linked_input = input_link
                            if output_type and input_type == output_type:
                                linked_input = input_link
                                break

                    if linked_input is None:
                        linked_input = fallback_linked_input

                    if linked_input is not None:
                        link_data = link_map[linked_input]
                        upstream_source_id = link_data['source_id']
                        upstream_source_slot = link_data['source_slot']

                        upstream_source_id, upstream_source_slot = trace_through_get_set_nodes(
                            upstream_source_id, upstream_source_slot
                        )
                        upstream_source_id, upstream_source_slot = trace_through_reroute(
                            upstream_source_id, upstream_source_slot
                        )
                        return trace_through_bypassed(
                            upstream_source_id, upstream_source_slot, visited
                        )
                break

        return (source_node_id, source_slot)

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
                            actual_source_id, actual_source_slot
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
                        primitive_inputs[input_name] = primitive_values[source_node_id_str]
                    elif actual_source_id in nodes_to_exclude:
                        pass
                    elif actual_source_id in bypassed_nodes:
                        _log.warning(
                            "Workflow bypassed node %s for input '%s' could not be resolved - connection skipped",
                            source_node_id, input_name)
                    else:
                        link_inputs[input_name] = [str(actual_source_id), actual_source_slot]

        ordered_inputs = _get_ordered_inputs(node_type, node, widget_orders)

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
                has_dict_widgets = any(isinstance(v, dict) for v in widget_values)

                if has_dict_widgets:
                    _process_dict_widget_values(widget_values, widget_inputs, link_inputs)
                else:
                    filtered_values = _filter_control_values(widget_values)
                    widget_mappings = _get_widget_mappings(node_type, node, widget_orders,
                                                           filtered_values)

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
                                    widget_inputs[widget_name] = value
                    else:
                        if filtered_values and warn_unmapped:
                            _log.warning(
                                "Workflow node type '%s' (id %s) unknown - widget values not mapped",
                                node_type, node_id)

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

        api_prompt[node_id] = api_node

    return api_prompt


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


def _get_ordered_inputs(node_type, node, widget_orders):
    """Get the ordered list of all inputs (widgets + connections) for a node type.
    Reads order from widget_orders[t]['input_order'] (from /object_info).
    """
    properties = node.get('properties', {})
    if 'Node name for S&R' in properties:
        node_type = properties['Node name for S&R']

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


def _classify_widget_spec(input_spec):
    """Return (is_widget, is_dynamic_combo) for an /object_info input spec.

    COLOR is a serialized hex-string widget (Painter.bg_color,
    ColorToRGBInt.color) and takes a widgets_values slot, matching the
    parser's _WIDGET_TYPES. Other uppercase V3 widget types (CURVE,
    BOUNDING_BOX, WEBCAM, IMAGECOMPARE) stay classified as sockets: the
    frontend persists them inconsistently (IMAGECOMPARE sets
    widget.serialize=False) and no reachable node exposes them today.
    """
    if not (isinstance(input_spec, (list, tuple)) and len(input_spec) >= 1):
        return False, False
    input_type = input_spec[0]
    if isinstance(input_type, (list, tuple)):
        return True, False
    if input_type in ['INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO', 'COLOR']:
        return True, False
    if isinstance(input_type, str) and input_type.startswith('COMFY_') and 'COMBO' in input_type:
        return True, True
    if isinstance(input_type, str) and not input_type.isupper():
        return True, False
    return False, False


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
    properties = node.get('properties', {})
    if 'Node name for S&R' in properties:
        node_type = properties['Node name for S&R']

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
    properties = node.get('properties', {})
    ue_properties = properties.get('ue_properties', {})
    widget_ue_connectable = ue_properties.get('widget_ue_connectable', {})

    if widget_ue_connectable and isinstance(widget_ue_connectable, dict):
        widget_names = list(widget_ue_connectable.keys())
        if widget_names and len(widget_names) >= len(filtered_values):
            return widget_names[:len(filtered_values)]

    all_inputs = []
    connected_inputs = set()
    widget_flagged_inputs = []

    for input_info in node.get('inputs', []):
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
                        default_inputs[input_name] = spec_options['default']
                    elif isinstance(input_type, list) and len(input_type) > 0:
                        default_inputs[input_name] = input_type[0]
                    elif input_type == 'COMBO' and isinstance(spec_options, dict) and 'options' in spec_options:
                        options = spec_options['options']
                        if isinstance(options, list) and len(options) > 0:
                            default_inputs[input_name] = options[0]
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


def inject_knob_values(api_workflow, gizmo_params, gizmo_node):
    """Replace values in the API workflow with current knob values from the gizmo.

    Reads each enabled param's _knob_name from the gizmo node and sets
    api_workflow[target_node_id]["inputs"][widget_name] = value.

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

        nid = str(p.get('target_node_id', ''))
        wname = p.get('widget_name', p.get('name', ''))

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


def inject_param_defaults(api_workflow, gizmo_params):
    """Apply Workflow Creator `default_value` for unchecked params.

    Enabled params are handled by inject_knob_values (reading from the
    gizmo knob). Unchecked params have no knob, so without this pass the
    value ultimately sent would come from the source `workflow.json`'s
    `widgets_values`, ignoring any default the user set in the Workflow
    Creator. File path params (role input/output) are skipped - their
    values are resolved at submit time by inject_input_paths /
    inject_output_params.

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
        nid = str(p.get('target_node_id', ''))
        wname = p.get('widget_name', p.get('name', ''))
        if not nid or not wname or nid not in api_workflow:
            continue
        api_workflow[nid]['inputs'][wname] = _coerce_value(dv, p.get('type', ''))


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
                       batch_idx, base_seeds):
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
    clamped to [0, min(2^64-1, real max)].
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
            nid = str(p.get('target_node_id', ''))
            wname = p.get('widget_name', p.get('name', ''))
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
