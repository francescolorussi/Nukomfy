"""Inline message labels (error / warning).

Shared factories for visually consistent inline error/warning messages
across panels. Use these instead of crafting one-off styled QLabels so
the look stays coherent (icon + colour + font size).

Two factories:
  - make_error_banner()    blocking errors (red)
  - make_warning_banner()  non-blocking warnings (orange)

Both return a QWidget composed of an icon + text label, hidden by
default. Call .set_message(text) to show/update; pass '' to hide.
"""

from Nukomfy.utils.qt_compat import QtWidgets, QtCore
from Nukomfy.gui.icons import ERROR as _GLYPH_ERROR, WARNING as _GLYPH_WARNING, icon_font
from Nukomfy.gui._theme import ERROR_COLOR, WARNING_INLINE

# Icon glyph renders a few points larger than the body text.
_ICON_FONT_BOOST = 3


def _make_message_widget(glyph, color, font_size=10, parent=None):
    """Internal: build an HBox with an icon QLabel + text QLabel.

    The returned QWidget exposes a `set_message(text)` callable: empty
    text hides the widget, non-empty shows it with the message.
    """
    widget = QtWidgets.QWidget(parent)
    lay = QtWidgets.QHBoxLayout(widget)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)

    icon_lbl = QtWidgets.QLabel(glyph)
    icon_lbl.setFont(icon_font(font_size + _ICON_FONT_BOOST))
    icon_lbl.setStyleSheet('color:{};'.format(color))
    icon_lbl.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)

    text_lbl = QtWidgets.QLabel('')
    text_lbl.setStyleSheet(
        'color:{};font-size:{}px;'.format(color, font_size))
    text_lbl.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
    text_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    text_lbl.setWordWrap(True)

    lay.addWidget(icon_lbl, 0)
    lay.addWidget(text_lbl, 1)

    widget.setVisible(False)

    def set_message(text):
        text = (text or '').strip()
        text_lbl.setText(text)
        widget.setVisible(bool(text))

    widget.set_message = set_message
    widget._text_label = text_lbl
    widget._icon_label = icon_lbl
    return widget


def make_error_banner(parent=None, font_size=10):
    """QWidget with ERROR glyph + red text. Hidden by default.

    Call .set_message(text) to display the error; .set_message('') to hide.
    Use for blocking errors: action will fail (invalid path, malformed
    JSON, missing required field).
    """
    return _make_message_widget(_GLYPH_ERROR, ERROR_COLOR, font_size, parent)


def make_warning_banner(parent=None, font_size=10):
    """QWidget with WARNING glyph + orange text. Hidden by default.

    Call .set_message(text) to display the warning; .set_message('') to hide.
    Use for non-blocking warnings: action proceeds but the user should
    know (duplicate workflow ID, ambiguous configuration).
    """
    return _make_message_widget(_GLYPH_WARNING, WARNING_INLINE, font_size, parent)
