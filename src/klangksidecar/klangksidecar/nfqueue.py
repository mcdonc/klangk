"""NFQUEUE consumer: consent-gate the connection SYN pending a verdict (#2324, #2329, #2450).

setup_nfq_consumer binds the queue + drives it from the event loop; cb
classifies one queued SYN (cached-verdict fast path / in-session host gates /
hand-off to decide_and_verdict); decide_and_verdict awaits the verdict and
applies it. netfilterqueue is imported lazily.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from . import packets, rules
from .allowlist import (
    add_session_deny,
    add_session_host,
    session_host_allows_ttl,
    session_host_denies_ttl,
)
from .config import CONSENT_REJECT_TTL, QUEUE_NUM, VERDICT_CACHE_TTL, duration_ttl
from .packets import parse_dest, parse_syn_tuple
from .rules import host_for
from .state import BG_TASKS, INFLIGHT, VERDICT_CACHE

# netfilterqueue is a sidecar-only optional dep ([nfqueue] extra: it links
# libnetfilter_queue, a C library, and may be absent in dev environments).
# Guarded module-scope import: importing the module works everywhere, and
# setup_nfq_consumer reports the absence at bind time.
try:
    from netfilterqueue import NetfilterQueue
except ImportError:  # the [nfqueue] extra is not installed
    NetfilterQueue = None

if TYPE_CHECKING:
    from .consent import SidecarConsentClient


def setup_nfq_consumer(client: SidecarConsentClient | None):
    """Bind the sidecar's NFQUEUE + drive it from the event loop (#2324, #2329).

    Consent gates the connection SYN, not the DNS query: a non-allow-listed name
    resolves (the workspace gets the IP) and the first packet to that IP is
    queued here pending a verdict -- so the human decision window is the kernel's
    connect timeout (tcp_syn_retries ~= 127s), not the resolver's <=30s
    getaddrinfo cap.

    The queue is read on the event-loop thread via ``get_fd()`` + ``add_reader``
    (netfilterqueue is otherwise synchronous). The per-packet callback
    (:func:`cb`) is **non-blocking**: it retains the packet + hands the verdict
    wait to a task (:func:`decide_and_verdict`), so a slow verdict on one flow
    does NOT serialize others -- distinct flows are held concurrently
    (netfilterqueue supports deferred verdicts; outstanding packets count
    against the kernel queue size, and the iptables rate-limit bounds arrivals).
    netfilterqueue is optional ([nfqueue] extra). Returns the bound
    ``NetfilterQueue`` (so :func:`shutdown` can unbind it on SIGTERM,
    #2400) or ``None`` on failure.
    """
    if NetfilterQueue is None:
        print("nfqueue: netfilterqueue not installed", flush=True)
        return None
    try:
        nfq = NetfilterQueue()
        nfq.bind(QUEUE_NUM, lambda pkt: cb(pkt, client))
        loop = asyncio.get_running_loop()
        # When the netlink socket is readable, process all pending packets on
        # this (loop) thread; cb then hands each off to a verdict task.
        loop.add_reader(nfq.get_fd(), lambda: drain(nfq))
        print(f"nfqueue consumer bound to queue {QUEUE_NUM}", flush=True)
        return nfq
    except Exception as exc:
        print(f"nfqueue consumer failed: {exc}", flush=True)
        return None


def drain(nfq) -> None:
    """Process pending NFQUEUE messages (called when the socket is readable)."""
    try:
        nfq.run(block=False)
    except Exception:
        pass  # a transient read failure defers to the next readability event


def classify_packet(payload: bytes) -> tuple[str, int, str, int] | None:
    """``(src_ip, src_port, dst_ip, dst_port)`` for a queued packet, or None
    when unparseable. The connection-level key (src IP+port + dst IP+port) is
    what makes duration=once re-prompt every subsequent connection instead of
    reusing a prior allow for the same destination (#2361)."""
    src_ip, src_port, tdst, tport, _ = parse_syn_tuple(payload)
    if tport:  # TCP: full connection tuple
        return src_ip, src_port, tdst, tport
    dst, port = parse_dest(payload)  # non-TCP: destination granularity
    if not dst:
        return None
    return src_ip, src_port, dst, port


def cb(pkt, client: SidecarConsentClient | None) -> None:
    """Classify + route one queued SYN -- non-blocking (#2324, #2329).

    A retransmit of an already-decided connection reuses the cached verdict
    inline (fast path). A new connection is retained + handed to
    :func:`decide_and_verdict` so the verdict wait doesn't block the queue's
    drain (distinct connections are held concurrently, not serialized behind
    the first).
    """
    # Every queued SYN is outbound egress activity -- bump klangkd's idle timer
    # (flood-gated inside the client) so an egress-only workload is not reaped
    # (#2479). First, before any classification: even a SYN we drop (cached
    # deny / retransmit) is real network activity.
    if client is not None:
        client.bump_activity()
    payload = pkt.get_payload()
    key = classify_packet(payload)
    if key is None or client is None:
        pkt.drop()  # unparseable / pure-static (no consent configured) -> drop
        return
    src_ip, src_port, dst, port = key
    if not client.connected:
        fail_fast_no_consent(pkt, payload, dst, port)
        return
    flow = (src_ip, src_port, dst, port)
    now = time.time()
    # SYN retransmit of an already-decided connection -> reuse the verdict so
    # the kernel's retransmits (tcp_syn_retries) don't each re-prompt.
    cached = VERDICT_CACHE.get(flow)
    if cached is not None and cached[1] > now:
        apply_cached_verdict(pkt, cached, payload, port)
        return
    # A SYN retransmit that arrives WHILE this connection is still held (no
    # verdict yet) must not spawn a second consent request: the in-flight task
    # resolves it, and a request per retransmit piles up duplicates that linger
    # past the first's resolution (#2345 e2e flake). Drop it -- the kernel sends
    # another retransmit once the verdict lands, and that one hits the cache.
    if flow in INFLIGHT:
        pkt.drop()
        return
    host = host_for(dst)  # DNS name if resolved here, else the IP
    if _session_gate_holds(pkt, flow, payload, dst, port, host, now):
        return
    pkt.retain()  # keep the payload valid past this callback (deferred verdict)
    INFLIGHT.add(flow)
    t = asyncio.create_task(decide_and_verdict(pkt, flow, dst, port, host, client))
    BG_TASKS.add(t)  # strong ref so the verdict task isn't GC'd
    t.add_done_callback(BG_TASKS.discard)


def _session_gate_holds(
    pkt, flow, payload, dst: str, port: int, host: str, now: float
) -> bool:
    """The last gates before prompting, both host-scoped. An in-session host
    ALLOW covers the whole domain, timed or forever (#2372, #2434): a SYN to
    a host:port the user already allowed -- including a CDN-rotated or
    resolver-cached IP that no fresh DNS resolution re-ACCEPTed -- is
    auto-allowed, the fix for the ALLOW-REFUSED mismatch (#2434): without it
    a timed allow only covered the IP resolved at decision time, so a
    CDN-rotated IP re-entered NFQUEUE and a hold timeout there fail-closed to
    a deny REJECT an in-effect allow should override.

    An in-session host DENY covers the whole domain too (#2446): a SYN to a
    host:port the user already denied -- including a CDN-rotated or
    resolver-cached IP that the per-IP REJECTED rule does not cover -- is
    denied fast, before prompting (the CARRYOVER-SURPRISE). Checked AFTER
    the allow gate so an in-effect allow still overrides an in-effect deny.

    Returns True when the SYN was handled."""
    remaining = session_host_allows_ttl(host, port) if port else None
    if remaining is not None:
        _allow_session_host(pkt, flow, dst, port, now, remaining)
        return True
    deny_remaining = session_host_denies_ttl(host, port) if port else None
    if deny_remaining is not None:
        deny_session_host(pkt, flow, payload, dst, port, now, deny_remaining)
        return True
    return False


def fail_fast_no_consent(pkt, payload, dst: str, port: int) -> None:
    """Consent WS down: consent is unavailable, so fail the off-list SYN
    FAST (ECONNREFUSED) like a deny verdict -- NOT a bare drop. A bare drop
    makes the kernel retransmit the SYN for ~127s (tcp_syn_retries),
    dangling the connection (#2308: no consent available -> a clean, prompt
    denial, not a hang). Forge the eager-deny RST inline (non-blocking
    sendto) so THIS connect() gets ECONNREFUSED at once, + a short REJECT
    (tcp-reset) backstop off-loop so retransmits are RST'd above NFQUEUE,
    then drop. On-list egress is unaffected (learned ACCEPT rules sit above
    NFQUEUE); once the WS reconnects, fresh off-list egress prompts again.
    The REJECT TTL is short, so no rule lingers past the outage (#2413).
    Unlike a `once` deny (#2463), this REJECT stays destination-scoped
    deliberately: during a WS outage no connection can be consented, so
    fail-fast (ECONNREFUSED) for every off-list connect to the same
    ip:port is the intended #2308 behavior, and this path writes no
    VERDICT_CACHE entry, so connection-scoping would gain nothing."""
    if port:
        packets.send_rst(payload)
        asyncio.get_running_loop().run_in_executor(
            None, rules.reject, dst, port, CONSENT_REJECT_TTL
        )
    pkt.drop()


def apply_cached_verdict(pkt, cached, payload, port: int) -> None:
    """A retransmit of an already-decided connection: reuse the verdict. A
    cached deny forges the eager-deny RST (a retried connect() to a denied
    flow fails fast too); TCP only (port 0 is non-TCP)."""
    if cached[0] == "allow":
        pkt.accept()
    else:
        if port:
            packets.send_rst(payload)
        pkt.drop()


def _allow_session_host(
    pkt, flow, dst: str, port: int, now: float, remaining: float
) -> None:
    """An in-session host allow covers the whole domain, timed or forever
    (#2372, #2434): a SYN to a host:port the user already allowed --
    including a CDN-rotated or resolver-cached IP that no fresh DNS
    resolution re-ACCEPTed -- is auto-allowed, the last gate before
    prompting, so the user isn't re-asked for a domain they allowed. This
    is the fix for the ALLOW-REFUSED mismatch (#2434): without it a timed
    allow only covered the IP resolved at decision time, so a CDN-rotated
    IP re-entered NFQUEUE and a hold timeout there fail-closed to a deny
    REJECT an in-effect allow should override."""
    # allow() forks iptables under LOCK -- run it off the loop thread (the
    # file's invariant; every other allow/learn/reject call is in the
    # executor). Fire-and-forget: conntrack ESTABLISHED,RELATED carries THIS
    # connection after pkt.accept(); the ACCEPT rule only helps FUTURE
    # connections, and the VERDICT_CACHE write below covers retransmits.
    # Learn for the allow's remaining window (timed) or ~forever; port-scoped
    # (the consented port) -- deliberately stricter than the consent-allow
    # path's all-ports learn (allow(dst, None, ...)).
    asyncio.get_running_loop().run_in_executor(
        None, rules.allow, dst, port, remaining, False
    )
    pkt.accept()
    VERDICT_CACHE[flow] = ("allow", now + VERDICT_CACHE_TTL)
    # NOTE (#2370): revoking an allow must also drop this host from
    # SESSION_HOST_ALLOWS and clear VERDICT_CACHE, or a revoked allow keeps
    # passing (ports_for re-allow-lists it + this cache reuses the verdict).
    # Wired with the revoke path in #2370.


def deny_session_host(
    pkt, flow, payload, dst: str, port: int, now: float, deny_remaining: float
) -> None:
    """An in-session host deny covers the whole domain, timed or forever
    (#2446). Forge the eager-deny RST so connect() fails fast
    (ECONNREFUSED) at once; a REJECT for the deny's remaining window
    backstops retransmits off-loop (reject() forks iptables under LOCK).
    TCP only (port 0 is non-TCP). The VERDICT_CACHE write covers
    retransmits of THIS flow."""
    if port:
        try:
            packets.send_rst(payload)
        except Exception:
            pass
        asyncio.get_running_loop().run_in_executor(
            None, rules.reject, dst, port, deny_remaining
        )
    pkt.drop()
    VERDICT_CACHE[flow] = ("deny", now + VERDICT_CACHE_TTL)


async def decide_and_verdict(
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
    if len(VERDICT_CACHE) > 4096:
        VERDICT_CACHE.clear()
    VERDICT_CACHE[flow] = (decision, now + VERDICT_CACHE_TTL)
    # Populate the in-session allow-list BEFORE the iptables fork below (which
    # yields to the executor): a SYN to a different IP of this host arriving
    # during that yield would otherwise miss SESSION_HOST_ALLOWS and re-prompt.
    # Both gates (ports_for + cb) read it (#2372). A timed allow is host-scoped
    # too (#2434): otherwise a CDN-rotated IP of a timed-allowed host re-enters
    # NFQUEUE, and a hold timeout there fail-closes to a deny REJECT an
    # in-effect allow should override -- an allow that refuses.
    ttl = duration_ttl(duration)
    remember_session_verdict(decision, host, port, ttl)
    loop = asyncio.get_running_loop()
    try:
        await apply_verdict(pkt, flow, dst, port, decision, ttl, loop)
    finally:
        # Connection resolved (verdict cached) -> retransmits now hit the cache,
        # not the in-flight check. Always discard, even if the verdict raised,
        # so a stuck connection can't block the tuple forever.
        INFLIGHT.discard(flow)


def remember_session_verdict(
    decision: str, host: str, port: int, ttl: float | None
) -> None:
    """Populate the in-session host memory BEFORE the iptables fork below
    (which yields to the executor): a SYN to a different IP of this host
    arriving during that yield would otherwise miss the gate and re-prompt.

    An allow is host-scoped (#2372; timed allows too, #2434): otherwise a
    CDN-rotated IP of a timed-allowed host re-enters NFQUEUE, and a hold
    timeout there fail-closes to a deny REJECT an in-effect allow should
    override -- an allow that refuses. The deny side is symmetric (#2446): a
    timed/forever deny is remembered by host (not just IP) so a retry --
    including a CDN-rotated IP -- is denied fast without re-prompting.
    ``once`` (ttl None) adds nothing (per-connection)."""
    if ttl is None or not port:
        return
    if decision == "allow":
        add_session_host(host, port, ttl)
    elif decision == "deny":
        add_session_deny(host, port, ttl)


async def apply_verdict(
    pkt,
    flow,
    dst: str,
    port: int,
    decision: str,
    ttl: float | None,
    loop,
) -> None:
    """Apply the decision to the held SYN, installing the kernel rule first
    (iptables forks run in the executor so they don't block the loop thread --
    matches the DNS path's learn_all; the packet is retained, so verdicting
    after the await is safe)."""
    if decision == "allow":
        # `once` (ttl None) -> no learn, just this connection (reconnect
        # re-prompts); a timed duration -> learn all-ports for it.
        if ttl is not None:
            try:
                await loop.run_in_executor(None, rules.allow, dst, None, ttl, False)
            except Exception:
                pass
        pkt.accept()
        return
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
    # SESSION_HOST_DENIES govern re-prompting). The source port is
    # guarded: a real TCP SYN never has src port 0 (RFC 793), so a
    # truthy flow[1] is the normal case; if it is ever 0 (a
    # non-TCP/unparseable flow), fall back to destination-scoped so a
    # future parse change can't silently re-introduce the over-deny.
    if port:
        try:
            packets.send_rst(pkt.get_payload())
        except Exception:
            pass
        reject_ttl = ttl if ttl is not None else CONSENT_REJECT_TTL
        try:
            if ttl is None and flow[1]:
                await loop.run_in_executor(
                    None, rules.reject, dst, port, reject_ttl, flow[1]
                )
            else:
                await loop.run_in_executor(None, rules.reject, dst, port, reject_ttl)
        except Exception:
            pass
    pkt.drop()
