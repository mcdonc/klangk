"""Controlled-DNS test fixture for the interactive-egress smoketest (#2424).

A reusable, self-contained fixture that makes the workspace's DNS resolution
deterministic: chosen hostnames resolve to single, stable, test-controlled IPs
(each fronted by a test HTTP/HTTPS server on :80 + :443), while every other
name is forwarded to a real upstream unchanged. This removes the two properties
of real hosts that today make three smoketest checklist items indeterminate
(#2392):

1. **CDN IP rotation / multiple A records** -- a later connection to the "same"
   host resolves to a *different* IP, masquerading as a snapshot-replay event
   (#2412). A controlled name has exactly one IP, so there is no cascade.
2. **The L3/L4 allow rule** -- once an apex IP is allow-listed, a subdomain that
   *shares* that IP is allowed at L3/L4 regardless of the L7 hostname spec. By
   putting the apex and the subdomain on *distinct* controlled IPs the L7 spec
   (``bare`` = exact/apex-only, ``.host`` = apex+subdomains, ``*.host`` =
   subdomains-only, #2377) is observable.

Delivery -- no new production code paths
----------------------------------------
The fixture drives the real stack only. It brings up two ordinary podman
containers on the default ``podman`` network and points the real network
sidecar's upstream resolver at the DNS container via the existing operator
knob ``KLANGKNETWORK_EGRESS_UPSTREAM`` (honored by
:meth:`ContainerManager.start_network_sidecar`, mirroring the
``KLANGKNETWORK_EGRESS_MIN_TTL`` / ``SWEEP_INTERVAL`` forwarding). The sidecar's
proxy then forwards the workspace's :53 queries to this fixture as it would any
upstream -- the same REDIRECT + mark + learn path production uses.

Topology (two containers, both on the default ``podman`` network so the
sidecar -- also on that network -- reaches them by bridge IP):

* **target** -- one container that holds N secondary IPs (added with
  ``--cap-add NET_ADMIN``) and serves HTTP (:80) + HTTPS (:443, self-signed,
  ``curl -k``) bound to ``0.0.0.0`` so a single server answers on *every* IP it
  holds. Controlled names resolve to one of these IPs; distinct names get
  distinct IPs so the L3/L4 rule can't paper over the L7 spec.
* **dns** -- one container running a UDP :53 forwarding resolver. A controlled
  name (read from a live, bind-mounted JSON map the fixture updates as it
  allocates names) is answered with its single mapped IP; anything else is
  forwarded to a real upstream so the rest of the smoketest (real hosts in the
  fuzz pool) keeps resolving.

The fixture is intentionally process-driven (``podman`` via ``subprocess``),
not a Python import of server internals: it stands up alongside -- and is torn
down by -- the human-run smoketest, never linked into the server.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

from _e2e_server import tracked_mkdtemp

# The default network-sidecar image has python3 + dnspython + openssl (the
# proxy's deps), so a single image serves as both the DNS forwarder and the
# multi-IP HTTP/HTTPS target -- no second image build. Overridable via the
# smoketest's --sidecar-image for a non-default deploy.
DEFAULT_IMAGE = "localhost/klangk-network-sidecar:latest"
DEFAULT_NETWORK = "podman"
# Secondary IPs are assigned in a high slice of the subnet's /24 (the gateway
# is reliably the .1, so <gw-first-three>.200+ is free of podman's low DHCP
# range). 32 IPs covers the host-scope (#2442 multi-level subdomains),
# snapshot, port-scope, coresident and per-connection-cache / fan-out phases
# all running at once (~27 distinct names), with headroom.
_IP_SLICE_START = 200
_IP_SLICE_SIZE = 32
# Forward unknown queries here (a public resolver the container can reach over
# its unrestricted egress). The rest of the smoketest's real hosts resolve
# through this, so the controlled upstream is a drop-in replacement.
_DEFAULT_FORWARD = "1.1.1.1"
_DNS_TTL = 60  # seconds; long enough that a learned IP isn't swept mid-phase


# --- in-container server scripts (embedded so no extra files ship) ----------
# Both run with the sidecar image's entrypoint cleared (else the image's
# iptables entrypoint runs). python3 + dnspython + openssl are present.

_TARGET_PY = r"""\
import os, socket, ssl, threading, time

# (The secondary IPs this target holds are added by the fixture after start via
# `podman exec ... ip addr add` -- see ControlledDns._install_target_ips. Each
# added IP is an address the bridge ARP-responds for, so a peer (the sidecar)
# reaches the one 0.0.0.0-bound server on any of them.)

# One self-signed cert (generated on the host, bind-mounted here) covers every
# IP/name (curl -k skips validation). The sidecar image ships no openssl, so the
# cert is produced on the host where openssl is available.
_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
try:
    _ctx.load_cert_chain("/mnt/ctrl-cert.pem", "/mnt/ctrl-key.pem")
except Exception:
    _ctx = None

_BODY = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"


def _serve(port, tls):
    l = socket.socket()
    l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    l.bind(("0.0.0.0", port))
    l.listen(64)
    while True:
        c, _ = l.accept()
        try:
            if tls and _ctx is not None:
                c = _ctx.wrap_socket(c, server_side=True)
            c.recv(4096)
            c.sendall(_BODY)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass


threading.Thread(target=_serve, args=(80, False), daemon=True).start()
threading.Thread(target=_serve, args=(443, True), daemon=True).start()
print("ctrl-target up", flush=True)
while True:
    time.sleep(60)
"""

_DNS_PY = r"""\
import json, os, socket
import dns.message, dns.rrset

_MAP_PATH = os.environ.get("CTRL_DNS_MAP", "/map/map.json")
_FORWARD = os.environ.get("CTRL_DNS_FORWARD", "1.1.1.1")
_TTL = int(os.environ.get("CTRL_DNS_TTL", "60"))


def _load():
    try:
        with open(_MAP_PATH) as f:
            m = json.load(f)
        return {str(k).rstrip(".").lower(): str(v) for k, v in m.items()}
    except Exception:
        return {}


def _ip_for(qname, m):
    return m.get(qname.rstrip(".").lower())


s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 53))
print("ctrl-dns listening", flush=True)
while True:
    try:
        data, addr = s.recvfrom(65535)
    except Exception:
        continue
    try:
        q = dns.message.from_wire(data)
    except Exception:
        continue
    if not q.question:
        continue
    name = q.question[0].name
    ip = _ip_for(name.to_text(), _load())
    if ip:
        resp = dns.message.make_response(q)
        resp.answer.append(
            dns.rrset.from_text(name, _TTL, "IN", "A", ip)
        )
        try:
            s.sendto(resp.to_wire(), addr)
        except Exception:
            pass
        continue
    # Unknown name -> forward to the real upstream (a fresh socket per query so
    # responses can't cross-match). Failure -> NXDOMAIN (fail-closed: the
    # smoketest treats an unresolvable real host as a finding, not a leak).
    try:
        f = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        f.settimeout(4)
        f.sendto(data, (_FORWARD, 53))
        rd, _ = f.recvfrom(65535)
        f.close()
        s.sendto(rd, addr)
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        resp = dns.message.make_response(q)
        import dns.rcode
        resp.set_rcode(dns.rcode.NXDOMAIN)
        try:
            s.sendto(resp.to_wire(), addr)
        except Exception:
            pass
"""


@dataclass
class _Procs:
    """Names of the fixture's containers + the host dir bind-mounted into dns."""

    target: str = ""
    dns: str = ""
    dir: str = ""
    base: str = ""  # "<gw-first-three-octets>", e.g. "10.88.0"
    ips: list[str] = field(
        default_factory=list
    )  # secondary IPs the target holds
    upstream_ip: str = (
        ""  # the dns container's bridge IP (the sidecar upstream)
    )
    map_path: str = ""  # host path of map.json (bind-mounted into dns)


def podman(*args: str, timeout: float = 60) -> str:
    """Run podman, return stdout (stripped); raise on non-zero."""
    r = subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"podman {' '.join(args)} failed (rc={r.returncode}): "
            f"{r.stderr.strip()}"
        )
    return r.stdout.strip()


def _net_ip(name: str, network: str = DEFAULT_NETWORK) -> str:
    """The container's bridge IP on ``network`` via inspect."""
    out = podman(
        "inspect",
        name,
        "--format",
        '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % network,
    )
    return out.strip()


def _net_gw(name: str, network: str = DEFAULT_NETWORK) -> str:
    out = podman(
        "inspect",
        name,
        "--format",
        '{{(index .NetworkSettings.Networks "%s").Gateway}}' % network,
    )
    return out.strip()


_FIXTURE_PREFIXES = ("ctrl-dns-target-", "ctrl-dns-dns-")


def cleanup_stale_containers() -> list[str]:
    """Remove leftover ``ctrl-dns-*`` containers from prior, interrupted runs.

    The fixture names its containers ``ctrl-dns-target-<pid>`` /
    ``ctrl-dns-dns-<pid>`` (``<pid>`` = the creating process's PID), so a new
    run gets fresh names and never reclaims an interrupted prior run's set.
    Those prior containers are created via raw ``podman run`` and carry **no**
    ``klangk.*`` labels, so klangkd's dead-owner reaper (#2430) cannot see
    them — they would run forever (#2443). This sweep is the only cleanup
    path for them, so the smoketest calls it before starting its own fixture
    (clean slate) and exposes it via ``--cleanup`` for recovering an existing
    mess.

    Returns the names removed. Best-effort: podman being absent or timing out
    is not fatal (returns whatever was removed so far).
    """
    removed: list[str] = []
    try:
        res = subprocess.run(
            ["podman", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return removed
    for name in res.stdout.split():
        # Match only the fixture's own prefixes (``--filter name=`` is a
        # substring match and could over-select); an unrelated container is
        # never touched.
        if not name.startswith(_FIXTURE_PREFIXES):
            continue
        try:
            subprocess.run(
                ["podman", "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            removed.append(name)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
    return removed


class ControlledDns:
    """A controlled-DNS + multi-IP HTTP/HTTPS fixture (#2424).

    Usage::

        dns = ControlledDns()
        dns.start()                          # brings up target + dns containers
        ip_a = dns.allocate("snap-a.test")   # single stable IP
        ip_b = dns.allocate("snap-b.test")   # a *different* stable IP
        # ... point the sidecar at dns.upstream_ip via KLANGKNETWORK_EGRESS_UPSTREAM
        dns.stop()

    Allocate before the sidecar queries the name (the dns container reads the
    live map per query, so late allocations are picked up). ``stop()`` is
    idempotent and safe from any thread.
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        network: str = DEFAULT_NETWORK,
        forward_upstream: str = _DEFAULT_FORWARD,
        ip_slice_size: int = _IP_SLICE_SIZE,
    ) -> None:
        self.image = image
        self.network = network
        self.forward_upstream = forward_upstream
        self.ip_slice_size = ip_slice_size
        self._p = _Procs()
        self._lock = threading.Lock()
        self._next = 0  # next secondary-IP slot to hand out
        self._map: dict[str, str] = {}
        self._target_script = ""
        self._dns_script = ""

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Bring up the target + dns containers and publish an empty map."""
        if self._p.target:
            return  # already started
        d = tracked_mkdtemp("ctrl-dns-")
        self._p.dir = d
        self._p.map_path = os.path.join(d, "map.json")
        self._write_map()

        # Write the two in-container scripts into the shared dir; the whole dir
        # is bind-mounted read-only into both containers at /mnt (the scripts +
        # the cert below + the live map all live there).
        self._target_script = os.path.join(d, "target.py")
        self._dns_script = os.path.join(d, "cdns_server.py")
        with open(self._target_script, "w") as f:
            f.write(_TARGET_PY)
        with open(self._dns_script, "w") as f:
            f.write(_DNS_PY)

        # Self-signed cert for the target's HTTPS listener, generated on the
        # host (the sidecar image ships no openssl). curl -k skips validation,
        # so any CN/validity is fine; 100 days so a checked-out worktree doesn't
        # need it regenerated for a long while.
        self._gen_cert(d)

        # 1) target: dynamic primary IP (for subnet detection) + NET_ADMIN so it
        #    can add the secondary slice it serves on. The secondary slice is
        #    derived from the gateway once we know it (below).
        self._p.target = "ctrl-dns-target-%d" % os.getpid()
        podman(
            "run",
            "-d",
            "--name",
            self._p.target,
            "--network",
            self.network,
            "--cap-add",
            "NET_ADMIN",
            "--entrypoint",
            "[]",
            "-v",
            "%s:/mnt:ro" % d,
            self.image,
            "python3",
            "/mnt/target.py",
        )
        gw = _net_gw(self._p.target, self.network)  # e.g. 10.88.0.1
        if not gw or gw == "none":
            raise RuntimeError(
                "controlled-dns: could not detect the podman gateway for "
                f"{self._p.target}; is the '{self.network}' network up?"
            )
        self._p.base = gw.rsplit(".", 1)[0]  # "10.88.0"
        self._p.ips = [
            "%s.%d" % (self._p.base, _IP_SLICE_START + i)
            for i in range(self.ip_slice_size)
        ]

        # Have the target actually add its secondary IPs and confirm each is
        # reachable from a peer before declaring readiness (avoids a race where
        # a phase curls before the addresses are installed).
        self._install_target_ips()
        self._wait_target_ready()

        # 2) dns: dynamic IP (this IS the sidecar upstream). Bind-mount the
        #    shared dir so it reads the live map at /mnt/map.json.
        self._p.dns = "ctrl-dns-dns-%d" % os.getpid()
        podman(
            "run",
            "-d",
            "--name",
            self._p.dns,
            "--network",
            self.network,
            "--entrypoint",
            "[]",
            "-v",
            "%s:/mnt:ro" % d,
            "-e",
            "CTRL_DNS_MAP=/mnt/map.json",
            "-e",
            "CTRL_DNS_FORWARD=%s" % self.forward_upstream,
            "-e",
            "CTRL_DNS_TTL=%d" % _DNS_TTL,
            self.image,
            "python3",
            "/mnt/cdns_server.py",
        )
        self._p.upstream_ip = _net_ip(self._p.dns, self.network)
        if not self._p.upstream_ip or self._p.upstream_ip == "none":
            raise RuntimeError(
                "controlled-dns: dns container has no IP on '%s'"
                % self.network
            )
        self._wait_dns_ready()

    def stop(self) -> None:
        """Tear down both containers + the temp dir (idempotent)."""
        with self._lock:
            for attr in ("dns", "target"):
                name = getattr(self._p, attr, "")
                if name:
                    try:
                        podman("rm", "-f", name, timeout=30)
                    except Exception:
                        pass
                    setattr(self._p, attr, "")
            if self._p.dir and os.path.isdir(self._p.dir):
                try:
                    import shutil

                    shutil.rmtree(self._p.dir, ignore_errors=True)
                except Exception:
                    pass
                self._p.dir = ""

    # -- the live name->IP map -------------------------------------------
    def allocate(self, name: str) -> str:
        """Assign ``name`` a single stable target IP (distinct per name).

        Returns the IP. Repeated calls for the same name return the same IP.
        The map is published atomically so the dns container picks it up on its
        next query.
        """
        with self._lock:
            key = name.rstrip(".").lower()
            if key in self._map:
                return self._map[key]
            if self._next >= len(self._p.ips):
                raise RuntimeError(
                    "controlled-dns: IP slice exhausted (%d names); raise "
                    "ip_slice_size" % len(self._p.ips)
                )
            ip = self._p.ips[self._next]
            self._next += 1
            self._map[key] = ip
            self._write_map()
            return ip

    def allocate_pair(self, apex: str, sub: str) -> tuple[str, str]:
        """Two names guaranteed to resolve to *distinct* IPs."""
        return self.allocate(apex), self.allocate(sub)

    def co_locate(self, name: str, sibling: str) -> str:
        """Map ``name`` to the SAME IP as an already-allocated ``sibling``.

        Two co-resident hostnames (same IP) share one L3/L4 iptables rule in
        the sidecar today, so allowing one affects the other (#2352). Returns
        the shared IP. ``sibling`` must already be allocated (via
        :meth:`allocate` / :meth:`allocate_pair`); ``name`` is then pointed at
        its IP rather than getting a fresh one.
        """
        with self._lock:
            sib_key = sibling.rstrip(".").lower()
            if sib_key not in self._map:
                raise RuntimeError(
                    "controlled-dns: cannot co-locate %r with unallocated %r"
                    % (name, sibling)
                )
            key = name.rstrip(".").lower()
            ip = self._map[sib_key]
            self._map[key] = ip
            self._write_map()
            return ip

    def ip_for(self, name: str) -> str:
        with self._lock:
            return self._map.get(name.rstrip(".").lower(), "")

    @property
    def upstream_ip(self) -> str:
        """The dns container's bridge IP -- set this as the sidecar upstream."""
        return self._p.upstream_ip

    @property
    def target_ips(self) -> list[str]:
        return list(self._p.ips)

    # -- internals --------------------------------------------------------
    def _write_map(self) -> None:
        """Atomically publish the name->IP map (tmp file + rename)."""
        tmp = self._p.map_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._map, f)
        os.replace(tmp, self._p.map_path)

    def _gen_cert(self, d: str) -> None:
        """Generate a self-signed cert+key into ``d`` (host openssl)."""
        cert = os.path.join(d, "ctrl-cert.pem")
        key = os.path.join(d, "ctrl-key.pem")
        r = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "100",
                "-nodes",
                "-subj",
                "/CN=controlled-dns",
            ],
            capture_output=True,
            text=True,
        )
        if (
            r.returncode != 0
            or not os.path.exists(cert)
            or not os.path.exists(key)
        ):
            raise RuntimeError(
                "controlled-dns: host openssl cert generation failed (rc=%d): %s"
                % (r.returncode, r.stderr.strip()[:200])
            )

    def _install_target_ips(self) -> None:
        env = "CTRL_TARGET_IPS=" + ",".join(self._p.ips)
        # exec ip addr add for each; ignore "already exists" duplicates.
        podman(
            "exec",
            "-e",
            env,
            self._p.target,
            "sh",
            "-c",
            "for ip in $(echo $CTRL_TARGET_IPS | tr ',' ' '); do "
            "ip addr add $ip/16 dev eth0 2>/dev/null || true; done",
        )

    def _wait_target_ready(self, timeout: float = 30.0) -> None:
        """A peer on the network can open a TCP connection to each target IP.

        Retries until ``timeout``. Each :meth:`_probe_target` iteration is
        bounded by the single per-IP connect timeout (the IPs are probed in
        parallel), so this budget buys several retries rather than being eaten
        by one slow iteration (#2473).
        """
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self._probe_target()
            if not last:
                return
            time.sleep(0.5)
        raise RuntimeError(
            "controlled-dns: target did not become reachable on all IPs within "
            f"{timeout:.0f}s; last={last!r}"
        )

    def _probe_target(self) -> str:
        """'' if every target IP accepts a TCP :443 connect, else an error.

        The probe container connects to every target IP **in parallel** (one
        thread per IP, each with its own 3s connect timeout), so one iteration
        is bounded by ~3s + podman overhead rather than 31x3s. A sequential
        probe made the readiness deadline shorter than a single iteration, so
        a transiently slow target never got a retry (#2473).
        """
        # Run a single ephemeral probe container that tries every IP at once.
        script = (
            "import socket,threading\n"
            "ips=%r\n"
            "bad=[]\n"
            "lock=threading.Lock()\n"
            "def probe(ip):\n"
            "    try:\n"
            "        s=socket.create_connection((ip,443),timeout=3); s.close()\n"
            "    except Exception as e:\n"
            "        with lock: bad.append(ip+':'+type(e).__name__)\n"
            "ts=[threading.Thread(target=probe,args=(ip,)) for ip in ips]\n"
            "for t in ts: t.start()\n"
            "for t in ts: t.join()\n"
            "print('BAD '+','.join(sorted(bad)) if bad else 'OK')\n"
        ) % (self._p.ips,)
        try:
            r = subprocess.run(
                [
                    "podman",
                    "run",
                    "--rm",
                    "--network",
                    self.network,
                    "--entrypoint",
                    "[]",
                    self.image,
                    "python3",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            return "probe raised: %r" % (e,)
        out = (r.stdout or "").strip()
        if out == "OK":
            return ""
        return out or r.stderr.strip()

    def _wait_dns_ready(self, timeout: float = 20.0) -> None:
        """The dns container forwards a real-name query (proves it's up)."""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self._probe_dns_forward()
            if not last:
                return
            time.sleep(0.5)
        raise RuntimeError(
            "controlled-dns: dns did not forward a real query within %.0fs; "
            "last=%r" % (timeout, last)
        )

    def _probe_dns_forward(self) -> str:
        """'' if the dns forwards example.com to a real A record, else error."""
        script = (
            "import socket\n"
            "import dns.message, dns.rdatatype\n"
            "q=dns.message.make_query('example.com','A')\n"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(5)\n"
            "s.sendto(q.to_wire(),(%r,53))\n"
            "try:\n"
            "    d,_=s.recvfrom(65535); r=dns.message.from_wire(d)\n"
            "    ips=[a.address for an in r.answer if an.rdtype==dns.rdatatype.A for a in an]\n"
            "    print('OK' if ips else 'EMPTY')\n"
            "except Exception as e:\n"
            "    print('ERR '+type(e).__name__)\n"
        ) % (self._p.upstream_ip,)
        try:
            r = subprocess.run(
                [
                    "podman",
                    "run",
                    "--rm",
                    "--network",
                    self.network,
                    "--entrypoint",
                    "[]",
                    self.image,
                    "python3",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            return "probe raised: %r" % (e,)
        out = (r.stdout or "").strip()
        return "" if out == "OK" else (out or r.stderr.strip())


# Convenience for ad-hoc / debug use from a shell.
if __name__ == "__main__":  # pragma: no cover
    import sys

    c = ControlledDns()
    try:
        c.start()
        a = c.allocate("snap-a.test")
        b = c.allocate("snap-b.test")
        print("upstream_ip =", c.upstream_ip)
        print("snap-a.test =", a)
        print("snap-b.test =", b)
        print("target_ips  =", c.target_ips)
        print("OK -- Ctrl-C to stop")
        while True:
            time.sleep(5)
    finally:
        c.stop()
        sys.exit(0)
