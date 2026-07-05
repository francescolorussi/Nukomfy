"""Nukomfy package entry point.

Configures the `nukomfy` root logger and registers Nuke menu entries.
"""
import logging
import threading

import nuke

from Nukomfy.version import __title__, __version__

_APP_NAME = __title__
_APP_VERSION = __version__

_log = logging.getLogger('Nukomfy')
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        fmt=f'[{_APP_NAME}] %(levelname)s: %(message)s'))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def _open_library():
    from Nukomfy.gui.library_panel import LibraryPanel
    LibraryPanel.show_panel()


def _open_render_queue():
    from Nukomfy.gui.render_queue_panel import show_render_queue
    show_render_queue()


def _open_settings():
    from Nukomfy.gui.settings_panel import SettingsPanel
    dlg = SettingsPanel()
    dlg.exec()
    # A machine removed or disabled in Settings must stop showing in an
    # already-open Render Manager. Reconcile its rows + store vs the live list.
    from Nukomfy.gui import render_queue_panel as _rq
    _rm = getattr(_rq, '_instance', None)
    if _rm is not None:
        try:
            _rm.reconcile_machines()
        except RuntimeError:
            pass


def _open_about():
    from Nukomfy.gui.about_dialog import AboutDialog
    dlg = AboutDialog()
    dlg.exec()


def _auto_purge_input_cache():
    try:
        from Nukomfy.core.settings import settings
        from Nukomfy.utils.path_utils import runtime_path
        import Nukomfy.data.input_cache_cleanup as input_cache_cleanup
        max_age = int(settings.input_cache_max_age_days or 0)
        if max_age <= 0:
            return
        raw = settings.default_input_cache_path
        base = runtime_path(raw, fallback=raw)
        purged_count, freed = input_cache_cleanup.purge_older_than(base, max_age)
        if purged_count:
            _log.info(
                'Input cache purged: %d dirs (%.1f MB, age > %d d) at %s',
                purged_count, freed / 1e6, max_age, input_cache_cleanup.user_scope_root(base))
    except Exception:
        _log.exception('Input cache auto-purge failed (non-fatal)')


def _eager_import_consumers():
    """Eagerly import plugin modules so their internal bindings resolve
    against a consistent module state at boot time."""
    try:
        from Nukomfy.core.identity import current_user as _cu
        _cu()
        import Nukomfy.client.manager_client
        import Nukomfy.data.input_cache_cleanup
        import Nukomfy.data.input_cache_writer
        import Nukomfy.utils.output_path
        import Nukomfy.gui.render_queue_actions
        import Nukomfy.gui.render_queue_myjobs
        import Nukomfy.gui.render_queue_panel
        import Nukomfy.gui.submit_panel
    except Exception:
        _log.exception('Eager import failed (non-fatal)')


_eager_import_consumers()

threading.Thread(
    target=_auto_purge_input_cache, daemon=True,
    name='nukomfy-cache-purge').start()

menu = nuke.menu('Nuke').addMenu('Nukomfy')
menu.addCommand('Library', _open_library)
menu.addCommand('Render Manager', _open_render_queue)
menu.addCommand('Settings', _open_settings)
menu.addSeparator()
menu.addCommand('About', _open_about)

_log.info('Nukomfy v%s ready', _APP_VERSION)
