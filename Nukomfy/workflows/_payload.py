"""Hidden-knob payload encoding helpers.

Nuke's String_Knob runs TCL substitution on its value at .nk reload AND
at copy/paste time. With long JSON payloads containing brackets and
braces (even when escaped in the .nk text), the parser intermittently
fails with "missing close-bracket" and replaces the value with that
error string. Encoding payloads as base64 ASCII bypasses this entirely:
the value contains only [A-Za-z0-9+/=] which TCL never interprets.

This module has zero dependencies on `nuke` so it can be used both
inside Nuke and in standalone tests.
"""

import base64
import json


def encode_payload(obj):
    raw = json.dumps(obj).encode('utf-8')
    return base64.b64encode(raw).decode('ascii')


def decode_payload(value, default=None):
    if not value:
        return default if default is not None else []
    s = value.strip()
    try:
        decoded = base64.b64decode(s, validate=True).decode('utf-8')
        return json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError):
        return default if default is not None else []
