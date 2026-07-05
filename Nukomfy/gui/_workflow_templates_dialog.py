"""Manage Workflow Templates dialog.

Modal, buffered add/remove editor for a workflow's own write templates,
opened from the Write Template combo in the Workflow Editor. Add a `.nk`
(validated, then copied or staged) or remove existing ones; every edit is
applied on Save and discarded on Cancel. Style mirrors the Shared Workflow
Folders dialog (modal, header + Add button, scrollable list, Save/Cancel).

Internal module - public entry point is `ManageWorkflowTemplatesDialog`.
"""

import os
import shutil

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui
from Nukomfy.gui import _dialogs
from Nukomfy.utils import fs_safe
from Nukomfy.gui._theme import SCROLLBAR_STYLE, apply_window_chrome
from Nukomfy.gui.icons import icon_font, set_press_icon, ADD, CLOSE
from Nukomfy.gui.add_workflow_parser import (
    _list_write_templates,
    _validate_nk_template_write_nodes,
    _TEMPLATE_IMAGE_FORMATS,
)


class ManageWorkflowTemplatesDialog(QtWidgets.QDialog):
    """Buffered editor for a workflow's own write templates.

    `workflow_dir` is the workflow's on-disk folder, or None for a workflow
    that has never been saved (Add mode). On Save:
      - saved workflow  -> new `.nk` files are written and removed ones are
        deleted in `<workflow_dir>/write_templates/` immediately;
      - new workflow    -> only the staged set is updated (the files are
        written at the workflow's own Save).
    Cancel discards every edit. After `exec_() == Accepted`, the caller reads
    `result_pending()` (staged {filename: source_path}) and
    `removed_filenames()` (so rows pointing at a removed template reset).

    Each working row carries (container, filename, source_path): source_path
    is the picked file for a newly added template, the incoming staged path
    for an existing Add-mode template, or None for one already on disk.
    """

    def __init__(self, workflow_dir, pending, parent=None):
        super().__init__(parent)
        self._workflow_dir = workflow_dir
        self._incoming_pending = dict(pending or {})
        self._result_pending = dict(self._incoming_pending)
        self._removed = set()

        self.setWindowTitle('Manage Workflow Templates')
        self.setModal(True)
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        apply_window_chrome(self)

        # Two short sentences, the second on its own line. The dialog opens
        # exactly as wide as the longest line needs and no wider (never
        # persisted), then is centred on the parent window. Resizable both ways.
        desc_text = ('Manage write templates specific to this workflow.\n'
                     'Global templates are not affected.')
        self._empty_text = 'No templates yet. Use Add Template to add one.'
        # Width must hold the longest description line (28px of root margins)
        # AND the empty-state line, which sits deeper (group border + padding +
        # scroll inset ~52px), so neither is clipped. Measured at the real 11px
        # text size, not the larger default dialog font. Never persisted.
        font11 = QtGui.QFont(self.font())
        font11.setPixelSize(11)
        fm = QtGui.QFontMetrics(font11)
        desc_w = max(fm.horizontalAdvance(line)
                     for line in desc_text.split('\n'))
        empty_w = fm.horizontalAdvance(self._empty_text)
        width = max(desc_w + 36, empty_w + 58)
        self.setMinimumSize(width, 180)
        self.resize(width, 240)
        if self.parent() is not None:
            geo = self.parent().window().geometry()
            self.move(geo.x() + (geo.width() - self.width()) // 2,
                      geo.y() + (geo.height() - self.height()) // 2)

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # Description on its own full-width line. The Add button lives in the
        # footer (bottom-left), so the text never wraps around it and the
        # dialog stays as narrow as the longest description line.
        desc = QtWidgets.QLabel(desc_text)
        desc.setStyleSheet('color:#888;font-size:11px;')
        desc.setWordWrap(True)
        root.addWidget(desc)

        # Scrollable list (same container look + app scrollbar as Shared).
        grp = QtWidgets.QGroupBox()
        grp.setStyleSheet(
            'QGroupBox{border:1px solid #3a3a3a;border-radius:3px;'
            'margin-top:0px;padding:8px;}')
        grp_lay = QtWidgets.QVBoxLayout(grp)
        grp_lay.setContentsMargins(0, 0, 0, 0)
        grp_lay.setSpacing(0)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            'QScrollArea{background:transparent;border:none;}'
            + SCROLLBAR_STYLE)
        inner = QtWidgets.QWidget()
        self._inner_lay = QtWidgets.QVBoxLayout(inner)
        self._inner_lay.setContentsMargins(0, 0, 6, 0)
        self._inner_lay.setSpacing(8)
        self._empty_lbl = QtWidgets.QLabel(self._empty_text)
        self._empty_lbl.setStyleSheet('color:#666;font-size:11px;')
        self._empty_lbl.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop)
        self._inner_lay.addWidget(self._empty_lbl)
        self._inner_lay.addStretch(1)  # rows insert before this stretch
        self._scroll.setWidget(inner)
        grp_lay.addWidget(self._scroll)
        root.addWidget(grp, 1)

        # Footer: Add Template (left) | Save (apply) / Cancel (discard) (right).
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(8)
        add_btn = QtWidgets.QPushButton(' Add Template')
        set_press_icon(add_btn, ADD)
        add_btn.setFixedHeight(24)
        add_btn.clicked.connect(self._on_add_clicked)
        footer.addWidget(add_btn)
        footer.addStretch()
        save_btn = QtWidgets.QPushButton('Save')
        save_btn.setFixedHeight(24)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save_clicked)
        cancel_btn = QtWidgets.QPushButton('Cancel')
        cancel_btn.setFixedHeight(24)
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(save_btn)
        footer.addWidget(cancel_btn)
        root.addLayout(footer)

        # Working rows + the set present at open (so Save computes removals).
        self._rows = []
        self._initial_filenames = set()
        for fname in self._current_filenames():
            self._add_row(fname, self._incoming_pending.get(fname))
            self._initial_filenames.add(fname)
        self._update_empty()

    def _current_filenames(self):
        """The workflow's existing templates (on disk, or staged in Add mode)."""
        if self._workflow_dir:
            return [f for _d, f, s in _list_write_templates(self._workflow_dir)
                    if s == 'workflow']
        return sorted(self._incoming_pending)

    def _add_row(self, filename, source_path):
        container = QtWidgets.QWidget()
        cl = QtWidgets.QHBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        lbl = QtWidgets.QLabel(os.path.splitext(filename)[0])
        lbl.setToolTip(filename)
        cl.addWidget(lbl, 1)
        rm_btn = QtWidgets.QPushButton(CLOSE)
        rm_btn.setFont(icon_font(14))
        rm_btn.setFixedSize(26, 26)
        rm_btn.setToolTip('Remove this template')
        rm_btn.setStyleSheet('QPushButton{color:#ccc;}')
        rm_btn.clicked.connect(lambda: self._remove_row(container))
        cl.addWidget(rm_btn)
        cl.setAlignment(rm_btn, QtCore.Qt.AlignTop)
        self._inner_lay.insertWidget(self._inner_lay.count() - 1, container)
        self._rows.append([container, filename, source_path])
        self._update_empty()

    def _remove_row(self, container):
        self._rows = [r for r in self._rows if r[0] is not container]
        self._inner_lay.removeWidget(container)
        container.deleteLater()
        self._update_empty()

    def _update_empty(self):
        self._empty_lbl.setVisible(not self._rows)

    def _on_add_clicked(self):
        path = _dialogs.get_open_file(
            self, 'Add Workflow Template', '', 'Nuke scripts (*.nk)')
        if not path:
            return
        if not path.lower().endswith('.nk'):
            ok, reason = False, 'Not a Nuke script (.nk) file'
        else:
            ok, reason = _validate_nk_template_write_nodes(path)
        if not ok:
            _dialogs.warn(
                self, 'Invalid write template',
                'This file can\'t be used as a write template:\n\n{}\n\n'
                'A template must contain exactly one Input node and one Write '
                'node with an image-sequence format ({}).'.format(
                    reason, ', '.join(sorted(_TEMPLATE_IMAGE_FORMATS))))
            return
        name = os.path.basename(path)
        existing = next((r for r in self._rows if r[1] == name), None)
        if existing is not None:
            ans = _dialogs.ask(
                self, 'Template already exists',
                'A template named "{}" is already in the list.\n\n'
                'Replace it?'.format(name),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ans == QtWidgets.QMessageBox.Yes:
                existing[2] = path  # overwrite this template's source at Save
            return
        self._add_row(name, path)

    def _on_save_clicked(self):
        kept = {r[1] for r in self._rows}
        removed = self._initial_filenames - kept
        if self._workflow_dir:
            if not self._apply_to_disk(removed):
                return  # error already shown; keep the dialog open
            self._result_pending = {}
        else:
            self._result_pending = {r[1]: r[2] for r in self._rows}
        self._removed = removed
        self.accept()

    def _apply_to_disk(self, removed):
        """Saved workflow: write the new templates and delete the removed
        ones. Returns False (keeping the dialog open) on a hard write error;
        a failed delete is reported by fs_safe but does not block."""
        tpl_dir = os.path.join(self._workflow_dir, 'write_templates')
        new_rows = [r for r in self._rows if r[2] is not None]
        if new_rows and not fs_safe.makedirs(
                tpl_dir, parent=self, action='add workflow template'):
            return False
        for _c, fname, src in new_rows:
            dst = os.path.join(tpl_dir, fname)
            tmp = dst + '.tmp'
            try:
                shutil.copyfile(src, tmp)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                _dialogs.warn(
                    self, 'Could not add template',
                    'Copying "{}" failed:\n\n{}'.format(fname, e))
                return False
            if not fs_safe.atomic_replace(tmp, dst):
                _dialogs.warn(
                    self, 'Could not add template',
                    'Saving "{}" failed (the file may be locked).'.format(
                        fname))
                return False
        for fname in removed:
            fs_safe.safe_delete_file(
                os.path.join(tpl_dir, fname), expected_basename=fname,
                parent=self, action='remove workflow template')
        # Drop the folder if nothing is left in it.
        try:
            if os.path.isdir(tpl_dir) and not os.listdir(tpl_dir):
                os.rmdir(tpl_dir)
        except OSError:
            pass
        return True

    def result_pending(self):
        """Updated staged {filename: source_path} (empty for a saved workflow)."""
        return self._result_pending

    def removed_filenames(self):
        """Filenames removed in this session (so their rows reset to Select…)."""
        return self._removed
