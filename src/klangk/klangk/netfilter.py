"""Per-workspace network egress filtering via OCI ``createContainer`` hooks.

A workspace may declare ``allowed_domains`` (a list of ``host`` or
``host:port`` specs). When the deployer has enabled netfilter
(``KLANGKD_NETFILTER_HOOKS_DIR``), a workspace with allowed_domains has:

* the OCI annotation ``klangk.netfilter.rules`` set to the resolved spec
  list, and
* ``--hooks-dir`` pointed at the directory this module populates,

so the bundled OCI hook fires at ``createContainer`` time, resolves each
host to IPs, and installs iptables rules inside the container's network
namespace that allow only loopback, DNS, the backend gateway, and the
listed destinations — default-dropping everything else. The hook runs
before the container process starts, so the ruleset is in place and
immutable before any user code runs (CAP_NET_ADMIN is dropped afterwards).

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

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# OCI annotation carrying the comma-separated ``host[:port]`` spec list.
# The hook JSON's ``annotations`` filter gates firing on this key's
# presence, so a workspace without it is never filtered.
ANNOTATION_KEY = "klangk.netfilter.rules"

# Filenames written into the configured hooks dir.
HOOK_JSON_NAME = "klangk-netfilter.json"
HOOK_SCRIPT_NAME = "klangk-netfilter.sh"

# A hostname or IP (v4/v6), optionally bracketed for v6, with an optional
# trailing ``:port``. Deliberately permissive on the host grammar — the
# hook does the real DNS resolution; this just rejects gross mistakes
# (empty specs, whitespace, non-numeric ports, stray slashes) so a typo in
# the API is rejected at the boundary rather than failing silently inside
# the container netns.
_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?"  # hostname / IPv4
    r"(?::[0-9]{1,5})?$"  # optional :port
)
_DOMAIN_BRACKET_RE = re.compile(
    r"^\[[0-9A-Fa-f:.]+\](?::[0-9]{1,5})?$"  # [ipv6](:port)?
)


def _valid_domain_spec(spec: str) -> bool:
    if not spec or any(ch.isspace() for ch in spec):
        return False
    if "/" in spec:
        return False
    return bool(_DOMAIN_RE.match(spec) or _DOMAIN_BRACKET_RE.match(spec))


def parse_allowed_domains(values: list[str]) -> list[str]:
    """Validate + normalize a list of ``host[:port]`` specs.

    Strips whitespace, drops empties, and de-duplicates while preserving
    first-seen order. Raises :class:`ValueError` listing every invalid
    spec so the API surfaces a precise error instead of a silent skip.
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
    return out


def render_rules_annotation(domains: list[str]) -> str:
    """Render the comma-separated annotation value from validated domains."""
    return ",".join(domains)


def render_hook_json(script_path: str) -> str:
    """Render the OCI hook JSON pointing at the absolute ``script_path``.

    The ``annotations`` map gates the hook to fire **only** for containers
    that carry :data:`ANNOTATION_KEY` — a workspace without the annotation
    (no allowed_domains) never triggers the hook, so it stays unrestricted.
    """
    return json.dumps(
        {
            "version": "1.0.0",
            "hook": {"path": os.path.abspath(script_path)},
            "when": {"always": 1},
            "stages": ["createContainer"],
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
# installs iptables rules in the container's network namespace (via
# nsenter on the init pid from the OCI state) that allow only loopback,
# DNS, the backend gateway, and the listed destinations — default-dropping
# everything else. Runs before the container process starts, so the
# ruleset is immutable before any user code runs (CAP_NET_ADMIN is dropped
# afterwards). See issue #1365.
set -u

state=$(cat)

# Extract the annotation value + the init pid with sed (no jq dependency).
rules=$(printf '%s' "$state" \
    | sed -n 's/.*"klangk.netfilter.rules"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
pid=$(printf '%s' "$state" \
    | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')

# Nothing to filter without a rules annotation or an init pid.
[ -n "$rules" ] || exit 0
[ -n "$pid" ] || exit 0
[ -e "/proc/$pid/ns/net" ] || exit 0

# iptables inside the container's network namespace. Failures are logged
# to stderr (captured by the OCI runtime) but do not abort the hook — the
# default-DROP policy below is the fail-closed posture for a misconfigured
# deploy, and a partial ruleset is still better than none.
ipt() {
    nsenter --net="/proc/$pid/ns/net" iptables "$@" || \
        echo "klangk-netfilter: iptables $* failed" >&2
}

# Resolve a hostname to unique A/AAAA IPs, one per line.
resolve() {
    getent ahosts "$1" 2>/dev/null | awk '{print $1}' | sort -u
}

# Print one ACCEPT rule per resolved IP for a host[:port] spec. Handles
# bracketed IPv6 literals ([::1], [2001:db8::1]:443) — the brackets are
# stripped and the optional ]:port suffix parsed — as well as plain
# hostnames/IPv4 with an optional :port. A non-numeric port is skipped
# defensively (the API validator rejects these, but the hook never trusts
# the annotation blindly).
accept_rules() {
    _spec=$1
    _host=
    _port=
    case "$_spec" in
        "["*"]"*)
            # [ipv6] or [ipv6]:port — drop the brackets + parse the port.
            _host=${_spec%%]*}        # "[ipv6"  (strip ](:port) suffix)
            _host=${_host#?}          # "ipv6"   (strip leading [)
            case "$_spec" in
                *"]:"*) _port=${_spec##*:} ;;
            esac
            ;;
        *)
            # hostname / IPv4, optional :port.
            _host=${_spec%%:*}
            case "$_spec" in
                *:*) _port=${_spec##*:} ;;
            esac
            ;;
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

# DNS: allow :53 ONLY to the container's configured resolvers (read from
# its /etc/resolv.conf via /proc/$pid/root — the OCI runtime has set up the
# container's mount namespace by createContainer time), not to any
# destination. A blanket :53 ACCEPT is an exfil / DNS-tunneling channel that
# defeats an anti-exfiltration filter. KLANGK_NETFILTER_RESOLV overrides the
# path (for tests); if the file is absent/unreadable DNS is blocked and the
# gap is logged (#1365).
_resolv=${KLANGK_NETFILTER_RESOLV:-/proc/$pid/root/etc/resolv.conf}
if [ -r "$_resolv" ]; then
    while read -r _kw _ns _rest; do
        [ "$_kw" = "nameserver" ] || continue
        [ -n "$_ns" ] || continue
        ipt -A OUTPUT -p udp --dport 53 -d "$_ns" -j ACCEPT
        ipt -A OUTPUT -p tcp --dport 53 -d "$_ns" -j ACCEPT
    done < "$_resolv"
else
    echo "klangk-netfilter: cannot read $_resolv; DNS will be blocked" >&2
fi

# Backend gateway (LLM proxy, browser delegate, chat bridge). The backend
# adds host.containers.internal:host-gateway to the container, so resolve it
# from the CONTAINER's /etc/hosts — the host netns this hook runs in does not
# know the name (it is a podman-injected container-side alias), and resolving
# it via the host's getent silently yields no IP, leaving the workspace cut
# off from its own backend. KLANGK_NETFILTER_HOSTS overrides the path (tests).
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
        return self.app.state.settings.netfilter_hooks_dir

    def default_domains(self) -> list[str]:
        """The deploy-wide default allow-list (#1365), already validated +
        de-duped at settings construction (a bad spec aborts boot).

        A workspace with no ``allowed_domains`` of its own inherits this.
        Returns a copy so callers can't mutate the cached settings list.
        """
        raw = self.app.state.settings.netfilter_default_domains
        return list(raw) if raw else []

    def enabled(self) -> bool:
        """Whether netfilter is armed on this deploy (hooks dir configured)."""
        return self.hooks_dir() is not None

    def hooks_dir(self) -> str | None:
        """Return the configured hooks dir (validated to exist), else ``None``.

        ``None`` (unset, or pointing somewhere that doesn't exist / can't be
        created) means netfilter is disabled: workspaces start unrestricted
        regardless of their ``allowed_domains``.
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

    def install_hooks(self) -> str | None:
        """Materialize the hook script + JSON into the hooks dir.

        Idempotent: re-writes both files on every call (so a package
        upgrade ships the new script). Returns the dir, or ``None`` when
        netfilter is disabled. Failures are logged and the feature is left
        disabled rather than crashing startup.
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
        logger.info(
            "Netfilter egress filtering enabled: OCI hooks installed in %s",
            path,
        )
        return path

    def create_kwargs(
        self, allowed_domains: list[str] | None
    ) -> tuple[dict[str, str] | None, str | None]:
        """Build ``(annotations, hooks_dir)`` for a workspace's container.

        Resolution (#1365): a workspace's non-empty ``allowed_domains``
        **overrides** the deploy-wide default; otherwise the default applies.
        ``(None, None)`` — unrestricted — only when both are empty, or when
        netfilter is disabled (no hooks dir). When an effective list exists
        but netfilter is disabled, a loud warning is logged: the container
        starts unrestricted and the operator must enable netfilter to
        enforce the policy.
        """
        # Workspace overrides the deploy default; empty/None inherits it.
        domains = (
            list(allowed_domains)
            if allowed_domains
            else self.default_domains()
        )
        if not domains:
            return None, None
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
            return None, None
        annotation = {ANNOTATION_KEY: render_rules_annotation(domains)}
        return annotation, path
