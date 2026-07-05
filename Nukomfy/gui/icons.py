"""Loads the Material Icons font and exposes codepoint constants.

Font: MaterialIcons-Regular.ttf (Google, Apache 2.0 - see resources/icons/LICENSE)
"""

import logging
import os

from Nukomfy.utils.qt_compat import QtCore, QtGui

_log = logging.getLogger(__name__)

_loaded = False


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    _loaded = True
    # resources/ lives at the package root, one level above this gui/ module.
    ttf = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'resources', 'icons', 'MaterialIcons-Regular.ttf')
    if QtGui.QFontDatabase.addApplicationFont(ttf) == -1:
        _log.warning("Material Icons font not loaded: %s", ttf)


def icon_font(size=14):
    """Return a QFont for Material Icons at the given pixel size."""
    _ensure_loaded()
    f = QtGui.QFont('Material Icons')
    f.setPixelSize(size)
    return f


def _material_pixmap(codepoint, color, size):
    """Internal: rasterize a Material Icons glyph to a QPixmap.

    Renders at 2x resolution for crisp display on HiDPI screens.
    """
    _ensure_loaded()
    scale = 2
    real = size * scale
    px = QtGui.QPixmap(real, real)
    px.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(px)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setRenderHint(QtGui.QPainter.TextAntialiasing)
    p.setPen(QtGui.QColor(color))
    f = QtGui.QFont('Material Icons')
    f.setPixelSize(real)
    p.setFont(f)
    p.drawText(px.rect(), QtCore.Qt.AlignCenter, codepoint)
    p.end()
    px.setDevicePixelRatio(scale)
    return px


def material_icon(codepoint, color='#ccc', size=16):
    """Create a single-state QIcon from a Material Icons codepoint."""
    return QtGui.QIcon(_material_pixmap(codepoint, color, size))


def set_press_icon(btn, codepoint, size=14, off='#ccc'):
    """Set a Material icon on `btn`."""
    btn.setIcon(material_icon(codepoint, off, size))


# Codepoints ----------------------------------------------------------------
# Reference: github.com/google/material-design-icons/blob/master/font/MaterialIcons-Regular.codepoints

# Toolbar / navigation
GRID_VIEW       = '\ue9b0'
VIEW_LIST       = '\ue8ef'
REFRESH         = '\ue5d5'
SEARCH          = '\ue8b6'
ADD             = '\ue145'
CLOSE           = '\ue5cd'
REMOVE          = '\ue15b'   # minus sign - dequeue a pending job

# Media
PLAY_ARROW      = '\ue037'
PAUSE           = '\ue034'
MOVIE           = '\ue02c'   # film reel - placeholder for GIF cards awaiting thumb

# Favorites
STAR            = '\ue838'
STAR_BORDER     = '\ue83a'

# Arrows
ARROW_UPWARD    = '\ue5d8'
ARROW_DOWNWARD  = '\ue5db'
ARROW_DROP_DOWN = '\ue5c5'   # arrow_drop_down (dropdown caret)

# Status
CHECK_CIRCLE    = '\ue86c'
ERROR           = '\ue000'
WARNING         = '\ue002'
WARNING_AMBER   = '\uf083'   # warning (outline triangle + !)
CIRCLE          = '\ue061'   # fiber_manual_record
HELP_OUTLINE    = '\ue8fd'   # help_outline (? inside a circle - unknown/lost jobs)
BLOCK           = '\ue14b'   # block (circle + diagonal slash - cancelled; UTF twin \u2298)
CANCEL          = '\ue5c9'   # cancel (filled circle + X) - Failed status (UTF twin \u2716)

# Actions
RESTART_ALT     = '\ue042'   # replay (counter-clockwise circular arrow)
EDIT            = '\ue3c9'   # edit (pencil)
CLOUD_DOWNLOAD  = '\ue2c0'   # cloud_download (cloud with arrow down - fetch from server)
SETTINGS_BACKUP_RESTORE = '\ue8ba'  # settings_backup_restore (circular arrow + dot - reset to defaults)
DELETE_SWEEP   = '\ue16c'   # delete_sweep (broom sweeping trash - bulk clear)
DELETE         = '\ue872'   # delete (single trash can - remove one entry)


# Image placeholders
ADD_PHOTO_ALTERNATE = ''   # image frame with `+` - load/upload an image
# Submit / queue
PUBLISH             = '\ue255'   # publish (arrow up from line - upload/submit)
LIST                = '\ue896'   # list (stacked lines with bullets)

# MyJobs row actions
DESCRIPTION         = '\ue873'   # description (document with lines - view log)
FILE_DOWNLOAD       = '\ue2c4'   # file_download (arrow down into line - import outputs)

# Checkboxes
CHECK_BOX           = '\ue834'
CHECK_BOX_BLANK     = '\ue835'   # check_box_outline_blank

# Lock state - settings_overrides indicator
LOCK                = '\ue897'   # lock (filled padlock - locked by global defaults)
