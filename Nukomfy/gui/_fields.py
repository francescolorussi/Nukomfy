"""Palette-pinning variants of the text-entry widgets.

A host tool sharing this QApplication can mutate the global palette, which
recolors anything Nukomfy leaves inherited: placeholder text (Qt has no
stylesheet property for it), field background, typed text and selection. These
subclasses pin Nuke's measured default palette at construction, so the look is
unchanged but immune to that mutation. Use them in place of QLineEdit /
QPlainTextEdit / QTextEdit for input fields.
"""
from Nukomfy.utils.qt_compat import QtCore, QtWidgets
from Nukomfy.gui._theme import apply_nukomfy_palette


def _exec_native_context_menu(widget, event):
    """Show the standard context menu drawn by the application's native style.

    When a widget carries its own stylesheet, Qt draws its context menu through
    the QStyleSheetStyle proxy, which paints the hovered item with the CSS
    default dark text, unreadable on the dark menu. Detaching the menu to a
    top-level popup leaves it with no stylesheet'd ancestor, so it renders with
    Nuke's native style like every other context menu. Qt.Popup is passed
    explicitly so it stays a frameless popup; the standard actions stay wired
    to this widget's cut/copy/paste slots. Detaching also drops the field's
    pinned palette, so it is re-pinned on the menu to keep it resistant to a
    mutated global palette.
    """
    menu = widget.createStandardContextMenu()
    menu.setParent(None, QtCore.Qt.Popup)
    apply_nukomfy_palette(menu)
    try:
        menu.exec_(event.globalPos())
    finally:
        menu.deleteLater()


class NukomfyLineEdit(QtWidgets.QLineEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_nukomfy_palette(self)

    def contextMenuEvent(self, event):
        _exec_native_context_menu(self, event)


class NukomfyPlainTextEdit(QtWidgets.QPlainTextEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_nukomfy_palette(self)

    def contextMenuEvent(self, event):
        _exec_native_context_menu(self, event)


class NukomfyTextEdit(QtWidgets.QTextEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_nukomfy_palette(self)

    def contextMenuEvent(self, event):
        _exec_native_context_menu(self, event)
