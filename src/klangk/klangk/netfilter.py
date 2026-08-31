"""Per-workspace network egress filtering via the FQDN network sidecar.

A workspace may declare ``allowed_domains`` (a list of ``host``,
``host:port``, or IPv4 CIDR specs — ``10.0.0.0/8`` / ``10.0.0.0/8:443``).
When the network sidecar is configured (``network_sidecar_image``), a
workspace with allowed_domains runs in the sidecar's network namespace
(``--network container:<sidecar>``); the sidecar owns the egress ruleset —
default-deny OUTPUT, loopback, established, the DNS REDIRECT + FQDN proxy
that learns and allow-lists resolved IPs, static CIDR allows, the backend
gateway, IPv6 default-deny, and (interactive mode) NFLOG. The workspace
container itself is unprivileged. The sidecar image + entrypoint live in
``src/containers/network/``.

**Fail-closed:** a workspace that declares ``allowed_domains`` but has no
sidecar configured refuses to start (rather than running unrestricted) —
silently ignoring an allow-list would disable a security control the user
requested (#2254 review B2).

**Unrestricted by default:** a workspace *without* ``allowed_domains`` starts
with normal unrestricted podman networking (no sidecar, no filtering).

This module owns the pure validators (module-level, unit-testable without an
app) and the :class:`NetFilter` state object (the settings surface + host
resolver detection). The :class:`NetFilter` state object is constructed once
in :func:`build_app` and stored on ``app.state.netfilter``.
"""

from __future__ import annotations

import ipaddress
import logging
import re

logger = logging.getLogger(__name__)

# A hostname or IPv4 address with an optional trailing ``:port``.
# Deliberately permissive on the host grammar — the sidecar's proxy does the
# real DNS resolution; this just rejects gross mistakes (empty specs,
# whitespace, non-numeric ports) so a typo in the API is rejected at the
# boundary rather than failing silently inside the container netns. IPv6
# literals are **not** accepted: IPv6 is default-denied inside filtered
# workspaces (#1936), so a v6 destination is neither reachable nor
# enforceable, and the bracket grammar (``[::1]:443``) has been removed. CIDR
# ranges (``10.0.0.0/8``) are handled before this regex runs — the ``/``
# routes them to :func:`valid_cidr_spec` — so this grammar stays
# host/IPv4-only (#1935).
_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?"  # hostname / IPv4
    r"(?::[0-9]{1,5})?$"  # optional :port
)


def _valid_domain_spec(spec: str) -> bool:
    if not spec or any(ch.isspace() for ch in spec):
        return False
    # A "/" denotes an IPv4 CIDR range (e.g. ``10.0.0.0/8``, optionally
    # scoped to a port as ``10.0.0.0/8:443``). The slash cleanly
    # distinguishes a CIDR from a ``host:port`` spec, so the host grammar
    # below is unchanged (#1935).
    if "/" in spec:
        return valid_cidr_spec(spec)
    # nginx-style host scopes (#2377): a bare host is EXACT (apex only); a
    # leading ``.`` is INCLUSIVE (apex + subdomains); ``*.`` is SUBDOMAINS only.
    # Strip the sigil and validate the remaining ``host[:port]`` grammar; a bare
    # ``*``, ``*.``, or ``.`` has no matchable base (#2256).
    spec = _strip_host_sigil(spec)
    if not spec:
        return False
    if not _DOMAIN_RE.match(spec):
        return False
    return _valid_spec_port(spec)


def _strip_host_sigil(spec: str) -> str:
    """Strip an nginx-style host-scope sigil (#2377): a bare host is EXACT
    (apex only); a leading ``.`` is INCLUSIVE (apex + subdomains); ``*.`` is
    SUBDOMAINS only. A bare ``*``, ``*.``, or ``.`` has no matchable base
    (#2256) and strips to the empty string."""
    if spec.startswith("*."):
        return spec[2:]
    if spec.startswith("."):
        return spec[1:]
    return spec


def _valid_spec_port(spec: str) -> bool:
    """The regex accepts up to 5 digits; additionally reject ports
    > 65535."""
    if ":" not in spec:
        return True
    port_str = spec.rsplit(":", 1)[1]
    if port_str and port_str.isdigit() and int(port_str) > 65535:
        return False
    return True


def valid_cidr_spec(spec: str) -> bool:
    """Validate an IPv4 CIDR spec, optionally scoped to a TCP port.

    Forms: ``<ip>/<plen>`` (e.g. ``10.0.0.0/8``) or ``<ip>/<plen>:<port>``
    (e.g. ``10.0.0.0/8:443``). IPv6 CIDRs are rejected — IPv6 is default-
    denied inside filtered workspaces (#1936), so a v6 range is neither
    reachable nor enforceable, and ``IPv4Network`` raises on a v6 string
    anyway. Host bits set on the network address (e.g. ``10.5.0.0/8``) are
    accepted (``strict=False``); iptables masks them correctly regardless, so
    the spec is kept as-typed for the round-trip (no normalization) —
    consistent with how host specs are treated. Bad prefix lengths (``/33``),
    missing prefixes (``10.0.0.0/``), and non-numeric prefixes are rejected
    via the ``IPv4Network`` ``ValueError`` (#1935).
    """
    # Split off an optional :port suffix first; the port grammar matches
    # the host spec (1–65535, digits only).
    cidr = spec
    port: str | None = None
    if ":" in spec:
        cidr, port = spec.rsplit(":", 1)
        if not port or not port.isdigit() or int(port) > 65535:
            return False
    try:
        ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return False
    return True


def parse_allowed_domains(
    values: list[str], label: str = "allowed_domains"
) -> list[str]:
    """Validate + normalize a list of ``host[:port]`` or IPv4 CIDR specs.

    Strips whitespace, drops empties, and de-duplicates while preserving
    first-seen order. Raises :class:`ValueError` (prefixed with ``label``)
    listing every invalid spec so the API surfaces a precise error instead of
    a silent skip. A ``/0`` CIDR (e.g. ``0.0.0.0/0``) is *valid* but matches all of
    IPv4 — effectively disabling the filter — so it earns a loud warning
    (not a rejection) so an operator who stumbles into it can't do so
    silently. "No allowed_domains" is the documented way to run
    unrestricted (#1935).
    """
    out: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for raw in values:
        spec = raw.strip()
        if not spec:
            continue
        if not _valid_domain_spec(spec):
            invalid.append(raw)
            continue
        if spec not in seen:
            seen.add(spec)
            out.append(spec)
    if invalid:
        raise ValueError(
            f"Invalid {label} entry/entries: "
            + ", ".join(repr(s) for s in invalid)
        )
    _warn_allow_all_cidrs(out)
    return out


def _warn_allow_all_cidrs(specs: list[str]) -> None:
    """Log a loud warning for any accepted ``/0`` CIDR (#1935).

    A prefix length of 0 (``0.0.0.0/0``, or any spec normalizing to it
    like ``10.5.0.0/0`` — ``IPv4Network`` masks the host bits away)
    matches the entire IPv4 space, so the ACCEPT rule the sidecar emits is
    effectively "allow all IPv4 egress". For an anti-exfiltration control
    that is a stealthy disable-the-filter primitive: it looks like a real
    rule, draws no warning elsewhere, and an operator can reach it by
    default-route mental model. The validator accepts it (rejecting would
    surprise an operator with a legitimate, if unusual, reason); this just
    makes it visible in the logs at the API boundary and at boot/SIGHUP
    (via the settings coercion path).
    """
    for spec in specs:
        if "/" not in spec:
            continue
        # The CIDR is everything before an optional :port suffix. This
        # parse cannot raise: the spec already passed valid_cidr_spec,
        # which ran the same IPv4Network(...) and rejected on ValueError.
        cidr = spec.rsplit(":", 1)[0] if ":" in spec else spec
        if ipaddress.IPv4Network(cidr, strict=False).prefixlen == 0:
            logger.warning(
                "allowed_domains entry %r is a /0 CIDR — it matches "
                "ALL IPv4 egress, effectively disabling the filter for "
                "IPv4. If unrestricted egress is intended, an empty "
                "allowed_domains list (or KLANGKD_NETFILTER_ENABLED=false) "
                "is the documented way to run unrestricted (#1935).",
                spec,
            )


def is_ipv4(s: str) -> bool:
    """True if ``s`` is a literal IPv4 address."""
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
    except ValueError:
        return False


def nameservers(path: str) -> list[str]:
    """IPv4 ``nameserver`` IPs from a resolv.conf file (best-effort)."""
    out: list[str] = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if (
                    len(parts) >= 2
                    and parts[0] == "nameserver"
                    and is_ipv4(parts[1])
                ):
                    out.append(parts[1])
    except OSError:
        pass
    return out


def detect_host_resolvers() -> list[str]:
    """Best-effort detection of the host's upstream IPv4 DNS resolvers.

    On systemd-resolved hosts ``/etc/resolv.conf`` is the ``127.0.0.53``
    stub; the real upstreams live in
    ``/run/systemd/resolve/resolv.conf``. IPv6 nameservers are excluded
    (IPv6 egress is default-denied in the sidecar's netns). Returns ``[]``
    when no usable resolver is found (#1365). The sidecar's proxy forwards
    to one of these (picking one that differs from the REDIRECT target for
    loop-avoidance)."""
    primary = nameservers("/etc/resolv.conf")
    if any(ns == "127.0.0.53" for ns in primary):
        upstream = nameservers("/run/systemd/resolve/resolv.conf")
        if upstream:
            return list(dict.fromkeys(upstream))
    return list(
        dict.fromkeys(ns for ns in primary if not ns.startswith("127."))
    )


class NetFilter:
    """Owns the settings-dependent netfilter surface (#1365).

    The host-resolver detection + the deploy default allow-list live here as
    methods reaching config through ``self.app.state.settings``; the pure
    validators stay module-level. Constructed once in :func:`build_app` on
    ``app.state.netfilter``.
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    def default_domains(self) -> list[str]:
        """The deploy-wide default allow-list (#1365), already validated +
        de-duped at settings construction (a bad spec aborts boot).

        A workspace with no ``allowed_domains`` of its own inherits this.
        Returns a copy so callers can't mutate the cached settings list.
        """
        raw = self.app.state.settings.netfilter_default_domains
        return list(raw) if raw else []

    def enabled(self) -> bool:
        """Whether FQDN egress filtering is available on this deploy.

        Armed = the master switch (``netfilter_enabled``) is on AND the
        network sidecar image is configured. A workspace with
        ``allowed_domains`` that starts when this is False is fail-closed at
        ``start_container_inner`` (it raises rather than running
        unrestricted) — so the API warns early when an operator persists an
        allow-list on a deploy that can't enforce it (#1365).
        """
        if not self.app.state.settings.netfilter_enabled:
            return False
        return bool(self.app.state.settings.network_sidecar_image)

    def resolvers(self) -> list[str]:
        """Host upstream DNS resolvers (IPv4) the sidecar's proxy forwards to.

        Re-detected each call (cheap file read) so a SIGHUP settings reload
        or a host resolver change takes effect for the next workspace without
        per-instance caching (#1365). ``start_network_sidecar`` picks the
        first one that differs from the REDIRECT target (1.1.1.1) for
        loop-avoidance."""
        return detect_host_resolvers()
