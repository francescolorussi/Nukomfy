"""Palette-pinned wrappers over Qt's stock message and file dialogs.

A host tool sharing this QApplication can mutate the global palette, which
recolors any widget Nukomfy leaves inherited - including the buttons and text
of QMessageBox / QFileDialog / QProgressDialog. Qt's static convenience methods
(QMessageBox.warning, QFileDialog.getOpenFileName) build and exec the dialog
internally, leaving no point to pin the palette, so these helpers build the
instance, pin Nuke's measured defaults, and exec it. The look is unchanged in a
clean session; only resistance to global-palette pollution is added.

On Windows/macOS the file helpers still use the OS-native dialog (the pin is a
harmless no-op there); on Linux, where the file dialog is Qt-drawn, the pin is
what actually defends it.
"""
from Nukomfy.utils.qt_compat import QtWidgets
from Nukomfy.gui._theme import apply_nukomfy_palette

QMessageBox = QtWidgets.QMessageBox
QFileDialog = QtWidgets.QFileDialog


def message_box(parent=None):
    """Return a QMessageBox with Nuke's palette pinned.

    For callers that build a box by hand (custom buttons, informative text):
    swap QtWidgets.QMessageBox(parent) for this and keep the rest.
    """
    box = QMessageBox(parent)
    apply_nukomfy_palette(box)
    return box


def _exec(parent, icon, title, text, buttons, default):
    box = message_box(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    if default is not None:
        box.setDefaultButton(default)
    return box.exec_()


def ask(parent, title, text, buttons=None, default=None):
    """Question dialog; returns the clicked QMessageBox standard button."""
    if buttons is None:
        buttons = QMessageBox.Yes | QMessageBox.No
    return _exec(parent, QMessageBox.Question, title, text, buttons, default)


def warn(parent, title, text, buttons=None, default=None):
    """Warning dialog; returns the clicked QMessageBox standard button."""
    if buttons is None:
        buttons = QMessageBox.Ok
    return _exec(parent, QMessageBox.Warning, title, text, buttons, default)


def inform(parent, title, text, buttons=None, default=None):
    """Information dialog; returns the clicked QMessageBox standard button."""
    if buttons is None:
        buttons = QMessageBox.Ok
    return _exec(parent, QMessageBox.Information, title, text, buttons, default)


def critical(parent, title, text, buttons=None, default=None):
    """Critical dialog; returns the clicked QMessageBox standard button."""
    if buttons is None:
        buttons = QMessageBox.Ok
    return _exec(parent, QMessageBox.Critical, title, text, buttons, default)


def _file_dialog(parent, caption, directory, filt, mode, accept,
                 dirs_only=False):
    dlg = QFileDialog(parent, caption or '', directory or '', filt or '')
    apply_nukomfy_palette(dlg)
    dlg.setFileMode(mode)
    dlg.setAcceptMode(accept)
    if dirs_only:
        dlg.setOption(QFileDialog.ShowDirsOnly, True)
    if dlg.exec_():
        files = dlg.selectedFiles()
        if files:
            return files[0]
    return ''


def get_open_file(parent, caption='', directory='', filt=''):
    """Open-file dialog; returns the selected path, or '' if cancelled."""
    return _file_dialog(parent, caption, directory, filt,
                        QFileDialog.ExistingFile, QFileDialog.AcceptOpen)


def get_save_file(parent, caption='', directory='', filt=''):
    """Save-file dialog; returns the chosen path, or '' if cancelled."""
    return _file_dialog(parent, caption, directory, filt,
                        QFileDialog.AnyFile, QFileDialog.AcceptSave)


def get_directory(parent, caption='', directory=''):
    """Directory picker; returns the chosen path, or '' if cancelled."""
    return _file_dialog(parent, caption, directory, '',
                        QFileDialog.Directory, QFileDialog.AcceptOpen,
                        dirs_only=True)
