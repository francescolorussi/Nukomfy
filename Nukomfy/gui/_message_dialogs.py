"""Reusable confirm / warning dialogs that list file paths.

Shared UI primitives for confirm/warning dialogs that show a primary text +
informative text + a list of paths. Used wherever a stock QMessageBox would
be too rigid: stock QMessageBox can't reliably size its detailed-text panel
(Qt re-applies setFixedSize on every layout pass), and its width doesn't
adapt to long path strings - the path wraps mid-word.

Public API:
    _PathListDialog(title, primary, informative, paths, buttons, icon, parent)
        .exec_() -> QtWidgets.QDialog.Accepted | .Rejected

The dialog mimics QMessageBox layout (icon + primary + informative + buttons)
but adds a "Show Details" toggle that reveals a no-wrap, scrollable list of
the supplied paths. Width is computed from QFontMetrics on the longest text
line, capped to 90% of the screen width.
"""

import os

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui._fields import NukomfyTextEdit
from Nukomfy.gui._theme import apply_window_chrome
from Nukomfy.gui.ui_state import cap_to_screen


# Threshold over which a single bullet line is head-ellipsis truncated and
# the full path is shown via tooltip on hover. ~100 chars at the default
# font width (~7px/char) caps the dialog at ~700px before ellipsis kicks
# in - comfortable on a 1920-wide screen with margins.
_PATH_DISPLAY_MAX_CHARS = 100


def _truncate_path(text, max_len=_PATH_DISPLAY_MAX_CHARS):
    """Head-ellipsis truncate so the file's tail (last dirs + name) stays
    visible. Returns (display, tooltip) - tooltip is '' when text fits.
    """
    if len(text) <= max_len:
        return text, ''
    return '…' + text[-(max_len - 1):], text


def _truncate_path_with_suffix(path, suffix, max_len=_PATH_DISPLAY_MAX_CHARS):
    """Head-ellipsis truncate the path while always preserving `suffix`
    (e.g. ' -> onwards') visible at the tail. Tooltip carries the full
    untruncated `path + suffix` when truncation kicks in.
    """
    full = path + suffix
    if len(full) <= max_len:
        return full, ''
    # Reserve room for the suffix; ellipsis-truncate the path portion.
    budget = max_len - len(suffix) - 1   # -1 for the leading '…'
    if budget < 1:
        # Suffix alone consumes the budget - degrade gracefully.
        return '…' + full[-(max_len - 1):], full
    return '…' + path[-budget:] + suffix, full


# Button preset -> (accept_label, reject_label, default_is_reject)
_BUTTON_PRESETS = {
    'YES_NO': ('Yes', 'No', True),
    'OVERWRITE_CANCEL': ('Overwrite', 'Cancel', True),
    'CONTINUE_CANCEL': ('Continue', 'Cancel', True),
    'OVERWRITE_AND_CANCEL_INFLIGHT': (
        'Cancel running job(s) and overwrite', 'Cancel submit', True),
}

# Icon preset -> QStyle.StandardPixmap
_ICON_PRESETS = {
    'question': QtWidgets.QStyle.SP_MessageBoxQuestion,
    'warning': QtWidgets.QStyle.SP_MessageBoxWarning,
    'critical': QtWidgets.QStyle.SP_MessageBoxCritical,
    'information': QtWidgets.QStyle.SP_MessageBoxInformation,
}


class _PathListDialog(QtWidgets.QDialog):
    """Confirm/warning dialog with primary + informative text and an
    optional path list shown via a Show Details toggle.

    Width is fixed and computed from the longest line in primary + informative
    (capped to 90% of screen). This guarantees long paths in the primary text
    don't wrap mid-word, while keeping the dialog visually consistent across
    short/long messages.

    Parameters
    ----------
    title : str
        Window title.
    primary : str
        Top-level message (e.g. "Output files already exist for 2 outputs:").
    informative : str
        Secondary message rendered below primary (e.g. "Do you want to
        overwrite them?").
    paths : list[str] | None
        Paths shown in the Details panel. Pass an empty list or None to
        hide the Show Details button entirely.
    buttons : str
        Key from _BUTTON_PRESETS (default 'YES_NO').
    icon : str
        Key from _ICON_PRESETS (default 'question').
    parent : QtWidgets.QWidget | None
    """

    _DETAIL_HEIGHT = 280

    def __init__(self, title, primary, informative, paths=None,
                 buttons='YES_NO', icon='question', parent=None):
        super(_PathListDialog, self).__init__(parent)
        self.setWindowTitle(title)
        self.setSizeGripEnabled(False)

        accept_label, reject_label, default_reject = _BUTTON_PRESETS.get(
            buttons, _BUTTON_PRESETS['YES_NO'])
        icon_enum = _ICON_PRESETS.get(icon, _ICON_PRESETS['question'])

        main = QtWidgets.QVBoxLayout(self)
        main.setContentsMargins(16, 16, 16, 12)
        main.setSpacing(12)

        # Top row: icon (left, top-aligned) + text column (right)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)

        icon_lbl = QtWidgets.QLabel()
        std_icon = self.style().standardIcon(icon_enum)
        icon_lbl.setPixmap(std_icon.pixmap(32, 32))
        icon_lbl.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        top.addWidget(icon_lbl, 0, QtCore.Qt.AlignTop)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setSpacing(10)
        primary_lbl = QtWidgets.QLabel(primary)
        primary_lbl.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        text_col.addWidget(primary_lbl)
        info_lbl = QtWidgets.QLabel(informative)
        info_lbl.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        text_col.addWidget(info_lbl)
        top.addLayout(text_col, 1)
        main.addLayout(top)

        # Detail panel - hidden until Show Details
        has_paths = bool(paths)
        if has_paths:
            self._detail = NukomfyTextEdit(self)
            self._detail.setReadOnly(True)
            self._detail.setPlainText('\n'.join(paths))
            self._detail.setLineWrapMode(QtWidgets.QTextEdit.NoWrap)
            self._detail.setWordWrapMode(QtGui.QTextOption.NoWrap)
            self._detail.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarAlwaysOn)
            self._detail.setVerticalScrollBarPolicy(
                QtCore.Qt.ScrollBarAlwaysOn)
            self._detail.setFixedHeight(self._DETAIL_HEIGHT)
            self._detail.setVisible(False)
            main.addWidget(self._detail)
        else:
            self._detail = None

        # Button row: accept + reject + (optional) Show Details
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        accept_btn = QtWidgets.QPushButton(accept_label, self)
        reject_btn = QtWidgets.QPushButton(reject_label, self)
        if default_reject:
            reject_btn.setDefault(True)
            reject_btn.setAutoDefault(True)
        else:
            accept_btn.setDefault(True)
            accept_btn.setAutoDefault(True)
        accept_btn.clicked.connect(self.accept)
        reject_btn.clicked.connect(self.reject)
        btn_row.addWidget(accept_btn)
        btn_row.addWidget(reject_btn)
        if has_paths:
            self._toggle_btn = QtWidgets.QPushButton(
                'Show Details…', self)
            self._toggle_btn.clicked.connect(self._toggle_detail)
            btn_row.addWidget(self._toggle_btn)
        main.addLayout(btn_row)

        # Width: enough to fit the longest line of primary + informative
        # text only (paths live in the detail panel, which has its own
        # horizontal scrollbar - sizing on paths would balloon the dialog
        # even when details are hidden). Capped to 90% screen, minimum
        # 420px so short messages still feel like a dialog and not a chip.
        fm = QtGui.QFontMetrics(primary_lbl.font())
        advance = getattr(fm, 'horizontalAdvance', None) or fm.width
        width_lines = primary.split('\n') + informative.split('\n')
        longest_px = max((advance(ln) for ln in width_lines), default=300)
        # +100 covers icon column, spacing, and layout margins.
        desired_w = longest_px + 100
        desired_w, _ = cap_to_screen(desired_w, reference=self.parent())
        self._fixed_width = max(desired_w, 420)
        self.setFixedWidth(self._fixed_width)
        # Let height be content-driven; lock it after first show.
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())
        apply_window_chrome(self)

    def _toggle_detail(self):
        if self._detail is None:
            return
        show = not self._detail.isVisible()
        self._detail.setVisible(show)
        self._toggle_btn.setText(
            'Hide Details…' if show else 'Show Details…')
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())


def _format_collision_lines(collisions, io_mode='', start_frame=None,
                            dir_path=''):
    """Compact summary of the files in this submit's scope.

    Every line includes the full directory path prefix. Multifile and
    Range cases append ` -> onwards` at the tail; truncation always
    preserves the suffix so the line still reads as a pattern.

    Returns: list of (display_text, tooltip_or_empty).
    """
    if not collisions:
        return []
    sorted_names = sorted(collisions)

    def _join(filename):
        if dir_path:
            return os.path.normpath(os.path.join(dir_path, filename))
        return filename

    # Sequence mode: end frame is indeterminate (model decides count at
    # runtime). Use `-> onwards` marker on the start-frame target.
    if io_mode == 'Sequence' and start_frame is not None:
        import re as _re
        first = next(iter(sorted_names))
        m = _re.match(r'^(.*?)\.(\d+)\.([^.]+)$', first)
        if m:
            prefix = m.group(1)
            ext = m.group(3)
            pad = len(m.group(2))
            filename = '{}.{}.{}'.format(
                prefix, str(start_frame).zfill(pad), ext)
        else:
            filename = first
        return [_truncate_path_with_suffix(_join(filename), ' → onwards')]

    # Single mode, multifile: full path + `-> onwards` so the line is a
    # pattern (path-anchored) consistent with Sequence mode.
    if len(sorted_names) > 1:
        return [_truncate_path_with_suffix(
            _join(sorted_names[0]), ' → onwards')]

    # Single mode, exactly one file: full path of the single target.
    return [_truncate_path(_join(sorted_names[0]))]


def _build_output_dialog(parent, title, icon_kind, primary_text,
                         bullet_lines, button_specs):
    """Internal: shared dialog skeleton used by all overwrite/ownership
    popups.

    button_specs: list of (label, return_value, tooltip, is_default).
    Click -> set result and accept(); the special return_value 'cancel'
    triggers reject() instead.
    """
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setSizeGripEnabled(False)

    main = QtWidgets.QVBoxLayout(dlg)
    main.setContentsMargins(18, 16, 18, 12)
    main.setSpacing(10)

    top = QtWidgets.QHBoxLayout()
    top.setSpacing(14)

    icon_lbl = QtWidgets.QLabel()
    icon_enum = _ICON_PRESETS.get(icon_kind, _ICON_PRESETS['question'])
    std_icon = dlg.style().standardIcon(icon_enum)
    icon_lbl.setPixmap(std_icon.pixmap(32, 32))
    icon_lbl.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
    top.addWidget(icon_lbl, 0, QtCore.Qt.AlignTop)

    text_col = QtWidgets.QVBoxLayout()
    text_col.setSpacing(6)

    primary_lbl = QtWidgets.QLabel(primary_text)
    primary_lbl.setWordWrap(True)
    text_col.addWidget(primary_lbl)

    # Bullets live in their own tight column: consecutive list items should
    # read as a compact list, not be spaced apart like paragraphs (the parent
    # column's 6px stays only between the primary text and the list).
    bullets_col = QtWidgets.QVBoxLayout()
    bullets_col.setSpacing(2)
    bullets_col.setContentsMargins(0, 2, 0, 4)

    for ln in bullet_lines:
        # Each entry is either a plain string (no truncation) or a
        # (display_text, tooltip) tuple emitted by _format_collision_lines
        # - tooltip carries the full path when the displayed line was
        # head-ellipsis truncated for layout fit.
        if isinstance(ln, tuple):
            text, tooltip = ln
        else:
            text, tooltip = ln, ''
        line_lbl = QtWidgets.QLabel('• ' + text)
        if tooltip:
            line_lbl.setToolTip(tooltip)
        line_lbl.setStyleSheet('color: #ccc; padding-left: 12px;')
        line_lbl.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        line_lbl.setWordWrap(True)
        bullets_col.addWidget(line_lbl)

    text_col.addLayout(bullets_col)

    top.addLayout(text_col, 1)
    main.addLayout(top)

    result = {'choice': 'cancel'}

    def _make_pick(action):
        def _pick():
            result['choice'] = action
            if action == 'cancel':
                dlg.reject()
            else:
                dlg.accept()
        return _pick

    btn_row = QtWidgets.QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch(1)

    for label, action, tooltip, is_default in button_specs:
        btn = QtWidgets.QPushButton(label, dlg)
        if tooltip:
            btn.setToolTip(tooltip)
        if is_default:
            btn.setDefault(True)
            btn.setAutoDefault(True)
        btn.clicked.connect(_make_pick(action))
        btn_row.addWidget(btn)

    main.addLayout(btn_row)

    fm = QtGui.QFontMetrics(primary_lbl.font())
    advance = getattr(fm, 'horizontalAdvance', None) or fm.width
    width_lines = [primary_text]
    for ln in bullet_lines:
        txt = ln[0] if isinstance(ln, tuple) else ln
        width_lines.append('• ' + txt)
    desired_w = max((advance(ln) for ln in width_lines), default=300) + 120
    desired_w, _ = cap_to_screen(desired_w, reference=parent)
    dlg.setFixedWidth(max(desired_w, 440))
    dlg.adjustSize()
    dlg.setFixedHeight(dlg.sizeHint().height())

    apply_window_chrome(dlg)
    dlg.exec_()
    return result['choice']


def prompt_overwrite_choice(parent, output_summaries):
    """3-button dialog for output overwrite resolution.

    output_summaries: list of dicts, one per output with collisions:
        {'name': str, 'dir': str, 'collisions': set[str], 'io_mode': str,
         'start_frame': int}

    Returns: 'delete_and_render' | 'overwrite' | 'cancel'.
    """
    bullet_lines = []
    for s in output_summaries:
        bullet_lines.extend(_format_collision_lines(
            s.get('collisions') or set(),
            io_mode=s.get('io_mode', ''),
            start_frame=s.get('start_frame'),
            dir_path=s.get('dir', '')))

    button_specs = [
        ('Delete and re-render', 'delete_and_render',
         'Remove the listed existing files before rendering. '
         'Other files in the directory are preserved.', False),
        ('Overwrite', 'overwrite',
         'Render without pre-deletion. Existing files are overwritten '
         'as the render produces them.', False),
        ('Cancel', 'cancel',
         'Abort the submit. No files will be modified.', True),
    ]

    return _build_output_dialog(
        parent=parent,
        title='Output collision detected',
        icon_kind='question',
        primary_text=('Some output files already exist. '
                      'The following files will be affected by your choice:'),
        bullet_lines=bullet_lines,
        button_specs=button_specs,
    )


