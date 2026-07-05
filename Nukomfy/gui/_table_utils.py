"""Shared QTableWidget column utilities.

Adjacent-column resize + proportional scaling on viewport resize, so the
total column width always matches the viewport and user-chosen proportions
survive shrink->expand cycles.
"""

from Nukomfy.utils.qt_compat import QtCore, QtWidgets


_COL_MIN = 30


class _ViewportResizeFilter(QtCore.QObject):
    def __init__(self, table, absorber_col):
        super().__init__(table)
        self._table = table
        self._col = absorber_col

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Resize:
            _proportional_fit(self._table)
        return False


class _BoundaryLockFilter(QtCore.QObject):
    """Suppress Qt resize grip at boundaries between an Interactive section
    and a Fixed section to its right. Qt draws a grip at the right edge of
    every Interactive section even when the next section is Fixed; dragging
    it lets the absorber redistribute, which reads as "drag right, column
    shrinks left".
    """

    GRIP_TOLERANCE = 4

    def __init__(self, header):
        super().__init__(header)
        self._header = header
        self._cursor_overridden = False

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QtCore.QEvent.MouseMove:
            if self._is_near_locked_boundary(event.pos().x()):
                if not self._cursor_overridden:
                    obj.setCursor(QtCore.Qt.ArrowCursor)
                    self._cursor_overridden = True
                return True
            if self._cursor_overridden:
                obj.unsetCursor()
                self._cursor_overridden = False
            return False
        if et in (QtCore.QEvent.MouseButtonPress,
                  QtCore.QEvent.MouseButtonDblClick):
            if event.button() != QtCore.Qt.LeftButton:
                return False
            return self._is_near_locked_boundary(event.pos().x())
        return False

    def _is_near_locked_boundary(self, x):
        h = self._header
        Fixed = QtWidgets.QHeaderView.Fixed
        count = h.count()
        for visual_i in range(count - 1):
            logical_i = h.logicalIndex(visual_i)
            logical_next = h.logicalIndex(visual_i + 1)
            if h.isSectionHidden(logical_i) or h.isSectionHidden(logical_next):
                continue
            if h.sectionResizeMode(logical_i) == Fixed:
                continue
            if h.sectionResizeMode(logical_next) != Fixed:
                continue
            edge = h.sectionViewportPosition(logical_i) + h.sectionSize(logical_i)
            if abs(x - edge) <= self.GRIP_TOLERANCE:
                return True
        return False


def _install_boundary_lock(table):
    """Lock resize grips at boundaries adjacent to Fixed sections.

    Callable independently of _install_absorber for tables that don't use
    the absorber pattern (simple fixed-right tables).
    """
    header = table.horizontalHeader()
    flt = _BoundaryLockFilter(header)
    header.viewport().installEventFilter(flt)
    table._boundary_lock_filter = flt
    return flt


def _proportional_fit(table):
    """Scale Interactive columns to fill the viewport, preserving ratios."""
    h = table.horizontalHeader()
    vw = table.viewport().width()
    if vw <= 0:
        return
    interactive = []
    fixed_total = 0
    for i in range(h.count()):
        if h.sectionResizeMode(i) == QtWidgets.QHeaderView.Fixed:
            fixed_total += h.sectionSize(i)
        else:
            interactive.append(i)
    if not interactive:
        return
    available = vw - fixed_total
    if available <= len(interactive) * _COL_MIN:
        return
    ratios = getattr(table, '_col_ratios', None)
    if ratios is None or len(ratios) != len(interactive):
        cur = [h.sectionSize(i) for i in interactive]
        cur_total = sum(cur) or 1
        ratios = [c / cur_total for c in cur]
        table._col_ratios = ratios

    h.blockSignals(True)
    widths = [max(_COL_MIN, int(available * r)) for r in ratios]
    remainder = available - sum(widths)
    widest = widths.index(max(widths))
    widths[widest] += remainder
    for j, col in enumerate(interactive):
        table.setColumnWidth(col, widths[j])
    h.blockSignals(False)


def _install_absorber(table, absorber_col):
    """Install adjacent-column resize + viewport-resize proportional fit.

    When ``absorber_col is None``, viewport underflow after a manual drag is
    redistributed proportionally across all Interactive sections via
    ``_proportional_fit`` instead of being absorbed by a single column.
    """
    h = table.horizontalHeader()

    def _find_neighbor(idx, direction=1):
        i = idx + direction
        while 0 <= i < h.count():
            if h.sectionResizeMode(i) != QtWidgets.QHeaderView.Fixed:
                return i
            i += direction
        return -1

    def _on_section_resized(idx, old_size, new_size):
        if old_size == 0 or new_size == 0:
            return
        vw = table.viewport().width()
        if vw <= 0:
            return
        diff = new_size - old_size
        if diff == 0:
            return

        h.blockSignals(True)

        neighbor = _find_neighbor(idx, 1)
        if neighbor < 0:
            neighbor = _find_neighbor(idx, -1)
        if neighbor >= 0:
            cur_neighbor = h.sectionSize(neighbor)
            new_neighbor = cur_neighbor - diff
            if new_neighbor < _COL_MIN:
                clamped = cur_neighbor - _COL_MIN
                table.setColumnWidth(neighbor, _COL_MIN)
                table.setColumnWidth(idx, old_size + clamped)
            else:
                table.setColumnWidth(neighbor, new_neighbor)
        else:
            table.setColumnWidth(idx, old_size)

        total = sum(h.sectionSize(i) for i in range(h.count()))
        if total > vw:
            excess = total - vw
            table.setColumnWidth(idx, max(_COL_MIN, h.sectionSize(idx) - excess))
        elif total < vw:
            if absorber_col is None:
                _proportional_fit(table)
            else:
                cur_abs = h.sectionSize(absorber_col)
                table.setColumnWidth(absorber_col, cur_abs + (vw - total))

        inter = [i for i in range(h.count())
                 if h.sectionResizeMode(i) != QtWidgets.QHeaderView.Fixed]
        sizes = [h.sectionSize(i) for i in inter]
        total = sum(sizes) or 1
        table._col_ratios = [s / total for s in sizes]

        h.blockSignals(False)

    h.sectionResized.connect(_on_section_resized)
    filt = _ViewportResizeFilter(table, absorber_col)
    table.viewport().installEventFilter(filt)

    _install_boundary_lock(table)
