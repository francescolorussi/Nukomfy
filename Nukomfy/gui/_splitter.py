"""Shared dotted splitter.

Two-stroke grip handle used by Submit panel and My Jobs tab - kept here
so both callers use the same visual style without copy-paste.
"""

from Nukomfy.utils.qt_compat import QtCore, QtGui, QtWidgets


class DottedSplitterHandle(QtWidgets.QSplitterHandle):
    """Custom splitter handle with two centred grip strokes. No hover state.

    Orientation-aware: for a vertical splitter (horizontal handle) the strokes
    spread horizontally; for a horizontal splitter (vertical handle) they
    spread vertically along the handle.
    """

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor('#666'))
        cx = self.width() // 2
        cy = self.height() // 2
        if self.orientation() == QtCore.Qt.Vertical:
            # Horizontal strip handle: two short horizontal strokes ("==" grip)
            # stacked vertically.
            stroke_w = 14
            stroke_h = 2
            inner_gap = 2
            x_left = cx - stroke_w // 2
            # Shift down a few px to compensate asymmetric groupbox gap.
            cy = cy + 3
            y_top = cy - inner_gap // 2 - stroke_h
            y_bot = cy + inner_gap // 2
            p.drawRoundedRect(x_left, y_top, stroke_w, stroke_h, 1, 1)
            p.drawRoundedRect(x_left, y_bot, stroke_w, stroke_h, 1, 1)
        else:
            # Vertical strip handle: two short vertical strokes ("||" grip)
            # centered.
            stroke_w = 2
            stroke_h = 14
            inner_gap = 2
            x_left = cx - inner_gap // 2 - stroke_w
            x_right = cx + inner_gap // 2
            y_top = cy - stroke_h // 2
            p.drawRoundedRect(x_left, y_top, stroke_w, stroke_h, 1, 1)
            p.drawRoundedRect(x_right, y_top, stroke_w, stroke_h, 1, 1)
        p.end()


class DottedSplitter(QtWidgets.QSplitter):
    """QSplitter that renders its handles with DottedSplitterHandle."""

    def createHandle(self):
        return DottedSplitterHandle(self.orientation(), self)
