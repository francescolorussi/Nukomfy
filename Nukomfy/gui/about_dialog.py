"""About dialog for the Nukomfy plugin."""

import os
import webbrowser

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui

from Nukomfy.gui.ui_state import center_on_screen
from Nukomfy.gui._theme import apply_window_chrome, WARNING_INLINE
from Nukomfy.version import __version__, __url__




class AboutDialog(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('About Nukomfy')
        self.setFixedSize(540, 260)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Window)
        self.setStyleSheet(
            'QDialog{background:#252525;color:#ccc;}'
            'QLabel{background:transparent;}')
        # Append the chrome now, before the labels are built: a stylesheet'd
        # dialog only picks up the label baseline on labels created after the
        # full stylesheet is in place. The dialog's own background above is
        # kept (chrome is appended, not replaced).
        apply_window_chrome(self)

        _res = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'resources')

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 18)
        lay.setSpacing(0)

        # Raster assets oversampled to physical pixels for crisp HiDPI display.
        dpr = self.devicePixelRatioF()

        logo_path = os.path.join(_res, 'nukomfy_logo.png')
        if os.path.isfile(logo_path):
            pixmap = QtGui.QPixmap(logo_path)
            pixmap = pixmap.scaledToWidth(
                round(320 * dpr), QtCore.Qt.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
            logo_label = QtWidgets.QLabel()
            logo_label.setPixmap(pixmap)
            logo_label.setAlignment(QtCore.Qt.AlignCenter)
            lay.addWidget(logo_label)

        lay.addSpacing(14)

        # Version row: version label + auto-derived prerelease chip (ALPHA/BETA/RC)
        version_row = QtWidgets.QHBoxLayout()
        version_row.setAlignment(QtCore.Qt.AlignCenter)
        version_row.setSpacing(8)
        version_row.setContentsMargins(0, 0, 0, 0)

        version_label = QtWidgets.QLabel(
            '<span style="color:#888;font-size:12px;">'
            'Version: {}</span>'.format(__version__))
        version_row.addWidget(version_label)

        if '-' in __version__:
            tag = __version__.split('-', 1)[1].split('.')[0].upper()
            chip = QtWidgets.QLabel(tag)
            chip.setStyleSheet(
                'QLabel{background:' + WARNING_INLINE + ';color:#fff;'
                'padding:1px 7px;border-radius:3px;'
                'font-size:9px;font-weight:bold;}')
            version_row.addWidget(chip)

        lay.addLayout(version_row)
        lay.addSpacing(4)

        author_label = QtWidgets.QLabel(
            '<span style="color:#888;font-size:12px;">Author: Francesco Lorussi</span>')
        author_label.setAlignment(QtCore.Qt.AlignCenter)
        lay.addWidget(author_label)

        lay.addSpacing(14)

        # GitHub icon (QLabel - same rendering pipeline as logo, no filters)
        gh_icon_path = os.path.join(_res, 'github_logo.png')
        if os.path.isfile(gh_icon_path):
            icon_sz = 28
            gh_px = QtGui.QPixmap(gh_icon_path)
            gh_px = gh_px.scaled(round(icon_sz * dpr), round(icon_sz * dpr),
                                 QtCore.Qt.KeepAspectRatio,
                                 QtCore.Qt.SmoothTransformation)
            gh_px.setDevicePixelRatio(dpr)
            gh_label = QtWidgets.QLabel()
            gh_label.setPixmap(gh_px)
            gh_label.setAlignment(QtCore.Qt.AlignCenter)
            gh_label.setCursor(QtCore.Qt.PointingHandCursor)
            gh_label.setToolTip('View on GitHub')
            gh_label.mousePressEvent = lambda _: webbrowser.open(__url__)

            gh_lay = QtWidgets.QHBoxLayout()
            gh_lay.setAlignment(QtCore.Qt.AlignCenter)
            gh_lay.addWidget(gh_label)
            lay.addLayout(gh_lay)

        # Born centered on the monitor that holds Nuke's main window.
        center_on_screen(self)
