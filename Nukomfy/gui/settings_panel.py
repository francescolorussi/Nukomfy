"""Tabbed settings dialog: Paths, Machines, Jobs, Interface."""

import logging
import os

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui import _dialogs

from Nukomfy.core.settings import settings, _DEFAULTS
from Nukomfy.utils.path_utils import canonical_path, runtime_path
from Nukomfy.utils.fs_safe import dangerous_root_kind
from Nukomfy.gui._theme import clamp_int_field, attach_int_clamp, SCROLLBAR_STYLE, ACCENT_GOLD
from Nukomfy.gui._inline_messages import make_error_banner
from Nukomfy.gui._no_wheel import NoWheelComboBox
from Nukomfy.gui._fields import NukomfyLineEdit

_log = logging.getLogger(__name__)


def _native_sep(path):
    """Return *path* with separators matching the current OS.

    Literal paths only: if *path* contains TCL syntax (`[...]`, `$var`,
    `{...}`) we return it untouched - rewriting separators inside a TCL
    expression would break it (on Windows, `\\` is an escape character
    in TCL, so converting `/` to `\\` inside `[getenv X]/sub` would
    corrupt the expression).
    """
    if not path:
        return path
    if any(c in path for c in '[]${}'):
        return path
    # First unify, then convert to the current OS separator.
    return path.replace('\\', '/').replace('/', os.sep)


def _humanize_bytes(n):
    """Format a byte count as '12.3 MB' or '1.2 GB' (1 GB = 1024 MB)."""
    mb = n / 1e6
    if mb >= 1024:
        return '{:.1f} GB'.format(mb / 1024)
    return '{:.1f} MB'.format(mb)


# Preview label style for the "valid / informational" state - used when
# the canonical path resolution is shown. Errors use `make_error_banner`
# from `_inline_messages.py` (separate widget, not a label restyle).
_PREVIEW_OK = 'color:#666;font-size:10px;font-style:italic;padding-left:2px;'

# Bordered-group style for the Paths / Interface / Jobs section boxes.
# Module-local, not a shared _theme constant: other panels' group boxes
# differ in padding and title offset.
_SECTION_GROUP_STYLE = (
    'QGroupBox{border:1px solid #3a3a3a;border-radius:3px;'
    'margin-top:6px;padding:10px 8px 8px 8px;font-size:11px;}'
    'QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;'
    'color:#eee;font-weight:bold;font-size:11px;}')
from Nukomfy.gui.ui_state import ui_state, center_on_screen, fit_to_screen
from Nukomfy.gui.icons import (icon_font, set_press_icon,
                   ADD, CLOSE, SETTINGS_BACKUP_RESTORE, DELETE_SWEEP,
                   LIST)
from Nukomfy.gui._lock_indicator import make_lock_label
from Nukomfy.utils.output_path import (preview_template, validate_template,
                         DEFAULT_OUTPUT_TEMPLATE)
from Nukomfy.gui._message_dialogs import _PathListDialog


def _classify_unsafe_cache_root(raw_path):
    """Return (kind, resolved) when *raw_path* would be a catastrophic
    Input Cache root, else (None, resolved).

    *kind* is one of ``'drive'`` | ``'home'`` | ``'system'`` and is used
    by the caller to phrase a precise error message. Delegates to
    ``fs_safe.dangerous_root_kind`` (the single rule shared with
    ``fs_safe._is_safe_rmtree_target``) so the Settings UI refuses up-front
    any path the backend would refuse at delete time.

    Returns ``(None, ...)`` for empty / invalid / unresolvable input -
    those cases are already covered by the existing ``has_visible_error``
    gate in :meth:`_PathsTab.save`.
    """
    ok, value, _reason = canonical_path(raw_path)
    if not ok:
        return (None, value)
    return (dangerous_root_kind(value), value)


def _show_unsafe_cache_path_dialog(parent, kind, resolved):
    """Show a Critical dialog explaining why the cache path is refused."""
    reasons = {
        'drive': 'a drive or filesystem root',
        'home': 'your home directory',
        'system': 'a system folder',
    }
    _dialogs.critical(
        parent, 'Unsafe Input Cache Path',
        'The Input Cache Path resolves to {}:\n\n{}\n\n'
        'Nukomfy will not store the input cache directly in this '
        'location. You can use a dedicated subfolder of it instead.'
        .format(reasons[kind], _native_sep(resolved)))


_TEMPLATE_TOOLTIP = (
    "<p style='white-space: pre; font-family: monospace;'>"
    'Available tokens:\n'
    '  {nk_file}            Nuke script name without version suffix\n'
    '                       (e.g. \'my_comp\' from my_comp_v003.nk)\n'
    '  {workflow}           Workflow name, sanitized\n'
    '  {workflow_alias}     Workflow Alias if set, else the workflow name\n'
    '  {output_name}        Output name, sanitized\n'
    '  {node}               Nuke node name, may end with a number\n'
    '                       (e.g. \'MyWorkflow1\')\n'
    '  {username}           OS username\n'
    '  {version}            Version string, padding from Settings [required]\n'
    '  {frame}              Frame number, padding from Settings   [required]\n'
    '  {ext}                File extension                        [required]\n'
    '  {output_index}       Output index (01, 02, …) for multi-output gizmos\n'
    '  {workflow_uuid}      Workflow UUID compacted (12 hex chars)\n'
    '  {workflow_category}  Workflow category tag(s) joined by \'_\'\n'
    '  {workflow_model}     Workflow model tag(s) joined by \'_\'\n'
    '\n'
    'What you can write here:\n'
    '  • Literal characters (letters, digits, _, -)\n'
    '  • The {token} placeholders from the list above\n'
    '  • Subfolder separators / or \\ (both accepted)\n'
    '  • At least one separator is required\n'
    '\n'
    'For dynamic paths (environment variables, project root, etc.) use the\n'
    'Output Path field above. That field accepts TCL expressions; this template\n'
    'field accepts only the tokens listed above.\n'
    '\n'
    'The Output Path field above is automatically prepended at runtime.\n'
    'All tokens here resolve from gizmo state alone, so the Settings\n'
    'preview, the gizmo live preview, and the Read Outputs button always\n'
    'agree on the path the submit will write.'
    '</p>'
)


_TOKEN_LIST = (
    '{nk_file}',
    '{workflow}',
    '{workflow_alias}',
    '{output_name}',
    '{node}',
    '{username}',
    '{version}',
    '{frame}',
    '{ext}',
    '{output_index}',
    '{workflow_uuid}',
    '{workflow_category}',
    '{workflow_model}',
)


_OUTPUT_PATH_TOOLTIP = (
    "<p style='white-space: pre; font-family: monospace;'>"
    'Base output directory. The Output Path Template below is appended\n'
    'to this path at submit time.\n'
    '\n'
    'TCL expressions are supported here (evaluated by Nuke at submit\n'
    'and at preview time). Common patterns:\n'
    '\n'
    '  [getenv PROJECT]/renders     environment variable\n'
    '  [value root.name]            current Nuke script name\n'
    '  [file dirname [value root.name]]/renders\n'
    '                               folder containing the .nk\n'
    '  [python {os.getlogin()}]     inline Python via Nuke TCL\n'
    '\n'
    'Literal brackets in the path: write \\[ and \\] (TCL escape).\n'
    '\n'
    'Note - [value this.name] and other this.X references work ONLY\n'
    'at gizmo runtime (live preview + Read Outputs button), because the\n'
    'gizmo is the current node during those operations. They fail in\n'
    'this Settings preview (no current node selected) and at submit time.\n'
    'For the node name use {node}. For the Workflow Alias use\n'
    '{workflow_alias}, both in the Template field below.\n'
    '\n'
    'The path is resolved at submit time: TCL expressions are evaluated,\n'
    'then the result is converted to an absolute, normalised path. On\n'
    'Windows, paths starting with \\ are anchored to the current drive.\n'
    'Relative paths and unresolved TCL are rejected with an inline\n'
    'error message; save is blocked until the path is valid.'
    '</p>'
)


def _default_template_native():
    """Return DEFAULT_OUTPUT_TEMPLATE with OS-native separators (display).

    Internally the template engine accepts both '/' and '\\' and normalises
    to '/' before format/runtime, so the displayed default just adopts the
    user's local OS convention for placeholder + Reset.
    """
    if os.sep == '/':
        return DEFAULT_OUTPUT_TEMPLATE
    return DEFAULT_OUTPUT_TEMPLATE.replace('/', os.sep)


class _TemplateField(QtWidgets.QWidget):
    """QLineEdit + Reset for the output path template, with live preview."""

    def __init__(self, output_field, parent=None):
        super().__init__(parent)
        self._output_field = output_field
        self._frame_padding_field = None
        self._version_padding_field = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self._row_lay = row  # exposed for set_locked() lock-indicator append
        self.edit = NukomfyLineEdit()
        self.edit.setPlaceholderText(_default_template_native())
        self.edit.setToolTip(_TEMPLATE_TOOLTIP)
        self.edit.textChanged.connect(self._update_preview)
        self._token_combo = NoWheelComboBox()
        self._token_combo.setToolTip('Insert a token at the cursor position')
        self._token_combo.setFixedHeight(24)
        self._token_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToContents)
        self._token_combo.addItem('Insert token…')
        for _tok in _TOKEN_LIST:
            self._token_combo.addItem(_tok)
        self._token_combo.activated[int].connect(self._on_token_chosen)
        self._last_cursor_pos = 0
        self._cursor_pos_set_by_user = False
        self._reset_btn = QtWidgets.QPushButton('Reset to Defaults')
        set_press_icon(self._reset_btn, SETTINGS_BACKUP_RESTORE)
        self._reset_btn.setFixedHeight(24)
        self._reset_btn.setToolTip('Reset the template to its default value')
        self._reset_btn.clicked.connect(self._reset)
        row.addWidget(self.edit)
        row.addWidget(self._token_combo)
        row.addWidget(self._reset_btn)
        self.edit.cursorPositionChanged.connect(self._remember_cursor_pos)
        self._lock_label = None
        lay.addLayout(row)

        # Preview slot: same stacked-widget pattern as _PathField.
        # OK preview (grey italic with `\u21b3 <resolved>`) and error banner
        # (icon + red message) overlay in the same area, no height jump.
        self._preview = QtWidgets.QLabel(self)
        self._preview.setStyleSheet(_PREVIEW_OK)
        # preview_template returns HTML-escaped text - force RichText so the
        # &nbsp; spacing renders as spaces; under AutoText a resolved path
        # with no real tag is treated as plain text and leaks &nbsp; verbatim.
        self._preview.setTextFormat(QtCore.Qt.RichText)
        self._preview.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._error_banner = make_error_banner(parent=self, font_size=10)

        _slot_h = 18
        self._preview.setFixedHeight(_slot_h)
        self._error_banner.setFixedHeight(_slot_h)

        self._preview_stack = QtWidgets.QStackedWidget(self)
        self._preview_stack.setFixedHeight(_slot_h)
        self._preview_stack.addWidget(self._preview)
        self._preview_stack.addWidget(self._error_banner)
        lay.addWidget(self._preview_stack)

        # Re-render preview whenever the Output Path text changes.
        try:
            self._output_field.edit.textChanged.connect(
                lambda _t: self._update_preview(self.edit.text()))
        except AttributeError:
            pass

    def _reset(self):
        self.edit.setText(_default_template_native())

    def _remember_cursor_pos(self, _old, new):
        self._last_cursor_pos = new
        self._cursor_pos_set_by_user = True

    def _on_token_chosen(self, index):
        if index <= 0:
            return
        token = self._token_combo.itemText(index)
        self._insert_token(token)
        self._token_combo.setCurrentIndex(0)

    def _insert_token(self, token_text):
        text = self.edit.text()
        if self._cursor_pos_set_by_user:
            pos = self._last_cursor_pos
            if pos < 0 or pos > len(text):
                pos = len(text)
        else:
            pos = len(text)
        self.edit.setText(text[:pos] + token_text + text[pos:])
        new_pos = pos + len(token_text)
        self.edit.setCursorPosition(new_pos)
        self._last_cursor_pos = new_pos
        self.edit.setFocus()

    def _output_root(self):
        raw = self._output_field.text().strip() if self._output_field else ''
        if not raw:
            return _DEFAULTS['default_output_path']
        return runtime_path(raw, fallback=raw)

    def _padding_overrides(self):
        """Read live UI values from the padding fields if available, so
        the preview reflects the in-progress Settings before save."""
        def _read(field):
            if field is None:
                return None
            try:
                return int(field.text().strip())
            except (TypeError, ValueError):
                return None
        return _read(self._frame_padding_field), _read(self._version_padding_field)

    def bind_padding_fields(self, frame_field, version_field):
        """Wire two QLineEdit padding fields so the preview re-renders
        as the user types (textChanged) - same UX as Output Path edits.
        """
        self._frame_padding_field = frame_field
        self._version_padding_field = version_field
        for f in (frame_field, version_field):
            try:
                f.textChanged.connect(
                    lambda _t: self._update_preview(self.edit.text()))
            except AttributeError:
                pass

    def _update_preview(self, raw):
        tpl = (raw or '').strip()
        if not tpl:
            self._error_banner.set_message('Template is empty')
            self._preview_stack.setCurrentWidget(self._error_banner)
            return
        ok, err = validate_template(tpl)
        if not ok:
            self._error_banner.set_message(err)
            self._preview_stack.setCurrentWidget(self._error_banner)
            return
        fp, vp = self._padding_overrides()
        resolved = preview_template(tpl, self._output_root(),
                                     placeholder_html_color=ACCENT_GOLD,
                                     frame_padding_override=fp,
                                     version_padding_override=vp)
        if resolved is None:
            self._error_banner.set_message('Invalid template')
            self._preview_stack.setCurrentWidget(self._error_banner)
            return
        # resolved is HTML-escaped; the label is forced to RichText at build
        # time, so &nbsp; renders as spacing instead of leaking verbatim.
        self._preview.setText('\u21b3&nbsp;&nbsp;{}'.format(resolved))
        self._preview_stack.setCurrentWidget(self._preview)

    def set_locked(self, locked, indicator=True):
        """Greyed-out edit + reset button. When `indicator=True`
        (default) also append a lock icon at the end of the same row;
        callers that already render their own lock indicator outside
        the field (e.g. the shared-folders dialog) can pass
        `indicator=False`."""
        self.edit.setEnabled(not locked)
        self._reset_btn.setEnabled(not locked)
        self._token_combo.setEnabled(not locked)
        if locked and indicator and self._lock_label is None:
            self._lock_label = make_lock_label(parent=self)
            self._row_lay.addWidget(self._lock_label)
        elif not locked and self._lock_label is not None:
            self._row_lay.removeWidget(self._lock_label)
            self._lock_label.deleteLater()
            self._lock_label = None

    def text(self):       return self.edit.text()
    def setText(self, v): self.edit.setText(v)


# ---------------------------------------------------------------------------
# Path field with TCL preview
# ---------------------------------------------------------------------------
class _PathField(QtWidgets.QWidget):
    def __init__(self, placeholder='', default_value=None, parent=None):
        super().__init__(parent)
        self._default_value = default_value
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self._row_lay = row  # exposed for set_locked() lock-indicator append
        self.edit = NukomfyLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self._update_preview)
        self._browse_btn = QtWidgets.QPushButton('…')
        self._browse_btn.setFixedWidth(30)
        self._browse_btn.setToolTip('Browse for folder (sets a literal path)')
        self._browse_btn.clicked.connect(self._browse)
        row.addWidget(self.edit)
        row.addWidget(self._browse_btn)
        self._reset_btn = None
        if default_value is not None:
            self._reset_btn = QtWidgets.QPushButton('Reset to Defaults')
            set_press_icon(self._reset_btn, SETTINGS_BACKUP_RESTORE)
            self._reset_btn.setFixedHeight(24)
            self._reset_btn.setToolTip('Reset this path to its default value')
            self._reset_btn.clicked.connect(self._reset)
            row.addWidget(self._reset_btn)
        self._lock_label = None
        lay.addLayout(row)

        # Preview slot: stacked widget so the OK preview and the error
        # banner occupy the exact same area - switching never creates a
        # height jump. Both widgets are forced to the same fixed height
        # so the icon (13px) doesn't visually push the slot taller than
        # the plain text preview (10px italic).
        self._preview = QtWidgets.QLabel(self)
        self._preview.setStyleSheet(_PREVIEW_OK)
        self._preview.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        # Parent the banner to `self` immediately so it's never created
        # as a top-level window - without parent it would briefly appear
        # as a flashing mini-window before being reparented to the stack.
        self._error_banner = make_error_banner(parent=self, font_size=10)

        _slot_h = 18
        self._preview.setFixedHeight(_slot_h)
        self._error_banner.setFixedHeight(_slot_h)

        self._preview_stack = QtWidgets.QStackedWidget(self)
        self._preview_stack.setFixedHeight(_slot_h)
        self._preview_stack.addWidget(self._preview)
        self._preview_stack.addWidget(self._error_banner)
        lay.addWidget(self._preview_stack)

    def _browse(self):
        current = runtime_path(self.edit.text(), fallback='')
        p = _dialogs.get_directory(self, 'Select folder', current)
        if p:
            self.edit.setText(_native_sep(p))

    def _reset(self):
        self.edit.setText(_native_sep(self._default_value))

    def _update_preview(self, raw):
        raw = (raw or '').strip()
        if not raw:
            self._preview.setText('')
            self._preview_stack.setCurrentWidget(self._preview)
            return
        ok, value, reason = canonical_path(raw)
        if not ok:
            self._error_banner.set_message(reason)
            self._preview_stack.setCurrentWidget(self._error_banner)
            return
        self._preview.setText('\u21b3  {}'.format(_native_sep(value)))
        self._preview_stack.setCurrentWidget(self._preview)

    def has_visible_error(self):
        """True when the preview currently shows a red error marker.

        Used by save() to distinguish "visible" errors (user can see the
        red message) from "empty but required" (no visible marker -
        needs its own popup).
        """
        raw = self.edit.text().strip()
        if not raw:
            return False
        ok, _value, _reason = canonical_path(raw)
        return not ok

    def set_locked(self, locked, indicator=True):
        """Greyed-out edit + browse + reset. When `indicator=True`
        (default) also append a lock icon at the end of the same row;
        callers that already render their own lock indicator outside
        the field (e.g. the shared-folders dialog rows) can pass
        `indicator=False`."""
        self.edit.setEnabled(not locked)
        self._browse_btn.setEnabled(not locked)
        if self._reset_btn is not None:
            self._reset_btn.setEnabled(not locked)
        if locked and indicator and self._lock_label is None:
            self._lock_label = make_lock_label(parent=self)
            self._row_lay.addWidget(self._lock_label)
        elif not locked and self._lock_label is not None:
            self._row_lay.removeWidget(self._lock_label)
            self._lock_label.deleteLater()
            self._lock_label = None

    def text(self):      return self.edit.text()
    def setText(self, v): self.edit.setText(_native_sep(v) if v else v)


# ---------------------------------------------------------------------------
# Shared workflow folders manager
# ---------------------------------------------------------------------------
class _SharedFoldersDialog(QtWidgets.QDialog):
    """Modal editor for the list of shared workflow folder paths.

    Why a dialog instead of inline rows: the in-place pattern in the
    Settings Paths tab caused dynamic-height layout issues (rows growing
    indefinitely + scroll cap calculations that fought QSS post-show
    rounding). A dedicated modal sidesteps the entire problem - its own
    scroll area can grow naturally to the dialog height, and the
    Settings tab itself shows a constant-height summary row.

    Modal: blocks the rest of the Nuke UI until closed. The user must
    click Close (or the window X) to return.

    Lifecycle: caller passes the current list, dialog edits an internal
    list of QPathFields, caller reads back via `get_paths()` after exec.
    """

    def __init__(self, initial_paths, parent=None):
        """`initial_paths` is `[(path, locked)]`. Locked rows are
        rendered with a lock icon in place of the Remove button and the
        underlying field is disabled."""
        super().__init__(parent)
        self.setWindowTitle('Shared Workflow Folders')
        self.setModal(True)
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        apply_window_chrome(self)
        # Initial size: ~50% of the Settings dialog, but floored at the
        # minimum width that keeps the header description on a single
        # line (description text width + Add button width + spacing +
        # margins). The user can resize larger; smaller is blocked by
        # `setMinimumWidth` so the header never wraps.
        _desc_text = (
            'Add or remove read-only folders that contain shared or team '
            'workflows. All path fields support Nuke TCL expressions.')
        fm = QtGui.QFontMetrics(self.font())
        _desc_w = fm.horizontalAdvance(_desc_text)
        # Add btn: "+" icon (~16) + " Add Shared Folder" text + padding.
        _add_w = fm.horizontalAdvance(' Add Shared Folder') + 40
        # 14 left margin + 8 spacing + add btn + 14 right margin.
        _min_w = 14 + _desc_w + 8 + _add_w + 14
        self.setMinimumWidth(_min_w)

        # Open at ~50% of the Settings window it was launched from (its
        # parent's top-level), floored at _min_w. Sized off the live parent,
        # not persisted state, so it never reads stale data and never
        # remembers its own size or position across opens.
        parent_win = self.parent().window() if self.parent() is not None else self
        self.resize(max(_min_w, parent_win.width() // 2),
                    max(320, parent_win.height() // 2))

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # Header: description (left) + Add button (right)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        desc = QtWidgets.QLabel(
            'Add or remove read-only folders that contain shared or team '
            'workflows. All path fields support Nuke TCL expressions.')
        desc.setStyleSheet('color:#888;font-size:11px;')
        desc.setWordWrap(True)
        header.addWidget(desc, 1)
        add_btn = QtWidgets.QPushButton(' Add Shared Folder')
        set_press_icon(add_btn, ADD)
        add_btn.setFixedHeight(24)
        add_btn.clicked.connect(lambda: self._add_row(''))
        header.addWidget(add_btn, 0, QtCore.Qt.AlignTop)
        root.addLayout(header)

        # Scrollable list of rows wrapped in a QGroupBox to visually
        # separate the path list from the header (description + Add) and
        # the footer (Save / Cancel). Border + padding give the path
        # block a clear "container" identity coherent with how the
        # Settings tab uses QGroupBox for Workflow / I/O / Substitutions
        # sections.
        grp_paths = QtWidgets.QGroupBox()
        grp_paths.setStyleSheet(
            'QGroupBox{border:1px solid #3a3a3a;border-radius:3px;'
            'margin-top:0px;padding:8px;}')
        grp_lay = QtWidgets.QVBoxLayout(grp_paths)
        grp_lay.setContentsMargins(0, 0, 0, 0)
        grp_lay.setSpacing(0)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            'QScrollArea{background:transparent;border:none;}'
            + SCROLLBAR_STYLE)
        inner = QtWidgets.QWidget()
        self._inner_lay = QtWidgets.QVBoxLayout(inner)
        # Right margin gives the X buttons breathing room from the
        # vertical scrollbar when it appears (without it the X visually
        # touches the scrollbar handle).
        self._inner_lay.setContentsMargins(0, 0, 6, 0)
        self._inner_lay.setSpacing(8)
        self._inner_lay.addStretch(1)  # rows are inserted before the stretch
        self._scroll.setWidget(inner)
        grp_lay.addWidget(self._scroll)
        root.addWidget(grp_paths, 1)

        # Footer: Save + Cancel (right-aligned, standard dialog order).
        # Save validates and applies; Cancel discards every edit since
        # the dialog opened - including additions and removals.
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch()
        save_btn = QtWidgets.QPushButton('Save')
        save_btn.setFixedHeight(24)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save_clicked)
        cancel_btn = QtWidgets.QPushButton('Cancel')
        cancel_btn.setFixedHeight(24)
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(save_btn)
        footer.addWidget(cancel_btn)
        root.addLayout(footer)

        # Track row widget+field+locked tuples. Locked rows come from
        # the settings_overrides/ file: their field is disabled
        # and the Remove button stays in place but is greyed. Add
        # Shared Folder always inserts unlocked rows.
        self._rows = []
        # When ANY initial row is locked we reserve a slot to the right
        # of every Remove button (lock icon on locked rows,
        # invisible spacer on unlocked rows) so the X buttons line up
        # vertically. Without this the locked rows would shift the X
        # button slightly left to make room for the lock icon.
        self._has_locks = any(locked for _path, locked in initial_paths)
        for path, locked in initial_paths:
            self._add_row(path, locked=locked)

    def _add_row(self, path='', locked=False):
        from Nukomfy.gui._lock_indicator import (make_lock_label,
                                                  LOCK_TOOLTIP)
        container = QtWidgets.QWidget()
        cl = QtWidgets.QHBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        field = _PathField('Path or TCL expression…')
        field.setText(path)
        # Locked rows: the field itself goes greyed (no internal lock
        # icon - the dialog renders the indicator outside in the
        # reserved slot), the Remove button stays in place but is
        # disabled.
        if locked:
            field.set_locked(True, indicator=False)
        rm_btn = QtWidgets.QPushButton(CLOSE)
        rm_btn.setFont(icon_font(14))
        rm_btn.setFixedSize(26, 26)
        # The :disabled selector is necessary because the base
        # `color:#ccc` rule would otherwise still apply when the button
        # is disabled (Qt only recolours through stylesheet pseudo-states
        # when explicitly defined). #888 mirrors the Qt-default disabled
        # appearance the adjacent `...` button gets without a custom
        # stylesheet, so the two buttons read as the same family.
        rm_btn.setStyleSheet(
            'QPushButton{color:#ccc;}'
            'QPushButton:disabled{color:#888;}')
        if locked:
            rm_btn.setEnabled(False)
            rm_btn.setToolTip(LOCK_TOOLTIP)
        else:
            rm_btn.clicked.connect(lambda: self._remove_row(container))
        cl.addWidget(field, 1)
        cl.addWidget(rm_btn)
        cl.setAlignment(rm_btn, QtCore.Qt.AlignTop)
        # Reserved slot (lock icon or invisible spacer) to keep the X
        # buttons aligned across rows when at least one is locked. When
        # the whole dialog has no locks, no slot is added so the layout
        # is byte-identical to the lock-free design.
        if self._has_locks:
            if locked:
                slot = make_lock_label(parent=container)
                slot.setToolTip(LOCK_TOOLTIP)
            else:
                slot = QtWidgets.QWidget(container)
            slot.setFixedSize(20, 26)
            cl.addWidget(slot)
            cl.setAlignment(slot, QtCore.Qt.AlignTop)
        # Insert before the trailing stretch so rows stack from the top.
        self._inner_lay.insertWidget(self._inner_lay.count() - 1, container)
        self._rows.append((container, field, locked))

    def _remove_row(self, container):
        self._rows = [(c, f, l) for c, f, l in self._rows
                      if c is not container]
        self._inner_lay.removeWidget(container)
        container.deleteLater()

    def get_user_paths(self):
        """Return the non-empty user-only paths (lock entries excluded).

        Used by the caller after a successful Save. Locked entries come
        from the global override file and are maintained by the admin,
        never written to the user JSON.
        """
        return [f.text().strip() for _c, f, locked in self._rows
                if not locked and f.text().strip()]

    def _on_save_clicked(self):
        """Validate non-empty rows; on any visible error, show a popup
        and keep the dialog open. Empty rows are ignored (dropped on
        accept). Mirrors the same gate the Settings Save uses for inline
        path fields elsewhere in the tab.

        Locked rows are skipped - their content comes verbatim from the
        global override and is the admin's responsibility, not the
        artist's."""
        for _c, field, locked in self._rows:
            if locked:
                continue
            raw = field.text().strip()
            if not raw:
                continue
            if field.has_visible_error():
                _dialogs.warn(
                    self, 'Cannot save settings',
                    'Cannot save. Fix the errors shown below the '
                    'invalid path fields.')
                return
        self.accept()


# ---------------------------------------------------------------------------
# Paths tab
# ---------------------------------------------------------------------------
class _PathsTab(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # State model for shared workflow folders. Edited via the
        # `_SharedFoldersDialog` modal opened by the Manage... button.
        # This list holds only USER-owned paths. Globals from
        # settings_overrides/ are pulled live from
        # settings.get_shared_paths_with_locks() and never persisted in
        # the user JSON.
        self._shared_paths = []
        self._build()
        self._load()

    def _add_locked_field(self, parent_lay, field, key):
        """Add a `_PathField` / `_TemplateField` to `parent_lay`. When
        the settings key is overridden globally, also flip the field
        into locked mode (greyed widgets + lock icon on the same row,
        added inside the field itself for vertical alignment)."""
        if settings.is_locked(key):
            field.set_locked(True)
        parent_lay.addWidget(field)

    def _build(self):
        _PH = 'Path or TCL expression…'
        _DESC = 'color:#888;font-size:11px;'

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(14, 14, 14, 14)

        # TCL note - once at top
        tcl_note = QtWidgets.QLabel('All path fields support Nuke TCL expressions.')
        tcl_note.setStyleSheet('color:#888;font-size:11px;')
        root.addWidget(tcl_note)

        # --- Workflow Paths group ---
        grp_wf = QtWidgets.QGroupBox('Workflow Paths')
        grp_wf.setStyleSheet(_SECTION_GROUP_STYLE)
        wf_lay = QtWidgets.QVBoxLayout(grp_wf)
        wf_lay.setSpacing(6)

        # Local Workflow Folder
        lbl_local = QtWidgets.QLabel('Local Workflow Folder')
        wf_lay.addWidget(lbl_local)
        desc_local = QtWidgets.QLabel('Main folder where your local workflows are stored.')
        desc_local.setStyleSheet(_DESC)
        wf_lay.addWidget(desc_local)
        self.local_field = _PathField(_PH, _DEFAULTS['local_workflow_path'])
        self._add_locked_field(wf_lay, self.local_field, 'local_workflow_path')
        if settings.disable_local_workflows:
            # Local workflows disabled: grey the field and explain why via a
            # container tooltip (shown over the disabled child widgets). The
            # lock icon stays reserved for a real path override, so add it
            # here only when _add_locked_field did not already (no override).
            if not settings.is_locked('local_workflow_path'):
                self.local_field.set_locked(True, indicator=False)
            self.local_field.setToolTip(
                'Local workflows are disabled. Only shared workflow '
                'folders are used.')
        # Block separator - Local block -> Shared block.
        wf_lay.addSpacing(14)

        # Shared Workflow Folder (read-only): constant-height summary
        # line + Manage button that opens a modal editor dialog. Avoids
        # the dynamic-height layout issues an in-place rows pattern
        # would cause.
        lbl_shared = QtWidgets.QLabel('Shared Workflow Folders (read-only)')
        wf_lay.addWidget(lbl_shared)
        desc_shared = QtWidgets.QLabel('Additional read-only folders for shared or team workflows.')
        desc_shared.setStyleSheet(_DESC)
        wf_lay.addWidget(desc_shared)

        # Summary line: "{n} folder(s) configured"
        self._shared_summary = QtWidgets.QLabel('')
        self._shared_summary.setStyleSheet('color:#bbb;')
        wf_lay.addWidget(self._shared_summary)

        # Manage button on its own row below the summary, left-aligned
        # (mirrors the "Add Shared Folder" placement of the previous
        # in-line layout - keeps the action discoverable next to the
        # description, not orphaned to the right).
        manage_row = QtWidgets.QHBoxLayout()
        manage_btn = QtWidgets.QPushButton(' Manage Shared Folders')
        set_press_icon(manage_btn, LIST)
        manage_btn.setFixedHeight(24)
        # Width sized to fit the longer label without ellipsis. Mirrors
        # the pattern of "Add Shared Folder" in the dialog header so the
        # two affordances feel like the same family.
        manage_btn.setFixedWidth(200)
        if settings.lock_shared_folders:
            # Shared folders are locked to the override: the user cannot
            # add/edit/remove them. Disable the button and explain why via
            # a wrapper tooltip (a disabled button shows no tooltip itself).
            manage_btn.setEnabled(False)
            manage_wrap = QtWidgets.QWidget()
            _mwl = QtWidgets.QHBoxLayout(manage_wrap)
            _mwl.setContentsMargins(0, 0, 0, 0)
            _mwl.addWidget(manage_btn)
            manage_wrap.setToolTip(
                'Shared workflow folders are managed by the settings '
                'override and cannot be changed here.')
            manage_row.addWidget(manage_wrap)
        else:
            manage_btn.setToolTip(
                'Open a dialog to add, remove or edit the shared workflow '
                'folders.\n\n'
                'Changes apply when you click Save inside the dialog.')
            manage_btn.clicked.connect(self._open_shared_manager)
            manage_row.addWidget(manage_btn)
        manage_row.addStretch()
        wf_lay.addLayout(manage_row)

        # State model: list[str]. Owns the canonical path values; the
        # manager dialog edits a working copy and writes back on Close.
        self._shared_paths = []

        root.addWidget(grp_wf)

        # --- I/O Paths group ---
        grp_io = QtWidgets.QGroupBox('Input / Output Paths')
        grp_io.setStyleSheet(_SECTION_GROUP_STYLE)
        io_lay = QtWidgets.QVBoxLayout(grp_io)
        io_lay.setSpacing(6)

        # Input Cache Path
        lbl_cache = QtWidgets.QLabel('Input Cache Path')
        io_lay.addWidget(lbl_cache)
        desc_cache = QtWidgets.QLabel('Temporary folder where input files are prepared for ComfyUI.')
        desc_cache.setStyleSheet(_DESC)
        io_lay.addWidget(desc_cache)
        self.input_cache_field = _PathField(_PH, _DEFAULTS['default_input_cache_path'])
        self._add_locked_field(io_lay, self.input_cache_field,
                               'default_input_cache_path')

        # Breathing room between the resolved path line and the TTL row.
        io_lay.addSpacing(6)

        # Auto-delete row: [ ] Auto-delete ... [N] days [Clear button]
        cache_opts_row = QtWidgets.QHBoxLayout()
        cache_opts_row.setSpacing(6)
        self._cache_ttl_chk = QtWidgets.QCheckBox(
            'Auto-delete input cache not used for more than')
        self._cache_ttl_chk.setToolTip(
            "On plugin startup, delete input cache folders whose "
            "'last used' timestamp is older than the threshold.\n\n"
            "Age is tracked inside each cache folder, "
            "updated on every submit that uses the cache.")
        self._cache_ttl_days = NukomfyLineEdit()
        self._cache_ttl_days.setValidator(QtGui.QIntValidator(1, 365, self))
        self._cache_ttl_days.setFixedWidth(50)
        self._cache_ttl_days.setAlignment(QtCore.Qt.AlignRight)
        self._cache_ttl_chk.toggled.connect(self._cache_ttl_days.setEnabled)
        attach_int_clamp(self._cache_ttl_days, 1, 365, 7)
        days_lbl = QtWidgets.QLabel('days')
        cache_opts_row.addWidget(self._cache_ttl_chk)
        cache_opts_row.addWidget(self._cache_ttl_days)
        cache_opts_row.addWidget(days_lbl)
        # Lock indicator next to the TTL controls when the global file
        # overrides `input_cache_max_age_days`. The Clear button
        # to the right is a manual action, NOT covered by the override.
        if settings.is_locked('input_cache_max_age_days'):
            self._cache_ttl_chk.setEnabled(False)
            self._cache_ttl_days.setEnabled(False)
            cache_opts_row.addWidget(make_lock_label(parent=self))

        # Small gap, then the Clear button right next to "days".
        cache_opts_row.addSpacing(12)
        self._cache_clean_btn = QtWidgets.QPushButton(' Clear My Input Cache')
        set_press_icon(self._cache_clean_btn, DELETE_SWEEP)
        self._cache_clean_btn.setFixedHeight(24)
        self._cache_clean_btn.setToolTip(
            'Delete every input cache folder belonging to the current OS '
            'user under the resolved path.\n\n'
            'Other users\' caches on the same share are not touched.')
        self._cache_clean_btn.clicked.connect(self._clean_all_cache)
        cache_opts_row.addWidget(self._cache_clean_btn)
        cache_opts_row.addStretch()
        io_lay.addLayout(cache_opts_row)

        # Per-submit cleanup of unused fp variants.
        cache_opts_row2 = QtWidgets.QHBoxLayout()
        cache_opts_row2.setSpacing(6)
        self._cleanup_variants_chk = QtWidgets.QCheckBox(
            'Delete unused variants of this input cache on submit')
        self._cleanup_variants_chk.setToolTip(
            'When enabled, at the start of each submit Nukomfy deletes '
            'older variants of the same input cache\n'
            'that no running render is using.\n\n'
            'A new variant is created each time you change input '
            'parameters or upstream connections, so they accumulate '
            'over time.\n\n'
            'Only your own variants are touched. Disable to keep them '
            'all on disk (relies on the TTL setting above for cleanup).')
        self._cleanup_variants_chk.toggled.connect(
            self._on_cleanup_variants_toggled)
        cache_opts_row2.addWidget(self._cleanup_variants_chk)
        if settings.is_locked('delete_unused_variants_on_submit'):
            self._cleanup_variants_chk.setEnabled(False)
            cache_opts_row2.addWidget(make_lock_label(parent=self))
        cache_opts_row2.addStretch()
        io_lay.addLayout(cache_opts_row2)
        # Block separator before the next section (Output Path). Needs
        # to visibly exceed the 6px default layout spacing between a
        # field and the label of the next block.
        io_lay.addSpacing(14)

        # Output Path
        lbl_out = QtWidgets.QLabel('Output Path')
        io_lay.addWidget(lbl_out)
        desc_out = QtWidgets.QLabel('Default folder where ComfyUI writes rendered output files.')
        desc_out.setStyleSheet(_DESC)
        io_lay.addWidget(desc_out)
        self.output_field = _PathField(_PH, _DEFAULTS['default_output_path'])
        self.output_field.edit.setToolTip(_OUTPUT_PATH_TOOLTIP)
        self._add_locked_field(io_lay, self.output_field, 'default_output_path')
        # Block separator - Output Path block -> Output Path Template block.
        io_lay.addSpacing(14)

        # Output Path Template
        lbl_tpl = QtWidgets.QLabel('Output Path Template')
        io_lay.addWidget(lbl_tpl)
        desc_tpl = QtWidgets.QLabel(
            'Customize the folder/file structure for rendered outputs. '
            'The Output Path above is automatically prepended.')
        desc_tpl.setStyleSheet(_DESC)
        desc_tpl.setWordWrap(True)
        io_lay.addWidget(desc_tpl)
        self.template_field = _TemplateField(self.output_field)
        self._add_locked_field(io_lay, self.template_field,
                               'output_path_template')

        # Padding settings clustered with Output Path + Template so all
        # "how the output path is built" controls live together.
        # Lock indicator follows the standard pattern (after the
        # spinbox, before the trailing stretch).
        io_lay.addSpacing(6)
        pad_row = QtWidgets.QHBoxLayout()
        pad_row.setSpacing(6)

        pad_row.addWidget(QtWidgets.QLabel('Frame Padding'))
        self._frame_padding_field = NukomfyLineEdit()
        self._frame_padding_field.setValidator(QtGui.QIntValidator(1, 9, self))
        self._frame_padding_field.setFixedWidth(30)
        self._frame_padding_field.setAlignment(QtCore.Qt.AlignRight)
        attach_int_clamp(self._frame_padding_field, 1, 9, 4)
        self._frame_padding_field.setToolTip(
            'Frame number padding applied to every NukomfyWrite.\n'
            'Range 1-9, default 4. Affects how the {frame} token expands '
            'in the output path and what NukomfyWrite writes to disk.\n'
            'Out-of-range values clamp automatically on focus-out.')
        pad_row.addWidget(self._frame_padding_field)
        if settings.is_locked('frame_padding'):
            self._frame_padding_field.setEnabled(False)
            pad_row.addWidget(make_lock_label(parent=self))

        pad_row.addSpacing(20)
        pad_row.addWidget(QtWidgets.QLabel('Version Padding'))
        self._version_padding_field = NukomfyLineEdit()
        self._version_padding_field.setValidator(QtGui.QIntValidator(1, 9, self))
        self._version_padding_field.setFixedWidth(30)
        self._version_padding_field.setAlignment(QtCore.Qt.AlignRight)
        attach_int_clamp(self._version_padding_field, 1, 9, 3)
        self._version_padding_field.setToolTip(
            'Version string padding for the {version} token.\n'
            'Range 1-9, default 3. Examples: 3 -> v001, 4 -> v0001, '
            '5 -> v00001.\n'
            'Out-of-range values clamp automatically on focus-out.')
        pad_row.addWidget(self._version_padding_field)
        if settings.is_locked('version_padding'):
            self._version_padding_field.setEnabled(False)
            pad_row.addWidget(make_lock_label(parent=self))

        pad_row.addStretch(1)
        io_lay.addLayout(pad_row)

        # Bind padding fields to the template preview so it re-renders
        # live as the user edits the spinboxes.
        self.template_field.bind_padding_fields(
            self._frame_padding_field, self._version_padding_field)

        root.addWidget(grp_io)

        # --- Path Substitutions group ---
        grp_sub = QtWidgets.QGroupBox('Path Substitutions')
        grp_sub.setStyleSheet(_SECTION_GROUP_STYLE)
        sub_lay = QtWidgets.QVBoxLayout(grp_sub)
        sub_lay.setSpacing(6)

        desc_sub = QtWidgets.QLabel(
            "Use Nuke's built-in path substitution rules (Settings > General > "
            'Path Substitutions) when submitting to a remote machine with '
            'a different OS.\nPaths are translated automatically both ways.')
        desc_sub.setStyleSheet(_DESC)
        desc_sub.setWordWrap(True)
        sub_lay.addWidget(desc_sub)

        self._sub_enabled_chk = QtWidgets.QCheckBox(
            "Apply Nuke path substitution rules")
        if settings.is_locked('path_substitution_enabled'):
            self._sub_enabled_chk.setEnabled(False)
            sub_chk_row = QtWidgets.QHBoxLayout()
            sub_chk_row.setContentsMargins(0, 0, 0, 0)
            sub_chk_row.addWidget(self._sub_enabled_chk)
            sub_chk_row.addWidget(make_lock_label(parent=self))
            sub_chk_row.addStretch(1)
            sub_lay.addLayout(sub_chk_row)
        else:
            sub_lay.addWidget(self._sub_enabled_chk)

        root.addWidget(grp_sub)
        root.addStretch()

    def _update_shared_summary(self):
        """Refresh the count line shown next to the Manage... button.

        Counts the merged view (globals + user) so the user sees the
        total number of shared roots that the Library will scan."""
        global_count = sum(
            1 for _p, locked in settings.get_shared_paths_with_locks()
            if locked)
        # When shared folders are locked to the override, user-added paths
        # are ignored, so the count reflects the override paths only.
        if settings.lock_shared_folders:
            n = global_count
        else:
            n = global_count + len(self._shared_paths)
        if n == 0:
            txt = 'No shared folders configured'
        elif n == 1:
            txt = '1 shared folder configured'
        else:
            txt = '{} shared folders configured'.format(n)
        self._shared_summary.setText(txt)

    def _open_shared_manager(self):
        """Open the modal editor for shared workflow folders.

        On Save (`exec_()` returns `Accepted`) the dialog's user-only
        paths replace `self._shared_paths`. Locked entries come from
        the global override and are shown as read-only rows in the
        dialog; they are never persisted into the user JSON. On Cancel
        (`Rejected`) all edits are discarded.
        """
        # Build the merged view: globals from settings_overrides/ first
        # (locked, as listed by the admin), followed by the user's own
        # in-flight paths (unsaved edits live in self._shared_paths).
        global_locked = [(p, True)
                         for p, locked in settings.get_shared_paths_with_locks()
                         if locked]
        user_unlocked = [(p, False) for p in self._shared_paths]
        dlg = _SharedFoldersDialog(global_locked + user_unlocked, parent=self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._shared_paths = list(dlg.get_user_paths())
            self._update_shared_summary()

    def _load(self):
        self.local_field.setText(settings.local_workflow_path)
        self.input_cache_field.setText(settings.default_input_cache_path)
        self.output_field.setText(settings.default_output_path)
        self.template_field.setText(settings.output_path_template)
        # Only the user-owned paths populate the editable model;
        # globals are pulled live from settings each time the dialog or
        # the summary line refreshes.
        self._shared_paths = [
            p for p, locked in settings.get_shared_paths_with_locks()
            if not locked
        ]
        self._update_shared_summary()
        self._sub_enabled_chk.setChecked(settings.path_substitution_enabled)
        # TTL widgets. Force-disable the days field when locked so the
        # checkbox->days `toggled` connection (set in _build) doesn't
        # re-enable it through the side door.
        ttl_days = int(settings.input_cache_max_age_days or 0)
        ttl_locked = settings.is_locked('input_cache_max_age_days')
        self._cache_ttl_chk.setChecked(ttl_days > 0)
        self._cache_ttl_days.setText(str(ttl_days if ttl_days > 0 else 7))
        self._cache_ttl_days.setEnabled(ttl_days > 0 and not ttl_locked)
        # Cleanup variants on submit
        cleanup_on = bool(getattr(
            settings, 'delete_unused_variants_on_submit', True))
        # block toggled signal during load so we don't fire the warning
        # popup when restoring user preference
        self._cleanup_variants_chk.blockSignals(True)
        self._cleanup_variants_chk.setChecked(cleanup_on)
        self._cleanup_variants_chk.blockSignals(False)
        # Padding fields
        self._frame_padding_field.setText(str(settings.frame_padding))
        self._version_padding_field.setText(str(settings.version_padding))

    def _on_cleanup_variants_toggled(self, checked):
        """Warn the user before disabling auto-cleanup (popup explains
        the trade-off: no auto-cleanup means variants accumulate)."""
        if checked:
            return  # Re-enabling needs no confirmation
        reply = _dialogs.warn(
            self,
            'Disable automatic variant cleanup?',
            'Each submit with different input parameters writes a new '
            'cache variant of this input alongside the existing ones.\n'
            'Without automatic cleanup, your input cache folder may grow '
            'significantly.\n\n'
            'Variants will only be removed if you click "Clear My Input '
            'Cache" manually, or if the auto-delete TTL above is '
            'configured.\n\nContinue?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if reply != QtWidgets.QMessageBox.Yes:
            # User declined -> revert silently
            self._cleanup_variants_chk.blockSignals(True)
            self._cleanup_variants_chk.setChecked(True)
            self._cleanup_variants_chk.blockSignals(False)

    def _clean_all_cache(self):
        """Confirm + wipe every input-cache leaf under the picker path.

        Uses the currently-edited picker value (not saved settings) so
        the user doesn't have to Save first. Explicit user action: deletes
        caches even if their state file is unreadable.

        Refuses up-front when the picker holds anything we can't safely
        act on - empty, broken TCL, or non-absolute - so we never scan
        a bogus location (a default fallback would also be wrong: the
        user clearly meant *this* path, not whatever default hides
        behind it).
        """
        import Nukomfy.data.input_cache_cleanup as input_cache_cleanup
        raw = self.input_cache_field.text().strip()
        if not raw or self.input_cache_field.has_visible_error():
            _dialogs.warn(
                self, 'Cannot clear cache',
                'Cannot delete. The Input Cache Path is invalid. '
                'Fix the field and try again.')
            return
        # Refuse to scan dangerous roots: with `C:\` or `C:\Windows` the
        # 2-level walk does hundreds of thousands of stat calls before
        # returning empty, which freezes the UI thread for minutes.
        kind, resolved = _classify_unsafe_cache_root(raw)
        if kind is not None:
            _show_unsafe_cache_path_dialog(self, kind, resolved)
            return
        base = runtime_path(raw, fallback=raw)
        entries = input_cache_cleanup.scan(base)

        if not entries:
            _dialogs.inform(
                self, 'No input cache found',
                'No input cache found at:\n{}'.format(_native_sep(base)))
            return

        total_bytes = sum(e['size_bytes'] for e in entries)
        size_str = _humanize_bytes(total_bytes)

        scope_native = _native_sep(input_cache_cleanup.user_scope_root(base))
        paths_native = [_native_sep(e['path']) for e in entries]

        dlg = _PathListDialog(
            title='Clear my input cache',
            primary=(
                'You are about to delete {} cache folder{} belonging to '
                'the current OS user under:\n{}'
                .format(len(entries), '' if len(entries) == 1 else 's',
                        scope_native)),
            informative=(
                'Total size: {}.\n\n'
                'Other users\' caches on the same share are not touched. '
                'This cannot be undone. Caches will regenerate on next '
                'submit.'.format(size_str)),
            paths=paths_native,
            buttons='YES_NO',
            icon='warning',
            parent=self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        n, freed = input_cache_cleanup.purge_all(base)

        if n:
            _log.info(
                'Input cache cleared: %d dirs (%.1f MB) at %s',
                n, freed / 1e6, input_cache_cleanup.user_scope_root(base))

        freed_str = _humanize_bytes(freed)
        _dialogs.inform(
            self, 'Input cache cleared',
            'Freed {} ({} folders).'.format(freed_str, n))

    def save(self):
        # Two-tier validation:
        #   (1) "visible" errors - bad TCL, non-absolute, bad template -
        #       already show in red under the offending field. Block
        #       save with a generic popup; no need to list the fields.
        #   (2) "empty required" - Local Workflow Folder, Input Cache
        #       Path, Default Output Path. The preview line is blank
        #       in this case (nothing to highlight), so surface it
        #       with a dedicated popup AFTER the user has cleared all
        #       the visible red errors.
        # Shared workflow rows are optional (empty rows are dropped at
        # save). The `_FALLBACK_ON_EMPTY` set in settings.py guards the
        # single-path fields: if one is blanked out, the read path returns
        # the default so runtime never sees an empty path.
        # Locked fields are skipped from every gate - their value
        # comes from the admin's global file and is not the artist's
        # responsibility. The setattr calls at the bottom are gated too;
        # the settings setter silently no-ops on locked keys (defence
        # in depth), but skipping them keeps the intent clear.
        has_visible_error = False
        for field, key in ((self.local_field, 'local_workflow_path'),
                           (self.input_cache_field,
                            'default_input_cache_path'),
                           (self.output_field, 'default_output_path')):
            if settings.is_locked(key):
                continue
            if field.has_visible_error():
                has_visible_error = True
        # Final validation gate for shared paths: the manager dialog
        # already shows red banners while editing, but a user could click
        # Save without re-opening Manage after settings load. Re-validate.
        # `self._shared_paths` excludes globals, so every entry is
        # user-owned and validation-eligible.
        for raw in self._shared_paths:
            if not raw.strip():
                continue
            ok, _val, _why = canonical_path(raw)
            if not ok:
                has_visible_error = True

        tpl = self.template_field.text().strip()
        if not settings.is_locked('output_path_template'):
            tpl_ok, _tpl_err = validate_template(tpl)
            if not tpl_ok:
                has_visible_error = True

        if has_visible_error:
            _dialogs.warn(
                self, 'Cannot save settings',
                'Cannot save settings. Fix the errors shown '
                'below the fields.')
            return False

        empty_required = False
        if (not settings.is_locked('local_workflow_path')
                and not self.local_field.text().strip()):
            empty_required = True
        if (not settings.is_locked('default_input_cache_path')
                and not self.input_cache_field.text().strip()):
            empty_required = True
        if (not settings.is_locked('default_output_path')
                and not self.output_field.text().strip()):
            empty_required = True
        if empty_required:
            _dialogs.warn(
                self, 'Cannot save settings',
                'Cannot save settings. Some fields are empty.')
            return False

        # Refuse cache roots that the backend would refuse at delete time
        # (drive roots, the home directory, well-known system folders).
        # Skipped when the cache path is locked - admin's call.
        if not settings.is_locked('default_input_cache_path'):
            kind, resolved = _classify_unsafe_cache_root(
                self.input_cache_field.text())
            if kind is not None:
                _show_unsafe_cache_path_dialog(self, kind, resolved)
                return False

        if not settings.is_locked('local_workflow_path'):
            settings.local_workflow_path = self.local_field.text().strip()
        # `shared_workflow_paths` is partially-lockable: the setter strips
        # globals automatically. Always assign - what we hand it is
        # already user-only since `self._shared_paths` excludes locked.
        settings.shared_workflow_paths = [
            p.strip() for p in self._shared_paths if p.strip()
        ]
        if not settings.is_locked('default_input_cache_path'):
            settings.default_input_cache_path = (
                self.input_cache_field.text().strip())
        if not settings.is_locked('default_output_path'):
            settings.default_output_path = self.output_field.text().strip()
        if not settings.is_locked('output_path_template'):
            settings.output_path_template = tpl
        if not settings.is_locked('path_substitution_enabled'):
            settings.path_substitution_enabled = (
                self._sub_enabled_chk.isChecked())
        if not settings.is_locked('input_cache_max_age_days'):
            if self._cache_ttl_chk.isChecked():
                settings.input_cache_max_age_days = clamp_int_field(
                    self._cache_ttl_days, 1, 365, 7)
            else:
                settings.input_cache_max_age_days = 0
        if not settings.is_locked('delete_unused_variants_on_submit'):
            settings.delete_unused_variants_on_submit = (
                self._cleanup_variants_chk.isChecked())
        # Padding fields. Coerced via attach_int_clamp at edit time, so
        # the value is already in [1, 9].
        if not settings.is_locked('frame_padding'):
            settings.frame_padding = clamp_int_field(
                self._frame_padding_field, 1, 9, 4)
        if not settings.is_locked('version_padding'):
            settings.version_padding = clamp_int_field(
                self._version_padding_field, 1, 9, 3)
        settings.save()
        return True


# ---------------------------------------------------------------------------
# UI tab - reset UI state to defaults
# ---------------------------------------------------------------------------
class _UITab(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        _DESC_STYLE = 'color:#888;font-size:11px;font-weight:normal;'

        # --- Window behaviour ---------------------------------------------
        wgrp = QtWidgets.QGroupBox('Window Behavior')
        wgrp.setStyleSheet(_SECTION_GROUP_STYLE)
        wl = QtWidgets.QVBoxLayout(wgrp)
        wl.setSpacing(6)
        winfo = QtWidgets.QLabel(
            'Set whether each panel floats above Nuke or behaves '
            'like a regular window. Takes effect on next open.')
        winfo.setStyleSheet(_DESC_STYLE)
        winfo.setWordWrap(True)
        wl.addWidget(winfo)

        wgrid = QtWidgets.QGridLayout()
        wgrid.setHorizontalSpacing(24)
        wgrid.setVerticalSpacing(4)
        wgrid.setContentsMargins(4, 4, 4, 0)

        hdr_panel = QtWidgets.QLabel('Panel')
        hdr_panel.setStyleSheet('color:#aaa;font-size:11px;font-weight:bold;')
        hdr_top = QtWidgets.QLabel('Always on Top')
        hdr_top.setStyleSheet('color:#aaa;font-size:11px;font-weight:bold;')
        hdr_top.setAlignment(QtCore.Qt.AlignCenter)
        wgrid.addWidget(hdr_panel, 0, 0)
        wgrid.addWidget(hdr_top, 0, 1)

        # Tiny vertical breather between header labels and the first
        # checkbox row so the header reads as a header (not just row 0).
        wgrid.addItem(
            QtWidgets.QSpacerItem(
                0, 6,
                QtWidgets.QSizePolicy.Minimum,
                QtWidgets.QSizePolicy.Fixed),
            1, 0, 1, 2)

        self._keep_on_top_chks = {}
        keep_on_top_rows = [
            ('Library', 'library_keep_on_top'),
            ('Render Manager', 'render_manager_keep_on_top'),
        ]
        for row_idx, (label_txt, key) in enumerate(keep_on_top_rows, start=2):
            lbl = QtWidgets.QLabel(label_txt)
            lbl.setStyleSheet('color:#ddd;font-size:11px;font-weight:normal;')
            wgrid.addWidget(lbl, row_idx, 0)
            chk = QtWidgets.QCheckBox()
            chk.setChecked(bool(getattr(settings, key)))
            chk.setToolTip(
                'Keep {} above the Nuke main window.\n\n'
                'Trade-offs:\n'
                '• No minimize button\n'
                '• No separate taskbar/dock icon\n'
                '• Maximize via double-click on the title bar\n\n'
                'Applies on next panel open.'.format(label_txt))
            self._keep_on_top_chks[key] = chk
            cell = QtWidgets.QWidget()
            hb = QtWidgets.QHBoxLayout(cell)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(4)
            hb.addStretch(1)
            hb.addWidget(chk)
            # Always reserve the padlock slot so the checkbox sits at the
            # same x-offset whether the row is locked or not. The padlock
            # is hidden on unlocked rows but its size is kept.
            lock_lbl = make_lock_label(parent=cell)
            if settings.is_locked(key):
                chk.setEnabled(False)
            else:
                sp = lock_lbl.sizePolicy()
                sp.setRetainSizeWhenHidden(True)
                lock_lbl.setSizePolicy(sp)
                lock_lbl.setVisible(False)
                chk.toggled.connect(
                    lambda checked, k=key:
                    self._on_keep_on_top_toggled(k, checked))
            hb.addWidget(lock_lbl)
            hb.addStretch(1)
            wgrid.addWidget(cell, row_idx, 1)

        wgrid.setColumnStretch(0, 0)
        wgrid.setColumnStretch(1, 0)
        wgrid.setColumnStretch(2, 1)
        wl.addLayout(wgrid)
        lay.addWidget(wgrp)

        # --- Gizmo --------------------------------------------------------
        ggrp = QtWidgets.QGroupBox('Gizmo')
        ggrp.setStyleSheet(_SECTION_GROUP_STYLE)
        glay = QtWidgets.QVBoxLayout(ggrp)
        glay.setSpacing(6)
        ginfo = QtWidgets.QLabel(
            'Options applied to each new gizmo when you create it in Nuke.')
        ginfo.setStyleSheet(_DESC_STYLE)
        ginfo.setWordWrap(True)
        glay.addWidget(ginfo)

        self._gizmo_name_prefix_chk = QtWidgets.QCheckBox(
            'Add Nukomfy prefix to gizmo name')
        self._gizmo_name_prefix_chk.setChecked(
            bool(settings.gizmo_name_prefix))
        self._gizmo_name_prefix_chk.setToolTip(
            'Add the "Nukomfy_" prefix to a new gizmo\'s node name '
            '(e.g. Nukomfy_MyWorkflow).\n'
            'When off, the node uses the workflow name only.\n\n'
            'Applies to gizmos created from now on; existing nodes keep '
            'their name.')
        if settings.is_locked('gizmo_name_prefix'):
            self._gizmo_name_prefix_chk.setEnabled(False)
            gizmo_chk_row = QtWidgets.QHBoxLayout()
            gizmo_chk_row.setContentsMargins(0, 0, 0, 0)
            gizmo_chk_row.addWidget(self._gizmo_name_prefix_chk)
            gizmo_chk_row.addWidget(make_lock_label(parent=self))
            gizmo_chk_row.addStretch(1)
            glay.addLayout(gizmo_chk_row)
        else:
            self._gizmo_name_prefix_chk.toggled.connect(
                self._on_gizmo_name_prefix_toggled)
            glay.addWidget(self._gizmo_name_prefix_chk)

        self._gizmo_disable_group_view_chk = QtWidgets.QCheckBox(
            'Disable Group View (Nuke 16+)')
        self._gizmo_disable_group_view_chk.setChecked(
            bool(settings.gizmo_disable_group_view))
        self._gizmo_disable_group_view_chk.setToolTip(
            'Create new gizmos with Group View turned off, so showing a '
            "gizmo's internals on the node graph is not available.\n"
            'When off, new gizmos allow Group View like any other group.\n\n'
            'Group View is a Nuke 16 feature; on older versions this '
            'setting has no effect.\n'
            'Applies to gizmos created from now on; existing nodes are '
            'unchanged.')
        if settings.is_locked('gizmo_disable_group_view'):
            self._gizmo_disable_group_view_chk.setEnabled(False)
            group_view_chk_row = QtWidgets.QHBoxLayout()
            group_view_chk_row.setContentsMargins(0, 0, 0, 0)
            group_view_chk_row.addWidget(self._gizmo_disable_group_view_chk)
            group_view_chk_row.addWidget(make_lock_label(parent=self))
            group_view_chk_row.addStretch(1)
            glay.addLayout(group_view_chk_row)
        else:
            self._gizmo_disable_group_view_chk.toggled.connect(
                self._on_gizmo_disable_group_view_toggled)
            glay.addWidget(self._gizmo_disable_group_view_chk)
        lay.addWidget(ggrp)

        # --- Library Card Fields -----------------------------------------
        cgrp = QtWidgets.QGroupBox('Library Card Fields')
        cgrp.setStyleSheet(_SECTION_GROUP_STYLE)
        cl = QtWidgets.QVBoxLayout(cgrp)
        cl.setSpacing(6)
        cinfo = QtWidgets.QLabel(
            'Show or hide individual fields on each Library card. '
            'The card height adjusts to what is visible, separately '
            'for grid and list view.')
        cinfo.setStyleSheet(_DESC_STYLE)
        cinfo.setWordWrap(True)
        cl.addWidget(cinfo)

        cgrid = QtWidgets.QGridLayout()
        cgrid.setHorizontalSpacing(24)
        cgrid.setVerticalSpacing(4)
        cgrid.setContentsMargins(4, 4, 4, 0)

        hdr_field = QtWidgets.QLabel('Field')
        hdr_field.setStyleSheet('color:#aaa;font-size:11px;font-weight:bold;')
        hdr_grid_lbl = QtWidgets.QLabel('Grid view')
        hdr_grid_lbl.setStyleSheet('color:#aaa;font-size:11px;font-weight:bold;')
        hdr_grid_lbl.setAlignment(QtCore.Qt.AlignCenter)
        hdr_list_lbl = QtWidgets.QLabel('List view')
        hdr_list_lbl.setStyleSheet('color:#aaa;font-size:11px;font-weight:bold;')
        hdr_list_lbl.setAlignment(QtCore.Qt.AlignCenter)
        cgrid.addWidget(hdr_field, 0, 0)
        cgrid.addWidget(hdr_grid_lbl, 0, 1)
        cgrid.addWidget(hdr_list_lbl, 0, 2)

        # Tiny vertical breather between header labels and the first
        # checkbox row so the header reads as a header (not just row 0).
        cgrid.addItem(
            QtWidgets.QSpacerItem(
                0, 6,
                QtWidgets.QSizePolicy.Minimum,
                QtWidgets.QSizePolicy.Fixed),
            1, 0, 1, 3)

        self._card_field_chks = {}
        card_fields = [
            ('Version', 'version'),
            ('Author', 'author'),
            ('Description', 'description'),
            ('Categories', 'categories'),
            ('Models', 'models'),
            ('Source', 'source'),
        ]
        for row_idx, (label_txt, suffix) in enumerate(card_fields, start=2):
            lbl = QtWidgets.QLabel(label_txt)
            lbl.setStyleSheet('color:#ddd;font-size:11px;font-weight:normal;')
            cgrid.addWidget(lbl, row_idx, 0)
            # The Source field has no effect when local workflows are
            # disabled (every card is shared): grey it like the local path
            # field, with a tooltip explaining why. A real override still
            # shows the lock icon on top.
            src_disabled = (settings.disable_local_workflows
                            and suffix == 'source')
            for col_idx, mode in enumerate(('grid', 'list'), start=1):
                key = 'library_{}_show_{}'.format(mode, suffix)
                chk = QtWidgets.QCheckBox()
                chk.setChecked(bool(getattr(settings, key)))
                if src_disabled:
                    chk.setToolTip('Local workflows are disabled, so the '
                                   'Source field is not used.')
                else:
                    chk.setToolTip(
                        'Show "{}" on {} view cards.'.format(label_txt, mode))
                self._card_field_chks[key] = chk
                cell = QtWidgets.QWidget()
                hb = QtWidgets.QHBoxLayout(cell)
                hb.setContentsMargins(0, 0, 0, 0)
                hb.setSpacing(4)
                hb.addStretch(1)
                hb.addWidget(chk)
                # Always reserve the padlock slot so the checkbox sits at
                # the same x-offset whether the row is locked or not. The
                # padlock is hidden on unlocked rows but its size is kept
                # (Qt's retainSizeWhenHidden).
                lock_lbl = make_lock_label(parent=cell)
                if settings.is_locked(key):
                    chk.setEnabled(False)
                else:
                    sp = lock_lbl.sizePolicy()
                    sp.setRetainSizeWhenHidden(True)
                    lock_lbl.setSizePolicy(sp)
                    lock_lbl.setVisible(False)
                    if src_disabled:
                        # Greyed by the disable-local setting; no lock icon
                        # (the lock is reserved for real overrides).
                        chk.setEnabled(False)
                    else:
                        chk.toggled.connect(
                            lambda checked, k=key:
                            self._on_card_field_toggled(k, checked))
                hb.addWidget(lock_lbl)
                hb.addStretch(1)
                cgrid.addWidget(cell, row_idx, col_idx)

        cgrid.setColumnStretch(0, 0)
        cgrid.setColumnStretch(1, 0)
        cgrid.setColumnStretch(2, 0)
        cgrid.setColumnStretch(3, 1)
        cl.addLayout(cgrid)

        # Before/after comparison slider: reset-to-centre on leave vs
        # keep-position-until-reopen (default). Lives in this group because
        # it governs how Library cards behave.
        cl.addSpacing(14)
        self._compare_reset_chk = QtWidgets.QCheckBox(
            'Reset the before/after slider when the pointer leaves a card')
        self._compare_reset_chk.setStyleSheet(
            'color:#ddd;font-size:11px;font-weight:normal;')
        self._compare_reset_chk.setChecked(
            bool(settings.library_compare_reset_on_leave))
        self._compare_reset_chk.setToolTip(
            'The before/after slider returns to the center as soon as the '
            'pointer leaves a card.\n'
            'When off, each card keeps its slider position until you close '
            'and reopen the Library.')
        if settings.is_locked('library_compare_reset_on_leave'):
            self._compare_reset_chk.setEnabled(False)
            cmp_chk_row = QtWidgets.QHBoxLayout()
            cmp_chk_row.setContentsMargins(0, 0, 0, 0)
            cmp_chk_row.addWidget(self._compare_reset_chk)
            cmp_chk_row.addWidget(make_lock_label(parent=self))
            cmp_chk_row.addStretch(1)
            cl.addLayout(cmp_chk_row)
        else:
            self._compare_reset_chk.toggled.connect(
                self._on_compare_reset_toggled)
            cl.addWidget(self._compare_reset_chk)

        lay.addWidget(cgrp)

        # --- Interface state reset ---------------------------------------
        grp = QtWidgets.QGroupBox('Interface State')
        grp.setStyleSheet(_SECTION_GROUP_STYLE)
        gl = QtWidgets.QVBoxLayout(grp)
        gl.setSpacing(10)
        info = QtWidgets.QLabel(
            'Reset Window Positions clears the saved positions of the '
            'Library and Render Manager panels.\n\n'
            'Reset UI to Defaults clears window sizes, positions, and '
            'table column widths for all Nukomfy panels, plus the '
            "Library's view mode (grid / list), zoom scale, GIF "
            'autoplay, and workflow sort order.')
        info.setStyleSheet(_DESC_STYLE)
        info.setWordWrap(True)
        gl.addWidget(info)
        reset_row = QtWidgets.QHBoxLayout()
        pos_btn = QtWidgets.QPushButton('Reset Window Positions')
        set_press_icon(pos_btn, SETTINGS_BACKUP_RESTORE)
        pos_btn.setFixedWidth(200)
        pos_btn.setFixedHeight(28)
        pos_btn.clicked.connect(self._reset_positions)
        reset_row.addWidget(pos_btn)
        reset_btn = QtWidgets.QPushButton('Reset UI to Defaults')
        set_press_icon(reset_btn, SETTINGS_BACKUP_RESTORE)
        reset_btn.setFixedWidth(200)
        reset_btn.setFixedHeight(28)
        reset_btn.clicked.connect(self._reset)
        reset_row.addWidget(reset_btn)
        reset_row.addStretch()
        gl.addLayout(reset_row)
        lay.addWidget(grp)
        lay.addStretch()

    def _on_keep_on_top_toggled(self, key, checked):
        # In-memory only; persisted by SettingsPanel._save (via
        # _PathsTab.save -> settings.save). SettingsPanel.reject restores
        # the snapshotted value if the user clicks Cancel.
        if settings.is_locked(key):
            return  # locked - defensive (the checkbox is disabled too)
        setattr(settings, key, bool(checked))

    def _on_card_field_toggled(self, key, checked):
        # In-memory only; persisted by SettingsPanel._save and rolled
        # back by SettingsPanel.reject. Live refresh keeps the Library
        # in sync while the dialog is open.
        if settings.is_locked(key):
            return  # locked - defensive (checkbox is disabled too)
        setattr(settings, key, bool(checked))
        try:
            from Nukomfy.gui.library_panel import LibraryPanel
            inst = LibraryPanel._instance
            if inst is not None:
                inst.on_card_visibility_changed()
        except (RuntimeError, AttributeError, ImportError):
            pass

    def _on_compare_reset_toggled(self, checked):
        # In-memory only; persisted by SettingsPanel._save and rolled back
        # by SettingsPanel.reject (the key is in the snapshot). Live-refresh
        # the open Library so re-enabling reset-on-leave recentres sliders.
        if settings.is_locked('library_compare_reset_on_leave'):
            return  # locked - defensive (the checkbox is disabled too)
        setattr(settings, 'library_compare_reset_on_leave', bool(checked))
        try:
            from Nukomfy.gui.library_panel import LibraryPanel
            inst = LibraryPanel._instance
            if inst is not None:
                inst.on_compare_reset_changed()
        except (RuntimeError, AttributeError, ImportError):
            pass

    def _on_gizmo_name_prefix_toggled(self, checked):
        # In-memory only; persisted by SettingsPanel._save and rolled back
        # by SettingsPanel.reject (the snapshot includes this key).
        if settings.is_locked('gizmo_name_prefix'):
            return  # locked - defensive (the checkbox is disabled too)
        setattr(settings, 'gizmo_name_prefix', bool(checked))

    def _on_gizmo_disable_group_view_toggled(self, checked):
        # In-memory only; persisted by SettingsPanel._save and rolled back
        # by SettingsPanel.reject (the snapshot includes this key).
        if settings.is_locked('gizmo_disable_group_view'):
            return  # locked - defensive (the checkbox is disabled too)
        setattr(settings, 'gizmo_disable_group_view', bool(checked))

    def _reset_positions(self):
        ui_state.reset_window_positions()
        _dialogs.inform(
            self, 'Window positions reset',
            'Window positions cleared. Panels will open at their default '
            'position the next time you open them.')

    def _reset(self):
        ui_state.reset()
        _dialogs.inform(
            self, 'UI reset',
            'UI reset to defaults. Panels will apply the new defaults '
            'the next time you open them.')


# Job-settings keys persisted by the Jobs tab. Snapshotted on dialog open so
# Cancel can revert in-memory edits (including Reset to defaults); persisted
# only when the user clicks Save (via _PathsTab.save -> settings.save).
_JOB_SETTING_KEYS = (
    'history_limit', 'local_history_max_entries', 'auto_refresh_enabled',
    'auto_refresh_interval', 'lost_job_timeout_days', 'batch_warning_threshold',
)


class _JobsTab(QtWidgets.QWidget):
    """Job-related settings, split out of the Machines tab: Render Manager
    display/refresh, the local job-history caps, and the submit batch
    warning. Edits apply in memory only; persistence is gated by the dialog's
    Save button (Cancel reverts via SettingsPanel._jobs_snapshot), like the
    Paths and Interface tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        _DESC_STYLE = 'color:#888;font-size:11px;font-weight:normal;'

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        def _group(title):
            grp = QtWidgets.QGroupBox(title)
            grp.setStyleSheet(_SECTION_GROUP_STYLE)
            lay = QtWidgets.QVBoxLayout(grp)
            lay.setSpacing(10)
            return grp, lay

        def _setting(group_lay, label_text, desc_text, control, key, unit=None):
            # Label and its control share one row, the control right after
            # the text; the trailing stretch keeps the pair left-aligned, so
            # there is no leading indent and the control is not pushed to the
            # panel edge. Grey description spans underneath.
            block = QtWidgets.QVBoxLayout()
            block.setSpacing(2)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(QtWidgets.QLabel(label_text))
            row.addWidget(control)
            if unit:
                row.addWidget(QtWidgets.QLabel(unit))
            if settings.is_locked(key):
                control.setEnabled(False)
                row.addWidget(make_lock_label())
            row.addStretch(1)
            block.addLayout(row)
            desc = QtWidgets.QLabel(desc_text)
            desc.setStyleSheet(_DESC_STYLE)
            desc.setWordWrap(True)
            block.addWidget(desc)
            group_lay.addLayout(block)

        def _int_field(value, lo, hi, on_commit, tooltip):
            f = NukomfyLineEdit(str(value))
            f.setFixedWidth(50)
            f.setAlignment(QtCore.Qt.AlignRight)
            f.setValidator(QtGui.QIntValidator(lo, hi))
            f.setToolTip(tooltip)
            attach_int_clamp(f, lo, hi, value, on_commit=on_commit)
            return f

        # --- Render Manager ----------------------------------------------
        rm_grp, rm_lay = _group('Render Manager')

        self._hist_edit = _int_field(
            settings.history_limit, 1, 50, self._on_history_limit_changed,
            'Max history rows shown per machine in the Render Manager '
            'sub-table (1-50).\n\n'
            'The local history file cap is independent - see '
            '"Max jobs in local history" below.')
        _setting(rm_lay, 'Max History rows per machine',
                 'Set how many finished jobs the Render Manager shows per '
                 'machine.',
                 self._hist_edit, 'history_limit')

        self._refresh_chk = QtWidgets.QCheckBox()
        self._refresh_chk.setChecked(settings.auto_refresh_enabled)
        self._refresh_chk.setToolTip(
            'Automatically update all machines at a fixed interval')
        self._refresh_chk.toggled.connect(self._on_auto_refresh_enabled_changed)
        _setting(rm_lay, 'Auto-update',
                 'Choose whether all machines update automatically at a '
                 'fixed interval.',
                 self._refresh_chk, 'auto_refresh_enabled')

        self._refresh_edit = _int_field(
            settings.auto_refresh_interval, 15, 120,
            self._on_auto_refresh_interval_changed,
            'Seconds between automatic updates (15-120)')
        self._refresh_edit.setEnabled(
            settings.auto_refresh_enabled
            and not settings.is_locked('auto_refresh_interval'))
        _setting(rm_lay, 'Auto-update interval',
                 'Set how often machines update, in seconds.',
                 self._refresh_edit, 'auto_refresh_interval', unit='seconds')

        root.addWidget(rm_grp)

        # --- Local Job History -------------------------------------------
        hist_grp, hist_lay = _group('Local Job History')

        self._local_hist_edit = _int_field(
            settings.local_history_max_entries, 5, 500,
            self._on_local_history_max_entries_changed,
            'Max total entries kept in the local history file (5-500).\n\n'
            'Older entries are dropped at next save when this cap is '
            'exceeded.')
        _setting(hist_lay, 'Max jobs in local history',
                 'Set how many jobs are kept in your local history file.',
                 self._local_hist_edit, 'local_history_max_entries')

        self._lost_edit = _int_field(
            settings.lost_job_timeout_days, 1, 90,
            self._on_lost_job_timeout_changed,
            "<p>Active jobs whose status cannot be verified with their "
            "machine (server offline, unreachable, or the job is no "
            "longer in the server's history) are moved from Active to "
            "History and marked as 'Unknown' after this many days since "
            "submit.<br><br>"
            "They stay in History until you remove them manually via "
            "Clear History or the row delete button.</p>")
        _setting(hist_lay, 'Mark jobs as lost after',
                 'Set how many days an unverifiable job waits before moving '
                 'to History.',
                 self._lost_edit, 'lost_job_timeout_days', unit='days')

        root.addWidget(hist_grp)

        # --- Submit ------------------------------------------------------
        submit_grp, submit_lay = _group('Submit')

        self._batch_warn_edit = _int_field(
            settings.batch_warning_threshold, 0, 100,
            self._on_batch_warning_threshold_changed,
            'Show a confirmation dialog when Submit is about to send '
            'this many jobs or more (Batch Count).\n\n'
            'Range 0-100. Set to 0 to disable the warning.')
        _setting(submit_lay, 'Warn before sending more than',
                 'Set how many jobs trigger a confirmation before submit.',
                 self._batch_warn_edit, 'batch_warning_threshold', unit='jobs')

        root.addWidget(submit_grp)
        root.addStretch(1)

        # Reset to defaults - restores every field on this tab to the
        # values in settings._DEFAULTS. Pinned bottom-right (tab-level
        # action). Locked fields are left untouched (setattr no-ops).
        self._reset_defaults_btn = QtWidgets.QPushButton('Reset to Defaults')
        set_press_icon(self._reset_defaults_btn, SETTINGS_BACKUP_RESTORE)
        self._reset_defaults_btn.setFixedHeight(24)
        self._reset_defaults_btn.setToolTip(
            'Reset all Jobs settings to their default values')
        self._reset_defaults_btn.clicked.connect(self._reset_defaults)
        reset_row = QtWidgets.QHBoxLayout()
        reset_row.addStretch()
        reset_row.addWidget(self._reset_defaults_btn)
        root.addLayout(reset_row)

    # ------------------------------------------------------------------
    # Handlers mutate settings in memory only; persistence is gated by the
    # dialog's Save button (Cancel reverts via SettingsPanel._jobs_snapshot),
    # matching the Paths and Interface tabs.
    def _on_history_limit_changed(self, value):
        settings.history_limit = value

    def _on_local_history_max_entries_changed(self, value):
        settings.local_history_max_entries = value

    def _on_auto_refresh_enabled_changed(self, checked):
        settings.auto_refresh_enabled = checked
        # Don't re-enable the interval edit if it's locked independently.
        if not settings.is_locked('auto_refresh_interval'):
            self._refresh_edit.setEnabled(checked)

    def _on_auto_refresh_interval_changed(self, value):
        settings.auto_refresh_interval = value

    def _on_lost_job_timeout_changed(self, value):
        settings.lost_job_timeout_days = value

    def _on_batch_warning_threshold_changed(self, value):
        settings.batch_warning_threshold = value

    def _reset_defaults(self):
        ans = _dialogs.ask(
            self, 'Reset to defaults?',
            'Restore all Jobs settings to their default values?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        for k in _JOB_SETTING_KEYS:
            setattr(settings, k, _DEFAULTS[k])
        # In-memory only - Save persists, Cancel reverts (see handlers).
        self._hist_edit.setText(str(settings.history_limit))
        self._local_hist_edit.setText(str(settings.local_history_max_entries))
        self._refresh_chk.setChecked(settings.auto_refresh_enabled)
        self._refresh_edit.setText(str(settings.auto_refresh_interval))
        # Respect a lock on auto_refresh_interval - the checkbox->edit
        # toggled connection would otherwise re-enable a locked field.
        self._refresh_edit.setEnabled(
            settings.auto_refresh_enabled
            and not settings.is_locked('auto_refresh_interval'))
        self._lost_edit.setText(str(settings.lost_job_timeout_days))
        self._batch_warn_edit.setText(str(settings.batch_warning_threshold))


# ---------------------------------------------------------------------------
# Settings Panel (tabbed)
# ---------------------------------------------------------------------------
from Nukomfy.gui._theme import apply_window_chrome
from Nukomfy.gui import _focus_drop


class SettingsPanel(QtWidgets.QDialog):

    # Interface-tab keys whose handlers mutate `settings` in memory while
    # the user is interacting with the dialog. We snapshot them on open
    # so Cancel can restore the pre-edit state (the user expects Save vs
    # Cancel to be meaningful. Machines tab is intentionally auto-save,
    # see `_build` comment.
    _UI_SNAPSHOT_KEYS = (
        'library_keep_on_top',
        'render_manager_keep_on_top',
        'library_grid_show_version',
        'library_grid_show_author',
        'library_grid_show_description',
        'library_grid_show_categories',
        'library_grid_show_models',
        'library_grid_show_source',
        'library_list_show_version',
        'library_list_show_author',
        'library_list_show_description',
        'library_list_show_categories',
        'library_list_show_models',
        'library_list_show_source',
        'library_compare_reset_on_leave',
        'gizmo_name_prefix',
        'gizmo_disable_group_view',
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Nukomfy - Settings')
        self.setMinimumSize(1100, 420)
        # Modal dialog - no minimize button (on Windows, minimizing a modal
        # dialog drags its owner app down with it). Maximize + Close only.
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.destroyed.connect(self._on_destroyed)
        apply_window_chrome(self)
        _focus_drop.install(self)
        # Snapshot Interface-tab values BEFORE _build so the snapshot
        # reflects on-disk state, not whatever the widgets initialise to.
        self._ui_snapshot = {
            k: bool(getattr(settings, k))
            for k in self._UI_SNAPSHOT_KEYS
        }
        # Jobs-tab values snapshotted the same way (raw int/bool) so Cancel
        # can revert in-memory edits, including Reset to defaults.
        self._jobs_snapshot = {
            k: getattr(settings, k) for k in _JOB_SETTING_KEYS
        }
        self._build()
        # Settings always opens at this default size, centered - neither size
        # nor position is persisted, so it reopens fresh every time. resize()
        # (not adjustSize) keeps the opening size identical to before at 100%;
        # the minimum height clamps it as needed. This is the single source
        # for the Settings default size (no longer stored in ui_state).
        self.resize(1360, 780)
        # Only on a screen too small for the default: shrink to fit, then wrap
        # the form tabs so their content scrolls. No-op (and no wrapping) when
        # the default fits, so a normal monitor keeps the full default size.
        if fit_to_screen(self) < 1.0:
            self._enable_tab_scroll()
        center_on_screen(self)

    def done(self, result):
        ui_state.save_column_widths('machines_tab', self._machines_tab.table)
        self._machines_tab._stop_workers()
        super().done(result)

    def closeEvent(self, event):
        ui_state.save_column_widths('machines_tab', self._machines_tab.table)
        self._machines_tab._stop_workers()
        super().closeEvent(event)

    def _on_destroyed(self):
        """Safety net: stop workers if Nuke exits without closeEvent."""
        try:
            self._machines_tab._stop_workers()
        except (RuntimeError, AttributeError):
            pass

    def _scroll_wrap(self, widget):
        """Wrap a tab page in a vertical scroll area so it scrolls instead of
        compressing when the window is shorter than its content (small screen
        or maximize on a small monitor). Invisible at full size - the
        scrollbar only appears when the viewport is shorter than the page."""
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
        return scroll

    def _enable_tab_scroll(self):
        """Re-host the form tabs inside scroll areas so their content stays
        reachable after the window has been shrunk to fit a small screen.
        Called only in that case (after fit_to_screen shrank the window); on
        a normal screen the tabs are left as built. Machines already scrolls
        (it is a table) and is left alone."""
        for widget, label in ((self._paths_tab, 'Paths'),
                              (self._jobs_tab, 'Jobs'),
                              (self._ui_tab, 'Interface')):
            idx = self.tabs.indexOf(widget)
            if idx != -1:
                self.tabs.removeTab(idx)
                self.tabs.insertTab(idx, self._scroll_wrap(widget), label)
        self.tabs.setCurrentIndex(0)

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        # Top margin 0 so the tab bar sits flush with the window chrome
        # (avoids the dialog-bg strip showing above the tabs on Qt 5.x).
        root.setContentsMargins(10, 0, 10, 10)
        root.setSpacing(0)

        self.tabs = QtWidgets.QTabWidget()
        # Tab styling kept in sync with the Render Manager top tabs
        # (gui/render_queue_panel.py _main_tabs) so the two panels feel
        # like the same app.
        from Nukomfy.gui._theme import TOP_TABS_STYLE_BASE, apply_tab_fit
        self.tabs.setStyleSheet(
            'QTabWidget::pane{border:1px solid #3a3a3a;border-bottom:none;}'
            + TOP_TABS_STYLE_BASE)
        apply_tab_fit(self.tabs, 16, bold=True)

        # Tab 1 - Paths
        self._paths_tab = _PathsTab()
        self.tabs.addTab(self._paths_tab, 'Paths')

        # Tab 2 - Machines
        from Nukomfy.gui.machines_panel import MachinesTab
        self._machines_tab = MachinesTab()
        self.tabs.addTab(self._machines_tab, 'Machines')

        # Tab 3 - Jobs
        self._jobs_tab = _JobsTab()
        self.tabs.addTab(self._jobs_tab, 'Jobs')

        # Tab 4 - Interface
        self._ui_tab = _UITab()
        self.tabs.addTab(self._ui_tab, 'Interface')

        root.addWidget(self.tabs, 1)

        # Bottom buttons - Save applies only to Paths (machines auto-save)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        btns_w = QtWidgets.QWidget()
        btns_l = QtWidgets.QHBoxLayout(btns_w)
        btns_l.setContentsMargins(10, 0, 10, 0)
        btns_l.addWidget(btns)
        root.addWidget(btns_w)

        # Pin the dialog's minimum height to the TALLEST tab. Qt sizes the
        # tab stack to the active page, so a short tab (Machines is now just
        # a table) would let the window shrink below what Paths / Jobs need
        # and clip their content on tab switch. Measured once, after every
        # tab and the button row exist, so it adapts if a tab's content
        # changes later.
        self.layout().activate()
        tab_heights = [self.tabs.widget(i).minimumSizeHint().height()
                       for i in range(self.tabs.count())]
        cur = self.tabs.currentWidget().minimumSizeHint().height()
        chrome = max(0, self.minimumSizeHint().height() - cur)
        self.setMinimumSize(1100,
                            max(420, max(tab_heights) + chrome))

    def reject(self):
        """Cancel discards Interface- and Jobs-tab edits.

        Both tabs apply changes live in memory (the Interface tab so the
        Library can repaint for visual feedback; the Jobs tab so the running
        session sees the value), but persistence is gated by Save. On Cancel
        we restore the pre-edit values in memory and, if the Library is open,
        refresh it so its view matches what's on disk.
        """
        changed_keys = []
        for key, original in self._ui_snapshot.items():
            try:
                current = bool(getattr(settings, key))
            except AttributeError:
                continue
            if current != original:
                setattr(settings, key, original)
                changed_keys.append(key)
        if any(k.startswith('library_') for k in changed_keys):
            try:
                from Nukomfy.gui.library_panel import LibraryPanel
                inst = LibraryPanel._instance
                if inst is not None:
                    inst.on_card_visibility_changed()
            except (RuntimeError, AttributeError, ImportError):
                pass
        # Revert Jobs-tab edits (including Reset to defaults): applied in
        # memory only, so restoring the snapshot is enough - nothing was
        # persisted to disk unless the user clicked Save.
        for key, original in self._jobs_snapshot.items():
            try:
                if getattr(settings, key) != original:
                    setattr(settings, key, original)
            except AttributeError:
                continue
        super().reject()

    def _save(self):
        if self._paths_tab.save():
            self.accept()