"""Client-side mount spec format validation.

Server-side enforcement (protected paths, allowed roots) is handled
by the backend.  This module only catches obvious format errors
before sending the spec to the API.
"""

import re

_VALID_MOUNT_OPTIONS = {
    "ro",
    "rw",
    "z",
    "Z",
    "nocopy",
    "consistent",
    "cached",
    "delegated",
}


def validate_mount_spec(spec: str) -> str | None:
    """Validate a container mount spec string.

    Returns None if valid, or an error message string if invalid.
    Valid forms: source:dest or source:dest:options
    The container path (dest) must be absolute.
    """
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        return (
            f"Invalid mount {spec!r}: "
            "expected source:dest or source:dest:options"
        )
    source, dest = parts[0], parts[1]
    if not source:
        return f"Invalid mount {spec!r}: source is empty"
    if not dest.startswith("/"):
        return (
            f"Invalid mount {spec!r}: "
            "container path must be absolute (start with /)"
        )
    if len(parts) == 3:
        options = parts[2]
        for opt in options.split(","):
            if opt and opt not in _VALID_MOUNT_OPTIONS:
                return f"Invalid mount {spec!r}: unknown option {opt!r}"
    return None


# An egress allowed-domain entry: ``host`` or ``host:port`` (DNS name or
# IPv4), or a bracketed IPv6 literal (``[::1]`` / ``[2001:db8::1]:443``).
# This catches gross typos client-side; the server
# (:func:`klangk.netfilter.parse_allowed_domains`) does the authoritative
# check (#1365, #1745).
_ALLOWED_DOMAIN_RE = re.compile(
    r"^(?:"
    r"\[[0-9a-fA-F:.]+\](?::\d{1,5})?"  # [ipv6] or [ipv6]:port
    r"|"
    r"[^\[\]/\s:]+(?::\d{1,5})?"  # host or host:port
    r")$"
)


def validate_allowed_domain_spec(spec: str) -> str | None:
    """Validate an egress allowed-domain entry (``host`` or ``host:port``).

    Returns None if valid, or an error message. Accepts a DNS name or IP
    (IPv4, or a bracketed IPv6 literal like ``[::1]`` / ``[::1]:443``),
    optionally followed by ``:port``. Empty / whitespace / stray slashes
    are rejected. Mirrors the Flutter ``validateAllowedDomainSpec`` and the
    TUI editor; the server does the authoritative validation (#1365, #1745).
    """
    s = spec.strip()
    if not s:
        return f"Invalid allowed-domain {spec!r}: empty"
    if not _ALLOWED_DOMAIN_RE.match(s):
        return (
            f"Invalid allowed-domain {spec!r}: "
            "expected host or host:port (IPv6 in brackets)"
        )
    return None
