"""FQDN allow/deny-list matching + in-session consent memory (#2450, #2377).

Static specs (parse_specs from KLANGKNETWORK_EGRESS_ALLOW / _REJECT) plus the
dynamic per-session host allow/deny lists a consent verdict installs
(#2372/#2434/#2446). ports_for / rejected_for are the DNS gate; the
_session_host_*_ttl helpers are the NFQUEUE gate's last-chance short-circuit.
"""

from __future__ import annotations

import os
import time

from . import (
    state,
)  # SESSION_HOST_ALLOWS/_DENIES read qualified so tests can reassign them

# Host-scope modes for an allow-list spec, nginx-style (#2377): a bare host is
# EXACT (apex only); a leading-dot ``.host`` is INCLUSIVE (apex + subdomains);
# ``*.host`` is SUBDOMAINS only. (Bare = exact is the breaking flip from the
# old "bare = apex+subdomains" model.) One definition shared by parse_specs /
# ports_for / session_host_allows_ttl.
EXACT = "exact"


INCLUSIVE = "inclusive"


SUBDOMAINS = "subdomains"


def _split_spec_port(spec: str) -> tuple[str, int | None]:
    """Split a trailing ``:port`` off a spec -> ``(host_part, port)``; the
    port is None when absent or not numeric (then the whole spec is the host,
    e.g. a bare IPv6 literal fragment)."""
    if ":" not in spec:
        return spec, None
    host_part, port_part = spec.rsplit(":", 1)
    if port_part.isdigit():
        return host_part, int(port_part)
    return spec, None


def _spec_scope(s: str) -> tuple[str, str]:
    """The nginx-style host-scope strip (#2377) -> ``(host, mode)``: a leading
    ``*.`` is SUBDOMAINS, a leading ``.`` is INCLUSIVE, bare is EXACT."""
    if s.startswith("*."):
        return s[2:], SUBDOMAINS
    if s.startswith("."):
        return s[1:], INCLUSIVE
    return s, EXACT


def _parse_one_spec(spec: str) -> tuple[str, int | None, str] | None:
    """One comma-separated entry -> ``(host, port, mode)``, or None when it
    is empty/a CIDR (CIDR specs are applied statically by the entrypoint)."""
    spec = spec.strip()
    if not spec or "/" in spec:
        return None
    spec, port = _split_spec_port(spec)
    s, mode = _spec_scope(spec.lower())
    if not s:
        return None
    return s, port, mode


def parse_specs(
    env_var: str = "KLANGKNETWORK_EGRESS_ALLOW",
) -> list[tuple[str, int | None, str]]:
    """Structured host specs from ``env_var`` (#2377, #2367).

    Each entry is ``(host, port, mode)``: ``mode`` is :data:`EXACT` (bare host,
    apex only), :data:`INCLUSIVE` (``.host``, apex + subdomains), or
    :data:`SUBDOMAINS` (``*.host``, subdomains only). ``port`` is ``None`` for
    all-ports. CIDR specs (``10.0.0.0/8``) are excluded — the entrypoint applies
    those statically. The grammar mirrors ``klangk.netfilter.parse_allowed_domains``.
    """
    out: list[tuple[str, int | None, str]] = []
    for spec in os.environ.get(env_var, "").split(","):
        parsed = _parse_one_spec(spec)
        if parsed is not None:
            out.append(parsed)
    return out


SPECS = parse_specs()


# Static deny-list specs from ``KLANGKNETWORK_EGRESS_REJECT`` (#2367): a name
# matching one of these is NXDOMAIN'd unconditionally (see :func:`rejected_for`).
REJECT_SPECS = parse_specs("KLANGKNETWORK_EGRESS_REJECT")


def host_matches(qname: str, host: str, mode: str) -> bool:
    """Does ``qname`` match ``host`` under nginx-style scope ``mode`` (#2377)?

    Shared by :func:`ports_for` (the DNS gate) and :func:`session_host_allows_ttl`
    (the NFQUEUE gate) so the two can't drift. :data:`EXACT` (bare host) matches
    the apex only; :data:`INCLUSIVE` (``.host``) matches apex + subdomains;
    :data:`SUBDOMAINS` (``*.host``) matches subdomains only. The suffix check
    requires a leading dot, so ``evilexample.com`` does NOT match
    ``example.com``.
    """
    if mode == SUBDOMAINS:
        return qname.endswith("." + host)
    if mode == INCLUSIVE:
        return qname == host or qname.endswith("." + host)
    return qname == host  # EXACT (and the safe default for an unknown mode)


def ports_for(qname: str) -> set[int] | None:
    """The ports a queried name is allowed on under :data:`SPECS` (#2377).

    ``None``  — a port-less spec matched (allow all ports).
    ``set()`` — nothing matched (deny).
    ``{443, ...}`` — allow exactly these TCP ports.

    Scope: a bare host is :data:`EXACT` (apex only); ``.host`` is
    :data:`INCLUSIVE` (apex + subdomains); ``*.host`` is :data:`SUBDOMAINS`
    (subdomains only, apex excluded).
    """
    ports: set[int] = set()
    _prune_session_allows()
    session = [(h, p, m) for (h, p, m, _exp) in state.SESSION_HOST_ALLOWS]
    for host, port, mode in (*SPECS, *session):
        if not host_matches(qname, host, mode):
            continue
        if port is None:
            return None  # an all-ports spec dominates
        ports.add(port)
    return ports


def rejected_for(qname: str) -> bool:
    """Does ``qname`` match a :data:`REJECT_SPECS` entry (#2367)?

    Parallel to :func:`ports_for` for the deny-list, but boolean -- a rejected
    name is NXDOMAIN'd unconditionally, so there is no port dimension. Matches
    via :func:`host_matches` (nginx-style scope): bare = apex only, ``.host`` =
    apex + subdomains, ``*.host`` = subdomains only.
    """
    return any(host_matches(qname, host, mode) for host, _port, mode in REJECT_SPECS)


def _prune_session_allows() -> None:
    """Drop expired in-session host allows (lazy sweep, #2434).

    :data:`SESSION_HOST_ALLOWS` is loop-only (no lock), so -- unlike
    :data:`LEARNED` / :data:`REJECTED`, which are swept off-loop by
    :func:`sweep_once` under :data:`LOCK` -- its timed entries expire here, on
    the loop, the next time a gate (:func:`ports_for`,
    :func:`session_host_allows_ttl`, :func:`add_session_host`) reads them.
    Cheap (the list is tiny -- one entry per consented host:port) and keeps the
    structure from growing unbounded across a long session.
    """
    now = time.time()
    state.SESSION_HOST_ALLOWS[:] = [t for t in state.SESSION_HOST_ALLOWS if t[3] > now]


def _add_session_entry(lst: list, host: str, port: int, ttl: float) -> None:
    """Add/refresh an EXACT ``(host, port)`` entry in *lst* (#2554).

    The shared body of :func:`add_session_host` (allows) and
    :func:`add_session_deny` (denies): deduped, a re-add refreshes the
    expiry (``max`` -- never shortens an unexpired entry). Loop-only
    (no lock). Callers prune first.
    """
    expire = time.time() + ttl
    spec = (host, port, EXACT)
    for i, (h, p, mode, _exp) in enumerate(lst):
        if (h, p, mode) == spec:
            lst[i] = (h, p, mode, max(_exp, expire))
            return
    lst.append((host, port, EXACT, expire))


def _entry_remaining(entry: tuple, host: str, port: int, now: float) -> float | None:
    """Remaining TTL an *lst* entry covers ``host:port``, or None (expired,
    no match, or port mismatch). Matches via :func:`host_matches`; the entry's
    port must match or be all-ports (``None``)."""
    h, p, mode, exp = entry
    if exp <= now:
        return None  # belt-and-suspenders: _prune ran above, but a just-expired
        # entry can survive the microseconds between its `now` and this one.
    if not host_matches(host, h, mode):
        return None
    if p != port and p is not None:
        return None
    return exp - now


def session_entry_ttl(lst: list, host: str, port: int) -> float | None:
    """Max remaining TTL of an entry in *lst* covering ``host:port``, or None.

    The shared body of :func:`session_host_allows_ttl` and
    :func:`session_host_denies_ttl`: matches via :func:`host_matches`
    (nginx-style scope; entries are added EXACT, so only the exact host
    matches, #2377); port must match (or the entry is all-ports).
    Loop-only (no lock). Callers prune first.
    """
    if not host:
        return None
    now = time.time()
    remainings = [
        r for e in lst if (r := _entry_remaining(e, host, port, now)) is not None
    ]
    return max(remainings, default=None)


def add_session_host(host: str, port: int, ttl: float) -> None:
    """Allow-list ``host:port`` in-session for a consent allow verdict (#2372,
    #2434).

    Adds ``(host, port, EXACT, now + ttl)`` to :data:`SESSION_HOST_ALLOWS` so
    :func:`ports_for` (the DNS gate) treats the host as allow-listed for the
    verdict's lifetime -- the DNS path then learns every resolved IP and allows
    it without NFQUEUE, so a CDN-rotated IP no longer re-prompts (or, if it
    still races NFQUEUE, :func:`session_host_allows_ttl` short-circuits it in
    :func:`cb`). EXACT scope: the user approved the specific qname they saw, so
    only that host (not its subdomains) is opened (#2377). Deduped; a re-allow
    of the same host:port refreshes the expiry (``max`` -- never shortens an
    unexpired entry). A timed allow (5s/5m/1h/tilrestart) is host-scoped just
    like ``forever`` (#2434); ``once`` carries no host-allow (per-connection, so
    a reconnect re-prompts). Loop-only (no lock).
    """
    _prune_session_allows()
    _add_session_entry(state.SESSION_HOST_ALLOWS, host, port, ttl)


def session_host_allows_ttl(host: str, port: int) -> float | None:
    """Remaining seconds an in-session allow covers ``host`` on ``port``, or
    ``None`` (#2372, #2434).

    Used by :func:`cb` as the last-chance gate before prompting: a SYN to a
    host:port the user allowed (timed or forever) -- including a CDN-rotated or
    resolver-cached IP that no fresh DNS resolution re-ACCEPTed -- is
    auto-allowed, learned for the allow's remaining window, so the user isn't
    re-asked (and a hold timeout can't fail-close a still-allowed host to a
    deny, #2434). Matches via :func:`host_matches` (nginx-style scope); entries
    are added EXACT by :func:`add_session_host`, so only the approved host
    matches (#2377). Returns the max remaining TTL across matching entries.
    Loop-only (no lock).
    """
    if not host:
        return None
    _prune_session_allows()
    return session_entry_ttl(state.SESSION_HOST_ALLOWS, host, port)


def _matches_static_spec(qname: str) -> bool:
    """Does any static :data:`SPECS` entry match ``qname`` (any port)?"""
    return any(host_matches(qname, host, mode) for host, _port, mode in SPECS)


def _min_session_allow_ttl(qname: str, now: float) -> float | None:
    """Min remaining TTL across unexpired session allows matching ``qname``,
    or None when none matches."""
    remainings = [
        exp - now
        for host, _port, mode, exp in state.SESSION_HOST_ALLOWS
        if exp > now and host_matches(qname, host, mode)
    ]
    return min(remainings, default=None)


def session_allow_rule_cap(qname: str) -> float | None:
    """Min remaining TTL bounding a DNS-path learned rule for ``qname``, or
    ``None`` (#2465).

    A timed consent allow adds the host to :data:`SESSION_HOST_ALLOWS`, so
    :func:`ports_for` treats it as allow-listed and the DNS path
    (:func:`respond_allowed` -> :func:`learn_all`) learns every resolved IP.
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
    :data:`SESSION_HOST_ALLOWS`); computed on the event-loop thread in
    :func:`respond_allowed` and passed to :func:`learn_all`, which runs
    off-loop in the executor.

    The static-spec check is qname-level (any port), so a host with a
    port-scoped static spec (``example.com:443``) AND a timed session allow on
    a *different* port (``example.com:8443``) leaves the :8443 learn uncapped
    -- pre-#2465 behavior (nothing was capped before), not a regression. The
    lingering :8443 ACCEPT rule (DNS TTL) sits at the top of OUTPUT and
    shadows NFQUEUE, so a retry past that verdict's window connects without a
    re-prompt until the DNS TTL elapses -- a known narrow leak for that combo,
    NOT covered by the NFQUEUE gate. All real consent flows hit this for a
    single host:port, where the cap is exact.
    """
    if _matches_static_spec(qname):
        return None  # a static spec matches -> forever -> DNS TTL is correct
    _prune_session_allows()
    return _min_session_allow_ttl(qname, time.time())


def _prune_session_denies() -> None:
    """Drop expired in-session host denies (lazy sweep, #2446).

    :data:`SESSION_HOST_DENIES` is loop-only (no lock), so -- like
    :data:`SESSION_HOST_ALLOWS` -- its timed entries expire here, on the loop,
    the next time a gate (:func:`session_host_denies_ttl`,
    :func:`add_session_deny`, :func:`drop_session_denies`) reads them. Cheap
    (the list is tiny -- one entry per denied host:port) and keeps the structure
    from growing unbounded across a long session.
    """
    now = time.time()
    state.SESSION_HOST_DENIES[:] = [t for t in state.SESSION_HOST_DENIES if t[3] > now]


def add_session_deny(host: str, port: int, ttl: float) -> None:
    """Deny ``host:port`` in-session for a consent deny verdict (#2446).

    The deny-side mirror of :func:`add_session_host`: adds
    ``(host, port, EXACT, now + ttl)`` to :data:`SESSION_HOST_DENIES` so
    :func:`cb` (via :func:`session_host_denies_ttl`) suppresses a re-prompt
    for a host the user already denied -- including a CDN-rotated or
    resolver-cached IP that the per-IP :data:`REJECTED` rule does not cover
    (the CARRYOVER-SURPRISE, #2446). EXACT scope (only the denied host, not its
    subdomains, #2377); deduped, a re-deny refreshes the expiry (``max`` --
    never shortens an unexpired entry). ``once`` adds nothing (per-connection,
    so a reconnect re-prompts). Loop-only (no lock).
    """
    _prune_session_denies()
    _add_session_entry(state.SESSION_HOST_DENIES, host, port, ttl)


def session_host_denies_ttl(host: str, port: int) -> float | None:
    """Remaining seconds an in-session deny covers ``host`` on ``port``, or
    ``None`` (#2446).

    The deny-side mirror of :func:`session_host_allows_ttl`, used by
    :func:`cb` as the last-chance gate before prompting: a SYN to a host:port
    the user already denied (timed or forever) -- including a CDN-rotated or
    resolver-cached IP that no fresh per-IP :data:`REJECTED` rule covers -- is
    denied fast (RST + short REJECT) without re-prompting. Matches via
    :func:`host_matches` (entries are added EXACT, so only the denied host
    matches, #2377); port must match (or the entry is all-ports). Returns the
    max remaining TTL across matching entries. Loop-only (no lock).
    """
    if not host:
        return None
    _prune_session_denies()
    return session_entry_ttl(state.SESSION_HOST_DENIES, host, port)


def drop_session_hosts(host: str) -> None:
    """Remove a host's in-session allow coverage (#2370, #2372, #2434).

    Drops every :data:`SESSION_HOST_ALLOWS` entry whose host matches
    (case-insensitive). Called on the **event loop** by
    :meth:`SidecarConsentClient.handle_drop_rule` **before** :func:`drop_for_host`
    forks iptables in the executor: while that fork runs (~tens of ms), the
    NFQUEUE consumer (:func:`cb` -> :func:`session_host_allows_ttl`) and the DNS
    path (:func:`ports_for`) both read :data:`SESSION_HOST_ALLOWS`, and a
    SYN/resolve arriving in that window would otherwise re-install a fresh
    ACCEPT (the host's remaining allow TTL, via :func:`allow`) that the revoke
    never clears. Clearing it first makes both gates deny during the window, so
    no fresh rule can be installed; :func:`drop_for_host` then removes the
    existing ACCEPTs. :data:`SESSION_HOST_ALLOWS` is loop-only (no lock) --
    touched on the loop, never inside :func:`drop_for_host` (executor thread). A
    deny revoke does not call this (a deny never adds to
    :data:`SESSION_HOST_ALLOWS`).
    """
    hl = host.lower()
    state.SESSION_HOST_ALLOWS[:] = [
        t for t in state.SESSION_HOST_ALLOWS if t[0].lower() != hl
    ]


def drop_session_denies(host: str) -> None:
    """Remove a host's in-session deny coverage (#2446).

    The deny-side mirror of :func:`drop_session_hosts`, called on the event
    loop by :meth:`SidecarConsentClient.handle_drop_rule` for a ``denied``
    revoke BEFORE :func:`drop_for_host` forks iptables in the executor: while
    that fork runs (~tens of ms), :func:`cb` reads :data:`SESSION_HOST_DENIES`,
    and a SYN arriving in that window would otherwise keep auto-denying (and
    re-installing a REJECT for) the host the operator just un-denied. Clearing
    it first lets the host re-prompt. :data:`SESSION_HOST_DENIES` is loop-only
    (no lock) -- touched on the loop, never inside :func:`drop_for_host`
    (executor thread).
    """
    hl = host.lower()
    state.SESSION_HOST_DENIES[:] = [
        t for t in state.SESSION_HOST_DENIES if t[0].lower() != hl
    ]
