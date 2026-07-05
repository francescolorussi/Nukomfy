"""Inputs / Outputs / Knobs tables for the Workflow Creator.

Three tables (Inputs / Outputs / Knobs) used by AddWorkflowDialog,
plus their shared helper widgets and the IO mode combo.

Internal module - public API surfaced via add_workflow.py.
"""

import logging

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.utils.suite_rules import (
    SUITE_VISIBILITY_RULES,
    suite_dependents,
    suite_group_widgets,
    is_v3_rebuild_trigger,
)

from Nukomfy.gui.ui_state import ui_state
from Nukomfy.gui._table_utils import _proportional_fit, _install_absorber
from Nukomfy.gui._theme import (
    TABLE_STYLE, apply_window_chrome, apply_nukomfy_palette, ERROR_COLOR)
from Nukomfy.gui._no_wheel import (
    NoWheelComboBox, NoWheelSpinBox, NoWheelDoubleSpinBox)
from Nukomfy.gui._fields import NukomfyLineEdit, NukomfyPlainTextEdit
from Nukomfy.workflows.workflow_converter import _SEED_MAX_INPUT

from Nukomfy.gui.add_workflow_parser import (
    _list_write_templates,
    _node_display_label,
    _node_cell_tooltip,
)
from Nukomfy.gui.workflow_state import (
    drop_empty_fixed_sections, make_v3_edit_key)

_log = logging.getLogger(__name__)

# Fixed widths (px) of the narrow Enable / Type columns, shared across the
# Inputs / Outputs / Knobs tables so sibling tables stay aligned.
_EN_COL_W = 26
_TY_COL_W = 70
# Default widths (px) of the Original Name / Gizmo Label columns, shared so
# the three tables start aligned (Interactive: user-resizable, then persisted).
_NM_COL_W = 170
_LB_COL_W = 180
# Extra width (px) added to a combo's text width for the dropdown arrow
# and cell margins.
_COMBO_TEXT_PADDING_PX = 28


def _ro_item(text, color='#888'):
    it = QtWidgets.QTableWidgetItem(text)
    it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
    it.setForeground(QtGui.QBrush(QtGui.QColor(color)))
    return it


def _ro_centered(text, color='#888'):
    it = _ro_item(text, color)
    it.setTextAlignment(QtCore.Qt.AlignCenter)
    return it


class _TooltipEditDialog(QtWidgets.QDialog):
    """Multi-line popup editor for the Tooltip column. Tooltip text in
    Nuke gizmos is often longer than a table cell can comfortably show
    inline - this dialog gives a comfortable QPlainTextEdit area."""

    def __init__(self, parent, current_text=''):
        super().__init__(parent)
        apply_window_chrome(self)
        self.setWindowTitle('Edit Tooltip')
        self.setModal(True)
        self.resize(440, 320)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12,
                                  12, 12)
        self._edit = NukomfyPlainTextEdit()
        self._edit.setPlainText(current_text or '')
        self._edit.setPlaceholderText(
            'Shown when hovering the knob in the gizmo Properties panel. '
            'Multi-line text is supported.')
        layout.addWidget(self._edit, 1)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save
            | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def text(self):
        return self._edit.toPlainText()


# Enable-column checkbox shared by all three tables (Inputs / Outputs /
# Knobs); tooltip set once here. Also on the wrapper: greyed-out rows
# (bypassed/muted) disable the checkbox, and a disabled widget shows no
# tooltip, so the enabled wrapper keeps it reachable.
_ENABLE_TOOLTIP = (
    "Show or hide this parameter on the gizmo.\n\n"
    "Visibility only: the value set here is\n"
    "always sent to ComfyUI at submit, even\n"
    "when the parameter is hidden.")


def _centered_checkbox(checked=False, enabled=True):
    cb = QtWidgets.QCheckBox()
    cb.setChecked(checked)
    cb.setEnabled(enabled)
    cb.setToolTip(_ENABLE_TOOLTIP)
    wrap = QtWidgets.QWidget()
    wl = QtWidgets.QHBoxLayout(wrap)
    wl.setContentsMargins(4, 0, 0, 0)
    wl.setAlignment(QtCore.Qt.AlignCenter)
    wl.addWidget(cb)
    wrap.setToolTip(_ENABLE_TOOLTIP)
    return wrap, cb


_DISABLED_FG = QtGui.QColor('#666')

# Accent colors for the structural-row fonts in the Knobs table, each
# tied to its row-family background (~3x bg luminance, same hue).
_GROUP_BRACKET_COLOR = '#5e7a66'    # group rows   (bg #1f2620, green)
_TEXT_ACCENT_COLOR = '#5e668a'      # text rows    (bg #1f2228, cool blue)
_DIVIDER_ACCENT_COLOR = '#5e7474'   # divider rows (bg #1f2424, teal-grey)
_NODE_STATE_TOOLTIP = {
    'muted': '\n(Node is muted in workflow. Runtime skips this node entirely. '
             'Downstream may fail or produce nothing.)',
    'bypassed': '\n(Node is bypassed in workflow. Inputs pass through to outputs '
                'unchanged, but this parameter has no effect.)',
    'disconnected': '\n(Node does not reach an output in the workflow, so it will '
                    'not run and the parameter has no effect. Preserved here in case '
                    'you reconnect it.)',
}
_NODE_STATE_PREFIX = {
    'muted': '(Muted) ',
    'bypassed': '(Bypassed) ',
    'disconnected': '(Disconnected) ',
}


def _read_enabled_with_intent(en_cb, p_data):
    """Compute (enabled, intent) for serialization in get_params.

    - BAD state: enabled forced to False, intent preserved from p_data.
    - GOOD state: enabled = intent = checkbox value.
    """
    saved_intent = p_data.get('_intent_enabled', True)
    if p_data.get('_node_state'):
        return False, saved_intent
    cb_value = en_cb.isChecked() if en_cb else True
    return cb_value, cb_value


def _apply_node_state_greying(table, row, state, en_wrap, grey_columns):
    """Visually disable the row for a bypassed/muted node.

    - Force the enabled checkbox unchecked + disabled (uncheckable).
    - Grey foreground on the columns listed in grey_columns.
    - Prepend '(Bypassed) ' / '(Muted) ' to the Node column text so the
      state is scannable without hovering for the tooltip.
    - Append a state-specific suffix to the Node column tooltip.
    """
    if not state:
        return
    if en_wrap is not None:
        en_cb = en_wrap.findChild(QtWidgets.QCheckBox)
        if en_cb is not None:
            en_cb.setChecked(False)
            en_cb.setEnabled(False)
    grey = QtGui.QBrush(_DISABLED_FG)
    nd_col = grey_columns.get('node')
    for col in grey_columns.values():
        it = table.item(row, col)
        if it is not None:
            it.setForeground(grey)
    if nd_col is not None:
        nd_it = table.item(row, nd_col)
        if nd_it is not None:
            prefix = _NODE_STATE_PREFIX.get(state, '')
            if prefix:
                nd_it.setText(prefix + (nd_it.text() or ''))
            suffix = _NODE_STATE_TOOLTIP.get(state, '')
            if suffix:
                existing = nd_it.toolTip() or ''
                nd_it.setToolTip(existing + suffix)




def _make_seed_validator():
    """Return a Qt validator that accepts only non-negative integer digits.

    Works on both PySide2 (QRegExp) and PySide6 (QRegularExpression).
    """
    try:
        rx = QtCore.QRegularExpression(r'^\d*$')
        return QtGui.QRegularExpressionValidator(rx)
    except AttributeError:
        pass
    try:
        rx = QtCore.QRegExp(r'^\d*$')
        return QtGui.QRegExpValidator(rx)
    except AttributeError:
        return None


def _decimals_for_step(step, fallback=3, hi=4):
    """Decimal places implied by a numeric `step` (0.01 -> 2, 0.001 -> 3,
    0.1 -> 1, 1 -> 0). ComfyUI declares `step` per numeric widget; deriving
    the spinbox precision from it shows exactly what the node intends instead
    of a fixed 4 decimals that exposes meaningless sub-step digits (e.g.
    0.7534 for a 0.01-step JPEG quality). Falls back when step is missing or
    invalid; clamped to [0, hi]."""
    if step is None:
        return fallback
    try:
        s = float(step)
    except (TypeError, ValueError):
        return fallback
    if s <= 0:
        return fallback
    text = ('%.10f' % s).rstrip('0').rstrip('.')
    return min(hi, len(text.split('.')[1]) if '.' in text else 0)


def _make_default_widget(param):
    """Create the appropriate widget for editing a default value.

    Returns (widget, get_value_fn, set_value_fn).
    """
    ptype = (param.get('type') or '').upper()
    default = param.get('default_value')

    if ptype == 'INT':
        # None (unset) and 0 (a legitimate declared max) must stay
        # distinct: only None falls back to the wide-open range.
        raw_max = param.get('max_value')
        try:
            max_v = int(raw_max) if raw_max is not None else None
        except (TypeError, ValueError):
            max_v = None

        # QSpinBox is C-int32: for huge ranges (uint64 seeds) use QLineEdit.
        if max_v is not None and max_v >= 2**32:
            w = NukomfyLineEdit()
            validator = _make_seed_validator()
            if validator is not None:
                w.setValidator(validator)

            def _clamp(v):
                try:
                    return max(0, min(int(v), _SEED_MAX_INPUT))
                except (ValueError, TypeError):
                    return 0

            if default is not None:
                w.setText(str(_clamp(default)))

            return (
                w,
                lambda: _clamp(w.text()),
                lambda v: w.setText(str(_clamp(v)) if v is not None else '0'),
            )

        w = NoWheelSpinBox()
        w.setRange(int(param.get('min_value') or 0),
                   min(max_v, int(1e9)) if max_v is not None else int(1e9))
        if default is not None:
            try:
                w.setValue(int(default))
            except (ValueError, TypeError):
                pass
        return w, lambda: w.value(), lambda v: w.setValue(int(v) if v is not None else 0)

    if ptype == 'FLOAT':
        step = param.get('_step')
        raw_max = param.get('max_value')
        try:
            fmax = float(raw_max) if raw_max is not None else 1e9
        except (TypeError, ValueError):
            fmax = 1e9
        w = NoWheelDoubleSpinBox()
        w.setDecimals(_decimals_for_step(step))
        w.setRange(float(param.get('min_value') or 0), fmax)
        if step:
            try:
                w.setSingleStep(float(step))
            except (TypeError, ValueError):
                pass
        if default is not None:
            try:
                w.setValue(float(default))
            except (ValueError, TypeError):
                pass
        return w, lambda: w.value(), lambda v: w.setValue(float(v) if v is not None else 0)

    if ptype == 'BOOLEAN':
        w = QtWidgets.QCheckBox()
        if default is not None:
            try:
                w.setChecked(bool(default))
            except (ValueError, TypeError):
                pass
        return w, lambda: w.isChecked(), lambda v: w.setChecked(bool(v) if v is not None else False)

    if ptype == 'COMBO':
        w = NoWheelComboBox()
        values = param.get('combo_values') or []
        if values:
            w.addItems([str(v) for v in values])
        if default is not None:
            idx = w.findText(str(default))
            if idx >= 0:
                w.setCurrentIndex(idx)
            elif not values:
                w.addItem(str(default))
        return w, lambda: w.currentText(), lambda v: w.setCurrentText(str(v) if v is not None else '')

    # STRING / fallback
    w = NukomfyLineEdit()
    if default is not None:
        w.setText(str(default))
    return w, lambda: w.text(), lambda v: w.setText(str(v) if v is not None else '')


# ---------------------------------------------------------------------------
# I/O Mode combo helpers (shared by _InputsTable and _OutputsTable)
# ---------------------------------------------------------------------------
_IOMODE_TOOLTIP_INPUT = (
    "Single - the workflow expects to receive exactly 1\n"
    "frame on this input. The current Nuke frame is sent\n"
    "at submit time.\n"
    "\n"
    "Sequence - the workflow expects to receive 1 or more\n"
    "frames on this input. A First/Last picker appears in\n"
    "the Submit panel; a 1-frame sequence (start=end) is\n"
    "allowed.\n"
    "\n"
    "Tip: when in doubt, choose Sequence. It covers both\n"
    "cases.\n"
    "\n"
    "Required - choose Single or Sequence for each\n"
    "enabled input.")

_IOMODE_TOOLTIP_OUTPUT = (
    "Single - the workflow expects to produce exactly 1\n"
    "frame on this output. Enables Batch Count to render\n"
    "multiple frames in one go.\n"
    "\n"
    "Sequence - the workflow expects to produce 1 or more\n"
    "frames on this output (the workflow itself decides\n"
    "how many are generated). Disables Batch Count.\n"
    "\n"
    "Tip: choose Single if the workflow produces one\n"
    "frame at a time; choose Sequence if it produces a\n"
    "sequence on its own.\n"
    "\n"
    "Required - choose Single or Sequence for each\n"
    "enabled output.")

_WRITE_TEMPLATE_TOOLTIP = (
    "Chain that caches this input's frames to disk before ComfyUI reads "
    "them: an Input feeding a Write, optionally with nodes in between "
    "(LUT, grade, OCIOFileTransform, …).\n"
    "The Write sets the cache format and settings (e.g. EXR, compression).\n"
    "\n"
    "Required - pick a template for each enabled input.")


class _InvalidBorderCombo(QtWidgets.QComboBox):
    """QComboBox that paints a red border on itself when ``invalid`` is set.

    Uses no stylesheet (the border is painted); the Nuke palette is pinned so a
    host mutating the global palette can't recolor it. Shared by the I/O Mode
    and Write Template combos.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        apply_nukomfy_palette(self)
        self._invalid = False

    def setInvalid(self, invalid):
        if self._invalid != bool(invalid):
            self._invalid = bool(invalid)
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._invalid:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        pen = QtGui.QPen(QtGui.QColor(ERROR_COLOR))
        pen.setWidth(1)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.drawRect(r)
        p.end()


class _IOModeCombo(_InvalidBorderCombo):
    pass


# Item-data roles for the Write Template combo. A real template row carries
# its (filename, source) tuple; an action row ("Add from File…" / "Clear")
# carries a verb the table turns into a workflow-global action. The empty
# state is Qt's native placeholder (currentIndex == -1), not a list item, so
# once a valid template is picked there is nothing empty to fall back to.
_TPL_DATA_ROLE = QtCore.Qt.UserRole
_TPL_ACTION_ROLE = QtCore.Qt.UserRole + 1


class _TemplateCombo(_InvalidBorderCombo):
    """Per-input Write Template picker.

    The list holds the real templates, then (when any exist) a separator and
    a single "Manage Workflow Templates…" action. That action row is a button,
    not a value: it is intercepted on ``activated``, the previous selection is
    restored, and the table is asked to open the manage dialog. The unselected
    state uses Qt's native placeholder (index -1) so it disappears as soon as a
    valid template is chosen and reappears only on fallback (template gone).
    """
    manageRequested = QtCore.Signal()
    valueChanged = QtCore.Signal()  # user picked a real template (not an action)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._committed_index = -1
        self.activated.connect(self._on_activated)

    def _on_activated(self, idx):
        if self.itemData(idx, _TPL_ACTION_ROLE) == 'manage':
            # Bounce the selection back to whatever was committed before the
            # click so the action label never becomes the combo's value.
            self.blockSignals(True)
            self.setCurrentIndex(self._committed_index)
            self.blockSignals(False)
            # The table defers + opens the modal; emit straight away (the
            # deferral must live on a stable object, not this per-row combo
            # which a refresh destroys).
            self.manageRequested.emit()
        else:
            self._committed_index = idx
            self.setInvalid(False)
            self.valueChanged.emit()

    def commit_index(self, idx):
        """Select programmatically and remember it as the committed value."""
        self._committed_index = idx
        self.blockSignals(True)
        self.setCurrentIndex(idx)
        self.blockSignals(False)


def _make_iomode_combo(selected='', tooltip=_IOMODE_TOOLTIP_INPUT):
    cb = _IOModeCombo()
    # Native placeholder for the unselected state (mirrors the Write Template
    # combo): no empty list item, so once a mode is picked there is nothing
    # empty to go back to. Older Qt without setPlaceholderText keeps the
    # leading empty item.
    has_placeholder = hasattr(cb, 'setPlaceholderText')
    if has_placeholder:
        cb.setPlaceholderText('Select…')
        cb.addItems(['Single', 'Sequence'])
    else:
        cb.addItems(['', 'Single', 'Sequence'])
    cb.setToolTip(tooltip)
    if selected in ('Single', 'Sequence'):
        cb.setCurrentText(selected)
    else:
        cb.setCurrentIndex(-1 if has_placeholder else 0)
    def _on_change(_idx, _cb=cb):
        if _cb.currentText():
            _cb.setInvalid(False)
    cb.currentIndexChanged.connect(_on_change)
    return cb


def _read_iomode_combo(widget):
    if isinstance(widget, QtWidgets.QComboBox):
        if widget.currentIndex() >= 0:
            return widget.currentText()
    return ''


def _style_iomode_combo(widget, invalid):
    if isinstance(widget, _IOModeCombo):
        widget.setInvalid(invalid)


def _iomode_column_width(widget):
    """Auto-width for the I/O Mode column based on header + content text."""
    fm = widget.fontMetrics()
    candidates = ('I/O Mode', 'Single', 'Sequence')
    text_w = max(fm.horizontalAdvance(t) for t in candidates)
    return text_w + _COMBO_TEXT_PADDING_PX


def _ensure_unique_label(table, label_col, changed_row):
    """Auto-rename `label` at `changed_row` if it collides with another row
    after sanitization.

    Two labels that look distinct but collapse to the same sanitized token
    (e.g. "My Input" / "My-Input") would drive the gizmo's Write node and
    input-cache folder to the same name downstream. Compare on the knob
    sanitizer (the stricter of the two, so its uniqueness implies the folder
    sanitizer's) and append `_1`, `_2`, ... to the typed text until the token
    is free. No-op if empty or already unique. Blocks table signals during the
    rewrite so itemChanged doesn't recurse.
    """
    from Nukomfy.gizmos.gizmo_builder import _safe_knob_name
    item = table.item(changed_row, label_col)
    if item is None:
        return
    raw = item.text().strip()
    if not raw:
        return
    taken = set()
    for r in range(table.rowCount()):
        if r == changed_row:
            continue
        it = table.item(r, label_col)
        if it:
            t = it.text().strip()
            if t:
                taken.add(_safe_knob_name(t))
    if _safe_knob_name(raw) not in taken:
        return
    n = 1
    while _safe_knob_name('{}_{}'.format(raw, n)) in taken:
        n += 1
    new_label = '{}_{}'.format(raw, n)
    table.blockSignals(True)
    try:
        item.setText(new_label)
    finally:
        table.blockSignals(False)


# ---------------------------------------------------------------------------
# Reorder wrapper - adds up/down arrows above a table that exposes
# `get_params()` + `load()` so rows can be reordered by the user.
# ---------------------------------------------------------------------------
class _TableReorderFrame(QtWidgets.QWidget):
    """Thin wrapper exposing `_move_up`/`_move_down` for the unified toolbar
    (the actual buttons live on `WorkflowDialog.params_tabs` corner widget)."""
    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.table = table
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.table)

    def _swap(self, a, b):
        params = self.table.get_params()
        if 0 <= a < len(params) and 0 <= b < len(params):
            params[a], params[b] = params[b], params[a]
            self.table.load(params)
            return True
        return False

    def _move_up(self):
        r = self.table.currentRow()
        if r <= 0:
            return
        if self._swap(r, r - 1):
            self.table.selectRow(r - 1)

    def _move_down(self):
        r = self.table.currentRow()
        if r < 0 or r >= self.table.rowCount() - 1:
            return
        if self._swap(r, r + 1):
            self.table.selectRow(r + 1)

    def can_move_up(self):
        return self.table.currentRow() > 0

    def can_move_down(self):
        r = self.table.currentRow()
        return 0 <= r < self.table.rowCount() - 1


# ---------------------------------------------------------------------------
# Inputs Table  (Enable | Type | Node | Original Name | Gizmo Label | Write Template | I/O Mode)
# ---------------------------------------------------------------------------
class _InputsTable(QtWidgets.QTableWidget):
    EN, TY, ND, NM, LB, WT, IM = 0, 1, 2, 3, 4, 5, 6

    # Re-emitted from the per-row template combos so the dialog can open the
    # manage-templates dialog (the combos can't reach the dialog directly).
    templateManageRequested = QtCore.Signal()
    # Fired when the user picks a template / I/O Mode value, so the dialog can
    # live-refresh the red tab dot as save-blocking errors get resolved.
    validityMaybeChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(0, 7, parent)
        self.setHorizontalHeaderLabels(
            ['', 'Type', 'Node', 'Original Name', 'Gizmo Label',
             'Write Template', 'I/O Mode'])
        h = self.horizontalHeader()
        h.setStretchLastSection(False)
        # Let the 26px Enable column hold its width (Qt clamps to ~30 default).
        h.setMinimumSectionSize(2)
        h.setSectionResizeMode(self.EN, QtWidgets.QHeaderView.Fixed)
        h.setSectionResizeMode(self.TY, QtWidgets.QHeaderView.Fixed)
        h.setSectionResizeMode(self.ND, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.NM, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.LB, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.WT, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.IM, QtWidgets.QHeaderView.Fixed)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setColumnWidth(self.EN, _EN_COL_W)
        self.setColumnWidth(self.TY, _TY_COL_W)
        self.setColumnWidth(self.ND, 120)
        self.setColumnWidth(self.NM, _NM_COL_W)
        self.setColumnWidth(self.LB, _LB_COL_W)
        self.setColumnWidth(self.WT, 120)
        self.setColumnWidth(self.IM, _iomode_column_width(self))
        _install_absorber(self, None)
        # Restore saved column widths
        h.blockSignals(True)
        ui_state.restore_column_widths('ep_inputs_table', self)
        self.setColumnWidth(self.EN, _EN_COL_W)
        self.setColumnWidth(self.TY, _TY_COL_W)
        self.setColumnWidth(self.IM, _iomode_column_width(self))
        h.blockSignals(False)
        _proportional_fit(self)
        _timer = QtCore.QTimer(self)
        _timer.setSingleShot(True)
        _timer.setInterval(500)
        _timer.timeout.connect(
            lambda: ui_state.save_column_widths('ep_inputs_table', self))
        h.sectionResized.connect(lambda *_: _timer.start())
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(TABLE_STYLE)
        self._templates = _list_write_templates()  # global only initially
        self._workflow_dir = None
        # Staged Add-mode templates: (display, filename, 'workflow') tuples not
        # yet on disk. Merged into the combo so they're selectable before Save;
        # written to the workflow folder at Save by the dialog.
        self._pending_templates = []
        self.itemChanged.connect(self._on_label_item_changed)

    def _on_label_item_changed(self, item):
        if item is None:
            return
        # Auto-sync tooltip = text on the editable Label column so a
        # truncated cell reveals its full value on hover.
        if item.column() == self.LB:
            item.setToolTip(item.text() or '')
            _ensure_unique_label(self, self.LB, item.row())

    def _combined_templates(self):
        """On-disk templates plus any staged Add-mode templates (deduped)."""
        disk = _list_write_templates(self._workflow_dir)
        seen = {(f, s) for _d, f, s in disk}
        return disk + [t for t in self._pending_templates
                       if (t[1], t[2]) not in seen]

    def set_workflow_dir(self, wf_dir):
        """Set the workflow directory to also scan for local templates."""
        self._workflow_dir = wf_dir
        self._templates = self._combined_templates()

    def set_pending_templates(self, tuples):
        """Replace the staged Add-mode template list (combo merge source)."""
        self._pending_templates = list(tuples)
        self._templates = self._combined_templates()

    def _emit_template_manage(self):
        # Defer so the combo popup finishes closing before the modal opens.
        # Deferring on the table (a stable object) is safe even though the
        # combo that requested it is destroyed by the post-action refresh.
        QtCore.QTimer.singleShot(0, self.templateManageRequested.emit)

    def refresh_templates(self, dropped=None):
        """Re-scan templates from disk and rebuild the rows, preserving each
        row's current pick. `dropped` is an optional set of filenames whose
        'workflow' source was just removed by a Clear: those rows reset to the
        placeholder instead of falling back to a same-named global, so the user
        re-picks consciously after an explicit clear (no red until they Save).
        Called after an add/clear action."""
        params = self.get_params()
        if dropped:
            for p in params:
                if (p.get('write_template_source') == 'workflow'
                        and p.get('write_template') in dropped):
                    p['write_template'] = ''
                    p['write_template_source'] = ''
        self._templates = self._combined_templates()
        self.load(params)

    def _make_template_combo(self, selected='', selected_source=''):
        cb = _TemplateCombo()
        # Native placeholder for the unselected state: no empty list item, so
        # once a valid template is chosen there is nothing empty to fall back
        # to (the picker is Required). Older Qt without setPlaceholderText
        # keeps a leading empty item instead.
        has_placeholder = hasattr(cb, 'setPlaceholderText')
        if has_placeholder:
            cb.setPlaceholderText('Select…')
        else:
            cb.addItem('')
        base = 0 if has_placeholder else 1  # index of the first template row
        # Real templates first; remember the index matching the saved choice.
        # Prefer an exact (filename, source) match so the user's pick between
        # same-named workflow/global templates is preserved; fall back to
        # filename only if the original source is gone.
        exact_idx = -1
        fname_idx = -1
        for i, (display, fname, src) in enumerate(self._templates):
            cb.addItem(display)
            cb.setItemData(base + i, (fname, src), _TPL_DATA_ROLE)
            if selected and fname == selected:
                if fname_idx < 0:
                    fname_idx = base + i
                if selected_source and src == selected_source:
                    exact_idx = base + i
        # Single action row, separated from the real templates: it opens the
        # manage dialog (add/remove this workflow's templates).
        if self._templates:
            cb.insertSeparator(cb.count())
        manage_idx = cb.count()
        cb.addItem('Manage Workflow Templates…')
        cb.setItemData(manage_idx, 'manage', _TPL_ACTION_ROLE)
        # Muted text so it reads as a command, not a value. The row keeps its
        # selection square so every row stays aligned (no custom delegate).
        cb.setItemData(manage_idx, QtGui.QColor('#888'), QtCore.Qt.ForegroundRole)
        cb.setToolTip(_WRITE_TEMPLATE_TOOLTIP)
        if exact_idx >= 0:
            cb.commit_index(exact_idx)
        elif fname_idx >= 0:
            cb.commit_index(fname_idx)
        else:
            cb.commit_index(-1 if has_placeholder else 0)
        cb.manageRequested.connect(self._emit_template_manage)
        return cb

    def _read_template(self, row):
        """Return (filename, source) for the selected template, or ('', '')
        when nothing is chosen (placeholder / empty item / an action row)."""
        cb = self.cellWidget(row, self.WT)
        if isinstance(cb, QtWidgets.QComboBox):
            data = cb.itemData(cb.currentIndex(), _TPL_DATA_ROLE)
            if isinstance(data, (tuple, list)) and len(data) == 2:
                return data[0], data[1]
        return '', ''

    def load(self, params):
        self.setRowCount(0)
        self.blockSignals(True)
        try:
            for p in params:
                r = self.rowCount()
                self.insertRow(r)
                en_wrap, _ = _centered_checkbox(
                    checked=bool(p.get('enabled', True)))
                self.setCellWidget(r, self.EN, en_wrap)
                self.setItem(r, self.TY, _ro_centered(p.get('type', '')))
                nd_item = _ro_item(_node_display_label(
                    p.get('node_type', ''), p.get('node_title', ''),
                    p.get('display_name', '')))
                tip = _node_cell_tooltip(p)
                if tip:
                    nd_item.setToolTip(tip)
                self.setItem(r, self.ND, nd_item)
                orig_name = p.get('name', '')
                orig = _ro_item(orig_name, '#aaa')
                orig.setData(QtCore.Qt.UserRole, p)
                orig.setToolTip(orig_name)
                self.setItem(r, self.NM, orig)
                lb_text = p.get('label', p.get('name', ''))
                lb_item = QtWidgets.QTableWidgetItem(lb_text)
                lb_item.setToolTip(lb_text)
                self.setItem(r, self.LB, lb_item)
                tpl_combo = self._make_template_combo(
                    p.get('write_template', ''),
                    p.get('write_template_source', ''))
                tpl_combo.valueChanged.connect(self.validityMaybeChanged)
                self.setCellWidget(r, self.WT, tpl_combo)
                io_combo = _make_iomode_combo(p.get('io_mode', ''),
                                              tooltip=_IOMODE_TOOLTIP_INPUT)
                io_combo.activated.connect(
                    lambda *_: self.validityMaybeChanged.emit())
                self.setCellWidget(r, self.IM, io_combo)
                _apply_node_state_greying(
                    self, r, p.get('_node_state'), en_wrap,
                    {'node': self.ND, 'name': self.NM})
        finally:
            self.blockSignals(False)

    def get_params(self):
        result = []
        for r in range(self.rowCount()):
            en_w = self.cellWidget(r, self.EN)
            en_cb = en_w.findChild(QtWidgets.QCheckBox) if en_w else None
            p = self.item(r, self.NM).data(QtCore.Qt.UserRole) or {}
            enabled, intent = _read_enabled_with_intent(en_cb, p)
            tpl_file, tpl_src = self._read_template(r)
            io_mode = _read_iomode_combo(self.cellWidget(r, self.IM))
            entry = {
                'name':           p.get('name', ''),
                'label':          self.item(r, self.LB).text().strip() or p.get('name', ''),
                'type':           p.get('type', ''),
                'role':           'input',
                'write_template': tpl_file,
                'write_template_source': tpl_src,
                'enabled':        enabled,
                '_intent_enabled': intent,
                'target_node_id': p.get('target_node_id'),
                'node_type':      p.get('node_type', ''),
                'node_title':     p.get('node_title', ''),
                'display_name':   p.get('display_name', ''),
                'widget_name':    p.get('widget_name', p.get('name', '')),
                'default_value':  p.get('default_value'),
                'io_mode':        io_mode,
            }
            # Persist _node_state snapshot for visualization on reopen
            if p.get('_node_state'):
                entry['_node_state'] = p['_node_state']
            result.append(entry)
        return result

    def mark_invalid_iomode(self, red_rows):
        for r in range(self.rowCount()):
            _style_iomode_combo(self.cellWidget(r, self.IM),
                                invalid=(r in red_rows))

    def mark_invalid_template(self, red_rows):
        for r in range(self.rowCount()):
            cb = self.cellWidget(r, self.WT)
            if isinstance(cb, _InvalidBorderCombo):
                cb.setInvalid(r in red_rows)


class _OutputsTable(QtWidgets.QTableWidget):
    EN, TY, ND, NM, LB, IM = 0, 1, 2, 3, 4, 5

    # Fired when the user picks an I/O Mode value, so the dialog can
    # live-refresh the red tab dot as save-blocking errors get resolved.
    validityMaybeChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(0, 6, parent)
        self.setHorizontalHeaderLabels(
            ['', 'Type', 'Node', 'Original Name', 'Gizmo Label', 'I/O Mode'])
        h = self.horizontalHeader()
        h.setStretchLastSection(False)
        # Let the 26px Enable column hold its width (Qt clamps to ~30 default).
        h.setMinimumSectionSize(2)
        h.setSectionResizeMode(self.EN, QtWidgets.QHeaderView.Fixed)
        h.setSectionResizeMode(self.TY, QtWidgets.QHeaderView.Fixed)
        h.setSectionResizeMode(self.ND, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.NM, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.LB, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.IM, QtWidgets.QHeaderView.Fixed)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setColumnWidth(self.EN, _EN_COL_W)
        self.setColumnWidth(self.TY, _TY_COL_W)
        self.setColumnWidth(self.ND, 120)
        self.setColumnWidth(self.NM, _NM_COL_W)
        self.setColumnWidth(self.LB, _LB_COL_W)
        self.setColumnWidth(self.IM, _iomode_column_width(self))
        _install_absorber(self, None)
        # Restore saved column widths
        h.blockSignals(True)
        ui_state.restore_column_widths('ep_outputs_table', self)
        self.setColumnWidth(self.EN, _EN_COL_W)
        self.setColumnWidth(self.TY, _TY_COL_W)
        self.setColumnWidth(self.IM, _iomode_column_width(self))
        h.blockSignals(False)
        _proportional_fit(self)
        _timer = QtCore.QTimer(self)
        _timer.setSingleShot(True)
        _timer.setInterval(500)
        _timer.timeout.connect(
            lambda: ui_state.save_column_widths('ep_outputs_table', self))
        h.sectionResized.connect(lambda *_: _timer.start())
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(TABLE_STYLE)
        self.itemChanged.connect(self._on_label_item_changed)

    def _on_label_item_changed(self, item):
        if item is None:
            return
        if item.column() == self.LB:
            item.setToolTip(item.text() or '')
            _ensure_unique_label(self, self.LB, item.row())

    def load(self, params):
        self.blockSignals(True)
        try:
            self.setRowCount(0)
            for p in params:
                r = self.rowCount()
                self.insertRow(r)
                en_wrap, _ = _centered_checkbox(
                    checked=bool(p.get('enabled', True)))
                self.setCellWidget(r, self.EN, en_wrap)
                self.setItem(r, self.TY, _ro_centered(p.get('type', '')))
                nd_item = _ro_item(_node_display_label(
                    p.get('node_type', ''), p.get('node_title', ''),
                    p.get('display_name', '')))
                tip = _node_cell_tooltip(p)
                if tip:
                    nd_item.setToolTip(tip)
                self.setItem(r, self.ND, nd_item)
                orig_name = p.get('name', '')
                orig = _ro_item(orig_name, '#aaa')
                orig.setData(QtCore.Qt.UserRole, p)
                orig.setToolTip(orig_name)
                self.setItem(r, self.NM, orig)
                lb_text = p.get('label', p.get('name', ''))
                lb_item = QtWidgets.QTableWidgetItem(lb_text)
                lb_item.setToolTip(lb_text)
                self.setItem(r, self.LB, lb_item)
                io_combo = _make_iomode_combo(p.get('io_mode', ''),
                                              tooltip=_IOMODE_TOOLTIP_OUTPUT)
                io_combo.activated.connect(
                    lambda *_: self.validityMaybeChanged.emit())
                self.setCellWidget(r, self.IM, io_combo)
                _apply_node_state_greying(
                    self, r, p.get('_node_state'), en_wrap,
                    {'node': self.ND, 'name': self.NM})
        finally:
            self.blockSignals(False)

    def get_params(self):
        result = []
        for r in range(self.rowCount()):
            en_w = self.cellWidget(r, self.EN)
            en_cb = en_w.findChild(QtWidgets.QCheckBox) if en_w else None
            p = self.item(r, self.NM).data(QtCore.Qt.UserRole) or {}
            enabled, intent = _read_enabled_with_intent(en_cb, p)
            io_mode = _read_iomode_combo(self.cellWidget(r, self.IM))
            entry = {
                'name':           p.get('name', ''),
                'label':          self.item(r, self.LB).text().strip() or p.get('name', ''),
                'type':           p.get('type', ''),
                'role':           'output',
                'enabled':        enabled,
                '_intent_enabled': intent,
                'target_node_id': p.get('target_node_id'),
                'node_type':      p.get('node_type', ''),
                'node_title':     p.get('node_title', ''),
                'display_name':   p.get('display_name', ''),
                'widget_name':    p.get('widget_name', p.get('name', '')),
                'default_value':  p.get('default_value'),
                'is_output':      True,
                'io_mode':        io_mode,
            }
            # Persist _node_state snapshot for visualization on reopen
            if p.get('_node_state'):
                entry['_node_state'] = p['_node_state']
            result.append(entry)
        return result

    def mark_invalid_iomode(self, red_rows):
        for r in range(self.rowCount()):
            _style_iomode_combo(self.cellWidget(r, self.IM),
                                invalid=(r in red_rows))


class _GroupBracketDelegate(QtWidgets.QStyledItemDelegate):
    """Paint vertical "[" brackets on the EN column connecting each
    Group Begin row to its End. Nested groups stack with growing X offsets.

    The owning `_KnobsTable` provides the bracket list via `_get_brackets()`
    so paint stays a flat read against a cached list.
    """

    _BAR_W   = 1  # vertical line thickness (px)
    _BASE_X  = 2  # left padding inside the EN cell (px)
    _STEP_X  = 2  # additional X offset per nesting depth (px)
    _CAP_LEN = 5  # horizontal cap length on Begin/End rows (px)

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        # Only paint brackets on the EN column (col 0).
        if index.column() != 0:
            return
        try:
            brackets = self._owner._get_brackets()
        except Exception:
            return
        if not brackets:
            return
        row = index.row()
        rect = option.rect
        # Adaptive step - at 4+ nesting levels the natural 3px step would
        # push the deepest line into the centered checkbox area. Compress
        # the offset progressively so all brackets fit within the safe
        # left margin (~quarter of the EN cell width).
        max_depth = max(d for (_, _, d) in brackets)
        safe_w = max(self._BASE_X + self._STEP_X, rect.width() // 4)
        natural = self._BASE_X + max_depth * self._STEP_X + self._BAR_W
        if max_depth > 0 and natural > safe_w:
            step = max(1, (safe_w - self._BASE_X - self._BAR_W) // max_depth)
        else:
            step = self._STEP_X
        painter.save()
        try:
            pen = QtGui.QPen(QtGui.QColor(_GROUP_BRACKET_COLOR))
            pen.setWidth(self._BAR_W)
            pen.setCapStyle(QtCore.Qt.FlatCap)
            painter.setPen(pen)
            for begin_row, end_row, depth in brackets:
                if row < begin_row or row > end_row:
                    continue
                x = rect.left() + self._BASE_X + depth * step
                mid_y = rect.top() + rect.height() // 2
                if begin_row == end_row:
                    # Degenerate (shouldn't happen, but stay safe): just a
                    # short cap.
                    painter.drawLine(x, mid_y, x + self._CAP_LEN, mid_y)
                    continue
                if row == begin_row:
                    # Vertical from middle to bottom + horizontal cap right.
                    painter.drawLine(x, mid_y, x, rect.bottom())
                    painter.drawLine(x, mid_y, x + self._CAP_LEN, mid_y)
                elif row == end_row:
                    # Vertical from top to middle + horizontal cap right.
                    painter.drawLine(x, rect.top(), x, mid_y)
                    painter.drawLine(x, mid_y, x + self._CAP_LEN, mid_y)
                else:
                    # Middle row: vertical through the whole height.
                    painter.drawLine(x, rect.top(), x, rect.bottom())
        finally:
            painter.restore()


class _NonSelectablePressGuard(QtCore.QObject):
    """Viewport press filter: consume a left click (single or double) that
    lands on a non-selectable row (a fixed section header) so the table never
    selects it.

    `SelectRows` highlights the whole row from any selectable cell and does not
    reliably honour a per-cell `~ItemIsSelectable`, so clearing the flag alone
    leaves the header clickable; consuming the press is deterministic. A
    double-click arrives as a separate MouseButtonDblClick (not a second
    press), so both are intercepted. The app-level focus filter runs first and
    has already dropped any prior selection by the time this fires.
    """

    def __init__(self, table):
        super().__init__(table)
        self._table = table

    def eventFilter(self, obj, e):
        if (e.type() in (QtCore.QEvent.MouseButtonPress,
                         QtCore.QEvent.MouseButtonDblClick)
                and e.button() == QtCore.Qt.LeftButton):
            pos = (e.position().toPoint() if hasattr(e, 'position')
                   else e.pos())
            idx = self._table.indexAt(pos)
            if idx.isValid() and not bool(
                    idx.flags() & QtCore.Qt.ItemIsSelectable):
                return True
        return False


class _KnobsTable(QtWidgets.QWidget):
    """Wraps a QTableWidget with move-up/down and add-separator controls."""
    EN, TY, ND, NM, LB, DV, TT = 0, 1, 2, 3, 4, 5, 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._output_node_ids = set()
        self._input_node_ids = set()
        # Snapshot dict (server-authoritative widget definitions) and
        # the flat per-option edits map. Installed by
        # install_snapshot_state(). The cascade reads option subs
        # from self._snapshot and persists user edits on non-active
        # options into self._v3_user_edits.
        self._snapshot = None
        self._v3_user_edits = {}
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Toolbar (up/down + add/remove separator) lives on the parent
        # QTabWidget as a corner widget - see WorkflowDialog build.

        # Table
        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ['', 'Type', 'Node', 'Original Name', 'Gizmo Label',
             'Default Value', 'Tooltip'])
        h = self.table.horizontalHeader()
        h.setStretchLastSection(False)
        # Let the 26px Enable column hold its width (Qt clamps to ~30 default).
        h.setMinimumSectionSize(2)
        h.setSectionResizeMode(self.EN, QtWidgets.QHeaderView.Fixed)
        h.setSectionResizeMode(self.TY, QtWidgets.QHeaderView.Fixed)
        h.setSectionResizeMode(self.ND, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.NM, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.LB, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.DV, QtWidgets.QHeaderView.Interactive)
        h.setSectionResizeMode(self.TT, QtWidgets.QHeaderView.Interactive)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.table.setColumnWidth(self.EN, _EN_COL_W)
        self.table.setColumnWidth(self.TY, _TY_COL_W)
        self.table.setColumnWidth(self.ND, 120)
        self.table.setColumnWidth(self.NM, _NM_COL_W)
        self.table.setColumnWidth(self.LB, _LB_COL_W)
        self.table.setColumnWidth(self.DV, 170)
        self.table.setColumnWidth(self.TT, 170)
        _install_absorber(self.table, None)
        # Restore saved column widths
        h.blockSignals(True)
        ui_state.restore_column_widths('ep_knobs_table', self.table)
        self.table.setColumnWidth(self.EN, _EN_COL_W)
        self.table.setColumnWidth(self.TY, _TY_COL_W)
        h.blockSignals(False)
        _proportional_fit(self.table)
        _timer = QtCore.QTimer(self.table)
        _timer.setSingleShot(True)
        _timer.setInterval(500)
        _timer.timeout.connect(
            lambda: ui_state.save_column_widths('ep_knobs_table', self.table))
        h.sectionResized.connect(lambda *_: _timer.start())
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        # Fixed section headers carry ~ItemIsSelectable, but SelectRows can
        # still highlight them; consume left presses on non-selectable rows so
        # the view never selects them.
        self._press_guard = _NonSelectablePressGuard(self.table)
        self.table.viewport().installEventFilter(self._press_guard)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet(TABLE_STYLE)
        # Keep tooltip in sync with cell text on the editable text
        # columns - when a cell is too narrow to show its full value,
        # the user gets the full text on hover.
        self.table.itemChanged.connect(self._sync_tooltip_with_text)
        # Tooltip column: open a multi-line popup editor on
        # double-click instead of the default inline QLineEdit.
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # Vertical bracket connectors on the EN column. Invalidate
        # the cache on any structural model change so the painter sees
        # fresh geometry; signal-driven so we don't have to thread the
        # invalidation through every mutation site.
        self._brackets_cache = None
        self._bracket_delegate = _GroupBracketDelegate(self)
        self.table.setItemDelegateForColumn(self.EN, self._bracket_delegate)
        m = self.table.model()
        m.rowsInserted.connect(self._invalidate_brackets)
        m.rowsRemoved.connect(self._invalidate_brackets)
        m.modelReset.connect(self._invalidate_brackets)
        m.dataChanged.connect(self._invalidate_brackets)
        lay.addWidget(self.table)

    def _sync_tooltip_with_text(self, item):
        """Auto-set tooltip = text on user-editable text columns so a
        truncated cell reveals its full content on hover. Read-only
        cells (Type, Node, Original Name) get their tooltip set at
        insertion time and aren't routed through here."""
        if item is None or item.column() not in (self.LB, self.TT):
            return
        item.setToolTip(item.text() or '')

    def _on_cell_double_clicked(self, row, col):
        """Tooltip column: open the multi-line popup editor anchored
        to the cell's screen position so it visually expands from
        where the user clicked. Other columns fall through to Qt's
        default edit behaviour."""
        if col != self.TT:
            return
        # Skip structural rows (separator / group / text row / fixed
        # section) - their TT cell is read-only filler.
        if (self._is_separator(row) or self._is_group_marker(row)
                or self._is_text_row(row)):
            return
        item = self.table.item(row, col)
        if item is None:
            return
        # Anchor: top-left of the cell in screen coordinates.
        rect = self.table.visualItemRect(item)
        anchor = self.table.viewport().mapToGlobal(rect.topLeft())
        # Width: at least the cell's width, but enough for comfortable
        # multi-line editing. Height: the cell's height plus room for
        # ~5 lines of text and a Save/Cancel button row.
        popup_w = max(rect.width(), 320)
        popup_h = 160
        dlg = _TooltipEditDialog(self, item.text())
        dlg.setWindowFlags(QtCore.Qt.Popup)
        dlg.resize(popup_w, popup_h)
        dlg.move(anchor)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            item.setText(dlg.text())

    @staticmethod
    def _get_dv(widget):
        """Read the current value from a default-value cell widget."""
        if widget is None:
            return None
        # Bare QCheckBox is wrapped in a centered container - unwrap.
        if (type(widget) is QtWidgets.QWidget
                and not isinstance(widget, (QtWidgets.QSpinBox,
                                            QtWidgets.QDoubleSpinBox,
                                            QtWidgets.QCheckBox,
                                            QtWidgets.QComboBox,
                                            QtWidgets.QLineEdit))):
            cb = widget.findChild(QtWidgets.QCheckBox)
            if cb is not None:
                return cb.isChecked()
        if isinstance(widget, QtWidgets.QSpinBox):
            return widget.value()
        if isinstance(widget, QtWidgets.QDoubleSpinBox):
            return widget.value()
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentText()
        if isinstance(widget, QtWidgets.QLineEdit):
            return widget.text()
        return None

    def _is_separator(self, row):
        it = self.table.item(row, self.NM)
        return it is not None and it.data(QtCore.Qt.UserRole + 1) == 'separator'

    # Text row tag stored in UserRole+1.
    _TEXT_ROW = 'text'

    def _is_text_row(self, row):
        it = self.table.item(row, self.NM)
        return it is not None and (
            it.data(QtCore.Qt.UserRole + 1) == self._TEXT_ROW)

    # Group marker tags stored in UserRole+1. Begin and End come in pairs
    # linked by the integer id stored in UserRole+2.
    _GROUP_BEGIN = 'group_begin'
    _GROUP_END   = 'group_end'

    def _is_group_begin(self, row):
        it = self.table.item(row, self.NM)
        return it is not None and (
            it.data(QtCore.Qt.UserRole + 1) == self._GROUP_BEGIN)

    def _is_group_end(self, row):
        it = self.table.item(row, self.NM)
        return it is not None and (
            it.data(QtCore.Qt.UserRole + 1) == self._GROUP_END)

    def _is_group_marker(self, row):
        return self._is_group_begin(row) or self._is_group_end(row)

    def _group_id_at(self, row):
        """Return the integer group id stored on a Begin/End row, else None."""
        if not self._is_group_marker(row):
            return None
        it = self.table.item(row, self.NM)
        v = it.data(QtCore.Qt.UserRole + 2) if it else None
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _next_group_id(self):
        """Auto-numbering: max existing group id + 1, or 1 if none."""
        used = set()
        for r in range(self.table.rowCount()):
            gid = self._group_id_at(r)
            if gid is not None:
                used.add(gid)
        return (max(used) + 1) if used else 1

    def _find_pair_row(self, row):
        """Given a Begin or End row, return the row index of its pair
        (matching id, opposite kind), or -1 if not found."""
        gid = self._group_id_at(row)
        if gid is None:
            return -1
        is_begin = self._is_group_begin(row)
        for r in range(self.table.rowCount()):
            if r == row:
                continue
            if self._group_id_at(r) != gid:
                continue
            if is_begin and self._is_group_end(r):
                return r
            if (not is_begin) and self._is_group_begin(r):
                return r
        return -1

    # ------------------------------------------------------------------
    # Bracket geometry for the EN-column delegate.
    # ------------------------------------------------------------------
    def _invalidate_brackets(self, *args, **kwargs):
        self._brackets_cache = None
        # Repaint the EN column so brackets follow row mutations.
        vp = self.table.viewport()
        if vp is not None:
            vp.update()

    def _get_brackets(self):
        """Return cached list of (begin_row, end_row, depth) tuples."""
        if self._brackets_cache is None:
            self._brackets_cache = self._compute_brackets()
        return self._brackets_cache

    def _compute_brackets(self):
        """Scan rows for Begin markers, pair them, compute nesting depth.

        Depth = number of OTHER brackets that fully contain this one.
        Sibling brackets (disjoint ranges) at the same level get the same
        depth, so they share an X offset without colliding.
        """
        pairs = []
        seen_ids = set()
        for r in range(self.table.rowCount()):
            if not self._is_group_begin(r):
                continue
            gid = self._group_id_at(r)
            if gid is None or gid in seen_ids:
                continue
            seen_ids.add(gid)
            pair = self._find_pair_row(r)
            if pair < 0:
                continue
            b = min(r, pair)
            e = max(r, pair)
            pairs.append((b, e))
        out = []
        for (b, e) in pairs:
            depth = sum(1 for (b2, e2) in pairs if b2 < b and e2 > e)
            out.append((b, e, depth))
        return out

    # Fixed section identifiers stored in UserRole+2 on fixed-section rows.
    # Group-marker rows reuse UserRole+2 for the integer group id; the two
    # never collide because rows are tagged by UserRole+1 first.
    _SECTION_INPUT  = 'section_input'
    _SECTION_MODEL  = 'section_model'
    _SECTION_OUTPUT = 'section_output'

    def _section_of_row(self, row):
        """Find which section (_SECTION_INPUT/MODEL/OUTPUT) contains the
        given row by scanning backward to the nearest fixed section
        header. Returns None if no section header is above (shouldn't
        happen in a well-formed table)."""
        for r in range(row, -1, -1):
            if self._is_fixed_section(r):
                return self._section_type(r)
        return None

    def _is_fixed_section(self, row):
        it = self.table.item(row, self.NM)
        return it is not None and it.data(QtCore.Qt.UserRole + 2) in (
            self._SECTION_INPUT, self._SECTION_MODEL, self._SECTION_OUTPUT)

    def _section_type(self, row):
        it = self.table.item(row, self.NM)
        return it.data(QtCore.Qt.UserRole + 2) if it else None

    def _find_section_row(self, section_id):
        for r in range(self.table.rowCount()):
            if self._section_type(r) == section_id:
                return r
        return -1

    def _drop_empty_sections_in_table(self):
        """Remove any fixed section header left with no content row before the
        next fixed section header or the end of the table. Live-table mirror of
        workflow_state.drop_empty_fixed_sections, for the mutation paths that
        edit rows without a full re-render (remove handlers, reorder). Scans
        bottom-up so removeRow never shifts a row still to visit."""
        r = self.table.rowCount() - 1
        while r >= 0:
            if self._is_fixed_section(r):
                nxt = r + 1
                if nxt >= self.table.rowCount() or self._is_fixed_section(nxt):
                    self.table.removeRow(r)
            r -= 1

    def _insert_fixed_section(self, row, title, section_id):
        """Insert a fixed section separator row. Bold name signals
        the section boundary; the user-added structural rows below
        use plain centered text on a tinted background instead."""
        self.table.insertRow(row)
        name_item = _ro_centered(title, '#6ab')
        f = name_item.font()
        f.setBold(True)
        name_item.setFont(f)
        name_item.setData(QtCore.Qt.UserRole + 1, 'separator')
        name_item.setData(QtCore.Qt.UserRole + 2, section_id)
        self.table.setItem(row, self.NM, name_item)
        self.table.setItem(row, self.TY, _ro_centered('', '#555'))
        self.table.setItem(row, self.ND, _ro_item('', '#555'))
        # Item under the EN cell widget so the row reports non-selectable for
        # this column too (indexAt on the EN cell otherwise defaults to
        # selectable, leaving a clickable sliver on the header).
        self.table.setItem(row, self.EN, _ro_item('', '#555'))
        self.table.setCellWidget(row, self.EN, QtWidgets.QWidget())
        self.table.setItem(row, self.LB, _ro_item('', '#555'))
        self.table.setItem(row, self.DV, _ro_item('', '#555'))
        self.table.setItem(row, self.TT, _ro_item('', '#555'))
        # Fixed section headers are structural, not actionable (can't be moved,
        # removed, or edited): make the row non-selectable so a click on it
        # deselects like empty space rather than highlighting a header.
        for c in range(self.table.columnCount()):
            it = self.table.item(row, c)
            if it:
                it.setBackground(QtGui.QBrush(QtGui.QColor('#1a2a2a')))
                it.setFlags(it.flags() & ~QtCore.Qt.ItemIsSelectable)

    def load(self, params, output_node_ids=None, input_node_ids=None):
        """Render rows in the order of `params`. The dialog precomposes
        params via workflow_state.compose_params_for_editor and the
        widget_order, so this method only dispatches by role to the
        right insertion helper.
        """
        self._output_node_ids = output_node_ids or set()
        self._input_node_ids = input_node_ids or set()
        # Drop fixed section headers whose section was emptied (Sync removal or
        # manual reorder) so no dangling 'X Parameters' header is shown.
        params = drop_empty_fixed_sections(params)
        self.table.setRowCount(0)

        _section_titles = {
            self._SECTION_INPUT: 'Input Parameters',
            self._SECTION_MODEL: 'Model Parameters',
            self._SECTION_OUTPUT: 'Output Parameters',
        }

        has_section = {self._SECTION_INPUT: False,
                       self._SECTION_MODEL: False,
                       self._SECTION_OUTPUT: False}

        for p in params:
            role = p.get('role')
            if role == 'separator':
                fixed = p.get('fixed')
                if fixed and fixed in _section_titles:
                    self._insert_fixed_section(
                        self.table.rowCount(),
                        _section_titles[fixed], fixed)
                    has_section[fixed] = True
                else:
                    self._insert_separator_row(
                        self.table.rowCount(),
                        label=p.get('label', ''))
            elif role == self._GROUP_BEGIN:
                self._insert_group_row(
                    self.table.rowCount(), self._GROUP_BEGIN,
                    p.get('id', 1), label=p.get('label', ''),
                    default=p.get('default', 'closed'))
            elif role == self._GROUP_END:
                self._insert_group_row(
                    self.table.rowCount(), self._GROUP_END,
                    p.get('id', 1))
            elif role == self._TEXT_ROW:
                self._insert_text_row(
                    self.table.rowCount(),
                    label=p.get('label', ''),
                    value=p.get('value', ''))
            elif role == 'knob':
                self._insert_knob_row(self.table.rowCount(), p)

        # Force an Output header only when output-side knobs exist without one
        # (defensive: section-aware ordering normally emits it). An emptied
        # Output section shows no header, consistent with Input/Model.
        output_has_knobs = any(
            p.get('role') == 'knob'
            and p.get('target_node_id') in self._output_node_ids
            for p in params)
        if output_has_knobs and not has_section[self._SECTION_OUTPUT]:
            self._insert_fixed_section(
                self.table.rowCount(), 'Output Parameters',
                self._SECTION_OUTPUT)

        # OR-normalize each EN-link group (Suite visibility rule or
        # V3 cluster on NukomfyRead / Write): a workflow JSON where
        # only one member of the group was exposed should open with all
        # members checked so the user keeps the linked-expose semantics
        # without having to redo the choice.
        self._normalize_link_enabled_groups()
        # Reconcile Suite trigger grey-out across the whole table once
        # all rows are in place. _insert_knob_row already applies the
        # state when both trigger and dependent happen to land in the
        # right insertion order; this catches the reverse pull.
        self._apply_suite_visibility_all()

    def _insert_knob_row(self, row, p):
        self.table.insertRow(row)
        en_wrap, _ = _centered_checkbox(checked=bool(p.get('enabled', True)))
        self.table.setCellWidget(row, self.EN, en_wrap)
        # Link EN checkbox bi-directionally across the row's link
        # group: Suite visibility rule (apply_color_transform group) or
        # V3 cluster (master + nested subs on NukomfyRead / Write).
        # Expose-on-gizmo is a shared decision - toggling any member
        # propagates to the others, and load-time OR semantics keep
        # every member of the group in sync.
        self._install_link_enable_hook(p, en_wrap)
        # Every member of an EN-link group (master AND dependents) stays
        # interactive: toggling any one propagates to the rest via
        # _sync_link_enabled_from (Suite visibility rules + V3 cluster
        # on NukomfyRead / Write). Every checkbox stays independently
        # clickable, with propagation added on top, so the user can
        # grab whichever member of the group is nearest.
        self.table.setItem(row, self.TY, _ro_centered(p.get('type', '')))
        nd_item = _ro_item(_node_display_label(
            p.get('node_type', ''), p.get('node_title', ''),
            p.get('display_name', '')))
        tip = _node_cell_tooltip(p)
        if tip:
            nd_item.setToolTip(tip)
        self.table.setItem(row, self.ND, nd_item)
        # V3 sub-knob display reads as a tree-style
        # indented child row. Box-drawing tree glyph (`└──`) + extra
        # leading whitespace gives a real-feeling indent; the master
        # name moves to a hover-tooltip so the cell stays compact even
        # when the column is narrow. Internal `name`/`widget_name`
        # keep the dotted path so save/load and submit pipeline stay
        # unchanged.
        orig_text = p.get('name', '')
        nm_tooltip = orig_text  # full name in case the cell is truncated
        if p.get('_v3_master'):
            sub_name = p.get('_v3_sub_name', '') or orig_text
            # Tree glyph at the start signals "child of master" - no
            # leading indent so the name doesn't lose horizontal space
            # on already-narrow cells.
            orig_text = '└── {}'.format(sub_name)
            nm_tooltip = 'Sub-input of: {}\n{}'.format(
                p.get('_v3_master', ''), sub_name)
        orig = _ro_item(orig_text, '#aaa')
        orig.setData(QtCore.Qt.UserRole, p)
        orig.setToolTip(nm_tooltip)
        self.table.setItem(row, self.NM, orig)
        lb_text = p.get('label', p.get('name', ''))
        lb_item = QtWidgets.QTableWidgetItem(lb_text)
        lb_item.setToolTip(lb_text)
        self.table.setItem(row, self.LB, lb_item)
        w, _, _ = _make_default_widget(p)
        # Center bare checkbox widgets in their cell - without a wrapper
        # they sit flush-left and look detached from the column header.
        if isinstance(w, QtWidgets.QCheckBox):
            wrap = QtWidgets.QWidget()
            wlay = QtWidgets.QHBoxLayout(wrap)
            wlay.setContentsMargins(0, 0, 0, 0)
            wlay.setAlignment(QtCore.Qt.AlignCenter)
            wlay.addWidget(w)
            self.table.setCellWidget(row, self.DV, wrap)
        else:
            self.table.setCellWidget(row, self.DV, w)
        # Suite trigger hook + initial state for dependents already in
        # the table. The reverse pull (dependents added after the
        # trigger) is covered by _apply_suite_visibility_all called at
        # the end of load() and install_snapshot_state().
        self._install_suite_trigger_hook(p, w)
        self._apply_suite_state_to_row(row, p)
        # Rebuild nested sub-rows live when a Suite V3 master
        # combo (NukomfyWrite.file_type) changes value.
        self._install_v3_rebuild_hook(p, w)
        tt_text = p.get('tooltip', '')
        tt_item = QtWidgets.QTableWidgetItem(tt_text)
        # Disable inline edit on Tooltip - double-click opens a
        # comfortable multi-line popup editor instead (handler in
        # _KnobsTable.__init__).
        tt_item.setFlags(tt_item.flags() & ~QtCore.Qt.ItemIsEditable)
        tt_item.setToolTip(tt_text)
        self.table.setItem(row, self.TT, tt_item)
        # Grey the row + disable the Enabled checkbox for bypassed/muted
        # nodes. Other editable fields (LB/TT/DV) stay interactive so the user
        # can prep metadata while the bypass is temporarily active - the EN
        # checkbox is the single source of truth for "will be exposed".
        node_state = p.get('_node_state')
        if node_state:
            _apply_node_state_greying(
                self.table, row, node_state, en_wrap,
                {'node': self.ND, 'name': self.NM})

    def _insert_separator_row(self, row, label=''):
        """User-added separator. The Gizmo Label cell is editable: when set,
        it becomes the Text_Knob `label` (left side) in the gizmo; when empty,
        the separator renders as a plain horizontal divider line."""
        self.table.insertRow(row)
        # Mark as separator via UserRole+1 (the internal role name is
        # 'separator'; the user-facing label is "Divider Line" to match
        # Nuke's terminology). Plain centered name on a tinted bg -
        # the hue family relates this row to the bold section
        # headers above without competing with them visually.
        name_item = _ro_centered('Divider Line', _DIVIDER_ACCENT_COLOR)
        name_item.setData(QtCore.Qt.UserRole + 1, 'separator')
        self.table.setItem(row, self.NM, name_item)
        self.table.setItem(row, self.TY, _ro_centered('', '#555'))
        self.table.setItem(row, self.ND, _ro_item('', '#555'))
        self.table.setCellWidget(row, self.EN, QtWidgets.QWidget())
        lb_item = QtWidgets.QTableWidgetItem(label)
        lb_item.setToolTip(
            'Optional: custom label shown on the left of the divider line.\n'
            'Leave empty for a plain horizontal line.')
        self.table.setItem(row, self.LB, lb_item)
        self.table.setItem(row, self.DV, _ro_item('', '#555'))
        self.table.setItem(row, self.TT, _ro_item('', '#555'))
        for c in range(self.table.columnCount()):
            it = self.table.item(row, c)
            if it:
                it.setBackground(QtGui.QBrush(QtGui.QColor('#1f2424')))

    def _insert_group_row(self, row, kind, group_id,
                          label='', default='closed'):
        """Insert a Begin or End group marker row.

        kind ∈ {self._GROUP_BEGIN, self._GROUP_END}. On Begin: Label is
        editable and a Default Value combo (Expanded/Collapsed) controls
        the runtime initial state. On End: all cells are read-only empty.
        Both rows share the same group_id stored in UserRole+2 so the
        pair can be matched across reorder operations."""
        is_begin = (kind == self._GROUP_BEGIN)
        self.table.insertRow(row)
        name_text = ('Group {} (Begin)'.format(group_id) if is_begin
                     else 'Group {} (End)'.format(group_id))
        name_item = _ro_centered(name_text, _GROUP_BRACKET_COLOR)
        name_item.setData(QtCore.Qt.UserRole + 1, kind)
        name_item.setData(QtCore.Qt.UserRole + 2, int(group_id))
        self.table.setItem(row, self.NM, name_item)
        self.table.setItem(row, self.TY, _ro_centered('', '#555'))
        self.table.setItem(row, self.ND, _ro_item('', '#555'))
        self.table.setCellWidget(row, self.EN, QtWidgets.QWidget())
        if is_begin:
            lb_item = QtWidgets.QTableWidgetItem(label)
            lb_item.setToolTip(
                'Optional: header text shown on the group in the gizmo '
                'Properties panel.\nLeave empty for an unnamed group.')
            self.table.setItem(row, self.LB, lb_item)
            combo = NoWheelComboBox()
            combo.addItems(['Expanded', 'Collapsed'])
            combo.setCurrentIndex(1 if (default or 'closed') == 'closed' else 0)
            combo.setToolTip(
                'Initial state of the group when the gizmo is created in '
                'Nuke.')
            self.table.setCellWidget(row, self.DV, combo)
        else:
            self.table.setItem(row, self.LB, _ro_item('', '#555'))
            self.table.setItem(row, self.DV, _ro_item('', '#555'))
        self.table.setItem(row, self.TT, _ro_item('', '#555'))
        for c in range(self.table.columnCount()):
            it = self.table.item(row, c)
            if it:
                it.setBackground(QtGui.QBrush(QtGui.QColor('#1f2620')))

    def _insert_text_row(self, row, label='', value=''):
        """User-added text row. Both Label (left side) and
        Default Value (right side, the text content) are editable.
        Renders as nuke.Text_Knob in the gizmo Properties panel -
        whitespace-preserving, HTML-supported."""
        self.table.insertRow(row)
        name_item = _ro_centered('Text', _TEXT_ACCENT_COLOR)
        name_item.setData(QtCore.Qt.UserRole + 1, self._TEXT_ROW)
        self.table.setItem(row, self.NM, name_item)
        self.table.setItem(row, self.TY, _ro_centered('', '#555'))
        self.table.setItem(row, self.ND, _ro_item('', '#555'))
        self.table.setCellWidget(row, self.EN, QtWidgets.QWidget())
        lb_item = QtWidgets.QTableWidgetItem(label)
        lb_item.setToolTip(
            'Optional: label shown on the left of the text in the\n'
            'gizmo Properties panel. Leave empty for a value-only row.')
        self.table.setItem(row, self.LB, lb_item)
        val_item = QtWidgets.QTableWidgetItem(value)
        val_item.setToolTip(
            'Text content shown on the right (HTML supported in Nuke).\n'
            'Leave empty for a label-only row.')
        self.table.setItem(row, self.DV, val_item)
        self.table.setItem(row, self.TT, _ro_item('', '#555'))
        for c in range(self.table.columnCount()):
            it = self.table.item(row, c)
            if it:
                it.setBackground(QtGui.QBrush(QtGui.QColor('#1f2228')))

    def _selected_row(self):
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _save_row_data(self, r):
        """Extract logical data from a row so it can be re-inserted."""
        if self._is_separator(r):
            d = {'role': 'separator'}
            st = self._section_type(r)
            if st:
                d['fixed'] = st
            else:
                lb = self.table.item(r, self.LB)
                # Preserve the raw text - a single space is a legitimate
                # label for Nuke (renders as a blank header), distinct
                # from no label at all.
                d['label'] = lb.text() if lb else ''
            return d
        if self._is_group_marker(r):
            kind = (self._GROUP_BEGIN if self._is_group_begin(r)
                    else self._GROUP_END)
            d = {'role': kind, 'id': self._group_id_at(r)}
            if kind == self._GROUP_BEGIN:
                lb = self.table.item(r, self.LB)
                d['label'] = lb.text() if lb else ''
                combo = self.table.cellWidget(r, self.DV)
                if isinstance(combo, QtWidgets.QComboBox):
                    d['default'] = ('open'
                                    if combo.currentText() == 'Expanded'
                                    else 'closed')
                else:
                    d['default'] = 'closed'
            return d
        if self._is_text_row(r):
            lb = self.table.item(r, self.LB)
            dv = self.table.item(r, self.DV)
            return {
                'role':  self._TEXT_ROW,
                'label': lb.text() if lb else '',
                'value': dv.text() if dv else '',
            }
        p = self.table.item(r, self.NM).data(QtCore.Qt.UserRole) or {}
        dv = self._get_dv(self.table.cellWidget(r, self.DV))
        if dv is None:
            dv = p.get('default_value')
        en_w = self.table.cellWidget(r, self.EN)
        en_cb = en_w.findChild(QtWidgets.QCheckBox) if en_w else None
        return {
            'role':    'knob',
            'data':    p,
            'label':   self.table.item(r, self.LB).text(),
            'tooltip': self.table.item(r, self.TT).text(),
            'enabled': en_cb.isChecked() if en_cb else True,
            'default_value': dv,
        }

    def _restore_row(self, r, rd):
        """Insert a fresh row at position r from saved data dict."""
        if rd['role'] == 'separator':
            fixed = rd.get('fixed')
            if fixed:
                titles = {
                    self._SECTION_INPUT: 'Input Parameters',
                    self._SECTION_MODEL: 'Model Parameters',
                    self._SECTION_OUTPUT: 'Output Parameters',
                }
                self._insert_fixed_section(
                    r, titles.get(fixed, 'Parameters'), fixed)
            else:
                self._insert_separator_row(r, label=rd.get('label', ''))
        elif rd['role'] in (self._GROUP_BEGIN, self._GROUP_END):
            self._insert_group_row(
                r, rd['role'], rd.get('id', 1),
                label=rd.get('label', ''),
                default=rd.get('default', 'closed'))
        elif rd['role'] == self._TEXT_ROW:
            self._insert_text_row(
                r, label=rd.get('label', ''),
                value=rd.get('value', ''))
        else:
            p = dict(rd['data'])
            p['label']   = rd['label']
            p['tooltip'] = rd['tooltip']
            if 'default_value' in rd:
                p['default_value'] = rd['default_value']
            self._insert_knob_row(r, p)
            en_w = self.table.cellWidget(r, self.EN)
            en_cb = en_w.findChild(QtWidgets.QCheckBox) if en_w else None
            if en_cb:
                en_cb.setChecked(rd['enabled'])

    # ------------------------------------------------------------------
    # V3-aware reorder. A master V3 widget owns a contiguous block
    # of sub-knobs immediately after it; the block moves together. Sub
    # rows themselves cannot be reordered independently.
    # ------------------------------------------------------------------
    def _row_param(self, r):
        if (self._is_separator(r) or self._is_group_marker(r)
                or self._is_text_row(r)):
            return None
        item = self.table.item(r, self.NM)
        return (item.data(QtCore.Qt.UserRole) or {}) if item else {}

    # ------------------------------------------------------------------
    # Suite boolean-trigger grey-out. See SUITE_VISIBILITY_RULES
    # at module level for the rule registry.
    # ------------------------------------------------------------------
    def _install_suite_trigger_hook(self, p, widget):
        """If this knob is a Suite trigger, connect its toggle signal so
        the dependent widgets follow its state. Called once per row at
        insert time."""
        node_type = p.get('node_type', '')
        widget_name = p.get('widget_name', p.get('name', ''))
        if not suite_dependents(node_type, widget_name):
            return
        if not isinstance(widget, QtWidgets.QCheckBox):
            return
        node_id = p.get('target_node_id')
        widget.toggled.connect(
            lambda checked, nid=node_id, wn=widget_name, nt=node_type:
            self._apply_suite_dependents(nt, nid, wn, checked))

    def _apply_suite_state_to_row(self, row, p):
        """If the row at `row` is a dependent of a Suite trigger already
        present elsewhere in the table, apply that trigger's current
        state (enabled/disabled) to the dependent's DV widget."""
        node_type = p.get('node_type', '')
        node_id = p.get('target_node_id')
        widget_name = p.get('widget_name', p.get('name', ''))
        rules = SUITE_VISIBILITY_RULES.get(node_type, {})
        for trigger_wn, deps in rules.items():
            if widget_name not in deps:
                continue
            checked = self._suite_trigger_state(node_id, trigger_wn)
            if checked is None:
                return  # trigger not yet inserted - load() will reconcile
            cell = self.table.cellWidget(row, self.DV)
            if cell is not None:
                cell.setEnabled(bool(checked))
            return

    def _suite_trigger_state(self, target_node_id, trigger_widget_name):
        """Find the trigger checkbox for (node_id, trigger_widget_name)
        and return its current checked state, or None if not present."""
        for r in range(self.table.rowCount()):
            p = self._row_param(r)
            if not p or p.get('target_node_id') != target_node_id:
                continue
            wn = p.get('widget_name', p.get('name', ''))
            if wn != trigger_widget_name:
                continue
            cell = self.table.cellWidget(r, self.DV)
            cb = (cell.findChild(QtWidgets.QCheckBox)
                  if cell is not None else None)
            if cb is None and isinstance(cell, QtWidgets.QCheckBox):
                cb = cell
            return cb.isChecked() if cb is not None else None
        return None

    def _apply_suite_dependents(self, node_type, target_node_id,
                                trigger_widget_name, checked):
        """Apply setEnabled(checked) to each dependent of this trigger
        in the table. Called by the toggled signal."""
        deps = suite_dependents(node_type, trigger_widget_name)
        if not deps:
            return
        for r in range(self.table.rowCount()):
            p = self._row_param(r)
            if not p or p.get('target_node_id') != target_node_id:
                continue
            wn = p.get('widget_name', p.get('name', ''))
            if wn not in deps:
                continue
            cell = self.table.cellWidget(r, self.DV)
            if cell is not None:
                cell.setEnabled(bool(checked))

    def _apply_suite_visibility_all(self):
        """Reconcile every Suite trigger in the table: scan rows for
        trigger knobs, apply their current state to all dependents.
        Idempotent - safe to call after any structural change (load,
        reorder)."""
        for r in range(self.table.rowCount()):
            p = self._row_param(r)
            if not p:
                continue
            node_type = p.get('node_type', '')
            widget_name = p.get('widget_name', p.get('name', ''))
            if not suite_dependents(node_type, widget_name):
                continue
            checked = self._suite_trigger_state(
                p.get('target_node_id'), widget_name)
            if checked is None:
                continue
            self._apply_suite_dependents(
                node_type, p.get('target_node_id'),
                widget_name, checked)

    # ------------------------------------------------------------------
    # EN-column bi-directional link. The
    # expose-on-gizmo decision is shared across two kinds of link
    # group:
    #
    # 1. Suite visibility rule group (apply_color_transform +
    #    input_transform / output_transform) - identified via
    #    SUITE_VISIBILITY_RULES on widget_name.
    # 2. V3 cluster on NukomfyRead / NukomfyWrite (master combo +
    #    every nested sub-input row) - identified at runtime via
    #    _v3_master metadata or the master detection helper.
    #
    # Toggling any member propagates to all others; load-time OR
    # normalise lifts the whole group when at least one member was
    # exposed in the saved state. Strictly a Suite-side behaviour:
    # third-party V3 clusters keep independent EN checkboxes.
    # ------------------------------------------------------------------
    _LINK_V3_NODE_TYPES = ('NukomfyRead', 'NukomfyWrite')

    def _link_group_key_for_row(self, row):
        """Return the link-group key for the row at `row`, or None if
        the row is not part of any EN-link group. Members of the same
        group share an EN propagation channel and OR semantics at load
        time. Two group kinds:
        - ('suite', frozenset(group_widget_names)) on (target_node_id,)
        - ('v3', top_master_widget_name) on (target_node_id,) for any
          row that belongs to a V3 cluster on NukomfyRead / Write at
          any nesting depth. The TOP master (first segment of a dotted
          path) is used so master + all descendants form ONE group.
        """
        p = self._row_param(row)
        if not p:
            return None
        node_type = p.get('node_type', '')
        nid = p.get('target_node_id')
        if nid is None:
            return None
        widget_name = p.get('widget_name', p.get('name', ''))
        group = suite_group_widgets(node_type, widget_name)
        if group:
            return (nid, 'suite', frozenset(group))
        if node_type in self._LINK_V3_NODE_TYPES:
            # Sub of a V3 cluster - climb to the top master (e.g.
            # _v3_master='file_type.compression' -> top='file_type').
            v3_master = p.get('_v3_master', '')
            if v3_master:
                top = v3_master.split('.', 1)[0]
                return (nid, 'v3', top)
            # The row may itself be a master with at least one sub
            # immediately following it - use the FIRST segment of its
            # widget_name as the group identity so nested masters
            # share the cluster of their top ancestor.
            master_wn = self._v3_master_name_at(row)
            if master_wn:
                top = master_wn.split('.', 1)[0]
                return (nid, 'v3', top)
        return None

    def _install_link_enable_hook(self, p, en_wrap):
        """Install the EN-link toggle propagation if this row can ever
        belong to a link group. For Suite visibility rules the widget
        name alone is enough to decide. For V3 clusters on
        NukomfyRead / NukomfyWrite the master row gains its group
        identity only after its first sub is inserted, so we install
        the hook on any knob of those node types and recompute the
        group key inside the handler. Dependent rows also get the
        signal connected (so the master's setChecked propagates) but
        their EN cell is locked non-interactive by
        _insert_knob_row."""
        node_type = p.get('node_type', '')
        widget_name = p.get('widget_name', p.get('name', ''))
        eligible = bool(suite_group_widgets(node_type, widget_name))
        if not eligible and node_type in self._LINK_V3_NODE_TYPES:
            eligible = True
        if not eligible:
            return
        en_cb = (en_wrap.findChild(QtWidgets.QCheckBox)
                 if en_wrap is not None else None)
        if en_cb is None:
            return
        nid = p.get('target_node_id')
        source_wn = widget_name
        en_cb.toggled.connect(
            lambda checked, _nid=nid, _wn=source_wn:
            self._sync_link_enabled_from(_nid, _wn, checked))

    def _find_row_by_nid_wn(self, target_node_id, widget_name):
        """Locate the row whose param has the given (target_node_id,
        widget_name). Returns the row index, or -1 if not present.
        Used by the toggle handler to recover the source row after
        the user may have reordered the table."""
        for r in range(self.table.rowCount()):
            p = self._row_param(r)
            if not p or p.get('target_node_id') != target_node_id:
                continue
            wn = p.get('widget_name', p.get('name', ''))
            if wn == widget_name:
                return r
        return -1

    def _sync_link_enabled_from(self, target_node_id, source_widget_name,
                                checked):
        """Propagate the EN checked state from the source row to every
        other member of its link group. The group key is recomputed
        from the source row each call so the V3 master / sub
        membership reflects the table's current shape."""
        source_row = self._find_row_by_nid_wn(
            target_node_id, source_widget_name)
        if source_row < 0:
            return
        key = self._link_group_key_for_row(source_row)
        if key is None:
            return
        for r in range(self.table.rowCount()):
            if r == source_row:
                continue
            if self._link_group_key_for_row(r) != key:
                continue
            en_w = self.table.cellWidget(r, self.EN)
            en_cb = (en_w.findChild(QtWidgets.QCheckBox)
                     if en_w is not None else None)
            if en_cb is None or en_cb.isChecked() == bool(checked):
                continue
            en_cb.blockSignals(True)
            try:
                en_cb.setChecked(bool(checked))
            finally:
                en_cb.blockSignals(False)

    def _normalize_link_enabled_groups(self):
        """OR semantics applied to every EN-link group: if ANY member
        is checked, force ALL members to checked. Called once after
        load so a workflow / saved state that exposes
        only one member of a linked group opens with the whole group
        exposed."""
        groups_by_key = {}
        for r in range(self.table.rowCount()):
            key = self._link_group_key_for_row(r)
            if key is None:
                continue
            en_w = self.table.cellWidget(r, self.EN)
            en_cb = (en_w.findChild(QtWidgets.QCheckBox)
                     if en_w is not None else None)
            if en_cb is None:
                continue
            groups_by_key.setdefault(key, []).append(en_cb)
        for cbs in groups_by_key.values():
            if not any(cb.isChecked() for cb in cbs):
                continue
            for cb in cbs:
                if cb.isChecked():
                    continue
                cb.blockSignals(True)
                try:
                    cb.setChecked(True)
                finally:
                    cb.blockSignals(False)

    # ------------------------------------------------------------------
    # Suite V3 DynamicCombo live sub-input rebuild. When a Suite master
    # combo such as NukomfyWrite.file_type changes value in the editor,
    # capture the current sub-row values into the per-option user-edits
    # map, remove the existing sub-rows, then add fresh sub-rows for the
    # new option from the snapshot. Restores user edits when the user
    # revisits a previously visited option.
    # ------------------------------------------------------------------
    def install_snapshot_state(self, snapshot, v3_user_edits,
                               overrides=None):
        """Snapshot-based load. Single source of truth for the
        cascade: the snapshot holds every V3 sub of every option of
        every master (DFS-ordered by the parser) and the flat user
        edits map carries per-option overrides on non-active subs.
        """
        self._snapshot = snapshot
        self._v3_user_edits = dict(v3_user_edits or {})
        # Session-level EN state for generic V3 subs (independent of the
        # master, unlike Suite). Lets an explicit uncheck survive a
        # master swap, which rebuilds the sub rows from the snapshot.
        # Seed it from persisted overrides so an uncheck made in a prior
        # session is restored by the cascade rebuild too: compose_params
        # honours overrides at the initial load, but the cascade re-inserts
        # sub rows straight from the snapshot, which would otherwise drop
        # the override.
        self._v3_enabled_state = {}
        overrides = overrides or {}
        for w in (snapshot or {}).get('widgets', []):
            if not w.get('_v3_master'):
                continue
            if w.get('node_type', '') in self._LINK_V3_NODE_TYPES:
                continue
            # overrides is keyed by make_override_key; make_v3_edit_key
            # produces the byte-identical '<nid>__<widget_name>' string,
            # so this lookup matches.
            key = make_v3_edit_key(
                w.get('target_node_id'),
                w.get('widget_name', w.get('name', '')))
            ov = overrides.get(key)
            if ov and 'enabled' in ov:
                self._v3_enabled_state[key] = ov['enabled']

    def _install_v3_rebuild_hook(self, p, widget):
        """Wire currentTextChanged on any V3 dynamic-combo master that
        may need its sub-rows rebuilt on value change: Suite top-level
        triggers (e.g. NukomfyWrite.file_type), Suite nested masters of
        any depth (e.g. file_type.compression, whose change toggles
        dw_compression_level), and generic third-party V3 masters (e.g.
        a Resize node's resize_type). The cascade reads option subs from
        self._snapshot and persists user edits on non-active subs into
        self._v3_user_edits."""
        node_type = p.get('node_type', '')
        widget_name = p.get('widget_name', p.get('name', ''))
        is_top_trigger = is_v3_rebuild_trigger(node_type, widget_name)
        is_dynamic_master = bool(p.get('_v3_is_dynamic_master'))
        if not (is_top_trigger or is_dynamic_master):
            return
        if not isinstance(widget, QtWidgets.QComboBox):
            return
        # Track current value so we know the old value at change time.
        widget._v3_last_value = widget.currentText()
        nid = p.get('target_node_id')

        def _on_text_changed(new_text, w=widget, master_nid=nid,
                             master_wn=widget_name):
            old_text = getattr(w, '_v3_last_value', '')
            w._v3_last_value = new_text
            if old_text == new_text:
                return
            # Resolve current master row by (nid, widget_name).
            for r in range(self.table.rowCount()):
                p_now = self._row_param(r)
                if not p_now:
                    continue
                if (p_now.get('target_node_id') != master_nid
                        or (p_now.get('widget_name', p_now.get('name', ''))
                            != master_wn)):
                    continue
                self._refresh_v3_master_subs(r, new_text, p_now)
                break

        widget.currentTextChanged.connect(_on_text_changed)

    def _read_dv_value(self, row):
        """Return the current Default Value cell content for `row`,
        regardless of widget type. Mirror of _get_dv() used by the
        save path."""
        cell = self.table.cellWidget(row, self.DV)
        if cell is None:
            return None
        return self._get_dv(cell)

    def _refresh_v3_master_subs(self, master_row, new_value, p_master):
        """Cascade rebuild driven by the snapshot. Captures the
        current sub-tree UI values into self._v3_user_edits (recursively,
        so nested sub-of-sub edits survive a top-master swap), removes the
        sub-rows, and inserts the subs of new_value from the snapshot with
        edits restored where present.
        """
        master_wn = p_master.get('widget_name', p_master.get('name', ''))
        nid = p_master.get('target_node_id')
        if not self._snapshot:
            return

        # (a) Capture current UI sub values into _v3_user_edits.
        self._snapshot_subs_into_user_edits(master_row, nid, master_wn)

        # (b) Remove the full sub-tree.
        _, b_end = self._v3_block_range(master_row)
        for r in range(b_end - 1, master_row, -1):
            self.table.removeRow(r)

        # (c) Insert subs of new_value from the snapshot (and cascade
        # into nested V3 sub-masters using their default_value).
        insert_row = master_row + 1
        self._insert_v3_subs_from_snapshot(
            insert_row, nid, master_wn, new_value)

        # (d) Align freshly-inserted sub rows' EN with the master.
        master_en_w = self.table.cellWidget(master_row, self.EN)
        master_cb = (master_en_w.findChild(QtWidgets.QCheckBox)
                     if master_en_w is not None else None)
        if master_cb is not None:
            self._sync_link_enabled_from(nid, master_wn,
                                         master_cb.isChecked())

    def _snapshot_default_for_widget(self, nid, widget_full_path):
        """Snapshot default_value for a single V3 sub-widget identified
        by its full dotted path. Returns None if not in snapshot.
        """
        if not self._snapshot:
            return None
        for w in self._snapshot.get('widgets', []):
            if w.get('target_node_id') != nid:
                continue
            if w.get('widget_name',
                     w.get('name', '')) == widget_full_path:
                return w.get('default_value')
        return None

    def _snapshot_subs_into_user_edits(self, master_row, nid, master_wn):
        """Capture current UI cell values for the DIRECT subs of the
        master and persist deltas vs the snapshot default into
        self._v3_user_edits, keyed by '<nid>__<widget_full_path>'.
        Recurses into nested V3 sub-masters using their current UI
        values so a top-master swap also persists sub-of-sub edits.

        A single edit slot per snapshot widget is intentional: a sub
        visible under multiple master options (e.g. dw_compression_level
        under both DWAA and DWAB) shares one user value across them, so
        toggling between codecs preserves the edit as long as the sub
        stays visible.
        """
        master_prefix = master_wn + '.'
        _, b_end = self._v3_block_range(master_row)
        nested_to_recurse = []
        for r in range(master_row + 1, b_end):
            p_sub = self._row_param(r)
            if not p_sub:
                continue
            if p_sub.get('_v3_master') != master_wn:
                continue
            sub_wn = p_sub.get('widget_name', p_sub.get('name', ''))
            if not sub_wn.startswith(master_prefix):
                continue
            relative = sub_wn[len(master_prefix):]
            if '.' in relative:
                continue
            cur = self._read_dv_value(r)
            if cur is None:
                cur = p_sub.get('default_value')
            edit_key = make_v3_edit_key(nid, sub_wn)
            snap_def = self._snapshot_default_for_widget(nid, sub_wn)
            if cur != snap_def:
                self._v3_user_edits[edit_key] = cur
            else:
                self._v3_user_edits.pop(edit_key, None)
            # Generic V3 subs have an independent EN (no link to the
            # master), so persist the checkbox state across this swap:
            # the rebuild re-creates the row from the snapshot otherwise,
            # losing an explicit uncheck. Suite subs follow their master
            # (re-aligned after the rebuild) and are not tracked here.
            if p_sub.get('node_type', '') not in self._LINK_V3_NODE_TYPES:
                en_w = self.table.cellWidget(r, self.EN)
                en_cb = (en_w.findChild(QtWidgets.QCheckBox)
                         if en_w is not None else None)
                if en_cb is not None:
                    self._v3_enabled_state[edit_key] = en_cb.isChecked()
            if p_sub.get('_v3_is_dynamic_master'):
                nested_to_recurse.append((r, sub_wn))
        for sub_row, sub_master_wn in nested_to_recurse:
            self._snapshot_subs_into_user_edits(
                sub_row, nid, sub_master_wn)

    def _insert_v3_subs_from_snapshot(self, insert_row, nid, master_wn,
                                      opt_value):
        """Insert UI rows for the DIRECT subs of master_wn visible under
        opt_value, sourced from self._snapshot.widgets in DFS order (the
        parser already lays them out master -> opt subs -> nested
        sub-of-sub). Edits in _v3_user_edits apply by widget full path
        (single slot per snapshot widget). For each direct sub that is
        itself a V3 master, cascade into its current value so the
        nested sub-tree appears immediately below.
        """
        if not self._snapshot:
            return insert_row
        master_prefix = master_wn + '.'
        opt_str = str(opt_value)
        for w in self._snapshot.get('widgets', []):
            if w.get('target_node_id') != nid:
                continue
            if w.get('_v3_master') != master_wn:
                continue
            show_keys = [str(k) for k in
                         (w.get('_v3_show_for_keys') or [])]
            if show_keys and opt_str not in show_keys:
                continue
            sub_wn = w.get('widget_name', w.get('name', ''))
            if not sub_wn.startswith(master_prefix):
                continue
            relative = sub_wn[len(master_prefix):]
            if '.' in relative:
                continue
            p_copy = dict(w)
            edit_key = make_v3_edit_key(nid, sub_wn)
            if edit_key in self._v3_user_edits:
                p_copy['default_value'] = self._v3_user_edits[edit_key]
            # Restore a generic sub's independent EN captured before the
            # rebuild (Suite subs are re-aligned to their master instead).
            if (w.get('node_type', '') not in self._LINK_V3_NODE_TYPES
                    and edit_key in self._v3_enabled_state):
                p_copy['enabled'] = self._v3_enabled_state[edit_key]
            self._insert_knob_row(insert_row, p_copy)
            insert_row += 1
            if p_copy.get('_v3_is_dynamic_master'):
                nested_value = p_copy.get('default_value')
                if nested_value is not None:
                    insert_row = self._insert_v3_subs_from_snapshot(
                        insert_row, nid, sub_wn, nested_value)
        return insert_row

    def _is_v3_sub_row(self, r):
        p = self._row_param(r)
        return bool(p and p.get('_v3_master'))

    def _v3_master_name_at(self, r):
        """Return the master widget_name (e.g. 'resize_type' or
        'file_type.compression' for a nested master) if row r is a V3
        master with at least one sub immediately following it,
        otherwise None.

        A row that is itself a sub of a higher master (its own
        _v3_master is set) can STILL be a master for deeper subs - the
        nesting NukomfyWrite case file_type.compression ->
        dw_compression_level requires both views to coexist.
        """
        p = self._row_param(r)
        if not p or self._is_separator(r):
            return None
        master = p.get('widget_name') or p.get('name') or ''
        nid = p.get('target_node_id')
        # A row is a master only if the next row is one of its subs.
        nxt = r + 1
        if nxt >= self.table.rowCount():
            return None
        sub = self._row_param(nxt)
        if (sub and sub.get('_v3_master') == master
                and sub.get('target_node_id') == nid):
            return master
        return None

    def _v3_block_range(self, r):
        """If row r is a V3 master, return (r, end_excl) covering
        master+subs (recursively, including sub-of-sub for any sub V3
        master deeper in the tree, e.g. file_type ->
        file_type.compression -> file_type.compression.dw_compression_level).
        Otherwise return (r, r+1).
        """
        master = self._v3_master_name_at(r)
        if not master:
            return r, r + 1
        nid = self._row_param(r).get('target_node_id')
        end = r + 1
        master_prefix = master + '.'
        while end < self.table.rowCount():
            sub = self._row_param(end)
            if not sub or sub.get('target_node_id') != nid:
                break
            sub_master = sub.get('_v3_master', '')
            if not sub_master:
                break
            # Accept any sub whose declared _v3_master is this master
            # or a dotted descendant path of this master (sub-of-sub
            # has _v3_master = 'file_type.compression' when master is
            # 'file_type').
            if not (sub_master == master
                    or sub_master.startswith(master_prefix)):
                break
            end += 1
        return r, end

    def _move_block(self, src_start, src_end_excl, delta):
        """Move rows [src_start, src_end_excl) up (delta=-1) or down (delta=+1)
        by one position, skipping over the neighbour block which may be a
        V3 master+subs cluster (must move atomically, never split it)."""
        rows = []
        for r in range(src_start, src_end_excl):
            rows.append(self._save_row_data(r))
        if delta < 0:
            # The neighbour above could be a V3 sub: trace back to find
            # its master and move the full block. Otherwise it's a
            # single row.
            above_idx = src_start - 1
            if self._is_v3_sub_row(above_idx):
                # Walk back to the master.
                p_sub = self._row_param(above_idx)
                master_wn = (p_sub or {}).get('_v3_master')
                nid = (p_sub or {}).get('target_node_id')
                cur = above_idx - 1
                while cur >= 0:
                    pc = self._row_param(cur)
                    if not pc:
                        break
                    if pc.get('_v3_master'):
                        cur -= 1
                        continue
                    cur_wn = pc.get('widget_name') or pc.get('name') or ''
                    if (cur_wn == master_wn
                            and pc.get('target_node_id') == nid):
                        break
                    cur -= 1
                n_start = cur if cur >= 0 else above_idx
            else:
                n_start = above_idx
            _, n_end = self._v3_block_range(n_start)
            # n_end may exceed above_idx+1 only if neighbour is a master
            # with subs (which can't be: above a master is always non-sub
            # of itself). Cap to src_start.
            n_end = min(n_end, src_start)
            n_rows = []
            for r in range(n_start, n_end):
                n_rows.append(self._save_row_data(r))
            for r in range(src_end_excl - 1, n_start - 1, -1):
                self.table.removeRow(r)
            new_start = n_start
            for i, rd in enumerate(rows):
                self._restore_row(new_start + i, rd)
            after = new_start + len(rows)
            for i, rd in enumerate(n_rows):
                self._restore_row(after + i, rd)
            self.table.selectRow(new_start)
        else:
            # Move down: the neighbour below could itself be a V3 master
            # (block) - move the entire neighbour block above us.
            n_start, n_end = self._v3_block_range(src_end_excl)
            n_rows = []
            for r in range(n_start, n_end):
                n_rows.append(self._save_row_data(r))
            for r in range(n_end - 1, src_start - 1, -1):
                self.table.removeRow(r)
            for i, rd in enumerate(n_rows):
                self._restore_row(src_start + i, rd)
            new_start = src_start + len(n_rows)
            for i, rd in enumerate(rows):
                self._restore_row(new_start + i, rd)
            self.table.selectRow(new_start)
        self._apply_suite_visibility_all()
        # Defence-in-depth: reorder is Model-only and boundary-trapped so it
        # cannot empty a section today; keep the invariant true even if a
        # future change loosens the move guards.
        self._drop_empty_sections_in_table()

    def _move_up(self):
        r = self._selected_row()
        if r <= 0:
            return
        if self._is_fixed_section(r):
            return  # can't move fixed sections
        if self._is_v3_sub_row(r):
            return  # V3 sub-knobs are bound to their master
        if self._section_of_row(r) in (self._SECTION_INPUT,
                                       self._SECTION_OUTPUT):
            return  # reorder allowed only inside Model Parameters
        # Paired movement - if End is selected and the row above is its
        # own Begin, move the pair up by 1 (otherwise End would
        # cross above its Begin which is forbidden).
        if self._is_group_end(r):
            pair = self._find_pair_row(r)
            if pair == r - 1:
                if r - 2 < 0 or self._is_fixed_section(r - 2):
                    return
                self._move_block(r - 1, r + 1, -1)
                # _move_block selects the first row of the moved block
                # (Begin's new position). User had End selected - keep
                # End selected at its new position (one row below Begin).
                self.table.selectRow(r - 1)
                return
        # Determine block start/end for the selected row (master of a V3
        # block or single non-V3 row).
        b_start, b_end = self._v3_block_range(r)
        if self._is_fixed_section(b_start - 1):
            return  # can't move above a section boundary
        self._move_block(b_start, b_end, -1)

    def _move_down(self):
        r = self._selected_row()
        if r < 0 or r >= self.table.rowCount() - 1:
            return
        if self._is_fixed_section(r):
            return  # can't move fixed sections
        if self._is_v3_sub_row(r):
            return  # V3 sub-knobs are bound to their master
        if self._section_of_row(r) in (self._SECTION_INPUT,
                                       self._SECTION_OUTPUT):
            return  # reorder allowed only inside Model Parameters
        # Paired movement - if Begin is selected and the row below is
        # its own End, move the pair down by 1 (otherwise Begin would
        # cross below its End which is forbidden).
        if self._is_group_begin(r):
            pair = self._find_pair_row(r)
            if pair == r + 1:
                if (r + 2 >= self.table.rowCount()
                        or self._is_fixed_section(r + 2)):
                    return
                self._move_block(r, r + 2, +1)
                return
        b_start, b_end = self._v3_block_range(r)
        if b_end >= self.table.rowCount():
            return
        if self._is_fixed_section(b_end):
            return  # can't move below a section boundary
        self._move_block(b_start, b_end, +1)

    def _can_add_separator(self):
        """Add Separator allowed only with a row selected inside the
        Model Parameters section. No selection or selection in another
        section -> button greyed (no magic default insertion)."""
        r = self._selected_row()
        if r < 0:
            return False
        # Don't allow inserting after a fixed section header itself -
        # that's an ambiguous "between sections" target.
        if self._is_fixed_section(r):
            return False
        return self._section_of_row(r) == self._SECTION_MODEL

    def _can_add_group(self):
        """Add Group allowed only with a row selected inside the Model
        Parameters section, same as Add Separator."""
        return self._can_add_separator()

    def _can_add_text(self):
        """Add Text allowed only with a row selected inside the Model
        Parameters section, same as Add Separator."""
        return self._can_add_separator()

    def _can_remove_group(self):
        r = self._selected_row()
        if r < 0:
            return False
        return self._is_group_marker(r)

    def _can_remove_text(self):
        r = self._selected_row()
        if r < 0:
            return False
        return self._is_text_row(r)

    def _safe_insert_row_in_model(self):
        """Compute a row index where a separator/group marker can be
        inserted safely inside Model Parameters.

        Rules (V3-aware + Model-only):
        * If selection is in a V3 master+subs block, jump to the row
          right after the block end.
        * Otherwise the insertion lands right after the selected row.
        * If no selection, append at the very end of the Model section
          (right before the Output Parameters header)."""
        r = self._selected_row()
        if r < 0:
            out_row = self._find_section_row(self._SECTION_OUTPUT)
            return out_row if out_row >= 0 else self.table.rowCount()
        if self._is_v3_sub_row(r):
            p_sub = self._row_param(r)
            master_wn = (p_sub or {}).get('_v3_master')
            nid = (p_sub or {}).get('target_node_id')
            cur = r - 1
            while cur >= 0:
                pc = self._row_param(cur)
                if not pc:
                    break
                if pc.get('_v3_master'):
                    cur -= 1
                    continue
                cur_wn = pc.get('widget_name') or pc.get('name') or ''
                if (cur_wn == master_wn
                        and pc.get('target_node_id') == nid):
                    break
                cur -= 1
            if cur >= 0:
                _, end = self._v3_block_range(cur)
                return end
            return r + 1
        _, end = self._v3_block_range(r)
        return end

    def _add_separator(self):
        if not self._can_add_separator():
            return
        insert_at = self._safe_insert_row_in_model()
        self._insert_separator_row(insert_at)
        self.table.selectRow(insert_at)

    def _add_group(self):
        if not self._can_add_group():
            return
        insert_at = self._safe_insert_row_in_model()
        gid = self._next_group_id()
        # Insert Begin then End consecutively. End goes at insert_at+1
        # because Begin was just inserted at insert_at.
        self._insert_group_row(insert_at, self._GROUP_BEGIN, gid)
        self._insert_group_row(insert_at + 1, self._GROUP_END, gid)
        self.table.selectRow(insert_at)

    def _add_text(self):
        if not self._can_add_text():
            return
        insert_at = self._safe_insert_row_in_model()
        self._insert_text_row(insert_at)
        self.table.selectRow(insert_at)

    def _remove_separator(self):
        r = self._selected_row()
        if r < 0 or not self._is_separator(r):
            return
        if self._is_fixed_section(r):
            return  # Fixed section separators cannot be removed
        self.table.removeRow(r)
        self._drop_empty_sections_in_table()

    def _remove_group(self):
        """Remove both rows (Begin + End) of the group whose marker is
        currently selected. The selection can be on either Begin or End
        - we find the pair via group id."""
        r = self._selected_row()
        if r < 0 or not self._is_group_marker(r):
            return
        pair = self._find_pair_row(r)
        if pair < 0:
            # Orphan marker (shouldn't happen) - remove just this row.
            self.table.removeRow(r)
            self._drop_empty_sections_in_table()
            return
        # Remove higher index first to keep the lower index stable.
        hi, lo = (r, pair) if r > pair else (pair, r)
        self.table.removeRow(hi)
        self.table.removeRow(lo)
        self._drop_empty_sections_in_table()

    def _remove_text(self):
        """Remove the selected text row."""
        r = self._selected_row()
        if r < 0 or not self._is_text_row(r):
            return
        self.table.removeRow(r)
        self._drop_empty_sections_in_table()

    def can_move_up(self):
        r = self._selected_row()
        if r <= 0:
            return False
        if self._is_fixed_section(r):
            return False
        if self._is_v3_sub_row(r):
            return False  # subs locked to master
        if self._section_of_row(r) in (self._SECTION_INPUT,
                                       self._SECTION_OUTPUT):
            return False  # reorder allowed only inside Model Parameters
        # End paired-up special case - neighbour is its own Begin, we
        # move the pair together. Allowed iff the pair has a non-
        # section row above it.
        if self._is_group_end(r):
            pair = self._find_pair_row(r)
            if pair == r - 1:
                return r - 2 >= 0 and not self._is_fixed_section(r - 2)
        b_start, _ = self._v3_block_range(r)
        if b_start <= 0:
            return False
        # The neighbour above is either a single row or a V3 master+subs
        # block. Section boundary check must look at the FIRST row of
        # the neighbour (the master, or the row itself if non-V3-sub).
        above_idx = b_start - 1
        if self._is_v3_sub_row(above_idx):
            p_sub = self._row_param(above_idx)
            master_wn = (p_sub or {}).get('_v3_master')
            nid = (p_sub or {}).get('target_node_id')
            cur = above_idx - 1
            while cur >= 0:
                pc = self._row_param(cur)
                if not pc:
                    break
                if pc.get('_v3_master'):
                    cur -= 1
                    continue
                cur_wn = pc.get('widget_name') or pc.get('name') or ''
                if (cur_wn == master_wn
                        and pc.get('target_node_id') == nid):
                    break
                cur -= 1
            neighbour_first = cur if cur >= 0 else above_idx
        else:
            neighbour_first = above_idx
        if self._is_fixed_section(neighbour_first):
            return False
        return True

    def can_move_down(self):
        r = self._selected_row()
        if r < 0 or r >= self.table.rowCount() - 1:
            return False
        if self._is_fixed_section(r):
            return False
        if self._is_v3_sub_row(r):
            return False  # subs locked to master
        if self._section_of_row(r) in (self._SECTION_INPUT,
                                       self._SECTION_OUTPUT):
            return False  # reorder allowed only inside Model Parameters
        # Begin paired-down special case - neighbour is its own End,
        # move the pair together.
        if self._is_group_begin(r):
            pair = self._find_pair_row(r)
            if pair == r + 1:
                nxt = r + 2
                return (nxt < self.table.rowCount()
                        and not self._is_fixed_section(nxt))
        _, b_end = self._v3_block_range(r)
        if b_end >= self.table.rowCount():
            return False
        if self._is_fixed_section(b_end):
            return False
        return True

    def can_remove_separator(self):
        r = self._selected_row()
        if r < 0 or not self._is_separator(r):
            return False
        if self._is_fixed_section(r):
            return False
        return True

    def get_params(self):
        result = []
        t = self.table
        for r in range(t.rowCount()):
            if self._is_separator(r):
                sep_data = {'role': 'separator'}
                st = self._section_type(r)
                if st:
                    sep_data['fixed'] = st
                else:
                    lb = t.item(r, self.LB)
                    # Preserve raw text - pure whitespace is a legit
                    # Nuke label (blank visible header), only an empty
                    # string means "no label".
                    sep_label = lb.text() if lb else ''
                    if sep_label:
                        sep_data['label'] = sep_label
                result.append(sep_data)
                continue
            if self._is_group_marker(r):
                kind = (self._GROUP_BEGIN if self._is_group_begin(r)
                        else self._GROUP_END)
                gd = {'role': kind, 'id': self._group_id_at(r)}
                if kind == self._GROUP_BEGIN:
                    lb = t.item(r, self.LB)
                    lbl = lb.text() if lb else ''
                    if lbl:
                        gd['label'] = lbl
                    combo = t.cellWidget(r, self.DV)
                    if isinstance(combo, QtWidgets.QComboBox):
                        gd['default'] = ('open'
                                         if combo.currentText() == 'Expanded'
                                         else 'closed')
                    else:
                        gd['default'] = 'closed'
                result.append(gd)
                continue
            if self._is_text_row(r):
                lb = t.item(r, self.LB)
                dv = t.item(r, self.DV)
                td = {'role': self._TEXT_ROW,
                      'label': lb.text() if lb else '',
                      'value': dv.text() if dv else ''}
                result.append(td)
                continue
            en_w = t.cellWidget(r, self.EN)
            en_cb = en_w.findChild(QtWidgets.QCheckBox) if en_w else None
            p     = t.item(r, self.NM).data(QtCore.Qt.UserRole) or {}
            enabled, intent = _read_enabled_with_intent(en_cb, p)
            label = t.item(r, self.LB).text().strip() or p.get('name', '')
            tip   = t.item(r, self.TT).text().strip()
            dv    = self._get_dv(t.cellWidget(r, self.DV))
            if dv is None:
                dv = p.get('default_value')
            entry = {
                'name':           p.get('name', ''),
                'label':          label,
                'type':           p.get('type', ''),
                'tooltip':        tip,
                'role':           'knob',
                'enabled':        enabled,
                '_intent_enabled': intent,
                'target_node_id': p.get('target_node_id'),
                'node_type':      p.get('node_type', ''),
                'node_title':     p.get('node_title', ''),
                'display_name':   p.get('display_name', ''),
                'widget_name':    p.get('widget_name', p.get('name', '')),
                'default_value':  dv,
                'is_output':      p.get('is_output', False),
            }
            if p.get('combo_values'):
                entry['combo_values'] = p['combo_values']
            if p.get('min_value') is not None:
                entry['min_value'] = p['min_value']
            if p.get('max_value') is not None:
                entry['max_value'] = p['max_value']
            if p.get('multiline'):
                entry['multiline'] = True
            if p.get('is_seed'):
                entry['is_seed'] = True
                if p.get('seed_control_default'):
                    entry['seed_control_default'] = p['seed_control_default']
            # Preserve PrimitiveNode metadata across save/load.
            # Without these the writeback in inject_primitive_values +
            # apply_seed_control can't find primitive params (the
            # _is_primitive flag is the discriminator).
            if p.get('_is_primitive'):
                entry['_is_primitive'] = True
                entry['_primitive_targets'] = p.get('_primitive_targets', [])
                entry['_primitive_outputs_name'] = p.get(
                    '_primitive_outputs_name', '')
                if p.get('_node_tooltip'):
                    entry['_node_tooltip'] = p['_node_tooltip']
            # Preserve COMFY_DYNAMICCOMBO_V3 sub-input metadata across
            # save/load. Without these the gizmo can't build the
            # visibility map and the submit-time strip has no spec to
            # consult. `_v3_show_for_keys` carries the inverse mapping
            # (each sub knows which option keys reveal it), which is
            # everything `strip_v3_inactive_subs` and the V3 visibility
            # map need - no per-master options_spec is required.
            if p.get('_v3_master'):
                entry['_v3_master'] = p['_v3_master']
                entry['_v3_sub_name'] = p.get('_v3_sub_name', '')
                entry['_v3_show_for_keys'] = list(
                    p.get('_v3_show_for_keys', []))
            # Tag carrying the "this sub is itself a V3 master" flag
            # across save/load so the rebuild hook reattaches on
            # reopen.
            if p.get('_v3_is_dynamic_master'):
                entry['_v3_is_dynamic_master'] = True
            # Ancestor conditions for nested V3 sub-of-sub (e.g.
            # dw_compression_level needs file_type=exr AND
            # compression in [DWAA, DWAB]). The gizmo builder ANDs
            # every ancestor entry across the visibility map.
            if p.get('_v3_ancestor_conditions'):
                entry['_v3_ancestor_conditions'] = [
                    list(a) for a in p['_v3_ancestor_conditions']]
            # Preserve knob rendering metadata across save/load.
            # gizmo_builder uses `_display_mode` to decide SLIDER flag on
            # Int_Knob; gizmo_callbacks uses `_step` for INT step snap at
            # commit. Both are read from the saved metadata.json, so they
            # must survive the round-trip through this table.
            if p.get('_display_mode'):
                entry['_display_mode'] = p['_display_mode']
            if p.get('_step') is not None:
                entry['_step'] = p['_step']
            # Persist _node_state snapshot for visualization on reopen
            if p.get('_node_state'):
                entry['_node_state'] = p['_node_state']
            result.append(entry)
        # Snapshot-based persistence: get_params emits UI-visible rows
        # only. Non-active V3 sub-options live in the persisted snapshot
        # (server-authoritative) and the editor's _v3_user_edits map
        # (per-option user-overridden values); the gizmo builder
        # consumes both via WorkflowItem.gizmo_params at build time.
        return result
