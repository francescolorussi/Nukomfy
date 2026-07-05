"""Central in-memory store backing the Render Manager and MyJobs views.

Populated by a single fetch cycle (UnifiedFetchWorker) and consumed
read-only by all views; emits `storeChanged` after every merge so
views refresh as a unit.

Responsibilities:
- Own the authoritative snapshot of live queue (running/pending) and
  per-machine status across all configured machines.
- Cache the local submit_history on open and keep it in sync when
  terminal state is persisted.
- Expose filters (queue_for_machine, jobs_for_user, get) as pure
  reads - no fetching inside the store.

Non-goals:
- Fetching (belongs to UnifiedFetchWorker).
- Writing terminal state (goes through submit_history.persist_* APIs;
  store is notified via `merge_terminal` after the write).
"""

import datetime as _dt

import Nukomfy.data.submit_history as submit_history
from Nukomfy.utils.qt_compat import QtCore


def _sent_at_to_epoch(sent_at):
    """Convert a `sent_at` field (ISO string set by submit_history.record_submit,
    or a numeric epoch) into epoch seconds (float). Returns 0.0 on unparseable
    input - kept numeric so the cache sort stays type-homogeneous with the
    server's `create_time` field.
    """
    if isinstance(sent_at, (int, float)):
        return float(sent_at)
    if isinstance(sent_at, str) and sent_at:
        try:
            return _dt.datetime.fromisoformat(sent_at).timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


class RenderDataStore(QtCore.QObject):
    """In-memory single source of truth for Render Manager views."""

    # Emitted after any mutation (ingest / merge / reload). Views connect
    # once and rebuild their rows from the current store state.
    storeChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Per-machine live snapshot. Fully replaced on every ingest for that
        # machine - running/pending don't outlive the fetch cycle.
        # {machine_url: {'status', 'running_jobs', 'pending_jobs', 'error'}}
        self._machine_state = {}

        # Live queue indexed by prompt_id for O(1) lookup.
        # Each job dict carries an attached 'nfy_machine_url' field (attached by
        # ingest_machine) so view filters can route without extra plumbing.
        self._running_jobs = {}   # {prompt_id: job_dict}
        self._pending_jobs = {}   # {prompt_id: job_dict}

        # Server-side recent terminals per machine - drives the History
        # sub-table under each Render Manager row. Shows jobs from ANY
        # user (not user-scoped). Cache lives across ticks so other
        # users' details aren't re-fetched on every refresh; entries
        # are pruned when they roll off the server's recent listing.
        # {machine_url: {prompt_id: detail_dict}}
        self._machine_recent_terminals = {}

        # Cached copy of submit_history.json. Reloaded lazily after a
        # persist_* call so terminal views reflect the write immediately.
        self._local_history = []   # list[entry_dict] (most recent first)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def load_local_history(self, entries=None):
        """Reload the full local history cache.

        Called once on panel open and again after any persist_* that
        mutates submit_history.json (so views re-render without a
        second fetch cycle).

        If *entries* is provided, use it directly - callers that already
        read the file in this refresh cycle (e.g. MyJobs reload after
        `refresh_ranges_from_disk`) pass the fresh list to avoid a
        redundant disk read on the UI thread.
        """
        if entries is not None:
            self._local_history = entries
        else:
            self._local_history = submit_history.get_history()

    def ingest_machine(self, machine_url, fetch_result):
        """Replace this machine's live snapshot from a fetch result.

        `fetch_result` shape (produced by UnifiedFetchWorker):
            {
                'status': 'idle'|'rendering'|'queued'|'offline',
                'running_jobs': [job_dict, ...],
                'pending_jobs': [job_dict, ...],
                'error': str | None,
            }

        Running/pending from previous ticks for this machine are dropped;
        per-machine state is strictly the latest fetch. Does not emit
        storeChanged - caller batches machines then emits once.
        """
        prev = self._machine_state.get(machine_url) or {}
        for job in prev.get('running_jobs') or []:
            pid = job.get('prompt_id')
            if pid and self._running_jobs.get(pid, {}).get(
                    'nfy_machine_url') == machine_url:
                self._running_jobs.pop(pid, None)
        for job in prev.get('pending_jobs') or []:
            pid = job.get('prompt_id')
            if pid and self._pending_jobs.get(pid, {}).get(
                    'nfy_machine_url') == machine_url:
                self._pending_jobs.pop(pid, None)

        running = fetch_result.get('running_jobs') or []
        pending = fetch_result.get('pending_jobs') or []
        for job in running:
            if not isinstance(job, dict):
                continue
            job['nfy_machine_url'] = machine_url
            pid = job.get('prompt_id')
            if pid:
                self._running_jobs[pid] = job
        for job in pending:
            if not isinstance(job, dict):
                continue
            job['nfy_machine_url'] = machine_url
            pid = job.get('prompt_id')
            if pid:
                self._pending_jobs[pid] = job

        self._machine_state[machine_url] = {
            'status': fetch_result.get('status', ''),
            'running_jobs': running,
            'pending_jobs': pending,
            'error': fetch_result.get('error'),
        }

        # Server-side recent-terminals cache merge. Pipeline runs unconditionally:
        # an empty listing drops stale cache entries via step 1, and step 3
        # backfills only when `missing` is non-empty.
        recent_ids = set(fetch_result.get('recent_terminal_ids') or ())
        cache = self._machine_recent_terminals.setdefault(machine_url, {})
        # 1. Drop entries rolled off the server's listing.
        for pid in list(cache):
            if pid not in recent_ids:
                del cache[pid]
        # 2. Add fresh detail for newly-fetched terminals.
        for detail in fetch_result.get('new_terminals') or []:
            pid = detail.get('prompt_id') if isinstance(detail, dict) else None
            if pid:
                cache[pid] = detail
        # 2.5 Server-side persistent terminals (manager-aware machines).
        # Purely additive: covers pids that step 2 would not reach (other
        # users, plus entries rolled off ComfyUI's in-memory history after
        # a server restart). Server entries are already nfy_*-keyed but
        # lack the derived fields _parse_job_detail produces - normalise
        # so cache sort and _JobDialog see the exact same schema as the
        # step-2 entries above.
        for entry in fetch_result.get('persistent_terminals') or []:
            if not isinstance(entry, dict):
                continue
            pid = entry.get('prompt_id')
            if not pid or pid in cache:
                continue
            merged = dict(entry)
            ct = merged.get('create_time')
            if not isinstance(ct, (int, float)) or not ct:
                merged['create_time'] = _sent_at_to_epoch(
                    entry.get('nfy_sent_at'))
            merged.setdefault(
                'completed', entry.get('nfy_status_str') == 'completed')
            merged.setdefault('nfy_execution_error', None)
            cache[pid] = merged
            recent_ids.add(pid)
        # 3. Backfill from local history for pids whose detail was skipped
        #    because they were already in `known` (cache plus locally-persisted).
        #    Guarantee create_time is a numeric epoch (fall back to sent_at
        #    parsed as ISO -> epoch): mixing str with the server's numeric
        #    create_time would break `recent_terminals_for_machine`'s sort.
        missing = [pid for pid in recent_ids if pid not in cache]
        if missing:
            missing_set = set(missing)
            for entry in self._local_history:
                pid = entry.get('prompt_id')
                if pid in missing_set and entry.get('nfy_terminal_persisted'):
                    merged = dict(entry)
                    ct = merged.get('create_time')
                    if not isinstance(ct, (int, float)) or not ct:
                        merged['create_time'] = _sent_at_to_epoch(
                            entry.get('nfy_sent_at'))
                    cache[pid] = merged
                    missing_set.discard(pid)
                    if not missing_set:
                        break

    def drop_live_job(self, prompt_id):
        """Optimistically remove *prompt_id* from the live indexes after
        a confirmed server-side delete (called by `_on_action_done` for
        `kind='remove'`). Without this the Queue sub-table would keep
        showing the row as a normal pending until the next fetch tick:
        `_pending_actions` is popped (no more grey-out) but the store
        snapshot still contains the pid -> rebuild renders it un-greyed.

        Updates both the prompt_id index AND the per-machine list inside
        `_machine_state` so `queue_for_machine` returns the correct view.
        Pending count on the main row catches up on the next refresh.
        """
        if not prompt_id:
            return
        self._running_jobs.pop(prompt_id, None)
        self._pending_jobs.pop(prompt_id, None)
        for state in self._machine_state.values():
            if not state:
                continue
            for key in ('running_jobs', 'pending_jobs'):
                lst = state.get(key)
                if isinstance(lst, list):
                    state[key] = [j for j in lst
                                  if j.get('prompt_id') != prompt_id]

    def drop_machine(self, machine_url):
        """Forget a machine (e.g., user removed it from Settings).

        Also clears its server-side recent-terminals cache so the History
        sub-table goes empty when the machine is removed.

        Clears its live queue entries from the prompt_id indexes; local
        history is untouched - a terminal row for a removed machine
        remains visible in MyJobs.
        """
        self._machine_recent_terminals.pop(machine_url, None)
        prev = self._machine_state.pop(machine_url, None)
        if not prev:
            return
        for job in prev.get('running_jobs') or []:
            pid = job.get('prompt_id')
            if pid and self._running_jobs.get(pid, {}).get(
                    'nfy_machine_url') == machine_url:
                self._running_jobs.pop(pid, None)
        for job in prev.get('pending_jobs') or []:
            pid = job.get('prompt_id')
            if pid and self._pending_jobs.get(pid, {}).get(
                    'nfy_machine_url') == machine_url:
                self._pending_jobs.pop(pid, None)

    def known_machine_urls(self):
        """URLs the store currently has data for (state or recent-terminals).

        Used by the Render Manager to evict machines removed or disabled in
        Settings (see drop_machine).
        """
        return set(self._machine_state) | set(self._machine_recent_terminals)

    def notify(self):
        """Explicit emit - use after a batch of ingest_machine calls."""
        self.storeChanged.emit()

    # ------------------------------------------------------------------
    # Read filters
    # ------------------------------------------------------------------
    def machine_info(self, machine_url):
        """Return the last ingested snapshot for *machine_url*, or None."""
        return self._machine_state.get(machine_url)

    def queue_for_machine(self, machine_url):
        """Return (running_jobs, pending_jobs) for a machine.

        Lists in server order. Empty lists if the machine hasn't been
        fetched yet or reported an error.
        """
        info = self._machine_state.get(machine_url)
        if not info:
            return ([], [])
        return (info.get('running_jobs') or [],
                info.get('pending_jobs') or [])

    def recent_terminals_for_machine(self, machine_url, limit=None):
        """Return server-side recent terminal jobs for *machine_url*.

        Drives the History sub-table under each Render Manager row. Shows
        jobs from ANY user (not user-scoped) - sourced from the server's
        `/api/jobs?limit=N` listing, not from local submit_history.

        Sorted most recent first by client submit time (`nfy_sent_at`),
        with server `create_time` as fallback for jobs submitted outside
        Nukomfy. `limit` caps row count (the caller passes
        `settings.history_limit`); pass None for unbounded.
        """
        cache = self._machine_recent_terminals.get(machine_url)
        if not cache:
            return []
        out = sorted(cache.values(),
                     key=lambda e: _sent_at_to_epoch(e.get('nfy_sent_at'))
                     or (e.get('create_time') or 0),
                     reverse=True)
        if limit is not None:
            return out[:limit]
        return out

    def jobs_for_user(self, username):
        """Return all local-history entries submitted by *username*.

        Filter is by `nfy_submitted_by` (username only). Hostname is
        metadata, never part of ownership. Same person submitting from
        multiple workstations sees their full history here.

        Live entries (running/pending not yet terminal) are enriched with
        the latest live dict so the view sees fresh status/progress.
        Entries keep their submit_history order - sent_at desc, fixed at
        submit time and never reshuffled.

        Each returned dict is a shallow merge: local base (all submit
        metadata) + overlay (live_state, live_status, live_progress,
        etc.) when the prompt_id is currently live.

        Non-terminal entries on a machine that is currently offline (or
        whose last fetch errored) are flagged `live_state='unknown'` so
        the user sees `? Unknown` instead of stale running/pending data.
        The display flips back to the real status as soon as the machine
        reconnects and the next fetch succeeds.
        """
        out = []
        for entry in self._local_history:
            if entry.get('nfy_submitted_by') != username:
                continue
            pid = entry.get('prompt_id')
            merged = dict(entry)
            if pid in self._running_jobs:
                merged['live_state'] = 'running'
                merged['live_job'] = self._running_jobs[pid]
            elif pid in self._pending_jobs:
                merged['live_state'] = 'pending'
                merged['live_job'] = self._pending_jobs[pid]
            elif entry.get('nfy_terminal_persisted'):
                merged['live_state'] = entry.get('nfy_status_str') or 'unknown'
            else:
                # Non-terminal local entry not seen in any live queue.
                # If the machine is offline / errored, the live state is
                # not knowable - show `unknown` (transient, flips back at
                # next successful fetch). Otherwise the reconciler will
                # eventually persist it as terminal or lost; until then
                # the view shows `awaiting`.
                machine_url = entry.get('nfy_machine_url')
                m_state = (self._machine_state.get(machine_url) or {}
                           if machine_url else {})
                if m_state.get('error') or m_state.get('status') == 'offline':
                    merged['live_state'] = 'unknown'
                else:
                    merged['live_state'] = 'awaiting'
            out.append(merged)
        return out

    def get(self, prompt_id):
        """Return the best available dict for *prompt_id*, or None.

        Priority: terminal-persisted local history -> live running -> live
        pending -> non-terminal local entry -> server-side recent-terminals
        cache. Terminal wins because that's the one with embedded
        messages/error/duration.

        Live (running/pending) dicts are returned as enriched shallow
        copies: the /api/queue payload is missing fields the dialog
        expects (status_str, seeds_used). See `_enrich_live`.

        The recent-terminals fallback covers jobs known only to the server
        (another user's, or submitted before the local DB was regenerated).
        Without it the Job dialog's Refresh - which routes through get()
        with no raw-row fallback of its own - blanked such a job to "No
        data available" while the first open (falling back to the raw
        History row) rendered it fine. Returned as a shallow copy with
        `nfy_machine_url` filled from the cache key when missing or empty:
        detail entries parsed from `/api/jobs/{id}` carry '' for jobs
        submitted outside Nukomfy, and the dialog's workflow fetch needs a
        real URL to route the request.
        """
        for entry in self._local_history:
            if (entry.get('prompt_id') == prompt_id
                    and entry.get('nfy_terminal_persisted')):
                return entry
        if prompt_id in self._running_jobs:
            return self._enrich_live(self._running_jobs[prompt_id], 'running')
        if prompt_id in self._pending_jobs:
            return self._enrich_live(self._pending_jobs[prompt_id], 'pending')
        for entry in self._local_history:
            if entry.get('prompt_id') == prompt_id:
                return entry
        for machine_url, cache in self._machine_recent_terminals.items():
            hit = cache.get(prompt_id)
            if hit:
                out = dict(hit)
                if not out.get('nfy_machine_url'):
                    out['nfy_machine_url'] = machine_url
                return out
        return None

    def _enrich_live(self, raw, live_state):
        """Shallow-copy *raw* and backfill fields the /api/queue payload
        omits but the Job dialog expects: `status_str` (knowable only
        from the queue collection - running vs pending), `seeds_used`
        and `machine_name` (both client-only, persisted in
        submit_history). Without this, the dialog Refresh button loses
        Status/Seed/Machine when re-pulling the entry. `setdefault`
        preserves any pre-existing enrichment.
        """
        out = dict(raw)
        out.setdefault('nfy_status_str', live_state)
        pid = out.get('prompt_id')
        if pid:
            need_seeds = not out.get('nfy_seeds_used')
            need_machine = not out.get('nfy_machine_name')
            if need_seeds or need_machine:
                for entry in self._local_history:
                    if entry.get('prompt_id') == pid:
                        if need_seeds:
                            seeds = entry.get('nfy_seeds_used')
                            if seeds:
                                out['nfy_seeds_used'] = seeds
                        if need_machine:
                            mname = entry.get('nfy_machine_name')
                            if mname:
                                out['nfy_machine_name'] = mname
                        break
        return out

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    def terminal_prompt_ids_for_machine(self, machine_url):
        """Set of pids already fully persisted locally for *machine_url*.

        Callers combine this with `cached_terminal_ids_for_machine` to build
        the `known` set passed to `fetch_all_for_machine` - pids in either
        set already have full detail, so the worker skips the `/api/jobs/{id}`
        detail call. Together they keep steady-state cost at 2 HTTP calls
        (queue + listing) while still populating the cache on cold-open.
        """
        return {e.get('prompt_id') for e in self._local_history
                if e.get('nfy_machine_url') == machine_url
                and e.get('nfy_terminal_persisted')
                and e.get('prompt_id')}

    def cached_terminal_ids_for_machine(self, machine_url):
        """Set of pids currently in the in-memory server-terminals cache.

        Companion to `terminal_prompt_ids_for_machine`: together they form
        the `known` set for `fetch_all_for_machine`, so detail is fetched
        only for pids we've neither cached this session nor persisted locally.
        """
        return set((self._machine_recent_terminals.get(machine_url) or {}).keys())

    def awaiting_prompt_ids_for_machine(self, machine_url):
        """Set of own prompt_ids submitted to *machine_url* but not yet
        persisted as terminal (`terminal_persisted=False`).

        Used by `fetch_all_for_machine` to reconcile own awaiting jobs
        even when their pid has fallen below the display window: detail
        is fetched for these regardless of `display_limit` so MyJobs can
        promote them to terminal state.
        """
        return {e.get('prompt_id') for e in self._local_history
                if e.get('nfy_machine_url') == machine_url
                and not e.get('nfy_terminal_persisted')
                and e.get('prompt_id')}
