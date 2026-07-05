"""Shared Qt stylesheet constants."""

# Semantic palette.
# Single source of truth for accent/status hex values used across panels.
# Structural greys (#1a1a1a, #1e1e1e, #2a2a2a, #333, #444, etc.) stay
# inline in the individual files: they have no semantic meaning, they're
# just dark-theme scaffold.

# Status / state colors.
ERROR_COLOR          = '#e53935'   # error, failed, offline
WARNING_STATUS       = '#f99500'   # queued, interrupted, attention-grab indicators
WARNING_INLINE       = '#d97f3a'   # inline form banners (Settings, Workflow Creator)
SUCCESS_COLOR        = '#6abf6e'   # success, running, online
INFO_COLOR           = '#3fa3f0'   # completed, info
ACCENT_GOLD          = '#daa520'   # favorites, toolbar checked
UNAVAILABLE_COLOR    = '#9c7fc7'   # cooperative soft-lock (Nukomfy Manager
                                   # Availability flag)

# Hover variants (lighter than base).
ERROR_HOVER          = '#ff5252'
WARNING_STATUS_HOVER = '#ffb347'
SUCCESS_HOVER        = '#8fd090'

# Library badge palette - family-aware pattern (same-hue dual-luminance:
# dark dim bg + saturated bright fg, matches Local/Shared badges). Steel +
# Rust palette: cool grey-blue for Categories, warm orange-brown for Mod.
# Desaturated industrial tones coherent with VFX/render-farm identity,
# non-clashing with gold favorites, green Local, blue Shared accents.
LIBRARY_CAT_BG       = '#2a3540'   # cat bg - HSL(210°, 21%, 21%) steel cool grey
LIBRARY_CAT_FG       = '#90b0c8'   # cat fg - HSL(206°, 33%, 67%) steel bright
LIBRARY_MOD_BG       = '#3e2e26'   # mod bg - HSL(20°, 24%, 20%) rust warm
LIBRARY_MOD_FG       = '#d49575'   # mod fg - HSL(20°, 50%, 65%) rust bright
# Sidebar section titles + checked-checkbox icon: deeper version of fg
# (same hue/sat, lower L) so the title reads as the badge family on the
# #222 sidebar bg and the active filter checkbox carries the same accent.
LIBRARY_CAT_TITLE    = '#5a85aa'   # cat sidebar - HSL(208°, 32%, 51%) steel deep
LIBRARY_MOD_TITLE    = '#c47545'   # mod sidebar - HSL(23°, 52%, 52%) rust deep

# Library source badges. Desaturated sage green for Local + slate blue
# for Shared, matched to the Steel & Rust industrial language. They sit
# at equivalent weight to Cat/Mod tags so the source reads as metadata,
# not as the primary card accent. Fg also reused as 1px border by _draw_badge.
LIBRARY_LOCAL_BG     = '#2a3a2a'   # local bg - HSL(120°, 16%, 20%) sage dark
LIBRARY_LOCAL_FG     = '#85b585'   # local fg/border - HSL(120°, 25%, 62%) sage bright
LIBRARY_SHARED_BG    = '#22324a'   # shared bg - HSL(213°, 36%, 21%) slate dark
LIBRARY_SHARED_FG    = '#7c9fc8'   # shared fg/border - HSL(213°, 38%, 64%) slate bright

# Library duplicate-id badge (orange family).
LIBRARY_DUP_BG       = '#5a3a0e'
LIBRARY_DUP_FG       = '#e4a15e'

# Library badge font size (base px, scale-aware via int(LIBRARY_BADGE_FONT_PX * scale)).
LIBRARY_BADGE_FONT_PX = 12

# Hyperlink color for <a href> in dark-theme dialogs (Qt's default
# #0000ee is unreadable on Nuke's dark gray backgrounds).
LINK_FG              = '#5eb3e4'   # cyan-blue, dark-theme hyperlink standard


TOOLTIP_STYLE = (
    'QToolTip{background:#1e1e1e;color:#ccc;border:1px solid #555;padding:4px;}'
)

# Shared scrollbar styling - applied at the dialog/panel root so it
# cascades to every scrollable child (QScrollArea, QPlainTextEdit,
# QTableWidget, QListView, QTextEdit). Without this, Qt falls back to
# the OS-native scrollbar which on Windows is wider, lighter, and
# visually inconsistent with the rest of the plugin.
SCROLLBAR_STYLE = (
    'QScrollBar:vertical{background:#1e1e1e;width:8px;margin:0;}'
    'QScrollBar::handle:vertical{background:#444;border-radius:3px;'
    'min-height:20px;}'
    'QScrollBar::handle:vertical:hover{background:#555;}'
    'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical'
    '{height:0px;width:0px;}'
    'QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical'
    '{background:transparent;}'
    'QScrollBar:horizontal{background:#1e1e1e;height:8px;margin:0;}'
    'QScrollBar::handle:horizontal{background:#444;border-radius:3px;'
    'min-width:20px;}'
    'QScrollBar::handle:horizontal:hover{background:#555;}'
    'QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal'
    '{height:0px;width:0px;}'
    'QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal'
    '{background:transparent;}'
)


# Text color for labels and checkboxes that carry no color of their own,
# applied at the window root so it cascades to every such child. Pins them to
# Nuke's measured WindowText (active / disabled); a host mutating the global
# palette then cannot recolor them, and unlike a palette this QSS cascades
# through the QStyleSheetStyle the window stylesheet installs. Group titles are
# unaffected (QGroupBox::title is not a QLabel), and a label with its own color
# wins over this baseline.
LABEL_BASELINE_STYLE = (
    'QLabel{color:#c8c8c8;}'
    'QLabel:disabled{color:#7d7d7d;}'
    'QCheckBox{color:#c8c8c8;}'
    'QCheckBox:disabled{color:#7d7d7d;}'
)


# Nuke's default widget palette, measured from a clean session. A host tool
# sharing this QApplication can mutate the global palette (this host recolored
# PlaceholderText), and any role left inherited follows that mutation. Pinning
# these measured defaults holds the look deterministic; the values are Nuke's
# own, so a clean session is visually unchanged. Each entry is (active,
# disabled) ARGB; disabled falls back to active when None, and Inactive always
# mirrors active. ToolTipBase/ToolTipText and Link are intentionally absent:
# tooltips are already forced dark via TOOLTIP_STYLE and links to LINK_FG
# inline, so pinning Nuke's yellow tooltip / blue link would be dead weight.
_NUKE_PALETTE = {
    'Window':          ((50, 50, 50, 255),    None),
    'WindowText':      ((200, 200, 200, 255), (125, 125, 125, 255)),
    'Base':            ((58, 58, 58, 255),     None),
    'Text':            ((200, 200, 200, 255), (125, 125, 125, 255)),
    'Highlight':       ((247, 147, 30, 255),   None),
    'HighlightedText': ((30, 30, 30, 255),     None),
    'Button':          ((77, 77, 77, 255),     None),
    'ButtonText':      ((200, 200, 200, 255), (125, 125, 125, 255)),
    'BrightText':      ((200, 200, 200, 255), (125, 125, 125, 255)),
    'PlaceholderText': ((255, 255, 255, 127), (25, 25, 25, 127)),
}

def _pin_roles(pal):
    """Pin the measured Nuke roles onto an existing QPalette and return it.

    Roles absent on the running Qt (e.g. PlaceholderText before 5.12) are
    skipped. Inactive mirrors Active; Disabled uses its own value when given.
    """
    from Nukomfy.utils.qt_compat import QtGui
    QPalette = QtGui.QPalette
    for name, (active, disabled) in _NUKE_PALETTE.items():
        role = getattr(QPalette, name, None)
        if role is None:
            continue
        active_c = QtGui.QColor(*active)
        pal.setColor(QPalette.Active, role, active_c)
        pal.setColor(QPalette.Inactive, role, active_c)
        pal.setColor(QPalette.Disabled, role,
                     QtGui.QColor(*disabled) if disabled else active_c)
    return pal


def apply_nukomfy_palette(widget):
    """Pin the measured Nuke palette roles on `widget`.

    Called from our input-field subclasses at construction. A host that mutates
    the global palette then cannot recolor the widget's background, text,
    selection or placeholder: these roles are pinned on the widget itself,
    since a palette set on the window does not reach it through the
    QStyleSheetStyle a stylesheet installs. Values are Nuke's defaults, so the
    look is unchanged; only resistance to global-palette pollution is added.
    Roles absent before Qt 5.12 (PlaceholderText) are skipped.
    """
    widget.setPalette(_pin_roles(widget.palette()))


def apply_window_chrome(widget):
    """Apply the shared tooltip, scrollbar and label/checkbox baseline.

    Label and checkbox colors are set here: this QSS cascades to children and
    beats a host-mutated global palette. Input-field colors are pinned per
    field instead (see apply_nukomfy_palette), because a palette set on the
    window does not reach the fields through the QStyleSheetStyle a widget
    stylesheet installs. The chrome is appended to the widget's existing
    stylesheet so a dialog that sets its own (e.g. a background) keeps it -
    call this once, after the widget sets its own stylesheet; a second call is
    a no-op.
    """
    if widget.property('_nfy_chrome'):
        return
    widget.setProperty('_nfy_chrome', True)
    widget.setStyleSheet(
        widget.styleSheet() + TOOLTIP_STYLE + SCROLLBAR_STYLE
        + LABEL_BASELINE_STYLE)


# Row/cell selection background. Vertical gold gradient matching Nuke's
# native selection style (#ff9f30 -> #d7801a top->bottom). Single source
# of truth - referenced by TABLE_STYLE, DETAIL_STYLE, plus the inline
# stylesheets in submit_panel and machines_panel.
TABLE_SELECTION_BG_TOP    = '#ff9f30'
TABLE_SELECTION_BG_BOTTOM = '#d7801a'
_TABLE_SEL_GRADIENT = (
    'qlineargradient(x1:0,y1:0,x2:0,y2:1,'
    'stop:0 ' + TABLE_SELECTION_BG_TOP + ','
    'stop:1 ' + TABLE_SELECTION_BG_BOTTOM + ')'
)
TABLE_SELECTION_RULE = (
    # `outline:0` removes Qt's dotted focus rect that otherwise sits on
    # top of the current cell and looks like a darker overlay inside the
    # selected row. The :focus / :active variants pin the same gradient
    # so the current cell doesn't tint differently from its siblings.
    # `color:#1e1e1e` matches the Nuke palette HighlightedText so that
    # the selected-row text reads dark on the orange gradient - the
    # `QTableWidget::item{color}` rule below would otherwise also win on
    # selected items, leaving them light grey on orange.
    'QTableWidget{outline:0;}'
    'QTableWidget::item:selected{background:' + _TABLE_SEL_GRADIENT +
    ';color:#1e1e1e;}'
    'QTableWidget::item:selected:focus{background:' + _TABLE_SEL_GRADIENT +
    ';color:#1e1e1e;}'
    'QTableWidget::item:selected:active{background:' + _TABLE_SEL_GRADIENT +
    ';color:#1e1e1e;}'
)

TABLE_STYLE = (
    'QTableWidget{background:#252525;color:#ccc;gridline-color:#333;'
    'alternate-background-color:#2a2a2a;border:1px solid #3a3a3a;}'
    # Explicit item color: Qt 5.12 (Nuke 13) ignores the QTableWidget
    # `color` cascade for items, rendering text near-black. Qt 5.15+
    # propagates correctly so this rule is a no-op there.
    'QTableWidget::item{color:#ccc;}'
    'QHeaderView::section{background:#1e1e1e;color:#aaa;padding:4px;'
    'font-weight:bold;border:none;border-right:1px solid #333;}'
    + TABLE_SELECTION_RULE
)

DETAIL_STYLE = (
    'QTableWidget{background:#1e1e1e;color:#bbb;gridline-color:#2a2a2a;'
    'alternate-background-color:#232323;border:none;}'
    # Qt 5.12 (Nuke 13) doesn't cascade `color` to items - explicit rule.
    'QTableWidget::item{color:#bbb;}'
    'QHeaderView::section{background:#181818;color:#888;padding:3px;'
    'font-size:11px;font-weight:bold;border:none;'
    'border-right:1px solid #2a2a2a;}'
    + TABLE_SELECTION_RULE
)

TAB_STYLE = (
    'QTabWidget::pane{border:none;}'
    'QTabBar::tab{background:#1a1a1a;color:#888;padding:4px 12px;'
    'border:1px solid #333;border-bottom:none;border-radius:3px 3px 0 0;}'
    'QTabBar::tab:selected{background:#252525;color:#ccc;}'
)


# Search / filter field (Library toolbar, MyJobs history).
SEARCH_FIELD_STYLE = (
    'QLineEdit{background:#1e1e1e;color:#ccc;border:1px solid #444;'
    'border-radius:3px;padding:3px 8px;}'
)


# Top-tabs stylesheet shared by Settings + Render Manager dialogs.
TOP_TABS_STYLE_BASE = (
    'QTabBar::tab{background:#1e1e1e;color:#888;font-weight:bold;'
    'padding:6px 16px;border:1px solid #333;border-bottom:none;'
    'border-radius:3px 3px 0 0;margin-right:2px;}'
    'QTabBar::tab:selected{background:#2a2a2a;color:#ccc;font-weight:bold;}'
)


def _make_fit_tab_bar(parent, h_padding, bold):
    """QTabBar that widens each tab to its full label width. Built lazily
    to avoid importing QtWidgets at module scope."""
    from Nukomfy.utils.qt_compat import QtGui, QtWidgets

    class _FitTabBar(QtWidgets.QTabBar):
        def tabSizeHint(self, index):
            sz = super(_FitTabBar, self).tabSizeHint(index)
            f = QtGui.QFont(self.font())
            if bold:
                f.setBold(True)
            fm = QtGui.QFontMetrics(f)
            text = self.tabText(index)
            try:
                w_text = fm.horizontalAdvance(text)
            except AttributeError:
                w_text = fm.width(text)
            # Stylesheet horizontal padding (each side) + fixed slack for
            # the 1px border and the style's internal tab chrome. Tabs
            # with setExpanding(False) are sized to exactly this hint, so
            # a tight reserve clips the label; expanding tabs just gain
            # harmless breathing room.
            w_total = w_text + 2 * h_padding + 16
            if w_total > sz.width():
                sz.setWidth(w_total)
            return sz

    return _FitTabBar(parent)


def apply_tab_fit(tab_widget, h_padding, bold=False):
    """Workaround for Qt 5.x (Nuke 13-15) where the QTabBar sizeHint is
    measured against the regular weight and under-counts the stylesheet
    padding, so the label clips. Replace the default
    tabBar with one that re-measures the label and reserves the
    stylesheet padding. Per-tab widths so short labels stay short.
    No-op on Qt 6+ (Nuke 16+) where sizeHint is correct.

    `h_padding` must match the stylesheet's horizontal `padding`; pass
    `bold=True` when the stylesheet uses `font-weight:bold`.

    Must be called BEFORE `addTab()` so the custom tabBar is in place
    when Qt computes sizes for the first time.
    """
    from Nukomfy.utils.qt_compat import QtCore
    try:
        qt_major = int(QtCore.qVersion().split('.')[0])
    except Exception:
        return  # cannot determine Qt version; skip the cosmetic workaround
    if qt_major < 6:
        tab_widget.setTabBar(_make_fit_tab_bar(tab_widget, h_padding, bold))


# QPushButton family stylesheets.
# To override a local semantic color, ALWAYS use
# 'QPushButton:!disabled{color:#X;}' so the :disabled rule from the base
# constant is preserved.

BUTTON_STYLE_TOOLBAR = (
    'QPushButton{background:#1e1e1e;color:#888;border:1px solid #444;'
    'border-radius:3px;font-size:11px;padding:0 6px;}'
    'QPushButton:hover:!disabled{color:#ccc;border-color:#666;}'
    'QPushButton:pressed:!disabled{background:#151515;}'
    'QPushButton:checked{background:#3a2a10;color:' + ACCENT_GOLD + ';'
    'border-color:' + ACCENT_GOLD + ';}'
    'QPushButton:disabled{color:#555;border-color:#333;}'
    # Mirror the same look on QToolButton - used for split/dropdown
    # toolbar buttons (e.g. the Knobs tab "Add ▾" with menu).
    'QToolButton{background:#1e1e1e;color:#888;border:1px solid #444;'
    'border-radius:3px;font-size:11px;padding:0 6px;}'
    'QToolButton:hover:!disabled{color:#ccc;border-color:#666;}'
    'QToolButton:pressed:!disabled{background:#151515;}'
    'QToolButton:disabled{color:#555;border-color:#333;}'
)

BUTTON_STYLE_CELL_ACTION = (
    'QPushButton{background:#2a2a2a;color:#aaa;border:1px solid #444;'
    'border-radius:2px;}'
    'QPushButton:hover:!disabled{background:#3a3a3a;color:#ccc;'
    'border-color:#666;}'
    'QPushButton:pressed:!disabled{background:#1e1e1e;}'
    'QPushButton:disabled{color:#555;background:#252525;'
    'border:1px solid #333;}'
)


def cell_action_colored(fg, hover_fg, hover_bg):
    """QSS for a cell-action button tinted to a status color: the base
    BUTTON_STYLE_CELL_ACTION plus a colored idle glyph and a hover that
    washes the background and brightens the glyph."""
    return (
        BUTTON_STYLE_CELL_ACTION
        + 'QPushButton:!disabled{color:' + fg + ';}'
        + 'QPushButton:hover:!disabled{background:' + hover_bg
        + ';color:' + hover_fg + ';}')


def cell_toolbar_icon(hover_color):
    """QSS for a compact icon button in a table action cell: dark base,
    glyph and border tinting to `hover_color` on hover. Unlike
    cell_action_colored the hover keeps the dark background instead of
    washing it."""
    return (
        BUTTON_STYLE_CELL_ACTION
        + 'QPushButton{background:#1a1a1a;color:#888;'
        + 'border:1px solid #333;border-radius:3px;}'
        + 'QPushButton:hover:!disabled{background:#1a1a1a;'
        + 'color:' + hover_color + ';border-color:#666;}')


def clamp_int_field(line_edit, min_v, max_v, fallback):
    """Read int from a QLineEdit, clamp to [min_v, max_v], rewrite the field
    so the UI reflects the corrected value, and return it.

    Used as the imperative read step before persisting (on save click,
    or as the body of an `attach_int_clamp` filter on focus-out).
    """
    text = line_edit.text().strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        value = fallback
    value = max(min_v, min(max_v, value))
    line_edit.setText(str(value))
    return value


# Lazy import to keep this module Qt-free at import time when possible.
def attach_int_clamp(line_edit, min_v, max_v, fallback, on_commit=None):
    """Make `line_edit` auto-clamp to [min_v, max_v] on focus-out.

    Why an event filter and not `editingFinished`: Qt's `QIntValidator`
    classifies out-of-range typing as Intermediate (e.g. `5` while
    min=15, since the user might still type `50`). On Intermediate,
    `editingFinished` is NOT emitted on focus-out, so the field would
    silently keep `5`. Intercepting `FocusOut` directly bypasses the
    validator state and always commits a clamped value.

    `fallback` is used when the field is empty or non-numeric.
    `on_commit(value)` is called after every clamp (use it to persist).
    """
    from Nukomfy.utils.qt_compat import QtCore

    class _Filter(QtCore.QObject):
        def eventFilter(self, obj, event):
            if event.type() == QtCore.QEvent.FocusOut:
                value = clamp_int_field(line_edit, min_v, max_v, fallback)
                if on_commit is not None:
                    try:
                        on_commit(value)
                    except Exception:
                        pass
            return False  # let the event through

    flt = _Filter(line_edit)
    line_edit.installEventFilter(flt)
    # Keep a reference so the filter isn't garbage-collected.
    line_edit._clamp_filter = flt
    return flt
