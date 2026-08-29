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
(``--network container:<network-sidecar>``, launched with
``--cap-drop net_raw`` and gosu-dropped to the non-root klangk user —
exactly how klangk launches a filtered workspace since #2347 removed
the enable_ping cap grant) and must:

  * resolve ``allowed.test`` to 1.2.3.4 (the proxy forwards to the upstream,
    learns the IP, returns it — proving the mark-based loop-avoidance works),
  * be **unable** to query the upstream directly: a direct ``@<upstream>
    exfil.test`` is REDIRECTed to the proxy (not the upstream) and denied ->
    NXDOMAIN, never 6.6.6.6. This is the #2264 fix — the mark scopes upstream
    access to the proxy; the workspace does not hold net_raw at all (#2347
    drops it unconditionally), so it cannot ``setsockopt(SO_MARK)`` to skip
    the REDIRECT, and all its :53 traffic is forced through the allow-listing
    proxy. (#2276 — see test_somark_bypass_blocked_under_production_caps.)

Requires: ``podman`` + Linux (NET_ADMIN netns + iptables). Skips otherwise.

Run with: ``devenv shell -- test-backend-e2e test_network_sidecar_e2e.py``
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
import uuid

import pytest

from _e2e_server import tracked_mkdtemp

# The network sidecar image source (entrypoint.sh + Dockerfile), relative to
# this e2e-tests dir. The proxy itself arrives as the klangksidecar wheel
# (built from src/klangksidecar, #2450) and is staged into the build via a named
# context (``--build-context sidecar=``), mirroring
# scripts/build-network-sidecar.sh — the image installs the wheel rather than
# a hand-copied proxy.py.
_NETWORK_SIDECAR_SRC = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "containers", "network"
    )
)
# Repo root, for ``uv build`` (the workspace pyproject lives there).
_REPO_ROOT = os.path.normpath(
    os.path.join(_NETWORK_SIDECAR_SRC, "..", "..", "..")
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
import os
import dns.resolver

# Replicate production (#2276): start as root, gosu-drop to the non-root klangk
# user (uid 1000) — the drop clears effective caps, so the workspace cannot
# setsockopt(SO_MARK). See _SOMARK_PROBE_PY for the explicit bypass probe.
os.setgid(1000)
os.setuid(1000)

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

# The #2264/#2276 bypass probe: arm a UDP socket with SO_MARK and, if it
# succeeds, send a DNS query DIRECTLY to the upstream (a marked packet skips the
# nat REDIRECT, which RETURNs marked traffic, and matches the filter's marked
# upstream ACCEPT). Prints one line:
#   "SO_MARK_EPERM"                   the workspace user lacks an *effective*
#                                     cap (production: non-root) — bypass CLOSED
#   "DIRECT <ip>,<ip>..." | "DIRECT EMPTY"  SO_MARK succeeded and the query
#                                     reached the upstream directly — bypass OPEN
#   "DIRECT_ERR <ExcType>"            SO_MARK succeeded but the query failed
#                                     (e.g. the filter dropped it) — not an exfil
# A filtered workspace's non-root user must get SO_MARK_EPERM; seeing the exfil
# IP (6.6.6.6) in a DIRECT line means the bypass is open.
_SOMARK_PROBE_PY = """\
import socket
import sys
import os
import dns.message

name = sys.argv[1]
server = sys.argv[2]
mark = int(sys.argv[3]) if len(sys.argv) > 3 else 75
# The #2264 bypass guard is USER-NAMESPACE ISOLATION (review #1 of the egress
# stack): the workspace runs in its own keep-id userns, distinct from the one
# that owns the network sidecar's netns, so its caps are not valid there and
# setsockopt(SO_MARK) EPERMs even though the klangk user has net_raw effective
# (podman promotes --cap-add caps to ambient for a non-root init). The faithful
# production repro launches this probe with --userns=keep-id:uid=1000,gid=1000
# (see _probe_somark(keep_id=True)); the probe is then already uid 1000, so no
# in-process drop is needed. The legacy default-userns repro below drops to
# uid 1000 in-process ONLY when PID 1 is root and we were not told to stay root
# (the filtered+sudo case, #2276 B); pass a 4th arg ("root") to stay root.
if os.getuid() == 0 and not (len(sys.argv) > 4 and sys.argv[4] == "root"):
    os.setgid(1000)
    os.setuid(1000)

q = dns.message.make_query(name, "A")
wire = q.to_wire()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
except PermissionError:
    print("SO_MARK_EPERM")
    sys.exit(0)

# Marked: this packet skips the nat REDIRECT and matches the filter's marked
# upstream ACCEPT — if the upstream answers, the bypass is open.
s.settimeout(4)
try:
    s.sendto(wire, (server, 53))
    data, _ = s.recvfrom(65535)
    resp = dns.message.from_wire(data)
    ips = [
        r.address
        for rr in resp.answer
        if rr.rdtype == 1
        for r in rr
    ]
    if ips:
        print("DIRECT " + ",".join(ips))
    else:
        print("DIRECT EMPTY")
except Exception as exc:
    print("DIRECT_ERR " + type(exc).__name__)
"""


def _podman(*args, check=True, timeout=120):
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _podman_exists(kind, name):
    """Exit-code probe: is the podman object still there?"""
    try:
        return (
            _podman(kind, "exists", name, check=False, timeout=10).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        # Probe itself wedged: assume present so the caller keeps trying.
        return True


def _podman_cleanup(kind, *targets, attempts=3, timeout=10):
    """Best-effort teardown: state-checked, sleep-free (#2616).

    Fixed sleeps are this suite's main source of flakes, so none remain
    here, including podman's own:

    - ``rm -f -t 0``: podman's default stop sequence SIGTERMs the container
      and waits up to 10 s before SIGKILL. ``-t 0`` goes straight to
      SIGKILL, removing that hidden fixed wait from every teardown.
    - A wedged invocation is killed by the subprocess timeout and retried
      immediately — a fresh podman start carries its own latency, which is
      all the spacing a retry needs.
    - Progress is measured against actual state, not the clock: after each
      attempt ``podman <kind> exists`` decides whether to try again.

    A target that survives every attempt only warns: all teardown names
    here are uuid-suffixed, so leftovers cannot collide with later runs,
    and the CI VM is ephemeral. Worst case is attempts x timeout per
    target, so even a multi-container teardown stays well under the 300 s
    per-test timeout stamp.
    """
    verb = {
        "container": ("rm", "-f", "-t", "0"),
        "network": ("network", "rm"),
        "image": ("rmi", "-f"),
    }[kind]
    for target in targets:
        for attempt in range(1, attempts + 1):
            try:
                _podman(*verb, target, check=False, timeout=timeout)
            except subprocess.TimeoutExpired:
                print(
                    f"podman {' '.join(verb)} {target} timed out after "
                    f"{timeout}s (attempt {attempt}/{attempts})"
                )
            if not _podman_exists(kind, target):
                break
        else:
            print(
                f"WARNING: podman {' '.join(verb)} {target} still present "
                f"after {attempts} attempts; leaving it (uuid-suffixed "
                "leftover, ephemeral CI VM)"
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
    # Build the klangksidecar wheel (the proxy package, #2450) and stage it as
    # the named context the Dockerfile consumes (COPY --from=sidecar). The
    # image pip-installs the wheel rather than a hand-copied proxy.py, so the
    # package can grow multifile without the build changing.
    wheel_dir = tracked_mkdtemp("netc-e2e-whl-")
    uv = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "klangksidecar",
            "--wheel",
            "--out-dir",
            wheel_dir,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert uv.returncode == 0, uv.stderr
    build = _podman(
        "build",
        "-q",
        "-t",
        tag,
        "--build-context",
        f"sidecar={wheel_dir}",
        _NETWORK_SIDECAR_SRC,
        timeout=300,
    )
    assert build.returncode == 0, build.stderr
    tmp = tracked_mkdtemp("netc-e2e-")
    fu = os.path.join(tmp, "fake_upstream.py")
    wq = os.path.join(tmp, "ws_query.py")
    sm = os.path.join(tmp, "somark_probe.py")
    with open(fu, "w") as fh:
        fh.write(_FAKE_UPSTREAM_PY)
    with open(wq, "w") as fh:
        fh.write(_WS_QUERY_PY)
    with open(sm, "w") as fh:
        fh.write(_SOMARK_PROBE_PY)
    yield {
        "image": tag,
        "fake_upstream": fu,
        "ws_query": wq,
        "somark_probe": sm,
    }
    _podman_cleanup("image", tag, timeout=30)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(wheel_dir, ignore_errors=True)


def _ip_of(name):
    out = _podman(
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        name,
    )
    return out.stdout.strip()


def _free_port():
    """A free host TCP port for the sidecar to publish (best-effort; tiny
    TOCTOU window before podman rebinds it)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
    # --disable-dns: nothing on these networks resolves by container name
    # (raw IPs via _ip_of), and aardvark-dns restarts race under concurrent
    # xdist workers ("bind udp 10.89.0.1:53: address already in use").
    _podman("network", "create", "--disable-dns", net)
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
        _podman_cleanup("container", nc, fu)
        _podman_cleanup("network", net)


def _query(env, stack, name, server=None):
    """Run a one-shot workspace container sharing the network sidecar's netns.

    Returns the ws_query.py stdout (one line: 'A ...' / 'NXDOMAIN' / 'ERR ...').

    Mirrors how klangk launches a filtered workspace's NETWORK sidecar join
    (#2264): the container shares the sidecar's netns with --cap-drop net_raw
    (the unconditional #2347 posture) and an in-process drop to uid 1000. DNS
    resolution (UDP :53, REDIRECTed to the proxy) does not depend on the
    workspace's userns or caps, so this simpler launch is fine for the
    resolution/exfil tests; the explicit SO_MARK bypass attempt — which DOES
    depend on cap posture / user-namespace isolation — lives in
    test_somark_bypass_blocked_under_production_userns (a faithful keep-id repro).

    The ``podman run`` itself can wedge under xdist load on the CI runner
    (the #2616 class — the query's own DNS lifetime is 6s, so a 60s
    timeout can only mean podman never came back). The query is one-shot
    and read-only, so a wedged invocation is killed by the timeout and
    retried immediately — a fresh podman start carries its own latency,
    which is all the spacing a retry needs (the #2619 teardown rationale,
    applied to the run path). A genuinely blocked answer still fails the
    assertion on the retried attempt; only the harness wedges.
    """
    _, network_sidecar = stack
    args = ["/wq.py", name]
    if server:
        args.append(server)
    run_args = (
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
    )
    last_exc = None
    for _attempt in range(2):
        try:
            out = _podman(*run_args, timeout=60)
            return out.stdout.strip()
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            print(
                f"podman run {name!r} timed out after 60s "
                "(wedged under load); retrying"
            )
    raise last_exc


def _probe_somark(
    env,
    stack,
    name,
    server,
    *,
    mark=75,
    as_root=False,
    cap_drop=False,
    keep_id=False,
):
    """Run the SO_MARK bypass probe as a filtered workspace.

    Returns the probe's stdout (see _SOMARK_PROBE_PY).

    * ``keep_id``: launch with ``--userns=keep-id:uid=1000,gid=1000`` — the
      FAITHFUL production userns repro (review #1) — plus ``--cap-add
      net_raw``. #2347 removed the cap grant, so production workspaces never
      hold net_raw; it is granted here deliberately to prove the guard holds
      against the worst case (an adversary that somehow reacquires the cap):
      the probe is uid 1000 from PID 1 (no in-process drop), the cap is
      effective, and SO_MARK must still EPERM via user-namespace isolation —
      the real bypass guard. Incompatible with ``as_root``/``cap_drop``
      (those model the legacy default-userns sudo case).
    * ``as_root``: stay root in the probe (simulate ``sudo``->root) instead of
      dropping to uid 1000 (the legacy default-userns non-root repro).
    * ``cap_drop``: launch with ``--cap-drop net_raw`` — the production
      launch for every workspace since #2347 (previously the filtered+sudo
      config, #2276 B).
    """
    _, network_sidecar = stack
    if keep_id:
        # Production-style launch: own keep-id userns. The probe skips its
        # in-process drop (it is already uid 1000). net_raw is granted
        # artificially (see above): strictly stronger than production.
        userns = ["--userns", "keep-id:uid=1000,gid=1000"]
        caps = ["--cap-add", "net_raw"]
        args = ["/probe.py", name, server, str(mark)]
    else:
        userns = []
        caps = (
            ["--cap-drop", "net_raw"] if cap_drop else ["--cap-add", "net_raw"]
        )
        args = ["/probe.py", name, server, str(mark)]
        if as_root:
            args.append("root")  # 4th arg => probe stays root (sudo->root)
    out = _podman(
        "run",
        "--rm",
        "--network",
        f"container:{network_sidecar}",
        *userns,
        *caps,
        "-v",
        f"{env['somark_probe']}:/probe.py:ro",
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

    def test_ipv6_egress_is_default_denied(self, env, stack):
        # #1936/#2255: the sidecar sets ip6tables -P OUTPUT DROP so an IPv6
        # path can't bypass the v4 allow-list. ip6tables ships in the same
        # alpine iptables package.
        _, network_sidecar = stack
        v6 = _podman(
            "exec", network_sidecar, "ip6tables", "-S", "OUTPUT"
        ).stdout
        assert any("-P OUTPUT DROP" in ln for ln in v6.splitlines()), (
            f"ip6tables OUTPUT policy is not DROP:\n{v6}"
        )

    def test_consent_recording_installs_nfqueue_rule(self, env):
        # #2242: whenever a consent endpoint is configured, the sidecar queues
        # blocked packets (--queue-num 5139) for its NFQUEUE consumer (proxy.py).
        # Recording is mode-independent (static vs interactive only affects the
        # recorded decision, applied by klangkd). Standalone sidecar (the rule
        # is installed before the proxy runs; no fake upstream needed).
        net = f"netc-nf-{uuid.uuid4().hex[:8]}"
        nc = f"netc-nf-{uuid.uuid4().hex[:8]}"
        # --disable-dns: see stack() — aardvark races under xdist.
        _podman("network", "create", "--disable-dns", net)
        try:
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
                "KLANGKNETWORK_EGRESS_UPSTREAM=8.8.8.8",
                "-e",
                "KLANGKNETWORK_EGRESS_ALLOW=allowed.test",
                "-e",
                "KLANGKNETWORK_EGRESS_CONSENT_URL=http://fake-klangkd:8995/internal/egress-consent/events",
                env["image"],
            )
            _wait_ready(nc)
            rules = _podman("exec", nc, "iptables", "-S", "OUTPUT").stdout
            assert any(
                "-j NFQUEUE" in ln and "5139" in ln
                for ln in rules.splitlines()
            ), f"no NFQUEUE queue-5139 rule in interactive mode:\n{rules}"
            # The NFQUEUE consumer thread (proxy.py) binds after the proxy is
            # ready; poll its log to prove the consumer runs in the real image.
            deadline = time.monotonic() + 20
            bound = False
            while time.monotonic() < deadline:
                logs = _podman("logs", nc, check=False).stdout
                if "nfqueue consumer bound to queue 5139" in logs:
                    bound = True
                    break
                time.sleep(0.5)
            assert bound, (
                "NFQUEUE consumer did not bind in interactive mode:\n"
                f"{_podman('logs', nc, check=False).stdout}"
            )
        finally:
            _podman_cleanup("container", nc)
            _podman_cleanup("network", net)

    def test_somark_bypass_blocked_under_production_userns(self, env, stack):
        # Review #1 of the egress stack: a filtered workspace launches in its
        # OWN keep-id userns (--userns=keep-id:uid=1000,gid=1000), distinct from
        # the userns that owns the network sidecar's netns. SO_MARK needs
        # CAP_NET_RAW/NET_ADMIN in the netns-OWNER's userns, so even though the
        # klangk user has net_raw EFFECTIVE (podman promotes --cap-add caps to
        # ambient for a non-root init), setsockopt(SO_MARK) EPERMs and the
        # workspace cannot skip the nat REDIRECT to reach the upstream directly
        # (the #2264 bypass). This is the FAITHFUL production-userns repro (the
        # probe runs as uid 1000 from PID 1, no in-process drop); since #2347
        # production never grants the workspace net_raw, so the probe's cap is
        # artificial — a worst-case adversary that somehow reacquired it. It
        # must get SO_MARK_EPERM and NEVER the exfil IP. A future change that
        # makes the workspace share the sidecar's userns (e.g. emptying
        # KLANGKD_USERNS) would flip this to SO_MARK_OK and fail here.
        upstream_ip, _ = stack
        out = _probe_somark(
            env, stack, "exfil.test", upstream_ip, keep_id=True
        )
        assert "6.6.6.6" not in out, (
            f"SO_MARK bypass OPEN under production userns — exfil IP reached:\n"
            f"{out}"
        )
        assert out.startswith("SO_MARK_EPERM"), (
            f"expected SO_MARK_EPERM (userns isolation; cap is effective but "
            f"not valid in the sidecar's netns), got:\n{out}"
        )

    def test_somark_bypass_closed_for_filtered_sudo_workspace(
        self, env, stack
    ):
        # #2276 (B) → #2347: every workspace drops net_raw (unconditionally
        # now) so even root (via sudo) cannot SO_MARK. Run the probe AS ROOT
        # (simulate sudo->root) against a workspace launched with
        # --cap-drop net_raw — the production launch; it
        # must get SO_MARK_EPERM, never the exfil IP. This pins the invariant
        # the fix relies on: net_raw is not in the bounding set, so root has no
        # more egress power than the non-root user for filtered traffic. NET_ADMIN
        # is never granted, so dropping net_raw alone closes SO_MARK (which needs
        # NET_ADMIN or NET_RAW) — if a future change grants either, this fails.
        upstream_ip, _ = stack
        out = _probe_somark(
            env, stack, "exfil.test", upstream_ip, as_root=True, cap_drop=True
        )
        assert "6.6.6.6" not in out, (
            f"SO_MARK bypass OPEN for filtered+sudo root — exfil IP reached:\n"
            f"{out}"
        )
        assert out.startswith("SO_MARK_EPERM"), (
            f"expected SO_MARK_EPERM (net_raw dropped; root can't mark), "
            f"got:\n{out}"
        )

    def test_host_port_published_on_sidecar_reaches_workspace(self, env):
        # #2267: a filtered workspace cannot publish host ports itself
        # (--publish is discarded under --network container:), so klangk
        # publishes them on the network sidecar instead. The workspace shares
        # the sidecar's netns, so the sidecar's --publish forwards into it and
        # reaches the workspace's listener. Prove end-to-end (real podman, real
        # sidecar image + iptables) that a host port reaches a workspace
        # listener THROUGH the sidecar's publish -- the whole point of routing
        # publish onto the sidecar.
        import urllib.request

        host_port = _free_port()
        net = f"netc-pp-{uuid.uuid4().hex[:8]}"
        nc = f"netc-pp-nc-{uuid.uuid4().hex[:8]}"
        ws = f"netc-pp-ws-{uuid.uuid4().hex[:8]}"
        # --disable-dns: see stack() — aardvark races under xdist.
        _podman("network", "create", "--disable-dns", net)
        try:
            # The sidecar owns the netns and publishes host_port -> :8000.
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
                "--publish",
                f"{host_port}:8000",
                "-e",
                "KLANGKNETWORK_EGRESS_UPSTREAM=8.8.8.8",
                "-e",
                "KLANGKNETWORK_EGRESS_ALLOW=allowed.test",
                env["image"],
            )
            _wait_ready(nc)
            # The workspace shares the sidecar's netns and binds :8000.
            _podman(
                "run",
                "-d",
                "--name",
                ws,
                "--network",
                f"container:{nc}",
                "--entrypoint",
                "python3",
                env["image"],
                "-m",
                "http.server",
                "8000",
                "--bind",
                "0.0.0.0",
            )
            # Reach the workspace's listener through the SIDECAR's published
            # port (127.0.0.1 avoids the IPv6 happy-eyeballs path the sidecar
            # kills). Poll briefly for http.server to finish binding.
            deadline = time.monotonic() + 8
            code = None
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{host_port}/", timeout=2
                    ) as resp:
                        code = resp.status
                        break
                except Exception:
                    time.sleep(0.3)
            assert code == 200, (
                f"published port {host_port} did not reach the workspace "
                f"listener (HTTP {code}); the sidecar must publish the "
                f"workspace's host ports (#2267)."
            )
        finally:
            _podman_cleanup("container", ws, nc)
            _podman_cleanup("network", net)


# ---------------------------------------------------------------------------
# SYN consent gate e2e (#2327), driven by #2336: concurrent flows must both
# produce consent requests at the verifier concurrently -- not one held behind
# the other (the pre-#2331 blocking-consumer serialization).
# ---------------------------------------------------------------------------

# Fake upstream that resolves two NON-allow-listed hosts (so they hit consent).
_CONSENT_UPSTREAM_PY = """\
import socket
import dns.message
import dns.rrset
import dns.rcode

RESOLVE = {"repoze.test": "1.1.1.1", "ford.test": "2.2.2.2"}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 53))
while True:
    data, addr = s.recvfrom(65535)
    try:
        q = dns.message.from_wire(data)
        name = q.question[0].name
        lname = name.to_text().rstrip(".").lower()
        resp = dns.message.make_response(q)
        if lname in RESOLVE:
            resp.answer.append(
                dns.rrset.from_text(name, 60, "IN", "A", RESOLVE[lname])
            )
        else:
            resp.set_rcode(dns.rcode.NXDOMAIN)
        s.sendto(resp.to_wire(), addr)
    except Exception:
        pass
"""

# Stub consent verifier: accepts the sidecar's egress-sidecar WS, records each
# egress frame's dst to stdout, and NEVER sends a verdict (holds forever) -- so
# a serialized sidecar would only ever report the first flow.
_CONSENT_VERIFIER_PY = """\
import asyncio
import json
import websockets

async def handler(ws):
    print("CONNECTED", flush=True)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "egress":
                print(f"EGRESS {msg.get('dst')}", flush=True)
    except websockets.ConnectionClosed:
        pass

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8995):
        await asyncio.Future()

asyncio.run(main())
"""

# Workspace trigger: resolve two hosts (via the sidecar DNS) + open TCP
# connections to both concurrently. The SYNs hit NFQUEUE + are held for consent.
# Blocks (the connects hang while held) for the test duration.
_CONSENT_TRIGGER_PY = """\
import socket
import threading

def connect(host):
    try:
        infos = socket.getaddrinfo(host, 80)
        for family, type_, proto, canon, sockaddr in infos:
            s = socket.socket(family, type_, proto)
            s.settimeout(60)
            try:
                s.connect(sockaddr)  # SYN -> NFQUEUE -> held for consent
            except Exception:
                pass
    except Exception:
        pass

t1 = threading.Thread(target=connect, args=("repoze.test",))
t2 = threading.Thread(target=connect, args=("ford.test",))
t1.start()
t2.start()
t1.join()
t2.join()
"""


def _wait_log(name, needle, timeout=20):
    """Poll ``podman logs <name>`` for ``needle`` or fail with the logs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = _podman("logs", name, check=False).stdout
        if needle in logs:
            return
        time.sleep(0.5)
    pytest.fail(
        f"{needle!r} not in {name} logs within {timeout}s\\n"
        f"logs:\\n{_podman('logs', name, check=False).stdout}"
    )


@pytest.fixture
def consent_stack(env):
    """Fake upstream (resolves repoze.test+ford.test) + a stub consent verifier
    (WS) + the sidecar pointed at it. Yields a dict with the container names +
    the trigger script path + image."""
    _require_platform()
    tmp = tracked_mkdtemp("consent-e2e-")
    fu = os.path.join(tmp, "consent_upstream.py")
    ver = os.path.join(tmp, "consent_verifier.py")
    trig = os.path.join(tmp, "consent_trigger.py")
    tok = os.path.join(tmp, "workspace-token")
    with open(fu, "w") as fh:
        fh.write(_CONSENT_UPSTREAM_PY)
    with open(ver, "w") as fh:
        fh.write(_CONSENT_VERIFIER_PY)
    with open(trig, "w") as fh:
        fh.write(_CONSENT_TRIGGER_PY)
    with open(tok, "w") as fh:
        fh.write("dummy-token")
    net = f"consent-e2e-{uuid.uuid4().hex[:8]}"
    fu_c = f"consent-fu-{uuid.uuid4().hex[:8]}"
    ver_c = f"consent-ver-{uuid.uuid4().hex[:8]}"
    nc = f"consent-nc-{uuid.uuid4().hex[:8]}"
    # --disable-dns: see stack() — aardvark races under xdist.
    _podman("network", "create", "--disable-dns", net)
    try:
        _podman(
            "run",
            "-d",
            "--name",
            fu_c,
            "--network",
            net,
            "--entrypoint",
            "python3",
            "-v",
            f"{fu}:/fu.py:ro",
            env["image"],
            "/fu.py",
        )
        fu_ip = _ip_of(fu_c)
        assert fu_ip, f"fake upstream {fu_c} has no IP"
        _podman(
            "run",
            "-d",
            "--name",
            ver_c,
            "--network",
            net,
            "--entrypoint",
            "python3",
            "-v",
            f"{ver}:/ver.py:ro",
            env["image"],
            "/ver.py",
        )
        ver_ip = _ip_of(ver_c)
        assert ver_ip, f"verifier {ver_c} has no IP"
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
            "-v",
            f"{tok}:/run/klangk/workspace-token:ro",
            "-e",
            f"KLANGKNETWORK_EGRESS_UPSTREAM={fu_ip}",
            # Allow the consent WS to the verifier (else its own SYN is NFQUEUE'd
            # -> the consumer holds the consent client's own connection: deadlock).
            "-e",
            f"KLANGKNETWORK_EGRESS_ALLOW=allowed.test,{ver_ip}/32:8995",
            "-e",
            f"KLANGKNETWORK_EGRESS_CONSENT_URL=http://{ver_ip}:8995",
            env["image"],
        )
        _wait_ready(nc)
        yield {
            "stub": ver_c,
            "sidecar": nc,
            "trigger": trig,
            "image": env["image"],
        }
    finally:
        _podman_cleanup("container", nc, ver_c, fu_c)
        _podman_cleanup("network", net)
        shutil.rmtree(tmp, ignore_errors=True)


class TestConsentConcurrentFlows:
    """#2336: distinct concurrent flows must both reach the consent verifier
    while both are still pending (the verifier never verdicts). A serialized
    sidecar would only report the first flow -> this fails."""

    def test_two_concurrent_connections_both_reported(self, consent_stack):
        stub = consent_stack["stub"]
        nc = consent_stack["sidecar"]
        # The sidecar's consent WS must connect to the verifier first.
        _wait_log(stub, "CONNECTED", timeout=30)
        # Trigger two concurrent connections from a workspace sharing the
        # sidecar's netns. Both SYNs hit NFQUEUE + are held for consent.
        trig_c = f"consent-trig-{uuid.uuid4().hex[:8]}"
        try:
            _podman(
                "run",
                "-d",
                "--name",
                trig_c,
                "--network",
                f"container:{nc}",
                "--entrypoint",
                "python3",
                "-v",
                f"{consent_stack['trigger']}:/trig.py:ro",
                consent_stack["image"],
                "/trig.py",
            )
            # Both egress frames must arrive WITHOUT the verifier verdicting
            # either (it never does). A serialized sidecar would never send the
            # 2nd -> the _wait_log times out -> this test fails (#2336).
            _wait_log(stub, "EGRESS repoze.test", timeout=30)
            _wait_log(stub, "EGRESS ford.test", timeout=30)
        finally:
            _podman_cleanup("container", trig_c)
