"""Manage ComfyUI machine connections: add, edit, remove, check status.

Designed to be embedded as a tab inside SettingsPanel.
"""

import logging

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui import _dialogs

_log = logging.getLogger(__name__)

from Nukomfy.client.machines import (
    Machine, machine_manager, check_machine, os_display_name, apply_machine_info)
from Nukomfy.core.settings import settings
from Nukomfy.gui.workers import UnifiedFetchWorker, stop_worker
from Nukomfy.gui._auto_refresh import busy_mark, schedule_after_min_visible
from Nukomfy.gui import _admin_gate
from Nukomfy.gui.icons import (icon_font, material_icon, set_press_icon,
                   ADD, REFRESH, RESTART_ALT, CLOSE,
                   EDIT, ARROW_UPWARD, ARROW_DOWNWARD,
                   LOCK)
from Nukomfy.gui.ui_state import ui_state

from Nukomfy.gui._theme import (
    cell_toolbar_icon,
    TABLE_STYLE, apply_window_chrome,
    ACCENT_GOLD, ERROR_COLOR,
)
from Nukomfy.gui.status_display import render_connectivity_status
from Nukomfy.gui import _focus_drop
from Nukomfy.gui._fields import NukomfyLineEdit


# Tooltip shown over the greyed machine-management controls when the
# lock_machines deployment flag is active (a disabled button cannot show
# a tooltip of its own, so it is set on a wrapper container).
_LOCK_MACHINES_TIP = ('Machines are managed by the settings override '
                      'and cannot be changed here.')


# ---------------------------------------------------------------------------
# Add / Edit dialog
# ---------------------------------------------------------------------------
class _MachineDialog(QtWidgets.QDialog):
    def __init__(self, machine=None, parent=None):
        super().__init__(parent)
        self.result_machine = None
        self._machine = machine
        self.setWindowTitle('Edit Machine' if machine else 'Add Machine')
        self.setMinimumWidth(400)
        _focus_drop.install(self)
        self._build()
        apply_window_chrome(self)
        # Clamp height to the layout's sizeHint - only width is resizable
        # (defaults to setMinimumWidth above). The dialog deliberately
        # does NOT persist its geometry: it's small and trivial, so a
        # consistent default open every time reads cleaner than restoring
        # a stale custom width across sessions.
        hint_h = self.sizeHint().height()
        self.setMinimumHeight(hint_h)
        self.setMaximumHeight(hint_h)
        if machine:
            self.name_edit.setText(machine.name)
            self.url_edit.setText(machine.url)
            self.hide_url_check.setChecked(bool(getattr(machine, 'hidden_url', False)))

    def _build(self):
        lay = QtWidgets.QFormLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        lay.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)

        self.name_edit = NukomfyLineEdit()
        self.name_edit.setPlaceholderText('Machine name…')
        lay.addRow('Name:', self.name_edit)

        self.url_edit = NukomfyLineEdit()
        self.url_edit.setPlaceholderText('http://hostname:port')
        self.url_edit.textChanged.connect(self._autofill_name)
        lay.addRow('ComfyUI URL:', self.url_edit)

        self.hide_url_check = QtWidgets.QCheckBox('Hide URL')
        self.hide_url_check.setToolTip(
            'When enabled, the URL is hidden in tables and logs,\n'
            'and stored using simple obfuscation in config files.')
        lay.addRow('', self.hide_url_check)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addRow(btns)

    def _autofill_name(self, url):
        if self.name_edit.text():
            return
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ''
            if host:
                self.name_edit.setText(host)
        except Exception:
            pass

    def _accept(self):
        name = self.name_edit.text().strip()
        url  = self.url_edit.text().strip()
        if not name:
            _dialogs.warn(self, 'Name required', 'Enter a name.')
            return
        if not url:
            _dialogs.warn(self, 'URL required', 'Enter a URL.')
            return

        # Validate URL structure before saving so the user gets a
        # clear message upfront instead of a cryptic urllib error at the
        # first fetch. Scheme rejection happens BEFORE the http:// auto-
        # prepend, otherwise `ftp://host` would become `http://ftp://host`
        # and slip past the scheme check.
        from urllib.parse import urlparse
        if '://' in url:
            try:
                preview = urlparse(url)
            except Exception as e:
                _dialogs.warn(
                    self, 'Invalid URL', 'Invalid URL: {}'.format(e))
                return
            if preview.scheme and preview.scheme not in ('http', 'https'):
                _dialogs.warn(
                    self, 'Invalid URL',
                    'URL must start with http:// or https://')
                return

        if not url.startswith(('http://', 'https://')):
            url = 'http://' + url

        try:
            parsed = urlparse(url)
        except Exception as e:
            _dialogs.warn(
                self, 'Invalid URL', 'Invalid URL: {}'.format(e))
            return

        if parsed.scheme not in ('http', 'https'):
            _dialogs.warn(
                self, 'Invalid URL',
                'URL must start with http:// or https://')
            return

        if not parsed.hostname:
            _dialogs.warn(
                self, 'Invalid URL',
                'URL must include a hostname '
                '(e.g., http://192.168.1.5:8188).')
            return

        if any(c in url for c in ('\n', '\r', '\t', '\x00')):
            _dialogs.warn(
                self, 'Invalid URL',
                'URL contains invalid control characters.')
            return

        try:
            port = parsed.port  # raises ValueError on malformed
        except ValueError:
            _dialogs.warn(
                self, 'Invalid URL',
                'URL port is invalid (must be an integer 1-65535).')
            return
        if port is not None and not (1 <= port <= 65535):
            _dialogs.warn(
                self, 'Invalid URL',
                'URL port must be between 1 and 65535.')
            return

        mid = self._machine.id if self._machine else None

        # Name uniqueness: the name is the user-facing identity (and, when
        # Hide URL is on, the key for obfuscation). Reject duplicates
        # upfront so the user picks a different name instead of getting a
        # silent rename downstream.
        if any(m.name == name and m.id != mid
               for m in machine_manager.machines):
            _dialogs.warn(
                self, 'Duplicate name',
                'A machine named "{}" already exists.\n'
                'Choose a different name.'.format(name))
            self.name_edit.setFocus()
            self.name_edit.selectAll()
            return

        self.result_machine = Machine(
            name, url, mid=mid,
            hidden_url=self.hide_url_check.isChecked())
        self.accept()

    def done(self, result):
        # Geometry deliberately not persisted - see comment in __init__.
        super().done(result)


# ---------------------------------------------------------------------------
# Column indices
# ---------------------------------------------------------------------------
_COL_ENABLED = 0   # enabled checkbox
_COL_STATUS  = 1   # online/offline dot
_COL_NAME    = 2
_COL_URL     = 3
_COL_COMFY   = 4
_COL_OS      = 5
_COL_GPU     = 6
_COL_VRAM    = 7
_COL_RAM     = 8
_COL_ACTIONS = 9   # refresh + reboot buttons
_HEADERS = ['Enabled', 'Status', 'Name', 'URL', 'ComfyUI', 'OS', 'GPU',
            'VRAM', 'RAM', '']


from Nukomfy.gui._table_utils import _proportional_fit, _install_absorber


# ---------------------------------------------------------------------------
# Machines Tab  (QWidget - embedded in SettingsPanel)
# ---------------------------------------------------------------------------
class MachinesTab(QtWidgets.QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_window_chrome(self)
        self._worker = None
        self._single_workers = {}   # {machine_id: UnifiedFetchWorker}
        self._refresh_btns = {}     # {machine_id: QPushButton}
        self._single_busy_start = {}  # {machine_id: monotonic stamp of greyed icon}
        # True while "Update All" runs: the per-row Refresh buttons are the
        # batch's to own, so the single-refresh re-enable paths defer to it.
        self._check_all_busy = False
        self._reboot_timer = None
        self._machines_locked = settings.lock_machines
        self._build()
        self._refresh_table()

    def showEvent(self, event):
        super().showEvent(event)
        # Auto-check all machines every time the tab becomes visible
        self._check_all()

    def _stop_workers(self):
        """Cancel all running status workers."""
        self._worker = stop_worker(self._worker)
        for mid in list(self._single_workers):
            stop_worker(self._single_workers[mid])
        self._single_workers.clear()
        # A stopped batch never reaches _on_check_all_finished; clear the flag
        # here so a later single refresh isn't swallowed.
        self._check_all_busy = False
        if self._reboot_timer:
            self._reboot_timer.stop()

    # ------------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── Toolbar ────────────────────────────────────────────────────
        tb = QtWidgets.QHBoxLayout()
        tb.setSpacing(6)

        self.add_btn    = QtWidgets.QPushButton('Add')
        set_press_icon(self.add_btn, ADD)
        self.edit_btn   = QtWidgets.QPushButton('Edit')
        set_press_icon(self.edit_btn, EDIT)
        self.remove_btn = QtWidgets.QPushButton('Remove')
        set_press_icon(self.remove_btn, CLOSE)
        self.check_btn  = QtWidgets.QPushButton('Update All')
        set_press_icon(self.check_btn, REFRESH)

        # Reorder buttons - same visual pattern as Workflow Creator
        # (_TableReorderFrame). Persists immediately, consistent with the
        # rest of the Machines tab (add/remove/edit/enable also auto-save).
        self.up_btn   = QtWidgets.QPushButton(ARROW_UPWARD)
        self.down_btn = QtWidgets.QPushButton(ARROW_DOWNWARD)
        for b in (self.up_btn, self.down_btn):
            b.setFont(icon_font(14))
            b.setFixedSize(26, 22)
            b.setStyleSheet(
                'QPushButton{background:#1e1e1e;color:#bbb;border:1px solid #444;'
                'border-radius:3px;font-size:11px;padding:0 6px;}'
                'QPushButton:hover{color:#fff;border-color:#666;}'
                'QPushButton:disabled{background:#1a1a1a;color:#444;'
                'border-color:#2a2a2a;}')
        self.up_btn.setToolTip('Move selected machine up')
        self.down_btn.setToolTip('Move selected machine down')
        self.up_btn.clicked.connect(lambda: self._move_selected(-1))
        self.down_btn.clicked.connect(lambda: self._move_selected(+1))
        self.up_btn.setEnabled(False)
        self.down_btn.setEnabled(False)

        self.edit_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)

        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.remove_btn.clicked.connect(self._remove)
        self.check_btn.clicked.connect(self._check_all)

        for b in (self.add_btn, self.edit_btn, self.remove_btn, self.check_btn):
            b.setFixedHeight(24)

        # Row-action buttons must NOT trigger _focus_drop's selection clear:
        # they operate on the currently selected machine row, so wiping the
        # selection on click silently no-ops the action.
        for b in (self.edit_btn, self.remove_btn, self.up_btn, self.down_btn):
            b.setProperty('_keep_selection', True)

        self._tb_add(tb, self.add_btn)
        self._tb_add(tb, self.edit_btn)
        self._tb_add(tb, self.remove_btn)
        tb.addSpacing(8)
        self._tb_add(tb, self.up_btn)
        self._tb_add(tb, self.down_btn)
        tb.addStretch()
        tb.addWidget(self.check_btn)
        root.addLayout(tb)

        # ── Table ──────────────────────────────────────────────────────
        self.table = QtWidgets.QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        h = self.table.horizontalHeader()
        h.setStretchLastSection(False)
        # Default Qt min section size (~30px) clamps narrow columns.
        h.setMinimumSectionSize(2)
        _fixed_cols = {_COL_ENABLED, _COL_STATUS, _COL_ACTIONS}
        for col in range(len(_HEADERS)):
            if col in _fixed_cols:
                h.setSectionResizeMode(col, QtWidgets.QHeaderView.Fixed)
            else:
                h.setSectionResizeMode(col, QtWidgets.QHeaderView.Interactive)
        self.table.setColumnWidth(_COL_ENABLED, 68)
        self.table.setColumnWidth(_COL_STATUS,  56)
        self.table.setColumnWidth(_COL_NAME,   160)
        self.table.setColumnWidth(_COL_URL,    300)
        self.table.setColumnWidth(_COL_COMFY,   80)
        self.table.setColumnWidth(_COL_OS,      80)
        self.table.setColumnWidth(_COL_GPU,    350)
        self.table.setColumnWidth(_COL_VRAM,    85)
        self.table.setColumnWidth(_COL_RAM,     85)
        # Actions col: 2 buttons (refresh+reboot) at 22px + 2px spacing
        # + 3 (1 left + 2 right, the right absorbs the 1px gridline overlap).
        self.table.setColumnWidth(_COL_ACTIONS, 22 * 2 + 5)
        _install_absorber(self.table, _COL_RAM)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet(TABLE_STYLE)
        self.table.doubleClicked.connect(self._edit)
        self.table.itemSelectionChanged.connect(self._on_selection)
        root.addWidget(self.table, 1)

        # Footnote (bottom-left, under the table): make the tab's auto-save
        # explicit. Every machine action (add, remove, enable/disable,
        # reorder) persists immediately, so the Save/Cancel buttons (which
        # gate the other Settings tabs) don't apply here.
        hint = QtWidgets.QLabel(
            '* Machine changes apply immediately and are not affected by '
            'Save or Cancel.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color:#666;font-size:11px;')
        root.addWidget(hint)

        # Restore saved column widths & persist on resize
        ui_state.restore_column_widths('machines_tab', self.table)
        # Force fixed columns back to their intended size (restore may
        # have overwritten them with stale saved values)
        h.blockSignals(True)
        self.table.setColumnWidth(_COL_ENABLED, 68)
        self.table.setColumnWidth(_COL_STATUS, 56)
        # Actions col: 2 buttons (refresh+reboot) at 22px + 2px spacing
        # + 3 (1 left + 2 right, the right absorbs the 1px gridline overlap).
        self.table.setColumnWidth(_COL_ACTIONS, 22 * 2 + 5)
        h.blockSignals(False)
        _proportional_fit(self.table)
        # Debounced save (avoid saving mid-cascade)
        self._col_save_timer = QtCore.QTimer(self.table)
        self._col_save_timer.setSingleShot(True)
        self._col_save_timer.setInterval(500)
        self._col_save_timer.timeout.connect(
            lambda: ui_state.save_column_widths('machines_tab', self.table))
        h.sectionResized.connect(lambda *_: self._col_save_timer.start())

    def _tb_add(self, tb, btn):
        # When lock_machines is active the management buttons are disabled
        # and wrapped so the explanatory tooltip still shows (a disabled
        # button shows no tooltip of its own).
        if not self._machines_locked:
            tb.addWidget(btn)
            return
        btn.setEnabled(False)
        wrap = QtWidgets.QWidget()
        wl = QtWidgets.QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(btn)
        wrap.setToolTip(_LOCK_MACHINES_TIP)
        tb.addWidget(wrap)

    # ------------------------------------------------------------------
    def _refresh_table(self):
        # Drop the old button refs before setRowCount(0) destroys the widgets,
        # so a machine removed since the last build leaves no dangling id.
        self._refresh_btns.clear()
        self.table.setRowCount(0)
        for m in machine_manager.machines:
            self._append_row(m, info=m.info)

    def _append_row(self, machine, info=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 26)

        # On locked machines the user keeps full control over the
        # operator-level affordances - Enabled checkbox, Refresh,
        # Reboot - so they can silence an offline shared farm locally
        # or trigger a live restart without admin intervention. Only
        # Edit / Remove (toolbar) are gated. The "this is a globally
        # managed machine" cue is a small lock icon prefixed inside the
        # Name cell (no row tinting; tinting reads as offline).
        chk = QtWidgets.QCheckBox()
        chk.setChecked(machine.enabled)
        chk.setToolTip('Enable this machine for submission')
        chk.toggled.connect(
            lambda checked, mid=machine.id: self._set_enabled(mid, checked))
        chk_w = QtWidgets.QWidget()
        chk_lay = QtWidgets.QHBoxLayout(chk_w)
        chk_lay.setContentsMargins(0, 0, 0, 0)
        chk_lay.setAlignment(QtCore.Qt.AlignCenter)
        chk_lay.addWidget(chk)
        self.table.setCellWidget(row, _COL_ENABLED, chk_w)

        # Status dot
        dot = QtWidgets.QLabel()
        dot.setAlignment(QtCore.Qt.AlignCenter)
        self._apply_dot(dot, info)
        self.table.setCellWidget(row, _COL_STATUS, dot)

        # Name (carries machine id in UserRole). Locked machines get a
        # small lock icon prefixed inside the cell - same row background
        # as user rows (no dark tint), the icon alone marks them as
        # globally managed.
        name_item = QtWidgets.QTableWidgetItem(machine.name)
        name_item.setData(QtCore.Qt.UserRole, machine.id)
        if machine.locked:
            name_item.setIcon(material_icon(LOCK, '#888', 14))
            name_item.setToolTip(
                'Locked by settings override - read only')
        self.table.setItem(row, _COL_NAME, name_item)

        self.table.setItem(row, _COL_URL, _make_url_item(machine))
        for col in (_COL_COMFY, _COL_OS, _COL_GPU, _COL_VRAM, _COL_RAM):
            it = _dim('-')
            it.setTextAlignment(QtCore.Qt.AlignCenter)
            self.table.setItem(row, col, it)

        # Actions cell - refresh + reboot. Stretch factors 1:2 compensate
        # for the table's 1px right-edge gridline (would otherwise eat the
        # right breathing pixel and yield 1px-left/0px-right asymmetry).
        # Both refresh and reboot are available on locked rows too -
        # they're operator actions on the live server, not edits to
        # the machine record. Only Edit / Remove (toolbar) are gated.
        actions_w = QtWidgets.QWidget()
        actions_lay = QtWidgets.QHBoxLayout(actions_w)
        actions_lay.setContentsMargins(0, 0, 0, 0)
        actions_lay.setSpacing(2)
        actions_lay.addStretch(1)

        refresh_btn = QtWidgets.QPushButton(REFRESH)
        refresh_btn.setFont(icon_font(14))
        refresh_btn.setFixedSize(22, 22)
        refresh_btn.setToolTip('Update this machine')
        refresh_btn.setStyleSheet(cell_toolbar_icon(ACCENT_GOLD))
        refresh_btn.clicked.connect(
            lambda _=False, mid=machine.id: self._check_one(mid))
        # A table rebuild mid Update All (e.g. a Move) would otherwise create
        # this button enabled and reopen the overlap window the batch closes.
        refresh_btn.setEnabled(not self._check_all_busy)
        actions_lay.addWidget(refresh_btn)
        self._refresh_btns[machine.id] = refresh_btn

        reboot_btn = QtWidgets.QPushButton(RESTART_ALT)
        reboot_btn.setFont(icon_font(14))
        reboot_btn.setFixedSize(22, 22)
        reboot_btn.setToolTip('Reboot ComfyUI on this machine')
        reboot_btn.setStyleSheet(cell_toolbar_icon(ERROR_COLOR))
        reboot_btn.clicked.connect(
            lambda _=False, mid=machine.id: self._reboot_machine(mid))
        actions_lay.addWidget(reboot_btn)
        actions_lay.addStretch(2)

        self.table.setCellWidget(row, _COL_ACTIONS, actions_w)

        if info:
            self._fill_row(row, info)

    def _fill_row(self, row, info):
        """Write the hardware columns for a row. Use the live result when the
        host answered, otherwise fall back to the in-memory snapshot from this
        session (consistent with the Render Manager); blank only when nothing
        is known yet."""
        dot = self.table.cellWidget(row, _COL_STATUS)
        if dot:
            self._apply_dot(dot, info)

        if info.get('online') or 'online' not in info:
            hw = info
        else:
            # Offline: show the last snapshot seen this session (held in
            # memory, never persisted), same as the Render Manager. Empty
            # when the machine was never reached this session.
            name_item = self.table.item(row, _COL_NAME)
            mid = name_item.data(QtCore.Qt.UserRole) if name_item else None
            m = machine_manager.get(mid) if mid else None
            hw = (m.info if m else None) or {}
        self.table.item(row, _COL_COMFY).setText(hw.get('comfyui_ver', '-'))
        self.table.item(row, _COL_OS).setText(
            os_display_name(hw.get('os')) or '-')
        self.table.item(row, _COL_GPU).setText(hw.get('gpu', '-'))
        self.table.item(row, _COL_VRAM).setText(hw.get('vram_total', '-'))
        self.table.item(row, _COL_RAM).setText(hw.get('ram_total', '-'))

        # Dim all text cells when the machine is unreachable. Status
        # dot stays red (strong signal). Refresh button unaffected.
        dim = info.get('online') is False
        brush = (QtGui.QBrush(QtGui.QColor('#606060')) if dim
                 else QtGui.QBrush())
        for col in (_COL_NAME, _COL_URL, _COL_COMFY, _COL_OS,
                    _COL_GPU, _COL_VRAM, _COL_RAM):
            it = self.table.item(row, col)
            if it:
                it.setForeground(brush)

    def _apply_dot(self, label, info):
        label.setFont(icon_font(14))
        if info is None or 'online' not in info:
            online = None
        else:
            online = bool(info.get('online'))
        tooltip_label, color, icon_char = render_connectivity_status(online)
        label.setText(icon_char)
        label.setStyleSheet('color:{};'.format(color))
        if online is False:
            label.setToolTip('Offline - {}'.format(info.get('error', 'unreachable')))
        else:
            label.setToolTip(tooltip_label)

    # ------------------------------------------------------------------
    def _row_for_id(self, machine_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COL_NAME)
            if item and item.data(QtCore.Qt.UserRole) == machine_id:
                return row
        return -1

    def _set_status_spinner(self, row):
        """Set a row's status dot to the neutral grey '\u2026' checking glyph,
        shown while a probe is pending and resolved to online/offline by the
        probe result."""
        if row < 0:
            return
        dot = self.table.cellWidget(row, _COL_STATUS)
        if dot:
            dot.setText('\u2026')
            dot.setFont(QtGui.QFont())
            dot.setStyleSheet('color:#888;font-size:11px;')

    def _reboot_alias_ids(self, machine_id):
        """All machine ids sharing this id's URL (alias rows). The reboot
        status reflections fan out to every row of the same server, not just
        the clicked one - otherwise a sibling alias keeps a stale Online dot
        for the whole restart (this tab has no periodic auto-refresh to heal
        it). Mirrors the submit-panel preflight fan-out (_reflect_preflight_status)."""
        m = machine_manager.get(machine_id)
        if not m:
            return [machine_id]
        ids = [mm.id for mm in machine_manager.machines if mm.url == m.url]
        if machine_id not in ids:
            ids.append(machine_id)
        return ids

    def _recheck_reboot_aliases(self, machine_id):
        """Re-probe every alias row after a reboot so the spinner on sibling
        rows resolves too. Each alias goes through the standard per-row Update
        path (_check_one), reusing its worker/cache/button handling."""
        for aid in self._reboot_alias_ids(machine_id):
            self._check_one(aid)

    def _selected_id(self):
        item = self.table.item(self.table.currentRow(), _COL_NAME)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _on_selection(self):
        # Management is locked to the settings override: the toolbar
        # buttons stay disabled regardless of which row is selected.
        if self._machines_locked:
            return
        has = bool(self.table.selectedItems())
        row = self.table.currentRow()
        n = self.table.rowCount()
        # Locked machines (from settings_overrides/) cannot be edited,
        # removed, or reordered. The first user machine cannot
        # move up either - it would cross the global boundary.
        mid = self._selected_id()
        m = machine_manager.get(mid) if mid else None
        is_locked = bool(m and m.locked)
        global_count = machine_manager.global_count()
        self.edit_btn.setEnabled(has and not is_locked)
        self.remove_btn.setEnabled(has and not is_locked)
        # Up: row > 0 AND not locked AND moving wouldn't enter the global
        # block (target row >= global_count).
        can_up = (has and not is_locked and row > 0
                  and (row - 1) >= global_count)
        # Down: row < n-1 AND not locked. Down can never cross into the
        # global block since globals always sit at the head.
        can_down = (has and not is_locked and 0 <= row < n - 1)
        self.up_btn.setEnabled(can_up)
        self.down_btn.setEnabled(can_down)

    def _move_selected(self, delta):
        """Shift selected machine up/down (-1/+1). Persists immediately."""
        mid = self._selected_id()
        if not mid:
            return
        new_idx = machine_manager.move(mid, delta)
        if new_idx < 0:
            return
        self._refresh_table()
        row = self._row_for_id(mid)
        if row >= 0:
            self.table.selectRow(row)

    # ------------------------------------------------------------------
    def _add(self):
        dlg = _MachineDialog(parent=self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        machine_manager.add(dlg.result_machine)
        self._append_row(dlg.result_machine, info=None)
        # Immediately check the new machine
        self._check_one(dlg.result_machine.id)
        self._notify_render_manager()

    def _edit(self):
        mid = self._selected_id()
        if not mid:
            return
        m = machine_manager.get(mid)
        if not m:
            return
        # Locked machines are not editable. The Edit toolbar button is
        # already disabled in _on_selection, but the table's
        # doubleClicked signal also routes here - guard explicitly so a
        # double-click on a locked row stays a no-op.
        if m.locked:
            return
        dlg = _MachineDialog(machine=m, parent=self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        machine_manager.update(dlg.result_machine)
        row = self._row_for_id(mid)
        if row >= 0:
            self.table.item(row, _COL_NAME).setText(dlg.result_machine.name)
            # URL cell rebuilt so the Hidden/visible state updates if the
            # user toggled the checkbox in the dialog.
            self.table.setItem(
                row, _COL_URL, _make_url_item(dlg.result_machine))
            # Re-check after edit
            self._check_one(dlg.result_machine.id)
        self._notify_render_manager()

    def _remove(self):
        mid = self._selected_id()
        if not mid:
            return
        m = machine_manager.get(mid)
        if not m:
            return
        ans = self._rich_msgbox(
            QtWidgets.QMessageBox.Question,
            f'Remove <b>{m.name}</b>?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No).exec_()
        if ans == QtWidgets.QMessageBox.Yes:
            w = self._single_workers.pop(mid, None)
            if w:
                stop_worker(w)
            self._refresh_btns.pop(mid, None)
            machine_manager.remove(mid)
            row = self._row_for_id(mid)
            if row >= 0:
                self.table.removeRow(row)
            self._notify_render_manager()

    # ------------------------------------------------------------------
    def _set_enabled(self, machine_id, enabled):
        # Locked machines are read-only. The UI hides their checkbox
        # so this branch shouldn't fire, but defensive guard covers any
        # code path that calls this directly.
        if not machine_manager.set_enabled(machine_id, enabled):
            return
        if enabled:
            self._check_one(machine_id)
        self._notify_render_manager()

    def _notify_render_manager(self):
        """Push a machine-list edit to an already-open Render Manager now.

        The Machines tab persists every change on the spot, but the Render
        Manager is a separate, non-modal window that otherwise only
        reconciles when the Settings dialog closes. Calling its idempotent
        reconcile here makes an add, remove, rename, or URL change show up
        live instead of waiting for the close.
        """
        from Nukomfy.gui import render_queue_panel as _rq
        rm = getattr(_rq, '_instance', None)
        if rm is None:
            return
        try:
            rm.reconcile_machines()
        except RuntimeError:
            # Panel torn down between the lookup and the call; the reconcile
            # on Settings close is the backstop.
            pass

    def _check_all(self):
        machines = machine_manager.enabled_machines
        if not machines:
            return
        if self._worker and self._worker.isRunning():
            return
        self._worker = stop_worker(self._worker)

        # Show spinner on the status dot for each machine being checked
        for m in machines:
            self._set_status_spinner(self._row_for_id(m.id))

        self.check_btn.setText('Updating…')
        self.check_btn.setEnabled(False)
        # Disable the per-row Refresh buttons for the batch's duration so a
        # click can't start a single refresh that overlaps and re-enables its
        # row mid-batch. Re-enabled together in _on_check_all_finished.
        self._check_all_busy = True
        for b in self._refresh_btns.values():
            try:
                b.setEnabled(False)
            except RuntimeError:
                pass
        self.table.clearSelection()
        self.table.setCurrentItem(None)
        # Invalidate the manager availability cache so an explicit
        # user-triggered "Update" fetches the current flag (otherwise the
        # 60s ping cache could mask a recent WebUI toggle).
        try:
            from Nukomfy.client import manager_client
            for _m in machines:
                manager_client.clear_cache(_m.url)
        except Exception:
            pass
        self._worker = UnifiedFetchWorker(machines, check_machine)
        self._worker.result.connect(self._on_result)
        self._worker.finished.connect(self._on_check_all_finished)
        self._worker.start()

    def _on_check_all_finished(self):
        self._worker = None
        self.check_btn.setText('Update All')
        self.check_btn.setEnabled(True)
        self._check_all_busy = False
        # Re-enable the per-row Refresh buttons together, except any whose own
        # single refresh is still running (its _on_single_finished owns that).
        for mid, b in self._refresh_btns.items():
            if mid in self._single_workers:
                continue
            try:
                b.setEnabled(True)
            except RuntimeError:
                pass

    def _check_one(self, machine_id):
        # Guard: already checking this machine
        w = self._single_workers.get(machine_id)
        if w and w.isRunning():
            return
        m = machine_manager.get(machine_id)
        if not m:
            return
        # Disable per-machine refresh button
        btn = self._refresh_btns.get(machine_id)
        if btn:
            btn.setEnabled(False)
            self._single_busy_start[machine_id] = busy_mark()
        # Show spinner on the status dot
        self._set_status_spinner(self._row_for_id(m.id))
        # Single-machine Update - same cache-invalidation logic.
        try:
            from Nukomfy.client import manager_client
            manager_client.clear_cache(m.url)
        except Exception:
            pass
        worker = UnifiedFetchWorker([m], check_machine)
        worker.result.connect(self._on_result)
        worker.finished.connect(
            lambda mid=machine_id: self._on_single_finished(mid))
        worker.start()
        self._single_workers[machine_id] = worker

    def _on_single_finished(self, machine_id):
        self._single_workers.pop(machine_id, None)
        start = self._single_busy_start.pop(machine_id, None)
        btn = self._refresh_btns.get(machine_id)
        if btn:
            def _reenable(b=btn):
                # An Update All started meanwhile: leave the re-enable to the
                # batch so the row doesn't flip back on clickable mid-batch.
                if self._check_all_busy:
                    return
                try:
                    b.setEnabled(True)
                except RuntimeError:
                    pass  # button was deleted (table rebuilt)
            schedule_after_min_visible(start, _reenable)

    def _on_result(self, machine_id, info):
        row = self._row_for_id(machine_id)
        if row >= 0:
            self._fill_row(row, info)

        # Re-enable per-machine button (skip during Update All - the batch
        # re-enables them together - and skip if a single worker owns it).
        if not self._check_all_busy and machine_id not in self._single_workers:
            btn = self._refresh_btns.get(machine_id)
            if btn:
                btn.setEnabled(True)

        # Update the in-memory snapshot on the Machine object (copy-on-write,
        # never persisted). Shared single writer, identical to the session
        # refresher behind Submit / Render Manager. `online` is tracked so
        # sort_offline_last reads the latest status on the next populate.
        m = machine_manager.get(machine_id)
        if m:
            apply_machine_info(m, info)

    def _rich_msgbox(self, icon, text, buttons=QtWidgets.QMessageBox.Ok):
        """Build a Nukomfy-titled QMessageBox with RichText so machine names
        and other essential terms can be bolded via inline HTML.

        Sized to hug its content (no extra padding to the right) and locked
        against resizing - same UX contract as the admin password dialog.
        """
        box = _dialogs.message_box(self)
        box.setIcon(icon)
        box.setWindowTitle('Nukomfy')
        box.setTextFormat(QtCore.Qt.RichText)
        box.setStandardButtons(buttons)
        box.setText(text)
        box.setWindowFlags(
            box.windowFlags() & ~QtCore.Qt.WindowMaximizeButtonHint
        )
        # Shrink-to-fit: forces the layout to the minimum size that holds
        # the icon + text + buttons. Removes the resize grip too.
        box.layout().setSizeConstraint(QtWidgets.QLayout.SetFixedSize)
        return box

    def _confirm_reboot(self, m, *, unavailable):
        """Custom Yes/No confirm dialog for reboot. Optionally prepended
        with a Material-glyph warning banner when the target machine is
        marked Unavailable. Uses `make_warning_banner` so the amber tone
        and icon match every other inline warning in the plugin instead
        of relying on QMessageBox's stock icon."""
        from Nukomfy.gui._inline_messages import make_warning_banner

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle('Reboot machine?')
        dlg.setWindowFlags(
            dlg.windowFlags() & ~QtCore.Qt.WindowMaximizeButtonHint
        )
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 14)
        lay.setSpacing(10)

        if unavailable:
            banner = make_warning_banner(dlg, font_size=11)
            # Explicit line break: the warning is two complete sentences,
            # one per visual line. Avoids relying on word-wrap heuristics
            # that broke "Reboot will / interrupt their session." across
            # an awkward boundary at narrow widths.
            line1 = 'Machine is marked <b>Unavailable</b>. Someone may be working on it.'
            line2 = 'Reboot will interrupt their session.'
            banner.set_message('{}<br>{}'.format(line1, line2))
            lay.addWidget(banner)
            # Measure the longer line at the actual rendered font and pin
            # the banner just wide enough to hold it without breaking.
            # Plain-text width (HTML tags stripped) is what QLabel cares
            # about for layout.
            fm = banner._text_label.fontMetrics()
            plain = (
                'Machine is marked Unavailable. '
                'Someone may be working on it.'
            )
            text_w = fm.horizontalAdvance(plain)
            # Icon column inside make_warning_banner is the glyph at
            # font_size+3 plus the 4px HBox spacing - roughly the icon's
            # font height. Add a small visual margin so the text doesn't
            # kiss the right edge.
            icon_w = fm.height() + 8
            banner.setMinimumWidth(text_w + icon_w + 12)

        question = QtWidgets.QLabel(
            'Are you sure you want to restart ComfyUI on '
            '<b>{}</b>?'.format(m.name)
        )
        question.setTextFormat(QtCore.Qt.RichText)
        question.setWordWrap(True)
        # Plain-text (HTML stripped) width so the dialog never clips the
        # machine name. SetFixedSize on the layout otherwise picks the
        # narrowest layout that satisfies word-wrap, which can cut
        # mid-sentence on long machine names.
        fm = question.fontMetrics()
        plain = 'Are you sure you want to restart ComfyUI on {}?'.format(m.name)
        question.setMinimumWidth(fm.horizontalAdvance(plain) + 20)
        lay.addWidget(question)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Yes
            | QtWidgets.QDialogButtonBox.No
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        dlg.layout().setSizeConstraint(QtWidgets.QLayout.SetFixedSize)
        return dlg.exec_() == QtWidgets.QDialog.Accepted

    def _reboot_machine(self, machine_id):
        """Restart ComfyUI on a machine via admin-gated manager endpoint."""
        m = machine_manager.get(machine_id)
        if not m:
            return

        # Invalidate the manager availability cache so the probe below
        # sees the live Availability flag. Without this, a user who just
        # toggled Availability in the WebUI would still see a stale
        # "Unavailable" warning for up to 60s.
        try:
            from Nukomfy.client import manager_client
            manager_client.clear_cache(m.url)
        except Exception:
            pass

        # Reachability probe before any user-facing prompt: an offline
        # machine cannot be rebooted, so confirming and then announcing
        # "manager not installed" would be misleading.
        probe = check_machine(m)
        if not probe.get('online'):
            # The probe is fresh truth: reflect the offline state in the
            # status dot before the dialog, otherwise the row keeps its
            # stale Online dot until the next refresh (the async Refresh
            # path repaints via _on_result the same way).
            for aid in self._reboot_alias_ids(machine_id):
                self._on_result(aid, probe)
            self._rich_msgbox(
                QtWidgets.QMessageBox.Warning,
                f'Machine <b>{m.name}</b> is offline. '
                f'Cannot send the reboot command.').exec_()
            return

        # When the machine is marked Unavailable the confirm dialog grows
        # a Material-styled warning banner above the question, matching
        # the rest of the plugin's inline warnings (`make_warning_banner`).
        # The admin password gate stays clean - the warning belongs to
        # the *decision* moment, not to the credential moment.
        if not self._confirm_reboot(
                m, unavailable=(probe.get('availability') == 'unavailable')):
            return

        outcome, password = _admin_gate.prompt_admin_password_ex(
            parent=self,
            base_url=m.url,
            operation_label="reboot",
            machine_label=m.name,
        )
        if outcome != "ok":
            # The gate reports *why* it returned no password, so the dot is
            # reflected without a second probe in every case but one:
            #   offline -> the gate's native probe just confirmed it; paint
            #     red from a synthetic result, no extra round-trip (this is
            #     the freeze a redundant check_machine call would otherwise add).
            #   error -> ambiguous (network drop or odd auth response); the
            #     only case where a fresh probe is still worth it.
            #   cancel / suite_missing / no_password / rate_limited -> the
            #     host answered, so it is online and the dot is already right.
            if outcome == "offline":
                off = {'online': False, 'error': 'unreachable'}
                for aid in self._reboot_alias_ids(machine_id):
                    self._on_result(aid, off)
            elif outcome == "error":
                probe = check_machine(m)
                for aid in self._reboot_alias_ids(machine_id):
                    self._on_result(aid, probe)
            return

        result = manager_client.reboot(m.url, password)
        if result == "ok":
            # The machine is restarting. Show the neutral "checking"
            # spinner rather than assert an unverified state: we have not
            # probed yet, so neither a stale Online nor a guessed Offline
            # is honest. The re-check timer below runs a real probe that
            # resolves the dot to Offline (still restarting) or Online
            # (back up). The 5s delay lets the old process finish dropping
            # so the probe doesn't catch a false Online during shutdown.
            for aid in self._reboot_alias_ids(machine_id):
                self._set_status_spinner(self._row_for_id(aid))
            self._rich_msgbox(
                QtWidgets.QMessageBox.Information,
                f'Reboot command sent to <b>{m.name}</b>.<br><br>'
                f'The machine will appear offline until it responds again. '
                f'Please wait for the restart to complete.').exec_()
            if self._reboot_timer:
                self._reboot_timer.stop()
            self._reboot_timer = QtCore.QTimer(self)
            self._reboot_timer.setSingleShot(True)
            self._reboot_timer.timeout.connect(
                lambda: self._recheck_reboot_aliases(machine_id))
            self._reboot_timer.start(5000)
        elif result == "wrong_password":
            self._rich_msgbox(
                QtWidgets.QMessageBox.Warning,
                f'Authentication expired on <b>{m.name}</b>. Please retry.').exec_()
        elif result == "rate_limited":
            self._rich_msgbox(
                QtWidgets.QMessageBox.Warning,
                f'Too many failed attempts on <b>{m.name}</b>. '
                f'Wait 1 minute and retry.').exec_()
        elif result == "no_password":
            self._rich_msgbox(
                QtWidgets.QMessageBox.Warning,
                f'No admin password configured on <b>{m.name}</b>.').exec_()
        else:
            # An unexpected failure here usually means the host dropped
            # after the password step. Re-probe and reflect so the dot
            # matches reality instead of the pre-reboot Online.
            probe = check_machine(m)
            for aid in self._reboot_alias_ids(machine_id):
                self._on_result(aid, probe)
            self._rich_msgbox(
                QtWidgets.QMessageBox.Warning,
                f'Reboot request failed. Check connection to <b>{m.name}</b>.'
            ).exec_()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _dim(text):
    item = QtWidgets.QTableWidgetItem(text)
    item.setForeground(QtGui.QColor('#666'))
    return item


def _make_url_item(machine):
    """URL cell: '(Hidden)' when machine.hidden_url, otherwise the
    plain URL. Parentheses are the cross-platform convention for a
    meta-state label (cf. `(deprecated)`, `(default)`)."""
    if machine.hidden_url:
        return _dim('(Hidden)')
    return _dim(machine.url)