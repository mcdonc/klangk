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

    def test_forever_hosts_consulted_alongside_specs(self, proxy, monkeypatch):
        # A `forever` consent verdict adds the host to _FOREVER_HOSTS (#2372);
        # ports_for must treat it as allow-listed (apex + subdomain, port-scoped)
        # just like a static spec, so a later CDN-rotated IP re-resolves and is
        # allowed without re-prompting.
        monkeypatch.setattr(proxy, "SPECS", [])
        monkeypatch.setattr(
            proxy, "_FOREVER_HOSTS", [("example.com", 443, False)]
        )
        assert proxy.ports_for("example.com") == {443}
        assert proxy.ports_for("api.example.com") == {443}
        assert proxy.ports_for("other.com") == set()

    def test_add_forever_host_dedups(self, proxy):
        proxy._FOREVER_HOSTS.clear()
        proxy._add_forever_host("example.com", 443)
        proxy._add_forever_host("example.com", 443)  # dup -> one entry
        proxy._add_forever_host("example.com", 8443)  # different port -> added
        assert proxy._FOREVER_HOSTS == [
            ("example.com", 443, False),
            ("example.com", 8443, False),
        ]


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

    def test_reject_installs_reject_rule_and_records(
        self, learned, monkeypatch
    ):
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=1)  # rule absent -> -I

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        monkeypatch.setattr(learned.time, "time", lambda: 1000.0)
        learned._REJECTED.clear()
        learned.reject("1.2.3.4", 80, 10)
        assert any(
            "-I" in a and "REJECT" in a and "tcp-reset" in a for a in runs
        )
        assert learned._REJECTED[("1.2.3.4", 80)] == 1010.0

    def test_sweep_removes_expired_reject_rules(self, learned, monkeypatch):
        removed = []

        def fake_run(args, **kw):
            if "-D" in args:
                removed.append(args)
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        learned._LEARNED.clear()
        learned._REJECTED.clear()
        learned._REJECTED[("1.2.3.4", 80)] = 100.0  # expired
        learned._REJECTED[("5.6.7.8", 443)] = 9999.0  # live
        learned.sweep_once(now=500.0)
        assert any(
            "1.2.3.4" in a and "REJECT" in a and "tcp-reset" in a
            for a in removed
        )
        assert ("1.2.3.4", 80) not in learned._REJECTED
        assert ("5.6.7.8", 443) in learned._REJECTED  # live one kept

    def test_all_ports_allow_supersedes_prior_port_denies(
        self, learned, monkeypatch
    ):
        # An all-ports allow must clear per-port REJECTs for that IP, else the
        # ACCEPT at the top of OUTPUT silently shadows a lingering REJECT (the
        # decider allowed the host -> a prior port-specific deny no longer applies).
        removed = []
        monkeypatch.setattr(
            learned,
            "_remove_reject",
            lambda ip, port: removed.append((ip, port)),
        )
        monkeypatch.setattr(learned, "_install", lambda *a: None)
        learned._REJECTED.clear()
        learned._REJECTED[("1.2.3.4", 443)] = 9999.0
        learned._REJECTED[("1.2.3.4", 80)] = 9999.0
        learned._REJECTED[("5.6.7.8", 443)] = 9999.0  # different IP, untouched
        learned.allow("1.2.3.4", None, 60)  # all-ports (consent path)
        assert ("1.2.3.4", 443) in removed
        assert ("1.2.3.4", 80) in removed
        assert ("5.6.7.8", 443) not in removed  # different IP kept
        assert ("1.2.3.4", 443) not in learned._REJECTED
        assert ("5.6.7.8", 443) in learned._REJECTED


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

    # --- parse_syn_tuple + build_rst_packet: the forged eager-deny RST (#2345) ---

    def test_parse_syn_tuple_tcp(self, proxy):
        assert proxy.parse_syn_tuple(
            _syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111)
        ) == ("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111)

    def test_parse_syn_tuple_ethernet_prefixed(self, proxy):
        eth = bytes(14)  # 14-byte L2 header before the IP packet
        assert proxy.parse_syn_tuple(
            eth + _syn_payload("1.1.1.1", 7, "2.2.2.2", 80, 5)
        ) == ("1.1.1.1", 7, "2.2.2.2", 80, 5)

    def test_parse_syn_tuple_rejects_non_tcp(self, proxy):
        # UDP -> all-zero tuple (a RST is TCP-only).
        udp = _syn_payload("1.1.1.1", 7, "2.2.2.2", 80, 5)
        udp = bytearray(udp)
        udp[9] = 17  # UDP
        assert proxy.parse_syn_tuple(bytes(udp)) == ("", 0, "", 0, 0)

    def test_parse_syn_tuple_malformed(self, proxy):
        assert proxy.parse_syn_tuple(b"") == ("", 0, "", 0, 0)
        assert proxy.parse_syn_tuple(bytes(10)) == ("", 0, "", 0, 0)
        assert proxy.parse_syn_tuple(b"\xff" * 20) == ("", 0, "", 0, 0)

    def test_build_rst_packet_layout_and_checksum(self, proxy):
        import socket as _sock
        import struct as _st

        pkt = proxy.build_rst_packet("1.2.3.4", 443, "10.0.0.5", 50000, 0x1111)
        assert len(pkt) == 40  # 20 IP + 20 TCP
        vi, tos, total, ident, frags, ttl, proto, ipck, src, dst = _st.unpack(
            "!BBHHHBBH4s4s", pkt[:20]
        )
        assert (vi & 0xF0) == 0x40 and proto == 6 and total == 40
        assert _sock.inet_ntoa(src) == "1.2.3.4"  # denied host is the source
        assert _sock.inet_ntoa(dst) == "10.0.0.5"  # workspace is the dest
        sport, dport, seq, ack, doff_flags, win, cksum, urg = _st.unpack(
            "!HHIIHHHH", pkt[20:]
        )
        assert sport == 443 and dport == 50000
        assert seq == 0  # RST seq is 0
        assert ack == 0x1112  # SYN seq + 1 (SYN consumes one sequence number)
        assert (doff_flags & 0x1FF) == 0x14  # RST + ACK
        assert doff_flags >> 12 == 5  # 20-byte TCP header (no options)
        # TCP checksum recomputes over the pseudo-header + TCP header (#2345:
        # the kernel validates it on INPUT, so it must be correct).
        tcp_zero = pkt[20:36] + b"\x00\x00" + pkt[38:40]
        pseudo = (
            _sock.inet_aton("1.2.3.4")
            + _sock.inet_aton("10.0.0.5")
            + _st.pack("!BBH", 0, 6, 20)
        )
        assert proxy._ones_checksum(pseudo + tcp_zero) == cksum

    def test_build_rst_packet_ack_wraps(self, proxy):
        import struct as _st

        # SYN seq 0xFFFFFFFF -> ack wraps to 0 (no overflow).
        pkt = proxy.build_rst_packet("1.2.3.4", 443, "10.0.0.5", 1, 0xFFFFFFFF)
        _ack = _st.unpack("!I", pkt[28:32])[0]
        assert _ack == 0

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

    # --- _record_hosts / _host_for: IP->host map for the SYN consent gate (#2324) ---

    def test_record_hosts_records_ip_to_host_without_accept(self, proxy):
        proxy._LEARNED.clear()
        proxy._record_hosts([("1.2.3.4", 60)], "evil.test")
        rec = proxy._LEARNED["1.2.3.4"]
        assert rec["host"] == "evil.test"
        assert rec["ports"] == set()  # NO ACCEPT rule installed

    def test_record_hosts_preserves_prior_learn_ports(self, learned):
        import time

        # a prior consent-allow learned the IP (ports={None}); a re-resolve
        # refreshes host + expire without dropping the ports.
        learned._LEARNED["1.2.3.4"] = {
            "expire": time.time() + 10,
            "ports": {None},
            "host": None,
        }
        learned._record_hosts([("1.2.3.4", 300)], "evil.test")
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["host"] == "evil.test"
        assert rec["ports"] == {None}  # preserved

    def test_host_for_returns_host_when_recorded(self, proxy):
        proxy._LEARNED.clear()
        proxy._record_hosts([("1.2.3.4", 60)], "evil.test")
        assert proxy._host_for("1.2.3.4") == "evil.test"

    def test_host_for_falls_back_to_ip_for_direct_connect(self, proxy):
        proxy._LEARNED.clear()
        assert proxy._host_for("5.6.7.8") == "5.6.7.8"  # no DNS record -> IP

    # --- SidecarConsentClient.request: fail-close contract ---

    async def test_request_fail_close_when_disconnected(self, proxy, tmp_path):
        # WS down -> "deny" at once, no frame sent (today's static behavior).
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        assert c.connected is False
        assert await c.request("evil.test", None) == ("deny", "once")

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
        await c._dispatch(
            json.dumps(
                {"type": "verdict", "id": frame["id"], "decision": "allow"}
            )
        )
        assert await task == ("allow", "once")
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
        await c._dispatch(
            json.dumps(
                {"type": "verdict", "id": frame["id"], "decision": "deny"}
            )
        )
        assert await task == ("deny", "once")

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
        await c._dispatch(
            json.dumps(
                {"type": "verdict", "id": frame["id"], "decision": "expired"}
            )
        )
        assert await task == ("deny", "once")

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
        assert await c.request("evil.test", None) == ("deny", "once")
        assert c._pending == {}

    async def test_request_send_error_fail_close(self, proxy, tmp_path):
        # ws.send raises -> "deny", slot cleaned up
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()

        class _FakeWS:
            async def send(self, frame):
                raise OSError("connection gone")

        c._ws = _FakeWS()
        assert await c.request("evil.test", None) == ("deny", "once")
        assert c._pending == {}

    async def test_dispatch_ignores_non_verdict_and_bad_id(
        self, proxy, tmp_path
    ):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        await c._dispatch("not-json")
        await c._dispatch(json.dumps({"type": "egress"}))  # wrong type
        await c._dispatch(
            json.dumps({"type": "verdict", "decision": "allow"})
        )  # no id
        await c._dispatch(
            json.dumps({"type": "verdict", "id": 123, "decision": "allow"})
        )  # non-str id
        # a verdict for an unknown id is a no-op (already popped/timed out)
        await c._dispatch(
            json.dumps({"type": "verdict", "id": "nope", "decision": "allow"})
        )

    async def test_dispatch_resolves_pending_future(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        fut = asyncio.get_running_loop().create_future()
        c._pending["abc"] = fut
        await c._dispatch(
            json.dumps({"type": "verdict", "id": "abc", "decision": "allow"})
        )
        assert fut.result() == ("allow", "once")
        assert "abc" not in c._pending

    async def test_fail_close_pending_resolves_deny_and_clears_cache(
        self, proxy, tmp_path
    ):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        fut = asyncio.get_running_loop().create_future()
        c._pending["x"] = fut
        # A lost connection is a fresh session: prior verdicts must not be trusted.
        proxy._VERDICT_CACHE.clear()
        proxy._VERDICT_CACHE[("1.2.3.4", 443)] = ("allow", 9999999.0)
        c._fail_close_pending()
        assert fut.result() == ("deny", "once")
        assert c._pending == {}
        assert (
            proxy._VERDICT_CACHE == {}
        )  # stale verdicts dropped on disconnect

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

    # --- _respond_recorded: resolve + record IP->host + respond (no ACCEPT) (#2324) ---

    async def test_respond_recorded_records_host_and_sends(
        self, proxy, monkeypatch
    ):
        proxy._LEARNED.clear()
        recorded = []
        monkeypatch.setattr(
            proxy,
            "_record_hosts",
            lambda recs, host: recorded.append((recs, host)),
        )
        monkeypatch.setattr(
            proxy, "a_records_with_ttl", lambda wire: [("1.2.3.4", 60)]
        )
        s = MagicMock()
        await proxy._respond_recorded(s, b"resp", ("1.2.3.4", 53), "evil.test")
        assert recorded == [([("1.2.3.4", 60)], "evil.test")]
        s.sendto.assert_called_once_with(b"resp", ("1.2.3.4", 53))

    async def test_respond_recorded_no_records_skips_record(
        self, proxy, monkeypatch
    ):
        proxy._LEARNED.clear()
        recorded = []
        monkeypatch.setattr(
            proxy,
            "_record_hosts",
            lambda recs, host: recorded.append((recs, host)),
        )
        monkeypatch.setattr(
            proxy, "a_records_with_ttl", lambda wire: []
        )  # upstream NXDOMAIN
        s = MagicMock()
        await proxy._respond_recorded(s, b"resp", ("1.2.3.4", 53), "evil.test")
        assert recorded == []  # nothing to record
        s.sendto.assert_called_once_with(
            b"resp", ("1.2.3.4", 53)
        )  # still forwarded

    async def test_respond_recorded_swallows_sendto_failure(
        self, proxy, monkeypatch
    ):
        proxy._LEARNED.clear()
        monkeypatch.setattr(proxy, "_record_hosts", lambda recs, host: None)
        monkeypatch.setattr(
            proxy, "a_records_with_ttl", lambda wire: [("1.2.3.4", 60)]
        )
        s = MagicMock()
        s.sendto.side_effect = OSError("gone")
        await proxy._respond_recorded(  # must not raise
            s, b"resp", ("1.2.3.4", 53), "evil.test"
        )

    # --- _run_nfq_consumer: graceful no-op without netfilterqueue ---

    def test_setup_nfq_consumer_noop_without_netfilterqueue(
        self, proxy, capsys
    ):
        # The server venv has no netfilterqueue -> lazy import fails -> logs +
        # returns instead of raising (before touching the event loop).
        proxy._setup_nfq_consumer(None)
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


def _syn_payload(
    src: str, sport: int, dst: str, dport: int, seq: int
) -> bytes:
    """A minimal IPv4 TCP SYN (20-byte IP + 20-byte TCP) with src IP + seq
    for parse_syn_tuple / RST-forging tests (#2345)."""
    b = bytearray(40)
    b[0] = 0x45
    b[9] = 6  # TCP
    b[12:16] = bytes(int(x) for x in src.split("."))  # src IP
    b[16:20] = bytes(int(x) for x in dst.split("."))  # dst IP
    b[20:22] = sport.to_bytes(2, "big")
    b[22:24] = dport.to_bytes(2, "big")
    b[24:28] = seq.to_bytes(4, "big")
    b[33] = 0x02  # SYN flag
    return bytes(b)


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

    def retain(self) -> None:
        pass  # the real binding needs retain() for a deferred verdict


class TestNfqueueCallback:
    """The SYN consent gate (#2324, #2329): the non-blocking NFQUEUE ``_cb``
    names the host, hands the verdict wait to a task, learns on allow, REJECTs
    on deny, and reuses the verdict for retransmits. ``_cb`` +
    ``_decide_and_verdict`` are driven directly (the loop-native get_fd /
    add_reader plumbing is exercised by the e2e, #2327)."""

    def _bind(self, proxy, monkeypatch, client):
        """Clear module state (the ``proxy`` fixture is module-scoped)."""
        proxy._VERDICT_CACHE.clear()
        proxy._INFLIGHT.clear()
        proxy._LEARNED.clear()
        proxy._FOREVER_HOSTS.clear()
        proxy._BG_TASKS.clear()

    async def _decide(self, proxy, pkt, client):
        """Run the non-blocking ``_cb`` to completion + await its verdict task(s)."""
        proxy._cb(pkt, client)
        if proxy._BG_TASKS:
            await asyncio.gather(*proxy._BG_TASKS)

    async def test_allow_verdict_accepts_and_learns_ip(
        self, proxy, monkeypatch
    ):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "restart"))
        learned = []
        monkeypatch.setattr(
            proxy,
            "allow",
            lambda ip, port, ttl: learned.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        client.request.assert_awaited_once_with(
            "1.2.3.4", 443
        )  # host=IP (no record)
        assert learned == [
            ("1.2.3.4", None, proxy._DURATION_FOREVER)
        ]  # learned all-ports

    async def test_allow_names_host_from_ip_map(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "restart"))
        monkeypatch.setattr(proxy, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts(
            [("1.2.3.4", 60)], "evil.test"
        )  # DNS resolved this IP
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        client.request.assert_awaited_once_with(
            "evil.test", 443
        )  # host, not IP

    async def test_forever_allow_adds_host_to_session_allowlist(
        self, proxy, monkeypatch
    ):
        # A `forever` allow approves the host (not just the resolved IP): the
        # sidecar adds it to _FOREVER_HOSTS so ports_for treats it as
        # allow-listed for the session -- a later CDN-rotated IP re-resolves
        # and is allowed without re-prompting (#2372).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "forever"))
        monkeypatch.setattr(proxy, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        assert ("example.com", 443, False) in proxy._FOREVER_HOSTS
        # ports_for now treats the host as allow-listed (apex + subdomain).
        monkeypatch.setattr(proxy, "SPECS", [])
        assert proxy.ports_for("example.com") == {443}
        assert proxy.ports_for("api.example.com") == {443}

    async def test_restart_allow_does_not_add_session_allowlist(
        self, proxy, monkeypatch
    ):
        # Only `forever` mutates the in-session allow-list; a restart/timed
        # allow is a plain in-memory IP learn (no domain-level coverage).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "restart"))
        monkeypatch.setattr(proxy, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert proxy._FOREVER_HOSTS == []

    async def test_deny_verdict_drops_and_rejects(self, proxy, monkeypatch):
        # deny -> drop the SYN + install a REJECT (tcp-reset) so the retransmit
        # gets RST'd (ECONNREFUSED), not a ~127s tcp_syn_retries wait.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        rejected = []
        monkeypatch.setattr(
            proxy,
            "reject",
            lambda ip, port, ttl: rejected.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        assert rejected == [
            ("1.2.3.4", 443, proxy.CONSENT_REJECT_TTL)
        ]  # eager deny

    async def test_request_error_drops(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=RuntimeError("boom"))
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"  # except -> deny -> drop

    async def test_verdict_timeout_drops(self, proxy, monkeypatch):
        # client.request enforces the hold timeout (asyncio.wait_for); a timeout
        # raises -> _decide_and_verdict fail-closes to deny -> drop.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=asyncio.TimeoutError)
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"

    async def test_ws_down_drops_without_request(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = False
        client.request = AsyncMock()
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)  # _cb drops inline (no task)
        assert pkt.verdict == "drop"
        client.request.assert_not_awaited()

    async def test_unparseable_dest_drops(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "restart"))
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(b"\x00" * 24)  # version nibble 0 -> parse_dest ("", 0)
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        client.request.assert_not_awaited()

    async def test_retransmit_reuses_verdict_without_re_request(
        self, proxy, monkeypatch
    ):
        # A SYN retransmit (tcp_syn_retries) of an already-decided flow reuses the
        # cached verdict so it doesn't re-prompt the decider.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "restart"))
        monkeypatch.setattr(proxy, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        pkt1 = _FakePkt(_ip_payload("1.2.3.4", 443))
        pkt2 = _FakePkt(_ip_payload("1.2.3.4", 443))  # retransmit
        await self._decide(proxy, pkt1, client)
        proxy._cb(pkt2, client)  # cache hit -> inline accept, no task
        assert pkt1.verdict == "accept"
        assert pkt2.verdict == "accept"  # reused the cached allow
        client.request.assert_awaited_once()  # NOT twice

    async def test_distinct_flows_held_concurrently_not_serialized(
        self, proxy, monkeypatch
    ):
        # #2329: distinct flows are held concurrently (two verdict tasks in
        # flight), not one-behind-the-other. The pre-fix blocking _cb serialized
        # them; this gates request() on both being started before either resolves.
        started = []
        gate = asyncio.Event()

        async def slow_request(host, port):
            started.append((host, port))
            await gate.wait()
            return "allow"

        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=slow_request)
        monkeypatch.setattr(proxy, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._cb(_FakePkt(_ip_payload("1.2.3.4", 443)), client)
        proxy._cb(_FakePkt(_ip_payload("5.6.7.8", 80)), client)
        # both verdict tasks started concurrently before either resolved
        for _ in range(100):
            if len(started) == 2:
                break
            await asyncio.sleep(0.001)
        assert len(proxy._BG_TASKS) == 2
        assert set(started) == {("1.2.3.4", 443), ("5.6.7.8", 80)}
        gate.set()  # release both
        await asyncio.gather(*proxy._BG_TASKS)

    async def test_allow_once_does_not_learn(self, proxy, monkeypatch):
        # `once` allow -> no ACCEPT rule (just this connection; reconnect
        # re-prompts).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "once"))
        learned = []
        monkeypatch.setattr(proxy, "allow", lambda *a: learned.append(a))
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        assert learned == []  # no learn for `once`

    async def test_allow_timed_duration_learns_for_ttl(
        self, proxy, monkeypatch
    ):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "1h"))
        learned = []
        monkeypatch.setattr(
            proxy,
            "allow",
            lambda ip, port, ttl: learned.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        assert learned == [("1.2.3.4", None, 3600)]  # 1h -> 3600s

    async def test_deny_timed_duration_rejects_for_ttl(
        self, proxy, monkeypatch
    ):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "15m"))
        rejected = []
        monkeypatch.setattr(
            proxy,
            "reject",
            lambda ip, port, ttl: rejected.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        assert rejected == [("1.2.3.4", 443, 900)]  # 15m -> 900s

    async def test_deny_once_uses_short_fail_close_ttl(
        self, proxy, monkeypatch
    ):
        # `once` deny -> the short CONSENT_REJECT_TTL (fail-close this conn).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        rejected = []
        monkeypatch.setattr(
            proxy, "reject", lambda ip, port, ttl: rejected.append(ttl)
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        assert rejected == [proxy.CONSENT_REJECT_TTL]

    # --- the forged eager-deny RST (#2345): connect() gets ECONNREFUSED at
    # once, independent of the conntrack/retransmit race. ---

    async def test_deny_verdict_forges_rst(self, proxy, monkeypatch):
        # deny -> forge the RST directly (ECONNREFUSED at once) + install the
        # REJECT backstop. The RST is sourced from the denied host and routed
        # to the workspace's local address.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy, "reject", lambda *a: None)
        sock = MagicMock()
        monkeypatch.setattr(proxy, "_RST_SOCK", sock)
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        sock.sendto.assert_called_once_with(
            proxy.build_rst_packet("1.2.3.4", 443, "10.0.0.5", 50000, 0x1111),
            ("10.0.0.5", 0),
        )

    async def test_deny_no_rst_socket_falls_back_to_reject(
        self, proxy, monkeypatch
    ):
        # No RST socket (NET_RAW absent / consent off) -> only the REJECT rule;
        # no exception, still drops.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy, "reject", lambda *a: None)
        monkeypatch.setattr(proxy, "_RST_SOCK", None)
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"

    async def test_cache_hit_deny_forges_rst(self, proxy, monkeypatch):
        # A retried connect() to a denied flow reuses the cached verdict and
        # also forges the RST (fails fast), not just drops.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy, "reject", lambda *a: None)
        sock = MagicMock()
        monkeypatch.setattr(proxy, "_RST_SOCK", sock)
        self._bind(proxy, monkeypatch, client)
        pkt1 = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 1))
        await self._decide(proxy, pkt1, client)  # populates the cache
        assert sock.sendto.call_count == 1  # original deny forged an RST
        pkt2 = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 2))
        proxy._cb(pkt2, client)  # cache hit -> inline forge + drop
        assert pkt2.verdict == "drop"
        client.request.assert_awaited_once()  # NOT re-requested
        assert sock.sendto.call_count == 2  # the retry also got an RST

    async def test_retransmit_during_hold_does_not_re_prompt(
        self, proxy, monkeypatch
    ):
        # A SYN retransmit that arrives WHILE the first is still held (before
        # any verdict) must not spawn a second consent request -- the in-flight
        # task resolves the flow. Without the _INFLIGHT dedup, each retransmit
        # piled up a pending request that lingered past the first's resolution
        # (#2345 e2e flake).
        started = []
        gate = asyncio.Event()

        async def slow_request(host, port):
            started.append((host, port))
            await gate.wait()
            return "deny"

        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=slow_request)
        monkeypatch.setattr(proxy, "reject", lambda *a: None)
        monkeypatch.setattr(proxy, "_RST_SOCK", None)
        self._bind(proxy, monkeypatch, client)
        pkt1 = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 1))
        pkt2 = _FakePkt(
            _syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 1)
        )  # retransmit during hold
        flow = ("10.0.0.5", 50000, "1.2.3.4", 443)
        proxy._cb(pkt1, client)  # spawns the task; connection is now in-flight
        proxy._cb(pkt2, client)  # in-flight -> drop, no new task
        assert pkt2.verdict == "drop"  # the retransmit is dropped
        assert len(proxy._BG_TASKS) == 1  # only ONE verdict task
        assert flow in proxy._INFLIGHT  # still held (connection-level key)
        gate.set()  # release the verdict
        await asyncio.gather(*proxy._BG_TASKS)
        assert len(started) == 1  # only ONE request to the decider
        assert flow not in proxy._INFLIGHT  # cleared after verdict

    async def test_distinct_source_port_re_prompts_after_allow_once(
        self, proxy, monkeypatch
    ):
        # #2361: a NEW connection (new source port) to the same (dst, port) is a
        # distinct flow, so an allow-once does NOT carry over -- the 2nd SYN is
        # a cache miss and re-prompts. (A retransmit -- same source port -- DOES
        # reuse the verdict; that's test_retransmit_during_hold_* above and the
        # cache-reuse tests.) This is the unit-level grounding of the
        # connection-keyed verdict cache.
        requests = []

        async def fast_request(host, port):
            requests.append((host, port))
            return ("allow", "once")

        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=fast_request)
        monkeypatch.setattr(proxy, "_RST_SOCK", None)
        self._bind(proxy, monkeypatch, client)
        # Connection A (sport 50000) -> allow/once -> cached for flow A only.
        proxy._cb(
            _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 1)),
            client,
        )
        await asyncio.gather(*proxy._BG_TASKS)
        # Connection B (sport 50001, same dst) -> DISTINCT flow -> cache MISS
        # -> re-prompts (a 2nd request), NOT a silent reuse of A's allow.
        proxy._cb(
            _FakePkt(_syn_payload("10.0.0.5", 50001, "1.2.3.4", 443, 1)),
            client,
        )
        await asyncio.gather(*proxy._BG_TASKS)
        assert len(requests) == 2, (
            "a new connection (new source port) must re-prompt after an "
            "allow-once, not reuse the prior verdict"
        )


def test_duration_ttl_mapping(proxy):
    assert proxy._duration_ttl("once") is None
    assert proxy._duration_ttl("5m") == 300
    assert proxy._duration_ttl("1h") == 3600
    assert proxy._duration_ttl("1w") == 604800
    assert proxy._duration_ttl("restart") == proxy._DURATION_FOREVER
    assert proxy._duration_ttl("forever") == proxy._DURATION_FOREVER
    assert proxy._duration_ttl("bogus") is None  # unknown -> None


class TestHandlePacket:
    """The per-packet DNS routing (#2311 half B, #2324): classify -> forward /
    resolve+record / NXDOMAIN. A statically-allow-listed name forwards + learns;
    a denied name in interactive mode resolves + records IP->host (its SYN is
    consent-gated at NFQUEUE); static mode (no client) -> NXDOMAIN."""

    async def test_allowed_name_forwards_and_learns(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "query_name", lambda wire: "allowed.test")
        monkeypatch.setattr(proxy, "SPECS", [("allowed.test", None, False)])
        fwd = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        fwd.assert_awaited_once()  # forwarded + learned, not denied
        s.sendto.assert_not_called()  # no NXDOMAIN

    async def test_denied_no_client_sends_nxdomain(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy, "SPECS", [])
        monkeypatch.setattr(proxy, "nxdomain_for", lambda d: b"NXD")
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))

    async def test_denied_with_client_resolves_and_records(
        self, proxy, monkeypatch
    ):
        # Interactive: a denied name resolves + records IP->host (NO ACCEPT) so
        # its SYN is consent-gated at NFQUEUE -- it is NOT held at the DNS query.
        monkeypatch.setattr(proxy, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy, "SPECS", [])
        rec = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_record", rec)
        nxd = MagicMock()
        monkeypatch.setattr(proxy, "_send_nxdomain", nxd)
        client = MagicMock()
        client.connected = True
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), client)
        rec.assert_awaited_once_with(s, b"q", ("1.2.3.4", 53), "evil.test")
        nxd.assert_not_called()  # not NXDOMAIN -- resolves for the SYN gate

    async def test_malformed_query_is_dropped(self, proxy, monkeypatch):
        def _boom(wire):
            raise RuntimeError("bad wire")

        monkeypatch.setattr(proxy, "query_name", _boom)
        fwd = AsyncMock()
        monkeypatch.setattr(proxy, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_not_called()  # dropped, no response
        fwd.assert_not_awaited()


class TestDropForHost:
    """Revoke rule-drop (#2339): drop_for_host + the drop_rule dispatch/ack."""

    def test_allowed_removes_learned_accept(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        runs = []
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: (
                runs.append(a[0]) or types.SimpleNamespace(returncode=0)
            ),
        )
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9999.0,
            "ports": {443, None},
            "host": "evil.test",
        }
        proxy.drop_for_host("evil.test", "allowed")
        assert "1.2.3.4" not in proxy._LEARNED
        # an ACCEPT rule delete per port
        assert sum(1 for r in runs if "-D" in r and "ACCEPT" in r) == 2

    def test_allowed_direct_ip_removes_learned(self, proxy, monkeypatch):
        # #2339 review #1: a direct-IP allow records host=None (no DNS), so the
        # allow branch must also admit the host string itself as a candidate IP.
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        runs = []
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: (
                runs.append(a[0]) or types.SimpleNamespace(returncode=0)
            ),
        )
        monkeypatch.setattr(proxy.time, "time", lambda: 0.0)
        proxy.allow(
            "203.0.113.9", None, 3600
        )  # direct-IP allow, host stays None
        assert proxy._LEARNED["203.0.113.9"]["host"] is None
        proxy.drop_for_host("203.0.113.9", "allowed")
        assert "203.0.113.9" not in proxy._LEARNED  # rule + record removed
        assert any("-D" in r and "ACCEPT" in r for r in runs)

    def test_denied_removes_rejects(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        runs = []
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: (
                runs.append(a[0]) or types.SimpleNamespace(returncode=0)
            ),
        )
        # the denied IP was DNS-recorded (host set) + has a REJECT rule
        proxy._LEARNED["5.6.7.8"] = {
            "expire": 9999.0,
            "ports": set(),
            "host": "bad.test",
        }
        proxy._REJECTED[("5.6.7.8", 443)] = 9999.0
        proxy.drop_for_host("bad.test", "denied")
        assert ("5.6.7.8", 443) not in proxy._REJECTED
        assert any("-D" in r and "REJECT" in r for r in runs)

    def test_denied_direct_ip_match(self, proxy, monkeypatch):
        # a direct-IP deny (host == ip, never DNS host-recorded) is still dropped
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        runs = []
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: (
                runs.append(a[0]) or types.SimpleNamespace(returncode=0)
            ),
        )
        proxy._REJECTED[("9.9.9.9", 80)] = 9999.0
        proxy.drop_for_host("9.9.9.9", "denied")
        assert ("9.9.9.9", 80) not in proxy._REJECTED

    def test_no_match_is_noop(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        ran = []
        monkeypatch.setattr(
            proxy.subprocess, "run", lambda *a, **k: ran.append(a[0])
        )
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9.0,
            "ports": {443},
            "host": "other.test",
        }
        proxy.drop_for_host("evil.test", "allowed")
        assert "1.2.3.4" in proxy._LEARNED  # untouched
        assert ran == []

    def test_unknown_decision_is_noop(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        ran = []
        monkeypatch.setattr(
            proxy.subprocess, "run", lambda *a, **k: ran.append(a[0])
        )
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9.0,
            "ports": {443},
            "host": "h.test",
        }
        proxy.drop_for_host("h.test", "bogus")
        assert "1.2.3.4" in proxy._LEARNED
        assert ran == []

    async def test_dispatch_drop_rule_acks_ok(
        self, proxy, tmp_path, monkeypatch
    ):
        proxy._LEARNED.clear()
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=0),
        )
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        await c._dispatch(
            json.dumps(
                {
                    "type": "drop_rule",
                    "id": "ack-1",
                    "host": "h.test",
                    "decision": "allowed",
                }
            )
        )
        ack = json.loads(sent[0])
        assert ack["type"] == "drop_ack"
        assert ack["id"] == "ack-1"
        assert ack["ok"] is True

    async def test_dispatch_drop_rule_bad_payload_acks_false(
        self, proxy, tmp_path
    ):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        # missing host + bogus decision -> drop skipped, ack ok=False
        await c._dispatch(
            json.dumps(
                {"type": "drop_rule", "id": "ack-2", "decision": "bogus"}
            )
        )
        ack = json.loads(sent[0])
        assert ack["ok"] is False
