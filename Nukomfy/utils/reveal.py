"""OS file manager reveal helper."""

import os
import subprocess
import sys
from logging import getLogger

_log = getLogger(__name__)


REVEAL_OK = 'ok'
REVEAL_EMPTY = 'empty'
REVEAL_MISSING = 'missing'
REVEAL_UNREACHABLE = 'unreachable'
REVEAL_LAUNCH_FAILED = 'launch_failed'


def reveal_folder(path):
    """Open *path* in the OS file manager.

    Returns a status code:
      - REVEAL_OK             - file manager command was spawned.
      - REVEAL_EMPTY          - *path* was empty / falsy.
      - REVEAL_MISSING        - *path* points to a local directory that
                                does not exist.
      - REVEAL_UNREACHABLE    - *path* is a UNC share (\\\\server\\...)
                                that could not be probed (server offline
                                or no permission).
      - REVEAL_LAUNCH_FAILED  - the OS file manager command could not
                                be spawned (OSError / FileNotFoundError).
    """
    if not path:
        return REVEAL_EMPTY
    target = os.path.normpath(path)
    try:
        exists = os.path.isdir(target)
    except OSError:
        exists = False
    if not exists:
        if target.startswith('\\\\') or target.startswith('//'):
            return REVEAL_UNREACHABLE
        return REVEAL_MISSING

    if sys.platform == 'win32':
        cmd = ['explorer', target]
    elif sys.platform == 'darwin':
        cmd = ['open', target]
    else:
        cmd = ['xdg-open', target]
    try:
        subprocess.Popen(cmd)
        return REVEAL_OK
    except (OSError, FileNotFoundError) as exc:
        _log.warning('reveal_folder failed (cmd=%s, path=%s): %s',
                     cmd[0], target, exc)
        return REVEAL_LAUNCH_FAILED
