"""Storage and management of ComfyUI machine entries (name, URL).

Saved to ~/.nuke/nukomfy_machines.json alongside other plugin settings.
"""

import json
import logging
import os
import secrets

_log = logging.getLogger(__name__)

_FILE = os.path.join(os.path.expanduser('~'), '.nuke', 'nukomfy_machines.json')


def _new_machine_id():
    """8 hex chars (32 bits of entropy). For a personal machine list of
    a few dozen entries the collision probability is negligible, and
    short ids stay readable in JSON files, warnings, and debug logs.
    """
    return secrets.token_hex(4)


class Machine:
    # Keys stored in the 'info' dict (everything except 'online' and 'error')
    _INFO_KEYS = ('os', 'comfyui_ver', 'gpu', 'vram_total', 'ram_total',
                  'availability')

    def __init__(self, name, url, mid=None, enabled=True, info=None,
                 locked=False, hidden_url=False):
        self.id      = mid or _new_machine_id()
        self.name    = name
        self.url     = (url or '').rstrip('/')
        self.enabled = enabled   # whether this machine is active for submission
        self.info    = info      # in-memory session snapshot, never persisted (see apply_machine_info)
        # True when this entry comes from the settings_overrides file.
        # Memory-only flag (NOT serialised). Locked machines cannot be
        # edited, removed, or reordered by the user (Enabled is the one
        # exception - see set_enabled).
        self.locked  = locked
        # True when the URL should be hidden from UI and stored obfuscated
        # at-rest. Persisted in JSON / DB. Not security; see USER_GUIDE.
        self.hidden_url = bool(hidden_url)

    def to_dict(self):
        from Nukomfy.utils.url_obfuscation import obfuscate_url
        url_out = (obfuscate_url(self.url, self.name)
                   if self.hidden_url else self.url)
        # `info` (hardware/version snapshot) is deliberately NOT persisted: it
        # is session-scoped, fetched live and kept only in memory (see
        # apply_machine_info). The file holds identity / config only.
        return {'id': self.id, 'name': self.name, 'url': url_out,
                'enabled': self.enabled, 'hidden_url': self.hidden_url}

    @staticmethod
    def from_dict(d, locked=False):
        from Nukomfy.utils.url_obfuscation import deobfuscate_url, is_obfuscated
        raw_url = d.get('url', '') or ''
        # The URL prefix is the source of truth for the hidden state:
        # if the stored URL is obfuscated, the machine is hidden no
        # matter what the JSON flag claims. Avoids broken HTTP calls
        # to an "nfyo:..." string and keeps the at-rest blob consistent.
        # The flag is still honoured when the URL is plain (so the
        # caller can mark a machine hidden via the dialog and let the
        # next save normalise the URL to its obfuscated form).
        hidden = is_obfuscated(raw_url) or bool(d.get('hidden_url', False))
        plain = (deobfuscate_url(raw_url, d.get('name', ''))
                 if is_obfuscated(raw_url) else raw_url)
        # `info` is intentionally not read back: the snapshot is re-fetched
        # live each session, never seeded from disk (a legacy `info` block in
        # an old file is ignored and dropped on the next save).
        return Machine(d.get('name', ''), plain, d.get('id'),
                       enabled=d.get('enabled', True),
                       locked=locked, hidden_url=hidden)

    def to_persistable_url(self):
        """URL ready for at-rest persistence (obfuscated if hidden_url).

        Use this whenever the URL is written to a payload that lands in
        the local DB (`extra_data`) or in a shared JSON state file. Read
        paths deobfuscate transparently so plain-URL callers see no diff.
        """
        from Nukomfy.utils.url_obfuscation import obfuscate_url
        if self.hidden_url:
            return obfuscate_url(self.url, self.name)
        return self.url


def _dedupe_names_in_place(machines):
    """Ensure machine names are unique. Renames duplicates with a `(N)`
    suffix. Iteration order matters: globals (locked) come first to win
    priority over user entries with the same name.
    """
    seen = set()
    for m in machines:
        base = m.name or ''
        candidate = base
        n = 2
        while candidate in seen:
            candidate = '{} ({})'.format(base, n)
            n += 1
        if candidate != m.name:
            _log.warning(
                'Machine renamed to avoid duplicate name: %s -> %s (id=%s)',
                m.name, candidate, m.id or '?')
            m.name = candidate
        seen.add(m.name)


class _MachineManager:
    def __init__(self):
        self._machines = []
        # The user can toggle the Enabled checkbox even on a locked
        # machine - they may want to silence an offline shared farm
        # locally without asking the admin to update the global file.
        # The override is persisted as a delta against the global file's
        # original `enabled` value, so removing the override file or
        # re-enabling the global brings the user back to the global
        # default automatically.
        self._user_global_overrides = {}   # {id: {'enabled': bool}}
        self._global_enabled_orig = {}     # {id: enabled_from_global_file}
        self._load()

    def _load(self):
        # Load global override first (locked machines), then merge user
        # machines on top, skipping any user entry whose id collides
        # with a global one (global wins - admins push canonical shared
        # machines, users keep their own deduplicated by uuid).
        # User file shape:
        #   {"machines": [...], "global_overrides": {id: {...}}}
        self._machines = []
        self._user_global_overrides = {}
        self._global_enabled_orig = {}

        global_data = []
        try:
            from Nukomfy.core.settings import load_override_json
            loaded = load_override_json('nukomfy_machines.json')
            if isinstance(loaded, dict):
                # Admin can drop their own ~/.nuke/nukomfy_machines.json
                # directly here. `global_overrides` if present is
                # user-scoped state and is ignored.
                raw = loaded.get('machines')
                if isinstance(raw, list):
                    global_data = raw
        except Exception:
            pass

        # Read user file.
        user_machines_raw = []
        user_overrides_raw = {}
        if os.path.isfile(_FILE):
            try:
                with open(_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    user_machines_raw = data.get('machines') or []
                    raw_ov = data.get('global_overrides') or {}
                    if isinstance(raw_ov, dict):
                        user_overrides_raw = raw_ov
            except Exception:
                pass

        global_machines = []
        global_ids = set()
        for d in global_data:
            try:
                m = Machine.from_dict(d, locked=True)
            except Exception:
                continue
            if m.id in global_ids:
                continue  # duplicate id within global file - keep first
            global_ids.add(m.id)
            # Snapshot the global's original `enabled` for delta tracking.
            self._global_enabled_orig[m.id] = m.enabled
            # Apply user override if present and well-formed.
            ov = user_overrides_raw.get(m.id)
            if isinstance(ov, dict) and 'enabled' in ov:
                m.enabled = bool(ov['enabled'])
                self._user_global_overrides[m.id] = {
                    'enabled': bool(ov['enabled'])
                }
            global_machines.append(m)

        # Detect orphan overrides - entries in the user file pointing to
        # global machine ids that no longer exist (admin removed them).
        # `self._user_global_overrides` already contains only valid
        # entries (the loop above only adds when the global matches),
        # so the in-memory state is clean. We just need to check whether
        # the on-disk file holds extra entries we should drop on save.
        orphans_pruned = any(
            stale_id not in global_ids for stale_id in user_overrides_raw)

        user_machines = []
        for d in user_machines_raw:
            try:
                m = Machine.from_dict(d, locked=False)
            except Exception:
                continue
            if m.id in global_ids:
                continue  # global wins on id collision
            user_machines.append(m)

        # Globals always come first; user machines preserve their own
        # order beneath. Reorder is constrained to within each block.
        self._machines = global_machines + user_machines

        # Dedup names: required because the name is the user-facing
        # identity and (when hidden_url is set) the key for URL
        # obfuscation. Globals win since they iterate first.
        _dedupe_names_in_place(self._machines)

        # If pruning removed any stale override, persist now so the
        # user file doesn't keep a dangling reference to a global
        # machine the admin has since removed.
        if orphans_pruned:
            self.save()

    def save(self):
        import Nukomfy.utils.fs_safe as fs_safe
        if not fs_safe.makedirs_silent(os.path.dirname(_FILE)):
            return
        # Globals are read-only and never serialised in `machines`.
        # Their per-user `enabled` deltas live in `global_overrides`.
        payload = {
            'machines': [m.to_dict() for m in self._machines if not m.locked],
            'global_overrides': dict(self._user_global_overrides),
        }
        tmp = _FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        fs_safe.atomic_replace(tmp, _FILE)

    def _effective_machines(self):
        # With lock_machines set, only the override (locked) machines reach
        # consumers; the user's own machines stay in the JSON but are
        # ignored (reactivated if the flag clears). Mirrors how
        # lock_shared_folders filters shared_workflow_paths.
        from Nukomfy.core.settings import settings
        if settings.lock_machines:
            return [m for m in self._machines if m.locked]
        return self._machines

    @property
    def machines(self):
        return list(self._effective_machines())

    @property
    def enabled_machines(self):
        return [m for m in self._effective_machines() if m.enabled]

    def global_count(self):
        """How many machines at the head of the list are globally locked.

        Used by the UI to gate reorder buttons (a user machine cannot
        move into the locked block, only swap with another user entry).
        """
        n = 0
        for m in self._machines:
            if m.locked:
                n += 1
            else:
                break
        return n

    def add(self, machine):
        self._machines.append(machine)
        _dedupe_names_in_place(self._machines)
        self.save()

    def update(self, machine):
        for i, m in enumerate(self._machines):
            if m.id == machine.id:
                # Defensive: refuse to overwrite a locked entry. The UI
                # already hides the Edit button for locked rows.
                if m.locked:
                    return
                # Preserve the locked flag across edits (always False here
                # since we just refused locked, but explicit beats implicit).
                machine.locked = False
                self._machines[i] = machine
                break
        _dedupe_names_in_place(self._machines)
        self.save()

    def remove(self, machine_id):
        target = self.get(machine_id)
        if target is None or target.locked:
            return
        self._machines = [m for m in self._machines if m.id != machine_id]
        self.save()

    def move(self, machine_id, delta):
        """Shift a machine by `delta` positions (-1 up, +1 down). Persists.

        Locked machines cannot be moved, and a user machine cannot cross
        into the locked block (the swap target must also be unlocked).
        """
        for i, m in enumerate(self._machines):
            if m.id == machine_id:
                if m.locked:
                    return i
                j = i + delta
                if not (0 <= j < len(self._machines)):
                    return i
                if self._machines[j].locked:
                    return i  # would cross into the locked block
                self._machines[i], self._machines[j] = \
                    self._machines[j], self._machines[i]
                self.save()
                return j
        return -1

    def set_enabled(self, machine_id, enabled):
        """Toggle the Enabled flag for any machine, including globals.

        For unlocked machines this is a plain mutation + save.
        For locked (global) machines we persist the new value as a
        delta against the original enabled value declared in the global
        file. When the user re-aligns to the global default, the
        override entry is dropped so the user file stays clean.
        """
        m = self.get(machine_id)
        if m is None:
            return False
        enabled = bool(enabled)
        m.enabled = enabled
        if m.locked:
            orig = self._global_enabled_orig.get(machine_id, True)
            if enabled == orig:
                self._user_global_overrides.pop(machine_id, None)
            else:
                self._user_global_overrides[machine_id] = {'enabled': enabled}
        self.save()
        return True

    def get(self, machine_id):
        for m in self._machines:
            if m.id == machine_id:
                return m
        return None


machine_manager = _MachineManager()


# ---------------------------------------------------------------------------
# Machine info fetcher  (blocking - always run in a thread)
# ---------------------------------------------------------------------------
def _fmt_bytes(n):
    if not n:
        return '-'
    return '{:.1f} GB'.format(n / 1024 ** 3)


def _clean_gpu_name(raw):
    """Strip ComfyUI device decorations from the GPU name.

    e.g. "cuda:0 NVIDIA GeForce RTX 2080 Ti : cudaMallocAsync"
      -> "NVIDIA GeForce RTX 2080 Ti"
    """
    import re
    s = raw.strip()
    # Remove leading "cuda:N " or "mps:N " prefix
    s = re.sub(r'^(cuda|mps|cpu):\d+\s*', '', s, flags=re.IGNORECASE)
    # Remove trailing " : <allocator>" suffix
    s = re.split(r'\s*:\s*\w+$', s)[0]
    return s.strip() or raw


def os_display_name(raw):
    """OS name for display (Windows/macOS/Linux). Distinct from
    path_substitution.normalize_os, which is path-substitution logic
    (returns 'OSX') and must stay untouched. Accepts sys.platform /
    system_stats (win32/darwin/linux), platform.platform()
    (Windows-10-..., Darwin-..., Linux-...) and canonical forms;
    unknown values are returned as-is rather than blanked.
    """
    if not raw:
        return ''
    k = str(raw).strip().lower()
    if k.startswith('win') or k == 'nt':
        return 'Windows'
    if k.startswith('darwin') or k.startswith('mac') or k == 'osx':
        return 'macOS'
    if k.startswith('linux'):
        return 'Linux'
    return str(raw).strip()


def check_machine(machine):
    """Fetch /system_stats and return `{online, os, comfyui_ver, python_ver,
    gpu, vram_total, ram_total}`. Returns `{online: False, error}` on failure."""
    import urllib.request
    try:
        url = machine.url.rstrip('/') + '/api/system_stats'
        req = urllib.request.Request(url, headers={'User-Agent': 'Nukomfy'})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())

        sys_info = data.get('system', {})
        devices  = data.get('devices', [])

        gpu_name   = '-'
        vram_total = '-'
        if devices:
            d = devices[0]
            gpu_name   = _clean_gpu_name(d.get('name', '-'))
            vram_total = _fmt_bytes(d.get('vram_total', 0))

        # Canonical OS normalization lives in path_substitution.py so
        # both sides (machine info, substitution engine) agree 100%.
        from Nukomfy.client.path_substitution import normalize_os
        os_str = normalize_os(sys_info.get('os', ''))

        # Availability comes from the Nukomfy Suite custom node (separate
        # ping with its own 60s cache). Default to 'available' when the
        # Suite is not installed or unreachable so the rest of the UI
        # treats the machine as usable.
        try:
            from Nukomfy.client import manager_client
            avail = manager_client.availability(machine.url) or 'available'
        except Exception:
            avail = 'available'

        return {
            'online':       True,
            'os':           os_str,
            'comfyui_ver':  sys_info.get('comfyui_version', '-'),
            'python_ver':   (sys_info.get('python_version', '-').split() or ['-'])[0],
            'gpu':          gpu_name,
            'vram_total':   vram_total,
            'ram_total':    _fmt_bytes(sys_info.get('ram_total', 0)),
            'availability': avail,
        }
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            try: e.read()  # drain -> clean FIN close, not RST (WinError 10054)
            except Exception: pass
        err = str(e)
        # A hidden-URL machine must never surface its URL/hostname. urllib
        # errors (notably SSL hostname-mismatch) can embed it, and this
        # `error` string ends up in the offline status-dot tooltip. Scrub
        # for hidden machines only, so a normal machine still shows its URL.
        if getattr(machine, 'hidden_url', False):
            from Nukomfy.utils.url_obfuscation import scrub_url_in_text
            err = scrub_url_in_text(err, machine.url)
        return {'online': False, 'error': err}


def apply_machine_info(machine, info):
    """Merge a `check_machine` result into `machine.info`, in memory only.

    The machine hardware/version snapshot is session-scoped: fetched live and
    kept in memory, never written to disk (every panel re-fetches it each
    session). Single in-memory writer, shared by every refresh path
    (Settings > Machines and the session refresher behind Submit / Render
    Manager). `online` is tracked on every call; the hardware keys
    (`_INFO_KEYS`) are overwritten only when the host answered, so an offline
    probe never blanks a known snapshot mid-session. Copy-on-write + single
    reassignment to stay consistent with `refresh_availability`, which
    publishes `info` from worker threads.
    """
    new_info = dict(machine.info or {})
    new_info['online'] = bool(info.get('online'))
    if info.get('online'):
        for k in Machine._INFO_KEYS:
            if k in info:
                new_info[k] = info[k]
    machine.info = new_info


def refresh_availability(machine):
    """Pull the current Availability flag from the Nukomfy Suite custom
    node and stash it on the `info` dict of every machine entry sharing
    this URL, so any UI reading `m.info.get('availability')` sees it.

    The fan-out across alias rows (same url, different id) is required
    because UnifiedFetchWorker dedups the fetch to the group
    representative: without it only `machine` would be refreshed, leaving
    alias rows in the Submit Panel and Render Manager on a stale flag.

    Cheap (60s in-process cache in `manager_client.ping`). Safe to call
    from worker threads - only mutates plain Python dicts, never Qt
    widgets. Falls back to 'available' if the Suite isn't reachable
    so the rest of the UI keeps treating the machine as usable.
    """
    try:
        from Nukomfy.client import manager_client
        value = manager_client.availability(machine.url) or 'available'
    except Exception:
        value = 'available'
    targets = [m for m in machine_manager.machines if m.url == machine.url]
    if machine not in targets:
        targets.append(machine)
    for m in targets:
        # Copy-on-write. This runs on worker threads (check_queue /
        # _unified_check), while the main-thread handlers (_on_result,
        # _reflect_preflight_status) rebuild m.info and reassign it in one
        # store. Mutating in place would race them (torn read of a half
        # published dict, or a lost update). Build a fresh dict and publish
        # it with a single assignment, matching the other writers.
        new_info = dict(m.info or {})
        new_info['availability'] = value
        m.info = new_info
    return value


def check_queue(machine):
    """Fetch /queue status. Returns `{status, running, pending, error}`.

    Side effect (only when the machine answered): refreshes
    `machine.info['availability']` so the Submit Panel status display
    reads the current Availability flag. Skipped on an unreachable host.
    """
    from Nukomfy.client.comfy_api import check_queue_status, STATUS_PROBE_TIMEOUT
    info = check_queue_status(machine.url, timeout=STATUS_PROBE_TIMEOUT)
    # Skip the manager availability ping when the queue probe already
    # failed: the host is unreachable, so pinging the same host's manager
    # endpoint would only stall on another full timeout.
    if not info.get('error'):
        refresh_availability(machine)
    return info
