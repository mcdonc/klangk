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

Limitations: transport is UDP only (TCP fallback is a future addition).
"""

import os
import socket
import subprocess
import threading
import time

import dns.message
import dns.rcode
import dns.rdatatype

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


def parse_specs() -> list[tuple[str, int | None, bool]]:
    """Structured allow-list specs from ``KLANGKNETWORK_EGRESS_ALLOW``.

    Each entry is ``(host, port, is_wildcard)`` where ``port`` is ``None``
    (all ports) and ``is_wildcard`` means ``*.host`` (subdomains only). CIDR
    specs (``10.0.0.0/8``) are excluded — the entrypoint applies those
    statically. The grammar mirrors ``klangk.netfilter.parse_allowed_domains``
    so the API and the sidecar agree on what a spec means (#2256).
    """
    out: list[tuple[str, int | None, bool]] = []
    for spec in os.environ.get("KLANGKNETWORK_EGRESS_ALLOW", "").split(","):
        spec = spec.strip()
        if not spec or "/" in spec:
            continue
        port: int | None = None
        if ":" in spec:
            host_part, port_part = spec.rsplit(":", 1)
            if port_part.isdigit():
                port = int(port_part)
                spec = host_part
        host = spec.lower()
        is_wildcard = host.startswith("*.")
        if is_wildcard:
            host = host[2:]
        if host:
            out.append((host, port, is_wildcard))
    return out


SPECS = parse_specs()

# Learned IPs: {ip: {"expire": epoch, "ports": set[int | None]}}. A ``None``
# in ``ports`` is the all-ports ACCEPT rule. Guarded by _LOCK because the
# sweeper thread removes entries while the main loop adds/refreshes them
# (only the main loop installs rules; the sweeper only removes).
_LEARNED: dict[str, dict] = {}
_LOCK = threading.Lock()


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


def ports_for(qname: str) -> set[int] | None:
    """The ports a queried name is allowed on under :data:`SPECS`.

    ``None``  — a port-less spec matched (allow all ports).
    ``set()`` — nothing matched (deny).
    ``{443, ...}`` — allow exactly these TCP ports.

    A bare host matches the apex + subdomains; a ``*.host`` wildcard matches
    subdomains only (the apex is deliberately excluded so ``*.pypi.org`` and
    ``pypi.org`` are distinct, non-redundant scopes) (#2256).
    """
    ports: set[int] = set()
    for host, port, is_wildcard in SPECS:
        if is_wildcard:
            matched = qname.endswith("." + host)
        else:
            matched = qname == host or qname.endswith("." + host)
        if not matched:
            continue
        if port is None:
            return None  # an all-ports spec dominates
        ports.add(port)
    return ports


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
    (also under the lock). Without that, the sweeper could delete a rule
    :func:`allow` just installed while ``_LEARNED`` still records it as
    present — a fail-closed availability gap that only self-heals on the next
    re-resolution (#2256 review). The main loop is single-threaded and the
    sweeper runs every ``SWEEP_INTERVAL``, so serializing the iptables calls
    under the lock is negligible contention.
    """
    expire = time.time() + max(ttl, MIN_TTL)
    with _LOCK:
        _install(ip, port)
        rec = _LEARNED.get(ip)
        if rec is None:
            _LEARNED[ip] = {"expire": expire, "ports": {port}}
        else:
            rec["expire"] = max(rec["expire"], expire)
            rec["ports"].add(port)


def sweep_once(now: float | None = None) -> list[tuple[str, set]]:
    """Remove learned IPs whose TTL has elapsed; return ``(ip, ports)`` removed.

    Removal runs **under** :data:`_LOCK` (see :func:`allow`): the rule delete
    and the ``_LEARNED`` delete are atomic, so a concurrent :func:`allow`
    can't re-record an IP whose kernel rule was just swept. Factored out of
    :func:`_sweeper` so it is unit-testable with a mocked clock and iptables
    (#2256).
    """
    if now is None:
        now = time.time()
    expired: list[tuple[str, set]] = []
    with _LOCK:
        for ip, rec in list(_LEARNED.items()):
            if rec["expire"] > now:
                continue
            ports = set(rec["ports"])
            for port in ports:
                try:
                    _remove(ip, port)
                except Exception:
                    pass  # a transient failure drops one rule, not the sweep
            del _LEARNED[ip]
            expired.append((ip, ports))
    return expired


def _sweeper() -> None:
    """Background thread: periodically drop learned IPs past their TTL."""
    while True:
        time.sleep(SWEEP_INTERVAL)
        sweep_once()


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


def _respond_allowed(
    s: socket.socket,
    resp: bytes,
    addr: tuple[str, int],
    qname: str,
    ports: set[int | None],
) -> None:
    """Learn the response's IPs (port-scoped, TTL-tracked) + send it, swallowing
    transient errors.

    A failure here (a transient ``iptables`` error in :func:`allow`, or a
    ``sendto`` to a vanished client) must drop only this one response — not
    kill the proxy. If it escaped :func:`main` the sidecar's PID 1 would exit,
    DNS would be dead for the workspace, and the learned ``ACCEPT`` rules would
    persist (a partial fail-open: previously-resolved hosts stay reachable).
    #2278.
    """
    try:
        recs = a_records_with_ttl(resp)
    except Exception:
        recs = []
    try:
        for ip, ttl in recs:
            for port in ports:
                allow(ip, port, ttl)
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


def main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", LISTEN_PORT))
    print(
        f"dns-proxy listening on 127.0.0.1:{LISTEN_PORT} "
        f"(upstream={UPSTREAM[0]}, allowed={SPECS})",
        flush=True,
    )
    check_mark()
    threading.Thread(target=_sweeper, daemon=True).start()
    while True:
        try:
            data, addr = s.recvfrom(65535)
        except Exception:
            continue
        try:
            qname = query_name(data)
        except Exception:
            continue  # malformed/unparseable query -> drop
        ports = ports_for(qname)
        deny, port_set = _decision(qname, ports)
        if deny:
            if DEBUG:
                print(f"deny  {qname}", flush=True)
            try:
                s.sendto(nxdomain_for(data), addr)
            except Exception:
                pass
            continue
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
        us.settimeout(3)
        try:
            us.sendto(data, UPSTREAM)
            resp, _ = us.recvfrom(65535)
        except Exception:
            us.close()
            continue
        us.close()
        _respond_allowed(s, resp, addr, qname, port_set)


if __name__ == "__main__":
    main()
