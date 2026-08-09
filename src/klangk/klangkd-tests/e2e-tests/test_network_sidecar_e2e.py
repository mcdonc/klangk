"""End-to-end test for the FQDN egress network sidecar's DNS enforcement (#2264, #2250).

This is the enforcement test the netfilter e2e suite explicitly deferred
("does NOT verify enforcement ... that requires real DNS and remote hosts").
It uses **real podman**, the **real network sidecar image** (built from
``src/containers/network``), and a **hermetic fake upstream** DNS server so
outcomes are deterministic (no internet dependency):

  *.allowed.test -> A 1.2.3.4   (proxy must learn + return this)
  *.exfil.test   -> A 6.6.6.6   (the data-exfil target; a workspace reaching
                                  this *directly* is exactly the bypass #2264
                                  closes)
  anything else  -> NXDOMAIN

A "workspace" container shares the network sidecar's netns
(``--network container:<network-sidecar>``, ``--cap-drop net_raw`` — as klangk
launches a filtered workspace) and must:

  * resolve ``allowed.test`` to 1.2.3.4 (the proxy forwards to the upstream,
    learns the IP, returns it — proving the mark-based loop-avoidance works),
  * be **unable** to query the upstream directly: a direct ``@<upstream>
    exfil.test`` is REDIRECTed to the proxy (not the upstream) and denied ->
    NXDOMAIN, never 6.6.6.6. This is the #2264 fix — the mark scopes upstream
    access to the proxy; the workspace lacks CAP_NET_RAW/NET_ADMIN and cannot
    mark, so all its :53 traffic is forced through the allow-listing proxy.

Requires: ``podman`` + Linux (NET_ADMIN netns + iptables). Skips otherwise.

Run with: ``devenv shell -- test-backend-e2e test_network_sidecar_e2e.py``
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

# The network sidecar image source (proxy.py + entrypoint.sh + Dockerfile),
# relative to this e2e-tests dir.
_NETWORK_SIDECAR_SRC = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "containers", "network"
    )
)

# Reused as the "workspace" + fake-upstream image: it has python3 + dnspython
# (the network sidecar's deps), so a filtered workspace can issue real DNS
# queries and the fake upstream can serve them — no second image build.


_FAKE_UPSTREAM_PY = """\
import socket
import dns.message
import dns.rcode
import dns.rrset


def serve():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 53))
    print("fake-upstream listening on 0.0.0.0:53", flush=True)
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
        resp = dns.message.make_response(q)
        lname = name.to_text().rstrip(".").lower()
        if lname.endswith("allowed.test"):
            resp.answer.append(
                dns.rrset.from_text(name, 60, "IN", "A", "1.2.3.4")
            )
        elif lname.endswith("exfil.test"):
            resp.answer.append(
                dns.rrset.from_text(name, 60, "IN", "A", "6.6.6.6")
            )
        else:
            resp.set_rcode(dns.rcode.NXDOMAIN)
        try:
            s.sendto(resp.to_wire(), addr)
        except Exception:
            pass


serve()
"""

# Issues a real DNS query (UDP) for <name> at <server> (default 1.1.1.1 — the
# resolver the network sidecar configures and the workspace inherits). Prints
# one line: "A <ip>,<ip>..." | "NXDOMAIN" | "ERR ...".
_WS_QUERY_PY = """\
import sys
import dns.resolver

name = sys.argv[1]
server = sys.argv[2] if len(sys.argv) > 2 else "1.1.1.1"
r = dns.resolver.Resolver(configure=False)
r.nameservers = [server]
r.port = 53
r.lifetime = 6.0
r.timeout = 2.0
try:
    ans = r.resolve(name, "A")
    print("A " + ",".join(x.address for x in ans))
except dns.resolver.NXDOMAIN:
    print("NXDOMAIN")
except Exception as exc:
    print("ERR " + type(exc).__name__ + " " + str(exc))
"""


def _podman(*args, check=True, timeout=120):
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _require_platform():
    if shutil.which("podman") is None:
        pytest.skip("podman not on PATH")
    if platform.system() != "Linux":
        pytest.skip(
            "egress network sidecar e2e is Linux-only (NET_ADMIN netns + iptables)"
        )


@pytest.fixture(scope="module")
def env():
    """Build the network sidecar image + materialize the helper scripts."""
    _require_platform()
    tag = f"netc-e2e:{uuid.uuid4().hex[:8]}"
    build = _podman(
        "build", "-q", "-t", tag, _NETWORK_SIDECAR_SRC, timeout=300
    )
    assert build.returncode == 0, build.stderr
    tmp = tempfile.mkdtemp(prefix="netc-e2e-")
    fu = os.path.join(tmp, "fake_upstream.py")
    wq = os.path.join(tmp, "ws_query.py")
    with open(fu, "w") as fh:
        fh.write(_FAKE_UPSTREAM_PY)
    with open(wq, "w") as fh:
        fh.write(_WS_QUERY_PY)
    yield {"image": tag, "fake_upstream": fu, "ws_query": wq}
    _podman("rmi", "-f", tag, check=False, timeout=60)
    shutil.rmtree(tmp, ignore_errors=True)


def _ip_of(name):
    out = _podman(
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        name,
    )
    return out.stdout.strip()


def _wait_ready(name, timeout=40):
    """Wait for the network sidecar's proxy to print its listening line."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _podman("logs", name, check=False).stdout
        if "dns-proxy listening" in logs:
            return
        # Surface an early crash (e.g. SO_MARK / iptables failure).
        state = _podman(
            "inspect",
            "-f",
            "{{.State.Status}} {{.State.ExitCode}}",
            name,
            check=False,
        ).stdout.strip()
        if "exited" in state or "stopped" in state:
            pytest.fail(
                f"network sidecar {name} exited before ready. state={state}\n"
                f"logs:\n{_podman('logs', name, check=False).stdout}"
            )
        time.sleep(0.5)
    pytest.fail(
        f"network sidecar {name} not ready within {timeout}s\n"
        f"logs:\n{_podman('logs', name, check=False).stdout}"
    )


@pytest.fixture
def stack(env):
    """A fresh (fake-upstream + network sidecar) pair on an isolated network.

    Yields (upstream_ip, network_sidecar_name). The network sidecar
    allow-lists allowed.test and points its upstream at the fake server.
    """
    net = f"netc-e2e-{uuid.uuid4().hex[:8]}"
    fu = f"netc-fu-{uuid.uuid4().hex[:8]}"
    nc = f"netc-nc-{uuid.uuid4().hex[:8]}"
    _podman("network", "create", net)
    try:
        # Fake upstream: reuse the network sidecar image (python3 + dnspython),
        # override the entrypoint to run the fake server. Listens on :53.
        _podman(
            "run",
            "-d",
            "--name",
            fu,
            "--network",
            net,
            "--entrypoint",
            "python3",
            "-v",
            f"{env['fake_upstream']}:/fu.py:ro",
            env["image"],
            "/fu.py",
        )
        upstream_ip = _ip_of(fu)
        assert upstream_ip, f"fake upstream {fu} has no IP"

        # The network sidecar: NET_ADMIN, allow-lists allowed.test, forwards to
        # the fake upstream. --dns 1.1.1.1 is the resolver the workspace inherits
        # (and that the nat REDIRECT sends to the proxy).
        _podman(
            "run",
            "-d",
            "--name",
            nc,
            "--network",
            net,
            "--cap-add",
            "net_admin",
            "--dns",
            "1.1.1.1",
            "-e",
            f"KLANGKNETWORK_EGRESS_UPSTREAM={upstream_ip}",
            "-e",
            "KLANGKNETWORK_EGRESS_ALLOW=allowed.test",
            env["image"],
        )
        _wait_ready(nc)
        yield upstream_ip, nc
    finally:
        for c in (nc, fu):
            _podman("rm", "-f", c, check=False, timeout=60)
        _podman("network", "rm", net, check=False, timeout=60)


def _query(env, stack, name, server=None):
    """Run a one-shot workspace container sharing the network sidecar's netns.

    Returns the ws_query.py stdout (one line: 'A ...' / 'NXDOMAIN' / 'ERR ...').
    --cap-drop net_raw mirrors how klangk launches a filtered workspace (#2264):
    the workspace must be unable to set SO_MARK.
    """
    _, network_sidecar = stack
    args = ["/wq.py", name]
    if server:
        args.append(server)
    out = _podman(
        "run",
        "--rm",
        "--network",
        f"container:{network_sidecar}",
        "--cap-drop",
        "net_raw",
        "-v",
        f"{env['ws_query']}:/wq.py:ro",
        "--entrypoint",
        "python3",
        env["image"],
        *args,
        timeout=60,
    )
    return out.stdout.strip()


class TestNetworkSidecarE2E:
    """The network sidecar enforces FQDN egress + closes the upstream bypass."""

    def test_allowed_domain_resolves(self, env, stack):
        # The workspace's configured resolver (1.1.1.1) is REDIRECTed to the
        # proxy, which forwards (marked) to the upstream, learns 1.2.3.4, and
        # returns it. This also proves mark-based loop-avoidance works: the
        # proxy's forward reached the upstream instead of looping.
        assert _query(env, stack, "allowed.test") == "A 1.2.3.4"

    def test_direct_upstream_exfil_is_blocked(self, env, stack):
        # #2264: the workspace queries the UPSTREAM DIRECTLY (@upstream_ip) for
        # the exfil target. The mark-scoped nat REDIRECT sends it to the proxy
        # (not the upstream) — the proxy denies exfil.test -> NXDOMAIN. If the
        # bypass were open, the workspace would get 6.6.6.6.
        upstream_ip, _ = stack
        assert (
            _query(env, stack, "exfil.test", server=upstream_ip) == "NXDOMAIN"
        )

    def test_denied_domain_is_nxdomain(self, env, stack):
        # A name not on the allow-list is denied by the proxy.
        assert _query(env, stack, "denied.test") == "NXDOMAIN"

    def test_network_sidecar_marks_upstream_rule(self, env, stack):
        # The mechanism: the network sidecar's OUTPUT allows upstream:53 only
        # for marked packets (the proxy), and the nat REDIRECT exempts marked
        # packets.
        _, network_sidecar = stack
        rules = _podman(
            "exec", network_sidecar, "iptables", "-S", "OUTPUT"
        ).stdout
        assert any(
            "53" in ln and "mark" in ln.lower() for ln in rules.splitlines()
        ), f"no mark-scoped upstream:53 rule in OUTPUT:\n{rules}"
        nat = _podman(
            "exec", network_sidecar, "iptables", "-t", "nat", "-S", "OUTPUT"
        ).stdout
        assert "REDIRECT" in nat and "RETURN" in nat, (
            f"nat OUTPUT should RETURN marked + REDIRECT the rest:\n{nat}"
        )
