"""Render Manager action dispatch (abort / remove).

Background worker (_ActionWorker) that POSTs abort / remove HTTP
requests, plus the orchestrator (_abort_or_remove_entry) that gates
ownership / pre-flight / double-click and hands off to the worker.
Includes the `_LOCALLY_REMOVED_TTL_S` constant used by the panel's
result filter, and the age-based local history sweep (_sweep_aged_out).

Internal module - public API of Render Manager surfaced via
render_queue_panel.py.
"""

import datetime as _datetime
import logging

from Nukomfy.utils.qt_compat import QtWidgets, QtCore
from Nukomfy.gui import _dialogs

from Nukomfy.client.comfy_api import abort, delete_from_queue
from Nukomfy.client.machines import machine_manager
from Nukomfy.client import manager_client
from Nukomfy.core.identity import current_user
from Nukomfy.core.settings import settings
from Nukomfy.gui import _admin_gate

_log = logging.getLogger(__name__)


def read_outputs(parent, output_paths, color=0):
    """Create Nuke Read nodes for *output_paths*, with warning popups parented
    to *parent* (so they don't slip behind the dialog on dismiss). Shared by
    the History sub-table, the machine job viewer, and MyJobs."""
    from Nukomfy.gizmos.gizmo_actions import scan_output_paths, create_read_nodes
    status, _ = scan_output_paths(output_paths)
    if status == 'no_dir':
        _dialogs.inform(
            parent, 'Could not import output',
            'Unable to import output.\n\n'
            'The output directory could not be found. The files may not '
            'have been rendered yet, or the directory may have been moved '
            'or deleted.')
        return
    if status == 'no_frames':
        _dialogs.inform(
            parent, 'No output frames found',
            'No output frames found on disk.\n\n'
            'The output directory exists but contains no rendered frames. '
            'The job may have failed, or the frames may have been deleted '
            'or overwritten by a later job.')
        return
    created = create_read_nodes(output_paths, on_empty_message=False,
                                color=color)
    if created > 0:
        msg = ('Imported 1 output.' if created == 1
               else 'Imported {} outputs.'.format(created))
        _dialogs.inform(parent, 'Outputs imported', msg)


# How long a locally-removed prompt_id stays in the filter set before
# being forcibly evicted (fallback cleanup when server-confirmed drop
# logic didn't catch it - e.g. if the machine went offline between
# remove and next refresh). 30s is plenty: `delete_from_queue` returns
# synchronously once the server has acknowledged, so in practice the
# very first post-remove refresh already confirms the deletion.
#
# `_locally_removed` is a server-data filter - it must outlast a fetch
# in flight that may still echo a deleted pid in `recent_terminal_ids`
# or `new_terminals`. (`_pending_actions`, the UI in-flight bridge, has
# no TTL: it is handed off to the server-side state, not timed out.)
_LOCALLY_REMOVED_TTL_S = 30.0



# ---------------------------------------------------------------------------
# Age-based safety net
# ---------------------------------------------------------------------------
# Recent terminals per machine are ingested by the unified fetch cycle
# (`UnifiedFetchWorker` + `RenderDataStore`) via `/api/jobs?limit=N` on every
# tick, and new terminals are persisted in `RenderQueuePanel._on_result`
# through `submit_history.persist_terminal_state`.
# The age-based sweep freezes local entries whose `sent_at` predates
# `lost_job_timeout_days` as `failed`, so MyJobs doesn't keep showing them
# as `awaiting` forever. Runs after each refresh cycle from
# `_on_refresh_finished`.
def _sweep_aged_out():
    """Freeze any non-terminal local entry older than the timeout.

    Safety net for entries we can't reconcile against a server (machine
    offline, removed from machines.json, or rolled off the server's recent
    listing before we saw it terminal). Idempotent - already-persisted
    entries are skipped by `terminal_persisted` check.
    """
    try:
        days = int(settings.lost_job_timeout_days)
    except (ValueError, TypeError):
        return
    if days <= 0:
        return
    from Nukomfy.data.submit_history import get_history, persist_as_lost
    cutoff = _datetime.datetime.now() - _datetime.timedelta(days=days)
    for e in get_history():
        if e.get('nfy_terminal_persisted'):
            continue
        pid = e.get('prompt_id')
        if not pid:
            continue
        sent_raw = e.get('nfy_sent_at', '')
        if not sent_raw:
            continue
        try:
            sent = _datetime.datetime.fromisoformat(sent_raw)
        except (ValueError, TypeError):
            continue
        if sent < cutoff:
            persist_as_lost(pid)


class _ActionWorker(QtCore.QThread):
    """Background POST for abort/remove so the UI stays responsive while
    the server processes the request (worst-case 10s HTTP timeout).
    Emits `done(prompt_id, ok)` once the POST returns.

    Two modes:
      * Regular (admin_password is None): aborts the running job via
        /nukomfy/abort, or removes a pending one via /api/queue delete.
      * Admin (admin_password is a verified password from _admin_gate):
        hits /nukomfy/admin/force_abort or /nukomfy/admin/force_remove on
        the custom node. On non-ok admin result, emits `admin_error(pid,
        reason)` before `done(pid, False)` so the panel can surface a
        descriptive popup distinct from the generic offline case.
    """

    done = QtCore.Signal(str, bool)
    admin_error = QtCore.Signal(str, str)  # (prompt_id, reason)
    # Regular (non-admin) abort only: the server reported the prompt is no
    # longer running (409). Routed apart from `done(ok=False)` so the panel
    # surfaces "no longer running" without marking the machine offline.
    not_running = QtCore.Signal(str)  # (prompt_id)

    def __init__(self, url, prompt_id, is_running, *,
                 admin_password=None, affected_user=None, nfy_job_id=None,
                 parent=None):
        super().__init__(parent)
        self._url = url
        self._pid = prompt_id
        self._is_running = is_running
        self._admin_password = admin_password
        self._affected_user = affected_user
        self._nfy_job_id = nfy_job_id
        self._cancelled = False

    def cancel(self):
        # Lets `stop_worker` (panel teardown) abandon a running POST without
        # emitting into a dying panel. The HTTP call itself isn't
        # interruptible; the flag just gates the post-return emit.
        self._cancelled = True

    def run(self):
        if self._admin_password is None:
            if self._is_running:
                res = abort(self._url, self._pid)
                if self._cancelled:
                    return
                if res == 'not_running':
                    # Job finished between the pre-flight snapshot and this
                    # POST. Not a machine failure - surface distinctly so the
                    # panel doesn't mark the (online) machine offline.
                    self.not_running.emit(self._pid)
                    return
                ok = (res == 'ok')
            else:
                ok = delete_from_queue(self._url, [self._pid])
                if self._cancelled:
                    return
            self.done.emit(self._pid, ok)
            return
        fn = (manager_client.force_abort if self._is_running
              else manager_client.force_remove)
        result = fn(self._url, self._admin_password, self._pid,
                    self._affected_user, self._nfy_job_id)
        if self._cancelled:
            return
        if result == 'not_running':
            # Admin force_abort authenticated but the job was not the running
            # prompt (or the mark failed in-process): no abort took effect.
            # Clear the optimistic row without marking the machine offline -
            # mirrors the regular (same-user) 409 path.
            self.not_running.emit(self._pid)
            return
        is_ok = (result == "ok")
        if not is_ok:
            self.admin_error.emit(self._pid, result)
        self.done.emit(self._pid, is_ok)


def _machine_name_for_url(machine_url):
    """Best-effort lookup of a friendly name for *machine_url*. Falls
    back to '?' when the machine is not in the manager (the URL itself
    must never be surfaced through this helper since it feeds user-
    visible popup text).
    """
    if not machine_url:
        return '?'
    for m in machine_manager.machines:
        if getattr(m, 'url', None) == machine_url:
            return m.name or '?'
    return '?'


def _info_rich(parent, html):
    """Information popup with HTML rendering enabled - used by call sites
    that want bold/italic markup. `QMessageBox.information(...)` is
    static and renders in AutoText mode which is not guaranteed to pick
    up `<b>` tags; setting RichText explicitly makes the bold reliable."""
    mbox = _dialogs.message_box(parent)
    mbox.setIcon(QtWidgets.QMessageBox.Information)
    mbox.setWindowTitle('Nukomfy')
    mbox.setTextFormat(QtCore.Qt.RichText)
    mbox.setText(html)
    mbox.setStandardButtons(QtWidgets.QMessageBox.Ok)
    mbox.exec_()


def _show_machine_offline_popup(parent, action_verb, machine_url):
    """Single explanatory dialog used both by the pre-flight gate in
    `_abort_or_remove_entry` and by the race fallback in
    `_on_action_done(ok=False)`. Same wording everywhere so the user
    always reads the same explanation when an action can't reach the
    server."""
    name = _machine_name_for_url(machine_url)
    msg = (
        "Machine '{name}' is offline.\n\n"
        "The {action} cannot be performed right now.\n"
        "Affected jobs will show as 'Unknown' until the machine "
        "reconnects."
    ).format(name=name, action=action_verb)
    _dialogs.inform(parent, 'Machine offline', msg)


def _abort_or_remove_entry(parent, panel, entry, is_running, machine_url):
    """Shared Abort/Remove flow for MyJobs Active rows - optimistic
    greyed-row pattern.

    1. Ownership + confirm gates.
    2. Mark prompt_id in `panel._pending_actions` - the row re-renders
       as greyed (action button disabled) on the next refresh tick.
    3. Fire the server POST on a worker thread (no UI freeze).
    4. On POST return:
         - success -> arm the pending_action for verification and kick
           `panel._refresh()`; `_on_store_changed` will verify whether
           the server removed the job from running/pending.
         - failure -> clear the pending_action immediately (row un-greys,
           user retries). No popup: the greyed row IS the feedback.

    Server is source of truth throughout. No pre-marking of cancelled.
    """
    # External jobs (submitted outside Nukomfy, e.g. the ComfyUI web UI)
    # carry no nfy_job_id and an empty nfy_submitted_by. The empty submitter
    # would slip past the cross-user admin-password gate below (job_user
    # falsy -> gate skipped), so a foreign prompt could be aborted with no
    # password at all. They are not the plugin's to manage: refuse here, the
    # authoritative boundary behind the already-greyed button and menu item.
    if not entry.get('nfy_job_id'):
        return

    job_user = entry.get('nfy_submitted_by') or ''
    current_user_name = current_user()
    action_verb = 'abort' if is_running else 'remove'
    prompt_id = entry.get('prompt_id', '')
    job_ref = entry.get('nfy_job_id') or prompt_id or '?'

    # Machine no longer in Settings: cannot dispatch HTTP. Tell the user
    # explicitly so the action does not appear to silently fail.
    known_urls = {(m.url or '').rstrip('/')
                  for m in machine_manager.machines}
    if (machine_url or '').rstrip('/') not in known_urls:
        _dialogs.inform(
            parent, 'Machine not configured',
            "Machine for job <b>{}</b> is no longer configured in "
            "Settings.<br>Re-add it to manage this job.".format(job_ref))
        return

    admin_password = None
    if job_user and job_user != current_user_name:
        # Cross-user: route through the admin password gate. The helper
        # also handles the "manager not installed" and "no password set"
        # cases with informative popups parametrised on operation_label.
        admin_password = _admin_gate.prompt_admin_password(
            parent=parent,
            base_url=machine_url,
            operation_label='force ' + action_verb,
            machine_label=_machine_name_for_url(machine_url),
            info_lines=[
                "Job <b>{}</b> submitted by <b>{}</b>.".format(
                    job_ref, job_user),
            ],
        )
        if admin_password is None:
            return

    if admin_password is None:
        confirm_text = ('Are you sure you want to abort job <b>{}</b>?'.format(job_ref)
                        if is_running
                        else 'Remove job <b>{}</b> from the queue?'.format(job_ref))
        mbox = _dialogs.message_box(parent)
        mbox.setIcon(QtWidgets.QMessageBox.Question)
        mbox.setWindowTitle('Abort job' if is_running else 'Remove job')
        mbox.setTextFormat(QtCore.Qt.RichText)
        mbox.setText(confirm_text)
        mbox.setStandardButtons(
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if mbox.exec_() != QtWidgets.QMessageBox.Yes:
            return
    if panel is None or not prompt_id:
        return

    # Pre-flight: if the store knows the machine is offline / errored,
    # don't even POST. The action cannot succeed and would leave the
    # row flickering between greyed and stale running/pending. A single
    # popup explains why and what the user will see ('Unknown' until
    # the machine reconnects).
    info = panel._store.machine_info(machine_url)
    if not info or info.get('error') or info.get('status') == 'offline':
        _show_machine_offline_popup(parent, action_verb, machine_url)
        return

    # Re-validate against the current store snapshot. Between dialog
    # open and Yes click, the server may have transitioned the job:
    #   pending -> running  (Remove on pending would POST DELETE on a
    #     running job; ComfyUI ignores DELETE on running, but we'd
    #     still wipe the local entry assuming success).
    #   pending/running -> terminal  (Remove/Abort on a job that
    #     already completed would also wipe a legitimate history entry).
    # The store may be slightly stale (last fetch 0-30s old) but covers
    # the common case where the dialog sits open across an auto-refresh
    # tick. A more aggressive re-fetch could be added later if needed.
    running_ids = {j.get('prompt_id')
                   for j in (info.get('running_jobs') or [])}
    pending_ids = {j.get('prompt_id')
                   for j in (info.get('pending_jobs') or [])}
    if is_running:
        if prompt_id not in running_ids:
            _info_rich(
                parent,
                "Job <b>{}</b> is no longer running and cannot be "
                "aborted.".format(job_ref))
            return
    else:
        if prompt_id in running_ids:
            _info_rich(
                parent,
                "Job <b>{}</b> has just started running and cannot be "
                "removed from the queue.<br><br>"
                "Use 'Abort' instead if you want to stop it.".format(
                    job_ref))
            return
        if prompt_id not in pending_ids:
            _info_rich(
                parent,
                "Job <b>{}</b> is no longer in the queue and cannot be "
                "removed.".format(job_ref))
            return

    # Guard against rapid double-click on the same row: a pending_action
    # already in flight means a worker is processing this pid. Silently
    # ignoring the second click avoids spawning two _ActionWorker threads
    # for the same prompt_id.
    if prompt_id in panel._pending_actions:
        return

    panel._pending_actions[prompt_id] = {
        'kind': 'abort' if is_running else 'remove',
        'nfy_machine_url': machine_url,
        # `armed` flips to True after the POST returns 2xx - verification
        # in `_on_store_changed` only runs for armed entries, so a
        # refresh that races the POST doesn't clear the flag prematurely.
        'armed': False,
    }
    # Re-render MyJobs Active + (if expanded on the same machine) the
    # per-machine Queue sub-table so the greyed state appears immediately,
    # before the POST even goes out. Both rebuilds are cheap (in-memory)
    # and pick up the fresh `_pending_actions` entry for the row variant.
    panel._notify_pending_actions_changed(machine_url)

    worker = _ActionWorker(
        machine_url, prompt_id, is_running,
        admin_password=admin_password,
        affected_user=(job_user if admin_password else None),
        nfy_job_id=(entry.get('nfy_job_id') if admin_password else None),
        parent=panel,
    )
    worker.done.connect(panel._on_action_done)
    worker.not_running.connect(panel._on_action_not_running)
    if admin_password is not None:
        worker.admin_error.connect(panel._on_admin_action_error)
    # Reap the thread when it finishes so `_action_workers` doesn't grow for
    # the panel's lifetime (parent=panel keeps finished threads alive too).
    worker.finished.connect(lambda w=worker: panel._reap_action_worker(w))
    worker.start()
    panel._action_workers.append(worker)


def _entry_for_log(entry):
    """Normalise a submit_history entry for `_JobDialog`: ensure `messages`
    exists (empty list placeholder) on a local copy so the source entry is
    not mutated by downstream consumers."""
    e = dict(entry)
    e.setdefault('nfy_messages', [])
    return e
