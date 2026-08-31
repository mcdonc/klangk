"""Client-side mount spec format validation.

Server-side enforcement (protected paths, allowed roots) is handled
by the backend.  This module only catches obvious format errors
before sending the spec to the API.
"""

import ipaddress
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


# An egress allowed-domain entry: ``host``, ``host:port``, or an IPv4 CIDR
# range (``10.0.0.0/8``, optionally ``10.0.0.0/8:443``). IPv6 literals and
# IPv6 CIDRs are rejected — IPv6 is disabled inside filtered containers
# (#1936), so a v6 destination is neither reachable nor enforceable, and
# the bracket grammar (``[::1]:443``) has been removed. This catches gross
# typos client-side; the server
# (:func:`klangk.netfilter.parse_allowed_domains`) does the authoritative
# check (#1365, #1745, #1935).
_ALLOWED_DOMAIN_RE = re.compile(
    r"^[^\[\]/\s:]+(?::\d{1,5})?$"  # host or host:port (IPv4 / DNS)
)


def validate_allowed_domain_spec(
    spec: str, *, allow_cidr: bool = True
) -> str | None:
    """Validate an egress allowed/rejected-domain entry.

    Returns None if valid, or an error message. Accepts a DNS name, an IPv4
    address (optionally followed by ``:port``), or -- when ``allow_cidr`` is
    True -- an IPv4 CIDR range (``10.0.0.0/8``, optionally scoped to a port as
    ``10.0.0.0/8:443``). Empty / whitespace / IPv6 literals (``[::1]``) / IPv6
    CIDRs are rejected — IPv6 is disabled inside filtered containers (#1936),
    so a v6 destination is neither reachable nor enforceable. Mirrors the Flutter
    ``validateAllowedDomainSpec`` and the TUI editor; the server does the
    authoritative validation (#1365, #1745, #1935).

    ``allow_cidr=False`` is used for ``rejected_domains`` (#2367): NXDOMAIN
    enforcement is name-level (no IP dimension), so a CIDR is meaningless there
    and is rejected up front rather than round-tripping to the API.
    """
    s = spec.strip()
    if not s:
        return f"Invalid allowed-domain {spec!r}: empty"
    # A "/" denotes an IPv4 CIDR range; route it to the CIDR check before
    # the host regex (which excludes "/").
    if "/" in s:
        if not allow_cidr:
            return (
                f"Invalid rejected-domain {spec!r}: CIDR ranges are not "
                "supported (NXDOMAIN is name-level)"
            )
        return _validate_cidr_domain_spec(spec, s)
    if not _ALLOWED_DOMAIN_RE.match(s):
        return (
            f"Invalid allowed-domain {spec!r}: "
            "expected host, host:port, or IPv4 CIDR (e.g. 10.0.0.0/8)"
        )
    return None


def _validate_cidr_domain_spec(spec: str, s: str) -> str | None:
    """Client-side IPv4 CIDR pre-check, mirroring the server's
    :func:`klangk.netfilter.valid_cidr_spec` (#1935).

    ``s`` is the stripped spec; ``spec`` is the original (for the error
    message). Accepts ``<ip>/<plen>`` or ``<ip>/<plen>:<port>`` (port
    1–65535). IPv6 CIDRs and malformed prefixes are rejected —
    ``IPv4Network`` raises on both.
    """
    cidr = s
    port: str | None = None
    if ":" in s:
        cidr, port = s.rsplit(":", 1)
        if not port or not port.isdigit() or int(port) > 65535:
            return (
                f"Invalid allowed-domain {spec!r}: CIDR port must be 1–65535"
            )
    try:
        ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return (
            f"Invalid allowed-domain {spec!r}: "
            "expected IPv4 CIDR (e.g. 10.0.0.0/8)"
        )
    return None
