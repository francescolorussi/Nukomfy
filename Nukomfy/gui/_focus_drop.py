"""Event filter that clears focus and selection on outside clicks.

Installs an event filter on a dialog so that clicking outside any editable
widget (QLineEdit / QAbstractSpinBox / QTextEdit / QPlainTextEdit) clears
its focus, AND any item view (QTableWidget / QListWidget / etc.) whose
viewport doesn't contain the click loses its selection.

Clearing focus fires `editingFinished` on line edits / spin boxes, which is
what the rest of the UI relies on to commit/validate values. The selection
clear runs with `blockSignals(True)` so state-bearing tables (e.g. the
Submit Panel machine selector that drives `_selected_machine`) keep their
underlying app state intact - only the visual highlight is removed.

The filter is installed on QApplication and scoped to the dialog window
via `obj.window() is scope`, so it intercepts clicks on child widgets
(other tables, group boxes, layout backgrounds) - not just clicks on the
dialog's bare background.

OPT-OUT: row-action buttons (Add / Remove / Edit / Delete / Move Up/Down
that operate on the currently selected row of a table) must NOT trigger a
selection clear, otherwise the action fires with an empty selection and
silently no-ops. Mark such widgets with the dynamic property
`_keep_selection`:
    btn.setProperty('_keep_selection', True)        # preserve all views
    btn.setProperty('_keep_selection', view)        # preserve only `view`
    btn.setProperty('_keep_selection', (v1, v2))    # preserve a subset
The marker is checked by walking up the parent chain from the press
target, so wrapping a marked button in extra layout widgets is fine.

OPT-IN: a dialog whose tables are the primary row-selection UI passes
`manage_row_selection=True`. A press on empty table space or a
non-selectable row then clears the selection like an outside click, while a
press on a column header preserves it (so a resize never drops the active
row). Pass `on_cleared` to refresh UI derived from the selection after a
clear, since the clear is blockSignals'd and fires no itemSelectionChanged.
"""

from Nukomfy.utils.qt_compat import QtCore, QtWidgets


_EDITABLE = (
    QtWidgets.QLineEdit,
    QtWidgets.QAbstractSpinBox,
    QtWidgets.QTextEdit,
    QtWidgets.QPlainTextEdit,
)


def _ancestor_itemview(w):
    p = w.parent() if w is not None else None
    while p is not None:
        if isinstance(p, QtWidgets.QAbstractItemView):
            return p
        p = p.parent()
    return None


def _ancestor_headerview(w):
    p = w
    while p is not None:
        if isinstance(p, QtWidgets.QHeaderView):
            return p
        p = p.parent()
    return None


def _collect_keep_selection(obj):
    """Walk parent chain from `obj` and aggregate `_keep_selection` markers.

    Returns (keep_all, keep_views) where:
      - keep_all is True if any ancestor's marker is the bool True;
      - keep_views is a set of QAbstractItemView whose selection must be
        preserved when the click is dispatched.
    Empty / no marker -> (False, set()) and the normal clearing applies.
    """
    keep_all = False
    keep_views = set()
    cur = obj
    while cur is not None:
        try:
            val = cur.property('_keep_selection')
        except Exception:
            val = None
        if val is True:
            keep_all = True
        elif isinstance(val, QtWidgets.QAbstractItemView):
            keep_views.add(val)
        elif isinstance(val, (list, tuple, set)):
            for v in val:
                if isinstance(v, QtWidgets.QAbstractItemView):
                    keep_views.add(v)
        cur = cur.parent()
    return keep_all, keep_views


class _FocusDropFilter(QtCore.QObject):
    def __init__(self, scope, on_cleared=None, manage_row_selection=False):
        # parent=scope so Qt auto-deletes the filter when the dialog dies
        super().__init__(scope)
        self._scope = scope
        self._on_cleared = on_cleared
        self._manage_rows = manage_row_selection

    def eventFilter(self, obj, e):
        if e.type() != QtCore.QEvent.MouseButtonPress:
            return False

        # App-level filter: scope to our dialog window only.
        if not isinstance(obj, QtWidgets.QWidget):
            return False
        if obj.window() is not self._scope:
            return False

        try:
            gp = (e.globalPos() if hasattr(e, 'globalPos')
                  else e.globalPosition().toPoint())
        except Exception:
            return False

        # 1. Editable focus drop: commit & clear focus when click lands
        #    outside the focused editor.
        fw = QtWidgets.QApplication.focusWidget()
        if isinstance(fw, _EDITABLE) and fw.window() is self._scope:
            local = fw.mapFromGlobal(gp)
            if not fw.rect().contains(local):
                view = _ancestor_itemview(fw)
                if view is not None:
                    try:
                        view.commitData(fw)
                        view.closeEditor(
                            fw,
                            QtWidgets.QAbstractItemDelegate.NoHint)
                    except Exception:
                        fw.clearFocus()
                else:
                    fw.clearFocus()

        # 2. Item view selection clear: any view whose viewport doesn't
        #    contain the click loses its selection. blockSignals avoids
        #    spurious itemSelectionChanged firing on state-bearing tables
        #    (e.g. Submit Panel machine selector - clearing the visual
        #    selection should NOT clear the underlying _selected_machine
        #    state the user already chose).
        # Opt-out: row-action buttons (toolbar Add/Remove/Up/Down, panel
        # Edit/Delete) tag themselves with `_keep_selection` so the click
        # that triggers them doesn't first wipe the very row they need.
        keep_all, keep_views = _collect_keep_selection(obj)
        # A press on a column header (resize / click) must not drop the row
        # selection when the dialog owns row selection as primary state -
        # otherwise widening a column deselects the row being edited.
        if self._manage_rows and not keep_all:
            if _ancestor_headerview(obj) is not None:
                keep_all = True
        cleared_any = False
        if not keep_all:
            for view in self._scope.findChildren(QtWidgets.QAbstractItemView):
                if view in keep_views:
                    continue
                # QHeaderView is a QAbstractItemView and shares its parent
                # table's selection model. Its tiny viewport never contains a
                # click inside the table body, so without this skip a click in
                # an open cell editor would wipe the SHARED currentIndex via
                # the header - committing and closing the editor mid-edit.
                if isinstance(view, QtWidgets.QHeaderView):
                    continue
                sm = view.selectionModel()
                if sm is None:
                    continue
                # Drop both selection AND currentIndex when the click lands
                # outside the viewport. Tables in `NoSelection` mode (Render
                # Manager Queue / History / MyJobs) never carry a selection
                # but Qt still tracks `currentIndex` and renders it as a
                # single-cell highlight - that's the residual orange the user
                # sees on individual cells.
                has_sel = sm.hasSelection()
                has_cur = sm.currentIndex().isValid()
                if not has_sel and not has_cur:
                    continue
                vp = view.viewport()
                local = vp.mapFromGlobal(gp)
                if vp.rect().contains(local):
                    # Inside the table body. With manage_row_selection, preserve
                    # only when the press lands on a real, SELECTABLE row; an
                    # empty-area press (no row under the cursor) or a press on a
                    # non-selectable row (e.g. a fixed section header) clears
                    # like an outside click, so the highlight tracks "a
                    # selectable row is selected".
                    if not self._manage_rows:
                        continue
                    idx = view.indexAt(local)
                    if idx.isValid() and bool(
                            idx.flags() & QtCore.Qt.ItemIsSelectable):
                        continue
                view.blockSignals(True)
                try:
                    if has_sel:
                        view.clearSelection()
                    if has_cur:
                        view.setCurrentIndex(QtCore.QModelIndex())
                finally:
                    view.blockSignals(False)
                cleared_any = True

        # blockSignals above suppresses itemSelectionChanged, so a dialog that
        # derives UI from the selection (e.g. a reorder toolbar) is notified
        # here instead.
        if cleared_any and self._on_cleared is not None:
            try:
                self._on_cleared()
            except Exception:
                pass
        return False


def install(dialog, on_cleared=None, manage_row_selection=False):
    """Attach a focus-drop + selection-drop filter to the dialog. Idempotent.

    on_cleared: optional callable invoked once after a press clears one or
        more views' selection. Lets a dialog refresh UI derived from the
        selection (e.g. a reorder toolbar's enabled state), which the
        blockSignals'd clear would otherwise leave stale.
    manage_row_selection: when True the dialog's tables are treated as
        primary row-selection UI: a press on empty table space (no row under
        the cursor) clears like an outside click, while a press on a column
        header preserves the selection so a resize never drops the active row.
    """
    if getattr(dialog, '_focus_drop_filter', None) is not None:
        return
    f = _FocusDropFilter(dialog, on_cleared, manage_row_selection)
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    app.installEventFilter(f)
    dialog._focus_drop_filter = f
    # Auto-cleanup when the dialog object is destroyed (Qt parents the
    # filter to scope above, but explicit removal keeps the app's filter
    # list tidy in long-lived sessions).
    try:
        dialog.destroyed.connect(
            lambda _o=None, _a=app, _f=f: _a.removeEventFilter(_f))
    except Exception:
        pass
