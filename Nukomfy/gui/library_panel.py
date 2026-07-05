"""Workflow browser with grid/list view, animated previews, and tag filtering.

Uses QListView + QStyledItemDelegate for virtualized rendering.
"""

import json
import logging
import os

from Nukomfy.utils.qt_compat import QtWidgets, QtCore, QtGui, _nuke_main_window
from Nukomfy.gui._fields import NukomfyLineEdit
from Nukomfy.gui import _dialogs

_log = logging.getLogger(__name__)

from Nukomfy.core.settings import settings
from Nukomfy.gui.ui_state import ui_state
from Nukomfy.gui.icons import (icon_font, material_icon, set_press_icon,
                   GRID_VIEW, VIEW_LIST,
                   PLAY_ARROW, PAUSE, REFRESH, STAR, STAR_BORDER,
                   SEARCH, ADD, CHECK_BOX, CHECK_BOX_BLANK,
                   WARNING, MOVIE, ARROW_UPWARD, ARROW_DOWNWARD)
import Nukomfy.workflows.workflow_loader as workflow_loader
from Nukomfy.gui import preview_thumb
from Nukomfy.gui._splitter import DottedSplitter
from Nukomfy.gui._no_wheel import NoWheelComboBox, NoWheelSlider

# Minimum width (px) of the library sidebar; also the floor used when
# computing or restoring the sidebar width.
_SIDEBAR_MIN_W = 170

# ---------------------------------------------------------------------------
# Favorites persistence
# ---------------------------------------------------------------------------
_FAV_FILE = os.path.join(os.path.expanduser('~'), '.nuke',
                         'nukomfy_favorites.json')


def _load_favorites():
    """Load favorite workflow UUIDs from disk."""
    if not os.path.isfile(_FAV_FILE):
        return set()
    try:
        with open(_FAV_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except Exception:
        pass
    return set()


def _save_favorites(favorites):
    """Save favorite workflow UUIDs to disk."""
    import Nukomfy.utils.fs_safe as fs_safe
    if not fs_safe.makedirs_silent(os.path.dirname(_FAV_FILE)):
        return
    tmp = _FAV_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sorted(favorites), f, indent=2)
        fs_safe.atomic_replace(tmp, _FAV_FILE)
    except OSError as e:
        _log.warning('failed to save favorites: %s', e)

THUMB_SIZE   = 200
GRID_SPACING = 8
from Nukomfy.gui._theme import (
    SCROLLBAR_STYLE, apply_window_chrome, ACCENT_GOLD, SEARCH_FIELD_STYLE,
    LIBRARY_CAT_BG, LIBRARY_CAT_FG, LIBRARY_CAT_TITLE,
    LIBRARY_MOD_BG, LIBRARY_MOD_FG, LIBRARY_MOD_TITLE,
    LIBRARY_LOCAL_BG, LIBRARY_LOCAL_FG,
    LIBRARY_SHARED_BG, LIBRARY_SHARED_FG,
    LIBRARY_DUP_BG, LIBRARY_DUP_FG,
    LIBRARY_BADGE_FONT_PX,
)
from Nukomfy.gui import _focus_drop

# Toolbar icon-button style (reload, sort direction): dark with a gold
# hover accent.
_TOOLBAR_ICON_BTN_STYLE = (
    'QPushButton{background:#1e1e1e;color:#888;border:1px solid #444;'
    'border-radius:3px;}'
    'QPushButton:hover{color:' + ACCENT_GOLD + ';border-color:#666;}'
    'QPushButton:pressed{background:#151515;}')

_pixmap_cache = {}   # preview_path -> {thumb_size: QPixmap}

# Tag badge colors  (background, text) - see Nukomfy/gui/_theme.py for hex.
_COLOR_CAT     = (LIBRARY_CAT_BG, LIBRARY_CAT_FG)
_COLOR_MOD     = (LIBRARY_MOD_BG, LIBRARY_MOD_FG)
_COLOR_SRC     = {'Local': LIBRARY_LOCAL_BG, 'Shared': LIBRARY_SHARED_BG}
_COLOR_SRC_TXT = {'Local': LIBRARY_LOCAL_FG, 'Shared': LIBRARY_SHARED_FG}
_COLOR_DUP     = (LIBRARY_DUP_BG, LIBRARY_DUP_FG)


def _display_name_for(item, name_dups, name_folder_dups):
    """Return the card title for `item`, disambiguating on collision.

    - If metadata.name is unique: return item.name as-is.
    - If two workflows share the name: append ' (<folder_basename>)'.
    - If name+basename also collide (rare): ' (<source>/<folder_basename>)'.
    """
    base = os.path.basename(item.folder_path)
    if item.name not in name_dups:
        return item.name
    key = (item.name, base)
    if key not in name_folder_dups:
        return '{} ({})'.format(item.name, base)
    return '{} ({}/{})'.format(item.name, item.source, base)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cached_pixmap(path, thumb):
    """Return a centre-cropped pixmap from cache, or load and cache it."""
    entry = _pixmap_cache.get(path)
    if entry and thumb in entry:
        return entry[thumb]
    raw = QtGui.QPixmap(path)
    if raw.isNull():
        return None
    px = raw.scaled(thumb, thumb,
                    QtCore.Qt.KeepAspectRatioByExpanding,
                    QtCore.Qt.SmoothTransformation)
    if px.width() > thumb or px.height() > thumb:
        x = (px.width() - thumb) // 2
        y = (px.height() - thumb) // 2
        px = px.copy(x, y, thumb, thumb)
    if path not in _pixmap_cache:
        _pixmap_cache[path] = {}
    _pixmap_cache[path][thumb] = px
    return px


def _gif_first_frame_pixmap(folder, thumb, basename=preview_thumb._BASENAME):
    """Return the static first-frame pixmap for a workflow's GIF preview,
    sourced from the co-located preview_thumb.webp file. Multi-tier:
    in-memory _pixmap_cache then disk lookup. `basename` selects the slot
    (primary preview_thumb.webp or the comparison preview_b_thumb.webp).

    Cache key includes the disk thumb's mtime so a regenerated thumb
    automatically invalidates the in-memory entry.
    """
    disk_path = preview_thumb.existing_thumb(folder, basename)
    if not disk_path:
        return None
    try:
        mtime = os.stat(disk_path).st_mtime_ns
    except OSError:
        return None
    cache_key = (mtime, thumb)
    entry = _pixmap_cache.get(disk_path)
    if entry and cache_key in entry:
        return entry[cache_key]
    raw = QtGui.QPixmap(disk_path)
    if raw.isNull():
        return None
    px = raw.scaled(thumb, thumb,
                    QtCore.Qt.KeepAspectRatioByExpanding,
                    QtCore.Qt.SmoothTransformation)
    if px.width() > thumb or px.height() > thumb:
        x = (px.width() - thumb) // 2
        y = (px.height() - thumb) // 2
        px = px.copy(x, y, thumb, thumb)
    if disk_path not in _pixmap_cache:
        _pixmap_cache[disk_path] = {}
    _pixmap_cache[disk_path][cache_key] = px
    return px


_PLACEHOLDER_PX = {}  # thumb_size -> QPixmap


def _placeholder_pixmap(thumb):
    """Neutral placeholder for GIF cards whose static thumb hasn't been
    generated yet (first scan, or read-only Shared folder without one).
    Cached per thumb size."""
    px = _PLACEHOLDER_PX.get(thumb)
    if px is not None:
        return px
    px = QtGui.QPixmap(thumb, thumb)
    px.fill(QtGui.QColor('#1a1a1a'))
    p = QtGui.QPainter(px)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setRenderHint(QtGui.QPainter.TextAntialiasing)
    p.setPen(QtGui.QColor('#444'))
    f = QtGui.QFont('Material Icons')
    f.setPixelSize(int(thumb * 0.25))
    p.setFont(f)
    p.drawText(px.rect(), QtCore.Qt.AlignCenter, MOVIE)
    p.end()
    _PLACEHOLDER_PX[thumb] = px
    return px


def _text_width(fm, text):
    try:
        return fm.horizontalAdvance(text)
    except AttributeError:
        return fm.width(text)


def _split_key(item):
    """Stable per-card key for the comparison slider position. workflow_id
    survives re-sort/filter; folder_path is the fallback when unset."""
    return item.workflow_id or item.folder_path


def _gif_paths(item):
    """Animatable GIF paths for a card: the primary preview plus the
    optional comparison second image. Both feed the same shared animation
    machinery (autoplay / hover / viewport culling)."""
    return [p for p in (item.preview_path, item.preview_path_b)
            if p and p.endswith('.gif')]


# ---------------------------------------------------------------------------
# Model - flat list of WorkflowItem objects
# ---------------------------------------------------------------------------
class _WorkflowModel(QtCore.QAbstractListModel):
    ItemRole = QtCore.Qt.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []

    def set_items(self, items):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return len(self._items)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        if role == self.ItemRole:
            return self._items[index.row()]
        return None

    def item_at(self, row):
        if 0 <= row < len(self._items):
            return self._items[row]
        return None




# ---------------------------------------------------------------------------
# GIF Manager - runs QMovie instances, triggers model repaints per-frame
# ---------------------------------------------------------------------------
class _GifManager(QtCore.QObject):
    """Lazy QMovie manager with viewport culling.

    - QMovie instances are created on-demand only for cards currently
      visible in the viewport. With autoplay ON, scrolling pauses out
      of view movies (`setPaused(True)`) and resumes / creates movies
      entering the viewport (after a 100ms scroll-stop debounce).
    - With autoplay OFF, only hover_start creates a single QMovie
      (after a 200ms grace timer to avoid thrash on fast pan); leave
      pauses it immediately.
    - frameChanged is connected only while a movie is running, so the
      ~30 FPS signal storm is bounded by the visible card count
      (typically 5-15) instead of the total filtered count.
    - LRU cap on total live QMovies (`_max_movies`) keeps memory
      bounded for very large libraries.
    - Static thumbs missing from disk are scheduled in background via
      ThumbnailWorker (Nukomfy.gui.workers); orphan thumbs (gif removed)
      are deleted in the same worker. Read-only Shared folders are
      skipped silently.
    """

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self._model = model
        self._view = None
        self._movies = {}            # gif_path -> QMovie
        self._path_to_rows = {}      # gif_path -> set(row)
        self._known_paths = []       # gif_path list in display order
        self._lru = []               # MRU at end; same paths as _movies
        self._max_movies = 200       # hard cap to keep memory bounded
        self._visible_paths = set()
        self._autoplay = True
        self._thumb_size = THUMB_SIZE
        self._thumb_worker = None
        # Debounce scrolling to avoid recomputing visibility on every pixel
        self._scroll_debounce = QtCore.QTimer(self)
        self._scroll_debounce.setSingleShot(True)
        self._scroll_debounce.setInterval(100)
        self._scroll_debounce.timeout.connect(self._recompute_visibility)
        # Hover grace: don't fire animations on fast cursor passes
        self._hover_grace = QtCore.QTimer(self)
        self._hover_grace.setSingleShot(True)
        self._hover_grace.setInterval(200)
        self._hover_grace.timeout.connect(self._on_hover_grace_fire)
        self._pending_hover_paths = set()

    # ---- view wiring ----------------------------------------------
    def attach_view(self, view):
        """Connect the QListView so we can listen to scroll events.

        Also listens to rangeChanged on the vertical scrollbar: Qt fires
        it whenever the content extent changes, which happens when
        QListView IconMode finishes the (sometimes deferred) wrap layout
        for the first time. Without this hook the cards painted before
        the final layout pass would never enter the visible set.
        """
        self._view = view
        try:
            sb = view.verticalScrollBar()
            sb.valueChanged.connect(
                lambda _v: self._scroll_debounce.start())
            sb.rangeChanged.connect(
                lambda _mn, _mx: self._scroll_debounce.start())
        except Exception:
            pass

    # ---- public API -----------------------------------------------
    def update(self, items, thumb_size):
        """Sync managed paths with the current filtered item list.

        Does NOT eagerly create QMovie instances - that's deferred to
        `_recompute_visibility()` (called via 0ms singleShot below).
        Schedules background generation/cleanup of co-located static
        thumbs for missing/orphan cases.
        """
        self._thumb_size = thumb_size
        self._known_paths = []
        self._path_to_rows.clear()
        needed = set()
        regen_items = []     # list[(folder, gif_path)]
        delete_folders = []  # list[folder]

        for i, item in enumerate(items):
            folder = item.folder_path
            gif = item.preview_path if (item.preview_path
                                        and item.preview_path.endswith('.gif')) else None
            gif_b = item.preview_path_b if (item.preview_path_b
                                            and item.preview_path_b.endswith('.gif')) else None
            # Register every GIF for animation - the primary preview and the
            # optional comparison second image both map to the same row, so
            # autoplay / hover / viewport culling drive them together and the
            # slider shows live frames per side.
            for g in (gif, gif_b):
                if g:
                    self._known_paths.append(g)
                    self._path_to_rows.setdefault(g, set()).add(i)
                    needed.add(g)
            # Background (re)generation of the static first-frame thumb is for
            # the primary slot only (the worker writes preview_thumb.webp); the
            # comparison slot's thumb is written at save time.
            if gif:
                if (preview_thumb.needs_regeneration(folder, gif)
                        and preview_thumb.is_writable(folder)):
                    regen_items.append((folder, gif))
            else:
                # No primary GIF: if a stale orphan thumb exists, clean it up.
                if (preview_thumb.needs_deletion(folder, has_gif=False)
                        and preview_thumb.is_writable(folder)):
                    delete_folders.append(folder)

        # Drop QMovies for paths no longer in scope (filter changes).
        # Surviving movies are re-scaled with aspect ratio preserved
        # (same logic as _aspect_scaled_size used at creation) so a
        # scale-slider change doesn't squash non-square frames.
        for path in list(self._movies):
            if path not in needed:
                self._evict_movie(path)
            else:
                self._movies[path].setScaledSize(
                    self._aspect_scaled_size(path))

        if regen_items or delete_folders:
            self._schedule_thumb_work(regen_items, delete_folders)

        # Recompute visibility deferred 0ms so the view has finished
        # laying out the model reset before we ask for visualRect.
        QtCore.QTimer.singleShot(0, self._recompute_visibility)

    def set_autoplay(self, enabled):
        """Toggle autoplay. When ON: visible cards animate (others stay
        paused). When OFF: everything pauses; hover drives single-card
        animation."""
        self._autoplay = enabled
        if enabled:
            for path in self._visible_paths:
                self._ensure_movie_running(path)
        else:
            for path, m in list(self._movies.items()):
                if m.state() == QtGui.QMovie.Running:
                    m.setPaused(True)
                    self._safe_disconnect_frame(m)
            if self._view:
                try:
                    self._view.viewport().update()
                except RuntimeError:
                    pass

    def hover_start(self, path):
        """Arm the grace timer; if not preempted by a hover_stop within
        200ms, animate the hovered card. A comparison card arms both of its
        GIFs (primary + second image)."""
        if self._autoplay:
            return
        self._pending_hover_paths.add(path)
        self._hover_grace.start()

    def hover_stop(self, path):
        """Cancel pending hover-start and pause any running movie for
        this path. No grace on leave - we want the card to settle
        instantly when the cursor moves out."""
        self._pending_hover_paths.discard(path)
        if not self._pending_hover_paths:
            self._hover_grace.stop()
        if self._autoplay:
            return
        m = self._movies.get(path)
        if m and m.state() == QtGui.QMovie.Running:
            m.setPaused(True)
            self._safe_disconnect_frame(m)

    def pixmap_if_running(self, path):
        """Current animated frame iff QMovie for *path* is running.

        Center-crops to a square matching `_thumb_size` so the framing
        is identical to the static preview_thumb.webp (which uses
        KeepAspectRatioByExpanding + center crop). Otherwise QMovie
        scaledSize would stretch a non-square GIF and the animated
        view would look distorted compared to its static thumb."""
        m = self._movies.get(path)
        if not m or m.state() != QtGui.QMovie.Running:
            return None
        px = m.currentPixmap()
        if px is None or px.isNull():
            return None
        target = self._thumb_size
        if px.width() != target or px.height() != target:
            x = max(0, (px.width() - target) // 2)
            y = max(0, (px.height() - target) // 2)
            w = min(target, px.width())
            h = min(target, px.height())
            px = px.copy(x, y, w, h)
        return px

    def _aspect_scaled_size(self, path):
        """Compute QSize for QMovie.setScaledSize that preserves the
        GIF's aspect ratio with the short side equal to thumb_size.
        Frames are then cropped to a square at paint time."""
        target = self._thumb_size
        try:
            reader = QtGui.QImageReader(path)
            reader.setDecideFormatFromContent(True)
            src = reader.size()
        except Exception:
            src = QtCore.QSize()
        if (not src.isValid()
                or src.width() <= 0 or src.height() <= 0):
            return QtCore.QSize(target, target)
        sw, sh = src.width(), src.height()
        short = min(sw, sh)
        scale = target / float(short)
        return QtCore.QSize(int(round(sw * scale)),
                            int(round(sh * scale)))

    def release_for_folder(self, folder):
        """Stop and free any QMovie whose source GIF lives under *folder*.

        On Windows a running QMovie holds an exclusive lock on the GIF
        file; the Workflow Editor cannot overwrite or delete the
        preview while that lock is active (WinError 32). Callers
        (typically add_workflow.py before _save touches preview files)
        invoke this to release the lock first.

        QMovie.deleteLater() is asynchronous, so a plain stop+drop
        leaves the file handle open until Qt's deferred-deletion pass.
        We have to (a) reset the file name so the underlying QImageReader
        closes the device synchronously, and (b) flush any pending
        DeferredDelete events before returning.
        """
        folder_norm = os.path.normcase(os.path.normpath(folder))
        paths_to_drop = []
        for p in list(self._movies):
            try:
                p_dir = os.path.normcase(os.path.normpath(os.path.dirname(p)))
            except (TypeError, ValueError):
                continue
            if p_dir == folder_norm:
                paths_to_drop.append(p)
        for p in paths_to_drop:
            m = self._movies.pop(p, None)
            if m is not None:
                self._safe_disconnect_frame(m)
                try:
                    m.stop()
                except Exception:
                    pass
                # Reset filename to force the internal QImageReader to
                # close its file device immediately.
                try:
                    m.setFileName('')
                except Exception:
                    pass
                try:
                    m.setParent(None)
                except Exception:
                    pass
                m.deleteLater()
            try:
                self._lru.remove(p)
            except ValueError:
                pass
            self._visible_paths.discard(p)
        # Drain pending DeferredDelete events so the QMovie destructors
        # actually run before the caller touches the filesystem.
        try:
            app = QtCore.QCoreApplication.instance()
            if app is not None:
                app.processEvents(QtCore.QEventLoop.AllEvents, 50)
                app.sendPostedEvents(None,
                                     QtCore.QEvent.DeferredDelete)
        except Exception:
            pass

    def stop_all(self):
        """Tear down all state. Called from LibraryPanel.closeEvent."""
        self._view = None
        self._scroll_debounce.stop()
        self._hover_grace.stop()
        if self._thumb_worker is not None:
            from Nukomfy.gui.workers import stop_worker
            # Disconnect custom thumbReady before stop_worker(): the generic
            # helper only handles common signal names; queued emits would
            # otherwise reach _on_thumb_ready on a partially-destroyed view.
            try:
                self._thumb_worker.thumbReady.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._thumb_worker = stop_worker(self._thumb_worker)
        for m in self._movies.values():
            self._safe_disconnect_frame(m)
            m.stop()
        self._movies.clear()
        self._lru.clear()
        self._path_to_rows.clear()
        self._known_paths = []
        self._visible_paths.clear()

    # ---- internals ------------------------------------------------
    def _on_hover_grace_fire(self):
        if not self._pending_hover_paths or self._autoplay:
            return
        # Hover always restarts from frame 0 - feels more like a deliberate
        # "play this preview" gesture than resuming where a previous hover
        # left off. A comparison card plays both of its GIFs.
        for path in list(self._pending_hover_paths):
            self._ensure_movie_running(path, from_start=True)
            if self._view:
                try:
                    for row in self._path_to_rows.get(path, ()):
                        self._view.update(self._model.index(row, 0))
                except RuntimeError:
                    pass

    def _recompute_visibility(self):
        """Compute the set of paths whose card intersects the current
        viewport. Pause QMovies leaving, resume/create movies entering
        (only when autoplay is ON).

        Iterates `_path_to_rows` (path -> {real_model_rows}) so we don't
        confuse the index of a path inside `_known_paths` with the
        actual model row. Workflows without a GIF preview (PNG/JPG/no
        preview) shift the model rows relative to the GIF-only list, so
        `_known_paths` order does not match the model row order.
        """
        if not self._view:
            return
        try:
            vp = self._view.viewport()
            vp_rect = vp.rect()
        except RuntimeError:
            return
        new_visible = set()
        for path, rows in self._path_to_rows.items():
            for row in rows:
                idx = self._model.index(row, 0)
                r = self._view.visualRect(idx)
                if not r.isEmpty() and r.intersects(vp_rect):
                    new_visible.add(path)
                    break
        old_visible = self._visible_paths
        self._visible_paths = new_visible
        for path in (old_visible - new_visible):
            m = self._movies.get(path)
            if m and m.state() == QtGui.QMovie.Running:
                m.setPaused(True)
                self._safe_disconnect_frame(m)
        if self._autoplay:
            for path in (new_visible - old_visible):
                self._ensure_movie_running(path)

    def _ensure_movie_running(self, path, from_start=False):
        """Create (if missing) and start/resume the QMovie for *path*.
        Honours the LRU cap by evicting the oldest non-visible movie.

        When `from_start=True` the movie is rewound to frame 0 before
        starting. Used by hover-driven activation so each hover plays
        the preview from the beginning. Autoplay-driven activation
        (scroll) leaves it False so re-entering the viewport resumes
        where the movie was paused (continuity)."""
        m = self._movies.get(path)
        if m is None:
            if len(self._movies) >= self._max_movies:
                self._evict_lru()
            m = QtGui.QMovie(path, parent=self)
            m.setScaledSize(self._aspect_scaled_size(path))
            self._movies[path] = m
            self._lru.append(path)
        else:
            try:
                self._lru.remove(path)
            except ValueError:
                pass
            self._lru.append(path)
        # Re-arm frameChanged exactly once for this run.
        self._safe_disconnect_frame(m)
        m.frameChanged.connect(lambda _f, p=path: self._on_frame(p))
        if from_start:
            # Stop+rewind+start: jumpToFrame on a running movie is a
            # no-op in Qt; we have to bring it back to NotRunning first.
            if m.state() != QtGui.QMovie.NotRunning:
                m.stop()
            m.jumpToFrame(0)
            m.start()
        elif m.state() == QtGui.QMovie.NotRunning:
            m.start()
        else:
            m.setPaused(False)

    def _evict_lru(self):
        """Evict the oldest non-visible movie. Falls back to the oldest
        visible only if the cap is reached AND every movie is visible
        (extreme edge case for huge zoom-out viewports)."""
        for path in list(self._lru):
            if path not in self._visible_paths:
                self._evict_movie(path)
                return
        if self._lru:
            self._evict_movie(self._lru[0])

    def _evict_movie(self, path):
        m = self._movies.pop(path, None)
        if m:
            self._safe_disconnect_frame(m)
            m.stop()
            m.deleteLater()
        try:
            self._lru.remove(path)
        except ValueError:
            pass
        self._visible_paths.discard(path)

    def _safe_disconnect_frame(self, m):
        try:
            m.frameChanged.disconnect()
        except (RuntimeError, TypeError):
            pass

    def _on_frame(self, path):
        if not self._view:
            return
        try:
            vp = self._view.viewport()
        except RuntimeError:
            return
        for row in self._path_to_rows.get(path, ()):
            idx = self._model.index(row, 0)
            rect = self._view.visualRect(idx)
            if not rect.isEmpty():
                vp.update(rect)

    def _schedule_thumb_work(self, regen_items, delete_folders):
        """Start (or extend) a ThumbnailWorker run for the given work."""
        from Nukomfy.gui.workers import ThumbnailWorker
        if (self._thumb_worker is not None
                and self._thumb_worker.isRunning()):
            self._thumb_worker.add_work(regen_items, delete_folders)
            return
        self._thumb_worker = ThumbnailWorker(regen_items, delete_folders,
                                             parent=self)
        self._thumb_worker.thumbReady.connect(self._on_thumb_ready)
        self._thumb_worker.start()

    def _on_thumb_ready(self, gif_path, ok):
        """Worker generated a static thumb on disk. Invalidate this
        workflow's cached pixmap and repaint just the rows showing this
        path. Filtered-out paths emit late: we ignore them safely."""
        # Late delivery guard: stop_all() nulls self._view; any access to
        # widget-bound state below would touch a destroyed object.
        if not self._view:
            return
        # ok=False means decode/write failed: nothing new on disk, the
        # cache is still valid and the card already shows the placeholder.
        if not ok:
            return
        if gif_path not in self._path_to_rows:
            return
        # Cache is keyed by the disk thumb path. Drop only THIS folder's
        # thumb entries (both slots): a regenerated thumb gets a new mtime,
        # so its stale mtime-keyed sub-entry would otherwise linger.
        folder = os.path.dirname(gif_path)
        _pixmap_cache.pop(
            preview_thumb.thumb_path_for(folder, preview_thumb._BASENAME), None)
        _pixmap_cache.pop(
            preview_thumb.thumb_path_for(folder, preview_thumb._BASENAME_B), None)
        try:
            for row in self._path_to_rows.get(gif_path, ()):
                self._view.update(self._model.index(row, 0))
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Card Delegate - paints each card directly (no child widgets)
# ---------------------------------------------------------------------------
class _CardDelegate(QtWidgets.QStyledItemDelegate):

    star_clicked = QtCore.Signal(str)  # workflow_id (UUID hex)

    def __init__(self, gif_mgr, parent=None):
        super().__init__(parent)
        self._gif = gif_mgr
        self._list_mode = False
        self._scale = 1.0
        self._thumb = THUMB_SIZE
        self._hover_row = -1
        self._hover_star = False  # True when mouse is over the star rect
        self._hover_split = {}    # _split_key(item) -> float in [0,1]
        self._height_cache = {}   # id(item) -> int
        # Layout caches: badges and capped paragraphs depend on (tags/text,
        # width, scale). Cleared whenever scale or items change so paint
        # and sizeHint never see stale entries.
        self._layout_cache = {}   # (id(tags), avail_w, max_rows) -> layout
        self._para_cache = {}     # (id(text), id(font), width, max_lines) -> result
        self._favorites = set()   # set of workflow UUIDs
        self._favorites_filter_active = False
        self._name_dups = set()           # metadata.name strings that collide
        self._name_folder_dups = set()    # (name, folder_basename) collisions

    def set_collisions(self, name_dups, name_folder_dups):
        """Update disambiguation context (called after each scan)."""
        self._name_dups = set(name_dups)
        self._name_folder_dups = set(name_folder_dups)
        self._height_cache.clear()
        self._layout_cache.clear()
        self._para_cache.clear()

    def configure(self, list_mode, scale):
        self._list_mode = list_mode
        self._scale = scale
        self._thumb = int(THUMB_SIZE * scale)
        self._height_cache.clear()
        self._layout_cache.clear()
        self._para_cache.clear()

    def refresh_visibility(self):
        """One of the library_*_show_* settings flipped: drop caches so
        sizeHint and paint pick up the new visibility flags on the next
        event loop tick."""
        self._height_cache.clear()
        self._layout_cache.clear()
        self._para_cache.clear()

    def _vis(self):
        """Return the 6-tuple (version, author, description, categories,
        models, source) of visibility flags for the active view mode. With
        local workflows disabled every card is shared, so the source badge
        is always suppressed."""
        src_off = settings.disable_local_workflows
        if self._list_mode:
            return (bool(settings.library_list_show_version),
                    bool(settings.library_list_show_author),
                    bool(settings.library_list_show_description),
                    bool(settings.library_list_show_categories),
                    bool(settings.library_list_show_models),
                    False if src_off else bool(settings.library_list_show_source))
        return (bool(settings.library_grid_show_version),
                bool(settings.library_grid_show_author),
                bool(settings.library_grid_show_description),
                bool(settings.library_grid_show_categories),
                bool(settings.library_grid_show_models),
                False if src_off else bool(settings.library_grid_show_source))

    # ── memoized layout helpers ────────────────────────────────────
    def _cached_layout(self, tags, avail_w, max_rows=2):
        if not tags:
            return [], None, 0
        key = (id(tags), avail_w, max_rows)
        cached = self._layout_cache.get(key)
        if cached is None:
            cached = _layout_capped_badges(tags, avail_w, self._scale,
                                           max_rows=max_rows)
            self._layout_cache[key] = cached
        return cached

    def _cached_rows(self, tags, avail_w, max_rows=2):
        if not tags:
            return 0
        placements, plus_pos, _ = self._cached_layout(tags, avail_w,
                                                     max_rows=max_rows)
        rows = 0
        for _, _, r in placements:
            if r + 1 > rows:
                rows = r + 1
        if plus_pos is not None and plus_pos[1] + 1 > rows:
            rows = plus_pos[1] + 1
        return max(1, rows)

    def _cached_paragraph(self, text, font, width, max_lines):
        if not text:
            return '', 0, False
        key = (id(text), id(font), width, max_lines)
        cached = self._para_cache.get(key)
        if cached is None:
            cached = _layout_paragraph_capped(text, font, width, max_lines)
            self._para_cache[key] = cached
        return cached

    # ── sizing ─────────────────────────────────────────────────────
    def sizeHint(self, option, index):
        item = index.data(_WorkflowModel.ItemRole)
        if not item:
            if self._list_mode:
                return QtCore.QSize(0, self._thumb + GRID_SPACING * 2)
            return QtCore.QSize(self._thumb + 16, self._thumb + 100)
        iid = id(item)
        h = self._height_cache.get(iid)
        if h is None:
            if self._list_mode:
                h = self._list_item_height(item)
            else:
                h = self._grid_item_height(item, self._thumb + 16)
            self._height_cache[iid] = h
        if self._list_mode:
            return QtCore.QSize(0, h)
        return QtCore.QSize(self._thumb + 16, h)

    def _grid_item_height(self, item, card_w):
        """Compute the exact height needed for a grid card. All variable-
        height fields are bounded: title and subtitle are single-line
        (elided if too long), description is capped at 3 lines, tag groups
        are capped at 2 rows each. Fields hidden via the
        `library_grid_show_*` settings are skipped entirely so the card
        shrinks accordingly."""
        s = self._scale
        avail_w = card_w - 12
        y = self._thumb + 5
        v_ver, v_aut, v_des, v_cat, v_mod, v_src = self._vis()

        # name - single line (elided if needed), always shown
        fs_n = max(9, int(14 * s))
        fm_n = QtGui.QFontMetrics(_font(fs_n, bold=True))
        y += fm_n.lineSpacing() + 2

        # subtitle - single line, only if at least one of version/author
        # is enabled AND populated on the item
        if (v_ver and item.version) or (v_aut and item.author):
            y += max(8, int(11 * s)) + 4

        # description - capped to 3 lines
        if v_des and item.description:
            fs_d = max(8, int(12 * s))
            fm_d = QtGui.QFontMetrics(_font(fs_d))
            _, lines, _ = self._cached_paragraph(
                item.description, _font(fs_d), avail_w, 3)
            if lines == 0:
                lines = 1
            y += fm_d.lineSpacing() * lines + 5

        # tags - capped to 2 rows per group
        badge_h = _badge_height(s)
        for tags, visible in ((item.tags_category, v_cat),
                              (item.tags_models, v_mod)):
            if not (visible and tags):
                continue
            rows = self._cached_rows(tags, avail_w, max_rows=2)
            y += badge_h * rows + (rows - 1) * 2 + 3

        # source badge + bottom padding; without source we still want a
        # small tail so the card doesn't hug the last visible row.
        if v_src:
            y += badge_h + 8
        else:
            y += 6
        return y

    def _list_item_height(self, item):
        """Compute the dynamic list-card height.

        height = max(thumb_floor, content_required)
        thumb_floor = self._thumb + 2 * pad  (slider-driven)

        The thumbnail stays square at the slider-driven thumb size, so
        the card never shrinks below that footprint at the current
        zoom. Hiding fields trims the content side; to make the card
        shorter the user lowers the zoom slider.
        """
        s = self._scale
        pad = GRID_SPACING
        v_ver, v_aut, v_des, v_cat, v_mod, v_src = self._vis()

        # best-effort text-column width using the current viewport;
        # height becomes inaccurate by at most +/-1 description line
        # on rapid resize, which the resize hook then corrects.
        view = self.parent()
        try:
            vw = view.viewport().width() if view is not None else 800
        except (AttributeError, RuntimeError):
            vw = 800
        text_w = max(1, vw - pad - self._thumb - pad * 2 - pad)

        fs_n = max(9, int(14 * s))
        y = pad + fs_n + 5  # name

        if (v_ver and item.version) or (v_aut and item.author):
            y += max(8, int(11 * s)) + 4

        if v_des and item.description:
            fs_d = max(8, int(13 * s))
            f_d = _font(fs_d)
            _, lines, _ = self._cached_paragraph(
                item.description, f_d, text_w, 3)
            if lines == 0:
                lines = 1
            fm_d = QtGui.QFontMetrics(f_d)
            y += fm_d.lineSpacing() * lines + 4

        INTERNAL_GAP = 6
        badge_h = _badge_height(s)
        fs_lbl = max(8, int(11 * s))
        fm_lbl = QtGui.QFontMetrics(_font(fs_lbl, bold=True))
        indent = max(_text_width(fm_lbl, 'Categories: '),
                     _text_width(fm_lbl, 'Models: '))
        avail = max(1, text_w - indent)
        has_cats = v_cat and bool(item.tags_category)
        has_mods = v_mod and bool(item.tags_models)
        cat_rows = self._cached_rows(item.tags_category, avail, max_rows=2) if has_cats else 0
        mod_rows = self._cached_rows(item.tags_models, avail, max_rows=2) if has_mods else 0
        cats_h = (badge_h * cat_rows + (cat_rows - 1) * 2) if cat_rows else 0
        mods_h = (badge_h * mod_rows + (mod_rows - 1) * 2) if mod_rows else 0
        if has_cats and has_mods:
            y += cats_h + INTERNAL_GAP + mods_h
        elif has_cats:
            y += cats_h
        elif has_mods:
            y += mods_h

        if v_src:
            y += badge_h + pad

        content_h = y + pad

        thumb_floor_h = self._thumb + 2 * pad
        return max(thumb_floor_h, content_h)

    # ── main paint entry ───────────────────────────────────────────
    def paint(self, painter, option, index):
        item = index.data(_WorkflowModel.ItemRole)
        if not item:
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setClipRect(option.rect)

        is_hovered = index.row() == self._hover_row
        bg = '#2e2e2e' if is_hovered else '#262626'
        painter.setBrush(QtGui.QColor(bg))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(option.rect, 4, 4)

        if self._list_mode:
            self._paint_list(painter, option.rect, item, is_hovered)
        else:
            self._paint_grid(painter, option.rect, item, is_hovered)
        painter.restore()

    # ── tooltip (overflow only) ────────────────────────────────
    def helpEvent(self, event, view, option, index):
        """Show the rich-HTML tooltip only when something on the card was
        actually truncated: title elided, subtitle elided, description
        capped at 3 lines, or a tag group hitting the 2-row cap. Cards
        whose content all fits stay quiet."""
        if event.type() != QtCore.QEvent.ToolTip:
            return super().helpEvent(event, view, option, index)
        item = index.data(_WorkflowModel.ItemRole)
        if not item:
            return super().helpEvent(event, view, option, index)
        s = self._scale
        v_ver, v_aut, v_des, v_cat, v_mod, _v_src = self._vis()

        # Resolve text-area width for both modes.
        if self._list_mode:
            pad = GRID_SPACING
            tx = option.rect.x() + pad + self._thumb + pad * 2
            text_w = option.rect.right() - tx - pad
            fs_lbl = max(8, int(11 * s))
            fm_lbl = QtGui.QFontMetrics(_font(fs_lbl, bold=True))
            indent = max(_text_width(fm_lbl, 'Categories: '),
                         _text_width(fm_lbl, 'Models: '))
            badges_w = max(1, text_w - indent)
        else:
            text_w = option.rect.width() - 12
            badges_w = text_w

        truncated = False

        # Title elide
        display_name = _display_name_for(
            item, self._name_dups, self._name_folder_dups)
        fs_n = max(9, int(14 * s))
        _, name_elided = _elide_to_width(display_name, _font(fs_n, bold=True), text_w)
        if name_elided:
            truncated = True

        # Subtitle elide
        if not truncated:
            sub = _subtitle(item, v_ver, v_aut)
            if sub:
                fs_s = max(8, int(11 * s))
                _, sub_elided = _elide_to_width(sub, _font(fs_s, italic=True), text_w)
                if sub_elided:
                    truncated = True

        # Description cap (only if the field is visible)
        if not truncated and v_des and item.description:
            fs_d = max(8, int(13 * s)) if self._list_mode else max(8, int(12 * s))
            _, _, desc_truncated = self._cached_paragraph(
                item.description, _font(fs_d), text_w, 3)
            if desc_truncated:
                truncated = True

        # Tag overflow (only on groups currently visible)
        if not truncated:
            for tags, visible in ((item.tags_category, v_cat),
                                   (item.tags_models, v_mod)):
                if not (visible and tags):
                    continue
                _, plus_pos, _ = self._cached_layout(
                    tags, badges_w, max_rows=2)
                if plus_pos is not None:
                    truncated = True
                    break

        if not truncated:
            QtWidgets.QToolTip.hideText()
            return True
        html = _build_card_tooltip(item)
        if html:
            QtWidgets.QToolTip.showText(event.globalPos(), html, view)
        else:
            QtWidgets.QToolTip.hideText()
        return True

    # ── click handling ─────────────────────────────────────────
    def editorEvent(self, event, model, option, index):
        if (event.type() == QtCore.QEvent.MouseButtonRelease and
                event.button() == QtCore.Qt.LeftButton):
            item = index.data(_WorkflowModel.ItemRole)
            if item:
                sr = self._star_rect(option, item)
                if sr.contains(event.pos()):
                    self.star_clicked.emit(item.workflow_id)
                    return True
        return super().editorEvent(event, model, option, index)

    # ── star (favorite) ──────────────────────────────────────────
    def _star_rect(self, option, item):
        """Return the QRect for the star icon within a card."""
        sz = max(16, int(20 * self._scale))
        margin = 4
        if self._list_mode:
            # Top-right of the card
            return QtCore.QRect(
                option.rect.right() - sz - margin,
                option.rect.top() + margin,
                sz, sz)
        else:
            # Top-right of the thumbnail area
            return QtCore.QRect(
                option.rect.right() - sz - margin - (option.rect.width() - self._thumb) // 2,
                option.rect.top() + margin,
                sz, sz)

    def _draw_star(self, p, rect, is_fav, hover=False):
        """Draw a star icon using Material Icons font."""
        p.save()
        if self._favorites_filter_active:
            color = QtGui.QColor('#f5c518')
            char = STAR
        elif is_fav:
            color = QtGui.QColor('#f5c518')   # gold full
            char = STAR
        elif hover:
            color = QtGui.QColor('#b8940f')   # gold 50%
            char = STAR
        else:
            color = QtGui.QColor('#555')
            char = STAR_BORDER
        p.setPen(color)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setFont(icon_font(rect.height()))
        p.drawText(rect, QtCore.Qt.AlignCenter, char)
        p.restore()

    # ── thumbnail ──────────────────────────────────────────────────
    def _thumb_px(self, item):
        if not item.preview_path:
            return None
        if item.preview_path.endswith('.gif'):
            # 1. If a QMovie for this path is actively animating, use its
            #    current frame (autoplay or hover-driven).
            px = self._gif.pixmap_if_running(item.preview_path)
            if px:
                return px
            # 2. Otherwise prefer the co-located preview_thumb.webp
            #    (static, fast, no GIF decode needed).
            cached = _gif_first_frame_pixmap(item.folder_path, self._thumb)
            if cached:
                return cached
            # 3. Thumb not generated yet (first scan, read-only Shared).
            #    Show neutral placeholder; generation is scheduled by
            #    _GifManager.update().
            return _placeholder_pixmap(self._thumb)
        return _cached_pixmap(item.preview_path, self._thumb)

    def _draw_thumb(self, p, rect, item):
        if item.has_comparison:
            self._draw_comparison(p, rect, item)
            return
        p.fillRect(rect, QtGui.QColor('#1a1a1a'))
        px = self._thumb_px(item)
        if px:
            p.drawPixmap(rect, px)
        else:
            p.setPen(QtGui.QColor('#666'))
            p.setFont(_font(max(8, int(10 * self._scale))))
            p.drawText(rect, QtCore.Qt.AlignCenter, 'No Preview')

    def _thumb_rect(self, rect):
        """Sub-rect of the card that holds the thumbnail. Single source of
        truth shared by paint and the hover/slider hit-testing in
        LibraryPanel.eventFilter so the two never drift."""
        th = self._thumb
        if self._list_mode:
            pad = GRID_SPACING
            inner = max(1, rect.height() - 2 * pad)
            th_actual = min(inner, int(THUMB_SIZE * self._scale))
            return QtCore.QRect(rect.x() + pad, rect.y() + pad,
                                th_actual, th_actual)
        tx = rect.x() + (rect.width() - th) // 2
        return QtCore.QRect(tx, rect.y(), th, th)

    # ── comparison slider (two previews) ───────────────────────────
    def _comparison_side_px(self, path, folder, basename):
        """Pixmap for one side of the comparison slider. GIF: the live
        QMovie frame while running (autoplay/hover), else the co-located
        static thumb, else a neutral placeholder. Static image: cached
        square crop. Mirrors `_thumb_px` so comparison GIFs animate exactly
        like single-image cards."""
        if not path:
            return None
        if path.endswith('.gif'):
            px = self._gif.pixmap_if_running(path)
            if px:
                return px
            return (_gif_first_frame_pixmap(folder, self._thumb, basename)
                    or _placeholder_pixmap(self._thumb))
        return _cached_pixmap(path, self._thumb)

    def _draw_comparison(self, p, rect, item):
        """Before/after split: preview.* on the left, preview_b.* on the
        right, divided at the per-card slider position (default centre)."""
        p.fillRect(rect, QtGui.QColor('#1a1a1a'))
        pxa = self._comparison_side_px(
            item.preview_path, item.folder_path, preview_thumb._BASENAME)
        pxb = self._comparison_side_px(
            item.preview_path_b, item.folder_path, preview_thumb._BASENAME_B)
        split = self._hover_split.get(_split_key(item), 0.5)
        split_x = rect.x() + int(round(split * rect.width()))
        # Clamp inside the thumb so the handle never sits a pixel past the edge.
        split_x = max(rect.x(), min(rect.right(), split_x))
        if pxa:
            p.save()
            p.setClipRect(QtCore.QRect(rect.x(), rect.y(),
                                       max(0, split_x - rect.x()),
                                       rect.height()))
            p.drawPixmap(rect, pxa)
            p.restore()
        if pxb:
            p.save()
            p.setClipRect(QtCore.QRect(split_x, rect.y(),
                                       max(0, rect.right() - split_x + 1),
                                       rect.height()))
            p.drawPixmap(rect, pxb)
            p.restore()
        self._draw_slider_handle(p, rect, split_x)

    def _draw_slider_handle(self, p, rect, split_x):
        """Before/after handle: a thin divider over a subtle
        dark casing (legible on light and dark frames) and a solid white
        thumb with a soft (faked) shadow - no hard border, no dark centre -
        plus a faint ‹ › drag hint. Neutral (gold is reserved for the
        favourite state)."""
        s = self._scale
        cx = int(split_x)
        cy = int(rect.center().y())
        p.save()
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        # Divider: dark casing under a thin white line -> visible on anything.
        casing = QtGui.QPen(QtGui.QColor(0, 0, 0, 70))
        casing.setWidth(max(2, int(round(3 * s))))
        p.setPen(casing)
        p.drawLine(cx, rect.top(), cx, rect.bottom())
        line = QtGui.QPen(QtGui.QColor(255, 255, 255, 225))
        line.setWidth(max(1, int(round(1.5 * s))))
        p.setPen(line)
        p.drawLine(cx, rect.top(), cx, rect.bottom())

        # Thumb: solid white circle with a soft shadow faked as three faint
        # dark rings of decreasing opacity (QPainter has no blur). No border.
        r = max(8, int(round(9 * s)))
        center = QtCore.QPoint(cx, cy)
        p.setPen(QtCore.Qt.NoPen)
        for off, a in ((3, 16), (2, 26), (1, 36)):
            p.setBrush(QtGui.QColor(0, 0, 0, a))
            p.drawEllipse(center, r + off, r + off)
        p.setBrush(QtGui.QColor(255, 255, 255))
        p.drawEllipse(center, r, r)

        # Faint ‹ › drag hint - light grey, so the white thumb never reads as
        # an eye (a dark centre would).
        chev = QtGui.QPen(QtGui.QColor(140, 140, 140))
        chev.setWidth(max(1, int(round(1.4 * s))))
        chev.setCapStyle(QtCore.Qt.RoundCap)
        chev.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(chev)
        p.setBrush(QtCore.Qt.NoBrush)
        g = max(2, int(round(r * 0.40)))   # tip offset from centre
        d = max(2, int(round(r * 0.32)))   # arm length
        p.drawLine(cx - g, cy, cx - g + d, cy - d)   # left chevron  ‹
        p.drawLine(cx - g, cy, cx - g + d, cy + d)
        p.drawLine(cx + g, cy, cx + g - d, cy - d)   # right chevron ›
        p.drawLine(cx + g, cy, cx + g - d, cy + d)
        p.restore()

    # ── grid paint ─────────────────────────────────────────────────
    def _paint_grid(self, p, r, item, is_hovered=False):
        th = self._thumb
        s = self._scale
        v_ver, v_aut, v_des, v_cat, v_mod, v_src = self._vis()

        # thumbnail centred at top
        self._draw_thumb(p, self._thumb_rect(r), item)

        x = r.x() + 6
        w = r.width() - 12
        y = r.y() + th + 5

        # name - single line, elided, always shown
        display_name = _display_name_for(item, self._name_dups, self._name_folder_dups)
        fs_n = max(9, int(14 * s))
        f_n = _font(fs_n, bold=True)
        p.setFont(f_n)
        p.setPen(QtGui.QColor('#eee'))
        name_text, _ = _elide_to_width(display_name, f_n, w)
        fm_n = QtGui.QFontMetrics(f_n)
        line_h_n = fm_n.lineSpacing()
        p.drawText(QtCore.QRect(x, y, w, line_h_n + 2),
                   QtCore.Qt.AlignLeft, name_text)
        y += line_h_n + 2

        # subtitle - single line, elided
        sub = _subtitle(item, v_ver, v_aut)
        if sub:
            fs_s = max(8, int(11 * s))
            f_s = _font(fs_s, italic=True)
            p.setFont(f_s)
            p.setPen(QtGui.QColor('#666'))
            sub_text, _ = _elide_to_width(sub, f_s, w)
            p.drawText(QtCore.QRect(x, y, w, fs_s + 4),
                       QtCore.Qt.AlignLeft, sub_text)
            y += fs_s + 4

        # description - capped to 3 lines with ellipsis
        if v_des and item.description:
            fs_d = max(8, int(12 * s))
            f_d = _font(fs_d)
            p.setFont(f_d)
            p.setPen(QtGui.QColor('#999'))
            desc_text, lines, _ = self._cached_paragraph(
                item.description, f_d, w, 3)
            fm_d = QtGui.QFontMetrics(f_d)
            line_h_d = fm_d.lineSpacing()
            box_h = max(1, lines) * line_h_d
            p.drawText(QtCore.QRect(x, y, w, box_h),
                       QtCore.Qt.AlignLeft | QtCore.Qt.TextWordWrap,
                       desc_text)
            y += box_h + 5

        # tags
        y = self._paint_tags(p, x, y, w, item, s, v_cat, v_mod)

        # source badge - anchored at bottom
        if v_src:
            bh = int(18 * s)
            src_y = r.bottom() - bh - 4
            bx = x
            _draw_badge(p, bx, max(y + 2, src_y), item.source,
                        _COLOR_SRC.get(item.source, '#333'),
                        _COLOR_SRC_TXT.get(item.source, '#aaa'), s,
                        border=_COLOR_SRC_TXT.get(item.source))
            if getattr(item, 'id_conflict', False):
                bx += _badge_width(item.source, s) + 4
                _draw_icon_text_badge(p, bx, max(y + 2, src_y),
                                      WARNING, 'Duplicate ID',
                                      _COLOR_DUP[0], _COLOR_DUP[1], s)

        # favorite star - top-right of thumbnail
        is_fav = bool(item.workflow_id) and item.workflow_id in self._favorites
        sr = self._star_rect(
            type('O', (), {'rect': r})(), item)
        star_hover = is_hovered and self._hover_star and not is_fav
        self._draw_star(p, sr, is_fav, hover=star_hover)

    # ── list paint ─────────────────────────────────────────────────
    def _paint_list(self, p, r, item, is_hovered=False):
        s = self._scale
        pad = GRID_SPACING
        v_ver, v_aut, v_des, v_cat, v_mod, v_src = self._vis()

        # thumbnail on the left - square sized to the card's inner height
        # so it shrinks/grows with the dynamic list-card height. Capped
        # at THUMB_SIZE * scale so a tall card (many tag rows) doesn't
        # stretch the thumb past its natural size.
        tr = self._thumb_rect(r)
        self._draw_thumb(p, tr, item)

        x = r.x() + pad + tr.width() + pad * 2
        w = r.right() - x - pad
        y = r.y() + pad

        # name - single line, elided, always shown
        display_name = _display_name_for(item, self._name_dups, self._name_folder_dups)
        fs_n = max(9, int(14 * s))
        f_n = _font(fs_n, bold=True)
        p.setFont(f_n)
        p.setPen(QtGui.QColor('#eee'))
        name_text, _ = _elide_to_width(display_name, f_n, w)
        p.drawText(QtCore.QRect(x, y, w, fs_n + 4),
                   QtCore.Qt.AlignLeft, name_text)
        y += fs_n + 5

        # subtitle - single line, elided
        sub = _subtitle(item, v_ver, v_aut)
        if sub:
            fs_s = max(8, int(11 * s))
            f_s = _font(fs_s, italic=True)
            p.setFont(f_s)
            p.setPen(QtGui.QColor('#666'))
            sub_text, _ = _elide_to_width(sub, f_s, w)
            p.drawText(QtCore.QRect(x, y, w, fs_s + 4),
                       QtCore.Qt.AlignLeft, sub_text)
            y += fs_s + 4

        # description - capped to 3 lines with ellipsis
        if v_des and item.description:
            fs_d = max(8, int(13 * s))
            f_d = _font(fs_d)
            p.setFont(f_d)
            p.setPen(QtGui.QColor('#999'))
            desc_text, desc_lines, _ = self._cached_paragraph(
                item.description, f_d, w, 3)
            fm_d = QtGui.QFontMetrics(f_d)
            line_h_d = fm_d.lineSpacing()
            box_h = max(1, desc_lines) * line_h_d
            p.drawText(QtCore.QRect(x, y, w, box_h),
                       QtCore.Qt.AlignLeft | QtCore.Qt.TextWordWrap,
                       desc_text)
            y += box_h + 4

        # tags rows - block centred between description and bottom-anchored
        # source. Visibility rules:
        #   • cat present, mod present: both rendered (Cat upper, Mod lower)
        #   • cat present, mod absent : Cat in upper slot, Mod hidden
        #   • cat absent , mod present: Mod takes the upper slot (Cat hidden)
        #   • both absent             : both hidden, no labels
        # A group hidden via library_list_show_* counts as absent.
        INTERNAL_GAP = 6
        badge_h = _badge_height(s)
        bottom_anchor_h = (badge_h + pad) if v_src else pad
        src_y = r.bottom() - bottom_anchor_h
        fs_lbl = max(8, int(11 * s))
        fm_lbl = QtGui.QFontMetrics(_font(fs_lbl, bold=True))
        indent = max(_text_width(fm_lbl, 'Categories: '),
                     _text_width(fm_lbl, 'Models: '))
        avail_for_badges = max(1, w - indent)
        has_cats = v_cat and bool(item.tags_category)
        has_mods = v_mod and bool(item.tags_models)
        cat_rows = self._cached_rows(item.tags_category, avail_for_badges, max_rows=2) if has_cats else 0
        mod_rows = self._cached_rows(item.tags_models, avail_for_badges, max_rows=2) if has_mods else 0
        cats_h = (badge_h * cat_rows + (cat_rows - 1) * 2) if cat_rows else 0
        mods_h = (badge_h * mod_rows + (mod_rows - 1) * 2) if mod_rows else 0

        if has_cats and has_mods:
            block_h = cats_h + INTERNAL_GAP + mods_h
        elif has_cats:
            block_h = cats_h
        elif has_mods:
            block_h = mods_h
        else:
            block_h = 0

        avail = max(0, src_y - y)
        slack = max(0, avail - block_h) if block_h else 0
        y_block = y + slack // 2

        if has_cats and has_mods:
            self._paint_tag_row(p, x, y_block, w, 'Categories:', item.tags_category,
                                _COLOR_CAT, s, indent=indent, max_rows=2)
            y_mod = y_block + cats_h + INTERNAL_GAP
            self._paint_tag_row(p, x, y_mod, w, 'Models:', item.tags_models,
                                _COLOR_MOD, s, indent=indent, max_rows=2)
            y = y_mod + mods_h
        elif has_cats:
            self._paint_tag_row(p, x, y_block, w, 'Categories:', item.tags_category,
                                _COLOR_CAT, s, indent=indent, max_rows=2)
            y = y_block + cats_h
        elif has_mods:
            # Models takes the upper slot
            self._paint_tag_row(p, x, y_block, w, 'Models:', item.tags_models,
                                _COLOR_MOD, s, indent=indent, max_rows=2)
            y = y_block + mods_h

        # source badge - anchored to bottom-left of card
        if v_src:
            bh = badge_h
            src_y = r.bottom() - bh - pad
            bx = x
            _draw_badge(p, bx, max(y + 2, src_y), item.source,
                        _COLOR_SRC.get(item.source, '#333'),
                        _COLOR_SRC_TXT.get(item.source, '#aaa'), s,
                        border=_COLOR_SRC_TXT.get(item.source))
            if getattr(item, 'id_conflict', False):
                bx += _badge_width(item.source, s) + 4
                _draw_icon_text_badge(p, bx, max(y + 2, src_y),
                                      WARNING, 'Duplicate ID',
                                      _COLOR_DUP[0], _COLOR_DUP[1], s)

        # favorite star - top-right of card
        is_fav = bool(item.workflow_id) and item.workflow_id in self._favorites
        sr = self._star_rect(
            type('O', (), {'rect': r})(), item)
        star_hover = is_hovered and self._hover_star and not is_fav
        self._draw_star(p, sr, is_fav, hover=star_hover)

    # ── tag helpers ────────────────────────────────────────────────
    def _paint_tags(self, p, x, y, w, item, scale,
                    show_categories=True, show_models=True):
        """Paint tag badges without prefix labels (grid mode). Each group
        (categories, models) is capped at 2 rows; overflow becomes a '+N'
        badge at the end of the last row. Each group advances `y` by the
        actual number of rows it occupied so groups never overlap. Groups
        disabled by `library_grid_show_*` settings are skipped entirely."""
        badge_h = _badge_height(scale)
        row_h = badge_h + 2
        for tags, colors, visible in ((item.tags_category, _COLOR_CAT, show_categories),
                                       (item.tags_models, _COLOR_MOD, show_models)):
            if not (visible and tags):
                continue
            placements, plus_pos, plus_n = self._cached_layout(
                tags, w, max_rows=2)
            for tag, off_x, row_idx in placements:
                _draw_badge(p, x + off_x, y + row_idx * row_h,
                            tag, colors[0], colors[1], scale)
            if plus_pos is not None:
                off_x, row_idx = plus_pos
                _draw_badge(p, x + off_x, y + row_idx * row_h,
                            '+{}'.format(plus_n),
                            colors[0], colors[1], scale)
            rows = 0
            for _, _, r in placements:
                if r + 1 > rows:
                    rows = r + 1
            if plus_pos is not None and plus_pos[1] + 1 > rows:
                rows = plus_pos[1] + 1
            rows = max(1, rows)
            y += badge_h * rows + (rows - 1) * 2 + 3
        return y

    def _paint_tag_row(self, p, x, y, w, label, tags, colors, scale,
                        indent=None, max_rows=3):
        """Paint a labelled tag row (list mode). The label is always drawn,
        even when `tags` is empty, so the row keeps its slot in the card.
        Badges are capped at `max_rows` rows; when the available width is
        too small to fit them all, the overflow becomes a '+N' badge at
        the end. `indent`, if provided, fixes the badge x-start so multiple
        labelled rows stay vertically aligned even though labels have
        different lengths."""
        fs = max(8, int(11 * scale))
        p.setFont(_font(fs, bold=True))
        p.setPen(QtGui.QColor('#888'))
        if indent is None:
            indent = _text_width(p.fontMetrics(), label + ' ')
        p.drawText(x, y + fs, label)
        badge_h = _badge_height(scale)
        row_h = badge_h + 2
        if not tags:
            # No tags: keep the row's footprint (one badge_h tall) so
            # neighbouring rows stay aligned across cards.
            return y + badge_h + 3
        avail_for_badges = max(1, w - indent)
        placements, plus_pos, plus_n = self._cached_layout(
            tags, avail_for_badges, max_rows=max_rows)
        for tag, off_x, row_idx in placements:
            _draw_badge(p, x + indent + off_x, y + row_idx * row_h,
                        tag, colors[0], colors[1], scale)
        if plus_pos is not None:
            off_x, row_idx = plus_pos
            _draw_badge(p, x + indent + off_x, y + row_idx * row_h,
                        '+{}'.format(plus_n), colors[0], colors[1], scale)
        rows = 0
        for _, _, r in placements:
            if r + 1 > rows:
                rows = r + 1
        if plus_pos is not None and plus_pos[1] + 1 > rows:
            rows = plus_pos[1] + 1
        rows = max(1, rows)
        y += badge_h * rows + (rows - 1) * 2 + 3
        return y


# ---------------------------------------------------------------------------
# Free-standing paint helpers
# ---------------------------------------------------------------------------
_font_cache = {}

def _font(px, bold=False, italic=False):
    key = (px, bold, italic)
    f = _font_cache.get(key)
    if f is None:
        f = QtGui.QFont()
        f.setPixelSize(px)
        if bold:
            f.setBold(True)
        if italic:
            f.setItalic(True)
        _font_cache[key] = f
    return f


def _subtitle(item, show_version=True, show_author=True):
    parts = []
    if show_version and item.version:
        parts.append('v{}'.format(item.version))
    if show_author and item.author:
        parts.append('by {}'.format(item.author))
    return ' - '.join(parts)


def _badge_width(text, scale):
    """Return the pixel width of a badge pill without drawing it."""
    fs = max(8, int(LIBRARY_BADGE_FONT_PX * scale))
    ph = max(3, int(7 * scale))
    fm = QtGui.QFontMetrics(_font(fs))
    return _text_width(fm, text) + ph * 2


def _badge_height(scale):
    """Return the actual rendered height of a badge pill. Mirrors the
    formula in _draw_badge so vertical spacing logic stays in sync at
    small zoom levels (where min-font/min-padding floors kick in)."""
    fs = max(8, int(LIBRARY_BADGE_FONT_PX * scale))
    pv = max(1, int(2 * scale))
    return fs + pv * 2 + 2


def _draw_icon_text_badge(p, x, y, icon_char, text, bg, fg, scale):
    """Rounded badge: material-icon glyph + text. Returns badge width."""
    fs = max(8, int(LIBRARY_BADGE_FONT_PX * scale))
    pv = max(1, int(2 * scale))
    ph = max(3, int(7 * scale))
    gap = max(2, int(3 * scale))
    icon_f = icon_font(fs)
    text_f = _font(fs)
    fm_i = QtGui.QFontMetrics(icon_f)
    fm_t = QtGui.QFontMetrics(text_f)
    iw = _text_width(fm_i, icon_char)
    tw = _text_width(fm_t, text)
    bw = iw + gap + tw + ph * 2
    bh = fs + pv * 2 + 2
    p.save()
    p.setBrush(QtGui.QColor(bg))
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(QtCore.QRect(int(x), int(y), bw, bh), 2, 2)
    p.setPen(QtGui.QColor(fg))
    p.setFont(icon_f)
    p.drawText(QtCore.QRect(int(x) + ph, int(y) + pv, iw + 2, bh - pv * 2),
               QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, icon_char)
    p.setFont(text_f)
    p.drawText(QtCore.QRect(int(x) + ph + iw + gap, int(y) + pv,
                            tw + 2, bh - pv * 2),
               QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
    p.restore()
    return bw


def _draw_badge(p, x, y, text, bg, fg, scale, border=None):
    """Draw a rounded badge pill. Returns the badge width.

    If `border` is set, paint a 1px stroke around the pill (matches the
    autoplay Play Button look on Source badges)."""
    fs = max(8, int(LIBRARY_BADGE_FONT_PX * scale))
    pv = max(1, int(2 * scale))
    ph = max(3, int(7 * scale))
    f = _font(fs)
    fm = QtGui.QFontMetrics(f)
    tw = _text_width(fm, text)
    bw = tw + ph * 2
    bh = fs + pv * 2 + 2

    p.save()
    p.setBrush(QtGui.QColor(bg))
    if border:
        p.setPen(QtGui.QPen(QtGui.QColor(border), 1))
    else:
        p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(QtCore.QRect(int(x), int(y), bw, bh), 2, 2)
    p.setFont(f)
    p.setPen(QtGui.QColor(fg))
    p.drawText(QtCore.QRect(int(x) + ph, int(y) + pv, tw + 2, bh - pv * 2),
               QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
    p.restore()
    return bw


def _elide_to_width(text, font, width):
    """Return (display_text, was_elided) for a single-line elide-right.
    Empty string returns ('', False)."""
    if not text:
        return '', False
    fm = QtGui.QFontMetrics(font)
    full = fm.horizontalAdvance(text) if hasattr(fm, 'horizontalAdvance') else fm.width(text)
    if full <= width:
        return text, False
    return fm.elidedText(text, QtCore.Qt.ElideRight, width), True


def _layout_paragraph_capped(text, font, width, max_lines):
    """Return (used_text, line_count, was_truncated). Lays out `text` word-
    wrapped to `width`, capped at `max_lines`. If truncated, the returned
    `used_text` is shortened so an ellipsis fits at the end of the last
    line. Caller is responsible for actually drawing the result."""
    if not text:
        return '', 0, False
    layout = QtGui.QTextLayout(text, font)
    layout.beginLayout()
    char_end = 0
    used_lines = 0
    while used_lines < max_lines:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        char_end = line.textStart() + line.textLength()
        used_lines += 1
    # Probe one more line to decide truncation
    extra = layout.createLine()
    truncated = extra.isValid()
    layout.endLayout()
    if not truncated:
        return text, used_lines, False
    # Build truncated text + ellipsis. Shrink until the "…" fits in last line.
    visible = text[:char_end].rstrip()
    ellipsis = '…'
    while visible:
        candidate = visible + ellipsis
        # Lay out candidate and check it fits in max_lines
        candidate_layout = QtGui.QTextLayout(candidate, font)
        candidate_layout.beginLayout()
        cand_lines = 0
        while True:
            ln = candidate_layout.createLine()
            if not ln.isValid():
                break
            ln.setLineWidth(width)
            cand_lines += 1
            if cand_lines > max_lines:
                break
        candidate_layout.endLayout()
        if cand_lines <= max_lines:
            return candidate, max_lines, True
        visible = visible[:-1].rstrip()
    return ellipsis, 1, True


def _layout_capped_badges(tags, avail_w, scale, max_rows=2, gap=4):
    """Compute placements for badges within a max_rows cap.

    Returns (placements, plus_pos, plus_n) where:
      placements = list of (tag, x_offset, row_index)
      plus_pos   = (x_offset, row_index) for the '+N' badge, or None
      plus_n     = number of overflow tags represented by the badge

    The '+N' badge is rendered at the end of the last row when there is
    overflow. If it would not fit alongside placed badges, badges are
    popped from the right (and added to plus_n) until it fits.
    """
    placements = []
    if not tags:
        return placements, None, 0
    bx = 0
    row = 0
    n = len(tags)
    overflow_start = -1  # index of first tag that didn't fit
    for i, tag in enumerate(tags):
        bw = _badge_width(tag, scale)
        if bx > 0 and bx + bw > avail_w:
            if row + 1 >= max_rows:
                overflow_start = i
                break
            row += 1
            bx = 0
        placements.append((tag, bx, row))
        bx += bw + gap
    if overflow_start < 0:
        return placements, None, 0
    # Build the +N badge
    last_row = max_rows - 1
    plus_n = n - overflow_start
    plus_text = '+{}'.format(plus_n)
    plus_bw = _badge_width(plus_text, scale)
    # Find the trailing x on the last row
    def _row_end_x():
        end = 0
        for _t, _x, _r in placements:
            if _r == last_row:
                end = _x + _badge_width(_t, scale) + gap
        return end
    px = _row_end_x()
    while px + plus_bw > avail_w and placements and placements[-1][2] == last_row:
        placements.pop()
        plus_n += 1
        plus_text = '+{}'.format(plus_n)
        plus_bw = _badge_width(plus_text, scale)
        px = _row_end_x()
    return placements, (px, last_row), plus_n


def _build_card_tooltip(item, include_description=True):
    """Rich-HTML tooltip showing full name, optional full description, and
    all categories/models with their original badge colours. Wrapped in a
    fixed-width container so long lists wrap to multiple lines instead of
    stretching off-screen. <br/> tags add vertical breathing room - Qt's
    tooltip rich-text engine ignores most CSS margins."""
    cats = getattr(item, 'tags_category', None) or []
    mods = getattr(item, 'tags_models', None) or []
    desc = getattr(item, 'description', None) or ''
    name = getattr(item, 'name', '') or ''
    parts = []
    if name:
        parts.append('<div style="font-weight:bold;color:#eee;">{}</div>'
                     .format(_html_escape(name)))
    sub = _subtitle(item)
    if sub:
        parts.append('<div style="color:#666;font-style:italic;">{}</div>'
                     .format(_html_escape(sub)))
    if include_description and desc:
        parts.append('<br/>')
        parts.append('<div style="color:#bbb;">{}</div>'
                     .format(_html_escape(desc)))
    if cats:
        parts.append('<br/>')
        parts.append('<div style="color:#777;font-weight:bold;">Categories</div>')
        parts.append(_tooltip_badges_html(cats, _COLOR_CAT))
    if mods:
        parts.append('<br/>')
        parts.append('<div style="color:#777;font-weight:bold;">Models</div>')
        parts.append(_tooltip_badges_html(mods, _COLOR_MOD))
    if not parts:
        return ''
    return ('<p style="white-space:pre-wrap;" width="380">'
            + ''.join(parts) + '</p>')


def _tooltip_badges_html(tags, colors):
    """Render tags as colored badges, separated by whitespace so Qt's
    rich-text engine can break long lines. `&nbsp;` is used INSIDE each
    badge so the badge itself stays atomic; regular spaces BETWEEN badges
    act as wrap points."""
    bg, fg = colors
    spans = [
        '<span style="background-color:{bg};color:{fg};">'
        '&nbsp;{t}&nbsp;</span>'.format(
            bg=bg, fg=fg, t=_html_escape(t))
        for t in tags
    ]
    return '<div>' + ' '.join(spans) + '</div>'


def _html_escape(s):
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


# ---------------------------------------------------------------------------
# Filter Sidebar
# ---------------------------------------------------------------------------
class _TagButton(QtWidgets.QPushButton):
    # Tooltip shows the full tag only when the rendered text doesn't fit
    # in the available width. Pattern: always register the tooltip (Qt
    # fires ToolTip events only when !toolTip().isEmpty()), then suppress
    # it in event() when the text is fully visible. Reacts dynamically
    # to splitter resize.
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setToolTip(text)

    def _is_truncated(self):
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        avail = self.width()
        if not self.icon().isNull():
            avail -= self.iconSize().width() + 4
        avail -= 4  # safety margin for the padding declared in the stylesheet
        return text_w > avail

    def event(self, ev):
        if ev.type() == QtCore.QEvent.ToolTip:
            if self._is_truncated():
                QtWidgets.QToolTip.showText(
                    ev.globalPos(), self.toolTip(), self)
            else:
                QtWidgets.QToolTip.hideText()
                ev.ignore()
            return True
        return super().event(ev)


class _FilterSection(QtWidgets.QWidget):
    """Sidebar sub-section (Categories or Models): header with mini search +
    scrollable list of tag toggle buttons. Layout/height are stable while the
    user types in the search; recompute happens only on populate or external
    resize."""

    changed = QtCore.Signal()

    def __init__(self, title, colors, title_color=None, parent=None):
        super().__init__(parent)
        self._title = title
        # `colors` is the full (bg, fg) tuple from _COLOR_CAT / _COLOR_MOD
        # used by the badges on cards. `title_color` is a separate, deeper
        # version of fg used here for the section title + checked checkbox
        # so the sidebar visual weight matches the badge's perceived
        # average (bg+fg weighted) instead of just the bright text inside.
        # Falls back to fg when not supplied.
        self._color_bg, self._color_fg = colors
        self._color_title = title_color if title_color is not None else self._color_fg
        self._buttons = {}         # tag -> QPushButton
        self.setStyleSheet('background:#222;')

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header: TITLE [search ×]
        header = QtWidgets.QWidget()
        header.setStyleSheet('background:#222;')
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(0, 6, 0, 2)
        hl.setSpacing(4)
        lbl = QtWidgets.QLabel(title)
        lbl.setStyleSheet('color:{};font-size:11px;font-weight:bold;'.format(self._color_title))
        hl.addWidget(lbl)
        hl.addStretch()
        self._search = NukomfyLineEdit()
        self._search.setPlaceholderText('Filter…')
        self._search.setClearButtonEnabled(True)
        self._search.addAction(material_icon(SEARCH, '#666', 12),
                               QtWidgets.QLineEdit.LeadingPosition)
        self._search.setFixedHeight(20)
        self._search.setStyleSheet(
            'QLineEdit{background:#1e1e1e;color:#ccc;font-size:10px;'
            'border:1px solid #333;border-radius:3px;padding:1px 4px;}'
            'QLineEdit:focus{border-color:#666;}')
        self._search.textChanged.connect(self._on_filter_changed)
        hl.addWidget(self._search, 1)
        v.addWidget(header)

        # Body: scroll area with vertical button list
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            'QScrollArea{background:#222;border:none;}' + SCROLLBAR_STYLE)
        self._inner = QtWidgets.QWidget()
        self._inner.setStyleSheet('background:#222;')
        self._list_layout = QtWidgets.QVBoxLayout(self._inner)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        self._list_layout.setAlignment(QtCore.Qt.AlignTop)
        self._scroll.setWidget(self._inner)
        v.addWidget(self._scroll, 1)

        # Empty when no tags - hide section completely
        self._empty = True

    # ── public API ─────────────────────────────────────────────────
    def populate(self, tags, active_tags, preserve_filter=False):
        """Rebuild buttons. By default also resets the per-section search;
        pass preserve_filter=True to re-apply the current search text after
        the rebuild (used when the scope changes due to global filters)."""
        kept_filter = self._search.text() if preserve_filter else ''

        # Tear down existing buttons
        while self._list_layout.count():
            it = self._list_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._buttons.clear()

        for tag in tags:
            btn = _TagButton(tag)
            btn.setCheckable(True)
            btn.setChecked(tag in active_tags)
            btn.setFocusPolicy(QtCore.Qt.NoFocus)
            btn.setStyleSheet(
                'QPushButton { color:#bbb;font-size:11px;background:transparent;'
                'border:none;text-align:left;padding:1px 0px; }')
            self._update_icon(btn, btn.isChecked())
            btn.toggled.connect(
                lambda c, _b=btn: self._on_btn_toggled(_b, c))
            self._list_layout.addWidget(btn)
            self._buttons[tag] = btn

        self._empty = not tags
        if preserve_filter:
            # Preserve the typed text and re-apply visibility.
            self._search.blockSignals(True)
            self._search.setText(kept_filter)
            self._search.blockSignals(False)
            self._apply_filter(kept_filter)
        else:
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        # Section is always visible: even empty sections keep their slot
        # so layout dimensions stay stable across filter changes.
        self.setVisible(True)

    def active_tags(self):
        return {t for t, b in self._buttons.items() if b.isChecked()}

    def reset(self):
        for b in self._buttons.values():
            b.setChecked(False)
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._apply_filter('')

    # ── internals ──────────────────────────────────────────────────
    def _update_icon(self, btn, checked):
        btn.setIcon(material_icon(
            CHECK_BOX if checked else CHECK_BOX_BLANK,
            self._color_title if checked else '#666', 14))

    def _on_btn_toggled(self, btn, checked):
        self._update_icon(btn, checked)
        self.changed.emit()

    def _on_filter_changed(self, text):
        self._apply_filter(text)

    def _apply_filter(self, text):
        needle = text.strip().lower()
        for tag, btn in self._buttons.items():
            btn.setVisible(needle in tag.lower() if needle else True)


class _FilterSidebar(QtWidgets.QWidget):
    """Library filter sidebar.

    Top zone (fixed height): Reset · Favorites · SOURCE Local/Shared.
    Bottom zone (flex): two _FilterSection widgets (Categories, Models)
    sharing the remaining vertical space 50/50.
    """

    changed = QtCore.Signal()
    reset_requested = QtCore.Signal()  # fires before `changed` on Reset click

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(_SIDEBAR_MIN_W)
        self.setStyleSheet('background:#222;')

        self._src_buttons = {}    # source -> QPushButton (toggle)
        self._fav_btn = None

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(3)
        self._top_layout = root

        self._build_source_section()

        # Sections container - flex zone below SOURCE separator
        self._sections_box = QtWidgets.QWidget()
        self._sections_box.setStyleSheet('background:#222;')
        sb_l = QtWidgets.QVBoxLayout(self._sections_box)
        sb_l.setContentsMargins(0, 0, 0, 0)
        sb_l.setSpacing(2)
        self._sections_layout = sb_l
        self._cat_section = _FilterSection('Categories', _COLOR_CAT, LIBRARY_CAT_TITLE)
        self._mod_section = _FilterSection('Models', _COLOR_MOD, LIBRARY_MOD_TITLE)
        self._cat_section.changed.connect(self.changed)
        self._mod_section.changed.connect(self.changed)
        sb_l.addWidget(self._cat_section)
        sb_l.addWidget(self._mod_section)
        root.addWidget(self._sections_box, 1)

    def _build_source_section(self):
        # Reset Filters button
        reset_btn = QtWidgets.QPushButton('Reset Filters')
        reset_btn.setIcon(material_icon(REFRESH, '#888', 12))
        reset_btn.setFixedHeight(22)
        reset_btn.setStyleSheet(
            'QPushButton{color:#888;font-size:10px;background:#1a1a1a;'
            'border:1px solid #333;border-radius:3px;}'
            'QPushButton:hover{color:#ccc;border-color:#666;}')
        reset_btn.setToolTip('Clear all active filters')
        reset_btn.clicked.connect(self.reset)
        self._top_layout.addWidget(reset_btn)

        sp1 = QtWidgets.QWidget()
        sp1.setFixedHeight(6)
        self._top_layout.addWidget(sp1)

        # Favorites filter
        self._fav_btn = QtWidgets.QPushButton('Favorites')
        self._fav_btn.setIcon(material_icon(STAR_BORDER, '#666', 12))
        self._fav_btn.setCheckable(True)
        self._fav_btn.setChecked(False)
        self._fav_btn.setFixedHeight(22)
        self._fav_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self._fav_btn.setStyleSheet(
            'QPushButton { color:#666;font-size:10px;background:#1a1a1a;'
            'border:1px solid #333;border-radius:3px; }'
            'QPushButton:checked { color:#f5c518;background:#3a3010;'
            'border-color:#f5c518; }')
        self._fav_btn.setToolTip('Show only workflows you marked as favorites')
        self._fav_btn.toggled.connect(self._on_fav_toggled)
        self._top_layout.addWidget(self._fav_btn)

        if settings.disable_local_workflows:
            # Local workflows disabled: skip the SOURCE filter (every
            # workflow is shared, so the Local/Shared toggle is meaningless).
            return

        sp2 = QtWidgets.QWidget()
        sp2.setFixedHeight(6)
        self._top_layout.addWidget(sp2)

        # SOURCE label
        lbl = QtWidgets.QLabel('Source')
        lbl.setStyleSheet('color:#777;font-size:11px;font-weight:bold;')
        self._top_layout.addWidget(lbl)

        # Local / Shared buttons
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        _SRC_TOOLTIPS = {
            'Local': 'Show workflows from your local library',
            'Shared': 'Show workflows from shared / team libraries',
        }
        for src in ('Local', 'Shared'):
            btn = QtWidgets.QPushButton(src)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setFixedHeight(22)
            color = _COLOR_SRC_TXT.get(src, '#aaa')
            btn.setStyleSheet(
                'QPushButton {{ color:#666;font-size:10px;background:#1a1a1a;'
                'border:1px solid #333;border-radius:3px; }}'
                'QPushButton:checked {{ color:{c};background:{bg};border-color:{c}; }}'
                .format(c=color, bg=_COLOR_SRC.get(src, '#222')))
            btn.setToolTip(_SRC_TOOLTIPS[src])
            btn.toggled.connect(lambda _checked: self.changed.emit())
            row.addWidget(btn)
            self._src_buttons[src] = btn
        self._top_layout.addLayout(row)

    def populate(self, cats, mods, active_tags=None, active_sources=None,
                 preserve_filter=False):
        """Rebuild Categories/Models lists. active_tags/active_sources restore
        saved state. When preserve_filter=True the per-section search text is
        kept (used on scope changes from global filters)."""
        if active_tags is None:
            active_tags = self.active_tags()
        if active_sources is None:
            active_sources = self.active_sources()

        self._cat_section.populate(cats, active_tags, preserve_filter=preserve_filter)
        self._mod_section.populate(mods, active_tags, preserve_filter=preserve_filter)

        # Restore source button states without triggering changed signal
        for src, btn in self._src_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(src in active_sources)
            btn.blockSignals(False)

        # Recompute proportional heights once (positions stable after this).
        self._recompute_section_heights()

    def active_tags(self):
        return self._cat_section.active_tags() | self._mod_section.active_tags()

    def active_sources(self):
        return {s for s, btn in self._src_buttons.items() if btn.isChecked()}

    def _on_fav_toggled(self, checked):
        if checked:
            self._fav_btn.setIcon(material_icon(STAR, '#f5c518', 12))
        else:
            self._fav_btn.setIcon(material_icon(STAR_BORDER, '#666', 12))
        self.changed.emit()

    def favorites_active(self):
        return self._fav_btn.isChecked() if self._fav_btn else False

    def set_favorites_active(self, active):
        if self._fav_btn:
            self._fav_btn.blockSignals(True)
            self._fav_btn.setChecked(active)
            self._update_fav_icon(active)
            self._fav_btn.blockSignals(False)

    def _update_fav_icon(self, checked):
        if checked:
            self._fav_btn.setIcon(material_icon(STAR, '#f5c518', 12))
        else:
            self._fav_btn.setIcon(material_icon(STAR_BORDER, '#666', 12))

    def reset(self):
        """Reset all filters to default (all sources on, no tags, no favorites,
        per-section searches cleared). Emits reset_requested first so the
        owning panel can clear the global workflow search too."""
        self.reset_requested.emit()
        self._cat_section.reset()
        self._mod_section.reset()
        for btn in self._src_buttons.values():
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)
        if self._fav_btn:
            self._fav_btn.blockSignals(True)
            self._fav_btn.setChecked(False)
            self._update_fav_icon(False)
            self._fav_btn.blockSignals(False)
        self.changed.emit()
        btn = self.focusWidget()
        if btn:
            btn.clearFocus()

    # ── height balancing ───────────────────────────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recompute_section_heights()

    def _recompute_section_heights(self):
        """Categories and Models always exist and split 50/50 of the
        available bottom-zone height as a cap. Heights are recomputed only
        on resize and on initial library open/refresh - not on filter
        changes - so layout stays stable while typing."""
        avail = self._sections_box.height()
        if avail <= 0:
            return
        cat_h = avail // 2
        mod_h = avail - cat_h
        self._cat_section.setMinimumHeight(0)
        self._mod_section.setMinimumHeight(0)
        self._cat_section.setMaximumHeight(max(1, cat_h))
        self._mod_section.setMaximumHeight(max(1, mod_h))


# ---------------------------------------------------------------------------
# Library Panel
# ---------------------------------------------------------------------------
class LibraryPanel(QtWidgets.QDialog):

    _instance = None

    @classmethod
    def show_panel(cls):
        if cls._instance is not None:
            try:
                if cls._instance.isVisible():
                    cls._instance.raise_()
                    cls._instance.activateWindow()
                    return
            except RuntimeError:
                # Instance was deleted by Qt
                cls._instance = None
        # When the user opted in to "Keep Library above Nuke", parent the
        # window to Nuke's main QMainWindow so Qt enforces parent-child
        # Z-order. Otherwise parent=None for a standalone OS window with
        # its own taskbar entry (current default behaviour).
        parent = _nuke_main_window() if settings.library_keep_on_top else None
        cls._instance = cls(parent=parent)
        cls._instance.show()
        # Bring it to the front on open, like the re-open branch above: X11
        # window managers don't auto-raise a newly shown Tool window over an
        # active sibling the way Windows does.
        cls._instance.raise_()
        cls._instance.activateWindow()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Nukomfy - Library')
        self.setMinimumSize(600, 400)
        if settings.library_keep_on_top and parent is not None:
            # Tool window: stays above the parent (Nuke), goes behind
            # other apps. No minimize button. Maximize button is a hint
            # that some platforms / window managers honour (Linux WMs
            # tend to; Windows tool windows hide it; macOS varies).
            self.setWindowFlags(
                QtCore.Qt.Tool
                | QtCore.Qt.WindowTitleHint
                | QtCore.Qt.WindowSystemMenuHint
                | QtCore.Qt.WindowMaximizeButtonHint
                | QtCore.Qt.WindowCloseButtonHint)
        else:
            self.setWindowFlags(
                QtCore.Qt.Window
                | QtCore.Qt.WindowTitleHint
                | QtCore.Qt.WindowSystemMenuHint
                | QtCore.Qt.WindowMinimizeButtonHint
                | QtCore.Qt.WindowMaximizeButtonHint
                | QtCore.Qt.WindowCloseButtonHint)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.destroyed.connect(self._on_destroyed)
        _focus_drop.install(self)

        self._all_items  = []
        self._last_scope_tags_key = None  # cache: (tuple(cats), tuple(mods))
        self._prev_filtered_ids = None    # cache: last filtered list identity
        self._prev_thumb = None           # last thumb size used by gif.update
        self._filtered   = []
        self._first_load = True
        s = ui_state.get('library_panel')
        self._list_mode = (s.get('view_mode', 'grid') == 'list')
        self._favorites = _load_favorites()
        self._autoplay = s.get('autoplay', True)

        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._apply_filters)

        self._build_ui()

        ui_state.restore_geometry('library_panel', self, with_position=True,
                                  fit=True)

        self.btn_list.setChecked(self._list_mode)
        self.btn_grid.setChecked(not self._list_mode)

        self.refresh()

    def _on_destroyed(self):
        """Safety net: stop GIFs even if closeEvent was not called (e.g. Nuke exit)."""
        try:
            self._gif.stop_all()
        except (RuntimeError, AttributeError):
            pass
        LibraryPanel._instance = None

    def closeEvent(self, event):
        from Nukomfy.gui.workers import stop_worker
        self._save_ui_state()
        self._debounce.stop()
        try:
            self.view.doubleClicked.disconnect(self._on_double_click)
        except (RuntimeError, TypeError):
            pass
        # Disconnect custom signal before stop_worker(): the generic helper
        # only handles common signal names, scanReady is custom and would
        # still deliver queued emits to a partially-destroyed widget.
        scan_w = getattr(self, '_scan_worker', None)
        if scan_w is not None:
            try:
                scan_w.scanReady.disconnect(self._on_scan_ready)
            except (RuntimeError, TypeError):
                pass
        self._scan_worker = stop_worker(scan_w)
        self._gif.stop_all()
        _pixmap_cache.clear()
        LibraryPanel._instance = None
        super().closeEvent(event)

    def _save_ui_state(self):
        ui_state.save_geometry('library_panel', self, with_position=True)
        state = dict(
            view_mode='list' if self._list_mode else 'grid',
            active_tags=sorted(self.sidebar.active_tags()),
            scale=self.scale_slider.value(),
            favorites_filter=self.sidebar.favorites_active(),
            autoplay=self._autoplay,
        )
        # With local workflows disabled there is no source filter; don't
        # overwrite the user's saved Local/Shared selection with an empty
        # set (it would reopen as an empty library if the flag is cleared).
        if not settings.disable_local_workflows:
            state['active_sources'] = sorted(self.sidebar.active_sources())
        ui_state.set('library_panel', **state)

    # ------------------------------------------------------------------
    def _build_ui(self):
        apply_window_chrome(self)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ────────────────────────────────────────────────────
        tb = QtWidgets.QWidget()
        tb.setStyleSheet('background:#252525;')
        tb.setFixedHeight(44)
        tbl = QtWidgets.QHBoxLayout(tb)
        tbl.setContentsMargins(10, 7, 10, 7)
        tbl.setSpacing(8)

        self.search = NukomfyLineEdit()
        self.search.setPlaceholderText('Search workflows…')
        self.search.setClearButtonEnabled(True)
        self.search.addAction(material_icon(SEARCH, '#666', 14),
                              QtWidgets.QLineEdit.LeadingPosition)
        self.search.setStyleSheet(SEARCH_FIELD_STYLE)
        self.search.textChanged.connect(self._debounce.start)

        self.count_lbl = QtWidgets.QLabel()
        # Pin a min width sized for the worst-case text we ever render
        # ('999/999 workflows') so the search bar next to it never
        # reflows when the label content changes (Loading… -> count).
        # Computed via QFontMetrics so we don't waste pixels on a
        # generous hardcoded value.
        self.count_lbl.setStyleSheet('color:#666;font-size:11px;')
        _f_count = QtGui.QFont(self.count_lbl.font())
        _f_count.setPixelSize(11)
        _fm_count = QtGui.QFontMetrics(_f_count)
        try:
            _w_count = _fm_count.horizontalAdvance('999/999 workflows')
        except AttributeError:
            _w_count = _fm_count.width('999/999 workflows')
        self.count_lbl.setMinimumWidth(_w_count + 4)
        self.count_lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        refresh_btn = QtWidgets.QPushButton(REFRESH)
        refresh_btn.setFont(icon_font(16))
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setToolTip('Reload')
        refresh_btn.setStyleSheet(_TOOLBAR_ICON_BTN_STYLE)
        refresh_btn.clicked.connect(self.refresh)

        self.btn_grid = QtWidgets.QPushButton(GRID_VIEW)
        self.btn_grid.setFont(icon_font(16))
        self.btn_grid.setFixedSize(28, 28)
        self.btn_grid.setToolTip('Grid view')
        self.btn_grid.setCheckable(True)
        self.btn_grid.setChecked(True)
        self.btn_grid.clicked.connect(lambda: self._set_view_mode(False))

        self.btn_list = QtWidgets.QPushButton(VIEW_LIST)
        self.btn_list.setFont(icon_font(16))
        self.btn_list.setFixedSize(28, 28)
        self.btn_list.setToolTip('List view')
        self.btn_list.setCheckable(True)
        self.btn_list.clicked.connect(lambda: self._set_view_mode(True))

        self.btn_anim = QtWidgets.QPushButton()
        self.btn_anim.setFont(icon_font(16))
        self.btn_anim.setFixedSize(28, 28)
        self.btn_anim.setToolTip('Toggle preview animations\n'
                                 'When off, GIFs animate only on hover')
        self._update_anim_btn()
        self.btn_anim.clicked.connect(self._toggle_autoplay)

        for btn in (self.btn_grid, self.btn_list):
            btn.setStyleSheet(
                'QPushButton{background:#1e1e1e;color:#888;border:1px solid #444;'
                'border-radius:3px;}'
                'QPushButton:checked{background:#3a2a10;color:' + ACCENT_GOLD +
                ';border-color:' + ACCENT_GOLD + ';}')

        # Workflow sort: criterion combo + direction button.
        s_cs = ui_state.get('library_panel')
        self._card_sort_key = s_cs.get('card_sort_key', 'name')
        if self._card_sort_key not in ('name', 'mtime', 'ctime'):
            self._card_sort_key = 'name'
        self._card_sort_dir = s_cs.get('card_sort_dir', 'asc')
        if self._card_sort_dir not in ('asc', 'desc'):
            self._card_sort_dir = 'asc'

        self.cmb_card_sort = NoWheelComboBox()
        self.cmb_card_sort.addItem('A-Z', 'name')
        self.cmb_card_sort.addItem('Date modified', 'mtime')
        self.cmb_card_sort.addItem('Date added', 'ctime')
        idx_map = {'name': 0, 'mtime': 1, 'ctime': 2}
        self.cmb_card_sort.setCurrentIndex(idx_map[self._card_sort_key])
        self.cmb_card_sort.setFixedHeight(28)
        # Pin a min width sized for the longest entry ('Date modified')
        # so toggling between A-Z and the date entries doesn't reflow
        # the toolbar width.
        _f_cs = QtGui.QFont(self.cmb_card_sort.font())
        _f_cs.setPixelSize(11)
        _fm_cs = QtGui.QFontMetrics(_f_cs)
        try:
            _w_cs = _fm_cs.horizontalAdvance('Date modified')
        except AttributeError:
            _w_cs = _fm_cs.width('Date modified')
        self.cmb_card_sort.setMinimumWidth(_w_cs + 32)
        self.cmb_card_sort.setStyleSheet(
            'QComboBox{background:#1e1e1e;color:#aaa;border:1px solid #444;'
            'border-radius:3px;padding:0 8px;font-size:11px;}'
            'QComboBox:hover{color:#ccc;border-color:#666;}'
            'QComboBox::drop-down{border:none;width:16px;}'
            'QComboBox QAbstractItemView{background:#1e1e1e;color:#ccc;'
            'border:1px solid #444;font-size:11px;'
            'selection-background-color:#3a2a10;'
            'selection-color:' + ACCENT_GOLD + ';}'
            'QComboBox QAbstractItemView::item{padding:3px 6px;}')
        self.cmb_card_sort.setToolTip('Sort workflows in the library')
        self.cmb_card_sort.currentIndexChanged.connect(self._on_card_sort_key_changed)

        self.btn_sort_dir = QtWidgets.QPushButton()
        self.btn_sort_dir.setFont(icon_font(16))
        self.btn_sort_dir.setFixedSize(28, 28)
        self.btn_sort_dir.setStyleSheet(_TOOLBAR_ICON_BTN_STYLE)
        self._update_sort_dir_button()
        self.btn_sort_dir.clicked.connect(self._on_card_sort_dir_clicked)

        # Scale slider - multiplier for thumbnail size and text
        s = ui_state.get('library_panel')
        scale_val = s.get('scale', 0)

        self.scale_slider = NoWheelSlider(QtCore.Qt.Horizontal)
        self.scale_slider.setRange(-5, 5)
        self.scale_slider.setValue(scale_val)
        self.scale_slider.setFixedWidth(84)
        self.scale_slider.setFixedHeight(20)
        self.scale_slider.setToolTip('Zoom: scale previews and text size')
        self.scale_slider.setStyleSheet(
            'QSlider::groove:horizontal{background:#333;height:4px;border-radius:2px;}'
            'QSlider::handle:horizontal{background:#666;width:12px;height:12px;'
            'margin:-4px 0;border-radius:6px;}'
            'QSlider::handle:horizontal:hover{background:#aaa;}')
        self.scale_slider.valueChanged.connect(self._on_scale_changed)

        self.scale_lbl = QtWidgets.QLabel('{}%'.format(int(100 + scale_val * 10)))
        self.scale_lbl.setStyleSheet('color:#666;font-size:10px;')
        self.scale_lbl.setFixedWidth(36)
        self.scale_lbl.setAlignment(QtCore.Qt.AlignCenter)

        # Add Workflow is omitted when local workflows are disabled: there
        # is nowhere to create one (shared roots are admin-managed).
        add_btn = None
        if not settings.disable_local_workflows:
            add_btn = QtWidgets.QPushButton('Add Workflow')
            set_press_icon(add_btn, ADD)
            add_btn.setFixedHeight(28)
            add_btn.clicked.connect(self._open_add_workflow)

        tbl.addWidget(self.search, 1)
        tbl.addWidget(self.count_lbl)
        tbl.addWidget(refresh_btn)
        tbl.addWidget(self.btn_grid)
        tbl.addWidget(self.btn_list)
        tbl.addWidget(self.btn_anim)
        tbl.addWidget(self.cmb_card_sort)
        tbl.addWidget(self.btn_sort_dir)
        tbl.addWidget(self.scale_slider)
        tbl.addWidget(self.scale_lbl)
        if add_btn is not None:
            tbl.addWidget(add_btn)
        root.addWidget(tb)

        # ── Body ───────────────────────────────────────────────────────
        self._body_splitter = DottedSplitter(QtCore.Qt.Horizontal)
        self._body_splitter.setHandleWidth(8)
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setStyleSheet('QSplitter{background:#222;}')

        self.sidebar = _FilterSidebar()
        self.sidebar.changed.connect(self._apply_filters)
        self.sidebar.reset_requested.connect(self._on_reset_requested)
        self._body_splitter.addWidget(self.sidebar)

        # Model / delegate / view
        self._model = _WorkflowModel(self)
        self._gif   = _GifManager(self._model, self)
        self._delegate = _CardDelegate(self._gif, self)

        self.view = QtWidgets.QListView()
        self._gif.attach_view(self.view)
        self.view.setModel(self._model)
        self.view.setItemDelegate(self._delegate)
        self.view.setMouseTracking(True)
        self.view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.view.setResizeMode(QtWidgets.QListView.Adjust)
        self.view.setMovement(QtWidgets.QListView.Static)
        self.view.setDragEnabled(False)
        # Force pixel-based scrolling so the mouse wheel feels fluid even
        # with tall cards. setSingleStep also overrides Qt's default which
        # in ScrollPerItem mode would be one item per wheel notch (huge
        # jump on 200+ px cards).
        self.view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(20)
        self.view.viewport().setCursor(QtCore.Qt.PointingHandCursor)
        self.view.setStyleSheet(
            'QListView{background:#262626;border:none;outline:none;}'
            + SCROLLBAR_STYLE)

        self._delegate._favorites = self._favorites
        self._delegate.star_clicked.connect(self._toggle_favorite)
        self._gif.set_autoplay(self._autoplay)

        self.view.doubleClicked.connect(self._on_double_click)
        self.view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        self.view.viewport().installEventFilter(self)

        self._body_splitter.addWidget(self.view)
        self._body_splitter.setStretchFactor(0, 0)
        self._body_splitter.setStretchFactor(1, 1)

        # Initial sidebar width: persisted value, clamped to [min, max]
        s_state = ui_state.get('library_panel')
        saved_w = s_state.get('sidebar_width', None)
        if saved_w:
            initial_sidebar_w = int(saved_w)
        else:
            initial_sidebar_w = 197
        # Defer applying sizes until view has a real width (after show)
        self._initial_sidebar_w = max(_SIDEBAR_MIN_W, initial_sidebar_w)

        self._body_splitter.splitterMoved.connect(self._on_splitter_moved)
        self._sidebar_save_timer = QtCore.QTimer(self)
        self._sidebar_save_timer.setSingleShot(True)
        self._sidebar_save_timer.setInterval(300)
        self._sidebar_save_timer.timeout.connect(self._save_sidebar_width)

        root.addWidget(self._body_splitter, 1)

    # ------------------------------------------------------------------
    # Splitter sizing
    # ------------------------------------------------------------------
    def _min_view_width_for_one_card(self):
        """Pixel width the workflow view needs to display at least one card."""
        thumb = getattr(self._delegate, '_thumb', THUMB_SIZE)
        # _grid_item card width = thumb + 16 (cf. _CardDelegate.sizeHint)
        # plus QListView spacing on each side and a bit of padding for the scrollbar
        return thumb + 16 + GRID_SPACING * 2 + 12

    def _max_sidebar_width(self):
        total = self._body_splitter.width()
        if total <= 0:
            return _SIDEBAR_MIN_W
        return max(_SIDEBAR_MIN_W, total - self._min_view_width_for_one_card())

    def _apply_splitter_sizes(self, sidebar_w):
        total = self._body_splitter.width()
        if total <= 0:
            return
        sw = max(_SIDEBAR_MIN_W, min(sidebar_w, self._max_sidebar_width()))
        self._body_splitter.setSizes([sw, max(0, total - sw)])

    def _on_splitter_moved(self, pos, index):
        if getattr(self, '_in_splitter_clamp', False):
            return
        sizes = self._body_splitter.sizes()
        if not sizes:
            return
        sidebar_w = sizes[0]
        max_w = self._max_sidebar_width()
        if sidebar_w > max_w:
            self._in_splitter_clamp = True
            try:
                self._apply_splitter_sizes(max_w)
            finally:
                self._in_splitter_clamp = False
        # Debounced persist
        self._sidebar_save_timer.start()

    def _save_sidebar_width(self):
        try:
            sizes = self._body_splitter.sizes()
            if not sizes:
                return
            sidebar_w = sizes[0]
            ui_state.set('library_panel',
                         sidebar_width=sidebar_w)
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        # Reopening the Library re-centres every comparison slider.
        try:
            self._delegate._hover_split.clear()
        except AttributeError:
            pass
        if not getattr(self, '_splitter_initialized', False):
            QtCore.QTimer.singleShot(0, self._init_splitter_sizes)
            self._splitter_initialized = True

    def _init_splitter_sizes(self):
        self._apply_splitter_sizes(self._initial_sidebar_w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # On window shrink, ensure sidebar respects the new max
        if getattr(self, '_splitter_initialized', False):
            sizes = self._body_splitter.sizes()
            if sizes:
                max_w = self._max_sidebar_width()
                if sizes[0] > max_w:
                    self._apply_splitter_sizes(max_w)

    # ------------------------------------------------------------------
    # Hover tracking via event filter on viewport
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.view.viewport():
            etype = event.type()
            if etype == QtCore.QEvent.MouseMove:
                idx = self.view.indexAt(event.pos())
                new_row = idx.row() if idx.isValid() else -1
                old_row = self._delegate._hover_row
                item = (idx.data(_WorkflowModel.ItemRole)
                        if idx.isValid() else None)
                vis_rect = self.view.visualRect(idx) if item else None

                # Check if mouse is over the star rect
                old_star = self._delegate._hover_star
                on_star = False
                if item:
                    opt = type('O', (), {'rect': vis_rect})()
                    sr = self._delegate._star_rect(opt, item)
                    on_star = sr.contains(event.pos())
                self._delegate._hover_star = on_star

                # Comparison slider: the split tracks the mouse only while the
                # cursor is over the preview itself - moving onto the card's
                # text area leaves the slider where it was.
                split_changed = False
                if item is not None and item.has_comparison:
                    tr = self._delegate._thumb_rect(vis_rect)
                    if tr.width() > 0 and tr.contains(event.pos()):
                        frac = (event.pos().x() - tr.x()) / float(tr.width())
                        frac = max(0.0, min(1.0, frac))
                        key = _split_key(item)
                        if self._delegate._hover_split.get(key) != frac:
                            self._delegate._hover_split[key] = frac
                            split_changed = True

                if new_row != old_row:
                    self._delegate._hover_row = new_row
                    old_item = (self._model.item_at(old_row)
                                if old_row >= 0 else None)
                    new_item = (self._model.item_at(new_row)
                                if new_row >= 0 else None)
                    # Hover-scrub slider: leaving a comparison card returns it
                    # to centre (drop its stored split position). When the user
                    # opts out of reset-on-leave, the split is kept until the
                    # Library is reopened (showEvent clears it).
                    if (settings.library_compare_reset_on_leave
                            and old_item is not None
                            and old_item.has_comparison):
                        self._delegate._hover_split.pop(
                            _split_key(old_item), None)
                    # Hover-driven animation (autoplay off): play every GIF on
                    # the card - the primary plus the optional comparison
                    # second image.
                    if not self._autoplay:
                        if old_item:
                            for p in _gif_paths(old_item):
                                self._gif.hover_stop(p)
                        if new_item:
                            for p in _gif_paths(new_item):
                                self._gif.hover_start(p)
                    if old_row >= 0:
                        self.view.update(self._model.index(old_row, 0))
                    if new_row >= 0:
                        self.view.update(self._model.index(new_row, 0))
                elif (on_star != old_star or split_changed) and new_row >= 0:
                    # Star hover or slider split changed within same row
                    self.view.update(self._model.index(new_row, 0))
            elif etype == QtCore.QEvent.Leave:
                old_row = self._delegate._hover_row
                self._delegate._hover_row = -1
                self._delegate._hover_star = False
                if old_row >= 0:
                    old_item = self._model.item_at(old_row)
                    # Leaving a comparison card returns its slider to centre,
                    # unless the user opted out of reset-on-leave (then the
                    # split persists until the Library is reopened).
                    if (settings.library_compare_reset_on_leave
                            and old_item is not None
                            and old_item.has_comparison):
                        self._delegate._hover_split.pop(
                            _split_key(old_item), None)
                    if not self._autoplay and old_item:
                        for p in _gif_paths(old_item):
                            self._gif.hover_stop(p)
                    self.view.update(self._model.index(old_row, 0))
            elif etype == QtCore.QEvent.Resize and self._list_mode:
                # _list_item_height reads viewport().width() to size the
                # text column; cached heights become stale on viewport
                # resize. Drop them and let Qt re-query sizeHint.
                self._delegate._height_cache.clear()
                self.view.scheduleDelayedItemsLayout()
        return super().eventFilter(obj, event)

    def on_card_visibility_changed(self):
        """Triggered by the Settings panel when a library_*_show_* key
        flipped. Cache wipe + viewport invalidate forces a relayout."""
        try:
            self._delegate.refresh_visibility()
            self.view.scheduleDelayedItemsLayout()
            self.view.viewport().update()
        except (RuntimeError, AttributeError):
            pass

    def on_compare_reset_changed(self):
        """Triggered by the Settings panel when reset-on-leave flips. If it
        is now on, recentre every card's slider at once so the change shows
        without re-hovering each card; if off, leave the splits untouched."""
        if not settings.library_compare_reset_on_leave:
            return
        try:
            self._delegate._hover_split.clear()
            self.view.viewport().update()
        except (RuntimeError, AttributeError):
            pass

    # ------------------------------------------------------------------
    def _on_reset_requested(self):
        """Reset Filters click - also clear the global workflow search.
        Block signals so we don't double-trigger _apply_filters; the
        sidebar's `changed` signal (emitted right after) handles refresh."""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)

    def _on_scale_changed(self, value):
        pct = int(100 + value * 10)
        self.scale_lbl.setText('{}%'.format(pct))
        self._debounce.start()

    # Workflow card sort
    def _update_sort_dir_button(self):
        is_asc = self._card_sort_dir == 'asc'
        glyph = ARROW_DOWNWARD if is_asc else ARROW_UPWARD
        self.btn_sort_dir.setText(glyph)
        if self._card_sort_key == 'name':
            label = 'A → Z' if is_asc else 'Z → A'
        else:
            label = 'oldest first' if is_asc else 'newest first'
        self.btn_sort_dir.setToolTip(
            'Direction: {} - click to invert'.format(label))

    def _on_card_sort_key_changed(self, _idx):
        key = self.cmb_card_sort.currentData() or 'name'
        if key == self._card_sort_key:
            return
        self._card_sort_key = key
        try:
            ui_state.set('library_panel', card_sort_key=key)
        except Exception:
            pass
        self._update_sort_dir_button()
        self._apply_filters()

    def _on_card_sort_dir_clicked(self):
        self._card_sort_dir = 'desc' if self._card_sort_dir == 'asc' else 'asc'
        try:
            ui_state.set('library_panel', card_sort_dir=self._card_sort_dir)
        except Exception:
            pass
        self._update_sort_dir_button()
        self._apply_filters()

    def _sort_filtered_in_place(self):
        key = self._card_sort_key
        reverse = (self._card_sort_dir == 'desc')
        if key == 'name':
            self._filtered.sort(key=lambda i: i.name.lower(), reverse=reverse)
        elif key == 'mtime':
            self._filtered.sort(key=lambda i: i.mtime, reverse=reverse)
        elif key == 'ctime':
            self._filtered.sort(key=lambda i: i.ctime, reverse=reverse)

    def _clamp_sidebar_width(self):
        """If the sidebar width currently exceeds the dynamic max (which
        depends on thumb size), shrink it. Called after view re-configure."""
        if not getattr(self, '_splitter_initialized', False):
            return
        sizes = self._body_splitter.sizes()
        if not sizes:
            return
        max_w = self._max_sidebar_width()
        if sizes[0] > max_w:
            self._apply_splitter_sizes(max_w)


    def _set_view_mode(self, list_mode):
        self._list_mode = list_mode
        self.btn_list.setChecked(list_mode)
        self.btn_grid.setChecked(not list_mode)
        self._apply_filters()

    # ------------------------------------------------------------------
    def _configure_view(self, scale):
        """Set QListView mode and grid size based on current settings."""
        self._delegate.configure(self._list_mode, scale)
        if self._list_mode:
            self.view.setViewMode(QtWidgets.QListView.ListMode)
            self.view.setWrapping(False)
            self.view.setSpacing(GRID_SPACING // 2)
            # Heights are now dynamic in list mode (content_required
            # depends on visible fields and viewport width), so uniform
            # sizing must be off.
            self.view.setUniformItemSizes(False)
            self.view.setGridSize(QtCore.QSize())   # clear grid size
        else:
            self.view.setViewMode(QtWidgets.QListView.IconMode)
            self.view.setWrapping(True)
            self.view.setUniformItemSizes(False)
            self.view.setGridSize(QtCore.QSize())   # per-item sizing
            self.view.setSpacing(GRID_SPACING // 2)

    # ------------------------------------------------------------------
    def refresh(self):
        """Asynchronous disk rescan via WorkflowScanWorker. The slow
        scan_workflows() I/O runs on a background thread so the UI is
        never blocked even with hundreds of workflow folders. Result is
        delivered via _on_scan_ready() once the thread emits scanReady.
        Mid-flight refreshes cancel the previous worker."""
        from Nukomfy.utils.path_utils import runtime_path
        from Nukomfy.gui.workers import WorkflowScanWorker, stop_worker
        _pixmap_cache.clear()
        self._last_scope_tags_key = None
        self._prev_filtered_ids = None
        self._prev_thumb = None
        self._scan_worker = stop_worker(getattr(self, '_scan_worker', None))
        try:
            self.count_lbl.setText('Loading…')
        except Exception:
            pass
        local = ('' if settings.disable_local_workflows
                 else runtime_path(settings.local_workflow_path,
                                   fallback=settings.local_workflow_path))
        shared = [runtime_path(p, fallback=p)
                  for p in settings.shared_workflow_paths]
        self._scan_worker = WorkflowScanWorker(local, shared, self)
        self._scan_worker.scanReady.connect(self._on_scan_ready)
        self._scan_worker.start()

    def _on_scan_ready(self, items, name_dups, name_folder_dups):
        """Handler for WorkflowScanWorker result. Runs on the main
        thread (Qt queued connection)."""
        # Late delivery guard: if the panel was closed while the worker
        # was still scanning, the queued signal can fire after closeEvent
        # has nulled the singleton - accessing self.* here would crash.
        if LibraryPanel._instance is not self:
            return
        self._all_items = list(items)
        self._delegate.set_collisions(name_dups, name_folder_dups)
        cats, mods = workflow_loader.collect_tags(self._all_items)
        if self._first_load:
            self._first_load = False
            s = ui_state.get('library_panel')
            self.sidebar.populate(cats, mods,
                active_tags=set(s.get('active_tags', [])),
                active_sources=set(s.get('active_sources', ['Local', 'Shared'])),
            )
            self.sidebar.set_favorites_active(s.get('favorites_filter', False))
        else:
            self.sidebar.populate(cats, mods)
        self._apply_filters()

    def _apply_filters(self):
        active_sources = self.sidebar.active_sources()
        # Scope = items selected by global filters (search text, sources,
        # favorites). Tag selections (Categories/Models) are NOT applied
        # here, so the sidebar lists only contain tags actually present
        # in the current scope.
        scope = workflow_loader.filter_items(
            self._all_items,
            text=self.search.text(),
            active_tags=None,
            active_sources=(None if settings.disable_local_workflows
                            else (active_sources
                                  if active_sources != {'Local', 'Shared'}
                                  else None))
        )
        fav_active = self.sidebar.favorites_active()
        self._delegate._favorites_filter_active = fav_active
        if fav_active:
            scope = [i for i in scope
                     if i.workflow_id and i.workflow_id in self._favorites]

        # Rebuild sidebar tag buttons from scope, preserving the per-section
        # search text. Currently-selected tags that disappear from scope are
        # auto-dropped (their button is no longer rendered). The rebuild is
        # skipped when the scope tag-set is unchanged from the previous
        # call - populating ~125 QPushButtons every keystroke is the main
        # bottleneck on big libraries; toggling source/favorites/tag/view/
        # scale typically leaves the scope set unchanged.
        scope_cats, scope_mods = workflow_loader.collect_tags(scope)
        scope_key = (tuple(scope_cats), tuple(scope_mods))
        if scope_key != self._last_scope_tags_key:
            self._last_scope_tags_key = scope_key
            prev_active_tags = self.sidebar.active_tags()
            self.sidebar.blockSignals(True)
            try:
                self.sidebar.populate(
                    scope_cats, scope_mods,
                    active_tags=prev_active_tags,
                    active_sources=active_sources,
                    preserve_filter=True,
                )
            finally:
                self.sidebar.blockSignals(False)

        # Now apply tag selections (intersected with what survived in scope)
        active_tags = self.sidebar.active_tags()
        if active_tags:
            self._filtered = [i for i in scope if all(
                tag in i.tags_category + i.tags_models + [i.source]
                for tag in active_tags)]
        else:
            self._filtered = list(scope)

        # Apply user-chosen sort to the filtered list.
        self._sort_filtered_in_place()

        scale = 1.0 + self.scale_slider.value() * 0.1
        self._configure_view(scale)
        self._clamp_sidebar_width()

        # Skip the model reset and GIF update when the filtered list and
        # thumb size are identical to the previous call. Toggling source/
        # favorites/tag/view/scale often produces the same result set as
        # before; resetting the model otherwise scrolls back to the top
        # and re-creates QMovie instances unnecessarily.
        thumb_now = int(THUMB_SIZE * scale)
        filtered_ids = tuple(id(it) for it in self._filtered)
        if (filtered_ids != self._prev_filtered_ids
                or thumb_now != self._prev_thumb):
            self._prev_filtered_ids = filtered_ids
            self._prev_thumb = thumb_now
            self._model.set_items(self._filtered)
            self._gif.update(self._filtered, thumb_now)

        total = len(self._all_items)
        shown = len(self._filtered)
        if shown == total:
            self.count_lbl.setText('{} workflows'.format(total))
        elif shown == 0:
            self.count_lbl.setText('No workflows')
        else:
            self.count_lbl.setText('{}/{} workflows'.format(shown, total))

    # ------------------------------------------------------------------
    def _toggle_favorite(self, workflow_id):
        """Toggle a workflow's favorite status by UUID."""
        if not workflow_id:
            return
        if workflow_id in self._favorites:
            self._favorites.discard(workflow_id)
        else:
            self._favorites.add(workflow_id)
        _save_favorites(self._favorites)
        self._delegate._favorites = self._favorites
        # If favorites filter is active, re-filter; otherwise just repaint
        if self.sidebar.favorites_active():
            self._apply_filters()
        else:
            self.view.viewport().update()

    def _update_anim_btn(self):
        """Update the animation button icon and style based on state."""
        if self._autoplay:
            self.btn_anim.setText(PLAY_ARROW)
            self.btn_anim.setStyleSheet(
                'QPushButton{background:#1e3a1e;color:#4caf50;border:1px solid #4caf50;'
                'border-radius:3px;}'
                'QPushButton:hover{background:#2a4f2a;}'
                'QPushButton:pressed{background:#163016;}')
        else:
            self.btn_anim.setText(PAUSE)
            self.btn_anim.setStyleSheet(
                'QPushButton{background:#3a2a1a;color:' + ACCENT_GOLD +
                ';border:1px solid ' + ACCENT_GOLD + ';'
                'border-radius:3px;}'
                'QPushButton:hover{background:#4a3622;}'
                'QPushButton:pressed{background:#2e2010;}')

    def _toggle_autoplay(self):
        """Toggle GIF autoplay mode."""
        self._autoplay = not self._autoplay
        self._update_anim_btn()
        self._gif.set_autoplay(self._autoplay)
        self.view.viewport().update()

    # ------------------------------------------------------------------
    def _on_double_click(self, index):
        if not self.isVisible():
            return
        item = index.data(_WorkflowModel.ItemRole)
        if item:
            # Defer: let Qt finish dispatching mouseDoubleClickEvent before
            # touching the Nuke DAG (avoids use-after-free on the view).
            QtCore.QTimer.singleShot(0, lambda it=item: self._import_gizmo(it))

    def _on_context_menu(self, pos):
        index = self.view.indexAt(pos)
        item = index.data(_WorkflowModel.ItemRole) if index.isValid() else None
        if not item:
            return
        self._context_menu(item, self.view.viewport().mapToGlobal(pos))

    def _context_menu(self, item, global_pos):
        menu = QtWidgets.QMenu(self)

        act_import = menu.addAction('Import as Gizmo')
        act_import.triggered.connect(lambda: self._import_gizmo(item))

        if item.source == 'Local':
            act_edit = menu.addAction('Edit Workflow')
            act_edit.triggered.connect(lambda: self._edit_workflow(item))

        # "Show in File Browser": omitted for shared workflows when shared
        # folders are locked to the override, so their on-disk location
        # stays hidden (local workflows keep it).
        if not (settings.lock_shared_folders and item.source != 'Local'):
            menu.addSeparator()
            act_folder = menu.addAction('Show in File Browser')
            act_folder.triggered.connect(lambda: self._reveal(item))

        menu.exec_(global_pos)

    def _import_gizmo(self, item):
        if getattr(item, 'id_conflict', False):
            if not self._confirm_duplicate_id_insert(item):
                return
        try:
            from Nukomfy.gizmos.gizmo_builder import build_gizmo
            build_gizmo(item)
        except Exception as e:
            _dialogs.warn(
                self, 'Could not create gizmo',
                'Error creating gizmo:\n{}'.format(e)
            )

    def _confirm_duplicate_id_insert(self, item):
        if item.source == 'Local':
            advice = ('Open <b>Edit Workflow</b> and click '
                      '<b>Regenerate ID</b> to make it unique.')
        else:
            advice = ('Copy this workflow to your <b>Local</b> library, '
                      'then open <b>Edit Workflow</b> and click '
                      '<b>Regenerate ID</b> to make it unique.')
        box = _dialogs.message_box(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle('Duplicate Workflow ID')
        box.setTextFormat(QtCore.Qt.RichText)
        box.setText(
            'This workflow shares its ID with another workflow.<br><br>'
            'The gizmo will work now, but if the source folder is '
            'renamed or moved the wrong workflow may be loaded.<br><br>'
            + advice
        )
        import_btn = box.addButton('Import anyway',
                                   QtWidgets.QMessageBox.AcceptRole)
        box.addButton('Cancel', QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(import_btn)
        box.exec_()
        return box.clickedButton() is import_btn

    def _edit_workflow(self, item):
        from Nukomfy.gui.add_workflow import AddWorkflowDialog
        dlg = AddWorkflowDialog(parent=self, workflow_item=item)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.refresh()

    def _reveal(self, item):
        from Nukomfy.utils.reveal import reveal_folder
        reveal_folder(item.folder_path)

    def _open_add_workflow(self):
        from Nukomfy.gui.add_workflow import AddWorkflowDialog
        dlg = AddWorkflowDialog(parent=self)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.refresh()
