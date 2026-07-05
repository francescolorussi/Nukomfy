"""Single source of truth for machine/job status labels, colors, and icons.

Consumed by render_queue_panel, submit_panel, and anywhere a status is shown.
"""

from Nukomfy.utils.qt_compat import QtWidgets
from Nukomfy.gui.icons import (
    icon_font, CIRCLE, CHECK_CIRCLE, HELP_OUTLINE, BLOCK, CANCEL)
from Nukomfy.gui._theme import (
    ERROR_COLOR, SUCCESS_COLOR, WARNING_STATUS, INFO_COLOR, UNAVAILABLE_COLOR,
)


# ---------------------------------------------------------------------------
# Machine status - keys match comfy_api.check_queue_status() output.
# ---------------------------------------------------------------------------
MACHINE_STATUS = {
    'idle':      ('Idle',      '#dedede',      CIRCLE),
    'rendering': ('Rendering', SUCCESS_COLOR,  CIRCLE),
    'queued':    ('Queued',    WARNING_STATUS, CIRCLE),
    'offline':   ('Offline',   '#606060',      CIRCLE),
}


# ---------------------------------------------------------------------------
# Job status - canonical keys = ComfyUI native taxonomy
# (ComfyUI comfy_execution/jobs.py):
#   pending / running / completed / failed / cancelled
# ComfyUI's own keys (in_progress/interrupted) are normalised to these
# canonical keys upstream in `_parse_job_detail`.
# Display labels are decoupled from the canonical keys: 'pending' shows as
# "Queued" to match the Render Manager sub-tab "Queue" and MACHINE_STATUS
# ['queued'], using industry-standard pipeline terminology.
# ---------------------------------------------------------------------------
JOB_STATUS = {
    'pending':   ('Queued',    '#dedede',     CIRCLE),
    'running':   ('Running',   SUCCESS_COLOR, CIRCLE),
    'completed': ('Completed', INFO_COLOR,    CHECK_CIRCLE),
    'failed':    ('Failed',    ERROR_COLOR,   CANCEL),
    'cancelled': ('Cancelled', '#a0a0a0',     BLOCK),
    # 'unknown' is transient: the machine is currently offline / no fetch
    # has resolved the pid yet (MyJobs Active). The next successful fetch
    # updates the status to whatever the server reports
    # (running/pending/terminal). A definitively lost job is frozen by
    # persist_as_lost as terminal `failed` (server 404 or
    # `lost_job_timeout_days` elapsed), NOT `unknown`.
    # Visually identical to the _FALLBACK below so every undetermined
    # row reads the same `? Unknown` to the user.
    'unknown':   ('Unknown',   '#888',    HELP_OUTLINE),
}

# Fallback: shown for entries with no status_str yet (fresh submit
# awaiting first reconcile, or transient `checking`/`not_in_queue`
# state in MyJobs). Visually identical to the 'unknown' entry above
# so the user always reads the same `? Unknown` regardless of why the
# state isn't knowable yet - placement (Active vs History) and
# subsequent fetches resolve the temporal meaning.
_FALLBACK = ('Unknown', '#888', HELP_OUTLINE)


# In-flight action states (client-derived, not server status_str): a job
# being aborted - still running while the server honours the interrupt - or
# a queued job being removed. Shown in neutral grey with no icon, kept
# identical to the click-time greyed status cell, until the server-side
# state takes over. "Aborting…" rides the `nfy_aborting` flag and survives a
# panel reopen / Nuke restart; "Removing…" is optimistic-only (a removed
# pending job is just deleted - there is no server "removing" state).
INFLIGHT_COLOR = '#777'
ABORTING_LABEL = 'Aborting…'
REMOVING_LABEL = 'Removing…'


def render_machine_status(key, availability=None):
    """Return (label, color_hex, icon_char) for a machine status key.

    `availability` ('available' / 'unavailable' / None) overrides the
    label and color for any non-offline status:
      - offline: always wins (machine unreachable, the Unavailable flag is
        moot until ping resumes).
      - any other status + unavailable: returns
        ('Unavailable', UNAVAILABLE_COLOR, CIRCLE) - single fixed label
        keeps the Status column narrow. The active queue state (running
        job, pending count) is still surfaced by the Render Manager
        sub-table for callers who need it.
      - None or 'available': unchanged behaviour.
    """
    if key == 'offline':
        return MACHINE_STATUS['offline']
    if availability == 'unavailable':
        return ('Unavailable', UNAVAILABLE_COLOR, CIRCLE)
    return MACHINE_STATUS.get(key, _FALLBACK)


def render_job_status(key):
    """Return (label, color_hex, icon_char) for a job status key.

    Callers pass canonical server keys (running/pending/completed/failed/
    cancelled); normalisation of upstream ComfyUI keys lives at the source
    in `_parse_job_detail`. Two client-derived keys, 'aborting' and
    'removing' (a job being torn down / dequeued), render as "Aborting…" /
    "Removing…" so the Job dialog header matches the greyed row instead of
    falling back to "Running"/"Queued".
    """
    if key == 'aborting':
        return (ABORTING_LABEL, INFLIGHT_COLOR, CIRCLE)
    if key == 'removing':
        return (REMOVING_LABEL, INFLIGHT_COLOR, CIRCLE)
    return JOB_STATUS.get(key or '', _FALLBACK)


# ---------------------------------------------------------------------------
# Relative time
# ---------------------------------------------------------------------------
def format_relative_time(timestamp_secs):
    """Return 'just now', 'Xs ago', 'Xm ago', 'Xh ago', 'Xd ago'.

    Accepts a unix timestamp (seconds). Used as tooltip annotation
    alongside the absolute datetime in Queue/History tables.
    """
    if not timestamp_secs:
        return ''
    try:
        import time
        delta = time.time() - float(timestamp_secs)
    except (TypeError, ValueError):
        return ''
    if delta < 0:
        return 'just now'
    if delta < 10:
        return 'just now'
    if delta < 60:
        return '{}s ago'.format(int(delta))
    if delta < 3600:
        return '{}m ago'.format(int(delta / 60))
    if delta < 86400:
        return '{}h ago'.format(int(delta / 3600))
    return '{}d ago'.format(int(delta / 86400))


# ---------------------------------------------------------------------------
# Connectivity status - Settings -> Machines binary dot (reachable/unreachable)
# ---------------------------------------------------------------------------
# Distinct from MACHINE_STATUS: Settings is a reachability test. Red signals
# a confirmed failure (server unreachable) - louder than MACHINE_STATUS grey,
# because Settings is where the user acts on connection problems.
def render_connectivity_status(online):
    if online is None:
        return ('Disabled', '#555', CIRCLE)
    if online:
        return ('Online', SUCCESS_COLOR, CIRCLE)
    return ('Offline', ERROR_COLOR, CIRCLE)


# ---------------------------------------------------------------------------
# Status cell widget (Material icon + colored label)
# ---------------------------------------------------------------------------
def _make_status_cell(icon_char, label, color):
    """Build a QWidget with a Material icon + label text, both colored.

    Labels are attached as `_icon_lbl` / `_text_lbl` attributes so the cell
    can be updated in-place via `_update_status_cell` instead of replacing
    the whole widget on each refresh tick (avoids flicker).
    """
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(8, 0, 4, 0)
    lay.setSpacing(6)
    icon_lbl = QtWidgets.QLabel(icon_char or '')
    icon_lbl.setFont(icon_font(12))
    icon_lbl.setStyleSheet('color:{};background:transparent;'.format(color))
    text_lbl = QtWidgets.QLabel(label or '')
    text_lbl.setStyleSheet('color:{};background:transparent;'.format(color))
    lay.addWidget(icon_lbl)
    lay.addWidget(text_lbl)
    lay.addStretch(1)
    w._icon_lbl = icon_lbl
    w._text_lbl = text_lbl
    return w


def _update_status_cell(widget, icon_char, label, color):
    """Update an existing status cell in-place. Returns True if updated,
    False if the widget is missing the expected attributes (caller should
    fall back to setCellWidget)."""
    if not widget or not hasattr(widget, '_icon_lbl'):
        return False
    style = 'color:{};background:transparent;'.format(color)
    widget._icon_lbl.setText(icon_char or '')
    widget._icon_lbl.setStyleSheet(style)
    widget._text_lbl.setText(label or '')
    widget._text_lbl.setStyleSheet(style)
    return True
