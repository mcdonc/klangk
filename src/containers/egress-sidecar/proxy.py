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

DNS wire parsing is delegated to **dnspython** (rather than hand-rolled byte
slicing) so EDNS, CNAME chains, TCP-sized responses, and malformed packets are
handled correctly — a parser bug in a security component is dangerous, and a
maintained library removes that risk.

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
cleaned up (no TTL expiry). Transport is UDP only (TCP fallback is a future
addition).
"""

import os
import socket
import subprocess

import dns.message
import dns.rcode
import dns.rdatatype

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


def query_name(wire: bytes) -> str:
    """The queried domain (lowercased, no trailing dot) from a DNS wire message."""
    msg = dns.message.from_wire(wire)
    if not msg.question:
        return ""
    return msg.question[0].name.to_text().rstrip(".").lower()


def a_records(wire: bytes) -> list[str]:
    """IPv4 A-record addresses from a DNS response wire (dotted-quad strings).

    Walks the answer section (following CNAME chains transparently — the A
    records for the canonical name are in the answer too) and returns only
    A-record IPs.
    """
    msg = dns.message.from_wire(wire)
    ips = []
    for rrset in msg.answer:
        if rrset.rdtype == dns.rdatatype.A:
            for rdata in rrset:
                ips.append(rdata.address)
    return ips


def nxdomain_for(wire: bytes) -> bytes:
    """An NXDOMAIN response wire for the given query wire."""
    query = dns.message.from_wire(wire)
    resp = dns.message.make_response(query)
    resp.set_rcode(dns.rcode.NXDOMAIN)
    return resp.to_wire()


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
            data, addr = s.recvfrom(65535)
        except Exception:
            continue
        try:
            qname = query_name(data)
        except Exception:
            continue  # malformed/unparseable query -> drop
        if not qname or not allowed(qname):
            if DEBUG:
                print(f"deny  {qname}", flush=True)
            try:
                s.sendto(nxdomain_for(data), addr)
            except Exception:
                pass
            continue
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.settimeout(3)
        try:
            us.sendto(data, UPSTREAM)
            resp, _ = us.recvfrom(65535)
        except Exception:
            us.close()
            continue
        us.close()
        try:
            ips = a_records(resp)
        except Exception:
            ips = []
        for ip in ips:
            allow_ip(ip)
        if DEBUG:
            print(f"allow {qname} -> {ips}", flush=True)
        s.sendto(resp, addr)


if __name__ == "__main__":
    main()
