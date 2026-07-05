"""Single PySide2/PySide6 import shim.

Usage: `from Nukomfy.utils.qt_compat import QtCore, QtGui, QtWidgets`
"""
try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide6 import QtCore, QtGui, QtWidgets


def _nuke_main_window():
    """Return Nuke's main window, or None if not found.

    Matches the DockMainWindow named 'NukeMainWindow' so we never latch
    onto another tool's top-level QMainWindow (any other docked tool),
    whose stylesheet would otherwise cascade into our panels. Among Nuke's
    own dock windows this also skips floating panels (Viewer, Properties)
    in favour of the real main window. Fails safe to None (panel becomes a
    standalone window) rather than parenting to a wrong window.
    """
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    dock_windows = []
    for w in app.topLevelWidgets():
        try:
            if w.metaObject().className() == 'Foundry::UI::DockMainWindow':
                dock_windows.append(w)
        except Exception:
            continue
    for w in dock_windows:
        if w.objectName() == 'NukeMainWindow':
            return w
    return dock_windows[0] if dock_windows else None
