#!/usr/bin/env python3
"""FQDN egress DNS proxy — runs in the network sidecar (#2250, #2253).

The sidecar shares the workspace's network namespace (the workspace runs
``--network container:<this sidecar>``). The sidecar's ``entrypoint.sh`` installs
a nat REDIRECT of the workspace's configured DNS resolvers (:53) to this proxy's
listen port; this proxy applies an FQDN allow-list, forwards allowed queries to a
*different* upstream (so the REDIRECT does not loop), learns the A-record IPs from
the responses, and inserts ``iptables -I OUTPUT 1 -d <ip> [-p tcp --dport <p>] -j
ACCEPT`` for each so the workspace can reach exactly the IPs it resolved — solving
DNS round-robin. Denied names get NXDOMAIN.

DNS wire parsing is delegated to **dnspython** (rather than hand-rolled byte
slicing) so EDNS, CNAME chains, TCP-sized responses, and malformed packets are
handled correctly — a parser bug in a security component is dangerous, and a
maintained library removes that risk.

Allow-list semantics (#2256):

- **Per-domain port scoping**: ``github.com:443`` allows only ``:443`` to
  github's learned IPs; ``github.com`` (no port) allows all ports. The port
  is taken from the spec that matched the queried name.
- **Wildcards**: ``*.pypi.org`` matches subdomains of ``pypi.org`` only (NOT
  the apex ``pypi.org`` itself); a bare ``pypi.org`` matches the apex + all
  subdomains. A single learned IP inherits the union of ports from every
  matching spec, and any port-less matching spec means all-ports.
- **Learned-IP TTL/cleanup**: each learned IP is allowed only for the TTL of
  the DNS response that resolved it; a background sweeper removes the ACCEPT
  rule once the TTL elapses so stale IPs do not linger.

Interactive consent hold (#2311 half B): when a consent endpoint is configured,
the proxy is an **asyncio** program that keeps one persistent WebSocket to
klangkd's ``/ws/egress-sidecar`` and, for each *denied* destination, sends an
``{type:egress, id, dst, dport}`` frame and *suspends* the query pending the
matching ``{type:verdict, id, decision}`` verdict instead of NXDOMAIN-ing at
once. The coordinator (klangkd) gate-checks: a registered decider -> the request
is held; otherwise it returns ``deny`` fast (the proxy NXDOMAIN/DROPs as before
-- no behavior change for static workspaces). An ``allow`` verdict lets the held
query proceed (DNS: resolve upstream + learn the IPs; NFQUEUE: ``accept`` the
packet, and conntrack's ``ESTABLISHED,RELATED`` rule passes the rest of the
connection). **Fail-close**: a down WebSocket or a timed-out verdict resolves to
``deny`` immediately, so the workspace stays locked down when klangkd or the
decider is unreachable (today's static behavior) — no new latency, no hang.

A non-``once`` verdict is remembered **by host** (not just by the IP resolved
at decision time), so a retry to a CDN-rotated IP is covered for the verdict's
lifetime without re-prompting. An ``allow`` is host-allow-listed
(:data:`_SESSION_HOST_ALLOWS`, #2372/#2434); a ``deny`` is host-deny-listed
(:data:`_SESSION_HOST_DENIES`, #2446) so the user isn't re-asked for a host they
already denied (the CARRYOVER-SURPRISE). ``once`` is per-connection (a reconnect
re-prompts); an in-effect allow overrides an in-effect deny at the gate.

Configuration (env):
  KLANGKNETWORK_EGRESS_ALLOW       comma-separated allow-list: ``host[:port]``,
                            ``*.domain[:port]``, or CIDR specs. CIDR specs are
                            applied statically by the entrypoint; this proxy
                            matches only the host/wildcard specs.
  KLANGKNETWORK_EGRESS_UPSTREAM    the real upstream resolver the proxy forwards to
                            (default ``8.8.8.8``). MUST differ from the
                            workspace's configured (redirected) resolvers or the
                            proxy's forwards loop back into itself.
  KLANGKNETWORK_EGRESS_LISTEN_PORT UDP port to listen on (default ``15353``).
  KLANGKNETWORK_IPTABLES    iptables binary (default ``iptables``).
  KLANGKNETWORK_EGRESS_DEBUG       if set, log each allow/deny decision.
  KLANGKNETWORK_EGRESS_MARK  fwmark for the proxy's upstream socket (default 75;
                            must match entrypoint.sh).
  KLANGKNETWORK_EGRESS_SWEEP_INTERVAL  seconds between TTL-expiry sweeps (default 5).
  KLANGKNETWORK_EGRESS_MIN_TTL  floor for a learned IP's lifetime so a 0-TTL
                            response does not immediately yank the rule the
                            workspace needs to reach the IP it just resolved
                            (default 30).
  KLANGKNETWORK_EGRESS_CONSENT_URL  klangkd consent endpoint (HTTP). When set,
                            the proxy opens the egress-sidecar WS + gates
                            non-allow-listed egress at the connection SYN
                            (NFQUEUE) pending a verdict. Empty -> static
                            NXDOMAIN/DROP (consent disabled).
  KLANGKNETWORK_EGRESS_HOLD_TIMEOUT  seconds to await a verdict before
                            fail-closing to deny (default 120). The gate is the
                            connection SYN, so this can match the kernel's
                            connect timeout (tcp_syn_retries ~= 127s), not a
                            DNS resolver's <=30s getaddrinfo cap (#2324).
  KLANGKNETWORK_EGRESS_VERDICT_CACHE_TTL  seconds to reuse a SYN verdict for an
                            (ip, port) flow so the kernel's SYN retransmits
                            (tcp_syn_retries) don't each re-prompt (default 120).
  KLANGKNETWORK_EGRESS_REJECT_TTL   seconds a deny keeps its REJECT (tcp-reset)
                            rule so the denied connection fails fast
                            (ECONNREFUSED) instead of waiting for tcp_syn_retries
                            (~127s) (default 10).

Limitations: transport is UDP only (TCP fallback is a future addition).
"""

import asyncio
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import threading
import time
import uuid

import dns.message
import dns.rcode
import dns.rdatatype

# The `websockets` client logs the full HTTP request line (incl. any ?token=
# query param) at DEBUG; cap it at WARNING so a workspace JWT can't leak to
# sidecar stdout/logs even if debug logging is enabled elsewhere (#2309).
logging.getLogger("websockets").setLevel(logging.WARNING)

UPSTREAM = (os.environ.get("KLANGKNETWORK_EGRESS_UPSTREAM", "8.8.8.8"), 53)
LISTEN_PORT = int(os.environ.get("KLANGKNETWORK_EGRESS_LISTEN_PORT", "15353"))
IPT = os.environ.get("KLANGKNETWORK_IPTABLES", "iptables")
DEBUG = bool(os.environ.get("KLANGKNETWORK_EGRESS_DEBUG"))
# fwmark the proxy stamps on its upstream socket so the sidecar's nat/filter
# rules (a) exempt the proxy's forwards from the :53 REDIRECT (loop-avoidance)
# and (b) allow only marked packets to reach the upstream. The workspace lacks
# CAP_NET_RAW/NET_ADMIN so it cannot mark — its :53 traffic is redirected here
# and allow-listed, closing the direct-to-upstream exfil bypass (#2264). Must
# match entrypoint.sh's KLANGKNETWORK_EGRESS_MARK.
MARK = int(os.environ.get("KLANGKNETWORK_EGRESS_MARK", "75"))
# Learned-IP housekeeping (#2256).
SWEEP_INTERVAL = float(os.environ.get("KLANGKNETWORK_EGRESS_SWEEP_INTERVAL", "5"))
MIN_TTL = float(os.environ.get("KLANGKNETWORK_EGRESS_MIN_TTL", "30"))
# --- interactive consent hold (#2311 half B): when a consent endpoint is set,
# the proxy holds denied egress pending a verdict over the egress-sidecar WS.
QUEUE_NUM = int(os.environ.get("KLANGKNETWORK_EGRESS_NFQUEUE_NUM", "5139"))
CONSENT_URL = os.environ.get("KLANGKNETWORK_EGRESS_CONSENT_URL", "")
# How long to await a verdict before fail-closing to deny. The consent gate is
# the connection SYN (NFQUEUE), so this can match the kernel's connect timeout
# (tcp_syn_retries ~= 127s) -- far longer than a DNS resolver's <=30s getaddrinfo
# cap (#2324). Should be >= klangkd's consent hold timeout so the sidecar is
# still waiting when the coordinator expires the hold (and returns deny/expired).
HOLD_TIMEOUT = float(os.environ.get("KLANGKNETWORK_EGRESS_HOLD_TIMEOUT", "120"))
# How long to reuse a SYN verdict for a (ip, port) flow. The kernel retransmits
# a held SYN (tcp_syn_retries); without reuse each retransmit would re-prompt.
# After an allow the IP is also learned (ACCEPT), so new SYNs stop hitting
# NFQUEUE -- this only covers retransmits that queued during the hold.
VERDICT_CACHE_TTL = float(
    os.environ.get("KLANGKNETWORK_EGRESS_VERDICT_CACHE_TTL", "120")
)
# How long a deny keeps its REJECT (tcp-reset) rule so the denied connection
# fails fast (ECONNREFUSED) instead of waiting for tcp_syn_retries (~127s).
# Only needs to catch the SYN retransmit (~1 RTO); the verdict cache separately
# keeps the deny from re-prompting for VERDICT_CACHE_TTL.
CONSENT_REJECT_TTL = float(os.environ.get("KLANGKNETWORK_EGRESS_REJECT_TTL", "10"))
# Opt-in per-RST debug logging (#2464): the forged eager-deny RST is the
# primary fast-refuse, so when a denied connection instead *times out* this
# logs each forged RST (socket open? sendto ok? the 4-tuple) to the sidecar's
# stdout. Off by default -- a denied connection's SYN retransmits each hit the
# cached deny and re-forge an RST, which would spam a production sidecar's log.
# The egress smoketest enables it (KLANGKNETWORK_EGRESS_DEBUG_RST=1, forwarded
# by ContainerManager._start_network_sidecar) and captures the sidecar's podman
# log so a fast-refuse miss is diagnosable after the run.
_RST_DEBUG = os.environ.get("KLANGKNETWORK_EGRESS_DEBUG_RST", "") == "1"


def _rst_debug(msg: str) -> None:
    """Emit a forged-RST diagnostic line when ``KLANGKNETWORK_EGRESS_DEBUG_RST``
    is on (#2464).

    Centralized so the egress-smoketest diagnostic is one branch to cover, not
    one per call site in :func:`_send_rst`. The smoketest enables the flag and
    captures the sidecar's podman log so a fast-refuse miss (a denied
    connection timing out instead of refusing fast) shows whether each RST
    fired.
    """
    if _RST_DEBUG:
        print(msg, flush=True)


# Duration token -> seconds the sidecar honors a verdict (#2328): an allow
# learns the IP for T; a deny REJECTs for T. `once` = this connection only (no
# learn; a short deny). `restart` = the container's lifetime (the sidecar's
# in-memory rules); `forever` = the workspace's lifetime -- at the sidecar level
# both map to a long in-memory TTL, but `forever`'s real distinction is that
# klangkd persists it across sidecar restarts: an allow is appended to the
# workspace's `allowed_domains`, which this sidecar re-reads on start
# (#2368), so the allow survives a container restart. (That cross-restart
# persistence is #2368's `forever`-allow sub-piece; the deny counterpart is
# #2369.)
_DURATION_SECONDS = {
    "5s": 5,  # test-only (#2363, subsumed by #2392); honored but never UI-offered
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
    "1w": 604800,
}
_DURATION_FOREVER = 365 * 86400  # ~a year; practically until restart


def _duration_ttl(duration: str) -> float | None:
    """Seconds for a timed/tilrestart/forever duration, or None for ``once``."""
    if duration in _DURATION_SECONDS:
        return _DURATION_SECONDS[duration]
    if duration in ("tilrestart", "forever"):
        return _DURATION_FOREVER
    return None  # "once" or unknown -> caller handles (no learn / short reject)


# The workspace JWT (rotated) is bind-mounted here read-only; read fresh on each
# (re)connect so rotation is picked up (#2242, #2311). Not baked in env because
# the workspace token expires and rotates.
WORKSPACE_TOKEN_PATH = "/run/klangk/workspace-token"


# Host-scope modes for an allow-list spec, nginx-style (#2377): a bare host is
# EXACT (apex only); a leading-dot ``.host`` is INCLUSIVE (apex + subdomains);
# ``*.host`` is SUBDOMAINS only. (Bare = exact is the breaking flip from the
# old "bare = apex+subdomains" model.) One definition shared by parse_specs /
# ports_for / _session_host_allows_ttl.
_EXACT = "exact"
_INCLUSIVE = "inclusive"
_SUBDOMAINS = "subdomains"


def parse_specs(
    env_var: str = "KLANGKNETWORK_EGRESS_ALLOW",
) -> list[tuple[str, int | None, str]]:
    """Structured host specs from ``env_var`` (#2377, #2367).

    Each entry is ``(host, port, mode)``: ``mode`` is :data:`_EXACT` (bare host,
    apex only), :data:`_INCLUSIVE` (``.host``, apex + subdomains), or
    :data:`_SUBDOMAINS` (``*.host``, subdomains only). ``port`` is ``None`` for
    all-ports. CIDR specs (``10.0.0.0/8``) are excluded — the entrypoint applies
    those statically. The grammar mirrors ``klangk.netfilter.parse_allowed_domains``.
    """
    out: list[tuple[str, int | None, str]] = []
    for spec in os.environ.get(env_var, "").split(","):
        spec = spec.strip()
        if not spec or "/" in spec:
            continue
        port: int | None = None
        if ":" in spec:
            host_part, port_part = spec.rsplit(":", 1)
            if port_part.isdigit():
                port = int(port_part)
                spec = host_part
        s = spec.lower()
        mode = _EXACT
        if s.startswith("*."):
            mode = _SUBDOMAINS
            s = s[2:]
        elif s.startswith("."):
            mode = _INCLUSIVE
            s = s[1:]
        if s:
            out.append((s, port, mode))
    return out


SPECS = parse_specs()
# Static deny-list specs from ``KLANGKNETWORK_EGRESS_REJECT`` (#2367): a name
# matching one of these is NXDOMAIN'd unconditionally (see :func:`rejected_for`).
REJECT_SPECS = parse_specs("KLANGKNETWORK_EGRESS_REJECT")

# Hosts allow-listed in-session by a consent ``allow`` verdict, timed or
# forever (#2372, #2434): each entry is (host, port, mode, expire), mirroring
# SPECS plus an expiry epoch. On an allow whose duration is not ``once``,
# _decide_and_verdict adds the consented host:port here (expire = now + the
# verdict's TTL; forever/tilrestart map to ~a year) so ports_for treats it as
# allow-listed for the verdict's lifetime -- the DNS path then learns every
# resolved IP and allows it without NFQUEUE, so a CDN-rotated IP does NOT
# re-prompt (and _session_host_allows_ttl short-circuits any SYN that still
# races NFQUEUE). This is the fix for the ALLOW-REFUSED mismatch (#2434): a
# timed allow used to cover only the IP resolved at decision time, so a
# CDN-rotated IP re-entered NFQUEUE and a hold timeout there fail-closed to a
# deny REJECT an in-effect allow should override. ``once`` adds nothing -- it is
# per-connection, so a reconnect re-prompts. Dies with the sidecar (in-memory);
# the persisted allowed_domains entry (#2368) covers the next restart. Touched
# only on the event-loop thread (the NFQUEUE consumer is loop-driven, and
# ports_for runs in the DNS loop) -- no lock, like SPECS. Timed entries expire
# lazily via _prune_session_allows (the structure is loop-only, so it can't be
# swept off-loop by sweep_once under _LOCK like _LEARNED/_REJECTED).
_SESSION_HOST_ALLOWS: list[tuple[str, int | None, str, float]] = []

# Hosts denied in-session by a consent ``deny`` verdict, timed or forever
# (#2446 -- the deny-side mirror of :data:`_SESSION_HOST_ALLOWS`). Each entry is
# (host, port, mode, expire). On a deny whose duration is not ``once``,
# _decide_and_verdict adds the denied host:port here (expire = now + the
# verdict's TTL) so _cb (via _session_host_denies_ttl) denies a retry fast --
# without re-prompting -- even when a CDN rotation or a DNS-TTL lapse means the
# per-IP _REJECTED rule no longer covers the destination. This is the fix for
# the CARRYOVER-SURPRISE finding (#2446): a timed deny used to cover only the
# IP resolved at decision time (plus a ~10s REJECT_TTL), so a rotated IP
# re-entered NFQUEUE and re-prompted the user for a host they had already
# denied. ``once`` adds nothing (per-connection, so a reconnect re-prompts).
# Dies with the sidecar (in-memory); revocation (drop rule, decision=denied)
# clears it via _drop_session_denies. Touched only on the event-loop thread
# (like _SESSION_HOST_ALLOWS) -- no lock. Timed entries expire lazily via
# _prune_session_denies. An in-effect allow still wins: _cb consults
# _session_host_allows_ttl BEFORE _session_host_denies_ttl, so an allow
# overrides an in-effect deny.
_SESSION_HOST_DENIES: list[tuple[str, int | None, str, float]] = []

# Learned IPs: {ip: {"expire": epoch, "ports": set[int | None], "host": str}}.
# ``ports`` holds the ACCEPT rule ports (a ``None`` is all-ports); ``host`` is
# the DNS name that resolved to this IP (named in the consent request). An
# entry can be host-only (ports empty, no ACCEPT) -- recorded when a
# non-allow-listed name resolves so its connection SYN is consent-gated at
# NFQUEUE (#2324). Guarded by _LOCK: the asyncio loop runs the iptables installs
# + sweeps in the default thread-pool executor (see _learn_all / _async_sweeper),
# so two worker threads can touch _LEARNED at once; the lock serializes the
# rule+record mutations. The NFQUEUE consumer reads ``host`` via _host_for
# (under the lock; the host is set by the DNS loop before the SYN arrives).
_LEARNED: dict[str, dict] = {}
_LOCK = threading.Lock()
# Reused SYN verdicts, keyed by connection (src IP+port, dst IP+port):
# {(src_ip, src_port, dst, port): (verdict, expire)} so the kernel's SYN
# retransmits (tcp_syn_retries) of an already-decided connection reuse the
# verdict without re-prompting. The connection key (not just dst) is what makes
# a NEW connection -- new source port -- re-prompt, so duration=once is truly
# per-connection (#2361). Touched only on the event-loop thread (the NFQUEUE
# consumer is loop-driven) -- no lock.
_VERDICT_CACHE: dict[tuple[str, int, str, int], tuple[str, float]] = {}
# Connections with a verdict task still in flight (held, no verdict yet):
# keyed by the same connection tuple, prevents a SYN retransmit during the
# hold from spawning a SECOND consent request. The post-verdict
# :data:`_VERDICT_CACHE` covers retransmits after a decision; this covers the
# window before it (#2345 e2e: retransmit-pileup left duplicate pending
# requests lingering past the first's resolution). Touched only on the
# event-loop thread -- no lock. Added in :func:`_cb`, discarded in
# :func:`_decide_and_verdict` once the verdict is cached.
_INFLIGHT: set[tuple[str, int, str, int]] = set()
# Temporary REJECT (tcp-reset) rules for denied connections, keyed by
# (ip, port, sport): sport=0 is destination-scoped (catches every connection to
# ip:port, used for a timed/forever deny whose over-deny is intended + the
# WS-down fail-close); a nonzero sport is connection-scoped (catches only
# retransmits of THAT connection, used for a ``once`` deny so a NEW connection
# to the same host:port re-prompts instead of being rejected above NFQUEUE,
# #2463). Swept by sweep_once alongside _LEARNED. Makes a deny fail-fast
# (ECONNREFUSED) instead of waiting for tcp_syn_retries (~127s) -- dropping a
# SYN alone doesn't fail connect(); the kernel just retransmits until its own
# timeout.
_REJECTED: dict[tuple[str, int, int], float] = {}
# Strong refs to background asyncio tasks (the TTL sweeper) so CPython doesn't
# GC a sleeping task. A done-callback discards each entry on completion.
_BG_TASKS: set[asyncio.Task] = set()


def query_name(wire: bytes) -> str:
    """The queried domain (lowercased, no trailing dot) from a DNS wire message."""
    msg = dns.message.from_wire(wire)
    if not msg.question:
        return ""
    return msg.question[0].name.to_text().rstrip(".").lower()


def a_records_with_ttl(wire: bytes) -> list[tuple[str, int]]:
    """``[(ip, ttl_seconds), ...]`` from a DNS response wire.

    Walks the answer section (CNAME chains are transparent — the A records
    for the canonical name are in the answer too) and returns each A-record
    address with its rrset TTL. The TTL drives learned-IP expiry (#2256).
    """
    msg = dns.message.from_wire(wire)
    out: list[tuple[str, int]] = []
    for rrset in msg.answer:
        if rrset.rdtype == dns.rdatatype.A:
            ttl = int(rrset.ttl)
            for rdata in rrset:
                out.append((rdata.address, ttl))
    return out


def nxdomain_for(wire: bytes) -> bytes:
    """An NXDOMAIN response wire for the given query wire."""
    query = dns.message.from_wire(wire)
    resp = dns.message.make_response(query)
    resp.set_rcode(dns.rcode.NXDOMAIN)
    return resp.to_wire()


def _host_matches(qname: str, host: str, mode: str) -> bool:
    """Does ``qname`` match ``host`` under nginx-style scope ``mode`` (#2377)?

    Shared by :func:`ports_for` (the DNS gate) and :func:`_session_host_allows_ttl`
    (the NFQUEUE gate) so the two can't drift. :data:`_EXACT` (bare host) matches
    the apex only; :data:`_INCLUSIVE` (``.host``) matches apex + subdomains;
    :data:`_SUBDOMAINS` (``*.host``) matches subdomains only. The suffix check
    requires a leading dot, so ``evilexample.com`` does NOT match
    ``example.com``.
    """
    if mode == _SUBDOMAINS:
        return qname.endswith("." + host)
    if mode == _INCLUSIVE:
        return qname == host or qname.endswith("." + host)
    return qname == host  # _EXACT (and the safe default for an unknown mode)


def ports_for(qname: str) -> set[int] | None:
    """The ports a queried name is allowed on under :data:`SPECS` (#2377).

    ``None``  — a port-less spec matched (allow all ports).
    ``set()`` — nothing matched (deny).
    ``{443, ...}`` — allow exactly these TCP ports.

    Scope: a bare host is :data:`_EXACT` (apex only); ``.host`` is
    :data:`_INCLUSIVE` (apex + subdomains); ``*.host`` is :data:`_SUBDOMAINS`
    (subdomains only, apex excluded).
    """
    ports: set[int] = set()
    _prune_session_allows()
    session = [(h, p, m) for (h, p, m, _exp) in _SESSION_HOST_ALLOWS]
    for host, port, mode in (*SPECS, *session):
        if not _host_matches(qname, host, mode):
            continue
        if port is None:
            return None  # an all-ports spec dominates
        ports.add(port)
    return ports


def rejected_for(qname: str) -> bool:
    """Does ``qname`` match a :data:`REJECT_SPECS` entry (#2367)?

    Parallel to :func:`ports_for` for the deny-list, but boolean -- a rejected
    name is NXDOMAIN'd unconditionally, so there is no port dimension. Matches
    via :func:`_host_matches` (nginx-style scope): bare = apex only, ``.host`` =
    apex + subdomains, ``*.host`` = subdomains only.
    """
    return any(_host_matches(qname, host, mode) for host, _port, mode in REJECT_SPECS)


def _prune_session_allows() -> None:
    """Drop expired in-session host allows (lazy sweep, #2434).

    :data:`_SESSION_HOST_ALLOWS` is loop-only (no lock), so -- unlike
    :data:`_LEARNED` / :data:`_REJECTED`, which are swept off-loop by
    :func:`sweep_once` under :data:`_LOCK` -- its timed entries expire here, on
    the loop, the next time a gate (:func:`ports_for`,
    :func:`_session_host_allows_ttl`, :func:`_add_session_host`) reads them.
    Cheap (the list is tiny -- one entry per consented host:port) and keeps the
    structure from growing unbounded across a long session.
    """
    now = time.time()
    _SESSION_HOST_ALLOWS[:] = [t for t in _SESSION_HOST_ALLOWS if t[3] > now]


def _add_session_host(host: str, port: int, ttl: float) -> None:
    """Allow-list ``host:port`` in-session for a consent allow verdict (#2372,
    #2434).

    Adds ``(host, port, _EXACT, now + ttl)`` to :data:`_SESSION_HOST_ALLOWS` so
    :func:`ports_for` (the DNS gate) treats the host as allow-listed for the
    verdict's lifetime -- the DNS path then learns every resolved IP and allows
    it without NFQUEUE, so a CDN-rotated IP no longer re-prompts (or, if it
    still races NFQUEUE, :func:`_session_host_allows_ttl` short-circuits it in
    :func:`_cb`). EXACT scope: the user approved the specific qname they saw, so
    only that host (not its subdomains) is opened (#2377). Deduped; a re-allow
    of the same host:port refreshes the expiry (``max`` -- never shortens an
    unexpired entry). A timed allow (5s/5m/1h/tilrestart) is host-scoped just
    like ``forever`` (#2434); ``once`` carries no host-allow (per-connection, so
    a reconnect re-prompts). Loop-only (no lock).
    """
    _prune_session_allows()
    expire = time.time() + ttl
    spec = (host, port, _EXACT)
    for i, (h, p, mode, _exp) in enumerate(_SESSION_HOST_ALLOWS):
        if (h, p, mode) == spec:
            _SESSION_HOST_ALLOWS[i] = (h, p, mode, max(_exp, expire))
            return
    _SESSION_HOST_ALLOWS.append((host, port, _EXACT, expire))


def _session_host_allows_ttl(host: str, port: int) -> float | None:
    """Remaining seconds an in-session allow covers ``host`` on ``port``, or
    ``None`` (#2372, #2434).

    Used by :func:`_cb` as the last-chance gate before prompting: a SYN to a
    host:port the user allowed (timed or forever) -- including a CDN-rotated or
    resolver-cached IP that no fresh DNS resolution re-ACCEPTed -- is
    auto-allowed, learned for the allow's remaining window, so the user isn't
    re-asked (and a hold timeout can't fail-close a still-allowed host to a
    deny, #2434). Matches via :func:`_host_matches` (nginx-style scope); entries
    are added EXACT by :func:`_add_session_host`, so only the approved host
    matches (#2377). Returns the max remaining TTL across matching entries.
    Loop-only (no lock).
    """
    if not host:
        return None
    _prune_session_allows()
    now = time.time()
    best: float | None = None
    for h, p, mode, exp in _SESSION_HOST_ALLOWS:
        if exp <= now:
            continue  # belt-and-suspenders: _prune ran above, but a just-expired
            # entry can survive the microseconds between its `now` and this one.
        if _host_matches(host, h, mode) and (p == port or p is None):
            remaining = exp - now
            if best is None or remaining > best:
                best = remaining
    return best


def _session_allow_rule_cap(qname: str) -> float | None:
    """Min remaining TTL bounding a DNS-path learned rule for ``qname``, or
    ``None`` (#2465).

    A timed consent allow adds the host to :data:`_SESSION_HOST_ALLOWS`, so
    :func:`ports_for` treats it as allow-listed and the DNS path
    (:func:`_respond_allowed` -> :func:`_learn_all`) learns every resolved IP.
    That learn used to install the ACCEPT rule for the response's DNS TTL --
    often minutes -- so a short verdict (5s) left a rule that outlived it: a
    retry past the window connected with no re-prompt (the allow/deny asymmetry
    of #2465 -- the deny side records no DNS-path learn, so it expired on
    time). The cap returned here bounds the rule's TTL at the min remaining
    across matching session allows, so the rule lapses with the verdict and a
    retry past the window re-prompts.

    ``None`` (no cap -- use the DNS TTL) when a static :data:`SPECS` entry
    matches: a static allow is forever, so the DNS TTL is the correct rule
    lifetime, and capping it would expire the rule early and -- in the gap
    between rule expiry and the next resolve -- re-prompt a forever-allowed
    host (a static spec has no NFQUEUE gate, only the learned rule covers its
    SYN). Also ``None`` when no session allow matches (a static-only or
    non-allow-listed name learns at its DNS TTL). Loop-only (reads
    :data:`_SESSION_HOST_ALLOWS`); computed on the event-loop thread in
    :func:`_respond_allowed` and passed to :func:`_learn_all`, which runs
    off-loop in the executor.
    """
    if any(_host_matches(qname, host, mode) for host, _port, mode in SPECS):
        return None  # a static spec matches -> forever -> DNS TTL is correct
    _prune_session_allows()
    now = time.time()
    best: float | None = None
    for host, _port, mode, exp in _SESSION_HOST_ALLOWS:
        if exp <= now:
            continue
        if _host_matches(qname, host, mode):
            remaining = exp - now
            if best is None or remaining < best:
                best = remaining
    return best


def _prune_session_denies() -> None:
    """Drop expired in-session host denies (lazy sweep, #2446).

    :data:`_SESSION_HOST_DENIES` is loop-only (no lock), so -- like
    :data:`_SESSION_HOST_ALLOWS` -- its timed entries expire here, on the loop,
    the next time a gate (:func:`_session_host_denies_ttl`,
    :func:`_add_session_deny`, :func:`_drop_session_denies`) reads them. Cheap
    (the list is tiny -- one entry per denied host:port) and keeps the structure
    from growing unbounded across a long session.
    """
    now = time.time()
    _SESSION_HOST_DENIES[:] = [t for t in _SESSION_HOST_DENIES if t[3] > now]


def _add_session_deny(host: str, port: int, ttl: float) -> None:
    """Deny ``host:port`` in-session for a consent deny verdict (#2446).

    The deny-side mirror of :func:`_add_session_host`: adds
    ``(host, port, _EXACT, now + ttl)`` to :data:`_SESSION_HOST_DENIES` so
    :func:`_cb` (via :func:`_session_host_denies_ttl`) suppresses a re-prompt
    for a host the user already denied -- including a CDN-rotated or
    resolver-cached IP that the per-IP :data:`_REJECTED` rule does not cover
    (the CARRYOVER-SURPRISE, #2446). EXACT scope (only the denied host, not its
    subdomains, #2377); deduped, a re-deny refreshes the expiry (``max`` --
    never shortens an unexpired entry). ``once`` adds nothing (per-connection,
    so a reconnect re-prompts). Loop-only (no lock).
    """
    _prune_session_denies()
    expire = time.time() + ttl
    spec = (host, port, _EXACT)
    for i, (h, p, mode, _exp) in enumerate(_SESSION_HOST_DENIES):
        if (h, p, mode) == spec:
            _SESSION_HOST_DENIES[i] = (h, p, mode, max(_exp, expire))
            return
    _SESSION_HOST_DENIES.append((host, port, _EXACT, expire))


def _session_host_denies_ttl(host: str, port: int) -> float | None:
    """Remaining seconds an in-session deny covers ``host`` on ``port``, or
    ``None`` (#2446).

    The deny-side mirror of :func:`_session_host_allows_ttl`, used by
    :func:`_cb` as the last-chance gate before prompting: a SYN to a host:port
    the user already denied (timed or forever) -- including a CDN-rotated or
    resolver-cached IP that no fresh per-IP :data:`_REJECTED` rule covers -- is
    denied fast (RST + short REJECT) without re-prompting. Matches via
    :func:`_host_matches` (entries are added EXACT, so only the denied host
    matches, #2377); port must match (or the entry is all-ports). Returns the
    max remaining TTL across matching entries. Loop-only (no lock).
    """
    if not host:
        return None
    _prune_session_denies()
    now = time.time()
    best: float | None = None
    for h, p, mode, exp in _SESSION_HOST_DENIES:
        if exp <= now:
            continue  # belt-and-suspenders: _prune ran above, but a just-expired
            # entry can survive the microseconds between its `now` and this one.
        if _host_matches(host, h, mode) and (p == port or p is None):
            remaining = exp - now
            if best is None or remaining > best:
                best = remaining
    return best


def _rule_args(ip: str, port: int | None) -> list[str]:
    """iptables OUTPUT rule args for ``ACCEPT`` to ``ip`` (optionally scoped)."""
    args = ["-d", ip]
    if port is not None:
        args += ["-p", "tcp", "--dport", str(port)]
    args += ["-j", "ACCEPT"]
    return args


def _rule_exists(ip: str, port: int | None) -> bool:
    return (
        subprocess.run(
            [IPT, "-C", "OUTPUT", *_rule_args(ip, port)],
            capture_output=True,
        ).returncode
        == 0
    )


def _install(ip: str, port: int | None) -> None:
    """Insert the ACCEPT rule at the top of OUTPUT if not already present."""
    if _rule_exists(ip, port):
        return
    subprocess.run(
        [IPT, "-I", "OUTPUT", "1", *_rule_args(ip, port)],
        capture_output=True,
    )


def _remove(ip: str, port: int | None) -> None:
    """Delete one matching ACCEPT rule; swallow failure if it's already gone."""
    subprocess.run(
        [IPT, "-D", "OUTPUT", *_rule_args(ip, port)],
        capture_output=True,
    )


def allow(ip: str, port: int | None, ttl: int | float) -> None:
    """Install (if new) the ACCEPT for ``ip[:port]`` and refresh its TTL.

    ``port`` is ``None`` for an all-ports rule. The learned IP's expiry is
    set to ``now + max(ttl, MIN_TTL)`` (a 0-TTL response must not yank the
    rule the workspace needs to reach the IP it just resolved) and only ever
    moves forward, so a shorter-TTL re-resolution can't prematurely expire a
    longer-lived prior rule (#2256).

    The install happens **under** :data:`_LOCK` so the kernel rule and its
    ``_LEARNED`` record are atomic w.r.t. :func:`sweep_once`'s remove+delete
    (also under the lock). Without that, a concurrent sweep running in another
    executor worker could delete a rule :func:`allow` just installed while
    ``_LEARNED`` still records it as present -- a fail-closed availability gap
    that only self-heals on the next re-resolution (#2256 review). ``allow`` and
    :func:`sweep_once` both run off the event loop in the default thread-pool
    executor (see :func:`_learn_all` / :func:`_async_sweeper`), so the lock
    genuinely serializes them; contention is negligible.
    """
    expire = time.time() + max(ttl, MIN_TTL)
    with _LOCK:
        _install(ip, port)
        rec = _LEARNED.get(ip)
        if rec is None:
            _LEARNED[ip] = {
                "expire": expire,
                "rule_expire": expire,
                "ports": {port},
                "host": None,
            }
        else:
            rec["expire"] = max(rec["expire"], expire)
            # rule_expire is the ACCEPT rule's lifetime, kept SEPARATE from the
            # host-mapping expire so a re-resolve's longer DNS TTL can't extend
            # a consent allow's rule past its verdict (#2408). max() preserves
            # the longest across static re-learns (#2256); for a consent allow
            # it is just the verdict's TTL (the pre-existing rule_expire is
            # absent -- only _record_hosts has touched the record -- and `or
            # 0.0` coerces the None).
            rec["rule_expire"] = max(rec.get("rule_expire") or 0.0, expire)
            rec["ports"].add(port)
            # ``host`` (set by _record_hosts) is preserved across re-learn.
        # An all-ports allow (the consent path) supersedes any prior per-port
        # denies for this IP -- otherwise the all-ports ACCEPT at the top of
        # OUTPUT would silently shadow a lingering REJECT (the decider allowed
        # the host, so a prior port-specific deny no longer applies).
        if port is None:
            for key in [k for k in _REJECTED if k[0] == ip]:
                try:
                    _remove_reject(*key)
                except Exception:
                    pass
                del _REJECTED[key]


def _reject_rule_args(ip: str, port: int, sport: int = 0) -> list[str]:
    """iptables OUTPUT rule args for REJECT (tcp-reset) to ``ip:port``.

    ``sport`` (the denied connection's source port) scopes the rule to
    retransmits of THAT connection only (#2463); 0/omitted leaves it
    destination-scoped (every connection to ``ip:port``).
    """
    args = ["-d", ip, "-p", "tcp", "--dport", str(port)]
    if sport:
        args += ["--sport", str(sport)]
    args += ["-j", "REJECT", "--reject-with", "tcp-reset"]
    return args


def _reject_rule_exists(ip: str, port: int, sport: int = 0) -> bool:
    return (
        subprocess.run(
            [IPT, "-C", "OUTPUT", *_reject_rule_args(ip, port, sport)],
            capture_output=True,
        ).returncode
        == 0
    )


def _install_reject(ip: str, port: int, sport: int = 0) -> None:
    """Insert the REJECT (tcp-reset) rule at the top of OUTPUT if not present."""
    if _reject_rule_exists(ip, port, sport):
        return
    subprocess.run(
        [IPT, "-I", "OUTPUT", "1", *_reject_rule_args(ip, port, sport)],
        capture_output=True,
    )


def _remove_reject(ip: str, port: int, sport: int = 0) -> None:
    """Delete the REJECT rule; swallow failure if it's already gone."""
    subprocess.run(
        [IPT, "-D", "OUTPUT", *_reject_rule_args(ip, port, sport)],
        capture_output=True,
    )


def reject(ip: str, port: int, ttl: float, sport: int = 0) -> None:
    """Install a temporary REJECT (tcp-reset) for ``ip:port`` + set its TTL.

    A denied SYN is dropped, but dropping a SYN doesn't fail ``connect()`` --
    the kernel retransmits (tcp_syn_retries, ~127s) before timing out. The
    REJECT rule makes the next retransmit get a RST, so ``connect()`` returns
    ECONNREFUSED at once (eager deny). Like :func:`allow`, the install + the
    ``_REJECTED`` record are atomic under :data:`_LOCK` w.r.t. :func:`sweep_once`.

    ``sport`` (the denied connection's source port) scopes the rule to
    retransmits of THAT connection only, so a NEW connection (different source
    port) to the same ``ip:port`` is NOT rejected above NFQUEUE and re-enters
    consent-gating (#2463). 0/omitted leaves the rule destination-scoped
    (every connection to ``ip:port``), which is correct for a timed/forever
    deny (its over-deny is intended -- the DB rule + ``_SESSION_HOST_DENIES``
    govern re-prompting) and the WS-down fail-close.
    """
    expire = time.time() + ttl
    with _LOCK:
        _install_reject(ip, port, sport)
        _REJECTED[(ip, port, sport)] = max(
            _REJECTED.get((ip, port, sport), 0.0), expire
        )


def drop_for_host(host: str, decision: str) -> set[str]:
    """Drop the sidecar's rules for a host (revocation, #2339).

    ``allowed``: remove the learned ACCEPT rules (+ ``_LEARNED`` records) for
    the host's IPs (revert to default/allow-list filtering).
    ``denied``: remove the temporary REJECT rules for the host's IPs (stop
    force-rejecting; the host is again subject to the allow-list).

    Returns the set of candidate IPs (the host's resolved IPs + the host
    string itself, for a direct-IP connect) so the caller --
    :meth:`SidecarConsentClient._handle_drop_rule`, on the event loop -- can
    clear the loop-only ``_SESSION_HOST_ALLOWS``/``_VERDICT_CACHE`` state
    (via :func:`_drop_session_hosts` / :func:`_clear_verdict_cache`). Those
    structures are documented loop-only
    (no lock) and must NOT be mutated here, since this function runs off the
    loop in the executor.

    Host->IP comes from ``_LEARNED[ip]["host"]`` (set by ``_record_hosts`` for
    every resolved name, allow-listed or not), so a deny's IPs are found too;
    the host string itself is also a candidate IP (a direct-IP connect that
    never went through DNS, and a direct-IP allow whose ``host`` is ``None``).
    Best-effort: a failed delete drops one rule, not the whole revoke. Sync
    (forks iptables) -- run off the loop; under ``_LOCK`` like allow/sweep.

    L3/L4 limit (co-resident hosts): the egress rules are per IP+port, so two
    DNS names that resolve to the SAME IP share one rule and cannot be revoked
    individually -- revoking one name removes the shared rule, affecting the
    other too. A correct per-host revoke for co-resident hosts (CDN/S3/Cloudflare
    fronted sites) needs L7/SNI filtering, which is a separate feature (#2352).
    """
    host_l = host.lower()
    with _LOCK:
        ips = [
            ip
            for ip, rec in _LEARNED.items()
            if (rec.get("host") or "").lower() == host_l
        ]
        # IPs that resolved to this host, plus the host itself if it's a
        # direct-IP connect/allow (the scan above misses a direct-IP allow,
        # whose host record is None).
        targets = {ip for ip in ips}
        targets.add(host_l)
        targets.add(host)
        if decision == "allowed":
            for ip in [i for i in targets if i in _LEARNED]:
                for port in list(_LEARNED[ip]["ports"]):
                    try:
                        _remove(ip, port)
                    except Exception:
                        pass
                del _LEARNED[ip]
        elif decision == "denied":
            for key in [k for k in _REJECTED if k[0] in targets]:
                try:
                    _remove_reject(*key)
                except Exception:
                    pass
                del _REJECTED[key]
    return targets


def _drop_session_hosts(host: str) -> None:
    """Remove a host's in-session allow coverage (#2370, #2372, #2434).

    Drops every :data:`_SESSION_HOST_ALLOWS` entry whose host matches
    (case-insensitive). Called on the **event loop** by
    :meth:`SidecarConsentClient._handle_drop_rule` **before** :func:`drop_for_host`
    forks iptables in the executor: while that fork runs (~tens of ms), the
    NFQUEUE consumer (:func:`_cb` -> :func:`_session_host_allows_ttl`) and the DNS
    path (:func:`ports_for`) both read :data:`_SESSION_HOST_ALLOWS`, and a
    SYN/resolve arriving in that window would otherwise re-install a fresh
    ACCEPT (the host's remaining allow TTL, via :func:`allow`) that the revoke
    never clears. Clearing it first makes both gates deny during the window, so
    no fresh rule can be installed; :func:`drop_for_host` then removes the
    existing ACCEPTs. :data:`_SESSION_HOST_ALLOWS` is loop-only (no lock) --
    touched on the loop, never inside :func:`drop_for_host` (executor thread). A
    deny revoke does not call this (a deny never adds to
    :data:`_SESSION_HOST_ALLOWS`).
    """
    hl = host.lower()
    _SESSION_HOST_ALLOWS[:] = [t for t in _SESSION_HOST_ALLOWS if t[0].lower() != hl]


def _drop_session_denies(host: str) -> None:
    """Remove a host's in-session deny coverage (#2446).

    The deny-side mirror of :func:`_drop_session_hosts`, called on the event
    loop by :meth:`SidecarConsentClient._handle_drop_rule` for a ``denied``
    revoke BEFORE :func:`drop_for_host` forks iptables in the executor: while
    that fork runs (~tens of ms), :func:`_cb` reads :data:`_SESSION_HOST_DENIES`,
    and a SYN arriving in that window would otherwise keep auto-denying (and
    re-installing a REJECT for) the host the operator just un-denied. Clearing
    it first lets the host re-prompt. :data:`_SESSION_HOST_DENIES` is loop-only
    (no lock) -- touched on the loop, never inside :func:`drop_for_host`
    (executor thread).
    """
    hl = host.lower()
    _SESSION_HOST_DENIES[:] = [t for t in _SESSION_HOST_DENIES if t[0].lower() != hl]


def _clear_verdict_cache(ips: set[str]) -> None:
    """Drop cached SYN verdicts for a revoked host's IPs (#2370).

    Called on the **event loop** by :meth:`SidecarConsentClient._handle_drop_rule`
    AFTER :func:`drop_for_host` (which returned the host's candidate IPs).
    :data:`_VERDICT_CACHE` is keyed by ``(src_ip, src_port, dst, port)``; dst is
    ``key[2]``. A cache hit only ``pkt.accept()``s a retransmit -- it does NOT
    re-install an ACCEPT rule (see :func:`_cb`) -- so this needs no
    pre-``drop_for_host`` window protection and may run after the drop.
    Loop-only dict (no lock). If a host's IP was already TTL-swept from
    :data:`_LEARNED` before the revoke, it is absent from ``ips`` and its cache
    entry self-expires at the ``_VERDICT_CACHE`` TTL (harmless: no ACCEPT rule,
    so a new flow re-prompts).
    """
    if ips:
        for key in [c for c in _VERDICT_CACHE if c[2] in ips]:
            del _VERDICT_CACHE[key]


def sweep_once(now: float | None = None) -> list[tuple[str, set]]:
    """Remove ACCEPT rules whose TTL has elapsed; return ``(ip, ports)`` removed.

    Two lifetimes are tracked per learned IP (#2408):

    * ``rule_expire`` -- the ACCEPT rule's lifetime (a consent allow's verdict,
      or a static re-learn's DNS TTL). When it elapses the kernel ACCEPT rule
      is deleted but the record is KEPT while its host-mapping ``expire`` is
      still valid, so :func:`_host_for` can still name the host for a fresh
      consent request.
    * ``expire`` -- the host-mapping lifetime (the DNS TTL). When it elapses
      AND no ACCEPT rule remains, the whole record is dropped.

    Records without ``rule_expire`` (host-mapping-only entries from
    :func:`_record_hosts`, or pre-#2408 records) fall back to ``expire`` for the
    rule sweep, preserving the old single-expiry behavior. Removal runs under
    :data:`_LOCK` (see :func:`allow`): the rule delete and the record delete are
    atomic, so a concurrent :func:`allow` can't re-record an IP whose kernel
    rule was just swept. Factored out of :func:`_async_sweeper` so it is
    unit-testable with a mocked clock and iptables (#2256).
    """
    if now is None:
        now = time.time()
    expired: list[tuple[str, set]] = []
    with _LOCK:
        for ip, rec in list(_LEARNED.items()):
            # Rule sweep: the ACCEPT rule's lifetime is rule_expire when set
            # (a consent allow, whose verdict must outlive the host-mapping's
            # DNS TTL, #2408), else expire (static re-learn / backward compat).
            rule_expire = rec.get("rule_expire", rec["expire"])
            if rec["ports"] and rule_expire <= now:
                ports = set(rec["ports"])
                for port in ports:
                    try:
                        _remove(ip, port)
                    except Exception:
                        pass  # a transient failure drops one rule, not the sweep
                expired.append((ip, ports))
                rec["ports"] = set()  # rule gone; keep record for naming
            # Record sweep: drop the host mapping once its own expire elapses
            # and no ACCEPT rule remains.
            if rec["expire"] <= now and not rec["ports"]:
                del _LEARNED[ip]
        # also sweep temporary REJECT (tcp-reset) rules for denied connections
        for key in [k for k, exp in _REJECTED.items() if exp <= now]:
            try:
                _remove_reject(*key)
            except Exception:
                pass
            del _REJECTED[key]
    return expired


async def _async_sweeper() -> None:
    """Background task: periodically drop learned IPs past their TTL (#2256).

    :func:`sweep_once` runs in the executor so its iptables ``-D`` forks don't
    block the loop (and so it can run concurrently with :func:`_learn_all`,
    serialized by :data:`_LOCK`).
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            await asyncio.get_running_loop().run_in_executor(None, sweep_once)
        except Exception:
            pass  # a transient sweep failure defers cleanup to the next tick


def _fmt_ports(ports: set[int | None]) -> str:
    return "all" if None in ports else ",".join(sorted(str(p) for p in ports))


def check_mark() -> None:
    """Verify the proxy can set SO_MARK (needs CAP_NET_ADMIN/NET_RAW).

    Without it the proxy's upstream forwards are not exempted from the nat
    REDIRECT and loop back into itself — DNS is broken. Fail loud at startup.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
    except OSError as exc:
        raise SystemExit(
            f"dns-proxy: cannot set SO_MARK={MARK} ({exc}); the sidecar needs "
            "CAP_NET_ADMIN for mark-based loop-avoidance (#2264)"
        )
    finally:
        probe.close()


def _learn_all(
    recs: list[tuple[str, int]],
    ports: set[int | None],
    cap: float | None = None,
) -> None:
    """Install the ACCEPT rule for each learned IP/port (sync; runs in the
    executor so the iptables forks don't block the event loop).

    ``cap``, when not ``None``, bounds each rule's TTL at ``min(dns_ttl, cap)``
    so a timed session-allow's learned rule does not outlive its verdict
    (#2465). The host mapping -- set earlier by :func:`_record_hosts` at the
    DNS TTL, ahead of the consent allow -- is untouched: :func:`allow`'s
    ``max`` keeps the longer mapping lifetime so :func:`_host_for` still names
    the host for a fresh re-prompt after the verdict lapses (#2408).
    """
    for ip, ttl in recs:
        rule_ttl = ttl if cap is None else min(ttl, cap)
        for port in ports:
            allow(ip, port, rule_ttl)


def _record_hosts(recs: list[tuple[str, int]], host: str) -> None:
    """Record IP->host in _LEARNED WITHOUT installing an ACCEPT rule (#2324).

    A non-allow-listed name resolves (the workspace gets the IP + can SYN) but
    its connection is consent-gated at the SYN (NFQUEUE), so the IP must NOT be
    allow-learned here. Recording host lets the NFQUEUE consumer name the host
    in the consent request. Sync; runs in the executor alongside allow/sweep
    (under _LOCK). The TTL refreshes on each resolve so a re-resolution extends
    the window in which a SYN names the right host.

    Only the host-mapping ``expire`` is touched here -- never ``rule_expire``
    (the ACCEPT rule's lifetime, set by :func:`allow`). Keeping the two
    separate is what lets a consent allow's rule expire at its verdict while
    the host mapping lives for the DNS TTL, so a re-resolve's longer DNS TTL
    can't extend an allow past its verdict (#2408).
    """
    now = time.time()
    with _LOCK:
        for ip, ttl in recs:
            expire = now + max(ttl, MIN_TTL)
            rec = _LEARNED.get(ip)
            if rec is None:
                _LEARNED[ip] = {"expire": expire, "ports": set(), "host": host}
            else:
                rec["expire"] = max(rec["expire"], expire)
                rec["host"] = host  # latest name that resolved to this IP


def _host_for(ip: str) -> str:
    """The DNS name that resolved to ``ip``, or ``ip`` itself (direct-IP connect)."""
    with _LOCK:
        return _LEARNED.get(ip, {}).get("host") or ip


async def _respond_allowed(
    s: socket.socket,
    resp: bytes,
    addr: tuple[str, int],
    qname: str,
    ports: set[int | None],
) -> None:
    """Learn the response's IPs (port-scoped, TTL-tracked) + send it, swallowing
    transient errors.

    The iptables installs (:func:`_learn_all`) run off the loop in the default
    thread-pool executor so a burst of learned IPs can't stall the DNS receive
    loop or verdict dispatch. A failure here (a transient ``iptables`` error, or
    a ``sendto`` to a vanished client) must drop only this one response -- not
    kill the proxy (#2278): if it escaped, the sidecar's PID 1 would exit, DNS
    would be dead for the workspace, and the learned ``ACCEPT`` rules would
    persist (a partial fail-open).
    """
    loop = asyncio.get_running_loop()
    try:
        recs = a_records_with_ttl(resp)
    except Exception:
        recs = []
    # Bound a timed session-allow's learned rule at its verdict's remaining
    # window (#2465): without this the DNS-path learn uses the response's DNS
    # TTL (often minutes), so a 5s allow leaves a rule that outlives it and a
    # retry past the window connects with no re-prompt. None for a static spec
    # (forever) or no session allow -- the DNS TTL is correct then. Computed
    # on the loop (reads loop-only _SESSION_HOST_ALLOWS) before the executor
    # fork below.
    cap = _session_allow_rule_cap(qname) if recs else None
    try:
        if recs:
            await loop.run_in_executor(None, _learn_all, recs, ports, cap)
        if DEBUG:
            print(
                f"allow {qname} -> {[ip for ip, _ in recs]} ports={_fmt_ports(ports)}",
                flush=True,
            )
        s.sendto(resp, addr)
    except Exception:
        pass


def _decision(qname: str, ports: set[int] | None) -> tuple[bool, set[int | None]]:
    """Classify a query: ``(deny, port_set)``.

    ``deny=True`` -> send NXDOMAIN. ``port_set`` is the set of ports to allow
    for each learned IP (a ``None`` entry is the all-ports rule). Factored out
    of :func:`main` so the None-vs-empty gate is unit-tested directly —
    inverting it (treating ``None`` as deny, or an empty set as allow) is a
    fail-open/fail-closed bug in the security gate (#2256).
    """
    if not qname or (ports is not None and not ports):
        return True, set()
    return False, ports if ports is not None else {None}


def parse_dest(payload: bytes) -> tuple[str, int]:
    """``(dst_ip, dst_port)`` from an IPv4 packet payload, or ``("", 0)``.

    The NFQUEUE payload may start at L3 (IP) or include a 14-byte Ethernet
    header; detect the IPv4 version nibble. Port is 0 for non-TCP/UDP. Pure
    so it can be unit-tested with synthetic bytes.
    """
    off = 0
    if len(payload) > 14 and (payload[0] >> 4) != 4 and (payload[14] >> 4) == 4:
        off = 14  # Ethernet header
    if off + 20 > len(payload) or (payload[off] >> 4) != 4:
        return "", 0
    ihl = (payload[off] & 0x0F) * 4
    if ihl < 20 or off + ihl > len(payload):
        return "", 0
    proto = payload[off + 9]
    dst = ".".join(str(b) for b in payload[off + 16 : off + 20])
    port = 0
    if proto in (6, 17) and off + ihl + 4 <= len(
        payload
    ):  # TCP / UDP, L4 header present
        port = int.from_bytes(payload[off + ihl + 2 : off + ihl + 4], "big")
    return dst, port


def parse_syn_tuple(payload: bytes) -> tuple[str, int, str, int, int]:
    """``(src_ip, src_port, dst_ip, dst_port, seq)`` from an IPv4 TCP SYN, or an
    all-zero tuple if unparseable.

    The SYN's source end (IP:port) is the workspace's local end and becomes the
    forged RST's *destination*; its sequence number drives the RST's ack (#2345).
    Pure (mirrors :func:`parse_dest`'s L3/L4 offset logic) so it can be
    unit-tested with synthetic bytes.
    """
    off = 0
    if len(payload) > 14 and (payload[0] >> 4) != 4 and (payload[14] >> 4) == 4:
        off = 14  # Ethernet header
    if off + 20 > len(payload) or (payload[off] >> 4) != 4:
        return "", 0, "", 0, 0
    ihl = (payload[off] & 0x0F) * 4
    if ihl < 20 or off + ihl > len(payload):
        return "", 0, "", 0, 0
    if payload[off + 9] != 6:  # TCP only
        return "", 0, "", 0, 0
    l4 = off + ihl
    if l4 + 12 > len(payload):  # sport + dport + seq
        return "", 0, "", 0, 0
    src_ip = ".".join(str(b) for b in payload[off + 12 : off + 16])
    dst_ip = ".".join(str(b) for b in payload[off + 16 : off + 20])
    src_port = int.from_bytes(payload[l4 : l4 + 2], "big")
    dst_port = int.from_bytes(payload[l4 + 2 : l4 + 4], "big")
    seq = int.from_bytes(payload[l4 + 4 : l4 + 8], "big")
    return src_ip, src_port, dst_ip, dst_port, seq


# ---------------------------------------------------------------------------
# Interactive consent: egress-sidecar WS client + hold paths (#2311 half B).
# ---------------------------------------------------------------------------


def _ws_url(consent_url: str) -> str:
    """Derive the ``/ws/egress-sidecar`` WS URL from the consent HTTP URL.

    ``http(s)://host:port/...`` -> ``ws(s)://host:port/ws/egress-sidecar``.
    Reuses :data:`CONSENT_URL`'s scheme+host so no new env var is needed; the
    sidecar leg of the consent contract (``sidecar <-WS-> klangkd``).
    """
    if consent_url.startswith("https://"):
        host = consent_url[len("https://") :].split("/", 1)[0]
        return f"wss://{host}/ws/egress-sidecar"
    if consent_url.startswith("http://"):
        host = consent_url[len("http://") :].split("/", 1)[0]
        return f"ws://{host}/ws/egress-sidecar"
    return consent_url  # already ws(s)://, or an exotic scheme used verbatim


class SidecarConsentClient:
    """Persistent WS client to klangkd's ``/ws/egress-sidecar`` (#2311 half B).

    One socket per workspace, opened at startup, reconnected (exponential
    backoff) on drop. :meth:`request` sends an egress frame and awaits the
    matching verdict frame by id. **Fail-close**: a down connection or a
    timed-out verdict resolves to ``"deny"`` immediately, so the proxy
    NXDOMAIN/DROPs (today's static behavior) when klangkd or the decider is
    unreachable -- the workspace never hangs on a pending connection.

    All ``_pending`` state lives on the event-loop thread (coroutines +
    ``_run``'s receive loop); the NFQUEUE consumer is itself loop-driven
    (``get_fd`` + ``add_reader``) and calls :meth:`request` directly, so it
    never crosses threads or touches ``_pending`` from outside the loop.
    """

    def __init__(self, consent_url: str, token_path: str, hold_timeout: float) -> None:
        self._url = _ws_url(consent_url)
        self._token_path = token_path
        self._hold_timeout = hold_timeout
        self._ws = None  # current websockets connection, or None
        self._connected = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}  # id -> Future
        self._stop = False
        self._no_token_warned = False
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._fail_close_pending()

    async def _run(self) -> None:
        import websockets  # sidecar-only dep; lazy so the module loads without it

        backoff = 1.0
        while not self._stop:
            token = self._read_token()
            if not token:
                # Token file not present yet (workspace JWT not written). Retry
                # without escalating backoff (expected at startup).
                if DEBUG and not self._no_token_warned:
                    print(
                        "consent: workspace token not yet present; retrying",
                        flush=True,
                    )
                    self._no_token_warned = True
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    additional_headers={"Authorization": "Bearer " + token},
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self._no_token_warned = False
                    backoff = 1.0
                    if DEBUG:
                        print(f"consent: connected to {self._url}", flush=True)
                    async for raw in ws:
                        await self._dispatch(raw)
            except Exception as exc:
                # Log the exception TYPE only -- the connection carries the
                # workspace JWT (Authorization header), and a websockets
                # exception could embed request details (#2309). The websockets
                # package logger is capped at WARNING on import so its DEBUG
                # request-line can't leak either.
                if DEBUG:
                    print(
                        f"consent: connection error: {type(exc).__name__}",
                        flush=True,
                    )
            finally:
                # Disconnected (clean close, error, or crash): the sidecar's
                # held connections die with it (fail-close). Any in-flight
                # request resolves to deny so its caller NXDOMAIN/DROPs.
                self._ws = None
                self._connected.clear()
                self._fail_close_pending()
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)  # capped exponential backoff

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "verdict":
            self._apply_verdict(msg)
        elif mtype == "drop_rule":
            await self._handle_drop_rule(msg)

    def _apply_verdict(self, msg: dict) -> None:
        vid = msg.get("id")
        decision = msg.get("decision")
        if not isinstance(vid, str):
            return
        fut = self._pending.pop(vid, None)
        if fut is not None and not fut.done():
            # Any non-"allow" verdict (deny, expired, malformed) -> deny.
            token = decision if decision == "allow" else "deny"
            duration = msg.get("duration") or "once"
            fut.set_result((token, duration))

    async def _handle_drop_rule(self, msg: dict) -> None:
        """klangkd asked us to drop a host's rules (revocation, #2339).

        Runs :func:`drop_for_host` off the loop (it forks iptables) and acks
        back so klangkd marks the verdict revoked only once the rule is gone.
        """
        ack_id = msg.get("id")
        host = msg.get("host")
        decision = msg.get("decision")
        ok = False
        if isinstance(host, str) and decision in ("allowed", "denied"):
            # #2370: close the session-allow gates BEFORE the executor window.
            # While drop_for_host forks iptables off-loop (~tens of ms), a racing
            # SYN/DNS could read _SESSION_HOST_ALLOWS and re-install a fresh
            # ACCEPT (the host's remaining allow TTL) the revoke never clears.
            # _drop_session_hosts runs on the loop (loop-only structure) and
            # makes _cb's _session_host_allows_ttl + ports_for deny during the
            # window. (A deny never adds to _SESSION_HOST_ALLOWS, so skip it
            # there.)
            if decision == "allowed":
                _drop_session_hosts(host)
            elif decision == "denied":
                # Clear the host-scoped deny memory (#2446) BEFORE drop_for_host
                # forks iptables, so a SYN arriving during that window re-prompts
                # instead of staying auto-denied (mirror of the allow revoke).
                _drop_session_denies(host)
            try:
                ips = await asyncio.get_running_loop().run_in_executor(
                    None, drop_for_host, host, decision
                )
                # Drop the host's cached verdicts on the loop AFTER the rule
                # removal (loop-only dict). A cache hit only pkt.accept()s a
                # retransmit -- it does not re-install an ACCEPT -- so this is
                # safe after the drop and needs no window protection.
                _clear_verdict_cache(ips)
                # The ack means "the rule is dropped" (#2339); the loop-state
                # clears above are pure dict ops and don't affect it.
                ok = True
            except Exception:
                ok = False
        if isinstance(ack_id, str) and self._ws is not None:
            try:
                await self._ws.send(
                    json.dumps({"type": "drop_ack", "id": ack_id, "ok": ok})
                )
            except Exception:
                pass

    async def request(self, dst: str, dport: int | None) -> tuple[str, str]:
        """Send an egress frame + await the verdict. Fail-close -> ``("deny",
        "once")``.

        Returns ``(decision, duration)`` where decision is ``"allow"`` or
        ``"deny"`` and duration is the verdict's duration token (#2328). A
        down connection returns the fail-close pair at once (no frame sent); a
        timed-out or disconnected-in-flight request resolves the same way.
        """
        if not self._connected.is_set() or self._ws is None:
            return "deny", "once"
        lid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[lid] = fut
        frame = json.dumps({"type": "egress", "id": lid, "dst": dst, "dport": dport})
        try:
            await self._ws.send(frame)
        except Exception:
            self._pending.pop(lid, None)
            return "deny", "once"
        try:
            return await asyncio.wait_for(fut, self._hold_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(lid, None)
            return "deny", "once"

    def _fail_close_pending(self) -> None:
        # A lost connection is a fresh session against a (possibly restarted)
        # coordinator, so prior verdicts must not be trusted: in-flight flows
        # re-prompt after reconnect instead of being silently re-allowed/denied
        # by a stale cached verdict (#2326 review).
        _VERDICT_CACHE.clear()
        for lid in list(self._pending):
            fut = self._pending.pop(lid, None)
            if fut is not None and not fut.done():
                fut.set_result(("deny", "once"))

    def _read_token(self) -> str:
        try:
            with open(self._token_path) as f:
                return f.read().strip()
        except OSError:
            return ""


async def _forward_and_learn(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    qname: str,
    port_set: set[int | None],
) -> None:
    """Forward a query wire to the upstream + learn/respond (allow path).

    Shared by the static-allow path and the consent-allow path (which passes
    ``{None}`` -- all-ports, like a port-less allow spec). Uses a non-blocking
    socket + the loop's sock_* helpers so the await yields to other holds +
    the WS receive loop while the upstream is pending.
    """
    loop = asyncio.get_running_loop()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    us.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
    us.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_sendto(us, data, UPSTREAM), 3)
        resp, _ = await asyncio.wait_for(loop.sock_recvfrom(us, 65535), 3)
    except Exception:
        return
    finally:
        us.close()  # closed on success, error, AND cancellation
    await _respond_allowed(s, resp, addr, qname, port_set)


async def _respond_recorded(
    s: socket.socket,
    resp: bytes,
    addr: tuple[str, int],
    qname: str,
) -> None:
    """Record the response's IP->host (NO ACCEPT install) + send it (#2324).

    The workspace gets the IP (can SYN) but the connection is consent-gated at
    the SYN (NFQUEUE), so the IP is NOT allow-learned here -- only the IP->host
    mapping is recorded so the NFQUEUE consumer can name the host. Mirrors
    :func:`_respond_allowed` minus the ACCEPT install.
    """
    loop = asyncio.get_running_loop()
    try:
        recs = a_records_with_ttl(resp)
    except Exception:
        recs = []
    try:
        if recs:
            await loop.run_in_executor(None, _record_hosts, recs, qname)
        if DEBUG:
            print(
                f"resolve {qname} -> {[ip for ip, _ in recs]} (consent at SYN)",
                flush=True,
            )
        s.sendto(resp, addr)
    except Exception:
        pass


async def _forward_and_record(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    qname: str,
) -> None:
    """Forward a non-allow-listed query upstream + respond, recording IP->host
    but NOT learning an ACCEPT (#2324).

    The workspace resolves the name (gets the IP, can SYN); the connection is
    consent-gated at the SYN (NFQUEUE) rather than held at the DNS query, so the
    human decision window is the kernel's connect timeout (~127s), not the
    resolver's <=30s getaddrinfo cap. Uses a non-blocking socket + the loop's
    sock_* helpers like :func:`_forward_and_learn`.
    """
    loop = asyncio.get_running_loop()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    us.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
    us.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_sendto(us, data, UPSTREAM), 3)
        resp, _ = await asyncio.wait_for(loop.sock_recvfrom(us, 65535), 3)
    except Exception:
        return
    finally:
        us.close()
    await _respond_recorded(s, resp, addr, qname)


def _send_nxdomain(s: socket.socket, data: bytes, addr: tuple[str, int]) -> None:
    try:
        s.sendto(nxdomain_for(data), addr)
    except Exception:
        pass


def _ones_checksum(data: bytes) -> int:
    """RFC 1071 ones-complement checksum over ``data`` (pad odd length)."""
    if len(data) % 2:
        data = data + b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += int.from_bytes(data[i : i + 2], "big")
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_rst_packet(
    src_ip: str, src_port: int, dst_ip: str, dst_port: int, seq: int
) -> bytes:
    """A 40-byte IPv4 TCP RST+ACK packet that forges the eager deny (#2345).

    ``src`` is the denied host (the workspace's remote end) so the RST matches
    the SYN_SENT socket's 4-tuple; ``dst`` is the workspace's local end.
    Replicates the kernel's SYN-to-closed-port RST: RST+ACK with ``seq=0`` and
    ``ack=seq+1`` (a SYN consumes one sequence number), so the workspace's
    SYN_SENT socket accepts it and ``connect()`` returns ECONNREFUSED. The TCP
    checksum is computed over the IPv4 pseudo-header + the TCP header; the IP
    checksum is left 0 for the kernel to fill (IP_HDRINCL). Pure so it can be
    unit-tested with synthetic bytes.
    """
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,  # version 4, IHL 5 (20-byte header)
        0,  # tos
        40,  # total length (20 IP + 20 TCP)
        0,  # id (kernel fills under IP_HDRINCL)
        0,  # flags + fragment offset
        64,  # ttl
        6,  # proto TCP
        0,  # IP checksum (kernel fills under IP_HDRINCL)
        socket.inet_aton(src_ip),
        socket.inet_aton(dst_ip),
    )
    doff_flags = (5 << 12) | 0x14  # data offset 5 (20 B) | RST(0x04) + ACK(0x10)
    tcp_no_cksum = struct.pack(
        "!HHIIHHHH",
        src_port,
        dst_port,
        0,  # seq
        (seq + 1) & 0xFFFFFFFF,  # ack
        doff_flags,
        0,  # window
        0,  # checksum placeholder
        0,  # urgent pointer
    )
    pseudo = (
        socket.inet_aton(src_ip)
        + socket.inet_aton(dst_ip)
        + struct.pack("!BBH", 0, 6, len(tcp_no_cksum))
    )
    cksum = _ones_checksum(pseudo + tcp_no_cksum)
    tcp_hdr = tcp_no_cksum[:16] + struct.pack("!H", cksum) + tcp_no_cksum[18:]
    return ip_hdr + tcp_hdr


# The raw socket used to forge the eager-deny RST (#2345). Opened lazily at
# startup (:func:`check_rst_socket`) with IP_HDRINCL so the denied host can be
# spoofed as the RST source (the workspace's SYN_SENT socket matches the remote
# tuple). ``None`` until then (also when consent is off or NET_RAW is absent);
# :func:`_send_rst` then no-ops and the REJECT rule is the only fail-fast path.
_RST_SOCK: socket.socket | None = None


def check_rst_socket() -> None:
    """Open the raw socket used to forge the eager-deny RST (#2345).

    Needs CAP_NET_RAW (the sidecar gets it). Best-effort: if the socket can't
    be opened, the REJECT rule remains the only fail-fast path (no behavior
    change). Logged at startup like :func:`check_mark`, and set non-blocking so
    :func:`_send_rst` is safe to call inline on the loop thread.
    """
    global _RST_SOCK
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
        s.setblocking(False)
        _RST_SOCK = s
    except OSError as exc:
        print(
            f"dns-proxy: cannot open RST socket ({exc}); eager-deny falls "
            "back to REJECT only (#2345)",
            flush=True,
        )


def _send_rst(payload: bytes) -> None:
    """Forge a RST to the workspace's SYN_SENT socket so ``connect()`` gets
    ECONNREFUSED at once, independent of conntrack/retransmit timing (#2345).

    Parses the held SYN's source end + seq, builds a RST sourced from the
    denied host, and sends it via the IP_HDRINCL raw socket (the dst is the
    workspace's own address, so the kernel routes it to lo and loops it back to
    the local TCP stack). No-op if the socket isn't open (consent disabled / no
    NET_RAW) -- then the REJECT rule is the only fail-fast path. Non-blocking
    (the socket is non-blocking; a 40-byte sendto won't block), so it is safe
    inline on the loop thread. Swallows errors: a transient send failure just
    leaves the REJECT backstop.
    """
    sock = _RST_SOCK
    if sock is None:
        _rst_debug("rst-forge: no raw socket (NET_RAW?) -- REJECT-only fast-refuse")
        return
    src_ip, src_port, dst_ip, dst_port, seq = parse_syn_tuple(payload)
    if not src_ip or not dst_port:
        _rst_debug(f"rst-forge: unparseable tuple (src={src_ip} dst_port={dst_port})")
        return
    try:
        sock.sendto(
            # RST source = the denied host (dst of the SYN); dest = the
            # workspace's local end (src of the SYN). sendto routes to the
            # workspace's local address so it loops back to local INPUT.
            build_rst_packet(dst_ip, dst_port, src_ip, src_port, seq),
            (src_ip, 0),
        )
        _rst_debug(
            f"rst-forge: sent {dst_ip}:{dst_port} -> {src_ip}:{src_port} "
            f"ack={(seq + 1) & 0xFFFFFFFF}"
        )
    except OSError as exc:
        _rst_debug(f"rst-forge: sendto failed: {exc!r}")


def _setup_nfq_consumer(client: SidecarConsentClient | None):
    """Bind the sidecar's NFQUEUE + drive it from the event loop (#2324, #2329).

    Consent gates the connection SYN, not the DNS query: a non-allow-listed name
    resolves (the workspace gets the IP) and the first packet to that IP is
    queued here pending a verdict -- so the human decision window is the kernel's
    connect timeout (tcp_syn_retries ~= 127s), not the resolver's <=30s
    getaddrinfo cap.

    The queue is read on the event-loop thread via ``get_fd()`` + ``add_reader``
    (netfilterqueue is otherwise synchronous). The per-packet callback
    (:func:`_cb`) is **non-blocking**: it retains the packet + hands the verdict
    wait to a task (:func:`_decide_and_verdict`), so a slow verdict on one flow
    does NOT serialize others -- distinct flows are held concurrently
    (netfilterqueue supports deferred verdicts; outstanding packets count
    against the kernel queue size, and the iptables rate-limit bounds arrivals).
    netfilterqueue is a sidecar-only dep, imported lazily so the module loads
    without it. Returns the bound ``NetfilterQueue`` (so :func:`_shutdown` can
    unbind it on SIGTERM, #2400) or ``None`` on failure.
    """
    try:
        from netfilterqueue import NetfilterQueue
    except Exception as exc:
        print(f"nfqueue: netfilterqueue unavailable ({exc})", flush=True)
        return None
    try:
        nfq = NetfilterQueue()
        nfq.bind(QUEUE_NUM, lambda pkt: _cb(pkt, client))
        loop = asyncio.get_running_loop()
        # When the netlink socket is readable, process all pending packets on
        # this (loop) thread; _cb then hands each off to a verdict task.
        loop.add_reader(nfq.get_fd(), lambda: _drain(nfq))
        print(f"nfqueue consumer bound to queue {QUEUE_NUM}", flush=True)
        return nfq
    except Exception as exc:
        print(f"nfqueue consumer failed: {exc}", flush=True)
        return None


def _drain(nfq) -> None:
    """Process pending NFQUEUE messages (called when the socket is readable)."""
    try:
        nfq.run(block=False)
    except Exception:
        pass  # a transient read failure defers to the next readability event


def _cb(pkt, client: SidecarConsentClient | None) -> None:
    """Classify + route one queued SYN -- non-blocking (#2324, #2329).

    A retransmit of an already-decided connection reuses the cached verdict
    inline (fast path). A new connection is retained + handed to
    :func:`_decide_and_verdict` so the verdict wait doesn't block the queue's
    drain (distinct connections are held concurrently, not serialized behind
    the first).
    """
    payload = pkt.get_payload()
    # Connection-level key (src IP+port + dst IP+port): a SYN retransmit shares
    # its connection's tuple, a NEW connection has a new source port. Keying the
    # verdict cache + in-flight set on the connection -- not just (dst, port) --
    # is what makes duration=once re-prompt every subsequent connection instead
    # of reusing a prior allow for the same destination (#2361).
    src_ip, src_port, tdst, tport, _ = parse_syn_tuple(payload)
    if tport:  # TCP: full connection tuple
        dst, port = tdst, tport
    else:  # non-TCP (UDP/other): no source port -> destination granularity
        dst, port = parse_dest(payload)
    if not dst or client is None:
        pkt.drop()  # unparseable / pure-static (no consent configured) -> drop
        return
    if not client.connected:
        # Consent WS down: consent is unavailable, so fail the off-list SYN
        # FAST (ECONNREFUSED) like a deny verdict -- NOT a bare drop. A bare
        # drop makes the kernel retransmit the SYN for ~127s (tcp_syn_retries),
        # dangling the connection (#2308: no consent available -> a clean,
        # prompt denial, not a hang). Forge the eager-deny RST inline
        # (non-blocking sendto) so THIS connect() gets ECONNREFUSED at once,
        # + a short REJECT (tcp-reset) backstop off-loop so retransmits are
        # RST'd above NFQUEUE, then drop. On-list egress is unaffected
        # (learned ACCEPT rules sit above NFQUEUE); once the WS reconnects,
        # fresh off-list egress prompts again. The REJECT TTL is short, so no
        # rule lingers past the outage (#2413). Unlike a `once` deny (#2463),
        # this REJECT stays destination-scoped deliberately: during a WS outage
        # no connection can be consented, so fail-fast (ECONNREFUSED) for every
        # off-list connect to the same ip:port is the intended #2308 behavior,
        # and this path writes no _VERDICT_CACHE entry, so connection-scoping
        # would gain nothing.
        if port:
            _send_rst(payload)
            asyncio.get_running_loop().run_in_executor(
                None, reject, dst, port, CONSENT_REJECT_TTL
            )
        pkt.drop()
        return
    flow = (src_ip, src_port, dst, port)
    now = time.time()
    # SYN retransmit of an already-decided connection -> reuse the verdict so
    # the kernel's retransmits (tcp_syn_retries) don't each re-prompt.
    cached = _VERDICT_CACHE.get(flow)
    if cached is not None and cached[1] > now:
        if cached[0] == "allow":
            pkt.accept()
        else:
            # Forge the eager-deny RST (a retried connect() to a denied flow
            # fails fast too), then drop. TCP only (port 0 is non-TCP).
            if port:
                _send_rst(payload)
            pkt.drop()
        return
    # A SYN retransmit that arrives WHILE this connection is still held (no
    # verdict yet) must not spawn a second consent request: the in-flight task
    # resolves it, and a request per retransmit piles up duplicates that linger
    # past the first's resolution (#2345 e2e flake). Drop it -- the kernel sends
    # another retransmit once the verdict lands, and that one hits the cache.
    if flow in _INFLIGHT:
        pkt.drop()
        return
    host = _host_for(dst)  # DNS name if resolved here, else the IP
    # An in-session host allow covers the whole domain, timed or forever
    # (#2372, #2434): a SYN to a host:port the user already allowed -- including
    # a CDN-rotated or resolver-cached IP that no fresh DNS resolution
    # re-ACCEPTed -- is auto-allowed here, the last gate before prompting, so
    # the user isn't re-asked for a domain they allowed. This is the fix for the
    # ALLOW-REFUSED mismatch (#2434): without it a timed allow only covered the
    # IP resolved at decision time, so a CDN-rotated IP re-entered NFQUEUE and a
    # hold timeout there fail-closed to a deny REJECT an in-effect allow should
    # override.
    remaining = _session_host_allows_ttl(host, port) if port else None
    if remaining is not None:
        # allow() forks iptables under _LOCK -- run it off the loop thread (the
        # file's invariant; every other allow/learn/reject call is in the
        # executor). Fire-and-forget: conntrack ESTABLISHED,RELATED carries THIS
        # connection after pkt.accept(); the ACCEPT rule only helps FUTURE
        # connections, and the _VERDICT_CACHE write below covers retransmits.
        # Learn for the allow's remaining window (timed) or ~forever; port-scoped
        # (the consented port) -- deliberately stricter than the consent-allow
        # path's all-ports learn (allow(dst, None, ...)).
        asyncio.get_running_loop().run_in_executor(None, allow, dst, port, remaining)
        pkt.accept()
        _VERDICT_CACHE[flow] = ("allow", now + VERDICT_CACHE_TTL)
        # NOTE (#2370): revoking an allow must also drop this host from
        # _SESSION_HOST_ALLOWS and clear _VERDICT_CACHE, or a revoked allow keeps
        # passing (ports_for re-allow-lists it + this cache reuses the verdict).
        # Wired with the revoke path in #2370.
        return
    # An in-session host deny covers the whole domain, timed or forever
    # (#2446): a SYN to a host:port the user already denied -- including a
    # CDN-rotated or resolver-cached IP that the per-IP _REJECTED rule does not
    # cover -- is denied fast here, before prompting, so the user isn't re-asked
    # for a domain they denied (the CARRYOVER-SURPRISE). Checked AFTER the allow
    # gate above so an in-effect allow still overrides an in-effect deny.
    deny_remaining = _session_host_denies_ttl(host, port) if port else None
    if deny_remaining is not None:
        # Forge the eager-deny RST so connect() fails fast (ECONNREFUSED) at
        # once; a REJECT for the deny's remaining window backstops retransmits
        # off-loop (reject() forks iptables under _LOCK). TCP only (port 0 is
        # non-TCP). The _VERDICT_CACHE write covers retransmits of THIS flow.
        if port:
            try:
                _send_rst(payload)
            except Exception:
                pass
            asyncio.get_running_loop().run_in_executor(
                None, reject, dst, port, deny_remaining
            )
        pkt.drop()
        _VERDICT_CACHE[flow] = ("deny", now + VERDICT_CACHE_TTL)
        return
    pkt.retain()  # keep the payload valid past this callback (deferred verdict)
    _INFLIGHT.add(flow)
    t = asyncio.create_task(_decide_and_verdict(pkt, flow, dst, port, host, client))
    _BG_TASKS.add(t)  # strong ref so the verdict task isn't GC'd
    t.add_done_callback(_BG_TASKS.discard)


async def _decide_and_verdict(
    pkt,
    flow: tuple[str, int, str, int],
    dst: str,
    port: int,
    host: str,
    client: SidecarConsentClient,
) -> None:
    """Await the consent verdict for a held SYN + apply it (deferred).

    ``allow`` -> learn the IP all-ports (reconnects + this connection's SYN
    retransmits pass without re-prompting) + ``pkt.accept()`` (conntrack
    ESTABLISHED,RELATED carries the in-flight connection); ``deny``/timeout/
    WS-down -> ``pkt.drop`` + a temporary REJECT (tcp-reset) rule so the next
    retransmit gets RST'd (ECONNREFUSED) instead of the kernel retransmitting
    for ~127s. The verdict is cached against ``flow`` (the connection tuple) so
    SYN retransmits (tcp_syn_retries) of THIS connection reuse it -- a new
    connection (new source port) is a cache miss and re-prompts, which is what
    makes duration=once per-connection (#2361).
    """
    try:
        decision, duration = await client.request(host, port)
    except Exception:
        decision, duration = "deny", "once"  # timeout / loop dead -> fail-close
    now = time.time()
    # Bound memory under a denied-flow flood (allowed flows get learned +
    # stop hitting NFQUEUE; only denied flows accumulate in the cache).
    if len(_VERDICT_CACHE) > 4096:
        _VERDICT_CACHE.clear()
    _VERDICT_CACHE[flow] = (decision, now + VERDICT_CACHE_TTL)
    # Populate the in-session allow-list BEFORE the iptables fork below (which
    # yields to the executor): a SYN to a different IP of this host arriving
    # during that yield would otherwise miss _SESSION_HOST_ALLOWS and re-prompt.
    # Both gates (ports_for + _cb) read it (#2372). A timed allow is host-scoped
    # too (#2434): otherwise a CDN-rotated IP of a timed-allowed host re-enters
    # NFQUEUE, and a hold timeout there fail-closes to a deny REJECT an
    # in-effect allow should override -- an allow that refuses.
    ttl = _duration_ttl(duration)
    if decision == "allow" and ttl is not None and port:
        _add_session_host(host, port, ttl)
    # Symmetric host-scoped memory on the deny side (#2446): a timed/forever
    # deny is remembered by host (not just IP) so a retry -- including a
    # CDN-rotated IP -- is denied fast without re-prompting. ``once`` (ttl
    # None) adds nothing (per-connection). Populated before the iptables fork
    # below so a SYN to a different IP of this host during the yield still hits
    # _session_host_denies_ttl in _cb.
    if decision == "deny" and ttl is not None and port:
        _add_session_deny(host, port, ttl)
    # Run the iptables fork (allow/reject) in the executor so it doesn't block
    # the loop thread -- matches the DNS path's _learn_all, which also runs
    # off the loop. The packet is retained, so verdicting after the await is
    # safe; the rule is installed before the SYN is released.
    loop = asyncio.get_running_loop()
    try:
        if decision == "allow":
            # `once` (ttl None) -> no learn, just this connection (reconnect
            # re-prompts); a timed duration -> learn all-ports for it.
            if ttl is not None:
                try:
                    await loop.run_in_executor(None, allow, dst, None, ttl)
                except Exception:
                    pass
            pkt.accept()
        else:
            # Forge a RST directly so connect() fails fast (ECONNREFUSED) at once,
            # independent of the conntrack/retransmit race that made the REJECT
            # rule flaky (#2345). The REJECT rule stays as a belt-and-suspenders
            # backstop for any retransmit the RST missed. `once` -> the short
            # fail-close reject window; a timed duration -> that long.
            # ``once`` is per-connection: scope the REJECT to THIS connection's
            # source port so a NEW connection (different sport) to the same
            # host:port re-prompts instead of being rejected above NFQUEUE for
            # the fail-close window (#2463). A timed/forever deny stays
            # destination-scoped -- its over-deny is correct (the DB rule +
            # _SESSION_HOST_DENIES govern re-prompting). The source port is
            # guarded: a real TCP SYN never has src port 0 (RFC 793), so a
            # truthy flow[1] is the normal case; if it is ever 0 (a
            # non-TCP/unparseable flow), fall back to destination-scoped so a
            # future parse change can't silently re-introduce the over-deny.
            if port:
                try:
                    _send_rst(pkt.get_payload())
                except Exception:
                    pass
                reject_ttl = ttl if ttl is not None else CONSENT_REJECT_TTL
                try:
                    if ttl is None and flow[1]:
                        await loop.run_in_executor(
                            None, reject, dst, port, reject_ttl, flow[1]
                        )
                    else:
                        await loop.run_in_executor(None, reject, dst, port, reject_ttl)
                except Exception:
                    pass
            pkt.drop()
    finally:
        # Connection resolved (verdict cached) -> retransmits now hit the cache,
        # not the in-flight check. Always discard, even if the verdict raised,
        # so a stuck connection can't block the tuple forever.
        _INFLIGHT.discard(flow)


async def _handle_packet(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    client: SidecarConsentClient | None,
) -> None:
    """Classify + route one DNS query (the per-packet body of :func:`_async_main`).

    Allow-listed names forward + learn (ACCEPT). A denied name in interactive
    mode (a consent client) resolves + responds + records IP->host but installs
    NO ACCEPT -- its connection SYN is consent-gated at NFQUEUE (#2324), so the
    human decision window is the kernel's connect timeout (~127s), not the
    resolver's <=30s getaddrinfo cap. Static mode (no client) -> NXDOMAIN.
    """
    try:
        qname = query_name(data)
    except Exception:
        return  # malformed/unparseable query -> drop
    # Static deny-list (#2367): a rejected name is NXDOMAIN'd unconditionally,
    # in BOTH static and interactive modes, and takes precedence over the
    # allow-list + consent (a name in both allowed + rejected is rejected).
    if rejected_for(qname):
        if DEBUG:
            print(f"reject {qname}", flush=True)
        _send_nxdomain(s, data, addr)
        return
    ports = ports_for(qname)
    deny, port_set = _decision(qname, ports)
    if deny:
        if DEBUG:
            print(f"deny  {qname}", flush=True)
        if client is not None:
            # Interactive: resolve + respond + record IP->host; the SYN is
            # consent-gated at NFQUEUE (not held here at the DNS query).
            await _forward_and_record(s, data, addr, qname)
        else:
            _send_nxdomain(s, data, addr)
        return
    await _forward_and_learn(s, data, addr, qname, port_set)


# Bound on the consent client's teardown during SIGTERM shutdown (#2400):
# client.stop() closes the WS (close_timeout=5s), and during klangkd shutdown
# the server may be going away, so an unbounded close handshake could
# re-introduce the 5s window this fix eliminates. Bounded so the whole
# teardown fits well inside podman's `stop -t 5`.
_SHUTDOWN_CLIENT_TIMEOUT = 2.0


async def _shutdown(
    client: SidecarConsentClient | None,
    nfq,
    sock: socket.socket,
    sweep: asyncio.Task | None,
) -> None:
    """Clean teardown on SIGTERM (#2400): stop the consent client, cancel the
    TTL sweeper, unbind NFQUEUE, close the DNS socket.

    The consent client's stop is bounded (:data:`_SHUTDOWN_CLIENT_TIMEOUT`) so a
    stalled WebSocket close handshake can't re-introduce the 5s window. Best-
    effort — a failure (or the bound) in one step must not skip the rest
    (process exit reaps whatever remains). Runs in :func:`_async_main`'s
    ``finally`` after the SIGTERM handler cancels the main task, so the proxy
    exits promptly instead of relying on podman's SIGKILL fallback — which a
    PID-1 sidecar always hit, because the kernel ignores default SIGTERM
    dispositions for a PID-namespace init (SIGNAL_UNKILLABLE: a fatal signal
    with no explicit handler is skipped for init).
    """
    if client is not None:
        try:
            await asyncio.wait_for(client.stop(), _SHUTDOWN_CLIENT_TIMEOUT)
        except Exception:
            pass
    if sweep is not None:
        sweep.cancel()
        try:
            await sweep
        except (asyncio.CancelledError, Exception):
            pass
    if nfq is not None:
        try:
            asyncio.get_running_loop().remove_reader(nfq.get_fd())
        except Exception:
            pass
        try:
            nfq.unbind()
        except Exception:
            pass
    try:
        sock.close()
    except Exception:
        pass


async def _async_main() -> None:
    """The asyncio DNS loop (#2311 half B, #2324): allow-listed + denied names
    resolve inline; a denied name in interactive mode records IP->host so its
    connection SYN is consent-gated at NFQUEUE (a separate thread).

    Installs an explicit SIGTERM handler (#2400) so podman's ``stop`` signal
    triggers a clean, prompt teardown instead of being ignored (the sidecar is
    PID 1, and the kernel suppresses default terminate dispositions for init).
    """
    loop = asyncio.get_running_loop()
    client: SidecarConsentClient | None = None
    if CONSENT_URL:
        client = SidecarConsentClient(CONSENT_URL, WORKSPACE_TOKEN_PATH, HOLD_TIMEOUT)
        await client.start()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", LISTEN_PORT))
    s.setblocking(False)
    print(
        f"dns-proxy listening on 127.0.0.1:{LISTEN_PORT} "
        f"(upstream={UPSTREAM[0]}, allowed={SPECS})",
        flush=True,
    )
    check_mark()
    _sweep = asyncio.create_task(_async_sweeper())
    _BG_TASKS.add(_sweep)  # strong ref for the loop's lifetime
    _sweep.add_done_callback(_BG_TASKS.discard)
    nfq = None
    # NFQUEUE consumer is driven by this event loop (get_fd + add_reader) so a
    # slow verdict on one SYN doesn't serialize others (#2324, #2329).
    if CONSENT_URL:
        check_rst_socket()  # eager-deny RST forge (#2345); best-effort (NET_RAW)
        nfq = _setup_nfq_consumer(client)  # bound NFQUEUE, for _shutdown (#2400)
    # The sidecar is PID 1 (entrypoint.sh execs python). The kernel suppresses
    # default terminate/stop dispositions for a PID-namespace init: a SIGTERM
    # with no handler installed is effectively ignored, so podman's `stop -t 5`
    # SIGTERM was no-op'd and EVERY removal fell back to SIGKILL after the full
    # 5s window (occasionally wedging in Stopping). Install an explicit handler
    # that cancels this task -> _shutdown closes the WS, unbinds NFQUEUE, closes
    # the socket -> prompt exit (#2400). (SIGKILL/SIGSTOP bypass this and always
    # work, which is why podman's SIGKILL fallback eventually cleared it.)
    main_task = asyncio.current_task()
    stopping = False

    def _on_sigterm() -> None:
        # Idempotent: a second SIGTERM arriving while _shutdown is mid-await
        # must NOT re-cancel the main task -- that CancelledError is a
        # BaseException, so _shutdown's `except Exception` guards don't catch
        # it and teardown would be aborted (skipping nfq.unbind/sock.close).
        # The first signal cancels; subsequent ones are no-ops (SIGKILL remains
        # podman's hard backstop if teardown hangs) (#2400).
        nonlocal stopping
        if stopping:
            return
        stopping = True
        if main_task is not None:
            main_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except (NotImplementedError, RuntimeError):
        pass  # signal handlers need the main thread + a supported loop backend
    try:
        while True:
            try:
                data, addr = await loop.sock_recvfrom(s, 65535)
            except Exception:
                continue
            await _handle_packet(s, data, addr, client)
    except asyncio.CancelledError:
        if DEBUG:
            print("dns-proxy: stop signal received, shutting down", flush=True)
    finally:
        await _shutdown(client, nfq, s, _sweep)


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
