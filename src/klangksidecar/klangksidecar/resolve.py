"""DNS wire helpers + the per-query server decision/respond/forward paths (#2450).

query_name / a_records_with_ttl / nxdomain_for wrap dnspython; _decision
classifies a query; _respond_allowed / _forward_and_learn learn + reply (allow
path), _respond_recorded / _forward_and_record record IP->host without an
ACCEPT (consent-at-SYN path, #2324); _handle_packet routes one query.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

import dns.message
import dns.rdatatype
import dns.rcode

from . import allowlist, rules
from .allowlist import ports_for, rejected_for
from .config import DEBUG, MARK, UPSTREAM
from .rules import _fmt_ports, _learn_all

if TYPE_CHECKING:
    from .consent import SidecarConsentClient


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
    try:
        if recs:
            # Bound a timed session-allow's learned rule at its verdict's
            # remaining window (#2465): without this the DNS-path learn uses
            # the response's DNS TTL (often minutes), so a 5s allow leaves a
            # rule that outlives it and a retry past the window connects with
            # no re-prompt. None for a static spec (forever) or no session
            # allow -- the DNS TTL is correct then. Computed here (inside the
            # try so a raise can't escape _respond_allowed and take down the
            # PID-1 sidecar, #2278) on the loop (reads loop-only
            # _SESSION_HOST_ALLOWS) before the executor fork below.
            cap = allowlist._session_allow_rule_cap(qname)
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


async def _forward_marked(data: bytes) -> bytes | None:
    """Forward a DNS wire to UPSTREAM on a fwmark'd non-blocking socket.

    The shared preamble of :func:`_forward_and_learn` and
    :func:`_forward_and_record` (#2554): a MARK'd UDP socket (so the
    entrypoint's rule exempt the proxy's own egress, #2264), send +
    bounded receive on the loop's sock_* helpers, closed on success,
    error, AND cancellation. Returns the response wire, or None on any
    upstream failure/timeout.
    """
    loop = asyncio.get_running_loop()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    us.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
    us.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_sendto(us, data, UPSTREAM), 3)
        resp, _ = await asyncio.wait_for(loop.sock_recvfrom(us, 65535), 3)
        return resp
    except Exception:
        return None
    finally:
        us.close()  # closed on success, error, AND cancellation


async def _forward_and_learn(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    qname: str,
    port_set: set[int | None],
) -> bool:
    """Forward a query wire to the upstream + learn/respond (allow path).

    Shared by the static-allow path and the consent-allow path (which passes
    ``{None}`` -- all-ports, like a port-less allow spec). Uses a non-blocking
    socket + the loop's sock_* helpers so the await yields to other holds +
    the WS receive loop while the upstream is pending. Returns True iff the
    upstream answered and the allow path ran -- the caller reports the
    outcome for egress auditing (#2304); an upstream failure is not an
    allow (nothing was resolved or learned).
    """
    resp = await _forward_marked(data)
    if resp is None:
        return False
    await _respond_allowed(s, resp, addr, qname, port_set)
    return True


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
            await loop.run_in_executor(None, rules._record_hosts, recs, qname)
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
    resp = await _forward_marked(data)
    if resp is None:
        return
    await _respond_recorded(s, resp, addr, qname)


def _send_nxdomain(s: socket.socket, data: bytes, addr: tuple[str, int]) -> None:
    try:
        s.sendto(nxdomain_for(data), addr)
    except Exception:
        pass


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

    Egress auditing (#2304) is unconditional (no opt-in setting): every
    outcome the DNS layer itself decides is reported to klangkd for
    recording -- ``allowed`` for a forwarded+learned allow-list /
    in-session-allowed name, ``denied`` for a reject-listed name (NXDOMAIN,
    both modes). A static off-list NXDOMAIN has no channel (a client-less
    sidecar has no WS). The interactive off-list path deliberately reports
    nothing here: the query resolves (not denied at this layer) and the
    decision point is the connection SYN, whose verdict -- human or policy --
    is already recorded by the coordinator via the ``egress`` frame.
    """
    try:
        qname = query_name(data)
    except Exception:
        return  # malformed/unparseable query -> drop
    # Every DNS query is outbound egress activity -- bump klangkd's idle timer
    # (flood-gated inside the client) so an egress-only workload is not reaped
    # (#2479). DNS is the broadest signal: every domain connect starts here,
    # including repeats to an already-allow-listed host whose connects then
    # bypass NFQUEUE entirely.
    if client is not None:
        client.bump_activity()
    # Static deny-list (#2367): a rejected name is NXDOMAIN'd unconditionally,
    # in BOTH static and interactive modes, and takes precedence over the
    # allow-list + consent (a name in both allowed + rejected is rejected).
    if rejected_for(qname):
        if DEBUG:
            print(f"reject {qname}", flush=True)
        _send_nxdomain(s, data, addr)
        if client is not None:
            client.record_dns("denied", qname)  # audit (#2304): policy deny
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
    if await _forward_and_learn(s, data, addr, qname, port_set):
        if client is not None:
            client.record_dns("allowed", qname)  # audit (#2304): policy allow
