"""Machine job viewer: all server-side persisted jobs for one machine.

Opened from the Render Manager machine context menu. Mirrors the History
sub-table (all users, server-backed) in a standalone, server-paginated
window with text search and status filters. Pagination, search and the
status filter all run server-side (one page per request) so the window
stays light on machines with thousands of jobs.

Reuses the History sub-table machinery verbatim: `_fill_common_job_cells`
for the shared columns, `_setup_table_columns` for the layout/persistence,
the same context-menu item builders, and the same Read-outputs path the
MyJobs history offers.
"""
import logging

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, _nuke_main_window
from Nukomfy.gui._fields import NukomfyLineEdit
from Nukomfy.gui import _dialogs
from Nukomfy.core.settings import settings
from Nukomfy.client import manager_client
from Nukomfy.gui.workers import UnifiedFetchWorker, stop_worker
from Nukomfy.gui.icons import (
    icon_font, material_icon, SEARCH, REFRESH, FILE_DOWNLOAD, DESCRIPTION)
from Nukomfy.gui._theme import (
    DETAIL_STYLE, BUTTON_STYLE_CELL_ACTION, cell_action_colored,
    SEARCH_FIELD_STYLE, SUCCESS_COLOR, SUCCESS_HOVER, apply_window_chrome)
from Nukomfy.gui._no_wheel import NoWheelComboBox
from Nukomfy.gui._render_data_store import _sent_at_to_epoch
from Nukomfy.gui.ui_state import ui_state
from Nukomfy.gui.render_queue_context_menu import (
    _add_copy_job_id, _add_show_output_folder)
from Nukomfy.gui.render_queue_actions import read_outputs
from Nukomfy.gui._filter_button import StatusFilterButton, JOB_STATUS_FILTERS
from Nukomfy.gui.render_queue_format import (
    _fill_common_job_cells, _format_duration, _setup_table_columns,
    _LeftElideDelegate, _install_empty_area_deselect, _fit_dialog_to_content,
    _install_click_outside_deselect,
    _centered_cell, _SUB_COL_STATUS, _SUB_COL_NKFILE,
)

_log = logging.getLogger(__name__)

# Page-size choices match the Suite web viewer for plugin <-> web parity.
_PAGE_SIZES = [50, 100, 200]

# Fixed standard window size in LOGICAL pixels (Qt scales for DPI; never
# multiply by the device pixel ratio). Wide enough to show every column
# without eliding; the window stays freely resizable and maximizable.
_DEFAULT_W = 1280
_DEFAULT_H = 600

# ui_state key for the persisted status filter. One global key for the All
# Jobs viewer (separate from MyJobs' own key).
_FILTER_STATE_KEY = 'machine_jobs_filter'

# Columns: the 7 shared History cells (0-6) + Duration + Read + Log.
_V_HEADERS = ['Status', 'Job ID', 'Sent', 'User', 'Workflow', 'Node',
              'NK File', 'Duration', '', '']
_V_WIDTHS = {0: 100, 1: 70, 2: 160, 3: 100, 4: 180, 5: 150,
             6: 220, 7: 90, 8: 24, 9: 24}
_V_COL_DURATION = 7
_V_COL_READ = 8
_V_COL_LOG = 9


class MachineJobsViewer(QtWidgets.QDialog):
    """Standalone, server-paginated job list for a single machine."""

    def __init__(self, machine, panel, parent=None):
        super().__init__(parent)
        self._machine = machine
        self._panel = panel
        self._store = getattr(panel, '_store', None)
        if self._store is not None:
            # The store is a QObject child of the Render Manager and dies
            # with it, while this viewer survives the panel. A dead store
            # does NOT raise on its pure-Python methods - it silently
            # returns caches frozen at destruction time - so drop the
            # reference the moment the C++ half goes away.
            self._store.destroyed.connect(self._on_store_destroyed)
        self._job_dialog = None
        self._page = 1
        self._page_size = _PAGE_SIZES[0]
        self._total = 0
        self._statuses = None      # set from the filter button in _build_ui
        self._query = None
        self._entries = []
        self._worker = None
        self._pending_from_action = False

        self.setWindowTitle('Nukomfy - All Jobs ({})'.format(machine.name))
        self.setMinimumSize(600, 400)
        # Follow the Render Manager's window rules: when kept on top it is a
        # Tool window pinned above Nuke, otherwise a normal maximizable window.
        if settings.render_manager_keep_on_top and parent is not None:
            self.setWindowFlags(
                QtCore.Qt.Tool
                | QtCore.Qt.WindowTitleHint
                | QtCore.Qt.WindowSystemMenuHint
                | QtCore.Qt.WindowMaximizeButtonHint
                | QtCore.Qt.WindowCloseButtonHint)
        else:
            self.setWindowFlags(
                QtCore.Qt.Window
                | QtCore.Qt.WindowTitleHint
                | QtCore.Qt.WindowSystemMenuHint
                | QtCore.Qt.WindowMinimizeButtonHint
                | QtCore.Qt.WindowMaximizeButtonHint
                | QtCore.Qt.WindowCloseButtonHint)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.destroyed.connect(self._on_destroyed)
        apply_window_chrome(self)

        self._build_ui()
        self.resize(_DEFAULT_W, _DEFAULT_H)
        # First-ever open: centre on the screen showing the Render Manager.
        # Saved geometry (size + position; normal geometry even if it was
        # closed maximized) overrides this on subsequent opens. The window is
        # independent of the Render Manager - it survives the panel closing.
        self._center_on_panel_screen()
        ui_state.restore_geometry('machine_jobs_viewer', self,
                                  with_position=True, fit=True)

        self._fetch(from_action=False)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)   # uniform gaps between items (matches the Suite)

        self._search = NukomfyLineEdit()
        self._search.setPlaceholderText('Search workflow, user, node, job id…')
        self._search.setClearButtonEnabled(True)
        self._search.addAction(material_icon(SEARCH, '#666', 14),
                               QtWidgets.QLineEdit.LeadingPosition)
        self._search.setStyleSheet(SEARCH_FIELD_STYLE)
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._on_search_committed)
        self._search.textChanged.connect(lambda _=None: self._debounce.start())
        bar.addWidget(self._search, 1)   # absorbs all spare width

        # Status filter: shared StatusFilterButton (persisted under the
        # viewer's own ui_state key); the same widget MyJobs uses.
        self._filter_btn = StatusFilterButton(
            JOB_STATUS_FILTERS, self._on_filter_changed,
            state_key=_FILTER_STATE_KEY)
        self._statuses = self._filter_btn.selected()
        bar.addWidget(self._filter_btn)

        bar.addWidget(QtWidgets.QLabel('Per page:'))
        self._page_combo = NoWheelComboBox()
        for n in _PAGE_SIZES:
            self._page_combo.addItem(str(n), n)
        self._page_combo.setCurrentIndex(0)
        self._page_combo.currentIndexChanged.connect(
            self._on_page_size_changed)
        bar.addWidget(self._page_combo)

        self._refresh_btn = QtWidgets.QPushButton(
            material_icon(REFRESH, '#ccc', 14), ' Refresh')
        self._refresh_btn.setToolTip('Refresh')
        self._refresh_btn.clicked.connect(
            lambda: self._fetch(from_action=True))
        bar.addWidget(self._refresh_btn)

        # Pager: « ‹ Page X of Y › » grouped tight, then a separate
        # "Showing X-Y of Z" counter. Buttons stay visible (greyed when not
        # applicable) so the toolbar never shifts.
        self._first_btn = QtWidgets.QToolButton()
        self._first_btn.setText('«')
        self._first_btn.setToolTip('First page')
        self._first_btn.clicked.connect(lambda: self._go_page(1))
        self._prev_btn = QtWidgets.QToolButton()
        self._prev_btn.setText('‹')
        self._prev_btn.setToolTip('Previous page')
        self._prev_btn.clicked.connect(lambda: self._go_page(self._page - 1))
        self._page_lbl = QtWidgets.QLabel('Page 1 of 1')
        self._next_btn = QtWidgets.QToolButton()
        self._next_btn.setText('›')
        self._next_btn.setToolTip('Next page')
        self._next_btn.clicked.connect(lambda: self._go_page(self._page + 1))
        self._last_btn = QtWidgets.QToolButton()
        self._last_btn.setText('»')
        self._last_btn.setToolTip('Last page')
        self._last_btn.clicked.connect(lambda: self._go_page(self._last_page()))
        self._count_lbl = QtWidgets.QLabel('')
        # Reserve fixed space for the variable-width pager labels so the
        # toolbar never shifts as page/counter values change (or on "Loading…").
        _fm = self._count_lbl.fontMetrics()
        self._page_lbl.setFixedWidth(_fm.horizontalAdvance('Page 8888 of 8888') + 6)
        self._page_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._count_lbl.setFixedWidth(
            _fm.horizontalAdvance('Showing 8888-8888 of 8888') + 6)
        self._count_lbl.setAlignment(QtCore.Qt.AlignCenter)
        pager = QtWidgets.QHBoxLayout()
        pager.setContentsMargins(0, 0, 0, 0)
        pager.setSpacing(2)
        for w in (self._first_btn, self._prev_btn, self._page_lbl,
                  self._next_btn, self._last_btn):
            pager.addWidget(w)
        pager_w = QtWidgets.QWidget()
        pager_w.setLayout(pager)
        bar.addWidget(pager_w)
        bar.addWidget(self._count_lbl)

        root.addLayout(bar)

        self._table = QtWidgets.QTableWidget(0, len(_V_HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection)
        self._table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(DETAIL_STYLE)
        self._table.setTextElideMode(QtCore.Qt.ElideLeft)
        self._table.setItemDelegateForColumn(
            _SUB_COL_NKFILE, _LeftElideDelegate(self._table))
        self._table.setFrameShape(QtWidgets.QFrame.NoFrame)
        _setup_table_columns(
            self._table, _V_HEADERS, _V_WIDTHS,
            fixed_cols={_SUB_COL_STATUS, _V_COL_READ, _V_COL_LOG},
            stretch_col=_SUB_COL_NKFILE,
            ui_key='rq_machine_viewer_table_v2',
            pixel_widths={_V_COL_READ: 22 + 3, _V_COL_LOG: 22 + 3})
        self._table.cellDoubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        _install_empty_area_deselect(self._table)
        # A press anywhere in the window off the table (toolbar, pager,
        # margins, button row) deselects too - matches the Render Manager.
        _install_click_outside_deselect(self, self._table)
        root.addWidget(self._table, 1)

        # Offline / unreachable overlay shown instead of the table.
        self._offline_widget = QtWidgets.QWidget()
        ow = QtWidgets.QVBoxLayout(self._offline_widget)
        ow.addStretch(1)
        self._offline_lbl = QtWidgets.QLabel()
        self._offline_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._offline_lbl.setStyleSheet('color:#888;')
        ow.addWidget(self._offline_lbl)
        retry_row = QtWidgets.QHBoxLayout()
        retry_row.addStretch(1)
        retry_btn = QtWidgets.QPushButton('Retry')
        retry_btn.setFixedHeight(24)
        retry_btn.clicked.connect(lambda: self._fetch(from_action=True))
        retry_row.addWidget(retry_btn)
        retry_row.addStretch(1)
        ow.addLayout(retry_row)
        ow.addStretch(1)
        self._offline_widget.hide()
        root.addWidget(self._offline_widget, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QtWidgets.QPushButton('Close')
        close_btn.setFixedHeight(24)
        # close() (not accept()) so closeEvent fires and the teardown runs -
        # accept() only hides, leaking the worker and skipping geometry save.
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # -------------------------------------------------------------- filters
    def _on_filter_changed(self):
        self._statuses = self._filter_btn.selected()
        self._go_page(1)

    def _on_search_committed(self):
        self._query = self._search.text().strip() or None
        self._go_page(1)

    def _on_page_size_changed(self, _idx):
        self._page_size = self._page_combo.currentData()
        self._go_page(1)

    def _go_page(self, page):
        self._page = max(1, page)
        self._fetch(from_action=True)

    # ---------------------------------------------------------------- fetch
    def _fetch(self, from_action):
        self._worker = stop_worker(self._worker)
        self._pending_from_action = from_action
        self._count_lbl.setText('Loading…')
        for b in (self._first_btn, self._prev_btn,
                  self._next_btn, self._last_btn):
            b.setEnabled(False)

        offset = (self._page - 1) * self._page_size
        limit = self._page_size
        statuses = self._statuses
        query = self._query

        def _do(m):
            return manager_client.get_persistent_history(
                m.url, limit=limit, offset=offset,
                statuses=statuses, query=query)

        self._worker = UnifiedFetchWorker([self._machine], _do)
        self._worker.result.connect(self._on_fetched)
        self._worker.start()

    def _on_fetched(self, _machine_id, payload):
        if not payload or not payload.get('ok'):
            self._on_offline(self._pending_from_action)
            return
        self._set_offline(False)
        entries = payload.get('entries') or []
        for e in entries:
            # The viewer fetches the persistent history directly, bypassing
            # the store's ingest, so derive the epoch `create_time` the Sent
            # column needs from the ISO `nfy_sent_at` (same as the store).
            if not e.get('create_time'):
                e['create_time'] = _sent_at_to_epoch(e.get('nfy_sent_at'))
            # Mirror ingest's URL attach too: the server stores the
            # submit-time URL (empty for jobs sent outside Nukomfy, another
            # client's address otherwise), while the Job dialog's workflow
            # fetch and Refresh need the URL THIS client reaches the
            # machine at.
            e['nfy_machine_url'] = self._machine.url
        self._total = int(payload.get('total_count') or 0)
        self._rebuild_rows(entries)
        self._update_pager()

    def _on_offline(self, from_action):
        self._set_offline(True)
        if from_action:
            _dialogs.warn(
                self, 'Nukomfy - All Jobs',
                "Machine '{}' is offline.\n\n"
                "The job list can't be updated right now. It will be "
                "available again once the machine reconnects.".format(
                    self._machine.name))

    def _set_offline(self, offline):
        if offline:
            self._offline_lbl.setText(
                "Machine '{}' is offline.\nJobs are unavailable until it "
                "reconnects.".format(self._machine.name))
            self._count_lbl.setText('')
            for b in (self._first_btn, self._prev_btn,
                      self._next_btn, self._last_btn):
                b.setEnabled(False)
        self._table.setVisible(not offline)
        self._offline_widget.setVisible(offline)
        # Refresh stays enabled so the user can retry from the toolbar too.
        for w in (self._search, self._filter_btn, self._page_combo):
            w.setEnabled(not offline)

    # ----------------------------------------------------------------- rows
    def _rebuild_rows(self, entries):
        # Save selection by pid before setRowCount(0) clears it, then
        # re-select the same job after refill if it survived. Keeps the
        # selection through refresh, filter, search and page-size changes
        # (same approach as the Render Manager subtables).
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
        self._entries = entries
        for entry in self._entries:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setRowHeight(row, 26)
            _fill_common_job_cells(self._table, row, entry)

            dur = QtWidgets.QTableWidgetItem(
                _format_duration(entry.get('nfy_duration', 0)))
            dur.setTextAlignment(QtCore.Qt.AlignCenter)
            self._table.setItem(row, _V_COL_DURATION, dur)

            self._table.setCellWidget(
                row, _V_COL_READ, self._make_read_button(entry))
            self._table.setCellWidget(
                row, _V_COL_LOG, self._make_log_button(entry))

        if saved_pid:
            for i, e in enumerate(self._entries):
                if e.get('prompt_id') == saved_pid:
                    self._table.selectRow(i)
                    self._table.scrollTo(self._table.model().index(i, 0))
                    break

    def _make_read_button(self, entry):
        outputs = entry.get('nfy_output_paths') or []
        status_str = (entry.get('nfy_status_str') or '').lower()
        can_read = bool(outputs) and status_str not in ('cancelled', 'failed')
        try:
            color = int(entry.get('nfy_read_color', 0) or 0)
        except (ValueError, TypeError):
            color = 0
        btn = QtWidgets.QPushButton(FILE_DOWNLOAD)
        btn.setFont(icon_font(14))
        btn.setFixedSize(22, 22)
        if can_read:
            btn.setToolTip('Read Output(s): create Read nodes for these '
                           'outputs')
            # Green matches MyJobs - the shared "available" affordance.
            btn.setStyleSheet(
                cell_action_colored(SUCCESS_COLOR, SUCCESS_HOVER, '#2a3a2a'))
            btn.clicked.connect(
                lambda _=False, p=list(outputs), c=color:
                    self._read_outputs(p, c))
        else:
            btn.setEnabled(False)
            btn.setToolTip('No outputs available for this job')
            btn.setStyleSheet(BUTTON_STYLE_CELL_ACTION)
        return _centered_cell(btn)

    def _make_log_button(self, entry):
        btn = QtWidgets.QPushButton(DESCRIPTION)
        btn.setFont(icon_font(14))
        btn.setFixedSize(22, 22)
        btn.setStyleSheet(BUTTON_STYLE_CELL_ACTION)
        btn.setToolTip('View execution log')
        btn.clicked.connect(lambda _=False, e=entry: self._open_detail(e, 'log'))
        return _centered_cell(btn)

    # -------------------------------------------------------------- actions
    def _on_store_destroyed(self, *_):
        self._store = None

    def _read_outputs(self, output_paths, color):
        read_outputs(self, output_paths, color)

    def _open_detail(self, entry, tab='detail'):
        # The Job dialog is parented to THIS viewer (not the Render Manager),
        # so it is a child of All Jobs: it closes when All Jobs closes and
        # opening it never disturbs this window.
        from Nukomfy.gui.render_queue_panel import _JobDialog
        # _store is dropped via its destroyed signal when the Render Manager
        # closes; when it is gone we fall back to the raw server entry.
        store = self._store
        pid = entry.get('prompt_id')
        job = None
        if store is not None:
            try:
                store.load_local_history()
                if pid:
                    job = store.get(pid)
            except Exception:
                _log.debug('store enrich failed', exc_info=True)
        if not job:
            job = dict(entry)
        if self._job_dialog is None:
            self._job_dialog = _JobDialog(self, store=store)
        self._job_dialog.populate(job, initial_tab=tab)
        self._job_dialog.setWindowState(QtCore.Qt.WindowNoState)
        _fit_dialog_to_content(self._job_dialog, 900, 650)
        self._job_dialog.show()
        self._job_dialog.raise_()
        self._job_dialog.activateWindow()

    def _on_double_click(self, row, _col):
        if 0 <= row < len(self._entries):
            self._open_detail(self._entries[row], 'detail')

    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._entries):
            return
        entry = self._entries[row]
        menu = QtWidgets.QMenu(self._table)
        detail_act = menu.addAction('Show Job Detail')
        detail_act.triggered.connect(
            lambda _=False, e=entry: self._open_detail(e, 'detail'))

        outputs = entry.get('nfy_output_paths') or []
        status_str = (entry.get('nfy_status_str') or '').lower()
        can_read = bool(outputs) and status_str not in ('cancelled', 'failed')
        try:
            color = int(entry.get('nfy_read_color', 0) or 0)
        except (ValueError, TypeError):
            color = 0
        read_act = menu.addAction('Read Output(s)')
        if can_read:
            read_act.triggered.connect(
                lambda _=False, p=list(outputs), c=color:
                    self._read_outputs(p, c))
        else:
            read_act.setEnabled(False)

        menu.addSeparator()
        _add_copy_job_id(menu, entry)
        _add_show_output_folder(menu, self._table.viewport(), entry)
        menu.exec_(self._table.viewport().mapToGlobal(pos))

    # ----------------------------------------------------------------- misc
    def _last_page(self):
        size = self._page_size
        return max(1, (self._total + size - 1) // size)

    def _update_pager(self):
        total = self._total
        size = self._page_size
        last = max(1, (total + size - 1) // size)
        if total > 0 and self._page > last:
            # A filter shrank the set below the current page - clamp + refetch.
            self._go_page(last)
            return
        self._page_lbl.setText('Page {} of {}'.format(self._page, last))
        if total <= 0:
            self._count_lbl.setText(
                'No matches' if (self._query or self._statuses) else 'No jobs')
        else:
            start = (self._page - 1) * size + 1
            end = min(self._page * size, total)
            self._count_lbl.setText(
                'Showing {}-{} of {}'.format(start, end, total))
        at_first = self._page <= 1
        at_last = self._page >= last
        self._first_btn.setEnabled(not at_first)
        self._prev_btn.setEnabled(not at_first)
        self._next_btn.setEnabled(not at_last)
        self._last_btn.setEnabled(not at_last)

    def _center_on_panel_screen(self):
        """Centre on the screen that shows the Render Manager (first open)."""
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        screen = None
        if self._panel is not None:
            try:
                screen = QtWidgets.QApplication.screenAt(
                    self._panel.frameGeometry().center())
            except (AttributeError, RuntimeError):
                screen = None
        if screen is None:
            screen = app.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        self.move(avail.center().x() - self.width() // 2,
                  avail.center().y() - self.height() // 2)

    def _stop_workers(self):
        self._worker = stop_worker(self._worker)
        # The Job dialog is a child of this WA_DeleteOnClose window: abandon
        # its in-flight workflow-API fetch before the widget tree dies.
        if self._job_dialog is not None:
            self._job_dialog.stop_workers()

    def closeEvent(self, event):
        flt = getattr(self, '_click_outside_deselect_filter', None)
        if flt is not None:
            flt.detach()
        ui_state.save_geometry('machine_jobs_viewer', self, with_position=True)
        self._stop_workers()
        _viewers.pop(self._machine.id, None)
        super().closeEvent(event)

    def _on_destroyed(self):
        """Safety net: stop workers if Nuke exits without closeEvent."""
        try:
            self._stop_workers()
        except (RuntimeError, AttributeError):
            pass


# One viewer per machine: reopening raises the existing window.
_viewers = {}


def show_machine_jobs_viewer(machine, panel):
    """Open (or raise) the job viewer for *machine*. Non-modal."""
    existing = _viewers.get(machine.id)
    if existing is not None:
        try:
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return existing
        except RuntimeError:
            pass
        _viewers.pop(machine.id, None)
    # Mirror the Render Manager's parenting so keep-on-top behaves identically.
    parent = (_nuke_main_window()
              if settings.render_manager_keep_on_top else None)
    viewer = MachineJobsViewer(machine, panel, parent=parent)
    _viewers[machine.id] = viewer
    viewer.show()
    # Bring it to the front on open, like the re-open branch above: X11 window
    # managers don't auto-raise a newly shown Tool window over its active
    # sibling (the Render Manager it was opened from) the way Windows does.
    viewer.raise_()
    viewer.activateWindow()
    return viewer
