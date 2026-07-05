"""Session-scoped refresher for ComfyUI machine hardware/version info.

Centralizes the one piece of machine state the panels never refreshed on
their own: the ``/system_stats`` snapshot (os, comfyui_ver, gpu, vram_total,
ram_total). The Submit Panel and Render Manager poll only ``/queue``, so they
display whatever a prior ``check_machine`` left cached in ``m.info``. This
service runs a full ``check_machine`` sweep once per Nuke session - the first
time a panel that lists machines becomes visible - then notifies listeners via
``infoChanged`` so their rows repaint.

Single owner, mirroring the ``RenderDataStore`` pattern: panels are read-only
subscribers. Settings > Machines keeps its own explicit Update path (it already
fetches on every show), so it is intentionally left untouched here.
"""

from Nukomfy.utils.qt_compat import QtCore
from Nukomfy.client.machines import (
    machine_manager, check_machine, apply_machine_info)
from Nukomfy.gui.workers import UnifiedFetchWorker


class _MachineInfoService(QtCore.QObject):
    # Emitted on the main thread after a machine's snapshot is applied.
    infoChanged = QtCore.Signal(str)   # machine_id

    def __init__(self):
        super().__init__()
        self._worker = None
        # Machine ids already hardware-refreshed in this Nuke session. In
        # memory only: a new process re-fetches everything on first view,
        # which is exactly the "refresh at startup" contract.
        self._refreshed_ids = set()

    def ensure_fresh(self):
        """Refresh the ``/system_stats`` snapshot for machines not yet
        refreshed this session. Idempotent: the first panel to show the
        machines does the sweep, later opens are no-ops. Non-blocking - the
        fetch runs on a background thread and rows repaint via ``infoChanged``.
        """
        if self._worker is not None and self._worker.isRunning():
            return
        targets = [m for m in machine_manager.machines
                   if m.id not in self._refreshed_ids]
        if not targets:
            return
        self._worker = UnifiedFetchWorker(targets, check_machine)
        self._worker.result.connect(self._on_result)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_result(self, machine_id, info):
        # Main thread (queued from the worker). The shared writer does the
        # in-memory copy-on-write apply; the service adds only the session
        # bookkeeping (mark refreshed, notify listeners).
        m = machine_manager.get(machine_id)
        if m is None:
            return
        apply_machine_info(m, info)
        if info.get('online'):
            # Mark refreshed only on a real answer, so a machine that was
            # offline at first view retries on the next panel open instead
            # of being stuck on a stale snapshot for the whole session.
            self._refreshed_ids.add(machine_id)
        self.infoChanged.emit(machine_id)

    def _on_finished(self):
        self._worker = None


_service = None


def service():
    """Process-wide singleton. Safe to call from a panel ``__init__`` - a
    QApplication exists by then in Nuke, and the object is created on the
    main thread."""
    global _service
    if _service is None:
        _service = _MachineInfoService()
    return _service
