"""Centralized output path construction with VFX naming conventions.

Single source of truth for the output path used by:
  - gizmo preview UI
  - gizmo "Read Outputs" action
  - workflow_converter (path sent to ComfyUI/NukomfyWrite)
  - submit_panel overwrite check
  - submit_panel history record

The structure is driven by a configurable template stored in
``settings.output_path_template``. ``output_root`` (the user-level
"Output Path" setting) is always prepended automatically and is NOT
a token in the template.
"""

import logging
import os
import re

from Nukomfy.core.identity import current_user
from Nukomfy.core.settings import settings

_log = logging.getLogger(__name__)


# Strip only characters that break an output-path component's sinks
# (filesystem / Nuke Read `file` knob / glob); keep everything else so a
# name of any script survives.
#   < > : " | ? *   illegal in a Windows filename
#   / \             path separators
#   [ ] { } $ `     Nuke file-knob TCL substitution ([ ] also glob metachars)
#   # %             frame-padding tokens (#### and %0Nd) - would collide with
#                   the real frame placeholder in the built path
#   \x00-\x1f       control characters
# Space -> '_' (Nuke fromUserText reads a space as a frame-range separator).
_UNSAFE_PATH_CHARS = re.compile(r'[<>:"|?*/\\\[\]{}$`#%\x00-\x1f]')
_NK_VERSION_RE = re.compile(r'[_.]v\d+$', re.IGNORECASE)
_HEX_RE = re.compile(r'[^a-fA-F0-9]')

# Windows reserved device names: invalid as a path segment even with an
# extension (CON and CON.exr both fail). sanitize_name checks the stem and
# prefixes a match with '_'. Enforced on every OS so a path written on
# Linux/macOS stays portable to a Windows render node on the farm.
_WIN_RESERVED_NAMES = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + ['COM{}'.format(i) for i in range(1, 10)]
    + ['LPT{}'.format(i) for i in range(1, 10)])

# Characters illegal in a path component on Windows (the strictest of the
# three target OSes). Rejected in the template so a saved template can
# never produce an unwritable path on a Windows machine. '/' and '\\' are
# NOT here - they are the subfolder separators.
_WIN_ILLEGAL_CHARS = '<>:"|?*'

# Full UUID4 hex (32 char) is too verbose in output paths. 12 char
# gives 16^12 = 281T combinations, birthday-paradox safe at ~16M
# workflows - well beyond any realistic Nukomfy library scale.
_UUID_DISPLAY_LEN = 12

# Cap padding to 9 digits for both frame and version.
# 9 digits = ~999M frames / ~999M versions, well beyond any realistic
# scale. Single-digit constants kept symmetric for UI consistency.
_MAX_FRAME_PADDING = 9
_MAX_VERSION_PADDING = 9

# Upper bound on the number of per-output name knobs (output_name_<i>)
# scanned on a gizmo.
_MAX_OUTPUT_KNOBS = 100

# Keep in sync with settings._DEFAULT_TEMPLATE_LOGICAL (duplicated to
# avoid a circular import: settings is a dependency of this module).
DEFAULT_OUTPUT_TEMPLATE = (
    '{nk_file}/{workflow}/{output_name}/{version}'
    '/{nk_file}_{output_name}_{version}.{frame}.{ext}'
)

REQUIRED_TOKENS = ('{version}', '{frame}', '{ext}')

_TAG_JOIN_MAX_LEN = 50


def sanitize_name(name):
    """Make `name` a safe output-path component, keeping its real name.

    Strips only characters that would break a filename (Windows/Linux/macOS)
    or Nuke's Read-node path parsing (see `_UNSAFE_PATH_CHARS`); every other
    character, including letters of any script and ordinary punctuation, is
    preserved. Space -> '_'. Leading/trailing dot/space/underscore are
    dropped (Windows silently strips a trailing dot/space). A component whose
    stem is a Windows reserved device name (CON, NUL, COM1-9, ...) is
    prefixed with '_'.
    """
    if not name:
        return 'output'
    s = _UNSAFE_PATH_CHARS.sub('', str(name).strip()).replace(' ', '_')
    s = re.sub(r'_+', '_', s).strip('_. ')
    if not s:
        return 'output'
    if s.split('.', 1)[0].upper() in _WIN_RESERVED_NAMES:
        s = '_' + s
    return s


def _get_username():
    """OS username for the {username} token."""
    return current_user()


def _compact_hex(value, n=_UUID_DISPLAY_LEN):
    """Sanitize a uuid-like value to lowercase hex, truncated to n chars.

    Truncation is applied even when the input is already short, so a
    `_nfy_wf_id` of any length (a full 32-char UUID or a 12-char one)
    maps to the same fixed-width path segment.
    """
    if not value:
        return '0' * n
    cleaned = _HEX_RE.sub('', str(value)).lower()
    if not cleaned:
        return '0' * n
    return cleaned[:n]


def _safe_int_str(value, width=2, fallback=None):
    """Format an int as zero-padded string. Invalid -> fallback or '00…0'."""
    if value is None:
        return fallback if fallback is not None else '0' * width
    try:
        return '{:0{w}d}'.format(int(value), w=width)
    except (ValueError, TypeError):
        return fallback if fallback is not None else '0' * width


def clamp_padding(value, fallback=4):
    """Coerce a frame_padding value to int in [1, _MAX_FRAME_PADDING].

    Bounds: min=1, max=9. 0/negative collapses to 1 silently. Values
    above 9 are capped silently.
    """
    try:
        v = int(value)
    except (ValueError, TypeError):
        v = int(fallback)
    return max(1, min(_MAX_FRAME_PADDING, v))


def clamp_version_padding(value, fallback=3):
    """Coerce version_padding to int in [1, _MAX_VERSION_PADDING].

    Bounds: min=1, max=9. Driven by `settings.version_padding`.
    """
    try:
        v = int(value)
    except (ValueError, TypeError):
        v = int(fallback)
    return max(1, min(_MAX_VERSION_PADDING, v))


def _join_tags(tags, fallback, max_len=_TAG_JOIN_MAX_LEN):
    """Join a list of tag strings with '_' after sanitize. Empty -> fallback."""
    if not tags:
        return fallback
    parts = [sanitize_name(t) for t in tags if t]
    parts = [p for p in parts if p and p != 'output']
    if not parts:
        return fallback
    joined = '_'.join(parts)
    truncated = joined[:max_len].rstrip('_')
    return truncated or fallback


def nk_file_stem():
    """Return .nk stem without versioning suffix, or 'Untitled'."""
    try:
        import nuke  # type: ignore
        raw = nuke.root().name()
    except Exception:
        return 'Untitled'
    if not raw or raw == 'Root':
        return 'Untitled'
    stem = os.path.splitext(os.path.basename(raw))[0]
    stem = _NK_VERSION_RE.sub('', stem)
    return stem or 'Untitled'


def _normalize_template(template):
    """Accept both '/' and '\\' as subfolder separators in user-written
    templates and normalise to '/' for the runtime engine. Comfy server
    paths are always sent with '/' regardless of the user's local OS.
    """
    if template is None:
        return template
    return str(template).replace('\\', '/')


def _resolve_template(template):
    if template is not None:
        return _normalize_template(template)
    try:
        from Nukomfy.core.settings import settings
        raw = getattr(settings, 'output_path_template',
                      DEFAULT_OUTPUT_TEMPLATE)
    except Exception:
        raw = DEFAULT_OUTPUT_TEMPLATE
    return _normalize_template(raw)


def _build_tokens(nk_file, workflow, output_name, version, frame_token, ext,
                  node_name=None,
                  workflow_alias=None,
                  output_index=None,
                  workflow_uuid=None, workflow_categories=None,
                  workflow_models=None):
    """Build the token dict consumed by the template engine.

    Each token has a fail-safe fallback so missing optional inputs never
    raise KeyError on `.format()`.
    """
    safe_ext = re.sub(r'[^a-zA-Z0-9]', '', str(ext)) or 'exr'
    if safe_ext != str(ext):
        _log.warning('Output extension sanitized: %r -> %r', ext, safe_ext)
    _vw = clamp_version_padding(settings.version_padding)
    # int(version) is the one token read that can raise (ValueError/
    # TypeError) before .format() - guard it so the docstring's "never
    # raises" contract holds. First version is 1, so that is the fallback.
    try:
        _vnum = int(version)
    except (ValueError, TypeError):
        _vnum = 1
    return {
        'nk_file': sanitize_name(nk_file),
        'workflow': sanitize_name(workflow),
        'workflow_alias': (sanitize_name(workflow_alias) if workflow_alias
                           else sanitize_name(workflow)),
        'output_name': sanitize_name(output_name),
        'node': sanitize_name(node_name) if node_name else 'node',
        'version': 'v{val:0{w}d}'.format(val=_vnum, w=_vw),
        'frame': frame_token,
        'ext': safe_ext,
        'output_index': _safe_int_str(output_index, width=2),
        'workflow_uuid': _compact_hex(workflow_uuid),
        'workflow_category': _join_tags(workflow_categories, 'uncategorized'),
        'workflow_model': _join_tags(workflow_models, 'unknown_model'),
        'username': sanitize_name(_get_username()),
    }


def build_output_path(output_root, nk_file, workflow, output_name,
                      version, frame_token, ext, node_name=None,
                      workflow_alias=None,
                      template=None,
                      output_index=None,
                      workflow_uuid=None, workflow_categories=None,
                      workflow_models=None):
    """Build the full output path from template + per-output context.

    ``output_root`` comes from the "Output Path" setting (already TCL-
    resolved by the caller) and is prepended automatically.

    Tokens available in the template:
      {nk_file}            .nk stem without version suffix, or 'Untitled'
      {workflow}           sanitized workflow name
      {workflow_alias}     sanitized Workflow Alias, or {workflow} if unset
      {output_name}        sanitized output name
      {node}               sanitized Nuke node name, may carry a numeric
                           suffix (defaults to 'node')
      {version}            'v001', 'v002' ...                  [REQUIRED]
      {frame}              frame padding token, e.g. '%04d'    [REQUIRED]
      {ext}                file extension                       [REQUIRED]
      {output_index}       2-digit output index '01', '02' ... (1-based)
      {workflow_uuid}      12-char hex workflow UUID (or '0' * 12)
      {workflow_category}  sanitized tag(s) joined by '_' (or 'uncategorized')
      {workflow_model}     sanitized model tag(s) joined by '_'
                           (or 'unknown_model')

    NOTE on {frame}: the caller builds frame_token from the per-output
    padding read from NukomfyWrite ('frame_padding' input/knob). Default 4
    if not specified. Examples:
      padding=4 -> frame_token='%04d' or '0042' (resolved single frame)
      padding=3 -> frame_token='%03d' or '042'
      padding=5 -> frame_token='%05d' or '00042'
    This module does not know the padding: it receives the already-
    formatted token from the caller, which has access to the knob.

    Submit-runtime tokens that *cannot* be resolved at gizmo/preview time
    (machine, job id, batch index, seed) are intentionally NOT supported,
    so the gizmo's Read Outputs button always rebuilds the same path the
    submit wrote.
    """
    template = _resolve_template(template)
    root = (output_root or '').replace('\\', '/').rstrip('/')
    tokens = _build_tokens(
        nk_file, workflow, output_name, version, frame_token, ext,
        node_name=node_name,
        workflow_alias=workflow_alias,
        output_index=output_index,
        workflow_uuid=workflow_uuid,
        workflow_categories=workflow_categories,
        workflow_models=workflow_models,
    )
    def _safe_fallback():
        # Guaranteed-valid path built from sanitized tokens, inside root.
        return os.path.join(
            root,
            tokens['nk_file'],
            tokens['workflow'],
            tokens['output_name'],
            tokens['version'],
            '{}_{}_{}_{}.{}'.format(
                tokens['nk_file'], tokens['output_name'],
                tokens['version'], tokens['frame'], tokens['ext']),
        ).replace('\\', '/')

    try:
        candidate = '{}/{}'.format(root, template.format(**tokens))
    except (KeyError, ValueError, IndexError, AttributeError) as e:
        # The Output Path template is user-editable. The Settings UI validates
        # it, but a hand-edited settings.json (or a bad default) could carry a
        # stray '{' or an unknown token - degrade to a safe path instead of
        # crashing at submit / Read Outputs / preview.
        _log.warning('Output template %r is invalid (%s) - using safe '
                     'fallback inside root', template, e)
        candidate = _safe_fallback()
    else:
        if root:
            norm_root = os.path.normpath(root)
            norm_candidate = os.path.normpath(candidate)
            if not (norm_candidate.startswith(norm_root + os.sep)
                    or norm_candidate == norm_root):
                _log.warning(
                    'Output path escaped root %r (candidate %r) - using '
                    'safe fallback inside root', root, candidate)
                candidate = _safe_fallback()
    return candidate


_SAMPLE_TOKENS = {
    'nk_file': 'My_Comp',
    'workflow': 'My_Workflow',
    'workflow_alias': 'My_Alias',
    'output_name': 'Output_Name',
    'node': 'Nukomfy_My_Workflow',
    'version': 'v001',
    'frame': '0001',
    'ext': 'exr',
    'output_index': '01',
    'workflow_uuid': '7b3aec123456',
    'workflow_category': 'Tag_Category',
    'workflow_model': 'Tag_Model',
    'username': 'My_User',
}


def validate_template(template):
    """Validate template. Returns (ok, error_message).

    Checks:
      - all REQUIRED_TOKENS present
      - template contains at least one '/' (subfolder): a flat template
        would make the output directory resolve to the output root
        itself, so a popup "Overwrite?" -> rmtree could wipe the entire
        root.
      - template resolves with sample values (catches unknown tokens)
      - no TCL-like syntax: the template is pure Python `.format()`,
        unlike the Output Path field above which *does* evaluate TCL.
        A stray `[`, `]`, or `$` would leak through as a literal char
        in the final path - almost always a user mistake.
    """
    if not template or not template.strip():
        return False, 'Template is empty'
    for c in '[]$':
        if c in template:
            return False, 'Brackets and "$" are not allowed here'
    for c in _WIN_ILLEGAL_CHARS:
        if c in template:
            return False, 'Character not allowed in a path: {}'.format(c)
    if any(ord(c) < 32 for c in template):
        return False, 'Control characters are not allowed'
    norm = _normalize_template(template)
    if '..' in norm:
        return False, 'The template cannot contain ".." (parent-folder steps)'
    if '/' not in norm:
        return False, 'Template must contain at least one subfolder separator (/ or \\)'
    for t in REQUIRED_TOKENS:
        if t not in norm:
            return False, 'Missing required token: {}'.format(t)
    try:
        norm.format(**_SAMPLE_TOKENS)
    except (KeyError, ValueError, IndexError) as e:
        return False, 'Invalid template: {}'.format(e)
    return True, ''


_PREVIEW_PH_OPEN = '\x00\x02PH_OPEN\x02\x00'
_PREVIEW_PH_CLOSE = '\x00\x02PH_CLOSE\x02\x00'


def preview_template(template, output_root='', placeholder_html_color=None,
                     frame_padding_override=None,
                     version_padding_override=None):
    """Resolve template with placeholder values for the settings UI.

    Returns the resolved string in OS-native separators (display only),
    or None if the template is invalid. Runtime paths from
    ``build_output_path`` keep '/' for cross-OS portability (Comfy server
    is often Linux); only this preview adopts OS conventions for the UI.

    Tokens that are process-level constants (env vars, OS user, current
    .nk filename, padding from Settings) are resolved to their real
    values in Settings preview; only gizmo-state-dependent tokens use
    sample placeholders. ``{frame}`` and ``{version}`` use the digit
    width from ``settings.frame_padding`` / ``settings.version_padding``
    so the preview reflects the configured padding.

    If ``placeholder_html_color`` is provided (e.g. ``'#daa520'``),
    returns Qt RichText HTML where placeholder token values are wrapped
    in literal ``{ }`` markers (only the braces mark them - the colour
    itself is not applied). The function escapes literal path segments to
    keep the HTML safe. Without the parameter, the return value is plain
    text (the form used by logging/test sites).
    """
    root = (output_root or '').replace('\\', '/').rstrip('/') or '/output'
    norm = _normalize_template(template) if template else template
    # Process-level constants resolved to real values.
    real_tokens = {
        'nk_file': sanitize_name(nk_file_stem()),
        'username': sanitize_name(_get_username()),
    }
    # Frame/version stay classified as placeholders (wrapped in {} in
    # HTML mode) but the *digit width* follows Settings so the preview
    # reflects the configured padding: '0001' if 4, '00001' if 5,
    # 'v001' if 3, 'v0001' if 4. Override params let the live Settings
    # dialog pass the in-progress UI values without committing them to
    # settings yet.
    _fw = clamp_padding(
        frame_padding_override if frame_padding_override is not None
        else settings.frame_padding)
    _vw = clamp_version_padding(
        version_padding_override if version_padding_override is not None
        else settings.version_padding)
    placeholder_overrides = {
        'frame': '1'.zfill(_fw),
        'version': 'v{val:0{w}d}'.format(val=1, w=_vw),
    }
    tokens = {}
    for k, v in _SAMPLE_TOKENS.items():
        if k in real_tokens:
            tokens[k] = real_tokens[k]
            continue
        sample_val = placeholder_overrides.get(k, v)
        if placeholder_html_color:
            tokens[k] = '{}{}{}'.format(
                _PREVIEW_PH_OPEN, sample_val, _PREVIEW_PH_CLOSE)
        else:
            tokens[k] = sample_val
    try:
        resolved = norm.format(**tokens)
    except (KeyError, ValueError, IndexError, AttributeError):
        return None
    full = '{}/{}'.format(root, resolved)
    if os.sep != '/':
        full = full.replace('/', os.sep)
    if not placeholder_html_color:
        return full
    # OS-sep replacement done - wrap placeholder values in literal { }
    # as the visual marker (no color: braces alone are the signal).
    # All literal segments HTML-escaped so user paths with < or & render
    # safely; only the braces mark the placeholders, the color is
    # intentionally not applied.
    import html as _html_mod
    parts = full.split(_PREVIEW_PH_OPEN)
    out = [_html_mod.escape(parts[0])]
    for chunk in parts[1:]:
        if _PREVIEW_PH_CLOSE in chunk:
            ph_val, rest = chunk.split(_PREVIEW_PH_CLOSE, 1)
            out.append('{{{}}}'.format(_html_mod.escape(ph_val)))
            out.append(_html_mod.escape(rest))
        else:
            out.append(_html_mod.escape(chunk))
    return ''.join(out)


def _per_output_value(gizmo, all_params, target_node_id, name, fallback):
    """Return the runtime value of `name` for the NukomfyWrite at
    `target_node_id`. If exposed as a knob (enabled=True), reads the live
    knob value; otherwise uses the workflow_creator's `default_value`.
    Mirrors the submit-time injection logic so Read Outputs and the
    gizmo preview reconstruct the exact path the submit will write.
    """
    fallback_val = fallback
    for p in all_params:
        if p.get('target_node_id') != target_node_id:
            continue
        if p.get('name') != name:
            continue
        default = p.get('default_value', fallback)
        if p.get('enabled', True):
            kn = p.get('_knob_name', '')
            k = gizmo.knob(kn) if kn else None
            if k is not None:
                try:
                    v = k.value()
                except Exception:
                    v = None
                if v not in (None, ''):
                    return v
        return default if default not in (None, '') else fallback_val
    return fallback_val


def resolve_gizmo_outputs(gizmo, frame_style='printf'):
    """Build all output paths from current gizmo knob state.

    Single source of truth for gizmo->path resolution, consumed by:
      - gizmo_callbacks._update_output_preview (HTML preview in UI)
      - gizmo_actions._resolve_output_paths    (Read Outputs button)
      - submit_panel._build_all_check_dirs     (pre-submit collision check)

    frame_style: 'printf' for '%04d', 'hash' for '####'.

    Returns list[dict] (one per enabled output role, in role order):
        {
          'path':            full output path with frame token,
          'io_mode':         'Single' | 'Sequence' | '',
          'ext':             resolved file_type,
          'padding':         resolved + clamped frame_padding (int),
          'name':            output_name (already stripped),
          'target_node_id':  node id in workflow,
          'output_index':    1-based index passed to build_output_path,
        }

    Returns [] if the gizmo lacks `_nfy_params` or has no enabled
    outputs.
    """
    import json
    from Nukomfy.core.settings import settings
    from Nukomfy.utils.path_utils import runtime_path
    from Nukomfy.workflows._payload import decode_payload

    params_knob = gizmo.knob('_nfy_params')
    if not params_knob:
        return []
    all_params = decode_payload(params_knob.value(), default=[])

    output_params = [p for p in all_params
                     if p.get('role') == 'output' and p.get('enabled', True)]
    if not output_params:
        return []

    ver_knob = gizmo.knob('_output_version')
    try:
        version = int(ver_knob.value()) if ver_knob else 1
    except (ValueError, TypeError):
        version = 1

    raw = settings.default_output_path
    base_dir = runtime_path(raw, fallback=raw).replace('\\', '/').rstrip('/')

    wf_name_knob = gizmo.knob('_nfy_wf_name')
    workflow_name = (wf_name_knob.value() if wf_name_knob else '') or 'output'
    alias_knob = gizmo.knob('_nfy_workflow_alias')
    workflow_alias = alias_knob.value() if alias_knob else ''

    output_names = []
    for i in range(_MAX_OUTPUT_KNOBS):
        knob = gizmo.knob('output_name_{}'.format(i))
        if not knob:
            break
        output_names.append(knob.value().strip() or 'output')
    if not output_names:
        on_knob = gizmo.knob('output_name')
        output_names = [
            (on_knob.value().strip() if on_knob else '') or 'output']

    nk_file = nk_file_stem()
    node_name = gizmo.name()

    wf_id_knob = gizmo.knob('_nfy_wf_id')
    workflow_uuid = wf_id_knob.value() if wf_id_knob else ''

    def _parse_json_list(name):
        k = gizmo.knob(name)
        if not k:
            return []
        try:
            data = json.loads(k.value() or '[]')
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    workflow_categories = _parse_json_list('_nfy_wf_categories')
    workflow_models = _parse_json_list('_nfy_wf_models')

    results = []
    # frame_padding is read from Settings, ignoring any per-NukomfyWrite
    # knob/workflow value. Single source of truth so the gizmo preview,
    # the submit pipeline, and Read Outputs all agree.
    padding = clamp_padding(settings.frame_padding)
    for i, op in enumerate(output_params):
        name = output_names[i] if i < len(output_names) else 'output'
        nid = op.get('target_node_id', '')
        ext = (_per_output_value(gizmo, all_params, nid, 'file_type', 'exr') or '').strip() or 'exr'
        if frame_style == 'hash':
            frame_pat = '#' * padding
        else:
            frame_pat = '%0{}d'.format(padding)
        path = build_output_path(base_dir, nk_file, workflow_name, name,
                                 version, frame_pat, ext,
                                 node_name=node_name,
                                 workflow_alias=workflow_alias,
                                 output_index=i + 1,
                                 workflow_uuid=workflow_uuid,
                                 workflow_categories=workflow_categories,
                                 workflow_models=workflow_models)
        results.append({
            'path': path,
            'io_mode': op.get('io_mode', ''),
            'ext': ext,
            'padding': padding,
            'name': name,
            'target_node_id': nid,
            'output_index': i + 1,
        })
    return results
