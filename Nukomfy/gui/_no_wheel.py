"""Wheel-inert variants of the value-editing widgets (combo, spin, slider).

Qt changes these widgets' value when the mouse wheel scrolls over them, so
scrolling a panel accidentally edits whatever field the cursor passes. These
subclasses ignore the wheel event and let it propagate to the parent, so the
surrounding view scrolls instead - matching Nuke desktop, where knobs are not
wheel-adjustable.

The override lives only on these widgets; nothing is installed on the
QApplication, so the rest of Nuke's UI is untouched.
"""
from Nukomfy.utils.qt_compat import QtCore, QtWidgets
from Nukomfy.gui._theme import apply_nukomfy_palette


class _NoWheelMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Drop wheel-to-focus too: hovering over the widget while scrolling
        # should not steal keyboard focus from elsewhere. Click/Tab focus stay.
        if self.focusPolicy() == QtCore.Qt.WheelFocus:
            self.setFocusPolicy(QtCore.Qt.StrongFocus)

    def wheelEvent(self, event):
        # Ignoring (instead of accepting) lets Qt forward the wheel to the
        # parent, so the panel/table still scrolls.
        event.ignore()


class NoWheelComboBox(_NoWheelMixin, QtWidgets.QComboBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pin the input palette (background, text, selection, placeholder) so a
        # host mutating the global palette cannot recolor the combo or its
        # popup. Values are Nuke's defaults, so the look is unchanged.
        apply_nukomfy_palette(self)


class NoWheelSpinBox(_NoWheelMixin, QtWidgets.QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_nukomfy_palette(self)


class NoWheelDoubleSpinBox(_NoWheelMixin, QtWidgets.QDoubleSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_nukomfy_palette(self)


class NoWheelSlider(_NoWheelMixin, QtWidgets.QSlider):
    pass
