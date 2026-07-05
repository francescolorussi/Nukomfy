"""Persists window sizes, view modes and filter selections across sessions.

Saved to ~/.nuke/nukomfy_uistate.json.
Only written when values differ from defaults so the file stays clean.
"""

import json
import logging
import os

_log = logging.getLogger(__name__)

_FILE = os.path.join(os.path.expanduser('~'), '.nuke', 'nukomfy_uistate.json')


# ---------------------------------------------------------------------------
# Window centering
# ---------------------------------------------------------------------------
def _screen_for_widget(widget):
    """Return the connected screen holding the largest portion of
    ``widget``, falling back to the screen under its center, then the
    primary screen. Only physically connected screens are considered, so
    a window can never be centered on a phantom monitor."""
    from Nukomfy.utils.qt_compat import QtCore, QtWidgets
    app = QtWidgets.QApplication
    screens = list(app.screens()) if hasattr(app, 'screens') else []
    if widget is not None and widget.isVisible() and screens:
        rect = None
        try:
            tl = widget.mapToGlobal(QtCore.QPoint(0, 0))
            rect = QtCore.QRect(tl, widget.size())
        except Exception:
            rect = None
        if rect is not None and rect.isValid():
            best, best_area = None, 0
            for s in screens:
                inter = s.geometry().intersected(rect)
                area = inter.width() * inter.height() if inter.isValid() else 0
                if area > best_area:
                    best, best_area = s, area
            if best is not None:
                return best
            if hasattr(app, 'screenAt'):
                try:
                    s = app.screenAt(rect.center())
                    if s is not None:
                        return s
                except Exception:
                    pass
    return app.primaryScreen()


def center_on_screen(window, reference=None):
    """Center ``window`` on the screen that holds ``reference`` (a visible
    widget). When ``reference`` is None, use the window's own visible
    parent if it has one, else Nuke's main window. Clamps the result
    inside the screen's available area so an oversized window never spills
    off a real monitor. Call after the final size is set; works before
    show() too (the move is respected when the window is first displayed).
    """
    from Nukomfy.utils.qt_compat import _nuke_main_window
    if reference is None:
        p = window.parentWidget() if hasattr(window, 'parentWidget') else None
        reference = p if (p is not None and p.isVisible()) else _nuke_main_window()
    screen = _screen_for_widget(reference)
    if screen is None:
        return
    avail = screen.availableGeometry()
    frame = window.frameGeometry()
    frame.moveCenter(avail.center())
    x = max(avail.left(), min(frame.left(), avail.right() - frame.width() + 1))
    y = max(avail.top(), min(frame.top(), avail.bottom() - frame.height() + 1))
    window.move(x, y)


def cap_to_screen(width, height=None, reference=None, ratio=0.9):
    """Clamp a desired (``width``, ``height``) to ``ratio`` of the available
    area of the screen holding ``reference`` (the dialog's parent), so the
    cap follows the monitor where the dialog appears rather than always the
    primary one. ``reference`` None falls back to Nuke's main window. Pass
    ``height`` None to cap width only; the height return is then None too."""
    from Nukomfy.utils.qt_compat import _nuke_main_window
    if reference is None:
        reference = _nuke_main_window()
    screen = _screen_for_widget(reference)
    if screen is None:
        return width, height
    avail = screen.availableGeometry()
    capped_w = min(width, int(avail.width() * ratio))
    capped_h = height if height is None else min(height, int(avail.height() * ratio))
    return capped_w, capped_h


def fit_to_screen(widget, fit_margin=0.98, target_margin=0.85):
    """Shrink a top-level widget to fit a small screen, proportionally, ONLY
    when its current size would not fit; no-op when it already fits within
    ``fit_margin`` of the available area - so a normal monitor (where the
    default always fits) is left byte-identical. When it overflows, scale
    down uniformly to ``target_margin`` of the available area: this keeps
    the default's aspect ratio and leaves a clear margin (the window opens
    noticeably smaller than the screen, not edge-to-edge), the body's scroll
    area covering the rest. Width never drops below the widget's own minimum
    width (avoids horizontal scroll); height may, with the minimum lowered
    to match, so the scroll area keeps the content reachable. Call AFTER the
    normal min/default size are set. Returns the scale applied (1.0 = none)."""
    from Nukomfy.utils.qt_compat import _nuke_main_window
    p = widget.parentWidget() if hasattr(widget, 'parentWidget') else None
    reference = p if (p is not None and p.isVisible()) else _nuke_main_window()
    screen = _screen_for_widget(reference)
    if screen is None:
        return 1.0
    avail = screen.availableGeometry()
    w, h = widget.width(), widget.height()
    if w <= avail.width() * fit_margin and h <= avail.height() * fit_margin:
        return 1.0
    scale = min(1.0,
                avail.width() * target_margin / w,
                avail.height() * target_margin / h)
    mn = widget.minimumSize()
    new_w = min(max(int(w * scale), mn.width()), avail.width())
    new_h = min(int(h * scale), avail.height())
    widget.setMinimumSize(min(mn.width(), avail.width()),
                          min(mn.height(), new_h))
    widget.resize(new_w, new_h)
    return scale


_DEFAULTS = {
    'library_panel': {
        'width': 1100, 'height': 740,
        'view_mode': 'grid',
        'active_sources': ['Local', 'Shared'],
        'active_tags': [],
        'scale': 0,
        'favorites_filter': False,
        'autoplay': True,
        'card_sort_key': 'name',
        'card_sort_dir': 'asc',
    },
    'submit_panel':        {'width': 1100, 'height': 610},
    'render_queue_panel':  {'width': 1440, 'height': 640},
    'machines_tab':          {},
    'rq_machine_table':      {},
    'rq_queue_table_v2':     {},
    'rq_history_table_v2':   {},
    # Machine Job Viewer (standalone window). Registered with no width/height
    # on purpose: the window centers itself on the Render Manager's screen at
    # first open, so restore_geometry must stay a no-op until a size is saved.
    'machine_jobs_viewer':        {},
    'rq_machine_viewer_table_v2': {},
    'machine_jobs_filter':        {},
    'myjobs_filter':              {},
    'submit_machine_table':  {},
    'myjobs_table_v2':       {},
    'ep_inputs_table':       {},
    'ep_outputs_table':      {},
    'ep_knobs_table':        {},
}


class _UIState:
    def __init__(self):
        self._state = {}
        self._load()

    def _load(self):
        import copy
        self._state = copy.deepcopy(_DEFAULTS)
        if os.path.isfile(_FILE):
            try:
                with open(_FILE, 'r', encoding='utf-8') as f:
                    on_disk = json.load(f)
                for key, val in on_disk.items():
                    if key in self._state and isinstance(val, dict):
                        self._state[key].update(val)
            except Exception as e:
                _log.warning('failed to load UI state: %s', e)

    def save(self):
        import Nukomfy.utils.fs_safe as fs_safe
        if not fs_safe.makedirs_silent(os.path.dirname(_FILE)):
            return
        tmp = _FILE + '.tmp'
        # UI state is non-critical and regenerable: a write failure (full
        # disk, revoked permission) must never propagate into the caller,
        # which is often a closeEvent or a UI action handler.
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, indent=2)
            fs_safe.atomic_replace(tmp, _FILE)
        except Exception as e:
            _log.warning('failed to save UI state: %s', e)
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def get(self, window_key):
        """Return the state dict for a window (always a copy of defaults + overrides)."""
        return dict(self._state.get(window_key, {}))

    def set(self, window_key, **kwargs):
        """Update one or more values for a window and persist."""
        if window_key not in self._state:
            self._state[window_key] = {}
        self._state[window_key].update(kwargs)
        self.save()

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def restore_geometry(self, window_key, widget, with_position=False,
                         fit=False):
        """Resize the widget from saved state.

        When ``with_position=True`` and the saved rect lands on an
        available screen, the position is clamped fully inside that
        screen's available area and applied with ``widget.move(x, y)``;
        otherwise the widget is centered on the active screen, so a first
        open or a position reset lands centered, not at Qt's default
        placement (which is off-centre, e.g. at OS scale != 100%).
        We deliberately use ``move`` (and not ``setGeometry``) because
        saved x/y come from ``widget.x()/y()``, which return the
        *frame* origin (including title bar). ``setGeometry`` would
        interpret them as the *client* origin, causing the frame to
        drift up by the title bar height on each close/reopen cycle.

        When ``fit=True`` the restored size is run through
        ``fit_to_screen`` (a no-op unless it overflows the screen), so a
        size saved on a larger monitor, or a default too big for a small
        screen, opens scaled to fit. It is idempotent: a size that already
        fits is left untouched, so reopening never shrinks it further.

        If the saved state is ``maximized``, the window is reopened
        maximized after its normal geometry is restored, so un-maximizing
        returns to the pre-maximize size (and position).
        """
        s = self.get(window_key)
        w, h = s.get('width'), s.get('height')
        if not (w and h):
            return
        rw, rh = int(w), int(h)
        widget.resize(rw, rh)
        if fit:
            fit_to_screen(widget)
            rw, rh = widget.width(), widget.height()
        if with_position:
            x, y = s.get('x'), s.get('y')
            rx = int(x) if x is not None else None
            ry = int(y) if y is not None else None
            if (rx is not None and ry is not None
                    and self._rect_on_any_screen(rx, ry, rw, rh)):
                rx, ry = self._clamp_rect_to_screen(rx, ry, rw, rh)
                widget.move(rx, ry)
            else:
                # No usable saved position (first open, after a position
                # reset, or saved off-screen): center on the active screen
                # instead of Qt's default placement, which lands off-centre
                # (e.g. at OS scale != 100%).
                center_on_screen(widget)
        # Maximize last: the resize/move above sets the normal geometry
        # Qt restores to when the user un-maximizes.
        if s.get('maximized'):
            from Nukomfy.utils.qt_compat import QtCore
            widget.setWindowState(widget.windowState() | QtCore.Qt.WindowMaximized)

    def save_geometry(self, window_key, widget, with_position=False):
        """Save current widget size and maximized state.

        If the widget is currently maximized, store its pre-maximize
        (normal) geometry so reopening restores to the size the user had
        before maximizing, plus a ``maximized`` flag so the window
        reopens maximized. When ``with_position=True``, x/y are
        persisted too.
        """
        maximized = widget.isMaximized()
        if maximized:
            g = widget.normalGeometry()
            w, h = g.width(), g.height()
            x, y = g.x(), g.y()
        else:
            w, h = widget.width(), widget.height()
            x, y = widget.x(), widget.y()
        payload = {'width': int(w), 'height': int(h), 'maximized': bool(maximized)}
        if with_position:
            payload['x'] = int(x)
            payload['y'] = int(y)
        self.set(window_key, **payload)

    @staticmethod
    def _rect_on_any_screen(x, y, w, h, min_visible=60):
        """True if at least ``min_visible`` px on each side of the rect
        overlap one of the available screens. The threshold avoids
        restoring a window almost entirely off-screen (e.g. after a
        monitor was unplugged or its layout changed)."""
        from Nukomfy.utils.qt_compat import QtCore, QtWidgets
        app = QtWidgets.QApplication.instance()
        if not app:
            return False
        rect = QtCore.QRect(x, y, max(w, 1), max(h, 1))
        for screen in app.screens():
            inter = screen.geometry().intersected(rect)
            if inter.width() >= min_visible and inter.height() >= min_visible:
                return True
        return False

    @staticmethod
    def _clamp_rect_to_screen(x, y, w, h):
        """Nudge a window rect fully inside the available area of the
        screen that holds the largest portion of it, so a saved position
        never reopens partly off-screen (e.g. after a scale or monitor
        layout change between sessions). Returns the clamped top-left
        ``(x, y)``; unchanged when no screen is available."""
        from Nukomfy.utils.qt_compat import QtCore, QtWidgets
        app = QtWidgets.QApplication.instance()
        if not app:
            return x, y
        rect = QtCore.QRect(x, y, max(w, 1), max(h, 1))
        best, best_area = None, 0
        for screen in app.screens():
            inter = screen.geometry().intersected(rect)
            area = inter.width() * inter.height() if inter.isValid() else 0
            if area > best_area:
                best, best_area = screen, area
        if best is None:
            best = app.primaryScreen()
        if best is None:
            return x, y
        avail = best.availableGeometry()
        cx = max(avail.left(), min(x, avail.right() - w + 1))
        cy = max(avail.top(), min(y, avail.bottom() - h + 1))
        return cx, cy

    def save_column_widths(self, key, table):
        """Save column widths for a QTableWidget."""
        h = table.horizontalHeader()
        widths = [h.sectionSize(i) for i in range(h.count())]
        self.set(key, column_widths=widths)

    def restore_column_widths(self, key, table):
        """Restore column widths for a QTableWidget.

        Signals are blocked during restore to avoid triggering the
        adjacent-column resize handler which would cascade and corrupt
        the saved proportions.
        """
        s = self.get(key)
        widths = s.get('column_widths', [])
        if not widths:
            return
        h = table.horizontalHeader()
        h.blockSignals(True)
        for i, w in enumerate(widths):
            if i < h.count() and w > 0:
                table.setColumnWidth(i, w)
        h.blockSignals(False)

    def reset(self):
        """Reset UI state to defaults, except per-panel content filters.

        Filters (``active_sources``, ``active_tags``, ``favorites_filter``)
        are preserved - those are reset via each panel's own Reset button
        (e.g. Library's "Reset Filters"). Everything else (sizes, positions,
        column widths, view mode, scale, autoplay) goes back to defaults."""
        PRESERVE_FILTER_KEYS = ('active_sources', 'active_tags',
                                'favorites_filter')
        import copy
        new_d = copy.deepcopy(_DEFAULTS)
        for key, val in self._state.items():
            if not isinstance(val, dict) or key not in new_d:
                continue
            for fk in PRESERVE_FILTER_KEYS:
                if fk in val:
                    new_d[key][fk] = val[fk]
        self._state = new_d
        self.save()

    def reset_window_positions(self):
        """Drop saved x/y for every window, leaving sizes / column widths /
        view modes / filters intact. Next panel open will appear at Qt's
        default position (centered on parent)."""
        for key, val in self._state.items():
            if isinstance(val, dict):
                val.pop('x', None)
                val.pop('y', None)
        self.save()


ui_state = _UIState()