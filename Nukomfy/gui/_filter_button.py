"""Reusable status-filter dropdown button.

A flat 'Filter (N)' button with a Material caret that opens a checkable
multi-select menu of job statuses. Fixed-width (sized for the widest label)
with the content centred, so toggling never shifts the surrounding toolbar.
Optionally persists the selection in ui_state. Shared by the machine job
viewer and the MyJobs tab so the control is defined once.
"""
from Nukomfy.utils.qt_compat import QtWidgets, QtCore
from Nukomfy.gui.icons import icon_font, ARROW_DROP_DOWN
from Nukomfy.gui.ui_state import ui_state

# Standard job-status filter options (display label, nfy_status_str value).
# Single source shared by every status filter (viewer, MyJobs, ...).
JOB_STATUS_FILTERS = [
    ('Completed', 'completed'),
    ('Failed', 'failed'),
    ('Cancelled', 'cancelled'),
    ('Running', 'running'),
    ('Queued', 'pending'),
]


class StatusFilterButton(QtWidgets.QPushButton):
    """'Filter (N)' dropdown of checkable status options.

    Parameters
    ----------
    options : list[tuple[str, str]]
        (display label, nfy_status_str value) pairs.
    on_changed : callable, optional
        Called with no args whenever the selection changes.
    state_key : str, optional
        ui_state key; when set, the selection is persisted across sessions.
    """

    def __init__(self, options, on_changed=None, state_key=None, parent=None):
        super().__init__(parent)
        self._options = list(options)
        self._on_changed = on_changed
        self._state_key = state_key
        self._statuses = self._load()

        self._menu = QtWidgets.QMenu(self)
        self._actions = []
        for label, value in self._options:
            act = self._menu.addAction(label)
            act.setCheckable(True)
            act.setData(value)
            act.setChecked(value in (self._statuses or []))
            # Connect after the initial setChecked so it doesn't fire mid-build.
            act.toggled.connect(lambda _=False: self._on_toggle())
            self._actions.append(act)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(6)
        self._label = QtWidgets.QLabel('Filter')
        self._label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        caret = QtWidgets.QLabel(ARROW_DROP_DOWN)
        caret.setFont(icon_font(14))
        caret.setStyleSheet('color:#bbb;')
        caret.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        lay.addStretch(1)
        lay.addWidget(self._label)
        lay.addWidget(caret)
        lay.addStretch(1)
        fw = self._label.fontMetrics().horizontalAdvance('Filter (5)')
        cw = caret.fontMetrics().horizontalAdvance(ARROW_DROP_DOWN)
        self.setFixedWidth(fw + cw + 6 + 20)
        self.clicked.connect(self._popup)
        self._update_label()

    # QPushButton sizes from its text; size from the child layout instead so
    # the label + caret are never clipped.
    def sizeHint(self):
        lay = self.layout()
        return lay.sizeHint() if lay is not None else super().sizeHint()

    def minimumSizeHint(self):
        lay = self.layout()
        return (lay.minimumSize() if lay is not None
                else super().minimumSizeHint())

    def selected(self):
        """Selected status values, or None when none are checked ('all')."""
        return self._statuses

    def _load(self):
        if not self._state_key:
            return None
        valid = {v for _, v in self._options}
        saved = ui_state.get(self._state_key).get('statuses', [])
        sel = [s for s in saved if s in valid] if isinstance(saved, list) else []
        return sel or None

    def _on_toggle(self):
        self._statuses = [a.data() for a in self._actions
                          if a.isChecked()] or None
        if self._state_key:
            ui_state.set(self._state_key, statuses=self._statuses or [])
        self._update_label()
        if self._on_changed:
            self._on_changed()

    def _update_label(self):
        n = len(self._statuses or [])
        self._label.setText('Filter' if not n else 'Filter ({})'.format(n))

    def _popup(self):
        self._menu.exec_(self.mapToGlobal(QtCore.QPoint(0, self.height())))
