"""Lock indicator helpers for admin-overridden rows.

Visual marker for settings/machines rows that are read-only because the
admin pushed an override via `Nukomfy/settings_overrides/`. The UI
follows a single rule: a locked control is greyed-out and gets a small
neutral-grey lock icon next to it (Material Icons LOCK at #888). Tooltip
is constant across the plugin so the user always reads the same line:
"Locked by settings override - read only".

For machine rows the visual cue is different: a small lock icon prefixed
to the Name cell, no row tinting (which would read as offline). The
Enabled checkbox stays live (per-user delta in `global_overrides`), only
Edit / Remove (toolbar) are gated.
"""

from Nukomfy.utils.qt_compat import QtWidgets, QtCore

from Nukomfy.gui.icons import icon_font, LOCK


LOCK_TOOLTIP = 'Locked by settings override - read only'
LOCK_COLOR = '#888'


def make_lock_label(tooltip=LOCK_TOOLTIP, parent=None, size=14):
    """Return a QLabel with the lock glyph at neutral grey."""
    lbl = QtWidgets.QLabel(parent)
    lbl.setText(LOCK)
    lbl.setFont(icon_font(size))
    lbl.setStyleSheet('color:{};'.format(LOCK_COLOR))
    lbl.setToolTip(tooltip)
    lbl.setAlignment(QtCore.Qt.AlignCenter)
    return lbl
