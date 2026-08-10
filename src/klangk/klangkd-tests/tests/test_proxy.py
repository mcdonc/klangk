"""Unit tests for the network sidecar's DNS proxy (``src/containers/network/proxy.py``).

``proxy.py`` is a standalone sidecar script — it lives under
``src/containers/network/`` (NOT in the ``klangk`` package, so it is not
coverage-gated) and is normally only exercised end-to-end by the real-podman
e2e. These tests import it as a module and drive its helpers directly.
``main()`` only runs under ``__name__ == "__main__"``, so importing is safe
(module level just reads env vars + builds the allow-list).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# proxy.py is a container script, not an importable package module — load it
# straight from its source path. test_proxy.py is at
# src/klangk/klangkd-tests/tests/, so parents[3] is the repo "src/".
_PROXY_PATH = (
    Path(__file__).resolve().parents[3] / "containers" / "network" / "proxy.py"
)


def _install_dns_stubs() -> None:
    """Stub dnspython so proxy.py imports in the server venv.

    dnspython is a sidecar-only dependency (installed in the network sidecar
    image, not the server's venv). proxy.py imports ``dns.message`` /
    ``dns.rcode`` / ``dns.rdatatype`` at module level; stub them so the import
    succeeds. The tests below monkeypatch the functions that actually use dnspython
    (a_records_with_ttl etc.), so the stubs only need to exist, not work.
    """
    if "dns" in sys.modules:
        return
    dns = types.ModuleType("dns")
    message = types.ModuleType("dns.message")
    message.from_wire = lambda *a, **k: None
    message.make_response = lambda *a, **k: None
    message.make_query = lambda *a, **k: None
    rcode = types.ModuleType("dns.rcode")
    rcode.NXDOMAIN = 3
    rdatatype = types.ModuleType("dns.rdatatype")
    rdatatype.A = 1
    dns.message = message
    dns.rcode = rcode
    dns.rdatatype = rdatatype
    sys.modules.update(
        {
            "dns": dns,
            "dns.message": message,
            "dns.rcode": rcode,
            "dns.rdatatype": rdatatype,
        }
    )


@pytest.fixture(scope="module")
def proxy():
    """Load proxy.py as an isolated module (with dnspython stubbed)."""
    _install_dns_stubs()
    spec = importlib.util.spec_from_file_location(
        "klangk_test_proxy", _PROXY_PATH
    )
    assert spec and spec.loader, f"could not load {_PROXY_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def learned(proxy):
    """A proxy module with the learned-IP table cleared (TTL/sweep tests)."""
    proxy._LEARNED.clear()
    return proxy


class TestParseSpecs:
    """Env parsing → ``(host, port|None, is_wildcard)`` triples. CIDRs are
    skipped (the entrypoint applies those statically); the grammar mirrors
    ``klangk.netfilter.parse_allowed_domains`` (#2256)."""

    def test_ports_wildcards_cidr_skip(self, proxy, monkeypatch):
        monkeypatch.setenv(
            "KLANGKNETWORK_EGRESS_ALLOW",
            "github.com:443,*.pypi.org,pypi.org,"
            "10.0.0.0/8,10.0.0.0/8:53,bare.com:8443",
        )
        assert proxy.parse_specs() == [
            ("github.com", 443, False),
            ("pypi.org", None, True),  # *.pypi.org
            ("pypi.org", None, False),
            ("bare.com", 8443, False),
        ]

    def test_wildcard_with_port(self, proxy, monkeypatch):
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_ALLOW", "*.pypi.org:443")
        assert proxy.parse_specs() == [("pypi.org", 443, True)]

    def test_empty_env(self, proxy, monkeypatch):
        monkeypatch.delenv("KLANGKNETWORK_EGRESS_ALLOW", raising=False)
        assert proxy.parse_specs() == []


class TestPortsFor:
    """``ports_for`` is the allow gate. Returns ``None`` (all ports), a port
    ``set`` (scoped allow), or ``set()`` (deny). A suffix-match regression here
    (e.g. a bare ``endswith(h)``) would wrongly admit ``evilgithub.com`` for an
    allow-listed ``github.com``. Pin the exact / subdomain / wildcard / port
    semantics so a refactor can't silently weaken it (#2256)."""

    @pytest.mark.parametrize(
        "qname, expected",
        [
            ("github.com", None),  # exact -> all ports
            ("api.github.com", None),  # subdomain -> all ports
            ("evilgithub.com", set()),  # boundary: NOT a subdomain
            ("github.com.attacker.test", set()),  # prefix-of, not a suffix
        ],
    )
    def test_suffix_boundary(self, proxy, monkeypatch, qname, expected):
        # The dot boundary ("." + h) stops evilgithub.com matching github.com.
        monkeypatch.setattr(proxy, "SPECS", [("github.com", None, False)])
        assert proxy.ports_for(qname) == expected

    def test_port_scope_applies_to_apex_and_subdomain(
        self, proxy, monkeypatch
    ):
        monkeypatch.setattr(proxy, "SPECS", [("github.com", 443, False)])
        assert proxy.ports_for("github.com") == {443}
        assert proxy.ports_for("api.github.com") == {443}

    def test_wildcard_excludes_apex(self, proxy, monkeypatch):
        # *.pypi.org matches subdomains only, NOT pypi.org itself — distinct
        # from a bare pypi.org (apex + subdomains).
        monkeypatch.setattr(proxy, "SPECS", [("pypi.org", None, True)])
        assert proxy.ports_for("pypi.org") == set()
        assert proxy.ports_for("a.pypi.org") is None
        assert proxy.ports_for("a.b.pypi.org") is None

    def test_wildcard_with_port(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "SPECS", [("pypi.org", 443, True)])
        assert proxy.ports_for("pypi.org") == set()
        assert proxy.ports_for("x.pypi.org") == {443}

    def test_all_ports_spec_dominates(self, proxy, monkeypatch):
        # github.com (all ports) + github.com:443 -> all ports win.
        monkeypatch.setattr(
            proxy,
            "SPECS",
            [
                ("github.com", None, False),
                ("github.com", 443, False),
            ],
        )
        assert proxy.ports_for("github.com") is None

    def test_multiple_ports_union(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy,
            "SPECS",
            [("github.com", 443, False), ("github.com", 8443, False)],
        )
        assert proxy.ports_for("github.com") == {443, 8443}

    def test_empty_specs_denies_all(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "SPECS", [])
        assert proxy.ports_for("anything.test") == set()

    def test_case_sensitive_relies_on_caller_lowercasing(
        self, proxy, monkeypatch
    ):
        # ports_for compares verbatim; parse_specs/query_name lowercase at the
        # edges. Pin that contract: a mixed-case qname does NOT match a
        # lowercased spec, so the lowercasing must not be dropped upstream.
        monkeypatch.setattr(proxy, "SPECS", [("github.com", None, False)])
        assert proxy.ports_for("GitHub.Com") == set()


class TestRuleArgs:
    """The iptables rule shape: port-scoped vs all-ports (#2256)."""

    def test_all_ports(self, proxy):
        assert proxy._rule_args("1.2.3.4", None) == [
            "-d",
            "1.2.3.4",
            "-j",
            "ACCEPT",
        ]

    def test_scoped_port(self, proxy):
        assert proxy._rule_args("1.2.3.4", 443) == [
            "-d",
            "1.2.3.4",
            "-p",
            "tcp",
            "--dport",
            "443",
            "-j",
            "ACCEPT",
        ]


class TestRuleArgsInvariant:
    """The dedup/delete cycle is sound only if -C (exists), -I (install), and
    -D (remove) build IDENTICAL args from one ``_rule_args``. A future edit
    that drifts would silently break dedup or orphan rules (#2256 review)."""

    @pytest.mark.parametrize("port", [None, 443])
    def test_check_install_remove_share_args(self, proxy, monkeypatch, port):
        calls = []

        def fake_run(args, **kw):
            calls.append(list(args))
            return types.SimpleNamespace(returncode=1)  # absent -> -I fires

        monkeypatch.setattr(proxy.subprocess, "run", fake_run)
        proxy._install("1.2.3.4", port)
        proxy._rule_exists("1.2.3.4", port)
        proxy._remove("1.2.3.4", port)
        expected = proxy._rule_args("1.2.3.4", port)
        ops = set()
        for cmd in calls:
            assert cmd[0] == proxy.IPT, cmd
            assert cmd[1] in ("-C", "-I", "-D"), cmd
            # The rule-matching args (-d/-p/--dport/-j ACCEPT) are the SHARED
            # tail across all three: -C and -D are "<op> OUTPUT <args>"; -I
            # is "-I OUTPUT 1 <args>" (the position arg sits before the tail).
            assert cmd[-len(expected) :] == expected, (cmd, expected)
            ops.add(cmd[1])
        assert {"-C", "-I", "-D"} <= ops


class TestDecision:
    """The None-vs-empty gate consumed by ``main()``. ``None`` (a port-less
    spec matched) is an ALLOW on all ports; an empty set is a DENY.
    Inverting either is a fail-open/fail-closed bug in the security gate
    (#2256 review)."""

    def test_empty_qname_denies(self, proxy):
        assert proxy._decision("", {443}) == (True, set())

    def test_none_ports_allows_all_ports(self, proxy):
        assert proxy._decision("a.com", None) == (False, {None})

    def test_empty_set_denies(self, proxy):
        assert proxy._decision("a.com", set()) == (True, set())

    def test_port_set_scoped_allow(self, proxy):
        assert proxy._decision("a.com", {443, 8443}) == (
            False,
            {443, 8443},
        )


class TestTTLAndSweep:
    """Learned-IP TTL tracking + expiry sweep (#2256)."""

    def test_allow_installs_and_records_expiry(self, learned, monkeypatch):
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=1)  # rule absent -> -I

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        monkeypatch.setattr(learned.time, "time", lambda: 1000.0)
        learned.allow("1.2.3.4", 443, 120)
        assert any("-I" in a for a in runs)
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["expire"] == 1000.0 + 120
        assert rec["ports"] == {443}

    def test_min_ttl_floors_zero_ttl(self, learned, monkeypatch):
        # A 0-TTL response must not yank the rule immediately.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned.allow("1.2.3.4", 443, 0)
        assert learned._LEARNED["1.2.3.4"]["expire"] == 0.0 + learned.MIN_TTL

    def test_dedup_skips_install_when_rule_exists(self, learned, monkeypatch):
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=0)  # rule exists

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned.allow("1.2.3.4", 443, 60)
        assert not any("-I" in a for a in runs)
        assert learned._LEARNED["1.2.3.4"]["ports"] == {443}

    def test_refresh_extends_expire_only_forward(self, learned, monkeypatch):
        # A later re-resolution with a longer TTL must not shorten an existing
        # rule's lifetime; expire only moves forward.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        clock = [1000.0, 2000.0]
        monkeypatch.setattr(learned.time, "time", lambda: clock.pop(0))
        learned.allow("1.2.3.4", 443, 60)  # expire 1060
        learned.allow("1.2.3.4", 443, 500)  # candidate 2500 -> max(1060, 2500)
        assert learned._LEARNED["1.2.3.4"]["expire"] == 2500.0

    def test_all_ports_rule_uses_none_port(self, learned, monkeypatch):
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned.allow("1.2.3.4", None, 60)
        assert learned._LEARNED["1.2.3.4"]["ports"] == {None}

    def test_cross_domain_shared_ip_accumulates_ports(
        self, learned, monkeypatch
    ):
        # Two different domains resolving to one CDN IP union their ports
        # under that single IP (#2256 review).
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned.allow("9.9.9.9", 443, 60)  # github.com:443 -> 9.9.9.9
        learned.allow("9.9.9.9", 8443, 60)  # registry.com:8443 -> 9.9.9.9
        assert learned._LEARNED["9.9.9.9"]["ports"] == {443, 8443}

    def test_sweep_removes_expired_rules(self, learned, monkeypatch):
        removed = []
        monkeypatch.setattr(
            learned, "_remove", lambda ip, port: removed.append((ip, port))
        )
        learned._LEARNED["1.2.3.4"] = {"expire": 500.0, "ports": {443, None}}
        gone = learned.sweep_once(now=1000.0)
        assert gone == [("1.2.3.4", {443, None})]
        assert ("1.2.3.4", 443) in removed
        assert ("1.2.3.4", None) in removed
        assert "1.2.3.4" not in learned._LEARNED

    def test_sweep_keeps_live_ips(self, learned, monkeypatch):
        monkeypatch.setattr(learned, "_remove", lambda *a: None)
        learned._LEARNED["1.2.3.4"] = {"expire": 2000.0, "ports": {443}}
        assert learned.sweep_once(now=1000.0) == []
        assert "1.2.3.4" in learned._LEARNED

    def test_sweep_boundary_keeps_equal_expire(self, learned, monkeypatch):
        # expire <= now is removed; a rule valid through `now` (expire > now)
        # is kept. expire == now counts as expired.
        monkeypatch.setattr(learned, "_remove", lambda *a: None)
        learned._LEARNED["dead"] = {"expire": 1000.0, "ports": {443}}
        learned._LEARNED["live"] = {"expire": 1000.001, "ports": {443}}
        gone = learned.sweep_once(now=1000.0)
        assert [ip for ip, _ in gone] == ["dead"]
        assert "live" in learned._LEARNED


class TestARecordsWithTtl:
    """``a_records_with_ttl`` extracts every A record from a DNS response's
    answer section — transparently across a CNAME chain — each paired with
    its rrset TTL. That transparency is the bounded widening risk in
    egress-filtering.md (#2279): an allow-listed domain that CNAMEs to an
    attacker-controlled (or shared-CDN) host has that host's A record learned,
    scoped to the declared port + TTL by #2256. Pin the extraction so a
    refactor can't silently change which records are learned."""

    class _RRset:
        """A fake DNS rrset: an rdtype, a ttl, and an iterable of rdata."""

        def __init__(self, rdtype, ttl, addresses):
            # 1 == dns.rdatatype.A (the stub pins A=1); 5 is CNAME.
            self.rdtype = rdtype
            self.ttl = ttl
            self._rdata = [types.SimpleNamespace(address=a) for a in addresses]

        def __iter__(self):
            return iter(self._rdata)

    @staticmethod
    def _msg(answer):
        return types.SimpleNamespace(answer=answer)

    def _wire(self, proxy, monkeypatch, answer):
        """Stub ``dns.message.from_wire`` to return a response with ``answer``;
        return throwaway wire bytes (the stub ignores them)."""
        monkeypatch.setattr(
            proxy.dns.message, "from_wire", lambda wire: self._msg(answer)
        )
        return b""

    def test_cname_chain_yields_target_a_records(self, proxy, monkeypatch):
        # cdn.example.com CNAME cdn-backend.fastly.net, then A 1.2.3.4.
        wire = self._wire(
            proxy,
            monkeypatch,
            [
                self._RRset(5, 60, []),  # CNAME — skipped
                self._RRset(1, 300, ["1.2.3.4"]),  # A — learned
            ],
        )
        # The CNAME target's A record is extracted (CNAME transparency, #2279).
        assert proxy.a_records_with_ttl(wire) == [("1.2.3.4", 300)]

    def test_apex_and_cname_target_a_records_both_learned(
        self, proxy, monkeypatch
    ):
        # Both the queried name's own A and the CNAME target's A sit in the
        # answer; both are learned (the widening — two IPs for one query).
        wire = self._wire(
            proxy,
            monkeypatch,
            [
                self._RRset(5, 60, []),
                self._RRset(1, 300, ["10.0.0.1"]),
                self._RRset(1, 300, ["10.0.0.2"]),
            ],
        )
        assert proxy.a_records_with_ttl(wire) == [
            ("10.0.0.1", 300),
            ("10.0.0.2", 300),
        ]

    def test_multiple_a_in_one_rrset(self, proxy, monkeypatch):
        wire = self._wire(
            proxy,
            monkeypatch,
            [self._RRset(1, 60, ["1.1.1.1", "2.2.2.2"])],
        )
        assert proxy.a_records_with_ttl(wire) == [
            ("1.1.1.1", 60),
            ("2.2.2.2", 60),
        ]

    def test_ttl_is_per_rrset(self, proxy, monkeypatch):
        wire = self._wire(
            proxy,
            monkeypatch,
            [
                self._RRset(1, 300, ["1.1.1.1"]),
                self._RRset(1, 30, ["2.2.2.2"]),
            ],
        )
        assert proxy.a_records_with_ttl(wire) == [
            ("1.1.1.1", 300),
            ("2.2.2.2", 30),
        ]

    def test_cname_only_yields_no_a_records(self, proxy, monkeypatch):
        wire = self._wire(proxy, monkeypatch, [self._RRset(5, 60, [])])
        assert proxy.a_records_with_ttl(wire) == []


class TestRespondAllowedSwallowsFailures:
    """#2278: a transient failure in allow or sendto must drop only the one
    response, not kill the proxy (an escaped raise would take down PID 1,
    leaving learned ACCEPT rules in place with DNS dead — a partial
    fail-open)."""

    async def test_allow_failure_does_not_propagate(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy, "a_records_with_ttl", lambda wire: [("1.2.3.4", 100)]
        )

        def _boom(ip, port, ttl):
            raise RuntimeError("iptables transient failure")

        monkeypatch.setattr(proxy, "allow", _boom)
        s = MagicMock()
        # Must not raise (the executor propagates the iptables error, which
        # _respond_allowed swallows).
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "allowed.test", {443}
        )

    async def test_sendto_failure_does_not_propagate(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy, "a_records_with_ttl", lambda wire: [("1.2.3.4", 100)]
        )
        monkeypatch.setattr(proxy, "allow", lambda *a: None)
        s = MagicMock()
        s.sendto.side_effect = OSError("client gone")
        # Must not raise.
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "allowed.test", {443}
        )

    async def test_happy_path_allows_each_ip_each_port_and_sends(
        self, proxy, monkeypatch
    ):
        monkeypatch.setattr(
            proxy,
            "a_records_with_ttl",
            lambda wire: [("1.2.3.4", 100), ("5.6.7.8", 200)],
        )
        calls = []
        monkeypatch.setattr(
            proxy, "allow", lambda ip, port, ttl: calls.append((ip, port, ttl))
        )
        s = MagicMock()
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "q", {443, 8443}
        )
        assert ("1.2.3.4", 443, 100) in calls
        assert ("1.2.3.4", 8443, 100) in calls
        assert ("5.6.7.8", 443, 200) in calls
        assert ("5.6.7.8", 8443, 200) in calls
        s.sendto.assert_called_once_with(b"resp", ("127.0.0.1", 1234))


class TestConsentForward:
    """Sidecar consent (#2242 recording -> #2311 half B hold): packet parsing,
    the egress-sidecar WS client's fail-close contract, the DNS hold path, and
    the NFQUEUE consumer's graceful no-op when netfilterqueue is absent (the
    server venv doesn't ship it)."""

    def test_parse_dest_tcp(self, proxy):
        ip = bytearray(40)
        ip[0] = 0x45  # IPv4, IHL 5 (20 bytes)
        ip[9] = 6  # TCP
        ip[12:16] = bytes([1, 1, 1, 1])  # src
        ip[16:20] = bytes([8, 8, 8, 8])  # dst
        ip[22:24] = (443).to_bytes(2, "big")  # TCP dport at IHL(20)+2
        assert proxy.parse_dest(bytes(ip)) == ("8.8.8.8", 443)

    def test_parse_dest_udp(self, proxy):
        ip = bytearray(28)
        ip[0] = 0x45
        ip[9] = 17  # UDP
        ip[16:20] = bytes([1, 2, 3, 4])
        ip[22:24] = (53).to_bytes(2, "big")  # UDP dport at 20+2
        assert proxy.parse_dest(bytes(ip)) == ("1.2.3.4", 53)

    def test_parse_dest_ethernet_prefixed(self, proxy):
        eth = bytes(14)  # 14-byte L2 header before the IP packet
        ip = bytearray(20)
        ip[0] = 0x45
        ip[16:20] = bytes([9, 9, 9, 9])
        assert proxy.parse_dest(bytes(eth) + bytes(ip)) == ("9.9.9.9", 0)

    def test_parse_dest_malformed(self, proxy):
        assert proxy.parse_dest(b"") == ("", 0)
        assert proxy.parse_dest(bytes(10)) == ("", 0)
        assert proxy.parse_dest(b"\xff" * 20) == ("", 0)  # not IPv4

    # --- _ws_url: derive the WS URL from the consent HTTP URL (#2311) ---

    def test_ws_url_http_to_ws(self, proxy):
        assert (
            proxy._ws_url(
                "http://host.containers.internal:9/internal/egress-consent/events"
            )
            == "ws://host.containers.internal:9/ws/egress-sidecar"
        )

    def test_ws_url_https_to_wss(self, proxy):
        assert (
            proxy._ws_url("https://klangkd.example/ev")
            == "wss://klangkd.example/ws/egress-sidecar"
        )

    def test_ws_url_passthrough_ws(self, proxy):
        # an already-ws(s):// URL is used verbatim
        assert (
            proxy._ws_url("ws://x/ws/egress-sidecar")
            == "ws://x/ws/egress-sidecar"
        )

    # --- _HoldLimiter: bounded in-flight DNS holds ---

    def test_hold_limiter_bounds_and_releases(self, proxy):
        lim = proxy._HoldLimiter(2)
        assert lim.try_acquire() is True
        assert lim.try_acquire() is True
        assert lim.try_acquire() is False  # exhausted
        lim.release()
        assert lim.try_acquire() is True  # freed

    def test_hold_limiter_release_floor(self, proxy):
        lim = proxy._HoldLimiter(1)
        lim.release()  # must not go negative
        assert lim.try_acquire() is True

    # --- SidecarConsentClient.request: fail-close contract ---

    async def test_request_fail_close_when_disconnected(self, proxy, tmp_path):
        # WS down -> "deny" at once, no frame sent (today's static behavior).
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        assert c.connected is False
        assert await c.request("evil.test", None) == "deny"

    async def test_request_sends_frame_and_resolves_on_verdict(
        self, proxy, tmp_path
    ):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        task = asyncio.create_task(c.request("evil.test", 443))
        await asyncio.sleep(0)  # let it send + await the verdict
        frame = json.loads(sent[0])
        assert frame["type"] == "egress"
        assert frame["dst"] == "evil.test"
        assert frame["dport"] == 443
        c._dispatch(
            json.dumps(
                {"type": "verdict", "id": frame["id"], "decision": "allow"}
            )
        )
        assert await task == "allow"
        assert c._pending == {}  # cleaned up

    async def test_request_deny_verdict(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        task = asyncio.create_task(c.request("evil.test", None))
        await asyncio.sleep(0)
        frame = json.loads(sent[0])
        c._dispatch(
            json.dumps(
                {"type": "verdict", "id": frame["id"], "decision": "deny"}
            )
        )
        assert await task == "deny"

    async def test_request_non_allow_verdict_is_deny(self, proxy, tmp_path):
        # expired/malformed decision -> deny (fail-close)
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        task = asyncio.create_task(c.request("evil.test", None))
        await asyncio.sleep(0)
        frame = json.loads(sent[0])
        c._dispatch(
            json.dumps(
                {"type": "verdict", "id": frame["id"], "decision": "expired"}
            )
        )
        assert await task == "deny"

    async def test_request_timeout_fail_close(self, proxy, tmp_path):
        # no verdict within HOLD_TIMEOUT -> "deny"; slot cleaned up
        c = proxy.SidecarConsentClient(
            "http://h/ev", str(tmp_path / "t"), 0.05
        )
        c._connected.set()

        class _FakeWS:
            async def send(self, frame):
                pass

        c._ws = _FakeWS()
        assert await c.request("evil.test", None) == "deny"
        assert c._pending == {}

    async def test_request_send_error_fail_close(self, proxy, tmp_path):
        # ws.send raises -> "deny", slot cleaned up
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()

        class _FakeWS:
            async def send(self, frame):
                raise OSError("connection gone")

        c._ws = _FakeWS()
        assert await c.request("evil.test", None) == "deny"
        assert c._pending == {}

    def test_dispatch_ignores_non_verdict_and_bad_id(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._dispatch("not-json")
        c._dispatch(json.dumps({"type": "egress"}))  # wrong type
        c._dispatch(
            json.dumps({"type": "verdict", "decision": "allow"})
        )  # no id
        c._dispatch(
            json.dumps({"type": "verdict", "id": 123, "decision": "allow"})
        )  # non-str id
        # a verdict for an unknown id is a no-op (already popped/timed out)
        c._dispatch(
            json.dumps({"type": "verdict", "id": "nope", "decision": "allow"})
        )

    async def test_dispatch_resolves_pending_future(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        fut = asyncio.get_running_loop().create_future()
        c._pending["abc"] = fut
        c._dispatch(
            json.dumps({"type": "verdict", "id": "abc", "decision": "allow"})
        )
        assert fut.result() == "allow"
        assert "abc" not in c._pending

    async def test_fail_close_pending_resolves_deny(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        fut = asyncio.get_running_loop().create_future()
        c._pending["x"] = fut
        c._fail_close_pending()
        assert fut.result() == "deny"
        assert c._pending == {}

    def test_read_token_missing_file(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient(
            "http://h/ev", str(tmp_path / "no-such"), 5
        )
        assert c._read_token() == ""

    def test_read_token_reads_file(self, proxy, tmp_path):
        f = tmp_path / "tok"
        f.write_text("abc-xyz\n")
        c = proxy.SidecarConsentClient("http://h/ev", str(f), 5)
        assert c._read_token() == "abc-xyz"

    # --- _gate_deny: should this denied query be held or fail-close NXDOMAIN? ---

    def test_gate_deny_false_when_no_client(self, proxy):
        # consent disabled -> never hold, always NXDOMAIN (today's behavior)
        assert proxy._gate_deny(None, proxy._HoldLimiter(8)) is False

    def test_gate_deny_false_when_disconnected(self, proxy):
        client = MagicMock()
        client.connected = False
        assert proxy._gate_deny(client, proxy._HoldLimiter(8)) is False

    def test_gate_deny_false_when_flood_bound(self, proxy):
        client = MagicMock()
        client.connected = True
        lim = proxy._HoldLimiter(1)
        assert lim.try_acquire()  # fill the only slot
        assert (
            proxy._gate_deny(client, lim) is False
        )  # exhausted -> fail-close

    def test_gate_deny_true_acquires_slot(self, proxy):
        client = MagicMock()
        client.connected = True
        lim = proxy._HoldLimiter(2)
        assert proxy._gate_deny(client, lim) is True
        assert lim.try_acquire() is True  # one slot now in use -> one left
        assert lim.try_acquire() is False  # both taken

    # --- _handle_hold: the DNS hold task (gate already passed) ---

    async def test_handle_hold_allow_forwards_upstream(
        self, proxy, monkeypatch
    ):
        monkeypatch.setattr(proxy, "nxdomain_for", lambda d: b"NXD")
        fwd = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_learn", fwd)
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value="allow")
        s = MagicMock()
        lim = proxy._HoldLimiter(8)
        await proxy._handle_hold(
            s, b"q", ("1.2.3.4", 53), "evil.test", client, lim
        )
        s.sendto.assert_not_called()  # not denied
        client.request.assert_awaited_once_with("evil.test", None)
        fwd.assert_awaited_once()  # forwarded upstream (all-ports)
        assert lim._in_flight == 0  # slot released

    async def test_handle_hold_deny_sends_nxdomain(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "nxdomain_for", lambda d: b"NXD")
        fwd = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_learn", fwd)
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value="deny")
        s = MagicMock()
        lim = proxy._HoldLimiter(8)
        await proxy._handle_hold(
            s, b"q", ("1.2.3.4", 53), "evil.test", client, lim
        )
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))
        fwd.assert_not_awaited()
        assert lim._in_flight == 0  # slot released

    async def test_handle_hold_request_error_fail_closes(
        self, proxy, monkeypatch
    ):
        # request() raising -> deny + NXDOMAIN + slot still released
        monkeypatch.setattr(proxy, "nxdomain_for", lambda d: b"NXD")
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=RuntimeError("boom"))
        s = MagicMock()
        lim = proxy._HoldLimiter(8)
        await proxy._handle_hold(
            s, b"q", ("1.2.3.4", 53), "evil.test", client, lim
        )
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))
        assert lim._in_flight == 0  # released despite the error

    # --- _run_nfq_consumer: graceful no-op without netfilterqueue ---

    def test_run_nfq_consumer_noop_without_netfilterqueue(self, proxy, capsys):
        # The server venv has no netfilterqueue -> lazy import fails -> logs +
        # returns instead of raising.
        proxy._run_nfq_consumer(None, asyncio.new_event_loop())
        assert "netfilterqueue unavailable" in capsys.readouterr().out


# --- helpers for the NFQUEUE callback + _handle_packet tests (#2311 half B) ---


def _ip_payload(dst: str, dport: int, proto: int = 6) -> bytes:
    """A minimal IPv4 packet (20-byte header + 4 bytes L4) for parse_dest."""
    b = bytearray(24)
    b[0] = 0x45  # version 4, IHL 5 (20-byte header)
    b[9] = proto  # L4 protocol (6 = TCP)
    b[16:20] = bytes(int(x) for x in dst.split("."))  # dst IP
    b[20:22] = (12345).to_bytes(2, "big")  # source port
    b[22:24] = dport.to_bytes(2, "big")  # destination port
    return bytes(b)


class _FakeNFQ:
    """Stand-in for netfilterqueue.NetfilterQueue: captures the bound callback
    and returns at once from run() so the test can drive _cb itself."""

    def __init__(self) -> None:
        self.cb = None
        self.queue = None

    def bind(self, queue: int, cb) -> None:
        self.queue = queue
        self.cb = cb

    def run(self) -> None:
        pass  # do not block -- the test invokes self.cb directly


class _FakePkt:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.verdict = None

    def get_payload(self) -> bytes:
        return self._payload

    def accept(self) -> None:
        self.verdict = "accept"

    def drop(self) -> None:
        self.verdict = "drop"


def _install_fake_nfq(monkeypatch, nfq: _FakeNFQ) -> None:
    """Make ``from netfilterqueue import NetfilterQueue`` return ``nfq``."""
    mod = types.ModuleType("netfilterqueue")
    mod.NetfilterQueue = lambda: nfq  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "netfilterqueue", mod)


class TestNfqueueCallback:
    """The NFQUEUE verdict->accept/drop path (#2311 half B review #1): the
    ``_cb`` body is otherwise untested (only the import-failure early-return
    was). Drive it with a stubbed netfilterqueue + a fake packet."""

    async def test_allow_verdict_accepts(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        nfq = _FakeNFQ()
        _install_fake_nfq(monkeypatch, nfq)
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value="allow")
        proxy._run_nfq_consumer(client, loop)  # binds cb; run() returns
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        # _cb blocks on the verdict (run_coroutine_threadsafe); run it in a
        # worker thread so the loop is free to resolve client.request.
        await asyncio.to_thread(nfq.cb, pkt)
        assert pkt.verdict == "accept"
        client.request.assert_awaited_once_with("1.2.3.4", 443)

    async def test_deny_verdict_drops(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        nfq = _FakeNFQ()
        _install_fake_nfq(monkeypatch, nfq)
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value="deny")
        proxy._run_nfq_consumer(client, loop)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await asyncio.to_thread(nfq.cb, pkt)
        assert pkt.verdict == "drop"

    async def test_request_error_drops(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        nfq = _FakeNFQ()
        _install_fake_nfq(monkeypatch, nfq)
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=RuntimeError("boom"))
        proxy._run_nfq_consumer(client, loop)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await asyncio.to_thread(nfq.cb, pkt)
        assert pkt.verdict == "drop"  # except -> deny -> drop

    async def test_verdict_timeout_drops(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(proxy, "HOLD_TIMEOUT", 0.05)
        nfq = _FakeNFQ()
        _install_fake_nfq(monkeypatch, nfq)
        client = MagicMock()
        client.connected = True
        # request outlasts HOLD_TIMEOUT -> fut.result() times out -> drop
        client.request = AsyncMock(side_effect=lambda *a: asyncio.sleep(0.5))
        proxy._run_nfq_consumer(client, loop)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await asyncio.to_thread(nfq.cb, pkt)
        assert pkt.verdict == "drop"
        await asyncio.sleep(0.6)  # let the abandoned sleep(0.5) finish cleanly

    async def test_ws_down_drops_without_request(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        nfq = _FakeNFQ()
        _install_fake_nfq(monkeypatch, nfq)
        client = MagicMock()
        client.connected = False
        client.request = AsyncMock()
        proxy._run_nfq_consumer(client, loop)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await asyncio.to_thread(nfq.cb, pkt)
        assert pkt.verdict == "drop"
        client.request.assert_not_awaited()

    async def test_unparseable_dest_drops(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        nfq = _FakeNFQ()
        _install_fake_nfq(monkeypatch, nfq)
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value="allow")
        proxy._run_nfq_consumer(client, loop)
        pkt = _FakePkt(b"\x00" * 24)  # version nibble 0 -> parse_dest ("", 0)
        await asyncio.to_thread(nfq.cb, pkt)
        assert pkt.verdict == "drop"
        client.request.assert_not_awaited()


class TestHandlePacket:
    """The per-packet routing extracted from _async_main (#2311 half B review #2):
    classify -> gate -> hold / NXDOMAIN / forward, incl. the property that a
    statically-allow-listed name is never held."""

    async def test_allowed_name_forwards_not_held(self, proxy, monkeypatch):
        proxy._BG_TASKS.clear()
        monkeypatch.setattr(proxy, "query_name", lambda wire: "allowed.test")
        monkeypatch.setattr(proxy, "SPECS", [("allowed.test", None, False)])
        fwd = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(
            s, b"q", ("1.2.3.4", 53), None, proxy._HoldLimiter(8)
        )
        fwd.assert_awaited_once()  # forwarded, not held/denied
        s.sendto.assert_not_called()  # no NXDOMAIN

    async def test_denied_no_client_sends_nxdomain(self, proxy, monkeypatch):
        proxy._BG_TASKS.clear()
        monkeypatch.setattr(proxy, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy, "SPECS", [])
        monkeypatch.setattr(proxy, "nxdomain_for", lambda d: b"NXD")
        s = MagicMock()
        await proxy._handle_packet(
            s, b"q", ("1.2.3.4", 53), None, proxy._HoldLimiter(8)
        )
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))

    async def test_denied_connected_spawns_hold(self, proxy, monkeypatch):
        proxy._BG_TASKS.clear()
        monkeypatch.setattr(proxy, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy, "SPECS", [])
        hold = AsyncMock()
        monkeypatch.setattr(proxy, "_handle_hold", hold)
        client = MagicMock()
        client.connected = True
        lim = proxy._HoldLimiter(8)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), client, lim)
        tasks = list(proxy._BG_TASKS)
        await asyncio.gather(*tasks)  # run the spawned hold task(s)
        hold.assert_awaited_once_with(
            s, b"q", ("1.2.3.4", 53), "evil.test", client, lim
        )
        assert proxy._BG_TASKS == set()  # done-callback discarded the entry

    async def test_denied_flood_bound_sends_nxdomain(self, proxy, monkeypatch):
        proxy._BG_TASKS.clear()
        monkeypatch.setattr(proxy, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy, "SPECS", [])
        monkeypatch.setattr(proxy, "nxdomain_for", lambda d: b"NXD")
        hold = AsyncMock()
        monkeypatch.setattr(proxy, "_handle_hold", hold)
        client = MagicMock()
        client.connected = True
        lim = proxy._HoldLimiter(1)
        assert lim.try_acquire()  # fill the only slot
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), client, lim)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))  # fail-close
        hold.assert_not_awaited()

    async def test_malformed_query_is_dropped(self, proxy, monkeypatch):
        proxy._BG_TASKS.clear()

        def _boom(wire):
            raise RuntimeError("bad wire")

        monkeypatch.setattr(proxy, "query_name", _boom)
        fwd = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(
            s, b"q", ("1.2.3.4", 53), None, proxy._HoldLimiter(8)
        )
        s.sendto.assert_not_called()  # dropped, no response
        fwd.assert_not_awaited()
