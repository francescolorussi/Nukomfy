"""URL obfuscation for at-rest storage of hidden machine URLs.

UI-level convenience to keep URLs out of casual sight in config files,
local DB, and logs. The algorithm is intentionally simple (XOR with a
key derived from the machine name) and is not a security boundary. For
real access control, configure ComfyUI behind a reverse proxy with
authentication, or restrict network access at the firewall level.
"""

import base64
import hashlib


_SALT = b'nukomfy-url-v1'
_PREFIX = 'nfyo:'


def _derive_key(machine_name):
    base = (machine_name or '').encode('utf-8')
    return hashlib.sha256(base + _SALT).digest()


def obfuscate_url(plain_url, machine_name):
    """Return `plain_url` wrapped as an obfuscated blob keyed by name."""
    if not plain_url:
        return ''
    key = _derive_key(machine_name)
    plain_bytes = plain_url.encode('utf-8')
    obf = bytes(b ^ key[i % len(key)] for i, b in enumerate(plain_bytes))
    return _PREFIX + base64.urlsafe_b64encode(obf).decode('ascii')


def deobfuscate_url(stored, machine_name):
    """Reverse `obfuscate_url`. Pass-through for plain URLs (no prefix).

    Returns `''` when the stored blob is malformed or the machine name
    does not match the one used to obfuscate. Silent by design: the
    caller cannot tell a corrupted record apart from a missing one,
    and a missing record never leaks anything useful.
    """
    if not stored or not stored.startswith(_PREFIX):
        return stored
    key = _derive_key(machine_name)
    try:
        obf = base64.urlsafe_b64decode(stored[len(_PREFIX):].encode('ascii'))
        plain_bytes = bytes(b ^ key[i % len(key)] for i, b in enumerate(obf))
        return plain_bytes.decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return ''


def is_obfuscated(stored):
    return isinstance(stored, str) and stored.startswith(_PREFIX)


def scrub_url_in_text(text, *urls):
    """Replace known URLs in `text` with their `fmt_machine()` form.

    Used on server error messages or exception traceback before they are
    surfaced to the UI, so that the URL does not leak through error text.
    """
    if not text:
        return text
    from Nukomfy.utils.log_format import fmt_machine
    out = str(text)
    for url in urls:
        if url and url in out:
            out = out.replace(url, fmt_machine(url))
    return out
