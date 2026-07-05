"""Dialog to import or edit a ComfyUI workflow in the personal library."""

import datetime
import json
import logging
import os
import shutil
import uuid

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui import _dialogs

from Nukomfy.core.settings import settings
from Nukomfy.utils import fs_safe
from Nukomfy.gui.ui_state import center_on_screen, fit_to_screen
from Nukomfy.gui.workers import stop_worker
from Nukomfy.workflows.workflow_loader import WORKFLOW_JSON, METADATA_JSON
from Nukomfy.gui.icons import (
    icon_font, material_icon, set_press_icon,
    ARROW_UPWARD, ARROW_DOWNWARD,
    ADD, REMOVE, REFRESH,
    CLOUD_DOWNLOAD, SETTINGS_BACKUP_RESTORE,
    ADD_PHOTO_ALTERNATE,
)
from Nukomfy.gui._theme import (
    BUTTON_STYLE_TOOLBAR, LINK_FG, ERROR_COLOR,
    apply_nukomfy_palette, apply_tab_fit, apply_window_chrome,
)
from Nukomfy.gui._inline_messages import make_warning_banner, make_error_banner
from Nukomfy.gui._splitter import DottedSplitter
from Nukomfy.gui._no_wheel import NoWheelComboBox
from Nukomfy.gui._fields import NukomfyLineEdit, NukomfyPlainTextEdit
from Nukomfy.gui import _focus_drop

from Nukomfy.gui.add_workflow_parser import (
    _ParamWorker,
    _UNSUPPORTED_WORKFLOW_SENTINEL,
    _NUKOMFY_SUITE_NOT_INSTALLED_SENTINEL,
    _NO_NUKOMFY_WRITE_NODE_SENTINEL,
    _NO_NUKOMFY_WRITE_OUTPUT_SENTINEL,
    _classify_workflow_json,
    _validate_nk_template_write_nodes,
    _write_templates_dir,
    _AUTO_FILE_PATH_NODES,
    _TEMPLATE_IMAGE_FORMATS,
    _number_duplicate_labels,
    _hash_workflow_json_bytes,
)
from Nukomfy.gui.add_workflow_tables import (
    _InputsTable, _OutputsTable, _KnobsTable, _TableReorderFrame,
)

_log = logging.getLogger(__name__)


# Max side (px) of the lazy logo/preview hover popups.
_HOVER_POPUP_MAX_SIDE = 512


# QValidator.Acceptable moved under the State enum in PySide6's strict
# enum mode; resolve it once so the custom validator works on both bindings.
try:
    _VALIDATOR_ACCEPTABLE = QtGui.QValidator.Acceptable
except AttributeError:
    _VALIDATOR_ACCEPTABLE = QtGui.QValidator.State.Acceptable

_WORKFLOW_ALIAS_PLACEHOLDER = 'Optional alias…'

_WORKFLOW_ALIAS_TOOLTIP = (
    'An optional alias for this workflow.\n\n'
    'When set, it becomes the gizmo node name and\n'
    'can be used in the output path with the\n'
    '{workflow_alias} token. Leave empty to fall\n'
    'back to the workflow name.\n\n'
    'The Nukomfy_ prefix is added automatically\n'
    'when enabled in Settings.')

_WORD_WRAP_TOOLTIP = (
    'Wraps the Description and Extra Info text\n'
    'to fit the panel width.\n\n'
    'Turn it off to show the text exactly as\n'
    'written, with line breaks only where you\n'
    'place them. This prevents long text from\n'
    'being clipped at the bottom of a narrow\n'
    'floating panel.')


# Preview-thumbnail styles, shared by the primary and second-preview
# swatches (placeholder = no image loaded; image = a pixmap is shown).
_PREV_THUMB_PLACEHOLDER_STYLE = 'background:#2b2b2b;color:#888;border:1px solid #333;'
_PREV_THUMB_IMAGE_STYLE = 'background:#1a1a1a;border:1px solid #333;'


class _WorkflowAliasValidator(QtGui.QValidator):
    """Constrain a field to a Nuke-safe gizmo name as the user types.

    Same character rule as gizmo_builder.sanitize_gizmo_chars: every
    character that is not alphanumeric (isalnum, Unicode) or '_' becomes
    '_', spaces included. The mapping is 1:1, so the cursor never moves.
    """
    def validate(self, text, pos):
        fixed = ''.join(
            ch if (ch.isalnum() or ch == '_') else '_' for ch in text)
        return (_VALIDATOR_ACCEPTABLE, fixed, pos)


def _contrast_text_color(color_int):
    """Return '#000' or '#fff' for best contrast over the given 0xRRGGBB00
    color, using the YIQ perceived-luminance formula Nuke uses to auto-pick
    the gizmo title text color over the tile_color."""
    r = (color_int >> 24) & 0xFF
    g = (color_int >> 16) & 0xFF
    b = (color_int >>  8) & 0xFF
    yiq = (r * 299 + g * 587 + b * 114) / 1000.0
    return '#000' if yiq >= 128 else '#fff'


def _compose_over_checker(pix, cell=8,
                          dark='#3a3a3a', light='#555555'):
    """Composite a (possibly alpha-blended) QPixmap over a transparency
    checkerboard, so transparent regions are immediately recognizable as
    such (standard transparency checkerboard)."""
    w, h = pix.width(), pix.height()
    bg = QtGui.QPixmap(w, h)
    bg.fill(QtGui.QColor(dark))
    p = QtGui.QPainter(bg)
    light_color = QtGui.QColor(light)
    for y in range(0, h, cell):
        for x in range(0, w, cell):
            if ((x // cell) + (y // cell)) % 2 == 0:
                p.fillRect(x, y, cell, cell, light_color)
    p.drawPixmap(0, 0, pix)
    p.end()
    return bg


class _GrowableTextEdit(NukomfyPlainTextEdit):
    """QPlainTextEdit with a small `sizeHint` so it doesn't inflate the
    parent layout's natural-height calculation (the default 256x192 hint
    overshoots the splitter viewport and forces a scrollbar). Combined
    with stretch=1 on its container in the layout, the widget still
    grows vertically to absorb extra space when the dialog is resized
    taller - driven by the layout's stretch math rather than by its
    own sizeHint."""

    def sizeHint(self):
        return QtCore.QSize(256, 36)

    def minimumSizeHint(self):
        return QtCore.QSize(0, 36)


class _LogoSwatch(QtWidgets.QLabel):
    """QLabel that forwards Enter/Leave events to the parent dialog so a
    full-size hover popup can be shown. `kind` selects which dialog method
    pair is invoked ('logo' for the title logo swatch, 'preview' for the
    workflow preview thumbnail)."""
    def __init__(self, dialog, kind='logo'):
        super().__init__(dialog)
        self._dialog = dialog
        self._kind = kind
        self.setMouseTracking(True)

    def enterEvent(self, ev):
        try:
            if self._kind == 'preview':
                self._dialog._show_preview_hover_popup()
            elif self._kind == 'preview_b':
                self._dialog._show_preview_b_hover_popup()
            else:
                self._dialog._show_logo_hover_popup()
        except Exception:
            pass
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        try:
            if self._kind == 'preview':
                self._dialog._hide_preview_hover_popup()
            elif self._kind == 'preview_b':
                self._dialog._hide_preview_b_hover_popup()
            else:
                self._dialog._hide_logo_hover_popup()
        except Exception:
            pass
        super().leaveEvent(ev)


class _SwatchActionsFilter(QtCore.QObject):
    """Left-click invokes `pick`; right-click opens a context menu with
    pick + reset actions, Nuke-style. `gate` (callable) returns False to
    suppress all interaction (e.g. when the swatch is logically disabled).
    Labels can be callables for dynamic wording."""
    def __init__(self, widget, pick, reset, pick_label, reset_label, gate):
        super().__init__(widget)
        self._widget = widget
        self._pick = pick
        self._reset = reset
        self._pick_label = pick_label
        self._reset_label = reset_label
        self._gate = gate

    @staticmethod
    def _resolve(label):
        return label() if callable(label) else label

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.MouseButtonPress:
            return False
        if self._gate is not None and not self._gate():
            return False
        if ev.button() == QtCore.Qt.LeftButton:
            self._pick()
            return True
        if ev.button() == QtCore.Qt.RightButton:
            menu = QtWidgets.QMenu(self._widget)
            menu.addAction(self._resolve(self._pick_label), self._pick)
            menu.addAction(self._resolve(self._reset_label), self._reset)
            menu.exec_(ev.globalPos())
            return True
        return False


def _install_swatch_actions(widget, pick, reset,
                            pick_label='Open color picker',
                            reset_label='Set color to default',
                            gate=None):
    """Left-click on swatch = pick; right-click = context menu with
    pick + reset actions (Nuke-style)."""
    widget.setCursor(QtCore.Qt.PointingHandCursor)
    f = _SwatchActionsFilter(
        widget, pick, reset, pick_label, reset_label, gate)
    widget.installEventFilter(f)
    widget._swatch_actions_filter = f


def _make_default_swatch_pixmap(w, h):
    """Nuke-style 'unset color' indicator: light grey rounded square with
    a darker diagonal stripe (top-right to bottom-left). Signals that
    the color is at Nuke default (no override applied), matching Nuke's
    own color knobs."""
    px = QtGui.QPixmap(w, h)
    px.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(px)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1), 2, 2)
    p.fillPath(path, QtGui.QColor('#cbcbcb'))
    p.setClipPath(path)
    p.setPen(QtGui.QPen(QtGui.QColor('#5d5d5d'), 2))
    p.drawLine(w, 0, 0, h)
    p.setClipping(False)
    p.setPen(QtGui.QPen(QtGui.QColor('#555'), 1))
    p.drawPath(path)
    p.end()
    return px


def _swatch_style(obj_name, hex_color, text_color):
    """Stylesheet for a color-swatch QLabel: the swatch shows *hex_color*,
    its tooltip previews the same color with contrasting text."""
    return (
        'QLabel#{}{{background:{};border:1px solid #888;'
        'border-radius:2px;}}'
        'QToolTip{{background:{};color:{};'
        'border:1px solid #555;padding:4px;}}'.format(
            obj_name, hex_color, hex_color, text_color))


# ---------------------------------------------------------------------------
# Add Workflow Dialog
# ---------------------------------------------------------------------------
def _same_path(a, b):
    """True when the two paths refer to the same physical location.

    Case-insensitive filesystems (Windows, default macOS) treat paths
    differing only in case as the same file/folder - copying onto it
    raises shutil.SameFileError. normcase covers Windows; the samefile
    inode check covers macOS, where Python's normcase is a no-op but the
    filesystem still matches case-insensitively. samefile needs both
    paths to exist - a missing one means a genuinely different target."""
    if (os.path.normcase(os.path.normpath(a))
            == os.path.normcase(os.path.normpath(b))):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _existing_workflow_name_in(folder):
    """Name of the workflow living in `folder`, or None if it holds none.

    A folder counts as a workflow's home when it carries either Nukomfy
    marker file. The display name comes from metadata.json when readable,
    else the folder basename."""
    if not (os.path.isfile(os.path.join(folder, WORKFLOW_JSON))
            or os.path.isfile(os.path.join(folder, METADATA_JSON))):
        return None
    name = os.path.basename(os.path.normpath(folder))
    try:
        with open(os.path.join(folder, METADATA_JSON), 'r',
                  encoding='utf-8') as f:
            name = json.load(f).get('name') or name
    except Exception:
        pass
    return name


class AddWorkflowDialog(QtWidgets.QDialog):

    def __init__(self, parent=None, workflow_item=None):
        super().__init__(parent)
        self._edit_item = workflow_item
        is_edit = workflow_item is not None
        title = 'Edit Workflow' if is_edit else 'Add Workflow'
        if is_edit and workflow_item.workflow_id:
            title += ' (ID: {})'.format(workflow_item.workflow_id)
        self.setWindowTitle(title)
        self.setMinimumSize(450, 830)
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowTitleHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowMaximizeButtonHint
            | QtCore.Qt.WindowCloseButtonHint)
        apply_window_chrome(self)
        # Owns the parameter tables' row selection: clicking empty table
        # space, a tab header, or any non-reorder widget deselects (and
        # refreshes the toolbar); column-header presses and the reorder
        # toolbar preserve the row.
        _focus_drop.install(
            self,
            on_cleared=self._update_reorder_state,
            manage_row_selection=True)
        self._json_path    = None
        self._json_error = None
        self._gizmo_color  = 0
        self._title_color = 0           # 0 == linked (use Gizmo Color)
        self._title_color_linked = True
        self._title_node_color = 0      # 0 == default Nuke (no note_font_color)
        self._title_mode = 'use_gizmo_color'
        self._title_logo_path_pending = None
        self._title_logo_clear_pending = False
        self._title_logo_full_pixmap = None
        self._title_logo_popup = None
        self._preview_full_pixmap = None
        self._preview_popup = None
        self._preview_popup_label = None
        self._preview_b_full_pixmap = None
        self._preview_b_popup = None
        self._preview_b_popup_label = None
        self._param_worker = None
        # Snapshot-based Workflow Editor state. The snapshot is the
        # server-authoritative widget definition set from the last Sync,
        # overrides the user delta layered on top, widget_order the UI
        # row layout, v3_user_edits the per-option sub-value cache.
        self._snapshot = None
        self._overrides = {}
        self._widget_order = []
        self._v3_user_edits = {}
        # True once a Save attempt has marked I/O Mode / Write Template errors,
        # so combo edits live-refresh the red tab dot until everything's valid.
        self._validation_active = False
        # Staged Add-mode workflow templates: filename -> picked source path.
        # Held in memory until Save writes them into the workflow folder (the
        # folder doesn't exist yet for a workflow that's never been saved).
        self._pending_templates = {}
        self._build_ui()
        # No per-dialog geometry persistence: always opens at the default
        # size and the splitter sizes from _build_ui; the dialog does not
        # remember its last size between sessions.
        self.resize(1500, 800)
        # On a screen smaller than full HD, scale the default down to keep
        # the same screen fraction (the body's scroll areas cover the reduced
        # height) and re-balance the splitter so the left pane keeps its share
        # instead of being squeezed by the parameter pane.
        scale = fit_to_screen(self)
        if scale < 1.0:
            self._splitter.setSizes([int(700 * scale), int(750 * scale)])
        # Born centered on the monitor that holds the parent (Library) panel.
        center_on_screen(self)
        if is_edit:
            self._prefill(workflow_item)

    def done(self, result):
        # Geometry is intentionally NOT persisted - the dialog always
        # opens at its default size.
        self._stop_param_worker()
        super().done(result)

    def _stop_param_worker(self):
        self._param_worker = stop_worker(self._param_worker)

    def _update_workflow_alias_placeholder(self, *args):
        """Preview the sanitized name in the Workflow Alias placeholder.

        Shows the workflow name run through the same character rule the
        field enforces (the value used when the field is left empty), or
        the static hint when there is no workflow name yet. The Nukomfy_
        prefix is a global setting applied at build, not shown here. The
        user's own text, when present, hides the placeholder.
        """
        from Nukomfy.gizmos.gizmo_builder import sanitize_gizmo_chars
        wf = self.name_edit.text().strip()
        self.workflow_alias_edit.setPlaceholderText(
            sanitize_gizmo_chars(wf) if wf else _WORKFLOW_ALIAS_PLACEHOLDER)

    def _prefill(self, item):
        """Pre-populate all fields from an existing WorkflowItem."""
        self.name_edit.setText(item.name)
        self.workflow_alias_edit.setText(item.workflow_alias)
        self.desc_edit.setPlainText(item.description)
        self.author_edit.setText(item.author)
        self.usage_edit.setPlainText(item.usage)
        if item.version:
            parts = item.version.split('.')
            try:
                self.ver_major.setText(str(int(parts[0])) if len(parts) > 0 else '1')
                self.ver_minor.setText(str(int(parts[1])) if len(parts) > 1 else '0')
                self.ver_patch.setText(str(int(parts[2])) if len(parts) > 2 else '0')
            except (ValueError, IndexError):
                pass
        self.tags_cat_edit.setText(', '.join(item.tags_category))
        self.tags_mod_edit.setText(', '.join(item.tags_models))

        if os.path.isfile(item.workflow_path):
            self._json_path = item.workflow_path
            self.json_edit.setText(os.path.normpath(item.workflow_path))
            _, self._json_error = self._validate_ui_workflow_file(item.workflow_path)
        self._refresh_status_label()
        # Snapshot-based load: read the persisted snapshot + overrides +
        # widget_order + v3_user_edits and render the editor tables.
        if item.snapshot:
            self._snapshot = item.snapshot
            self._overrides = dict(item.overrides)
            self._widget_order = list(item.widget_order)
            self._v3_user_edits = dict(item.v3_user_edits)
            self._render_tables_from_state()
            self._btn_reset_defaults.setVisible(True)
            # Snapshot is now loaded: re-run the banner pass so the hash
            # mismatch check fires on reopen (the first pass above ran
            # before _snapshot was set and skipped the hash compare).
            self._refresh_status_label()

        self._gizmo_color = item.gizmo_color
        self._update_color_swatch()

        opts = item.gizmo_options
        self._opt_title.setChecked(opts.get('title', True))
        self._opt_versioning.setChecked(opts.get('versioning', True))
        self._opt_author.setChecked(opts.get('author', True))
        self._opt_desc.setChecked(opts.get('description', True))
        self._opt_usage.setChecked(opts.get('usage', True))
        self._opt_output_preview.setChecked(opts.get('output_preview', True))
        self._opt_color_reads.setChecked(opts.get('color_reads', True))
        self._opt_word_wrap.setChecked(opts.get('word_wrap', False))

        title_node_hex = opts.get('title_node_color')
        if (title_node_hex and isinstance(title_node_hex, str)
                and title_node_hex.startswith('#')):
            try:
                self._title_node_color = (
                    int(title_node_hex.lstrip('#'), 16) << 8) | 0x01
            except ValueError:
                self._title_node_color = 0
        else:
            self._title_node_color = 0
        self._update_title_node_swatch()

        # Title style + Title Color
        title_mode = opts.get('title_mode', 'use_gizmo_color')
        if title_mode not in ('default', 'use_gizmo_color', 'use_custom_logo'):
            title_mode = 'use_gizmo_color'
        self._title_mode = title_mode
        for i in range(self._title_mode_combo.count()):
            if self._title_mode_combo.itemData(i) == title_mode:
                self._title_mode_combo.blockSignals(True)
                self._title_mode_combo.setCurrentIndex(i)
                self._title_mode_combo.blockSignals(False)
                break
        title_color_hex = opts.get('title_color')
        if (title_color_hex and isinstance(title_color_hex, str)
                and title_color_hex.startswith('#')):
            try:
                # Alpha-byte marker on load so pure black survives the
                # "0 = unset" check (hex storage strips the alpha).
                self._title_color = (
                    int(title_color_hex.lstrip('#'), 16) << 8) | 0x01
                self._title_color_linked = False
            except ValueError:
                self._title_color = 0
                self._title_color_linked = True
        else:
            self._title_color = 0
            self._title_color_linked = True
        self._refresh_title_widgets()

        if item.preview_path and os.path.isfile(item.preview_path):
            # Only accept the existing preview if it's a loadable image -
            # a corrupted file on disk would otherwise leave `prev_edit`
            # populated and silently re-copy the broken file at save.
            px = QtGui.QPixmap(item.preview_path)
            if not px.isNull():
                self.prev_edit.setText(os.path.normpath(item.preview_path))
                self._preview_full_pixmap = px
                self._show_prev_thumb_image(
                    px.scaled(72, 72, QtCore.Qt.KeepAspectRatio,
                              QtCore.Qt.SmoothTransformation))
            else:
                _log.warning(
                    'workflow preview file is not a valid image, '
                    'ignoring: %s', item.preview_path)

        if item.preview_path_b and os.path.isfile(item.preview_path_b):
            pxb = QtGui.QPixmap(item.preview_path_b)
            if not pxb.isNull():
                self.prev_edit_b.setText(os.path.normpath(item.preview_path_b))
                self._preview_b_full_pixmap = pxb
                self._show_prev_b_thumb_image(
                    pxb.scaled(72, 72, QtCore.Qt.KeepAspectRatio,
                               QtCore.Qt.SmoothTransformation))
            else:
                _log.warning(
                    'workflow second preview file is not a valid image, '
                    'ignoring: %s', item.preview_path_b)

        if getattr(item, 'id_conflict', False):
            self._conflict_banner.setVisible(True)
            self.name_edit.setToolTip('Workflow ID: ' + (item.workflow_id or ''))

    def _on_regenerate_id(self):
        """Assign a fresh workflow_id to the item under edit, persist it."""
        if not self._edit_item:
            return
        import Nukomfy.workflows.workflow_loader as workflow_loader
        new_id = uuid.uuid4().hex[:12]
        if workflow_loader.save_workflow_id(self._edit_item, new_id):
            self._edit_item._id_conflict = False
            self._conflict_banner.setVisible(False)
            self.setWindowTitle(
                'Edit Workflow (ID: {})'.format(new_id))
            self.name_edit.setToolTip('Workflow ID: ' + new_id)
        else:
            _dialogs.warn(
                self, 'Regenerate ID failed',
                'Could not write the new workflow ID to the workflow '
                'metadata file. Check file permissions and retry.')

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        # spacing=0 + explicit addSpacing(10) before Save/Cancel - keeps
        # the splitter flush with the dialog top margin and lets the
        # button row sit at the bottom with the same 10px gap used
        # elsewhere in the plugin.
        root.setSpacing(0)
        root.setContentsMargins(14, 14, 14, 14)

        # --- Workflow JSON file ---
        grp_json = QtWidgets.QGroupBox('Workflow JSON File')
        gl = QtWidgets.QVBoxLayout(grp_json)
        gl.setSpacing(4)

        # Primary picker (UI workflow)
        ui_row = QtWidgets.QHBoxLayout()
        self.json_edit = NukomfyLineEdit()
        self.json_edit.setPlaceholderText('Select a .json or .app.json file…')
        self.json_edit.setReadOnly(True)
        btn_json = QtWidgets.QPushButton('Browse…')
        btn_json.setFixedWidth(80)
        btn_json.clicked.connect(self._browse_json)
        ui_row.addWidget(self.json_edit)
        ui_row.addWidget(btn_json)
        gl.addLayout(ui_row)

        # Status banner: error message about the loaded JSON workflow file.
        # Uses the shared `make_error_banner` factory for visual consistency
        # with other inline error messages across the plugin. Hidden by
        # default so the groupbox stays compact around the file picker row;
        # the top scroll area reflows to absorb the height delta when the
        # banner is shown/hidden.
        self._status_banner = make_error_banner(parent=self, font_size=11)
        self._status_banner.layout().setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self._status_banner)
        # Hash mismatch warning: source workflow.json bytes changed since
        # the last Sync. Mutually exclusive with `_status_banner` (a file
        # with a JSON error is not hashed). The dialog grows by at most
        # one banner height (~30px) regardless of which one is active.
        self._hash_warn_banner = make_warning_banner(parent=self, font_size=11)
        self._hash_warn_banner.layout().setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self._hash_warn_banner)
        # --- Info ---
        grp_info = QtWidgets.QGroupBox('Information')
        fl = QtWidgets.QFormLayout(grp_info)
        fl.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)
        self.name_edit = NukomfyLineEdit()
        self.name_edit.setPlaceholderText('Workflow name…')
        # Optional alias for this workflow; empty -> the workflow name is
        # used. When set it becomes the gizmo node name and feeds the
        # {workflow_alias} output-path token. The empty-field placeholder
        # previews the sanitized name derived from the workflow name,
        # refreshed live as that name is typed. The global Nukomfy_ prefix
        # is applied at build, not shown here - it is a global setting, not
        # a per-workflow choice.
        self.workflow_alias_edit = NukomfyLineEdit()
        self.workflow_alias_edit.setValidator(
            _WorkflowAliasValidator(self.workflow_alias_edit))
        self.workflow_alias_edit.setToolTip(_WORKFLOW_ALIAS_TOOLTIP)
        self.workflow_alias_edit.setPlaceholderText(_WORKFLOW_ALIAS_PLACEHOLDER)
        self.name_edit.textChanged.connect(self._update_workflow_alias_placeholder)
        self.desc_edit = NukomfyPlainTextEdit()
        self.desc_edit.setPlaceholderText('Short description…')
        self.desc_edit.setFixedHeight(52)
        self.tags_cat_edit = NukomfyLineEdit()
        self.tags_cat_edit.setPlaceholderText('Upscaling, Image to Video, …  (comma-separated)')
        self.tags_mod_edit = NukomfyLineEdit()
        self.tags_mod_edit.setPlaceholderText('Z-Image, Wan2.2, …  (comma-separated)')
        # Version (v major.minor.patch)
        ver_w = QtWidgets.QWidget()
        ver_l = QtWidgets.QHBoxLayout(ver_w)
        ver_l.setContentsMargins(0, 0, 0, 0)
        ver_l.setSpacing(2)
        ver_prefix = QtWidgets.QLabel('v')
        ver_l.addWidget(ver_prefix)
        self.ver_major = NukomfyLineEdit('1')
        self.ver_minor = NukomfyLineEdit('0')
        self.ver_patch = NukomfyLineEdit('0')
        for le in (self.ver_major, self.ver_minor, self.ver_patch):
            le.setValidator(QtGui.QIntValidator(0, 999))
            le.setFixedWidth(30)
            le.setAlignment(QtCore.Qt.AlignCenter)
        ver_l.addWidget(self.ver_major)
        ver_l.addWidget(QtWidgets.QLabel('.'))
        ver_l.addWidget(self.ver_minor)
        ver_l.addWidget(QtWidgets.QLabel('.'))
        ver_l.addWidget(self.ver_patch)
        ver_l.addStretch()

        self.author_edit = NukomfyLineEdit()
        self.author_edit.setPlaceholderText('Workflow author…')
        # Warning banner (orange) for duplicate workflow ID - built via
        # the shared `make_warning_banner` factory so the look matches
        # other inline warnings across the plugin. Font size 11 + tight
        # vertical margins keep the banner compact and visually aligned
        # with the inline Regenerate ID button.
        self._conflict_banner = make_warning_banner(parent=self, font_size=11)
        self._conflict_banner.set_message(
            'This workflow ID is shared with another workflow.')
        self._conflict_banner.layout().setContentsMargins(0, 0, 0, 0)
        self._conflict_banner.layout().setSpacing(6)
        self._regen_id_btn = QtWidgets.QPushButton('Regenerate ID')
        set_press_icon(self._regen_id_btn, REFRESH)
        self._regen_id_btn.setFixedHeight(20)
        self._regen_id_btn.setStyleSheet(
            'QPushButton{padding:1px 8px;font-size:11px;}')
        self._regen_id_btn.setToolTip(
            'Assign a fresh unique workflow ID and save it to the workflow metadata file')
        self._regen_id_btn.clicked.connect(self._on_regenerate_id)
        self._conflict_banner.layout().addWidget(self._regen_id_btn, 0)
        self._conflict_banner.setVisible(False)
        fl.addRow('', self._conflict_banner)
        fl.addRow('Name:', self.name_edit)
        fl.addRow('Alias:', self.workflow_alias_edit)
        fl.addRow('Description:', self.desc_edit)
        fl.addRow('Version:', ver_w)
        fl.addRow('Author:', self.author_edit)
        fl.addRow('Category tags:', self.tags_cat_edit)
        fl.addRow('Model tags:', self.tags_mod_edit)
        # --- Preview images (two columns: primary + optional second) ---
        # Static images are re-encoded to 512x512 at save time so any
        # oversized PNG/JPG/WEBP is automatically capped. GIFs keep their
        # original dimensions (no multi-frame encoder available). A second
        # preview turns the Library card into a before/after comparison
        # slider; one image alone behaves exactly as before. Split into two
        # equal columns to keep the same overall width.
        def _make_preview_col(title, tip, kind, browse_cb, reset_cb):
            grp = QtWidgets.QGroupBox(title)
            grp.setToolTip(tip)
            col = QtWidgets.QHBoxLayout(grp)
            thumb = _LogoSwatch(self, kind=kind)
            thumb.setFixedSize(72, 72)
            thumb.setAlignment(QtCore.Qt.AlignCenter)
            # Formats are already named in the box title; placeholder
            # repeats less and matches the rest of the UI.
            edit = NukomfyLineEdit()
            edit.setPlaceholderText('Select Preview Image…')
            edit.setReadOnly(True)
            btn_browse = QtWidgets.QPushButton('Browse…')
            btn_browse.setFixedHeight(24)
            btn_browse.clicked.connect(browse_cb)
            # Reset mirrors the gizmo title logo Reset (confirm popup +
            # fs_safe.safe_delete_file basename gate). Always visible.
            btn_reset = QtWidgets.QPushButton('Reset')
            btn_reset.setFixedHeight(24)
            btn_reset.setToolTip(
                'Delete the preview image file from the workflow folder')
            btn_reset.clicked.connect(reset_cb)
            btns = QtWidgets.QHBoxLayout()
            btns.setContentsMargins(0, 0, 0, 0)
            btns.addWidget(btn_browse)
            btns.addWidget(btn_reset)
            ctrls = QtWidgets.QVBoxLayout()
            ctrls.setContentsMargins(0, 0, 0, 0)
            ctrls.addWidget(edit)
            ctrls.addLayout(btns)
            col.addWidget(thumb)
            col.addLayout(ctrls)
            return grp, edit, thumb, btn_reset

        (grp_prev, self.prev_edit, self.prev_thumb,
         self._btn_prev_reset) = _make_preview_col(
            'Preview Image (square, max 512x512)',
            'Accepts GIF, PNG, JPEG or WEBP files.',
            'preview', self._browse_preview, self._preview_remove)
        self._show_prev_thumb_placeholder()

        (grp_prev_b, self.prev_edit_b, self.prev_thumb_b,
         self._btn_prev_b_reset) = _make_preview_col(
            'Second Preview (optional)',
            'Optional. A second image turns the Library card into a '
            'before/after comparison slider.',
            'preview_b', self._browse_preview_b, self._preview_remove_b)
        self._show_prev_b_thumb_placeholder()

        grp_prev_row = QtWidgets.QWidget()
        prev_row = QtWidgets.QHBoxLayout(grp_prev_row)
        prev_row.setContentsMargins(0, 0, 0, 0)
        prev_row.addWidget(grp_prev, 1)
        prev_row.addWidget(grp_prev_b, 1)
        # --- Usage notes ---
        grp_usage = QtWidgets.QGroupBox('Extra Info')
        grp_usage.setToolTip(
            'Shown in the gizmo Properties panel. Not shown in the Library.')
        ul = QtWidgets.QVBoxLayout(grp_usage)
        self.usage_edit = _GrowableTextEdit()
        self.usage_edit.setPlaceholderText(
            'Describe how to use this workflow, tips, recommended settings…')
        ul.addWidget(self.usage_edit)

        # --- Exposed parameters - 3 tabs ---
        grp_params = QtWidgets.QGroupBox('Exposed Parameters')
        prl = QtWidgets.QVBoxLayout(grp_params)

        # Top row: status label left, fetch button right
        params_top = QtWidgets.QHBoxLayout()
        self.params_lbl = QtWidgets.QLabel(
            'Select a workflow JSON, then click "Sync Parameters" to load.')
        self.params_lbl.setStyleSheet(
            'color:#777;font-size:11px;font-style:italic;')
        params_top.addWidget(self.params_lbl, 1)

        # Reset Defaults - restores server-fetched fields (Include, Gizmo
        # Label, Tooltip, Default Value on Parameters, V3 sub-options, row
        # order) to the fresh-from-Sync state. User-owned fields (Write
        # Template, I/O Mode) are left untouched. Hidden until tables
        # populated.
        self._btn_reset_defaults = QtWidgets.QPushButton('Reset to Defaults')
        set_press_icon(self._btn_reset_defaults, SETTINGS_BACKUP_RESTORE)
        self._btn_reset_defaults.setFixedHeight(22)
        self._btn_reset_defaults.setToolTip(
            'Reset server-fetched fields back to the workflow defaults:\n'
            '  \u2022 Include in Gizmo \u2192 workflow default '
            '(checked for explicitly exposed parameters, unchecked for '
            'auto-discovered Nuke parameters you never opted into)\n'
            '  \u2022 Gizmo Label \u2192 original parameter name\n'
            '  \u2022 Tooltip \u2192 workflow default\n'
            '  \u2022 Default Value (Parameters) \u2192 workflow JSON value\n'
            '  \u2022 Sub-options of dynamic combos for every value\n'
            '    (not just the current selection)\n'
            '  \u2022 Row order \u2192 workflow default\n'
            '    (manual reordering will be lost)\n\n'
            'Your local settings are preserved:\n'
            '  \u2022 Write Template, I/O Mode\n\n'
            'Bypassed/muted/disconnected nodes keep their disabled state.')
        self._btn_reset_defaults.clicked.connect(self._reset_defaults)
        self._btn_reset_defaults.setVisible(False)
        params_top.addWidget(self._btn_reset_defaults)

        self._btn_update = QtWidgets.QPushButton('Sync Parameters')
        set_press_icon(self._btn_update, CLOUD_DOWNLOAD)
        self._btn_update.setFixedHeight(22)
        self._btn_update.setToolTip(
            'Re-scan the workflow JSON and pull the current\n'
            'parameter definitions from the ComfyUI server.\n\n'
            'Rows you have edited are left untouched: your labels,\n'
            'default values, write templates, I/O modes and tooltips\n'
            'are preserved.\n\n'
            'Rows you never edited refresh to the current server\n'
            'values. New parameters are added at the bottom, and\n'
            'parameters removed from the workflow are dropped.\n\n'
            'Disabled until a valid workflow JSON is loaded above.')
        # Disabled by default - `_refresh_status_label` enables it once
        # a valid JSON is loaded. Without this, "Add Workflow" mode
        # (new, no item to edit) would render the button visibly
        # active before any JSON is selected.
        self._btn_update.setEnabled(False)
        self._btn_update.clicked.connect(self._update_params)
        params_top.addWidget(self._btn_update)
        prl.addLayout(params_top)

        self.params_tabs = QtWidgets.QTabWidget()
        self.params_tabs.setStyleSheet(
            'QTabWidget::pane{border-top:1px solid #444;}'
            'QTabBar::tab{background:#1e1e1e;color:#888;padding:4px 14px;'
            'border:1px solid #333;border-bottom:none;margin-right:2px;}'
            'QTabBar::tab:selected{background:#2a2a2a;color:#eee;}')
        self.params_tabs.setVisible(False)
        apply_tab_fit(self.params_tabs, 14)

        self.inputs_table  = _InputsTable()
        self.knobs_table   = _KnobsTable()
        self.outputs_table = _OutputsTable()
        self._inputs_tab  = _TableReorderFrame(self.inputs_table)
        self._outputs_tab = _TableReorderFrame(self.outputs_table)
        self.params_tabs.addTab(self._inputs_tab,   'Inputs')
        self.params_tabs.addTab(self.knobs_table,   'Parameters')
        self.params_tabs.addTab(self._outputs_tab,  'Outputs')

        # --- Unified toolbar (top-right corner of the tab bar) ---
        # Stays fixed when switching tabs. Up/Down dispatches to the active
        # tab's table; Add/Remove Separator is Parameters-only (greyed elsewhere).
        tb_widget = QtWidgets.QWidget()
        tb = QtWidgets.QHBoxLayout(tb_widget)
        tb.setContentsMargins(0, 0, 4, 2)
        tb.setSpacing(2)

        self._btn_tb_up = QtWidgets.QPushButton(ARROW_UPWARD)
        self._btn_tb_up.setFont(icon_font(14))
        self._btn_tb_up.setFixedSize(26, 22)
        self._btn_tb_up.setToolTip('Move selected row up')
        self._btn_tb_up.clicked.connect(self._tb_move_up)

        self._btn_tb_down = QtWidgets.QPushButton(ARROW_DOWNWARD)
        self._btn_tb_down.setFont(icon_font(14))
        self._btn_tb_down.setFixedSize(26, 22)
        self._btn_tb_down.setToolTip('Move selected row down')
        self._btn_tb_down.clicked.connect(self._tb_move_down)

        # Add ▾ - single QToolButton with dropdown menu containing
        # "Separator" and "Group" actions. Each action is enabled
        # independently in _update_reorder_state so the menu reflects
        # what's currently insertable.
        self._btn_tb_add = QtWidgets.QToolButton()
        self._btn_tb_add.setText('Add')
        self._btn_tb_add.setIcon(material_icon(ADD, '#888', 12))
        self._btn_tb_add.setToolButtonStyle(
            QtCore.Qt.ToolButtonTextBesideIcon)
        self._btn_tb_add.setPopupMode(
            QtWidgets.QToolButton.InstantPopup)
        self._btn_tb_add.setFixedHeight(22)
        self._btn_tb_add.setToolTip(
            'Insert a collapsible group, a text row, or a divider\n'
            'line inside the Model Parameters section. Requires a\n'
            'row selected in Model. The new entry is placed below it.')
        # Order matches Nuke's "Add knob" menu in the gizmo Properties:
        # Group -> Text -> Divider Line. Internally the divider role stays
        # 'separator' (schema), only the user-facing label is renamed
        # to match Nuke's terminology.
        add_menu = QtWidgets.QMenu(self._btn_tb_add)
        self._action_add_group = add_menu.addAction('Group')
        self._action_add_text = add_menu.addAction('Text')
        self._action_add_sep = add_menu.addAction('Divider Line')
        self._action_add_group.triggered.connect(self._tb_add_group)
        self._action_add_text.triggered.connect(self._tb_add_text)
        self._action_add_sep.triggered.connect(self._tb_add_sep)
        self._btn_tb_add.setMenu(add_menu)

        # Remove - single button, auto-detects whether the selected row
        # is a separator or a group marker. Enabled iff one of the two
        # remove operations can run.
        self._btn_tb_remove = QtWidgets.QPushButton('Remove')
        self._btn_tb_remove.setIcon(material_icon(REMOVE, '#888', 12))
        self._btn_tb_remove.setFixedHeight(22)
        self._btn_tb_remove.setToolTip(
            'Remove the selected divider line, group, or text row\n'
            '(auto-detects). Removing a Begin/End marker deletes\n'
            'the whole group. Parameters that were inside stay where\n'
            'they are.')
        self._btn_tb_remove.clicked.connect(self._tb_remove_selected)

        for btn in (self._btn_tb_up, self._btn_tb_down,
                    self._btn_tb_add, self._btn_tb_remove):
            btn.setStyleSheet(BUTTON_STYLE_TOOLBAR)
            # These buttons act on the currently selected row of the
            # active params tab. Tag them so _focus_drop preserves the
            # tables' selection when the click lands on the toolbar.
            btn.setProperty('_keep_selection', True)

        tb.addWidget(self._btn_tb_up)
        tb.addWidget(self._btn_tb_down)
        tb.addSpacing(10)
        tb.addWidget(self._btn_tb_add)
        tb.addWidget(self._btn_tb_remove)

        self.params_tabs.setCornerWidget(tb_widget, QtCore.Qt.TopRightCorner)
        self.params_tabs.currentChanged.connect(
            lambda _idx: self._update_reorder_state())
        # React to selection changes inside each table so the toolbar
        # enabled-state follows the current row/position.
        self.inputs_table.itemSelectionChanged.connect(
            self._update_reorder_state)
        self.inputs_table.templateManageRequested.connect(self._tpl_manage)
        self.inputs_table.validityMaybeChanged.connect(self._revalidate_after_fix)
        self.outputs_table.validityMaybeChanged.connect(self._revalidate_after_fix)
        self.knobs_table.table.itemSelectionChanged.connect(
            self._update_reorder_state)
        self.outputs_table.itemSelectionChanged.connect(
            self._update_reorder_state)
        self._update_reorder_state()

        prl.addWidget(self.params_tabs)

        _LABEL_STYLE = 'color:#aaa;font-size:11px;'
        _CHECKBOX_STYLE = 'color:#bbb;font-size:11px;'
        _SWATCH_TOOLTIP = (
            'Left-click: pick color\n'
            'Right-click: open menu'
        )
        _SWATCH_PX = 22

        def _build_color_swatch(object_name, tooltip=_SWATCH_TOOLTIP):
            sw = QtWidgets.QLabel()
            sw.setObjectName(object_name)
            sw.setFixedSize(_SWATCH_PX, _SWATCH_PX)
            sw.setAlignment(QtCore.Qt.AlignCenter)
            sw.setToolTip(tooltip)
            sw.setPixmap(_make_default_swatch_pixmap(_SWATCH_PX, _SWATCH_PX))
            return sw

        # --- Gizmo Appearance: DAG tile colors + Apply ---
        grp_opts = QtWidgets.QGroupBox('Gizmo Appearance')
        opts_lay = QtWidgets.QVBoxLayout(grp_opts)
        opts_lay.setSpacing(8)
        opts_lay.setContentsMargins(10, 10, 10, 10)

        # Row 1: DAG colors
        gizmo_color_lbl = QtWidgets.QLabel('Gizmo Color:')
        gizmo_color_lbl.setStyleSheet(_LABEL_STYLE)
        self._color_swatch = _build_color_swatch('nukomfyGizmoColorSwatch')
        _install_swatch_actions(
            self._color_swatch,
            pick=self._pick_color,
            reset=self._clear_color)

        title_node_lbl = QtWidgets.QLabel('Title Color:')
        title_node_lbl.setStyleSheet(_LABEL_STYLE)
        self._title_node_swatch = _build_color_swatch(
            'nukomfyTitleNodeColorSwatch')
        _install_swatch_actions(
            self._title_node_swatch,
            pick=self._pick_title_node_color,
            reset=self._reset_title_node_color)

        self._opt_color_reads = QtWidgets.QCheckBox(
            'Apply gizmo color to Read nodes')
        self._opt_color_reads.setChecked(True)
        self._opt_color_reads.setToolTip(
            'Use gizmo color as the tile color for Read nodes created by '
            'this gizmo and by MyJobs')
        self._opt_color_reads.setStyleSheet(_CHECKBOX_STYLE)

        # Row 2 widgets - constructed first so we can place them in the grid
        # alongside Row 1.
        style_lbl = QtWidgets.QLabel('Header style:')
        style_lbl.setStyleSheet(_LABEL_STYLE)
        self._title_mode_combo = NoWheelComboBox()
        self._title_mode_combo.addItem('Default', 'default')
        self._title_mode_combo.addItem('Use gizmo color', 'use_gizmo_color')
        self._title_mode_combo.addItem('Use custom logo', 'use_custom_logo')
        self._title_mode_combo.setCurrentIndex(1)
        self._title_mode_combo.setToolTip(
            'How the gizmo header is rendered in the node graph:\n'
            '\n'
            'Default - plain Nuke header, no tint or logo\n'
            'Use gizmo color - header tinted with the gizmo color\n'
            'Use custom logo - header shows a custom logo image')
        self._title_mode_combo.currentIndexChanged.connect(
            self._on_title_mode_changed)

        self._title_lbl = QtWidgets.QLabel('Header Color:')
        self._title_lbl.setStyleSheet(_LABEL_STYLE)
        self._title_swatch = _LogoSwatch(self)
        self._title_swatch.setObjectName('nukomfyTitleColorSwatch')
        self._title_swatch.setFixedSize(_SWATCH_PX, _SWATCH_PX)
        self._title_swatch.setAlignment(QtCore.Qt.AlignCenter)
        self._title_swatch.setPixmap(
            _make_default_swatch_pixmap(_SWATCH_PX, _SWATCH_PX))
        # Right-click menu labels switch with title_mode (color vs logo);
        # gate suppresses both clicks when the mode is 'default'.
        _install_swatch_actions(
            self._title_swatch,
            pick=self._title_pick_or_load,
            reset=self._title_reset,
            pick_label=lambda: ('Load image…'
                                if self._title_mode == 'use_custom_logo'
                                else 'Open color picker'),
            reset_label=lambda: ('Delete logo'
                                 if self._title_mode == 'use_custom_logo'
                                 else 'Set color to default'),
            gate=lambda: self._title_mode != 'default')

        # Row 1: DAG colors + Apply checkbox (HBox with explicit spacing
        # between the two label+swatch pairs).
        row_colors = QtWidgets.QHBoxLayout()
        row_colors.setSpacing(6)
        row_colors.addWidget(gizmo_color_lbl)
        row_colors.addWidget(self._color_swatch)
        row_colors.addSpacing(28)
        row_colors.addWidget(title_node_lbl)
        row_colors.addWidget(self._title_node_swatch)
        row_colors.addSpacing(36)
        row_colors.addWidget(self._opt_color_reads)
        row_colors.addStretch()
        opts_lay.addLayout(row_colors)

        # --- Gizmo Panel Appearance: header HTML inside properties panel ---
        grp_panel = QtWidgets.QGroupBox('Gizmo Panel Appearance')
        panel_lay = QtWidgets.QVBoxLayout(grp_panel)
        panel_lay.setSpacing(8)
        panel_lay.setContentsMargins(10, 10, 10, 10)

        # Header row: swatch + style combo + word-wrap toggle
        self._opt_word_wrap = QtWidgets.QCheckBox('Enable word wrap')
        self._opt_word_wrap.setChecked(False)
        self._opt_word_wrap.setStyleSheet(_CHECKBOX_STYLE)
        self._opt_word_wrap.setToolTip(_WORD_WRAP_TOOLTIP)
        row_header = QtWidgets.QHBoxLayout()
        row_header.setSpacing(6)
        row_header.addWidget(self._title_lbl)
        row_header.addWidget(self._title_swatch)
        row_header.addSpacing(28)
        row_header.addWidget(style_lbl)
        row_header.addWidget(self._title_mode_combo)
        row_header.addSpacing(28)
        row_header.addWidget(self._opt_word_wrap)
        row_header.addStretch()
        panel_lay.addLayout(row_header)

        # Show row
        show_lbl = QtWidgets.QLabel('Show in gizmo panel:')
        show_lbl.setStyleSheet(_LABEL_STYLE)
        self._opt_title      = QtWidgets.QCheckBox('Header')
        self._opt_versioning = QtWidgets.QCheckBox('Versioning')
        self._opt_author     = QtWidgets.QCheckBox('Author')
        self._opt_desc       = QtWidgets.QCheckBox('Description')
        self._opt_usage      = QtWidgets.QCheckBox('Extra Info')
        self._opt_output_preview = QtWidgets.QCheckBox('Output Preview')
        row_show = QtWidgets.QHBoxLayout()
        row_show.setSpacing(12)
        row_show.addWidget(show_lbl)
        for cb in (self._opt_title, self._opt_versioning, self._opt_author,
                   self._opt_desc, self._opt_usage, self._opt_output_preview):
            cb.setChecked(True)
            cb.setStyleSheet(_CHECKBOX_STYLE)
            row_show.addWidget(cb)
        row_show.addStretch()
        panel_lay.addLayout(row_show)

        # Initial Header swatch state: linked to Gizmo Color (which is 0).
        self._refresh_title_widgets()

        # --- Left pane (metadata + gizmo appearance) ---
        # All metadata groupboxes + grp_opts stack vertically in the left
        # pane of a horizontal splitter. The right pane holds grp_params.
        # If the left pane natural height exceeds the dialog, the wrapping
        # scroll area handles overflow (without distorting the right pane).
        left_pane_inner = QtWidgets.QWidget()
        left_lay = QtWidgets.QVBoxLayout(left_pane_inner)
        left_lay.setSpacing(10)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(grp_json)
        left_lay.addWidget(grp_info)
        left_lay.addWidget(grp_prev_row)
        # grp_usage gets stretch=1: at default dialog size the textedit
        # sits at its 64px sizeHint (controlled by _GrowableTextEdit so
        # the layout's natural sum doesn't blow past the viewport); when
        # the dialog grows, the extra height is absorbed by the textedit
        # rather than appearing as empty space, keeping grp_opts pinned
        # to the bottom - bottom-aligned with grp_params on the right.
        left_lay.addWidget(grp_usage, 1)
        left_lay.addWidget(grp_opts)
        left_lay.addWidget(grp_panel)

        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidget(left_pane_inner)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(200)

        # --- Right pane (Exposed Parameters) ---
        right_pane = QtWidgets.QWidget()
        right_lay = QtWidgets.QVBoxLayout(right_pane)
        right_lay.setSpacing(0)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.addWidget(grp_params, 1)
        # Right pane has a hard 400px floor - splitter handle stops there.
        # Left pane (above) has a 200px shrink floor before the curtain
        # snap kicks in.
        right_pane.setMinimumWidth(400)

        # --- Horizontal splitter (dotted handle, professional Qt model) ---
        # Stretch factors do all the work natively - no custom resize
        # event handling needed. When the dialog grows or shrinks, the
        # right pane absorbs the delta (stretch=1) and the left pane
        # stays at its current pixel width (stretch=0).
        # This is the standard side-panel plus main-panel splitter pattern.
        self._splitter = DottedSplitter(QtCore.Qt.Horizontal)
        self._splitter.setChildrenCollapsible(True)
        self._splitter.setHandleWidth(16)
        self._splitter.addWidget(left_scroll)
        self._splitter.addWidget(right_pane)
        # Left pane can curtain shut (drag handle past 200px -> snap to 0
        # so the right pane takes the full window). Right pane has a hard
        # 400px floor - handle stops there, no curtain on that side.
        self._splitter.setCollapsible(0, True)
        self._splitter.setCollapsible(1, False)
        # Stretch factors: left fixed, right absorbs window resize.
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([700, 750])
        # Both panes carry their own min widths (above); the splitter
        # respects them and stops the handle at those bounds. Stretch
        # factors are equal - Qt's default redistribution kicks in only
        # when the user drags a corner (handled by `resizeEvent` below for
        # edge-aware semantics).
        root.addWidget(self._splitter, 1)

        # --- Buttons ---
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        # 10px gap before Save/Cancel.
        root.addSpacing(10)
        root.addWidget(btns)

    # ------------------------------------------------------------------
    def _pick_color(self):
        try:
            import nuke
        except ImportError:
            _dialogs.warn(
                self, 'Color picker unavailable',
                'The Nuke color picker can only be used inside Nuke.')
            return
        color = nuke.getColor(self._gizmo_color or 0)
        if color is not None:
            # OR with 0x01 in the alpha byte to mark "explicitly picked".
            # Pure black returns 0x00000000 from the picker - without this
            # marker it would be indistinguishable from "unset" (0 means
            # default/no-color in our convention).
            self._gizmo_color = color | 0x01
            self._update_color_swatch()
            self._refresh_title_swatch()

    def _clear_color(self):
        self._gizmo_color = 0
        self._update_color_swatch()
        self._refresh_title_swatch()

    def _update_color_swatch(self):
        if self._gizmo_color:
            c = self._gizmo_color
            r = (c >> 24) & 0xFF
            g = (c >> 16) & 0xFF
            b = (c >>  8) & 0xFF
            hex_color = '#{:02X}{:02X}{:02X}'.format(r, g, b)
            text_color = _contrast_text_color(c)
            self._color_swatch.setPixmap(QtGui.QPixmap())
            self._color_swatch.setStyleSheet(
                _swatch_style('nukomfyGizmoColorSwatch', hex_color, text_color))
            self._color_swatch.setToolTip('Gizmo color: {}'.format(hex_color))
        else:
            self._color_swatch.setStyleSheet('')
            self._color_swatch.setPixmap(_make_default_swatch_pixmap(
                self._color_swatch.width(),
                self._color_swatch.height()))
            self._color_swatch.setToolTip(
                'Left-click: pick color\n'
                'Right-click: open menu')

    # ------------------------------------------------------------------
    def _pick_title_node_color(self):
        try:
            import nuke
        except ImportError:
            _dialogs.warn(
                self, 'Color picker unavailable',
                'The Nuke color picker can only be used inside Nuke.')
            return
        color = nuke.getColor(self._title_node_color or 0)
        if color is not None:
            # Alpha-byte marker so pure black survives the "0 = unset" check.
            self._title_node_color = color | 0x01
            self._update_title_node_swatch()

    def _reset_title_node_color(self):
        self._title_node_color = 0
        self._update_title_node_swatch()

    def _update_title_node_swatch(self):
        if self._title_node_color:
            c = self._title_node_color
            r = (c >> 24) & 0xFF
            g = (c >> 16) & 0xFF
            b = (c >>  8) & 0xFF
            hex_color = '#{:02X}{:02X}{:02X}'.format(r, g, b)
            text_color = _contrast_text_color(c)
            self._title_node_swatch.setPixmap(QtGui.QPixmap())
            self._title_node_swatch.setStyleSheet(
                _swatch_style('nukomfyTitleNodeColorSwatch', hex_color, text_color))
            self._title_node_swatch.setToolTip(
                'Title color: {}'.format(hex_color))
        else:
            self._title_node_swatch.setStyleSheet('')
            self._title_node_swatch.setPixmap(_make_default_swatch_pixmap(
                self._title_node_swatch.width(),
                self._title_node_swatch.height()))
            self._title_node_swatch.setToolTip(
                'Left-click: pick color\n'
                'Right-click: open menu')

    # ------------------------------------------------------------------
    # Title style: dropdown + Title Color picker / logo upload
    # ------------------------------------------------------------------
    def _on_title_mode_changed(self, idx):
        mode = self._title_mode_combo.itemData(idx)
        if not mode:
            return
        self._title_mode = mode
        self._hide_logo_hover_popup()
        self._refresh_title_widgets()

    def _refresh_title_widgets(self):
        """Relabel the Header swatch based on the current title_mode. The
        label flips between 'Header Color:' and 'Header Logo:' when the
        mode is logo. In 'default' mode the gate in _install_swatch_actions
        suppresses clicks and the cursor flips to arrow to signal
        non-interactivity - we don't `setEnabled(False)` because that
        would also dim the swatch visually."""
        if self._title_mode == 'use_custom_logo':
            self._title_lbl.setText('Header Logo:')
        else:
            self._title_lbl.setText('Header Color:')
        self._title_swatch.setEnabled(True)
        if self._title_mode == 'default':
            self._title_swatch.setCursor(QtCore.Qt.ArrowCursor)
        else:
            self._title_swatch.setCursor(QtCore.Qt.PointingHandCursor)
        self._refresh_title_swatch()

    def _recompute_title_color_from_gizmo(self):
        """In-panel title color follows the gizmo color exactly when linked."""
        return self._gizmo_color

    def _title_pick_or_load(self):
        if self._title_mode == 'default':
            return
        if self._title_mode == 'use_gizmo_color':
            try:
                import nuke
            except ImportError:
                _dialogs.warn(
                    self, 'Color picker unavailable',
                    'The Nuke color picker can only be used inside Nuke.')
                return
            seed = (self._title_color
                    if not self._title_color_linked and self._title_color
                    else self._recompute_title_color_from_gizmo())
            picked = nuke.getColor(seed or 0)
            if picked is not None:
                # Alpha-byte marker (see _pick_color comment).
                self._title_color = picked | 0x01
                self._title_color_linked = False
                self._refresh_title_swatch()
            return
        # use_custom_logo
        path = _dialogs.get_open_file(
            self, 'Select Logo', '', 'PNG Image (*.png)')
        if not path:
            return
        self._title_logo_path_pending = path
        self._title_logo_clear_pending = False
        self._refresh_title_swatch()

    def _title_reset(self):
        if self._title_mode == 'default':
            return
        if self._title_mode == 'use_gizmo_color':
            self._title_color = 0
            self._title_color_linked = True
            self._refresh_title_swatch()
            return

        # use_custom_logo: discard any pending upload and mark the on-disk
        # logo for deletion at save (clear_pending). Nothing is written to
        # disk here - cancelling the dialog leaves gizmo_logo.png untouched.
        # The swatch reads clear_pending to render the empty state.
        self._title_logo_path_pending = None
        self._title_logo_clear_pending = True
        self._hide_logo_hover_popup()
        self._refresh_title_swatch()

    def _resolve_title_color_int(self):
        """Effective title color int (0xRRGGBB00) for swatch rendering."""
        if self._title_color_linked:
            return self._recompute_title_color_from_gizmo()
        return self._title_color

    def _refresh_title_swatch(self):
        """Render the Header swatch - solid color in use_gizmo_color mode,
        image preview (or icon fallback) in use_custom_logo mode, default
        stripe pixmap in default mode."""
        self._title_swatch.setText('')
        self._title_swatch.setPixmap(QtGui.QPixmap())
        self._title_swatch.setFont(QtGui.QFont())  # reset (Material font set
                                                   # only by the fallback)
        self._title_logo_full_pixmap = None
        sw_w = self._title_swatch.width()
        sw_h = self._title_swatch.height()

        if self._title_mode == 'default':
            self._title_swatch.setStyleSheet('')
            self._title_swatch.setPixmap(
                _make_default_swatch_pixmap(sw_w, sw_h))
            self._title_swatch.setToolTip(
                'Header is at Nuke default. Change Header style to enable.')
            return

        if self._title_mode == 'use_gizmo_color':
            color_int = self._resolve_title_color_int()
            if color_int:
                r = (color_int >> 24) & 0xFF
                g = (color_int >> 16) & 0xFF
                b = (color_int >>  8) & 0xFF
                hex_color = '#{:02X}{:02X}{:02X}'.format(r, g, b)
                text_color = _contrast_text_color(color_int)
                self._title_swatch.setStyleSheet(
                    _swatch_style('nukomfyTitleColorSwatch', hex_color, text_color))
                tip = ('Header color: {} (linked to gizmo color)' if
                       self._title_color_linked else
                       'Header color: {} (manual override)').format(hex_color)
                self._title_swatch.setToolTip(tip)
            else:
                self._title_swatch.setStyleSheet('')
                self._title_swatch.setPixmap(
                    _make_default_swatch_pixmap(sw_w, sw_h))
                self._title_swatch.setToolTip(
                    'Header color: Nuke default (no gizmo color set)')
            return

        # use_custom_logo
        full_pix = None
        if self._title_logo_path_pending and os.path.isfile(
                self._title_logo_path_pending):
            full_pix = QtGui.QPixmap(self._title_logo_path_pending)
        elif (not self._title_logo_clear_pending and self._edit_item
              and self._edit_item.folder_path):
            existing = os.path.join(self._edit_item.folder_path,
                                    'gizmo_logo.png')
            if os.path.isfile(existing):
                full_pix = QtGui.QPixmap(existing)
        if full_pix is not None and not full_pix.isNull():
            self._title_logo_full_pixmap = full_pix
            scaled_pix = full_pix.scaled(
                self._title_swatch.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation)
            self._title_swatch.setPixmap(scaled_pix)
            self._title_swatch.setStyleSheet(
                'QLabel#nukomfyTitleColorSwatch{'
                'background:#222;border:1px solid #888;'
                'border-radius:2px;}')
            # Suppress the text tooltip - the hover popup already shows the
            # full-size image, two overlapping tooltips would conflict.
            self._title_swatch.setToolTip('')
        else:
            self._title_swatch.setFont(icon_font(13))
            self._title_swatch.setText(ADD_PHOTO_ALTERNATE)
            self._title_swatch.setStyleSheet(
                'QLabel#nukomfyTitleColorSwatch{'
                'background:#2b2b2b;color:#888;'
                'border:1px solid #888;border-radius:2px;}')
            self._title_swatch.setToolTip(
                'No logo loaded. Left-click to load.')

    # ------------------------------------------------------------------
    # Hover preview popup for the logo swatch
    # ------------------------------------------------------------------
    def _show_image_hover_popup(self, anchor, full_pixmap, popup_attr, label_attr):
        """Show a frameless, mouse-transparent popup with *full_pixmap*
        (scaled to _HOVER_POPUP_MAX_SIDE, composited over a checker) placed
        next to *anchor*. The QFrame and its QLabel are created lazily and
        cached on the instance under *popup_attr* / *label_attr*. Shared by
        the logo and preview swatch hover popups.
        """
        popup = getattr(self, popup_attr)
        if popup is None:
            popup = QtWidgets.QFrame(
                self,
                QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint)
            # Decorative popup: it must never receive mouse events. (This
            # does not by itself stop the edge flicker on a top-level popup
            # window; the off-cursor side placement below does.)
            popup.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            popup.setStyleSheet(
                'QFrame{background:#1e1e1e;border:1px solid #555;}')
            lay = QtWidgets.QVBoxLayout(popup)
            lay.setContentsMargins(4, 4, 4, 4)
            label = QtWidgets.QLabel(popup)
            label.setStyleSheet('QLabel{background:transparent;border:none;}')
            lay.addWidget(label)
            setattr(self, popup_attr, popup)
            setattr(self, label_attr, label)

        pix = full_pixmap
        max_side = _HOVER_POPUP_MAX_SIDE
        if pix.width() > max_side or pix.height() > max_side:
            pix = pix.scaled(
                max_side, max_side,
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation)
        getattr(self, label_attr).setPixmap(_compose_over_checker(pix))
        popup.adjustSize()

        # Try positions in order: right, left, below, above the swatch.
        # First candidate that fits the screen whole is used; if none fit,
        # fall back to a side placement clamped on the perpendicular axis
        # only (see below) so the popup never lands under the cursor.
        sw_top_left = anchor.mapToGlobal(QtCore.QPoint(0, 0))
        sw_w, sw_h = anchor.width(), anchor.height()
        pw = popup.width()
        ph = popup.height()
        gap = 8
        margin = 4

        screen = None
        if hasattr(QtWidgets.QApplication, 'screenAt'):
            try:
                screen = QtWidgets.QApplication.screenAt(sw_top_left)
            except Exception:
                screen = None
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        geom = screen.availableGeometry()

        candidates = [
            (sw_top_left.x() + sw_w + gap, sw_top_left.y()),     # right
            (sw_top_left.x() - pw - gap,   sw_top_left.y()),     # left
            (sw_top_left.x(),              sw_top_left.y() + sw_h + gap),  # below
            (sw_top_left.x(),              sw_top_left.y() - ph - gap),    # above
        ]
        chosen = None
        for x, y in candidates:
            if (x >= geom.left() + margin
                    and x + pw <= geom.right() - margin
                    and y >= geom.top() + margin
                    and y + ph <= geom.bottom() - margin):
                chosen = (x, y)
                break
        if chosen is None:
            # No side fits the popup whole. Place it adjacent to the
            # anchor on a side with room, clamping only the perpendicular
            # axis. Clamping both axes would drag the popup over the anchor
            # and under the cursor, giving the swatch a spurious
            # leaveEvent -> hide -> re-enter -> show flicker loop
            # (WA_TransparentForMouseEvents does not prevent this for a
            # top-level popup window).
            cx = max(geom.left() + margin,
                     min(sw_top_left.x(), geom.right() - pw - margin))
            cy = max(geom.top() + margin,
                     min(sw_top_left.y(), geom.bottom() - ph - margin))
            right_x = sw_top_left.x() + sw_w + gap
            left_x = sw_top_left.x() - pw - gap
            below_y = sw_top_left.y() + sw_h + gap
            above_y = sw_top_left.y() - ph - gap
            if right_x + pw <= geom.right() - margin:
                chosen = (right_x, cy)
            elif left_x >= geom.left() + margin:
                chosen = (left_x, cy)
            elif below_y + ph <= geom.bottom() - margin:
                chosen = (cx, below_y)
            elif above_y >= geom.top() + margin:
                chosen = (cx, above_y)
            else:
                chosen = (cx, cy)

        popup.move(chosen[0], chosen[1])
        popup.show()

    def _show_logo_hover_popup(self):
        if (self._title_mode != 'use_custom_logo'
                or self._title_logo_full_pixmap is None):
            return
        self._show_image_hover_popup(
            self._title_swatch, self._title_logo_full_pixmap,
            '_title_logo_popup', '_title_logo_popup_label')

    def _hide_logo_hover_popup(self):
        if self._title_logo_popup is not None:
            self._title_logo_popup.hide()

    # ------------------------------------------------------------------
    def _browse_json(self):
        path = _dialogs.get_open_file(
            self, 'Select Workflow', '',
            'ComfyUI JSON (*.json *.app.json);;All Files (*)')
        if not path:
            return
        self._json_path = path
        # New workflow selected: discard any templates staged for a prior one.
        self._pending_templates = {}
        self.inputs_table.set_pending_templates([])
        self.json_edit.setText(os.path.normpath(path))
        kind, err = self._validate_ui_workflow_file(path)
        self._json_error = err
        if err:
            self.params_lbl.setText('')
            self._refresh_status_label()
            return
        if not self.name_edit.text():
            base = os.path.basename(path)
            base_lower = base.lower()
            for ext in ('.app.json', '.json'):
                if base_lower.endswith(ext):
                    base = base[:-len(ext)]
                    break
            self.name_edit.setText(base.replace('_', ' ').title())
        self.params_lbl.setText(
            'Workflow selected. Click Sync Parameters to load.')
        self._refresh_status_label()

    def _compute_workflow_json_hash(self, path):
        """SHA256 of the source workflow.json bytes, prefixed `sha256:`.
        Returns None if the file is missing or unreadable so the caller
        can treat a missing source the same as a no-op (no banner).
        """
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, 'rb') as f:
                return _hash_workflow_json_bytes(f.read())
        except OSError:
            return None

    def _refresh_status_label(self):
        """Update the inline banners below the UI workflow picker.
        Two banners coexist mutually exclusively: `_status_banner` (red,
        JSON error) and `_hash_warn_banner` (orange, source workflow.json
        changed since last Sync). At most one is visible at any time. The
        banner is part of the layout, so its height is absorbed by the
        Extra Info field (stretch=1) without resizing the dialog.
        """
        ui_loaded = bool(self._json_path and os.path.isfile(self._json_path))
        self._btn_update.setEnabled(not self._json_error and ui_loaded)
        # Gate the hash banner on the LOGICAL status message we are
        # about to set, not the widget's current isVisible() flag - in
        # PySide that flag does not flip synchronously when set_message
        # toggles visibility, which would let both banners coexist.
        status_msg = self._json_error or ''
        hash_msg = ''
        if not status_msg and self._snapshot:
            snap_hash = self._snapshot.get('workflow_json_hash')
            cur_hash = self._compute_workflow_json_hash(self._json_path)
            if snap_hash and cur_hash and cur_hash != snap_hash:
                hash_msg = (
                    'Source workflow.json changed since last Sync. '
                    'Click Sync Parameters to refresh the parameter list.')
        self._status_banner.set_message(status_msg)
        self._hash_warn_banner.set_message(hash_msg)

    def _validate_ui_workflow_file(self, path):
        """Return (kind, error_message_or_None). kind ∈ {'ui','api','invalid','unreadable'}."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return 'unreadable', 'Workflow file is missing or not valid JSON.'
        kind = _classify_workflow_json(data)
        if kind == 'ui':
            return kind, None
        if kind == 'api':
            return kind, ('Workflow file is in API format. Export the '
                          'standard workflow from ComfyUI via File '
                          '\u2192 Export.')
        return kind, 'Workflow file is not a valid ComfyUI workflow.'

    def _show_prev_thumb_placeholder(self):
        """Render the preview thumbnail in its 'no image loaded' state -
        Material Icons `add_photo_alternate` glyph in muted grey, matching
        the Title Logo swatch placeholder for visual consistency."""
        self.prev_thumb.setPixmap(QtGui.QPixmap())
        self.prev_thumb.setFont(icon_font(28))
        self.prev_thumb.setText(ADD_PHOTO_ALTERNATE)
        self.prev_thumb.setStyleSheet(_PREV_THUMB_PLACEHOLDER_STYLE)
        self.prev_thumb.setToolTip(
            'No preview image loaded. Click Browse… to select one.')

    def _show_prev_thumb_image(self, pixmap):
        """Render the preview thumbnail with the loaded pixmap, clearing
        the placeholder font/text so the glyph doesn't bleed through."""
        self.prev_thumb.setText('')
        self.prev_thumb.setFont(QtGui.QFont())
        self.prev_thumb.setStyleSheet(_PREV_THUMB_IMAGE_STYLE)
        self.prev_thumb.setPixmap(pixmap)
        self.prev_thumb.setToolTip('')

    def _show_prev_b_thumb_placeholder(self):
        """Second-preview swatch in its 'no image loaded' state - same
        look as the primary swatch placeholder."""
        self.prev_thumb_b.setPixmap(QtGui.QPixmap())
        self.prev_thumb_b.setFont(icon_font(28))
        self.prev_thumb_b.setText(ADD_PHOTO_ALTERNATE)
        self.prev_thumb_b.setStyleSheet(_PREV_THUMB_PLACEHOLDER_STYLE)
        self.prev_thumb_b.setToolTip(
            'Optional second preview. Click Browse… to add a before/after '
            'comparison image.')

    def _show_prev_b_thumb_image(self, pixmap):
        self.prev_thumb_b.setText('')
        self.prev_thumb_b.setFont(QtGui.QFont())
        self.prev_thumb_b.setStyleSheet(_PREV_THUMB_IMAGE_STYLE)
        self.prev_thumb_b.setPixmap(pixmap)
        self.prev_thumb_b.setToolTip('')

    def _browse_preview(self):
        path = _dialogs.get_open_file(
            self, 'Select Preview Image', '',
            'Images (*.gif *.png *.jpg *.jpeg *.webp);;All Files (*)')
        if not path:
            return
        # Validate the file is actually a loadable image before accepting.
        # Without this guard, a non-image file with an image extension
        # (e.g. a .txt renamed to .png) would be copied verbatim into the
        # workflow folder at save time, producing a broken preview.
        px = QtGui.QPixmap(path)
        if px.isNull():
            _dialogs.warn(
                self, 'Invalid image',
                'The selected file could not be loaded as an image:\n\n'
                '{}\n\n'
                'Please select a valid GIF, PNG, JPEG or WEBP file.'.format(
                    os.path.normpath(path)))
            return
        self.prev_edit.setText(os.path.normpath(path))
        self._preview_full_pixmap = px
        self._show_prev_thumb_image(
            px.scaled(72, 72, QtCore.Qt.KeepAspectRatio,
                      QtCore.Qt.SmoothTransformation))

    def _browse_preview_b(self):
        path = _dialogs.get_open_file(
            self, 'Select Second Preview Image', '',
            'Images (*.gif *.png *.jpg *.jpeg *.webp);;All Files (*)')
        if not path:
            return
        px = QtGui.QPixmap(path)
        if px.isNull():
            _dialogs.warn(
                self, 'Invalid image',
                'The selected file could not be loaded as an image:\n\n'
                '{}\n\n'
                'Please select a valid GIF, PNG, JPEG or WEBP file.'.format(
                    os.path.normpath(path)))
            return
        self.prev_edit_b.setText(os.path.normpath(path))
        self._preview_b_full_pixmap = px
        self._show_prev_b_thumb_image(
            px.scaled(72, 72, QtCore.Qt.KeepAspectRatio,
                      QtCore.Qt.SmoothTransformation))

    def _preview_remove(self):
        """Clear the primary preview slot. The on-disk file is removed at
        save time (the save reconciles disk to the fields), not here - so
        cancelling the dialog leaves the workflow on disk untouched."""
        self.prev_edit.setText('')
        self._preview_full_pixmap = None
        self._hide_preview_hover_popup()
        self._show_prev_thumb_placeholder()

    def _preview_remove_b(self):
        """Clear the second preview slot. On-disk removal happens at save,
        not here (mirror of `_preview_remove`)."""
        self.prev_edit_b.setText('')
        self._preview_b_full_pixmap = None
        self._hide_preview_b_hover_popup()
        self._show_prev_b_thumb_placeholder()

    # ------------------------------------------------------------------
    # Hover preview popup for the workflow Preview Image (mirrors the
    # title-logo popup: full-size pixmap with checker background, smart
    # placement, transparent for mouse events).
    # ------------------------------------------------------------------
    def _show_preview_hover_popup(self):
        if self._preview_full_pixmap is None:
            return
        self._show_image_hover_popup(
            self.prev_thumb, self._preview_full_pixmap,
            '_preview_popup', '_preview_popup_label')

    def _hide_preview_hover_popup(self):
        if self._preview_popup is not None:
            self._preview_popup.hide()

    def _show_preview_b_hover_popup(self):
        if self._preview_b_full_pixmap is None:
            return
        self._show_image_hover_popup(
            self.prev_thumb_b, self._preview_b_full_pixmap,
            '_preview_b_popup', '_preview_b_popup_label')

    def _hide_preview_b_hover_popup(self):
        if self._preview_b_popup is not None:
            self._preview_b_popup.hide()

    # --- Unified params toolbar dispatchers -------------------------------
    def _tb_move_up(self):
        w = self.params_tabs.currentWidget()
        if hasattr(w, '_move_up'):
            w._move_up()

    def _tb_move_down(self):
        w = self.params_tabs.currentWidget()
        if hasattr(w, '_move_down'):
            w._move_down()

    def _tb_add_sep(self):
        self.knobs_table._add_separator()
        self._update_reorder_state()

    def _tb_add_group(self):
        self.knobs_table._add_group()
        self._update_reorder_state()

    def _tb_add_text(self):
        self.knobs_table._add_text()
        self._update_reorder_state()

    def _tb_remove_selected(self):
        """Auto-detect: removes the selected separator / group (both
        rows) / text row. No-op otherwise (button is greyed in that
        case)."""
        kt = self.knobs_table
        r = kt._selected_row()
        if r < 0:
            return
        if kt._is_group_marker(r):
            kt._remove_group()
        elif kt._is_text_row(r):
            kt._remove_text()
        elif kt._is_separator(r) and not kt._is_fixed_section(r):
            kt._remove_separator()
        self._update_reorder_state()

    def _update_reorder_state(self):
        """Reflect active-tab row/selection in the unified toolbar.
        Buttons are greyed when the corresponding action isn't possible."""
        w = self.params_tabs.currentWidget()
        if hasattr(w, 'can_move_up'):
            self._btn_tb_up.setEnabled(w.can_move_up())
            self._btn_tb_down.setEnabled(w.can_move_down())
        else:
            self._btn_tb_up.setEnabled(False)
            self._btn_tb_down.setEnabled(False)

        is_knobs = w is self.knobs_table
        can_add_sep = is_knobs and self.knobs_table._can_add_separator()
        can_add_grp = is_knobs and self.knobs_table._can_add_group()
        can_add_txt = is_knobs and self.knobs_table._can_add_text()
        self._action_add_sep.setEnabled(can_add_sep)
        self._action_add_group.setEnabled(can_add_grp)
        self._action_add_text.setEnabled(can_add_txt)
        self._btn_tb_add.setEnabled(
            can_add_sep or can_add_grp or can_add_txt)
        can_remove = (
            is_knobs
            and (self.knobs_table.can_remove_separator()
                 or self.knobs_table._can_remove_group()
                 or self.knobs_table._can_remove_text()))
        self._btn_tb_remove.setEnabled(can_remove)

    # ------------------------------------------------------------------
    # Workflow write templates (Inputs tab combo actions)
    # ------------------------------------------------------------------
    def _current_workflow_dir(self):
        """The workflow's on-disk library folder, or None when it doesn't
        exist yet (Add mode, before the first Save). Workflow-specific write
        templates live inside this folder; for a never-saved workflow they are
        staged in memory and written here at Save instead."""
        if not self._json_path:
            return None
        d = os.path.dirname(self._json_path)
        return d if fs_safe.is_workflow_folder(d) else None

    def _pending_tuples(self):
        """Staged templates as (display, filename, 'workflow') combo tuples."""
        return [('{} (workflow)'.format(os.path.splitext(f)[0]), f, 'workflow')
                for f in self._pending_templates]

    def _tpl_manage(self):
        """Open the modal manage dialog for this workflow's write templates.
        On Save it applies the edits (saved workflow: writes/deletes files now;
        new workflow: updates the staged set, written at the workflow's own
        Save), then refreshes the combos. Cancel changes nothing."""
        from Nukomfy.gui._workflow_templates_dialog import \
            ManageWorkflowTemplatesDialog
        dlg = ManageWorkflowTemplatesDialog(
            self._current_workflow_dir(), dict(self._pending_templates),
            parent=self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._pending_templates = dlg.result_pending()
        self.inputs_table.set_pending_templates(self._pending_tuples())
        self.inputs_table.refresh_templates(dropped=dlg.removed_filenames())

    def _update_params(self):
        """Fetch a new snapshot from a running ComfyUI machine (Sync
        Parameters button). The button is gated by `_refresh_status_label`
        so it's disabled when no JSON is loaded or the JSON has a
        validation error - these guards are a safety net for edge cases
        (file deleted from disk while the dialog is open) and bail
        silently. In-editor edits are flushed to overrides first so unsaved
        changes survive, then migrated against the new snapshot in
        `_on_params_fetched`.
        """
        if not self._json_path or not os.path.isfile(self._json_path):
            return
        if self._json_error:
            return

        # Flush in-editor edits into _overrides / _widget_order /
        # _v3_user_edits before the resync rebuilds the tables, so unsaved
        # changes (EN toggles, labels, default values, row order) survive
        # Sync instead of being discarded. Mirrors the save path; the
        # migrate_* calls in _on_params_fetched reconcile the captured
        # state against the new snapshot. No-op before the first snapshot
        # exists (the method guards on self._snapshot).
        self._collect_state_from_tables()

        # Show progress dialog
        self._progress = QtWidgets.QProgressDialog(
            'Fetching workflow parameters\u2026', 'Cancel', 0, 0, self)
        apply_nukomfy_palette(self._progress)
        self._progress.setWindowTitle('Fetching parameters')
        self._progress.setWindowModality(QtCore.Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setMinimumWidth(320)
        self._progress.canceled.connect(self._cancel_update)
        self._btn_update.setEnabled(False)

        # Start background worker
        self._stop_param_worker()
        self._param_worker = _ParamWorker(self._json_path)
        self._param_worker.finished.connect(self._on_params_fetched)
        self._param_worker.start()

    def _cancel_update(self):
        self._stop_param_worker()
        self._btn_update.setEnabled(True)

    def _on_params_fetched(self, success, snapshot, error_msg,
                           missing_nodes, non_nukomfy_outputs=None):
        # Ignore a stale emission from a superseded worker: a rapid
        # second Sync stops the previous worker, but a finished signal
        # it already queued can still arrive here.
        if self.sender() is not self._param_worker:
            return
        # Close progress dialog
        if hasattr(self, '_progress') and self._progress:
            self._progress.close()
            self._progress = None
        self._btn_update.setEnabled(True)

        if not success:
            if error_msg == _UNSUPPORTED_WORKFLOW_SENTINEL:
                self._show_unsupported_workflow_dialog()
            elif error_msg == _NUKOMFY_SUITE_NOT_INSTALLED_SENTINEL:
                self._show_nukomfy_suite_not_installed_dialog()
            elif error_msg == _NO_NUKOMFY_WRITE_NODE_SENTINEL:
                self._show_no_nukomfy_write_node_dialog()
            elif error_msg == _NO_NUKOMFY_WRITE_OUTPUT_SENTINEL:
                self._show_no_nukomfy_write_output_dialog()
            elif error_msg:
                _dialogs.warn(self, 'Fetch failed', error_msg)
            return

        # Migrate existing user state against the new snapshot before
        # installing it - widgets removed server-side drop their
        # override entries, widgets added server-side land at the tail
        # of widget_order, v3 edits for non-existent sub options are
        # discarded.
        from Nukomfy.gui.workflow_state import (
            migrate_overrides, migrate_widget_order,
            migrate_v3_user_edits)
        self._overrides = migrate_overrides(self._overrides, snapshot)
        self._widget_order = migrate_widget_order(self._widget_order,
                                                  snapshot)
        self._v3_user_edits = migrate_v3_user_edits(self._v3_user_edits,
                                                    snapshot)
        self._snapshot = snapshot

        # Number duplicate labels on the freshly-synced inputs/outputs
        # so a first-time Sync produces unique gizmo labels. Subsequent
        # user edits in the UI go through `_ensure_unique_label`.
        widgets = snapshot.get('widgets', [])
        inputs_w = [w for w in widgets if w.get('role') == 'input']
        outputs_w = [w for w in widgets if w.get('role') == 'output']
        _number_duplicate_labels(inputs_w)
        _number_duplicate_labels(outputs_w)

        self._render_tables_from_state()

        # Sync writes a fresh snapshot whose hash matches the current
        # workflow.json bytes by construction, so the hash mismatch
        # banner (if any) is no longer accurate. Recompute and clear.
        self._refresh_status_label()

        if missing_nodes:
            missing_set = set(missing_nodes)
            self._highlight_missing(self.inputs_table, missing_set)
            self._highlight_missing_knobs(self.knobs_table, missing_set)
            self._highlight_missing(self.outputs_table, missing_set)

        self._btn_reset_defaults.setVisible(bool(widgets))

        # Show warning about missing nodes
        if missing_nodes:
            _dialogs.warn(self, 'Missing node types',
                'These node types were not found on any configured '
                'machine:\n\n'
                + '\n'.join('  \u2022 {}'.format(n) for n in missing_nodes)
                + '\n\nTheir parameters are highlighted in red and '
                'their types or values may be inaccurate.')

        # Show warning about output-marked nodes that aren't NukomfyWrite. They
        # won't render to file through the gizmo (only NukomfyWrite is auto-
        # managed) - the user should know they'll be silently ignored.
        if non_nukomfy_outputs:
            lines = []
            for node_type, title in non_nukomfy_outputs:
                if title and title != node_type:
                    lines.append('  \u2022 {} - {}'.format(
                        node_type, title))
                else:
                    lines.append('  \u2022 {}'.format(node_type))
            _dialogs.warn(self,
                'Unsupported output nodes',
                'Only NukomfyWrite is supported as a gizmo output. '
                'The following output-marked nodes will not be '
                'rendered to file:\n\n'
                + '\n'.join(lines)
                + '\n\nTheir outputs stay inside ComfyUI. Replace them '
                'with NukomfyWrite to make the result available in Nuke.')

    def _reset_defaults(self):
        """Reset to the snapshot defaults: clear server-derived overrides,
        V3 user edits, and widget_order (returns to the parser's natural
        order from the last Sync). I/O Mode and Write Template choices
        are preserved - they're a local workflow-folder binding the user
        makes once and the workflow JSON / server has no analogue to
        fall back to.
        """
        if not self._snapshot:
            return
        reply = _dialogs.ask(
            self, 'Reset defaults',
            'Reset every field back to the workflow defaults?\n\n'
            '  \u2022 Include in Gizmo \u2192 workflow default\n'
            '  \u2022 Gizmo Label \u2192 original parameter name\n'
            '  \u2022 Tooltip \u2192 workflow default\n'
            '  \u2022 Default Value (Parameters) \u2192 workflow JSON value\n'
            '  \u2022 Sub-options of dynamic combos for every value\n'
            '    (not just the current selection)\n'
            '  \u2022 Row order \u2192 workflow default\n'
            '    (manual reordering will be lost)\n\n'
            'Your local settings will be preserved:\n'
            '  \u2022 Write Template\n'
            '  \u2022 I/O Mode\n\n'
            'Bypassed/muted/disconnected nodes keep their disabled state.',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if reply != QtWidgets.QMessageBox.Yes:
            return
        # Preserve user-owned local fields that have no server analogue.
        # Everything else is cleared so the snapshot defaults take over.
        preserved = {}
        for key, entry in self._overrides.items():
            kept = {f: v for f, v in entry.items()
                    if f in ('io_mode', 'write_template',
                             'write_template_source')}
            if kept:
                preserved[key] = kept
        self._overrides = preserved
        self._v3_user_edits = {}
        self._widget_order = []
        self._render_tables_from_state()

    def _show_unsupported_workflow_dialog(self):
        """Show dialog when the workflow JSON contains no Nukomfy Suite nodes."""
        box = _dialogs.message_box(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle('Workflow missing Nukomfy Suite nodes')
        box.setTextFormat(QtCore.Qt.RichText)
        box.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        box.setText(
            'This workflow does not use <b>Nukomfy Suite</b> nodes.<br><br>'
            'Nukomfy requires <b>NukomfyRead</b> and <b>NukomfyWrite</b> '
            'nodes for file I/O.<br><br>'
            'Install them from:<br>'
            '<a href="https://github.com/francescolorussi/ComfyUI-Nukomfy-Suite" '
            'style="color:{link};">'
            'https://github.com/francescolorussi/ComfyUI-Nukomfy-Suite</a>'
            .format(link=LINK_FG))
        box.setStandardButtons(QtWidgets.QMessageBox.Ok)
        box.exec_()

    def _show_nukomfy_suite_not_installed_dialog(self):
        """Show dialog when Nukomfy Suite nodes are used in the workflow but
        the ComfyUI server does not have the pack installed."""
        box = _dialogs.message_box(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle('Nukomfy Suite not installed on server')
        box.setTextFormat(QtCore.Qt.RichText)
        box.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        box.setText(
            'This workflow uses <b>Nukomfy Suite</b> nodes, but the ComfyUI '
            'server does not have the pack installed.<br><br>'
            'Install it on the server from:<br>'
            '<a href="https://github.com/francescolorussi/ComfyUI-Nukomfy-Suite" '
            'style="color:{link};">'
            'https://github.com/francescolorussi/ComfyUI-Nukomfy-Suite</a>'
            .format(link=LINK_FG))
        box.setStandardButtons(QtWidgets.QMessageBox.Ok)
        box.exec_()

    def _show_no_nukomfy_write_node_dialog(self):
        """Show dialog when the workflow has no NukomfyWrite node at all."""
        box = _dialogs.message_box(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle('Workflow has no NukomfyWrite node')
        box.setTextFormat(QtCore.Qt.RichText)
        box.setText(
            'This workflow has no <b>NukomfyWrite</b> node.<br><br>'
            'Add a NukomfyWrite node to write the output, then mark it as '
            "an app output in ComfyUI's App Builder.")
        box.setStandardButtons(QtWidgets.QMessageBox.Ok)
        box.exec_()

    def _show_no_nukomfy_write_output_dialog(self):
        """Show dialog when NukomfyWrite nodes exist but none is marked as output."""
        box = _dialogs.message_box(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle('No NukomfyWrite marked as output')
        box.setTextFormat(QtCore.Qt.RichText)
        box.setText(
            'This workflow has one or more <b>NukomfyWrite</b> nodes, '
            'but none of them is marked as an app output.<br><br>'
            "Open ComfyUI's App Builder and mark at least one NukomfyWrite "
            'as an output.')
        box.setStandardButtons(QtWidgets.QMessageBox.Ok)
        box.exec_()

    @staticmethod
    def _highlight_missing(table_widget, missing_set):
        """Highlight rows whose node_type is in missing_set (QTableWidget)."""
        red = QtGui.QColor(80, 20, 20)
        nm_col = table_widget.NM
        for r in range(table_widget.rowCount()):
            item = table_widget.item(r, nm_col)
            if not item:
                continue
            p = item.data(QtCore.Qt.UserRole) or {}
            if p.get('node_type', '') in missing_set:
                for c in range(table_widget.columnCount()):
                    ci = table_widget.item(r, c)
                    if ci:
                        ci.setBackground(red)

    @staticmethod
    def _highlight_missing_knobs(knobs_widget, missing_set):
        """Highlight rows in _KnobsTable whose node_type is in missing_set."""
        red = QtGui.QColor(80, 20, 20)
        t = knobs_widget.table
        nm_col = knobs_widget.NM
        for r in range(t.rowCount()):
            item = t.item(r, nm_col)
            if not item:
                continue
            p = item.data(QtCore.Qt.UserRole) or {}
            if p.get('node_type', '') in missing_set:
                for c in range(t.columnCount()):
                    ci = t.item(r, c)
                    if ci:
                        ci.setBackground(red)

    def _render_tables_from_state(self):
        """Compose effective params from the editor state (snapshot +
        overrides + widget_order + v3_user_edits) and render the three
        editor tables. Bumps tab counts and the status label.
        """
        from Nukomfy.gui.workflow_state import (
            compose_params_for_editor,
            initial_widget_order_for_snapshot)
        # Gate on is_workflow_folder (via _current_workflow_dir): in Add mode
        # this is None, so the source JSON's sibling write_templates/ folder is
        # not scanned - only a saved workflow exposes its on-disk templates
        # (staged ones come from set_pending_templates).
        self.inputs_table.set_workflow_dir(self._current_workflow_dir())

        # Use the persisted widget_order if the user has reordered or
        # added structural rows; otherwise generate the parser's
        # natural layout with section markers for the editor view.
        order = self._widget_order
        if not order:
            order = initial_widget_order_for_snapshot(self._snapshot)

        composed = compose_params_for_editor(
            self._snapshot, self._overrides, order,
            self._v3_user_edits)

        inputs = [p for p in composed if p.get('role') == 'input']
        knobs_all = [p for p in composed
                     if p.get('role') in ('knob', 'separator',
                                          'group_begin', 'group_end',
                                          'text')]
        knobs_only = [p for p in composed if p.get('role') == 'knob']
        outputs = [p for p in composed if p.get('role') == 'output']

        output_node_ids = {p.get('target_node_id') for p in composed
                           if p.get('is_output')
                           and p.get('node_type') in _AUTO_FILE_PATH_NODES}
        input_node_ids = {p.get('target_node_id') for p in inputs}

        self.inputs_table.load(inputs)
        self.knobs_table.load(knobs_all, output_node_ids=output_node_ids,
                              input_node_ids=input_node_ids)
        self.outputs_table.load(outputs)
        self.knobs_table.install_snapshot_state(self._snapshot,
                                                self._v3_user_edits,
                                                self._overrides)

        n_in = len(inputs)
        n_kn = len(knobs_only)
        n_out = len(outputs)
        self.params_tabs.setTabText(0, 'Inputs ({})'.format(n_in))
        self.params_tabs.setTabText(1, 'Parameters ({})'.format(n_kn))
        self.params_tabs.setTabText(2, 'Outputs ({})'.format(n_out))
        self.params_lbl.setText('')
        self.params_tabs.setVisible(True)
        # Fresh render = fresh validation state: the rebuilt combos carry no
        # red borders, so drop any stale tab dots and disarm live re-validation.
        self._validation_active = False
        self._update_iomode_tab_dots(False, False)

    # ------------------------------------------------------------------
    def _update_iomode_tab_dots(self, inputs_invalid, outputs_invalid):
        """Append/remove a red dot on the Inputs/Outputs tab titles."""
        # The tab bar stylesheet hardcodes text color, so setTabTextColor
        # and rich-text labels are overridden. Use setTabIcon with a red dot
        # pixmap - this is unaffected by the stylesheet.
        if not hasattr(self, '_iomode_red_icon'):
            size = 10
            pm = QtGui.QPixmap(size, size)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setBrush(QtGui.QColor(ERROR_COLOR))
            p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(0, 0, size, size)
            p.end()
            self._iomode_red_icon = QtGui.QIcon(pm)
        empty = QtGui.QIcon()
        for tab_idx, invalid in ((0, inputs_invalid), (2, outputs_invalid)):
            self.params_tabs.setTabIcon(
                tab_idx, self._iomode_red_icon if invalid else empty)

    def _revalidate_after_fix(self):
        """Live-refresh the red tab dots while a failed Save's errors are being
        resolved, so the dot disappears the moment the last invalid I/O Mode /
        Write Template is fixed - no second Save needed. Gated by
        `_validation_active` so a fresh, never-saved workflow doesn't sprout
        dots. Per-combo red borders are cleared by the combos themselves; here
        we only own the tab dots."""
        if not self._validation_active:
            return
        inputs = self.inputs_table.get_params()
        outputs = self.outputs_table.get_params()
        bad_io_in = any(p.get('enabled', True) and not p.get('io_mode')
                        for p in inputs)
        bad_tpl = any(p.get('enabled', True) and not p.get('write_template')
                      for p in inputs)
        bad_io_out = any(p.get('enabled', True) and not p.get('io_mode')
                         for p in outputs)
        self._update_iomode_tab_dots(bad_io_in or bad_tpl, bad_io_out)
        if not (bad_io_in or bad_tpl or bad_io_out):
            self._validation_active = False

    def _collect_state_from_tables(self):
        """Pull UI-visible rows from the three editor tables and rebuild
        self._overrides + self._widget_order against the current
        snapshot. The V3 user edits dict is the live KnobsTable map:
        cascade callbacks write into it on every master swap, so by
        save time it already reflects the user's edits to non-active
        option subs (no migration needed here).

        Called at save time. Snapshot widgets remain untouched - they
        are the server-authoritative layer, only resynced via Sync.
        """
        from Nukomfy.gui.workflow_state import (
            diff_widget_against_snapshot, make_override_key)
        if not self._snapshot:
            return

        snap_by_key = {
            (w.get('target_node_id'),
             w.get('widget_name', w.get('name', ''))): w
            for w in self._snapshot.get('widgets', [])
        }

        flat_params = (self.inputs_table.get_params()
                      + self.knobs_table.get_params()
                      + self.outputs_table.get_params())

        overrides = {}
        widget_order = []
        visible_keys = set()
        for p in flat_params:
            role = p.get('role')
            if role in ('separator', 'group_begin', 'group_end', 'text'):
                entry = {'kind': role}
                for k in ('fixed', 'label', 'id', 'default', 'value'):
                    if k in p:
                        entry[k] = p[k]
                widget_order.append(entry)
                continue
            if role not in ('input', 'knob', 'output'):
                continue
            nid = p.get('target_node_id')
            wn = p.get('widget_name', p.get('name', ''))
            snap_w = snap_by_key.get((nid, wn))
            if snap_w is None:
                # Widget not in snapshot (e.g. parser-only entry or one
                # the user's editor session added before a Sync). Skip:
                # the persistent state only tracks widgets the snapshot
                # knows about.
                continue
            delta = diff_widget_against_snapshot(snap_w, p)
            if delta:
                overrides[make_override_key(nid, wn)] = delta
            widget_order.append([nid, wn])
            visible_keys.add(make_override_key(nid, wn))

        # Generic V3 subs hidden under a non-active master option are not
        # rows above, so an explicit EN toggle on them would be lost at
        # save. The KnobsTable cascade captured their EN in
        # _v3_enabled_state; persist it as an override when it differs
        # from the snapshot default. That default is exposure-based for
        # generic V3 subs (parser-added subs default False, exposed ones
        # default True), so both a check and an uncheck must be honoured.
        # Visible subs stay authoritative via their row above.
        snap_by_strkey = {
            make_override_key(k[0], k[1]): w
            for k, w in snap_by_key.items()}
        en_state = getattr(self.knobs_table, '_v3_enabled_state', {}) or {}
        for ov_key, en_val in en_state.items():
            if ov_key in visible_keys:
                continue
            snap_w = snap_by_strkey.get(ov_key)
            base = snap_w.get('enabled', True) if snap_w else True
            if en_val == base:
                continue
            entry = dict(overrides.get(ov_key, {}))
            entry['enabled'] = en_val
            entry['_intent_enabled'] = en_val
            overrides[ov_key] = entry

        # V3 user edits live directly on the KnobsTable: cascade
        # callbacks write into it on every master swap (see
        # _refresh_v3_master_subs / _snapshot_subs_into_user_edits).
        # By save time the map already reflects every per-option
        # delta vs the snapshot default.
        #
        # A hidden generic V3 sub thus has its state split across two
        # stores: its EN flag in _overrides (block above), its value
        # here in _v3_user_edits. This is intentional, not an oversight:
        # _overrides is the per-row UI delta layer shared by every
        # widget, while _v3_user_edits is the cascade-captured value
        # that has to outlive the row when a master swap hides it.
        # _merge_widget layers them back at load (value first, then the
        # override on top).
        self._overrides = overrides
        self._widget_order = widget_order
        self._v3_user_edits = dict(
            getattr(self.knobs_table, '_v3_user_edits', {}) or {})

    def _save(self):
        name = self.name_edit.text().strip()
        workflow_alias = self.workflow_alias_edit.text().strip()
        if not name:
            _dialogs.warn(
                self, 'Missing workflow name',
                'A workflow name is required before saving.')
            return
        if not self._json_path or not os.path.isfile(self._json_path):
            _dialogs.warn(
                self, 'No workflow selected',
                'A workflow JSON file must be selected before saving.')
            return
        if self._json_error:
            # Error is already shown inline in the status row - no modal.
            return

        # Re-validate that the workflow JSON still reads and parses at save
        # time: it was parsed when selected, but may have changed or been
        # removed on disk since. The parsed result is not needed here, only
        # that the load succeeds, so it is intentionally discarded.
        try:
            with open(self._json_path, 'r', encoding='utf-8') as f:
                json.load(f)
        except Exception as e:
            _dialogs.warn(
                self, 'Workflow file unreadable',
                'The workflow JSON file could not be read.\n\n{}'.format(e))
            return
        from Nukomfy.utils.path_utils import runtime_path
        local = runtime_path(settings.local_workflow_path,
                             fallback=settings.local_workflow_path)
        if not local:
            _dialogs.warn(
                self, 'Local workflow path not set',
                'A local workflow path must be configured in Settings '
                'before saving.')
            return

        # Warn if title style is "use_custom_logo" but no logo image will
        # be present after save (no pending upload, no file already on
        # disk). The gizmo would silently fall back to a text title,
        # which is rarely what the user wants when this mode is set.
        if self._title_mode == 'use_custom_logo':
            has_pending = (self._title_logo_path_pending
                           and os.path.isfile(
                               self._title_logo_path_pending))
            existing_logo = (self._edit_item is not None
                             and self._edit_item.folder_path
                             and os.path.isfile(os.path.join(
                                 self._edit_item.folder_path,
                                 'gizmo_logo.png')))
            if not has_pending and not existing_logo:
                ans = _dialogs.ask(
                    self, 'No logo loaded',
                    'Title style is set to "Use custom logo" but no logo '
                    'image has been loaded.\n\n'
                    'The gizmo will fall back to a text title until a '
                    'logo is added.\n\n'
                    'Continue saving?',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if ans != QtWidgets.QMessageBox.Yes:
                    return

        if not fs_safe.makedirs(local, parent=self,
                                action='workflow library root'):
            return

        # Pull user edits from the editor tables into in-memory state
        # (_overrides, _widget_order, _v3_user_edits). The tables expose
        # diff helpers that compare the row state against snapshot
        # defaults; this is the single point where row-level UI mutations
        # become persistent.
        if self._snapshot:
            self._collect_state_from_tables()

        from Nukomfy.gui.workflow_state import compose_params
        all_params = compose_params(
            self._snapshot, self._overrides, self._widget_order,
            self._v3_user_edits)

        has_output = any(p.get('role') == 'output' and p.get('enabled', True)
                         for p in all_params)
        if not has_output:
            _dialogs.warn(
                self, 'No output parameter',
                'The workflow must expose at least one output parameter.'
                '\n\n'
                'Outputs define where rendered files are written.')
            return

        # Validate: io_mode required for all enabled I/O params, and a write
        # template required for every enabled input. Both surface the same
        # way - red border + tab dot + focus, no popup, no disk write.
        input_params_saved = [p for p in all_params
                              if p.get('role') == 'input']
        output_params_saved = [p for p in all_params
                               if p.get('role') == 'output']
        invalid_inputs = {r for r, p in enumerate(input_params_saved)
                          if p.get('enabled', True) and not p.get('io_mode')}
        invalid_outputs = {r for r, p in enumerate(output_params_saved)
                           if p.get('enabled', True) and not p.get('io_mode')}
        invalid_templates = {r for r, p in enumerate(input_params_saved)
                             if p.get('enabled', True)
                             and not p.get('write_template')}
        if invalid_inputs or invalid_outputs or invalid_templates:
            self.inputs_table.mark_invalid_iomode(invalid_inputs)
            self.outputs_table.mark_invalid_iomode(invalid_outputs)
            self.inputs_table.mark_invalid_template(invalid_templates)
            self._update_iomode_tab_dots(
                bool(invalid_inputs or invalid_templates),
                bool(invalid_outputs))
            # Arm live re-validation: fixing the flagged combos now clears the
            # tab dot as soon as the last error is resolved (no re-Save needed).
            self._validation_active = True
            # Focus jump: first tab with errors -> scroll + focus first red row.
            # I/O Mode errors take focus precedence; otherwise the template col.
            if invalid_inputs or invalid_templates:
                self.params_tabs.setCurrentWidget(self._inputs_tab)
                if invalid_inputs:
                    first = min(invalid_inputs)
                    col = self.inputs_table.IM
                else:
                    first = min(invalid_templates)
                    col = self.inputs_table.WT
                it = self.inputs_table.item(first, 0)
                if it:
                    self.inputs_table.scrollToItem(it)
                w = self.inputs_table.cellWidget(first, col)
                if w:
                    w.setFocus()
            else:
                self.params_tabs.setCurrentWidget(self._outputs_tab)
                first = min(invalid_outputs)
                it = self.outputs_table.item(first, 0)
                if it:
                    self.outputs_table.scrollToItem(it)
                w = self.outputs_table.cellWidget(first, self.outputs_table.IM)
                if w:
                    w.setFocus()
            return  # No popup, no disk write - red borders + tab dots only
        # Validation passed - clear any previous red borders / dots
        self.inputs_table.mark_invalid_iomode(set())
        self.outputs_table.mark_invalid_iomode(set())
        self.inputs_table.mark_invalid_template(set())
        self._update_iomode_tab_dots(False, False)
        self._validation_active = False

        # Validate: labels must be unique within the inputs group and within
        # the outputs group (an input and an output may share a label).
        for role, role_label in (('input', 'input'), ('output', 'output')):
            seen = set()
            for p in all_params:
                if p.get('role') != role or not p.get('enabled', True):
                    continue
                lbl = p.get('label', p.get('name', ''))
                if lbl in seen:
                    _dialogs.warn(
                        self, 'Duplicate {} label'.format(role_label),
                        'The label "{}" is used by more than one {}.\n\n'
                        'Each {} must have a unique label.'
                        .format(lbl, role_label, role_label))
                    return
                seen.add(lbl)

        is_edit = self._edit_item is not None
        old_folder = self._edit_item.folder_path if is_edit else None

        # Reuse the output-path sanitizer so the workflow folder keeps the
        # workflow's real name (Unicode + ordinary punctuation), stripping
        # only filesystem/Nuke-unsafe chars instead of every non-alnum.
        from Nukomfy.utils.output_path import sanitize_name
        folder_name = sanitize_name(name)
        dest = os.path.join(local, folder_name)

        # In edit mode: keep original folder unless name was explicitly changed
        if is_edit and old_folder:
            old_meta_name = self._edit_item.name
            if name == old_meta_name:
                # Name unchanged - save in the original folder regardless
                # of folder name mismatch (e.g. duplicated folders)
                dest = old_folder

        # Every enabled input is guaranteed to have a write template selected
        # by the gate above; here we only check the chosen template still
        # exists on disk and is structurally valid.
        for p in all_params:
            if p.get('role') != 'input' or not p.get('enabled', True):
                continue
            tpl = p.get('write_template', '')
            src = (p.get('write_template_source') or '').strip()
            # A staged (Add-mode) workflow template isn't on disk yet; validate
            # its source file and let the Save-time write below commit it.
            if src == 'workflow' and tpl in self._pending_templates:
                ok, reason = _validate_nk_template_write_nodes(
                    self._pending_templates[tpl])
                if not ok:
                    _dialogs.warn(
                        self, 'Invalid write template',
                        'Workflow template "{}" for input "{}" is not '
                        'valid:\n\n{}'.format(
                            tpl, p.get('label', p.get('name', '?')), reason))
                    return
                continue
            # In an edit-rename the workflow's templates still live in the old
            # folder until the copytree below populates `dest`; check there.
            wf_tpl_dir = old_folder if (is_edit and old_folder) else dest
            wf_tpl = os.path.join(wf_tpl_dir, 'write_templates', tpl)
            global_tpl = os.path.join(_write_templates_dir(), tpl)
            if not os.path.isfile(global_tpl) and not os.path.isfile(wf_tpl):
                _dialogs.warn(
                    self, 'Write template not found',
                    'Write template "{}" for input "{}" was not found.\n\n'
                    "Place it in the global templates folder or in the "
                    "workflow's write_templates/ folder.".format(
                        tpl, p.get('label', p.get('name', '?'))))
                return
            # Validate the template the gizmo will actually paste - must
            # match the source the user explicitly chose.
            if src == 'global':
                resolved_tpl = global_tpl
            elif src == 'workflow':
                resolved_tpl = wf_tpl
            else:
                resolved_tpl = wf_tpl if os.path.isfile(wf_tpl) else global_tpl
            ok, reason = _validate_nk_template_write_nodes(resolved_tpl)
            if not ok:
                _dialogs.warn(
                    self, 'Invalid write template',
                    'Write template "{}" for input "{}" is not valid:\n\n'
                    '{}\n\n'
                    'Templates must contain exactly one Input node and one '
                    'Write node with an image-sequence format ({}).'.format(
                        tpl, p.get('label', p.get('name', '?')), reason,
                        ', '.join(sorted(_TEMPLATE_IMAGE_FORMATS))))
                return

        edit_in_place = is_edit and bool(old_folder) and (
            os.path.normpath(dest) == os.path.normpath(old_folder))
        # Same physical folder even when the string compare fails: a
        # case-only rename on a case-insensitive filesystem (Windows,
        # default macOS). Handled below as a plain os.rename that aligns
        # the folder's stored case with the chosen name.
        same_folder = edit_in_place or (
            is_edit and bool(old_folder) and _same_path(dest, old_folder))

        if os.path.isdir(dest) and not same_folder:
            # A rename onto another workflow's folder would copytree over
            # its workflow.json/metadata.json and then delete the source,
            # destroying an unrelated workflow - refuse outright. Add mode
            # keeps the merge prompt below: overwriting a same-name
            # workflow there is an explicit, informed choice.
            target_name = _existing_workflow_name_in(dest) if is_edit else None
            if target_name is not None:
                _dialogs.warn(
                    self, 'Name already in use',
                    'The folder "{}" already belongs to another workflow '
                    '("{}").\n\nRenaming would overwrite that workflow. '
                    'Choose a different name.'.format(
                        folder_name, target_name))
                return
            ans = _dialogs.ask(
                self, 'Folder already exists',
                'A folder named "{}" already exists in the workflow library.'
                '\n\nMerge into it? Files with the same name will be '
                'overwritten. Other existing files will be preserved.'.format(
                    folder_name),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ans != QtWidgets.QMessageBox.Yes:
                return

        # If editing and name changed, move the contents to the new folder.
        if is_edit and not edit_in_place and old_folder and os.path.isdir(old_folder):
            if same_folder:
                # Case-only rename: same physical folder, so a plain rename
                # updates its stored case. Release the Library's QMovie
                # handle first (an open preview.gif locks the folder on
                # Windows). On failure keep the existing case - cosmetic,
                # not worth failing the save.
                try:
                    from Nukomfy.gui.library_panel import LibraryPanel as _LP
                    if getattr(_LP, '_instance', None) is not None:
                        _LP._instance._gif.release_for_folder(old_folder)
                except Exception:
                    pass
                try:
                    os.rename(old_folder, dest)
                except OSError as e:
                    _log.warning(
                        'case-only folder rename to "%s" failed: %s', dest, e)
                    dest = old_folder
            else:
                # Copy + delete rather than os.rename: works cross-drive
                # and ensures _save() overwrites workflow.json /
                # metadata.json in dest.
                try:
                    shutil.copytree(old_folder, dest, dirs_exist_ok=True)
                except OSError as e:
                    _log.warning('failed to move workflow folder to "%s": %s',
                                 dest, e)
                    _dialogs.warn(
                        self, 'Workflow not renamed',
                        'The workflow folder could not be moved:\n\n{}\n\n'
                        'The original workflow is unchanged.'.format(e))
                    return
                # Gate the rename-cleanup rmtree behind workflow-folder
                # ownership (metadata.json + workflow.json present). A folder
                # without those Nukomfy markers is refused - we never wipe
                # an unrelated user folder even if the rename code path is
                # triggered with a wrong `old_folder` value.
                fs_safe.safe_delete_dir(old_folder, parent=self,
                                        action='workflow rename cleanup',
                                        sentinel_kind='workflow')
            # Update paths that pointed to the old folder
            if self._json_path and _same_path(
                    os.path.dirname(self._json_path), old_folder):
                self._json_path = os.path.join(
                    dest, os.path.basename(self._json_path))
            prev_text = self.prev_edit.text().strip()
            if prev_text and _same_path(
                    os.path.dirname(prev_text), old_folder):
                self.prev_edit.setText(
                    os.path.join(dest, os.path.basename(prev_text)))
            prev_b_text = self.prev_edit_b.text().strip()
            if prev_b_text and _same_path(
                    os.path.dirname(prev_b_text), old_folder):
                self.prev_edit_b.setText(
                    os.path.join(dest, os.path.basename(prev_b_text)))
        else:
            if not fs_safe.makedirs(dest, parent=self,
                                    action='save workflow'):
                return

        # Commit staged (Add-mode) workflow templates into the folder now that
        # `dest` exists - done after the folder-exists confirm so we never
        # trigger the merge prompt with our own folder. Validated above.
        if self._pending_templates:
            tpl_dir = os.path.join(dest, 'write_templates')
            if not fs_safe.makedirs(tpl_dir, parent=self,
                                    action='save workflow templates'):
                return
            for fname, src_path in list(self._pending_templates.items()):
                dst = os.path.join(tpl_dir, fname)
                tmp = dst + '.tmp'
                try:
                    shutil.copyfile(src_path, tmp)
                except OSError as e:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    _dialogs.warn(
                        self, 'Could not save template',
                        'Saving the workflow template "{}" failed:\n\n'
                        '{}'.format(fname, e))
                    return
                if not fs_safe.atomic_replace(tmp, dst):
                    _dialogs.warn(
                        self, 'Could not save template',
                        'Saving the workflow template "{}" failed (the file '
                        'may be locked).'.format(fname))
                    return
            self._pending_templates = {}

        wf_dest = os.path.join(dest, WORKFLOW_JSON)
        if not _same_path(self._json_path, wf_dest):
            try:
                shutil.copy2(self._json_path, wf_dest)
            except OSError as e:
                _log.warning('failed to copy workflow.json to "%s": %s',
                             wf_dest, e)
                _dialogs.warn(
                    self, 'Workflow not saved',
                    'The workflow file could not be written:\n\n{}\n\n'
                    'The workflow was not saved. Try again.'.format(e))
                return

        # Release any QMovie in the Library that holds an exclusive
        # lock on the existing preview.gif (Windows). Without
        # this, the safe_delete_file below fails with WinError 32 when
        # the workflow being edited is currently visible/animated.
        try:
            from Nukomfy.gui.library_panel import LibraryPanel as _LP
            if getattr(_LP, '_instance', None) is not None:
                _LP._instance._gif.release_for_folder(dest)
        except Exception:
            pass

        # Preview images - canonical naming by POSITION among present
        # sources, not by which column. One image is always preview.* (so
        # emptying one slot collapses to a single preview); two images
        # become preview.* + preview_b.*. Invariant: preview_b.* never
        # exists without preview.*. Sources come from prefill (already
        # canonical) or Browse (external files), so the only overwrite case
        # is a slot collapsing onto preview.* - handled copy-before-delete.
        from Nukomfy.gui import preview_thumb
        _slots = (('preview', preview_thumb._BASENAME),
                  ('preview_b', preview_thumb._BASENAME_B))
        _all_preview_names = (
            'preview.gif', 'preview.png', 'preview.jpg', 'preview.jpeg',
            'preview.webp',
            'preview_b.gif', 'preview_b.png', 'preview_b.jpg',
            'preview_b.jpeg', 'preview_b.webp')
        srcs = []
        for _edit in (self.prev_edit, self.prev_edit_b):
            _s = _edit.text().strip()
            if _s and os.path.isfile(_s):
                srcs.append(_s)

        # Resolve each present source to its canonical destination (by order).
        dests = []
        for i, src in enumerate(srcs):
            stem, thumb_name = _slots[i]
            ext = os.path.splitext(src)[1].lower() or '.png'
            dests.append(
                (src, os.path.join(dest, stem + ext), ext, thumb_name))
        desired = {os.path.normpath(d[1]) for d in dests}

        # Write sources to their destinations first (copy-before-delete: a
        # kept image may live under a name we're about to clean up, e.g.
        # collapsing preview_b.* down to preview.*). A write failure (disk
        # full, locked, read-only) is non-fatal: warn and let the save finish
        # without that preview rather than aborting the save mid-way.
        preview_copy_failed = []
        for src, dest_prev, ext, thumb_name in dests:
            if not _same_path(src, dest_prev):
                # Static images (PNG/JPG/WEBP) are re-encoded to a 512x512
                # square crop so cards stay uniform and disk usage is
                # bounded. GIFs have no multi-frame encoder (no PIL in
                # Nuke), so they raw-copy, preserving animation + size.
                try:
                    if not preview_thumb.resize_static_preview(src, dest_prev):
                        shutil.copy2(src, dest_prev)
                except OSError as e:
                    _log.warning('failed to write preview "%s": %s',
                                 dest_prev, e)
                    preview_copy_failed.append(os.path.basename(dest_prev))
                    continue
            # Co-located static thumb: GIF -> first-frame thumb; static
            # source -> none needed (already square-capped).
            if ext == '.gif' and preview_thumb.is_writable(dest):
                pix = preview_thumb.render_first_frame(dest_prev)
                if pix is not None:
                    preview_thumb.write_thumb(dest, pix, thumb_name)
                else:
                    preview_thumb.delete_thumb(dest, thumb_name)
            else:
                preview_thumb.delete_thumb(dest, thumb_name)
        if preview_copy_failed:
            _dialogs.warn(
                self, 'Preview not saved',
                'The preview image could not be written:\n\n{}\n\nThe '
                'workflow was saved without it.'.format(
                    '\n'.join(preview_copy_failed)))

        # Make the on-disk preview files match the present sources exactly:
        # remove any preview file that isn't a destination we just wrote.
        # Skipped entirely if a write failed above - otherwise a source that
        # lives in this folder (a collapse onto preview.*) could be deleted
        # even though its destination was never written, losing the only copy.
        # All deletions (including emptying a slot via Reset) happen here, at
        # save - so cancelling the dialog never touches disk. NOTE: the loop
        # var must NOT be `name` - that would shadow the workflow name read
        # above and corrupt metadata['name']. safe_delete_file gates on the
        # expected basename and refuses symlinks.
        if not preview_copy_failed:
            for prev_name in _all_preview_names:
                old_path = os.path.join(dest, prev_name)
                if (os.path.isfile(old_path)
                        and os.path.normpath(old_path) not in desired):
                    fs_safe.safe_delete_file(
                        old_path, expected_basename=prev_name,
                        parent=self, action='remove old preview')

        # Drop thumbs for slots with no image (went from 2 -> 1, or 0).
        for i in range(len(srcs), len(_slots)):
            preview_thumb.delete_thumb(dest, _slots[i][1])

        # Title logo (PNG). A pending upload is copied in; a pending clear
        # (Reset) removes the on-disk file. Both happen only here, at save -
        # cancelling the dialog leaves gizmo_logo.png untouched.
        logo_dest = os.path.join(dest, 'gizmo_logo.png')
        if (self._title_mode == 'use_custom_logo'
                and self._title_logo_path_pending
                and os.path.isfile(self._title_logo_path_pending)):
            try:
                shutil.copy2(self._title_logo_path_pending, logo_dest)
            except OSError as e:
                _log.warning('failed to copy logo to "%s": %s', logo_dest, e)
                _dialogs.warn(
                    self, 'Title logo not saved',
                    'The title logo could not be copied:\n\n{}\n\nThe '
                    'gizmo will show the text title instead. You can '
                    're-add the logo by editing the workflow.'.format(e))
        elif self._title_logo_clear_pending and os.path.isfile(logo_dest):
            # Reset marked the logo for removal: delete at save. Basename
            # gate plus symlink refusal for safety.
            fs_safe.safe_delete_file(
                logo_dest, expected_basename='gizmo_logo.png',
                parent=self, action='remove title logo')
        self._title_logo_clear_pending = False
        self._title_logo_path_pending = None

        tags_cat = [t.strip() for t in self.tags_cat_edit.text().split(',')
                    if t.strip()]
        tags_mod = [t.strip() for t in self.tags_mod_edit.text().split(',')
                    if t.strip()]
        created = datetime.date.today().isoformat()
        workflow_id = ''
        if self._edit_item:
            # After rename, old folder is gone - read from dest (copytree
            # already copied metadata.json there)
            old_meta_path = os.path.join(dest, METADATA_JSON)
            try:
                with open(old_meta_path, 'r', encoding='utf-8') as f:
                    old_meta = json.load(f)
                created = old_meta.get('created', created)
                workflow_id = old_meta.get('workflow_id', '') or ''
            except Exception:
                pass
        if not workflow_id:
            workflow_id = uuid.uuid4().hex[:12]

        version = '{}.{}.{}'.format(
            int(self.ver_major.text() or '0'),
            int(self.ver_minor.text() or '0'),
            int(self.ver_patch.text() or '0'))

        meta = {
            'name':          name,
            'workflow_alias': workflow_alias,
            'workflow_id':   workflow_id,
            'description':   self.desc_edit.toPlainText().strip(),
            'version':       version,
            'author':        self.author_edit.text().strip(),
            'usage':         self.usage_edit.toPlainText().strip(),
            'tags_category': tags_cat,
            'tags_models':   tags_mod,
            'created':       created,
            # Snapshot-based persistence: see workflow_state.py. The
            # snapshot is the server-authoritative widget set from the
            # last Sync, _overrides the user delta, _widget_order the
            # UI row layout, _v3_user_edits the per-option sub-value
            # cache for V3 dynamic combo masters.
            '_snapshot':       self._snapshot,
            '_overrides':      self._overrides,
            '_widget_order':   self._widget_order,
            '_v3_user_edits':  self._v3_user_edits,
            'gizmo_color':   self._gizmo_color,
            'gizmo_options': {
                'title':        self._opt_title.isChecked(),
                'versioning':   self._opt_versioning.isChecked(),
                'author':       self._opt_author.isChecked(),
                'description':  self._opt_desc.isChecked(),
                'usage':        self._opt_usage.isChecked(),
                'output_preview': self._opt_output_preview.isChecked(),
                'title_mode':   self._title_mode,
                'title_color':  (None if self._title_color_linked else
                                 '#{:06X}'.format(
                                     (self._title_color >> 8) & 0xFFFFFF)),
                'title_node_color': (None if not self._title_node_color else
                                     '#{:06X}'.format(
                                         (self._title_node_color >> 8) & 0xFFFFFF)),
                'color_reads':  self._opt_color_reads.isChecked(),
                'word_wrap':    self._opt_word_wrap.isChecked(),
            },
        }

        # Workflow integrity hash (logical, strips cosmetic fields).
        try:
            from Nukomfy.workflows.workflow_loader import workflow_logical_hash
            meta['workflow_hash'] = workflow_logical_hash(wf_dest)
        except Exception:
            meta['workflow_hash'] = ''

        try:
            meta_path = os.path.join(dest, METADATA_JSON)
            tmp = meta_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            if not fs_safe.atomic_replace(tmp, meta_path):
                raise OSError('could not write the file: {}'.format(meta_path))
        except Exception as e:
            _dialogs.critical(
                self, 'Metadata write failed',
                'The workflow metadata could not be saved.\n\n{}'.format(e))
            return

        self.accept()