"""iptables rule management + learned-IP lifecycle (#2256, #2450).

allow/reject install OUTPUT ACCEPT/REJECT rules under LOCK (atomic w.r.t.
sweep_once); drop_for_host revokes a host's rules; the TTL sweeper
(async_sweeper / sweep_once) reaps expired rules. learn_all / record_hosts
/ host_for drive the learned-IP map the DNS + NFQUEUE paths share.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import time

from . import config
from .config import MARK, MIN_TTL, SWEEP_INTERVAL
from .state import LEARNED, LOCK, REJECTED


def rule_args(ip: str, port: int | None) -> list[str]:
    """iptables OUTPUT rule args for ``ACCEPT`` to ``ip`` (optionally scoped)."""
    args = ["-d", ip]
    if port is not None:
        args += ["-p", "tcp", "--dport", str(port)]
    args += ["-j", "ACCEPT"]
    return args


def rule_exists(ip: str, port: int | None) -> bool:
    return (
        subprocess.run(
            [config.IPT, "-C", "OUTPUT", *rule_args(ip, port)],
            capture_output=True,
        ).returncode
        == 0
    )


def install(ip: str, port: int | None) -> None:
    """Insert the ACCEPT rule at the top of OUTPUT if not already present."""
    if rule_exists(ip, port):
        return
    subprocess.run(
        [config.IPT, "-I", "OUTPUT", "1", *rule_args(ip, port)],
        capture_output=True,
    )


def remove(ip: str, port: int | None) -> None:
    """Delete one matching ACCEPT rule; swallow failure if it's already gone."""
    subprocess.run(
        [config.IPT, "-D", "OUTPUT", *rule_args(ip, port)],
        capture_output=True,
    )


def _floored_expire(ttl: int | float, floor: bool) -> float:
    """The rule/record expiry for a TTL of ``ttl``: floored at
    :data:`MIN_TTL` when ``floor`` (the static-spec DNS learn), verbatim when
    not (a consent-verdict TTL -- see :func:`allow`)."""
    return time.time() + (max(ttl, MIN_TTL) if floor else ttl)


def _record_allow(ip: str, port: int | None, expire: float) -> None:
    """Create/refresh the learned IP's ``LEARNED`` record for an allow
    (caller holds :data:`LOCK`)."""
    rec = LEARNED.get(ip)
    if rec is None:
        LEARNED[ip] = {
            "expire": expire,
            "rule_expire": expire,
            "ports": {port},
            "host": None,
        }
        return
    rec["expire"] = max(rec["expire"], expire)
    # rule_expire is the ACCEPT rule's lifetime, kept SEPARATE from the
    # host-mapping expire so a re-resolve's longer DNS TTL can't extend
    # a consent allow's rule past its verdict (#2408). max() preserves
    # the longest across static re-learns (#2256); for a consent allow
    # it is just the verdict's TTL (the pre-existing rule_expire is
    # absent -- only record_hosts has touched the record -- and `or
    # 0.0` coerces the None).
    rec["rule_expire"] = max(rec.get("rule_expire") or 0.0, expire)
    rec["ports"].add(port)
    # ``host`` (set by record_hosts) is preserved across re-learn.


def _supersede_port_denies(ip: str) -> None:
    """Remove any lingering per-port REJECTs for ``ip`` (caller holds LOCK).

    An all-ports allow (the consent path) supersedes any prior per-port
    denies for this IP -- otherwise the all-ports ACCEPT at the top of
    OUTPUT would silently shadow a lingering REJECT (the decider allowed
    the host, so a prior port-specific deny no longer applies)."""
    for key in [k for k in REJECTED if k[0] == ip]:
        try:
            remove_reject(*key)
        except Exception:
            pass
        del REJECTED[key]


def allow(ip: str, port: int | None, ttl: int | float, floor: bool = True) -> None:
    """Install (if new) the ACCEPT for ``ip[:port]`` and refresh its TTL.

    ``port`` is ``None`` for an all-ports rule. The learned IP's expiry is
    set to ``now + ttl`` and only ever moves forward, so a shorter-TTL
    re-resolution can't prematurely expire a longer-lived prior rule (#2256).
    ``floor`` (default ``True``) raises the TTL to :data:`MIN_TTL` -- the
    0-TTL-DNS-response safety net (a resolver may hand back a 0-TTL A record,
    and that must not yank the rule the workspace needs to reach the IP it
    just resolved). A *consent-verdict* TTL is the user's intent, not a DNS
    TTL, so the consent paths (:func:`nfqueue.decide_and_verdict`, the ``cb``
    in-session auto-allow, and a capped :func:`learn_all`) pass
    ``floor=False`` -- a timed verdict's rule lapses at the verdict, not at
    MIN_TTL (#2465: otherwise a ``5s`` verdict's rule lived 30s under the
    default MIN_TTL). The static-spec DNS learn keeps the default floor.

    The install happens **under** :data:`LOCK` so the kernel rule and its
    ``LEARNED`` record are atomic w.r.t. :func:`sweep_once`'s remove+delete
    (also under the lock). Without that, a concurrent sweep running in another
    executor worker could delete a rule :func:`allow` just installed while
    ``LEARNED`` still records it as present -- a fail-closed availability gap
    that only self-heals on the next re-resolution (#2256 review). ``allow`` and
    :func:`sweep_once` both run off the event loop in the default thread-pool
    executor (see :func:`learn_all` / :func:`async_sweeper`), so the lock
    genuinely serializes them; contention is negligible.
    """
    expire = _floored_expire(ttl, floor)
    with LOCK:
        install(ip, port)
        _record_allow(ip, port, expire)
        if port is None:
            _supersede_port_denies(ip)


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


def reject_rule_exists(ip: str, port: int, sport: int = 0) -> bool:
    return (
        subprocess.run(
            [config.IPT, "-C", "OUTPUT", *_reject_rule_args(ip, port, sport)],
            capture_output=True,
        ).returncode
        == 0
    )


def install_reject(ip: str, port: int, sport: int = 0) -> None:
    """Insert the REJECT (tcp-reset) rule at the top of OUTPUT if not present."""
    if reject_rule_exists(ip, port, sport):
        return
    subprocess.run(
        [config.IPT, "-I", "OUTPUT", "1", *_reject_rule_args(ip, port, sport)],
        capture_output=True,
    )


def remove_reject(ip: str, port: int, sport: int = 0) -> None:
    """Delete the REJECT rule; swallow failure if it's already gone."""
    subprocess.run(
        [config.IPT, "-D", "OUTPUT", *_reject_rule_args(ip, port, sport)],
        capture_output=True,
    )


def reject(ip: str, port: int, ttl: float, sport: int = 0) -> None:
    """Install a temporary REJECT (tcp-reset) for ``ip:port`` + set its TTL.

    A denied SYN is dropped, but dropping a SYN doesn't fail ``connect()`` --
    the kernel retransmits (tcp_syn_retries, ~127s) before timing out. The
    REJECT rule makes the next retransmit get a RST, so ``connect()`` returns
    ECONNREFUSED at once (eager deny). Like :func:`allow`, the install + the
    ``REJECTED`` record are atomic under :data:`LOCK` w.r.t. :func:`sweep_once`.

    ``sport`` (the denied connection's source port) scopes the rule to
    retransmits of THAT connection only, so a NEW connection (different source
    port) to the same ``ip:port`` is NOT rejected above NFQUEUE and re-enters
    consent-gating (#2463). 0/omitted leaves the rule destination-scoped
    (every connection to ``ip:port``), which is correct for a timed/forever
    deny (its over-deny is intended -- the DB rule + ``SESSION_HOST_DENIES``
    govern re-prompting) and the WS-down fail-close.
    """
    expire = time.time() + ttl
    with LOCK:
        install_reject(ip, port, sport)
        REJECTED[(ip, port, sport)] = max(REJECTED.get((ip, port, sport), 0.0), expire)


def _drop_targets(host: str) -> set[str]:
    """The candidate IPs a host revoke must touch (caller holds LOCK):
    every learned IP that resolved to ``host`` (via ``LEARNED[ip]["host"]``,
    set by ``record_hosts`` for every resolved name, allow-listed or not),
    plus the host string itself (a direct-IP connect that never went through
    DNS, and a direct-IP allow whose ``host`` is ``None``)."""
    host_l = host.lower()
    ips = [
        ip for ip, rec in LEARNED.items() if (rec.get("host") or "").lower() == host_l
    ]
    return {ip for ip in ips} | {host_l, host}


def drop_for_host(host: str, decision: str) -> set[str]:
    """Drop the sidecar's rules for a host (revocation, #2339).

    ``allowed``: remove the learned ACCEPT rules (+ ``LEARNED`` records) for
    the host's IPs (revert to default/allow-list filtering).
    ``denied``: remove the temporary REJECT rules for the host's IPs (stop
    force-rejecting; the host is again subject to the allow-list).

    Returns the set of candidate IPs so the caller --
    :meth:`SidecarConsentClient.handle_drop_rule`, on the event loop -- can
    clear the loop-only ``SESSION_HOST_ALLOWS``/``VERDICT_CACHE`` state
    (via :func:`drop_session_hosts` / :func:`clear_verdict_cache`). Those
    structures are documented loop-only
    (no lock) and must NOT be mutated here, since this function runs off the
    loop in the executor.

    Host->IP comes from ``LEARNED[ip]["host"]`` (set by ``record_hosts`` for
    every resolved name, allow-listed or not), so a deny's IPs are found too;
    the host string itself is also a candidate IP (a direct-IP connect that
    never went through DNS, and a direct-IP allow whose ``host`` is ``None``).
    Best-effort: a failed delete drops one rule, not the whole revoke. Sync
    (forks iptables) -- run off the loop; under ``LOCK`` like allow/sweep.

    L3/L4 limit (co-resident hosts): the egress rules are per IP+port, so two
    DNS names that resolve to the SAME IP share one rule and cannot be revoked
    individually -- revoking one name removes the shared rule, affecting the
    other too. A correct per-host revoke for co-resident hosts (CDN/S3/Cloudflare
    fronted sites) needs L7/SNI filtering, which is a separate feature (#2352).
    """
    with LOCK:
        targets = _drop_targets(host)
        if decision == "allowed":
            _drop_learned_rules(targets)
        elif decision == "denied":
            _drop_reject_rules(targets)
    return targets


def _remove_learned_ports(ip: str) -> None:
    """Remove every learned ACCEPT rule for ``ip``'s recorded ports
    (best-effort: a failed delete drops one rule, not the whole revoke)."""
    for port in list(LEARNED[ip]["ports"]):
        try:
            remove(ip, port)
        except Exception:
            pass


def _drop_learned_rules(targets: set[str]) -> None:
    """Remove the learned ACCEPT rules (+ ``LEARNED`` records) for the
    target IPs (best-effort: a failed delete drops one rule, not the whole
    revoke). Caller holds ``LOCK``."""
    for ip in [i for i in targets if i in LEARNED]:
        _remove_learned_ports(ip)
        del LEARNED[ip]


def _drop_reject_rules(targets: set[str]) -> None:
    """Remove the temporary REJECT rules for the target IPs (best-effort).
    Caller holds ``LOCK``."""
    for key in [k for k in REJECTED if k[0] in targets]:
        try:
            remove_reject(*key)
        except Exception:
            pass
        del REJECTED[key]


def _sweep_learned_rules(ip: str, rec: dict, now: float) -> set | None:
    """Rule sweep for one learned IP: delete the ACCEPT rules whose lifetime
    elapsed, returning the swept ports (or None when nothing swept). The
    lifetime is ``rule_expire`` when set (a consent allow, whose verdict must
    outlive the host-mapping's DNS TTL, #2408), else ``expire`` (static
    re-learn / backward compat)."""
    rule_expire = rec.get("rule_expire", rec["expire"])
    if not (rec["ports"] and rule_expire <= now):
        return None
    ports = set(rec["ports"])
    for port in ports:
        try:
            remove(ip, port)
        except Exception:
            pass  # a transient failure drops one rule, not the sweep
    return ports


def _sweep_learned_record(
    ip: str, rec: dict, now: float, expired: list[tuple[str, set]]
) -> None:
    """Sweep one learned-IP record in place (caller holds ``LOCK``):
    rule sweep via :func:`_sweep_learned_rules`; record sweep -- drop the
    host mapping once its own expire elapses and no ACCEPT rule remains."""
    ports = _sweep_learned_rules(ip, rec, now)
    if ports:
        expired.append((ip, ports))
        rec["ports"] = set()  # rule gone; keep record for naming
    # Record sweep: drop the host mapping once its own expire elapses
    # and no ACCEPT rule remains.
    if rec["expire"] <= now and not rec["ports"]:
        del LEARNED[ip]


def _sweep_rejected(now: float) -> None:
    """Remove temporary REJECT (tcp-reset) rules whose TTL elapsed
    (best-effort; caller holds ``LOCK``)."""
    for key in [k for k, exp in REJECTED.items() if exp <= now]:
        try:
            remove_reject(*key)
        except Exception:
            pass
        del REJECTED[key]


def sweep_once(now: float | None = None) -> list[tuple[str, set]]:
    """Remove ACCEPT rules whose TTL has elapsed; return ``(ip, ports)`` removed.

    Two lifetimes are tracked per learned IP (#2408):

    * ``rule_expire`` -- the ACCEPT rule's lifetime (a consent allow's verdict,
      or a static re-learn's DNS TTL). When it elapses the kernel ACCEPT rule
      is deleted but the record is KEPT while its host-mapping ``expire`` is
      still valid, so :func:`host_for` can still name the host for a fresh
      consent request.
    * ``expire`` -- the host-mapping lifetime (the DNS TTL). When it elapses
      AND no ACCEPT rule remains, the whole record is dropped.

    Records without ``rule_expire`` (host-mapping-only entries from
    :func:`record_hosts`, or pre-#2408 records) fall back to ``expire`` for the
    rule sweep, preserving the old single-expiry behavior. Removal runs under
    :data:`LOCK` (see :func:`allow`): the rule delete and the record delete are
    atomic, so a concurrent :func:`allow` can't re-record an IP whose kernel
    rule was just swept. Factored out of :func:`async_sweeper` so it is
    unit-testable with a mocked clock and iptables (#2256).
    """
    if now is None:
        now = time.time()
    expired: list[tuple[str, set]] = []
    with LOCK:
        for ip, rec in list(LEARNED.items()):
            _sweep_learned_record(ip, rec, now, expired)
        _sweep_rejected(now)
    return expired


async def async_sweeper() -> None:
    """Background task: periodically drop learned IPs past their TTL (#2256).

    :func:`sweep_once` runs in the executor so its iptables ``-D`` forks don't
    block the loop (and so it can run concurrently with :func:`learn_all`,
    serialized by :data:`LOCK`).
    """
    # Runs for the sidecar's lifetime; cancelled at shutdown, never exits
    # by falling through the condition (the arc to loop exit is unreachable).
    while True:  # pragma: no branch
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            await asyncio.get_running_loop().run_in_executor(None, sweep_once)
        except Exception:
            pass  # a transient sweep failure defers cleanup to the next tick


def fmt_ports(ports: set[int | None]) -> str:
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


# --- workspace-egress accounting (#2485) -------------------------------------
# A mangle-OUTPUT rule whose in-kernel byte counter the idle-activity sampler
# (consent.activity_sampler) polls once per ACTIVITY_GATE_S. The kernel bumps
# the counter on every matching packet at packet-processing time; our code only
# reads it -- no per-packet/per-flow Python. The match is scoped to UNmarked
# traffic (the proxy's own upstream DNS carries SO_MARK=MARK; the workspace
# cannot mark), optionally excluding the klangkd WS host, so it counts only the
# workspace's real egress -- not the sidecar's own control plane, which would
# otherwise self-sustain (an activity frame / WS keepalive moves bytes -> looks
# like traffic -> bumps the timer -> the workspace never goes idle). Mangle,
# not filter, so the rule never fights the learned ACCEPT/REJECT rules'
# `-I OUTPUT 1` inserts; `-j ACCEPT` terminates only the mangle chain, leaving
# the nat REDIRECT + filter OUTPUT path intact. (The forged eager-deny RST
# from packets.py is also unmarked + not the WS host, so it is counted too --
# harmless: it correlates 1:1 with a SYN the #2481 NFQUEUE hook already bumped,
# and bump_activity's flood gate dedupes.)
ACCT_COMMENT = "klangk-acct"


def acct_match(exclude_ip: str | None) -> list[str]:
    """iptables match/target args for the accounting rule: unmarked traffic
    (workspace egress), optionally excluding the klangkd WS host."""
    args = ["-m", "mark", "!", "--mark", str(MARK)]
    if exclude_ip:
        args += ["!", "-d", exclude_ip]
    args += ["-m", "comment", "--comment", ACCT_COMMENT, "-j", "ACCEPT"]
    return args


def install_acct(exclude_ip: str | None = None) -> None:
    """Install the mangle-OUTPUT egress-accounting rule (#2485), idempotently.

    Best-effort: a failure to install is silent -- the sampler then reads a
    flat zero counter and never bumps, i.e. falls back to the #2481 DNS/SYN
    behavior (an egress-only long-lived flow could be reaped, but egress itself
    is unaffected). Sync (forks iptables) like allow/sweep.
    """
    try:
        check = subprocess.run(
            [config.IPT, "-t", "mangle", "-C", "OUTPUT", *acct_match(exclude_ip)],
            capture_output=True,
        )
        if check.returncode == 0:
            return  # already present
        subprocess.run(
            [config.IPT, "-t", "mangle", "-A", "OUTPUT", *acct_match(exclude_ip)],
            capture_output=True,
        )
    except Exception:
        pass


def _acct_bytes_from_output(out: str) -> int:
    """The byte count on the ``--comment``-tagged accounting rule line, or 0
    when the rule is absent or the columns don't parse. With ``-v`` the first
    two columns are pkts/bytes; ``-x`` makes bytes exact (no K/M suffix)."""
    for line in out.splitlines():
        if ACCT_COMMENT in line:
            parts = line.split()  # parts[0]=pkts, parts[1]=bytes (the -v cols)
            try:
                return int(parts[1])
            except (IndexError, ValueError):
                return 0
    return 0


def acct_bytes() -> int:
    """Current byte count on the accounting rule, or 0 if absent/unreadable.

    Parsed from ``iptables -t mangle -L OUTPUT -v -x -n``; the ``--comment``
    tag uniquely identifies the rule line. 0 on any parse / subprocess
    failure so the sampler treats a missing rule as a flat baseline
    (never as a burst of activity).
    """
    try:
        out = subprocess.run(
            [config.IPT, "-t", "mangle", "-L", "OUTPUT", "-v", "-x", "-n"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except Exception:
        return 0
    return _acct_bytes_from_output(out)


def learn_all(
    recs: list[tuple[str, int]],
    ports: set[int | None],
    cap: float | None = None,
) -> None:
    """Install the ACCEPT rule for each learned IP/port (sync; runs in the
    executor so the iptables forks don't block the event loop).

    ``cap``, when not ``None``, bounds each rule's TTL at ``min(dns_ttl, cap)``
    so a timed session-allow's learned rule does not outlive its verdict
    (#2465). The host mapping -- set earlier by :func:`record_hosts` at the
    DNS TTL, ahead of the consent allow -- is untouched: :func:`allow`'s
    ``max`` keeps the longer mapping lifetime so :func:`host_for` still names
    the host for a fresh re-prompt after the verdict lapses (#2408).
    """
    for ip, ttl in recs:
        rule_ttl = ttl if cap is None else min(ttl, cap)
        for port in ports:
            # cap=None (static spec) -> a DNS-response TTL, floored at MIN_TTL
            # for 0-TTL safety; cap set (session allow) -> the verdict's
            # remaining window, NOT floored so a sub-MIN_TTL verdict (5s)
            # lapses at the verdict, not at MIN_TTL (#2465).
            allow(ip, port, rule_ttl, floor=cap is None)


def record_hosts(recs: list[tuple[str, int]], host: str) -> None:
    """Record IP->host in LEARNED WITHOUT installing an ACCEPT rule (#2324).

    A non-allow-listed name resolves (the workspace gets the IP + can SYN) but
    its connection is consent-gated at the SYN (NFQUEUE), so the IP must NOT be
    allow-learned here. Recording host lets the NFQUEUE consumer name the host
    in the consent request. Sync; runs in the executor alongside allow/sweep
    (under LOCK). The TTL refreshes on each resolve so a re-resolution extends
    the window in which a SYN names the right host.

    Only the host-mapping ``expire`` is touched here -- never ``rule_expire``
    (the ACCEPT rule's lifetime, set by :func:`allow`). Keeping the two
    separate is what lets a consent allow's rule expire at its verdict while
    the host mapping lives for the DNS TTL, so a re-resolve's longer DNS TTL
    can't extend an allow past its verdict (#2408).
    """
    now = time.time()
    with LOCK:
        for ip, ttl in recs:
            expire = now + max(ttl, MIN_TTL)
            rec = LEARNED.get(ip)
            if rec is None:
                LEARNED[ip] = {"expire": expire, "ports": set(), "host": host}
            else:
                rec["expire"] = max(rec["expire"], expire)
                rec["host"] = host  # latest name that resolved to this IP


def host_for(ip: str) -> str:
    """The DNS name that resolved to ``ip``, or ``ip`` itself (direct-IP connect)."""
    with LOCK:
        return LEARNED.get(ip, {}).get("host") or ip
