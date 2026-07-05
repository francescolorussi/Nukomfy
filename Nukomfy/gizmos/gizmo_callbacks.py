"""Runtime callbacks for Nukomfy gizmo nodes (onCreate, knobChanged, version).

Kept in a separate module so the logic is editable in an IDE with full
syntax support - the gizmo itself only stores a thin try/import/except
stub that calls into this module.
"""

import logging
import os

import nuke  # type: ignore

from Nukomfy.workflows.workflow_converter import _SEED_MAX_INPUT

_log = logging.getLogger(__name__)


# Knobs whose changes trigger an output path preview refresh - every
# knob read by _update_output_preview must be covered here. 'name' is the
# node name ({node} token); per-output 'output_name_<i>' and deduped
# 'file_type_<nodeid>' knobs are matched by prefix in _is_path_trigger.
_PATH_PREVIEW_TRIGGERS = frozenset({
    'output_name', '_output_version', 'file_type', 'showPanel', 'name',
})


# Knob flags used by the V3 visibility runtime. Both expressed as raw
# hex from Foundry's `Knob.h` so the V3 cascade has a single source of
# truth - the symbol `nuke.HIDDEN` is not exposed to Python, and
# STARTLINE is kept in hex too for consistency with HIDDEN (0x40000 ==
# 0x0000000000040000, identical 32-bit int values).
#
# Pair semantics: every V3 sub toggle writes HIDDEN and STARTLINE
# together. Hiding collapses the row out of the layout; clearing
# STARTLINE in tandem prevents a blank line artifact when the row
# reappears at a non-startline position.
_HIDDEN_FLAG = 0x40000
_STARTLINE_FLAG = 0x1000

# Text_Knob word-wrap. Nuke's low knob-flag bits are type-specific: on a
# Text_Knob 0x1 is WORD_WRAP (it is MAGNITUDE on a numeric knob). Not exposed
# as a named nuke.* constant, so the raw Knob.h hex is used. Re-applied in
# on_create because Nuke drops programmatic knob flags on .nk reload /
# copy-paste, exactly like HIDDEN above.
_WORD_WRAP_FLAG = 0x1


def _decode_knob(node, knob_name, default):
    """Read and decode a hidden payload knob, returning `default` when the
    knob is absent or empty. Centralizes the read + base64/JSON decode the
    knobChanged validators run against the params blob and the V3 / Suite
    visibility maps."""
    k = node.knob(knob_name)
    raw = k.value() if k is not None else ''
    if not raw:
        return default
    from Nukomfy.workflows._payload import decode_payload
    return decode_payload(raw, default=default) or default


def _is_path_trigger(name):
    return (name in _PATH_PREVIEW_TRIGGERS
            or name.startswith('output_name_')
            or name.startswith('file_type_'))


# ---------------------------------------------------------------------------
# onCreate - rebuild Multiline_Eval_String_Knob after paste / comp reload
# ---------------------------------------------------------------------------
def on_create(node):
    """Rebuild multiline knobs so they render expanded after paste/reload.

    Nuke's panel layout squeezes multiline knobs to 1 line when 7+ knobs
    follow them in the same tab. Destroying and re-adding the knob via
    Python restores the expanded height. Order is preserved by holding
    references to all trailing knobs, removing them, re-creating the
    multiline, then re-adding the trailing knobs unchanged (removeKnob
    only detaches - the Python objects keep their state).
    """
    # Re-resolve the header HTML on every load so a moved/renamed workflow
    # folder still resolves the logo image path (uses UUID lookup).
    _refresh_header_html(node)

    from Nukomfy.workflows._payload import decode_payload

    names_k = node.knob('_nfy_multiline_names')
    wanted = (set(s for s in names_k.value().split(',') if s)
              if names_k is not None else set())

    if wanted:
        # Nuke normalizes label==name to an empty label in the .nk, so ml.label()
        # returns '' after paste/reload. Read the authoritative label from
        # _nfy_params (keyed by _knob_name) instead.
        labels_by_name = {}
        params_k = node.knob('_nfy_params')
        if params_k is not None:
            for p in decode_payload(params_k.value(), default=[]) or []:
                if p.get('role') == 'separator':
                    continue
                kn = p.get('_knob_name') or p.get('name', '')
                if kn:
                    labels_by_name[kn] = p.get('label', '')

        multilines = [
            k for k in node.allKnobs()
            if k.name() in wanted
            and isinstance(k, nuke.Multiline_Eval_String_Knob)
        ]

        for ml in multilines:
            name = ml.name()
            label = labels_by_name.get(name, ml.label())
            try:
                tooltip = ml.tooltip()
            except Exception:
                tooltip = ''
            value = ml.value()

            all_knobs = node.allKnobs()
            idx = all_knobs.index(ml)
            trailing = all_knobs[idx + 1:]

            for tk in reversed(trailing):
                try:
                    node.removeKnob(tk)
                except Exception:
                    pass
            try:
                node.removeKnob(ml)
            except Exception:
                pass

            new_k = nuke.Multiline_Eval_String_Knob(name, label)
            if tooltip:
                new_k.setTooltip(tooltip)
            node.addKnob(new_k)
            new_k.setText(value)

            for tk in trailing:
                try:
                    node.addKnob(tk)
                except Exception:
                    pass

    # Sync V3 sub-knob visibility on copy/paste/load. Without this,
    # gizmos restored from a .nk show all subs visible regardless of the
    # master's saved value. Must run AFTER the multiline rebuild above
    # because rebuilt multiline knobs are fresh objects without the
    # INVISIBLE flag set on the originals - applying visibility before
    # the rebuild would lose the flag on multiline V3 sub-inputs (e.g.
    # `storyboards.storyboard_X_prompt`).
    v3_map_k = node.knob('_v3_visibility_map')
    if v3_map_k is not None and v3_map_k.value():
        v3_map = decode_payload(v3_map_k.value(), default={})
        _refresh_v3_all_subs(node, v3_map or {})

    # Sync Suite boolean-trigger grey-out on copy/paste/load. Without
    # this, gizmos restored from a .nk show dependents enabled even when
    # the saved trigger value is False.
    suite_map_k = node.knob('_suite_grey_map')
    if suite_map_k is not None and suite_map_k.value():
        suite_map = decode_payload(suite_map_k.value(), default={})
        for trigger_kn, dep_kns in (suite_map or {}).items():
            trigger_knob = node.knob(trigger_kn)
            if trigger_knob is None:
                continue
            _refresh_suite_grey_out(node, trigger_knob.value(), dep_kns)

    # Re-apply WORD_WRAP: Nuke drops programmatic knob flags on .nk reload /
    # copy-paste (same reason HIDDEN is re-applied above). The per-knob policy
    # lives in the helper (output preview always wraps; header and Extra Info
    # follow the gizmo's word_wrap choice).
    _reapply_word_wrap(node)

    # Reset any internal Write output transform the local color config does not
    # know (gizmo loaded/pasted across different color configs or modes) to
    # 'default'.
    _fix_invalid_write_colorspaces(node)


def _reapply_word_wrap(node):
    """Re-apply WORD_WRAP to the gizmo's static Text_Knobs (Nuke drops
    programmatic flags on .nk reload / copy-paste). Output-path previews
    always wrap: their break points are width-stable so they never clip.
    Header and Extra Info follow the gizmo's word_wrap choice (persisted in
    _nfy_word_wrap) - off means the author manages line breaks manually,
    avoiding the bottom-clip on narrow floating panels. Idempotent; called
    from on_create."""
    # Output-path previews: always wrapped.
    names = ['_output_preview']
    i = 0
    while node.knob('_output_preview_{}'.format(i)) is not None:
        names.append('_output_preview_{}'.format(i))
        i += 1
    for kn in names:
        k = node.knob(kn)
        if k is not None:
            k.setFlag(_WORD_WRAP_FLAG)

    # Header + Extra Info: gated on the persisted word_wrap choice. Clear the
    # flag explicitly when off (robust even if a stale flag survived a load).
    ww_k = node.knob('_nfy_word_wrap')
    word_wrap = ww_k is None or ww_k.value() != '0'
    for kn in ('_header', '_usage_text'):
        k = node.knob(kn)
        if k is None:
            continue
        if word_wrap:
            k.setFlag(_WORD_WRAP_FLAG)
        else:
            k.clearFlag(_WORD_WRAP_FLAG)


def _fix_invalid_write_colorspaces(node):
    """Reset every internal Write 'colorspace' (output transform) the active
    color config does not know back to 'default', for gizmos loaded or pasted
    across different OCIO configs or color-management modes (the carried
    colorspace shows as 'Error: (...) not found'). Works in OCIO and Nuke-default
    alike.

    Reads the group's children via node.nodes() rather than entering the group
    with `with node:`: switching Nuke's context into the group while the group's
    own onCreate runs during a script load corrupts the loader and crashes the
    Root's onCreate. node.nodes() lists the children without a context switch.

    Nuke appends an unknown colorspace to that Write's own menu as the LAST entry
    and drops it again the moment the knob moves off it: a value before the last
    index is a real menu item (valid); a value at the last index is checked by
    moving to 'default' and seeing whether it survives in the now-clean menu."""
    try:
        for w in node.nodes():
            if w.Class() != 'Write':
                continue
            k = w.knob('colorspace')
            if k is None:
                continue
            try:
                idx = int(k.getValue())
                last = len(k.values()) - 1
            except Exception:
                continue
            if idx < last:
                continue
            bad = k.value()
            k.setValue(0)  # 'default' is always index 0 and always valid
            known = [v.split('\t')[0] for v in k.values()]
            if bad.split('\t')[0] in known:
                k.setValue(idx)  # genuine last colorspace -> restore
            else:
                _log.warning(
                    "%s.%s: output transform %r not found, reset to %r",
                    node.name(), w.name(), bad, k.value())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# knobChanged - seed validation + live output path preview
# ---------------------------------------------------------------------------
def on_knob_changed(node, knob):
    try:
        knob_name = knob.name()
    except Exception:
        return

    # --- Seed validation ---
    seed_k = node.knob('_nfy_seed_knob_names')
    seed_names = (
        set(s for s in seed_k.value().split(',') if s)
        if seed_k is not None else set()
    )

    if knob_name in seed_names:
        _validate_seed(node, knob, knob_name)

    # --- Numeric clamp + INT step snap ---
    # Skip seed widgets (handled above via _validate_seed against
    # _SEED_MAX_INPUT). Operates on any INT/FLOAT widget with min/max
    # recorded in _nfy_params, regardless of slider or spinbox rendering.
    if knob_name not in seed_names:
        _clamp_and_snap_numeric(node, knob, knob_name)

    # --- Output path preview ---
    if _is_path_trigger(knob_name):
        if node.knob('_output_version') is not None and (
                node.knob('_output_preview') is not None
                or node.knob('_output_preview_0') is not None):
            _update_output_preview(node)

    # --- V3 sub-knob visibility ---
    # Any V3 master change triggers an AND-reeval of every sub: a
    # nested sub (e.g. dw_compression_level) depends on multiple
    # masters (file_type AND compression) and must hide if ANY of
    # them fails.
    v3_map = _decode_knob(node, '_v3_visibility_map', {})
    if knob_name in v3_map:
        _refresh_v3_all_subs(node, v3_map, source_knob_name=knob_name)

    # --- Suite boolean-trigger grey-out ---
    suite_map = _decode_knob(node, '_suite_grey_map', {})
    if knob_name in suite_map:
        _refresh_suite_grey_out(node, knob.value(), suite_map[knob_name])


def _clamp_and_snap_numeric(node, knob, knob_name):
    """Clamp INT/FLOAT knob value to [min_value, max_value] and snap INT to
    `_step` (if >1) on user commit.

    Nuke `Int_Knob.setRange()` / `Double_Knob.setRange()` set the slider
    range only - they do NOT enforce bounds on typed values. This helper
    enforces bounds at commit time so the API receives values that ComfyUI
    would accept. Programmatic `setValue()` during build / .nk load does
    not fire knobChanged, so saved out-of-range values are preserved until
    the user actively edits the knob.
    """
    matched = None
    for p in _decode_knob(node, '_nfy_params', []):
        if p.get('_knob_name') == knob_name:
            matched = p
            break
    if matched is None:
        return

    ptype = (matched.get('type', '') or '').upper()
    if ptype not in ('INT', 'FLOAT'):
        return

    min_v = matched.get('min_value')
    max_v = matched.get('max_value')
    step = matched.get('_step')

    if min_v is None and max_v is None and (not step or step <= 1):
        return

    try:
        cur = knob.value()
    except Exception:
        return
    try:
        cur_num = float(cur)
    except (TypeError, ValueError):
        return

    new_num = cur_num
    if min_v is not None:
        try:
            new_num = max(float(min_v), new_num)
        except (TypeError, ValueError):
            pass
    if max_v is not None:
        try:
            new_num = min(float(max_v), new_num)
        except (TypeError, ValueError):
            pass

    if ptype == 'INT' and step and step > 1 and min_v is not None:
        try:
            base = float(min_v)
            step_f = float(step)
            new_num = round((new_num - base) / step_f) * step_f + base
            # Re-clamp after snap in case snap pushed us back outside
            if max_v is not None:
                new_num = min(float(max_v), new_num)
            new_num = max(base, new_num)
        except (TypeError, ValueError):
            pass

    if ptype == 'INT':
        new_num = int(round(new_num))

    # Only write back if the value actually changes - prevents redundant
    # knobChanged re-entry and avoids dirtying the script for no-op commits.
    if abs(new_num - cur_num) < 1e-9:
        return
    try:
        knob.setValue(new_num)
    except Exception:
        pass


def _apply_visibility(sub_knob, visible):
    """Hide / show a knob using the HIDDEN + STARTLINE pair (raw hex).
    Applies to every V3 sub - Suite Tab_Knob wraps, Suite sub knobs,
    and non-Suite V3 sub knobs. STARTLINE is paired with HIDDEN on
    plain knobs so the row does not leave a blank line; Tab_Knob has
    no STARTLINE concept, so the STARTLINE write is a no-op for the
    wrap (Nuke ignores the flag on Tab_Knob).

    Skips the flag writes when the knob is already in the target
    state. HIDDEN is the canonical bit (STARTLINE is always paired
    with it), so a single getFlag check is sufficient."""
    try:
        cur_hidden = bool(sub_knob.getFlag(_HIDDEN_FLAG))
        if visible == (not cur_hidden):
            return
        if visible:
            sub_knob.clearFlag(_HIDDEN_FLAG)
            sub_knob.setFlag(_STARTLINE_FLAG)
        else:
            sub_knob.setFlag(_HIDDEN_FLAG)
            sub_knob.clearFlag(_STARTLINE_FLAG)
    except Exception:
        pass


def _refresh_v3_all_subs(node, v3_map, source_knob_name=None):
    """Re-evaluate visibility for every sub_kn in `v3_map` by AND-ing
    every condition that mentions it. A nested sub (e.g.
    dw_compression_level) appears under multiple master_kn entries
    (one per ancestor); it is visible iff EVERY master that lists it
    has its current value in the allowed set.

    `source_knob_name`: when provided, restrict the pass to subs that
    have this knob among their master / ancestor conditions. Skips
    touching subs of unrelated V3 clusters (e.g. resize_type subs are
    not re-flagged when file_type changes), which avoids the layout
    pass per untouched knob and the resulting flicker.

    v3_map schema: {master_kn: {sub_kn: [allowed_values]}}."""
    sub_conditions = {}
    for master_kn, sub_map in v3_map.items():
        if not isinstance(sub_map, dict):
            continue
        for sub_kn, allowed in sub_map.items():
            sub_conditions.setdefault(sub_kn, []).append(
                (master_kn, list(allowed or [])))
    if source_knob_name is not None:
        sub_conditions = {
            sub_kn: conds
            for sub_kn, conds in sub_conditions.items()
            if any(m == source_knob_name for m, _ in conds)
        }
    for sub_kn, conds in sub_conditions.items():
        sub_knob = node.knob(sub_kn)
        if sub_knob is None:
            continue
        visible = True
        for master_kn, allowed in conds:
            master_knob = node.knob(master_kn)
            if master_knob is None:
                continue
            if master_knob.value() not in allowed:
                visible = False
                break
        _apply_visibility(sub_knob, visible)


def _refresh_suite_grey_out(node, trigger_value, dep_kns):
    """Apply setEnabled(bool(trigger_value)) to each dependent knob.
    Used for Suite boolean triggers (apply_color_transform ->
    input_transform / output_transform). Dependent stays visible but
    greys out, following Nuke desktop Read / Write conventions.

    Skips the call when the knob is already in the target state - avoids
    redundant setEnabled during refresh passes."""
    enabled = bool(trigger_value)
    for dep_kn in dep_kns:
        dep_knob = node.knob(dep_kn)
        if dep_knob is None:
            continue
        try:
            if dep_knob.enabled() != enabled:
                dep_knob.setEnabled(enabled)
        except Exception:
            pass


def _validate_seed(node, knob, knob_name):
    last_k = node.knob(knob_name + '_last')
    # Clamp ceiling = the widget's real max (falls back to the global
    # uint64 cap). Keeps signed-int seeds (e.g. max=2^31-1) in range.
    seed_max = _SEED_MAX_INPUT
    for p in _decode_knob(node, '_nfy_params', []):
        if p.get('_knob_name') == knob_name:
            mx = p.get('max_value')
            if mx is not None:
                try:
                    seed_max = min(int(mx), _SEED_MAX_INPUT)
                except (TypeError, ValueError):
                    pass
            break
    txt = str(knob.value()).strip()
    try:
        seed_value = int(txt)
    except (ValueError, TypeError):
        fallback = '0'
        if last_k is not None:
            try:
                int(last_k.value())
                fallback = last_k.value()
            except (ValueError, TypeError):
                pass
        knob.setValue(fallback)
    else:
        if seed_value < 0:
            seed_value = 0
        elif seed_value > seed_max:
            seed_value = seed_max
        if str(seed_value) != knob.value():
            knob.setValue(str(seed_value))
        if last_k is not None:
            last_k.setValue(str(seed_value))


def _update_output_preview(node):
    # No-op when the gizmo omits the path preview (Output Preview unchecked):
    # neither preview knob exists, so there is nothing to refresh.
    if (node.knob('_output_preview') is None
            and node.knob('_output_preview_0') is None):
        return
    from Nukomfy.utils.output_path import resolve_gizmo_outputs

    outputs = resolve_gizmo_outputs(node, frame_style='printf')
    previews = [o['path'] for o in outputs]

    # The trailing <br> separates stacked output blocks and the last block
    # from the collapsible output-parameter groups. With no such group the
    # last preview's <br> would stack with the submit row's own spacer and
    # double the gap before the separator, so it is dropped in that case.
    has_opts = any(k.name().startswith('_out_opts') for k in node.allKnobs())

    def _fmt(p, trailing_br=True):
        br = '<br>' if trailing_br else ''
        return '<font color="#666" size="2">&#x21B3; {}</font>{}'.format(p, br)

    # Display OS-native separators (matching the plugin's other user-facing
    # paths), but insert a zero-width space after each separator so Qt can
    # wrap on a backslash, which it otherwise treats as non-breaking. The knob
    # is display-only: the resolved path value is untouched.
    def _disp(p):
        np = os.path.normpath(p)
        return np.replace(os.sep, os.sep + '&#8203;')

    # Multi-output gizmos carry one preview knob per output, placed right
    # after each corresponding output_name_i. Write each path into its own
    # knob so the UI alternates name/preview rows.
    per_knob = node.knob('_output_preview_0')
    if per_knob is not None:
        last = len(previews) - 1
        for i, path in enumerate(previews):
            pk = node.knob('_output_preview_{}'.format(i))
            if pk is not None:
                pk.setValue(_fmt(_disp(path), i < last or has_opts))
    else:
        parts = [_fmt(_disp(p), i < len(previews) - 1 or has_opts)
                 for i, p in enumerate(previews)]
        node['_output_preview'].setValue(''.join(parts))


# ---------------------------------------------------------------------------
# Version up / down buttons
# ---------------------------------------------------------------------------
def version_up(node):
    v = int(node['_output_version'].value())
    node['_output_version'].setValue(v + 1)
    nuke.root().setModified(True)


def version_down(node):
    v = int(node['_output_version'].value())
    node['_output_version'].setValue(max(1, v - 1))
    nuke.root().setModified(True)


def set_latest_version(node):
    """Scan output dirs and set _output_version to the highest version
    found on disk across all enabled outputs. Silent if already at max;
    popup if no renders exist.

    Multi-output gizmos with diverging output dirs use the GLOBAL max:
    a single Version applies to the gizmo as a whole, so even if one
    output has v005 and another has v007, the version is set to 7.
    """
    import os
    import re
    from Nukomfy.utils.output_path import resolve_gizmo_outputs

    outputs = resolve_gizmo_outputs(node, frame_style='printf')
    if not outputs:
        return

    version_re = re.compile(r'^v(\d+)$')
    versions_found = set()

    for o in outputs:
        # path: <root>/<workflow>_<output>/v001/<output>_v001.%04d.ext
        # parent of file = version dir (vNNN); parent of that = workflow
        # dir where sibling vNNN dirs live.
        version_dir = os.path.dirname(o['path'])
        workflow_dir = os.path.dirname(version_dir)
        if not workflow_dir or not os.path.isdir(workflow_dir):
            continue
        try:
            for entry in os.listdir(workflow_dir):
                m = version_re.match(entry)
                if m and os.path.isdir(os.path.join(workflow_dir, entry)):
                    versions_found.add(int(m.group(1)))
        except OSError:
            continue

    if not versions_found:
        nuke.message('No renders found on disk. Version unchanged.')
        return

    max_v = max(versions_found)
    current = int(node['_output_version'].value())
    if max_v == current:
        return
    node['_output_version'].setValue(max_v)
    nuke.root().setModified(True)


# ---------------------------------------------------------------------------
# Header HTML refresh - re-resolves the title-mode == use_custom_logo path
# via UUID lookup so a moved Library root still finds the logo file.
# ---------------------------------------------------------------------------
def _refresh_header_html(node):
    header_k = node.knob('_header')
    wf_id_k = node.knob('_nfy_wf_id')
    if header_k is None or wf_id_k is None:
        return
    workflow_id = wf_id_k.value()

    # Lazy imports: defer the heavy submit_panel/Qt graph until first gizmo
    # load that needs it. After Nuke caches the modules, subsequent calls
    # are free.
    try:
        from Nukomfy.gui.submit_panel import _resolve_workflow_path
        from Nukomfy.workflows.workflow_loader import WorkflowItem
        from Nukomfy.gizmos.gizmo_builder import build_header_html
    except Exception:
        return

    resolved = _resolve_workflow_path(workflow_id)
    if not resolved:
        return
    folder = os.path.dirname(resolved)
    try:
        # source ('Local' / 'Shared') is informational only; use a fixed
        # placeholder since the header build uses only folder + metadata.
        wf_item = WorkflowItem(folder, 'Local')
        new_html = build_header_html(wf_item)
    except Exception:
        return
    try:
        if header_k.value() != new_html:
            header_k.setValue(new_html)
    except Exception:
        pass
