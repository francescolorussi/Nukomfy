"""Centralized OS user resolution for Nukomfy."""
from __future__ import annotations

import getpass
import os
import socket

_UNKNOWN = "unknown"


def _make_resolver():
    cached = None

    def current_user() -> str:
        """Return the OS username of the current session, cached for the
        life of the process. Falls back to "unknown" if no name can be
        resolved.
        """
        nonlocal cached
        if cached is not None:
            return cached
        try:
            username = os.getlogin()
        except OSError:
            username = ""
        if not username:
            try:
                username = getpass.getuser() or ""
            except Exception:
                username = ""
        cached = username or _UNKNOWN
        return cached

    return current_user


current_user = _make_resolver()


def header_user() -> str:
    """Return `current_user()` coerced to a latin-1-safe HTTP header value.

    http.client encodes header values as latin-1, so a username with
    characters outside latin-1 (Cyrillic, CJK, ...) raises UnicodeEncodeError
    at urlopen. Only the header transport needs coercing: the canonical
    identity (JSON bodies, ownership filters, filesystem paths) keeps the full
    Unicode name. Unencodable characters become '?'.
    """
    return current_user().encode("latin-1", "replace").decode("latin-1")


def ws_session_id() -> str:
    """Return the WebSocket clientId (the ?clientId= query param) for this
    process: ``user@host#pid``.

    Stable for the life of the process and unique across processes and hosts,
    so every viewer keeps its own server-side socket and receives the broadcast
    progress stream. This is a transport session id, NOT a user identity -
    ownership and "my jobs" filters use current_user().
    """
    return "{}@{}#{}".format(current_user(), socket.gethostname(), os.getpid())
