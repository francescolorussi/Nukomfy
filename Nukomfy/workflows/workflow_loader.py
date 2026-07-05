"""Workflow folder loading and metadata read/write."""

import hashlib
import json
import logging
import os

_log = logging.getLogger(__name__)

# Standard filenames inside a workflow folder.
WORKFLOW_JSON = 'workflow.json'
METADATA_JSON = 'metadata.json'


# Cosmetic fields stripped from the workflow JSON before hashing. ComfyUI
# rewrites these on every editor save (node reorder, properties.ver bump,
# slot_index / localized_name toggles on inputs/outputs) without changing
# the logical graph, so they would otherwise produce false-positive
# integrity-hash mismatches. Covers subgraph internals and virtual nodes
# (Reroute, GetNode/SetNode, PrimitiveNode, Note, MarkdownNote, Group).
_WF_TOP_POP = ('revision', 'groups')
_WF_NODE_POP = ('pos', 'size', 'bgcolor', 'color', 'title')
_WF_NODE_PROPERTIES_POP = ('ver', 'models')
_WF_NODE_PIN_POP = ('slot_index', 'localized_name')
_WF_EXTRA_POP = ('ds', 'frontendVersion', 'workflowRendererVersion')
_WF_NOTE_NODE_TYPES = ('Note', 'MarkdownNote')


def _strip_node_cosmetic(n):
    if not isinstance(n, dict):
        return
    for k in _WF_NODE_POP:
        n.pop(k, None)
    if n.get('type') in _WF_NOTE_NODE_TYPES:
        n.pop('widgets_values', None)
    props = n.get('properties')
    if isinstance(props, dict):
        for pk in _WF_NODE_PROPERTIES_POP:
            props.pop(pk, None)
    for pin_key in ('inputs', 'outputs'):
        for pin in n.get(pin_key, []) or []:
            if isinstance(pin, dict):
                for pk in _WF_NODE_PIN_POP:
                    pin.pop(pk, None)


def _strip_workflow_cosmetic(d):
    if not isinstance(d, dict):
        return
    for k in _WF_TOP_POP:
        d.pop(k, None)
    nodes = d.get('nodes')
    if isinstance(nodes, list):
        for n in nodes:
            _strip_node_cosmetic(n)
        # Sort nodes by id so a re-order by ComfyUI does not change the hash.
        nodes.sort(key=lambda x: (x.get('id') if isinstance(x, dict) else 0))
    links = d.get('links')
    if isinstance(links, list):
        # Top-level links: each entry is a list [link_id, src_node, src_slot,
        # dst_node, dst_slot, type]. Sort by link_id for stability.
        links.sort(key=lambda x: (x[0] if isinstance(x, list) and x else 0))
    extra = d.get('extra')
    if isinstance(extra, dict):
        for k in _WF_EXTRA_POP:
            extra.pop(k, None)
    defs = d.get('definitions')
    if isinstance(defs, dict):
        sgs = defs.get('subgraphs')
        if isinstance(sgs, list):
            for sg in sgs:
                _strip_workflow_cosmetic(sg)


def workflow_logical_hash(wf_path):
    """SHA-1 of the workflow JSON canonicalized (cosmetic fields stripped).

    Used to detect logical edits to the workflow on disk after a gizmo
    was created. Layout/zoom/colors/frontend version do not affect the
    hash.
    """
    with open(wf_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    _strip_workflow_cosmetic(data)
    canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(canonical.encode('utf-8')).hexdigest()


class WorkflowItem:
    def __init__(self, folder_path, source):
        self.folder_path   = folder_path
        self.source        = source          # 'Local' | 'Shared'
        self.workflow_path = os.path.join(folder_path, WORKFLOW_JSON)
        self.metadata_path = os.path.join(folder_path, METADATA_JSON)

        # Preview: GIF takes precedence, then PNG/JPG
        self.preview_path = None
        for fname in ('preview.gif', 'preview.png', 'preview.jpg',
                      'preview.jpeg', 'preview.webp'):
            p = os.path.join(folder_path, fname)
            if os.path.isfile(p):
                self.preview_path = p
                break

        # Optional second preview (slot 2) - drives the Library comparison
        # slider. Invariant (enforced at save): preview_b.* never exists
        # without preview.*, so this is only set on genuine pairs.
        self.preview_path_b = None
        for fname in ('preview_b.gif', 'preview_b.png', 'preview_b.jpg',
                      'preview_b.jpeg', 'preview_b.webp'):
            p = os.path.join(folder_path, fname)
            if os.path.isfile(p):
                self.preview_path_b = p
                break

        # Sort keys cached at scan time so re-sort is O(N log N) without
        # repeated stat() calls. mtime: max(workflow.json, metadata.json)
        # - "last edit". ctime: folder creation time - "added to library"
        # (Windows/Mac birthtime; Linux is inode change time, an
        # acceptable approximation).
        self.mtime = self._compute_mtime()
        self.ctime = self._compute_ctime()

        self._meta = self._load_meta()
        self._workflow = None   # lazy
        self._id_conflict = False

    @property
    def has_comparison(self):
        """True when both preview slots are present - the Library renders a
        before/after comparison slider instead of a single thumbnail."""
        return bool(self.preview_path) and bool(self.preview_path_b)

    def _compute_mtime(self):
        latest = 0.0
        for p in (self.workflow_path, self.metadata_path):
            try:
                t = os.path.getmtime(p)
                if t > latest:
                    latest = t
            except OSError:
                pass
        return latest

    def _compute_ctime(self):
        try:
            return os.path.getctime(self.folder_path)
        except OSError:
            return 0.0

    def _load_meta(self):
        defaults = {
            'name': os.path.basename(self.folder_path),
            'workflow_alias': '',
            'description': '', 'author': '',
            'version': '',
            'workflow_id': '',
            'tags_category': [], 'tags_models': [],
            'usage': '',
            'workflow_hash': '',
            'gizmo_color': 0,
            'gizmo_options': {
                'versioning': True, 'author': True,
                'description': True, 'usage': True,
                'output_preview': True,
            },
            # Snapshot-based persistence (Workflow Editor architecture):
            # _snapshot is the server-authoritative widget set, _overrides
            # the user delta, _widget_order the UI row order (interleaved
            # widget refs and structural rows), _v3_user_edits the V3 sub
            # value cache for non-active options.
            '_snapshot': None,
            '_overrides': {},
            '_widget_order': [],
            '_v3_user_edits': {},
        }
        if not os.path.isfile(self.metadata_path):
            return defaults
        try:
            with open(self.metadata_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            defaults.update(data)
        except Exception as e:
            _log.warning(
                'Workflow metadata load failed at %s: %s',
                self.metadata_path, e)
        return defaults

    @property
    def name(self):            return str(self._meta.get('name') or os.path.basename(self.folder_path))
    @property
    def workflow_alias(self):  return self._meta.get('workflow_alias', '') or ''
    @property
    def description(self):     return str(self._meta.get('description') or '')
    @property
    def author(self):          return self._meta.get('author', '')
    @property
    def version(self):         return self._meta.get('version', '')
    @property
    def workflow_id(self):     return self._meta.get('workflow_id', '') or ''
    @property
    def workflow_hash(self):   return self._meta.get('workflow_hash', '') or ''
    @property
    def id_conflict(self):     return self._id_conflict
    @property
    def tags_category(self):   return self._meta.get('tags_category', [])
    @property
    def tags_models(self):     return self._meta.get('tags_models', [])
    @property
    def gizmo_params(self):
        # Snapshot-based architecture: gizmo_params is composed from the
        # persistent _snapshot + _overrides + _widget_order + _v3_user_edits.
        # Empty list when the workflow has no snapshot yet - the gizmo
        # builder will produce an empty group and the user knows to open
        # the editor and Sync.
        snap = self._meta.get('_snapshot')
        if not snap:
            return []
        from Nukomfy.gui.workflow_state import compose_params
        return compose_params(
            snap,
            self._meta.get('_overrides') or {},
            self._meta.get('_widget_order') or [],
            self._meta.get('_v3_user_edits') or {})

    @property
    def snapshot(self):
        return self._meta.get('_snapshot')

    @property
    def overrides(self):
        return self._meta.get('_overrides') or {}

    @property
    def widget_order(self):
        return self._meta.get('_widget_order') or []

    @property
    def v3_user_edits(self):
        return self._meta.get('_v3_user_edits') or {}

    @property
    def usage(self):           return self._meta.get('usage', '')
    @property
    def gizmo_color(self):     return self._meta.get('gizmo_color', 0)
    @property
    def gizmo_options(self):
        defaults = {'versioning': True, 'author': True,
                    'description': True, 'usage': True,
                    'output_preview': True,
                    'title': True,
                    'title_mode': 'use_gizmo_color',
                    'title_color': None,
                    'title_node_color': None,
                    'color_reads': True,
                    'word_wrap': True}
        saved = self._meta.get('gizmo_options', {})
        defaults.update(saved)
        return defaults

    @property
    def workflow(self):
        if self._workflow is None and os.path.isfile(self.workflow_path):
            try:
                with open(self.workflow_path, 'r', encoding='utf-8') as f:
                    self._workflow = json.load(f)
            except Exception as e:
                _log.warning(
                    'Workflow JSON load failed at %s: %s', self.workflow_path, e)
                self._workflow = {}
        return self._workflow


def _write_metadata_field(metadata_path, key, value):
    """Write/update a single key in metadata.json. Silent on failure."""
    import Nukomfy.utils.fs_safe as fs_safe
    try:
        data = {}
        if os.path.isfile(metadata_path):
            with open(metadata_path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
        data[key] = value
        tmp = metadata_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return fs_safe.atomic_replace(tmp, metadata_path)
    except Exception as e:
        _log.warning(
            "Workflow metadata write failed for key '%s' at %s: %s",
            key, metadata_path, e)
        return False


def save_workflow_id(item, new_id):
    """Persist a new workflow_id for an item. Returns True on success."""
    ok = _write_metadata_field(item.metadata_path, 'workflow_id', new_id)
    if ok:
        item._meta['workflow_id'] = new_id
    return ok


def scan_workflows(local_path, shared_paths=None):
    items = []
    for folder, source in [(local_path, 'Local')] + [(p, 'Shared') for p in (shared_paths or [])]:
        if not folder or not os.path.isdir(folder):
            continue
        try:
            for entry in sorted(os.listdir(folder)):
                ep = os.path.join(folder, entry)
                if os.path.isdir(ep) and (
                    os.path.isfile(os.path.join(ep, WORKFLOW_JSON)) or
                    os.path.isfile(os.path.join(ep, METADATA_JSON))
                ):
                    items.append(WorkflowItem(ep, source))
        except OSError as e:
            _log.warning(
                'Workflow scan skipped unreadable folder %s: %s', folder, e)

    # Collision detection: mark items sharing the same workflow_id.
    by_id = {}
    for it in items:
        wid = it.workflow_id
        if wid:
            by_id.setdefault(wid, []).append(it)
    for wid, group in by_id.items():
        if len(group) > 1:
            for it in group:
                it._id_conflict = True

    return items


def collect_tags(items):
    cats, mods = set(), set()
    for i in items:
        cats.update(i.tags_category)
        mods.update(i.tags_models)
    return sorted(cats), sorted(mods)


def filter_items(items, text='', active_tags=None, active_sources=None):
    if active_sources is not None:
        items = [i for i in items if i.source in active_sources]
    if text:
        t = text.lower()
        items = [i for i in items if t in i.name.lower() or t in i.description.lower()]
    if active_tags:
        items = [i for i in items if all(
            tag in i.tags_category + i.tags_models + [i.source] for tag in active_tags
        )]
    return items
