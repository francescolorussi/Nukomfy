"""Reusable QThread workers and lifecycle helpers used by multiple panels."""

from Nukomfy.utils.qt_compat import QtCore


# Workers that have been cancelled but may still be running.
# Prevent garbage-collection (and the crash it causes) by holding a reference.
_abandoned = set()


def abandon_worker(worker):
    """Move *worker* into the abandoned set; auto-discard when it finishes."""
    _abandoned.add(worker)
    try:
        worker.finished.connect(lambda: _abandoned.discard(worker))
    except RuntimeError:
        pass


def stop_worker(worker):
    """Disconnect signals, cancel, abandon if still running.  Returns None."""
    if worker is None:
        return None
    for attr in ('result', 'finished', 'stage_changed', 'progress', 'completed'):
        sig = getattr(worker, attr, None)
        if sig is not None:
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
    if worker.isRunning():
        worker.cancel()
        # Reparent to None: a QThread created with parent=<widget> is a Qt
        # child of that widget. WA_DeleteOnClose destroys the widget AND
        # its children - destroying a still-running QThread crashes Nuke
        # ("QThread: Destroyed while thread is still running"). Detaching
        # here lets the worker survive the widget teardown until it sees
        # the cancel flag and exits cleanly.
        try:
            worker.setParent(None)
        except (RuntimeError, TypeError):
            pass
        abandon_worker(worker)
    return None


class UnifiedFetchWorker(QtCore.QThread):
    """Fetch state from a list of machines in parallel via *check_fn*.

    Generic runner: each panel supplies the check_fn that defines what
    "state" means for it (e.g. queue counts for submit_panel, reachability
    for machines_panel, full queue+recent-terminals snapshot for
    render_queue_panel's `_unified_check`).

    URL dedup: machines are grouped by `url` before dispatch, so when 2+
    entries share the same endpoint (alias rows) `check_fn` runs **once**
    per unique URL. Each alias still receives its own `result.emit`, with
    a deepcopy of the payload so receivers that mutate `info` in place
    (e.g. RenderQueuePanel._on_result filtering against `_locally_removed`)
    don't contaminate the other alias.

    Signals:
        result(machine_id: str, info: dict)
    """
    result = QtCore.Signal(str, object)

    def __init__(self, machines, check_fn, parent=None):
        super().__init__(parent)
        self._machines = list(machines)
        self._check_fn = check_fn
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import copy

        # Group machines by URL: alias entries (same `url`, different `id`)
        # share one HTTP fetch but each alias still receives `result.emit`
        # so per-machine_id UI rows + ingest_machine + lost/orphan detection
        # continue to run for every entry. Without this dedup N alias on
        # the same endpoint = N redundant queue+listing+detail HTTP bursts
        # at every cold-open / Update All / per-machine refresh.
        by_url = {}
        for m in self._machines:
            by_url.setdefault(m.url, []).append(m)

        def _check_group(machines_for_url):
            rep = machines_for_url[0]
            return machines_for_url, self._check_fn(rep)

        pool_size = max(len(by_url), 1)
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures = [pool.submit(_check_group, ms)
                       for ms in by_url.values()]
            for future in as_completed(futures):
                if self._cancelled:
                    return
                try:
                    machines, info = future.result()
                    for m in machines:
                        # Receivers (e.g. RenderQueuePanel._on_result)
                        # mutate `info` in place; each alias gets its
                        # own copy to avoid cross-alias contamination.
                        self.result.emit(m.id, copy.deepcopy(info))
                except RuntimeError:
                    # Receiver destroyed (Nuke shutting down)
                    return


class ThumbnailWorker(QtCore.QThread):
    """Generate or delete preview_thumb.webp co-located with preview.gif.

    Decodes the first frame of one or more GIFs in parallel via
    ThreadPoolExecutor (max 4 workers), saves each
    as a static thumb next to the source via Nukomfy.gui.preview_thumb, and
    emits per-file signals so the Library can repaint just the affected
    cards.

    Streaming: callers can append more work via `add_work()` while the
    thread is running; the loop drains both the initial batch and any
    extras until exhausted.

    Signals:
        thumbReady(gif_path: str, ok: bool)
            Emitted once per regenerated GIF. `ok=False` means decode or
            write failed - delete jobs do not emit (cheap and silent).
    """
    thumbReady = QtCore.Signal(str, bool)

    def __init__(self, regen_items, delete_folders, parent=None):
        super().__init__(parent)
        # regen_items: list[(folder, gif_path)]
        self._regen = list(regen_items)
        self._delete = list(delete_folders)
        self._extra_regen = []
        self._extra_delete = []
        self._lock = QtCore.QMutex()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def add_work(self, regen_items, delete_folders):
        """Append more work while the thread is running. Items already
        processed are filtered by the per-run `seen` sets."""
        with QtCore.QMutexLocker(self._lock):
            self._extra_regen.extend(regen_items)
            self._extra_delete.extend(delete_folders)

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from Nukomfy.gui import preview_thumb

        def _gen(folder, gif_path):
            pix = preview_thumb.render_first_frame(gif_path)
            if pix is None:
                return gif_path, False
            ok = preview_thumb.write_thumb(folder, pix)
            return gif_path, ok

        seen_regen = set()
        seen_delete = set()
        with ThreadPoolExecutor(max_workers=4) as pool:
            while True:
                if self._cancelled:
                    return
                with QtCore.QMutexLocker(self._lock):
                    regen_batch = list(self._regen) + list(self._extra_regen)
                    delete_batch = list(self._delete) + list(self._extra_delete)
                    self._regen = []
                    self._extra_regen = []
                    self._delete = []
                    self._extra_delete = []
                regen_batch = [(f, g) for (f, g) in regen_batch
                               if g not in seen_regen]
                delete_batch = [d for d in delete_batch
                                if d not in seen_delete]
                if not regen_batch and not delete_batch:
                    break
                seen_regen.update(g for _, g in regen_batch)
                seen_delete.update(delete_batch)
                # Deletes are cheap, do them inline (no thread pool needed)
                for folder in delete_batch:
                    if self._cancelled:
                        return
                    preview_thumb.delete_thumb(folder)
                if regen_batch:
                    futures = {pool.submit(_gen, f, g): g
                               for (f, g) in regen_batch}
                    for fut in as_completed(futures):
                        if self._cancelled:
                            return
                        try:
                            gif_path, ok = fut.result()
                            self.thumbReady.emit(gif_path, ok)
                        except RuntimeError:
                            # Receiver destroyed (Nuke shutting down)
                            return


class WorkflowScanWorker(QtCore.QThread):
    """Background scan_workflows + name-collision detection for Library.

    Lifts `workflow_loader.scan_workflows()` off the main thread so the
    Library panel can show "Loading..." while the I/O runs. Emits a
    single batched signal when done.

    Signals:
        scanReady(items: list[WorkflowItem],
                  name_dups: set[str],
                  name_folder_dups: set[(str, str)])
    """
    scanReady = QtCore.Signal(object, object, object)

    def __init__(self, local_path, shared_paths, parent=None):
        super().__init__(parent)
        self._local = local_path
        self._shared = list(shared_paths or [])
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        import os
        import Nukomfy.workflows.workflow_loader as wl
        items = wl.scan_workflows(self._local, self._shared)
        if self._cancelled:
            return
        seen_names = {}
        seen_name_folder = {}
        for it in items:
            seen_names[it.name] = seen_names.get(it.name, 0) + 1
            key = (it.name, os.path.basename(it.folder_path))
            seen_name_folder[key] = seen_name_folder.get(key, 0) + 1
        name_dups = {n for n, c in seen_names.items() if c > 1}
        name_folder_dups = {k for k, c in seen_name_folder.items() if c > 1}
        if self._cancelled:
            return
        try:
            self.scanReady.emit(items, name_dups, name_folder_dups)
        except RuntimeError:
            # Receiver destroyed
            return
