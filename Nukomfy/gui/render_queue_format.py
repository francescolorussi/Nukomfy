"""Render Manager format and render helpers.

Format helpers (date, duration, log messages, HTML field renderers),
detail section composers (5 collapsible sections of _JobDialog), cell
builders for sub-tables (Queue/History/MyJobs), shared column index
constants, and reusable widget primitives (_HatchedProgressCell,
_CollapsibleSection, _LeftElideDelegate, _WorkflowApiTab).

Internal module - public API of Render Manager surfaced via
render_queue_panel.py (`show_render_queue`).
"""

import datetime as _datetime
import html as _html
import json as _json
import logging

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui._fields import NukomfyPlainTextEdit, NukomfyTextEdit

import Nukomfy.data.submit_history as submit_history
from Nukomfy.data.submit_history import resolve_ends_from_disk as _resolve_ends_from_disk
from Nukomfy.gui.ui_state import ui_state, cap_to_screen
from Nukomfy.gui.icons import (icon_font, REFRESH,
                               CHECK_CIRCLE, ERROR, WARNING, WARNING_AMBER,
                               CIRCLE, PLAY_ARROW, BLOCK, CANCEL)
from Nukomfy.gui._theme import (
    ERROR_COLOR, WARNING_STATUS, SUCCESS_COLOR,
)
from Nukomfy.gui._table_utils import _proportional_fit, _install_absorber
from Nukomfy.gui.status_display import (
    render_job_status, format_relative_time, _make_status_cell)

_log = logging.getLogger(__name__)

# Display format for submission/job timestamps.
_DT_FORMAT = '%Y-%m-%d %H:%M:%S'
# Bytes per gigabyte, for humanizing VRAM/RAM sizes.
_BYTES_PER_GB = 1024 ** 3


def detail_doc_font(px=11):
    """Nuke's UI font (Preferences > Appearance > UIFont) at `px` pixels, so the
    Detail dialog text matches the rest of Nuke's UI on any OS with no hardcoded
    font name. Read from the UIFont knob, falling back to the Qt application font
    then Qt's default so a missing font can never break the dialog."""
    family = ''
    try:
        import nuke
        family = (nuke.toNode('preferences')['UIFont'].value() or '').strip()
    except Exception:
        family = ''
    if family:
        f = QtGui.QFont(family)
    else:
        try:
            f = QtGui.QFont(QtWidgets.QApplication.font())
        except Exception:
            f = QtGui.QFont()
    f.setPixelSize(px)
    return f


# URLs of machines seen as hidden in the current session. Filled by
# `_resolve_machine` whenever a hidden machine is looked up in the
# manager; lets us treat URLs of admin-removed-hidden machines as still
# hidden during the session (privacy), without leaking them after a
# restart (cache is in-memory only by design).
_LAST_KNOWN_HIDDEN_URLS = set()


def _resolve_machine(entry):
    """Resolve a job entry to (name, url, status).

    status is one of:
      - 'configured_visible'  : machine in manager, hidden_url=False
      - 'configured_hidden'   : machine in manager, hidden_url=True
      - 'removed_hidden'      : not in manager but URL was seen hidden
                                in this session (privacy preserved)
      - 'removed_visible'     : not in manager, never hidden in session

    Match strategy:
      1. Exact match on (name + url): safest discriminator, always
         preferred. Returns the manager's live flag (hidden or visible).
      2. URL match where ALL matching machines are visible: assume a
         rename of a visible entry (the job was on a visible machine
         that has since been renamed). Use the first visible match.
      3. URL match where at least one matching machine is hidden but
         no exact (name + url) match was found: treat the entry as
         orphan with the snapshot DB name, status `removed_hidden`.
         This avoids two leaks: (a) revealing the hidden machine's
         name via a job that belonged to a visible co-tenant; (b)
         revealing that the hidden machine shares a URL with a
         visible one by relabelling a visible job as the hidden one.
      4. No URL match: orphan with the snapshot DB name.
    """
    url = (entry.get('nfy_machine_url') or '').rstrip('/')
    db_name = entry.get('nfy_machine_name') or ''
    db_hidden = bool(entry.get('nfy_machine_hidden_url'))
    try:
        from Nukomfy.client.machines import machine_manager
        machines = list(machine_manager.machines)
    except Exception:
        machines = []

    # `db_hidden` is the source of truth for display status: a log is
    # an immutable record of what was real at submit. The current
    # manager state only contributes the live name (so dedup-induced
    # renames propagate) and the manager presence flag (configured vs
    # removed). It cannot flip a visible record to hidden or vice
    # versa retroactively.
    if db_hidden:
        if url:
            _LAST_KNOWN_HIDDEN_URLS.add(url)
        for m in machines:
            mu = (m.url or '').rstrip('/')
            if mu == url and (m.name or '') == db_name:
                return (m.name, None, 'configured_hidden')
        return (db_name, None, 'removed_hidden')

    url_matches = [m for m in machines
                   if url and (m.url or '').rstrip('/') == url]

    # 1. Exact (name + url) match. Status stays visible (db_hidden is
    # False here) even if the machine has since been marked hidden.
    for m in url_matches:
        if (m.name or '') == db_name:
            return (m.name, url, 'configured_visible')

    # 2. Rename of a visible machine: URL match (no exact name match).
    # Pick the first by URL. The historic log was visible, keep it so.
    if url_matches:
        return (url_matches[0].name, url, 'configured_visible')

    # 3. Orphan: URL not in manager. Visible at submit -> surface the
    # snapshot URL as-is: the history record is a log.
    return (db_name, url or None, 'removed_visible')


# Epoch values above this are milliseconds, not seconds (heuristic split).
_EPOCH_MS_THRESHOLD = 1e12


def _to_epoch_seconds(ts):
    """Coerce a raw timestamp (epoch seconds, or ms above the threshold) to
    float seconds. Returns 0.0 for missing, non-numeric, or zero input."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return 0.0
    if not ts:
        return 0.0
    if ts > _EPOCH_MS_THRESHOLD:
        ts = ts / 1000
    return ts


def _fmt_epoch(ts, fallback):
    """Format an epoch (seconds, or ms above the threshold) as an absolute
    date string, returning *fallback* for missing, non-numeric, or
    out-of-range values. A corrupt server timestamp (negative, absurdly
    large, or a non-numeric string) otherwise raises OverflowError/OSError/
    ValueError from fromtimestamp and crashes the whole Detail/Log render."""
    ts = _to_epoch_seconds(ts)
    if not ts:
        return fallback
    try:
        return _datetime.datetime.fromtimestamp(ts).strftime(_DT_FORMAT)
    except (OverflowError, OSError, ValueError):
        return fallback


def _safe_int(value, default=1):
    """int() that returns *default* for non-numeric input. Raw server-queue
    data can carry a non-numeric batch count/index that would otherwise
    raise ValueError and abort the Render section."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_sent(entry):
    """Resolve a job's 'Sent' time as (display_str, epoch_seconds).

    Single source of truth: the client submit time (`nfy_sent_at`, local
    ISO written at submit). The server epoch (`create_time`) is a fallback
    only for jobs submitted outside Nukomfy, which carry no `nfy_sent_at`.
    Anchoring every view to the one client clock keeps the "Sent" column
    consistent even when the render machine's clock differs from the
    client's. `epoch` (0.0 when unknown) feeds the relative-time tooltip;
    `display_str` is '-' when nothing is known.
    """
    sent_raw = entry.get('nfy_sent_at', '') if entry else ''
    if sent_raw:
        try:
            dt = _datetime.datetime.fromisoformat(sent_raw)
            return dt.strftime(_DT_FORMAT), dt.timestamp()
        except (ValueError, TypeError):
            return sent_raw, 0.0
    ts = _to_epoch_seconds(entry.get('create_time', 0)) if entry else 0.0
    return _fmt_epoch(ts, '-'), ts


def _format_duration(seconds):
    """Format duration in seconds to human-readable string. Returns
    plain ASCII '-' when not yet known (consistent with the missing-
    value placeholder used across the Detail dialog)."""
    if not seconds or seconds <= 0:
        return '-'
    if seconds < 60:
        return '{}s'.format(int(seconds))
    m = int(seconds / 60)
    s = int(seconds % 60)
    if m < 60:
        return '{}m {}s'.format(m, s)
    h = int(m / 60)
    m = m % 60
    return '{}h {}m'.format(h, m)
def _format_range_label(entry):
    """Render a single range entry as 'start-end' (or 'start-?' if unresolved)."""
    if not isinstance(entry, dict):
        return ''
    s = entry.get('start')
    f = entry.get('end')
    s_str = '?' if s is None else str(s)
    f_str = '?' if f is None else str(f)
    return '{}-{}'.format(s_str, f_str)

def _render_ranges_lines(ranges, label_singular, label_plural,
                         output_paths=None):
    """Build HTML lines for a ranges block.

    - 1 entry    -> single inline line:  "<b>label_singular:</b>  1-10"
    - 2+ entries -> header + indented rows: "<b>label_plural:</b>"
                                            "  name: 1-10"
                                            "  name: 5-?"

    If *output_paths* is provided and aligned with *ranges*, Range entries
    with end=None will have their end derived by globbing the disk.
    """
    if not ranges or not isinstance(ranges, list):
        return []
    resolved = (_resolve_ends_from_disk(ranges, output_paths)
                if output_paths else ranges)
    valid = [e for e in resolved if isinstance(e, dict)]
    if not valid:
        return []
    lines = []
    if len(valid) == 1:
        lines.append('<b>{}:</b>  {}'.format(
            label_singular, _format_range_label(valid[0])))
    else:
        lines.append('<b>{}:</b>'.format(label_plural))
        for e in valid:
            name = e.get('name', '?')
            lines.append('&nbsp;&nbsp;{}: {}'.format(
                name, _format_range_label(e)))
    return lines

def _clean_output_label(name):
    """Strip trailing 'File'/'file' from a gizmo output knob label."""
    if not name:
        return ''
    s = str(name)
    for suffix in ('File', 'file'):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    return s.rstrip('_ -')

def _centered_cell(button):
    """Wrap an icon-only button in a centered cell widget.

    Stretch factors are 1:2 (left:right): QTableWidget draws a 1px
    gridline at each cell's right edge that visually overlaps the
    rightmost pixel of breathing room. With column = button+3, the
    1:2 stretch yields 1px left, 2px right - of which 1px is consumed
    by the gridline, leaving a balanced 1px visible breathing on each
    side regardless of DPI scale.
    """
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    lay.addWidget(button)
    lay.addStretch(2)
    return w

_SCOPE_LABEL = {'workflow': 'workflow', 'global': 'global'}


def _render_input_caches_lines(input_caches):
    """Build HTML lines for the per-input cache directories.

    - 0 entries -> []                                              (dash row)
    - 1 entry   -> '<b>Input cache:</b>  /path'
    - 2+        -> header + indented '<i>name</i>: /path' rows.

    Mirrors the (output_paths / write_templates) pattern. Per-input
    label is derived from the path basename - the parent of the 16-hex
    fingerprint folder is the input name set by
    `_build_output_dir(scope_name, input_name, fp_segment)`.
    """
    if not input_caches:
        return []
    if not isinstance(input_caches, list):
        return []
    paths = [p for p in input_caches if p]
    if not paths:
        return []
    if len(paths) == 1:
        return ['<b>Input cache:</b>  {}'.format(paths[0])]
    lines = ['<b>Input caches:</b>']
    for p in paths:
        norm = p.replace('\\', '/').rstrip('/')
        # Path layout: <root>/<user>/<project>/<workflow>/<input>/<cache_key>
        # Drop the fingerprint and use its parent as the human label.
        parts = norm.rsplit('/', 2)
        label = parts[-2] if len(parts) >= 2 else 'input'
        lines.append('&nbsp;&nbsp;{}: {}'.format(label, p))
    return lines


def _render_write_templates_lines(write_templates):
    """Build HTML lines for the per-input write template mapping.

    - 0 entries -> []                                             (dash row)
    - 1 entry   -> '<b>Write template:</b>  tpl.nk (scope)'
    - 2+        -> header + indented '<i>input</i>: tpl.nk (scope)' rows.

    Mirrors :func:`_render_output_paths_lines` so the section reads
    consistently (single value inline, multi value as nested list).
    """
    if not write_templates:
        return []
    def _fmt_one(entry):
        tpl = (entry.get('template') or '').strip() or '?'
        # Strip .nk for display - Workflow Creator combo shows 'png' not
        # 'png.nk', keep the audit trail consistent.
        if tpl.lower().endswith('.nk'):
            tpl = tpl[:-3]
        scope_key = (entry.get('scope') or '').lower()
        scope = _SCOPE_LABEL.get(scope_key, scope_key or '?')
        base = '{} ({})'.format(tpl, scope)
        if entry.get('missing'):
            # The configured template was not found at gizmo build time and
            # a default Write node was used instead. Flag the substitution
            # so the audit trail does not read as if the template applied.
            # Material warning icon in-app; material_to_utf swaps it for a
            # plain glyph on copy / save.
            return ('{} Error: {} not found. '
                    'Used a default Write node.').format(
                        _icon_html(WARNING_AMBER, WARNING_STATUS), base)
        return base
    if len(write_templates) == 1:
        return ['<b>Write template:</b>  {}'.format(
            _fmt_one(write_templates[0]))]
    lines = ['<b>Write templates:</b>']
    for entry in write_templates:
        label = (entry.get('input') or '').strip() or 'input'
        lines.append('&nbsp;&nbsp;{}: {}'.format(label, _fmt_one(entry)))
    return lines


def _render_output_paths_lines(output_paths, names=None):
    """Build HTML lines for output paths.

    - 0 entries -> []
    - 1 entry   -> '<b>Output Path:</b>  /path' (no label)
    - 2+        -> header + indented '<i>name</i>: /path' rows. Labels come
                  from *names* (aligned list), with 'File' suffix stripped.
    """
    if not output_paths:
        return []
    if len(output_paths) == 1:
        return ['<b>Output Path:</b>  {}'.format(output_paths[0])]
    lines = ['<b>Output Paths:</b>']
    for idx, p in enumerate(output_paths):
        raw = (names[idx] if names and idx < len(names) else '') or ''
        label = _clean_output_label(raw) or 'output {}'.format(idx + 1)
        lines.append('&nbsp;&nbsp;{}: {}'.format(label, p))
    return lines


def _render_seeds_line(seeds):
    """Build HTML line for seeds.

    - empty -> None
    - 1 seed -> '<b>Seed:</b>  value' (no key label)
    - 2+ -> '<b>Seeds:</b>  k1: v1 | k2: v2'
    """
    if not seeds:
        return None
    if len(seeds) == 1:
        val = next(iter(seeds.values()))
        return '<b>Seed:</b>  {}'.format(val)
    return '<b>Seeds:</b>  {}'.format(
        ' | '.join('{}: {}'.format(k, v) for k, v in seeds.items()))


# Map execution-log event -> (Material Icon codepoint, colour). Rendered in
# the Material Icons font for crisp in-app icons. On copy / save the text is
# run through `material_to_utf` so the PUA codepoints become portable Unicode
# glyphs (the icon font is unavailable outside Nuke).
_LOG_ICON_FOR_EVENT = {
    'execution_start':       (PLAY_ARROW,   SUCCESS_COLOR),
    'execution_cached':      (REFRESH,      '#888'),
    'executing':             (CIRCLE,       '#dedede'),
    'execution_success':     (CHECK_CIRCLE, SUCCESS_COLOR),
    'execution_error':       (ERROR,        ERROR_COLOR),
    'execution_interrupted': (WARNING,      WARNING_STATUS),
}

# Material Icons PUA codepoint -> portable Unicode glyph. Applied when Log /
# Detail text leaves Nuke (copy / save): the icon font renders as tofu in any
# other app, so we silently swap to the equivalent Unicode glyph (the same
# glyphs the Suite's web log uses).
_MATERIAL_TO_UTF = {
    PLAY_ARROW: '▶', REFRESH: '↻', CIRCLE: '●',
    CHECK_CIRCLE: '✔', ERROR: '✖', WARNING: '⚠', WARNING_AMBER: '⚠',
    BLOCK: '⊘', CANCEL: '✖',
}


def material_to_utf(text):
    """Swap Material Icon PUA codepoints for portable Unicode glyphs, for
    copy / save of the Log and Detail tabs (in-app they render in the
    Material Icons font, which other apps lack)."""
    for codepoint, glyph in _MATERIAL_TO_UTF.items():
        if codepoint in text:
            text = text.replace(codepoint, glyph)
    return text


def _icon_html(codepoint, colour='#ccc'):
    """Inline HTML span rendering a Material Icon glyph in the icon font."""
    return ('<span style="font-family:\'Material Icons\';color:{};'
            'vertical-align:middle;">{}</span>').format(colour, codepoint)


_NO_DATA_HTML = '<i style="color:#666;">No data available.</i>'

# Synthetic log lines for terminal reasons that leave ComfyUI's in-memory log
# empty: a server restart/crash mid-render, or a running job dropped from the
# queue with no terminal event. The reason is all that survives, so the Log
# surfaces it in plain language with the same icon+colour vocabulary the real
# events use (error / interrupted).
_LOG_FOR_REASON = {
    'server_restart': (
        ERROR, ERROR_COLOR,
        'The server was restarted while this job was running, '
        'so it did not finish.'),
    'running_disappeared': (
        ERROR, ERROR_COLOR,
        'This job stopped responding and was dropped from the queue.'),
}


def _synthetic_log_html(execution_error):
    """One plain-language line for a known terminal reason that captured no
    log, else the no-data placeholder."""
    reason = execution_error.get('reason', '') if isinstance(
        execution_error, dict) else ''
    info = _LOG_FOR_REASON.get(reason)
    if not info:
        return _NO_DATA_HTML
    codepoint, colour, text = info
    return '{}  {}'.format(_icon_html(codepoint, colour), _html.escape(text))


def _synthetic_log_plaintext(execution_error):
    """Plain-text twin of _synthetic_log_html for Log copy / save."""
    reason = execution_error.get('reason', '') if isinstance(
        execution_error, dict) else ''
    info = _LOG_FOR_REASON.get(reason)
    if not info:
        return 'No data available.'
    codepoint, _colour, text = info
    glyph = _MATERIAL_TO_UTF.get(codepoint, '')
    return '{} {}'.format(glyph, text) if glyph else text


def _anchor_msg_time(msg_ts, sent_epoch, create_time, fallback):
    """Express a server event time on the client clock.

    Client submit instant plus the server-measured offset from submit to
    the event. Both server values (create_time and the message timestamp)
    share one clock, so their delta is immune to client/server clock skew;
    only the delta is trusted, never the server's absolute wall clock.
    Falls back to the raw server timestamp when the anchor is unavailable
    (e.g. jobs submitted outside Nukomfy).
    """
    create_sec = _to_epoch_seconds(create_time)
    event_sec = _to_epoch_seconds(msg_ts)
    if sent_epoch and create_sec and event_sec:
        try:
            return _datetime.datetime.fromtimestamp(
                sent_epoch + (event_sec - create_sec)).strftime(_DT_FORMAT)
        except (OverflowError, OSError, ValueError):
            pass
    return _fmt_epoch(msg_ts, fallback)


def _format_log_messages(messages, execution_error=None, entry=None):
    """Format execution messages as HTML log text with Material icons.

    Shows only ComfyUI-side events (execution_start/cached/executing/
    success/error/interrupted). Output path and "Job cancelled by user"
    lines belong to the Detail tab, not here - the Log tab is a pure
    server log view. `execution_error` gets a red left side-bar accent
    (same pattern as the Detail dialog error section) so it pops out
    from the normal flat-rendered events. When ComfyUI captured no log
    (server restart, dropped job) the persisted reason becomes a single
    plain-language line instead of an empty placeholder.
    """
    fallback = _synthetic_log_html(execution_error)
    if not messages:
        return fallback
    sent_epoch = _resolve_sent(entry)[1] if entry else 0.0
    create_time = entry.get('create_time', 0) if entry else 0
    lines = []
    for msg in messages:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        msg_type = msg[0]
        msg_data = msg[1] if isinstance(msg[1], dict) else {}
        time_str = _anchor_msg_time(
            msg_data.get('timestamp', 0), sent_epoch, create_time, '')

        icon_codepoint, colour = _LOG_ICON_FOR_EVENT.get(msg_type, ('', '#ccc'))
        icon = _icon_html(icon_codepoint, colour) if icon_codepoint else ''

        # Timestamp-first layout (syslog/journalctl convention):
        # "<icon> <timestamp>  <event>". Lets the eye scan the time
        # column without grammar friction.
        ts_label = ('<span style="color:#888;">{}</span>'
                    .format(time_str or '?'))
        if msg_type == 'execution_start':
            lines.append('{} {}  Started'.format(icon, ts_label))
        elif msg_type == 'execution_cached':
            nodes = msg_data.get('nodes', [])
            lines.append('{} {}  Cached: {} nodes'.format(
                icon, ts_label, len(nodes)))
        elif msg_type == 'executing':
            node = msg_data.get('node', '?')
            display = msg_data.get('display_node', node)
            lines.append('{} {}  Executing node {}'.format(
                icon, ts_label, _html.escape(str(display))))
        elif msg_type == 'execution_success':
            lines.append('{} {}  Completed'.format(icon, ts_label))
        elif msg_type == 'execution_error':
            # Red side-bar accent box: a 2-column table with a thin
            # bgcolor cell. Qt's rich-text subset supports border/padding
            # only on <table>, not on <div>, so the side-bar has to be a
            # table cell rather than a CSS border. This visual-only wrapper
            # is dropped by the plain-text twin _format_log_plaintext (it
            # explodes into phantom lines under toPlainText).
            err_lines = ['{} {}  <b>Error</b>'.format(icon, ts_label)]
            exc_msg = msg_data.get('exception_message', '')
            if exc_msg:
                err_lines.append(_html.escape(str(exc_msg)))
            node_id = msg_data.get('node_id', '')
            node_type = msg_data.get('node_type', '')
            if node_id or node_type:
                err_lines.append('<i>At node: {} ({})</i>'.format(
                    _html.escape(str(node_type or '?')),
                    _html.escape(str(node_id or '?'))))
            tb = msg_data.get('traceback', '')
            if tb:
                if isinstance(tb, list):
                    tb = '\n'.join(str(t) for t in tb)
                tb = str(tb).strip('\n').strip()
                # A <div> (not <pre>) so the traceback inherits the document's
                # default font instead of Qt's built-in monospace <pre> default;
                # white-space:pre-wrap keeps the line breaks.
                err_lines.append(
                    ('<div style="color:#aaa;background:#1a1a1a;padding:4px 6px;'
                     'border-radius:3px;white-space:pre-wrap;'
                     'margin:2px 0 0 0;">{tb}</div>')
                    .format(tb=_html.escape(tb)))
            lines.append(
                '<table border="0" cellspacing="0" cellpadding="0" '
                'style="margin:2px 0;">'
                '<tr>'
                '<td bgcolor="{c}" width="1" '
                'style="background-color:{c};">&nbsp;</td>'
                '<td width="10">&nbsp;</td>'
                '<td>{body}</td>'
                '</tr>'
                '</table>'.format(c=ERROR_COLOR, body='<br>'.join(err_lines)))
        elif msg_type == 'execution_interrupted':
            lines.append('{} {}  Interrupted'.format(icon, ts_label))

    if not lines:
        return fallback
    return '<br>'.join(lines)


def _format_log_plaintext(messages, execution_error=None, entry=None):
    """Plain-text twin of _format_log_messages for Log tab copy / save.

    Built from the source events, not scraped from the rendered widget:
    toPlainText() explodes the execution_error box (an HTML <table> with a
    red side-bar plus a <pre> traceback) into phantom blank / single-space
    lines around the error. Same glyph + timestamp vocabulary as the
    rendered log, minus the visual-only table wrapper - so a copied error
    log reads with the single spacing the in-app box already shows.
    """
    fallback = _synthetic_log_plaintext(execution_error)
    if not messages:
        return fallback
    sent_epoch = _resolve_sent(entry)[1] if entry else 0.0
    create_time = entry.get('create_time', 0) if entry else 0
    lines = []
    for msg in messages:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        msg_type = msg[0]
        msg_data = msg[1] if isinstance(msg[1], dict) else {}
        time_str = _anchor_msg_time(
            msg_data.get('timestamp', 0), sent_epoch, create_time, '?')
        glyph = _MATERIAL_TO_UTF.get(
            _LOG_ICON_FOR_EVENT.get(msg_type, ('', ''))[0], '')
        # Single spaces: the rendered HTML collapses its icon/ts/label
        # whitespace to single spaces, so toPlainText (today's copy) does
        # too - match it exactly for non-error events.
        prefix = '{} {}'.format(glyph, time_str) if glyph else time_str
        if msg_type == 'execution_start':
            lines.append('{} Started'.format(prefix))
        elif msg_type == 'execution_cached':
            nodes = msg_data.get('nodes', [])
            lines.append('{} Cached: {} nodes'.format(prefix, len(nodes)))
        elif msg_type == 'executing':
            node = msg_data.get('node', '?')
            display = msg_data.get('display_node', node)
            lines.append('{} Executing node {}'.format(prefix, display))
        elif msg_type == 'execution_success':
            lines.append('{} Completed'.format(prefix))
        elif msg_type == 'execution_error':
            lines.append('{} Error'.format(prefix))
            exc_msg = msg_data.get('exception_message', '')
            if exc_msg:
                lines.append(str(exc_msg))
            node_id = msg_data.get('node_id', '')
            node_type = msg_data.get('node_type', '')
            if node_id or node_type:
                lines.append('At node: {} ({})'.format(
                    node_type or '?', node_id or '?'))
            tb = msg_data.get('traceback', '')
            if tb:
                if isinstance(tb, list):
                    tb = '\n'.join(str(t) for t in tb)
                tb = str(tb).strip('\n').strip()
                # Blank line sets the raw stack trace off from the
                # human-readable error summary above, mirroring the visual
                # separation the in-app <pre> block already shows.
                lines.append('')
                lines.append(tb)
        elif msg_type == 'execution_interrupted':
            lines.append('{} Interrupted'.format(prefix))
    if not lines:
        return fallback
    return '\n'.join(lines)


def _render_job_header(entry):
    """Shared header HTML for the unified Job dialog.

    Displays the common summary (prompt_id, nfy_job_id, status, submitted,
    duration) above both the Detail and Log tabs so it stays visible no
    matter which tab the user picks. Robust to the two timestamp shapes
    we store: epoch `create_time` (queue/server data) or ISO `sent_at`
    (submit_history entries).
    """
    if not entry:
        return _NO_DATA_HTML
    status_text, color, _icon = render_job_status(entry.get('nfy_status_str', ''))
    # Prefer the client submit time (nfy_sent_at) over the server queue
    # epoch (create_time), matching _render_detail_header so the summary
    # and the Detail body never show two different "Submitted" times for
    # the same job (a refreshed running job carries both).
    submit_str, _ = _resolve_sent(entry)
    dur_str = _format_duration(entry.get('nfy_duration', 0))
    nfy_job = entry.get('nfy_job_id', '')
    pid = entry.get('prompt_id', '')
    lines = []
    if nfy_job:
        lines.append('<b>Job ID:</b>  {}'.format(_html.escape(nfy_job)))
    if pid:
        lines.append('<b>Prompt ID:</b>  {}'.format(_html.escape(pid)))
    lines.append(
        '<b>Status:</b> <span style="color:{}">{}</span> &nbsp; '
        '<b>Submitted:</b> {} &nbsp; '
        '<b>Duration:</b> {}'.format(
            color, status_text, submit_str, dur_str))
    return '<br>'.join(lines)


# ---------------------------------------------------------------------------
# Job detail dialog - header + collapsible sections
# ---------------------------------------------------------------------------

# Uniform placeholder for missing / not-yet-set values across the
# Detail tab. Italic gray ASCII hyphen - same style for every field
# so rows never appear / disappear with state. Plain `-`, never em-dash:
# the long dash creates ambiguity between "missing" and a literal
# user-typed dash inside a text param value.
_FIELD_DASH_HTML = '<i style="color:#888;">-</i>'


def _emit_field(label, value, html_value=False):
    """Build an HTML row '<b>label:</b>  value'. Empty value renders as
    the uniform dash placeholder so every row stays visible.

    html_value=True passes the value through unescaped (caller already
    composed an HTML fragment, e.g. coloured status with a span).
    """
    if value in (None, '', 0) or (isinstance(value, str) and not value.strip()):
        rhs = _FIELD_DASH_HTML
    elif html_value:
        rhs = str(value)
    else:
        # pre-wrap: preserve newlines / runs of spaces in free-text values
        # through both the HTML render and the toPlainText() behind Copy / Save.
        rhs = ('<span style="white-space:pre-wrap;">{}</span>'
               .format(_html.escape(str(value))))
    return '<b>{}:</b>  {}'.format(label, rhs)

def _render_detail_header(entry):
    """Compact 9-field header rendered at the top of the Detail tab body.

    Order: Job ID + Prompt ID, then Status, Submitted at, Duration,
    Submitted by (username only - host shown alongside the Machine
    field), Machine (name + url), Frame range, Output (newline-joined
    paths). All rows are ALWAYS rendered with a uniform dash
    placeholder for missing / in-progress values - UI rows must not
    appear/disappear with state.
    """
    if not entry:
        return ''
    lines = []
    lines.append(_emit_field('Job ID', entry.get('nfy_job_id')))
    lines.append(_emit_field('Prompt ID', entry.get('prompt_id')))
    # Always route through render_job_status: an empty status_str must map
    # to the same "Unknown" fallback used by the summary header and the
    # Queue/History/MyJobs tables.
    label, colour, _icon = render_job_status(entry.get('nfy_status_str', ''))
    lines.append(_emit_field(
        'Status',
        '<span style="color:{}">{}</span>'.format(colour, label),
        html_value=True))
    # Client submit time is the single source for both 'Submitted' (here)
    # and 'Sent' (submission section), at second precision - identical on
    # the Suite web Detail. create_time (server queue epoch) is only a
    # fallback for entries that lack nfy_sent_at.
    submit_str, _ = _resolve_sent(entry)
    if submit_str == '-':
        submit_str = ''
    lines.append(_emit_field('Submitted', submit_str))
    dur = entry.get('nfy_duration', 0) or 0
    lines.append(_emit_field(
        'Duration',
        _format_duration(dur) if dur and dur > 0 else None))
    lines.append(_emit_field(
        'Submitted by', entry.get('nfy_submitted_by')))
    m_name, m_url, m_status = _resolve_machine(entry)
    if m_status in ('configured_hidden', 'removed_hidden'):
        # Hidden status keeps the URL out of the header. Just the name.
        lines.append(_emit_field('Machine', m_name or None))
    elif m_name and m_url:
        mach = '{} ({})'.format(
            _html.escape(m_name), _html.escape(m_url))
        lines.append(_emit_field('Machine', mach, html_value=True))
    elif m_name:
        lines.append(_emit_field('Machine', m_name))
    else:
        lines.append(_emit_field('Machine', None))
    frame_range = (entry.get('nfy_output_ranges')
                   or entry.get('nfy_frame_range') or [])
    fr_str = ''
    if frame_range:
        try:
            fr_str = submit_history.format_frame_range(frame_range)
        except Exception:
            fr_str = ''
    lines.append(_emit_field('Output range', fr_str))
    # Output paths - always rendered, including cancelled/failed jobs:
    # the path is a static job metadatum (where output was expected),
    # useful to locate partial output even after a failure. Dash
    # placeholder only when no paths were recorded at all.
    output_paths = entry.get('nfy_output_paths') or []
    if output_paths:
        if len(output_paths) == 1:
            out_html = _html.escape(output_paths[0])
        else:
            out_html = '<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'.join(
                _html.escape(p) for p in output_paths)
        lines.append(_emit_field('Output', out_html, html_value=True))
    else:
        lines.append(_emit_field('Output', None))
    return '<br>'.join(lines)


def _short_job_ref(entry):
    """Short human-friendly id for window titles.

    Prefers nfy_job_id (7-char Base62). Falls back to truncated prompt_id
    for entries that don't carry a job id. Returns empty string when
    neither exists.
    """
    nfy_job = entry.get('nfy_job_id') or ''
    if nfy_job:
        return nfy_job
    pid = entry.get('prompt_id') or ''
    if not pid:
        return ''
    return pid[:8] + '\u2026' if len(pid) > 8 else pid

def _fit_dialog_to_content(dialog, default_w, default_h,
                           screen_cap_ratio=0.85):
    """Open *dialog* at a sensible default size, capped at screen.

    QTextEdit sizeHint does not grow with its document, so Qt's own
    adjustSize can't pick the right window size for a long log. We set
    a tuned default per dialog and rely on the internal scrollbar to
    handle large content. The screen cap prevents the default from
    overflowing on small monitors. The user can still maximise manually
    via the window's maximise button.
    """
    default_w, default_h = cap_to_screen(
        default_w, default_h, reference=dialog.parent(),
        ratio=screen_cap_ratio)
    # Respect dialog's own minimum if caller's default is below it.
    min_size = dialog.minimumSize()
    default_w = max(default_w, min_size.width())
    default_h = max(default_h, min_size.height())
    dialog.resize(default_w, default_h)


def _setup_table_columns(table, headers, widths, fixed_cols=None,
                         stretch_col=None, ui_key=None,
                         pixel_widths=None):
    """Configure table headers.

    *fixed_cols* - set of column indices with fixed width (buttons, icons).
    *stretch_col* - single column index that absorbs remaining space
                    (Interactive but auto-adjusts to prevent overflow).
    *pixel_widths* - optional {col: exact_pixels} dict giving action
                    columns an exact width, so the breathing room around
                    an icon-only button stays a literal few pixels
                    (e.g. button + 3) for symmetric centering against the
                    1px gridline.
    Other columns are Interactive (user-resizable).
    """
    table.setHorizontalHeaderLabels(headers)
    table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    # Smooth pixel-wise scrolling (default is ScrollPerItem which feels jumpy).
    table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
    table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
    h = table.horizontalHeader()
    h.setStretchLastSection(False)
    # Default Qt minimum section size (~30px) clamps narrow action
    # columns. Lower it so 22+2-style action widths take effect.
    h.setMinimumSectionSize(2)
    fixed = fixed_cols or set()
    for i in range(len(headers)):
        if i in fixed:
            h.setSectionResizeMode(i, QtWidgets.QHeaderView.Fixed)
        else:
            h.setSectionResizeMode(i, QtWidgets.QHeaderView.Interactive)
    for col, w in widths.items():
        table.setColumnWidth(col, w)
    if pixel_widths:
        for col, px in pixel_widths.items():
            table.setColumnWidth(col, px)
    if stretch_col is not None:
        _install_absorber(table, stretch_col)
    if ui_key:
        h.blockSignals(True)
        ui_state.restore_column_widths(ui_key, table)
        # Force fixed columns back to intended size (restore may have
        # overwritten them with stale saved values)
        for col in fixed:
            if col in widths:
                table.setColumnWidth(col, widths[col])
        if pixel_widths:
            for col, px in pixel_widths.items():
                table.setColumnWidth(col, px)
        h.blockSignals(False)
        _proportional_fit(table)
        timer = QtCore.QTimer(table)
        timer.setSingleShot(True)
        timer.setInterval(500)
        timer.timeout.connect(lambda: ui_state.save_column_widths(ui_key, table))
        h.sectionResized.connect(lambda _i, _o, _n: timer.start())


class _EmptyAreaDeselectFilter(QtCore.QObject):
    """Clear the table's selection when the user presses blank viewport
    space (below the last row, where no item sits under the cursor).

    Without this a selection only clears when clicking OUTSIDE the table
    entirely; users expect clicking the empty area inside the table to
    deselect too. Signals fire normally (the deselect is intentional), so
    the one-selection coordinator and the refresh-restore see it.
    """

    def __init__(self, table):
        super().__init__(table)
        self._table = table

    def eventFilter(self, obj, event):
        if (event.type() == QtCore.QEvent.MouseButtonPress
                and not self._table.indexAt(event.pos()).isValid()):
            sm = self._table.selectionModel()
            if sm is not None and (sm.hasSelection()
                                   or sm.currentIndex().isValid()):
                self._table.clearSelection()
                self._table.setCurrentIndex(QtCore.QModelIndex())
        return False


def _install_empty_area_deselect(table):
    """Press on blank viewport space clears the table selection. Idempotent."""
    if getattr(table, '_empty_area_deselect_filter', None) is not None:
        return
    flt = _EmptyAreaDeselectFilter(table)
    table.viewport().installEventFilter(flt)
    table._empty_area_deselect_filter = flt
    return flt


class _ClickOutsideDeselectFilter(QtCore.QObject):
    """Clear *table*'s selection when a press lands inside *dialog*'s own
    window but off the table - the toolbar, pager, margins or button row.

    Complements `_EmptyAreaDeselectFilter`, which only covers blank space
    inside the table viewport: together they make a press anywhere off a
    row deselect it, matching the Render Manager where tables fill the
    whole panel. Installed on QApplication so it sees every press; the
    `window()` gate keeps it to the dialog's own window, so a child window
    (e.g. the Job dialog) never clears the row behind it. Interactive
    controls are skipped so refresh/pager/search keep working untouched.
    """

    _INTERACTIVE_TYPES = (
        QtWidgets.QAbstractButton,
        QtWidgets.QComboBox,
        QtWidgets.QLineEdit,
        QtWidgets.QPlainTextEdit,
        QtWidgets.QTextEdit,
        QtWidgets.QAbstractSpinBox,
        QtWidgets.QTabBar,
        QtWidgets.QSlider,
        QtWidgets.QScrollBar,
        QtWidgets.QMenu,
        QtWidgets.QMenuBar,
    )

    def __init__(self, dialog, table):
        super().__init__(dialog)
        self._dialog = dialog
        self._table = table
        QtWidgets.QApplication.instance().installEventFilter(self)

    def detach(self):
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.MouseButtonPress:
            return False
        if not isinstance(obj, QtWidgets.QWidget):
            return False
        if obj.window() is not self._dialog:
            return False
        cur = obj
        while cur is not None and cur is not self._dialog:
            if isinstance(cur, self._INTERACTIVE_TYPES) or cur is self._table:
                return False
            cur = cur.parent()
        sm = self._table.selectionModel()
        if sm is not None and (sm.hasSelection() or sm.currentIndex().isValid()):
            self._table.clearSelection()
            self._table.setCurrentIndex(QtCore.QModelIndex())
        return False


def _install_click_outside_deselect(dialog, table):
    """Press inside *dialog* but off *table* (and off interactive controls)
    clears the table selection. Returns the filter so the caller can
    `detach()` it on close - it lives on QApplication. Idempotent."""
    if getattr(dialog, '_click_outside_deselect_filter', None) is not None:
        return dialog._click_outside_deselect_filter
    flt = _ClickOutsideDeselectFilter(dialog, table)
    dialog._click_outside_deselect_filter = flt
    return flt


# ---------------------------------------------------------------------------

_WS_MISSING_TOOLTIP = (
    'Live progress unavailable.\n'
    'Install the websocket-client package to enable real-time updates.'
)

# One-line variant for the parenthetical of a polled bar's tooltip, where
# the first line already carries the last-polled node/step.
_WS_MISSING_NOTE = (
    'Live progress unavailable. '
    'Install the websocket-client package to enable real-time updates.'
)


def _coarse_progress_tooltip(node_tip):
    """Tooltip for a no-WS polled progress bar.

    Shows the last-polled node/step on the first line (what was running at
    the last refresh), then the install-websocket note in parentheses. Falls
    back to the bare note when no node info has been polled yet.
    """
    if node_tip:
        return '{}\n({})'.format(node_tip, _WS_MISSING_NOTE)
    return _WS_MISSING_TOOLTIP


class _HatchedProgressCell(QtWidgets.QWidget):
    """Diagonal line fill used when live progress is unavailable.

    Shown for jobs we can't track in real time (pending in queue, or running
    without a WebSocket connection). Uses Qt's built-in `FDiagPattern` brush
    - the standard UI idiom for "field disabled / no data yet".
    """

    def __init__(self, tooltip=None, parent=None):
        super(_HatchedProgressCell, self).__init__(parent)
        if tooltip:
            self.setToolTip(tooltip)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        # 1px inset matches QProgressBar's default frame so this cell
        # doesn't visually overflow into the adjacent column when a
        # row mixes hatched + bar widgets side-by-side.
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QtGui.QColor('#2a2a2a'))
        p.fillRect(r,
                   QtGui.QBrush(QtGui.QColor(90, 90, 90),
                                QtCore.Qt.FDiagPattern))
        p.end()


class _HatchedFillProgressCell(QtWidgets.QWidget):
    """Progress bar whose filled portion is diagonally hatched.

    Used for a running job tracked only through the periodic poll, with no
    live WebSocket: the fill width is the real percentage, the hatch texture
    (same idiom as `_HatchedProgressCell`) signals the value advances at
    refresh cadence rather than in real time. The unfilled remainder stays a
    plain dark track. `set_fraction` lets the panel push poll updates in
    place without rebuilding the row.
    """

    def __init__(self, fraction=0.0, tooltip=None, parent=None):
        super(_HatchedFillProgressCell, self).__init__(parent)
        self._fraction = max(0.0, min(1.0, float(fraction)))
        if tooltip:
            self.setToolTip(tooltip)

    def set_fraction(self, fraction):
        f = max(0.0, min(1.0, float(fraction)))
        if f != self._fraction:
            self._fraction = f
            self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QtGui.QColor('#222222'))
        fill_w = int(round(r.width() * self._fraction))
        if fill_w > 0:
            fr = QtCore.QRect(r.left(), r.top(), fill_w, r.height())
            p.fillRect(fr, QtGui.QColor('#2a2a2a'))
            # Hatch in the live bar's accent (palette Highlight, the colour
            # QProgressBar::chunk uses) so the polled bar reads as the same
            # progress colour, just textured to signal it isn't live. The
            # grey _HatchedProgressCell stays grey for no-data/unknown states.
            accent = self.palette().color(QtGui.QPalette.Highlight)
            p.fillRect(fr, QtGui.QBrush(accent, QtCore.Qt.FDiagPattern))
        p.setPen(QtGui.QColor('#cccccc'))
        p.drawText(r, QtCore.Qt.AlignCenter,
                   '{}%'.format(int(self._fraction * 100)))
        p.end()


# ---------------------------------------------------------------------------
# Detail tab - collapsible sections
# ---------------------------------------------------------------------------
# Module-level dict: section_id -> expanded(bool). Survives dialog
# rebuilds within the same Python session, resets on Nuke restart.
# Defaults applied on first encounter via `_default_expanded()`.
_DETAIL_SECTION_EXPANDED = {}

_SECTION_DEFAULT_EXPANDED = {
    'submission':       True,
    'machine_snapshot': True,
    'workflow':         True,
    'render_config':    True,
    'submitted_params': True,
}


def _default_expanded(section_id):
    """Return the persisted expanded state for *section_id*, falling
    back to the spec-driven default the first time we see it."""
    if section_id in _DETAIL_SECTION_EXPANDED:
        return _DETAIL_SECTION_EXPANDED[section_id]
    return _SECTION_DEFAULT_EXPANDED.get(section_id, False)


_COLLAPSIBLE_HEADER_STYLE = (
    'QToolButton {'
    '  font-size: 11px;'
    '  font-weight: bold;'
    '  border: none;'
    '  padding: 6px 0px 4px 0px;'
    '  text-align: left;'
    '  color: #ddd;'
    '  background: transparent;'
    '  border-bottom: 1px solid #3a3a3a;'
    '}'
    'QToolButton:hover { color: #fff; }'
)

class _CollapsibleSection(QtWidgets.QWidget):
    """Bold-titled section with arrow toggle, no border/box.

    Header: QToolButton with right/down arrow + bold title, separator
    line below. Body: read-only QTextEdit holding HTML, sized to fit
    its content (no internal scrollbars - outer QScrollArea handles
    overflow). Hidden completely when content is empty.
    """

    def __init__(self, section_id, title, parent=None):
        super().__init__(parent)
        self._section_id = section_id
        self._has_content = False
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(0)
        self._toggle = QtWidgets.QToolButton()
        self._toggle.setText('  ' + title)
        self._toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._toggle.setCheckable(True)
        self._toggle.setStyleSheet(_COLLAPSIBLE_HEADER_STYLE)
        self._toggle.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed)
        self._toggle.setCursor(QtCore.Qt.PointingHandCursor)
        self._toggle.toggled.connect(self._on_toggled)
        lay.addWidget(self._toggle)
        self._body = NukomfyTextEdit()
        self._body.setReadOnly(True)
        self._body.setFrameStyle(0)
        # No internal scrollbars - body is sized to fit content; the
        # outer QScrollArea (in _JobDialog) handles vertical overflow
        # of the whole Detail panel.
        self._body.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._body.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._body.setStyleSheet(
            'QTextEdit { background: transparent; border: none; '
            'padding: 4px 0px 8px 16px; color: #ccc; }')
        # Base font via the document default font, not a stylesheet: on Qt 6.5
        # a stylesheet font doesn't reach bold or bare-text fragments, so only
        # setDefaultFont applies Nuke's UI font to every one.
        # See detail_doc_font above.
        self._body.document().setDefaultFont(detail_doc_font())
        # Long lines (paths) wrap rather than horizontally overflowing.
        self._body.setWordWrapMode(QtGui.QTextOption.WordWrap)
        lay.addWidget(self._body)
        # Recompute height when document content changes (set_content_html
        # may run before the widget is laid out, so viewport width is 0
        # at that moment - the contentsChanged + showEvent paths catch
        # the late layout pass).
        self._body.document().contentsChanged.connect(
            self._reflow_body_height)
        # Apply persisted/default expanded state
        self._toggle.setChecked(_default_expanded(section_id))
        self._on_toggled(self._toggle.isChecked())

    def _on_toggled(self, checked):
        self._toggle.setArrowType(
            QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        self._body.setVisible(checked)
        _DETAIL_SECTION_EXPANDED[self._section_id] = bool(checked)
        self._reflow_body_height()

    def _reflow_body_height(self):
        """Resize body to exactly fit document height.

        Without this the QTextEdit keeps its default sizeHint and clips
        long content behind an internal scrollbar (we explicitly
        disable that scrollbar) - the user-facing symptom is "section
        rows hidden when expanded".
        """
        if not self._body.isVisible():
            return
        doc = self._body.document()
        # Use the body's own width as the wrap width - viewport().width()
        # can be 0 before the first layout pass, in which case fall
        # back to the widget's width.
        w = self._body.viewport().width() or self._body.width()
        if w > 0:
            doc.setTextWidth(w)
        h = int(doc.size().height()) + 16
        self._body.setFixedHeight(max(h, 24))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Width changes invalidate document.size() - recompute the
        # body's fixed height so wrapping changes (longer / shorter
        # lines) don't leave gaps or clip rows.
        self._reflow_body_height()

    def showEvent(self, event):
        super().showEvent(event)
        # First show: viewport now has its real width - recompute.
        self._reflow_body_height()

    def set_content_html(self, html):
        """Set the HTML body. Empty -> section is hidden completely."""
        if html and html.strip():
            self._body.setHtml(html)
            self._has_content = True
            self.setVisible(True)
            self._reflow_body_height()
        else:
            self._body.clear()
            self._has_content = False
            self.setVisible(False)



def _render_section_submission(entry):
    """Submission section: who/when/from-where (the submitter side).

    All rows always rendered with dash placeholder for missing
    fields - UI rows never appear/disappear with state.
    """
    if not entry:
        return ''
    env = entry.get('nfy_environment') or {}
    sent_str, _ = _resolve_sent(entry)
    if sent_str == '-':
        sent_str = ''
    return '<br>'.join([
        _emit_field('Submitter',       entry.get('nfy_submitted_by')),
        _emit_field('Submitter host',  entry.get('nfy_submitter_host')),
        _emit_field('OS',              env.get('os_submitter')),
        _emit_field('Nuke version',    env.get('nuke_version')),
        _emit_field('Nukomfy version', env.get('nukomfy_version')),
        _emit_field('Sent',            sent_str),
    ])


def _render_section_machine(entry):
    """Machine snapshot at submit: server-side info captured pre-POST.

    All rows always rendered with dash placeholder for missing
    fields - UI rows never appear/disappear with state.
    """
    if not entry:
        return ''
    snap = entry.get('nfy_server_snapshot') or {}
    sys_info = snap.get('system_stats') or {}
    # OS + GPU are normalised at capture (system_capture.py), so the
    # snapshot is already display-ready here - print verbatim.
    os_srv = sys_info.get('os', '')
    cv = (snap.get('server_version', '')
          or sys_info.get('comfyui_version', ''))
    pyv = sys_info.get('python_version', '')
    if pyv:
        # Strip verbose `(tags/v..., compiler info)` suffix - same fix
        # as machines.py for the Settings panel.
        pyv = pyv.split()[0] if pyv.split() else pyv
    gpu = sys_info.get('gpu', '')
    vram = sys_info.get('vram_total', 0) or 0
    ram = sys_info.get('ram_total', 0) or 0
    m_name, m_url, m_status = _resolve_machine(entry)
    if m_status in ('configured_hidden', 'removed_hidden'):
        # Parentheses mark this as a meta-state label, not a URL value
        # (same convention as the Machines table).
        url_field = '(Hidden)'
    else:
        # configured_visible OR removed_visible: surface the URL.
        # For removed_visible this is the snapshot recorded at submit.
        url_field = m_url
    return '<br>'.join([
        _emit_field('Name',            m_name or entry.get('nfy_machine_name')),
        _emit_field('URL',             url_field),
        _emit_field('OS',              os_srv),
        _emit_field('ComfyUI version', cv),
        _emit_field('Python version',  pyv),
        _emit_field('GPU',             gpu),
        _emit_field('VRAM',
                    '{:.1f} GB'.format(vram / _BYTES_PER_GB) if vram else ''),
        _emit_field('RAM',
                    '{:.1f} GB'.format(ram / _BYTES_PER_GB) if ram else ''),
    ])


def _render_section_workflow(entry):
    """Workflow section: what was run.

    All rows always rendered with dash placeholder for missing
    fields - UI rows never appear/disappear with state.
    """
    if not entry:
        return ''
    meta = entry.get('nfy_workflow_meta') or {}
    return '<br>'.join([
        _emit_field('Name',    entry.get('nfy_workflow_name')),
        _emit_field('Version', meta.get('workflow_version')),
        _emit_field('Author',  meta.get('workflow_author')),
        _emit_field('UUID',    meta.get('workflow_uuid')),
    ])


def _render_section_render(entry):
    """Render configuration: frame range, batch, paths, input cache, …

    All rows always rendered with dash placeholder for missing
    fields - UI rows never appear/disappear with state.
    Multi-entry ranges (output + input) keep their multi-line format
    when present; rendered with the placeholder when absent.
    """
    if not entry:
        return ''
    lines = []
    lines.append(_emit_field('NK File', entry.get('nfy_nk_file')))
    lines.append(_emit_field('Node', entry.get('nfy_node_name')))
    bc = _safe_int(entry.get('nfy_batch_count', 1) or 1)
    bi = _safe_int(entry.get('nfy_batch_index', 1) or 1)
    lines.append(_emit_field('Batch', '{}/{}'.format(bi, bc)))
    # Output ranges - when present, may render across multiple lines
    # (range rows + per-output indented rows). When absent, single
    # placeholder row.
    output_ranges = (entry.get('nfy_output_ranges')
                     or entry.get('nfy_frame_range') or [])
    if output_ranges:
        rng_lines = _render_ranges_lines(
            output_ranges, 'Output Range', 'Output Ranges')
        if rng_lines:
            lines.extend(rng_lines)
        else:
            lines.append(_emit_field('Output Range', None))
    else:
        lines.append(_emit_field('Output Range', None))
    input_ranges = entry.get('nfy_input_ranges') or []
    if input_ranges:
        rng_lines = _render_ranges_lines(
            input_ranges, 'Input Range', 'Input Ranges')
        if rng_lines:
            lines.extend(rng_lines)
        else:
            lines.append(_emit_field('Input Range', None))
    else:
        lines.append(_emit_field('Input Range', None))
    seed_line = _render_seeds_line(entry.get('nfy_seeds_used') or {})
    lines.append(seed_line if seed_line else _emit_field('Seed', None))
    # Input cache before Output paths - logical input -> output order.
    # Multi-input: one cache dir per input that wrote a cache.
    env = entry.get('nfy_environment') or {}
    ic_lines = _render_input_caches_lines(env.get('input_cache'))
    if ic_lines:
        lines.extend(ic_lines)
    else:
        lines.append(_emit_field('Input cache', None))
    # Write template(s) used for this submit - captured from the gizmo
    # `_nfy_params` at submit time. 0 -> dash; 1 -> inline value;
    # 2+ -> header + indented per-input rows. Records which write template
    # was used (global vs workflow).
    meta = entry.get('nfy_workflow_meta') or {}
    wt_lines = _render_write_templates_lines(meta.get('write_templates'))
    if wt_lines:
        lines.extend(wt_lines)
    else:
        lines.append(_emit_field('Write template', None))
    # Output paths - always shown, including cancelled/failed jobs: the
    # path is a static job metadatum (where output was expected), useful
    # to locate partial output even after a failure.
    output_paths = entry.get('nfy_output_paths') or []
    if output_paths:
        output_names = [e.get('name', '') for e in output_ranges
                        if isinstance(e, dict)]
        path_lines = _render_output_paths_lines(output_paths, output_names)
        if path_lines:
            lines.extend(path_lines)
        else:
            lines.append(_emit_field('Output Path', None))
    else:
        lines.append(_emit_field('Output Path', None))
    # Read color intentionally NOT rendered here - it's an internal
    # field used only when Nukomfy creates Read nodes from rendered
    # output (gizmo runtime), not user-facing diagnostic info.
    return '<br>'.join(lines)


def _render_section_submitted_params(entry):
    """Visible vs Hidden parameter listing.

    Universe of params = the `params_spec` array snapshotted in
    `nfy_workflow_meta` at submit time - these are the params the
    author configured in the Workflow Creator (role=='knob'). Workflow
    inputs NOT in params_spec (internal plumbing the author didn't
    surface) are intentionally absent from this list.

    Split via the `enabled` flag on each spec entry:
    - Visible = enabled=True (knob shown on the gizmo Properties)
    - Hidden  = enabled=False (configured but not promoted)

    Values come from the workflow API JSON BLOB persisted at submit time
    (loaded lazily). Rows: `(node_class) param_name = value` with
    the node_class rendered in italic gray as a secondary qualifier.
    Both subsections always rendered (UI invariant: rows must not
    appear/disappear with state).
    """
    if not entry:
        return ''
    meta = entry.get('nfy_workflow_meta') or {}
    spec = meta.get('params_spec') or []
    # Workflow API resolution is centralised in `_JobDialog.populate`
    # (sees BLOB-or-HTTP fallback), then injected into `entry` as
    # `nfy_workflow_api`. This renderer is purely read-only - no I/O.
    wf_api = entry.get('nfy_workflow_api')
    wf_api_safe = wf_api if isinstance(wf_api, dict) else {}

    def _value_for(node_id, name):
        node = wf_api_safe.get(str(node_id)) or {}
        if not isinstance(node, dict):
            return None
        inputs = node.get('inputs') or {}
        if not isinstance(inputs, dict):
            return None
        return inputs.get(name)

    def _class_for(node_id):
        node = wf_api_safe.get(str(node_id)) or {}
        if isinstance(node, dict):
            return node.get('class_type', '?') or '?'
        return '?'

    visible_rows = []
    hidden_rows = []
    for p in spec:
        if not isinstance(p, dict):
            continue
        node_id = str(p.get('node_id', ''))
        name = p.get('name', '')
        if not node_id or not name:
            continue
        cls = _class_for(node_id)
        value = _value_for(node_id, name)
        # Show only parameters actually submitted. A value absent from
        # the workflow API BLOB - a V3 sub-option not active for the
        # current file_type (stripped before submit), or a node dropped
        # from the graph - was never sent, so skip it instead of listing
        # a bare dash.
        if value is None:
            continue
        # Distinguish a real value from an explicitly empty string: a
        # text param submitted blank is meaningful (the user chose to
        # send nothing) and renders as (empty). Link references (Comfy
        # `[node_id, output_idx]`) are filtered out of params_spec but
        # defensive-handled.
        if isinstance(value, list):
            rhs = _html.escape('<link>')
        elif isinstance(value, bool):
            # Match the literal sent to ComfyUI (and the Suite web Detail):
            # lowercase, not Python's str(True)/str(False).
            rhs = 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            # Canonical JSON form so the number reads identically to the
            # Workflow (API) tab (str() and json can diverge on edge cases).
            rhs = _html.escape(_json.dumps(value))
        elif isinstance(value, str) and value == '':
            rhs = '<i style="color:#888;">(empty)</i>'
        else:
            # No length cap, and pre-wrap so newlines and runs of spaces in a
            # free-text value (e.g. a multi-line prompt) survive both the HTML
            # render and the toPlainText() behind Copy / Save - Qt collapses
            # whitespace otherwise.
            rhs = ('<span style="white-space:pre-wrap;">{}</span>'
                   .format(_html.escape(str(value))))
        # Layout: `(NodeClass) <name> [(<label>)] = <value>`.
        # - Node class always first, italic gray (context).
        # - Raw workflow input name as the leading param identifier.
        # - Custom gizmo label appended in parentheses ONLY when the
        #   author renamed it; otherwise nothing extra (no redundant
        #   "(seed)" after "seed").
        label = p.get('label') or ''
        # Node id alongside the class so two nodes of the same class
        # (e.g. two NukomfyRead) are distinguishable. Class in parens,
        # id in its own `[ID: n]` bracket so it reads as a separate
        # qualifier, not part of the class name (mirrors the Workflow
        # Creator `(ID: <id>)` tooltip convention).
        node_part = ('<i style="color:#888;">({}) [ID: {}]</i>'
                     .format(_html.escape(str(cls)), _html.escape(node_id)))
        name_part = _html.escape(str(name))
        _quals = []
        if label and label != name:
            _quals.append(_html.escape(str(label)))
        # A Primitive that drives several widgets emits one row per target
        # (so none is hidden); the marker explains why they share a value.
        if p.get('primitive'):
            _quals.append('via Primitive')
        label_part = ' ({})'.format(', '.join(_quals)) if _quals else ''
        row = '{node_part} {name}{label_part} = {rhs}'.format(
            node_part=node_part,
            name=name_part,
            label_part=label_part,
            rhs=rhs)
        if p.get('enabled'):
            visible_rows.append(row)
        else:
            hidden_rows.append(row)
    visible_block = ('<b>Visible</b><br>'
                     + ('<br>'.join(visible_rows) if visible_rows
                        else _FIELD_DASH_HTML))
    hidden_block = ('<b>Hidden</b><br>'
                    + ('<br>'.join(hidden_rows) if hidden_rows
                       else _FIELD_DASH_HTML))
    return visible_block + '<br><br>' + hidden_block


# ---------------------------------------------------------------------------
# Workflow (API) tab - JSON dump of the payload posted to /prompt


class _WorkflowApiTab(QtWidgets.QWidget):
    """View-only JSON dump of the workflow API payload.

    Source: resolved by the owning dialog - local BLOB persisted at
    submit, the dialog's fetch cache, or the off-thread HTTP chain
    (ComfyUI's `/api/jobs/{prompt_id}`, then the Suite's persistent
    `/nukomfy/jobs/history/{prompt_id}/workflow_api`).

    The body is a `QPlainTextEdit` (not QTextEdit) - pretty-printed
    JSON can run thousands of lines; the plain-text widget is faster
    and uses noticeably less memory than the rich-text version.
    Copy / Save controls live on the dialog footer (operate on the
    active tab) - no per-tab toolbar.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_text = ''  # pretty-printed JSON, exposed via text()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._view = NukomfyPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        # No `padding`: the text view's vertical scrollbar is anchored
        # to the inner padding edge, so any padding pushes the scrollbar
        # away from the tab right edge - making it visually offset from
        # the Detail tab's QScrollArea scrollbar (which sits flush
        # against the tab edge). Use viewport-margins instead so the
        # text content gets the same 8px breathing room as Detail
        # without moving the scrollbar.
        self._view.setStyleSheet(
            'QPlainTextEdit { background: #1e1e1e; color: #ccc; '
            'border: none; }')
        # Nuke's UI font (default body size). For a plain-text view setFont
        # drives the font (a stylesheet font-family wouldn't reach it on Qt 6.5).
        self._view.setFont(detail_doc_font())
        self._view.setViewportMargins(8, 8, 0, 8)
        lay.addWidget(self._view, 1)

    def populate(self, entry):
        """Refresh the JSON dump. Reads `entry['nfy_workflow_api']`
        which the dialog has already resolved (local BLOB -> HTTP
        fallback for cross-user jobs) - this widget is purely
        view-only, no I/O."""
        wf_api = None
        if isinstance(entry, dict):
            wf_api = entry.get('nfy_workflow_api')
        if isinstance(wf_api, dict) and wf_api:
            try:
                self._raw_text = _json.dumps(
                    wf_api, indent=2, sort_keys=False, ensure_ascii=False)
            except Exception:
                _log.exception('Workflow API JSON dump failed')
                self._raw_text = ''
        else:
            self._raw_text = ''
        self._view.setPlainText(self._raw_text)
        self._view.moveCursor(QtGui.QTextCursor.Start)

    def show_loading(self):
        """Show a placeholder while the dialog fetches the workflow API
        off-thread for a server-only job. `_raw_text` stays empty so Copy /
        Save don't capture the placeholder."""
        self._raw_text = ''
        self._view.setPlainText('Loading…')

    def text(self):
        """Return the pretty-printed JSON text currently displayed.
        Used by the dialog footer's Copy / Save buttons."""
        return self._raw_text


# ---------------------------------------------------------------------------
# Unified Job dialog: shared header + [Detail | Log | Workflow API] tabs.
# ---------------------------------------------------------------------------
# Detail + Log bodies share a transparent stylesheet so they inherit
# the dialog's #1e1e1e background, with no widget-level font-family:
# the text font comes from the document default font (detail_doc_font).


class _LeftElideDelegate(QtWidgets.QStyledItemDelegate):
    # Nuke's Qt style on Linux re-elides item text inside CE_ItemViewItem
    # even when opt.textElideMode is ElideNone: a pre-elided path already
    # starts with an ellipsis, gets elided a second time from the right, and
    # collapses to bare dots at some column widths. Draw the item chrome via
    # the style with empty text, then paint the left-elided text ourselves so
    # the style never re-elides it. Reproduces the margin/alignment/colour of
    # QCommonStyle::viewItemDrawText so it stays pixel-aligned with the
    # natively rendered columns.
    def paint(self, painter, option, index):
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget else QtWidgets.QApplication.style()
        # Measure the text sub-rect and elide while the item still holds its
        # text, before blanking it below for the chrome pass.
        text_rect = style.subElementRect(
            QtWidgets.QStyle.SE_ItemViewItemText, opt, widget)
        margin = style.pixelMetric(
            QtWidgets.QStyle.PM_FocusFrameHMargin, None, widget) + 1
        text_rect = text_rect.adjusted(margin, 0, -margin, 0)
        fm = QtGui.QFontMetrics(opt.font)
        elided = fm.elidedText(opt.text, QtCore.Qt.ElideLeft, text_rect.width())
        opt.text = ''
        style.drawControl(
            QtWidgets.QStyle.CE_ItemViewItem, opt, painter, widget)
        if not (opt.state & QtWidgets.QStyle.State_Enabled):
            cg = QtGui.QPalette.Disabled
        elif not (opt.state & QtWidgets.QStyle.State_Active):
            cg = QtGui.QPalette.Inactive
        else:
            cg = QtGui.QPalette.Normal
        role = (QtGui.QPalette.HighlightedText
                if opt.state & QtWidgets.QStyle.State_Selected
                else QtGui.QPalette.Text)
        painter.save()
        painter.setPen(opt.palette.color(cg, role))
        painter.setFont(opt.font)
        painter.drawText(text_rect, int(opt.displayAlignment), elided)
        painter.restore()


# ---------------------------------------------------------------------------
# Job detail widget (embedded in expanded row)
# ---------------------------------------------------------------------------
# Shared job subtable column indices - first 7 cols identical in Queue
# + History. Divergence starts at col 7.
_SUB_COL_STATUS   = 0
_SUB_COL_JOB      = 1
_SUB_COL_SENT     = 2
_SUB_COL_USER     = 3
_SUB_COL_WORKFLOW = 4
_SUB_COL_NODE     = 5
_SUB_COL_NKFILE   = 6
# Queue-specific
_Q_COL_PROGRESS = 7
_Q_COL_ACTIONS  = 8

# History-specific
_H_COL_DURATION = 7
_H_COL_READ     = 8
_H_COL_LOG      = 9

_Q_HEADERS = ['Status', 'Job ID', 'Sent', 'User', 'Workflow', 'Node',
              'NK File', 'Progress', '']
_Q_WIDTHS  = {0: 100, 1: 70, 2: 160, 3: 100, 4: 180, 5: 150,
              6: 220, 7: 120, 8: 24}

_H_HEADERS = ['Status', 'Job ID', 'Sent', 'User', 'Workflow', 'Node',
              'NK File', 'Duration', '', '']
_H_WIDTHS  = {0: 100, 1: 70, 2: 160, 3: 100, 4: 180, 5: 150,
              6: 220, 7: 90, 8: 24, 9: 24}


def _fill_common_job_cells(table, row, entry):
    """Populate the 7 columns shared between Queue and History subtables:
    Status | Sent | User | Job | Workflow | Node | NK File.

    Callers append their own divergent columns starting at col 7 (Progress
    or Duration) and col 8 (Actions or Log).
    """
    # Status - icon+text cell widget, single cell
    label, color, icon_char = render_job_status(entry.get('nfy_status_str', ''))
    table.setCellWidget(row, _SUB_COL_STATUS,
                        _make_status_cell(icon_char, label, color))

    # Sent - client submit time (nfy_sent_at); server create_time only as a
    # fallback for jobs submitted outside Nukomfy. Relative time in tooltip.
    sent_display, sent_epoch = _resolve_sent(entry)
    sent_item = QtWidgets.QTableWidgetItem(sent_display)
    sent_item.setTextAlignment(QtCore.Qt.AlignCenter)
    rel = format_relative_time(sent_epoch) if sent_epoch else ''
    if rel:
        sent_item.setToolTip(rel)
    table.setItem(row, _SUB_COL_SENT, sent_item)

    # User - nfy_submitted_by (username only). Hostname goes to
    # tooltip; full detail surfaced in the popup Detail dialog.
    user = entry.get('nfy_submitted_by') or '-'
    user_item = QtWidgets.QTableWidgetItem(user)
    user_item.setTextAlignment(QtCore.Qt.AlignCenter)
    host = entry.get('nfy_submitter_host', '')
    if host:
        user_item.setToolTip('Submitted from: {}'.format(host))
    table.setItem(row, _SUB_COL_USER, user_item)

    # Job - nfy_job_id (7-char Base62) for Nukomfy submits; 'External' for
    # jobs submitted from outside Nukomfy (e.g., ComfyUI web UI), which
    # never carry a nfy_job_id in extra_data.
    job_item = QtWidgets.QTableWidgetItem(entry.get('nfy_job_id') or 'External')
    job_item.setTextAlignment(QtCore.Qt.AlignCenter)
    table.setItem(row, _SUB_COL_JOB, job_item)

    # Workflow
    wf_item = QtWidgets.QTableWidgetItem(
        entry.get('nfy_workflow_name', '') or '-')
    wf_item.setTextAlignment(QtCore.Qt.AlignCenter)
    table.setItem(row, _SUB_COL_WORKFLOW, wf_item)

    # Node
    node_item = QtWidgets.QTableWidgetItem(
        entry.get('nfy_node_name', '') or '-')
    node_item.setTextAlignment(QtCore.Qt.AlignCenter)
    table.setItem(row, _SUB_COL_NODE, node_item)

    # NK File - full path with ElideLeft (set table-wide) so filename
    # (rightmost component) stays visible even when cell is narrow. The '-'
    # placeholder (no path) is centred like the other empty cells; a real
    # path stays left-aligned so ElideLeft keeps the filename in view.
    nk = entry.get('nfy_nk_file', '')
    nk_item = QtWidgets.QTableWidgetItem(nk or '-')
    if not nk:
        nk_item.setTextAlignment(QtCore.Qt.AlignCenter)
    nk_item.setToolTip(nk or 'Unknown')
    table.setItem(row, _SUB_COL_NKFILE, nk_item)


_MJ_COL_STATUS   = 0
_MJ_COL_JOB      = 1
_MJ_COL_SENT     = 2
_MJ_COL_MACHINE  = 3
_MJ_COL_WORKFLOW = 4
_MJ_COL_NODE     = 5
_MJ_COL_NKFILE   = 6
# col 7: Progress (Active) / Duration (History)
_MJ_COL_PROGDUR  = 7
# col 8: Actions (Abort/Remove - Active; Log+Read icons - History)
_MJ_COL_ACTIONS  = 8
# col 9: Delete (History only)
_MJH_COL_DELETE  = 9

_MJA_HEADERS = ['Status', 'Job ID', 'Sent', 'Machine', 'Workflow', 'Node',
                'NK File', 'Progress', '']
_MJA_WIDTHS  = {0: 100, 1: 70, 2: 160, 3: 160, 4: 180, 5: 150,
                6: 220, 7: 120, 8: 24}

_MJH_HEADERS = ['Status', 'Job ID', 'Sent', 'Machine', 'Workflow', 'Node',
                'NK File', 'Duration', '', '']
_MJH_WIDTHS  = {0: 100, 1: 70, 2: 160, 3: 160, 4: 180, 5: 150,
                6: 220, 7: 90, 8: 50, 9: 24}

# Matches submit_panel.py styled groupboxes so the dotted splitter handle's
# +3px compensation lands centred between the two panes (same structure:
# splitter -> QWidget pane margin 0 -> styled QGroupBox -> content).
_MJ_GROUPBOX_STYLE = (
    'QGroupBox{border:1px solid #3a3a3a;'
    'border-radius:3px;margin-top:6px;padding-top:2px;}'
    'QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;'
    'color:#eee;font-weight:bold;}'
)


def _fill_myjobs_cells(table, row, entry):
    """Populate the 7 columns shared between MyJobs Active and History
    (Status | Job ID | Sent | Machine | Workflow | Node | NK File).

    Divergence from `_fill_common_job_cells`: col 3 shows machine_name
    instead of user - MyJobs is scoped to the current user, so Machine
    is the useful cross-row dimension.
    """
    # Status - icon+label cell widget
    label, color, icon_char = render_job_status(entry.get('nfy_status_str', ''))
    table.setCellWidget(row, _MJ_COL_STATUS,
                        _make_status_cell(icon_char, label, color))

    # Job ID (nfy_job_id, 7-char Base62) - '-' when the entry has none.
    job_item = QtWidgets.QTableWidgetItem(entry.get('nfy_job_id') or '-')
    job_item.setTextAlignment(QtCore.Qt.AlignCenter)
    table.setItem(row, _MJ_COL_JOB, job_item)

    # Sent - client submit time (nfy_sent_at), one source of truth shared
    # with every other view via _resolve_sent. Relative time in tooltip.
    sent_display, sent_epoch = _resolve_sent(entry)
    sent_item = QtWidgets.QTableWidgetItem(sent_display)
    sent_item.setTextAlignment(QtCore.Qt.AlignCenter)
    if sent_epoch:
        rel = format_relative_time(sent_epoch)
        if rel:
            sent_item.setToolTip(rel)
    table.setItem(row, _MJ_COL_SENT, sent_item)

    # Machine - name in cell. URL tooltip only for non-hidden visible
    # machines; hidden machines (configured or removed) leak nothing,
    # removed visible machines get a warning marker (centered widget).
    m_name, m_url, m_status = _resolve_machine(entry)
    cell_name = m_name or entry.get('nfy_machine_name', '') or '-'
    if m_status == 'removed_visible':
        # Plain QTableWidgetItem renders setIcon flush-left while the
        # text stays centered, which looks broken. Use a centered
        # icon+label widget instead.
        widget = _make_machine_warning_cell(cell_name)
        # Tooltip shows the snapshot URL too - this is a log, surface
        # what was recorded so the user can identify the machine.
        if m_url:
            widget.setToolTip(
                'Machine no longer in Settings\n{}'.format(m_url))
        else:
            widget.setToolTip('Machine no longer in Settings')
        # Clear any prior QTableWidgetItem; otherwise selection/Qt may
        # render both layers.
        table.setItem(row, _MJ_COL_MACHINE, QtWidgets.QTableWidgetItem())
        table.setCellWidget(row, _MJ_COL_MACHINE, widget)
    else:
        # Drop any previous cell widget left from a removed_visible
        # render on this row (cell widgets persist across refreshes).
        table.removeCellWidget(row, _MJ_COL_MACHINE)
        mach_item = QtWidgets.QTableWidgetItem(cell_name)
        mach_item.setTextAlignment(QtCore.Qt.AlignCenter)
        if m_status == 'configured_visible' and m_url:
            mach_item.setToolTip(m_url)
        table.setItem(row, _MJ_COL_MACHINE, mach_item)

    # Workflow
    wf_item = QtWidgets.QTableWidgetItem(
        entry.get('nfy_workflow_name', '') or '-')
    wf_item.setTextAlignment(QtCore.Qt.AlignCenter)
    table.setItem(row, _MJ_COL_WORKFLOW, wf_item)

    # Node
    node_item = QtWidgets.QTableWidgetItem(
        entry.get('nfy_node_name', '') or '-')
    node_item.setTextAlignment(QtCore.Qt.AlignCenter)
    table.setItem(row, _MJ_COL_NODE, node_item)

    # NK File - ElideLeft keeps filename visible when column is narrow. The
    # '-' placeholder (no path) is centred like the other empty cells; a
    # real path stays left-aligned so ElideLeft keeps the filename in view.
    nk = entry.get('nfy_nk_file', '')
    nk_item = QtWidgets.QTableWidgetItem(nk or '-')
    if not nk:
        nk_item.setTextAlignment(QtCore.Qt.AlignCenter)
    nk_item.setToolTip(nk or 'Unknown')
    table.setItem(row, _MJ_COL_NKFILE, nk_item)


def _make_machine_warning_cell(name):
    """Centered icon+label widget for the MyJobs Machine column when
    the machine is no longer in Settings. Plain QTableWidgetItem's
    setIcon puts the icon flush-left while keeping the text centered,
    so we use a custom widget to keep them visually together.
    """
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addStretch(1)
    icon_lbl = QtWidgets.QLabel(WARNING)
    icon_lbl.setFont(icon_font(12))
    icon_lbl.setStyleSheet('color:#888;background:transparent;')
    text_lbl = QtWidgets.QLabel(name or '-')
    text_lbl.setStyleSheet('color:#888;background:transparent;')
    lay.addWidget(icon_lbl)
    lay.addWidget(text_lbl)
    lay.addStretch(1)
    return w

