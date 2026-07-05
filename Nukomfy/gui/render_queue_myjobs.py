"""Render Manager My Jobs widgets.

Personal submit-history widgets: Active (running/pending) and History
(terminal) tables, plus their splitter container `_MyJobsWidget`.
Filter `submit_history` to the current user, render via the shared
cell builder, and delegate dialog opening to the parent panel.

Internal module - public API of Render Manager surfaced via
render_queue_panel.py.
"""

import logging
import os

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui._fields import NukomfyLineEdit
from Nukomfy.gui import _dialogs

import Nukomfy.client.ws_client as ws_client
import Nukomfy.data.submit_history as submit_history
from Nukomfy.core.identity import current_user, ws_session_id
from Nukomfy.gui.ui_state import ui_state
from Nukomfy.gui.icons import (icon_font, set_press_icon, material_icon,
                               CLOSE, REMOVE, REFRESH, SEARCH,
                               DESCRIPTION, FILE_DOWNLOAD, DELETE)
from Nukomfy.gui._filter_button import StatusFilterButton, JOB_STATUS_FILTERS
from Nukomfy.gui._theme import (
    TABLE_STYLE,
    BUTTON_STYLE_CELL_ACTION, cell_action_colored, SEARCH_FIELD_STYLE,
    ERROR_COLOR, ERROR_HOVER,
    WARNING_STATUS, WARNING_STATUS_HOVER,
    SUCCESS_COLOR, SUCCESS_HOVER,
)
from Nukomfy.gui._splitter import DottedSplitter
from Nukomfy.gui.status_display import (
    render_job_status, ABORTING_LABEL, REMOVING_LABEL, INFLIGHT_COLOR)

from Nukomfy.gui.render_queue_format import (
    _format_duration, _centered_cell,
    _fill_myjobs_cells, _setup_table_columns, _install_empty_area_deselect,
    _make_status_cell, _HatchedProgressCell, _HatchedFillProgressCell,
    _LeftElideDelegate,
    _WS_MISSING_TOOLTIP, _coarse_progress_tooltip,
    _MJ_COL_STATUS, _MJ_COL_JOB, _MJ_COL_SENT, _MJ_COL_MACHINE,
    _MJ_COL_WORKFLOW, _MJ_COL_NODE, _MJ_COL_NKFILE,
    _MJ_COL_PROGDUR, _MJ_COL_ACTIONS, _MJH_COL_DELETE,
    _MJA_HEADERS, _MJA_WIDTHS, _MJH_HEADERS, _MJH_WIDTHS,
    _MJ_GROUPBOX_STYLE,
)
from Nukomfy.gui.render_queue_actions import (
    _abort_or_remove_entry, _entry_for_log, read_outputs)
from Nukomfy.gui.render_queue_context_menu import show_job_context_menu
from Nukomfy.utils.log_format import fmt_job

_log = logging.getLogger(__name__)


class _MyJobsTableBase(QtWidgets.QWidget):
    """Common table scaffolding for `_MyJobsActive` and `_MyJobsHistory`.

    Subclasses supply headers/widths/ui_key, then override `_fill_row`
    to populate the per-row divergent columns (progress/duration +
    actions). The shared 7-column base is filled by `_fill_myjobs_cells`.
    """

    # Overridden by subclasses
    _HEADERS = []
    _WIDTHS = {}
    _FIXED_COLS = frozenset()
    _UI_KEY = ''

    def _pixel_widths(self):
        """Subclass hook: return {col: exact_pixels} dict for action
        columns whose breathing room must remain a literal +2px at any
        DPI scale (see `_setup_table_columns` docstring)."""
        return {}

    def __init__(self, container, parent=None):
        super().__init__(parent)
        self._container = container  # _MyJobsWidget - for detail dialog + panel
        self._entries = []
        # Cached signature of the last `set_entries` input. Every tab
        # switch triggers `_MyJobsWidget._reload` -> `set_entries`, but the
        # data is usually unchanged between visits - compare a cheap
        # tuple of the visible fields and skip the full table rebuild
        # (setRowCount(0) + N×QTableWidgetItem/QPushButton creations)
        # when nothing that affects rendering has moved.
        self._last_sig = None
        # {prompt_id: (machine_url, _HatchedFillProgressCell)} - no-WS coarse
        # bars, refreshed in place from the shared cache (Active only).
        self._poll_progress_cells = {}

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._table = QtWidgets.QTableWidget(0, len(self._HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(TABLE_STYLE)
        self._table.setTextElideMode(QtCore.Qt.ElideLeft)
        self._table.setItemDelegateForColumn(
            _MJ_COL_NKFILE, _LeftElideDelegate(self._table))
        _setup_table_columns(self._table, self._HEADERS, self._WIDTHS,
                             fixed_cols=self._FIXED_COLS,
                             stretch_col=_MJ_COL_NKFILE,
                             ui_key=self._UI_KEY,
                             pixel_widths=self._pixel_widths())
        self._table.cellDoubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        # Click on blank space inside the table deselects the row.
        _install_empty_area_deselect(self._table)
        lay.addWidget(self._table, 1)

    def set_entries(self, entries):
        """Replace table contents with *entries*.

        Short-circuits when the visible-field signature matches the last
        successful build - the common tab-switch-without-changes case.
        Subclasses provide `_signature()` over the fields they actually
        render so the check reflects exactly what `_fill_row` reads.
        """
        entries_list = list(entries)
        sig = self._signature(entries_list)
        if sig == self._last_sig:
            # Keep the entries reference fresh (double-click + container
            # actions look it up by index) but skip the widget rebuild.
            self._entries = entries_list
            # No rebuild, but the no-WS coarse bars still need to advance
            # from the freshly-polled cache (they carry no live WS signal).
            self._refresh_live_progress()
            return

        # Save selection by pid before `setRowCount(0)` clears it.
        saved_pid = None
        cur_row = -1
        sm = self._table.selectionModel()
        if sm is not None:
            sel_rows = sm.selectedRows()
            if sel_rows:
                cur_row = sel_rows[0].row()
        if cur_row < 0:
            cur_row = self._table.currentRow()
        if 0 <= cur_row < len(self._entries):
            saved_pid = self._entries[cur_row].get('prompt_id')

        self._table.setRowCount(0)
        self._poll_progress_cells = {}  # rebuilt by _fill_row below
        self._entries = entries_list
        for entry in self._entries:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setRowHeight(row, 26)
            _fill_myjobs_cells(self._table, row, entry)
            self._fill_row(row, entry)
        self._last_sig = sig

        if saved_pid:
            for i, e in enumerate(self._entries):
                if e.get('prompt_id') == saved_pid:
                    self._table.selectRow(i)
                    self._table.scrollTo(self._table.model().index(i, 0))
                    break

    def _signature(self, entries):
        """Return a tuple of the fields that drive rendering.

        Any change in the returned value forces a rebuild.
        """
        raise NotImplementedError

    def _fill_row(self, row, entry):
        """Hook - subclasses populate col 7+ (divergent section)."""
        raise NotImplementedError

    def _refresh_live_progress(self):
        """Hook - advance in-place live progress widgets when the row set is
        unchanged. No-op for tables without poll-fed bars (e.g. History)."""
        pass

    def _base_sig_fields(self, entry):
        """Shared 7-column signature tuple - mirrors `_fill_myjobs_cells`.
        Subclass `_signature` extends this with its divergent columns."""
        return (
            entry.get('prompt_id'),
            entry.get('nfy_status_str'),
            entry.get('nfy_job_id'),
            entry.get('nfy_sent_at'),
            entry.get('nfy_machine_name'),
            entry.get('nfy_machine_url'),
            entry.get('nfy_workflow_name'),
            entry.get('nfy_node_name'),
            entry.get('nfy_nk_file'),
        )

    def _on_double_click(self, row, _col):
        if row < 0 or row >= len(self._entries):
            return
        self._container.show_detail(self._entries[row])

    def _on_context_menu(self, pos):
        """Subclass hook - dispatch the right-click menu for the row
        under *pos*. Subclasses provide the correct `kind` token so the
        shared builder picks the matching voice set."""
        raise NotImplementedError

    def remove_entry_row(self, prompt_id):
        """In-place row removal - used after a self-initiated delete so
        we don't rebuild the whole table from disk."""
        idx = next((i for i, e in enumerate(self._entries)
                    if e.get('prompt_id') == prompt_id), -1)
        if idx >= 0:
            self._table.removeRow(idx)
            self._entries.pop(idx)
            # Invalidate the cache so the next `set_entries` rebuilds
            # cleanly (the removed pid is no longer in the new input).
            self._last_sig = None
            return True
        return False



class _MyJobsActive(_MyJobsTableBase):
    """Jobs still running or pending. Progress + Abort/Remove actions.

    The Actions cell variant is picked from live state resolved by the
    container (`_MyJobsWidget._state_for_entry`):
        running  -> red Abort       (interrupt)
        pending  -> orange Remove   (dequeue)
        unknown  -> grey "?"        (machine unreachable popup on click)
    """

    _HEADERS = _MJA_HEADERS
    _WIDTHS = _MJA_WIDTHS
    _FIXED_COLS = frozenset({_MJ_COL_STATUS, _MJ_COL_ACTIONS})
    _UI_KEY = 'myjobs_active_table_v1'

    def _pixel_widths(self):
        # Single icon button (abort/remove) - 22px + 3 (1 left + 2 right;
        # right side absorbs 1px gridline overlap to keep visible 1+1).
        return {_MJ_COL_ACTIONS: 22 + 3}

    def _signature(self, entries):
        # `_fill_row` routes on the resolved state_kind (running / pending
        # / checking / not_in_queue / unreachable) rather than the raw
        # `live_state`, so we resolve each entry here to catch machine
        # online/offline transitions. Pending-action kind is included
        # because the greyed "Aborting…/Removing…" variant is a panel-
        # level override that's invisible in the entry dict itself.
        panel = self._container._panel
        pending = panel._pending_actions if panel is not None else {}
        ep = panel._progress_endpoint_ok if panel is not None else {}
        rows = []
        for e in entries:
            state_kind, _url = self._container._state_for_entry(e)
            pending_action = pending.get(e.get('prompt_id')) or {}
            # Endpoint availability rides the sig so a Suite gaining/losing
            # the progress route reselects the right widget (coarse bar vs
            # hatched) for each running row on the next tick.
            rows.append(
                self._base_sig_fields(e)
                + (state_kind, pending_action.get('kind'), ep.get(_url, True))
            )
        return tuple(rows)

    def _fill_row(self, row, entry):
        state_kind, _url = self._container._state_for_entry(entry)
        # "Aborting…" overlays a greyed look from two sources: the optimistic
        # click bridge (`_pending_actions`, sub-second) and the server-side
        # abort flag (state_kind == 'aborting', which survives a panel
        # reopen). "Removing…" is optimistic-only. Both grey the row +
        # disable the button until the server-side state takes over.
        pid = entry.get('prompt_id', '')
        pending_action = None
        panel = self._container._panel
        if panel is not None and pid:
            pending_action = panel._pending_actions.get(pid)
        pa_kind = pending_action.get('kind', '') if pending_action else ''
        # The optimistic override yields to the offline state: when the
        # machine is unreachable we can't confirm the abort/remove, so the
        # row reads "? Unknown" instead of a stale "Aborting…"/"Removing…".
        reachable = state_kind != 'unreachable'
        show_aborting = (state_kind == 'aborting'
                         or (pa_kind == 'abort' and reachable))
        show_removing = pa_kind == 'remove' and reachable
        in_flight = show_aborting or show_removing

        # Override Status cell to reflect live state instead of the
        # submit-history `status_str` (which is empty until reconcile).
        # Maps MyJobs Active state -> JOB_STATUS key for consistent
        # colour/icon treatment with per-machine Queue.
        state_to_status = {
            'running': 'running',
            'pending': 'pending',
            'checking': '',         # _FALLBACK -> "Unknown"
            'not_in_queue': '',     # _FALLBACK -> "Unknown"
            'unreachable': 'unknown',
        }
        status_key = state_to_status.get(state_kind, '')
        label, color, icon_char = render_job_status(status_key)
        if show_aborting:
            label, color, icon_char = ABORTING_LABEL, INFLIGHT_COLOR, ''
        elif show_removing:
            label, color, icon_char = REMOVING_LABEL, INFLIGHT_COLOR, ''
        self._table.setCellWidget(
            row, _MJ_COL_STATUS,
            _make_status_cell(icon_char, label, color))

        # Progress widget - mirrors the per-machine Queue: which one appears
        # depends on whether progress DATA is reachable, not just the local
        # websocket-client package. Bars seed from the shared cache so a
        # value survives a restart / second viewer / rebuild.
        cached = None
        if panel is not None and pid:
            cached = panel._live_progress.get((_url, pid))
        cached_frac = cached.get('fraction') if cached else None
        cached_tip = cached.get('tooltip', '') if cached else ''
        endpoint_ok = True
        if panel is not None:
            endpoint_ok = panel._progress_endpoint_ok.get(_url, True)
        ws_usable = (state_kind == 'running' and ws_client.AVAILABLE
                     and (endpoint_ok or cached_frac is not None))
        if in_flight:
            # Greyed flat bar while aborting/removing - don't show live
            # progress once the job is being torn down.
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setEnabled(False)
        elif ws_usable:
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(max(0.0, min(1.0, cached_frac or 0.0)) * 100))
            bar.setTextVisible(True)
            bar.setFormat('%p%')
            bar.setAlignment(QtCore.Qt.AlignCenter)
            if cached_tip:
                bar.setToolTip(cached_tip)
            if pid:
                self._container._progress_bars[pid] = bar
        elif state_kind == 'pending':
            # Flat 0% grey bar - hatched pattern is reserved for
            # "no live data / unknown" states, not queued jobs.
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
        elif state_kind == 'running' and (cached_frac is not None or endpoint_ok):
            # Polled value, no live WS: real bar with a diagonally hatched
            # fill ("polled, not live"); refreshed in place each tick.
            bar = _HatchedFillProgressCell(
                fraction=cached_frac or 0.0,
                tooltip=(_coarse_progress_tooltip(cached_tip)
                         if not ws_client.AVAILABLE else (cached_tip or None)))
            if pid:
                self._poll_progress_cells[pid] = (_url, bar)
        else:
            tip = (_WS_MISSING_TOOLTIP
                   if state_kind == 'running' and not ws_client.AVAILABLE
                   else None)
            bar = _HatchedProgressCell(tooltip=tip)
        self._table.setCellWidget(row, _MJ_COL_PROGDUR, bar)

        # Actions - variant by state (mirrors per-machine Queue styling).
        if in_flight:
            # Greyed + disabled while aborting/removing.
            btn_char, c, hc, hbg = (CLOSE if show_aborting else REMOVE,
                                    '#555', '#555', '#2a2a2a')
            tooltip = ABORTING_LABEL if show_aborting else REMOVING_LABEL
            enabled = False
        elif state_kind == 'running':
            btn_char, c, hc, hbg = (CLOSE, ERROR_COLOR, ERROR_HOVER, '#3a2020')
            tooltip = 'Abort this job'
            enabled = True
        elif state_kind == 'pending':
            btn_char, c, hc, hbg = (REMOVE, WARNING_STATUS, WARNING_STATUS_HOVER, '#3a2f1a')
            tooltip = 'Remove from queue'
            enabled = True
        elif state_kind in ('checking', 'not_in_queue'):
            # No live data yet / transient post-complete window -
            # disable so the user can't accidentally freeze an entry
            # whose server state we haven't confirmed.
            btn_char, c, hc, hbg = (CLOSE, '#555', '#555', '#2a2a2a')
            tooltip = ('Verifying state\u2026' if state_kind == 'checking'
                       else 'Job not in the active queue. Refreshing…')
            enabled = False
        else:  # unreachable
            # Machine offline / unreachable: server state not
            # confirmed. Allow the user to manually purge the local
            # entry - the confirmation dialog warns that the render
            # may still be running on the server. X icon in grey:
            # not a server abort (would be red), not a queue
            # removal (would be orange `-`), but a local-only
            # delete with explicit confirmation.
            btn_char, c, hc, hbg = (CLOSE, '#9e9e9e', '#bdbdbd', '#3a3a3a')
            tooltip = 'Remove from local history (server status not confirmed)'
            enabled = True
        btn = QtWidgets.QPushButton(btn_char)
        btn.setFont(icon_font(14))
        btn.setFixedSize(22, 22)
        btn.setToolTip(tooltip)
        btn.setEnabled(enabled)
        btn.setStyleSheet(cell_action_colored(c, hc, hbg))
        if enabled:
            btn.clicked.connect(
                lambda _=False, e=entry:
                    self._container._on_active_action(e))
        self._table.setCellWidget(row, _MJ_COL_ACTIONS, _centered_cell(btn))

        # Dim the text cells of the row while an abort/remove is in flight,
        # so the whole row reads as "something's happening, wait".
        if in_flight:
            dim_brush = QtGui.QBrush(QtGui.QColor('#606060'))
            for col in (_MJ_COL_JOB, _MJ_COL_SENT, _MJ_COL_MACHINE,
                        _MJ_COL_WORKFLOW, _MJ_COL_NODE, _MJ_COL_NKFILE):
                it = self._table.item(row, col)
                if it is not None:
                    it.setForeground(dim_brush)

    def _refresh_live_progress(self):
        """Advance the no-WS coarse bars in place from the shared cache.
        Active spans machines, so each cell carries its own machine_url."""
        panel = self._container._panel
        if panel is None or not self._poll_progress_cells:
            return
        for pid, (url, cell) in list(self._poll_progress_cells.items()):
            data = panel._live_progress.get((url, pid))
            if not data:
                continue
            try:
                cell.set_fraction(data.get('fraction', 0.0))
                node_tip = data.get('tooltip', '')
                cell.setToolTip(_coarse_progress_tooltip(node_tip)
                                if not ws_client.AVAILABLE
                                else (node_tip or ''))
            except RuntimeError:
                self._poll_progress_cells.pop(pid, None)

    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._entries):
            return
        show_job_context_menu(
            self,
            self._table.viewport().mapToGlobal(pos),
            self._entries[row],
            kind='myjobs_active',
            panel=self._container._panel,
            container=self._container)



class _MyJobsHistory(_MyJobsTableBase):
    """Jobs in terminal state. Duration + Log/Read icons + Delete."""

    _HEADERS = _MJH_HEADERS
    _WIDTHS = _MJH_WIDTHS
    _FIXED_COLS = frozenset({_MJ_COL_STATUS, _MJ_COL_ACTIONS,
                             _MJH_COL_DELETE})
    _UI_KEY = 'myjobs_history_table_v1'

    def _pixel_widths(self):
        # Actions: 2 buttons (read+log) at 22px + 4 spacing + 3 (1 left
        #          + 2 right, right absorbs gridline overlap).
        # Delete:  single 22px + 3 (1 left + 2 right).
        return {
            _MJ_COL_ACTIONS: 22 * 2 + 7,
            _MJH_COL_DELETE: 22 + 3,
        }

    def _signature(self, entries):
        # History rows are frozen: only `status_str` / `duration` /
        # outputs availability / `read_color` drive the render. Cheap
        # tuple keeps unchanged-data tab switches O(N) comparison vs
        # O(N) widget rebuild.
        return tuple(
            self._base_sig_fields(e) + (
                e.get('nfy_duration'),
                bool(e.get('nfy_output_paths')),
                int(e.get('nfy_read_color', 0) or 0),
                bool(e.get('nfy_terminal_persisted')),
            )
            for e in entries
        )

    def _fill_row(self, row, entry):
        # Duration
        dur_item = QtWidgets.QTableWidgetItem(
            _format_duration(entry.get('nfy_duration', 0)))
        dur_item.setTextAlignment(QtCore.Qt.AlignCenter)
        self._table.setItem(row, _MJ_COL_PROGDUR, dur_item)

        # Actions - two compact icon buttons: Read Outputs + Log.
        # Stretch factors 1:2 compensate for QTableWidget's 1px right-edge
        # gridline that would otherwise consume the right breathing space.
        actions_widget = QtWidgets.QWidget()
        actions_lay = QtWidgets.QHBoxLayout(actions_widget)
        actions_lay.setContentsMargins(0, 0, 0, 0)
        actions_lay.setSpacing(4)
        actions_lay.addStretch(1)

        log_btn = QtWidgets.QPushButton(DESCRIPTION)
        log_btn.setFont(icon_font(14))
        log_btn.setFixedSize(22, 22)
        log_btn.setToolTip('View execution log')
        log_btn.setStyleSheet(BUTTON_STYLE_CELL_ACTION)
        log_btn.clicked.connect(
            lambda _=False, e=entry: self._container.show_log(e))

        outputs = entry.get('nfy_output_paths', [])
        status_str = (entry.get('nfy_status_str') or '').lower()
        can_read = bool(outputs) and status_str not in ('cancelled', 'failed')
        try:
            read_color = int(entry.get('nfy_read_color', 0) or 0)
        except (ValueError, TypeError):
            read_color = 0
        read_btn = QtWidgets.QPushButton(FILE_DOWNLOAD)
        read_btn.setFont(icon_font(14))
        read_btn.setFixedSize(22, 22)
        if can_read:
            read_btn.setToolTip(
                'Read Output(s): create Read nodes for these outputs')
            # Green matches the SUCCESS_COLOR - shared palette for
            # "available / successful" affordances across the panel.
            read_btn.setStyleSheet(
                cell_action_colored(SUCCESS_COLOR, SUCCESS_HOVER, '#2a3a2a'))
            read_btn.clicked.connect(
                lambda _=False, paths=outputs, clr=read_color:
                    self._container.retrieve(paths, clr))
        else:
            read_btn.setEnabled(False)
            read_btn.setToolTip('No outputs available for this job')
            read_btn.setStyleSheet(BUTTON_STYLE_CELL_ACTION)
        actions_lay.addWidget(read_btn)
        actions_lay.addWidget(log_btn)
        actions_lay.addStretch(2)

        self._table.setCellWidget(row, _MJ_COL_ACTIONS, actions_widget)

        # Delete - trash icon, semantically clearer than × (which we use for
        # abort/close across the panel).
        pid = entry.get('prompt_id', '')
        del_btn = QtWidgets.QPushButton(DELETE)
        del_btn.setFont(icon_font(14))
        del_btn.setFixedSize(22, 22)
        del_btn.setToolTip('Remove this entry from your job history')
        del_btn.setStyleSheet(
            cell_action_colored(ERROR_COLOR, ERROR_HOVER, '#3a2020'))
        del_btn.clicked.connect(
            lambda _=False, prompt_id=pid:
                self._container.delete_entry(prompt_id))
        self._table.setCellWidget(row, _MJH_COL_DELETE, _centered_cell(del_btn))

    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._entries):
            return
        show_job_context_menu(
            self,
            self._table.viewport().mapToGlobal(pos),
            self._entries[row],
            kind='myjobs_history',
            panel=self._container._panel,
            container=self._container)


class _MyJobsWidget(QtWidgets.QWidget):
    """Container orchestrating Active + History via a vertical dotted
    splitter. Public API preserved for the panel: `_reload()`,
    `_ignore_next_file_change`.
    """

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel
        # Self-initiated writes (delete/clear) skip the next
        # fileChanged-driven reload so we don't rebuild the whole pair
        # of tables right after an in-place row removal.
        self._ignore_next_file_change = False

        # {prompt_id: QProgressBar} for live WS updates on Active rows.
        # Populated by `_MyJobsActive._fill_row` when a row renders in
        # 'running' state. Stale refs are naturally shed on each rebuild.
        self._progress_bars = {}
        # {machine_url: ProgressMonitor} - one WS monitor per machine
        # referenced by an Active entry. Attached lazily in
        # `_ensure_ws_monitors` after each reload.
        self._ws_monitors = {}

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Full History list - the search + status filter live on the History
        # toolbar below and filter ONLY this (Active is always shown).
        self._all_history = []

        # Vertical splitter - layout mirrors Submit panel exactly so the
        # dotted handle (which has a fixed +3px groupbox-gap compensation)
        # sits centred between the two panes. Each pane is a plain QWidget
        # with zero margins containing a styled GroupBox inside.
        self._splitter = DottedSplitter(QtCore.Qt.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(16)

        self._active = _MyJobsActive(self)
        self._history = _MyJobsHistory(self)

        # --- Active pane ---
        active_pane = QtWidgets.QWidget()
        active_pane_lay = QtWidgets.QVBoxLayout(active_pane)
        active_pane_lay.setContentsMargins(0, 0, 0, 0)

        active_grp = QtWidgets.QGroupBox('Active')
        active_grp.setStyleSheet(_MJ_GROUPBOX_STYLE)
        active_inner = QtWidgets.QVBoxLayout(active_grp)
        active_inner.setContentsMargins(10, 8, 10, 10)
        active_inner.setSpacing(8)

        # Active toolbar - hosts the MyJobs-side twin of the Machines
        # tab's Update All button. Both buttons share one AutoRefreshTimer
        # so the countdown (and in-flight 'Updating\u2026' label) stays in
        # sync across tabs (one timer, one trigger).
        active_tb = QtWidgets.QHBoxLayout()
        active_tb.addStretch()
        self._refresh_btn = QtWidgets.QPushButton('Update All')
        set_press_icon(self._refresh_btn, REFRESH)
        self._refresh_btn.setFixedHeight(24)
        self._refresh_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self._refresh_btn.setToolTip('Update all machines now')
        self._refresh_btn.clicked.connect(self._trigger_panel_refresh)
        active_tb.addWidget(self._refresh_btn)
        active_inner.addLayout(active_tb)
        active_inner.addWidget(self._active)
        active_pane_lay.addWidget(active_grp)

        # --- History pane ---
        history_pane = QtWidgets.QWidget()
        history_pane_lay = QtWidgets.QVBoxLayout(history_pane)
        history_pane_lay.setContentsMargins(0, 0, 0, 0)

        history_grp = QtWidgets.QGroupBox('Local Job History')
        history_grp.setStyleSheet(_MJ_GROUPBOX_STYLE)
        history_inner = QtWidgets.QVBoxLayout(history_grp)
        history_inner.setContentsMargins(10, 8, 10, 10)
        history_inner.setSpacing(8)

        # Clear History toolbar - scoped to History only. Active rows are
        # server-live and cannot be "cleared" locally (that would drop
        # the record without actually aborting the job).
        hist_tb = QtWidgets.QHBoxLayout()
        hist_tb.setSpacing(8)
        # Search + status filter + count - scoped to History ONLY, on the same
        # row as Clear so it's unambiguous they filter this table (not Active).
        self._search = NukomfyLineEdit()
        self._search.setPlaceholderText('Search workflow, user, node, job id…')
        self._search.setClearButtonEnabled(True)
        self._search.addAction(material_icon(SEARCH, '#666', 14),
                               QtWidgets.QLineEdit.LeadingPosition)
        self._search.setStyleSheet(SEARCH_FIELD_STYLE)
        self._search_debounce = QtCore.QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(200)
        self._search_debounce.timeout.connect(self._apply_history_filter)
        self._search.textChanged.connect(
            lambda _=None: self._search_debounce.start())
        self._mj_filter = StatusFilterButton(
            JOB_STATUS_FILTERS, self._apply_history_filter,
            state_key='myjobs_filter')
        self._hist_count_lbl = QtWidgets.QLabel('')
        self._hist_count_lbl.setStyleSheet('color:#a0a0a0;')
        # Reserve space for a 4-digit count so the row never shifts as N changes.
        self._hist_count_lbl.setFixedWidth(
            self._hist_count_lbl.fontMetrics().horizontalAdvance('8888 jobs') + 6)
        self._hist_count_lbl.setAlignment(QtCore.Qt.AlignCenter)
        hist_tb.addWidget(self._search, 1)
        hist_tb.addWidget(self._mj_filter)
        hist_tb.addWidget(self._hist_count_lbl)
        hist_tb.addSpacing(8)
        clear_btn = QtWidgets.QPushButton('Clear Local History')
        set_press_icon(clear_btn, CLOSE)
        clear_btn.setFixedHeight(24)
        clear_btn.setToolTip('Remove all finished entries from your history')
        clear_btn.clicked.connect(self._clear_history)
        hist_tb.addWidget(clear_btn)
        history_inner.addLayout(hist_tb)
        history_inner.addWidget(self._history)
        history_pane_lay.addWidget(history_grp)

        self._splitter.addWidget(active_pane)
        self._splitter.addWidget(history_pane)
        # Default: equal 50/50 split. Qt redistributes [1, 1] proportionally
        # to fill the actual height available at show time.
        self._splitter.setSizes([1, 1])
        lay.addWidget(self._splitter, 1)

        # Restore persisted split sizes
        saved = ui_state.get('myjobs_splitter').get('splitter_sizes')
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            try:
                self._splitter.setSizes([int(saved[0]), int(saved[1])])
            except (TypeError, ValueError):
                pass

        self._reload()

        # Primary wake-up is the central store: every UnifiedFetchWorker
        # tick emits `storeChanged` which collapses Active/History views
        # into a single reload per cycle.
        store = getattr(panel, '_store', None) if panel is not None else None
        if store is not None:
            store.storeChanged.connect(self._reload)

        # QFileSystemWatcher stays as a safety net for external writers
        # (other Nuke instances, manual edits). In the normal flow the
        # storeChanged reload gets there first.
        import Nukomfy.data.submit_history as submit_history
        # Storage uses SQLite WAL. Touch the singleton connection so the
        # .db-wal/.db-shm siblings exist before we add the watch.
        try:
            import Nukomfy.data.db as _db
            _db._connect()
        except Exception:
            pass
        self._history_path = submit_history.history_path()
        self._watcher = QtCore.QFileSystemWatcher(self)
        if os.path.isfile(self._history_path):
            self._watcher.addPath(self._history_path)
        # Under WAL the actual writes go into .db-wal until checkpoint -
        # watch it too or we miss the change events.
        wal_path = self._history_path + '-wal'
        if os.path.isfile(wal_path):
            self._watcher.addPath(wal_path)
        self._watcher.fileChanged.connect(self._on_file_changed)

    # ------------------------------------------------------------------
    # Lifecycle / reload
    # ------------------------------------------------------------------
    def _on_file_changed(self, path):
        if self._ignore_next_file_change:
            self._ignore_next_file_change = False
        else:
            self._reload()
        # QFileSystemWatcher drops the path after a rewrite - re-add it
        if not self._watcher.files():
            self._watcher.addPath(path)

    def showEvent(self, event):
        super().showEvent(event)
        self._reload()

    def hideEvent(self, event):
        # Persist splitter sizes when the tab is hidden (dialog close or
        # tab switch). Cheap and avoids a full closeEvent wiring.
        try:
            sizes = self._splitter.sizes()
            if sizes and all(s > 0 for s in sizes):
                ui_state.set('myjobs_splitter', splitter_sizes=list(sizes))
        except Exception:
            pass
        super().hideEvent(event)

    def _reload(self):
        """Refresh both sub-tables from the central store.

        Entries come from `RenderDataStore.jobs_for_user(me)`, which
        overlays the live queue onto the local submit_history cache and
        tags each entry with `live_state` in one of:
        running / pending / awaiting / completed / failed / cancelled /
        unknown. Active pane shows the first three; History pane shows
        the rest.

        Filter by current OS user (nfy_submitted_by) so the same person
        submitting from multiple hosts sees their full history. Active rows preserve `sent_at` desc ordering - fixed at
        submit time, never reshuffled by live state transitions.
        """
        from Nukomfy.data.submit_history import refresh_ranges_from_disk
        # Single disk read shared between the range-resolver and the
        # store's cache hydration - cuts UI-thread I/O in half on every
        # tab switch. Falls back to a fresh `get_history()` inside the
        # store if the range pass raised (rare; glob failures don't).
        fresh_entries = None
        try:
            fresh_entries = refresh_ranges_from_disk()
        except Exception:
            pass
        me = current_user()
        store = getattr(self._panel, '_store', None) if self._panel else None
        if store is not None:
            # Sync the store's local-history cache before reading:
            # QFileSystemWatcher fires on external writes (other Nuke
            # instances, manual edits) and the store wouldn't otherwise know
            # to refresh. Callers that mutate history themselves already
            # invoke `store.load_local_history()` explicitly.
            try:
                store.load_local_history(entries=fresh_entries)
            except Exception:
                pass
            entries = store.jobs_for_user(me)
            # Split by `terminal_persisted` (authoritative): an entry is
            # in History only when the server has definitively closed it
            # (completed/failed/cancelled, or persist_as_lost -> failed).
            # `live_state` is not authoritative here: `unknown` for offline
            # machines (transient, not terminal) must not fall through to
            # History, which would hide live jobs whose machine briefly
            # disconnected.
            active = [e for e in entries
                      if not e.get('nfy_terminal_persisted')]
            history = [e for e in entries
                       if e.get('nfy_terminal_persisted')]
        else:
            # Safety fallback - shouldn't hit in production since the panel
            # always constructs the store before building MyJobs.
            from Nukomfy.data.submit_history import get_history
            entries = [
                e for e in get_history()
                if e.get('nfy_submitted_by') == me
            ]
            active = [e for e in entries if not e.get('nfy_terminal_persisted')]
            history = [e for e in entries if e.get('nfy_terminal_persisted')]
        active.sort(key=self._active_sort_key)
        self._active.set_entries(active)
        self._all_history = history
        self._apply_history_filter()
        self._ensure_ws_monitors(active)

    def _apply_history_filter(self):
        """Re-filter the History sub-table by the search text + status filter
        (client-side, History only - Active is always shown, so one control
        never ambiguously filters two tables). Updates the job counter."""
        q = self._search.text().strip().lower()
        sel = self._mj_filter.selected()
        sset = set(sel) if sel else None

        def keep(e):
            if sset and (e.get('nfy_status_str') or '').lower() not in sset:
                return False
            if q:
                fields = (e.get('nfy_workflow_name'), e.get('nfy_submitted_by'),
                          e.get('nfy_node_name'), e.get('nfy_nk_file'),
                          e.get('nfy_job_id'), e.get('prompt_id'))
                if not any(q in str(f).lower() for f in fields if f):
                    return False
            return True

        shown = [e for e in self._all_history if keep(e)]
        self._history.set_entries(shown)
        self._hist_count_lbl.setText(
            '1 job' if len(shown) == 1 else '{} jobs'.format(len(shown)))

    _ACTIVE_STATE_RANK = {'running': 0, 'pending': 1, 'awaiting': 2,
                          'unknown': 3}

    def _active_sort_key(self, entry):
        """Sort key mirroring per-machine Queue ordering.

        Rank by live state (running -> pending -> awaiting), then by
        `sent_at` ascending within each rank, finally by `queue_position`
        (server-side monotonic submit counter) so a batch submitted in
        the same instant - identical `sent_at` - preserves server FIFO
        instead of falling back to Python's input order on ties.
        """
        rank = self._ACTIVE_STATE_RANK.get(entry.get('live_state', ''), 3)
        return (rank,
                entry.get('nfy_sent_at', ''),
                entry.get('queue_position', 0))

    # ------------------------------------------------------------------
    # Actions delegated from sub-widgets
    # ------------------------------------------------------------------
    def show_detail(self, entry):
        """Open the unified Job dialog on the Detail tab for *entry*."""
        self._panel.show_job_dialog(
            self._prepare_dialog_entry(entry), initial_tab='detail')

    def show_log(self, entry):
        """Open the unified Job dialog on the Log tab for *entry*."""
        self._panel.show_job_dialog(
            self._prepare_dialog_entry(entry), initial_tab='log')

    def _prepare_dialog_entry(self, entry):
        """Normalise *entry* for the shared Job dialog.

        Active rows have no `status_str` yet in submit_history (it only
        lands at terminal-persist time), so the dialog header would fall
        back to "?". Resolve the live state and inject it so
        running/pending/unknown show with their correct label+colour.
        """
        e = _entry_for_log(entry)
        if not e.get('nfy_status_str'):
            state_kind, _url = self._state_for_entry(entry)
            # Base live status only. The in-flight overlay (Aborting…/
            # Removing…) is applied centrally in the Job dialog's populate(),
            # so it covers every view and the dialog's own Refresh button.
            e['nfy_status_str'] = {
                'running': 'running',
                'aborting': 'running',
                'pending': 'pending',
                'unreachable': 'unknown',
            }.get(state_kind, '')
        return e

    def retrieve(self, output_paths, color=0):
        """Create Nuke Read nodes for the given output paths (warning popups
        parented here so they don't slip behind the dialog on dismiss)."""
        read_outputs(self, output_paths, color)

    def delete_entry(self, prompt_id):
        """Remove a single entry from history (with confirmation).

        Self-initiated write -> suppress the watcher-driven reload and
        update the sub-table in-place so other widgets sharing refresh
        cycles don't get rebuilt alongside us.
        """
        # Try both sub-tables to find the entry ref for the confirmation
        entry = next((e for e in self._active._entries
                      if e.get('prompt_id') == prompt_id), None) \
            or next((e for e in self._history._entries
                     if e.get('prompt_id') == prompt_id), None)
        job_ref = (entry.get('nfy_job_id') if entry else None) \
            or prompt_id or '?'
        ans = _dialogs.ask(
            self, 'Remove job?',
            'Remove job {} from your job history?'.format(job_ref),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        btn = self.focusWidget()
        if btn:
            btn.clearFocus()
        if ans != QtWidgets.QMessageBox.Yes:
            return
        from Nukomfy.data.submit_history import delete_entry
        self._ignore_next_file_change = True
        delete_entry(prompt_id)
        removed = self._active.remove_entry_row(prompt_id) \
            or self._history.remove_entry_row(prompt_id)
        if not removed:
            self._ignore_next_file_change = False
            self._reload()

    # ------------------------------------------------------------------
    # Active-row state resolution + Abort/Remove dispatch
    # ------------------------------------------------------------------
    def _state_for_entry(self, entry):
        """Resolve live queue state for an Active MyJobs entry.

        Prefers the `live_state` annotation attached by
        `RenderDataStore.jobs_for_user` (set on every reload).
        `awaiting` entries are then resolved against the store's
        per-machine snapshot to distinguish:
            'checking'    - machine not yet fetched this session
            'unreachable' - machine confirmed offline or unknown
            'not_in_queue'- machine online but prompt_id absent

        A running job flagged `nfy_aborting` resolves to 'aborting'.

        Returns (kind, machine_url).
        """
        url = entry.get('nfy_machine_url', '')
        pid = entry.get('prompt_id')
        live = entry.get('live_state')
        if live == 'running':
            # Server-side abort flag on the live job: the job is still
            # running while the server honours the interrupt. Rides
            # /api/queue, so it survives a panel reopen / Nuke restart.
            live_job = entry.get('live_job') or {}
            if live_job.get('nfy_aborting'):
                return ('aborting', url)
            return ('running', url)
        if live == 'pending':
            return ('pending', url)
        if not pid:
            return ('checking', url)
        panel = self._panel
        store = getattr(panel, '_store', None) if panel is not None else None
        if store is None:
            return ('checking', url)
        from Nukomfy.client.machines import machine_manager
        m = next((mc for mc in machine_manager.machines
                  if mc.url == url), None)
        if m is None:
            return ('unreachable', url)
        info = store.machine_info(url)
        if info is None:
            return ('checking', url)
        if info.get('status') == 'offline':
            return ('unreachable', url)
        return ('not_in_queue', url)

    def _on_active_action(self, entry):
        """Route the Active row's Abort/Remove click based on live state.

        Three actionable kinds:
          - running   -> server-side abort via `_abort_or_remove_entry`
          - pending   -> server-side remove from queue
          - unreachable -> local-only delete from `submit_history.json`
            after explicit confirmation (the render may still be
            running on the server but we have no live link to it).

        `checking`, `not_in_queue`, and `aborting` render a disabled
        button, so this slot never fires for them.
        """
        state_kind, url = self._state_for_entry(entry)
        if state_kind in ('running', 'pending'):
            _abort_or_remove_entry(
                self, self._panel, entry,
                is_running=(state_kind == 'running'),
                machine_url=url)
        elif state_kind == 'unreachable':
            self._on_active_remove_unconfirmed(entry)

    def _on_active_remove_unconfirmed(self, entry):
        """Manual delete of a MyJobs Active entry whose server state
        couldn't be confirmed (machine offline or unreachable).

        Shows a warning dialog explaining that the local record will
        be deleted permanently while the render may still be alive
        on the server. If the machine later returns online with this
        job still in its queue, the orphan re-add path in
        `_on_result` brings the entry back automatically.
        """
        pid = entry.get('prompt_id')
        if not pid:
            return
        msg = _dialogs.message_box(self._panel)
        msg.setIcon(QtWidgets.QMessageBox.Warning)
        msg.setWindowTitle('Remove unconfirmed entry?')
        msg.setText(
            'The status of this render has not been confirmed '
            'with its machine (offline or unreachable).')
        msg.setInformativeText(
            'The render may still be running on the server.\n\n'
            'Removing this entry deletes it from local history '
            'permanently.\n\n'
            'If the machine comes back online and the '
            'job is still in its queue, the entry is re-added '
            'automatically.\n\nContinue?')
        yes = msg.addButton('Remove', QtWidgets.QMessageBox.AcceptRole)
        msg.addButton('Cancel', QtWidgets.QMessageBox.RejectRole)
        msg.setDefaultButton(yes)
        msg.exec_()
        if msg.clickedButton() is not yes:
            return
        try:
            submit_history.delete_entry(pid)
        except Exception as e:
            _log.exception(
                'Local history delete failed for %s', fmt_job(pid))
            _dialogs.warn(
                self._panel, 'Could not remove entry',
                'This entry could not be removed from local history:'
                '\n\n{}'.format(e))
            return
        if self._panel is not None:
            self._panel._store.load_local_history()
            self._panel._store.notify()

    # ------------------------------------------------------------------
    # Live progress (WebSocket)
    # ------------------------------------------------------------------
    def _ensure_ws_monitors(self, active_entries):
        """Attach one ProgressMonitor per unique machine_url in Active.

        Monitors are singletons managed by `ws_client.get_manager()`, so
        attaching twice is a no-op on the manager side. We keep a local
        ref+connection per url so we can disconnect when the widget is
        torn down (`closeEvent`-style via destroyed signal).
        """
        if not ws_client.AVAILABLE:
            return
        wanted = {e.get('nfy_machine_url') for e in active_entries
                  if e.get('nfy_machine_url')}
        for url in wanted:
            if url in self._ws_monitors:
                continue
            mon = ws_client.get_manager().monitor_for(url, ws_session_id())
            if mon is None:
                continue
            mon.progress.connect(self._on_ws_progress)
            mon.lifecycle.connect(self._on_ws_lifecycle)
            self._ws_monitors[url] = mon

    def _on_ws_progress(self, prompt_id, fraction, tooltip_text):
        """Update the Active-row progress bar for *prompt_id*, if any.

        Signature matches `ProgressMonitor.progress` - same slot shape
        as `_JobDetailWidget._on_ws_progress`. Safe against stale refs
        left over from a prior rebuild (caught via RuntimeError).
        """
        bar = self._progress_bars.get(prompt_id)
        if bar is None:
            return
        try:
            pct = max(0, min(100, int(fraction * 100)))
            bar.setValue(max(bar.value(), pct))
            bar.setFormat('%p%')
            bar.setToolTip(tooltip_text or '')
            if tooltip_text and QtWidgets.QToolTip.isVisible() \
                    and bar.underMouse():
                QtWidgets.QToolTip.showText(
                    QtGui.QCursor.pos(), tooltip_text, bar)
        except RuntimeError:
            self._progress_bars.pop(prompt_id, None)

    def _on_ws_lifecycle(self, prompt_id, event):
        """Terminal WS event -> refresh the panel so the row migrates.

        The panel's reconciler will update submit_history and our
        fileChanged watcher fires the reload, but proactively nudging
        the panel refresh makes the transition feel snappy.
        """
        if event in ('success', 'error', 'interrupted'):
            self._progress_bars.pop(prompt_id, None)
            panel = self._panel
            if panel is not None:
                try:
                    panel._refresh()
                except Exception:
                    pass

    def _trigger_panel_refresh(self):
        """Forward this tab's Update All click to the panel's shared
        refresh cycle. Both buttons fire the same `_refresh_manual`
        (cancel-restart) so there's never a duplicate fetch."""
        if self._panel is not None:
            try:
                self._panel._refresh_manual()
            except Exception:
                pass

    def _clear_history(self):
        """Clear only History rows (terminal_persisted entries). Active
        rows are server-live - dropping them locally would lose the job
        ref without actually aborting it."""
        if not self._history._entries:
            return
        ans = _dialogs.ask(
            self, 'Clear job history?',
            'Remove all {} entries from your job history?\n\n'
            'Active jobs (running/pending) will not be affected.'.format(
                len(self._history._entries)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        btn = self.focusWidget()
        if btn:
            btn.clearFocus()
        if ans != QtWidgets.QMessageBox.Yes:
            return
        from Nukomfy.data.submit_history import clear_terminal
        self._ignore_next_file_change = True
        clear_terminal()
        self._history.set_entries([])
