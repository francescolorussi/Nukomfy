"""Modal submit dialog: queue status, frame ranges, input cache, submit."""

import datetime
import json
import logging
import os
import uuid

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui._fields import NukomfyLineEdit
from Nukomfy.gui import _dialogs

_log = logging.getLogger(__name__)

from Nukomfy.client.machines import machine_manager, check_queue
from Nukomfy.client.comfy_api import post_prompt
from Nukomfy.core.identity import current_user, ws_session_id
from Nukomfy.core.settings import settings
from Nukomfy.utils.log_format import fmt_job, fmt_machine
from Nukomfy.gui.ui_state import ui_state, center_on_screen
from Nukomfy.gui.icons import (icon_font, material_icon, set_press_icon,
                    REFRESH, PUBLISH,
                    VIEW_LIST, ARROW_UPWARD, ARROW_DOWNWARD)
from Nukomfy.gui.workers import UnifiedFetchWorker, stop_worker
from Nukomfy.gui._auto_refresh import RefreshCycle


from Nukomfy.gui._table_utils import _proportional_fit, _install_absorber
from Nukomfy.gui._splitter import DottedSplitter
from Nukomfy.gui import _focus_drop
from Nukomfy.gui._message_dialogs import _PathListDialog


# ---------------------------------------------------------------------------
# Column indices - machine table
# ---------------------------------------------------------------------------
# Layout mirrors the Render Manager: selector + merged-dot Status.
_M_COL_SEL    = 0
_M_COL_STATUS = 1
_M_COL_NAME   = 2
_M_COL_QUEUE  = 3
_M_COL_COMFY  = 4
_M_COL_OS     = 5
_M_COL_GPU    = 6
_M_COL_VRAM   = 7
_M_COL_RAM    = 8
_M_HEADERS    = ['', 'Status', 'Name', 'Queue',
                 'ComfyUI', 'OS', 'GPU', 'VRAM', 'RAM']

from Nukomfy.gui.status_display import (
    render_machine_status, _make_status_cell, _update_status_cell)


def _resolve_workflow_path(workflow_id, stored_path=''):
    """Locate the workflow JSON by UUID against the configured Library roots.

    The gizmo stores only the workflow UUID (no absolute path), so lookup
    is robust to folder renames/moves and to sharing the .nk across
    machines. `stored_path` is an optional in-memory fast-path hint (a path
    already resolved earlier this session); when it points at an existing
    file it is returned directly to avoid a rescan.
    """
    if stored_path and os.path.isfile(stored_path):
        return stored_path

    if not workflow_id:
        return None

    from Nukomfy.core.settings import settings
    from Nukomfy.utils.path_utils import runtime_path
    try:
        from Nukomfy.workflows.workflow_loader import scan_workflows
        local_root = ('' if settings.disable_local_workflows
                      else runtime_path(settings.local_workflow_path,
                                        fallback=settings.local_workflow_path))
        shared_roots = [runtime_path(p, fallback=p)
                        for p in settings.shared_workflow_paths]
        matches = [it for it in scan_workflows(local_root, shared_roots)
                   if it.workflow_id == workflow_id]
        if not matches:
            return None
        chosen = matches[0]
        if len(matches) > 1:
            _log.warning(
                "Workflow UUID %s found in multiple folders: %s (using %s)",
                workflow_id,
                [it.folder_path for it in matches],
                chosen.folder_path)
        if os.path.isfile(chosen.workflow_path):
            return chosen.workflow_path.replace('\\', '/')
    except Exception as e:
        _log.warning(
            'Workflow UUID lookup failed for %s: %s', workflow_id, e)
    return None


_WORKFLOW_MISSING_MSG = (
    'The workflow file referenced by this gizmo could not be found:\n\n'
    '{path}\n\n'
    'It may have been removed, moved, or the Library folder is currently '
    'offline. Open the Library, recreate the gizmo from the intended '
    'workflow, and try again.'
)


# ---------------------------------------------------------------------------
# Selector cell - paints a thin green accent bar on the left edge when
# the radio in the row is selected. Keeps the radio untouched, no
# layout shift.
# ---------------------------------------------------------------------------
class _SelectorCell(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = False

    def set_selected(self, on):
        if self._selected != bool(on):
            self._selected = bool(on)
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._selected:
            return
        p = QtGui.QPainter(self)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor('#3fa668'))
        p.drawRect(0, 0, max(2, 3), self.height())
        p.end()


# ---------------------------------------------------------------------------
# Integer-only editor delegate - used on frame-range columns to reject
# letters, symbols, and out-of-range values at input time (paste included).
# ---------------------------------------------------------------------------
class _IntEditDelegate(QtWidgets.QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = NukomfyLineEdit(parent)
        editor.setValidator(QtGui.QIntValidator(editor))
        return editor


# ---------------------------------------------------------------------------
# Submit worker - full pipeline
# ---------------------------------------------------------------------------
def _build_all_check_dirs(gizmo, output_starts, batch_count):
    """Per-output collision detection for the pre-submit overwrite
    dispatch. For each enabled output role, computes:

        - The output directory (`dir`).
        - The basename pattern (with `####` placeholder).
        - The set of basenames this submit could write (`target_basenames`):
            Single mode: exact frames {start, start+1, ..., start+batch-1}.
            Sequence mode: {start, ..., 10**pad - 1} (natural upper bound
                         by padding - Sequence output count is unknown
                         until the model produces it, so we treat
                         'frame >= start' as the conservative scope of
                         this submit).
        - The set of basenames already on disk matching the pattern (`existing`).
        - The intersection (`collisions`).

    Frame numbers below `start_frame` (Sequence) and frames outside the
    Single batch window are NOT in `target_basenames` - they're considered
    out of scope for this submit and preserved by both Delete and Overwrite
    paths.

    Returns list of dicts (one per output role, no dedup yet - the popup
    dispatcher merges by directory afterwards).
    """
    from Nukomfy.utils.output_path import resolve_gizmo_outputs

    outputs = resolve_gizmo_outputs(gizmo, frame_style='hash')
    if not outputs:
        return []

    results = []
    for idx, o in enumerate(outputs):
        full_path = o['path']
        pad = o['padding']
        io_mode = o['io_mode']
        placeholder = '#' * pad

        check_dir = os.path.dirname(full_path)
        pattern_basename = os.path.basename(full_path)
        start_frame = (output_starts[idx] if idx < len(output_starts) else 1)

        if io_mode == 'Single':
            frame_iter = range(start_frame, start_frame + batch_count)
        else:  # 'Sequence' or '' - enumerate from start to padding limit
            frame_iter = range(start_frame, 10 ** pad)
        target_basenames = {
            pattern_basename.replace(placeholder, str(n).zfill(pad))
            for n in frame_iter
        }

        existing = set()
        from Nukomfy.utils.fs_safe import _long_path
        if os.path.isdir(_long_path(check_dir)):
            try:
                import glob as _glob
                # glob.escape the literal dir + basename so bracket chars in a
                # user output root are not read as a glob char class; the frame
                # placeholder carries no glob metachars, so it survives to '*'.
                glob_pat = os.path.join(
                    _glob.escape(_long_path(check_dir)),
                    _glob.escape(pattern_basename).replace(placeholder, '*'))
                for f in _glob.glob(glob_pat):
                    existing.add(os.path.basename(f))
            except OSError:
                pass

        collisions = existing & target_basenames

        results.append({
            'name': o['name'],
            'dir': check_dir,
            'pattern': pattern_basename,
            'padding': pad,
            'io_mode': io_mode,
            'start_frame': start_frame,
            'target_basenames': target_basenames,
            'existing': existing,
            'collisions': collisions,
        })
    return results


def _build_output_path_info(gizmo):
    """Build output path info dict from gizmo knobs. Must run on main thread."""
    from Nukomfy.core.settings import settings
    from Nukomfy.utils.path_utils import runtime_path
    from Nukomfy.utils.output_path import nk_file_stem

    out_name_knob = gizmo.knob('output_name')
    if not out_name_knob:
        # Multi-output: try per-output name knobs
        output_name_0 = gizmo.knob('output_name_0')
        if not output_name_0:
            return None
        output_name = output_name_0.value().strip() or 'output'
    else:
        output_name = out_name_knob.value().strip() or 'output'

    ver_knob = gizmo.knob('_output_version')
    try:
        version = int(ver_knob.value()) if ver_knob else 1
    except (ValueError, TypeError):
        version = 1

    raw = settings.default_output_path
    base_dir = runtime_path(raw, fallback=raw)

    wf_name_knob = gizmo.knob('_nfy_wf_name')
    workflow_name = (wf_name_knob.value() if wf_name_knob else '') or 'output'
    alias_knob = gizmo.knob('_nfy_workflow_alias')
    workflow_alias = alias_knob.value() if alias_knob else ''

    # Collect per-output names (for multi-output gizmos)
    output_names = []
    for i in range(100):
        knob = gizmo.knob('output_name_{}'.format(i))
        if not knob:
            break
        output_names.append(knob.value().strip() or 'output')

    # Workflow metadata for output_path tokens
    wf_id_knob = gizmo.knob('_nfy_wf_id')
    workflow_uuid = wf_id_knob.value() if wf_id_knob else ''

    def _parse_json_list(knob_name):
        k = gizmo.knob(knob_name)
        if not k:
            return []
        try:
            data = json.loads(k.value() or '[]')
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    workflow_categories = _parse_json_list('_nfy_wf_categories')
    workflow_models = _parse_json_list('_nfy_wf_models')

    return {
        'output_dir': base_dir.replace('\\', '/').rstrip('/'),
        'nk_file': nk_file_stem(),
        'node_name': gizmo.name(),
        'output_name': output_name,
        'output_names': output_names,
        'version': version,
        'workflow_name': workflow_name,
        'workflow_alias': workflow_alias,
        'workflow_uuid': workflow_uuid,
        'workflow_categories': workflow_categories,
        'workflow_models': workflow_models,
    }


def _find_my_in_flight_for_gizmo(machine_url, submitted_by, submitter_host,
                                 nk_file, node_name):
    """Return running/pending jobs on `machine_url` submitted by this user
    from the same gizmo in the same .nk file. Each dict carries an extra
    `_queue_state` field: 'running' or 'pending'.

    Match is on `(nfy_submitted_by, nfy_submitter_host)`, the canonical
    split of the submitter identity. Used to detect the concurrency case where a second
    submit would wipe the Input Cache or output dir still in use by a
    previous job from the same gizmo + same workstation.
    """
    from Nukomfy.client.comfy_api import check_queue_status
    status = check_queue_status(machine_url)
    if status.get('error'):
        return []
    matches = []
    for state, key in (('running', 'running_jobs'), ('pending', 'pending_jobs')):
        for j in status.get(key) or []:
            if j.get('nfy_submitted_by') != submitted_by:
                continue
            if j.get('nfy_submitter_host') != submitter_host:
                continue
            if j.get('nfy_nk_file') != nk_file:
                continue
            if j.get('nfy_node_name') != node_name:
                continue
            j2 = dict(j)
            j2['_queue_state'] = state
            matches.append(j2)
    return matches


def _match_job_to_check_dir(jobs, check_dir):
    """Return the first job in `jobs` whose any output_path resolves to
    `check_dir` (same directory, case-insensitive on Windows), else None.
    """
    if not check_dir:
        return None
    target = os.path.normpath(check_dir).casefold()
    for j in jobs:
        for p in j.get('nfy_output_paths') or []:
            if not p:
                continue
            try:
                d = os.path.normpath(os.path.dirname(p)).casefold()
            except Exception:
                continue
            if d == target:
                return j
    return None


def _match_job_to_cache_dir(jobs, new_cache_dirs):
    """Return the first job in `jobs` whose any input_cache_dir matches one
    of `new_cache_dirs`, else None. Workflow JSON always uses forward-slash
    (post path_substitution for the target machine), so comparison is on
    slash-normalized, rstripped, casefolded paths.
    """
    if not new_cache_dirs:
        return None
    targets = {d.replace('\\', '/').rstrip('/').casefold()
               for d in new_cache_dirs if d}
    if not targets:
        return None
    for j in jobs:
        for d in j.get('input_cache_dirs') or []:
            if not d:
                continue
            if d.replace('\\', '/').rstrip('/').casefold() in targets:
                return j
    return None


def _ask_input_cache_conflict(parent, job, machine):
    """Warn the user their Input Cache is still being used by another job.

    Returns 'cancel_inflight' | 'wait' | 'continue'.
    """
    pid = (job.get('prompt_id') or '')[:8]
    state = 'Running' if job.get('_queue_state') == 'running' else 'Queued'
    mname = getattr(machine, 'name', None) or 'Unnamed machine'
    text = (
        "You're about to rewrite the Input Cache for this gizmo, but "
        "another of your jobs is still using it:\n\n"
        "\u2022 Job: {pid}\n"
        "\u2022 Machine: {mname}\n"
        "\u2022 Status: {state}\n\n"
        "Continuing may cause the running job to fail or produce "
        "corrupted frames."
    ).format(pid=pid, mname=mname, state=state)

    box = _dialogs.message_box(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle('Input Cache in use by a running job')
    box.setText(text)
    b_cancel = box.addButton(
        'Cancel running job(s) and continue', QtWidgets.QMessageBox.AcceptRole)
    b_wait = box.addButton('Wait', QtWidgets.QMessageBox.RejectRole)
    b_force = box.addButton(
        'Submit anyway', QtWidgets.QMessageBox.DestructiveRole)
    box.setDefaultButton(b_wait)
    box.exec_()
    clicked = box.clickedButton()
    if clicked is b_cancel:
        return 'cancel_inflight'
    if clicked is b_force:
        return 'continue'
    return 'wait'


def _ask_output_in_use(parent, conflicting_jobs, machine):
    """Warn that one or more output folders already exist AND running/
    queued jobs of ours are writing there.

    *conflicting_jobs* is a list of ``(job_dict, check_dir)`` tuples - one
    entry per colliding output folder. Returns
    ``'cancel_and_overwrite'`` | ``'cancel_submit'`` ('cancel_and_overwrite' cancels ALL
    conflicting jobs).
    """
    mname = getattr(machine, 'name', None) or 'Unnamed machine'
    n = len(conflicting_jobs)
    if n == 1:
        job, cd = conflicting_jobs[0]
        pid = (job.get('prompt_id') or '')[:8]
        primary = (
            'The output folder already exists and one of your jobs is '
            'still writing there:\n{}\nJob {} on {}.'.format(
                os.path.normpath(cd), pid, mname))
    else:
        lines = []
        for (job, cd) in conflicting_jobs:
            pid = (job.get('prompt_id') or '')[:8]
            lines.append('  {}  (job {} on {})'.format(
                os.path.normpath(cd), pid, mname))
        primary = ('{} output folders already exist and your running '
                   'jobs are still writing there:\n{}').format(
                       n, '\n'.join(lines))
    dlg = _PathListDialog(
        title='Output in use by a running job',
        primary=primary,
        informative=(
            "Overwriting now would destroy frames that are currently being written."),
        paths=[],
        buttons='OVERWRITE_AND_CANCEL_INFLIGHT',
        icon='warning',
        parent=parent)
    if dlg.exec_() == QtWidgets.QDialog.Accepted:
        return 'cancel_and_overwrite'
    return 'cancel_submit'


def _ask_batch_threshold(parent, n, machine):
    """Warn before submitting `n` jobs when `n` >= batch_warning_threshold.
    Returns True to proceed, False to cancel.
    """
    mname = getattr(machine, 'name', None) or 'Unnamed machine'
    box = _dialogs.message_box(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle('Confirm batch submit')
    box.setTextFormat(QtCore.Qt.RichText)
    box.setText(
        'You are about to send <b>{}</b> jobs to '
        '<b>{}</b>.<br><br>Continue?'.format(n, mname))
    b_yes = box.addButton('Submit', QtWidgets.QMessageBox.YesRole)
    b_no = box.addButton('Cancel', QtWidgets.QMessageBox.NoRole)
    box.setDefaultButton(b_no)
    box.exec_()
    return box.clickedButton() is b_yes


def _cancel_in_flight_job(machine_url, job):
    """Cancel a job on `machine_url`. Uses the abort endpoint for running
    jobs, `/queue` DELETE for pending. Both underlying helpers swallow
    errors, so this is best-effort fire-and-forget.
    """
    from Nukomfy.client.comfy_api import abort, delete_from_queue
    pid = job.get('prompt_id')
    if not pid:
        return
    try:
        if job.get('_queue_state') == 'running':
            abort(machine_url, prompt_id=pid)
        else:
            delete_from_queue(machine_url, [pid])
    except Exception as e:
        _log.warning(
            'Cancel queued %s on %s failed: %s',
            fmt_job(pid), fmt_machine(machine_url), e)


def _stop_jobs_and_wait(parent, machine_url, jobs):
    """Cancel each job and synchronously wait - without timeout - for
    ComfyUI to report a terminal status before returning.

    Returns True if every conflicting job reached a terminal state and
    the caller may proceed with the submit; False if the user aborted
    via the dialog's Close button or the machine became unreachable
    mid-wait. In either False case the caller MUST abort the submit
    without touching local files.

    No automatic timeout by design: a fail-open after N seconds would
    re-introduce a race where local cleanup steps on frames the server
    is still writing. ComfyUI checks the interrupt flag only between
    KSampler steps, and VAEDecode/SaveImage
    finish the current frame regardless - heavy workflows can take 30s+
    to actually stop. The user always has an explicit Close button to
    bail out manually if the wait becomes unacceptable.
    """
    if not jobs:
        return True
    n = len(jobs)
    job_ids = [j.get('nfy_job_id') or '?' for j in jobs
               if isinstance(j, dict)]
    if n == 1:
        header = 'Stopping job {}…'.format(job_ids[0])
    else:
        header = 'Stopping {} jobs ({})…'.format(n, ', '.join(job_ids))
    detail = ('ComfyUI must finish the current step before the job '
              'is fully released.\nThe submit will continue '
              'automatically once the server confirms cleanup.')

    # Subclass that swallows ESC and window-close events: only the
    # explicit Close button is allowed to abort the wait. Without this,
    # any of those would emit `rejected` and the post-finally check
    # would mis-read it as a user cancel.
    class _StopDlg(QtWidgets.QDialog):
        def keyPressEvent(self, e):
            if e.key() == QtCore.Qt.Key_Escape:
                e.ignore()
                return
            super().keyPressEvent(e)

        def closeEvent(self, e):
            if not getattr(self, '_allow_close', False):
                e.ignore()
                return
            super().closeEvent(e)

    dlg = _StopDlg(parent)
    dlg.setWindowTitle('Stopping jobs')
    dlg.setWindowModality(QtCore.Qt.ApplicationModal)
    dlg.setWindowFlags(
        dlg.windowFlags()
        & ~QtCore.Qt.WindowCloseButtonHint
        & ~QtCore.Qt.WindowContextHelpButtonHint)
    layout = QtWidgets.QVBoxLayout(dlg)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)

    header_lbl = QtWidgets.QLabel(header, dlg)
    f = header_lbl.font()
    f.setBold(True)
    header_lbl.setFont(f)
    layout.addWidget(header_lbl)

    detail_lbl = QtWidgets.QLabel(detail, dlg)
    detail_lbl.setWordWrap(True)
    layout.addWidget(detail_lbl)

    timer_lbl = QtWidgets.QLabel('Time elapsed: 0s', dlg)
    timer_lbl.setAlignment(QtCore.Qt.AlignCenter)
    layout.addWidget(timer_lbl)

    def _fmt_elapsed(ms):
        s = int(ms // 1000)
        if s < 60:
            return '{}s'.format(s)
        return '{}m {:02d}s'.format(s // 60, s % 60)

    btn_row = QtWidgets.QHBoxLayout()
    btn_row.addStretch(1)
    close_btn = QtWidgets.QPushButton('Close', dlg)
    close_btn.setAutoDefault(False)
    close_btn.setDefault(False)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)
    apply_window_chrome(dlg)

    user_cancelled = {'flag': False}

    def _on_close():
        user_cancelled['flag'] = True
        dlg._allow_close = True
        dlg.reject()
    close_btn.clicked.connect(_on_close)

    dlg.setMinimumWidth(440)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    QtWidgets.QApplication.processEvents()

    machine_lost = False
    try:
        from Nukomfy.client.comfy_api import (fetch_job_status,
                                        _TERMINAL_JOB_STATUSES)
        for job in jobs:
            _cancel_in_flight_job(machine_url, job)
            QtWidgets.QApplication.processEvents()
            pid = job.get('prompt_id') if isinstance(job, dict) else None
            if not pid:
                continue
            t0 = QtCore.QElapsedTimer()
            t0.start()
            while True:
                if user_cancelled['flag']:
                    break
                kind, item = fetch_job_status(machine_url, pid)
                if kind == 'unreachable':
                    _log.warning(
                        'Machine %s unreachable while waiting on %s - '
                        'submit aborted',
                        fmt_machine(machine_url), fmt_job(pid))
                    machine_lost = True
                    break
                if kind == 'not_found':
                    break
                if (kind == 'ok' and item and
                        item.get('nfy_status_str', '')
                        in _TERMINAL_JOB_STATUSES):
                    break
                timer_lbl.setText(
                    'Time elapsed: ' + _fmt_elapsed(t0.elapsed()))
                QtWidgets.QApplication.processEvents()
                QtCore.QThread.msleep(300)
                QtWidgets.QApplication.processEvents()
            if machine_lost or user_cancelled['flag']:
                break
    finally:
        dlg._allow_close = True
        dlg.close()
        dlg.deleteLater()

    if user_cancelled['flag']:
        return False
    if machine_lost:
        _dialogs.warn(
            parent, 'Machine unreachable',
            'The ComfyUI machine became unreachable while waiting for '
            'the running job to stop.\n\n'
            'Submit aborted - your Input Cache and output frames were '
            'left untouched. Try again once the machine is back online.')
        return False
    return True


class _UserCancelled(Exception):
    """Raised when the user cancels the input cache write."""
    pass


class _SilentAbort(Exception):
    """Raised when the pipeline must abort but the user has already
    seen a detailed dialog from a lower-level helper. The outer
    exception handler swallows this without showing a second popup -
    the lower-level dialog is the only one the user reads."""
    pass


# ---------------------------------------------------------------------------
# Submit Panel
# ---------------------------------------------------------------------------
from Nukomfy.gui._theme import (
    TABLE_STYLE, apply_window_chrome)

# Section group-box style (Machines, Options): bordered box with a bold title.
_GROUP_STYLE = (
    'QGroupBox{border:1px solid #3a3a3a;'
    'border-radius:3px;margin-top:6px;padding-top:10px;}'
    'QGroupBox::title{subcontrol-origin:margin;left:8px;'
    'padding:0 4px;color:#eee;font-weight:bold;}')


class SubmitPanel(QtWidgets.QDialog):

    def __init__(self, gizmo_node, parent=None):
        super().__init__(parent)
        self._gizmo = gizmo_node

        # Refresh path preview before showing the panel so the user always
        # sees a value coherent with the current comp name (catches the
        # rare case where the .nk has been renamed without a knob touch).
        try:
            from Nukomfy.gizmos.gizmo_callbacks import _update_output_preview
            _update_output_preview(gizmo_node)
        except Exception:
            pass

        self._status_worker = None
        self._refresh_cycle = RefreshCycle(self._on_refresh_ready)
        self._auto_timer = None
        self._auto_timer_fired = False
        self._auto_selected = False
        self._manual_selected = False
        self._auto_applying = False
        self._row_status = {}  # row_idx -> status string
        # Per-row Availability flag, parallel to _row_status. Auto-select
        # and the radio enabled-state both consult this so an Unavailable
        # row behaves identically to an offline row (radio disabled, not
        # picked, selection dropped on transition).
        self._row_availability = {}  # row_idx -> 'available' | 'unavailable'
        self._row_pending = {}  # row_idx -> pending int (queue length)
        self._machines_populated = False  # Gate _populate_machines to init only

        self.setWindowTitle('Nukomfy - Submit to ComfyUI')
        self.setMinimumSize(1100, 400)
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint)
        apply_window_chrome(self)
        _focus_drop.install(self)

        # Load gizmo params
        from Nukomfy.workflows._payload import decode_payload
        params_knob = gizmo_node.knob('_nfy_params')
        self._gizmo_params = (
            decode_payload(params_knob.value(), default=[])
            if params_knob else [])

        wf_id_knob = gizmo_node.knob('_nfy_wf_id')
        self._workflow_id = wf_id_knob.value() if wf_id_knob else ''
        # Resolved from the UUID at submit time; show_submit_panel may
        # pre-set it to skip a rescan. No path is stored on the gizmo.
        self._workflow_path = ''

        self._input_params = [p for p in self._gizmo_params
                              if p.get('role') == 'input'
                              and p.get('enabled', True)]
        self._output_params = [p for p in self._gizmo_params
                               if p.get('role') == 'output'
                               and p.get('enabled', True)]
        # Only inputs flagged 'Sequence' appear in the Input Frame Ranges table.
        # 'Single' inputs get nuke.frame() at submit time.
        self._range_input_params = [p for p in self._input_params
                                    if p.get('io_mode') == 'Sequence']
        # Batch Count is only meaningful when every output is 'Single' -
        # mixing 'Sequence' outputs would cause overlapping writes.
        self._all_outputs_single = (
            bool(self._output_params)
            and all(p.get('io_mode') == 'Single'
                    for p in self._output_params))
        self._output_dirty = {}  # row_idx -> user-touched Start Frame?
        self._output_updating = False  # reentrancy guard for live-link
        self._input_updating = False  # reentrancy guard for empty-cell refill
        self._preflight_info_cache = None  # populated by _preflight_workflow_nodes

        self._build()
        ui_state.restore_geometry('submit_panel', self, fit=True)
        # Restore splitter proportion if a saved value is present.
        saved = ui_state.get('submit_panel').get('splitter_sizes')
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            try:
                self._splitter.setSizes([int(saved[0]), int(saved[1])])
            except (TypeError, ValueError):
                pass
        # Parentless modal: born centered on the monitor that holds Nuke's
        # main window. Size is restored from disk; position is intentionally
        # not persisted (only Library and Render Manager remember position).
        center_on_screen(self)
        self._refresh_machines()
        # Refresh the hardware/version snapshot once per session - the
        # periodic worker only polls /queue, so these columns would otherwise
        # show a stale cache until Settings > Machines is opened. Connect
        # first so the first sweep's results repaint this dialog's rows.
        from Nukomfy.gui._machine_info_service import service
        self._machine_info_service = service()
        self._machine_info_service.infoChanged.connect(self._on_machine_info_changed)
        self._machine_info_service.ensure_fresh()
        self._populate_inputs()
        self._populate_outputs()
        self._update_frame_range_availability()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Vertical splitter: Machines on top, Options + buttons on bottom.
        # 60/40 default; sizes persist via ui_state.
        self._splitter = DottedSplitter(QtCore.Qt.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(16)

        top_pane = QtWidgets.QWidget()
        top_lay = QtWidgets.QVBoxLayout(top_pane)
        top_lay.setContentsMargins(0, 0, 0, 0)

        bottom_pane = QtWidgets.QWidget()
        bottom_lay = QtWidgets.QVBoxLayout(bottom_pane)
        bottom_lay.setContentsMargins(0, 0, 0, 0)

        # ── Machine table ─────────────────────────────────────────
        machines_grp = QtWidgets.QGroupBox('Machines')
        machines_grp.setStyleSheet(_GROUP_STYLE)
        machines_inner = QtWidgets.QVBoxLayout(machines_grp)
        machines_inner.setContentsMargins(10, 0, 10, 10)

        tb = QtWidgets.QHBoxLayout()
        tb.addStretch()
        self._refresh_btn = QtWidgets.QPushButton('Update All')
        set_press_icon(self._refresh_btn, REFRESH)
        self._refresh_btn.setFixedHeight(24)
        self._refresh_btn.setToolTip('Update all machines now')
        self._refresh_btn.clicked.connect(self._refresh_machines)
        tb.addWidget(self._refresh_btn)
        machines_inner.addLayout(tb)

        self._machine_table = QtWidgets.QTableWidget(0, len(_M_HEADERS))
        self._machine_table.setHorizontalHeaderLabels(_M_HEADERS)
        h = self._machine_table.horizontalHeader()
        h.setStretchLastSection(False)
        for col in range(len(_M_HEADERS)):
            if col in (_M_COL_SEL, _M_COL_STATUS):
                h.setSectionResizeMode(col, QtWidgets.QHeaderView.Fixed)
            else:
                h.setSectionResizeMode(col, QtWidgets.QHeaderView.Interactive)
        self._machine_table.setColumnWidth(_M_COL_SEL, 26)
        self._machine_table.setColumnWidth(_M_COL_STATUS, 100)
        self._machine_table.setColumnWidth(_M_COL_NAME, 130)
        self._machine_table.setColumnWidth(_M_COL_COMFY, 80)
        self._machine_table.setColumnWidth(_M_COL_OS, 80)
        self._machine_table.setColumnWidth(_M_COL_QUEUE, 60)
        self._machine_table.setColumnWidth(_M_COL_GPU, 350)
        self._machine_table.setColumnWidth(_M_COL_VRAM, 85)
        self._machine_table.setColumnWidth(_M_COL_RAM, 85)
        _install_absorber(self._machine_table, _M_COL_RAM)
        self._machine_table.verticalHeader().setVisible(False)
        # Qt native row selection (yellow bg) for strong row feedback on
        # click, plus the _SelectorCell green bar on the selector column.
        self._machine_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection)
        self._machine_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self._machine_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        self._machine_table.setAlternatingRowColors(True)
        self._machine_table.setStyleSheet(TABLE_STYLE)
        self._machine_table.cellClicked.connect(self._on_machine_clicked)
        ui_state.restore_column_widths('submit_machine_table', self._machine_table)
        # Force fixed columns back after restore (Status is locked like in
        # Render Manager so the dot+label cell stays at its ideal width).
        h = self._machine_table.horizontalHeader()
        h.blockSignals(True)
        self._machine_table.setColumnWidth(_M_COL_SEL, 26)
        self._machine_table.setColumnWidth(_M_COL_STATUS, 100)
        h.blockSignals(False)
        _proportional_fit(self._machine_table)
        machines_inner.addWidget(self._machine_table)

        top_lay.addWidget(machines_grp)

        # ── Options group ─────────────────────────────────────────
        opts_grp = QtWidgets.QGroupBox('Options')
        opts_grp.setStyleSheet(_GROUP_STYLE)
        opts_lay = QtWidgets.QVBoxLayout(opts_grp)
        opts_lay.setContentsMargins(10, 0, 10, 10)
        opts_lay.setSpacing(8)

        # Row: Batch Count (right). The "Force Rewrite Input Cache"
        # checkbox is intentionally absent - fingerprint + per-frame
        # mtime/size validation
        # makes manual force unnecessary. The internal `force=` param of
        # write_input_cache is kept for future programmatic callers.
        top_row = QtWidgets.QHBoxLayout()
        top_row.addStretch()
        batch_lbl = QtWidgets.QLabel('Batch Count:')
        top_row.addWidget(batch_lbl, 0)

        # Custom batch counter: number field + stacked up/down arrows
        batch_w = QtWidgets.QWidget()
        batch_w.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                              QtWidgets.QSizePolicy.Fixed)
        batch_outer = QtWidgets.QHBoxLayout(batch_w)
        batch_outer.setContentsMargins(0, 0, 0, 0)
        batch_outer.setSpacing(3)

        # Flat by default; pressed state uses Nuke's palette Highlight
        # color (same orange used for selected table rows) so it matches
        # whatever Nuke version/theme is active.
        _hl = self.palette().color(QtGui.QPalette.Highlight).name()
        _BATCH_BTN = (
            'QPushButton{border:none;background:transparent;}'
            'QPushButton:hover{background:#3a3a3a;}'
            'QPushButton:pressed{background:' + _hl + ';color:#000;}'
        )

        self._batch_edit = NukomfyLineEdit('1')
        self._batch_edit.setValidator(QtGui.QIntValidator(1, 100))
        self._batch_edit.setFixedSize(26, 18)
        self._batch_edit.setAlignment(QtCore.Qt.AlignCenter)
        # No explicit font-size -> inherits the app default, keeping the
        # field visually consistent with surrounding widgets.
        self._batch_edit.setStyleSheet(
            'QLineEdit{background:#252525;color:#ccc;border:1px solid #444;'
            'border-radius:3px;}'
            'QLineEdit:disabled{background:#1e1e1e;color:#555;'
            'border:1px solid #333;}')
        self._batch_edit.setToolTip(
            'Number of jobs to submit. Each job uses a\n'
            'different output frame number.\n\n'
            'Useful with a random seed to generate multiple variants.\n\n'
            'When greater than 1, the input frame range is locked.')
        self._batch_edit.editingFinished.connect(self._on_batch_changed)
        batch_outer.addWidget(self._batch_edit)

        # Stacked up/down buttons (total height = edit height)
        arrows = QtWidgets.QWidget()
        arrows.setFixedSize(14, 18)
        arrows_lay = QtWidgets.QVBoxLayout(arrows)
        arrows_lay.setContentsMargins(0, 0, 0, 0)
        arrows_lay.setSpacing(1)

        self._batch_up = QtWidgets.QPushButton(ARROW_UPWARD)
        self._batch_up.setFont(icon_font(8))
        self._batch_up.setFixedSize(14, 8)
        self._batch_up.setStyleSheet(
            _BATCH_BTN + 'QPushButton{border-radius:2px 2px 0 0;}')
        self._batch_up.clicked.connect(self._batch_increment)

        self._batch_down = QtWidgets.QPushButton(ARROW_DOWNWARD)
        self._batch_down.setFont(icon_font(8))
        self._batch_down.setFixedSize(14, 8)
        self._batch_down.setStyleSheet(
            _BATCH_BTN + 'QPushButton{border-radius:0 0 2px 2px;}')
        self._batch_down.clicked.connect(self._batch_decrement)

        arrows_lay.addWidget(self._batch_up)
        arrows_lay.addWidget(self._batch_down)
        batch_outer.addWidget(arrows)
        top_row.addWidget(batch_w)
        opts_lay.addLayout(top_row)

        # Frame range columns: non-interactive, auto-fit to content and
        # header. Min width ensures the header ("Start Frame") fits
        # comfortably when values are short; column grows to accommodate
        # arbitrary digit counts the user may enter.
        _FR_COL_MIN = 95
        _FR_ROW_H = 26

        def _fit_io_table_height(tbl, n_rows):
            h = (tbl.horizontalHeader().sizeHint().height()
                 + _FR_ROW_H * max(n_rows, 1)
                 + 2 * tbl.frameWidth())
            tbl.setFixedHeight(h)

        # Input frame ranges table - only for inputs flagged 'range'
        if self._range_input_params:
            opts_lay.addWidget(QtWidgets.QLabel('Input Frame Ranges:'))
            self._input_table = QtWidgets.QTableWidget(0, 3)
            self._input_table.setHorizontalHeaderLabels(
                ['Input', 'First', 'Last'])
            ih = self._input_table.horizontalHeader()
            ih.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
            ih.setSectionResizeMode(
                1, QtWidgets.QHeaderView.ResizeToContents)
            ih.setSectionResizeMode(
                2, QtWidgets.QHeaderView.ResizeToContents)
            ih.setMinimumSectionSize(_FR_COL_MIN)
            self._input_table.verticalHeader().setVisible(False)
            self._input_table.verticalHeader().setDefaultSectionSize(_FR_ROW_H)
            self._input_table.setAlternatingRowColors(True)
            self._input_table.setStyleSheet(TABLE_STYLE)
            _int_delegate_i = _IntEditDelegate(self._input_table)
            self._input_table.setItemDelegateForColumn(1, _int_delegate_i)
            self._input_table.setItemDelegateForColumn(2, _int_delegate_i)
            self._input_table.itemChanged.connect(
                self._on_input_range_changed)
            _fit_io_table_height(self._input_table,
                                 len(self._range_input_params))
            opts_lay.addWidget(self._input_table)
        else:
            self._input_table = None

        # Output start frames table - one row per enabled output
        if self._output_params:
            opts_lay.addWidget(QtWidgets.QLabel('Output Start Frames:'))
            self._output_table = QtWidgets.QTableWidget(0, 2)
            self._output_table.setHorizontalHeaderLabels(
                ['Output', 'Start Frame'])
            oh = self._output_table.horizontalHeader()
            oh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
            oh.setSectionResizeMode(
                1, QtWidgets.QHeaderView.ResizeToContents)
            oh.setMinimumSectionSize(_FR_COL_MIN)
            self._output_table.verticalHeader().setVisible(False)
            self._output_table.verticalHeader().setDefaultSectionSize(_FR_ROW_H)
            self._output_table.setAlternatingRowColors(True)
            self._output_table.setStyleSheet(TABLE_STYLE)
            self._output_table.setItemDelegateForColumn(
                1, _IntEditDelegate(self._output_table))
            _fit_io_table_height(self._output_table,
                                 len(self._output_params))
            opts_lay.addWidget(self._output_table)
        else:
            self._output_table = None

        # Push tables to top; remaining space goes to a stretch inside
        # the groupbox so the groupbox extends down to the buttons with
        # no visible gap.
        opts_lay.addStretch(1)
        opts_grp.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                               QtWidgets.QSizePolicy.Expanding)
        bottom_lay.addWidget(opts_grp)

        # ── Bottom buttons ────────────────────────────────────────
        btn_lay = QtWidgets.QHBoxLayout()
        btn_lay.addStretch()
        self._submit_btn = QtWidgets.QPushButton('Submit')
        self._submit_btn.setIcon(material_icon(PUBLISH, '#fff', 14))
        self._submit_btn.setFixedHeight(28)
        self._submit_btn.setStyleSheet(
            'QPushButton{font-weight:bold;}'
            'QPushButton:disabled{color:#555;}')
        self._submit_btn.clicked.connect(self._on_submit)
        btn_lay.addWidget(self._submit_btn)
        self._cancel_btn = QtWidgets.QPushButton('Cancel')
        self._cancel_btn.setFixedHeight(28)
        self._cancel_btn.clicked.connect(self.reject)
        btn_lay.addWidget(self._cancel_btn)
        bottom_lay.addLayout(btn_lay)

        self._splitter.addWidget(top_pane)
        self._splitter.addWidget(bottom_pane)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        # Default 60/40 (Machines top gets more space); ui_state restore
        # happens after _build in __init__.
        self._splitter.setSizes([360, 240])
        root.addWidget(self._splitter)

    # ------------------------------------------------------------------
    # Machine table
    # ------------------------------------------------------------------
    def _populate_machines(self):
        # The "Show offline in Submit" toggle is not present here -
        # offline machines are always visible
        # (sorted to the bottom) like in Render Manager. The only way to
        # hide a machine from Submit is to disable it in Settings ->
        # Machines (then it falls out of `enabled_machines` upstream).
        # Sort: cached-online first, cached-offline at the bottom.
        # Unknown (no cached info) sorts as online so fresh-boot order
        # is natural.
        machines = sorted(
            machine_manager.enabled_machines,
            key=lambda m: (m.info or {}).get('online') is False)

        self._machine_table.setRowCount(0)
        # Release the previous button group when _populate_machines is
        # called more than once per session (currently only via the
        # post-check re-sort in _resort_machines_table). Without
        # deleteLater, the old group lingers as a child of the dialog
        # until the dialog closes.
        prev_group = getattr(self, '_sel_group', None)
        if prev_group is not None:
            prev_group.deleteLater()
        self._sel_group = QtWidgets.QButtonGroup(self)
        self._sel_group.buttonClicked.connect(self._on_radio_clicked)

        for m in machines:
            row = self._machine_table.rowCount()
            self._machine_table.insertRow(row)
            self._machine_table.setRowHeight(row, 26)

            # Radio button for selection (wrapped in _SelectorCell which
            # paints a green accent bar on its left edge when selected).
            rb = QtWidgets.QRadioButton()
            rb.setEnabled(False)  # enabled when status is known
            rb_wrap = _SelectorCell()
            rb_lay = QtWidgets.QHBoxLayout(rb_wrap)
            rb_lay.setContentsMargins(6, 0, 0, 0)
            rb_lay.setAlignment(QtCore.Qt.AlignCenter)
            rb_lay.addWidget(rb)
            self._sel_group.addButton(rb, row)
            self._machine_table.setCellWidget(row, _M_COL_SEL, rb_wrap)

            # Status cell (icon + label, placeholder until first check)
            self._machine_table.setCellWidget(
                row, _M_COL_STATUS,
                _make_status_cell('', '-', '#888'))

            # Name (store machine id)
            name_item = QtWidgets.QTableWidgetItem(m.name)
            name_item.setData(QtCore.Qt.UserRole, m.id)
            self._machine_table.setItem(row, _M_COL_NAME, name_item)

            # Queue
            q_item = QtWidgets.QTableWidgetItem('-')
            q_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self._machine_table.setItem(row, _M_COL_QUEUE, q_item)

            # GPU / VRAM / RAM - show cached info if available
            info = m.info or {}
            for col, key in ((_M_COL_COMFY, 'comfyui_ver'),
                             (_M_COL_OS, 'os'),
                             (_M_COL_GPU, 'gpu'),
                             (_M_COL_VRAM, 'vram_total'),
                             (_M_COL_RAM, 'ram_total')):
                it = QtWidgets.QTableWidgetItem(info.get(key, '-'))
                it.setTextAlignment(QtCore.Qt.AlignCenter)
                self._machine_table.setItem(row, col, it)

            # Preserve greyed text for machines last seen offline so the
            # row doesn't flash to white during the refresh window.
            if info.get('online') is False:
                brush = QtGui.QBrush(QtGui.QColor('#606060'))
                for col in (_M_COL_NAME, _M_COL_COMFY, _M_COL_OS,
                            _M_COL_QUEUE,
                            _M_COL_GPU, _M_COL_VRAM, _M_COL_RAM):
                    it = self._machine_table.item(row, col)
                    if it:
                        it.setForeground(brush)

    def _row_for_machine(self, machine_id):
        for row in range(self._machine_table.rowCount()):
            item = self._machine_table.item(row, _M_COL_NAME)
            if item and item.data(QtCore.Qt.UserRole) == machine_id:
                return row
        return -1

    def _machine_id_at_row(self, row):
        """Return the machine id stashed on column NAME's UserRole, or None."""
        item = self._machine_table.item(row, _M_COL_NAME)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _on_machine_clicked(self, row, _col):
        """Select the radio button when any cell in a row is clicked."""
        sel_wrap = self._machine_table.cellWidget(row, _M_COL_SEL)
        if sel_wrap:
            rb = sel_wrap.findChild(QtWidgets.QRadioButton)
            if rb and rb.isEnabled():
                rb.setChecked(True)
                # User-initiated row click counts as manual.
                self._manual_selected = True
                self._stop_auto_timer()
                self._update_row_highlight()

    def _on_radio_clicked(self, _btn):
        """User clicked a radio button directly - lock selection manually."""
        if self._auto_applying:
            return
        self._manual_selected = True
        self._stop_auto_timer()
        self._update_row_highlight()

    def _refresh_machines(self, *_args):
        # Accepts a `checked: bool` from QPushButton.clicked without
        # treating it as a meaningful argument (Qt always passes False
        # for non-checkable buttons).
        self._status_worker = stop_worker(self._status_worker)

        # Build the table once, then update rows in-place while the check
        # is running. Previous status stays visible (no placeholder, no
        # 'Updating...' flash on offline rows), Queue/HW columns don't
        # blink to '-'. Re-sort is deferred to _on_refresh_done so the
        # rows don't dance during the check window; at that point a single
        # online-first reorder happens (skipped if order is already
        # correct - typical on subsequent refreshes).
        if not self._machines_populated:
            self._populate_machines()
            self._row_status = {}
            self._row_availability = {}
            self._row_pending = {}
            self._machines_populated = True

        machines = machine_manager.enabled_machines
        if not machines:
            return

        self._refresh_btn.setText('Updating\u2026')
        self._refresh_btn.setEnabled(False)
        self._refresh_cycle.begin()

        # Start/restart auto-select cycle unless user has locked it.
        if not self._manual_selected:
            self._start_auto_select_cycle()

        # Explicit user click ("Update All") means "I want the fresh
        # state, ignore the 60s availability cache". Invalidate so the
        # next manager_client.availability() call hits the wire.
        try:
            from Nukomfy.client import manager_client
            for _m in machines:
                manager_client.clear_cache(_m.url)
        except Exception:
            pass

        self._status_worker = UnifiedFetchWorker(machines, check_queue)
        self._status_worker.result.connect(self._on_status_result)
        self._status_worker.finished.connect(self._on_refresh_done)
        self._status_worker.start()

    def _on_refresh_done(self):
        self._status_worker = None
        # All status results are in - re-sort rows so online machines
        # bubble to the top and offline sink to the bottom. Deferred to
        # here (not live per result) to avoid N micro-jumps during the
        # check window. Early-exits when the order is already correct.
        self._resort_machines_table()
        # All checks in - run a final best-effort auto-select so late
        # responders don't delay the pick.
        self._auto_timer_fired = True
        self._try_auto_select()
        # Button text/enabled settle via the refresh cycle (anti-flicker).
        self._refresh_cycle.finish()

    def _on_refresh_ready(self):
        # Refresh cycle settled (fetch done, or the soft deadline elapsed
        # with slow/offline machines still polling in the background).
        # Restore the idle label and ensure enabled. Submit Panel has no
        # auto-refresh countdown, so just the plain label. Defensive - may
        # fire after the panel closed.
        try:
            self._refresh_btn.setText('Update All')
            self._refresh_btn.setIcon(material_icon(REFRESH, '#ccc', 14))
            self._refresh_btn.setEnabled(True)
        except RuntimeError:
            pass

    def _resort_machines_table(self):
        """Reorder rows online-first. No-op if order is already correct."""
        sorted_ids = [
            m.id for m in sorted(
                machine_manager.enabled_machines,
                key=lambda m: (m.info or {}).get('online') is False)]

        current_ids = [self._machine_id_at_row(row)
                       for row in range(self._machine_table.rowCount())]

        if current_ids == sorted_ids:
            return

        selected_row = self._current_selected_row()
        selected_mid = (self._machine_id_at_row(selected_row)
                        if selected_row >= 0 else None)
        prev_manual = self._manual_selected

        per_mid_state = {}
        for row in range(self._machine_table.rowCount()):
            mid = self._machine_id_at_row(row)
            if mid is None:
                continue
            per_mid_state[mid] = (
                self._row_status.get(row, 'offline'),
                self._row_availability.get(row, 'available'),
                self._row_pending.get(row, 0))

        self._machine_table.setUpdatesEnabled(False)
        try:
            self._populate_machines()
            self._row_status = {}
            self._row_availability = {}
            self._row_pending = {}
            for mid, (status, avail, pending) in per_mid_state.items():
                new_row = self._row_for_machine(mid)
                if new_row >= 0:
                    self._apply_row_visuals(new_row, status, avail, pending)
            if selected_mid is not None:
                new_row = self._row_for_machine(selected_mid)
                if new_row >= 0:
                    if prev_manual:
                        sel_wrap = self._machine_table.cellWidget(
                            new_row, _M_COL_SEL)
                        if sel_wrap:
                            rb = sel_wrap.findChild(QtWidgets.QRadioButton)
                            if rb and rb.isEnabled():
                                self._auto_applying = True
                                try:
                                    rb.setChecked(True)
                                finally:
                                    self._auto_applying = False
                        self._manual_selected = True
                        self._machine_table.selectRow(new_row)
                        self._update_row_highlight()
                    else:
                        self._apply_auto_selection(new_row)
        finally:
            self._machine_table.setUpdatesEnabled(True)

    # ------------------------------------------------------------------
    # Auto-select
    # ------------------------------------------------------------------
    def _start_auto_select_cycle(self):
        """Begin a new auto-select window: 1s timer + ongoing status taps."""
        self._stop_auto_timer()
        self._auto_selected = False
        self._auto_timer_fired = False
        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._on_auto_timer_fired)
        self._auto_timer.start(1000)

    def _stop_auto_timer(self):
        if self._auto_timer is not None:
            try:
                self._auto_timer.stop()
            except Exception:
                pass
        self._auto_timer = None
        self._auto_timer_fired = False

    def _on_auto_timer_fired(self):
        self._auto_timer_fired = True
        self._try_auto_select()

    def _try_auto_select(self):
        """Evaluate auto-select given the current per-row statuses.

        Priority: first idle machine in list order. If the timer has
        already fired and no idle is known yet, fall back to the first
        online machine (rendering/busy/online) in list order. A manual
        selection always wins and short-circuits this logic.
        """
        if self._manual_selected:
            return

        # Pass 1 - find first idle (skip Unavailable: cooperative lock).
        idle_row = -1
        for row in range(self._machine_table.rowCount()):
            if self._row_status.get(row) != 'idle':
                continue
            if self._row_availability.get(row) == 'unavailable':
                continue
            idle_row = row
            break

        if idle_row >= 0:
            if self._current_selected_row() != idle_row:
                self._apply_auto_selection(idle_row)
            self._auto_selected = True
            return

        # Pass 2 - post-timeout only: first online-of-any-kind (still skip
        # Unavailable so the soft-lock holds even in the fallback path).
        if not self._auto_timer_fired:
            return
        for row in range(self._machine_table.rowCount()):
            st = self._row_status.get(row)
            if st not in ('rendering', 'busy', 'online'):
                continue
            if self._row_availability.get(row) == 'unavailable':
                continue
            if self._current_selected_row() != row:
                self._apply_auto_selection(row)
            self._auto_selected = True
            return

    def _apply_auto_selection(self, row):
        """Auto-select sets ONLY the radio button. No Qt row highlight
        (`selectRow`) and no green bar (`_SelectorCell`) - those appear
        only when the user explicitly clicks the row
        (-> `_manual_selected=True`)."""
        sel_wrap = self._machine_table.cellWidget(row, _M_COL_SEL)
        if not sel_wrap:
            return
        rb = sel_wrap.findChild(QtWidgets.QRadioButton)
        if not rb:
            return
        rb.setEnabled(True)
        self._auto_applying = True
        try:
            rb.setChecked(True)
        finally:
            self._auto_applying = False
        # Clear any stale Qt row selection inherited from a previous state.
        self._machine_table.clearSelection()
        self._update_row_highlight()

    def _current_selected_row(self):
        btn = self._sel_group.checkedButton() if self._sel_group else None
        if btn is None:
            return -1
        return self._sel_group.id(btn)

    def _update_row_highlight(self):
        """Paint a green accent bar on the selector of the selected
        row. No Qt row highlight on auto-select: that is handled only
        in the click handlers
        (`_on_machine_clicked`/`_on_radio_clicked`)."""
        sel_row = self._current_selected_row()
        for row in range(self._machine_table.rowCount()):
            cell = self._machine_table.cellWidget(row, _M_COL_SEL)
            if isinstance(cell, _SelectorCell):
                cell.set_selected(row == sel_row)

    def _apply_row_visuals(self, row, status, avail, pending):
        """Repaint a row's cells from (status, avail, pending). No side effects.

        Populates _row_status/_row_availability/_row_pending, status cell +
        tooltip, queue text, radio enabled-state, HW columns (from cached
        m.info), and the dim foreground brush for offline/unavailable rows.
        Used both by _on_status_result (live check) and _resort_machines_table
        (replay after sort-rebuild).
        """
        self._row_status[row] = status
        self._row_availability[row] = avail
        self._row_pending[row] = pending

        # Treat Unavailable as a hard-block identical to offline at every
        # layer (selector, queue cell, dim brush) - popup warning would
        # only catch click-Submit.
        not_submittable = (status == 'offline') or (avail == 'unavailable')

        label, color, icon_char = render_machine_status(status, avail)
        status_cell = self._machine_table.cellWidget(row, _M_COL_STATUS)
        _update_status_cell(status_cell, icon_char, label, color)
        if status_cell is not None:
            if avail == 'unavailable':
                status_cell.setToolTip(
                    'Marked Unavailable by the machine owner.\n'
                    'New submissions are blocked until the flag is cleared.')
            else:
                status_cell.setToolTip('')

        q_item = self._machine_table.item(row, _M_COL_QUEUE)
        if q_item:
            q_item.setText(
                str(pending) if status != 'offline' else '-')

        sel_wrap = self._machine_table.cellWidget(row, _M_COL_SEL)
        if sel_wrap:
            rb = sel_wrap.findChild(QtWidgets.QRadioButton)
            if rb:
                rb.setEnabled(not not_submittable)

        m = machine_manager.get(self._machine_id_at_row(row))
        hw = (m.info if m else None) or {}
        for col, key in ((_M_COL_COMFY, 'comfyui_ver'),
                         (_M_COL_OS, 'os'),
                         (_M_COL_GPU, 'gpu'),
                         (_M_COL_VRAM, 'vram_total'),
                         (_M_COL_RAM, 'ram_total')):
            it = self._machine_table.item(row, col)
            if it:
                # OS empty-string falls back to '-' (a partial check_machine
                # response can return ''); other keys use the default-only
                # path because their semantics treat missing == empty.
                val = (hw.get(key) or '-') if key == 'os' else hw.get(key, '-')
                it.setText(val)

        brush = (QtGui.QBrush(QtGui.QColor('#606060'))
                 if not_submittable else QtGui.QBrush())
        for col in (_M_COL_NAME, _M_COL_COMFY, _M_COL_OS, _M_COL_QUEUE,
                    _M_COL_GPU, _M_COL_VRAM, _M_COL_RAM):
            it = self._machine_table.item(row, col)
            if it:
                it.setForeground(brush)

    def _on_machine_info_changed(self, machine_id):
        """Repaint just the hardware/version cells of a row when the shared
        MachineInfoService refreshes that machine's /system_stats snapshot.
        Status / queue / availability stay owned by the periodic worker."""
        try:
            row = self._row_for_machine(machine_id)
            if row < 0:
                return
            m = machine_manager.get(machine_id)
            hw = (m.info if m else None) or {}
            for col, key in ((_M_COL_COMFY, 'comfyui_ver'),
                             (_M_COL_OS, 'os'),
                             (_M_COL_GPU, 'gpu'),
                             (_M_COL_VRAM, 'vram_total'),
                             (_M_COL_RAM, 'ram_total')):
                it = self._machine_table.item(row, col)
                if it:
                    val = (hw.get(key) or '-') if key == 'os' else hw.get(key, '-')
                    it.setText(val)
        except RuntimeError:
            pass  # table/dialog torn down between emit and slot

    def _on_status_result(self, machine_id, info):
        status = info.get('status', 'offline')
        # Update cached online flag BEFORE the row check. Without this,
        # a machine cached as offline (filtered out of the table by
        # _populate_machines) would have its check result discarded,
        # keeping `m.info[online]` stuck at False forever. The cache
        # flips to True even when the row isn't in the current table -
        # the next populate / refresh can then include the machine.
        m = machine_manager.get(machine_id)
        if m is not None:
            m.info = dict(m.info or {})
            m.info['online'] = status != 'offline'

        row = self._row_for_machine(machine_id)
        if row < 0:
            return

        # Availability is cached on Machine.info via check_machine. Honour
        # the combined (status, availability) tuple so Render Manager and
        # Submit Panel display the same suffix / replacement.
        avail = (m.info or {}).get('availability') if m else None
        avail = avail or 'available'
        pending = info.get('pending', 0)
        self._apply_row_visuals(row, status, avail, pending)

        # If the currently-selected machine just went offline (or was just
        # marked Unavailable), drop the selection (Qt does not auto-uncheck
        # on setEnabled(False)) and let auto-select fall back to the first
        # idle, then to any other online + available machine. Manual lock is
        # intentionally broken here: a locked non-submittable machine cannot
        # be submitted to, so the lock is no longer meaningful.
        not_submittable = (status == 'offline') or (avail == 'unavailable')
        if not_submittable and self._current_selected_row() == row:
            sel_wrap = self._machine_table.cellWidget(row, _M_COL_SEL)
            rb = sel_wrap.findChild(QtWidgets.QRadioButton) if sel_wrap else None
            if rb is not None and self._sel_group is not None:
                self._sel_group.setExclusive(False)
                rb.setChecked(False)
                self._sel_group.setExclusive(True)
            self._manual_selected = False
            self._auto_selected = False
            self._machine_table.clearSelection()
            self._update_row_highlight()
            # Skip the 1s timer wait: the previous pick is gone, any other
            # online row is a valid fallback right now.
            self._auto_timer_fired = True
            self._try_auto_select()

        # Once we've seen a first response, shorten the auto-select
        # fallback. The full 1s window is needed only to give all machines
        # a chance to reply; if any did and none reported idle, the timer
        # would normally still wait 1s before picking a fallback even
        # though no additional information could change the decision.
        # 400ms is enough to let near-simultaneous siblings arrive (covers
        # LAN local + multi-hop with margin) without inflicting a 1s wait
        # when one machine is in HTTP timeout.
        if (self._auto_timer is not None
                and not self._auto_timer_fired
                and not self._auto_selected):
            remaining = self._auto_timer.remainingTime()
            if remaining > 400:
                self._auto_timer.start(400)
        # Try auto-select after each status arrival.
        self._try_auto_select()

    def _reflect_preflight_status(self, machine_id, reach):
        """Repaint a machine's status row from a synchronous pre-flight
        check_machine() result, so a host discovered offline (or freshly
        marked Unavailable) at Submit time stops showing a stale status.

        Display-only: the selection / auto-select gate is intentionally
        left untouched (the submit is aborted by the caller regardless).
        Persists online / availability on Machine.info for every alias
        sharing this URL (same server = same connectivity), mirroring
        refresh_availability, so a sibling row keeps no stale status and
        cannot bias auto-select toward the wrong alias."""
        m = machine_manager.get(machine_id)
        online = bool(reach.get('online'))
        if m is not None:
            targets = [mm for mm in machine_manager.machines if mm.url == m.url]
            if m not in targets:
                targets.append(m)
            for mm in targets:
                mm.info = dict(mm.info or {})
                mm.info['online'] = online
                if online:
                    mm.info['availability'] = reach.get('availability') or 'available'
        row = self._row_for_machine(machine_id)
        if row < 0:
            return
        if online:
            # Keep the last known queue status; only the Availability flag
            # could have changed. A stale 'offline' falls back to 'idle'
            # since the probe just confirmed the host is reachable.
            status = self._row_status.get(row, 'idle')
            if status == 'offline':
                status = 'idle'
        else:
            status = 'offline'
        avail = (m.info or {}).get('availability') if m else None
        avail = avail or 'available'
        self._apply_row_visuals(
            row, status, avail, self._row_pending.get(row, 0))

    # ------------------------------------------------------------------
    # Input frame ranges
    # ------------------------------------------------------------------
    def _populate_inputs(self):
        if not self._input_table or not self._range_input_params:
            return

        self._input_table.blockSignals(True)
        try:
            self._input_table.setRowCount(0)
            for param in self._range_input_params:
                row = self._input_table.rowCount()
                self._input_table.insertRow(row)
                self._input_table.setRowHeight(row, 26)

                name = param.get('label', param.get('name', 'input'))
                name_item = QtWidgets.QTableWidgetItem(name)
                name_item.setFlags(name_item.flags() & ~QtCore.Qt.ItemIsEditable)
                self._input_table.setItem(row, 0, name_item)

                first, last = self._default_input_range(row)

                first_item = QtWidgets.QTableWidgetItem(str(first))
                first_item.setFlags(first_item.flags() | QtCore.Qt.ItemIsEditable)
                self._input_table.setItem(row, 1, first_item)

                last_item = QtWidgets.QTableWidgetItem(str(last))
                last_item.setFlags(last_item.flags() | QtCore.Qt.ItemIsEditable)
                self._input_table.setItem(row, 2, last_item)
        finally:
            self._input_table.blockSignals(False)

    def _populate_outputs(self):
        if not self._output_table:
            return
        default = self._compute_output_start_default()
        self._output_table.blockSignals(True)
        try:
            self._output_table.setRowCount(0)
            for i, param in enumerate(self._output_params):
                row = self._output_table.rowCount()
                self._output_table.insertRow(row)
                self._output_table.setRowHeight(row, 26)

                name = param.get('label', param.get('name', 'output'))
                name_item = QtWidgets.QTableWidgetItem(name)
                name_item.setFlags(name_item.flags() & ~QtCore.Qt.ItemIsEditable)
                self._output_table.setItem(row, 0, name_item)

                sf_item = QtWidgets.QTableWidgetItem(str(default))
                sf_item.setFlags(sf_item.flags() | QtCore.Qt.ItemIsEditable)
                self._output_table.setItem(row, 1, sf_item)
                self._output_dirty[i] = False
        finally:
            self._output_table.blockSignals(False)
        self._output_table.itemChanged.connect(self._on_output_item_changed)

    def _compute_output_start_default(self):
        import nuke  # type: ignore
        ranges = self._read_input_ranges()
        if ranges:
            return min(fr[0] for _, fr in ranges)
        return int(nuke.root().firstFrame())

    def _on_input_range_changed(self, item):
        """Refill a First/Last cell that does not hold an integer with its
        live default, then re-link non-dirty output Start Frames."""
        if self._input_updating or self._output_updating:
            return
        # QIntValidator classifies '' and a lone '-' as Intermediate, so Qt
        # commits them; snap a non-integer cell back to the row default
        # instead of letting _read_input_ranges silently read it as frame 1.
        if item is not None and item.column() in (1, 2):
            try:
                int(item.text())
            except ValueError:
                first, last = self._default_input_range(item.row())
                self._input_updating = True
                try:
                    item.setText(str(first if item.column() == 1 else last))
                finally:
                    self._input_updating = False
        self._relink_outputs_to_input_defaults()

    def _on_output_item_changed(self, item):
        if self._output_updating:
            return
        if item.column() != 1:
            return
        row = item.row()
        text = item.text().strip()
        if not text:
            # Empty -> re-attach to live default
            self._output_dirty[row] = False
            default = self._compute_output_start_default()
            self._output_updating = True
            try:
                item.setText(str(default))
            finally:
                self._output_updating = False
        else:
            self._output_dirty[row] = True

    def _relink_outputs_to_input_defaults(self):
        if not self._output_table:
            return
        default = self._compute_output_start_default()
        self._output_updating = True
        try:
            for i in range(self._output_table.rowCount()):
                if self._output_dirty.get(i):
                    continue
                it = self._output_table.item(i, 1)
                if it and it.text() != str(default):
                    it.setText(str(default))
        finally:
            self._output_updating = False

    def _read_input_ranges(self):
        """Read (param, (first, last)) tuples from the input table (range-only)."""
        ranges = []
        if not self._input_table:
            return ranges
        for i in range(self._input_table.rowCount()):
            param = self._range_input_params[i]
            try:
                first = int(self._input_table.item(i, 1).text())
                last = int(self._input_table.item(i, 2).text())
            except (ValueError, AttributeError):
                first = last = 1
            ranges.append((param, (first, last)))
        return ranges

    def _default_input_range(self, row):
        """Live default (first, last) for input `row`: the upstream clip's
        frame range, else the script root range. The gizmo input slot is
        indexed over ALL input params, so map the range-table row back to
        the original param index."""
        import nuke  # type: ignore
        param = self._range_input_params[row]
        try:
            gizmo_idx = self._input_params.index(param)
        except ValueError:
            gizmo_idx = row
        upstream = self._gizmo.input(gizmo_idx)
        if upstream:
            try:
                return upstream.firstFrame(), upstream.lastFrame()
            except Exception:
                pass
        return nuke.root().firstFrame(), nuke.root().lastFrame()

    def _read_output_start_frames(self):
        """Return list of start_frame ints, one per output param."""
        result = []
        if not self._output_table:
            return result
        for i in range(self._output_table.rowCount()):
            try:
                sf = int(self._output_table.item(i, 1).text())
            except (ValueError, AttributeError):
                sf = 1
            result.append(sf)
        return result

    def _build_full_input_ranges(self, input_params):
        """Return one (param, (first, last)) entry per enabled input param.

        Range inputs read from the table; single inputs use nuke.frame().
        Order matches input_params (gizmo-slot order).
        """
        import nuke  # type: ignore
        table_ranges = self._read_input_ranges()
        table_by_param = {id(p): fr for p, fr in table_ranges}
        current = int(nuke.frame())
        full = []
        for p in input_params:
            if p.get('io_mode') == 'Sequence':
                fr = table_by_param.get(id(p), (current, current))
            else:
                fr = (current, current)
            full.append((p, fr))
        return full

    def _batch_value(self):
        try:
            return max(1, min(100, int(self._batch_edit.text())))
        except ValueError:
            return 1

    def _batch_increment(self):
        v = min(100, self._batch_value() + 1)
        self._batch_edit.setText(str(v))
        self._on_batch_changed()

    def _batch_decrement(self):
        v = max(1, self._batch_value() - 1)
        self._batch_edit.setText(str(v))
        self._on_batch_changed()

    def _on_batch_changed(self):
        """When batch > 1, lock input frame range to single-frame."""
        v = self._batch_value()
        self._batch_edit.setText(str(v))
        self._update_frame_range_availability()

    def _update_frame_range_availability(self):
        """Enable/disable Batch Count and lock input ranges as needed."""
        # Batch is allowed only when every output is 'single'. A 'range'
        # output would overlap its own writes across batch iterations.
        if not self._all_outputs_single:
            self._batch_edit.blockSignals(True)
            self._batch_edit.setText('1')
            self._batch_edit.blockSignals(False)
            self._batch_edit.setEnabled(False)
            self._batch_up.setEnabled(False)
            self._batch_down.setEnabled(False)
            self._batch_edit.setToolTip(
                "Batch Count is available only when every output is set "
                "to Single.")
        else:
            self._batch_edit.setEnabled(True)
            self._batch_up.setEnabled(True)
            self._batch_down.setEnabled(True)
            self._batch_edit.setToolTip(
                'Number of jobs to submit. Each job uses a\n'
                'different output frame number.\n\n'
                'Useful with a random seed to generate multiple variants.\n\n'
                'When greater than 1, the input frame range is locked.')
        if not self._input_table:
            return
        batch = self._batch_value()
        enabled = (batch <= 1)
        for i in range(self._input_table.rowCount()):
            for col in (1, 2):
                item = self._input_table.item(i, col)
                if item:
                    flags = item.flags()
                    if enabled:
                        flags |= QtCore.Qt.ItemIsEditable
                        flags |= QtCore.Qt.ItemIsEnabled
                        item.setForeground(QtGui.QColor('#ccc'))
                    else:
                        flags &= ~QtCore.Qt.ItemIsEditable
                        flags &= ~QtCore.Qt.ItemIsEnabled
                        item.setForeground(QtGui.QColor('#555'))
                    item.setFlags(flags)

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------
    def _selected_machine_id(self):
        """Return the machine id of the selected radio button, or None."""
        btn = self._sel_group.checkedButton()
        if btn is None:
            return None
        row = self._sel_group.id(btn)
        item = self._machine_table.item(row, _M_COL_NAME)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _on_submit(self):
        # Block submit if any enabled I/O param lacks its io_mode setting.
        missing_io_mode = any(
            p.get('role') in ('input', 'output')
            and p.get('enabled', True)
            and not p.get('io_mode')
            for p in self._gizmo_params
        )
        if missing_io_mode:
            _dialogs.warn(
                self, 'Missing Input/Output mode',
                'One or more Input/Output rows have no mode set.\n\n'
                'Open "Edit Workflow", set the Input/Output Mode for each '
                'row, and save again.')
            return
        mid = self._selected_machine_id()
        if mid is None:
            _dialogs.warn(
                self, 'No machine selected', 'Select a machine first.')
            return
        # Race guard: file may have been deleted or the Library root
        # may have gone offline while the panel was open. Re-resolve by
        # UUID against the current Library roots.
        resolved = _resolve_workflow_path(self._workflow_id, self._workflow_path)
        if not resolved:
            _dialogs.warn(
                self, 'Workflow missing',
                _WORKFLOW_MISSING_MSG.format(
                    path=self._workflow_path or self._workflow_id or '<unknown>'))
            return
        self._workflow_path = resolved

        # Workflow integrity check. The gizmo carries a hash of the
        # workflow JSON taken at creation time (cosmetic fields stripped).
        # If the file on disk no longer matches, the TD/owner has likely
        # edited the shared workflow and the gizmo's stored params may not
        # resolve to the same nodes/widgets anymore.
        stored_hash_knob = self._gizmo.knob('_nfy_wf_hash')
        stored_hash = stored_hash_knob.value() if stored_hash_knob else ''
        if stored_hash:
            from Nukomfy.workflows.workflow_loader import workflow_logical_hash
            try:
                current_hash = workflow_logical_hash(resolved)
            except Exception as e:
                # The integrity check is advisory: never let a hash failure
                # block a legitimate submit. Degrade to permissive, but log
                # it so a silently skipped check is visible in the console.
                _log.warning(
                    'Could not verify workflow integrity for %s: %s '
                    '(skipping the modified-workflow check)', resolved, e)
                current_hash = ''
            if current_hash and current_hash != stored_hash:
                btn = _dialogs.warn(
                    self, 'Workflow modified',
                    'The workflow file on disk has changed since this '
                    'gizmo was created.\n\n'
                    'Knob values you set may not be applied correctly '
                    'because exposed parameters in the workflow may have '
                    'been renamed, removed, or replaced.\n\n'
                    'Recommended: recreate the gizmo from the Library to '
                    're-sync.',
                    QtWidgets.QMessageBox.Cancel
                    | QtWidgets.QMessageBox.Ignore,
                    QtWidgets.QMessageBox.Cancel)
                if btn == QtWidgets.QMessageBox.Cancel:
                    return

        # Preflight: first confirm the machine is actually reachable
        # (otherwise fetch_object_info would return empty info
        # for every type and we'd falsely flag everything as "missing"),
        # then ask it which node types it supports.
        m = machine_manager.get(mid)
        if m is not None:
            from Nukomfy.client.machines import check_machine, apply_machine_info
            # Invalidate the manager availability cache so the pre-flight
            # probe sees the live Availability flag - without this a
            # freshly toggled Unavailable stays masked by the 60s ping
            # cache (mirror of the reboot path in machines_panel).
            try:
                from Nukomfy.client import manager_client
                manager_client.clear_cache(m.url)
            except Exception:
                pass
            reach = check_machine(m)
            if not reach.get('online'):
                # Reflect the freshly probed offline state in the machine
                # row before the dialog - otherwise it keeps showing a
                # stale Idle status until the next manual refresh.
                self._reflect_preflight_status(mid, reach)
                from Nukomfy.utils.url_obfuscation import scrub_url_in_text
                ref = m.name if m.hidden_url else '{} - {}'.format(m.name, m.url)
                _dialogs.warn(
                    self, 'Machine unreachable',
                    'Could not reach the selected ComfyUI server '
                    '({ref}).\n\n'
                    'Details: {err}\n\n'
                    'Check that the machine is powered on, that ComfyUI '
                    'is running, and that the URL is correct. Use the '
                    'Update button in the machine list to retry.'.format(
                        ref=ref,
                        err=scrub_url_in_text(
                            str(reach.get('error', 'no response')), m.url)))
                return
            # Cooperative availability gate. The machine owner has marked
            # it Unavailable via the Nukomfy Manager sidebar - treat as a
            # hard block on this side (mirror of the offline path above).
            # Native ComfyUI endpoints stay open: the gate is enforced
            # UI-side only.
            if reach.get('availability') == 'unavailable':
                # Reflect the freshly probed Unavailable flag in the row
                # before the dialog (same stale-status fix as the offline
                # branch above).
                self._reflect_preflight_status(mid, reach)
                mbox = _dialogs.message_box(self)
                mbox.setIcon(QtWidgets.QMessageBox.Warning)
                mbox.setWindowTitle('Machine unavailable')
                mbox.setTextFormat(QtCore.Qt.RichText)
                mbox.setText(
                    'Machine <b>{name}</b> is marked <b>Unavailable</b>.'
                    '<br><br>'
                    'Someone may be working on it. Wait for the owner '
                    'to set it back to Available before submitting.'
                    .format(name=m.name))
                mbox.setStandardButtons(QtWidgets.QMessageBox.Ok)
                mbox.exec_()
                return
            # The preflight probe just returned fresh hardware/version info
            # (os, gpu, ...). Apply it now so the submit pipeline - notably
            # cross-OS path substitution, which reads machine.info['os'] - uses
            # live data even when the per-session info sweep has not reached
            # this machine yet (or it was offline during that sweep).
            apply_machine_info(m, reach)
            try:
                info_cache, missing = self._preflight_workflow_nodes(m)
            except Exception as e:
                # This fetch can fail because the host dropped after the
                # online probe above, not only for a real compatibility
                # issue. Re-probe and reflect the verified state: offline ->
                # the row goes offline; still online -> it stays online (the
                # error was workflow-side, not the host).
                self._reflect_preflight_status(mid, check_machine(m))
                from Nukomfy.utils.url_obfuscation import scrub_url_in_text
                _dialogs.warn(
                    self, 'Compatibility check failed',
                    'Could not verify workflow compatibility with the '
                    'selected machine:\n\n{}'.format(
                        scrub_url_in_text(str(e), m.url)))
                return
            if missing:
                _dialogs.warn(
                    self, 'Missing nodes',
                    'The selected machine ({name}) does not recognize '
                    'the following node types:\n\n  - {nodes}\n\n'
                    'These are custom nodes that need to be installed on '
                    'that ComfyUI server, or select a different '
                    'machine that has them.'.format(
                        name=m.name,
                        nodes='\n  - '.join(sorted(missing))))
                return
            # Cache for _run_submit_pipeline so it does not refetch.
            self._preflight_info_cache = info_cache
        self._submit_to(mid)

    def _preflight_workflow_nodes(self, m):
        """Return (info_cache, missing_types) for the selected machine.

        Two sets are collected:
        - `fetch_types`: every node type referenced by the workflow -
          matches the superset that `_run_submit_pipeline` will fetch, so
          the returned `info_cache` can be reused without a second
          roundtrip.
        - `check_types`: `fetch_types` minus the ones `ui_to_api` would
          skip anyway (virtual, subgraph wrappers, muted/bypassed). The
          "missing" set is computed only on `check_types` to avoid false
          positives on nodes the server isn't expected to know.
        """
        from Nukomfy.workflows.workflow_converter import (fetch_object_info,
                                        _VIRTUAL_NODE_TYPES,
                                        _SKIPPED_NODE_MODES)

        fetch_types = set()
        check_types = set()
        with open(self._workflow_path, 'r', encoding='utf-8') as f:
            wf = json.load(f)
        subgraph_uuids = {
            sg.get('id')
            for sg in wf.get('definitions', {}).get('subgraphs', []) or []
            if sg.get('id')
        }
        for n in wf.get('nodes', []):
            t = n.get('type', '')
            if not t:
                continue
            fetch_types.add(t)
            if t in _VIRTUAL_NODE_TYPES or t in subgraph_uuids:
                continue
            if n.get('mode') in _SKIPPED_NODE_MODES:
                continue
            check_types.add(t)
        for sg in wf.get('definitions', {}).get('subgraphs', []) or []:
            for n in sg.get('nodes', []) or []:
                t = n.get('type', '')
                if not t:
                    continue
                fetch_types.add(t)
                if t in _VIRTUAL_NODE_TYPES or t in subgraph_uuids:
                    continue
                if n.get('mode') in _SKIPPED_NODE_MODES:
                    continue
                check_types.add(t)

        info_cache = fetch_object_info(m.url, fetch_types)
        missing = {t for t in check_types
                   if not isinstance(info_cache.get(t), dict)
                   or not info_cache.get(t)}
        return info_cache, missing

    def _submit_to(self, machine_id):
        m = machine_manager.get(machine_id)
        if not m:
            return

        import socket
        import nuke  # type: ignore
        force = False  # Per-frame mtime/size validation makes a manual force toggle unnecessary
        batch_count = self._batch_value()

        # Block fat-finger submits before the heavier pre-flight checks.
        threshold = settings.batch_warning_threshold
        if threshold > 0 and batch_count >= threshold:
            if not _ask_batch_threshold(self, batch_count, m):
                return

        nk_path = nuke.root().knob('name').value() or 'Untitled'
        node_name = self._gizmo.name()

        input_params = [p for p in self._gizmo_params
                        if p.get('role') == 'input' and p.get('enabled', True)]
        output_params = [p for p in self._gizmo_params
                         if p.get('role') == 'output' and p.get('enabled', True)]

        full_input_ranges = self._build_full_input_ranges(input_params)

        # Every enabled input must be connected before any network probe or
        # destructive output prompt: the input cache write fails on an
        # unconnected input, so surface it up front.
        unconnected = [p for i, (p, _fr) in enumerate(full_input_ranges)
                       if self._gizmo.input(i) is None]
        if unconnected:
            names = [p.get('label') or p.get('name') or 'input'
                     for p in unconnected]
            if len(names) == 1:
                body = ('The input "{}" is not connected to any node.\n\n'
                        'Connect it and submit again.'.format(names[0]))
            else:
                body = ('These inputs are not connected to any node:\n\n'
                        '  - {}\n\n'
                        'Connect them and submit again.'.format(
                            '\n  - '.join(names)))
            _dialogs.warn(self, 'Input not connected', body)
            return

        # Concurrency check: another of my jobs still in queue for this
        # gizmo? A second submit would wipe the Input Cache / output dir
        # out from under the running job.
        my_user = current_user()
        my_host = socket.gethostname()
        in_flight = _find_my_in_flight_for_gizmo(
            m.url, my_user, my_host, nk_path, node_name)

        # Input Cache conflict check. Match on the leaf dirs the submit would
        # actually rewrite (a reuse_full leaf is not a conflict), not just
        # (user, host, nk_file, node_name): after Settings -> Input Cache Path
        # changes, the new submit writes to a disjoint path and cannot corrupt
        # the in-flight job's cache.
        if input_params and in_flight:
            from Nukomfy.data.input_cache_writer import preview_input_cache_dirs
            new_cache_dirs = preview_input_cache_dirs(
                self._gizmo, full_input_ranges, m)
            conflicting = _match_job_to_cache_dir(in_flight, new_cache_dirs)
            if conflicting:
                choice = _ask_input_cache_conflict(self, conflicting, m)
                if choice == 'wait':
                    return
                if choice == 'cancel_inflight':
                    # Synchronous cancel - wait for the server to flush
                    # the running prompt from /api/queue before we
                    # touch the Input Cache dir locally. Closes the race
                    # where SaveImage/VAEDecode still holds files open
                    # and Windows file-lock breaks selective_delete.
                    if not _stop_jobs_and_wait(
                            self, m.url, [conflicting]):
                        return
                    # Re-query so the next collision check reflects post-cancel state.
                    in_flight = _find_my_in_flight_for_gizmo(
                        m.url, my_user, my_host, nk_path, node_name)
                # 'continue' -> proceed as-is (expert opt-out)

        # Per-output dispatch dict. Maps absolute output dir to
        # (action, collisions_set) where action ∈
        #   'silent'             - no collision, submit normally
        #   'delete_and_render'  - pre-delete the colliding files, then
        #                          submit (foreign + non-conflicting Nukomfy
        #                          preserved by selective_delete_dir)
        #   'overwrite'          - no pre-deletion; ComfyUI overwrites the
        #                          colliding files in place on successful
        #                          write (failed render leaves old files
        #                          intact - rollback safe)
        # Populated by the pre-flight dispatcher below; consumed AFTER
        # the input cache write succeeds (sentinel-gated transactional flow).
        deferred_output_actions = {}

        # Pre-flight collision detection. Reads output_starts up here so
        # the dispatcher can enumerate target basenames per output.
        # Multi-output check + gizmo-resident metadata tokens are
        # preserved (
        # honoured by build_output_path).
        # A reversed range (First > Last) produces an empty range() downstream:
        # zero frames cached with no error and a false success dialog. Catch it
        # here and abort before any cache write, output scan, or POST.
        inverted = [(p, first, last)
                    for p, (first, last) in full_input_ranges
                    if p.get('io_mode') == 'Sequence' and first > last]
        if inverted:
            if len(inverted) == 1:
                p, first, last = inverted[0]
                body = (
                    'The input "{}" has its First frame ({}) after its Last '
                    'frame ({}).\n\n'
                    'Fix the frame range and submit again.'.format(
                        p.get('label') or p.get('name') or 'input',
                        first, last))
            else:
                rows = '\n  - '.join(
                    '"{}": First {}, Last {}'.format(
                        p.get('label') or p.get('name') or 'input', first, last)
                    for p, first, last in inverted)
                body = (
                    'These inputs have the First frame after the Last '
                    'frame:\n\n  - {}\n\n'
                    'Fix the frame ranges and submit again.'.format(rows))
            _dialogs.warn(self, 'Invalid frame range', body)
            return
        output_starts = self._read_output_start_frames()

        check_data = _build_all_check_dirs(
            self._gizmo, output_starts, batch_count)
        if check_data:
            # Dedup by directory: multi-output gizmos may resolve to the
            # same dir if the template doesn't include {output_name}.
            # Merge target_basenames + existing + collisions per dir.
            by_dir = {}
            for d in check_data:
                if d['dir'] not in by_dir:
                    agg = dict(d)
                    agg['merged_outputs'] = [d['name']]
                    by_dir[d['dir']] = agg
                else:
                    agg = by_dir[d['dir']]
                    agg['target_basenames'] = (agg['target_basenames']
                                               | d['target_basenames'])
                    agg['existing'] = agg['existing'] | d['existing']
                    agg['collisions'] = agg['collisions'] | d['collisions']
                    agg['merged_outputs'].append(d['name'])

            conflict_dirs = [d for d in by_dir.values() if d['collisions']]

            if conflict_dirs:
                # Output-in-use check first - output folder used by a running job.
                # Match each conflict dir against in-flight jobs; one
                # dialog lists every collision.
                conflicting_jobs = []
                for d in conflict_dirs:
                    m_job = _match_job_to_check_dir(in_flight, d['dir'])
                    if m_job:
                        conflicting_jobs.append((m_job, d['dir']))
                if conflicting_jobs:
                    choice = _ask_output_in_use(
                        self, conflicting_jobs, m)
                    if choice == 'cancel_submit':
                        return
                    # Synchronous cancel - wait for the server to flush
                    # each running prompt before submit proceeds,
                    # so ComfyUI's in-place overwrite of the output dir
                    # doesn't collide with the previous job still
                    # writing the same frames.
                    if not _stop_jobs_and_wait(
                            self, m.url,
                            [j for (j, _cd) in conflicting_jobs]):
                        return
                    # User already consented to overwrite by cancelling
                    # the running jobs - implicit Overwrite (no further
                    # popup, no pre-deletion).
                    for d in conflict_dirs:
                        deferred_output_actions[d['dir']] = (
                            'overwrite', d['collisions'])
                else:
                    # Overwrite popup: 3-way Cancel / Delete and re-render
                    # / Overwrite. Default Cancel (conservative - Delete
                    # is potentially destructive).
                    from Nukomfy.gui._message_dialogs import (
                        prompt_overwrite_choice)
                    summaries = [
                        {
                            'name': ' / '.join(d['merged_outputs']),
                            'dir': d['dir'],
                            'collisions': d['collisions'],
                            'io_mode': d['io_mode'],
                            'start_frame': d['start_frame'],
                        }
                        for d in conflict_dirs
                    ]
                    choice = prompt_overwrite_choice(self, summaries)
                    if choice == 'cancel':
                        return
                    # Apply the chosen action to every conflict dir
                    # (single popup, single decision - applied uniformly
                    # to all colliding outputs).
                    for d in conflict_dirs:
                        deferred_output_actions[d['dir']] = (
                            choice, d['collisions'])

        # Close the submit panel before starting work
        ui_state.save_geometry('submit_panel', self)
        self.hide()
        QtWidgets.QApplication.processEvents()

        # ── 1. Write Input Cache ─────────────────────────────────
        write_results = []
        if input_params and full_input_ranges:
            try:
                write_results = self._write_input_cache(
                    full_input_ranges, force)
            except _UserCancelled:
                _dialogs.inform(
                    None, 'Submit cancelled',
                    'Job not submitted: write cancelled by user.')
                self.accept()
                return
            except Exception as e:
                _dialogs.warn(
                    None, 'Input Cache write failed',
                    'Input Cache write failed:\n{}\n\n'
                    'Existing output frames were left intact.'.format(e))
                self.accept()
                return

        # ── 2. Prepare & submit workflow ─────────────────────────
        # deferred_output_actions carries the per-dir choice from the
        # pre-flight popup ('delete_and_render' or 'overwrite' for
        # dirs with collisions; absent dirs proceed silent). The pipeline
        # consumes the dict, dispatches selective_delete_dir only for
        # 'delete_and_render' entries, and writes ownership sentinels
        # with the new render's patterns for every output dir.
        try:
            self._run_submit_pipeline(
                m, write_results, input_params, output_params,
                full_input_ranges, output_starts, batch_count,
                nk_path, node_name,
                deferred_output_actions=deferred_output_actions)
        except _SilentAbort:
            # A lower-level helper already showed a detailed dialog -
            # don't stack a second generic "Submit failed" on top.
            self.accept()
            return
        except Exception as e:
            from Nukomfy.utils.url_obfuscation import scrub_url_in_text
            _dialogs.warn(
                None, 'Submit failed',
                'Submit failed:\n{}'.format(
                    scrub_url_in_text(str(e), m.url)))
            self.accept()
            return

        # ── 3. Success ───────────────────────────────────────────
        self.accept()
        _show_success_dialog(machine_id)

    # ------------------------------------------------------------------
    # Input cache write (synchronous, with nuke.ProgressTask)
    # ------------------------------------------------------------------
    def _write_input_cache(self, input_ranges, force):
        import nuke  # type: ignore
        from Nukomfy.data.input_cache_writer import write_input_cache

        results = []

        for i, (param, frange) in enumerate(input_ranges):
            name = param.get('label', param.get('name', 'input'))

            # ProgressTask as label only - shows message in Nuke's task panel
            task = nuke.ProgressTask(
                'Input Cache: {}'.format(name))
            try:
                if task.isCancelled():
                    raise _UserCancelled()

                result = write_input_cache(
                    self._gizmo, i, param, frange, force=force)

                # Check after write - if user cancelled the ProgressTask
                # during render, stop before starting the next input
                if task.isCancelled():
                    raise _UserCancelled()

                results.append(result)
            except _UserCancelled:
                raise
            except RuntimeError:
                if task.isCancelled():
                    raise _UserCancelled()
                raise
            finally:
                del task

        return results

    # ------------------------------------------------------------------
    # Submit pipeline (synchronous - workflow conversion + API call)
    # ------------------------------------------------------------------
    def _run_submit_pipeline(self, m, write_results, input_params,
                             output_params, input_ranges, output_starts,
                             batch_count, nk_path, node_name,
                             deferred_output_actions=None):
        import copy
        import socket
        from Nukomfy.workflows.workflow_converter import (
            ui_to_api, inject_knob_values, inject_param_defaults,
            inject_input_paths, inject_output_params, fetch_object_info,
            apply_seed_control, inject_primitive_values,
            strip_v3_inactive_subs, live_api_node_ids,
            normalize_v3_sub_enabled,
        )

        # Convert UI workflow to API format via ui_to_api()
        with open(self._workflow_path, 'r', encoding='utf-8') as f:
            wf = json.load(f)
        node_types = {n.get('type', '') for n in wf.get('nodes', [])
                      if n.get('type')}
        for sg in wf.get('definitions', {}).get('subgraphs', []) or []:
            for n in sg.get('nodes', []) or []:
                t = n.get('type', '')
                if t:
                    node_types.add(t)
        # Reuse preflight's fetch when available - avoids a redundant
        # /object_info roundtrip.
        info_cache = getattr(self, '_preflight_info_cache', None)
        if not info_cache or set(node_types) - set(info_cache.keys()):
            info_cache = fetch_object_info(m.url, node_types)
        # Write current gizmo knob values into PrimitiveNode
        # widgets_values BEFORE conversion, so the converter inlines
        # the up-to-date values into all target nodes.
        inject_primitive_values(wf, self._gizmo_params, self._gizmo)
        api_wf = ui_to_api(wf, info_cache)

        # Nodes that actually reach an output (what ComfyUI executes).
        # Drives params_spec below so the job Detail records only the
        # parameters that influenced the render - a node absent from the
        # submitted graph, or one that survives conversion but feeds only
        # a dead branch, is excluded. Falls back to the full node set if
        # reachability can't be computed (no output node detected).
        live_api_ids = live_api_node_ids(api_wf, info_cache)
        spec_node_ids = (live_api_ids if live_api_ids is not None
                         else set(api_wf.keys()))

        # Reconcile stale `enabled` flags on V3 subs whose knob exists
        # (a Suite sub whose master option was switched on the gizmo):
        # promote them so their current values are read below instead of
        # the spec default, and the Detail lists them as Visible.
        normalize_v3_sub_enabled(self._gizmo_params, self._gizmo)
        # Apply Workflow Creator defaults for unchecked params, then
        # overlay values from enabled gizmo knobs.
        inject_param_defaults(api_wf, self._gizmo_params)
        inject_knob_values(api_wf, self._gizmo_params, self._gizmo)
        # Prune V3 sub-inputs whose master option is no longer active
        # (the converter's expansion was based on the workflow's original
        # widgets_values; the user may have switched master via knob).
        strip_v3_inactive_subs(api_wf, self._gizmo_params, self._gizmo)

        # Snapshot base seed values before the batch loop so increment/
        # decrement/randomize can be applied per-iteration (control_after_generate).
        base_seeds = {}
        seed_labels = {}
        for p in self._gizmo_params:
            if not p.get('is_seed') or not p.get('enabled', True):
                continue
            kn = p.get('_knob_name', '')
            if not kn:
                continue
            k = self._gizmo.knob(kn)
            if k is None:
                continue
            try:
                base_seeds[kn] = int(k.value())
            except (ValueError, TypeError):
                base_seeds[kn] = 0
            seed_labels[kn] = p.get('label') or p.get('name') or kn
        last_used_seeds = {}

        # Inject input cache file paths
        if input_params and write_results:
            inject_input_paths(api_wf, input_params, write_results, machine=m)

        # Build output info
        out_info = _build_output_path_info(self._gizmo) if output_params else None

        # Build metadata.
        # `submitted_by` (username) is the user identity - the only field
        # consumed by ownership checks and "is my job?" filters.
        # `submitter_host` is metadata for trace (tooltip / Detail dialog).
        # The WebSocket clientId (ws_session_id(), unique per process) is the
        # transport session id used on the POST and the WS - NOT user identity.
        submitted_by = current_user()
        submitter_host = socket.gethostname()
        extra = {}
        if out_info:
            wf_name = out_info.get('workflow_name', '')
            if wf_name:
                extra['nfy_workflow_name'] = wf_name

        # Build per-output frame_range template.
        # Single: end = start (e.g. 5-5 after format). Range: end = None -
        # the true last frame is unknown until the render completes, and
        # display-time glob derivation fills it in.
        frame_range_entries = []
        for i, op in enumerate(output_params):
            sf = output_starts[i] if i < len(output_starts) else 1
            if out_info:
                _names = out_info.get('output_names', [])
                name = (_names[i] if i < len(_names) and _names[i]
                        else op.get('label', 'output'))
            else:
                name = op.get('label', 'output')
            io_mode = op.get('io_mode', '')
            end = sf if io_mode == 'Single' else None
            frame_range_entries.append({
                'name': name, 'start': sf, 'end': end, 'io_mode': io_mode,
            })

        # Build input_ranges entries (doesn't shift per batch - inputs are
        # the same across iterations).
        input_ranges_entries = []
        full_by_param = {id(p): fr for p, fr in input_ranges}
        for ip in input_params:
            io_mode = ip.get('io_mode', '')
            name = ip.get('label') or ip.get('name') or 'input'
            fr = full_by_param.get(id(ip))
            if fr:
                first, last = fr
            else:
                first = last = 0
            input_ranges_entries.append({
                'name': name, 'start': first, 'end': last, 'io_mode': io_mode,
            })
        # Build output path template list (with #### pattern) and per-output
        # paddings. Pass gizmo-resident metadata tokens
        # ({output_index}, {workflow_uuid}, {workflow_category},
        # {workflow_model}) so the path matches what the gizmo's preview
        # and Read Outputs reconstruct. Submit-runtime tokens are not
        # supported (would break Read Outputs).
        output_path_list = []
        output_paddings = []
        if out_info:
            from Nukomfy.utils.output_path import build_output_path, clamp_padding
            from Nukomfy.core.settings import settings as _settings
            # frame_padding is read from Settings.
            pad = clamp_padding(_settings.frame_padding)
            output_names = out_info.get('output_names', [])
            for idx, op in enumerate(output_params):
                if idx < len(output_names) and output_names[idx]:
                    name = output_names[idx]
                else:
                    name = out_info.get('output_name', 'output')
                nid = str(op.get('target_node_id', ''))
                node_inputs = api_wf.get(nid, {}).get('inputs', {})
                ext = node_inputs.get('file_type', 'exr')
                output_paddings.append(pad)
                frame_pat = '#' * pad
                path = build_output_path(
                    out_info.get('output_dir', ''),
                    out_info.get('nk_file', 'Untitled'),
                    out_info.get('workflow_name', 'output'),
                    name,
                    out_info.get('version', 1),
                    frame_pat, ext,
                    node_name=out_info.get('node_name'),
                    workflow_alias=out_info.get('workflow_alias'),
                    output_index=idx + 1,
                    workflow_uuid=out_info.get('workflow_uuid'),
                    workflow_categories=out_info.get('workflow_categories'),
                    workflow_models=out_info.get('workflow_models'))
                output_path_list.append(path)

        if nk_path:
            extra['nfy_nk_file'] = nk_path
        if node_name:
            extra['nfy_node_name'] = node_name
        if output_path_list:
            extra['nfy_output_paths'] = output_path_list

        # Selective overwrite + ownership sentinel claim.
        # Group basename patterns per directory (multiple outputs may
        # share a directory if the template doesn't include
        # {output_name}). Pattern is the basename with `####` placeholder
        # intact - selective_delete_dir only removes files matching
        # this template, preserving any foreign files the user added.
        if output_path_list:
            import Nukomfy.utils.fs_safe as fs_safe
            patterns_per_dir = {}
            for path in output_path_list:
                d = os.path.dirname(path)
                bn = os.path.basename(path)
                patterns_per_dir.setdefault(d, []).append(bn)

            # 1) Apply deferred overwrite actions:
            #    - 'delete_and_render': selective_delete_dir with explicit
            #      delete_basenames = collisions (only the colliding files
            #      removed, foreign + non-conflicting Nukomfy preserved).
            #    - 'overwrite': symmetric ownership precondition via
            #      assert_output_ownership - both Delete and Overwrite
            #      are destructive (overwriting existing files in place
            #      replaces them just like Delete does), so both refuse
            #      uniformly when the sentinel is missing. Same popup
            #      as Delete. Then ComfyUI overwrites the colliding
            #      files in place on successful write.
            #    Dirs absent from the dict are 'silent' (no collision).
            actions_map = deferred_output_actions or {}
            for cd, (action, collisions) in actions_map.items():
                if not os.path.exists(fs_safe._long_path(cd)):
                    continue
                if action == 'delete_and_render':
                    # selective_delete_dir shows a detailed popup on
                    # sentinel issue / OS error. On refusal we raise
                    # _SilentAbort so the outer handler doesn't add a
                    # second generic dialog.
                    if not fs_safe.selective_delete_dir(
                            cd, parent=self,
                            action='output delete and re-render',
                            sentinel_kind='output',
                            delete_basenames=collisions):
                        raise _SilentAbort()
                elif action == 'overwrite':
                    if not fs_safe.assert_output_ownership(
                            cd, parent=self,
                            action='output overwrite'):
                        raise _SilentAbort()

            # 2) Claim ownership of every target dir (existing + new)
            # with the patterns we're about to write. Writing the
            # sentinel BEFORE the POST means a crash mid-render still
            # leaves a marker the next attempt can recognise.
            wf_uuid_knob = self._gizmo.knob('_nfy_wf_id')
            wf_uuid = wf_uuid_knob.value() if wf_uuid_knob else ''
            for cd, pats in patterns_per_dir.items():
                # fs_safe.makedirs already shows a detailed popup on
                # failure (perms, drive unreachable, etc.). _SilentAbort
                # avoids stacking a second dialog above.
                if not fs_safe.makedirs(cd, parent=self,
                                        action='output sentinel claim'):
                    raise _SilentAbort()
                fs_safe.write_output_sentinel(
                    cd, workflow_id=wf_uuid,
                    gizmo_name=self._gizmo.name(),
                    file_patterns=pats)

        # Submit (loop for batch count). The finally advances the seed
        # knobs by the number of jobs actually queued: a mid-batch
        # failure after a successful POST must still advance them, or
        # the next submit would silently reuse an already-used seed.
        posts_ok = 0
        try:
            for batch_idx in range(batch_count):
                submit_wf = copy.deepcopy(api_wf)

                used_this_iter = apply_seed_control(
                    submit_wf, self._gizmo_params, self._gizmo,
                    batch_idx, base_seeds)
                if used_this_iter:
                    last_used_seeds = used_this_iter

                if output_params:
                    # Shift every per-output start by the batch offset.
                    # Batch>1 is only possible when all outputs are 'single',
                    # so consecutive frames are what we want.
                    per_output_starts = [
                        (output_starts[i] if i < len(output_starts) else 1) + batch_idx
                        for i in range(len(output_params))
                    ]
                    inject_output_params(submit_wf, output_params,
                                         per_output_starts, out_info, machine=m)

                # Per-iteration output_ranges (shifted by batch_idx). For Range
                # outputs end stays None - display resolves it from disk.
                iter_entries = []
                for e in frame_range_entries:
                    iter_entries.append({
                        'name': e['name'],
                        'start': e['start'] + batch_idx,
                        'end': (e['end'] + batch_idx) if e['end'] is not None else None,
                        'io_mode': e['io_mode'],
                    })

                # Per-iteration output paths with resolved frame numbers (Single)
                # or #### template left intact (Range - end unknown).
                iter_output_paths = []
                for idx, op in enumerate(output_params):
                    if idx >= len(output_path_list):
                        continue
                    sf = output_starts[idx] if idx < len(output_starts) else 1
                    io_mode = op.get('io_mode', '')
                    pad = output_paddings[idx] if idx < len(output_paddings) else 4
                    if io_mode == 'Single':
                        frame_num = sf + batch_idx
                        frame_str = str(frame_num).zfill(pad)
                        path = output_path_list[idx].replace(
                            '#' * pad, frame_str)
                    else:
                        path = output_path_list[idx]
                    iter_output_paths.append(path)

                # Per-iteration extras (includes batch_index and shifted output
                # ranges; input_ranges are constant across the batch).
                from Nukomfy.data.submit_history import generate_job_id, record_submit
                nfy_job_id = generate_job_id()

                # Build seeds_used_labeled before the POST so the
                # iter_extra wire enrichment below can include it.
                seeds_used_labeled = {
                    seed_labels.get(kn, kn): v
                    for kn, v in used_this_iter.items()
                }

                # Read the read_color knob before the POST; record_submit
                # consumes it below.
                read_color_knob = self._gizmo.knob('_nfy_read_color')
                try:
                    read_color = (int(read_color_knob.value())
                                  if read_color_knob else 0)
                except (ValueError, TypeError):
                    read_color = 0

                # Collect diagnostic capture once, BEFORE the POST, so we
                # can splat the same data into both `extra_data` (HTTP
                # wire - visible cross-user via /api/history) and the local
                # DB record. The HTTP probe inside collect_submit_capture has
                # a short timeout (2s) so a slow /system_stats never blocks
                # the submit. Per-knob soft-fail for the 3 gizmo metadata.
                try:
                    from Nukomfy.client.system_capture import collect_submit_capture
                    _capture = collect_submit_capture(
                        m, submit_wf, write_results=write_results)
                except Exception:
                    _log.exception(
                        'Submit environment capture failed (batch %d, non-fatal)',
                        batch_idx + 1)
                    _capture = {}
                try:
                    _wfa = self._gizmo.knob('_nfy_wf_author')
                    if _wfa and _wfa.value():
                        _capture['workflow_author'] = _wfa.value()
                    _wfu = self._gizmo.knob('_nfy_wf_id')
                    if _wfu and _wfu.value():
                        _capture['workflow_uuid'] = _wfu.value()
                    # Capture the workflow SemVer (metadata.json `version`)
                    # snapshotted at build time in `_nfy_wf_version`. This is
                    # the asset version, not the render counter (v001/v002,
                    # already visible in Output Path).
                    _wfv = self._gizmo.knob('_nfy_wf_version')
                    if _wfv and _wfv.value():
                        _capture['workflow_version'] = _wfv.value()
                    # Snapshot of every workflow param the author configured
                    # in the Workflow Creator (role=='knob'),
                    # with the `enabled` flag preserved so the Detail tab
                    # can split into Exposed (enabled=True, visible on the
                    # gizmo Properties) vs Hidden (enabled=False, defined
                    # but not promoted to a knob). Workflow_api inputs not
                    # in this list (e.g. internal Comfy plumbing like
                    # NukomfyWrite.file_path or fixed sampler config the
                    # author chose not to surface) are NOT shown - keeps
                    # the Submitted parameters listing focused on what the
                    # author meant to expose.
                    # `label` is the user-facing display name on the gizmo
                    # Properties panel (set by the author in Workflow Creator).
                    # Falls back to the widget name when not customised. Detail
                    # tab shows it as the primary identifier (what the user
                    # actually sees on the gizmo) instead of the raw input name.
                    # Params of nodes that don't reach an output are dropped:
                    # they were never executed, so listing them as "submitted"
                    # (with no resolvable value) misleads.
                    params_spec = []
                    for p in (self._gizmo_params or []):
                        if p.get('role') != 'knob':
                            continue
                        enabled = bool(p.get('enabled', False))
                        if p.get('_is_primitive'):
                            # The PrimitiveNode is inlined away during conversion,
                            # so its own id is absent from the API graph. Emit one
                            # entry per reachable target (where the value lands):
                            # a multi-target primitive drives the same value into
                            # several widgets, so each gets its own row (none is
                            # hidden) tagged `primitive` so the Detail can mark it
                            # and explain the shared value. Mirrors
                            # inject_primitive_values, which writes to the same
                            # targets.
                            for t in p.get('_primitive_targets', []) or []:
                                tid = str(t.get('node_id', ''))
                                twn = t.get('widget_name', '') or ''
                                if tid in spec_node_ids and twn:
                                    params_spec.append({
                                        'node_id': tid,
                                        'name': twn,
                                        'label': '',
                                        'enabled': enabled,
                                        'primitive': True,
                                    })
                            continue
                        if p.get('target_node_id') is None:
                            continue
                        nid = str(p.get('target_node_id', ''))
                        name = p.get('widget_name') or p.get('name', '')
                        if not name or nid not in spec_node_ids:
                            continue
                        params_spec.append({
                            'node_id': nid,
                            'name': name,
                            'label': p.get('label') or '',
                            'enabled': enabled,
                        })
                    if params_spec:
                        _capture['params_spec'] = params_spec
                    # Per-input write template capture. The workflow author
                    # may declare a custom `.nk` template per
                    # input (workflow) or rely on the global library (default).
                    # Recording it here closes the log-dialog goal
                    # "which write template was used".
                    from Nukomfy.client.system_capture import extract_write_templates
                    _wt = extract_write_templates(
                        self._gizmo_params, self._workflow_path)
                    if _wt:
                        _capture['write_templates'] = _wt
                except Exception:
                    _log.exception(
                        'Gizmo metadata capture failed (batch %d, non-fatal)',
                        batch_idx + 1)

                # Bundle the diagnostic capture into the same 3 JSON groups
                # used by the DB schema (workflow_meta / environment /
                # server_snapshot) so a TD/IT inspecting another user's job
                # via /api/history sees the full diagnostic context.
                _wf_meta = {k: _capture[k] for k in (
                    'workflow_uuid', 'workflow_author',
                    'workflow_version', 'params_spec',
                    'write_templates') if k in _capture}
                _env = {k: _capture[k] for k in (
                    'os_submitter', 'nuke_version', 'nukomfy_version',
                    'input_cache') if k in _capture}
                _srv = {}
                if 'system_stats' in _capture:
                    _srv['system_stats'] = _capture['system_stats']
                if 'server_version' in _capture:
                    _srv['server_version'] = _capture['server_version']

                iter_extra = dict(extra)
                iter_extra['nfy_batch_count'] = batch_count
                iter_extra['nfy_batch_index'] = batch_idx + 1
                iter_extra['nfy_job_id'] = nfy_job_id
                iter_extra['nfy_submitted_by'] = submitted_by
                iter_extra['nfy_submitter_host'] = submitter_host
                iter_extra['nfy_machine_name'] = m.name
                iter_extra['nfy_machine_url'] = m.to_persistable_url()
                # Single submit instant for this batch item: the same value
                # feeds the HTTP wire (below), the local DB record and - via
                # extra_data - the Suite server record, so all three agree.
                sent_at = datetime.datetime.now().isoformat(
                    timespec='microseconds')
                iter_extra['nfy_sent_at'] = sent_at
                if input_ranges_entries:
                    iter_extra['nfy_input_ranges'] = input_ranges_entries
                if iter_entries:
                    iter_extra['nfy_output_ranges'] = iter_entries
                    # Also expose as nfy_frame_range so the parser's lookup
                    # `extra.get('nfy_frame_range')` resolves.
                    iter_extra['nfy_frame_range'] = iter_entries
                if iter_output_paths:
                    iter_extra['nfy_output_paths'] = iter_output_paths
                if seeds_used_labeled:
                    iter_extra['nfy_seeds_used'] = seeds_used_labeled
                if _wf_meta:
                    iter_extra['nfy_workflow_meta'] = _wf_meta
                if _env:
                    iter_extra['nfy_environment'] = _env
                if _srv:
                    iter_extra['nfy_server_snapshot'] = _srv

                # Generate the prompt_id client-side and send it on the POST: the
                # server accepts a user-supplied prompt_id and echoes it back, so
                # the queue listing and the progress broadcast share one id.
                # Canonical hyphenated form (str, not .hex): newer ComfyUI builds
                # validate prompt_id as a real UUID and reject the bare 32-hex form.
                prompt_id = str(uuid.uuid4())

                # Append in_use entry to every input cache sentinel BEFORE
                # the POST so the cleanup-on-submit logic in any
                # parallel Nuke session can see this job claiming the cache.
                # On POST failure we roll back. On terminal job (success/
                # fail/cancel/remove) the entry is removed via the hook in
                # render_queue_panel persist_terminal_state.
                cache_dirs_for_in_use = []
                try:
                    from Nukomfy.data.input_cache_writer import add_in_use_entry
                    for wr in (write_results or []):
                        od = wr.get('output_dir') if isinstance(wr, dict) else None
                        if od:
                            cache_dirs_for_in_use.append(od)
                            add_in_use_entry(
                                od, m.to_persistable_url(), m.name, prompt_id)
                except Exception:
                    _log.exception(
                        'Input cache in-use register failed for %s (non-fatal)',
                        fmt_job(prompt_id))

                # The in-use claim above is written before the POST, so any early
                # exit before the job is recorded would leak it (no terminal hook
                # ever clears an unrecorded job). A `committed` flag + `finally`
                # rolls it back for every failure path - POST error, missing
                # server id, or a record_submit error - in one place.
                committed = False
                try:
                    try:
                        resp = post_prompt(m.url, submit_wf,
                                           client_id=ws_session_id(),
                                           extra_data=iter_extra or None,
                                           prompt_id=prompt_id)
                    except Exception as e:
                        _log.error(
                            'Submit failed for %s to %s: %s',
                            fmt_job(nfy_job_id), fmt_machine(m.url, m.name), e)
                        raise

                    # The job is queued from this point on (even when
                    # the response lacks an id): count it so the seed
                    # knobs advance in the finally below.
                    posts_ok += 1
                    server_prompt_id = resp.get('prompt_id', '')
                    if not server_prompt_id:
                        raise RuntimeError('The server did not return a job ID.')
                    # Defensive: if the server ignored our prompt_id (shouldn't
                    # happen on current ComfyUI), re-key the input-cache in-use
                    # entries to the server-issued id. Progress needs no action -
                    # it is keyed by the server's prompt_id in both the queue
                    # listing and the broadcast.
                    if server_prompt_id != prompt_id:
                        # Re-key in_use entries to the server-issued prompt_id
                        try:
                            from Nukomfy.data.input_cache_writer import (
                                add_in_use_entry, remove_in_use_entry)
                            for od in cache_dirs_for_in_use:
                                remove_in_use_entry(od, prompt_id)
                                add_in_use_entry(
                                    od, m.to_persistable_url(), m.name,
                                    server_prompt_id)
                        except Exception:
                            _log.exception(
                                'Input cache in-use re-key failed: %s -> %s',
                                fmt_job(prompt_id), fmt_job(server_prompt_id))
                        prompt_id = server_prompt_id

                    # `_capture` was built once before the POST so the same
                    # diagnostic dict feeds both `extra_data` HTTP wire and the
                    # local DB record - no duplicate /api/system_stats probe.
                    # `workflow_api_payload` snapshots the dict actually posted
                    # to /prompt into the row's BLOB column (atomic INSERT, no
                    # second UPDATE).
                    record_submit(
                        prompt_id=prompt_id,
                        machine_name=m.name,
                        machine_url=m.to_persistable_url(),
                        workflow_name=extra.get('nfy_workflow_name', ''),
                        frame_range=iter_entries,
                        nk_file=nk_path,
                        node_name=node_name,
                        output_paths=iter_output_paths,
                        batch_count=batch_count,
                        seeds_used=seeds_used_labeled or None,
                        input_ranges=input_ranges_entries or None,
                        batch_index=batch_idx + 1,
                        nfy_job_id=nfy_job_id,
                        nfy_submitted_by=submitted_by,
                        nfy_submitter_host=submitter_host,
                        read_color=read_color,
                        sent_at=sent_at,
                        workflow_api_payload=submit_wf,
                        **_capture,
                    )
                    committed = True
                finally:
                    if not committed:
                        # Keyed by the current prompt_id (re-keyed to the server id
                        # above when they differ).
                        try:
                            from Nukomfy.data.input_cache_writer import remove_in_use_entry
                            for od in cache_dirs_for_in_use:
                                remove_in_use_entry(od, prompt_id)
                        except Exception:
                            _log.exception(
                                'Input cache in-use rollback failed for %s',
                                fmt_job(prompt_id))

        finally:
            self._advance_seed_knobs(base_seeds, last_used_seeds, posts_ok)

    def _advance_seed_knobs(self, base_seeds, last_used_seeds, posts_ok):
        """Write the NEXT seed into each seed knob after queueing.

        ComfyUI convention: the shown value was used by the queued job(s);
        the control advances it afterwards. `posts_ok` is the number of
        jobs actually queued - 0 means nothing was submitted, so the
        user's value stays untouched for the retry.
          randomize -> new random value, not yet used
          increment -> base + posts_ok
          decrement -> base - posts_ok
          fixed     -> unchanged (no write)
        All clamped to the widget's real max.
        """
        if not posts_ok:
            return
        # Everything is inside the try: this method runs from a finally, so
        # a stray import/lookup error must not mask the loop's exception.
        try:
            import nuke  # type: ignore
            from Nukomfy.workflows.workflow_converter import (
                _seed_max_for, random_seed_value)
            next_seeds = {}
            for p in self._gizmo_params:
                if not p.get('is_seed') or not p.get('enabled', True):
                    continue
                kn = p.get('_knob_name', '')
                if not kn or kn not in base_seeds:
                    continue
                ctrl = self._gizmo.knob(kn + '_control')
                mode = ctrl.value() if ctrl else 'fixed'
                base = base_seeds[kn]
                seed_max = _seed_max_for(p)
                if mode == 'randomize':
                    # Gate on last_used_seeds: a seed apply_seed_control
                    # never processed must not advance.
                    if kn in last_used_seeds:
                        next_seeds[kn] = random_seed_value(p)
                elif mode == 'increment':
                    next_seeds[kn] = max(0, min(base + posts_ok, seed_max))
                elif mode == 'decrement':
                    next_seeds[kn] = max(0, min(base - posts_ok, seed_max))
        except Exception:
            _log.exception('Seed knob advance computation failed')
            return

        for kn, val in next_seeds.items():
            k = self._gizmo.knob(kn)
            if k is None:
                continue
            try:
                if isinstance(k, nuke.String_Knob):
                    k.setValue(str(val))
                    last_k = self._gizmo.knob(kn + '_last')
                    if last_k is not None:
                        last_k.setValue(str(val))
                else:
                    k.setValue(val)
            except Exception:
                _log.exception(
                    'Seed knob write-back failed for %s - the next submit '
                    'may reuse the same seed', kn)

    def closeEvent(self, event):
        self._save_layout_state()
        self._stop_auto_timer()
        self._status_worker = stop_worker(self._status_worker)
        self._disconnect_machine_info_service()
        super().closeEvent(event)

    def done(self, result):
        self._save_layout_state()
        self._stop_auto_timer()
        self._status_worker = stop_worker(self._status_worker)
        self._disconnect_machine_info_service()
        super().done(result)

    def _disconnect_machine_info_service(self):
        """Drop the MachineInfoService subscription before this modal dialog
        is torn down. The service outlives the dialog (process singleton), so
        a late sweep result must not repaint a closing window. Idempotent."""
        svc = getattr(self, '_machine_info_service', None)
        if svc is not None:
            try:
                svc.infoChanged.disconnect(self._on_machine_info_changed)
            except (RuntimeError, TypeError):
                pass
            self._machine_info_service = None

    def _save_layout_state(self):
        ui_state.save_geometry('submit_panel', self)
        ui_state.save_column_widths('submit_machine_table', self._machine_table)
        try:
            sizes = list(self._splitter.sizes())
            if len(sizes) == 2 and all(s > 0 for s in sizes):
                ui_state.set('submit_panel', splitter_sizes=sizes)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Success dialog (shown after a successful submit)
# ---------------------------------------------------------------------------

def _show_success_dialog(machine_id=None):
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle('Job sent')
    dlg.setMinimumWidth(320)
    dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.Window)

    lay = QtWidgets.QVBoxLayout(dlg)
    lay.setContentsMargins(20, 20, 20, 16)
    lay.setSpacing(16)

    msg = QtWidgets.QLabel('Job sent to ComfyUI.')
    msg.setStyleSheet('color:#ccc;font-size:13px;')
    msg.setAlignment(QtCore.Qt.AlignCenter)
    lay.addWidget(msg)

    btn_lay = QtWidgets.QHBoxLayout()
    btn_lay.addStretch()
    rq_btn = QtWidgets.QPushButton('Render Manager')
    set_press_icon(rq_btn, VIEW_LIST, size=16)
    rq_btn.setFixedHeight(28)
    rq_btn.setAutoDefault(False)
    rq_btn.setDefault(False)
    rq_btn.setStyleSheet('QPushButton{font-weight:bold;}')
    rq_btn.clicked.connect(
        lambda: (_open_render_queue(machine_id), dlg.accept()))
    btn_lay.addWidget(rq_btn)
    close_btn = QtWidgets.QPushButton('Close (5)')
    close_btn.setFixedHeight(28)
    close_btn.setAutoDefault(False)
    close_btn.setDefault(True)
    close_btn.clicked.connect(dlg.accept)
    btn_lay.addWidget(close_btn)
    btn_lay.addStretch()
    lay.addLayout(btn_lay)

    # 5s auto-close countdown - gives the user a moment to click Render
    # Queue before the dialog dismisses itself.
    remaining = [5]
    countdown = QtCore.QTimer(dlg)
    countdown.setInterval(1000)

    def _tick():
        remaining[0] -= 1
        if remaining[0] <= 0:
            countdown.stop()
            dlg.accept()
        else:
            close_btn.setText('Close ({})'.format(remaining[0]))

    countdown.timeout.connect(_tick)
    countdown.start()

    # Fixed, non-resizable size locked to the natural layout size. Qt scales
    # the layout with the monitor DPI like the rest of the UI. adjustSize()
    # must run first so both setFixedSize and center_on_screen see the real
    # (post-layout) size.
    dlg.adjustSize()
    dlg.setFixedSize(dlg.size())
    center_on_screen(dlg)
    dlg.exec_()


def _open_render_queue(machine_id=None):
    from Nukomfy.gui.render_queue_panel import show_render_queue
    QtCore.QTimer.singleShot(
        0, lambda: show_render_queue(expand_machine_id=machine_id))


# ---------------------------------------------------------------------------
# Entry point (called from gizmo PyScript_Knob)
# ---------------------------------------------------------------------------
def show_submit_panel(gizmo_node):
    """Open the submit panel for the given gizmo node.

    Pre-flight: refuse to open if the workflow JSON referenced by the
    gizmo cannot be located. The workflow is located by UUID against the
    current Local/Shared roots, so renaming or moving the Library folder
    still lets old gizmos submit.
    """
    wf_id_knob = gizmo_node.knob('_nfy_wf_id')
    wf_id = wf_id_knob.value() if wf_id_knob else ''
    resolved = _resolve_workflow_path(wf_id)
    if not resolved:
        _dialogs.warn(
            None, 'Workflow missing',
            _WORKFLOW_MISSING_MSG.format(path=wf_id or '<empty>'))
        return
    panel = SubmitPanel(gizmo_node)
    panel._workflow_path = resolved
    panel.exec()
