"""Plugin settings singleton with attribute dispatch and admin overrides."""

import json
import logging
import os


_log = logging.getLogger(__name__)

_FILE = os.path.join(os.path.expanduser('~'), '.nuke', 'nukomfy_settings.json')

# Read-only overrides pushed by an admin. Folder lives at the package
# root, NOT inside core/, so the path resolution must walk up one level
# from this file's location.
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_OVERRIDES_DIR = os.path.join(_PLUGIN_ROOT, 'settings_overrides')


def load_override_json(filename):
    """Read settings_overrides/<filename>. Return None if missing/malformed.

    Boot-only read - no file watcher, no live reload. Artists must
    restart Nuke to pick up changes pushed by the admin.
    """
    path = os.path.join(SETTINGS_OVERRIDES_DIR, filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

# Logical template (always '/'-based - the engine accepts both '/' and '\\'
# and normalises to '/' before format/runtime). The default seeded into
# Settings on first run uses OS-native separators so the field, the
# placeholder, the Reset button, and the preview all read consistently
# for the user's local OS. Keep in sync with
# output_path.DEFAULT_OUTPUT_TEMPLATE (duplicated to avoid a circular
# import).
_DEFAULT_TEMPLATE_LOGICAL = (
    '{nk_file}/{workflow}/{output_name}/{version}'
    '/{nk_file}_{output_name}_{version}.{frame}.{ext}'
)

_DEFAULTS = {
    'local_workflow_path': os.path.join(os.path.expanduser('~'), 'Nukomfy', 'workflows'),
    'shared_workflow_paths': [],
    'default_input_cache_path': os.path.join(os.path.expanduser('~'), 'Nukomfy', 'input_cache'),
    'default_output_path': os.path.join(os.path.expanduser('~'), 'Nukomfy', 'output'),
    'history_limit': 10,
    # Cap on entries kept in submit_history.json. Distinct from
    # `history_limit` (which is a per-machine view cap inside the Render
    # Manager sub-table). When the file exceeds this count, oldest
    # entries by `sent_at` are dropped at next `_save`.
    'local_history_max_entries': 100,
    'auto_refresh_enabled': True,
    'auto_refresh_interval': 30,
    'path_substitution_enabled': True,
    'output_path_template': _DEFAULT_TEMPLATE_LOGICAL.replace('/', os.sep),
    # 0 = disabled. When > 0, boot hook purges input cache dirs whose
    # last_used_utc is older than this many days.
    'input_cache_max_age_days': 0,
    # When True, every submit cleans up sibling fp variants of the SAME
    # input cache that are no longer in use by any running render. ON by
    # default keeps the cache lean; turning OFF accumulates variants
    # across submits (see TTL setting above for eventual cleanup).
    # Scoped to the current user's branch only.
    'delete_unused_variants_on_submit': True,
    # Active jobs we can't reconcile with their machine (server unreachable
    # or 404) are frozen as "Unknown" in History after this many days since
    # submit. 1-90. See MyJobs Active wiring for reconciler behaviour.
    'lost_job_timeout_days': 1,
    # Per-panel: when True, the panel is owned by Nuke's main window so
    # it stays above Nuke (cross-platform) but loses the minimize button
    # and the separate taskbar/dock entry. Applied at panel open: toggle
    # then reopen the panel for the change to take effect. Default on.
    'library_keep_on_top': True,
    'render_manager_keep_on_top': True,
    # Frame number padding. Forces every NukomfyWrite to use this padding
    # regardless of what the workflow JSON had. Range [1, 9].
    'frame_padding': 4,
    # Version string padding (e.g. v001 with 3, v0001 with 4). Range [1, 9].
    'version_padding': 3,
    # Gizmo node naming: when True, a new gizmo's Nuke node name is prefixed
    # with "Nukomfy_" (e.g. Nukomfy_MyWorkflow); when False the node uses the
    # workflow name only. Applied at gizmo creation, existing nodes unchanged.
    'gizmo_name_prefix': True,
    # Group View (Nuke 16+): when True, a new gizmo is created with the native
    # disable_group_view knob checked, so its internals cannot be shown inline
    # on the node graph. Guarded by knob presence at creation (Nuke 14 has no
    # Group View). Applied at gizmo creation, existing nodes unchanged.
    'gizmo_disable_group_view': False,
    # Threshold above which Submit asks for confirmation. 0 = disabled.
    # Range [0, 100] (max aligned with the batch_count widget cap).
    'batch_warning_threshold': 20,
    # Library card field visibility. 12 independent toggles (6 fields x
    # 2 view modes). When False, the field is skipped both in height
    # computation and paint, so the card shrinks accordingly. List view
    # height becomes dynamic when fields are hidden. All default True
    # (full-card appearance) except grid Description, which defaults False.
    'library_grid_show_version': True,
    'library_grid_show_author': True,
    'library_grid_show_description': False,
    'library_grid_show_categories': True,
    'library_grid_show_models': True,
    'library_grid_show_source': True,
    'library_list_show_version': True,
    'library_list_show_author': True,
    'library_list_show_description': True,
    'library_list_show_categories': True,
    'library_list_show_models': True,
    'library_list_show_source': True,
    # Before/after comparison slider reset behaviour. When True, each card's
    # slider snaps back to centre as soon as the pointer leaves the card.
    # When False (default), the slider keeps its position until the Library
    # is closed and reopened.
    'library_compare_reset_on_leave': False,
    # Hidden deployment flags (no UI control - set via the settings
    # override file or by hand in the user JSON; all default False =
    # current behavior). disable_local_workflows: the library and submit
    # read shared roots only, the local path is ignored. lock_shared_folders:
    # shared paths come from the override only, user-added shared paths are
    # ignored and the Manage button is greyed. lock_machines: the machine
    # list comes from the override only, user machines are ignored and the
    # add/edit/remove/reorder controls are greyed.
    'disable_local_workflows': False,
    'lock_shared_folders': False,
    'lock_machines': False,
}


def resolve_path(path):
    """Resolve a path string that may contain Nuke TCL expressions.
    Examples:
      "[getenv NUKE_TEMP_DIR]/workflows"
      "[file dirname [value root.name]]/output"
    Uses nuke.tcl('subst') so [cmd] and $var substitutions are evaluated.
    Falls back to the raw string if nuke is unavailable or evaluation fails.
    """
    if not path:
        return path
    # Normalise to forward slashes - avoids TCL interpreting backslash
    # sequences (\f = formfeed, \n = newline, etc.) in Windows paths,
    # and Nuke handles forward slashes on all platforms.
    path = path.replace('\\', '/')
    try:
        import nuke
        resolved = nuke.tcl('subst', path)
        return resolved if resolved is not None else path
    except Exception:
        return path

# Keys whose empty/falsy value falls back to the default (otherwise the
# user could blank out the path from the UI and end up with "").
_FALLBACK_ON_EMPTY = frozenset({
    'default_input_cache_path', 'default_output_path',
    'output_path_template',
})

# Setter coercers (keep stored value in a canonical type).
_COERCE = {
    'shared_workflow_paths': list,
    'path_substitution_enabled': bool,
    'input_cache_max_age_days': int,
    'lost_job_timeout_days': int,
    'local_history_max_entries': int,
    'library_keep_on_top': bool,
    'render_manager_keep_on_top': bool,
    'delete_unused_variants_on_submit': bool,
    'frame_padding': int,
    'version_padding': int,
    'gizmo_name_prefix': bool,
    'gizmo_disable_group_view': bool,
    'batch_warning_threshold': int,
    'library_grid_show_version': bool,
    'library_grid_show_author': bool,
    'library_grid_show_description': bool,
    'library_grid_show_categories': bool,
    'library_grid_show_models': bool,
    'library_grid_show_source': bool,
    'library_list_show_version': bool,
    'library_list_show_author': bool,
    'library_list_show_description': bool,
    'library_list_show_categories': bool,
    'library_list_show_models': bool,
    'library_list_show_source': bool,
    'library_compare_reset_on_leave': bool,
    'disable_local_workflows': bool,
    'lock_shared_folders': bool,
    'lock_machines': bool,
}


class _Settings:
    """User settings + global read-only override.

    Two backing dicts kept separate so writes never mix with global
    overrides:
      - `_user_settings`: what the user persisted to ~/.nuke/nukomfy_settings.json
      - `_global_overrides`: read-only override from
        Nukomfy/settings_overrides/nukomfy_settings.json (may be partial)

    Read priority: global -> user -> factory defaults. Writes go to
    `_user_settings` only and are silently ignored for keys present in `_global_overrides`
    (the UI greys out locked fields, but defensive no-op covers any
    code path that bypasses the UI).

    `shared_workflow_paths` is the only field with merge semantics: the
    effective list is `global_paths + user_extras` (globals first, fixed;
    user can add/remove only the non-global entries). The setter strips
    any global-locked path the caller hands us so we never accidentally
    persist a duplicate.
    """

    def __init__(self):
        object.__setattr__(self, '_user_settings', {})
        object.__setattr__(self, '_global_overrides', {})
        self._load()

    def _load(self):
        # User layer.
        user_d = {}
        if os.path.isfile(_FILE):
            try:
                with open(_FILE, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    user_d = loaded
            except Exception:
                pass
        object.__setattr__(self, '_user_settings', user_d)

        # Global layer - read-only override. Empty dict if file missing
        # or malformed (load_override_json returns None silently in those
        # cases).
        global_d = {}
        try:
            loaded = load_override_json('nukomfy_settings.json')
            if isinstance(loaded, dict):
                # Only keep keys we actually know about - unknown keys in
                # the global file would be ignored at __getattr__ anyway,
                # but pruning here keeps `is_locked` accurate.
                global_d = {k: v for k, v in loaded.items() if k in _DEFAULTS}
        except Exception:
            pass
        object.__setattr__(self, '_global_overrides', global_d)

    def save(self):
        import Nukomfy.utils.fs_safe as fs_safe
        if not fs_safe.makedirs_silent(os.path.dirname(_FILE)):
            return
        # Persist only what the user has set. Global overrides are never
        # written into the user file - that way, removing an override on
        # the global side surfaces the user's prior value (or the factory
        # default if they never personalised it).
        data = {k: self._user_settings[k] for k in _DEFAULTS if k in self._user_settings}
        tmp = _FILE + '.tmp'
        # A write failure (full disk, revoked permission) must never surface
        # as a raw traceback in the Settings apply handler; atomic_replace
        # keeps the previous file intact on failure.
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            fs_safe.atomic_replace(tmp, _FILE)
        except Exception as e:
            _log.warning('failed to save settings: %s', e)
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Public API for the UI to query lock state.
    # ------------------------------------------------------------------
    def is_locked(self, name):
        """True when `name` is overridden by the settings overrides file.

        Note: `shared_workflow_paths` is *partially* lockable (the global
        contributes fixed entries, but the user can still add/remove their
        own). This method returns True if the global file contains the
        key at all - callers that need the merge view should call
        `get_shared_paths_with_locks()` instead.
        """
        return name in self._global_overrides

    def get_shared_paths_with_locks(self):
        """Return the effective shared workflow paths as `[(path, locked)]`.

        Globals come first (locked=True, in the order written by the
        admin), followed by user-only paths (locked=False, in the order
        the user arranged them). Duplicates between the two layers are
        resolved in favour of the global entry.
        """
        global_paths = list(self._global_overrides.get('shared_workflow_paths') or [])
        user_paths = list(self._user_settings.get('shared_workflow_paths') or [])
        seen = set(global_paths)
        out = [(p, True) for p in global_paths]
        for p in user_paths:
            if p in seen:
                continue
            out.append((p, False))
            seen.add(p)
        return out

    # ------------------------------------------------------------------
    def __getattr__(self, name):
        if name not in _DEFAULTS:
            raise AttributeError(name)

        # shared_workflow_paths: merge view (globals first, then user).
        # With lock_shared_folders set, drop the user entries so only the
        # admin override paths reach consumers (the user's own shared paths
        # stay in the JSON but are ignored - reactivated if the flag clears).
        if name == 'shared_workflow_paths':
            pairs = self.get_shared_paths_with_locks()
            if self.lock_shared_folders:
                pairs = [(p, locked) for p, locked in pairs if locked]
            return [p for p, _locked in pairs]

        # Global override wins.
        if name in self._global_overrides:
            val = self._global_overrides[name]
            if name in _FALLBACK_ON_EMPTY and not val:
                return _DEFAULTS[name]
            return val

        # User layer.
        val = self._user_settings.get(name)
        if name in _FALLBACK_ON_EMPTY and not val:
            return _DEFAULTS[name]
        return _DEFAULTS[name] if val is None else val

    def __setattr__(self, name, value):
        if name in _DEFAULTS:
            # Locked by global -> silently no-op. The UI disables the
            # widget for locked keys, but this guard covers any code
            # path that mutates settings programmatically.
            if name in self._global_overrides and name != 'shared_workflow_paths':
                return
            coerce = _COERCE.get(name)
            value = coerce(value) if coerce else value
            # shared_workflow_paths is partially lockable - strip the
            # global-locked entries from what we persist as user.
            if name == 'shared_workflow_paths':
                global_paths = set(
                    self._global_overrides.get('shared_workflow_paths') or [])
                value = [p for p in (value or []) if p not in global_paths]
            self._user_settings[name] = value
        else:
            object.__setattr__(self, name, value)


settings = _Settings()
