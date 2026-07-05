"""Nuke Group gizmo builder mapping exposed ComfyUI widgets to Nuke knobs.

Maps each exposed ComfyUI widget (INT, FLOAT, STRING, BOOLEAN, COMBO,
seed) to a corresponding Nuke knob, propagates the tooltip, and adds the
randomize companion checkbox for seed widgets.
"""

import os
import json
import html
import logging
import nuke  # type: ignore

from Nukomfy.core.settings import settings
from Nukomfy.workflows.workflow_converter import _SEED_MAX_INPUT
from Nukomfy.workflows._payload import encode_payload
from Nukomfy.gui._theme import LINK_FG
from Nukomfy.utils.suite_rules import suite_dependents
from Nukomfy.gizmos.gizmo_callbacks import (
    _HIDDEN_FLAG, _STARTLINE_FLAG, _WORD_WRAP_FLAG)

_log = logging.getLogger(__name__)

_SEED_CONTROL_OPTIONS = ['randomize', 'increment', 'decrement', 'fixed']


# Suite node types whose V3 dynamic-combo subs always materialize as knobs
# (even when not exposed) because the runtime visibility cascade needs them
# present. NukomfyWrite additionally wraps consecutive subs of the same
# option in a per-option Tab_Knob group.
_SUITE_V3_NODE_TYPES = ('NukomfyRead', 'NukomfyWrite')
_SUITE_TAB_NODES = ('NukomfyWrite',)


def _write_templates_dir():
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_root, 'write_templates')


def _node_reaches_upstream(start, target):
    """True if *target* is reachable walking upstream (inputs) from *start*."""
    seen = set()
    stack = [start]
    while stack:
        n = stack.pop()
        if n is None or n in seen:
            continue
        seen.add(n)
        if n is target:
            return True
        try:
            for i in range(n.inputs()):
                stack.append(n.input(i))
        except Exception:
            pass
    return False


def _warn_template_name_refs(pasted, names):
    """Log a warning if a pasted node references one of *names* (the Input /
    Write original names we are about to rename) in an expression. setName
    does not propagate to expressions, so such a link would break. Fail-safe:
    any error here is swallowed - it must never abort a gizmo build."""
    import re
    pats = [(nm, re.compile(r'\b' + re.escape(nm) + r'\b')) for nm in names if nm]
    if not pats:
        return
    for n in pasted:
        try:
            knobs = list(n.knobs().values())
        except Exception:
            continue
        for k in knobs:
            try:
                if not k.hasExpression():
                    continue
                txt = k.toScript(False)
            except Exception:
                continue
            for nm, pat in pats:
                if txt and pat.search(txt):
                    _log.warning(
                        'Write template: node %r references %r by name in an '
                        'expression; the link may break after rename. Prefer '
                        'self-contained expressions.', n.name(), nm)
                    break


def _paste_chain_template(template_path, input_label):
    """Paste a write-template chain; return (input_node, write_node, pasted).

    The template is a self-contained chain with exactly one Input (the
    entry the gizmo feeds) and one Write (the cache terminal). The whole
    file is recreated 1:1 via nodePaste so expressions, clones and links
    are preserved. Only the Input and the Write are renamed (chain nodes
    keep their pasted names so intra-chain links survive); any Output node
    is removed (the gizmo owns the single Output). `pasted` is the list of
    surviving chain nodes (for the caller to lay out). Returns
    (None, None, []) on a missing file, an invalid shape, or a failed
    rename - the caller then falls back to a plain Input -> Write.
    """
    if not template_path or not os.path.isfile(template_path):
        return None, None, []
    try:
        # Clear the selection so the paste does not splice the chain head into
        # a stray selected node.
        for _n in nuke.allNodes():
            _n.setSelected(False)
        # Capture pasted nodes by diffing allNodes() before/after, and tolerate
        # an exception from nodePaste. When a knob value is invalid under the
        # local color config (e.g. "Bad value for display" opening a workflow
        # authored under a different OCIO config), nodePaste raises BUT the
        # nodes are still created and Nuke resets the offending knob to a valid
        # value. Bailing out here would discard the template and leave an
        # orphan Write next to the fallback - the cross-config "2 Writes /
        # template not found" bug. The Input/Write validation below guards
        # against a genuinely failed paste.
        before = set(nuke.allNodes())
        try:
            nuke.nodePaste(template_path)
        except Exception as _e:
            # Often a cross-config color knob ("Bad value for display"); the
            # nodes are still created. Logged at debug for field diagnosis of a
            # genuinely corrupt template (the Input/Write check below decides).
            _log.debug('nodePaste raised for %s (nodes still created): %s',
                       template_path, _e)
        pasted = [n for n in nuke.allNodes() if n not in before]
        inputs = [n for n in pasted if n.Class() == 'Input']
        writes = [n for n in pasted if n.Class() == 'Write']
        outputs = [n for n in pasted if n.Class() == 'Output']

        def _discard():
            for n in pasted:
                try:
                    nuke.delete(n)
                except Exception:
                    pass

        if len(inputs) != 1 or len(writes) != 1:
            _discard()
            return None, None, []
        inp, write = inputs[0], writes[0]

        # Drop the Output delimiter(s); the gizmo creates its own.
        for o in outputs:
            try:
                pasted.remove(o)
            except ValueError:
                pass
            try:
                nuke.delete(o)
            except Exception:
                pass

        # The Write must be downstream of the Input (chain traceable).
        if not _node_reaches_upstream(write, inp):
            _discard()
            return None, None, []

        _warn_template_name_refs(pasted, (inp.name(), write.name()))
        try:
            inp.setName(_safe_knob_name(input_label))
            write.setName('Write_{}'.format(_safe_knob_name(input_label)))
        except Exception:
            # A rename failure (e.g. an unexpected name clash) must not fall
            # through to the outer handler, which returns the fallback signal
            # WITHOUT deleting the pasted chain - leaving orphan nodes next to
            # the plain Input->Write the caller then builds.
            _discard()
            return None, None, []
        return inp, write, pasted
    except Exception:
        return None, None, []


def _layout_chain(nodes, base_x, base_y=0, dy=80, dx=110):
    """Lay pasted chain nodes in a tidy column at base_x, ordered by input
    distance from the chain root (Input at top, Write at the bottom). Nodes
    sharing a depth are spread sideways. Defensive against cycles and against
    nodes whose position cannot be set."""
    nodeset = set(nodes)
    depth = {}

    def _depth(n, seen):
        if n in depth:
            return depth[n]
        if n in seen:
            return 0
        seen = seen | {n}
        best = 0
        try:
            for i in range(n.inputs()):
                up = n.input(i)
                if up in nodeset:
                    best = max(best, _depth(up, seen) + 1)
        except Exception:
            pass
        depth[n] = best
        return best

    for n in nodes:
        _depth(n, set())
    levels = {}
    for n in nodes:
        levels.setdefault(depth[n], []).append(n)
    for lvl, ns in levels.items():
        for j, n in enumerate(sorted(ns, key=lambda x: x.name())):
            try:
                n.setXYpos(int(base_x + j * dx), int(base_y + lvl * dy))
            except Exception:
                pass


def _force_safe_write_knobs(write):
    """Force the internal Write into a state safe for cache rendering and a
    correct round-trip: not reading a file back, no frame-range clamp,
    enabled, single view (keeps the cache one sequence matching the file
    pattern), auto-create dirs. Every other knob stays as the template set
    it (file_type, compression, colorspace, channels, ...)."""
    missing = []
    for name, val in (('reading', False), ('use_limit', False),
                      ('disable', False), ('create_directories', True)):
        k = write.knob(name)
        if k is None:
            missing.append(name)
            continue
        try:
            k.setValue(val)
        except Exception:
            pass
    vk = write.knob('views')
    if vk is not None:
        try:
            views = nuke.views() or ['main']
            vk.fromScript(views[0])
        except Exception:
            pass
    else:
        missing.append('views')
    if missing:
        # A real Write always exposes these; a template whose terminal lacks
        # them leaves the cache unforced (e.g. 'reading' still on would read
        # the file back instead of writing it). Surface it for field
        # diagnosis rather than silently shipping a broken cache.
        _log.warning('Write template: cache Write %r is missing expected '
                     'knob(s) %s; left unforced.',
                     write.name(), ', '.join(missing))


def _warn_missing_write_templates(missing):
    """Popup listing inputs whose write template file was not found, with its
    scope (global / workflow). ``missing`` is a list of (label, filename,
    source)."""
    from Nukomfy.utils.qt_compat import QtCore, QtWidgets, _nuke_main_window
    from Nukomfy.gui import _dialogs

    def _scope(src):
        return src if src in ('global', 'workflow') else 'global or workflow'

    detail_plain = ['  - {}: {} ({})'.format(lbl, tpl, _scope(src))
                    for lbl, tpl, src in missing]
    detail_html = '<br>'.join(
        '&nbsp;&nbsp;- {}: {} ({})'.format(
            html.escape(lbl), html.escape(tpl), _scope(src))
        for lbl, tpl, src in missing)
    intro = ('Some write templates could not be loaded and were replaced '
             'with a default Write node:')
    footer = ('Restore or fix the files (one Input and one Write, connected '
              'Input to Write), or edit the workflow to pick another template.')
    body = '{}<br><br>{}<br><br>{}'.format(intro, detail_html, footer)

    try:
        parent = _nuke_main_window()
    except Exception:
        parent = None
    box = _dialogs.message_box(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle('Cannot build gizmo')
    box.setTextFormat(QtCore.Qt.RichText)
    box.setText(body)
    box.setStandardButtons(QtWidgets.QMessageBox.Ok)

    # Size the dialog to hold its widest line on a single line (no wrap),
    # adapting to the dynamic list rows: set the message label's minimum
    # width to the widest rendered line plus breathing room. QMessageBox
    # otherwise chooses its own width and wraps the longer sentences.
    fm = box.fontMetrics()
    widest = max(fm.horizontalAdvance(s)
                 for s in [intro, footer] + detail_plain)
    label = box.findChild(QtWidgets.QLabel, 'qt_msgbox_label')
    if label is not None:
        label.setWordWrap(True)
        label.setMinimumWidth(widest + 32)
    box.exec_()


# ---------------------------------------------------------------------------
# ComfyUI type -> Nuke knob builder
# ---------------------------------------------------------------------------

# ComfyUI display modes that render the widget as an interactive
# selector (slider/dial/gradient bar) rather than a spinbox.
_SLIDER_DISPLAY_MODES = ('slider', 'knob', 'gradientslider')


def _parse_hex_color(hex_str):
    """Parse '#RRGGBB' or '#RRGGBBAA' into a tuple of floats 0.0-1.0.

    Returns 3-tuple for RGB, 4-tuple for RGBA, or None if invalid.
    """
    if not isinstance(hex_str, str):
        return None
    h = hex_str.lstrip('#').strip()
    try:
        if len(h) == 6:
            return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))
        if len(h) == 8:
            return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4, 6))
    except ValueError:
        pass
    return None


def _make_knob(param):
    """Create a Nuke knob from a gizmo_param dict. Returns the knob or None."""
    name     = param.get('name', '')
    label    = _sanitize_label(param.get('label', name))
    tooltip  = param.get('tooltip', '')
    ptype    = param.get('type', '').upper()
    default  = param.get('default_value')
    knob_name = param.get('_knob_name', _safe_knob_name(name))

    if ptype == 'INT':
        min_v = param.get('min_value')
        max_v = param.get('max_value')

        try:
            max_v_raw = int(param.get('max_value') or 0)
        except (TypeError, ValueError):
            max_v_raw = 0

        # Seeds and uint64-range INTs become String_Knob: no precision
        # limit, no slider, validation via knobChanged + a hidden
        # "{name}_last" knob. Every is_seed param uses String for uniform
        # UX even when its range fits in int (e.g. signed-int max=2^31-1).
        if param.get('is_seed') or max_v_raw >= 2**32:
            k = nuke.String_Knob(knob_name, label)
            # Clamp the build-time default to the widget's real max
            # (falls back to the global uint64 cap when unset).
            seed_max = (min(max_v_raw, _SEED_MAX_INPUT)
                        if max_v_raw > 0 else _SEED_MAX_INPUT)
            default_str = '0'
            if default is not None:
                try:
                    default_str = str(max(0, min(int(default), seed_max)))
                except (ValueError, TypeError):
                    pass
            k.setValue(default_str)
            if tooltip:
                k.setTooltip(tooltip)
            return k

        # INT slider (display in {slider, knob, gradientslider} with a
        # range) -> Double_Knob. Int_Knob's slider needs the SLIDER flag,
        # whose Qt geometry renders compressed at create and is dropped on
        # .nk save/load. Double_Knob has a native slider, always full-width,
        # no flag. The wire value stays int because _nfy_params keeps
        # type='INT' and _clamp_and_snap_numeric rounds at commit.
        if (param.get('_display_mode') in _SLIDER_DISPLAY_MODES
                and (min_v is not None or max_v is not None)):
            k = nuke.Double_Knob(knob_name, label)
            k.setRange(float(min_v if min_v is not None else 0),
                       float(max_v if max_v is not None else 1e9))
            if default is not None:
                try:
                    k.setValue(float(default))
                except (ValueError, TypeError):
                    pass
            if tooltip:
                k.setTooltip(tooltip)
            return k

        # INT spinbox (display number / none) -> Int_Knob. No SLIDER flag,
        # so no geometry bug; born correct.
        k = nuke.Int_Knob(knob_name, label)
        if default is not None:
            try:
                val = int(default)
                k.setValue(min(val, int(1e9)))
            except (ValueError, TypeError):
                pass
        if min_v is not None or max_v is not None:
            k.setRange(int(min_v if min_v is not None else 0),
                        int(min(max_v if max_v is not None else 1e9, 1e9)))
        if tooltip:
            k.setTooltip(tooltip)
        return k

    if ptype == 'FLOAT':
        k = nuke.Double_Knob(knob_name, label)
        min_v = param.get('min_value')
        max_v = param.get('max_value')
        if min_v is not None or max_v is not None:
            k.setRange(float(min_v if min_v is not None else 0),
                        float(max_v if max_v is not None else 1))
        if default is not None:
            try:
                k.setValue(float(default))
            except (ValueError, TypeError):
                pass
        if tooltip:
            k.setTooltip(tooltip)
        return k

    if ptype == 'FILE':
        k = nuke.File_Knob(knob_name, label)
        if default is not None:
            k.setValue(str(default))
        if tooltip:
            k.setTooltip(tooltip)
        return k

    if ptype == 'STRING':
        if param.get('multiline'):
            k = nuke.Multiline_Eval_String_Knob(knob_name, label)
            if default is not None:
                k.setText(str(default))
        else:
            k = nuke.String_Knob(knob_name, label)
            if default is not None:
                k.setValue(str(default))
        if tooltip:
            k.setTooltip(tooltip)
        return k

    if ptype == 'BOOLEAN':
        k = nuke.Boolean_Knob(knob_name, label)
        k.setFlag(nuke.STARTLINE)
        if default is not None:
            try:
                k.setValue(bool(default))
            except (ValueError, TypeError):
                pass
        if tooltip:
            k.setTooltip(tooltip)
        return k

    if ptype == 'COMBO':
        values = param.get('combo_values', [])
        # Strip empty strings - a sole "" entry would force Nuke to log
        # "Bad value for X" on every load because the Enumeration's default
        # value (empty) is not a valid item label. Happens for upload-style
        # COMBOs like VHS_LoadVideoFFmpeg.video / VHS_LoadAudioUpload.audio
        # whose ComfyUI list is dynamic and may be empty until upload.
        values = [v for v in (values or []) if str(v).strip()]
        if not values and default is not None and str(default).strip():
            values = [str(default)]
        if values:
            k = nuke.Enumeration_Knob(knob_name, label, values)
            if default is not None and str(default).strip():
                k.setValue(str(default))
        else:
            # Empty option list -> fall back to free-text String_Knob so the
            # user can type a filename / value manually.
            k = nuke.String_Knob(knob_name, label)
            if default is not None:
                k.setValue(str(default))
        if tooltip:
            k.setTooltip(tooltip)
        return k

    if ptype == 'COLOR':
        # ComfyUI COLOR type stores hex string ('#RRGGBB' or '#RRGGBBAA').
        # Map to AColor_Knob when alpha is present, otherwise Color_Knob.
        # Used by core nodes (Painter.bg_color, ColorToRGBInt.color) and any
        # custom node declaring `("COLOR", {"default": "#hex"})` in INPUT_TYPES.
        rgba = _parse_hex_color(default) if isinstance(default, str) else None
        has_alpha = rgba is not None and len(rgba) == 4
        if has_alpha:
            k = nuke.AColor_Knob(knob_name, label)
        else:
            k = nuke.Color_Knob(knob_name, label)
        if rgba is not None:
            for i, v in enumerate(rgba):
                try:
                    k.setValue(float(v), i)
                except Exception:
                    pass
        if tooltip:
            k.setTooltip(tooltip)
        return k

    # Fallback: plain String_Knob (no TCL evaluation - prevents [..] / $var corruption)
    k = nuke.String_Knob(knob_name, label)
    if default is not None:
        k.setValue(str(default))
    if tooltip:
        k.setTooltip(tooltip)
    return k


def _safe_knob_name(name):
    """Sanitize a name into a valid Nuke node/knob name.

    Keeps letters and digits (Unicode included) and underscore; any other
    character becomes '_'. A node name cannot start with a digit (knob names
    are more permissive), so a leading digit or an empty result is prefixed
    with '_' (e.g. "3D Mask" -> "_3D_Mask") to keep the Input/Write node names
    legal. No-op for names that are already valid.
    """
    s = ''.join(c if c.isalnum() or c == '_' else '_' for c in name)
    if not s or s[0].isdigit():
        s = '_' + s
    return s


def _nuke_color(color_int):
    """Convert 0xRRGGBB00 to 0xRRGGBBFF for Nuke tile_color."""
    if not color_int:
        return 0
    return (color_int & 0xFFFFFF00) | 0xFF


def _assign_unique_knob_names(params):
    """Assign unique _knob_name to params, disambiguating duplicates by node_id."""
    _SKIP_ROLES = ('separator', 'group_begin', 'group_end', 'text')
    name_count = {}
    for p in params:
        if p.get('role') in _SKIP_ROLES:
            continue
        n = p.get('name', '')
        name_count[n] = name_count.get(n, 0) + 1

    for p in params:
        if p.get('role') in _SKIP_ROLES:
            continue
        n = p.get('name', '')
        if name_count.get(n, 0) > 1:
            nid = p.get('target_node_id', '')
            p['_knob_name'] = _safe_knob_name('{}_{}'.format(n, nid))
        else:
            p['_knob_name'] = _safe_knob_name(n)


# ---------------------------------------------------------------------------
# Callback stubs - thin wrappers that import gizmo_callbacks at runtime.
# Automatic callbacks (onCreate, knobChanged) silently swallow ImportError
# so the gizmo can be pasted into a vanilla Nuke without spam. User-clicked
# buttons (Submit, Read Outputs, Version up/down/latest) instead show a
# friendly popup pointing to the install page.
# ---------------------------------------------------------------------------
_NUKOMFY_REQUIRED_MSG = (
    '<p><b>Nukomfy is not installed</b></p>'
    '<p>This gizmo needs Nukomfy to work. Get it from<br>'
    '<a href="https://github.com/francescolorussi/Nukomfy" '
    'style="color:{link};">'
    'github.com/francescolorussi/Nukomfy</a></p>'
).format(link=LINK_FG)


def _user_button_stub(import_line, call_line):
    """try/except wrapper for user-clicked buttons with friendly fallback.

    Fallback is nuke.message: it renders the RichText body with a clickable
    link and needs no Nukomfy (nor PySide) code, so the gizmo still points a
    vanilla-Nuke user at the install page.
    """
    return (
        'try:\n'
        '    {imp}\n'
        '    {call}\n'
        'except ImportError:\n'
        '    nuke.message({msg})\n'
    ).format(imp=import_line, call=call_line,
             msg=repr(_NUKOMFY_REQUIRED_MSG))


_ONCREATE_STUB = (
    'try:\n'
    '    from Nukomfy.gizmos.gizmo_callbacks import on_create\n'
    '    on_create(nuke.thisNode())\n'
    'except ImportError:\n'
    '    pass'
)

_KNOB_CHANGED_STUB = (
    'try:\n'
    '    from Nukomfy.gizmos.gizmo_callbacks import on_knob_changed\n'
    '    on_knob_changed(nuke.thisNode(), nuke.thisKnob())\n'
    'except ImportError:\n'
    '    pass'
)

_VERSION_UP_STUB = _user_button_stub(
    'from Nukomfy.gizmos.gizmo_callbacks import version_up',
    'version_up(nuke.thisNode())')

_VERSION_DOWN_STUB = _user_button_stub(
    'from Nukomfy.gizmos.gizmo_callbacks import version_down',
    'version_down(nuke.thisNode())')

_VERSION_LATEST_STUB = _user_button_stub(
    'from Nukomfy.gizmos.gizmo_callbacks import set_latest_version',
    'set_latest_version(nuke.thisNode())')


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------
def _logo_path_for_workflow(workflow_path):
    """Absolute, forward-slash path to gizmo_logo.png next to workflow.json."""
    if not workflow_path:
        return ''
    wf_dir = os.path.dirname(workflow_path)
    return os.path.join(wf_dir, 'gizmo_logo.png').replace('\\', '/')


def build_header_html(workflow_item):
    """Compose the gizmo header HTML based on gizmo_options.title_mode.

    use_custom_logo  -> <img src="..."/> if gizmo_logo.png exists in the
                        workflow folder, else falls back to plain text title.
    use_gizmo_color  -> colored title (manual override from title_color if
                        set, else the gizmo color itself).
    default          -> plain <font size="5"><b>name</b></font>.

    The title block is suppressed entirely when gizmo_options.title is False.
    Subtitle (version/author) and description are appended after the title
    block when their respective flags are on; separators are recomputed so
    they don't leave a leading blank line when the title is hidden.
    """
    name    = workflow_item.name
    author  = workflow_item.author
    version = workflow_item.version
    desc    = workflow_item.description
    color   = workflow_item.gizmo_color
    opts    = workflow_item.gizmo_options

    header_html = ''
    if opts.get('title', True):
        title_mode = opts.get('title_mode', 'use_gizmo_color')
        if title_mode == 'use_custom_logo':
            logo_path = _logo_path_for_workflow(workflow_item.workflow_path)
            if logo_path and os.path.isfile(logo_path):
                header_html = '<img src="{}"/>'.format(logo_path)
            else:
                header_html = '<font size="5"><b>{}</b></font>'.format(
                    _html_escape(name))
        elif title_mode == 'use_gizmo_color' and color:
            custom = opts.get('title_color')
            if custom and isinstance(custom, str) and custom.startswith('#'):
                title_color = custom
            else:
                title_color = '#{:06X}'.format((color >> 8) & 0xFFFFFF)
            header_html = '<font size="5" color="{}"><b>{}</b></font>'.format(
                title_color, _html_escape(name))
        else:
            header_html = '<font size="5"><b>{}</b></font>'.format(
                _html_escape(name))

    subtitle_parts = []
    if opts.get('versioning', True) and version:
        subtitle_parts.append('v{}'.format(_html_escape(version)))
    if opts.get('author', True) and author:
        subtitle_parts.append('by {}'.format(_html_escape(author)))
    if subtitle_parts:
        sep = '<br>' if header_html else ''
        header_html += sep + '<font size="3" color="#999">{}</font>'.format(
            ' - '.join(subtitle_parts))
    if opts.get('description', True) and desc:
        desc_html = _sanitize_label(desc).replace('\n', '<br>')
        sep = '<br><br>' if header_html else ''
        header_html += sep + '<font color="#999">{}</font>'.format(desc_html)
    return header_html


def _classify_params(params):
    """Split gizmo_params into the param lists build_gizmo needs.

    Drops params that must not materialize (non-functional nodes, disabled
    knobs, Suite V3 subs whose top master is itself disabled), assigns
    unique knob names, and groups the kept knobs into input / model /
    output sections by their target node. Returns:
    (inputs_p, knobs_p, outputs_p, all_params,
     input_knobs, model_knobs, output_knobs).
    """
    # Filter only enabled params (separators have no 'enabled' key).
    # Exception for Suite NukomfyRead / NukomfyWrite: their V3 sub
    # rows always materialize as knobs in the gizmo even when
    # enabled=False, because the parser flags non-current-option subs
    # disabled in the Workflow Editor but they must still exist for
    # the runtime visibility cascade (file_type change reveals the
    # right sub set). For any other custom node, the user's expose
    # decision in the Workflow Editor is final.
    #
    # Sub-exception: when the top-level Suite V3 MASTER itself is
    # disabled (user did not opt in to expose file_type), the subs
    # must NOT materialize either. Without the master knob in the
    # gizmo, _emit_v3_option_tab_open cannot resolve the master to
    # hide the non-active Tab_Knob wraps, so every "<FORMAT> Options"
    # tab would stay visible at runtime.

    # Pre-compute: top-level Suite V3 masters that are disabled. Their
    # subs share fate with the master (drop them too).
    _disabled_v3_masters = {
        (p.get('target_node_id'),
         p.get('widget_name') or p.get('name') or '')
        for p in params
        if (p.get('node_type') in _SUITE_V3_NODE_TYPES
            and p.get('_v3_is_dynamic_master')
            and not p.get('_v3_master')
            and not p.get('enabled', True))}

    def _kept(p):
        # A non-functional node (disconnected/bypassed/muted) never ships
        # in the gizmo - it has no runtime effect. Guard here so it holds
        # on every build path regardless of the Enabled/override chain
        # (a non-active V3 sub of such a node has no row to force off).
        if p.get('_node_state'):
            return False
        v3m = p.get('_v3_master', '')
        # A Suite V3 sub shares the fate of its TOP master: when that master
        # is not exposed there is no master knob to drive the visibility
        # cascade, so no option wrap may materialize - not even a sub that
        # is itself enabled. A nested master (e.g. file_type.exr_compression)
        # keeps enabled=True in the snapshot to drive its own subtree, so
        # this guard MUST precede the `enabled` short-circuit below, or that
        # sub slips through and its option wrap is built visible even though
        # the master is locked to a single option.
        if v3m and p.get('node_type') in _SUITE_V3_NODE_TYPES:
            top_master = v3m.split('.', 1)[0]
            if (p.get('target_node_id'), top_master) in _disabled_v3_masters:
                return False
        if p.get('enabled', True):
            return True
        # Suite V3 sub of an EXPOSED master (else already dropped above):
        # keep even when disabled so the runtime cascade can reveal it when
        # its option is picked.
        if v3m and p.get('node_type') in _SUITE_V3_NODE_TYPES:
            return True
        # Generic (non-Suite) V3 master: always materialize, even when not
        # exposed, so it drives sub visibility + submit-strip. _add_knobs
        # builds it hidden (locked at its saved value) when disabled.
        if (p.get('_v3_is_dynamic_master') and not v3m
                and p.get('node_type') not in _SUITE_V3_NODE_TYPES):
            return True
        return False
    inputs_p  = [p for p in params
                 if p.get('role') == 'input' and _kept(p)]
    # Include group_begin/group_end markers and user text rows - they
    # are structural, always included (no 'enabled' flag).
    knobs_p   = [p for p in params
                 if (p.get('role') in ('separator', 'group_begin',
                                       'group_end', 'text')
                     or (p.get('role') == 'knob' and _kept(p)))]
    outputs_p = [p for p in params
                 if p.get('role') == 'output' and _kept(p)]

    # Assign unique knob names (disambiguate duplicates like tonemap_2 / tonemap_3)
    all_params = inputs_p + knobs_p + outputs_p
    _assign_unique_knob_names(all_params)

    # Classify knobs by section (using fixed separators from metadata)
    input_node_ids = {p.get('target_node_id') for p in inputs_p}
    output_node_ids = {p.get('target_node_id') for p in outputs_p}
    # Separate knobs into sections based on saved order
    # knobs_p already contains fixed section separators in the correct order
    input_knobs = []   # knobs from input nodes
    model_knobs = []   # knobs from model nodes (neither input nor output)
    output_knobs = []  # knobs from output nodes
    for kp in knobs_p:
        if kp.get('role') == 'separator' and kp.get('fixed'):
            continue  # skip fixed section separators, we create them ourselves
        nid = kp.get('target_node_id')
        if nid in input_node_ids:
            input_knobs.append(kp)
        elif nid in output_node_ids:
            output_knobs.append(kp)
        else:
            model_knobs.append(kp)
    return (inputs_p, knobs_p, outputs_p, all_params,
            input_knobs, model_knobs, output_knobs)


def sanitize_gizmo_chars(name):
    """Map a raw name to Nuke-safe characters: anything that is not
    alphanumeric or '_' becomes '_'. isalnum is Unicode-aware on purpose -
    Nuke accepts accented/CJK node names but rejects punctuation. Single
    source of the character rule, shared by final_gizmo_node_name and the
    Workflow Editor name field (validator + placeholder).
    """
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name)


def final_gizmo_node_name(name):
    """Resolve a raw name to the final Nuke node name for a gizmo.

    Sanitizes the characters (see sanitize_gizmo_chars), then prepends the
    Nukomfy_ prefix when the setting is on, or guards a leading digit with
    '_' when it is off (Nuke node names must not start with a number). The
    Workflow Editor builds the same name, so its preview and the built
    node never drift.
    """
    safe_name = sanitize_gizmo_chars(name)
    if settings.gizmo_name_prefix:
        safe_name = 'Nukomfy_' + safe_name
    elif safe_name[:1].isdigit():
        # Without the prefix a leading digit is possible; Nuke node names
        # must not start with a number.
        safe_name = '_' + safe_name
    return safe_name


def _create_group(name, color, opts):
    """Create the Group node, name it, and apply tile + title-node colors."""
    # inpanel=True so Nuke computes the initial multiline-knob height at
    # creation; with inpanel=False, Multiline_Eval_String_Knob gets saved
    # collapsed and reloads collapsed.
    group = nuke.createNode('Group', inpanel=True)
    group.setName(final_gizmo_node_name(name))

    if color:
        group['tile_color'].setValue(_nuke_color(color))

    title_node_hex = opts.get('title_node_color')
    if (title_node_hex and isinstance(title_node_hex, str)
            and title_node_hex.startswith('#')):
        try:
            # Presence of the hex IS the "set" signal; always apply with
            # opaque alpha so pure black ('#000000') renders as black on
            # the tile instead of being treated as 0/unset by Nuke.
            rgb = int(title_node_hex.lstrip('#'), 16) & 0xFFFFFF
            group['note_font_color'].setValue((rgb << 8) | 0xFF)
        except ValueError:
            pass

    # Group View is a Nuke 16 feature; the disable_group_view knob is absent
    # on Nuke 14, so guard on presence before setting it.
    if (settings.gizmo_disable_group_view
            and 'disable_group_view' in group.knobs()):
        group['disable_group_view'].setValue(True)
    return group


def _add_header_section(group, workflow_item):
    """Add the title/header HTML and the optional Extra Info block."""
    opts = workflow_item.gizmo_options
    usage = workflow_item.usage
    # When off, the author controls line breaks manually (the text is shown
    # verbatim), which sidesteps Nuke's word-wrapped label clipping the last
    # lines in a narrow floating panel. on_create re-reads this from
    # _nfy_word_wrap. Output-path previews always wrap (handled separately).
    word_wrap = opts.get('word_wrap', True)

    header_html = build_header_html(workflow_item)
    if header_html:
        header_knob = nuke.Text_Knob('_header', '', header_html)
        if word_wrap:
            header_knob.setFlag(_WORD_WRAP_FLAG)
        group.addKnob(header_knob)

    if opts.get('usage', True) and usage:
        group.addKnob(nuke.Text_Knob('_sp_usage', '', '<br>'))
        group.addKnob(nuke.Text_Knob('_usage', '', '<b>Extra Info</b>'))
        group.addKnob(nuke.Text_Knob('_usage_line', '', ''))
        usage_html = _sanitize_label(usage).replace('\n', '<br>')
        usage_knob = nuke.Text_Knob('_usage_text', '', usage_html)
        if word_wrap:
            usage_knob.setFlag(_WORD_WRAP_FLAG)
        group.addKnob(usage_knob)


def _build_widget_to_knob(all_params):
    """Map (target_node_id, widget_name) -> runtime knob name. Two readers:
    V3 subs resolving their master's knob (nested masters are indexed too,
    so deeper subs find their parent), and Suite grey-out resolving trigger
    and dependent widgets on the same node."""
    out = {}
    for kp in all_params:
        if kp.get('role') == 'separator':
            continue
        wn = kp.get('widget_name') or kp.get('name') or ''
        nid = kp.get('target_node_id')
        kn = kp.get('_knob_name', '')
        if wn and kn:
            out[(nid, wn)] = kn
    return out


def _build_suite_grey_map(all_params, widget_to_knob):
    """Map {trigger_knob: [dependent_knobs]} for Suite boolean-trigger
    grey-out, resolving widget names to knob names on the same node. Read at
    runtime by on_knob_changed and on paste/load by on_create."""
    out = {}
    for kp in all_params:
        if kp.get('role') == 'separator':
            continue
        node_type = kp.get('node_type', '')
        widget_name = kp.get('widget_name') or kp.get('name') or ''
        deps = suite_dependents(node_type, widget_name)
        if not deps:
            continue
        nid = kp.get('target_node_id')
        trigger_kn = widget_to_knob.get((nid, widget_name))
        if not trigger_kn:
            continue
        dep_kns = []
        for dep_wn in deps:
            dep_kn = widget_to_knob.get((nid, dep_wn))
            if dep_kn:
                dep_kns.append(dep_kn)
        if dep_kns:
            out[trigger_kn] = dep_kns
    return out


class _KnobCtx(object):
    """Mutable state shared by the knob-adding helpers. build_gizmo creates
    one bundling its own lists/maps (by reference); the _add_knobs pass
    fills them and build_gizmo reads them back to emit the storage knobs and
    the onCreate script."""
    def __init__(self, group, widget_to_knob, v3_visibility_map,
                 seed_knob_names, multiline_knob_names,
                 hidden_master_knob_names, pending_storage_knobs):
        self.group = group
        self.widget_to_knob = widget_to_knob
        self.v3_visibility_map = v3_visibility_map
        self.seed_knob_names = seed_knob_names
        self.multiline_knob_names = multiline_knob_names
        self.hidden_master_knob_names = hidden_master_knob_names
        self.pending_storage_knobs = pending_storage_knobs


# Suite NukomfyWrite-specific: wrap consecutive sub-inputs of the same
# DynamicCombo option (e.g. all `exr_*` subs) in a TABBEGINCLOSEDGROUP /
# TABENDGROUP Tab_Knob pair. ALL option wraps are created at build time;
# non-active wraps are hidden via the HIDDEN flag (raw hex). The runtime
# cascade toggles the same flag to show/hide. file_type='hdr' simply hides
# every wrap (HDR has no options).
def _emit_v3_option_tab_open(ctx, nid, top_master, opt_key):
    group = ctx.group
    _widget_to_knob = ctx.widget_to_knob
    v3_visibility_map = ctx.v3_visibility_map
    tab_kn = '_v3tab_{}_{}_{}'.format(
        _safe_knob_name(str(nid)),
        _safe_knob_name(top_master),
        _safe_knob_name(opt_key))
    tab_label = '{} Options'.format(opt_key.upper())
    tab_open = nuke.Tab_Knob(
        tab_kn, tab_label, nuke.TABBEGINCLOSEDGROUP)
    group.addKnob(tab_open)
    master_kn = _widget_to_knob.get((nid, top_master))
    if master_kn:
        v3_visibility_map.setdefault(
            master_kn, {})[tab_kn] = [opt_key]
        try:
            mk = group.knob(master_kn)
            if mk is not None and mk.value() != opt_key:
                # Pair HIDDEN + STARTLINE atomically even on the
                # Tab_Knob wrap. Nuke ignores STARTLINE on tabs so
                # the second call is a functional no-op, but it
                # keeps the build-time write consistent with the
                # runtime _apply_visibility pattern.
                tab_open.setFlag(_HIDDEN_FLAG)
                tab_open.clearFlag(_STARTLINE_FLAG)
        except Exception:
            pass
    return tab_kn


def _emit_v3_option_tab_close(ctx, tab_kn):
    ctx.group.addKnob(nuke.Tab_Knob(
        tab_kn + '_end', '', nuke.TABENDGROUP))


def _add_knobs(ctx, knob_list):
    """Add a section's knobs to the group: user separators, group brackets,
    text rows, real knobs, seeds, and V3 sub registration. Mutates ctx
    (v3_visibility_map, the seed / multiline / hidden-master name lists, and
    pending storage knobs)."""
    group = ctx.group
    _widget_to_knob = ctx.widget_to_knob
    v3_visibility_map = ctx.v3_visibility_map
    seed_knob_names = ctx.seed_knob_names
    multiline_knob_names = ctx.multiline_knob_names
    hidden_master_knob_names = ctx.hidden_master_knob_names
    _pending_storage_knobs = ctx.pending_storage_knobs

    # State for the per-option Tab_Knob wrap around Suite V3
    # subs. `_open_tab` is (nid, top_master, opt_key, tab_kn) when
    # a tab is currently open and the next knob should land
    # inside it; None when the previous knob was not a Suite V3
    # sub (or had a different option), so a fresh tab must be
    # opened before the next sub is added.
    _open_tab = None

    for kp in knob_list:
        # Decide whether this knob extends or breaks the current
        # Suite V3 option group.
        _kp_opt = None
        _kp_top = None
        _kp_nid = None
        if (kp.get('role') == 'knob'
                and kp.get('node_type') in _SUITE_TAB_NODES
                and kp.get('_v3_master')):
            _kp_v3m = kp.get('_v3_master', '')
            _kp_top = _kp_v3m.split('.', 1)[0]
            _kp_nid = kp.get('target_node_id')
            if '.' not in _kp_v3m:
                # Direct sub of the top master: its option key
                # comes straight from _v3_show_for_keys.
                _show_keys = list(
                    kp.get('_v3_show_for_keys', []) or [])
                if len(_show_keys) == 1:
                    _kp_opt = _show_keys[0]
            else:
                # Nested sub (e.g. dw_compression_level under
                # exr_compression): inherit the currently open
                # tab's option key if it matches the top master.
                if (_open_tab is not None
                        and _open_tab[0] == _kp_nid
                        and _open_tab[1] == _kp_top):
                    _kp_opt = _open_tab[2]
        # Open / close tab boundaries.
        if _kp_opt is not None:
            _new_tab_key = (_kp_nid, _kp_top, _kp_opt)
            if (_open_tab is None
                    or _open_tab[:3] != _new_tab_key):
                if _open_tab is not None:
                    _emit_v3_option_tab_close(ctx, _open_tab[3])
                _tab_kn = _emit_v3_option_tab_open(
                    ctx, _kp_nid, _kp_top, _kp_opt)
                _open_tab = (_kp_nid, _kp_top, _kp_opt, _tab_kn)
        else:
            # Knob does not belong to any Suite V3 option tab:
            # close any open tab before adding it.
            if _open_tab is not None:
                _emit_v3_option_tab_close(ctx, _open_tab[3])
                _open_tab = None
        if kp.get('role') == 'separator':
            sep_text = kp.get('label', '')
            sep_knob_name = '_sep_{}'.format(
                _safe_knob_name(sep_text) if sep_text else str(id(kp)))
            if sep_text:
                sk = nuke.Text_Knob(sep_knob_name,
                                    '<b>{}</b>'.format(
                                        _html_escape(sep_text)), '')
            else:
                sk = nuke.Text_Knob(sep_knob_name, '', '')
            group.addKnob(sk)
            continue
        # User-defined collapsible group brackets in Model Parameters.
        # Begin emits a TABBEGIN[CLOSED]GROUP, End emits
        # a TABENDGROUP - knobs added between the two automatically
        # land inside the resulting tab.
        if kp.get('role') == 'group_begin':
            gid = kp.get('id', 0)
            glabel = kp.get('label', '') or ''
            gflag = (nuke.TABBEGINGROUP
                     if (kp.get('default', 'closed') == 'open')
                     else nuke.TABBEGINCLOSEDGROUP)
            gname = '_nukomfy_group_{}'.format(gid)
            group.addKnob(nuke.Tab_Knob(gname, glabel, gflag))
            continue
        if kp.get('role') == 'group_end':
            gid = kp.get('id', 0)
            gname = '_nukomfy_group_{}_end'.format(gid)
            group.addKnob(nuke.Tab_Knob(gname, '', nuke.TABENDGROUP))
            continue
        # User-added text knob in Model Parameters. Stable auto-name
        # based on row identity; label and value are
        # the user's raw strings (whitespace-preserving, HTML
        # supported by Nuke's Text_Knob).
        if kp.get('role') == 'text':
            tname = '_nukomfy_text_{}'.format(id(kp))
            tlabel = kp.get('label', '') or ''
            tvalue = kp.get('value', '') or ''
            group.addKnob(nuke.Text_Knob(tname, tlabel, tvalue))
            continue
        knob = _make_knob(kp)
        if knob:
            group.addKnob(knob)
            # Generic V3 master built while not exposed: hide it
            # (locked at its saved value). It stays present to drive
            # sub visibility + submit-strip, but the artist cannot
            # change the option. Suite masters are never force-hidden
            # here - their cluster exposure is atomic.
            if (kp.get('_v3_is_dynamic_master')
                    and not kp.get('_v3_master')
                    and kp.get('node_type') not in _SUITE_V3_NODE_TYPES
                    and not kp.get('enabled', True)):
                knob.setFlag(_HIDDEN_FLAG)
                knob.clearFlag(_STARTLINE_FLAG)
                hidden_master_knob_names.append(
                    kp.get('_knob_name', ''))
            # V3 sub-knob - register for visibility tracking. The
            # visibility map carries one entry per master that
            # gates this sub: the direct _v3_master plus any
            # ancestor in _v3_ancestor_conditions for nested subs
            # (dw_compression_level needs file_type=exr AND
            # compression in [DWAA, DWAB]). gizmo_callbacks ANDs
            # every entry mentioning the sub at refresh time.
            # Every sub (Suite or non-Suite) is toggled via the atomic
            # HIDDEN + STARTLINE pair, which keeps the row layout stable
            # as it toggles.
            if kp.get('_v3_master'):
                master_wn = kp.get('_v3_master', '')
                nid = kp.get('target_node_id')
                master_kn = _widget_to_knob.get((nid, master_wn))
                sub_kn = kp.get('_knob_name', '')
                show_keys = list(kp.get('_v3_show_for_keys', []) or [])
                if master_kn and sub_kn:
                    v3_visibility_map.setdefault(
                        master_kn, {})[sub_kn] = show_keys
                # Ancestor conditions for nested subs.
                for anc in (kp.get('_v3_ancestor_conditions') or []):
                    if not (isinstance(anc, (list, tuple))
                            and len(anc) == 2):
                        continue
                    anc_path, anc_keys = anc[0], list(anc[1] or [])
                    anc_kn = _widget_to_knob.get(
                        (nid, anc_path))
                    if anc_kn and sub_kn:
                        v3_visibility_map.setdefault(
                            anc_kn, {})[sub_kn] = anc_keys
                if sub_kn:
                    try:
                        visible = True
                        if master_kn:
                            mk = group.knob(master_kn)
                            if mk is not None and mk.value() not in show_keys:
                                visible = False
                        if visible:
                            for anc in (kp.get('_v3_ancestor_conditions') or []):
                                if not (isinstance(anc, (list, tuple))
                                        and len(anc) == 2):
                                    continue
                                anc_path, anc_keys = anc[0], list(anc[1] or [])
                                anc_kn = _widget_to_knob.get(
                                    (nid, anc_path))
                                if anc_kn is None:
                                    continue
                                ak = group.knob(anc_kn)
                                if ak is None:
                                    continue
                                if ak.value() not in anc_keys:
                                    visible = False
                                    break
                        if not visible:
                            knob.setFlag(_HIDDEN_FLAG)
                            knob.clearFlag(_STARTLINE_FLAG)
                    except Exception:
                        pass
            if (kp.get('type', '').upper() == 'STRING'
                    and kp.get('multiline')):
                multiline_knob_names.append(
                    kp.get('_knob_name', ''))
            if kp.get('is_seed'):
                kn = kp.get('_knob_name', '')
                ctrl = nuke.Enumeration_Knob(
                    '{}_control'.format(kn),
                    '',
                    _SEED_CONTROL_OPTIONS)
                # Pick up the workflow's original control_after_generate
                # so the gizmo mirrors what ComfyUI showed (e.g. 'fixed').
                # Fallback to 'randomize' when the param carries no
                # recorded value.
                seed_ctrl_default = kp.get('seed_control_default')
                if seed_ctrl_default in _SEED_CONTROL_OPTIONS:
                    ctrl.setValue(seed_ctrl_default)
                else:
                    ctrl.setValue('randomize')
                ctrl.setTooltip(
                    'Control how the seed changes after each generation:\n'
                    '\n'
                    'randomize - pick a new random seed\n'
                    'increment - raise the seed by one\n'
                    'decrement - lower the seed by one\n'
                    'fixed - keep the seed unchanged')
                ctrl.clearFlag(nuke.STARTLINE)
                group.addKnob(ctrl)
                _last_k = nuke.String_Knob('{}_last'.format(kn), '')
                _last_k.setValue(str(knob.value()))
                _last_k.setVisible(False)
                _pending_storage_knobs.append(_last_k)
                seed_knob_names.append(kn)
            # frame_padding never becomes a gizmo knob. No-op - the
            # workflow_creator filters it but if a stale metadata.json
            # sneaks one in, _add_knobs would have already added it;
            # leave the knob orphan and ignored at runtime (read sites
            # all use settings.frame_padding).

    # Close any Suite V3 option tab still open at the end of the
    # knob list (defensive - normally the next non-Suite-V3 knob
    # closes it).
    if _open_tab is not None:
        _emit_v3_option_tab_close(ctx, _open_tab[3])


def _add_input_section(ctx, inputs_p, input_knobs):
    """Input Parameters section: each input's knobs in a collapsible tab.
    Always grouped (even for a single input) for visual consistency."""
    if not input_knobs:
        return
    group = ctx.group
    group.addKnob(nuke.Text_Knob('_sp_inp_params', '', '<br>'))
    group.addKnob(nuke.Text_Knob('_inp_params_label', '',
                                 '<b>Input Parameters</b>'))
    group.addKnob(nuke.Text_Knob('_inp_params_line', '', ''))
    knobs_by_input = {}
    for kp in input_knobs:
        nid = kp.get('target_node_id')
        knobs_by_input.setdefault(nid, []).append(kp)
    for i, ip in enumerate(inputs_p):
        nid = ip.get('target_node_id')
        node_knobs = knobs_by_input.get(nid, [])
        if not node_knobs:
            continue
        lbl = _sanitize_label(
            ip.get('label', ip.get('name', 'Input {}'.format(i + 1))))
        grp_name = '_inp_opts_{}'.format(i)
        grp_label = '{} Parameters'.format(lbl)
        group.addKnob(nuke.Tab_Knob(grp_name, grp_label,
                                    nuke.TABBEGINCLOSEDGROUP))
        _add_knobs(ctx, node_knobs)
        group.addKnob(nuke.Tab_Knob(grp_name + '_end', grp_label,
                                    nuke.TABENDGROUP))


def _add_model_section(ctx, model_knobs):
    """Model Parameters section (knobs from nodes that are neither input nor
    output)."""
    if not model_knobs:
        return
    group = ctx.group
    group.addKnob(nuke.Text_Knob('_sp_params', '', '<br>'))
    group.addKnob(nuke.Text_Knob('_params_label', '',
                                 '<b>Model Parameters</b>'))
    group.addKnob(nuke.Text_Knob('_params_line', '', ''))
    _add_knobs(ctx, model_knobs)


def _add_output_section(ctx, outputs_p, output_knobs, show_preview=True):
    """Output Parameters section: label, the global versioning row
    (Version + up / down / Set Latest), per-output name + preview rows, and
    the output-node knobs in collapsible tabs. When show_preview is False the
    resolved-path preview knobs are omitted (the output name knobs stay)."""
    group = ctx.group
    group.addKnob(nuke.Text_Knob('_sp_output', '', '<br>'))
    group.addKnob(nuke.Text_Knob('_output_label', '', '<b>Output Parameters</b>'))
    group.addKnob(nuke.Text_Knob('_output_line', '', ''))

    # Versioning row at the top of the Output section (before names).
    # Version is a global property of the output set: placing it here
    # makes it visually clear that it applies to all outputs.
    _out_ver = nuke.Int_Knob('_output_version', 'Version:')
    _out_ver.setValue(1)
    _out_ver.setTooltip(
        'Current output version.\n'
        'Use the up/down arrows or "Set Latest" to change it.')
    group.addKnob(_out_ver)

    ver_up = nuke.PyScript_Knob('version_up', u'↑', _VERSION_UP_STUB)
    ver_up.clearFlag(nuke.STARTLINE)
    ver_up.setTooltip(
        'Increment Version by 1.\n'
        'The next render writes to the new version folder.')
    group.addKnob(ver_up)

    ver_down = nuke.PyScript_Knob('version_down', u'↓',
                                  _VERSION_DOWN_STUB)
    ver_down.clearFlag(nuke.STARTLINE)
    ver_down.setTooltip(
        'Decrement Version by 1.\n'
        'Existing files are not deleted: this only changes where the '
        'next render will write.')
    group.addKnob(ver_down)

    ver_latest = nuke.PyScript_Knob('version_latest', 'Set Latest',
                                    _VERSION_LATEST_STUB)
    ver_latest.clearFlag(nuke.STARTLINE)
    ver_latest.setTooltip(
        'Scan the output directory and set Version to the highest '
        'version currently rendered on disk.\n\n'
        'For multi-output gizmos, uses the global maximum across all outputs.')
    group.addKnob(ver_latest)

    # Spacer before the output_name rows (matches the pattern used
    # between other gizmo entries).
    group.addKnob(nuke.Text_Knob('_sp_after_ver', '', ' '))

    multi_output = len(outputs_p) > 1

    # Output name(s) - one per output for multi-output, single for single output.
    # Each name is followed immediately by its own preview path, so the multi-
    # output layout reads as [name_1, preview_1, name_2, preview_2, ...] rather
    # than all names stacked above all previews.
    # Text_Knob value is initialized with a single space (not empty) - an empty
    # value is rendered by Nuke as a horizontal separator line, which leaves an
    # overlay artifact when knobChanged later fills it.
    if multi_output:
        for i, op in enumerate(outputs_p):
            lbl = _sanitize_label(
                op.get('label', op.get('name', 'output_{}'.format(i))))
            out_name = nuke.EvalString_Knob(
                'output_name_{}'.format(i),
                '{} Name'.format(lbl))
            out_name.setValue(lbl.lower())
            out_name.setTooltip(
                'Output file name for "{}". Must be unique across outputs.'
                .format(lbl))
            group.addKnob(out_name)
            if show_preview:
                preview_i = nuke.Text_Knob(
                    '_output_preview_{}'.format(i), ' ', ' ')
                preview_i.setFlag(_WORD_WRAP_FLAG)
                preview_i.setTooltip(
                    'Preview of the resolved output path for "{}".'.format(lbl))
                group.addKnob(preview_i)
            elif i < len(outputs_p) - 1 or output_knobs:
                # Spacer in place of the omitted preview so consecutive output
                # names don't cram. Skipped after the last name when no
                # output-parameter groups follow: the submit row already
                # carries its own leading spacer, so one here would double the
                # gap before the separator.
                group.addKnob(
                    nuke.Text_Knob('_sp_outname_{}'.format(i), '', ' '))
    else:
        lbl = _sanitize_label(
            outputs_p[0].get('label', 'Output') if outputs_p else 'Output')
        out_name = nuke.EvalString_Knob('output_name', '{} Name'.format(lbl))
        out_name.setValue(lbl.lower())
        out_name.setTooltip(
            'Output file name. The full path is built automatically.')
        group.addKnob(out_name)
        if show_preview:
            preview = nuke.Text_Knob('_output_preview', ' ', ' ')
            preview.setFlag(_WORD_WRAP_FLAG)
            preview.setTooltip(
                'Preview of the resolved output path.')
            group.addKnob(preview)
        elif output_knobs:
            # Spacer in place of the omitted preview, kept only when output-
            # parameter groups follow (the submit row already carries its own
            # leading spacer, so without groups this would double the gap).
            group.addKnob(nuke.Text_Knob('_sp_outname', '', ' '))

    # Output-node knobs inside a collapsible group
    if output_knobs:
        if multi_output:
            # Group output knobs by target_node_id to match output index
            knobs_by_node = {}
            for kp in output_knobs:
                nid = kp.get('target_node_id')
                knobs_by_node.setdefault(nid, []).append(kp)
            for i, op in enumerate(outputs_p):
                nid = op.get('target_node_id')
                node_knobs = knobs_by_node.get(nid, [])
                if not node_knobs:
                    continue
                lbl = _sanitize_label(
                    op.get('label', op.get('name', 'Output {}'.format(i + 1))))
                grp_name = '_out_opts_{}'.format(i)
                grp_label = '{} Parameters'.format(lbl)
                group.addKnob(nuke.Tab_Knob(grp_name, grp_label,
                                            nuke.TABBEGINCLOSEDGROUP))
                _add_knobs(ctx, node_knobs)
                group.addKnob(nuke.Tab_Knob(grp_name + '_end', grp_label,
                                            nuke.TABENDGROUP))
        else:
            single_lbl = _sanitize_label(
                outputs_p[0].get('label', 'Output') if outputs_p else 'Output')
            grp_label = '{} Parameters'.format(single_lbl)
            group.addKnob(nuke.Tab_Knob('_out_opts', grp_label,
                                        nuke.TABBEGINCLOSEDGROUP))
            _add_knobs(ctx, output_knobs)
            group.addKnob(nuke.Tab_Knob('_out_opts_end', grp_label,
                                        nuke.TABENDGROUP))


def _add_state_knobs(ctx, workflow_alias, suite_grey_map):
    """Hidden state knobs (workflow alias, seed / multiline / V3-map /
    suite-map) plus the knobChanged and onCreate scripts. Applies the initial
    Suite grey-out state and re-hides generic V3 masters built hidden via
    onCreate (Nuke drops the HIDDEN flag on .nk reload)."""
    group = ctx.group
    _pending_storage_knobs = ctx.pending_storage_knobs
    seed_knob_names = ctx.seed_knob_names
    multiline_knob_names = ctx.multiline_knob_names
    v3_visibility_map = ctx.v3_visibility_map
    hidden_master_knob_names = ctx.hidden_master_knob_names

    _wf_alias = nuke.String_Knob('_nfy_workflow_alias', '')
    _wf_alias.setVisible(False)
    _wf_alias.setValue(workflow_alias)
    _pending_storage_knobs.append(_wf_alias)

    # Set knobChanged for seed validation + output path preview
    group['knobChanged'].setValue(_KNOB_CHANGED_STUB)

    # Hidden: seed knob names (read by on_knob_changed for validation)
    _seed_names = nuke.String_Knob('_nfy_seed_knob_names', '')
    _seed_names.setVisible(False)
    _seed_names.setValue(','.join(n for n in seed_knob_names if n))
    _pending_storage_knobs.append(_seed_names)

    # Hidden: multiline knob names (read by on_create for rebuild)
    _ml_names = nuke.String_Knob('_nfy_multiline_names', '')
    _ml_names.setVisible(False)
    _ml_names.setValue(','.join(n for n in multiline_knob_names if n))
    _pending_storage_knobs.append(_ml_names)

    # V3 visibility map (read by on_knob_changed + on_create).
    _v3_map = nuke.String_Knob('_v3_visibility_map', '')
    _v3_map.setVisible(False)
    _v3_map.setValue(encode_payload(v3_visibility_map))
    _pending_storage_knobs.append(_v3_map)

    # Suite boolean-trigger grey-out map. Read by on_knob_changed +
    # on_create. Initial state is also applied here so dependents start
    # greyed if the trigger default is False (currently it defaults True
    # on Read/Write, but the apply pass keeps the build self-contained).
    _suite_map = nuke.String_Knob('_suite_grey_map', '')
    _suite_map.setVisible(False)
    _suite_map.setValue(encode_payload(suite_grey_map))
    _pending_storage_knobs.append(_suite_map)
    for trigger_kn, dep_kns in suite_grey_map.items():
        trigger_knob = group.knob(trigger_kn)
        if trigger_knob is None:
            continue
        try:
            checked = bool(trigger_knob.value())
        except Exception:
            checked = True
        for dep_kn in dep_kns:
            dep_knob = group.knob(dep_kn)
            if dep_knob is None:
                continue
            try:
                dep_knob.setEnabled(checked)
            except Exception:
                pass

    # Set onCreate to rebuild multiline knobs on paste / comp reload, plus
    # re-apply the HIDDEN flag on generic V3 masters built hidden (Nuke
    # drops it on .nk reload).
    oncreate_script = _ONCREATE_STUB
    if hidden_master_knob_names:
        hide_lines = []
        for _kn in hidden_master_knob_names:
            if not _kn:
                continue
            hide_lines.append(
                "try:\n"
                "    _k = nuke.thisNode()['{kn}']\n"
                "    _k.setFlag({hflag})\n"
                "    _k.clearFlag({sflag})\n"
                "except Exception:\n"
                "    pass".format(
                    kn=_kn, hflag=_HIDDEN_FLAG, sflag=_STARTLINE_FLAG))
        if hide_lines:
            oncreate_script = oncreate_script + "\n" + "\n".join(hide_lines)
    group['onCreate'].setValue(oncreate_script)


def _add_submit_read_row(group, outputs_p, tight_top=False):
    """Submit to ComfyUI + Read Output(s) buttons, plus the batch-read-mode
    dropdown when every output is Single-mode (so batch_count > 1 is
    possible). tight_top drops the leading spacer when the element above
    already carries its own bottom gap (the small-font path preview row), so
    the gap before the separator is not doubled."""
    if not tight_top:
        group.addKnob(nuke.Text_Knob('_sp_render2', '', ' '))
    group.addKnob(nuke.Text_Knob('', '', ''))
    group.addKnob(nuke.Text_Knob('_1', '', ' '))

    # batch_read_mode dropdown is only meaningful when the workflow
    # supports batch_count > 1. Submit panel forces batch=1 if any output
    # is Range, so for those workflows the dropdown would be a dead UI
    # element - skip its creation entirely.
    _supports_batch = bool(outputs_p) and all(
        op.get('io_mode', '') == 'Single' for op in outputs_p)

    _SUBMIT_SCRIPT = _user_button_stub(
        'from Nukomfy.gui.submit_panel import show_submit_panel',
        'show_submit_panel(nuke.thisNode())')
    submit_btn = nuke.PyScript_Knob('nfy_submit',
                                     '<b>Submit to ComfyUI</b>',
                                     _SUBMIT_SCRIPT)
    submit_btn.setFlag(nuke.STARTLINE)
    group.addKnob(submit_btn)

    _READ_OUTPUTS_SCRIPT = _user_button_stub(
        'from Nukomfy.gizmos.gizmo_actions import read_outputs',
        'read_outputs(nuke.thisNode())')
    read_btn = nuke.PyScript_Knob('nfy_read_outputs', 'Read Output(s)',
                                  _READ_OUTPUTS_SCRIPT)
    read_btn.clearFlag(nuke.STARTLINE)
    group.addKnob(read_btn)

    if _supports_batch:
        # How Read Output(s) handles multiple batch files in Single-mode
        # outputs. Default 'Batch as single sequence' imports one Read with
        # first/last from glob. 'Batch as separate Reads' creates one Read
        # per file lined up horizontally; multi-output workflows stack one
        # row per output.
        _BATCH_READ_MODES = (
            'Batch as single sequence',
            'Batch as separate Reads',
        )
        batch_read_mode = nuke.Enumeration_Knob(
            'batch_read_mode', '', list(_BATCH_READ_MODES))
        batch_read_mode.clearFlag(nuke.STARTLINE)
        batch_read_mode.setTooltip(
            'How Read Output(s) imports batch results.\n\n'
            'Batch as single sequence (default): all batch frames load '
            'into a single Read node as a frame sequence.\n\n'
            'Batch as separate Reads: each batch file becomes its own '
            'Read node, lined up horizontally. With multiple outputs, '
            'one row per output.')
        group.addKnob(batch_read_mode)


def _add_metadata_knobs(ctx, workflow_item, params, color, opts):
    """Hidden workflow-metadata snapshot knobs read at submit time (path,
    id, hash, categories, models, author, version), the full params payload,
    and the read-node tile-color snapshot."""
    _pending_storage_knobs = ctx.pending_storage_knobs

    # Hidden knobs: workflow metadata needed at submit time. Only the
    # workflow UUID is stored - the JSON is located at submit/read time by
    # UUID lookup against the configured Library roots
    # (_resolve_workflow_path). No absolute path is persisted, so a shared
    # .nk never carries the creator's local filesystem path.
    _wf_id = nuke.String_Knob('_nfy_wf_id', '')
    _wf_id.setVisible(False)
    _wf_id.setValue(workflow_item.workflow_id or '')
    _pending_storage_knobs.append(_wf_id)

    # Real workflow name (metadata.json `name`), snapshotted so the
    # {workflow} output-path token and the Render Manager display always
    # show the workflow itself - independent of any per-instance alias.
    _wf_name = nuke.String_Knob('_nfy_wf_name', '')
    _wf_name.setVisible(False)
    _wf_name.setValue(workflow_item.name or '')
    _pending_storage_knobs.append(_wf_name)

    # Snapshot the workflow logical hash at gizmo creation time so the
    # submit pipeline can detect TD edits to the shared workflow file.
    _wf_hash = nuke.String_Knob('_nfy_wf_hash', '')
    _wf_hash.setVisible(False)
    _wf_hash.setValue(workflow_item.workflow_hash or '')
    _pending_storage_knobs.append(_wf_hash)

    # Workflow metadata snapshots for output_path tokens
    # ({workflow_category}, {workflow_model}). String_Knob carrying a JSON
    # list so multi-tag values stay structured at submit time.
    _wf_categories = nuke.String_Knob('_nfy_wf_categories', '')
    _wf_categories.setVisible(False)
    _wf_categories.setValue(json.dumps(list(workflow_item.tags_category or [])))
    _pending_storage_knobs.append(_wf_categories)

    _wf_models = nuke.String_Knob('_nfy_wf_models', '')
    _wf_models.setVisible(False)
    _wf_models.setValue(json.dumps(list(workflow_item.tags_models or [])))
    _pending_storage_knobs.append(_wf_models)

    # Snapshot the workflow author at gizmo creation time so the submit
    # pipeline can persist it in the job record without re-reading the
    # workflow's metadata.json (which may have moved or been deleted).
    _wf_author = nuke.String_Knob('_nfy_wf_author', '')
    _wf_author.setVisible(False)
    _wf_author.setValue(workflow_item.author or '')
    _pending_storage_knobs.append(_wf_author)

    # SemVer-style workflow version from the metadata.json `version`
    # field - what the user sets in Workflow Creator (e.g. "1.0.0").
    # Snapshotted here so the submit pipeline can persist it without
    # re-reading metadata.json (parallel to _nfy_wf_author above).
    _wf_version = nuke.String_Knob('_nfy_wf_version', '')
    _wf_version.setVisible(False)
    _wf_version.setValue(workflow_item.version or '')
    _pending_storage_knobs.append(_wf_version)

    # String_Knob (not EvalString) to avoid TCL evaluation corrupting JSON.
    # Serialize the FULL params list (including unchecked ones) so that
    # submit-time can honour user-modified default_value for params the
    # user chose not to expose as knobs. `all_params` drives UI
    # construction only; the knob payload must carry everything.
    _params_knob = nuke.String_Knob('_nfy_params', '')
    _params_knob.setVisible(False)
    _params_knob.setValue(encode_payload(params))
    _pending_storage_knobs.append(_params_knob)

    # Read-node tile color snapshot: the gizmo's creation-time color, so
    # Read nodes spawned by `Read Outputs` and by MyJobs match the gizmo
    # even if the user later changes the Group's `tile_color`. Empty when
    # no color was picked or the user disabled `color_reads`.
    _read_color = nuke.String_Knob('_nfy_read_color', '')
    _read_color.setVisible(False)
    if color and opts.get('color_reads', True):
        _read_color.setValue(str(_nuke_color(color)))
    _pending_storage_knobs.append(_read_color)

    # Word-wrap choice (gizmo_options.word_wrap), persisted so on_create
    # knows whether to re-apply WORD_WRAP to the header / Extra Info knobs
    # (Nuke drops programmatic flags on .nk reload). Output preview always wraps.
    _word_wrap = nuke.String_Knob('_nfy_word_wrap', '')
    _word_wrap.setVisible(False)
    _word_wrap.setValue('1' if opts.get('word_wrap', True) else '0')
    _pending_storage_knobs.append(_word_wrap)


def _build_internal_graph(group, inputs_p, workflow_item):
    """Internal node graph: per input, the write-template chain (Input ->
    ... -> Write) recreated from the chosen template, plus the single
    Output node Nuke requires (connected to the first Input when present)."""
    group.begin()

    y_pos = 0
    first_inp = None
    missing_templates = []
    flag_dirty = False

    for i, ip in enumerate(inputs_p):
        x = i * 200
        inp_label = ip.get('label', ip.get('name', 'Input{}'.format(i + 1)))

        # Resolve the per-input template path. Honour the user's explicit
        # choice (write_template_source) when present; otherwise look up
        # workflow-local first, then global.
        tpl_name = ip.get('write_template', '')
        tpl_path = ''
        src = ''
        if tpl_name:
            src = (ip.get('write_template_source') or '').strip()
            wf_tpl = os.path.join(os.path.dirname(workflow_item.workflow_path),
                                  'write_templates', tpl_name)
            gl_tpl = os.path.join(_write_templates_dir(), tpl_name)
            if src == 'global':
                tpl_path = gl_tpl
            elif src == 'workflow':
                tpl_path = wf_tpl
            else:
                tpl_path = wf_tpl if os.path.isfile(wf_tpl) else gl_tpl

        inp_node, write, pasted = _paste_chain_template(tpl_path, inp_label)
        fell_back = write is None and bool(tpl_name)
        if inp_node is None or write is None:
            if tpl_name:
                missing_templates.append((inp_label, tpl_name, src))
            # Fallback: a plain Input -> Write pass-through. Deselect first so
            # createNode does not splice the new nodes into a stray selection.
            for _n in nuke.allNodes():
                _n.setSelected(False)
            inp_node = nuke.createNode('Input', inpanel=False)
            inp_node.setName(_safe_knob_name(inp_label))
            write = nuke.createNode('Write', inpanel=False)
            write.setName('Write_{}'.format(_safe_knob_name(inp_label)))
            write.setInput(0, inp_node)
            pasted = [inp_node, write]

        # Persist whether the configured template was applied, so the submit
        # pipeline can flag the fallback in the job Detail (otherwise the
        # substitution is silent). Clear any stale flag from a prior build
        # when the template now applies cleanly (e.g. the file was restored).
        if fell_back:
            if not ip.get('write_template_missing'):
                ip['write_template_missing'] = True
                flag_dirty = True
        elif ip.pop('write_template_missing', None):
            flag_dirty = True

        # Bind this Input to external pipe `i` explicitly. The cache writer
        # reads pipe `i` (gizmo_node.input(i)) and the matching Write_<label>,
        # so the Input feeding that Write must be pipe i. Default templates get
        # sequential numbers from paste order, but a user template whose Input
        # carries a non-default `number` would otherwise mis-map the pipes.
        _num = inp_node.knob('number')
        if _num is not None:
            try:
                _num.setValue(i)
            except Exception:
                pass

        _force_safe_write_knobs(write)
        _layout_chain(pasted, x, y_pos)
        if first_inp is None:
            first_inp = inp_node

    # Output node - required by Nuke for Group to be valid.
    # Deselect first: createNode splices the new node into the selected node's
    # output, which would otherwise insert this Output between the chain's
    # Input and the next node. Connect explicitly to the first Input below.
    for _n in nuke.allNodes():
        _n.setSelected(False)
    out_node = nuke.createNode('Output', inpanel=False)
    if first_inp is not None:
        out_node.setInput(0, first_inp)
        # Park the Output to the right of the input columns so it never
        # overlaps a chain.
        out_node.setXYpos(len(inputs_p) * 200, y_pos)
    else:
        out_node.setXYpos(0, y_pos)

    group.end()

    if missing_templates:
        _warn_missing_write_templates(missing_templates)

    return flag_dirty


def _finalize_gizmo(group, name):
    """Rename the native User tab to the workflow name and force the initial
    output-path preview (showPanel fires before knobChanged is set)."""
    # Rename the native `User` tab to the workflow tab name. setName
    # changes the python-side identifier, setLabel updates the tab
    # title shown in the panel.
    user_knob = group.knob('User')
    if user_knob:
        user_knob.setName(_safe_knob_name(name) + '_tab')
        user_knob.setLabel(_sanitize_label(name))

    # Force initial output preview update (showPanel fires during
    # createNode before knobChanged is set, so the preview is empty).
    from Nukomfy.gizmos.gizmo_callbacks import _update_output_preview
    try:
        _update_output_preview(group)
    except Exception:
        pass


def build_gizmo(workflow_item):
    """Create a Nuke Group gizmo from a WorkflowItem and return the Group node."""
    name   = workflow_item.workflow_alias or workflow_item.name
    color  = workflow_item.gizmo_color
    params = workflow_item.gizmo_params  # list of dicts with 'role' key
    opts   = workflow_item.gizmo_options

    (inputs_p, _knobs_p, outputs_p, all_params,
     input_knobs, model_knobs, output_knobs) = _classify_params(params)

    # --- Create Group node, then header / Extra Info ----------------------
    # Reuse Nuke's native `User` tab as the workflow tab: the first addKnob
    # (the header) creates it implicitly; it is renamed to the workflow
    # name at the end of build_gizmo, after all knobs are placed.
    group = _create_group(name, color, opts)
    _add_header_section(group, workflow_item)

    # Collected during _add_knobs so the knobChanged script knows which
    # knobs are seeds and must be validated.
    seed_knob_names = []
    # frame_padding is not a per-NukomfyWrite knob; it's read from Settings.
    # Filtered upstream via _AUTO_MANAGED_WIDGETS in the Workflow Creator.
    # Names of multiline knobs we own - the onCreate rebuild uses this list
    # to avoid touching Nuke's default Group `label` knob (also multiline).
    multiline_knob_names = []
    # Generic V3 masters built while not exposed: the knob is created
    # hidden (locked at its saved value) so it still drives sub
    # visibility + submit-strip. onCreate re-applies the HIDDEN flag
    # because Nuke does not reliably persist it through .nk save/load
    # (same class of issue as the SLIDER flag above).
    hidden_master_knob_names = []

    # Storage knobs (never user-visible): collected here, addKnob is deferred
    # to the end of build_gizmo - after the last visible button - so they
    # don't sit between the Output Parameters tab and the Submit / Read
    # buttons, where hidden knobs in the middle of the panel cause a visible
    # re-layout pass each time a TABBEGINCLOSEDGROUP is expanded. HIDDEN flag
    # (setVisible) rather than the permanent INVISIBLE flag: same visual
    # result, lighter Qt path.
    _pending_storage_knobs = []

    # V3 sub-input visibility map {master_kn: {sub_kn: [option_keys]}}.
    # Created empty here, then populated by the knob-adding helpers below as
    # they emit each V3 master / sub. Read at runtime by on_knob_changed and
    # on paste/load by on_create.
    v3_visibility_map = {}
    _widget_to_knob = _build_widget_to_knob(all_params)
    suite_grey_map = _build_suite_grey_map(all_params, _widget_to_knob)

    # Build all knobs through the _add_knobs pass; it fills _kctx
    # (v3_visibility_map + the seed / multiline / hidden-master name lists
    # + pending storage knobs) that the storage knobs and the onCreate
    # script consume afterwards.
    _kctx = _KnobCtx(group, _widget_to_knob, v3_visibility_map,
                     seed_knob_names, multiline_knob_names,
                     hidden_master_knob_names, _pending_storage_knobs)

    _add_input_section(_kctx, inputs_p, input_knobs)
    _add_model_section(_kctx, model_knobs)

    show_preview = workflow_item.gizmo_options.get('output_preview', True)
    _add_output_section(_kctx, outputs_p, output_knobs, show_preview)

    # Workflow alias in gizmo-char form (custom name if set, else the
    # workflow name). Run through sanitize_gizmo_chars so {workflow_alias}
    # matches the gizmo node's base name and the Alias field placeholder
    # (e.g. "Z-Image-Turbo" -> "Z_Image_Turbo") instead of the raw,
    # hyphen-keeping workflow name.
    workflow_alias = sanitize_gizmo_chars(name)
    _add_state_knobs(_kctx, workflow_alias, suite_grey_map)

    # When the path preview is the last element above the submit row (preview
    # on, no output-parameter groups), its small-font row already carries a
    # bottom gap; skip the submit row's own leading spacer so it is not doubled.
    tight_submit = show_preview and not output_knobs
    _add_submit_read_row(group, outputs_p, tight_submit)

    _add_metadata_knobs(_kctx, workflow_item, params, color, opts)

    # Flush all storage knobs at the end of the panel, after the last
    # user-visible button (Submit / Read Output(s) / Read mode). Keeps the
    # visible portion of the panel free of hidden knobs that would
    # otherwise sit in the Qt layout flow and trigger a perceptible
    # re-layout pass each time a TABBEGINCLOSEDGROUP is expanded.
    for _k in _pending_storage_knobs:
        group.addKnob(_k)

    if _build_internal_graph(group, inputs_p, workflow_item):
        # A write-template fallback (or its clearing) mutated the params;
        # re-serialise so submit-time extract_write_templates sees the flag.
        group.knob('_nfy_params').setValue(encode_payload(params))
    _finalize_gizmo(group, name)

    return group


# Non-ASCII codepoint ranges that render poorly as Nuke knob labels or
# tab titles (missing-glyph boxes, zero-width gaps, layout artifacts).
# Latin accented, CJK, Cyrillic, Greek, Arabic, Hebrew are PRESERVED -
# Qt/Nuke render these correctly. Only emoji/pictographs, zero-width
# chars, bidi overrides and C1 controls are stripped.
_STRIP_CP_RANGES = (
    (0x0080, 0x009F),   # C1 controls
    (0x200B, 0x200F),   # zero-width space/joiner/non-joiner, LRM/RLM
    (0x2028, 0x202E),   # line/para separators, bidi override controls
    (0x2060, 0x206F),   # word joiner, invisible operators, reserved
    (0x2600, 0x27BF),   # misc symbols + dingbats (✓ ☀ ♥ ☆ ▶ …)
    (0xFE00, 0xFE0F),   # variation selectors (often paired with emoji)
    (0xFEFF, 0xFEFF),   # byte-order mark
    (0x1F000, 0x1FFFF), # emoji + pictographs (emoticons, transport, etc.)
    (0xE0000, 0xE007F), # tag codepoints
)


def _sanitize_label(text):
    """Strip codepoints that render poorly in Nuke labels/titles.

    Safe to apply to any user-supplied string (workflow name, knob label,
    tab title, separator text). Preserves all legitimate scripts and
    accented chars; removes only emoji, pictographs, zero-width marks
    and C1 controls. Idempotent.
    """
    if not text:
        return text
    out = []
    for ch in str(text):
        cp = ord(ch)
        for lo, hi in _STRIP_CP_RANGES:
            if lo <= cp <= hi:
                break
        else:
            out.append(ch)
    return ''.join(out)


def _html_escape(text):
    return html.escape(_sanitize_label(text), quote=True)