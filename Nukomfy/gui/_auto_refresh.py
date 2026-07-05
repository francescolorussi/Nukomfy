"""Shared refresh-button timing helpers.

Three pieces every refresh button reuses:
- `AutoRefreshTimer`: a 1-second countdown bound to one or more
  QPushButtons. The idle label (e.g. 'Update All') shows when off; while
  running it is suffixed with '(N)' counting down, and at zero the
  supplied callback fires. Toggling `settings.auto_refresh_enabled` off
  stops it and restores the idle label. Used by the Render Manager
  'Update All' button (mirrored across the Render Manager and MyJobs
  tabs). Deliberately dumb: the caller owns `reset()` and `stop()`.
- `RefreshCycle`: drives a non-blocking refresh button - busy until the
  fetch finishes or a soft deadline elapses, so a slow/offline machine
  never UI-blocks the user past the deadline.
- `busy_mark()` / `schedule_after_min_visible()`: a min-visible anti-
  flicker floor for the per-machine refresh icons.
"""

import time as _time

from Nukomfy.utils.qt_compat import QtCore
from Nukomfy.core.settings import settings


# Minimum time a refresh button stays greyed/busy so a fast fetch is
# still perceptible. Measured from when the busy state first shows (via
# `busy_mark()`), NOT a tail tacked on after completion: a normal network
# fetch already exceeds this, so it adds zero wait in practice. The floor
# only engages if a fetch returns in under this many ms, preventing an
# imperceptible flash. Single source of truth for every refresh button
# (Render Manager Update All + per-machine, Settings > Machines).
MIN_BUSY_VISIBLE_MS = 400


def busy_mark():
    """Record when a button's busy/greyed state first shows. Pair the
    returned stamp with `schedule_after_min_visible`."""
    return _time.monotonic()


def schedule_after_min_visible(start, callback):
    """Run *callback* once the busy state marked by *start* has been
    visible for at least `MIN_BUSY_VISIBLE_MS`. Fires on the next
    event-loop pass when the floor is already met (the common case for a
    real network fetch, so no extra wait). A None *start* fires now."""
    if start is None:
        remaining = 0
    else:
        elapsed_ms = (_time.monotonic() - start) * 1000.0
        remaining = max(0, int(MIN_BUSY_VISIBLE_MS - elapsed_ms))
    QtCore.QTimer.singleShot(remaining, callback)


# Soft deadline: the refresh button returns to its ready state after this
# long even if slow/offline machines are still being polled in the
# background. Decoupled from the per-machine network timeout
# (comfy_api.STATUS_PROBE_TIMEOUT) on purpose: the button frees fast, the
# worker keeps polling, and each machine's row updates when it answers.
SOFT_READY_MS = 2000


class RefreshCycle:
    """Drives a non-blocking refresh button (stale-while-revalidate).

    The button stays busy/disabled until the fetch finishes OR the soft
    deadline (SOFT_READY_MS) elapses - whichever comes first, but never
    before MIN_BUSY_VISIBLE_MS (anti-flicker). So a slow/offline machine
    never UI-blocks the user past the soft deadline: at that point the
    button returns to ready while the worker keeps polling in the
    background, and each machine's row updates when it answers.

    The owner supplies one callback:
      on_ready() - restore the ready state (re-enable the button, restore
                   its idle label / countdown). Fired exactly once per
                   cycle. Must be defensive: the panel may close before it
                   fires.

    Generation-guarded plus a one-shot `_settled` flag, so a timer from a
    cycle superseded by a cancel-restart, or a late `finish()` after the
    soft deadline already settled, is ignored."""

    def __init__(self, on_ready):
        self._on_ready = on_ready
        self._gen = 0
        self._start = None
        self._settled = False

    def begin(self):
        """Call when a refresh starts (button just shown busy)."""
        self._gen += 1
        self._start = _time.monotonic()
        self._settled = False
        gen = self._gen
        # Free the button at the soft deadline even if the worker is still
        # polling slow/offline machines in the background.
        QtCore.QTimer.singleShot(SOFT_READY_MS, lambda: self._settle(gen))

    def finish(self):
        """Call from the worker's finished handler: settle now (respecting
        the anti-flicker floor) unless the soft deadline already did."""
        gen = self._gen
        if self._start is None:
            self._settle(gen)
            return
        elapsed_ms = (_time.monotonic() - self._start) * 1000.0
        remaining = max(0, int(MIN_BUSY_VISIBLE_MS - elapsed_ms))
        if remaining:
            QtCore.QTimer.singleShot(remaining, lambda: self._settle(gen))
        else:
            self._settle(gen)

    def _settle(self, gen):
        if gen == self._gen and not self._settled:
            self._settled = True
            self._on_ready()


class AutoRefreshTimer(QtCore.QObject):
    """Countdown timer that updates *buttons*' text and calls
    *on_timeout* when it reaches zero.

    Accepts either a single QPushButton or a list of buttons - useful
    when the same logical control is rendered in multiple tabs
    (Render Manager + MyJobs). All bound buttons share one countdown
    and label update; callers pick any one to wire to `clicked`.

    Reads interval from `settings.auto_refresh_interval` on each
    `reset()`, and reads the enabled gate from
    `settings.auto_refresh_enabled` on both `reset()` and every tick -
    so a settings toggle during an active countdown takes effect on
    the next tick without any extra wiring from the caller.
    """

    def __init__(self, buttons, idle_text, on_timeout, parent=None):
        super().__init__(parent)
        self._buttons = (list(buttons)
                         if isinstance(buttons, (list, tuple))
                         else [buttons])
        self._idle_text = idle_text
        self._on_timeout = on_timeout
        self._countdown = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def reset(self):
        """Restart the countdown from `settings.auto_refresh_interval`.
        No-op (and resets each button's label) when auto-refresh is off."""
        if settings.auto_refresh_enabled:
            self._countdown = settings.auto_refresh_interval
            self._update_button()
            if not self._timer.isActive():
                self._timer.start()
        else:
            self.stop()

    def stop(self):
        """Halt the countdown and restore the idle label on every button."""
        self._timer.stop()
        self._countdown = 0
        for btn in self._buttons:
            try:
                btn.setText(self._idle_text)
            except RuntimeError:
                pass  # button widget already deleted

    def _tick(self):
        if not settings.auto_refresh_enabled:
            self.stop()
            return
        self._countdown -= 1
        if self._countdown <= 0:
            self._timer.stop()
            self._on_timeout()
        else:
            self._update_button()

    def _update_button(self):
        text = '{} ({})'.format(self._idle_text, self._countdown)
        for btn in self._buttons:
            try:
                btn.setText(text)
            except RuntimeError:
                pass  # button widget already deleted
