"""WebSocket progress client.

Optional dependency on `websocket-client`. When absent, `AVAILABLE` is
False and callers skip the progress/tooltip features entirely.

One `ProgressMonitor` instance per ComfyUI machine. It opens a persistent
WebSocket with a per-process `clientId` (see `core.identity.ws_session_id`)
and listens for the Suite's broadcast progress events:

  * `nukomfy.progress`  -> {prompt_id, fraction, tooltip, state}
  * `nukomfy.lifecycle` -> {prompt_id, event}

The Suite computes the weighted fraction server-side and broadcasts it to
every connected client, so the monitor is a thin relay: it parses the event
and emits a Qt signal. It keeps no per-job state - any client sees the live
progress of any running job on the machine, regardless of who submitted it.

Note: `client_id` / `clientId` in this module is purely the WebSocket session
identifier - NOT a user identity. The user identity travels in `extra_data`
as `nfy_submitted_by` (username) and `nfy_submitter_host` (hostname).

Qt signals are emitted from the background thread; connect with the default
auto-connection type so slots run on the receiver's owning thread.
"""

import hashlib
import json
import threading
import time
import urllib.parse

from Nukomfy.utils.qt_compat import QtCore

try:
    import websocket  # websocket-client package
    AVAILABLE = True
    # Silence the library's own logger - our on_error handler already
    # swallows errors, but websocket-client logs connection failures
    # separately via stdlib logging (e.g. the pre-warm retry loop against
    # offline machines spams "WinError 10013 ... goodbye" on Windows). We
    # handle reconnect/backoff ourselves, no user-facing value in the noise.
    import logging as _logging
    _logging.getLogger('websocket').setLevel(_logging.CRITICAL)
except ImportError:
    websocket = None
    AVAILABLE = False


# WebSocket keepalive heartbeat and reconnect backoff ceiling (seconds).
_WS_PING_INTERVAL_SEC = 30
_WS_PING_TIMEOUT_SEC = 10
_RECONNECT_BACKOFF_MAX_SEC = 30.0


class ProgressMonitor(QtCore.QObject):
    """WebSocket listener for a single ComfyUI machine.

    Subscribes to the Suite's broadcast progress events and re-emits them as
    Qt signals for any job running on the machine (broadcast, not isolated to
    the submitter).
    """

    # prompt_id, global_fraction (0.0..1.0), tooltip_text
    progress = QtCore.Signal(str, float, str)
    # prompt_id, event in {'start', 'success', 'error', 'interrupted'}
    lifecycle = QtCore.Signal(str, str)

    def __init__(self, machine_url, client_id, parent=None):
        super(ProgressMonitor, self).__init__(parent)
        self._machine_url = machine_url
        self._client_id = client_id
        self._ws = None
        self._thread = None
        self._stopping = False
        self._connected = False

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        if not AVAILABLE or self._thread is not None:
            return
        self._stopping = False
        thread_tag = hashlib.sha1(
            (self._machine_url or '').encode('utf-8')).hexdigest()[:8]
        self._thread = threading.Thread(
            target=self._run, name='ws-' + thread_tag, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # -- internals ----------------------------------------------------------

    def _ws_url(self):
        base = self._machine_url.rstrip('/')
        if base.startswith('http://'):
            base = 'ws://' + base[len('http://'):]
        elif base.startswith('https://'):
            base = 'wss://' + base[len('https://'):]
        return '{}/ws?clientId={}'.format(
            base, urllib.parse.quote(self._client_id, safe=''))

    def _run(self):
        """Reconnect loop. Backoff capped at 30s. Exits on stop().

        websocket-client's run_forever() swallows a refused connection and
        returns normally (it does not raise), so a dropped live session and an
        offline machine look identical on return. We distinguish them via the
        `_connected` flag (set by on_open): a session that actually opened
        resets the backoff for a fast reconnect on a transient drop; an attempt
        that never opened grows the backoff so offline-but-enabled machines are
        not hammered once per second for the whole process lifetime.
        """
        backoff = 1.0
        while not self._stopping:
            self._connected = False
            try:
                self._ws = websocket.WebSocketApp(
                    self._ws_url(),
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, err: None,
                    on_close=lambda ws, code, reason: None,
                )
                self._ws.run_forever(ping_interval=_WS_PING_INTERVAL_SEC, ping_timeout=_WS_PING_TIMEOUT_SEC)
            except Exception:
                pass
            if self._stopping:
                break
            backoff = 1.0 if self._connected else min(backoff * 2.0, _RECONNECT_BACKOFF_MAX_SEC)
            time.sleep(backoff)

    def _on_open(self, ws):
        self._connected = True

    def _on_message(self, ws, message):
        if isinstance(message, (bytes, bytearray)):
            return
        try:
            msg = json.loads(message)
        except Exception:
            return
        etype = msg.get('type')
        data = msg.get('data') or {}
        prompt_id = data.get('prompt_id') or ''
        if not prompt_id:
            return
        if etype == 'nukomfy.progress':
            try:
                fraction = float(data.get('fraction') or 0.0)
            except (TypeError, ValueError):
                fraction = 0.0
            self.progress.emit(prompt_id, fraction, data.get('tooltip') or '')
        elif etype == 'nukomfy.lifecycle':
            self.lifecycle.emit(prompt_id, data.get('event') or '')


# ---------------------------------------------------------------------------
# Module-level singleton manager - one ProgressMonitor per machine_url.
# ---------------------------------------------------------------------------
class _MonitorManager(object):
    """Lazily spawns and owns `ProgressMonitor` instances across the session.

    Monitors are created on first access (submit or panel open) and live for
    the lifetime of the Nuke process. Threads are daemon so they die with
    the interpreter - no explicit shutdown path required.
    """

    def __init__(self):
        self._monitors = {}
        self._lock = threading.Lock()

    def monitor_for(self, machine_url, client_id):
        """Return (creating if needed) the ProgressMonitor for this machine.

        Returns None when the optional `websocket-client` package is absent
        - callers should treat that as "no live progress, skip integration".
        """
        if not AVAILABLE or not machine_url or not client_id:
            return None
        with self._lock:
            pm = self._monitors.get(machine_url)
            if pm is None:
                pm = ProgressMonitor(machine_url, client_id)
                pm.start()
                self._monitors[machine_url] = pm
            return pm


_manager = None


def get_manager():
    """Return the process-wide MonitorManager singleton."""
    global _manager
    if _manager is None:
        _manager = _MonitorManager()
    return _manager
