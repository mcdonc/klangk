#!/usr/bin/env python3
"""FQDN egress DNS proxy — runs in the egress sidecar (#2250, #2253).

The sidecar shares the workspace's network namespace (the workspace runs
``--network container:<this sidecar>``). The sidecar's ``entrypoint.sh`` installs
a nat REDIRECT of the workspace's configured DNS resolvers (:53) to this proxy's
listen port; this proxy applies an FQDN allow-list, forwards allowed queries to a
*different* upstream (so the REDIRECT does not loop), learns the A-record IPs from
the responses, and inserts ``iptables -I OUTPUT 1 -d <ip> -j ACCEPT`` for each so
the workspace can reach exactly the IPs it resolved — solving DNS round-robin.
Denied names get NXDOMAIN.

Configuration (env):
  KLANGK_EGRESS_ALLOW       comma-separated allow-list: ``host[:port]`` or CIDR
                            specs. CIDR specs are applied statically by the
                            entrypoint; this proxy matches only the host specs
                            (exact or suffix).
  KLANGK_EGRESS_UPSTREAM    the real upstream resolver the proxy forwards to
                            (default ``8.8.8.8``). MUST differ from the
                            workspace's configured (redirected) resolvers or the
                            proxy's forwards loop back into itself.
  KLANGK_EGRESS_LISTEN_PORT UDP port to listen on (default ``15353``).
  KLANGK_EGRESS_IPTABLES    iptables binary (default ``iptables``).
  KLANGK_EGRESS_DEBUG       if set, log each allow/deny decision.

Limitations (tracked in #2256): a learned IP is allow-listed on *all* ports (no
per-domain port scoping yet), no wildcard domains, and learned IPs are never
cleaned up (no TTL expiry).
"""

import os
import socket
import struct
import subprocess

UPSTREAM = (os.environ.get("KLANGK_EGRESS_UPSTREAM", "8.8.8.8"), 53)
LISTEN_PORT = int(os.environ.get("KLANGK_EGRESS_LISTEN_PORT", "15353"))
IPT = os.environ.get("KLANGK_EGRESS_IPTABLES", "iptables")
DEBUG = bool(os.environ.get("KLANGK_EGRESS_DEBUG"))


def host_specs() -> list[str]:
    """Host specs (suffix-match targets) from KLANGK_EGRESS_ALLOW.

    CIDR specs (``10.0.0.0/8``) are excluded — the entrypoint applies those
    statically. ``host:port`` specs are stripped to the host part.
    """
    out = []
    for spec in os.environ.get("KLANGK_EGRESS_ALLOW", "").split(","):
        spec = spec.strip()
        if not spec or "/" in spec:
            continue
        host = spec.split(":", 1)[0].lower()
        if host:
            out.append(host)
    return out


ALLOWED = host_specs()


def parse_qname(msg: bytes) -> str:
    """The queried domain (lowercased), from a DNS wire-format message."""
    i = 12  # skip the 12-byte header
    labels = []
    while i < len(msg) and msg[i] != 0:
        n = msg[i]
        labels.append(msg[i + 1 : i + 1 + n].decode("ascii", "ignore"))
        i += 1 + n
    return ".".join(labels).lower()


def parse_a_ips(msg: bytes) -> list[str]:
    """IPv4 A-record addresses from a DNS response (dotted-quad strings)."""
    ancount = struct.unpack("!H", msg[6:8])[0]
    i = 12
    while i < len(msg) and msg[i] != 0:  # skip the question QNAME
        i += 1 + msg[i]
    i += 5  # null + QTYPE(2) + QCLASS(2)
    ips = []
    for _ in range(ancount):
        if i >= len(msg):
            break
        if msg[i] & 0xC0 == 0xC0:  # compressed name pointer
            i += 2
        else:
            while i < len(msg) and msg[i] != 0:
                i += 1 + msg[i]
            i += 1
        if i + 10 > len(msg):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", msg[i : i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:  # A
            ips.append(".".join(str(b) for b in msg[i : i + 4]))
        i += rdlen
    return ips


def build_nxdomain(query: bytes) -> bytes:
    """An NXDOMAIN response for the given query (preserves id + question)."""
    return query[:2] + b"\x81\x83" + query[4:6] + b"\x00\x00\x00\x00" + query[12:]


def allowed(qname: str) -> bool:
    return any(qname == h or qname.endswith("." + h) for h in ALLOWED)


def allow_ip(ip: str) -> None:
    """Insert an allow-rule at the top of OUTPUT for a learned IP."""
    subprocess.run(
        [IPT, "-I", "OUTPUT", "1", "-d", ip, "-j", "ACCEPT"],
        capture_output=True,
    )


def main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", LISTEN_PORT))
    print(
        f"dns-proxy listening on 127.0.0.1:{LISTEN_PORT} "
        f"(upstream={UPSTREAM[0]}, allowed={ALLOWED})",
        flush=True,
    )
    while True:
        try:
            data, addr = s.recvfrom(4096)
        except Exception:
            continue
        try:
            qname = parse_qname(data)
        except Exception:
            continue
        if not allowed(qname):
            if DEBUG:
                print(f"deny  {qname}", flush=True)
            s.sendto(build_nxdomain(data), addr)
            continue
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.settimeout(3)
        try:
            us.sendto(data, UPSTREAM)
            resp, _ = us.recvfrom(4096)
        except Exception:
            us.close()
            continue
        us.close()
        ips = parse_a_ips(resp)
        for ip in ips:
            allow_ip(ip)
        if DEBUG:
            print(f"allow {qname} -> {ips}", flush=True)
        s.sendto(resp, addr)


if __name__ == "__main__":
    main()
