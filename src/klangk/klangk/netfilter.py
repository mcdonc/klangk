"""Per-workspace network egress filtering via OCI ``createContainer`` hooks.

A workspace may declare ``allowed_domains`` (a list of ``host``,
``host:port``, or IPv4 CIDR specs — ``10.0.0.0/8`` / ``10.0.0.0/8:443``).
When the deployer has enabled netfilter
(``KLANGKD_NETFILTER_HOOKS_DIR``), a workspace with allowed_domains has:

* the OCI annotation ``klangk.netfilter.rules`` set to the resolved spec
  list, and
* ``--hooks-dir`` pointed at the directory this module populates,

so the bundled OCI hook fires at ``createContainer`` time, resolves each
host to IPs, and installs iptables rules inside the container's network
namespace that allow only loopback, DNS, the backend gateway, and the
listed destinations — default-dropping everything else. The hook runs
before the container process starts, so the ruleset is in place before
any user code runs; a filtered container also gets
:data:`DROPPED_CAPABILITIES` (``NET_ADMIN``) dropped so the entrypoint
cannot flush the ruleset. The ruleset is immutable **only under the
runtime's default capability set** — granting ``NET_ADMIN`` (e.g.
``--cap-add NET_ADMIN``), running ``--privileged``, or a permissive
seccomp profile lets the entrypoint ``iptables -F OUTPUT`` and exfiltrate
freely. See issue #1773.

**Backward compatible / fail-open:** a workspace without ``allowed_domains``
gets no annotation, no ``--hooks-dir``, and unrestricted networking exactly
as before. If a workspace *does* declare ``allowed_domains`` but netfilter
is not enabled (no hooks dir configured), the workspace starts
**unrestricted** and the server logs a loud warning — the deployer must
satisfy the deployment requirements (iptables available where the OCI
runtime executes) before the filter is enforced. See issue #1365.

This module owns the settings-dependent surface (the hooks-dir resolver and
the annotation builder); the pure validators/renderers are module-level so
they are unit-testable without an app. The :class:`NetFilter` state object
is constructed once in :func:`build_app` and stored on
``app.state.netfilter``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import re
import subprocess

logger = logging.getLogger(__name__)

# OCI annotation carrying the comma-separated ``host[:port]`` spec list.
# The hook JSON's ``annotations`` filter gates firing on this key's
# presence, so a workspace without it is never filtered.
ANNOTATION_KEY = "klangk.netfilter.rules"

# OCI annotation carrying the egress mode (``static`` or ``interactive``).
# When ``interactive``, the hook adds a rate-limited NFLOG rule before the
# final DROP so the consent daemon can observe blocked destinations via a
# netlink queue and prompt a human (#2239). When absent or ``static``, no
# NFLOG rule is added and the ruleset is fully immutable.
ANNOTATION_EGRESS_MODE_KEY = "klangk.netfilter.egress_mode"

# OCI annotation carrying the comma-separated DNS resolver IPs the hook
# must allow on :53. At the createContainer stage the hook executes in the
# container *network* namespace but the host *mount* namespace, so it
# cannot read the container's /etc/resolv.conf — netavark writes that only
# after the create hooks. The server detects the host's upstream resolvers,
# passes them to the container via ``--dns`` (so they are the ones actually
# used) and mirrors them here so the hook's DNS-allow rules match (#1365).
ANNOTATION_RESOLVERS_KEY = "klangk.netfilter.resolvers"

# NFLOG group number used by the interactive-mode rule. The consent daemon
# listens on this group via a netlink socket (no dmesg/journal parsing).
# Picked to avoid the default 0 and common sniffing groups.
NFLOG_GROUP = 5139

# Log prefix format for NFLOG packets. The consent daemon parses this
# prefix to correlate blocked packets with a container (and thus a
# workspace). The tag is the first 12 characters of the OCI container id
# (the authoritative id from the runtime state JSON, not the hostname).
# iptables --nflog-prefix has a 64-byte cap; "klangk-egress:" (14) + 12 +
# ":" (1) = 27 bytes — well within the limit.
LOG_PREFIX_FMT = "klangk-egress:{tag}:"
LOG_PREFIX_TAG_LEN = 12

# Filenames written into the configured hooks dir.
HOOK_JSON_NAME = "klangk-netfilter.json"
HOOK_SCRIPT_NAME = "klangk-netfilter.sh"

# Subdir under ``state_dir`` used for the hooks dir when the operator
# doesn't set ``KLANGKD_NETFILTER_HOOKS_DIR`` explicitly — so netfilter is
# armed out of the box without a second env var (#1774).
DEFAULT_HOOKS_SUBDIR = "oci-hooks"

# Linux capabilities explicitly dropped from a *filtered* workspace's
# container. ``NET_ADMIN`` lets a process flush/replace the iptables
# ruleset the hook installed, defeating the filter. It is already absent
# from podman's default capability set, so dropping it explicitly is a
# no-op under defaults and defense-in-depth against an operator who grants
# it (``--cap-add NET_ADMIN``), runs the deploy ``--privileged``, or uses a
# permissive seccomp profile. See issue #1773.
DROPPED_CAPABILITIES = ("NET_ADMIN",)

# The OCI hook search paths podman uses by default. ``--hooks-dir`` on the
# ``podman create`` command line *overrides* these (it does not append), so
# passing only klangk's hooks dir for a filtered workspace would silently
# disable every *other* createContainer hook an operator relies on
# (monitoring, secrets injection, GPU, corporate integrations). To keep
# them running, a filtered container passes klangk's dir AND these two
# standard default dirs (podman tolerates dirs that don't exist — it just
# finds no hooks in them). A non-standard hooks dir configured only via
# ``containers.conf`` is still clobbered by an explicit ``--hooks-dir``
# (documented limitation). See issue #1770.
STANDARD_HOOK_DIRS = (
    "/usr/share/containers/oci/hooks.d",
    "/etc/containers/oci/hooks.d",
)

# VM-internal paths for macOS (podman machine).  The hook JSON goes in a
# standard OCI hooks dir so podman discovers it automatically (no
# ``--hooks-dir`` needed — that flag is silently ignored in remote mode).
# The script goes alongside it.  Both directories live under ``/etc/``
# which is writable and persistent across reboots on Fedora CoreOS.
VM_HOOKS_JSON_DIR = "/etc/containers/oci/hooks.d"
VM_HOOKS_SCRIPT_DIR = "/etc/containers/hooks"

# A hostname or IPv4 address with an optional trailing ``:port``.
# Deliberately permissive on the host grammar — the hook does the real DNS
# resolution; this just rejects gross mistakes (empty specs, whitespace,
# non-numeric ports) so a typo in the API is rejected at the boundary rather
# than failing silently inside the container netns. IPv6 literals are **not**
# accepted: IPv6 is disabled inside filtered containers (#1936), so a v6
# destination is neither reachable nor enforceable, and the bracket grammar
# (``[::1]:443``) has been removed. CIDR ranges (``10.0.0.0/8``) are handled
# before this regex runs — the ``/`` routes them to :func:`_valid_cidr_spec`
# — so this grammar stays host/IPv4-only (#1935).
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
        return _valid_cidr_spec(spec)
    if not _DOMAIN_RE.match(spec):
        return False
    # The regex accepts up to 5 digits; additionally reject ports > 65535.
    if ":" in spec:
        port_str = spec.rsplit(":", 1)[1]
        if port_str and port_str.isdigit() and int(port_str) > 65535:
            return False
    return True


def _valid_cidr_spec(spec: str) -> bool:
    """Validate an IPv4 CIDR spec, optionally scoped to a TCP port.

    Forms: ``<ip>/<plen>`` (e.g. ``10.0.0.0/8``) or ``<ip>/<plen>:<port>``
    (e.g. ``10.0.0.0/8:443``). IPv6 CIDRs are rejected — IPv6 is disabled
    inside filtered containers (#1936), so a v6 range is neither reachable
    nor enforceable, and ``IPv4Network`` raises on a v6 string anyway.
    Host bits set on the network address (e.g. ``10.5.0.0/8``) are accepted
    (``strict=False``); iptables masks them correctly regardless, so the
    spec is kept as-typed for the round-trip (no normalization) — consistent
    with how host specs are treated. Bad prefix lengths (``/33``), missing
    prefixes (``10.0.0.0/``), and non-numeric prefixes are rejected via the
    ``IPv4Network`` ``ValueError`` (#1935).
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


def parse_allowed_domains(values: list[str]) -> list[str]:
    """Validate + normalize a list of ``host[:port]`` or IPv4 CIDR specs.

    Strips whitespace, drops empties, and de-duplicates while preserving
    first-seen order. Raises :class:`ValueError` listing every invalid
    spec so the API surfaces a precise error instead of a silent skip.
    A ``/0`` CIDR (e.g. ``0.0.0.0/0``) is *valid* but matches all of
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
            "Invalid allowed_domains entry/entries: "
            + ", ".join(repr(s) for s in invalid)
        )
    _warn_allow_all_cidrs(out)
    return out


def _warn_allow_all_cidrs(specs: list[str]) -> None:
    """Log a loud warning for any accepted ``/0`` CIDR (#1935).

    A prefix length of 0 (``0.0.0.0/0``, or any spec normalizing to it
    like ``10.5.0.0/0`` — ``IPv4Network`` masks the host bits away)
    matches the entire IPv4 space, so the ACCEPT rule the hook emits is
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
        # parse cannot raise: the spec already passed _valid_cidr_spec,
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


def render_rules_annotation(domains: list[str]) -> str:
    """Render the comma-separated annotation value from validated domains."""
    return ",".join(domains)


def _is_ipv4(s: str) -> bool:
    """True if ``s`` is a literal IPv4 address."""
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
    except ValueError:
        return False


def _nameservers(path: str) -> list[str]:
    """IPv4 ``nameserver`` IPs from a resolv.conf file (best-effort)."""
    out: list[str] = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if (
                    len(parts) >= 2
                    and parts[0] == "nameserver"
                    and _is_ipv4(parts[1])
                ):
                    out.append(parts[1])
    except OSError:
        pass
    return out


def _detect_host_resolvers() -> list[str]:
    """Best-effort detection of the host's upstream IPv4 DNS resolvers.

    On systemd-resolved hosts ``/etc/resolv.conf`` is the ``127.0.0.53``
    stub; the real upstreams live in
    ``/run/systemd/resolve/resolv.conf``. IPv6 nameservers are excluded
    (IPv6 egress is disabled in the container netns). Returns ``[]`` when
    no usable resolver is found (#1365)."""
    primary = _nameservers("/etc/resolv.conf")
    if any(ns == "127.0.0.53" for ns in primary):
        upstream = _nameservers("/run/systemd/resolve/resolv.conf")
        if upstream:
            return list(dict.fromkeys(upstream))
    return list(
        dict.fromkeys(ns for ns in primary if not ns.startswith("127."))
    )


def render_hook_json(
    script_path: str, *, stage: str = "createContainer"
) -> str:
    """Render the OCI hook JSON pointing at the absolute ``script_path``.

    The ``annotations`` map gates the hook to fire **only** for containers
    that carry :data:`ANNOTATION_KEY` — a workspace without the annotation
    (no allowed_domains) never triggers the hook, so it stays unrestricted.

    ``stage`` is ``createContainer`` on Linux (the historical default —
    rootful hooks receive a valid PID) and ``createRuntime`` on macOS
    (podman machine's rootless API delivers ``pid: 0`` at
    ``createContainer`` time, but a real PID at ``createRuntime``).
    """
    return json.dumps(
        {
            "version": "1.0.0",
            "hook": {"path": os.path.abspath(script_path)},
            "when": {"always": True},
            "stages": [stage],
            "annotations": {ANNOTATION_KEY: ".*"},
        },
        indent=2,
    )


# The OCI hook script. POSIX sh (no bashisms): it may run under a minimal
# /bin/sh in the runtime namespace. Reads the container state JSON from
# stdin, resolves the annotation's hosts to IPs, and installs a
# default-deny egress ruleset in the container netns via nsenter. Kept as
# the single source of truth so :func:`NetFilter.install_hooks` can
# materialize it at runtime without a packaging/data-file dependency.
HOOK_SCRIPT = r"""#!/bin/sh
# klangk OCI createContainer hook — per-workspace egress filtering.
#
# Fires only for containers that carry the `klangk.netfilter.rules`
# annotation (the hook JSON's `annotations` filter gates this). Reads the
# host[:port] specs from that annotation, resolves each host to IPs, and
# installs iptables rules that allow only loopback, DNS, the backend
# gateway, and the listed destinations — default-dropping everything else.
#
# createContainer is the one OCI stage with all three properties needed
# for a host-enforced, container-immutable filter: it runs BEFORE NET_ADMIN
# is dropped (so the hook can set rules the started container — which has
# NET_ADMIN dropped — cannot undo), the hook path resolves on the HOST so
# a tampered in-container iptables cannot subvert it, and it executes IN
# the container network namespace so iptables targets the right netns —
# with NO nsenter (we are already in it). The hook runs in the host mount
# namespace, so it cannot read the container's /etc/resolv.conf (netavark
# writes it only after the create hooks); DNS resolvers come from the
# `klangk.netfilter.resolvers` annotation instead (#1365). See #1773.
set -u

state=$(cat)

# Extract the annotation values + the init pid with sed (no jq dependency).
rules=$(printf '%s' "$state" \
    | sed -n 's/.*"klangk.netfilter.rules"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
egress_mode=$(printf '%s' "$state" \
    | sed -n 's/.*"klangk.netfilter.egress_mode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
resolvers=$(printf '%s' "$state" \
    | sed -n 's/.*"klangk.netfilter.resolvers"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
pid=$(printf '%s' "$state" \
    | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
# The authoritative container id from the OCI runtime state JSON — used as
# the NFLOG prefix tag so the consent daemon can correlate blocked packets
# with a workspace. Truncated to 12 chars (podman short-id convention).
_cid=$(printf '%s' "$state" \
    | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    | cut -c1-12)

# Nothing to filter without a rules annotation. (pid is only needed for
# the backend-gateway /etc/hosts read below; the netns need not be
# nsenter'd — at the createContainer stage the OCI runtime executes this
# hook INSIDE the container's network namespace already, so iptables /
# ip6tables / sysctl apply to it directly. The hook's binaries resolve on
# the host (createContainer hooks' paths resolve in the runtime namespace),
# so a tampered in-container iptables cannot subvert the filter.)
[ -n "$rules" ] || exit 0

# iptables / ip6tables act on the container's network namespace directly
# (we are already in it). Failures are logged to stderr (captured by the
# OCI runtime) but do not abort the hook — the default-DROP policy below
# is the fail-closed posture for a misconfigured deploy, and a partial
# ruleset is still better than none.
ipt() {
    iptables "$@" || echo "klangk-netfilter: iptables $* failed" >&2
}
ipt6() {
    ip6tables "$@" || echo "klangk-netfilter: ip6tables $* failed" >&2
}

# Resolve a hostname to unique IPv4 A records, one per line. AAAA (IPv6)
# records are filtered out: IPv6 is disabled in the container netns (#1936),
# so a v6 address is neither reachable nor installable in iptables (which
# is v4-only and would reject `-d <v6>`, logging noise to stderr).
resolve() {
    getent ahosts "$1" 2>/dev/null \
        | awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $1}' \
        | sort -u
}

# Print one ACCEPT rule per resolved IPv4 for a host[:port] spec, or a
# single -d <cidr> rule for a CIDR spec. A non-numeric port is skipped
# defensively — the API validator rejects these, and the hook re-checks
# the port (not the host/CIDR shape, which the validator already guards)
# as a cheap guard against a corrupted annotation. Bracketed IPv6
# literals are no longer accepted — IPv6 is disabled in the container
# netns, so the v6 grammar has been removed (#1936).
accept_rules() {
    _spec=$1
    # CIDR range (e.g. 10.0.0.0/8 or 10.0.0.0/8:443): emit -d <ip>/<plen>
    # directly with NO DNS/getent resolution. A CIDR isn't a hostname —
    # `getent ahosts "10.0.0.0/8"` returns nothing, so routing it through
    # resolve() would silently drop the rule (the workspace would appear
    # filtered while the whole subnet was blocked) (#1935). The API
    # validator guarantees a valid IPv4 CIDR + optional port; the hook
    # still guards against a non-numeric port defensively.
    case "$_spec" in
        */*)
            _cidr=${_spec%%:*}
            _port=
            case "$_spec" in
                *:*) _port=${_spec##*:} ;;
            esac
            if [ -n "$_port" ]; then
                case "$_port" in
                    *[!0-9]*) return 0 ;;
                esac
                printf '%s\n' "-d $_cidr -p tcp --dport $_port -j ACCEPT"
            else
                printf '%s\n' "-d $_cidr -j ACCEPT"
            fi
            return
            ;;
    esac
    # hostname / IPv4, optional :port.
    _host=${_spec%%:*}
    _port=
    case "$_spec" in
        *:*) _port=${_spec##*:} ;;
    esac
    [ -n "$_host" ] || return 0
    # Defensive: skip a non-numeric port rather than emit a bad rule.
    if [ -n "$_port" ]; then
        case "$_port" in
            *[!0-9]*) return 0 ;;
        esac
    fi
    for _ip in $(resolve "$_host"); do
        if [ -n "$_port" ]; then
            printf '%s\n' "-d $_ip -p tcp --dport $_port -j ACCEPT"
        else
            printf '%s\n' "-d $_ip -j ACCEPT"
        fi
    done
}

# Default-deny egress; allow loopback + established first.
ipt -P OUTPUT DROP
ipt -A OUTPUT -o lo -j ACCEPT
ipt -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# IPv6 egress is default-denied via ip6tables — klangk does not support
# IPv6 egress (#1936); without this an IPv6 path would bypass the v4
# allow-list (nearly every host publishes a AAAA record). (The hook formerly
# also ran `sysctl net.ipv6.conf.*.disable_ipv6=1`, but that fires at
# createContainer, before pasta configures the netns, and the disabled knob
# makes pasta's IPv6 address setup fail with EPERM — so the sysctl is dropped
# and ip6tables DROP alone carries the v6 default-deny. ip6tables is verified
# at startup; a deploy without it gets a warning and no v6 default-deny.)
ipt6 -P OUTPUT DROP

# DNS: allow :53 ONLY to the container's configured resolvers — NOT to any
# destination (a blanket :53 ACCEPT is an exfil / DNS-tunneling channel that
# defeats an anti-exfiltration filter). At the createContainer stage the
# hook runs in the host mount namespace, so it cannot read the container's
# own /etc/resolv.conf (netavark writes it only after the create hooks).
# The server therefore detects the host's upstream resolvers, passes them
# to the container via --dns, and mirrors the same IPs in the
# `klangk.netfilter.resolvers` annotation so the allow-rules match what the
# container actually queries (#1365).
if [ -n "$resolvers" ]; then
    _save_ifs=$IFS; IFS=,
    set -- $resolvers
    IFS=$_save_ifs
    for _ns in "$@"; do
        [ -n "$_ns" ] || continue
        ipt -A OUTPUT -p udp --dport 53 -d "$_ns" -j ACCEPT
        ipt -A OUTPUT -p tcp --dport 53 -d "$_ns" -j ACCEPT
    done
else
    echo "klangk-netfilter: no resolvers annotation; DNS will be blocked" >&2
fi

# Backend gateway (LLM proxy, browser delegate, chat bridge). The backend
# adds host.containers.internal:host-gateway to the container, so resolve it
# from the CONTAINER's /etc/hosts — the host netns this hook runs in does not
# know the name (it is a podman-injected container-side alias), and resolving
# it via the host's getent silently yields no IP, leaving the workspace cut
# off from its own backend. KLANGK_NETFILTER_HOSTS overrides the path (tests).
#
# NOTE: at the createContainer stage podman delivers pid=0 in the OCI state
# (the container process isn't started yet), so /proc/$pid/root/etc/hosts is
# unreadable on a real deploy — this read succeeds only under the test
# harness (KLANGK_NETFILTER_HOSTS). Allowing the gateway IP therefore needs
# it mirrored via an annotation (like the DNS resolvers) — a #1365 follow-up.
# Until then a filtered workspace must reach its backend via an allowed
# domain/IP rather than host.containers.internal.
_hosts=${KLANGK_NETFILTER_HOSTS:-/proc/$pid/root/etc/hosts}
if [ -r "$_hosts" ]; then
    while read -r _gip _grest; do
        # Skip comment/blank lines.
        case "$_gip" in \#*|"") continue ;; esac
        case " $_grest " in
            *" host.containers.internal "*)
                [ -n "$_gip" ] && ipt -A OUTPUT -d "$_gip" -j ACCEPT
                ;;
        esac
    done < "$_hosts"
fi

# Per-workspace allowed destinations. Split the comma-separated rules under
# IFS=',', then RESTORE IFS before the loop body so that (a) accept_rules'
# command substitutions split getent's newline-separated output into IPs,
# and (b) the unquoted $_rule below word-splits into separate iptables argv
# entries. Without the restore, every ACCEPT rule collapsed into one blob
# argument that iptables rejected, and multi-IP hosts collapsed into one
# garbage IP — silently, since ipt()'s failures are only logged (#1365).
_save_ifs=$IFS
IFS=','
set -- $rules
IFS=$_save_ifs
for _spec in "$@"; do
    [ -n "$_spec" ] || continue
    accept_rules "$_spec" | while IFS= read -r _rule; do
        [ -n "$_rule" ] || continue
        # $_rule is intentionally unquoted: each line is a series of
        # iptables flags that must word-split into separate arguments.
        ipt -A OUTPUT $_rule
    done
done

# Interactive egress consent mode (#2239). When egress_mode is
# "interactive", add a rate-limited NFLOG rule so blocked destinations
# are delivered to the consent daemon via a netlink socket (group 5139).
# NFLOG is the purpose-built iptables-to-userspace channel: it avoids
# kernel ring-buffer noise (dmesg/journal), supports group multiplexing,
# and the daemon needs only a netlink socket — no privileged journal
# access or prefix-regex parsing.
#
# The NFLOG prefix uses the first 12 chars of the OCI container id
# (extracted from the runtime state JSON above, same as podman's short
# id) for workspace correlation. klangk's container registry already
# knows each container's ID, so the consent daemon maps
# nflog-prefix → container_id → workspace_id.
# Format: "klangk-egress:<12-char-id>:" (27 bytes, well within the
# 64-byte --nflog-prefix cap).
#
# The rate limit (5/sec, burst 20) caps NFLOG flooding from adversarial
# containers — the DROP still fires for every packet, only the NFLOG
# notification is throttled. An explicit final DROP follows (redundant
# given the OUTPUT policy, but makes the chain self-documenting and
# ensures NFLOG precedes DROP regardless of future chain additions).
# In static mode (the default), no NFLOG rule is added — the ruleset
# is fully immutable and identical to the pre-#2239 behavior.
if [ "$egress_mode" = "interactive" ]; then
    _tag=${_cid:-unknown}
    ipt -A OUTPUT -m limit --limit 5/sec --limit-burst 20 \
        -j NFLOG --nflog-group 5139 --nflog-prefix "klangk-egress:${_tag}:"
    ipt -A OUTPUT -j DROP
fi

exit 0
"""


class NetFilter:
    """Owns the settings-dependent netfilter surface (#1365).

    The hooks-dir resolver and the annotation/``--hooks-dir`` builder live
    here as methods reaching config through ``self.app.state.settings``;
    the pure validators/renderers stay module-level. Constructed once in
    :func:`build_app` on ``app.state.netfilter``.
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def _raw_hooks_dir(self) -> str | None:
        # #1774: netfilter_enabled is the master switch; when False the
        # feature is fully off (no dir, no install, enabled() False).
        if not self.app.state.settings.netfilter_enabled:
            return None
        raw = self.app.state.settings.netfilter_hooks_dir
        if raw:
            return raw
        # Default to a state_dir subdir so netfilter is armed out of the
        # box without a second env var (#1774). state_dir is always resolved
        # at construction (_require_dirs fails fast otherwise, #1461).
        return os.path.join(
            self.app.state.settings.state_dir, DEFAULT_HOOKS_SUBDIR
        )

    def default_domains(self) -> list[str]:
        """The deploy-wide default allow-list (#1365), already validated +
        de-duped at settings construction (a bad spec aborts boot).

        A workspace with no ``allowed_domains`` of its own inherits this.
        Returns a copy so callers can't mutate the cached settings list.
        """
        raw = self.app.state.settings.netfilter_default_domains
        return list(raw) if raw else []

    def enabled(self) -> bool:
        """Whether netfilter is armed on this deploy.

        Armed = the hooks dir is configured AND the OCI hook script + JSON
        are installed and current. A configured-but-not-installed dir (a
        partial ``install_hooks()`` failure, or a stale hook from an old
        klangk version) is NOT armed — callers fail open with a loud
        warning rather than appearing filtered while running unrestricted
        (#1771).
        """
        path = self.hooks_dir()
        return path is not None and self._hook_files_current(path)

    def hooks_dir(self) -> str | None:
        """Return the effective hooks dir (validated to exist), else ``None``.

        With netfilter enabled (the default), an unset ``netfilter_hooks_dir``
        resolves to ``<state_dir>/oci-hooks`` (#1774). ``None`` — netfilter
        disabled via the master switch, the dir unset with no ``state_dir``,
        or pointing somewhere that can't be created — means workspaces start
        unrestricted regardless of their ``allowed_domains``. This is the
        *configured* dir; :meth:`enabled` additionally requires the hook to
        be installed + current (#1771).
        """
        raw = self._raw_hooks_dir
        if not raw:
            return None
        path = os.path.realpath(raw)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            logger.error(
                "KLANGKD_NETFILTER_HOOKS_DIR=%s cannot be created (%s); "
                "per-workspace egress filtering is disabled",
                raw,
                exc,
            )
            return None
        return path

    def _hook_files_current(self, path: str) -> bool:
        """True iff the hook script + JSON in *path* are present and match
        the in-tree content.

        Detects a partial ``install_hooks()`` (one file written, the other
        not), a missing hook, and a stale hook left over from an old klangk
        version. Stateless — the filesystem is the source of truth, so this
        survives restarts and doesn't depend on ``install_hooks()`` having
        run in this process (#1771).
        """
        script_path = os.path.join(path, HOOK_SCRIPT_NAME)
        json_path = os.path.join(path, HOOK_JSON_NAME)
        try:
            with open(script_path) as f:
                if f.read() != HOOK_SCRIPT:
                    return False
            with open(json_path) as f:
                if f.read() != render_hook_json(script_path):
                    return False
        except OSError:
            return False
        return True

    def install_hooks(self) -> str | None:
        """Materialize the hook script + JSON into the hooks dir.

        Idempotent: re-writes both files on every call (so a package
        upgrade ships the new script). Returns the dir, or ``None`` when
        netfilter is disabled. Failures are logged and the feature is left
        disabled rather than crashing startup.

        On macOS the local copy is still written (so :meth:`enabled` /
        :meth:`_hook_files_current` can validate without SSH), and the
        hooks are additionally copied into the podman machine VM where
        the OCI runtime can actually find them.
        """
        path = self.hooks_dir()
        if path is None:
            return None
        script_path = os.path.join(path, HOOK_SCRIPT_NAME)
        json_path = os.path.join(path, HOOK_JSON_NAME)
        try:
            with open(script_path, "w") as f:
                f.write(HOOK_SCRIPT)
            os.chmod(script_path, 0o755)
            with open(json_path, "w") as f:
                f.write(render_hook_json(script_path))
        except OSError as exc:
            logger.error(
                "Could not install netfilter hooks into %s: %s "
                "(per-workspace egress filtering is disabled)",
                path,
                exc,
            )
            return None
        if platform.system() == "Darwin":
            if not self._install_hooks_in_vm():
                return None
        logger.info(
            "Netfilter egress filtering enabled: OCI hooks installed in %s",
            path,
        )
        return path

    def _install_hooks_in_vm(self) -> bool:
        """Copy hook files into the podman machine VM (macOS only).

        On macOS, podman runs in remote mode — the OCI runtime is inside
        a CoreOS VM and cannot see host filesystem paths.  Additionally,
        ``--hooks-dir`` is silently ignored by the remote client, so the
        hook JSON must be placed in a hooks dir that podman discovers.

        Rootless podman does **not** check any hooks directory by default
        (``oci-hooks(5)``), so the installer also writes a
        ``containers.conf`` drop-in that adds the hooks dir to the
        rootless user's config.  Both hooks and config live under
        ``/etc/`` which is writable and persistent on Fedora CoreOS.

        A single ``podman machine ssh`` call runs a shell script that
        creates directories and writes all files, avoiding multiple
        round-trips.
        """
        vm_script = f"{VM_HOOKS_SCRIPT_DIR}/{HOOK_SCRIPT_NAME}"
        vm_json = f"{VM_HOOKS_JSON_DIR}/{HOOK_JSON_NAME}"
        vm_hook_json = render_hook_json(vm_script, stage="createRuntime")

        # Build an installer script piped through stdin to a single SSH
        # call.  The heredoc delimiters are quoted (no shell expansion)
        # and unique enough to avoid collisions with file contents.
        installer = (
            "set -e\n"
            f"mkdir -p {VM_HOOKS_SCRIPT_DIR} {VM_HOOKS_JSON_DIR}\n"
            f"cat > {vm_script} << 'KLANGK_SCRIPT_EOF'\n"
            f"{HOOK_SCRIPT}"  # ends with \n
            "KLANGK_SCRIPT_EOF\n"
            f"chmod 755 {vm_script}\n"
            f"cat > {vm_json} << 'KLANGK_JSON_EOF'\n"
            f"{vm_hook_json}\n"
            "KLANGK_JSON_EOF\n"
            # Rootless podman has NO default hooks dir (oci-hooks(5)):
            # without a containers.conf entry the hook is never found.
            # Write a system-wide drop-in so every user (including the
            # rootless ``core`` user that podman machine runs as) picks
            # it up.
            "mkdir -p /etc/containers/containers.conf.d\n"
            "cat > /etc/containers/containers.conf.d/klangk-hooks.conf"
            " << 'KLANGK_CONF_EOF'\n"
            "[engine]\n"
            f'hooks_dir = ["{VM_HOOKS_JSON_DIR}"]\n'
            "KLANGK_CONF_EOF\n"
        )

        podman = self.app.state.settings.podman_bin or "podman"
        try:
            result = subprocess.run(
                [podman, "machine", "ssh", "sudo", "sh", "-s"],
                input=installer,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.error(
                    "Could not install netfilter hooks into podman machine "
                    "VM (exit %d): %s "
                    "(per-workspace egress filtering is disabled on macOS)",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error(
                "Could not install netfilter hooks into podman machine "
                "VM: %s (per-workspace egress filtering is disabled on "
                "macOS)",
                exc,
            )
            return False
        logger.info(
            "Netfilter hooks installed in podman machine VM: %s and %s",
            vm_script,
            vm_json,
        )
        return True

    def resolvers(self) -> list[str]:
        """Host upstream DNS resolvers (IPv4) to allow on :53 and pass to the
        container via ``--dns``. Re-detected each call (cheap file read) so a
        SIGHUP settings reload or a host resolver change takes effect for
        the next workspace without per-instance caching (#1365)."""
        return _detect_host_resolvers()

    def create_kwargs(
        self,
        allowed_domains: list[str] | None,
        egress_mode: str = "static",
    ) -> tuple[
        dict[str, str] | None,
        list[str] | None,
        list[str] | None,
        list[str] | None,
    ]:
        """Build ``(annotations, hooks_dirs, cap_drop, dns)`` for a container.

        Resolution (#1365): a workspace's non-empty ``allowed_domains``
        **overrides** the deploy-wide default; otherwise the default applies.
        ``(None, None, None)`` — unrestricted — only when both are empty, or
        when netfilter is disabled (no hooks dir). When an effective list
        exists but netfilter is disabled, a loud warning is logged: the
        container starts unrestricted and the operator must enable netfilter
        to enforce the policy.

        ``hooks_dirs`` is klangk's hooks dir followed by
        :data:`STANDARD_HOOK_DIRS`: ``--hooks-dir`` overrides (does not
        append) podman's default hook search paths, so the standard dirs are
        repeated explicitly to keep operator createContainer hooks running
        for a filtered workspace (#1770).

        ``cap_drop`` is :data:`DROPPED_CAPABILITIES` (``NET_ADMIN``) for a
        filtered container, so the entrypoint cannot flush the iptables
        ruleset (#1773).

        When ``egress_mode`` is ``"interactive"`` (#2239), the
        :data:`ANNOTATION_EGRESS_MODE_KEY` annotation is added so the OCI
        hook installs a rate-limited NFLOG rule (group
        :data:`NFLOG_GROUP`) before the final DROP, enabling the consent
        daemon to observe blocked destinations via a netlink socket."""
        _valid = ("static", "interactive")
        if egress_mode not in _valid:
            raise ValueError(
                f"egress_mode must be one of {_valid!r}, got {egress_mode!r}"
            )
        # Workspace overrides the deploy default; empty/None inherits it.
        domains = (
            list(allowed_domains)
            if allowed_domains
            else self.default_domains()
        )
        if not domains:
            return None, None, None, None
        path = self.hooks_dir()
        if path is None:
            logger.warning(
                "Effective allowed_domains=%s but netfilter is "
                "disabled (KLANGKD_NETFILTER_HOOKS_DIR is unset or "
                "unwritable); the workspace will start with UNRESTRICTED "
                "egress. Configure KLANGKD_NETFILTER_HOOKS_DIR and ensure "
                "iptables is available where the OCI runtime executes to "
                "enforce the filter (#1365).",
                domains,
            )
            return None, None, None, None
        if not self._hook_files_current(path):
            # Configured but the OCI hook isn't installed / current: a
            # partial install_hooks() failure or a stale hook from an old
            # version. Do NOT hand podman this dir — the runtime would run
            # no hook and the workspace would appear filtered while running
            # unrestricted. Fail open with a loud warning instead (#1771).
            logger.warning(
                "KLANGKD_NETFILTER_HOOKS_DIR=%s is configured but the OCI "
                "hook is not installed or is stale (%s / %s missing or out "
                "of date); the workspace will start with UNRESTRICTED "
                "egress. Restart the server (install_hooks() runs at "
                "startup) or confirm the dir is writable (#1771).",
                path,
                HOOK_SCRIPT_NAME,
                HOOK_JSON_NAME,
            )
            return None, None, None, None
        annotation = {ANNOTATION_KEY: render_rules_annotation(domains)}
        if egress_mode == "interactive":
            annotation[ANNOTATION_EGRESS_MODE_KEY] = egress_mode
        # DNS resolvers the hook must allow on :53. The hook runs in the
        # host mount ns at createContainer and cannot read the container's
        # resolv.conf (netavark writes it later), so the same IPs are
        # mirrored here AND returned as ``dns`` for ``--dns`` — making the
        # container's actual resolvers match what the hook allows (#1365).
        _resolvers = self.resolvers()
        if _resolvers:
            annotation[ANNOTATION_RESOLVERS_KEY] = ",".join(_resolvers)
        # macOS: ``--hooks-dir`` is silently ignored in remote mode.
        # Hooks are in the standard dir inside the VM (installed by
        # ``_install_hooks_in_vm``), discovered automatically by podman.
        hooks_dirs = (
            None
            if platform.system() == "Darwin"
            else [path, *STANDARD_HOOK_DIRS]
        )
        return (annotation, hooks_dirs, list(DROPPED_CAPABILITIES), _resolvers)
