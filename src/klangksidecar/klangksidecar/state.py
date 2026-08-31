"""Shared mutable state for the network sidecar's DNS proxy (#2450).

Every in-process mutable container lives here so each submodule binds the
SAME object (``from .state import LEARNED``). All are mutated in place
(dict/list/set ops) and never rebound, which is what makes cross-module
sharing by import safe. (The one rebound global, _RST_SOCK, lives in
packets.py next to its mutator.)
"""

from __future__ import annotations

import asyncio
import threading

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
# swept off-loop by sweep_once under LOCK like LEARNED/REJECTED).
SESSION_HOST_ALLOWS: list[tuple[str, int | None, str, float]] = []


# Hosts denied in-session by a consent ``deny`` verdict, timed or forever
# (#2446 -- the deny-side mirror of :data:`SESSION_HOST_ALLOWS`). Each entry is
# (host, port, mode, expire). On a deny whose duration is not ``once``,
# _decide_and_verdict adds the denied host:port here (expire = now + the
# verdict's TTL) so _cb (via _session_host_denies_ttl) denies a retry fast --
# without re-prompting -- even when a CDN rotation or a DNS-TTL lapse means the
# per-IP REJECTED rule no longer covers the destination. This is the fix for
# the CARRYOVER-SURPRISE finding (#2446): a timed deny used to cover only the
# IP resolved at decision time (plus a ~10s REJECT_TTL), so a rotated IP
# re-entered NFQUEUE and re-prompted the user for a host they had already
# denied. ``once`` adds nothing (per-connection, so a reconnect re-prompts).
# Dies with the sidecar (in-memory); revocation (drop rule, decision=denied)
# clears it via _drop_session_denies. Touched only on the event-loop thread
# (like SESSION_HOST_ALLOWS) -- no lock. Timed entries expire lazily via
# _prune_session_denies. An in-effect allow still wins: _cb consults
# _session_host_allows_ttl BEFORE _session_host_denies_ttl, so an allow
# overrides an in-effect deny.
SESSION_HOST_DENIES: list[tuple[str, int | None, str, float]] = []


# Learned IPs: {ip: {"expire": epoch, "ports": set[int | None], "host": str}}.
# ``ports`` holds the ACCEPT rule ports (a ``None`` is all-ports); ``host`` is
# the DNS name that resolved to this IP (named in the consent request). An
# entry can be host-only (ports empty, no ACCEPT) -- recorded when a
# non-allow-listed name resolves so its connection SYN is consent-gated at
# NFQUEUE (#2324). Guarded by LOCK: the asyncio loop runs the iptables installs
# + sweeps in the default thread-pool executor (see _learn_all / _async_sweeper),
# so two worker threads can touch LEARNED at once; the lock serializes the
# rule+record mutations. The NFQUEUE consumer reads ``host`` via _host_for
# (under the lock; the host is set by the DNS loop before the SYN arrives).
LEARNED: dict[str, dict] = {}


LOCK = threading.Lock()


# Reused SYN verdicts, keyed by connection (src IP+port, dst IP+port):
# {(src_ip, src_port, dst, port): (verdict, expire)} so the kernel's SYN
# retransmits (tcp_syn_retries) of an already-decided connection reuse the
# verdict without re-prompting. The connection key (not just dst) is what makes
# a NEW connection -- new source port -- re-prompt, so duration=once is truly
# per-connection (#2361). Touched only on the event-loop thread (the NFQUEUE
# consumer is loop-driven) -- no lock.
VERDICT_CACHE: dict[tuple[str, int, str, int], tuple[str, float]] = {}


# Connections with a verdict task still in flight (held, no verdict yet):
# keyed by the same connection tuple, prevents a SYN retransmit during the
# hold from spawning a SECOND consent request. The post-verdict
# :data:`VERDICT_CACHE` covers retransmits after a decision; this covers the
# window before it (#2345 e2e: retransmit-pileup left duplicate pending
# requests lingering past the first's resolution). Touched only on the
# event-loop thread -- no lock. Added in :func:`_cb`, discarded in
# :func:`_decide_and_verdict` once the verdict is cached.
INFLIGHT: set[tuple[str, int, str, int]] = set()


# Temporary REJECT (tcp-reset) rules for denied connections, keyed by
# (ip, port, sport): sport=0 is destination-scoped (catches every connection to
# ip:port, used for a timed/forever deny whose over-deny is intended + the
# WS-down fail-close); a nonzero sport is connection-scoped (catches only
# retransmits of THAT connection, used for a ``once`` deny so a NEW connection
# to the same host:port re-prompts instead of being rejected above NFQUEUE,
# #2463). Swept by sweep_once alongside LEARNED. Makes a deny fail-fast
# (ECONNREFUSED) instead of waiting for tcp_syn_retries (~127s) -- dropping a
# SYN alone doesn't fail connect(); the kernel just retransmits until its own
# timeout.
REJECTED: dict[tuple[str, int, int], float] = {}


# Strong refs to background asyncio tasks (the TTL sweeper) so CPython doesn't
# GC a sleeping task. A done-callback discards each entry on completion.
BG_TASKS: set[asyncio.Task] = set()


def clear_verdict_cache(ips: set[str]) -> None:
    """Drop cached SYN verdicts for a revoked host's IPs (#2370).

    Called on the **event loop** by :meth:`SidecarConsentClient.handle_drop_rule`
    AFTER :func:`drop_for_host` (which returned the host's candidate IPs).
    :data:`VERDICT_CACHE` is keyed by ``(src_ip, src_port, dst, port)``; dst is
    ``key[2]``. A cache hit only ``pkt.accept()``s a retransmit -- it does NOT
    re-install an ACCEPT rule (see :func:`_cb`) -- so this needs no
    pre-``drop_for_host`` window protection and may run after the drop.
    Loop-only dict (no lock). If a host's IP was already TTL-swept from
    :data:`LEARNED` before the revoke, it is absent from ``ips`` and its cache
    entry self-expires at the ``VERDICT_CACHE`` TTL (harmless: no ACCEPT rule,
    so a new flow re-prompts).
    """
    if ips:
        for key in [c for c in VERDICT_CACHE if c[2] in ips]:
            del VERDICT_CACHE[key]
