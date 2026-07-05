"""Machine statuses with expandable job details and personal submit history.

Accessible from the toolbar menu and after a successful submit.
"""

import datetime as _datetime
import logging
import time as _time

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui, _nuke_main_window
from Nukomfy.gui import _dialogs
from Nukomfy.gui._fields import NukomfyTextEdit

import Nukomfy.client.ws_client as ws_client
import Nukomfy.data.submit_history as submit_history
from Nukomfy.client.machines import machine_manager
from Nukomfy.client.comfy_api import (
    fetch_all_for_machine,
    fetch_workflow_api,
)
from Nukomfy.core.identity import current_user, ws_session_id
from Nukomfy.core.settings import settings
from Nukomfy.gui.ui_state import ui_state
from Nukomfy.gui.workers import UnifiedFetchWorker, stop_worker
from Nukomfy.gui._render_data_store import RenderDataStore
from Nukomfy.gui.icons import (icon_font, set_press_icon, _ensure_loaded,
                               CLOSE, REMOVE, REFRESH,
                               DESCRIPTION, VIEW_LIST, FILE_DOWNLOAD)
from Nukomfy.gui._theme import (
    TABLE_STYLE, DETAIL_STYLE, TAB_STYLE,
    BUTTON_STYLE_CELL_ACTION, cell_action_colored, cell_toolbar_icon, apply_tab_fit,
    apply_window_chrome,
    ERROR_COLOR, ERROR_HOVER,
    WARNING_STATUS, WARNING_STATUS_HOVER,
    ACCENT_GOLD, SUCCESS_COLOR, SUCCESS_HOVER,
)
from Nukomfy.gui._auto_refresh import (
    AutoRefreshTimer, RefreshCycle, busy_mark, schedule_after_min_visible)
from Nukomfy.gui import _focus_drop
from Nukomfy.gui.status_display import (
    render_machine_status, ABORTING_LABEL, REMOVING_LABEL, INFLIGHT_COLOR,
    _update_status_cell)

from Nukomfy.gui.render_queue_format import (
    detail_doc_font,
    material_to_utf,
    _format_duration,
    _format_log_messages,
    _format_log_plaintext,
    _render_section_submission, _render_section_machine,
    _render_section_workflow, _render_section_render,
    _render_section_submitted_params,
    _render_detail_header, _render_job_header,
    _short_job_ref,
    _fill_common_job_cells, _centered_cell,
    _setup_table_columns, _fit_dialog_to_content,
    _install_empty_area_deselect,
    _make_status_cell,
    _SUB_COL_STATUS, _SUB_COL_NKFILE,
    _Q_COL_PROGRESS, _Q_COL_ACTIONS,
    _H_COL_DURATION, _H_COL_READ, _H_COL_LOG,
    _Q_HEADERS, _Q_WIDTHS, _H_HEADERS, _H_WIDTHS,
    _HatchedProgressCell, _HatchedFillProgressCell, _CollapsibleSection,
    _LeftElideDelegate, _WorkflowApiTab,
    _WS_MISSING_TOOLTIP, _coarse_progress_tooltip,
)
from Nukomfy.gui.render_queue_actions import (
    _abort_or_remove_entry, read_outputs,
    _LOCALLY_REMOVED_TTL_S, _sweep_aged_out,
    _show_machine_offline_popup,
)
from Nukomfy.gui.render_queue_context_menu import show_job_context_menu
from Nukomfy.gui.render_queue_myjobs import _MyJobsWidget
from Nukomfy.utils.log_format import fmt_job, fmt_machine

_log = logging.getLogger(__name__)


# Vertical padding (px) added below the detail tab widget when sizing an
# expanded machine's detail row.
_DETAIL_ROW_PADDING = 12


def _placeholder_label(text):
    """Grey, centered placeholder QLabel for empty / loading tab states."""
    lbl = QtWidgets.QLabel(text)
    lbl.setStyleSheet('color:#666;padding:12px;')
    lbl.setAlignment(QtCore.Qt.AlignCenter)
    return lbl


def _sort_offline_last(machines):
    """Stable sort: online machines first (alphabetical), offline last."""
    online = []
    offline = []
    for m in machines:
        if getattr(m, 'online', True):
            online.append(m)
        else:
            offline.append(m)
    return online + offline


class _WorkflowApiFetchWorker(QtCore.QThread):
    """Fetch the workflow API JSON for a server-only job off the main thread.

    Own jobs resolve from the local BLOB synchronously in the dialog; this
    worker runs only the HTTP fallback chain, so opening the Detail dialog
    on a job whose local BLOB is missing never freezes the GUI while the
    requests are in flight:

    1. `fetch_workflow_api` - ComfyUI's native `/api/jobs/{prompt_id}`.
       Covers jobs still in the server's in-memory history.
    2. `manager_client.get_persistent_workflow_api` - the Suite's
       persistent history. Covers everything else: the native history
       does not survive a ComfyUI restart, the persistent one does.
    """
    result = QtCore.Signal(str, object)  # (prompt_id, wf_api dict | None)

    def __init__(self, machine_url, prompt_id, parent=None):
        super().__init__(parent)
        self._url = machine_url
        self._pid = prompt_id
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from Nukomfy.client import manager_client
        try:
            wf_api = fetch_workflow_api(self._url, self._pid)
        except Exception:
            _log.exception(
                'Workflow API fetch failed for %s on %s',
                fmt_job(self._pid), fmt_machine(self._url))
            wf_api = None
        if not (isinstance(wf_api, dict) and wf_api) and not self._cancelled:
            try:
                wf_api = manager_client.get_persistent_workflow_api(
                    self._url, self._pid)
            except Exception:
                _log.exception(
                    'Persistent workflow API fetch failed for %s on %s',
                    fmt_job(self._pid), fmt_machine(self._url))
                wf_api = None
        if self._cancelled:
            return
        try:
            self.result.emit(
                self._pid, wf_api if isinstance(wf_api, dict) and wf_api else None)
        except RuntimeError:
            pass  # receiver destroyed (Nuke shutting down)


class _EntryRefetchWorker(QtCore.QThread):
    """Re-fetch one job entry from the Suite's persistent history off the
    main thread.

    Backs the Job dialog's Refresh when the store can't resolve the job:
    rows opened from the All Jobs viewer bypass the store (whose bounded
    recent-terminals cache holds only the newest few terminals per machine),
    and the store itself dies with the Render Manager while the viewer -
    and this dialog - stay open.
    """
    result = QtCore.Signal(str, object)  # (prompt_id, entry dict | None)

    def __init__(self, machine_url, prompt_id, parent=None):
        super().__init__(parent)
        self._url = machine_url
        self._pid = prompt_id
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from Nukomfy.client import manager_client
        entry = None
        try:
            payload = manager_client.get_persistent_history_one(
                self._url, self._pid)
        except Exception:
            _log.exception(
                'Persistent entry fetch failed for %s on %s',
                fmt_job(self._pid), fmt_machine(self._url))
            payload = None
        if payload and payload.get('ok'):
            e = payload.get('entry')
            if isinstance(e, dict) and e:
                entry = e
        if self._cancelled:
            return
        try:
            self.result.emit(self._pid, entry)
        except RuntimeError:
            pass  # receiver destroyed (Nuke shutting down)


class _JobDialog(QtWidgets.QDialog):
    """Unified job popup. Persistent: construct once, call populate() per job.

    Shows a shared header with prompt_id / nfy_job_id / status / submitted /
    duration, then a tab widget with two pages:

    - Detail        - composite: HTML compact header on top + 5
                      collapsible sections (Submission / Machine
                      snapshot / Workflow / Render configuration /
                      Submitted parameters). Sections with no
                      populated fields are hidden entirely.
    - Log           - ComfyUI execution messages (execution_start,
                      executing, execution_success, execution_error
                      with red accent, ...).
    - Workflow (API)- pretty-printed JSON dump of the payload posted
                      to /prompt, plus search / Copy / Save toolbar.
                      Source: load_workflow_api(prompt_id) BLOB persisted
                      at submit time.
    """

    _SECTION_SPECS = (
        ('submission',       'Submission',                  _render_section_submission),
        ('machine_snapshot', 'Machine snapshot at submit',  _render_section_machine),
        ('workflow',         'Workflow',                    _render_section_workflow),
        ('render_config',    'Render configuration',        _render_section_render),
        ('submitted_params', 'Submitted parameters',        _render_section_submitted_params),
    )

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.setMinimumSize(600, 450)
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint)
        # Chrome before the UI is built so every label (including those inside
        # the detail tab's stylesheet'd sub-widgets) inherits the baseline.
        apply_window_chrome(self)
        # Material Icons font must be registered before any QTextEdit
        # renders HTML using font-family:'Material Icons'.
        _ensure_loaded()

        # Dialog is a snapshot. Manual Refresh button (top-right)
        # re-pulls the current entry from the store (or the server's
        # persistent history) on demand, instead of auto-updating or
        # auto-closing - the user controls when to re-sync, so a running
        # render never has its log dialog disappear from under them.
        self._store = store
        if store is not None:
            # The store is a QObject child of the Render Manager panel and
            # dies with it, while this dialog can outlive both (All Jobs
            # viewer). A dead store does NOT raise on its pure-Python
            # methods - it silently returns caches frozen at destruction
            # time - so drop the reference the moment the C++ half goes
            # away and let Refresh route to the persistent-history re-fetch.
            store.destroyed.connect(self._on_store_destroyed)
        self._current_prompt_id = None
        self._current_entry = None
        # In-flight cross-user workflow-API fetch (off-thread); the dialog is
        # a singleton reused across jobs, so at most one runs at a time.
        self._wf_worker = None
        # In-flight Refresh re-fetch from the persistent history (used when
        # the store can't resolve the current job); same singleton rule.
        self._refetch_worker = None
        # Graphs fetched for server-only jobs, keyed by prompt_id. A workflow
        # API graph is immutable once submitted, so a fetched copy never goes
        # stale: reusing it on Refresh/re-open renders Submitted parameters
        # and the Workflow tab in one pass - no fetch round-trip and no
        # visible refill - exactly like a job with a local BLOB. Bounded FIFO.
        self._wf_api_cache = {}
        self._initial_tab = 'detail'
        # Source events behind the Log tab, stashed at populate time so
        # Copy / Save can rebuild clean plain text from the data instead of
        # scraping the rendered widget (its error box is an HTML table that
        # toPlainText would explode into phantom blank lines).
        self._log_messages = []
        self._log_execution_error = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Header bar: summary label on the left, Refresh button on the
        # right (vertically centered). Object-name selector so the
        # bg/border applies only to the container, not cascaded down to
        # the QPushButton (which would lose its default hover/pressed
        # styling).
        _header_box = QtWidgets.QWidget()
        _header_box.setObjectName('JobHeaderBox')
        _header_box.setStyleSheet(
            'QWidget#JobHeaderBox{background:#1e1e1e;border:1px solid #333;'
            'border-radius:3px;}')
        _header_lay = QtWidgets.QHBoxLayout(_header_box)
        _header_lay.setContentsMargins(8, 4, 8, 4)
        _header_lay.setSpacing(8)

        self._header = QtWidgets.QLabel()
        self._header.setStyleSheet(
            'QLabel{background:transparent;border:none;color:#ccc;}')
        # setFont (not the stylesheet) so the summary bar matches the Detail
        # body font: on Qt 6.5 a stylesheet font-family doesn't reach the
        # QLabel's bold HTML tags, but setFont does.
        self._header.setFont(detail_doc_font(11))
        self._header.setWordWrap(True)
        self._header.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        _header_lay.addWidget(self._header, 1)

        self._refresh_btn = QtWidgets.QPushButton('Refresh')
        set_press_icon(self._refresh_btn, REFRESH)
        self._refresh_btn.setToolTip(
            "Refresh this job's status now")
        self._refresh_btn.setFixedHeight(24)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        _header_lay.addWidget(self._refresh_btn, 0, QtCore.Qt.AlignVCenter)

        lay.addWidget(_header_box)

        self._tabs = QtWidgets.QTabWidget()
        self._tabs.setStyleSheet(TAB_STYLE)
        apply_tab_fit(self._tabs, 12)

        # Detail tab: composite layout. Top = HTML compact header
        # (`_render_detail_header`). Below = 5 collapsible sections
        # wrapped in QScrollArea for tall jobs.
        detail_root = QtWidgets.QWidget()
        detail_root.setStyleSheet('QWidget { background: #1e1e1e; }')
        detail_lay = QtWidgets.QVBoxLayout(detail_root)
        detail_lay.setContentsMargins(8, 8, 8, 8)
        detail_lay.setSpacing(0)

        self._detail_header_text = NukomfyTextEdit()
        self._detail_header_text.setReadOnly(True)
        self._detail_header_text.setFrameStyle(0)
        self._detail_header_text.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOff)
        self._detail_header_text.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOff)
        self._detail_header_text.setStyleSheet(
            'QTextEdit { background: transparent; border: none; '
            'color: #ccc; }')
        # Base font via the document default font, not a stylesheet: on Qt 6.5
        # a stylesheet font doesn't reach bold or bare-text fragments, so only
        # setDefaultFont applies Nuke's UI font (and the size) to every one.
        self._detail_header_text.document().setDefaultFont(detail_doc_font())
        self._detail_header_text.setWordWrapMode(QtGui.QTextOption.WordWrap)
        self._detail_header_text.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        detail_lay.addWidget(self._detail_header_text)

        # Visual gap header to first section (no graphical separator).
        detail_lay.addSpacing(8)

        self._sections = {}
        for sid, title, _renderer in self._SECTION_SPECS:
            sec = _CollapsibleSection(sid, title)
            self._sections[sid] = sec
            detail_lay.addWidget(sec)
        detail_lay.addStretch(1)

        detail_scroll = QtWidgets.QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameStyle(0)
        detail_scroll.setStyleSheet(
            'QScrollArea { background: #1e1e1e; border: none; }')
        detail_scroll.setWidget(detail_root)
        self._tabs.addTab(detail_scroll, 'Detail')

        # Log tab - styled to match the Detail tab (background, border, and
        # the same document default font).
        self._log_text = NukomfyTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setFrameStyle(0)
        self._log_text.setStyleSheet(
            'QTextEdit { background: #1e1e1e; color: #ccc; '
            'border: none; padding: 8px; }')
        self._log_text.document().setDefaultFont(detail_doc_font())
        self._tabs.addTab(self._log_text, 'Log')

        # Workflow (API) tab - JSON dump. Order: Detail (0), Log (1),
        # Workflow API (2). Tab index 1 stays "Log" so the
        # `initial_tab='log'` path in populate() still resolves
        # correctly.
        self._workflow_tab = _WorkflowApiTab()
        self._tabs.addTab(self._workflow_tab, 'Workflow (API)')

        lay.addWidget(self._tabs, 1)

        btn_lay = QtWidgets.QHBoxLayout()
        btn_lay.addStretch()
        # Copy + Save live in the footer, left of Close,
        # and act on whichever tab is currently active. Detail / Log
        # save as .txt (plain text dump), Workflow (API) saves as
        # .json. Same widgets across all 3 tabs - no per-tab toolbar.
        self._copy_btn = QtWidgets.QPushButton('Copy')
        self._copy_btn.setFixedHeight(24)
        self._copy_btn.setToolTip('Copy the active tab content to the clipboard')
        self._copy_btn.clicked.connect(self._on_copy_active)
        btn_lay.addWidget(self._copy_btn)
        self._save_btn = QtWidgets.QPushButton('Save…')
        self._save_btn.setFixedHeight(24)
        self._save_btn.setToolTip(
            'Save the active tab content to a file '
            '(.json for Workflow API, .txt for Detail / Log)')
        self._save_btn.clicked.connect(self._on_save_active)
        btn_lay.addWidget(self._save_btn)
        close_btn = QtWidgets.QPushButton('Close')
        close_btn.setFixedHeight(24)
        close_btn.clicked.connect(self.accept)
        btn_lay.addWidget(close_btn)
        lay.addLayout(btn_lay)

    def _inject_cached_workflow_api(self, entry):
        """Return a shallow copy of `entry` with `nfy_workflow_api` populated
        from a source available without a network round-trip: the local BLOB
        (`submit_history.load_workflow_api`, a ~ms disk read for own jobs) or
        this dialog's fetch cache (server-only jobs whose graph already
        arrived in a previous populate). Original entry when neither has it.

        The HTTP fallback chain (up to a 10s timeout) is NOT done here: it
        runs off-thread in `_start_workflow_api_fetch` so opening the dialog
        never freezes the GUI. We intentionally don't cache the graphs in the
        RenderDataStore - a workflow graph dict per history row would cost
        meaningful RAM for a value only shown when a Detail dialog opens.

        Returns a shallow copy because mutating the original entry would
        contaminate the store the caller passed in.
        """
        if not isinstance(entry, dict):
            return entry
        pid = entry.get('prompt_id', '') or ''
        if not pid:
            return entry
        try:
            wf_api = submit_history.load_workflow_api(pid)
        except Exception:
            _log.exception(
                'Workflow API load failed for %s', fmt_job(pid))
            wf_api = None
        if not (isinstance(wf_api, dict) and wf_api):
            wf_api = self._wf_api_cache.get(pid)
        if not (isinstance(wf_api, dict) and wf_api):
            return entry
        out = dict(entry)
        out['nfy_workflow_api'] = wf_api
        return out

    def _start_workflow_api_fetch(self, entry):
        """Kick an off-thread HTTP fetch of the workflow API for a server-only
        job, then refresh the workflow-dependent views when it arrives. No-op
        when the graph is already injected (local BLOB or dialog cache)."""
        # Cancel any fetch still in flight from a previous populate (the
        # dialog is a singleton reused across jobs). stop_worker also
        # disconnects its signals, so a stale result can't land.
        self._wf_worker = stop_worker(self._wf_worker)
        if not isinstance(entry, dict):
            return
        wf = entry.get('nfy_workflow_api')
        if isinstance(wf, dict) and wf:
            return  # already resolved from the local BLOB
        pid = entry.get('prompt_id', '') or ''
        url = entry.get('nfy_machine_url', '') or ''
        if not pid or not url:
            return
        self._workflow_tab.show_loading()
        worker = _WorkflowApiFetchWorker(url, pid)
        worker.result.connect(self._on_workflow_api_fetched)
        worker.finished.connect(lambda w=worker: self._clear_wf_worker(w))
        self._wf_worker = worker
        worker.start()

    def _clear_wf_worker(self, worker):
        # Only clear if this is still the current worker - a superseded fetch
        # finishing later must not null out a newer one's reference.
        if self._wf_worker is worker:
            self._wf_worker = None
        # Free the C++ thread object: the finished-signal lambda captures the
        # worker and the connection holds the lambda, so without an explicit
        # deleteLater every completed fetch would pin its QThread for the
        # process lifetime (same reason _reap_action_worker deletes).
        worker.deleteLater()

    def stop_workers(self):
        """Abandon any in-flight fetch (workflow API / Refresh re-fetch).
        Called from the owning window's teardown: the dialog is destroyed
        with its WA_DeleteOnClose owner, and destroying a still-running
        QThread crashes Nuke ("QThread: Destroyed while thread is still
        running")."""
        self._wf_worker = stop_worker(self._wf_worker)
        self._refetch_worker = stop_worker(self._refetch_worker)

    def _on_store_destroyed(self, *_):
        self._store = None

    def _on_workflow_api_fetched(self, prompt_id, wf_api):
        """Apply an off-thread workflow-API result. Runs on the main thread."""
        if isinstance(wf_api, dict) and wf_api:
            # Memoize regardless of which job is on screen: the graph is
            # immutable after submit, so the result stays valid for its own
            # prompt_id and saves the fetch (and the visible params refill)
            # next time that job is shown.
            self._wf_api_cache[prompt_id] = wf_api
            while len(self._wf_api_cache) > 16:
                self._wf_api_cache.pop(next(iter(self._wf_api_cache)))
        # Stale result: the singleton dialog moved to another job while the
        # fetch was in flight.
        if prompt_id != self._current_prompt_id:
            return
        base = self._current_entry if isinstance(self._current_entry, dict) else {}
        if not (isinstance(wf_api, dict) and wf_api):
            # Fetch failed / no workflow: settle the "Loading…" placeholder
            # to the empty end-state.
            self._workflow_tab.populate(base)
            return
        entry = dict(base)
        entry['nfy_workflow_api'] = wf_api
        self._current_entry = entry
        # Refresh only the workflow-dependent views (Submitted parameters
        # section + Workflow (API) tab).
        self._populate_detail(entry)
        self._workflow_tab.populate(entry)

    def _populate_detail(self, entry):
        """Fill compact header + 5 collapsible sections."""
        self._detail_header_text.setHtml(_render_detail_header(entry))
        # Size the header to its content now, then again after the event
        # loop drains: the first populate runs before .show()/resize, so
        # viewport().width() is not final, the text wraps differently and
        # the height is off by ~1 row (a blank strip above the first
        # section). The deferred pass recomputes at the real width.
        self._fit_header_height()
        QtCore.QTimer.singleShot(0, self._fit_header_height)
        # Populate sections - each hides itself if its content is empty.
        for sid, _title, renderer in self._SECTION_SPECS:
            html = renderer(entry) if entry else ''
            self._sections[sid].set_content_html(html)

    def _fit_header_height(self):
        """Pin the compact header QTextEdit to its document height.

        QTextEdit's heightSizeHint doesn't grow with content, so we measure
        the laid-out document at the current viewport width. Called from
        _populate_detail (immediate + one tick deferred) and resizeEvent so
        the height always tracks the final, post-show width - mirroring the
        self-correcting reflow the collapsible sections already do.
        """
        header = getattr(self, '_detail_header_text', None)
        if header is None:
            return
        doc = header.document()
        doc.setTextWidth(header.viewport().width())
        h = int(doc.size().height()) + 16
        header.setFixedHeight(max(h, 32))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The header width tracks the dialog width; recompute its height so
        # a manual resize doesn't leave a stale value (blank strip / clipped
        # row), the same way _CollapsibleSection handles its own resize.
        self._fit_header_height()

    def populate(self, entry, initial_tab='detail', reset_scroll=True):
        """Refresh header + both tab bodies with a new job entry.

        `reset_scroll=True` (default, used when the panel re-opens the
        dialog on a different job): scroll positions of Detail
        QScrollArea and Log QTextEdit are reset to top, so each open
        starts fresh. The Refresh button inside the dialog passes
        `reset_scroll=False` so re-pulling the same job preserves the
        user's current scroll position.
        """
        # In-flight overlay (Aborting…/Removing…) so the header matches the
        # row on BOTH first open and the dialog's Refresh button. Applied
        # here, the single render point, via the owning panel.
        panel = self.parent()
        if panel is not None and hasattr(panel, '_overlay_inflight_status'):
            entry = panel._overlay_inflight_status(entry)
        self._current_prompt_id = entry.get('prompt_id') if isinstance(entry, dict) else None
        self._initial_tab = initial_tab
        # Resolve the workflow API JSON from the local BLOB or the dialog's
        # fetch cache (fast, no network). Renderers below see it via
        # `entry['nfy_workflow_api']`. When neither has it (server-only job,
        # first look), `_start_workflow_api_fetch` below fetches it off-thread
        # and refreshes the dependent views on arrival.
        entry = self._inject_cached_workflow_api(entry)
        self._current_entry = entry
        job_ref = _short_job_ref(entry)
        self.setWindowTitle('Job - {}'.format(job_ref or 'Unknown'))
        self._header.setText(_render_job_header(entry))
        self._populate_detail(entry)
        self._log_messages = entry.get('nfy_messages', [])
        self._log_execution_error = entry.get('nfy_execution_error')
        self._log_text.setHtml(_format_log_messages(
            self._log_messages, self._log_execution_error,
            self._current_entry))
        self._workflow_tab.populate(entry)
        self._start_workflow_api_fetch(entry)
        if initial_tab == 'log':
            self._tabs.setCurrentIndex(1)
        elif initial_tab == 'workflow':
            self._tabs.setCurrentIndex(2)
        else:
            self._tabs.setCurrentIndex(0)
        if reset_scroll:
            # Detail tab is wrapped in a QScrollArea - reach via the
            # QTabWidget index 0 so we don't keep a stale handle to a
            # rebuilt widget. Log tab's QTextEdit owns its own scrollbar.
            detail_scroll = self._tabs.widget(0)
            if isinstance(detail_scroll, QtWidgets.QScrollArea):
                vbar = detail_scroll.verticalScrollBar()
                if vbar is not None:
                    vbar.setValue(0)
            log_vbar = self._log_text.verticalScrollBar()
            if log_vbar is not None:
                log_vbar.setValue(0)
            wf_view = getattr(self._workflow_tab, '_view', None)
            if wf_view is not None:
                wf_vbar = wf_view.verticalScrollBar()
                if wf_vbar is not None:
                    wf_vbar.setValue(0)

    def _active_tab_payload(self):
        """Return (kind, text, default_basename) for the currently active
        tab. `kind` is one of 'detail', 'log', 'workflow' - used to pick
        the file extension on Save."""
        idx = self._tabs.currentIndex()
        # Default basename: prefer nfy_job_id, then truncated prompt_id.
        entry = (self._store.get(self._current_prompt_id)
                 if self._store and self._current_prompt_id else {}) or {}
        job_ref = (entry.get('nfy_job_id', '')
                   or (self._current_prompt_id or '')[:8] or 'job')
        if idx == 1:
            return ('log',
                    _format_log_plaintext(self._log_messages,
                                          self._log_execution_error,
                                          self._current_entry),
                    'nukomfy_log_{}'.format(job_ref))
        if idx == 2:
            return ('workflow', self._workflow_tab.text(),
                    'nukomfy_workflow_{}'.format(job_ref))
        # Detail tab: header + every visible section's plain text.
        parts = [self._detail_header_text.toPlainText()]
        for sid, title, _renderer in self._SECTION_SPECS:
            sec = self._sections.get(sid)
            if sec is None or not sec.isVisible():
                continue
            body_text = sec._body.toPlainText()
            if body_text.strip():
                parts.append('')
                parts.append('--- {} ---'.format(title))
                parts.append(body_text)
        return ('detail', material_to_utf('\n'.join(parts).strip()),
                'nukomfy_detail_{}'.format(job_ref))

    def _on_copy_active(self):
        """Copy active tab content to the clipboard."""
        _kind, text, _name = self._active_tab_payload()
        if not text:
            return
        try:
            QtWidgets.QApplication.clipboard().setText(text)
        except Exception:
            _log.exception('Clipboard copy failed')

    def _on_save_active(self):
        """Save active tab content to a file. Workflow API -> .json,
        Detail / Log -> .txt."""
        kind, text, default_name = self._active_tab_payload()
        if not text:
            return
        if kind == 'workflow':
            ext = '.json'
            ffilter = 'JSON (*.json);;All files (*)'
        else:
            ext = '.txt'
            ffilter = 'Text (*.txt);;All files (*)'
        # Sanitise filename for cross-OS safety.
        suggested = ''.join(
            c if c.isalnum() or c in ('_', '-', '.') else '_'
            for c in default_name) + ext
        path = _dialogs.get_save_file(
            self, 'Save', suggested, ffilter)
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        except OSError as e:
            _log.exception('Render Manager tab save failed: %s', e)
            _dialogs.warn(
                self, 'Save failed',
                'Could not save:\n\n{}'.format(e))

    def _on_refresh_clicked(self):
        """Re-pull the current job and re-populate, preserving the active
        tab and scroll position.

        Source chain: the store first (synchronous - covers everything the
        Render Manager itself shows). When the store can't resolve the job,
        fall back to an off-thread re-fetch from the Suite's persistent
        history: rows opened from the All Jobs viewer bypass the store, and
        the store dies with the Render Manager while the viewer stays open.
        A failed re-fetch keeps the current content on screen - a Refresh
        that can't reach the server must not wipe what the user is reading.
        """
        pid = self._current_prompt_id
        if not pid:
            return
        # _store is dropped via its destroyed signal when the Render Manager
        # closes, so a dead store can't serve its frozen caches here.
        entry = self._store.get(pid) if self._store is not None else None
        if entry:
            self._repopulate_same_job(entry)
            return
        url = ''
        if isinstance(self._current_entry, dict):
            url = self._current_entry.get('nfy_machine_url', '') or ''
        if not url:
            return
        self._refetch_worker = stop_worker(self._refetch_worker)
        worker = _EntryRefetchWorker(url, pid)
        worker.result.connect(self._on_entry_refetched)
        worker.finished.connect(lambda w=worker: self._clear_refetch_worker(w))
        self._refetch_worker = worker
        worker.start()

    def _clear_refetch_worker(self, worker):
        # Same lifecycle as _clear_wf_worker: only null the reference if it
        # is still the current worker, always free the C++ thread object.
        if self._refetch_worker is worker:
            self._refetch_worker = None
        worker.deleteLater()

    def _on_entry_refetched(self, prompt_id, entry):
        """Apply an off-thread persistent-history result. Runs on the main
        thread. None (machine offline / job gone server-side) is dropped so
        the current content stays."""
        if prompt_id != self._current_prompt_id:
            return  # dialog moved to another job while the fetch ran
        if not (isinstance(entry, dict) and entry):
            return
        entry = dict(entry)
        # The server stores the submit-time machine URL (empty for jobs
        # sent outside Nukomfy); keep the URL this client actually reached
        # the machine at, so the workflow-API fetch stays routable.
        url = ''
        if isinstance(self._current_entry, dict):
            url = self._current_entry.get('nfy_machine_url', '') or ''
        if url:
            entry['nfy_machine_url'] = url
        self._repopulate_same_job(entry)

    def _repopulate_same_job(self, entry):
        """populate() wrapper for refreshing the job already on screen:
        keep the active tab and the scroll position."""
        pid = self._current_prompt_id
        idx = self._tabs.currentIndex()
        active_tab = (
            'log' if idx == 1
            else 'workflow' if idx == 2
            else 'detail')
        self.populate(entry, initial_tab=active_tab, reset_scroll=False)
        # `populate` re-derives _current_prompt_id from the entry; pin the
        # original pid so a malformed entry can't break the next Refresh.
        self._current_prompt_id = pid


# ---------------------------------------------------------------------------
# Left-elide delegate (_LeftElideDelegate, in render_queue_format) keeps the
# filename (rightmost path segment) visible when the NK File column is narrow.
# It draws the elided text itself instead of the style, because Nuke's Qt
# style on Linux re-elides and collapses long paths to bare dots otherwise.


class _JobDetailWidget(QtWidgets.QWidget):
    """Tab widget showing Queue and History for a single machine."""

    abort_requested = QtCore.Signal()       # triggers parent to refresh machine
    height_changed = QtCore.Signal()        # tab content height changed

    def __init__(self, queue_info, machine_url, initial_tab=0,
                 panel=None, parent=None,
                 pending_queue_pid=None, pending_history_pid=None):
        super().__init__(parent)
        self._machine_url = machine_url
        self._history_loaded = False
        self._history_items = []  # store for log access
        self._queue_jobs = []     # store for double-click access
        # Pending selection pids - set by the panel right before a
        # collapse+re-expand so the fresh widget can restore the user's
        # selection in Queue/History once the sub-tables are populated.
        # One-shot: cleared after applied.
        self._pending_queue_pid = pending_queue_pid
        self._pending_history_pid = pending_history_pid
        self._initial_tab = initial_tab  # applied in resizeEvent
        # Persistent dialogs live on the panel (stable across auto-refresh).
        # This widget is destroyed and recreated whenever a machine's detail
        # row is refreshed, so parenting dialogs here would tear them down
        # mid-use and crash on rapid reopen.
        self._panel = panel

        # {prompt_id: QProgressBar} - rebuilt on every Queue tab rebuild.
        # Slots look up the bar by prompt_id; stale entries from prior
        # rebuilds are naturally shed when the tab is recreated.
        self._progress_bars = {}

        # {prompt_id: _HatchedFillProgressCell} - the no-WS coarse bars.
        # No live WS signal drives these, so the panel refreshes them in
        # place from the shared cache on every store tick (poll cadence).
        self._poll_progress_cells = {}
        # Last-seen progress-endpoint availability for this machine. A flip
        # (Suite gained/lost the relay) changes which progress widget each
        # running row needs, so `_on_store_changed` forces a rebuild on
        # change. Seeded from the cache so the first tick is a no-op.
        self._last_endpoint_ok = (
            panel._progress_endpoint_ok.get(machine_url, True)
            if panel is not None else True)

        # One WS monitor per machine (singleton), signals connected once per
        # widget instance and disconnected in destroyed() to avoid dispatch
        # into a freed widget when the detail row is rebuilt on auto-refresh.
        self._ws_monitor = None
        if ws_client.AVAILABLE:
            self._ws_monitor = ws_client.get_manager().monitor_for(
                machine_url, ws_session_id())
            if self._ws_monitor is not None:
                self._ws_monitor.progress.connect(self._on_ws_progress)
                self._ws_monitor.lifecycle.connect(self._on_ws_lifecycle)
                # Qt calls destroyed() on any thread; we only use it to drop
                # the ref so that signal dispatch (still delivered via queued
                # connection) can detect the widget is gone.
                self.destroyed.connect(self._on_destroyed)

        status = queue_info.get('status')
        self._pending = status is None   # check still in progress
        self._offline = status == 'offline'

        # Signature of the last built Queue content - used by
        # `_on_store_changed` to skip rebuilds when nothing in the Queue
        # tab would visibly change (protects live WS progress bars from
        # flickering on every tick).
        self._last_queue_sig = None
        self._last_history_sig = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(20, 4, 4, 4)
        lay.setSpacing(0)

        self._tabs = QtWidgets.QTabWidget()
        self._tabs.setStyleSheet(TAB_STYLE)
        apply_tab_fit(self._tabs, 12)
        self._tabs.tabBar().setExpanding(False)
        self._tabs.tabBar().setUsesScrollButtons(False)
        lay.addWidget(self._tabs)

        # --- Queue tab (rebuilt on every refresh; see update_queue) ---
        self._queue_content = self._build_queue_tab(queue_info)
        self._tabs.addTab(self._queue_content, 'Queue')
        self._tabs.setFixedHeight(self._height_for_table(self._queue_content))

        # Apply pending Queue selection (cross-rebuild preservation: panel
        # passes the previously-selected pid as kwarg so the fresh widget
        # re-selects the same row once `_build_queue_tab` has populated it).
        if (self._pending_queue_pid
                and isinstance(self._queue_content, QtWidgets.QTableWidget)):
            for i, job in enumerate(self._queue_jobs):
                if job.get('prompt_id') == self._pending_queue_pid:
                    self._queue_content.selectRow(i)
                    self._queue_content.scrollTo(
                        self._queue_content.model().index(i, 0))
                    break
            self._pending_queue_pid = None  # one-shot consume

        # Subscribe to store mutations so Queue/History tabs re-render
        # from the central snapshot (single source of truth). Guarded by
        # signature so the Queue rebuild only runs when the running/pending
        # set actually changes - WS progress bars stay live otherwise.
        store = getattr(panel, '_store', None) if panel is not None else None
        if store is not None:
            store.storeChanged.connect(self._on_store_changed)
            # Ensure disconnect on teardown; self.destroyed may already be
            # connected above for WS cleanup - _on_destroyed handles both.
            if not ws_client.AVAILABLE:
                self.destroyed.connect(self._on_destroyed)

        # --- History tab (rebuilt when data arrives; see set_history) ---
        h_text = ('No recent history' if self._offline
                  else 'Updating\u2026')
        self._history_content = _placeholder_label(h_text)
        self._tabs.addTab(self._history_content, 'History')
        # Prevent tab bar from ever scrolling - both tabs must always fit
        bar = self._tabs.tabBar()
        bar.setMinimumWidth(bar.sizeHint().width())
        self._tabs.currentChanged.connect(self._on_tab_changed)
        # Initial height is measured before layout completes - re-fit after
        # the event loop drains so Qt reports real header/row sizes.
        QtCore.QTimer.singleShot(0, self._fit_height_deferred)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._initial_tab:
            self._tabs.setCurrentIndex(self._initial_tab)
            self._initial_tab = None

    def _height_for_table(self, table):
        """Calculate tab widget height from a QTableWidget's row count."""
        tab_bar_h = (self._tabs.tabBar().height()
                     or self._tabs.tabBar().sizeHint().height())
        if table is None or not isinstance(table, QtWidgets.QTableWidget):
            # Empty / placeholder: match the height of a 1-row table so
            # the pane doesn't visually jump when the first entry arrives.
            return tab_bar_h + 24 + 26
        # Use `.height()` (actual post-layout size) when Qt has finished
        # laying out - `sizeHint()` returns an arbitrary target that
        # overestimates by a few px. sizeHint() is the fallback only for
        # the first `_fit_height` pass, before the widget has rendered;
        # `_fit_height_deferred` re-fires after the event loop drains and
        # uses the real values. Rows summed via `rowHeight()` (authoritative
        # per-section height set by `setRowHeight`). No minimum floor for
        # tables - the empty-state label case already short-circuits above
        # with `100`, so forcing a floor here just creates dead
        # space below the last row when row count is small.
        rows_total = sum(table.rowHeight(i) for i in range(table.rowCount()))
        header_h = (table.horizontalHeader().height()
                    or table.horizontalHeader().sizeHint().height())
        frame_h = 2 * table.frameWidth()
        return tab_bar_h + header_h + rows_total + frame_h

    def _fit_height(self):
        """Resize tabs to fit the currently visible tab's content."""
        if self._tabs.currentIndex() == 0:
            h = self._height_for_table(self._queue_content)
        else:
            h = self._height_for_table(self._history_content)
        self._tabs.setFixedHeight(h)
        self.height_changed.emit()
        # Second pass deferred: on first call the table may not have
        # finished layout yet (header/rows return stale sizeHints). Re-fit
        # after the event loop drains so the final render has the exact
        # height - kills the phantom empty strip.
        QtCore.QTimer.singleShot(0, self._fit_height_deferred)

    def _fit_height_deferred(self):
        if self._tabs.currentIndex() == 0:
            h = self._height_for_table(self._queue_content)
        else:
            h = self._height_for_table(self._history_content)
        if h != self._tabs.height():
            self._tabs.setFixedHeight(h)
            self.height_changed.emit()

    def _build_queue_tab(self, queue_info):
        """Build a fresh Queue tab widget (table or placeholder label).

        Returned widget is inserted directly as the tab's page, replacing
        any prior one. Column widths persist across rebuilds via
        `ui_key='rq_queue_table_v4'`.

        Source of truth for running/pending jobs is the central
        `RenderDataStore`. `queue_info` is still consulted for the
        machine-level `status` (offline/idle/rendering) which lives
        outside the store's job indexes.
        """
        store = getattr(self._panel, '_store', None) if self._panel else None
        if store is not None:
            running, pending = store.queue_for_machine(self._machine_url)
        else:
            # Fallback when the widget is constructed without a panel
            # (shouldn't happen in production but keeps the widget usable
            # in isolated tests).
            running = queue_info.get('running_jobs', []) or []
            pending = queue_info.get('pending_jobs', []) or []

        # Cache signature so store-driven rebuilds can short-circuit when
        # no visible change is pending. Include the per-machine set of
        # `_pending_actions` pids so a click-triggered grey-out shows
        # up immediately (without this, the sig compares equal and the
        # rebuild gets skipped).
        pending_ids = self._panel_pending_ids()
        self._last_queue_sig = _queue_signature({
            'status': queue_info.get('status', ''),
            'running_jobs': running,
            'pending_jobs': pending,
        }, pending_ids)

        # Bars belong to rows we are about to rebuild; old refs are stale.
        self._progress_bars = {}
        self._poll_progress_cells = {}

        if self._pending:
            self._queue_jobs = []
            return _placeholder_label('Updating\u2026')

        if self._offline or (not running and not pending):
            self._queue_jobs = []
            return _placeholder_label('No jobs in queue')

        table = QtWidgets.QTableWidget(0, len(_Q_HEADERS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(DETAIL_STYLE)
        # ElideLeft so NK File path keeps the filename visible when narrow.
        table.setTextElideMode(QtCore.Qt.ElideLeft)
        table.setItemDelegateForColumn(
            _SUB_COL_NKFILE, _LeftElideDelegate(table))
        # Subtable is size-to-fit (_fit_height); no vertical scrollbar,
        # no frame border (otherwise leaves an empty strip below last row).
        table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        table.setFrameShape(QtWidgets.QFrame.NoFrame)
        _setup_table_columns(
            table, _Q_HEADERS, _Q_WIDTHS,
            fixed_cols={_SUB_COL_STATUS, _Q_COL_ACTIONS},
            stretch_col=_SUB_COL_NKFILE,
            ui_key='rq_queue_table_v4',
            pixel_widths={_Q_COL_ACTIONS: 22 + 3})
        table.cellDoubleClicked.connect(self._on_queue_double_click)
        table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self._on_queue_context_menu(t, pos))
        _install_empty_area_deselect(table)
        if self._panel is not None:
            table.itemSelectionChanged.connect(
                lambda t=table: self._panel._enforce_single_selection(t))

        self._queue_jobs = []
        for job in running:
            job['nfy_status_str'] = 'running'
            self._queue_jobs.append(job)
            self._add_job_row(table, job, is_running=True)
        # ComfyUI's `queue_pending` is a heap; iterating its underlying
        # list does NOT yield FIFO order for ties - for a batch of N jobs
        # submitted in the same instant, the next-to-run can land mid-
        # list (or last). Sort by `queue_position` (server-side monotonic
        # submit counter, captured by `_parse_queue_item`) so the row
        # right under the running one is always the next to execute.
        pending_sorted = sorted(
            pending, key=lambda j: j.get('queue_position', 0))
        for job in pending_sorted:
            job['nfy_status_str'] = 'pending'
            self._queue_jobs.append(job)
            self._add_job_row(table, job, is_running=False)
        return table

    def update_queue(self, queue_info):
        """Replace queue tab content with updated data (full rebuild).

        Rebuilding the whole tab page is what guarantees Qt repaints on
        every refresh. Column widths survive because `_build_queue_tab`
        passes `ui_key='rq_queue_table_v4'` to `_setup_table_columns`.
        """
        self._pending = False
        self._offline = queue_info.get('status') == 'offline'
        # Save in-widget selection (in case this is called outside the
        # cross-rebuild path - `_pending_queue_pid` covers that one).
        old_widget = self._tabs.widget(0)
        saved_queue_pid = None
        if isinstance(old_widget, QtWidgets.QTableWidget):
            cur_row = -1
            sm = old_widget.selectionModel()
            if sm is not None:
                rows = sm.selectedRows()
                if rows:
                    cur_row = rows[0].row()
            if cur_row < 0:
                cur_row = old_widget.currentRow()
            if 0 <= cur_row < len(self._queue_jobs):
                saved_queue_pid = self._queue_jobs[cur_row].get('prompt_id')

        cur_tab = self._tabs.currentIndex()
        new_widget = self._build_queue_tab(queue_info)
        self._tabs.removeTab(0)
        self._tabs.insertTab(0, new_widget, 'Queue')
        self._queue_content = new_widget
        if old_widget:
            old_widget.deleteLater()
        self._tabs.setCurrentIndex(cur_tab)

        restore_pid = saved_queue_pid or self._pending_queue_pid
        if restore_pid and isinstance(new_widget, QtWidgets.QTableWidget):
            for i, job in enumerate(self._queue_jobs):
                if job.get('prompt_id') == restore_pid:
                    new_widget.selectRow(i)
                    new_widget.scrollTo(new_widget.model().index(i, 0))
                    break
        self._pending_queue_pid = None  # one-shot consume

        self._fit_height()

    def _add_job_row(self, table, job, is_running=False):
        row = table.rowCount()
        table.insertRow(row)
        # Row height (26) aligned with main Machines table +
        # MyJobs. Anything tighter leaves only 1 px breathing
        # above/below the 22px action buttons + progress bar, which
        # breaks vertical uniformity when a row is selected (orange
        # selection bg leaks as a thin top/bottom strip).
        table.setRowHeight(row, 26)

        _fill_common_job_cells(table, row, job)

        prompt_id = job.get('prompt_id', '')
        # "Aborting\u2026" overlays a greyed look from two sources: the optimistic
        # click bridge (`_pending_actions`, sub-second) and the server-side
        # `nfy_aborting` flag carried on the running job - the latter survives
        # a panel reopen / Nuke restart. "Removing\u2026" is optimistic-only:
        # pending jobs are deleted, never flag-marked. The row is never
        # force-removed - it exits when the server stops reporting the job;
        # `_queue_signature` tracks `nfy_aborting` so the transition still
        # triggers a rebuild from the store's fresh queue lists.
        pending_action = None
        if self._panel is not None and prompt_id:
            pending_action = self._panel._pending_actions.get(prompt_id)
        pa_kind = pending_action.get('kind', '') if pending_action else ''
        is_aborting = bool(job.get('nfy_aborting')) or pa_kind == 'abort'
        is_removing = pa_kind == 'remove'
        greyed = is_aborting or is_removing
        if is_aborting:
            # Overwrite the status cell set by `_fill_common_job_cells`.
            table.setCellWidget(
                row, _SUB_COL_STATUS,
                _make_status_cell('', ABORTING_LABEL, INFLIGHT_COLOR))
        elif is_removing:
            table.setCellWidget(
                row, _SUB_COL_STATUS,
                _make_status_cell('', REMOVING_LABEL, INFLIGHT_COLOR))

        # Progress cell. Which widget appears depends on whether progress
        # DATA is actually reachable, not just on the local websocket-client
        # package:
        #   aborting/removing      -> greyed flat bar (cached value distrusted)
        #   running + live WS      -> live QProgressBar (updates via signal)
        #   pending                -> greyed flat bar at 0% ("not started")
        #   running, polled value  -> hatched-fill bar (% from poll, not live)
        #   running, no data       -> fully hatched cell (Suite without relay)
        cached = None
        if self._panel is not None and prompt_id:
            cached = self._panel._live_progress.get(
                (self._machine_url, prompt_id))
        cached_frac = cached.get('fraction') if cached else None
        cached_tip = cached.get('tooltip', '') if cached else ''
        # Optimistic until a poll proves the route absent, so a just-started
        # job still gets a live bar before the first poll lands.
        endpoint_ok = True
        if self._panel is not None:
            endpoint_ok = self._panel._progress_endpoint_ok.get(
                self._machine_url, True)
        # Live WS bar needs a WS channel AND a reason to expect progress: the
        # Suite serves the poll route (`endpoint_ok`, optimistic until the
        # first poll proves otherwise) OR events have already arrived
        # (`cached_frac`). So a Suite-less machine (404 route, no relay, no
        # events) falls through to the hatched cell instead of a bar stuck at
        # 0, while a relay-but-no-endpoint Suite (events flow) keeps its live
        # bar via `cached_frac`. The endpoint flip in `_on_store_changed`
        # rebuilds the row once the first poll settles `endpoint_ok`.
        ws_usable = (is_running and self._ws_monitor is not None
                     and bool(prompt_id)
                     and (endpoint_ok or cached_frac is not None))
        if greyed:
            # Greyed flat bar: once the job is being torn down (aborting or
            # removing) we don't trust cached progress.
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setEnabled(False)
            table.setCellWidget(row, _Q_COL_PROGRESS, bar)
        elif ws_usable:
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(max(0.0, min(1.0, cached_frac or 0.0)) * 100))
            bar.setTextVisible(True)
            bar.setFormat('%p%')
            bar.setAlignment(QtCore.Qt.AlignCenter)
            if cached_tip:
                bar.setToolTip(cached_tip)
            table.setCellWidget(row, _Q_COL_PROGRESS, bar)
            self._progress_bars[prompt_id] = bar
        elif not is_running:
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setEnabled(False)
            table.setCellWidget(row, _Q_COL_PROGRESS, bar)
        elif cached_frac is not None or endpoint_ok:
            # Running, no live WS channel, but the poll has (or will have) a
            # value: a real bar whose fill is diagonally hatched to signal
            # "polled, not live". Refreshed in place from the cache per tick.
            cell = _HatchedFillProgressCell(
                fraction=cached_frac or 0.0,
                tooltip=(_coarse_progress_tooltip(cached_tip)
                         if not ws_client.AVAILABLE else (cached_tip or None)))
            table.setCellWidget(row, _Q_COL_PROGRESS, cell)
            self._poll_progress_cells[prompt_id] = cell
        else:
            # No live data on any channel (e.g. a ComfyUI without the Suite
            # relay): the fully hatched cell signals "progress unavailable".
            tip = (_WS_MISSING_TOOLTIP
                   if is_running and not ws_client.AVAILABLE else None)
            table.setCellWidget(row, _Q_COL_PROGRESS,
                                _HatchedProgressCell(tooltip=tip))

        # Actions - semantically distinct buttons:
        #   running -> Abort (CLOSE icon, red)           - interrupts GPU work
        #   pending -> Remove from queue (REMOVE, orange) - just dequeues
        #   aborting/removing -> greyed + disabled (server flag or in-flight click)
        #   external (no nfy_job_id) -> greyed + disabled (managed elsewhere)
        if greyed:
            btn_char = CLOSE if is_aborting else REMOVE
            base_color, hover_color, hover_bg = '#555', '#555', '#2a2a2a'
            tooltip = ABORTING_LABEL if is_aborting else REMOVING_LABEL
            enabled = False
        elif not job.get('nfy_job_id'):
            # Job submitted outside Nukomfy (e.g., ComfyUI web UI):
            # neither abort nor dequeue is the plugin's responsibility.
            btn_char = CLOSE if is_running else REMOVE
            base_color, hover_color, hover_bg = '#555', '#555', '#2a2a2a'
            tooltip = 'External job. Manage it from ComfyUI directly.'
            enabled = False
        elif is_running:
            btn_char, base_color, hover_color, hover_bg = (
                CLOSE, ERROR_COLOR, ERROR_HOVER, '#3a2020')
            tooltip = 'Abort this job'
            enabled = True
        else:
            btn_char, base_color, hover_color, hover_bg = (
                REMOVE, WARNING_STATUS, WARNING_STATUS_HOVER, '#3a2f1a')
            tooltip = 'Remove from queue'
            enabled = True
        abort_btn = QtWidgets.QPushButton(btn_char)
        abort_btn.setFont(icon_font(14))
        abort_btn.setFixedSize(22, 22)
        abort_btn.setToolTip(tooltip)
        abort_btn.setEnabled(enabled)
        abort_btn.setStyleSheet(
            cell_action_colored(base_color, hover_color, hover_bg))
        abort_btn.clicked.connect(
            lambda _=False, j=job, run=is_running: self._abort_job(j, run))
        table.setCellWidget(row, _Q_COL_ACTIONS, _centered_cell(abort_btn))

    # -- WebSocket slots ----------------------------------------------------
    # Live progress bar + active-node tooltip driven by `ws_client`.
    # All slots defensively check that the bar ref is still valid: the
    # signal is queued cross-thread and may fire after the Queue tab has
    # been rebuilt (stale prompt_id) or after the widget is destroyed.

    def _on_ws_progress(self, prompt_id, fraction, tooltip_text):
        # Mirror into the shared panel cache (ratchet max) so a rebuild or a
        # second view (MyJobs, a re-expand) reseeds from the live value, not
        # the last poll. The server already ratchets; the max() also guards
        # against a stale poll value landing after a higher WS value.
        if self._panel is not None and prompt_id:
            key = (self._machine_url, prompt_id)
            prev = self._panel._live_progress.get(key)
            prev_frac = prev.get('fraction', 0.0) if prev else 0.0
            self._panel._live_progress[key] = {
                'fraction': max(prev_frac, float(fraction)),
                'tooltip': tooltip_text
                or (prev.get('tooltip', '') if prev else ''),
            }
        bar = self._progress_bars.get(prompt_id)
        if bar is None:
            return
        try:
            pct = max(0, min(100, int(fraction * 100)))
            bar.setValue(max(bar.value(), pct))
            bar.setFormat('%p%')
            bar.setToolTip(tooltip_text or '')
            # Live-refresh the tooltip if the user is currently hovering the
            # bar - setToolTip alone doesn't update an already-visible popup.
            if tooltip_text and QtWidgets.QToolTip.isVisible() \
                    and bar.underMouse():
                QtWidgets.QToolTip.showText(
                    QtGui.QCursor.pos(), tooltip_text, bar)
        except RuntimeError:
            # Qt widget gone between lookup and use - drop the stale ref.
            self._progress_bars.pop(prompt_id, None)

    def _on_ws_lifecycle(self, prompt_id, event):
        # On terminal events, ask the parent panel to refresh its view so the
        # row migrates from Queue to History with the correct server status.
        if event in ('success', 'error', 'interrupted'):
            self._progress_bars.pop(prompt_id, None)
            self._poll_progress_cells.pop(prompt_id, None)
            if self._panel is not None:
                self._panel._live_progress.pop(
                    (self._machine_url, prompt_id), None)
            self.abort_requested.emit()

    def _refresh_poll_progress_cells(self):
        """Push the latest cached fraction into the no-WS coarse bars.

        These have no live WS signal, so the panel's poll-fed cache is their
        only update source; called on every store tick (poll cadence) when a
        full rebuild is skipped. Tooltip stays the build-time one.
        """
        panel = self._panel
        if panel is None or not self._poll_progress_cells:
            return
        for pid, cell in list(self._poll_progress_cells.items()):
            data = panel._live_progress.get((self._machine_url, pid))
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

    def _on_destroyed(self, *_args):
        """Disconnect WS + store signals when this widget is torn down.

        Called from the main thread just before the Python wrapper is gone.
        Prevents queued emissions from reaching a freed widget on auto-
        refresh (the detail widget is recreated on every update_queue).
        """
        # Detach store listener first - storeChanged is emitted from the
        # panel on every tick and must not fire into a destroyed widget.
        panel = self._panel
        self._panel = None
        store = getattr(panel, '_store', None) if panel is not None else None
        if store is not None:
            try:
                store.storeChanged.disconnect(self._on_store_changed)
            except (RuntimeError, TypeError):
                pass

        mon = self._ws_monitor
        self._ws_monitor = None
        if mon is None:
            return
        try:
            mon.progress.disconnect(self._on_ws_progress)
        except (RuntimeError, TypeError):
            pass
        try:
            mon.lifecycle.disconnect(self._on_ws_lifecycle)
        except (RuntimeError, TypeError):
            pass

    def _on_store_changed(self):
        """React to central store mutations.

        Queue tab is rebuilt only when its signature (status + running +
        pending prompt_ids) changes, so live WS progress bars of unchanged
        running jobs are preserved tick-to-tick. History tab rebuilds from
        the server-side recent-terminals cache on every emit (cheap - no
        live widgets to preserve).
        """
        panel = self._panel
        store = getattr(panel, '_store', None) if panel is not None else None
        if store is None or not self._machine_url:
            return
        info = store.machine_info(self._machine_url) or {}
        status = info.get('status')
        # Keep offline/pending flags in sync with the store's view of the
        # machine - the widget was built once with `queue_info` but the
        # machine's availability can flip between ticks.
        self._pending = status is None
        self._offline = status == 'offline'
        running, pending = store.queue_for_machine(self._machine_url)
        new_sig = _queue_signature({
            'status': status or '',
            'running_jobs': running,
            'pending_jobs': pending,
        }, self._panel_pending_ids())
        # A progress-endpoint flip (Suite gained/lost the relay) changes the
        # widget each running row needs but leaves the running/pending sig
        # unchanged, so force a rebuild on change.
        endpoint_ok = (
            self._panel._progress_endpoint_ok.get(self._machine_url, True)
            if self._panel is not None else True)
        endpoint_flipped = endpoint_ok != self._last_endpoint_ok
        self._last_endpoint_ok = endpoint_ok
        if new_sig != self._last_queue_sig or endpoint_flipped:
            # Reuse update_queue() so tab selection + height fit go through
            # the same path as user-initiated refreshes.
            self.update_queue({'status': status,
                               'running_jobs': running,
                               'pending_jobs': pending})
        else:
            # Sig unchanged: advance the no-WS coarse bars in place from the
            # freshly-polled cache (they have no live WS signal of their own).
            self._refresh_poll_progress_cells()
        # History tab is redrawn from the store's server-side cache
        # independently.
        if self._history_loaded:
            self._refresh_history_from_store()

    def _abort_job(self, job, is_running):
        """Delegate Abort/Remove to the unified flow shared with MyJobs.

        `_abort_or_remove_entry` owns ownership checks, confirm dialog,
        async POST via `_ActionWorker`, optimistic grey-out via
        `panel._pending_actions`, and server-confirmed row drop via
        `_verify_pending_actions`. Queue sub-table and MyJobs Active now
        use a single code path so a click in either place produces the
        same feedback and resolution semantics.
        """
        panel = self._panel
        if panel is None:
            return
        _abort_or_remove_entry(
            parent=self, panel=panel, entry=job,
            is_running=is_running, machine_url=self._machine_url)

    def _panel_pending_ids(self):
        """Return the pids with an optimistic abort/remove grey-out on this
        machine. Folded into `_queue_signature` so a click-triggered
        grey-out - which doesn't alter the running/pending lists - still
        rebuilds the Queue tab. The server-side `nfy_aborting` flag is
        tracked separately, inside `_queue_signature`'s running tuple."""
        panel = self._panel
        if panel is None or not self._machine_url:
            return set()
        return {pid for pid, pa in panel._pending_actions.items()
                if pa.get('nfy_machine_url') == self._machine_url}

    def _on_tab_changed(self, index):
        if index == 1 and not self._history_loaded:
            if self._pending:
                return  # wait for check to complete
            self._history_loaded = True
            if not self._offline:
                # History is served from the central store (all users on
                # this machine, server-backed). First paint reads whatever
                # the last fetch produced; subsequent emissions refresh
                # automatically via `_on_store_changed`.
                self._refresh_history_from_store()
            # Offline case: the placeholder label built in __init__ already
            # reads "No recent history" - no action needed.
        self._fit_height()

    def _refresh_history_from_store(self):
        """Repaint the History tab from the store's recent-terminals cache.

        Shows Nukomfy submits from ANY user on this machine - the cache is
        populated from `/api/jobs?limit=N` (server-backed, includes jobs
        from external clients like the ComfyUI web UI). Entries without
        `nfy_job_id` are filtered out: the History sub-table only tracks
        what was submitted through this plugin. Local-user scoping lives
        only in MyJobs; this sub-table stays cross-user for Nukomfy jobs.
        """
        panel = self._panel
        store = getattr(panel, '_store', None) if panel is not None else None
        if store is None or not self._machine_url:
            return
        try:
            limit = int(settings.history_limit)
        except (ValueError, TypeError):
            limit = 10
        # Fetch unbounded then filter+slice locally so the visible row
        # count is always N Nukomfy entries, never (N - externals).
        items = store.recent_terminals_for_machine(
            self._machine_url, limit=None)
        items = [it for it in items if it.get('nfy_job_id')][:limit]
        sig = tuple(
            (it.get('prompt_id', ''),
             it.get('nfy_status_str', ''),
             it.get('nfy_duration', 0) or 0,
             it.get('nfy_execution_error') or '')
            for it in items
        )
        if sig == self._last_history_sig:
            return
        self._last_history_sig = sig
        self.set_history(items)

    def _build_history_tab(self, items):
        """Build a fresh History tab widget (table or placeholder label).

        Returned widget is inserted directly as the tab's page, replacing
        any prior one. Column widths persist across rebuilds via
        `ui_key='rq_history_table_v4'`.
        """
        if not items:
            return _placeholder_label('No recent history')

        table = QtWidgets.QTableWidget(0, len(_H_HEADERS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setStyleSheet(DETAIL_STYLE)
        table.setTextElideMode(QtCore.Qt.ElideLeft)
        table.setItemDelegateForColumn(
            _SUB_COL_NKFILE, _LeftElideDelegate(table))
        table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        table.setFrameShape(QtWidgets.QFrame.NoFrame)
        _setup_table_columns(
            table, _H_HEADERS, _H_WIDTHS,
            fixed_cols={_SUB_COL_STATUS, _H_COL_READ, _H_COL_LOG},
            stretch_col=_SUB_COL_NKFILE,
            ui_key='rq_history_table_v4',
            pixel_widths={_H_COL_READ: 22 + 3, _H_COL_LOG: 22 + 3})
        table.cellDoubleClicked.connect(self._on_history_double_click)
        table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self._on_history_context_menu(t, pos))
        _install_empty_area_deselect(table)
        if self._panel is not None:
            table.itemSelectionChanged.connect(
                lambda t=table: self._panel._enforce_single_selection(t))

        for item in items:
            row = table.rowCount()
            table.insertRow(row)
            # Aligned with Queue sub-table + MyJobs (26).
            table.setRowHeight(row, 26)

            _fill_common_job_cells(table, row, item)

            dur_item = QtWidgets.QTableWidgetItem(
                _format_duration(item.get('nfy_duration', 0)))
            dur_item.setTextAlignment(QtCore.Qt.AlignCenter)
            table.setItem(row, _H_COL_DURATION, dur_item)

            # Read Output(s) button - green when outputs are available
            # (same affordance as MyJobs / the machine job viewer).
            outputs = item.get('nfy_output_paths') or []
            status_str = (item.get('nfy_status_str') or '').lower()
            can_read = (bool(outputs)
                        and status_str not in ('cancelled', 'failed'))
            try:
                read_color = int(item.get('nfy_read_color', 0) or 0)
            except (ValueError, TypeError):
                read_color = 0
            read_btn = QtWidgets.QPushButton(FILE_DOWNLOAD)
            read_btn.setFont(icon_font(14))
            read_btn.setFixedSize(22, 22)
            if can_read:
                read_btn.setToolTip(
                    'Read Output(s): create Read nodes for these outputs')
                read_btn.setStyleSheet(
                    cell_action_colored(SUCCESS_COLOR, SUCCESS_HOVER, '#2a3a2a'))
                read_btn.clicked.connect(
                    lambda _=False, p=list(outputs), c=read_color:
                        read_outputs(table, p, c))
            else:
                read_btn.setEnabled(False)
                read_btn.setToolTip('No outputs available for this job')
                read_btn.setStyleSheet(BUTTON_STYLE_CELL_ACTION)
            table.setCellWidget(row, _H_COL_READ, _centered_cell(read_btn))

            # Log button - Material icon for visual consistency with MyJobs.
            log_btn = QtWidgets.QPushButton(DESCRIPTION)
            log_btn.setFont(icon_font(14))
            log_btn.setFixedSize(22, 22)
            log_btn.setToolTip('View execution log')
            log_btn.setStyleSheet(BUTTON_STYLE_CELL_ACTION)
            log_btn.clicked.connect(
                lambda _=False, i=item: self._show_log(i))
            table.setCellWidget(row, _H_COL_LOG, _centered_cell(log_btn))

        return table

    def set_history(self, items):
        """Replace history tab content with fetched data (full rebuild)."""
        old_widget = self._tabs.widget(1)
        saved_pid = None
        if isinstance(old_widget, QtWidgets.QTableWidget):
            cur_row = -1
            sel_model = old_widget.selectionModel()
            if sel_model is not None:
                rows = sel_model.selectedRows()
                if rows:
                    cur_row = rows[0].row()
            if cur_row < 0:
                cur_row = old_widget.currentRow()
            if 0 <= cur_row < len(self._history_items):
                saved_pid = self._history_items[cur_row].get('prompt_id')

        self._history_items = items
        cur_tab = self._tabs.currentIndex()
        new_widget = self._build_history_tab(items)
        self._tabs.removeTab(1)
        self._tabs.insertTab(1, new_widget, 'History')
        self._history_content = new_widget
        if old_widget:
            old_widget.deleteLater()
        self._tabs.setCurrentIndex(cur_tab)

        # `saved_pid` covers in-place rebuilds inside the same widget.
        # `_pending_history_pid` covers cross-widget rebuilds: the panel
        # collapses/re-expands the detail row on a `_on_result` rebuild,
        # which destroys this widget - the previous selection is read by
        # the panel before collapse and re-injected here as fallback.
        restore_pid = saved_pid or self._pending_history_pid
        if restore_pid and isinstance(new_widget, QtWidgets.QTableWidget):
            for i, it in enumerate(items):
                if it.get('prompt_id') == restore_pid:
                    new_widget.selectRow(i)
                    new_widget.scrollTo(new_widget.model().index(i, 0))
                    break
        self._pending_history_pid = None  # one-shot consume

        self._fit_height()

    def get_current_selection(self):
        """Return (queue_pid, history_pid) for the rows currently selected
        in Queue and History sub-tables. Used by the panel to preserve
        selection across collapse+re-expand of the detail widget.
        """
        def _read(widget, items):
            if not isinstance(widget, QtWidgets.QTableWidget):
                return None
            cur_row = -1
            sm = widget.selectionModel()
            if sm is not None:
                rows = sm.selectedRows()
                if rows:
                    cur_row = rows[0].row()
            if cur_row < 0:
                cur_row = widget.currentRow()
            if 0 <= cur_row < len(items):
                return items[cur_row].get('prompt_id')
            return None

        queue_pid = _read(self._tabs.widget(0), self._queue_jobs)
        history_pid = _read(self._tabs.widget(1), self._history_items)
        return queue_pid, history_pid

    def _show_log(self, item):
        """Open the unified Job dialog on the Log tab for a history item.

        Delegates to the panel-owned persistent dialog so auto-refresh
        (which destroys and rebuilds this widget) doesn't tear it down.
        """
        self._panel.show_job_dialog(item, initial_tab='log')

    def _on_queue_double_click(self, row, _col):
        """Open the unified Job dialog on the Detail tab for a queue job.

        Routes through `store.get(pid)` so the entry is enriched with
        client-side fields (`status_str`, `seeds_used`, `machine_name`)
        not present in the raw /api/queue payload - same path used by
        the dialog's Refresh button, so first open and refresh render
        identically. Reloads local-history first because a job
        submitted between fetch ticks isn't yet in the store cache
        (`load_local_history` is fired by `_on_result` only when
        terminals are persisted, not for fresh submits).
        """
        if row < 0 or row >= len(self._queue_jobs):
            return
        row_job = self._queue_jobs[row]
        # External jobs (no Nukomfy metadata) open an all-empty dialog;
        # suppress the double-click, mirroring the greyed context-menu
        # detail item and abort button for them.
        if not row_job.get('nfy_job_id'):
            return
        pid = row_job.get('prompt_id')
        store = self._panel._store if self._panel else None
        if store is not None:
            try:
                store.load_local_history()
            except Exception:
                pass
        job = store.get(pid) if (store and pid) else None
        if not job:
            job = dict(row_job)
        self._panel.show_job_dialog(job, initial_tab='detail')

    def _on_history_double_click(self, row, _col):
        """Open the unified Job dialog on the Detail tab for a history item.

        Routes through `store.get(pid)` so the entry is enriched with
        client-side fields (`machine_name`) not present in the raw
        /api/jobs payload - same path used by Queue double-click and the
        dialog's Refresh button. Falls back to the raw server entry if
        the pid is unknown to the store (e.g. another user's job).
        Reloads local-history first to pick up any submit_history.json
        change since the last fetch tick.
        """
        if row < 0 or row >= len(self._history_items):
            return
        raw = self._history_items[row]
        pid = raw.get('prompt_id')
        store = self._panel._store if self._panel else None
        if store is not None:
            try:
                store.load_local_history()
            except Exception:
                pass
        job = store.get(pid) if (store and pid) else None
        if not job:
            job = raw
        self._panel.show_job_dialog(job, initial_tab='detail')

    def _on_queue_context_menu(self, table, pos):
        if self._panel is None:
            return
        row = table.rowAt(pos.y())
        if row < 0 or row >= len(self._queue_jobs):
            return
        show_job_context_menu(
            self,
            table.viewport().mapToGlobal(pos),
            self._queue_jobs[row],
            kind='queue_machine',
            panel=self._panel,
            machine_url=self._machine_url)

    def _on_history_context_menu(self, table, pos):
        if self._panel is None:
            return
        row = table.rowAt(pos.y())
        if row < 0 or row >= len(self._history_items):
            return
        show_job_context_menu(
            self,
            table.viewport().mapToGlobal(pos),
            self._history_items[row],
            kind='history_machine',
            panel=self._panel,
            machine_url=self._machine_url)


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------
# Machine table columns (Status column merges dot + status text)
_COL_STATUS  = 0
_COL_NAME    = 1
_COL_QUEUE   = 2
_COL_COMFY   = 3
_COL_OS      = 4
_COL_GPU     = 5
_COL_VRAM    = 6
_COL_RAM     = 7
_COL_REFRESH = 8
_HEADERS     = ['Status', 'Name', 'Queue', 'ComfyUI', 'OS', 'GPU', 'VRAM', 'RAM', '']

_M_WIDTHS = {0: 120, 1: 200, 2: 80, 3: 80, 4: 80, 5: 250, 6: 85, 7: 85,
             8: 22 * 2 + 5}   # dual action button (gap-2)



def _queue_signature(info, pending_ids=()):
    """Stable hashable repr of queue state for change detection.

    Two ticks with the same signature mean nothing visible in the expanded
    detail widget would change, so we can skip the costly collapse+rebuild.

    `pending_ids` is the set of prompt_ids with an active
    `_pending_actions` entry on this machine. Including it in the sig is
    what makes a click-triggered grey-out (which doesn't alter
    running/pending lists) still cause the detail widget to rebuild.
    Callers that don't care (panel-level fetch-result comparisons) can
    omit it - default `()` stays self-consistent.

    Note: `running`/`pending` are int counts in the info dict - the actual
    job lists live under `running_jobs`/`pending_jobs` (see check_queue_status).
    """
    if not info:
        return None
    status = info.get('status', '')
    # `nfy_aborting` rides with each running pid: an aborting job keeps the
    # same prompt_id, so without it the signature wouldn't change and the
    # "Aborting…" rebuild would be skipped.
    running = tuple((j.get('prompt_id', ''), bool(j.get('nfy_aborting')))
                    for j in info.get('running_jobs', []))
    pending = tuple(j.get('prompt_id', '')
                    for j in info.get('pending_jobs', []))
    return (status, running, pending, tuple(sorted(pending_ids)))




class RenderQueuePanel(QtWidgets.QDialog):

    def __init__(self, parent=None, expand_machine_id=None):
        super().__init__(parent)
        # machine_id to auto-expand after the initial populate (set by
        # `show_render_queue` after a submit).
        self._pending_expand_id = expand_machine_id
        self.setWindowTitle('Nukomfy - Render Manager')
        # Default panel size is comfortable for the full machines table;
        # the floor here is set lower so the user can shrink down to a
        # MyJobs-only view (the 2 MyJobs tables are the smallest useful
        # configuration). Qt's layout sizeHint provides a natural floor
        # below this value if widgets actually need more.
        self.setMinimumSize(800, 450)
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
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.destroyed.connect(self._on_destroyed)
        apply_window_chrome(self)
        _focus_drop.install(self)
        self._worker = None
        self._single_workers = {}     # {machine_id: UnifiedFetchWorker}
        self._refresh_btns = {}       # {machine_id: QPushButton}
        self._refresh_cycle = RefreshCycle(self._on_refresh_ready)
        self._single_busy_start = {}      # {machine_id: monotonic stamp of greyed icon}
        # Auto-refresh countdown bound to the "Update All" button. See
        # `gui/_auto_refresh.py` - the timer is instantiated after the
        # button is built (in `_build`), so we keep a placeholder here.
        self._auto_refresh = None
        self._expanded_row = -1       # currently expanded machine row
        self._expanded_id = None      # machine_id of expanded row
        self._detail_row = -1         # row index of the detail widget
        self._detail_widget = None
        # Re-entry guard for the one-selection coordinator
        # (_enforce_single_selection): the machine table and the detail
        # Queue/History sub-tables share a single logical selection, so
        # selecting in one clears the others without looping.
        self._sel_sync_active = False
        self._queue_data = {}         # machine_id -> queue info dict
        # prompt_ids removed locally via "Remove from queue" / abort,
        # mapped to the monotonic timestamp of the removal. Filtered out
        # of fresh server responses in `_on_result` until either (a) the
        # server confirms the deletion (stops reporting the ID on the same
        # machine) or (b) `_LOCALLY_REMOVED_TTL_S` elapses. Without this
        # filter, a `check_queue` poll that fires right after the local
        # removal and still sees the just-deleted job would cause a
        # signature mismatch -> detail widget rebuild -> QProgressBars of
        # OTHER active jobs reset to 0%.
        self._locally_removed = {}    # {prompt_id: monotonic_ts}
        # Optimistic action flow: between the user clicking Abort/Remove
        # and the first fetch carrying the server's new state, the entry
        # sits here as a sub-second bridge. The row greys + disables the
        # button so the user sees "action in flight". `_on_store_changed`
        # then hands off: for abort, once the server reports the job gone
        # (terminal) or flagged `nfy_aborting`, the entry is cleared and
        # the server-side state drives the row from there (so "Aborting…"
        # survives a panel reopen); for remove, `_on_action_done` finalises
        # it synchronously. No popups on success - the greyed row IS the
        # feedback, server is source of truth.
        # {prompt_id: {'kind': 'abort'|'remove', 'nfy_machine_url': str,
        #              'armed': bool}}
        self._pending_actions = {}
        # Live progress cache, owned by the panel so it survives the
        # collapse+rebuild of every _JobDetailWidget. Keyed (machine_url,
        # prompt_id) -> {'fraction': float, 'tooltip': str}; fed by both the
        # WS stream and the periodic poll, ratcheted (max). Detail/MyJobs
        # bars seed from it so a value survives a Nuke restart, a second
        # viewer, or a Queue rebuild mid-render.
        self._live_progress = {}
        # machine_url -> bool: did the last poll that checked find the
        # /nukomfy/progress route? Lets the no-WS path tell a Suite that
        # serves progress (-> coarse bar) from one that doesn't (-> hatched).
        # Optimistic default (absent == assume present) until a poll proves
        # otherwise, so a just-started job still gets a live bar.
        self._progress_endpoint_ok = {}
        # Background workers spawned for async abort/remove POSTs. Held here
        # so Qt doesn't GC them mid-run; each is reaped on its `finished`
        # signal (`_reap_action_worker`), and any still running are stopped
        # in `_stop_workers` on panel close.
        self._action_workers = []
        # Cached username for "is my job?" matches against
        # `nfy_submitted_by` in store entries (bold indicator, etc).
        # Hostname is metadata, never part of the ownership match.
        self._my_username = current_user()
        # Persistent dialog owned by the panel (not by _JobDetailWidget,
        # which is destroyed and rebuilt on every auto-refresh). Keeping
        # it here prevents use-after-free on rapid reopen after a refresh.
        self._job_dialog = None
        # Central in-memory store backing Queue/History sub-tables and
        # MyJobs. Populated by `_on_result` from unified fetch cycles; all
        # views consume it read-only via `storeChanged`.
        self._store = RenderDataStore(self)
        self._store.load_local_history()
        self._store.storeChanged.connect(self._on_store_changed)
        # Debounce store.notify() during Update All: N parallel fetches would
        # otherwise trigger N full MyJobs reloads (file IO + table rebuild)
        # on the main thread, freezing the UI. Each `_on_result` schedules
        # this single-shot timer; any further result within the quiet window
        # restarts it, so the fan-out fires once per cycle.
        self._notify_timer = QtCore.QTimer(self)
        self._notify_timer.setSingleShot(True)
        self._notify_timer.setInterval(150)
        self._notify_timer.timeout.connect(self._fire_store_notify)
        self._build()
        self._initial_refresh()
        ui_state.restore_geometry('render_queue_panel', self, with_position=True,
                                  fit=True)
        # Auto-expand the target machine (if provided) once the rows
        # exist - `_populate()` runs from `_initial_refresh`, so the
        # rows are ready by this point.
        self._auto_expand_pending()
        # Refresh the hardware/version snapshot once per session - the unified
        # worker polls /queue + /api/jobs only, never /system_stats, so these
        # columns would otherwise show a stale cache. Connect before the sweep
        # so the first results repaint these rows.
        from Nukomfy.gui._machine_info_service import service
        self._machine_info_service = service()
        self._machine_info_service.infoChanged.connect(self._on_machine_info_changed)
        self._machine_info_service.ensure_fresh()

    def _close_child_dialogs(self):
        """Trigger save_geometry on child dialogs by calling close() before
        Qt destroys them as children. Without this, parent destruction skips
        their closeEvent/done() and ui_state is never persisted."""
        if self._job_dialog is not None and self._job_dialog.isVisible():
            self._job_dialog.close()

    def done(self, result):
        ui_state.save_geometry('render_queue_panel', self, with_position=True)
        self._close_child_dialogs()
        self._stop_workers()
        super().done(result)

    def closeEvent(self, event):
        ui_state.save_geometry('render_queue_panel', self, with_position=True)
        self._close_child_dialogs()
        self._stop_workers()
        global _instance
        _instance = None
        super().closeEvent(event)

    def _on_destroyed(self):
        """Safety net: stop workers if Nuke exits without closeEvent."""
        try:
            self._stop_workers()
        except (RuntimeError, AttributeError):
            pass
        # The MachineInfoService outlives this panel (process singleton);
        # drop the subscription so a late sweep result can't reach a freed row.
        try:
            self._machine_info_service.infoChanged.disconnect(
                self._on_machine_info_changed)
        except (RuntimeError, TypeError, AttributeError):
            pass
        global _instance
        _instance = None

    def _stop_workers(self):
        if self._auto_refresh is not None:
            self._auto_refresh.stop()
        self._worker = stop_worker(self._worker)
        for mid in list(self._single_workers):
            stop_worker(self._single_workers[mid])
        self._single_workers.clear()
        # Abandon any in-flight abort/remove POST so a still-running thread
        # isn't destroyed with the panel ("QThread: Destroyed while thread is
        # still running"). stop_worker reparents to None + holds a ref until
        # it exits; finished ones were already reaped.
        for w in list(self._action_workers):
            stop_worker(w)
        self._action_workers.clear()
        # Same hazard for the Job dialog's workflow-API fetch: the dialog is
        # a child of this panel and dies with it.
        if self._job_dialog is not None:
            self._job_dialog.stop_workers()

    # ------------------------------------------------------------------
    # Unified Job dialog accessor (used by sub-tables and MyJobs)
    # ------------------------------------------------------------------
    def show_job_dialog(self, entry, initial_tab='detail'):
        """Open the unified Job dialog on *entry* (Detail or Log tab).

        Non-modal: blocking exec_() from a cellDoubleClicked slot would
        nest an event loop inside the widget that emitted the signal.
        Auto-refresh firing inside that nested loop destroys the widget,
        and returning to the dead slot receiver crashes Qt.
        """
        if self._job_dialog is None:
            self._job_dialog = _JobDialog(self, store=self._store)
        self._job_dialog.populate(entry, initial_tab=initial_tab)
        self._job_dialog.setWindowState(QtCore.Qt.WindowNoState)
        _fit_dialog_to_content(self._job_dialog,
                               900, 650)
        self._job_dialog.show()
        self._job_dialog.raise_()
        self._job_dialog.activateWindow()

    def _overlay_inflight_status(self, entry):
        """Return *entry* (copied only if changed) with `nfy_status_str`
        reflecting an in-flight abort/remove, so the Job dialog header matches
        the greyed row no matter which view opened it (Queue sub-table passes
        the raw job dict, MyJobs passes a prepared entry). "Aborting…" from the
        server flag - top-level on queue dicts, inside `live_job` for MyJobs -
        or the optimistic click; "Removing…" optimistic-only. Terminal entries
        are left untouched.
        """
        st = entry.get('nfy_status_str', '') or ''
        if st not in ('', 'running', 'pending'):
            return entry
        # Offline machine: can't confirm the action, so yield to the base
        # status ("? Unknown") rather than a stale "Aborting…"/"Removing…".
        machine_url = entry.get('nfy_machine_url')
        info = self._store.machine_info(machine_url) if machine_url else None
        if info and (info.get('error') or info.get('status') == 'offline'):
            return entry
        pid = entry.get('prompt_id')
        pa = self._pending_actions.get(pid) if pid else None
        pa_kind = pa.get('kind', '') if pa else ''
        aborting = bool(entry.get('nfy_aborting')
                        or (entry.get('live_job') or {}).get('nfy_aborting'))
        if aborting or pa_kind == 'abort':
            new_status = 'aborting'
        elif pa_kind == 'remove':
            new_status = 'removing'
        else:
            return entry
        out = dict(entry)
        out['nfy_status_str'] = new_status
        return out

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        # Top margin 0 so the tab bar sits flush with the window chrome
        # (avoids the dialog-bg strip showing above the tabs on Qt 5.x).
        root.setContentsMargins(10, 0, 10, 10)
        root.setSpacing(8)

        # Top-level tab widget - see _theme.TOP_TABS_STYLE_BASE.
        self._main_tabs = QtWidgets.QTabWidget()
        from Nukomfy.gui._theme import TOP_TABS_STYLE_BASE
        self._main_tabs.setStyleSheet(
            'QTabWidget::pane{border:1px solid #3a3a3a;}'
            + TOP_TABS_STYLE_BASE)
        apply_tab_fit(self._main_tabs, 16, bold=True)

        root.addWidget(self._main_tabs, 1)

        # --- Tab 1: Machines ---
        machines_widget = QtWidgets.QWidget()
        machines_lay = QtWidgets.QVBoxLayout(machines_widget)
        machines_lay.setContentsMargins(10, 10, 10, 10)
        machines_lay.setSpacing(8)

        # Update All - canonical position inside the Machines toolbar.
        # A twin button lives on the MyJobs
        # tab wired to the same AutoRefreshTimer so the countdown is
        # visible on both tabs; either button triggers the same unified
        # refresh - one timer, one trigger.
        tb = QtWidgets.QHBoxLayout()
        tb.addStretch()
        self._refresh_btn = QtWidgets.QPushButton('Update All')
        set_press_icon(self._refresh_btn, REFRESH)
        self._refresh_btn.setFixedHeight(24)
        self._refresh_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self._refresh_btn.setToolTip('Update all machines now')
        self._refresh_btn.clicked.connect(self._refresh_manual)
        tb.addWidget(self._refresh_btn)
        machines_lay.addLayout(tb)

        # Machine table
        self._table = QtWidgets.QTableWidget(0, len(_HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        # Full-row selection highlights the clicked/expanded machine. The
        # detail row inserted below is a spanned cellWidget carrying a
        # NoItemFlags blocker item, so it stays unselectable and the
        # selection gradient never bleeds into it. `cellClicked` (below)
        # drives expand/collapse; the selection itself is purely visual.
        self._table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(TABLE_STYLE)
        _setup_table_columns(self._table, _HEADERS, _M_WIDTHS,
                             fixed_cols={_COL_STATUS, _COL_REFRESH},
                             stretch_col=_COL_RAM,
                             ui_key='rq_machine_table_v2',
                             pixel_widths={_COL_REFRESH: 22 * 2 + 5})
        self._table.cellClicked.connect(self._on_row_clicked)
        # One logical selection across the machine table and the detail
        # sub-tables: selecting a machine clears any selected job and
        # vice versa (sub-tables wire the same slot at build time).
        self._table.itemSelectionChanged.connect(
            lambda: self._enforce_single_selection(self._table))
        # Click on blank space inside the table deselects the machine.
        _install_empty_area_deselect(self._table)
        # Right-click a machine row -> context menu (View All Jobs).
        self._table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(
            self._on_machine_context_menu)
        machines_lay.addWidget(self._table, 1)

        self._main_tabs.addTab(machines_widget, 'Render Manager')

        # --- Tab 2: My Jobs ---
        self._my_jobs_widget = _MyJobsWidget(panel=self)
        self._main_tabs.addTab(self._my_jobs_widget, 'My Jobs')

        # AutoRefreshTimer - drives the countdown on BOTH Update All
        # buttons (Machines toolbar + MyJobs toolbar).
        self._auto_refresh = AutoRefreshTimer(
            [self._refresh_btn, self._my_jobs_widget._refresh_btn],
            'Update All', self._refresh, parent=self)

        # Close button - height matches the project standard (24px)
        # used elsewhere (job dialog Close, Settings reset buttons, etc.)
        # so the panel button visually matches Settings Save/Cancel which
        # use Qt's default button size.
        btn_lay = QtWidgets.QHBoxLayout()
        btn_lay.addStretch()
        close_btn = QtWidgets.QPushButton('Close')
        close_btn.setFixedHeight(24)
        close_btn.clicked.connect(self.accept)
        btn_lay.addWidget(close_btn)
        root.addLayout(btn_lay)

    # ------------------------------------------------------------------
    # Populate machine rows
    # ------------------------------------------------------------------
    def reconcile_machines(self):
        """Sync machine rows with the live enabled list after Settings closes.

        A machine removed or disabled there loses its row + store data; a
        machine added or re-enabled gets a fresh row (appended, status filled
        by a targeted refresh). A machine edited in place - renamed, or its
        URL changed - has its existing row updated without a rebuild, so the
        live status is preserved. Other existing rows are left untouched.
        drop_machine() leaves local MyJobs history intact. No-op when nothing
        changed.
        """
        live = list(machine_manager.enabled_machines)
        live_ids = {m.id for m in live}
        live_urls = {m.url for m in live}
        # Snapshot what each row currently displays, keyed by machine id, so
        # an in-place edit (a rename or a URL change both keep the id) can be
        # detected against the live record - neither shows up in removed/added.
        shown = {}   # {id: (name_text, url)}
        for r in range(self._table.rowCount()):
            it = self._table.item(r, _COL_NAME)
            if it is not None:
                shown[it.data(QtCore.Qt.UserRole)] = (
                    it.text(), it.data(QtCore.Qt.UserRole + 1))
        removed = set(shown) - live_ids
        added = [m for m in live if m.id not in shown]
        changed = [m for m in live
                   if m.id in shown
                   and (shown[m.id][0] != m.name or shown[m.id][1] != m.url)]
        # A URL change rebinds the machine to a different store key and detail
        # target; a pure rename does not, so the rename can update in place
        # even while the row is expanded.
        url_changed = [m for m in changed if shown[m.id][1] != m.url]
        stale_store = self._store.known_machine_urls() - live_urls
        if not removed and not added and not changed and not stale_store:
            return
        # Collapse the open detail only when its index/binding is at risk:
        # add/remove shift the cached detail index, and a URL change rebinds
        # the expanded machine to a new store key. A rename (or a URL change
        # on a different, collapsed machine) leaves the open detail valid.
        expanded_url_changed = (
            self._expanded_id is not None
            and self._expanded_id in {m.id for m in url_changed})
        if removed or added or expanded_url_changed:
            self._collapse_detail()
        if removed:
            for row in range(self._table.rowCount() - 1, -1, -1):
                it = self._table.item(row, _COL_NAME)
                if it is None:
                    continue
                mid = it.data(QtCore.Qt.UserRole)
                if mid in removed:
                    self._table.removeRow(row)
                    self._queue_data.pop(mid, None)
                    self._refresh_btns.pop(mid, None)
                    # Cancel any in-flight single-machine refresh for this
                    # gone machine and drop its bookkeeping (mirrors
                    # _stop_workers); its late result is also dropped by the
                    # row guard in _on_result.
                    stop_worker(self._single_workers.pop(mid, None))
                    self._single_busy_start.pop(mid, None)
        if added:
            # Insert each added machine at its offline-last sorted position
            # (not appended), so the row order matches a full _populate.
            rank = {mm.id: i for i, mm in enumerate(_sort_offline_last(live))}
            for m in sorted(added, key=lambda x: rank[x.id]):
                at = 0
                for r in range(self._table.rowCount()):
                    it = self._table.item(r, _COL_NAME)
                    if it is None:
                        continue
                    rid = it.data(QtCore.Qt.UserRole)
                    if rid in rank and rank[rid] < rank[m.id]:
                        at = r + 1
                self._add_machine_row(m, at=at)
        # Update edited rows in place. Resolved by id (not a cached index) so
        # this is robust to the row shifts the add/remove passes may have
        # caused. The Name cell carries both the visible name and, in
        # UserRole+1, the URL that _expand_machine reads - rewrite both.
        for m in changed:
            row = self._row_for_machine(m.id)
            if row < 0:
                continue
            it = self._table.item(row, _COL_NAME)
            if it is None:
                continue
            it.setText(m.name)
            it.setData(QtCore.Qt.UserRole + 1, m.url)
        for url in stale_store:
            self._store.drop_machine(url)
        if stale_store:
            self._store.notify()
        # Targeted status fill for new rows and for machines whose URL moved
        # (the new URL has no store data yet). A pure rename keeps its URL, so
        # its cached status stays valid - no refetch needed.
        for m in added + url_changed:
            try:
                self._refresh_one(m.id)
            except Exception:
                pass

    def _populate(self):
        self._collapse_detail()
        self._queue_data.clear()
        self._refresh_btns.clear()
        # Offline machines sink to the bottom so online machines are
        # read first. Stable sort on cached `m.info.online` - unknown (no
        # cached info yet) counts as online so fresh-boot order is natural.
        machines = _sort_offline_last(machine_manager.enabled_machines)
        self._table.setRowCount(0)

        for m in machines:
            self._add_machine_row(m)

    def _add_machine_row(self, m, at=None):
        """Build one machine row (placeholder status until the next fetch).

        Shared by _populate (full build, appends) and reconcile_machines,
        which passes `at` to insert a newly enabled/added machine at its
        offline-last sorted position (so the order matches a full rebuild).
        """
        row = self._table.rowCount() if at is None else at
        self._table.insertRow(row)
        self._table.setRowHeight(row, 26)

        # Status cell (merged dot + text) - placeholder until first refresh
        self._table.setCellWidget(
            row, _COL_STATUS,
            _make_status_cell('', '-', '#888'))

        name_item = QtWidgets.QTableWidgetItem(m.name)
        name_item.setData(QtCore.Qt.UserRole, m.id)
        name_item.setData(QtCore.Qt.UserRole + 1, m.url)
        self._table.setItem(row, _COL_NAME, name_item)

        queue_item = QtWidgets.QTableWidgetItem('-')
        queue_item.setTextAlignment(QtCore.Qt.AlignCenter)
        self._table.setItem(row, _COL_QUEUE, queue_item)

        info = m.info or {}
        for col, key in ((_COL_COMFY, 'comfyui_ver'),
                         (_COL_OS, 'os'),
                         (_COL_GPU, 'gpu'),
                         (_COL_VRAM, 'vram_total'),
                         (_COL_RAM, 'ram_total')):
            it = QtWidgets.QTableWidgetItem(info.get(key, '-'))
            it.setTextAlignment(QtCore.Qt.AlignCenter)
            self._table.setItem(row, col, it)

        # Preserve greyed text for machines last seen offline so the
        # row doesn't flash to white during the initial refresh.
        if info.get('online') is False:
            brush = QtGui.QBrush(QtGui.QColor('#606060'))
            for col in (_COL_NAME, _COL_COMFY, _COL_OS, _COL_QUEUE,
                        _COL_GPU, _COL_VRAM, _COL_RAM):
                it = self._table.item(row, col)
                if it:
                    it.setForeground(brush)

        # Action buttons (centred in cell): View All Jobs + Refresh. Same
        # icon-button style; dual-button layout (gap-2, column 22*2+5).
        _action_style = cell_toolbar_icon(ACCENT_GOLD)

        view_btn = QtWidgets.QPushButton(VIEW_LIST)
        view_btn.setFont(icon_font(14))
        view_btn.setFixedSize(22, 22)
        view_btn.setToolTip('View all jobs on this machine')
        view_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        view_btn.setStyleSheet(_action_style)
        view_btn.clicked.connect(
            lambda _=False, mm=m: self._open_machine_jobs_viewer(mm))

        refresh_btn = QtWidgets.QPushButton(REFRESH)
        refresh_btn.setFont(icon_font(14))
        refresh_btn.setFixedSize(22, 22)
        refresh_btn.setToolTip('Update this machine')
        refresh_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        refresh_btn.setStyleSheet(_action_style)
        refresh_btn.clicked.connect(
            lambda _=False, mid=m.id: self._refresh_one(mid))
        self._refresh_btns[m.id] = refresh_btn

        rw = QtWidgets.QWidget()
        rl = QtWidgets.QHBoxLayout(rw)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        rl.addStretch(1)
        rl.addWidget(view_btn)
        rl.addWidget(refresh_btn)
        rl.addStretch(2)
        self._table.setCellWidget(row, _COL_REFRESH, rw)

    def _row_for_machine(self, machine_id):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _COL_NAME)
            if item and item.data(QtCore.Qt.UserRole) == machine_id:
                return row
        return -1

    def _on_machine_info_changed(self, machine_id):
        """Repaint just the hardware/version cells of a machine row when the
        shared MachineInfoService refreshes its /system_stats snapshot. Status
        / queue / availability stay owned by the unified worker."""
        try:
            row = self._row_for_machine(machine_id)
            if row < 0:
                return
            m = machine_manager.get(machine_id)
            hw = (m.info if m else None) or {}
            for col, key in ((_COL_COMFY, 'comfyui_ver'),
                             (_COL_OS, 'os'),
                             (_COL_GPU, 'gpu'),
                             (_COL_VRAM, 'vram_total'),
                             (_COL_RAM, 'ram_total')):
                it = self._table.item(row, col)
                if it:
                    val = (hw.get(key) or '-') if key == 'os' else hw.get(key, '-')
                    it.setText(val)
        except RuntimeError:
            pass  # table/panel torn down between emit and slot

    def _is_detail_row(self, row):
        """Check if a row is a detail expansion row (no name item)."""
        return self._table.item(row, _COL_NAME) is None

    # ------------------------------------------------------------------
    # Expand / collapse detail
    # ------------------------------------------------------------------
    def _on_row_clicked(self, row, _col):
        if self._is_detail_row(row):
            return  # clicked on the detail widget itself

        # Get machine info
        name_item = self._table.item(row, _COL_NAME)
        if not name_item:
            return
        machine_id = name_item.data(QtCore.Qt.UserRole)
        machine_url = name_item.data(QtCore.Qt.UserRole + 1)

        # If clicking the same row that's expanded -> collapse
        if self._expanded_row == row:
            self._collapse_detail()
            return

        # Collapse any existing expansion
        old_detail_row = self._detail_row
        self._collapse_detail()
        # Adjust row index: if the removed detail was above, rows shifted up
        if old_detail_row >= 0 and old_detail_row < row:
            row -= 1

        self._expand_machine(row, machine_id, machine_url)

    def _auto_expand_pending(self):
        """Consume `_pending_expand_id` set at construction time.

        Safe no-op if no target set or if the machine row isn't in the
        table (e.g. machine was removed between submit and panel open).
        """
        mid = self._pending_expand_id
        self._pending_expand_id = None
        if not mid:
            return
        row = self._row_for_machine(mid)
        if row < 0:
            return
        name_item = self._table.item(row, _COL_NAME)
        if name_item is None:
            return
        machine_url = name_item.data(QtCore.Qt.UserRole + 1)
        self._expand_machine(row, mid, machine_url)
        self._table.selectRow(row)

    def expand_machine_by_id(self, machine_id):
        """Programmatic expand, used when the panel is already open and
        we want to focus a specific machine (e.g., after a submit).

        If already expanded on the requested machine, sync only the Qt
        row selection (which may still be on the previous row). Otherwise
        collapse the current expansion and open the target.
        """
        if not machine_id:
            return
        if self._expanded_id == machine_id:
            row = self._row_for_machine(machine_id)
            if row >= 0:
                self._table.selectRow(row)
            return
        row = self._row_for_machine(machine_id)
        if row < 0:
            return
        old_detail_row = self._detail_row
        self._collapse_detail()
        if old_detail_row >= 0 and old_detail_row < row:
            row -= 1
        name_item = self._table.item(row, _COL_NAME)
        if name_item is None:
            return
        machine_url = name_item.data(QtCore.Qt.UserRole + 1)
        self._expand_machine(row, machine_id, machine_url)
        self._table.selectRow(row)

    def _expand_machine(self, row, machine_id, machine_url, initial_tab=0,
                        pending_queue_pid=None, pending_history_pid=None):
        """Insert a detail row below the given machine row.

        `pending_*_pid` are used by the auto-refresh rebuild path in
        `_on_result` to preserve sub-table selection across the
        destroy+recreate of the detail widget.
        """
        info = self._queue_data.get(machine_id, {})

        # Insert detail row below
        detail_row = row + 1
        self._table.insertRow(detail_row)

        widget = _JobDetailWidget(info, machine_url, initial_tab=initial_tab,
                                  panel=self,
                                  pending_queue_pid=pending_queue_pid,
                                  pending_history_pid=pending_history_pid)
        widget.abort_requested.connect(
            lambda mid=machine_id: self._refresh_one(mid))
        widget.height_changed.connect(self._on_detail_height_changed)
        # Block selection on the spanned detail cell. Without this item,
        # clicks landing on the _JobDetailWidget background (e.g. around
        # the inner sub-table, the tab bar strip, or the right-of-table
        # gap when the sub-table is narrower than the spanned width) fall
        # through to the outer machines table and select the expansion
        # cell - Qt then paints the orange selection gradient across the
        # full cell, which bleeds visually through the transparent
        # _JobDetailWidget background.
        blocker = QtWidgets.QTableWidgetItem()
        blocker.setFlags(QtCore.Qt.NoItemFlags)
        self._table.setItem(detail_row, 0, blocker)
        self._table.setCellWidget(detail_row, 0, widget)
        self._table.setSpan(detail_row, 0, 1, len(_HEADERS))

        # Calculate height based on content
        height = widget._tabs.height() + _DETAIL_ROW_PADDING
        self._table.setRowHeight(detail_row, height)

        self._expanded_row = row
        self._expanded_id = machine_id
        self._detail_row = detail_row
        self._detail_widget = widget

    def _collapse_detail(self):
        if self._detail_row >= 0 and self._detail_row < self._table.rowCount():
            self._table.removeRow(self._detail_row)
        self._expanded_row = -1
        self._expanded_id = None
        self._detail_row = -1
        self._detail_widget = None

    def _enforce_single_selection(self, source):
        """Keep a single logical selection across the machine table and the
        detail Queue/History sub-tables. When `source` gains a selection,
        drop it from every other table. Re-entry guarded so the cascade of
        clearSelection() calls can't loop, and a no-op when `source` was
        just cleared (so deselects don't fight the refresh-restore:
        only the one table that actually held a selection is restored, and
        re-selecting it simply re-clears the already-empty others)."""
        if self._sel_sync_active:
            return
        sm = source.selectionModel()
        if sm is None or not sm.hasSelection():
            return
        others = [self._table]
        dw = self._detail_widget
        if dw is not None:
            others.append(dw._tabs.widget(0))   # Queue sub-table (or label)
            others.append(dw._tabs.widget(1))   # History sub-table (or label)
        self._sel_sync_active = True
        try:
            for t in others:
                if t is source or not isinstance(t, QtWidgets.QTableWidget):
                    continue
                osm = t.selectionModel()
                if osm is not None and osm.hasSelection():
                    t.clearSelection()
        finally:
            self._sel_sync_active = False

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------
    def _unified_check(self, machine):
        """`UnifiedFetchWorker` check_fn: unified fetch for a single machine.

        Runs on the worker thread. Detail HTTP calls are bounded by:
          - `known` (in-memory cache plus locally-persisted) -> skipped entirely
          - `display_limit` (settings.history_limit) -> cap on non-known pids
            fetched for the History sub-table (top-N from the listing)
          - `my_awaiting_ids` -> always fetched, regardless of position, so
            MyJobs can reconcile own jobs whose pid fell below display_limit
        Steady-state cost (cache warm): 2 HTTP calls (queue + listing).
        """
        try:
            known = (
                self._store.cached_terminal_ids_for_machine(machine.url)
                | self._store.terminal_prompt_ids_for_machine(machine.url)
            )
        except Exception:
            known = set()
        try:
            awaiting = self._store.awaiting_prompt_ids_for_machine(machine.url)
        except Exception:
            awaiting = set()
        try:
            display_limit = int(settings.history_limit)
        except (ValueError, TypeError):
            display_limit = 10
        # Fetch first; refresh the Availability flag only if the machine
        # answered. An unreachable host already stalled the queue probe,
        # so pinging the same host's manager endpoint would only stall
        # again - skipping it roughly halves the wait for an offline box.
        result = fetch_all_for_machine(machine.url,
                                       known_terminal_ids=known,
                                       display_limit=display_limit,
                                       my_awaiting_ids=awaiting)
        if not result.get('error'):
            try:
                from Nukomfy.client.machines import refresh_availability
                refresh_availability(machine)
            except Exception:
                pass
        return result

    def _refresh_buttons(self):
        """Every Update All button bound to the shared countdown/refresh.
        Currently: Machines toolbar button + MyJobs History toolbar twin."""
        btns = [self._refresh_btn]
        mj = getattr(self, '_my_jobs_widget', None)
        twin = getattr(mj, '_refresh_btn', None) if mj is not None else None
        if twin is not None:
            btns.append(twin)
        return btns

    def _set_refresh_busy(self, busy):
        """Toggle the in-flight 'Updating\u2026' state on every bound button
        so the user sees parity between the Machines and MyJobs tabs."""
        if busy:
            self._refresh_cycle.begin()
        for b in self._refresh_buttons():
            try:
                b.setEnabled(not busy)
                if busy:
                    b.setText('Updating\u2026')
            except RuntimeError:
                pass  # widget was deleted

    def _initial_refresh(self):
        """Full table build + check - called once from __init__."""
        self._populate()
        machines = machine_manager.enabled_machines
        # Pre-warm the WS monitors for every enabled machine so the socket
        # is already connected by the time the user submits a job - without
        # this the first submit races the WS handshake and loses the
        # `execution_cached` event (the bar then under-counts cached nodes).
        # Each monitor is a daemon thread with 30s pings: negligible load.
        self._prewarm_ws_monitors(machines)
        if not machines:
            self._auto_refresh.reset()
            return
        self._set_refresh_busy(True)
        self._worker = UnifiedFetchWorker(machines, self._unified_check)
        self._worker.result.connect(self._on_result)
        self._worker.finished.connect(self._on_refresh_finished)
        self._worker.start()

    def _prewarm_ws_monitors(self, machines):
        if not ws_client.AVAILABLE:
            return
        mgr = ws_client.get_manager()
        for m in machines:
            try:
                mgr.monitor_for(m.url, ws_session_id())
            except Exception:
                pass

    def _refresh_manual(self):
        """Manual Update All click: supersede any in-flight refresh
        (cancel-restart) so a slow machine never UI-blocks the user. The
        running worker is cancelled via the crash-safe `stop_worker`
        (reparent + abandon). Auto-refresh and the abort-grace refresh keep
        `_refresh`'s skip-if-running guard - they skip rather than
        cancel-restart.
        """
        if self._worker and self._worker.isRunning():
            self._worker = stop_worker(self._worker)
        self._refresh()

    def _refresh(self):
        """Lightweight refresh: update all machine rows in-place."""
        if self._worker and self._worker.isRunning():
            # Auto-refresh / abort-grace fired while a refresh is still
            # polling a slow/offline machine in the background. Re-arm the
            # countdown so it retries once the worker is free, instead of
            # stalling (the AutoRefreshTimer stops its timer when it fires).
            try:
                self._auto_refresh.reset()
            except RuntimeError:
                pass
            return
        self._auto_refresh.stop()
        self._worker = stop_worker(self._worker)
        machines = machine_manager.enabled_machines
        if not machines:
            self._auto_refresh.reset()
            return
        self._set_refresh_busy(True)
        # Clear the 60s availability ping cache on explicit Update All so
        # a fresh ping is issued. Otherwise a user who just toggled
        # Availability in the WebUI would have to wait up to a minute for
        # the plugin to notice.
        try:
            from Nukomfy.client import manager_client
            for _m in machines:
                manager_client.clear_cache(_m.url)
        except Exception:
            pass
        self._worker = UnifiedFetchWorker(machines, self._unified_check)
        self._worker.result.connect(self._on_result)
        self._worker.finished.connect(self._on_refresh_finished)
        self._worker.start()

    def _on_machine_context_menu(self, pos):
        """Right-click menu on a machine row: view all its jobs."""
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        name_item = self._table.item(row, _COL_NAME)
        if name_item is None:
            return  # expanded detail rows carry no machine item
        machine_id = name_item.data(QtCore.Qt.UserRole)
        if not machine_id:
            return
        m = machine_manager.get(machine_id)
        if not m:
            return
        menu = QtWidgets.QMenu(self._table)
        act_jobs = menu.addAction('View All Jobs…')
        act_jobs.triggered.connect(
            lambda _=False, mm=m: self._open_machine_jobs_viewer(mm))
        menu.exec_(self._table.viewport().mapToGlobal(pos))

    def _open_machine_jobs_viewer(self, machine):
        from Nukomfy.gui.machine_jobs_viewer import show_machine_jobs_viewer
        show_machine_jobs_viewer(machine, panel=self)

    def _refresh_one(self, machine_id):
        """Refresh a single machine.

        Mirrors `_refresh` (Update All) scoped to one machine: kick off
        the unified worker and let `_on_result` decide whether to rebuild
        the expanded detail widget via the signature short-circuit. No
        upfront collapse+re-expand, so an unchanged machine doesn't flash
        "Fetching data\u2026" on every click.
        """
        # Guard: this machine already has a check running
        w = self._single_workers.get(machine_id)
        if w and w.isRunning():
            return
        m = machine_manager.get(machine_id)
        if not m:
            return
        btn = self._refresh_btns.get(machine_id)
        if btn:
            btn.setEnabled(False)
            self._single_busy_start[machine_id] = busy_mark()
        # Explicit single-machine refresh invalidates the availability
        # ping cache so a fresh value is fetched, mirroring _refresh().
        try:
            from Nukomfy.client import manager_client
            manager_client.clear_cache(m.url)
        except Exception:
            pass
        worker = UnifiedFetchWorker([m], self._unified_check)
        worker.result.connect(self._on_result)
        worker.finished.connect(
            lambda mid=machine_id: self._on_single_finished(mid))
        worker.start()
        self._single_workers[machine_id] = worker

    def _on_single_finished(self, machine_id):
        """Cleanup after single-machine check completes."""
        self._single_workers.pop(machine_id, None)
        start = self._single_busy_start.pop(machine_id, None)
        btn = self._refresh_btns.get(machine_id)
        if btn:
            def _reenable(b=btn):
                try:
                    b.setEnabled(True)
                except RuntimeError:
                    pass  # button was deleted (table rebuilt)
            schedule_after_min_visible(start, _reenable)

    def _on_refresh_finished(self):
        self._worker = None
        # Age-based sweep: runs once per full refresh cycle so stale local
        # entries (incomplete jobs whose machine went away or whose
        # terminal fell off the server listing before we saw it) get
        # frozen as `unknown` after `lost_job_timeout_days`.
        try:
            _sweep_aged_out()
        except Exception:
            _log.exception('History sweep (aged-out) failed')
        self._refresh_cycle.finish()

    def _on_refresh_ready(self):
        # Refresh cycle settled (fetch done, or the soft deadline elapsed
        # with slow/offline machines still polling in the background).
        # Return the buttons to ready: AutoRefreshTimer.reset() re-labels
        # them to 'Update All (N)', so we only flip enabled state here.
        # Defensive - may fire after the panel closed.
        for b in self._refresh_buttons():
            try:
                b.setEnabled(True)
            except RuntimeError:
                pass  # widget was deleted
        try:
            self._auto_refresh.reset()
        except RuntimeError:
            pass

    def _on_result(self, machine_id, info):
        # `fetch_all_for_machine` piggybacks freshly-terminal
        # detail payloads (messages, execution_error, outputs_count,
        # preview_output) next to the queue snapshot. Freeze them in local
        # history here, before anything else, so the store's subsequent
        # `load_local_history` picks them up and every downstream view
        # (Queue, History, MyJobs) sees the same terminal rows.
        #
        # Use `get` (not `pop`): the store also consumes `new_terminals` in
        # `ingest_machine` to hydrate its per-machine recent-terminals cache
        # with the fresh parsed detail dicts (create_time, nfy_submitted_by,
        # workflow_name, …). Popping the key would strip those detail dicts
        # before the store could see them, forcing fallback to local-history
        # backfill - which has no create_time -> Sent column = "-" and leaves
        # the cache empty for foreign-user jobs.
        # Evict TTL-aged entries from `_locally_removed` BEFORE the
        # filter passes below - keeps the mask honest for the rest of this
        # tick. Kept simple (no per-machine server-confirmed drop) because
        # a partial refresh cycle may return another machine's info first -
        # that machine naturally doesn't report IDs belonging to the machine
        # where the removal happened, and dropping prematurely would
        # reactivate the race. 30s TTL is plenty: `delete_from_queue` is
        # synchronous, the filter is only needed for the first refresh
        # after the removal.
        if self._locally_removed:
            now = _time.monotonic()
            for pid in list(self._locally_removed):
                if now - self._locally_removed[pid] > _LOCALLY_REMOVED_TTL_S:
                    del self._locally_removed[pid]

        # Filter fresh server state against `_locally_removed` (pids the
        # user just deleted via `_on_action_done`). Without these filters
        # a fetch in flight at the moment of the delete would resurrect
        # the pid in the queue, the terminals cache, or fabricate a
        # `cancelled` history entry from a server-side echo.
        if self._locally_removed and info:
            for key in ('running_jobs', 'pending_jobs'):
                lst = info.get(key)
                if isinstance(lst, list):
                    info[key] = [j for j in lst
                                 if j.get('prompt_id')
                                 not in self._locally_removed]
            if isinstance(info.get('pending_jobs'), list):
                info['pending'] = len(info['pending_jobs'])
            # Also filter terminal-side data: ComfyUI doesn't normally emit
            # /api/jobs entries for pending DELETE-d jobs, but if the job
            # had just started running at the moment of DELETE the server
            # may produce a `cancelled` terminal. Mask it so the deleted
            # entry stays gone from every view.
            nt = info.get('new_terminals')
            if isinstance(nt, list):
                info['new_terminals'] = [
                    d for d in nt
                    if isinstance(d, dict)
                    and d.get('prompt_id') not in self._locally_removed]
            rti = info.get('recent_terminal_ids')
            if isinstance(rti, (list, tuple, set)):
                info['recent_terminal_ids'] = [
                    pid for pid in rti
                    if pid not in self._locally_removed]

        new_terminals = info.get('new_terminals') or []
        persisted_any = False
        for detail in new_terminals:
            pid = detail.get('prompt_id')
            status = detail.get('nfy_status_str', '')
            if not pid or status not in ('completed', 'failed', 'cancelled'):
                continue
            if submit_history.is_terminal_persisted(pid):
                continue
            submit_history.persist_terminal_state(
                pid, status, detail.get('nfy_duration', 0),
                execution_error=detail.get('nfy_execution_error'),
                outputs_count=detail.get('nfy_outputs_count', 0),
                messages=detail.get('nfy_messages'),
                preview_output=detail.get('nfy_preview_output'),
                outputs=detail.get('nfy_outputs'),
            )
            persisted_any = True

        # Persistent-history promotion. Covers awaiting pids that rolled
        # off ComfyUI's in-memory /api/jobs listing before the client
        # could see them terminal: fetch_all_for_machine's listing-bound
        # detail pass can't reach them, so the manager-side persistent
        # storage is the authoritative fallback. Same idempotency guards
        # as the new_terminals loop above; mask `_locally_removed` too so
        # a server echo of a just-deleted pid can't resurrect the entry.
        persistent_terminals = info.get('persistent_terminals') or []
        if self._locally_removed and persistent_terminals:
            persistent_terminals = [
                d for d in persistent_terminals
                if isinstance(d, dict)
                and d.get('prompt_id') not in self._locally_removed]
        for detail in persistent_terminals:
            pid = detail.get('prompt_id')
            status = detail.get('nfy_status_str', '')
            if not pid or status not in ('completed', 'failed', 'cancelled'):
                continue
            if submit_history.is_terminal_persisted(pid):
                continue
            submit_history.persist_terminal_state(
                pid, status, detail.get('nfy_duration', 0),
                execution_error=detail.get('nfy_execution_error'),
                outputs_count=detail.get('nfy_outputs_count', 0),
                messages=detail.get('nfy_messages'),
                preview_output=detail.get('nfy_preview_output'),
                outputs=detail.get('nfy_outputs'),
            )
            persisted_any = True

        # Server-evidence-based lost detection. The fetch succeeded and
        # the server is authoritative: any non-terminal local entry on
        # this machine whose prompt_id is absent from running, pending,
        # AND recent_terminal_ids has been definitively lost (server
        # restart, history rolled off, etc.). 10s grace from sent_at
        # protects against the race where a fetch in flight pre-dates
        # a fresh submit (its snapshot wouldn't include the new pid).
        # `persist_as_lost` is a one-way latch - the grace prevents
        # marking a legitimate fresh submit as lost forever.
        m_obj_lost = machine_manager.get(machine_id)
        if (m_obj_lost is not None and not info.get('error')
                and info.get('status') != 'offline'):
            machine_url_lost = m_obj_lost.url
            running_ids = {j.get('prompt_id')
                           for j in (info.get('running_jobs') or [])}
            pending_ids = {j.get('prompt_id')
                           for j in (info.get('pending_jobs') or [])}
            recent_ids = set(info.get('recent_terminal_ids') or ())
            known = (running_ids | pending_ids | recent_ids
                     | set(self._locally_removed.keys()))
            now_dt = _datetime.datetime.now()
            grace = _datetime.timedelta(seconds=10)
            for entry in submit_history.get_history():
                if entry.get('nfy_machine_url') != machine_url_lost:
                    continue
                if entry.get('nfy_terminal_persisted'):
                    continue
                pid_e = entry.get('prompt_id')
                if not pid_e or pid_e in known:
                    continue
                sent_raw = entry.get('nfy_sent_at', '')
                if not sent_raw:
                    continue
                try:
                    sent = _datetime.datetime.fromisoformat(sent_raw)
                except (ValueError, TypeError):
                    continue
                if now_dt - sent < grace:
                    continue
                try:
                    submit_history.persist_as_lost(pid_e)
                    persisted_any = True
                except Exception:
                    _log.exception(
                        'Persist-as-lost failed for %s', fmt_job(pid_e))

            # Orphan re-add: server reports OUR running/pending job
            # whose pid is missing from local history. Only one
            # explanation - the user manually deleted an unreachable
            # entry while the machine was offline; now it's back and
            # the job is still alive on the server. Re-add as a
            # fresh entry so MyJobs Active reflects reality.
            # RECENT-TERMINALS ARE INTENTIONALLY SKIPPED - a
            # completed/failed/cancelled job that the user deleted is
            # "done"; resurfacing it on every fetch would defeat the
            # delete intent.
            local_pids = {e.get('prompt_id')
                          for e in submit_history.get_history()
                          if e.get('prompt_id')}
            me = self._my_username
            locally_removed_keys = set(self._locally_removed.keys())
            for src_jobs in (info.get('running_jobs') or [],
                             info.get('pending_jobs') or []):
                for j in src_jobs:
                    if not isinstance(j, dict):
                        continue
                    pid_o = j.get('prompt_id')
                    if not pid_o or pid_o in local_pids:
                        continue
                    if pid_o in locally_removed_keys:
                        continue
                    if j.get('nfy_submitted_by') != me:
                        continue
                    try:
                        submit_history.record_submit(
                            prompt_id=pid_o,
                            nfy_job_id=j.get('nfy_job_id', ''),
                            nfy_submitted_by=j.get('nfy_submitted_by', ''),
                            nfy_submitter_host=j.get(
                                'nfy_submitter_host', ''),
                            machine_name=m_obj_lost.name,
                            machine_url=m_obj_lost.to_persistable_url(),
                            workflow_name=j.get('nfy_workflow_name', ''),
                            frame_range=j.get('nfy_frame_range', ''),
                            nk_file=j.get('nfy_nk_file', ''),
                            node_name=j.get('nfy_node_name', ''),
                            output_paths=j.get('nfy_output_paths', []) or [],
                            batch_count=j.get('nfy_batch_count', 1),
                            batch_index=j.get('nfy_batch_index', 1),
                            read_color=0,
                            input_ranges=j.get('nfy_input_ranges', []),
                        )
                        # Backfill original sent_at from server's
                        # create_time epoch. Without this the entry
                        # would carry "now" and live a full
                        # `lost_job_timeout_days` again.
                        ct = j.get('create_time')
                        if isinstance(ct, (int, float)) and ct > 0:
                            iso = _datetime.datetime.fromtimestamp(
                                ct).isoformat(timespec='seconds')
                            submit_history.update_entry(
                                pid_o, nfy_sent_at=iso)
                        persisted_any = True
                    except Exception:
                        _log.exception(
                            'Orphan re-add failed for %s', fmt_job(pid_o))

        if persisted_any:
            self._store.load_local_history()
        # A late fetch result can land here after the machine was removed or
        # disabled in Settings: reconcile_machines() already evicted its row
        # and store entry. Without a row it is no longer shown, so drop the
        # result before the mirror writes below - re-adding it to _queue_data
        # or the store would resurface the machine until the panel is reopened.
        row = self._row_for_machine(machine_id)
        if row < 0:
            return

        # Capture previous signature before overwriting - used below to
        # decide whether the expanded detail widget needs a rebuild.
        old_sig = _queue_signature(self._queue_data.get(machine_id))
        # Store full queue data for expansion
        self._queue_data[machine_id] = info

        # Mirror the fresh snapshot into the central store. History
        # sub-table + MyJobs consume it read-only; the emit at the end of
        # this method wakes any listeners for a single rebuild per tick.
        m_obj = machine_manager.get(machine_id)
        if m_obj is not None:
            try:
                self._store.ingest_machine(m_obj.url, info)
            except Exception:
                _log.exception(
                    'Store ingest failed for %s',
                    fmt_machine(m_obj.url, m_obj.name))

        # Merge the live-progress snapshot into the panel cache (ratchet
        # max; never zeroed on omission) and prune entries for jobs no
        # longer running. Only on a successful fetch: an unreachable tick
        # carries no authoritative running set, so leaving the cache intact
        # keeps the bars rather than wiping them on a transient blip.
        if (m_obj is not None and not info.get('error')
                and info.get('status') != 'offline'):
            url = m_obj.url
            endpoint_ok = info.get('progress_endpoint_ok')
            if endpoint_ok is not None:
                self._progress_endpoint_ok[url] = endpoint_ok
            live = info.get('live_progress')
            if isinstance(live, dict):
                for pid, pdata in live.items():
                    if not pid or not isinstance(pdata, dict):
                        continue
                    key = (url, pid)
                    prev = self._live_progress.get(key)
                    prev_frac = prev.get('fraction', 0.0) if prev else 0.0
                    try:
                        frac = float(pdata.get('fraction', 0.0) or 0.0)
                    except (TypeError, ValueError):
                        frac = prev_frac
                    self._live_progress[key] = {
                        'fraction': max(prev_frac, frac),
                        'tooltip': pdata.get('tooltip')
                        or (prev.get('tooltip', '') if prev else ''),
                    }
            running_pids = {j.get('prompt_id')
                            for j in (info.get('running_jobs') or [])
                            if j.get('prompt_id')}
            for key in [k for k in self._live_progress
                        if k[0] == url and k[1] not in running_pids]:
                del self._live_progress[key]

        # MyJobs Active/History refresh is driven by the `store.notify()`
        # fan-out at the end of this method - no explicit ping needed.

        status = info.get('status', 'offline')
        # Availability is cached on the Machine.info dict (populated by
        # `check_machine` via manager_client.availability). The queue fetch
        # info dict doesn't carry it directly, so read from the machine.
        avail = None
        if m_obj is not None and isinstance(m_obj.info, dict):
            avail = m_obj.info.get('availability')
        label, color, icon_char = render_machine_status(status, avail)

        # Update status cell in-place when possible (avoids per-tick
        # widget swap that caused visible flicker on every machine row).
        existing = self._table.cellWidget(row, _COL_STATUS)
        if not _update_status_cell(existing, icon_char, label, color):
            self._table.setCellWidget(
                row, _COL_STATUS,
                _make_status_cell(icon_char, label, color))
        # Tooltip explaining the Unavailable soft-lock. Reach the current
        # widget after the update / replace so we tag whichever cell
        # ended up live.
        status_widget = self._table.cellWidget(row, _COL_STATUS)
        if status_widget is not None:
            if avail == 'unavailable':
                status_widget.setToolTip(
                    'Marked Unavailable by the machine owner.\n'
                    'New submissions are blocked until the flag is cleared.')
            else:
                status_widget.setToolTip('')

        pending_count = info.get('pending', 0)
        queue_item = self._table.item(row, _COL_QUEUE)
        if queue_item:
            queue_item.setText(
                str(pending_count) if status != 'offline' else '-')

        m = machine_manager.get(machine_id)
        # Remember last known online status on the Machine so the next
        # panel open sorts offline machines to the bottom. In-memory only;
        # machines.json persists hardware fields only.
        if m is not None:
            m.info = dict(m.info or {})
            m.info['online'] = status != 'offline'
        hw = (m.info if m else None) or {}
        comfy_item = self._table.item(row, _COL_COMFY)
        if comfy_item:
            comfy_item.setText(hw.get('comfyui_ver', '-'))
        os_item = self._table.item(row, _COL_OS)
        if os_item:
            os_item.setText(hw.get('os') or '-')
        gpu_item = self._table.item(row, _COL_GPU)
        if gpu_item:
            gpu_item.setText(hw.get('gpu', '-'))
        vram_item = self._table.item(row, _COL_VRAM)
        if vram_item:
            vram_item.setText(hw.get('vram_total', '-'))
        ram_item = self._table.item(row, _COL_RAM)
        if ram_item:
            ram_item.setText(hw.get('ram_total', '-'))

        # Dim all text cells on offline rows to reinforce the "not
        # responding" signal. Status cell keeps its own color.
        dim = status == 'offline'
        brush = (QtGui.QBrush(QtGui.QColor('#606060')) if dim
                 else QtGui.QBrush())
        for col in (_COL_NAME, _COL_COMFY, _COL_OS, _COL_QUEUE,
                    _COL_GPU, _COL_VRAM, _COL_RAM):
            it = self._table.item(row, col)
            if it:
                it.setForeground(brush)

        # Bold the machine name when this user has running or queued jobs
        # here. Recomputed on every tick so it drops off automatically
        # once the last job finishes.
        name_item = self._table.item(row, _COL_NAME)
        if name_item:
            # Match by username only (nfy_submitted_by). Hostname is
            # metadata, not part of ownership. Jobs without
            # nfy_submitted_by (server-side queue items with no Nukomfy
            # extra_data) produce no bold, which is correct: no reliable
            # username means we cannot claim ownership.
            has_mine = False
            for key in ('running_jobs', 'pending_jobs'):
                for j in info.get(key) or []:
                    if j.get('nfy_submitted_by') == self._my_username:
                        has_mine = True
                        break
                if has_mine:
                    break
            font = name_item.font()
            if font.bold() != has_mine:
                font.setBold(has_mine)
                name_item.setFont(font)

        # Re-enable per-machine refresh button (skip if single worker handles it)
        if machine_id not in self._single_workers:
            btn = self._refresh_btns.get(machine_id)
            if btn:
                btn.setEnabled(True)

        # Update expanded detail widget with new data.
        #
        # We always collapse + re-expand rather than calling
        # `update_queue()` in-place. Nested tab-content replacement
        # inside the cellWidget doesn't reliably trigger Qt to relayout
        # the outer main-table row, so the visible content can stay
        # stale on full refresh. Rebuilding the whole `_JobDetailWidget`
        # avoids that - the new instance is inserted as a fresh cellWidget
        # and Qt lays it out properly every time.
        #
        # Skip the rebuild while a modal dialog is active: a modal
        # (QMessageBox confirm, etc.) spins its own event loop inside a
        # slot of `_detail_widget`. Destroying `_detail_widget` underneath
        # that call-stack crashes Qt when the slot returns. The next tick
        # will rebuild once the modal is gone.
        if self._expanded_id == machine_id and self._detail_widget:
            modal_active = QtWidgets.QApplication.activeModalWidget() is not None
            # Skip the costly collapse+rebuild when nothing visible in
            # the detail widget would change (status, running list,
            # pending list all identical to the previous tick).
            sig_unchanged = (old_sig is not None
                             and old_sig == _queue_signature(info))
            if not modal_active and not sig_unchanged:
                tab_idx = self._detail_widget._tabs.currentIndex()
                # Preserve sub-table selection across the destroy+recreate:
                # read pids now, pass to the fresh widget via ctor kwargs so
                # `_build_queue_tab` (sync) and `set_history` (later) can
                # re-select once their tables are populated.
                q_pid, h_pid = self._detail_widget.get_current_selection()
                self._collapse_detail()
                name_item = self._table.item(row, _COL_NAME)
                if name_item:
                    self._expand_machine(
                        row, machine_id,
                        name_item.data(QtCore.Qt.UserRole + 1),
                        initial_tab=tab_idx,
                        pending_queue_pid=q_pid,
                        pending_history_pid=h_pid)

        # Fan out to all store-bound listeners (MyJobs + expanded History
        # sub-table). Debounced: during Update All, 22 machines deliver
        # results in quick succession - coalesce to a single notify after
        # the burst settles so MyJobs rebuilds once, not per machine.
        self._schedule_store_notify()

    # ------------------------------------------------------------------
    # History sub-table - server-backed (shows every user's jobs on that
    # machine, not just the local-history subset which is user-scoped).
    # Feed comes from `RenderDataStore._machine_recent_terminals`, hydrated
    # by every `UnifiedFetchWorker` tick. The detail widget reads it via
    # `_refresh_history_from_store` - no dedicated worker needed here.
    # ------------------------------------------------------------------
    def _on_detail_height_changed(self):
        """Sync detail table row height when tab content changes."""
        if self._detail_row >= 0 and self._detail_widget:
            self._table.setRowHeight(
                self._detail_row,
                self._detail_widget._tabs.height() + _DETAIL_ROW_PADDING)

    def _schedule_store_notify(self):
        """(Re)start the debounce timer for store.notify().

        Called from `_on_result` per machine; a burst of 22 results during
        Update All collapses into a single fan-out after the last one
        settles. Single-machine refresh still emits once (same as before)
        with an imperceptible 150ms delay.
        """
        try:
            self._notify_timer.start()
        except RuntimeError:
            pass

    def _fire_store_notify(self):
        """Timer slot - emit the coalesced storeChanged."""
        try:
            self._store.notify()
        except Exception:
            pass

    def _on_store_changed(self):
        """Hook for store-driven views.

        Render Manager's per-machine History sub-table is intentionally
        server-backed (so it surfaces jobs submitted by other users on
        that machine), so it does not listen here. MyJobs consumes the
        store via its own wiring (_MyJobsWidget._on_store_changed).

        Also verifies armed pending_actions: each is handed off to the
        server-side state. For abort, once the server reports the job
        gone (terminal) or flagged `nfy_aborting`, the optimistic entry
        is cleared and the server flag drives the row from there (so the
        aborting state survives a panel reopen). For remove, the entry is
        finalised synchronously in `_on_action_done`.
        """
        self._verify_pending_actions()

    def _reap_action_worker(self, worker):
        """Drop a finished `_ActionWorker` so `_action_workers` doesn't grow
        for the panel's lifetime (mirrors the `_single_workers` cleanup).
        `deleteLater` frees the C++ object, which `parent=panel` would
        otherwise keep alive until the panel is destroyed."""
        try:
            self._action_workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def _on_action_done(self, prompt_id, ok):
        """Receive the `_ActionWorker.done` signal and finalize the
        pending_action. Runs on the Qt main thread.

        Split path by `kind`:
        - `remove` (pending) ok=True: DELETE 2xx is the server's
          confirmation the job is gone. Pending jobs never run, so
          there's no terminal record to persist - wipe the local entry
          entirely and mask the pid for 30s so a racing fetch doesn't
          resurrect it. Single rebuild, no further verification.
        - `abort` (running) ok=True: keep the pa armed and schedule a
          refresh; the unified fetch will persist the terminal state
          via `new_terminals` once the server transitions the job.
        - ok=False: machine unreachable / server error - clear pa, popup,
          schedule a refresh. The store is left untouched: the next real
          fetch reconciles. (A 409 "no longer running" is routed to
          `_on_action_not_running` instead.)
        """
        pa = self._pending_actions.get(prompt_id)
        if not pa:
            return
        machine_url = pa.get('nfy_machine_url')
        kind = pa.get('kind')
        action_verb = 'abort' if kind == 'abort' else 'remove'
        if not ok:
            # POST failed despite the pre-flight check (race: the machine
            # went offline between the check and the POST, or the server
            # rejected the request). We can't know the machine's or job's
            # true state from a failed POST, so don't fabricate a store
            # snapshot: ingesting an empty running/pending list here wiped
            # every OTHER job on the machine (including other users') from
            # the store until the next fetch. Just clear the in-flight
            # grey-out and kick a refresh so the next real fetch reconciles -
            # a genuinely offline machine is then marked offline from live
            # data and `jobs_for_user` shows the affected job as 'Unknown'.
            self._pending_actions.pop(prompt_id, None)
            self._notify_pending_actions_changed(machine_url)
            _log.warning(
                "Action '%s' POST failed for %s on %s",
                action_verb, fmt_job(prompt_id), fmt_machine(machine_url))
            try:
                _show_machine_offline_popup(self, action_verb, machine_url)
            except Exception:
                pass
            QtCore.QTimer.singleShot(2500, self._refresh_if_alive)
            return
        if pa.get('kind') == 'remove':
            # Race-safe ordering: mask FIRST so a fetch already in flight
            # filters this pid out of running/pending/new_terminals before
            # the rebuild reads the store.
            self._locally_removed[prompt_id] = _time.monotonic()
            # Optimistically drop from the store so the Queue sub-table
            # rebuild (after pop pa + notify) finds the pid gone and the
            # row disappears immediately. Without this the row would
            # un-grey and re-render as a normal pending until the next
            # fetch (Update All / auto-refresh tick) catches up.
            try:
                self._store.drop_live_job(prompt_id)
            except Exception:
                _log.exception(
                    'Drop live job failed for %s', fmt_job(prompt_id))
            try:
                submit_history.delete_entry(prompt_id)
            except Exception:
                _log.exception(
                    'Local history delete failed for %s after DELETE 2xx',
                    fmt_job(prompt_id))
            try:
                self._store.load_local_history()
            except Exception:
                _log.exception(
                    'Local history reload failed after entry deletion')
            self._pending_actions.pop(prompt_id, None)
            self._notify_pending_actions_changed(machine_url)
            return
        # `abort` path - arm the bridge and kick a refresh after a short
        # grace so the first fetch carries the server's new state. The row
        # stays greyed via the bridge until `_verify_pending_actions` hands
        # off to the server-side state (job gone, or `nfy_aborting` visible).
        pa['armed'] = True
        QtCore.QTimer.singleShot(2500, self._refresh_if_alive)

    def _on_admin_action_error(self, prompt_id, reason):
        """Surface an admin-gated force action failure with a descriptive
        popup. The pending_action grey-out is cleared by `_on_action_done`
        (which also fires on admin failure with ok=False); this slot only
        adds the user-facing explanation so the error isn't conflated with
        the generic offline case."""
        messages = {
            'wrong_password': (
                'Admin password was rejected by the server.'),
            'rate_limited': (
                'Too many failed attempts on this machine. Try again '
                'later.'),
            'no_password': (
                'No admin password is configured on the target machine.'),
            'missing_prompt_id': (
                'Nukomfy could not identify the job to act on. '
                'Refresh the Render Manager and try again.'),
            'error': (
                'Network error or server unavailable. The force action '
                'may not have been performed.'),
        }
        msg = messages.get(
            reason, 'Force action failed: {}'.format(reason))
        try:
            _dialogs.warn(self, 'Force action failed', msg)
        except Exception:
            _log.exception(
                'Admin-action error popup failed for %s', fmt_job(prompt_id))

    def _on_action_not_running(self, prompt_id):
        """The abort did not take effect: the job was no longer the running
        prompt. Reached by the regular (same-user) 409 and by admin
        force_abort when the server reports `action_performed` False. Clear
        the optimistic grey-out and explain, without marking the (online)
        machine offline.
        """
        pa = self._pending_actions.pop(prompt_id, None)
        if pa is None:
            return
        self._notify_pending_actions_changed(pa.get('nfy_machine_url'))
        try:
            _dialogs.inform(
                self, 'Cannot abort job',
                'This job is no longer running and cannot be aborted.')
        except Exception:
            _log.exception(
                'Not-running popup failed for %s', fmt_job(prompt_id))

    def _refresh_if_alive(self):
        """Safe `_refresh` wrapper - the panel may have closed between
        scheduling and firing."""
        try:
            self._refresh()
        except RuntimeError:
            pass  # widget already deleted

    def _verify_pending_actions(self):
        """Hand off each armed `abort` bridge to the server-side state.

        `kind='remove'` is finalized synchronously in `_on_action_done` and
        never reaches here (a defensive skip handles the impossible case).

        Outcomes for `kind='abort'`:
        - Machine unreachable -> keep waiting (next successful fetch verifies).
        - Job gone from the live queue -> terminal; pop the bridge. The
          unified fetch persists the terminal via `new_terminals`.
        - Job still live AND flagged `nfy_aborting` -> the server has taken
          over: pop the bridge and let the row read "Aborting…" from the
          flag, which survives a panel reopen / Nuke restart.
        - Job still live, flag not yet visible -> a fetch raced the in-process
          swap; keep the bridge greyed, the next fetch carries the flag.

        No 15s timeout: the server reports "aborting" honestly for as long
        as the job lives, so there is nothing to fall back to.
        """
        if not self._pending_actions:
            return
        machines_to_refresh = set()
        for pid, pa in list(self._pending_actions.items()):
            if not pa.get('armed'):
                continue
            if pa.get('kind') == 'remove':
                self._pending_actions.pop(pid, None)
                machines_to_refresh.add(pa.get('nfy_machine_url'))
                continue
            machine_url = pa.get('nfy_machine_url')
            info = self._store.machine_info(machine_url)
            if not info or info.get('error'):
                continue  # machine offline - wait for next fetch
            running = info.get('running_jobs') or []
            pending = info.get('pending_jobs') or []
            live_ids = ({j.get('prompt_id') for j in running}
                        | {j.get('prompt_id') for j in pending})
            if pid not in live_ids:
                # Gone from the live queue - terminal. `_on_result` persists
                # it via `new_terminals`; nothing extra needed here.
                self._pending_actions.pop(pid, None)
                machines_to_refresh.add(machine_url)
                continue
            # Still live: hand off to the server-side flag once it's visible.
            if any(j.get('prompt_id') == pid and j.get('nfy_aborting')
                   for j in running):
                self._pending_actions.pop(pid, None)
                machines_to_refresh.add(machine_url)
            # else: flag not yet carried by this fetch - keep the bridge.
        if machines_to_refresh:
            # Rebuild MyJobs once (covers every machine) + the expanded
            # detail widget if its machine is one of those that changed.
            modal_active = (
                QtWidgets.QApplication.activeModalWidget() is not None)
            mj = getattr(self, '_my_jobs_widget', None)
            if mj is not None and not modal_active:
                try:
                    mj._reload()
                except RuntimeError:
                    pass  # widget already deleted
                except Exception:
                    _log.exception('MyJobs reload failed (verify path)')
            dw = getattr(self, '_detail_widget', None)
            if dw is not None and not modal_active:
                dw_url = getattr(dw, '_machine_url', None)
                if dw_url in machines_to_refresh:
                    try:
                        dw._on_store_changed()
                    except RuntimeError:
                        pass
                    except Exception:
                        _log.exception(
                            'Job detail rebuild failed (verify path)')

    def _notify_pending_actions_changed(self, machine_url=None):
        """Rebuild the views that read `_pending_actions` when an entry
        is added or cleared.

        - MyJobs Active / History: always (MyJobs aggregates across
          machines, so any mutation is potentially visible).
        - Queue sub-table: only when the expanded detail widget belongs
          to `machine_url` - `_on_store_changed` reads the store + the
          per-machine pending_ids set, and the extended `_queue_signature`
          then triggers the rebuild.

        Skip both rebuilds if a modal dialog is active. Destroying widgets
        underneath a modal's pumped event loop crashed Nuke during
        multi-remove sequences (the second QMessageBox.question opened
        while the first action's reload tried to rebuild MyJobs). The
        next `storeChanged` from auto-refresh will rebuild once the modal
        is closed.
        """
        modal_active = (
            QtWidgets.QApplication.activeModalWidget() is not None)
        mj = getattr(self, '_my_jobs_widget', None)
        if mj is not None and not modal_active:
            try:
                mj._reload()
            except RuntimeError:
                pass  # widget already deleted
            except Exception:
                _log.exception('MyJobs reload failed (pending notify path)')
        dw = getattr(self, '_detail_widget', None)
        if (dw is not None and machine_url is not None
                and not modal_active):
            if getattr(dw, '_machine_url', None) == machine_url:
                try:
                    dw._on_store_changed()
                except RuntimeError:
                    pass
                except Exception:
                    _log.exception(
                        'Job detail rebuild failed (pending notify path)')



# ---------------------------------------------------------------------------
# Singleton entry point
# ---------------------------------------------------------------------------


_instance = None


def show_render_queue(expand_machine_id=None):
    """Show the Render Manager panel (callable from menu). Non-modal.

    If `expand_machine_id` is provided (called from a Submit success
    dialog), auto-expand that machine's row - both when opening fresh
    and when raising an already-open instance.
    """
    global _instance
    if _instance is not None:
        try:
            if _instance.isVisible() or _instance.isMinimized():
                # If minimized to taskbar, restore before raise - `raise_()`
                # alone doesn't un-minimize on Windows.
                if _instance.isMinimized():
                    _instance.setWindowState(
                        (_instance.windowState() & ~QtCore.Qt.WindowMinimized)
                        | QtCore.Qt.WindowActive)
                    _instance.showNormal()
                _instance.raise_()
                _instance.activateWindow()
                # Re-opening counts as "seeing the machines" again: refresh any
                # machine not yet checked this session (no-op if all fresh).
                try:
                    from Nukomfy.gui._machine_info_service import service
                    service().ensure_fresh()
                except Exception:
                    pass
                if expand_machine_id:
                    _instance.expand_machine_by_id(expand_machine_id)
                    # Kick a fresh fetch for the submit's target machine so
                    # the new job replaces its placeholder `?` row with
                    # the real pending/running state immediately, instead
                    # of waiting for the next auto-refresh tick.
                    try:
                        _instance._refresh_one(expand_machine_id)
                    except Exception:
                        pass
                return
        except RuntimeError:
            # Instance was deleted by Qt
            _instance = None
    parent = _nuke_main_window() if settings.render_manager_keep_on_top else None
    _instance = RenderQueuePanel(parent=parent,
                                  expand_machine_id=expand_machine_id)
    _instance.show()
    # Bring it to the front on open, like the re-open branch above: X11 window
    # managers don't auto-raise a newly shown Tool window over an active
    # sibling the way Windows does.
    _instance.raise_()
    _instance.activateWindow()
