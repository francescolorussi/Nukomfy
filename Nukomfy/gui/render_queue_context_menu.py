"""Render Manager right-click context menu.

Builds the per-row QMenu shown when the user right-clicks a job in the
Queue / History sub-tables of a per-machine panel, or in the MyJobs
Active / Local History tables. All menu items delegate to functions already
in use by the action buttons - the menu is a thin entry-point, no
business logic lives here.

Internal module - public API of Render Manager surfaced via
render_queue_panel.py.
"""

import logging
import os

from Nukomfy.utils.qt_compat import QtWidgets
from Nukomfy.gui import _dialogs

from Nukomfy.gui.render_queue_actions import _abort_or_remove_entry, read_outputs
from Nukomfy.gui.status_display import ABORTING_LABEL, REMOVING_LABEL
from Nukomfy.utils.reveal import (
    reveal_folder,
    REVEAL_OK, REVEAL_MISSING, REVEAL_UNREACHABLE, REVEAL_LAUNCH_FAILED,
)

_log = logging.getLogger(__name__)


def show_job_context_menu(parent_widget, global_pos, entry, *,
                          kind, panel,
                          container=None, machine_url=None):
    """Build and exec the right-click QMenu for a job row.

    Parameters
    ----------
    parent_widget : QWidget
        Widget that owns the right-click (typically the QTableWidget
        viewport). Used as parent for the QMenu and for any modal
        dialog spawned by an action.
    global_pos : QPoint
        Screen-mapped position where the menu pops up.
    entry : dict
        The job dict for the right-clicked row. The mapping is the
        same one already used by the action buttons of the matching
        table (`_queue_jobs[row]`, `_history_items[row]`,
        `_MyJobsTableBase._entries[row]`).
    kind : str
        One of 'queue_machine' | 'history_machine'
              | 'myjobs_active' | 'myjobs_history'.
    panel : RenderQueuePanel
        Owning panel - holds `_pending_actions`, `_store`,
        `show_job_dialog`.
    container : _MyJobsWidget, optional
        Required for the 'myjobs_*' kinds. Source of `show_detail`,
        `retrieve`, `delete_entry`, `_on_active_action`,
        `_state_for_entry`.
    machine_url : str, optional
        Required for the 'queue_machine' / 'history_machine' kinds:
        the machine on which the job lives.
    """
    if entry is None:
        return
    menu = QtWidgets.QMenu(parent_widget)

    if kind == 'queue_machine':
        _build_queue_machine(menu, parent_widget, entry, panel, machine_url)
    elif kind == 'history_machine':
        _build_history_machine(menu, parent_widget, entry, panel, machine_url)
    elif kind == 'myjobs_active':
        if container is None:
            return
        _build_myjobs_active(menu, parent_widget, entry, panel, container)
    elif kind == 'myjobs_history':
        if container is None:
            return
        _build_myjobs_history(menu, parent_widget, entry, panel, container)
    else:
        return

    if not menu.actions():
        return
    menu.exec_(global_pos)


# ---------------------------------------------------------------------------
# Kind-specific builders
# ---------------------------------------------------------------------------

def _build_queue_machine(menu, parent_widget, entry, panel, machine_url):
    is_running = (entry.get('nfy_status_str') or '').lower() == 'running'
    prompt_id = entry.get('prompt_id', '')

    _add_job_detail(menu, panel, entry)

    if is_running:
        act_text = 'Abort job'
    else:
        act_text = 'Remove from queue'
    act = menu.addAction(act_text)
    if not entry.get('nfy_job_id'):
        # External job (submitted outside Nukomfy): aborting/dequeuing it is
        # not the plugin's responsibility. The row's action button is already
        # greyed for this; the context menu is a second path to the same
        # POST, so it must honour the same rule or it silently bypasses it.
        act.setEnabled(False)
        act.setToolTip('External job. Manage it from ComfyUI directly.')
    else:
        pa = panel._pending_actions.get(prompt_id) if prompt_id else None
        pa_kind = pa.get('kind', '') if pa else ''
        # "Aborting…" comes from the server flag (survives a panel reopen) or
        # the optimistic click; "Removing…" is optimistic-only. Either way the
        # action is in flight -> disabled, mirroring the greyed row.
        is_aborting = bool(entry.get('nfy_aborting')) or pa_kind == 'abort'
        is_removing = pa_kind == 'remove'
        if is_aborting or is_removing:
            act.setEnabled(False)
            act.setText(ABORTING_LABEL if is_aborting else REMOVING_LABEL)
        else:
            act.triggered.connect(
                lambda _=False: _abort_or_remove_entry(
                    parent_widget, panel, entry,
                    is_running=is_running, machine_url=machine_url))

    menu.addSeparator()
    _add_copy_job_id(menu, entry)
    _add_show_output_folder(menu, parent_widget, entry)


def _build_history_machine(menu, parent_widget, entry, panel, machine_url):
    _add_job_detail(menu, panel, entry)
    _add_read_outputs(menu, parent_widget, entry)
    menu.addSeparator()
    _add_copy_job_id(menu, entry)
    _add_show_output_folder(menu, parent_widget, entry)


def _build_myjobs_active(menu, parent_widget, entry, panel, container):
    state_kind, _url = container._state_for_entry(entry)
    prompt_id = entry.get('prompt_id', '')
    pa = panel._pending_actions.get(prompt_id) if prompt_id else None

    _add_job_detail_myjobs(menu, container, entry)

    if state_kind == 'running':
        act = menu.addAction('Abort job')
    elif state_kind == 'aborting':
        # Server-side abort flag (or post-handoff): disabled, mirrors the
        # greyed row. The pa override below relabels the optimistic window.
        act = menu.addAction(ABORTING_LABEL)
        act.setEnabled(False)
    elif state_kind == 'pending':
        act = menu.addAction('Remove from queue')
    elif state_kind == 'unreachable':
        act = menu.addAction('Remove from local history')
        act.setToolTip(
            'Server status not confirmed. This only removes the local entry.')
    elif state_kind == 'checking':
        act = menu.addAction('Abort job')
        act.setEnabled(False)
        act.setToolTip('Verifying state…')
    else:  # not_in_queue
        act = menu.addAction('Abort job')
        act.setEnabled(False)
        act.setToolTip('Job not in the active queue. Refreshing…')

    if pa is not None and state_kind != 'unreachable':
        # Optimistic override yields to the offline state (see _fill_row).
        kind = pa.get('kind', '')
        act.setEnabled(False)
        act.setText(ABORTING_LABEL if kind == 'abort' else REMOVING_LABEL)
    elif act.isEnabled():
        act.triggered.connect(
            lambda _=False: container._on_active_action(entry))

    menu.addSeparator()
    _add_copy_job_id(menu, entry)
    _add_show_output_folder(menu, parent_widget, entry)


def _build_myjobs_history(menu, parent_widget, entry, panel, container):
    _add_job_detail_myjobs(menu, container, entry)

    outputs = entry.get('nfy_output_paths') or []
    status_str = (entry.get('nfy_status_str') or '').lower()
    can_read = bool(outputs) and status_str not in ('cancelled', 'failed')
    try:
        read_color = int(entry.get('nfy_read_color', 0) or 0)
    except (ValueError, TypeError):
        read_color = 0
    read_act = menu.addAction('Read Output(s)')
    if can_read:
        read_act.triggered.connect(
            lambda _=False: container.retrieve(outputs, read_color))
    else:
        read_act.setEnabled(False)
        read_act.setToolTip('No outputs available for this job')

    pid = entry.get('prompt_id', '')
    del_act = menu.addAction('Remove from history')
    if pid:
        del_act.triggered.connect(
            lambda _=False: container.delete_entry(pid))
    else:
        del_act.setEnabled(False)

    menu.addSeparator()
    _add_copy_job_id(menu, entry)
    _add_show_output_folder(menu, parent_widget, entry)


# ---------------------------------------------------------------------------
# Shared menu-item helpers
# ---------------------------------------------------------------------------

def _add_job_detail(menu, panel, entry):
    """Add 'Job Detail' for per-machine tables (Queue / History sub-tables).

    Replicates the enrichment step used by the existing double-click
    handlers: prefer the store's enriched copy of the job, fall back
    to the raw entry if the store doesn't know the pid.
    """
    act = menu.addAction('Show Job Detail')
    # External jobs (submitted outside Nukomfy, e.g. the ComfyUI web UI)
    # carry no Nukomfy metadata: the dialog would show empty Detail/Log and
    # a workflow that never resolves. They only ever surface in the Queue
    # sub-table (the Suite never persists foreign prompts), so grey the item
    # out - the row exists only to signal that the machine is busy.
    if not entry.get('nfy_job_id'):
        act.setEnabled(False)
        act.setToolTip('External job. No Nukomfy details available.')
        return

    def _open():
        pid = entry.get('prompt_id')
        store = getattr(panel, '_store', None)
        if store is not None:
            try:
                store.load_local_history()
            except Exception:
                _log.debug('Job Detail: load_local_history failed',
                           exc_info=True)
        job = store.get(pid) if (store and pid) else None
        if not job:
            job = dict(entry)
        panel.show_job_dialog(job, initial_tab='detail')

    act.triggered.connect(lambda _=False: _open())


def _add_job_detail_myjobs(menu, container, entry):
    """Add 'Job Detail' for MyJobs tables (delegates to container)."""
    act = menu.addAction('Show Job Detail')
    act.triggered.connect(lambda _=False: container.show_detail(entry))


def _add_copy_job_id(menu, entry):
    """Add 'Copy Job ID' - copies nfy_job_id.

    Greyed for external jobs (no nfy_job_id): the only id they have is the
    raw ComfyUI prompt_id, which is not a Nukomfy job the plugin manages -
    copying it serves no purpose. Nukomfy jobs always carry a nfy_job_id.
    """
    job_id = entry.get('nfy_job_id') or ''
    act = menu.addAction('Copy Job ID')
    if not job_id:
        act.setEnabled(False)
        return
    act.triggered.connect(
        lambda _=False, jid=job_id:
            QtWidgets.QApplication.clipboard().setText(jid))


def _add_read_outputs(menu, parent_widget, entry):
    """Add 'Read Output(s)' - create Read nodes for the job's outputs.

    Greyed when there are no outputs or the job failed / was cancelled,
    mirroring the MyJobs history and the machine job viewer.
    """
    outputs = entry.get('nfy_output_paths') or []
    status_str = (entry.get('nfy_status_str') or '').lower()
    can_read = bool(outputs) and status_str not in ('cancelled', 'failed')
    act = menu.addAction('Read Output(s)')
    if not can_read:
        act.setEnabled(False)
        return
    try:
        color = int(entry.get('nfy_read_color', 0) or 0)
    except (ValueError, TypeError):
        color = 0
    act.triggered.connect(
        lambda _=False, p=list(outputs), c=color, pw=parent_widget:
            read_outputs(pw, p, c))


def _add_show_output_folder(menu, parent_widget, entry):
    """Add 'Show Output(s) Folder' - opens each distinct output folder.

    Derives a deduplicated list of folders from `nfy_output_paths`
    (populated at submit time). Greyed when no output paths are known.
    On failure (folder missing, share offline, launch error) shows a
    concise popup - never falls back to a parent directory.
    """
    paths = entry.get('nfy_output_paths') or []
    act = menu.addAction('Show Output(s) in File Browser')
    folders = _distinct_folders(paths)
    if not folders:
        act.setEnabled(False)
        return
    act.triggered.connect(
        lambda _=False, fs=folders, pw=parent_widget:
            _open_output_folders(pw, fs))


def _distinct_folders(paths):
    """Return the deduplicated list of `dirname(p)` for each path,
    preserving submission order. Dedup is case-insensitive on Windows."""
    seen = set()
    out = []
    for p in paths:
        if not p:
            continue
        folder = os.path.dirname(p) or p
        key = os.path.normcase(os.path.normpath(folder))
        if key in seen:
            continue
        seen.add(key)
        out.append(folder)
    return out


def _open_output_folders(parent_widget, folders):
    results = [reveal_folder(f) for f in folders]
    n_total = len(results)
    n_opened = sum(1 for r in results if r == REVEAL_OK)
    if n_opened == n_total:
        return
    msg = _reveal_failure_message(results, n_total, n_opened)
    if msg:
        _dialogs.inform(parent_widget, 'Could not open folder', msg)


def _reveal_failure_message(results, n_total, n_opened):
    if n_opened > 0:
        return 'Some output folders could not be opened.'
    reasons = {r for r in results if r != REVEAL_OK}
    if reasons == {REVEAL_MISSING}:
        return 'Output folder not created yet.'
    if reasons == {REVEAL_UNREACHABLE}:
        return ('Output folder not reachable. Network share or remote '
                'drive may be offline.')
    if reasons == {REVEAL_LAUNCH_FAILED}:
        return 'File manager unavailable.'
    return 'Output folder unavailable.'
