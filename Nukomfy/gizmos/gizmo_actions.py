"""Functions triggered by gizmo PyScript_Knob buttons."""

import os


def _resolve_output_paths(gizmo):
    """Return list of (path, io_mode) tuples for enabled output roles.
    Thin wrapper around `output_path.resolve_gizmo_outputs` preserving
    the (path, io_mode) tuple shape consumed by `read_outputs` and
    `_expand_paths_split_batch`.
    """
    from Nukomfy.utils.output_path import resolve_gizmo_outputs
    outputs = resolve_gizmo_outputs(gizmo, frame_style='printf')
    return [(o['path'], o['io_mode']) for o in outputs]


def _expand_paths_split_batch(paths_with_modes):
    """Expand Single-mode outputs into one entry per file on disk.

    For each (path, io_mode) tuple:
        - Single mode + pattern path: glob the disk and return all matching
          files sorted (zero-padded frame numbers sort correctly).
        - Single mode + no files matching: fall back to [path] so the
          downstream `scan_output_paths` raises the existing 'no_frames'
          popup.
        - Sequence mode (or any other): keep the single pattern path so
          create_read_nodes_grid can produce a sequence Read.

    Returns list[list[str]] - outer index = output role (vertical row in
    the grid), inner = files (horizontal columns).
    """
    import re
    import glob
    from Nukomfy.utils.fs_safe import _long_path
    rows = []
    for path, io_mode in paths_with_modes:
        if io_mode == 'Single':
            # glob.escape the literal path so bracket chars in a user output
            # root (e.g. .../proj[v2]/...) are not read as a glob char class.
            escaped = glob.escape(path)
            glob_pat = re.sub(r'%0\d+d|#+', '*', escaped)
            if glob_pat != escaped:
                files = sorted(glob.glob(_long_path(glob_pat)))
                rows.append(files if files else [path])
                continue
        rows.append([path])
    return rows


def scan_output_paths(paths):
    """Pre-scan paths and return (status, path_frames).

    status: 'ok' | 'no_dir' | 'no_frames'
    path_frames: list of (path, frames_list) for paths whose parent dir
    exists. For a padded pattern, frames is the globbed frame numbers;
    for a single-file path, [0] if the file exists, else [].

    Used by `create_read_nodes` and by MyJobs so the caller can show a
    Qt popup parented to its own widget instead of `nuke.message`.
    """
    import re
    import glob
    from Nukomfy.utils.fs_safe import _long_path

    valid_paths = []
    for path in paths:
        if not path:
            continue
        check = re.sub(r'%0\d+d|#+', '0001', path)
        if os.path.isdir(_long_path(os.path.dirname(check))):
            valid_paths.append(path)

    if not valid_paths:
        return 'no_dir', []

    path_frames = []
    for path in valid_paths:
        frames = []
        escaped = glob.escape(path)
        glob_pat = re.sub(r'%0\d+d|#+', '*', escaped)
        if glob_pat != escaped:
            for f in glob.glob(_long_path(glob_pat)):
                fm = re.search(r'(\d+)(?=\.[^.]+$)', f.replace('\\', '/'))
                if fm:
                    frames.append(int(fm.group(1)))
        else:
            if os.path.isfile(_long_path(path)):
                frames.append(0)
        path_frames.append((path, frames))

    if not any(frames for _, frames in path_frames):
        return 'no_frames', path_frames

    return 'ok', path_frames


def create_read_nodes(paths, xy=None, on_empty_message=True, color=0):
    """Create Nuke Read nodes for `paths`, auto-detecting frame ranges.

    Returns the number of Reads created. If `xy` is given as (x, y), the
    first Read is placed there and subsequent ones step 150 px to the right.
    `color` is a Nuke tile_color integer (0 = Nuke default).
    """
    import nuke  # type: ignore

    status, path_frames = scan_output_paths(paths)

    if status == 'no_dir':
        if on_empty_message:
            nuke.message('Unable to import output.\n\n'
                         'The output directory could not be found. '
                         'The files may not have been rendered yet, '
                         'or the directory may have been moved or deleted.')
        return 0

    if status == 'no_frames':
        if on_empty_message:
            nuke.message('No output frames found on disk.\n\n'
                         'The output directory exists but contains no '
                         'rendered frames. The job may have failed, or '
                         'the frames may have been deleted or overwritten '
                         'by a later job.')
        return 0

    # When `xy` is provided (e.g. from the gizmo button), anchor the first
    # Read there and step +200 px per extra output. When `xy` is None (e.g.
    # from MyJobs, where no gizmo position is available), let Nuke auto-place
    # the first Read wherever it would normally go (selected node / DAG
    # default), then step subsequent Reads +200 from that anchor.
    from Nukomfy.utils.fs_safe import to_short_path_win
    anchor = None
    created = 0
    for path, frames in path_frames:
        read = nuke.createNode('Read', inpanel=False)
        # fromUserText parses the path the same way the file browser does
        # (frame range, padding token, etc.) - createNode + setValue leaves
        # the node with stale first/last and it reports "file not found".
        # Win 8.3 short form keeps the path under MAX_PATH for Nuke's
        # Read node, which doesn't honour the long-path adapter.
        read.knob('file').fromUserText(to_short_path_win(path))
        if frames and any(f > 0 for f in frames):
            first, last = min(frames), max(frames)
            read.knob('first').setValue(first)
            read.knob('last').setValue(last)
            read.knob('origfirst').setValue(first)
            read.knob('origlast').setValue(last)
        if xy is not None:
            x0, y0 = xy
            read.setXYpos(x0 + created * 150, y0)
        else:
            if anchor is None:
                anchor = (read.xpos(), read.ypos())
            else:
                read.setXYpos(anchor[0] + created * 150, anchor[1])
        if color:
            read.knob('tile_color').setValue(color)
        created += 1
    return created


def create_read_nodes_grid(rows, xy, on_empty_message=True, color=0,
                           y_step=110, x_step=150):
    """Create Read nodes laid out in a grid: outer = output role (rows
    stacked vertically), inner = files (columns laid out horizontally).

    Used by `read_outputs` when the gizmo's `batch_read_mode` is
    'Batch as separate Reads' and at least one Single-mode output has
    multiple files on disk. Pre-scans all paths flattened to show a
    single no_dir/no_frames popup consistent with `create_read_nodes`.
    """
    import nuke  # type: ignore

    flat = [p for row in rows for p in row]
    status, _ = scan_output_paths(flat)
    if status == 'no_dir':
        if on_empty_message:
            nuke.message('Unable to import output.\n\n'
                         'The output directory could not be found. '
                         'The files may not have been rendered yet, '
                         'or the directory may have been moved or deleted.')
        return 0
    if status == 'no_frames':
        if on_empty_message:
            nuke.message('No output frames found on disk.\n\n'
                         'The output directory exists but contains no '
                         'rendered frames. The job may have failed, or '
                         'the frames may have been deleted or overwritten '
                         'by a later job.')
        return 0

    from Nukomfy.utils.fs_safe import to_short_path_win
    x0, y0 = xy
    created = 0
    for r, row in enumerate(rows):
        for c, path in enumerate(row):
            read = nuke.createNode('Read', inpanel=False)
            read.knob('file').fromUserText(to_short_path_win(path))
            single_status, single_path_frames = scan_output_paths([path])
            if single_status == 'ok' and single_path_frames and single_path_frames[0][1]:
                frames = single_path_frames[0][1]
                if any(f > 0 for f in frames):
                    first, last = min(frames), max(frames)
                    read.knob('first').setValue(first)
                    read.knob('last').setValue(last)
                    read.knob('origfirst').setValue(first)
                    read.knob('origlast').setValue(last)
            read.setXYpos(x0 + c * x_step, y0 + r * y_step)
            if color:
                read.knob('tile_color').setValue(color)
            created += 1
    return created


def read_outputs(gizmo):
    """Create Nuke Read nodes for resolved output paths.

    Dispatches on the gizmo's `batch_read_mode` knob (only present on
    batch-supporting workflows): 'Batch as single sequence' (default)
    creates one Read per output with first/last auto-detected from disk;
    'Batch as separate Reads' expands each Single-mode output's files
    into individual Reads, laid out as a grid (one row per output, files
    horizontally within each row).
    """
    paths_with_modes = _resolve_output_paths(gizmo)
    if not paths_with_modes:
        return
    color_knob = gizmo.knob('_nfy_read_color')
    try:
        color = int(color_knob.value()) if color_knob else 0
    except (ValueError, TypeError):
        color = 0

    mode_knob = gizmo.knob('batch_read_mode')
    split = (mode_knob is not None
             and mode_knob.value() == 'Batch as separate Reads')

    # Exit Group context so Read nodes are created in the main DAG
    gizmo.end()
    base_xy = (gizmo.xpos(), gizmo.ypos() + 65)

    if split:
        rows = _expand_paths_split_batch(paths_with_modes)
        if any(len(r) > 1 for r in rows):
            create_read_nodes_grid(rows, xy=base_xy, color=color)
        else:
            # No output expanded -> single-row layout, identical to default
            flat = [r[0] for r in rows]
            create_read_nodes(flat, xy=base_xy, color=color)
    else:
        paths = [p for p, _io in paths_with_modes]
        create_read_nodes(paths, xy=base_xy, color=color)
