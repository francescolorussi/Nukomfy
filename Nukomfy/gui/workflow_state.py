"""Workflow Editor in-memory state and snapshot composition.

Holds the runtime state of the Workflow Editor and the deterministic
composition of effective params for the gizmo builder. State lives in
four layers:

  * _snapshot        - server-authoritative widget definitions
  * _overrides       - per-widget delta of user edits
  * _widget_order    - UI row order (interleaved widget refs + user rows)
  * _v3_user_edits   - flat dict of per-option sub edits on V3 dynamic
                      combo masters

compose_params() produces the flat list (role / enabled /
default_value / _v3_master / ...) the gizmo builder and submit pipeline
consume.
"""

import logging


_log = logging.getLogger(__name__)


# Fields that live in `_overrides`. Every other field comes from snapshot.
# `_intent_enabled` mirrors `enabled` for the cluster-link contract and is
# tracked here so reopening preserves the user's intent independently of
# bypass / mute reconciliations.
_OVERRIDE_FIELDS = (
    'enabled',
    '_intent_enabled',
    'label',
    'tooltip',
    'default_value',
    'io_mode',
    'write_template',
    'write_template_source',
)


# Structural row kinds inside _widget_order. Anything that is not one of
# these is a widget reference (list/tuple of [target_node_id, widget_name]).
_STRUCT_KINDS = frozenset({
    'separator',
    'group_begin',
    'group_end',
    'text',
})


# Canonical Knobs-table section order: input < model < output. Shared by
# initial_widget_order_for_snapshot (first sync) and migrate_widget_order
# (incremental sync) so both place a knob in the same section.
_SECTION_RANK = {'section_input': 0, 'section_model': 1, 'section_output': 2}


def make_override_key(nid, widget_name):
    """Stable key for the _overrides dict: '<nid>__<widget_name>'."""
    return '{}__{}'.format(nid, widget_name)


def _widget_name(d):
    """Widget identity key: 'widget_name', falling back to 'name', then ''."""
    return d.get('widget_name', d.get('name', ''))


def make_v3_edit_key(nid, widget_full_path):
    """Stable key for the _v3_user_edits flat dict:
    '<nid>__<widget_full_path>'. A V3 sub-widget with
    `_v3_show_for_keys=['DWAA','DWAB']` is a single snapshot widget
    (the parser is first-seen-wins) and gets a single edit slot here -
    the user's edit persists across master swaps as long as the widget
    is still visible under the new option.
    """
    return '{}__{}'.format(nid, widget_full_path)


def _widget_index(snapshot):
    """Build (nid, widget_name) -> widget dict from a snapshot."""
    return {
        (w.get('target_node_id'), _widget_name(w)): w
        for w in (snapshot or {}).get('widgets', [])
    }


def _struct_kind(entry):
    """Return the kind string if entry is a structural row marker,
    else None (meaning it's a widget reference).
    """
    if isinstance(entry, dict):
        return entry.get('kind')
    return None


def _widget_ref(entry):
    """Return (nid, widget_name) if entry is a widget reference,
    else (None, None).
    """
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return entry[0], entry[1]
    return None, None


def _apply_v3_user_edits(widget, v3_user_edits):
    """Return widget with default_value swapped in from v3_user_edits if
    the widget is a V3 sub and an edit is recorded for its full path.
    """
    if not widget.get('_v3_master'):
        return widget
    nid = widget.get('target_node_id')
    sub_full = _widget_name(widget)
    if not sub_full:
        return widget
    key = make_v3_edit_key(nid, sub_full)
    if key in v3_user_edits:
        out = dict(widget)
        out['default_value'] = v3_user_edits[key]
        return out
    return widget


def _merge_widget(snapshot_widget, override_entry, v3_user_edits):
    """Produce the effective param dict by layering override on snapshot.
    `enabled` defaults to True unless overridden.

    Layering order (lowest -> highest precedence):
      1. snapshot defaults
      2. v3_user_edits (per-widget edit captured by cascade pre-removal)
      3. override_entry (last explicit user edit on this UI row)

    The override wins because it reflects the most recent confirmation
    from the editor: when the widget was visible at save time, its UI
    cell value drove the override delta, which supersedes any earlier
    cascade-captured edit on the same widget.
    """
    p = dict(snapshot_widget)
    p = _apply_v3_user_edits(p, v3_user_edits or {})
    if override_entry:
        for k, v in override_entry.items():
            p[k] = v
    p.setdefault('enabled', True)
    return p


def _struct_entry_to_param(entry):
    """Convert a structural row marker in _widget_order to a flat
    param dict (role='separator'/'group_begin'/'group_end'/'text').
    """
    kind = entry.get('kind')
    if kind == 'separator':
        out = {'role': 'separator'}
        if 'fixed' in entry:
            out['fixed'] = entry['fixed']
        if 'label' in entry:
            out['label'] = entry['label']
        return out
    if kind in ('group_begin', 'group_end'):
        out = {'role': kind, 'id': entry.get('id', 0)}
        if kind == 'group_begin':
            if 'label' in entry:
                out['label'] = entry['label']
            out['default'] = entry.get('default', 'closed')
        return out
    if kind == 'text':
        return {
            'role': 'text',
            'label': entry.get('label', ''),
            'value': entry.get('value', ''),
        }
    return None


def compose_params(snapshot, overrides, widget_order, v3_user_edits):
    """Produce the flat list of params (the gizmo_params shape) from
    the snapshot + overrides + widget_order + v3_user_edits state.

    V3 sub widgets always follow their direct master in DFS order so
    the gizmo builder can emit Tab_Knob wraps in the right place even
    when the editor's widget_order only lists the current-option subs.
    Non-current-option subs and nested sub-of-sub trail the master via
    the snapshot's V3 hierarchy.

    Returns ``[]`` if snapshot is empty - the strict pre-release contract
    is that workflows without a snapshot have not been re-synced and
    should not build.
    """
    snapshot = snapshot or {}
    overrides = overrides or {}
    v3_user_edits = v3_user_edits or {}
    widgets = snapshot.get('widgets', [])
    if not widgets:
        return []
    index = _widget_index(snapshot)

    # V3 master path -> list of direct sub widgets, in snapshot order.
    children_by_master = {}
    for w in widgets:
        v3m = w.get('_v3_master', '')
        if not v3m:
            continue
        nid = w.get('target_node_id')
        children_by_master.setdefault((nid, v3m), []).append(w)

    result = []
    seen_widget_keys = set()

    def _emit_widget(w):
        nid = w.get('target_node_id')
        wn = _widget_name(w)
        key = (nid, wn)
        if key in seen_widget_keys:
            return
        seen_widget_keys.add(key)
        ov = overrides.get(make_override_key(nid, wn))
        result.append(_merge_widget(w, ov, v3_user_edits))
        # Recursively emit V3 descendants right after the master so
        # non-current-option subs and nested sub-of-sub follow the
        # master in DFS order. Children already covered by widget_order
        # are skipped via seen_widget_keys.
        for sub in children_by_master.get((nid, wn), []):
            _emit_widget(sub)

    if widget_order:
        for entry in widget_order:
            kind = _struct_kind(entry)
            if kind in _STRUCT_KINDS:
                struct = _struct_entry_to_param(entry)
                if struct is not None:
                    result.append(struct)
                continue
            nid, wn = _widget_ref(entry)
            if wn is None:
                continue
            w = index.get((nid, wn))
            if w is None:
                continue
            _emit_widget(w)

    # Append widgets not yet emitted in their natural snapshot order so
    # a freshly-synced workflow with no widget_order builds with the
    # parser's DFS layout, and a widget added by a later Sync lands at
    # the tail rather than dropping out.
    for w in widgets:
        _emit_widget(w)

    return result


def _v3_visible(widget, master_current_values):
    """True if the widget's V3 subtree gates are satisfied by the
    current master values. Used by compose_params_for_editor to hide
    sub-rows of non-active options from the editor table view.
    """
    if not widget.get('_v3_master'):
        return True
    nid = widget.get('target_node_id')
    master_path = widget.get('_v3_master', '')
    show_keys = [str(k) for k in (widget.get('_v3_show_for_keys') or [])]
    if show_keys:
        mv = master_current_values.get((nid, master_path))
        if mv is not None and str(mv) not in show_keys:
            return False
    for anc in (widget.get('_v3_ancestor_conditions') or []):
        if not (isinstance(anc, (list, tuple)) and len(anc) == 2):
            continue
        anc_path, anc_keys = anc[0], [str(k) for k in (anc[1] or [])]
        if not anc_keys:
            continue
        anc_val = master_current_values.get((nid, anc_path))
        if anc_val is not None and str(anc_val) not in anc_keys:
            return False
    return True


def compose_params_for_editor(snapshot, overrides, widget_order,
                              v3_user_edits):
    """Same as compose_params but filters V3 sub rows of non-active
    options out of the editor table view. The current option of each
    master is read from the composed params (default_value after
    overrides), so changing a master in the editor surfaces its option
    subset deterministically.
    """
    full = compose_params(snapshot, overrides, widget_order, v3_user_edits)
    if not full:
        return []
    master_current = {}
    for p in full:
        if p.get('role') != 'knob':
            continue
        nid = p.get('target_node_id')
        wn = _widget_name(p)
        master_current[(nid, wn)] = p.get('default_value')
    return [p for p in full if _v3_visible(p, master_current)]


def diff_widget_against_snapshot(snapshot_widget, current_state):
    """Compute the override delta for a single widget. `current_state`
    is a dict of the same shape as snapshot_widget but with the values
    the editor row currently exposes. Returns the dict of fields that
    differ from snapshot defaults (None means "use snapshot"); keys
    outside _OVERRIDE_FIELDS are ignored.

    `enabled` defaults to True in the snapshot when absent (the parser
    encodes only the False case via _node_state-driven greying);
    `_intent_enabled` follows enabled when not explicitly different.
    """
    delta = {}
    for field in _OVERRIDE_FIELDS:
        if field not in current_state:
            continue
        new_val = current_state[field]
        if field == 'enabled':
            base = snapshot_widget.get('enabled', True)
        elif field == '_intent_enabled':
            base = snapshot_widget.get('_intent_enabled',
                                       snapshot_widget.get('enabled', True))
        elif field == 'label':
            base = snapshot_widget.get('label',
                                       snapshot_widget.get('name', ''))
        elif field == 'tooltip':
            base = snapshot_widget.get('tooltip', '')
        elif field == 'default_value':
            base = snapshot_widget.get('default_value')
        else:
            base = snapshot_widget.get(field, '')
        if new_val != base:
            delta[field] = new_val
    return delta


def _snapshot_io_node_ids(snapshot):
    """Return (input_node_ids, output_node_ids) from a snapshot's role-tagged
    widgets. A knob sharing a node with a file input/output is input/output-
    side; any other knob is model-side."""
    input_node_ids = set()
    output_node_ids = set()
    for w in (snapshot or {}).get('widgets', []):
        role = w.get('role')
        if role == 'input':
            input_node_ids.add(w.get('target_node_id'))
        elif role == 'output':
            output_node_ids.add(w.get('target_node_id'))
    return input_node_ids, output_node_ids


def section_for_widget(widget, input_node_ids, output_node_ids):
    """Canonical Knobs-table section for a knob: 'section_input' /
    'section_model' / 'section_output'. None for role=input/output widgets -
    they live in the dedicated Inputs/Outputs tables, not the sectioned Knobs
    table. Shared by initial_widget_order_for_snapshot and migrate_widget_order
    so first-sync and incremental-sync placement stay identical."""
    if widget.get('role') in ('input', 'output'):
        return None
    nid = widget.get('target_node_id')
    if nid in input_node_ids:
        return 'section_input'
    if nid in output_node_ids:
        return 'section_output'
    return 'section_model'


def _fixed_section_of(entry):
    """Return the section id if entry is a canonical fixed-section separator,
    else None. User dividers carry a label, not a 'fixed' in _SECTION_RANK, so
    they are never mistaken for section boundaries."""
    if isinstance(entry, dict) and entry.get('kind') == 'separator':
        fx = entry.get('fixed')
        if fx in _SECTION_RANK:
            return fx
    return None


def _splice_into_section(order, ref, target_section):
    """Insert `ref` at the end of its canonical section in `order`, creating
    the section's fixed separator if absent (placed by input<model<output
    rank). 'End of section' = just before the next fixed-section separator, so
    the knob sits after any user divider/text rows and outside every group.
    Mutates and returns `order`."""
    target_rank = _SECTION_RANK[target_section]
    sec_idx = next((i for i, e in enumerate(order)
                    if _fixed_section_of(e) == target_section), -1)
    if sec_idx >= 0:
        insert_at = next((j for j in range(sec_idx + 1, len(order))
                          if _fixed_section_of(order[j]) is not None), len(order))
        order.insert(insert_at, ref)
        return order
    # Section absent: open it right before the first higher-rank section.
    for i, e in enumerate(order):
        fx = _fixed_section_of(e)
        if fx is not None and _SECTION_RANK[fx] > target_rank:
            order.insert(i, {'kind': 'separator', 'fixed': target_section})
            order.insert(i + 1, ref)
            return order
    # No higher-rank section to anchor against (degenerate order missing the
    # canonical separators): open the target section header, then append, so a
    # model knob never renders under a lower-rank section. Mirrors the
    # higher-rank branch and initial_widget_order_for_snapshot, which always
    # emit a header for a non-empty section.
    order.append({'kind': 'separator', 'fixed': target_section})
    order.append(ref)
    return order


def initial_widget_order_for_snapshot(snapshot):
    """Generate the initial widget_order for a freshly-synced snapshot:
    inputs, then knobs split by section_input/section_model/section_output,
    then outputs.
    """
    widgets = (snapshot or {}).get('widgets', [])
    if not widgets:
        return []

    input_node_ids, output_node_ids = _snapshot_io_node_ids(snapshot)

    inputs = []
    knobs_model = []
    knobs_input_side = []
    knobs_output_side = []
    outputs = []
    for w in widgets:
        ref = [w.get('target_node_id'), _widget_name(w)]
        role = w.get('role')
        if role == 'input':
            inputs.append(ref)
        elif role == 'output':
            outputs.append(ref)
        else:
            sec = section_for_widget(w, input_node_ids, output_node_ids)
            if sec == 'section_input':
                knobs_input_side.append(ref)
            elif sec == 'section_output':
                knobs_output_side.append(ref)
            else:
                knobs_model.append(ref)

    order = []
    order.extend(inputs)
    if knobs_input_side:
        order.append({'kind': 'separator', 'fixed': 'section_input'})
        order.extend(knobs_input_side)
    if knobs_model:
        order.append({'kind': 'separator', 'fixed': 'section_model'})
        order.extend(knobs_model)
    if knobs_output_side or outputs:
        order.append({'kind': 'separator', 'fixed': 'section_output'})
        order.extend(knobs_output_side)
    order.extend(outputs)
    return order


def drop_empty_fixed_sections(params):
    """Remove a fixed section-header separator (section_input/model/output)
    that has no content row before the next fixed-section header or the end -
    a section emptied by a Sync removal or a manual reorder must not show a
    dangling 'X Parameters' header. Operates on composed params (role-keyed);
    user dividers (separator without a canonical `fixed`), text and group rows
    count as content and are never dropped."""
    def _is_section(p):
        return (isinstance(p, dict) and p.get('role') == 'separator'
                and p.get('fixed') in _SECTION_RANK)

    out = []
    for i, p in enumerate(params):
        if _is_section(p):
            nxt = params[i + 1] if i + 1 < len(params) else None
            if nxt is None or _is_section(nxt):
                continue  # nothing in this section: drop the header
        out.append(p)
    return out


def migrate_widget_order(old_order, new_snapshot):
    """Filter `old_order` to keep only entries still valid in the new snapshot
    (widget exists or structural marker), then splice each widget new to the
    snapshot into its canonical Knobs section (model/input/output-side by node
    membership), creating the section separator if absent. Used by Sync to
    preserve user reorder decisions across server-side widget add/remove while
    landing a newly-exposed knob (e.g. seed) under its correct section.
    """
    if not old_order:
        return initial_widget_order_for_snapshot(new_snapshot)
    new_keys = {
        (w.get('target_node_id'), _widget_name(w))
        for w in (new_snapshot or {}).get('widgets', [])
    }
    kept = []
    kept_widget_keys = set()
    for entry in old_order:
        if _struct_kind(entry) in _STRUCT_KINDS:
            kept.append(entry)
            continue
        nid, wn = _widget_ref(entry)
        if wn is None:
            continue
        if (nid, wn) in new_keys:
            kept.append(entry)
            kept_widget_keys.add((nid, wn))
    input_node_ids, output_node_ids = _snapshot_io_node_ids(new_snapshot)
    for w in (new_snapshot or {}).get('widgets', []):
        nid = w.get('target_node_id')
        wn = _widget_name(w)
        if (nid, wn) in kept_widget_keys:
            continue
        # V3 sub-widgets are re-homed under their master by compose_params'
        # DFS, so keeping them out of widget_order avoids a stray sectioned
        # ref and still composes to the same layout.
        if w.get('_v3_master'):
            continue
        section = section_for_widget(w, input_node_ids, output_node_ids)
        if section is None:
            # role=input/output: dedicated Inputs/Outputs tables, no sections.
            kept.append([nid, wn])
        else:
            _splice_into_section(kept, [nid, wn], section)
    return kept


def migrate_overrides(old_overrides, new_snapshot):
    """Drop overrides for widgets removed from the snapshot. Preserve
    everything else as-is (combo_values changes are tolerated: the
    builder will validate at gizmo time).
    """
    if not old_overrides:
        return {}
    new_keys = {
        make_override_key(
            w.get('target_node_id'),
            _widget_name(w))
        for w in (new_snapshot or {}).get('widgets', [])
    }
    return {k: v for k, v in old_overrides.items() if k in new_keys}


def migrate_v3_user_edits(old_edits, new_snapshot):
    """Drop edits whose (nid, widget_full_path) is no longer in the
    new snapshot. A V3 sub-widget removed server-side loses its edit;
    one that survives keeps its edit verbatim.
    """
    if not old_edits:
        return {}
    valid_keys = set()
    for w in (new_snapshot or {}).get('widgets', []):
        if not w.get('_v3_master'):
            continue
        nid = w.get('target_node_id')
        sub_full = _widget_name(w)
        if not sub_full:
            continue
        valid_keys.add(make_v3_edit_key(nid, sub_full))
    return {k: v for k, v in old_edits.items() if k in valid_keys}
