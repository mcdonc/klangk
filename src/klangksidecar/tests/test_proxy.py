"""Unit tests for the network sidecar's DNS proxy (the ``klangksidecar`` package).

The proxy began as a byte-for-byte copy of ``src/containers/network/proxy.py``
(the container-embedded copy that still ships in the sidecar image) and is now
split into per-concern submodules (config/state/allowlist/rules/resolve/packets/
consent/nfqueue/app); this suite drives its helpers directly via the flat
``klangksidecar.X`` re-export. ``app.main()`` runs only under
``__name__ == "__main__"``, so importing the package is safe. The
main()/signal-handler/real-socket paths are exercised end-to-end by the
real-podman e2e (``src/klangk/klangkd-tests/e2e-tests/test_network_sidecar_e2e.py``),
not here, so this suite imposes no coverage gate.
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


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
    """The ``klangksidecar`` package (the proxy, split into submodules #2450).

    The proxy was a single ``proxy.py``; it is now the ``klangksidecar`` package
    (config/state/allowlist/rules/resolve/packets/consent/nfqueue/app). This returns
    the package; the re-export in ``__init__`` keeps the flat ``proxy.X`` API these
    tests use. Monkeypatching a package-defined function targets its defining
    submodule (``proxy.rules``, ``proxy.resolve``, ...).
    """
    _install_dns_stubs()
    import klangksidecar

    return klangksidecar


@pytest.fixture
def learned(proxy):
    """A proxy module with the learned-IP table cleared (TTL/sweep tests)."""
    proxy._LEARNED.clear()
    return proxy


class TestParseSpecs:
    """Env parsing → ``(host, port|None, mode)`` triples. CIDRs are skipped
    (the entrypoint applies those statically); the grammar mirrors
    ``klangk.netfilter.parse_allowed_domains`` (#2377 nginx-style scopes)."""

    def test_bare_is_exact_dot_is_inclusive_wildcard_is_subdomains(
        self, proxy, monkeypatch
    ):
        monkeypatch.setenv(
            "KLANGKNETWORK_EGRESS_ALLOW",
            "github.com:443,*.pypi.org,.pypi.org,bare.com:8443,"
            "10.0.0.0/8,10.0.0.0/8:53",
        )
        assert proxy.parse_specs() == [
            ("github.com", 443, proxy._EXACT),  # bare -> exact
            ("pypi.org", None, proxy._SUBDOMAINS),  # *.pypi.org
            ("pypi.org", None, proxy._INCLUSIVE),  # .pypi.org
            ("bare.com", 8443, proxy._EXACT),
        ]

    def test_wildcard_with_port(self, proxy, monkeypatch):
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_ALLOW", "*.pypi.org:443")
        assert proxy.parse_specs() == [("pypi.org", 443, proxy._SUBDOMAINS)]

    def test_inclusive_with_port(self, proxy, monkeypatch):
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_ALLOW", ".pypi.org:443")
        assert proxy.parse_specs() == [("pypi.org", 443, proxy._INCLUSIVE)]

    def test_empty_env(self, proxy, monkeypatch):
        monkeypatch.delenv("KLANGKNETWORK_EGRESS_ALLOW", raising=False)
        assert proxy.parse_specs() == []


class TestPortsFor:
    """``ports_for`` is the allow gate (#2377 nginx-style scopes). Returns
    ``None`` (all ports), a port ``set`` (scoped allow), or ``set()`` (deny).
    Bare host = EXACT (apex only); ``.host`` = INCLUSIVE (apex + subdomains);
    ``*.host`` = SUBDOMAINS only. Pin the boundaries so a refactor can't
    silently weaken them."""

    def test_bare_host_is_exact_apex_only(self, proxy, monkeypatch):
        # bare github.com -> EXACT (#2377): apex only, NOT subdomains.
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("github.com", None, proxy._EXACT)]
        )
        assert proxy.ports_for("github.com") is None  # apex
        assert proxy.ports_for("api.github.com") == set()  # subdomain denied
        assert proxy.ports_for("evilgithub.com") == set()  # boundary
        assert proxy.ports_for("github.com.attacker.test") == set()

    def test_inclusive_matches_apex_and_subdomains(self, proxy, monkeypatch):
        # .github.com -> apex + subdomains (any depth) -- the old bare behavior.
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("github.com", None, proxy._INCLUSIVE)]
        )
        assert proxy.ports_for("github.com") is None
        assert proxy.ports_for("api.github.com") is None
        assert proxy.ports_for("a.b.github.com") is None
        assert proxy.ports_for("evilgithub.com") == set()  # boundary

    def test_wildcard_excludes_apex(self, proxy, monkeypatch):
        # *.pypi.org -> subdomains only, NOT the apex.
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("pypi.org", None, proxy._SUBDOMAINS)]
        )
        assert proxy.ports_for("pypi.org") == set()
        assert proxy.ports_for("a.pypi.org") is None
        assert proxy.ports_for("a.b.pypi.org") is None

    def test_wildcard_with_port(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("pypi.org", 443, proxy._SUBDOMAINS)]
        )
        assert proxy.ports_for("pypi.org") == set()
        assert proxy.ports_for("x.pypi.org") == {443}

    def test_port_scope_exact(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("github.com", 443, proxy._EXACT)]
        )
        assert proxy.ports_for("github.com") == {443}
        assert proxy.ports_for("api.github.com") == set()  # exact -> no subdomain

    def test_all_ports_spec_dominates(self, proxy, monkeypatch):
        # github.com (all ports) + github.com:443 -> all ports win.
        monkeypatch.setattr(
            proxy.allowlist,
            "SPECS",
            [
                ("github.com", None, proxy._EXACT),
                ("github.com", 443, proxy._EXACT),
            ],
        )
        assert proxy.ports_for("github.com") is None

    def test_multiple_ports_union(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.allowlist,
            "SPECS",
            [
                ("github.com", 443, proxy._EXACT),
                ("github.com", 8443, proxy._EXACT),
            ],
        )
        assert proxy.ports_for("github.com") == {443, 8443}

    def test_empty_specs_denies_all(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        assert proxy.ports_for("anything.test") == set()

    def test_case_sensitive_relies_on_caller_lowercasing(self, proxy, monkeypatch):
        # ports_for compares verbatim; parse_specs/query_name lowercase at the
        # edges. A mixed-case qname does NOT match a lowercased spec.
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("github.com", None, proxy._EXACT)]
        )
        assert proxy.ports_for("GitHub.Com") == set()

    def test_session_host_allows_consulted_alongside_specs(self, proxy, monkeypatch):
        # A consent `allow` verdict (timed or forever) adds the host (EXACT) to
        # _SESSION_HOST_ALLOWS (#2372, #2377, #2434): ports_for treats that exact
        # host as allow-listed for the verdict's lifetime, so a later CDN-rotated
        # IP re-resolves and is allowed without re-prompting.
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", 443, proxy._EXACT, float("inf"))],
        )
        assert proxy.ports_for("example.com") == {443}
        assert proxy.ports_for("api.example.com") == set()  # exact -> no subdomain
        assert proxy.ports_for("other.com") == set()

    def test_add_session_host_dedups_and_refreshes(self, proxy, monkeypatch):
        # A re-allow of the same host:port refreshes the expiry (max -- never
        # shortens an unexpired entry); a different port adds a new entry.
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy._add_session_host("example.com", 443, 300)  # expire 1300
        proxy._add_session_host("example.com", 443, 60)  # dup -> max(1300, 1060)
        proxy._add_session_host("example.com", 8443, 300)  # different port
        assert proxy.state._SESSION_HOST_ALLOWS == [
            ("example.com", 443, proxy._EXACT, 1300.0),
            ("example.com", 8443, proxy._EXACT, 1300.0),
        ]

    def test_session_host_allows_ttl(self, proxy, monkeypatch):
        # The NFQUEUE-gate predicate (#2372, #2434): session entries are EXACT
        # (#2377), so only the approved host (not its subdomains) matches; port
        # must match. Returns the remaining TTL (truthy) or None. Mirrors
        # ports_for via the shared _host_matches.
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", 443, proxy._EXACT, float("inf"))],
        )
        assert proxy._session_host_allows_ttl("example.com", 443)
        assert proxy._session_host_allows_ttl("api.example.com", 443) is None
        assert proxy._session_host_allows_ttl("example.com", 80) is None
        assert proxy._session_host_allows_ttl("other.com", 443) is None
        assert proxy._session_host_allows_ttl("evilexample.com", 443) is None
        assert proxy._session_host_allows_ttl("", 443) is None
        # all-ports entry (p is None) matches any port -- defensive (entries are
        # added port-scoped, but the predicate tolerates None).
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", None, proxy._EXACT, float("inf"))],
        )
        assert proxy._session_host_allows_ttl("example.com", 8080)

    def test_session_allow_rule_cap_returns_remaining(self, proxy, monkeypatch):
        # #2465: the DNS-path learn cap. A timed session allow bounds the
        # learned ACCEPT rule at its remaining window so the rule lapses with
        # the verdict (not the response's DNS TTL). Returns the min remaining
        # across matching entries; EXACT scope (#2377 -- only the approved
        # host, not its subdomains).
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", 443, proxy._EXACT, 1005.0)],  # 5s remaining
        )
        assert proxy._session_allow_rule_cap("example.com") == 5.0
        assert (
            proxy._session_allow_rule_cap("api.example.com") is None
        )  # exact -> no subdomain
        assert proxy._session_allow_rule_cap("other.com") is None

    def test_session_allow_rule_cap_none_for_static_spec(self, proxy, monkeypatch):
        # A static SPECS entry is a forever allow, so the DNS TTL is the correct
        # rule lifetime -- no cap (capping would expire the rule early and
        # re-prompt a forever-allowed host in the gap before the next resolve;
        # a static spec has no NFQUEUE gate, only the learned rule). Returns
        # None even when a session allow also matches.
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("example.com", None, proxy._EXACT)]
        )
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", 443, proxy._EXACT, 1005.0)],
        )
        assert proxy._session_allow_rule_cap("example.com") is None

    def test_session_allow_rule_cap_expired_entry_pruned(self, proxy, monkeypatch):
        # A session allow past its window is pruned (lazy sweep) -> no match ->
        # None. This is the within-vs-exceeding boundary: once the verdict has
        # lapsed, the DNS path stops bounding (and, via ports_for, stops
        # learning) the host.
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", 443, proxy._EXACT, 999.0)],  # already expired
        )
        assert proxy._session_allow_rule_cap("example.com") is None
        assert proxy.state._SESSION_HOST_ALLOWS == []  # pruned

    def test_session_allow_rule_cap_min_across_entries(self, proxy, monkeypatch):
        # Two session allows for the host (different ports) -> the most
        # restrictive (min remaining) bounds the rule. (Multi-port combo is an
        # edge case; the min is a conservative bound -- a longer-lived port's
        # rule may lapse early, but its session allow still covers the SYN at
        # the NFQUEUE gate, so no re-prompt.)
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [
                ("example.com", 443, proxy._EXACT, 1005.0),  # 5s
                ("example.com", 8443, proxy._EXACT, 1300.0),  # 300s
            ],
        )
        assert proxy._session_allow_rule_cap("example.com") == 5.0

    def test_add_session_deny_dedups_and_refreshes(self, proxy, monkeypatch):
        # Mirror of test_add_session_host_dedups_and_refreshes (#2446): a
        # re-deny of the same host:port refreshes the expiry (max -- never
        # shortens an unexpired entry); a different port adds a new entry.
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        proxy.state._SESSION_HOST_DENIES.clear()
        proxy._add_session_deny("example.com", 443, 300)  # expire 1300
        proxy._add_session_deny("example.com", 443, 60)  # dup -> max(1300, 1060)
        proxy._add_session_deny("example.com", 8443, 300)  # different port
        assert proxy.state._SESSION_HOST_DENIES == [
            ("example.com", 443, proxy._EXACT, 1300.0),
            ("example.com", 8443, proxy._EXACT, 1300.0),
        ]

    def test_session_host_denies_ttl(self, proxy, monkeypatch):
        # The deny-side NFQUEUE-gate predicate (#2446): entries are EXACT
        # (#2377 -- only the denied host, not its subdomains); port must match.
        # Returns the remaining TTL (truthy) or None. Mirrors
        # test_session_host_allows_ttl via the shared _host_matches.
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_DENIES",
            [("example.com", 443, proxy._EXACT, float("inf"))],
        )
        assert proxy._session_host_denies_ttl("example.com", 443)
        assert proxy._session_host_denies_ttl("api.example.com", 443) is None
        assert proxy._session_host_denies_ttl("example.com", 80) is None
        assert proxy._session_host_denies_ttl("other.com", 443) is None
        assert proxy._session_host_denies_ttl("evilexample.com", 443) is None
        assert proxy._session_host_denies_ttl("", 443) is None
        # all-ports entry (p is None) matches any port.
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_DENIES",
            [("example.com", None, proxy._EXACT, float("inf"))],
        )
        assert proxy._session_host_denies_ttl("example.com", 8080)


class TestRejectedFor:
    """``rejected_for`` is the static deny gate (#2367): a name matching a
    REJECT_SPECS entry is NXDOMAIN'd unconditionally. Same nginx-style scopes
    as ``ports_for`` (bare = exact apex, ``.host`` = inclusive, ``*.host`` =
    subdomains only). The module-level ``REJECT_SPECS`` is computed at import,
    so each test reparses from the env + reassigns it before asserting."""

    def test_bare_is_exact_apex_only(self, proxy, monkeypatch):
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_REJECT", "evil.com")
        proxy.allowlist.REJECT_SPECS = proxy.parse_specs("KLANGKNETWORK_EGRESS_REJECT")
        assert proxy.rejected_for("evil.com") is True  # apex
        assert proxy.rejected_for("api.evil.com") is False  # subdomain (exact)
        assert proxy.rejected_for("evilevil.com") is False  # suffix boundary

    def test_inclusive_matches_apex_and_subdomains(self, proxy, monkeypatch):
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_REJECT", ".malicious.net")
        proxy.allowlist.REJECT_SPECS = proxy.parse_specs("KLANGKNETWORK_EGRESS_REJECT")
        assert proxy.rejected_for("malicious.net") is True
        assert proxy.rejected_for("x.malicious.net") is True
        assert proxy.rejected_for("xmalicious.net") is False  # boundary

    def test_subdomains_excludes_apex(self, proxy, monkeypatch):
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_REJECT", "*.bad.org")
        proxy.allowlist.REJECT_SPECS = proxy.parse_specs("KLANGKNETWORK_EGRESS_REJECT")
        assert proxy.rejected_for("bad.org") is False  # apex not rejected
        assert proxy.rejected_for("a.bad.org") is True

    def test_empty_reject_specs_denies_nothing(self, proxy, monkeypatch):
        monkeypatch.delenv("KLANGKNETWORK_EGRESS_REJECT", raising=False)
        proxy.allowlist.REJECT_SPECS = proxy.parse_specs("KLANGKNETWORK_EGRESS_REJECT")
        assert proxy.rejected_for("anything.test") is False


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

    def test_cross_domain_shared_ip_accumulates_ports(self, learned, monkeypatch):
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
            learned.rules, "_remove", lambda ip, port: removed.append((ip, port))
        )
        learned._LEARNED["1.2.3.4"] = {"expire": 500.0, "ports": {443, None}}
        gone = learned.sweep_once(now=1000.0)
        assert gone == [("1.2.3.4", {443, None})]
        assert ("1.2.3.4", 443) in removed
        assert ("1.2.3.4", None) in removed
        assert "1.2.3.4" not in learned._LEARNED

    def test_sweep_keeps_live_ips(self, learned, monkeypatch):
        monkeypatch.setattr(learned.rules, "_remove", lambda *a: None)
        learned._LEARNED["1.2.3.4"] = {"expire": 2000.0, "ports": {443}}
        assert learned.sweep_once(now=1000.0) == []
        assert "1.2.3.4" in learned._LEARNED

    def test_sweep_boundary_keeps_equal_expire(self, learned, monkeypatch):
        # expire <= now is removed; a rule valid through `now` (expire > now)
        # is kept. expire == now counts as expired.
        monkeypatch.setattr(learned.rules, "_remove", lambda *a: None)
        learned._LEARNED["dead"] = {"expire": 1000.0, "ports": {443}}
        learned._LEARNED["live"] = {"expire": 1000.001, "ports": {443}}
        gone = learned.sweep_once(now=1000.0)
        assert [ip for ip, _ in gone] == ["dead"]
        assert "live" in learned._LEARNED

    def test_consent_allow_rule_expire_is_separate_from_dns_ttl(
        self, learned, monkeypatch
    ):
        # #2408: a consent allow's ACCEPT rule must expire at the verdict, not at
        # the host-mapping DNS TTL. _record_hosts records the IP with a long DNS
        # TTL; a subsequent short consent allow installs the ACCEPT with a SHORT
        # rule_expire (separate from the long host-mapping expire). The consent
        # path passes floor=False (#2465) so a 5s verdict's rule is 5s, not
        # MIN_TTL-floored.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned._record_hosts([("1.2.3.4", 300)], "evil.test")  # DNS TTL 300s
        learned.allow(
            "1.2.3.4", None, 5, floor=False
        )  # consent allow, 5s (NOT floored -> 5s, #2465)
        rec = learned._LEARNED["1.2.3.4"]
        # rule_expire is the verdict (5s), NOT the DNS TTL and NOT MIN_TTL;
        # the host-mapping expire keeps the longer DNS TTL. The two are separate.
        assert rec["rule_expire"] == 5.0
        assert rec["expire"] == 300.0
        assert rec["rule_expire"] < rec["expire"]
        assert rec["ports"] == {None}

    def test_sweep_removes_consent_rule_at_verdict_keeps_host_mapping(
        self, learned, monkeypatch
    ):
        # #2408: at the consent verdict's TTL the ACCEPT rule is swept, but the
        # record (host mapping) is KEPT while its longer DNS-TTL expire is still
        # valid -- so _host_for can still name the host for the fresh
        # re-prompt. Only after the DNS TTL does the record itself go.
        removed = []
        monkeypatch.setattr(
            learned.rules, "_remove", lambda ip, port: removed.append((ip, port))
        )
        learned._LEARNED["1.2.3.4"] = {
            "expire": 300.0,  # host mapping (DNS TTL)
            "rule_expire": 5.0,  # consent verdict
            "ports": {None},
            "host": "evil.test",
        }
        # at t=5: rule swept, record kept for naming.
        gone = learned.sweep_once(now=5.0)
        assert gone == [("1.2.3.4", {None})]
        assert removed == [("1.2.3.4", None)]
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["ports"] == set()  # rule gone
        assert rec["host"] == "evil.test"  # mapping retained
        # at t=300: the host mapping elapses -> record dropped.
        learned.sweep_once(now=300.0)
        assert "1.2.3.4" not in learned._LEARNED

    def test_consent_allow_rule_not_extended_by_re_resolve_regression(
        self, learned, monkeypatch
    ):
        # #2408 exact-bug regression: a short consent allow's ACCEPT rule must
        # expire at the verdict even when the host is re-resolved (each new
        # connection re-queries DNS, bumping the host-mapping expire to the
        # longer DNS TTL). allow(short) -> _record_hosts(long) -> sweep(at
        # verdict) -> rule gone, host mapping retained.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        removed = []
        monkeypatch.setattr(
            learned.rules, "_remove", lambda ip, port: removed.append((ip, port))
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned.allow(
            "1.2.3.4", None, 5, floor=False
        )  # consent allow (NOT floored -> 5s, #2465)
        verdict = learned._LEARNED["1.2.3.4"]["rule_expire"]
        # the re-resolve (long DNS TTL) must NOT extend the rule's lifetime.
        learned._record_hosts([("1.2.3.4", 300)], "evil.test")
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["rule_expire"] == verdict
        assert rec["expire"] > verdict  # host mapping kept the DNS TTL
        # sweep at the verdict -> rule removed, record retained for naming.
        gone = learned.sweep_once(now=verdict)
        assert gone == [("1.2.3.4", {None})]
        assert removed == [("1.2.3.4", None)]
        kept = learned._LEARNED["1.2.3.4"]
        assert kept["ports"] == set()
        assert kept["host"] == "evil.test"

    def test_timed_allow_dns_learn_lapses_at_verdict_under_default_min_ttl(
        self, learned, monkeypatch
    ):
        # Reviewer I1/B1: prove the security property under PRODUCTION conditions
        # -- a UI-offered duration (5m=300s, >> default MIN_TTL=30), a real
        # timeline (verdict t=0, re-resolve t=120), and the REAL allow() ->
        # _LEARNED (no mock). A mid-window re-resolve carrying a long DNS TTL
        # must NOT extend rule_expire past the verdict; sweep at the verdict
        # removes the rule while the longer-DNS-TTL host mapping is retained.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        removed = []
        monkeypatch.setattr(
            learned.rules, "_remove", lambda ip, port: removed.append((ip, port))
        )
        assert learned.MIN_TTL == 30  # the production default
        # t=0: initial resolve (host mapping at a long DNS TTL) + a 5m consent
        # allow (floor=False -> rule_expire is the verdict, not MIN_TTL).
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned._record_hosts([("1.2.3.4", 600)], "example.com")
        learned.allow("1.2.3.4", 443, 300, floor=False)  # 5m verdict
        assert learned._LEARNED["1.2.3.4"]["rule_expire"] == 300.0
        # t=120: mid-window re-resolve; the session allow has 180s remaining.
        # The DNS response carries a 600s TTL; WITHOUT the cap that would push
        # rule_expire to 720 (120+600). The cap bounds it at min(600, 180)=180,
        # which allow() (floor=False) lands at 120+180=300 -> rule stays at 300.
        monkeypatch.setattr(learned.time, "time", lambda: 120.0)
        learned._learn_all([("1.2.3.4", 600)], {443}, cap=180)
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["rule_expire"] == 300.0  # NOT 720 -- bounded at the verdict
        # t=300 (the verdict): rule swept, host mapping (DNS TTL 600) retained.
        gone = learned.sweep_once(now=300.0)
        assert gone == [("1.2.3.4", {443})]
        assert removed == [("1.2.3.4", 443)]
        kept = learned._LEARNED["1.2.3.4"]
        assert kept["ports"] == set()  # rule gone
        assert kept["host"] == "example.com"  # mapping retained for naming
        assert kept["expire"] > 300  # outlives the rule (DNS TTL 600)

    def test_short_verdict_lapses_at_verdict_not_min_ttl(self, learned, monkeypatch):
        # B1 (#2465 + reviewer): a sub-MIN_TTL consent verdict (the test-only
        # 5s) must lapse at 5s, NOT at MIN_TTL=30. Pre-fix the consent path
        # floored the rule at MIN_TTL, so a 5s verdict's rule lived 30s and a
        # retry at ~10s connected with no re-prompt. floor=False (consent path +
        # capped DNS learn) makes the rule lapse at the verdict even under the
        # default MIN_TTL. Verified end-to-end on both the verdict learn and a
        # mid-window capped re-resolve.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        removed = []
        monkeypatch.setattr(
            learned.rules, "_remove", lambda ip, port: removed.append((ip, port))
        )
        assert learned.MIN_TTL == 30  # the production default
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned._record_hosts([("1.2.3.4", 600)], "example.com")
        learned.allow("1.2.3.4", 443, 5, floor=False)  # 5s verdict
        assert learned._LEARNED["1.2.3.4"]["rule_expire"] == 5.0  # NOT 30
        # t=2: mid-window re-resolve (cap=3 remaining); the capped, unfloored
        # learn keeps rule_expire at the verdict (5), not MIN_TTL (30+).
        monkeypatch.setattr(learned.time, "time", lambda: 2.0)
        learned._learn_all([("1.2.3.4", 300)], {443}, cap=3)
        assert learned._LEARNED["1.2.3.4"]["rule_expire"] == 5.0  # still 5
        # sweep at the verdict (t=5) -> rule gone; host mapping retained.
        gone = learned.sweep_once(now=5.0)
        assert gone == [("1.2.3.4", {443})]
        assert removed == [("1.2.3.4", 443)]
        assert learned._LEARNED["1.2.3.4"]["host"] == "example.com"

    def test_dns_learn_uncapped_extends_past_verdict(self, learned, monkeypatch):
        # The bug the cap corrects (documents #2465): a session-allow host's
        # DNS-path learn WITH the DNS TTL (no cap) extends rule_expire past the
        # verdict -- the rule outlives the verdict, so sweep at the verdict
        # leaves it in place (the retry-past-window connects with no
        # re-prompt). Here _learn_all is called WITHOUT the cap (as if the
        # _session_allow_rule_cap step were missing); the realistic test above
        # shows the cap prevents it.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        learned._record_hosts([("1.2.3.4", 600)], "example.com")
        learned.allow("1.2.3.4", 443, 300, floor=False)  # 5m verdict
        verdict = learned._LEARNED["1.2.3.4"]["rule_expire"]
        assert verdict == 300.0
        monkeypatch.setattr(learned.time, "time", lambda: 120.0)
        learned._learn_all([("1.2.3.4", 600)], {443})  # NO cap -> DNS TTL wins
        assert (
            learned._LEARNED["1.2.3.4"]["rule_expire"] == 720.0
        )  # 120+600 -> extended past the 300s verdict -> the bug

    def test_static_spec_learn_0_ttl_floored_at_min_ttl(self, learned, monkeypatch):
        # Nit 3 (#2465 review): the static-spec DNS learn (cap=None) keeps the
        # MIN_TTL floor even though `floor` is now conditional -- a 0-TTL DNS
        # response must not yank the rule the workspace just resolved. Uses the
        # real allow() -> _LEARNED (the floor is applied inside allow, so a mock
        # of allow would not see it). Guards the 0-TTL safety net across the
        # floor-is-conditional change.
        monkeypatch.setattr(
            learned.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=1),
        )
        monkeypatch.setattr(learned.time, "time", lambda: 0.0)
        # cap=None (default) -> floor=True -> a 0-TTL A record is floored up to
        # MIN_TTL, not installed as a 0-lifetime rule.
        learned._learn_all([("1.2.3.4", 0)], {443})
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["rule_expire"] == learned.MIN_TTL  # 0 floored up, not yanked
        assert rec["ports"] == {443}

    def test_reject_installs_reject_rule_and_records(self, learned, monkeypatch):
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=1)  # rule absent -> -I

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        monkeypatch.setattr(learned.time, "time", lambda: 1000.0)
        learned._REJECTED.clear()
        learned.reject("1.2.3.4", 80, 10)
        assert any("-I" in a and "REJECT" in a and "tcp-reset" in a for a in runs)
        assert not any("--sport" in a for a in runs)  # dest-scoped omits --sport
        assert learned._REJECTED[("1.2.3.4", 80, 0)] == 1010.0

    def test_reject_connection_scoped_adds_sport(self, learned, monkeypatch):
        # #2463: a connection-scoped REJECT (nonzero sport) carries --sport so a
        # real iptables rule matches only retransmits of THAT connection; a
        # destination-scoped reject (sport 0) omits it. The (ip, port, sport)
        # key keeps the two rule kinds distinct in _REJECTED.
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=1)  # rule absent -> -I

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        monkeypatch.setattr(learned.time, "time", lambda: 1000.0)
        learned._REJECTED.clear()
        learned.reject("1.2.3.4", 443, 10, 50000)  # connection-scoped
        scoped = [a for a in runs if "--sport" in a][0]
        assert "-d" in scoped and "1.2.3.4" in scoped
        assert "--dport" in scoped and "443" in scoped
        assert scoped[scoped.index("--sport") + 1] == "50000"
        assert "REJECT" in scoped and "tcp-reset" in scoped
        assert learned._REJECTED[("1.2.3.4", 443, 50000)] == 1010.0

    def test_sweep_removes_expired_reject_rules(self, learned, monkeypatch):
        removed = []

        def fake_run(args, **kw):
            if "-D" in args:
                removed.append(args)
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(learned.subprocess, "run", fake_run)
        learned._LEARNED.clear()
        learned._REJECTED.clear()
        learned._REJECTED[("1.2.3.4", 80, 0)] = 100.0  # expired
        learned._REJECTED[("5.6.7.8", 443, 0)] = 9999.0  # live
        learned.sweep_once(now=500.0)
        assert any(
            "1.2.3.4" in a and "REJECT" in a and "tcp-reset" in a for a in removed
        )
        assert ("1.2.3.4", 80, 0) not in learned._REJECTED
        assert ("5.6.7.8", 443, 0) in learned._REJECTED  # live one kept

    def test_all_ports_allow_supersedes_prior_port_denies(self, learned, monkeypatch):
        # An all-ports allow must clear per-port REJECTs for that IP, else the
        # ACCEPT at the top of OUTPUT silently shadows a lingering REJECT (the
        # decider allowed the host -> a prior port-specific deny no longer applies).
        removed = []
        monkeypatch.setattr(
            learned.rules,
            "_remove_reject",
            lambda ip, port, sport=0: removed.append((ip, port, sport)),
        )
        monkeypatch.setattr(learned.rules, "_install", lambda *a: None)
        learned._REJECTED.clear()
        learned._REJECTED[("1.2.3.4", 443, 0)] = 9999.0
        learned._REJECTED[("1.2.3.4", 80, 0)] = 9999.0
        learned._REJECTED[("5.6.7.8", 443, 0)] = 9999.0  # different IP, untouched
        learned.allow("1.2.3.4", None, 60)  # all-ports (consent path)
        assert ("1.2.3.4", 443, 0) in removed
        assert ("1.2.3.4", 80, 0) in removed
        assert ("5.6.7.8", 443, 0) not in removed  # different IP kept
        assert ("1.2.3.4", 443, 0) not in learned._REJECTED
        assert ("5.6.7.8", 443, 0) in learned._REJECTED


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

    def test_apex_and_cname_target_a_records_both_learned(self, proxy, monkeypatch):
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
            proxy.resolve, "a_records_with_ttl", lambda wire: [("1.2.3.4", 100)]
        )

        def _boom(ip, port, ttl):
            raise RuntimeError("iptables transient failure")

        monkeypatch.setattr(proxy.rules, "allow", _boom)
        s = MagicMock()
        # Must not raise (the executor propagates the iptables error, which
        # _respond_allowed swallows).
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "allowed.test", {443}
        )

    async def test_sendto_failure_does_not_propagate(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.resolve, "a_records_with_ttl", lambda wire: [("1.2.3.4", 100)]
        )
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
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
            proxy.resolve,
            "a_records_with_ttl",
            lambda wire: [("1.2.3.4", 100), ("5.6.7.8", 200)],
        )
        calls = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: calls.append((ip, port, ttl)),
        )
        s = MagicMock()
        await proxy._respond_allowed(s, b"resp", ("127.0.0.1", 1234), "q", {443, 8443})
        assert ("1.2.3.4", 443, 100) in calls
        assert ("1.2.3.4", 8443, 100) in calls
        assert ("5.6.7.8", 443, 200) in calls
        assert ("5.6.7.8", 8443, 200) in calls
        s.sendto.assert_called_once_with(b"resp", ("127.0.0.1", 1234))

    async def test_respond_allowed_caps_rule_at_session_allow_window(
        self, proxy, monkeypatch
    ):
        # #2465 regression: a timed session allow's DNS-path learn must NOT
        # install the ACCEPT rule for the response's (long) DNS TTL -- that
        # left a rule outliving a 5s verdict, so a retry past the window
        # connected with no re-prompt. The rule TTL is bounded at the session
        # allow's remaining window (here 5s), not the 300s DNS TTL.
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
        monkeypatch.setattr(
            proxy.state,
            "_SESSION_HOST_ALLOWS",
            [("example.com", 443, proxy._EXACT, 1005.0)],  # 5s remaining
        )
        monkeypatch.setattr(
            proxy.resolve,
            "a_records_with_ttl",
            lambda wire: [("1.2.3.4", 300)],  # DNS TTL 300s
        )
        calls = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: calls.append((ip, port, ttl)),
        )
        s = MagicMock()
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "example.com", {443}
        )
        assert ("1.2.3.4", 443, 5) in calls  # capped at the verdict window
        # the 300s DNS TTL must NOT reach the rule for this session-allowed host.
        assert all(ttl <= 5 for _ip, _port, ttl in calls)

    async def test_respond_allowed_no_cap_for_static_spec(self, proxy, monkeypatch):
        # A static SPECS entry is forever -> the DNS TTL is the correct rule
        # lifetime (no cap). Capping would expire the rule early and, in the
        # gap before the next resolve, re-prompt a forever-allowed host (a
        # static spec has no NFQUEUE gate).
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("example.com", None, proxy._EXACT)]
        )
        monkeypatch.setattr(
            proxy.resolve,
            "a_records_with_ttl",
            lambda wire: [("1.2.3.4", 300)],
        )
        calls = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: calls.append((ip, port, ttl)),
        )
        s = MagicMock()
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "example.com", {None}
        )
        assert ("1.2.3.4", None, 300) in calls  # DNS TTL, not capped

    async def test_respond_allowed_no_cap_when_no_session_allow(
        self, proxy, monkeypatch
    ):
        # No session allow and no static spec for the qname -> no cap. (In
        # production ports_for would have denied this name, so _respond_allowed
        # would not run; the cap helper still returns None defensively.)
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.state, "_SESSION_HOST_ALLOWS", [])
        monkeypatch.setattr(
            proxy.resolve,
            "a_records_with_ttl",
            lambda wire: [("1.2.3.4", 300)],
        )
        calls = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: calls.append((ip, port, ttl)),
        )
        s = MagicMock()
        await proxy._respond_allowed(
            s, b"resp", ("127.0.0.1", 1234), "example.com", {443}
        )
        assert ("1.2.3.4", 443, 300) in calls  # DNS TTL, not capped

    def test_learn_all_cap_bounds_each_rule_ttl(self, proxy, monkeypatch):
        # _learn_all threads the cap through to allow() as min(dns_ttl, cap),
        # per IP/port. The host mapping (set separately by _record_hosts at the
        # DNS TTL) is unaffected.
        monkeypatch.setattr(proxy.time, "time", lambda: 0.0)
        calls = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: calls.append((ip, port, ttl)),
        )
        proxy._learn_all([("1.2.3.4", 300), ("5.6.7.8", 60)], {443, 8443}, cap=5)
        # every rule TTL is capped at 5 (min of dns_ttl and cap).
        assert all(ttl == 5 for _ip, _port, ttl in calls)
        assert ("1.2.3.4", 443, 5) in calls
        assert ("5.6.7.8", 8443, 5) in calls

    def test_learn_all_no_cap_uses_dns_ttl(self, proxy, monkeypatch):
        # Backward compat: cap defaults to None -> the DNS TTL reaches allow()
        # unchanged (the static-spec path).
        monkeypatch.setattr(proxy.time, "time", lambda: 0.0)
        calls = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: calls.append((ip, port, ttl)),
        )
        proxy._learn_all([("1.2.3.4", 300)], {443})
        assert calls == [("1.2.3.4", 443, 300)]


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
        assert proxy._ws_url("ws://x/ws/egress-sidecar") == "ws://x/ws/egress-sidecar"

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

    def test_record_hosts_does_not_touch_consent_rule_expire(self, learned):
        # #2408: _record_hosts refreshes only the host-mapping expire -- never
        # rule_expire (the ACCEPT rule's lifetime, set by allow). So a
        # re-resolve can extend the host mapping (for naming) without extending
        # a consent allow's rule past its verdict.
        import time

        consent_rule_expire = time.time() + 5
        learned._LEARNED["1.2.3.4"] = {
            "expire": time.time() + 5,
            "rule_expire": consent_rule_expire,
            "ports": {None},
            "host": None,
        }
        learned._record_hosts([("1.2.3.4", 300)], "evil.test")
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["rule_expire"] == consent_rule_expire  # untouched
        assert rec["host"] == "evil.test"  # host name refreshed

    def test_record_hosts_extends_ttl_for_pure_host_mapping(self, learned):
        # A pure host-mapping entry (no ACCEPT rule, pre-consent) still gets
        # its host-mapping TTL extended on re-resolve, so the NFQUEUE consumer
        # can name the host for a fresh consent request.
        import time

        base = time.time() + 60
        learned._LEARNED["1.2.3.4"] = {
            "expire": base,
            "ports": set(),
            "host": "a.test",
        }
        learned._record_hosts([("1.2.3.4", 300)], "b.test")
        rec = learned._LEARNED["1.2.3.4"]
        assert rec["expire"] > base  # extended by the longer DNS TTL
        assert rec["host"] == "b.test"
        assert rec["ports"] == set()

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

    async def test_request_sends_frame_and_resolves_on_verdict(self, proxy, tmp_path):
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
            json.dumps({"type": "verdict", "id": frame["id"], "decision": "allow"})
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
            json.dumps({"type": "verdict", "id": frame["id"], "decision": "deny"})
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
            json.dumps({"type": "verdict", "id": frame["id"], "decision": "expired"})
        )
        assert await task == ("deny", "once")

    async def test_request_timeout_fail_close(self, proxy, tmp_path):
        # no verdict within HOLD_TIMEOUT -> "deny"; slot cleaned up
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 0.05)
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

    async def test_dispatch_ignores_non_verdict_and_bad_id(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        await c._dispatch("not-json")
        await c._dispatch(json.dumps({"type": "egress"}))  # wrong type
        await c._dispatch(json.dumps({"type": "verdict", "decision": "allow"}))  # no id
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
        assert proxy._VERDICT_CACHE == {}  # stale verdicts dropped on disconnect

    def test_read_token_missing_file(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "no-such"), 5)
        assert c._read_token() == ""

    def test_read_token_reads_file(self, proxy, tmp_path):
        f = tmp_path / "tok"
        f.write_text("abc-xyz\n")
        c = proxy.SidecarConsentClient("http://h/ev", str(f), 5)
        assert c._read_token() == "abc-xyz"

    # --- _respond_recorded: resolve + record IP->host + respond (no ACCEPT) (#2324) ---

    async def test_respond_recorded_records_host_and_sends(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        recorded = []
        monkeypatch.setattr(
            proxy.rules,
            "_record_hosts",
            lambda recs, host: recorded.append((recs, host)),
        )
        monkeypatch.setattr(
            proxy.resolve, "a_records_with_ttl", lambda wire: [("1.2.3.4", 60)]
        )
        s = MagicMock()
        await proxy._respond_recorded(s, b"resp", ("1.2.3.4", 53), "evil.test")
        assert recorded == [([("1.2.3.4", 60)], "evil.test")]
        s.sendto.assert_called_once_with(b"resp", ("1.2.3.4", 53))

    async def test_respond_recorded_no_records_skips_record(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        recorded = []
        monkeypatch.setattr(
            proxy.rules,
            "_record_hosts",
            lambda recs, host: recorded.append((recs, host)),
        )
        monkeypatch.setattr(
            proxy.resolve, "a_records_with_ttl", lambda wire: []
        )  # upstream NXDOMAIN
        s = MagicMock()
        await proxy._respond_recorded(s, b"resp", ("1.2.3.4", 53), "evil.test")
        assert recorded == []  # nothing to record
        s.sendto.assert_called_once_with(b"resp", ("1.2.3.4", 53))  # still forwarded

    async def test_respond_recorded_swallows_sendto_failure(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        monkeypatch.setattr(proxy.rules, "_record_hosts", lambda recs, host: None)
        monkeypatch.setattr(
            proxy.resolve, "a_records_with_ttl", lambda wire: [("1.2.3.4", 60)]
        )
        s = MagicMock()
        s.sendto.side_effect = OSError("gone")
        await proxy._respond_recorded(  # must not raise
            s, b"resp", ("1.2.3.4", 53), "evil.test"
        )

    # --- _run_nfq_consumer: graceful no-op without netfilterqueue ---

    def test_setup_nfq_consumer_noop_without_netfilterqueue(
        self, proxy, monkeypatch, capsys
    ):
        # netfilterqueue absent (dev venv without the [nfqueue] extra) -> the
        # guarded module-scope import left NetfilterQueue = None -> logs +
        # returns instead of raising (before touching the event loop).
        monkeypatch.setattr(proxy.nfqueue, "NetfilterQueue", None)
        proxy._setup_nfq_consumer(None)
        assert "netfilterqueue not installed" in capsys.readouterr().out


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


def _syn_payload(src: str, sport: int, dst: str, dport: int, seq: int) -> bytes:
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


class TestBumpActivity:
    """Idle-activity flood gate (#2479): the sidecar bumps klangkd's idle timer
    on egress/network activity, throttled to <=1 frame per ACTIVITY_GATE_S."""

    def _client(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        # Deterministic gate on the first bump regardless of machine uptime:
        # time.monotonic() on a fresh CI VM is the boot clock (~a minute),
        # which can be SMALLER than the jittered gate window (30-60s for the
        # default 60s base), suppressing even the first send (#2557 CI flake).
        c._last_activity_send = float("-inf")
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        return c, sent

    async def _drain(self, c):
        # run any scheduled activity-send tasks to completion (deterministic)
        if c._activity_tasks:
            await asyncio.gather(*c._activity_tasks, return_exceptions=True)

    async def test_first_call_sends_activity_frame(self, proxy, tmp_path):
        c, sent = self._client(proxy, tmp_path)
        c.bump_activity()
        await self._drain(c)
        assert [json.loads(f) for f in sent] == [{"type": "activity"}]

    async def test_rapid_calls_within_gate_send_once(self, proxy, tmp_path):
        c, sent = self._client(proxy, tmp_path)
        for _ in range(50):
            c.bump_activity()
        await self._drain(c)
        assert len(sent) == 1  # flood-gated: <=1 per ACTIVITY_GATE_S

    async def test_sends_again_after_gate_window_elapses(
        self, proxy, tmp_path, monkeypatch
    ):
        c, sent = self._client(proxy, tmp_path)
        base = proxy.config.ACTIVITY_GATE_S
        # Pin the jitter to the full base so the window is deterministic here;
        # the jitter range itself is covered by test_gate_jittered_below_base.
        monkeypatch.setattr(proxy.consent, "_jittered_gate", lambda: base)
        c.bump_activity()  # first -> sends (sets _last_activity_send = now)
        c.bump_activity()  # within base -> suppressed
        await self._drain(c)
        assert len(sent) == 1
        # simulate the gate window elapsing (first event after a quiet period)
        c._last_activity_send = time.monotonic() - (base + 1)
        c.bump_activity()  # past base -> sends again
        await self._drain(c)
        assert len(sent) == 2

    async def test_gate_jittered_below_base_lets_early_send_through(
        self, proxy, tmp_path, monkeypatch
    ):
        # #2479: the suppression window is jittered (0.5x..1.0x of the base) so
        # workspaces don't herd onto a shared send cadence. At minimum jitter a
        # send just past HALF the base forwards; at maximum jitter (the full
        # base) that same elapsed time is still suppressed.
        base = proxy.config.ACTIVITY_GATE_S
        elapsed = base * 0.5 + 1  # just past half the base

        c1, sent1 = self._client(proxy, tmp_path)
        monkeypatch.setattr(proxy.consent, "_jittered_gate", lambda: base * 0.5)
        c1._last_activity_send = time.monotonic() - elapsed
        c1.bump_activity()
        await self._drain(c1)
        assert len(sent1) == 1  # jitter collapsed the window to 0.5x -> forwards

        c2, sent2 = self._client(proxy, tmp_path)
        monkeypatch.setattr(proxy.consent, "_jittered_gate", lambda: base)
        c2._last_activity_send = time.monotonic() - elapsed
        c2.bump_activity()
        await self._drain(c2)
        assert sent2 == []  # full-base window still suppresses at half-base elapsed

    async def test_disconnected_is_noop(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c.bump_activity()  # not connected, no _ws -> must not schedule/raise
        await self._drain(c)
        assert c._activity_tasks == set()

    async def test_send_error_is_swallowed(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()

        class _BadWS:
            async def send(self, frame):
                raise OSError("gone")

        c._ws = _BadWS()
        c.bump_activity()  # must not raise
        await self._drain(c)  # the scheduled send raises internally, swallowed


class TestEgressAcct:
    """mangle-OUTPUT egress byte accounting (#2485): the in-kernel rule the
    idle-activity sampler polls, scoped to unmarked (workspace) traffic and
    excluding the WS host so the sidecar's own control plane can't self-sustain
    the idle timer."""

    def test_acct_match_unmarked_with_exclude(self, proxy):
        m = proxy.rules._acct_match("10.0.0.1")
        # mark negation (workspace egress is unmarked) ...
        assert "mark" in m and "--mark" in m and str(proxy.config.MARK) in m
        # ... WS-host dest exclusion ...
        assert "-d" in m and "10.0.0.1" in m
        # ... comment tag (so acct_bytes can find the line) + ACCEPT target.
        assert proxy.rules._ACCT_COMMENT in m
        assert m[-1] == "ACCEPT"

    def test_acct_match_omits_exclude_when_none(self, proxy):
        m = proxy.rules._acct_match(None)
        assert "-d" not in m  # no WS host to exclude
        assert proxy.rules._ACCT_COMMENT in m

    def test_install_acct_idempotent_when_present(self, proxy, monkeypatch):
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=0)  # rule exists -> no -A

        monkeypatch.setattr(proxy.subprocess, "run", fake_run)
        proxy.rules.install_acct("1.2.3.4")
        assert any("-C" in a for a in runs)  # checked
        assert not any("-A" in a for a in runs)  # present -> no append
        # all ops hit the mangle table (never filter, to avoid fighting learned
        # rules' -I OUTPUT 1 inserts).
        assert all("-t" in a and "mangle" in a for a in runs)

    def test_install_acct_appends_when_absent(self, proxy, monkeypatch):
        runs = []

        def fake_run(args, **kw):
            runs.append(args)
            return types.SimpleNamespace(returncode=1)  # absent -> -A

        monkeypatch.setattr(proxy.subprocess, "run", fake_run)
        proxy.rules.install_acct(None)
        assert any("-A" in a and "OUTPUT" in a for a in runs)

    def test_install_acct_swallows_failure(self, proxy, monkeypatch):
        def boom(args, **kw):
            raise OSError("no iptables / no mangle module")

        monkeypatch.setattr(proxy.subprocess, "run", boom)
        proxy.rules.install_acct(None)  # must not raise

    def test_acct_bytes_parses_counter_line(self, proxy, monkeypatch):
        out = (
            "Chain OUTPUT (policy ACCEPT 0 packets, 0 bytes)\n"
            " pkts bytes target prot opt in out source destination\n"
            "   42 1000000 ACCEPT all -- * * 0.0.0.0/0 0.0.0.0/0"
            " mark match ! 0x4b /* klangk-acct */\n"
        )
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0),
        )
        assert proxy.rules.acct_bytes() == 1000000

    def test_acct_bytes_zero_when_rule_absent(self, proxy, monkeypatch):
        out = "Chain OUTPUT (policy ACCEPT 0 packets, 0 bytes)\n"  # no acct line
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0),
        )
        assert proxy.rules.acct_bytes() == 0

    def test_acct_bytes_zero_on_subprocess_failure(self, proxy, monkeypatch):
        def boom(*a, **k):
            raise OSError("gone")

        monkeypatch.setattr(proxy.subprocess, "run", boom)
        assert proxy.rules.acct_bytes() == 0

    def test_acct_bytes_non_integer_column_is_zero(self, proxy, monkeypatch):
        # Safety net if an iptables-nft version emits an unexpected -v layout:
        # the comment-tagged line's bytes column isn't an int -> 0.
        out = (
            "   42 notanumber ACCEPT all -- * * 0.0.0.0/0 0.0.0.0/0 /* klangk-acct */\n"
        )
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0),
        )
        assert proxy.rules.acct_bytes() == 0

    def test_acct_bytes_malformed_line_is_zero(self, proxy, monkeypatch):
        # Comment tag present but too few columns to hold pkts+bytes -> 0.
        out = "klangk-acct\n"
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0),
        )
        assert proxy.rules.acct_bytes() == 0


class TestActivitySampler:
    """Workspace-egress byte sampling -> idle bump (#2485): the sampler polls
    the scoped byte counter once per gate window and, on a positive delta,
    bumps the idle timer via bump_activity (which flood-gates the WS send).
    Per-tick logic is factored into _activity_delta for deterministic tests."""

    def test_delta_positive_bumps(self, proxy):
        bumped, prev = proxy.consent._activity_delta(lambda: 1000, 0)
        assert bumped and prev == 1000

    def test_delta_zero_no_bump(self, proxy):
        bumped, prev = proxy.consent._activity_delta(lambda: 500, 500)
        assert not bumped and prev == 500

    def test_delta_reset_rebaselines_without_bump(self, proxy):
        # counter wrapped below prev (rule re-added / restart): NOT activity.
        bumped, prev = proxy.consent._activity_delta(lambda: 5, 1000)
        assert not bumped and prev == 5

    def test_safe_bytes_read_failure_is_zero(self, proxy):
        def boom():
            raise OSError("iptables gone")

        assert proxy.consent._safe_bytes(boom) == 0

    def test_safe_bytes_non_int_is_zero(self, proxy):
        assert proxy.consent._safe_bytes(lambda: "nonsense") == 0

    async def test_sampler_bumps_only_on_real_traffic(self, proxy):
        # One init read then a sequence: quiet, +100 (bump), flat, +150 (bump),
        # flat, reset (no bump). Once readings exhaust, get_bytes raises
        # StopIteration -> _safe_bytes -> 0 -> flat. Bumps must total 2.
        readings = iter([0, 0, 100, 100, 250, 250, 10])
        bumps = []

        class C:
            def bump_activity(self):
                bumps.append(1)

        task = asyncio.create_task(
            proxy.consent._activity_sampler(C(), lambda: next(readings), 0.0)
        )
        await asyncio.sleep(0.1)  # plenty of 0-interval ticks to drain readings
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(bumps) == 2  # only the two positive deltas bumped


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
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy.state._SESSION_HOST_DENIES.clear()
        proxy._BG_TASKS.clear()

    async def _decide(self, proxy, pkt, client):
        """Run the non-blocking ``_cb`` to completion + await its verdict task(s)."""
        proxy._cb(pkt, client)
        if proxy._BG_TASKS:
            await asyncio.gather(*proxy._BG_TASKS)

    async def test_allow_verdict_accepts_and_learns_ip(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "tilrestart"))
        learned = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: learned.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        client.request.assert_awaited_once_with("1.2.3.4", 443)  # host=IP (no record)
        assert learned == [
            ("1.2.3.4", None, proxy._DURATION_FOREVER)
        ]  # learned all-ports

    async def test_allow_names_host_from_ip_map(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "tilrestart"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts([("1.2.3.4", 60)], "evil.test")  # DNS resolved this IP
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        client.request.assert_awaited_once_with("evil.test", 443)  # host, not IP

    async def test_forever_allow_adds_host_to_session_allowlist(
        self, proxy, monkeypatch
    ):
        # A `forever` allow approves the host (not just the resolved IP): the
        # sidecar adds it to _SESSION_HOST_ALLOWS so ports_for treats it as
        # allow-listed for the session -- a later CDN-rotated IP re-resolves
        # and is allowed without re-prompting (#2372, #2434).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "forever"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        specs = [(h, p, m) for (h, p, m, _exp) in proxy.state._SESSION_HOST_ALLOWS]
        assert ("example.com", 443, proxy._EXACT) in specs
        # ports_for now treats the host as allow-listed (EXACT -- apex only).
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        assert proxy.ports_for("example.com") == {443}
        assert proxy.ports_for("api.example.com") == set()  # exact -> no subdomain

    async def test_cb_auto_allows_syn_to_session_allowed_host_ip(
        self, proxy, monkeypatch
    ):
        # A SYN to an IP whose host was allowed forever is auto-allowed at the
        # NFQUEUE gate (no consent prompt), even though no ACCEPT rule covered
        # it -- covers a CDN-rotated / cached IP that no fresh DNS resolution
        # re-learned (#2372). allow() runs off the loop in the executor; the
        # verdict is cached for retransmits.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "forever"))
        allowed = []
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: allowed.append(a))
        self._bind(proxy, monkeypatch, client)
        proxy._add_session_host("example.com", 443, proxy._DURATION_FOREVER)
        proxy._record_hosts([("2.2.2.2", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("2.2.2.2", 443))
        proxy._cb(pkt, client)  # sync: auto-allows inline, no task
        await asyncio.sleep(0.1)  # let the executor run allow()
        assert pkt.verdict == "accept"
        client.request.assert_not_awaited()  # no consent prompt
        assert not proxy._BG_TASKS  # no verdict task spawned
        # allow() called port-scoped, in the executor, with the allow's remaining
        # TTL (~forever for a fresh forever entry).
        assert len(allowed) == 1
        assert allowed[0][0] == "2.2.2.2"
        assert allowed[0][1] == 443
        assert allowed[0][2] >= proxy._DURATION_FOREVER - 5
        # Verdict cached so a retransmit reuses it without re-prompting.
        assert any(v[0] == "allow" for v in proxy._VERDICT_CACHE.values())

    async def test_cb_does_not_auto_allow_wrong_port(self, proxy, monkeypatch):
        # The gate is host+port scoped end-to-end: a SYN to a forever host on a
        # DIFFERENT port (the allow was :443; this SYN is :80) is NOT
        # auto-allowed -- it reaches the consent prompt (the security property,
        # exercised through _cb, not just the predicate).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "once"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._add_session_host("example.com", 443, proxy._DURATION_FOREVER)
        proxy._record_hosts([("2.2.2.2", 60)], "example.com")
        proxy._cb(_FakePkt(_ip_payload("2.2.2.2", 80)), client)
        await asyncio.gather(*proxy._BG_TASKS)  # the prompt task ran
        client.request.assert_awaited_once_with("example.com", 80)

    async def test_once_allow_does_not_add_session_allowlist(self, proxy, monkeypatch):
        # Only `once` skips the in-session host allow-list: it is per-connection
        # (a reconnect re-prompts), so a CDN-rotated IP of a once-allowed host
        # must NOT be auto-allowed. Timed/forever/tilrestart all host-scope
        # (#2434); `once` alone stays an in-memory IP learn of the resolved IP.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "once"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert proxy.state._SESSION_HOST_ALLOWS == []

    async def test_timed_allow_is_host_scoped(self, proxy, monkeypatch):
        # #2434 regression: a TIMED allow (not just forever) host-scopes. The
        # verdict adds the host to _SESSION_HOST_ALLOWS, so a CDN-rotated IP of
        # the host -- resolved AFTER the allow, with no ACCEPT rule of its own
        # -- is auto-allowed at the NFQUEUE gate WITHOUT re-prompting. Pre-fix
        # this SYN re-entered NFQUEUE, and a hold timeout there fail-closed to a
        # deny REJECT (the ALLOW-REFUSED mismatch: an in-effect allow that
        # refuses).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "5m"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        # Decide on IP_A (the resolved IP at decision time).
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        await self._decide(proxy, _FakePkt(_ip_payload("1.2.3.4", 443)), client)
        specs = [(h, p, m) for (h, p, m, _exp) in proxy.state._SESSION_HOST_ALLOWS]
        assert ("example.com", 443, proxy._EXACT) in specs  # host-scoped
        # A CDN-rotated IP_B (no ACCEPT, never consented) is auto-allowed at the
        # NFQUEUE gate -- no consent prompt, no verdict task spawned.
        proxy._record_hosts([("9.9.9.9", 60)], "example.com")
        client2 = MagicMock()
        client2.connected = True
        client2.request = AsyncMock(return_value=("deny", "5s"))
        pkt2 = _FakePkt(_ip_payload("9.9.9.9", 443))
        proxy._cb(pkt2, client2)
        await asyncio.sleep(0.05)
        assert pkt2.verdict == "accept"  # auto-allowed, not denied
        client2.request.assert_not_awaited()  # no re-prompt
        assert not proxy._BG_TASKS

    def test_timed_session_allow_expires(self, proxy, monkeypatch):
        # #2434: a timed session-allow expires (lazy prune), so the host
        # re-enters consent-gating once its window elapses -- it does not leak
        # like a forever entry.
        clock = [1000.0]
        monkeypatch.setattr(proxy.time, "time", lambda: clock[0])
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy._add_session_host("example.com", 443, 300)  # expire 1300
        assert proxy._session_host_allows_ttl("example.com", 443) == 300.0
        clock[0] = 1400.0  # past the 300s window
        assert proxy._session_host_allows_ttl("example.com", 443) is None
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        assert proxy.ports_for("example.com") == set()  # pruned -> denied
        assert proxy.state._SESSION_HOST_ALLOWS == []

    async def test_timed_deny_is_host_scoped(self, proxy, monkeypatch):
        # #2446 regression: a TIMED deny host-scopes (mirror of the allow side).
        # The verdict adds the host to _SESSION_HOST_DENIES, so a CDN-rotated IP
        # of the host -- resolved AFTER the deny, with no per-IP _REJECTED rule
        # of its own -- is auto-denied at the NFQUEUE gate WITHOUT re-prompting
        # (the CARRYOVER-SURPRISE fix). Pre-fix this SYN re-entered NFQUEUE and
        # re-prompted for a host the user had already denied.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "5m"))
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        # Decide (deny) on IP_A (the resolved IP at decision time).
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        await self._decide(proxy, _FakePkt(_ip_payload("1.2.3.4", 443)), client)
        specs = [(h, p, m) for (h, p, m, _exp) in proxy.state._SESSION_HOST_DENIES]
        assert ("example.com", 443, proxy._EXACT) in specs  # host-scoped
        # A CDN-rotated IP_B (no REJECT, never consented) is auto-denied at the
        # NFQUEUE gate -- no consent prompt, no verdict task spawned.
        proxy._record_hosts([("9.9.9.9", 60)], "example.com")
        client2 = MagicMock()
        client2.connected = True
        client2.request = AsyncMock(return_value=("allow", "5s"))
        pkt2 = _FakePkt(_ip_payload("9.9.9.9", 443))
        proxy._cb(pkt2, client2)
        await asyncio.sleep(0.05)
        assert pkt2.verdict == "drop"  # auto-denied, not allowed
        client2.request.assert_not_awaited()  # no re-prompt (the fix)
        assert not proxy._BG_TASKS

    async def test_cb_auto_denies_syn_to_session_denied_host_ip(
        self, proxy, monkeypatch
    ):
        # A SYN to an IP whose host was denied (timed) is auto-denied at the
        # NFQUEUE gate (no consent prompt): a REJECT for the deny's remaining
        # window runs off-loop, the SYN is dropped, and the verdict is cached
        # for retransmits (#2446). reject() runs in the executor.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "5s"))
        rejected = []
        monkeypatch.setattr(
            proxy.rules,
            "reject",
            lambda ip, port, ttl: rejected.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        proxy._add_session_deny("example.com", 443, proxy._DURATION_FOREVER)
        proxy._record_hosts([("2.2.2.2", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("2.2.2.2", 443))
        proxy._cb(pkt, client)  # sync: auto-denies inline, no task
        await asyncio.sleep(0.1)  # let the executor run reject()
        assert pkt.verdict == "drop"
        client.request.assert_not_awaited()  # no consent prompt
        assert not proxy._BG_TASKS  # no verdict task spawned
        # reject() called port-scoped, in the executor, with the deny's
        # remaining window (~forever for a fresh forever entry).
        assert len(rejected) == 1
        assert rejected[0][0] == "2.2.2.2"
        assert rejected[0][1] == 443
        assert rejected[0][2] >= proxy._DURATION_FOREVER - 5
        # Verdict cached so a retransmit reuses it.
        assert any(v[0] == "deny" for v in proxy._VERDICT_CACHE.values())

    async def test_cb_does_not_auto_deny_wrong_port(self, proxy, monkeypatch):
        # The deny gate is host+port scoped end-to-end: a SYN to a denied host
        # on a DIFFERENT port (deny was :443; this SYN is :80) is NOT
        # auto-denied -- it reaches the consent prompt (the security property,
        # exercised through _cb, not just the predicate).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._add_session_deny("example.com", 443, proxy._DURATION_FOREVER)
        proxy._record_hosts([("2.2.2.2", 60)], "example.com")
        proxy._cb(_FakePkt(_ip_payload("2.2.2.2", 80)), client)
        await asyncio.gather(*proxy._BG_TASKS)  # the prompt task ran
        client.request.assert_awaited_once_with("example.com", 80)

    async def test_once_deny_does_not_add_session_denylist(self, proxy, monkeypatch):
        # `once` deny is per-connection (a reconnect re-prompts), so it adds
        # nothing to _SESSION_HOST_DENIES (mirror of once-allow). Timed/forever
        # host-scope (#2446); `once` alone stays a per-IP REJECT.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._record_hosts([("1.2.3.4", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert proxy.state._SESSION_HOST_DENIES == []

    def test_timed_session_deny_expires(self, proxy, monkeypatch):
        # #2446: a timed session-deny expires (lazy prune), so the host
        # re-enters consent-gating once its window elapses -- it does not leak
        # like a forever entry.
        clock = [1000.0]
        monkeypatch.setattr(proxy.time, "time", lambda: clock[0])
        proxy.state._SESSION_HOST_DENIES.clear()
        proxy._add_session_deny("example.com", 443, 300)  # expire 1300
        assert proxy._session_host_denies_ttl("example.com", 443) == 300.0
        clock[0] = 1400.0  # past the 300s window
        assert proxy._session_host_denies_ttl("example.com", 443) is None
        assert proxy.state._SESSION_HOST_DENIES == []

    async def test_allow_overrides_in_effect_deny_at_gate(self, proxy, monkeypatch):
        # Acceptance (#2446): an in-effect allow overrides an in-effect deny
        # at the gate -- _cb consults _session_host_allows_ttl BEFORE
        # _session_host_denies_ttl, so a SYN to a host that is both allowed and
        # denied is ACCEPTED (the allow wins), not auto-denied.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
        self._bind(proxy, monkeypatch, client)
        proxy._add_session_deny("example.com", 443, proxy._DURATION_FOREVER)
        proxy._add_session_host("example.com", 443, proxy._DURATION_FOREVER)
        proxy._record_hosts([("2.2.2.2", 60)], "example.com")
        pkt = _FakePkt(_ip_payload("2.2.2.2", 443))
        proxy._cb(pkt, client)
        await asyncio.sleep(0.05)
        assert pkt.verdict == "accept"  # allow wins over the in-effect deny
        client.request.assert_not_awaited()
        assert not proxy._BG_TASKS

    async def test_deny_verdict_drops_and_rejects(self, proxy, monkeypatch):
        # deny -> drop the SYN + install a REJECT (tcp-reset) so the retransmit
        # gets RST'd (ECONNREFUSED), not a ~127s tcp_syn_retries wait. A `once`
        # deny is connection-scoped (#2463): the REJECT carries the denying
        # connection's source port (here 50000) so a NEW connection to the same
        # host:port re-prompts instead of being rejected above NFQUEUE.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        rejected = []
        monkeypatch.setattr(
            proxy.rules,
            "reject",
            lambda ip, port, ttl, sport=0: rejected.append((ip, port, ttl, sport)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        assert rejected == [
            ("1.2.3.4", 443, proxy.CONSENT_REJECT_TTL, 50000)
        ]  # eager deny, connection-scoped to this connection's source port

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

    async def test_ws_down_fails_fast_not_hang(self, proxy, monkeypatch):
        # WS down -> consent unavailable -> fail the off-list SYN FAST
        # (ECONNREFUSED via a forged RST + a short REJECT backstop), not a
        # bare drop the kernel retransmits for ~127s (#2308/#2413: no consent
        # -> clean prompt denial, not a dangling connection). On-list egress
        # is unaffected (learned ACCEPT rules sit above NFQUEUE).
        client = MagicMock()
        client.connected = False
        client.request = AsyncMock()
        rejected = []
        monkeypatch.setattr(
            proxy.rules,
            "reject",
            lambda ip, port, ttl: rejected.append((ip, port, ttl)),
        )
        rst = []
        monkeypatch.setattr(
            proxy.packets, "_send_rst", lambda payload: rst.append(payload)
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        proxy._cb(pkt, client)  # _cb fails fast inline (no _BG_TASK)
        # poll (not a fixed sleep) for the off-loop REJECT install to land --
        # robust under CI load; the RST is forged inline so it's already in.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        while not rejected and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert pkt.verdict == "drop"
        client.request.assert_not_awaited()
        assert rst  # eager-deny RST forged so connect() gets ECONNREFUSED
        assert rejected == [
            ("1.2.3.4", 443, proxy.CONSENT_REJECT_TTL)
        ]  # retransmit backstop (above NFQUEUE)

    async def test_unparseable_dest_drops(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "tilrestart"))
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
        client.request = AsyncMock(return_value=("allow", "tilrestart"))
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
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
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: None)
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
        monkeypatch.setattr(proxy.rules, "allow", lambda *a: learned.append(a))
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        assert learned == []  # no learn for `once`

    async def test_allow_timed_duration_learns_for_ttl(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("allow", "1h"))
        learned = []
        monkeypatch.setattr(
            proxy.rules,
            "allow",
            lambda ip, port, ttl, floor=True: learned.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "accept"
        assert learned == [("1.2.3.4", None, 3600)]  # 1h -> 3600s

    async def test_deny_timed_duration_rejects_for_ttl(self, proxy, monkeypatch):
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "15m"))
        rejected = []
        monkeypatch.setattr(
            proxy.rules,
            "reject",
            lambda ip, port, ttl: rejected.append((ip, port, ttl)),
        )
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_ip_payload("1.2.3.4", 443))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        assert rejected == [("1.2.3.4", 443, 900)]  # 15m -> 900s

    async def test_deny_once_uses_short_fail_close_ttl(self, proxy, monkeypatch):
        # `once` deny -> the short CONSENT_REJECT_TTL (fail-close this conn).
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        rejected = []
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: rejected.append(a[2]))
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
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        sock = MagicMock()
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", sock)
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"
        sock.sendto.assert_called_once_with(
            proxy.build_rst_packet("1.2.3.4", 443, "10.0.0.5", 50000, 0x1111),
            ("10.0.0.5", 0),
        )

    async def test_deny_no_rst_socket_falls_back_to_reject(self, proxy, monkeypatch):
        # No RST socket (NET_RAW absent / consent off) -> only the REJECT rule;
        # no exception, still drops.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
        self._bind(proxy, monkeypatch, client)
        pkt = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111))
        await self._decide(proxy, pkt, client)
        assert pkt.verdict == "drop"

    def test_send_rst_paths_and_debug(self, proxy, monkeypatch, capsys):
        # Direct coverage of _send_rst's four branches + the opt-in RST debug
        # log (#2464): no-socket, success, sendto-failure, unparseable, and the
        # KLANGKNETWORK_EGRESS_DEBUG_RST flag on/off. The flag defaults off so
        # the debug print is the only branch the indirect _decide tests miss.
        syn = _syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111)

        # No socket -> no-op; with the debug flag on, note the REJECT-only fall
        # back (covers _rst_debug's print branch).
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
        monkeypatch.setattr(proxy.config, "_RST_DEBUG", True)
        proxy._send_rst(syn)
        assert "no raw socket" in capsys.readouterr().out

        # Socket set, sendto succeeds -> forged + a "sent" debug line.
        sock = MagicMock()
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", sock)
        proxy._send_rst(syn)
        sock.sendto.assert_called_once()
        assert "rst-forge: sent" in capsys.readouterr().out

        # sendto raises -> swallowed + a "failed" debug line.
        sock.sendto.side_effect = OSError("boom")
        proxy._send_rst(syn)
        assert "sendto failed" in capsys.readouterr().out

        # Unparseable tuple -> no sendto + an "unparseable" debug line.
        sock.sendto.reset_mock()
        proxy._send_rst(b"\x00\x00\x00\x00")  # too short / not IPv4
        assert "unparseable" in capsys.readouterr().out
        assert sock.sendto.call_count == 0  # nothing forged for a bad tuple

        # Flag off (the default) -> no debug output at all (covers the False
        # branch of _rst_debug's `if _RST_DEBUG`).
        monkeypatch.setattr(proxy.config, "_RST_DEBUG", False)
        proxy._send_rst(syn)
        assert capsys.readouterr().out == ""

    async def test_cache_hit_deny_forges_rst(self, proxy, monkeypatch):
        # A retried connect() to a denied flow reuses the cached verdict and
        # also forges the RST (fails fast), not just drops.
        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(return_value=("deny", "once"))
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        sock = MagicMock()
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", sock)
        self._bind(proxy, monkeypatch, client)
        pkt1 = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 1))
        await self._decide(proxy, pkt1, client)  # populates the cache
        assert sock.sendto.call_count == 1  # original deny forged an RST
        pkt2 = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 2))
        proxy._cb(pkt2, client)  # cache hit -> inline forge + drop
        assert pkt2.verdict == "drop"
        client.request.assert_awaited_once()  # NOT re-requested
        assert sock.sendto.call_count == 2  # the retry also got an RST

    async def test_retransmit_during_hold_does_not_re_prompt(self, proxy, monkeypatch):
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
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
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
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
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

    async def test_deny_once_re_prompts_new_connection_same_host(
        self, proxy, monkeypatch
    ):
        # #2463: a `once` deny governs only the deciding connection. A NEW
        # connection (new source port) to the same (dst, port) must re-prompt --
        # the connection-scoped REJECT (keyed on the source port) does not catch
        # it, the verdict cache misses (distinct flow), and `once` adds no
        # _SESSION_HOST_DENIES entry. This is the deny-side mirror of
        # test_distinct_source_port_re_prompts_after_allow_once, and the
        # unit-level grounding of the #2463 fix.
        requests = []

        async def fast_request(host, port):
            requests.append((host, port))
            return ("deny", "once")

        client = MagicMock()
        client.connected = True
        client.request = AsyncMock(side_effect=fast_request)
        monkeypatch.setattr(proxy.rules, "reject", lambda *a: None)
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
        self._bind(proxy, monkeypatch, client)
        # Connection A (sport 50000) -> deny/once -> fail-close + connection-
        # scoped REJECT for sport 50000 only.
        proxy._cb(
            _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 1)),
            client,
        )
        await asyncio.gather(*proxy._BG_TASKS)
        assert proxy.state._SESSION_HOST_DENIES == []  # `once` adds no host-deny
        # Connection B (sport 50001, same dst) -> DISTINCT flow -> the
        # connection-scoped REJECT (sport 50000) does not match, the verdict
        # cache misses, and there is no session-deny gate -> re-prompts.
        proxy._cb(
            _FakePkt(_syn_payload("10.0.0.5", 50001, "1.2.3.4", 443, 1)),
            client,
        )
        await asyncio.gather(*proxy._BG_TASKS)
        assert len(requests) == 2, (
            "a new connection (new source port) must re-prompt after a "
            "deny-once, not be silently rejected for the fail-close window"
        )

    async def test_deny_once_reject_is_connection_scoped(self, proxy, monkeypatch):
        # #2463: the fail-close REJECT installed by a `once` deny carries the
        # denying connection's source port (--sport), so a real iptables rule
        # would catch only retransmits of THAT connection, leaving a NEW
        # connection (different sport) to re-enter NFQUEUE and re-prompt. A
        # timed/forever deny, by contrast, stays destination-scoped (sport 0)
        # -- its over-deny is intended.
        for duration, expect_sport in [("once", 50000), ("15m", 0)]:
            client = MagicMock()
            client.connected = True
            client.request = AsyncMock(return_value=("deny", duration))
            rejected = []
            monkeypatch.setattr(
                proxy.rules,
                "reject",
                lambda ip, port, ttl, sport=0: rejected.append(sport),
            )
            self._bind(proxy, monkeypatch, client)
            pkt = _FakePkt(_syn_payload("10.0.0.5", 50000, "1.2.3.4", 443, 0x1111))
            await self._decide(proxy, pkt, client)
            assert rejected == [expect_sport], duration


def test_duration_ttl_mapping(proxy):
    assert proxy._duration_ttl("once") is None
    assert proxy._duration_ttl("5m") == 300
    assert proxy._duration_ttl("1h") == 3600
    assert proxy._duration_ttl("1w") == 604800
    assert proxy._duration_ttl("tilrestart") == proxy._DURATION_FOREVER
    assert proxy._duration_ttl("forever") == proxy._DURATION_FOREVER
    assert proxy._duration_ttl("bogus") is None  # unknown -> None


class TestHandlePacket:
    """The per-packet DNS routing (#2311 half B, #2324): classify -> forward /
    resolve+record / NXDOMAIN. A statically-allow-listed name forwards + learns;
    a denied name in interactive mode resolves + records IP->host (its SYN is
    consent-gated at NFQUEUE); static mode (no client) -> NXDOMAIN."""

    async def test_allowed_name_forwards_and_learns(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "allowed.test")
        monkeypatch.setattr(proxy.allowlist, "SPECS", [("allowed.test", None, False)])
        fwd = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        fwd.assert_awaited_once()  # forwarded + learned, not denied
        s.sendto.assert_not_called()  # no NXDOMAIN

    async def test_denied_no_client_sends_nxdomain(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.resolve, "nxdomain_for", lambda d: b"NXD")
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))

    async def test_denied_with_client_resolves_and_records(self, proxy, monkeypatch):
        # Interactive: a denied name resolves + records IP->host (NO ACCEPT) so
        # its SYN is consent-gated at NFQUEUE -- it is NOT held at the DNS query.
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        rec = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_record", rec)
        nxd = MagicMock()
        monkeypatch.setattr(proxy.resolve, "_send_nxdomain", nxd)
        client = MagicMock()
        client.connected = True
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), client)
        rec.assert_awaited_once_with(s, b"q", ("1.2.3.4", 53), "evil.test")
        nxd.assert_not_called()  # not NXDOMAIN -- resolves for the SYN gate

    async def test_malformed_query_is_dropped(self, proxy, monkeypatch):
        def _boom(wire):
            raise RuntimeError("bad wire")

        monkeypatch.setattr(proxy.resolve, "query_name", _boom)
        fwd = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_not_called()  # dropped, no response
        fwd.assert_not_awaited()

    async def test_rejected_name_sends_nxdomain_static(self, proxy, monkeypatch):
        # #2367: a rejected name is NXDOMAIN'd unconditionally (before the
        # allow-list + consent), in BOTH modes. Static mode (no client).
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(
            proxy.allowlist, "REJECT_SPECS", [("evil.test", None, proxy._EXACT)]
        )
        monkeypatch.setattr(proxy.resolve, "nxdomain_for", lambda d: b"NXD")
        fwd = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))
        fwd.assert_not_awaited()

    async def test_rejected_name_nxdomain_even_with_client(self, proxy, monkeypatch):
        # Reject takes precedence over consent: even in interactive mode a
        # rejected name NXDOMAINs (no resolve+record, no prompt).
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(
            proxy.allowlist, "REJECT_SPECS", [("evil.test", None, proxy._EXACT)]
        )
        monkeypatch.setattr(proxy.resolve, "nxdomain_for", lambda d: b"NXD")
        rec = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_record", rec)
        s = MagicMock()
        client = MagicMock()
        client.connected = True
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), client)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))
        rec.assert_not_awaited()

    async def test_reject_only_static_denies_all(self, proxy, monkeypatch):
        # No allow-list but a reject-list, static (no client): with the
        # default-allow mode removed (#2367 review), a reject-only static
        # workspace is DENY-ALL -- a non-rejected name NXDOMAINs (fail-closed,
        # not fail-open). The reject list is a useful blocklist only in
        # interactive mode (or alongside an allow-list); static mode is being
        # phased out. ports_for on empty SPECS -> set() -> _decision denies.
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "benign.test")
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(
            proxy.allowlist, "REJECT_SPECS", [("evil.test", None, proxy._EXACT)]
        )
        monkeypatch.setattr(proxy.resolve, "nxdomain_for", lambda d: b"NXD")
        fwd = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))
        fwd.assert_not_awaited()

    async def test_rejected_takes_precedence_over_allowed(self, proxy, monkeypatch):
        # A name in BOTH allowed + rejected is rejected (deny wins).
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "dual.test")
        monkeypatch.setattr(
            proxy.allowlist, "SPECS", [("dual.test", None, proxy._EXACT)]
        )
        monkeypatch.setattr(
            proxy.allowlist, "REJECT_SPECS", [("dual.test", None, proxy._EXACT)]
        )
        monkeypatch.setattr(proxy.resolve, "nxdomain_for", lambda d: b"NXD")
        fwd = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_forward_and_learn", fwd)
        s = MagicMock()
        await proxy._handle_packet(s, b"q", ("1.2.3.4", 53), None)
        s.sendto.assert_called_once_with(b"NXD", ("1.2.3.4", 53))
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
            lambda *a, **k: runs.append(a[0]) or types.SimpleNamespace(returncode=0),
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
            lambda *a, **k: runs.append(a[0]) or types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(proxy.time, "time", lambda: 0.0)
        proxy.allow("203.0.113.9", None, 3600)  # direct-IP allow, host stays None
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
            lambda *a, **k: runs.append(a[0]) or types.SimpleNamespace(returncode=0),
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
            lambda *a, **k: runs.append(a[0]) or types.SimpleNamespace(returncode=0),
        )
        proxy._REJECTED[("9.9.9.9", 80)] = 9999.0
        proxy.drop_for_host("9.9.9.9", "denied")
        assert ("9.9.9.9", 80) not in proxy._REJECTED

    def test_no_match_is_noop(self, proxy, monkeypatch):
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        ran = []
        monkeypatch.setattr(proxy.subprocess, "run", lambda *a, **k: ran.append(a[0]))
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
        monkeypatch.setattr(proxy.subprocess, "run", lambda *a, **k: ran.append(a[0]))
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9.0,
            "ports": {443},
            "host": "h.test",
        }
        proxy.drop_for_host("h.test", "bogus")
        assert "1.2.3.4" in proxy._LEARNED
        assert ran == []

    async def test_dispatch_drop_rule_acks_ok(self, proxy, tmp_path, monkeypatch):
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

    async def test_dispatch_drop_rule_bad_payload_acks_false(self, proxy, tmp_path):
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "t"), 5)
        c._connected.set()
        sent = []

        class _FakeWS:
            async def send(self, frame):
                sent.append(frame)

        c._ws = _FakeWS()
        # missing host + bogus decision -> drop skipped, ack ok=False
        await c._dispatch(
            json.dumps({"type": "drop_rule", "id": "ack-2", "decision": "bogus"})
        )
        ack = json.loads(sent[0])
        assert ack["ok"] is False

    def test_drop_for_host_returns_candidate_ips(self, proxy, monkeypatch):
        # #2370: drop_for_host returns the candidate IP set so the loop-side
        # _clear_verdict_cache can clear _VERDICT_CACHE for those IPs.
        proxy._LEARNED.clear()
        proxy._REJECTED.clear()
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=0),
        )
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9999.0,
            "ports": {443},
            "host": "evil.test",
        }
        ips = proxy.drop_for_host("evil.test", "allowed")
        # resolved IP + the host string itself (direct-IP-allow candidate)
        assert ips == {"1.2.3.4", "evil.test"}

    def test_drop_session_denies_removes_host_entries(self, proxy):
        # A denied revoke drops the host's _SESSION_HOST_DENIES coverage
        # (#2446); other hosts are left intact. Mirror of the allow revoke
        # (test_drop_session_hosts_removes_host_entries).
        proxy.state._SESSION_HOST_DENIES.clear()
        proxy.state._SESSION_HOST_DENIES[:] = [
            ("evil.test", 443, proxy._EXACT, float("inf")),
            ("other.test", 80, proxy._EXACT, float("inf")),
        ]
        proxy._drop_session_denies("Evil.TEST")  # case-insensitive
        assert proxy.state._SESSION_HOST_DENIES == [
            ("other.test", 80, proxy._EXACT, float("inf"))
        ]

    def test_drop_session_hosts_removes_host_entries(self, proxy):
        # An allowed revoke drops the host's _SESSION_HOST_ALLOWS coverage
        # (in-session allow from #2372/#2434); other hosts are left intact.
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy.state._SESSION_HOST_ALLOWS[:] = [
            ("evil.test", 443, proxy._EXACT, float("inf")),
            ("other.test", 80, proxy._EXACT, float("inf")),
        ]
        proxy._drop_session_hosts("Evil.TEST")  # case-insensitive
        assert proxy.state._SESSION_HOST_ALLOWS == [
            ("other.test", 80, proxy._EXACT, float("inf"))
        ]

    def test_clear_verdict_cache_drops_host_entries(self, proxy):
        # Cached verdicts keyed by (src_ip, src_port, dst, port); dst is key[2].
        # Only the revoked host's IPs are cleared; an unrelated IP is kept.
        proxy._VERDICT_CACHE.clear()
        proxy._VERDICT_CACHE[("10.0.0.1", 1, "1.2.3.4", 443)] = ("allow", 9e9)
        proxy._VERDICT_CACHE[("10.0.0.1", 2, "5.6.7.8", 443)] = ("deny", 9e9)
        proxy._clear_verdict_cache({"1.2.3.4", "evil.test"})
        assert ("10.0.0.1", 1, "1.2.3.4", 443) not in proxy._VERDICT_CACHE
        assert ("10.0.0.1", 2, "5.6.7.8", 443) in proxy._VERDICT_CACHE

    def test_clear_verdict_cache_empty_ips_is_noop(self, proxy):
        # No resolved IPs (host never seen / swept) -> nothing to clear.
        proxy._VERDICT_CACHE.clear()
        proxy._VERDICT_CACHE[("10.0.0.1", 1, "9.9.9.9", 443)] = ("allow", 9e9)
        proxy._clear_verdict_cache(set())
        assert proxy._VERDICT_CACHE == {("10.0.0.1", 1, "9.9.9.9", 443): ("allow", 9e9)}

    async def test_dispatch_drop_rule_clears_session_state(
        self, proxy, tmp_path, monkeypatch
    ):
        # End-to-end (#2370): an allow revoke's drop_rule clears the in-session
        # _SESSION_HOST_ALLOWS (BEFORE the iptables fork, so a racing SYN can't
        # re-install a fresh ACCEPT during the window) + _VERDICT_CACHE (after),
        # else the next DNS resolution re-learns the host.
        proxy._LEARNED.clear()
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy._VERDICT_CACHE.clear()
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=0),
        )
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9e9,
            "ports": {443},
            "host": "h.test",
        }
        proxy.state._SESSION_HOST_ALLOWS.append(
            ("h.test", 443, proxy._EXACT, float("inf"))
        )
        proxy._VERDICT_CACHE[("10.0.0.1", 1, "1.2.3.4", 443)] = ("allow", 9e9)
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
        assert proxy.state._SESSION_HOST_ALLOWS == []
        assert proxy._VERDICT_CACHE == {}
        assert json.loads(sent[0])["ok"] is True

    async def test_dispatch_drop_rule_denied_clears_session_deny(
        self, proxy, tmp_path, monkeypatch
    ):
        # #2446: a DENY revoke's drop_rule clears the in-session
        # _SESSION_HOST_DENIES BEFORE the iptables fork, so a SYN racing that
        # fork can't keep auto-denying the host the operator just un-denied.
        # Mirror of test_dispatch_drop_rule_clears_session_state (allow revoke).
        proxy._LEARNED.clear()
        proxy.state._SESSION_HOST_DENIES.clear()
        proxy._VERDICT_CACHE.clear()
        monkeypatch.setattr(
            proxy.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=0),
        )
        proxy._LEARNED["1.2.3.4"] = {
            "expire": 9e9,
            "ports": set(),
            "host": "h.test",
        }
        proxy.state._SESSION_HOST_DENIES.append(
            ("h.test", 443, proxy._EXACT, float("inf"))
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
                    "id": "ack-2",
                    "host": "h.test",
                    "decision": "denied",
                }
            )
        )
        assert proxy.state._SESSION_HOST_DENIES == []
        assert json.loads(sent[0])["ok"] is True


class TestSigtermShutdown:
    """SIGTERM teardown (#2400): the sidecar is PID 1 (entrypoint.sh execs
    python), and the kernel ignores default terminate dispositions for a
    PID-namespace init (``SIGNAL_UNKILLABLE``: a fatal signal with no explicit
    handler is skipped for init), so podman's ``stop`` SIGTERM was no-op'd and
    every removal fell back to SIGKILL after the 5s timeout. ``proxy.py`` now
    installs an explicit SIGTERM handler that cancels the main task -> clean
    teardown (close the WS, unbind NFQUEUE, close the DNS socket) -> prompt exit."""

    async def test_setup_nfq_consumer_returns_nfq_for_unbind(self, proxy, monkeypatch):
        # _setup_nfq_consumer returns the bound NFQUEUE so _shutdown can unbind
        # it on SIGTERM (clean PID-1 teardown, #2400). The import is a guarded
        # module-scope one; patch the resolved symbol directly.
        nfq = MagicMock()
        nfq.get_fd = MagicMock(return_value=7)
        monkeypatch.setattr(
            proxy.nfqueue, "NetfilterQueue", MagicMock(return_value=nfq)
        )
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_reader", MagicMock())
        assert proxy._setup_nfq_consumer(None) is nfq
        nfq.bind.assert_called_once()

    async def test_setup_nfq_consumer_bind_failure_returns_none(
        self, proxy, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            proxy.nfqueue,
            "NetfilterQueue",
            MagicMock(side_effect=RuntimeError("bind boom")),
        )
        assert proxy._setup_nfq_consumer(None) is None
        assert "nfqueue consumer failed" in capsys.readouterr().out

    async def test_shutdown_closes_socket_stops_client_unbinds_nfq(self, proxy):
        # _shutdown best-effort-closes every resource (#2400); a failure in one
        # step must not skip the rest.
        sock = MagicMock()
        nfq = MagicMock()
        nfq.get_fd = MagicMock(return_value=9)
        client = MagicMock()
        client.stop = AsyncMock()
        sweep = asyncio.get_running_loop().create_task(asyncio.sleep(100))
        await proxy._shutdown(client, nfq, sock, sweep)
        client.stop.assert_awaited_once()
        nfq.unbind.assert_called_once()
        sock.close.assert_called_once()
        assert sweep.cancelled()

    async def test_sigterm_handler_cancels_main_and_runs_teardown(
        self, proxy, monkeypatch
    ):
        # The sidecar is PID 1; the kernel ignores a default-disposition
        # SIGTERM, so an explicit handler is what lets podman's `stop` prompt
        # the exit (#2400). Registering the handler + cancelling the main task
        # must unwind _async_main and run _shutdown (close the DNS socket).
        monkeypatch.setattr(proxy.config, "CONSENT_URL", "")  # no WS client / NFQUEUE
        monkeypatch.setattr(proxy.rules, "check_mark", lambda: None)
        fake_sock = MagicMock()
        fake_sock.close = MagicMock()
        monkeypatch.setattr(proxy.socket, "socket", lambda *a, **k: fake_sock)

        loop = asyncio.get_running_loop()
        handlers = {}

        def fake_add_signal_handler(sig, cb, *args):
            handlers[sig] = cb

        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)

        gate = asyncio.Event()

        async def hang(*a, **k):
            await gate.wait()
            return b"", ("127.0.0.1", 0)

        monkeypatch.setattr(loop, "sock_recvfrom", hang)

        task = asyncio.create_task(proxy._async_main())
        # Let the task run to its first await; the SIGTERM handler is registered
        # synchronously before it.
        for _ in range(100):
            if signal.SIGTERM in handlers:
                break
            await asyncio.sleep(0.01)
        assert signal.SIGTERM in handlers, "SIGTERM handler not registered"

        # Simulate podman's SIGTERM arriving at PID 1.
        handlers[signal.SIGTERM]()
        await asyncio.wait_for(task, timeout=2)
        assert task.done()
        # Clean teardown ran: the DNS listen socket was closed.
        fake_sock.close.assert_called()

    async def test_sigterm_handler_registered_even_with_consent(
        self, proxy, monkeypatch
    ):
        # The handler is installed on the consent path too (where NFQUEUE + the
        # WS client must be torn down); only the registration is asserted, since
        # fully driving the WS client + NFQUEUE is the e2e's job (#2327).
        monkeypatch.setattr(
            proxy.config, "CONSENT_URL", "http://klangkd/ws/egress-sidecar"
        )
        monkeypatch.setattr(proxy.rules, "check_mark", lambda: None)
        monkeypatch.setattr(proxy.packets, "check_rst_socket", lambda: None)
        # #2485: the egress-accounting resolve/install/read are blocking (DNS +
        # iptables) -- stub them so this unit test does no real I/O, like the
        # other startup mocks above.
        monkeypatch.setattr(proxy.app, "_resolve_ws_host", lambda url: None)
        monkeypatch.setattr(proxy.rules, "install_acct", lambda *a, **k: None)
        monkeypatch.setattr(proxy.rules, "acct_bytes", lambda: 0)
        started = []

        class _FakeClient:
            async def start(self):
                started.append(True)

        monkeypatch.setattr(
            proxy.consent, "SidecarConsentClient", lambda *a: _FakeClient()
        )
        fake_nfq = MagicMock()
        monkeypatch.setattr(
            proxy.nfqueue, "_setup_nfq_consumer", lambda client: fake_nfq
        )
        fake_sock = MagicMock()
        fake_sock.close = MagicMock()
        monkeypatch.setattr(proxy.socket, "socket", lambda *a, **k: fake_sock)

        loop = asyncio.get_running_loop()
        handlers = {}
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, cb, *a: handlers.__setitem__(sig, cb),
        )
        gate = asyncio.Event()
        monkeypatch.setattr(
            loop,
            "sock_recvfrom",
            lambda *a, **k: gate.wait(),
        )

        task = asyncio.create_task(proxy._async_main())
        for _ in range(100):
            if signal.SIGTERM in handlers:
                break
            await asyncio.sleep(0.01)
        assert started  # consent client brought up
        assert signal.SIGTERM in handlers
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_second_sigterm_does_not_abort_teardown(self, proxy, monkeypatch):
        # Regression (#2400 review): a second SIGTERM arriving while _shutdown
        # is mid-await must NOT re-cancel the main task. The CancelledError is a
        # BaseException, so _shutdown's `except Exception` guards don't catch
        # it -- without the idempotency flag, nfq.unbind()/sock.close() get
        # skipped and the clean teardown the PR exists to provide is aborted.
        monkeypatch.setattr(proxy.config, "CONSENT_URL", "http://k/ev")
        monkeypatch.setattr(proxy.rules, "check_mark", lambda: None)
        monkeypatch.setattr(proxy.packets, "check_rst_socket", lambda: None)
        # #2485: the egress-accounting resolve/install/read are blocking (DNS +
        # iptables) -- stub them so this unit test does no real I/O, like the
        # other startup mocks above.
        monkeypatch.setattr(proxy.app, "_resolve_ws_host", lambda url: None)
        monkeypatch.setattr(proxy.rules, "install_acct", lambda *a, **k: None)
        monkeypatch.setattr(proxy.rules, "acct_bytes", lambda: 0)

        in_stop = asyncio.Event()
        release_stop = asyncio.Event()

        class _BlockingClient:
            async def start(self):
                pass

            async def stop(self):
                in_stop.set()
                await release_stop.wait()  # hold teardown mid-flight

        monkeypatch.setattr(
            proxy.consent, "SidecarConsentClient", lambda *a: _BlockingClient()
        )
        nfq = MagicMock()
        nfq.get_fd = MagicMock(return_value=11)
        monkeypatch.setattr(proxy.nfqueue, "_setup_nfq_consumer", lambda client: nfq)
        fake_sock = MagicMock()
        fake_sock.close = MagicMock()
        monkeypatch.setattr(proxy.socket, "socket", lambda *a, **k: fake_sock)

        loop = asyncio.get_running_loop()
        handlers = {}
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, cb, *a: handlers.__setitem__(sig, cb),
        )
        gate = asyncio.Event()
        monkeypatch.setattr(loop, "sock_recvfrom", lambda *a, **k: gate.wait())

        task = asyncio.create_task(proxy._async_main())
        for _ in range(100):
            if signal.SIGTERM in handlers:
                break
            await asyncio.sleep(0.01)
        # First SIGTERM -> main task cancels -> _shutdown -> client.stop() blocks.
        handlers[signal.SIGTERM]()
        for _ in range(100):
            if in_stop.is_set():
                break
            await asyncio.sleep(0.01)
        assert in_stop.is_set()  # teardown reached client.stop()
        # Second SIGTERM WHILE client.stop() is blocked. With the idempotency
        # guard this is a no-op; without it the re-cancel aborts _shutdown.
        handlers[signal.SIGTERM]()
        await asyncio.sleep(0.05)  # let the (no-op) second signal settle
        release_stop.set()  # unblock client.stop(); teardown must finish
        await asyncio.wait_for(task, timeout=3)
        assert task.done()
        nfq.unbind.assert_called()  # teardown was NOT aborted
        fake_sock.close.assert_called()

    async def test_shutdown_bounds_a_slow_client_stop(self, proxy, monkeypatch):
        # _shutdown bounds client.stop() so a stalled WS close handshake (its
        # close_timeout can be 5s, and the server may be going away during
        # klangkd shutdown) can't re-introduce the 5s SIGKILL window (#2400).
        monkeypatch.setattr(proxy.app, "_SHUTDOWN_CLIENT_TIMEOUT", 0.1)
        nfq = MagicMock()
        nfq.get_fd = MagicMock(return_value=11)
        sock = MagicMock()

        class _SlowClient:
            async def stop(self):
                await asyncio.sleep(100)  # never completes in time

        loop = asyncio.get_running_loop()
        sweep = loop.create_task(asyncio.sleep(100))
        t0 = loop.time()
        await proxy._shutdown(_SlowClient(), nfq, sock, sweep)
        elapsed = loop.time() - t0
        assert elapsed < 1.0  # bounded well under the 5s window
        nfq.unbind.assert_called()  # teardown proceeded past the timed-out stop
        sock.close.assert_called()

    async def test_stop_swallows_cancelled_backoff_task(self, proxy, tmp_path):
        # Regression (#2657): stop() awaited its cancelled _run task behind an
        # `except Exception`, but the CancelledError a cancelled task raises is
        # a BaseException (3.8+), so a stop() issued while _run was parked in
        # the token-retry / reconnect-backoff sleep escaped stop(), escaped
        # _shutdown's `except Exception` around the wait_for, and dumped a raw
        # traceback out of asyncio.run on every workspace removal whose WS was
        # down (eviction e2e saw it via the evict-bystander container) --
        # skipping nfq.unbind/sock.close and exiting 1. The guard now matches
        # _shutdown's sweep/sampler pattern.
        c = proxy.SidecarConsentClient("http://h/ev", str(tmp_path / "missing"), 5)
        await c.start()
        await asyncio.sleep(0.05)  # let _run park in the token-retry sleep
        assert not c._task.done()
        fut = asyncio.get_running_loop().create_future()
        c._pending["lid"] = fut
        await c.stop()  # must return, not raise CancelledError
        assert c._task.done()
        # Teardown continued past the guarded await: pending requests were
        # fail-closed (deny) by _fail_close_pending.
        assert fut.done() and fut.result() == ("deny", "once")

    async def test_shutdown_survives_cancelled_error_from_client_stop(self, proxy):
        # #2657 defense in depth: _shutdown's wait_for(client.stop()) guard is
        # widened to (CancelledError, Exception) so no BaseException path out
        # of the client can abort teardown -- the same widening its own
        # sweep/sampler guards already have.
        nfq = MagicMock()
        nfq.get_fd = MagicMock(return_value=9)
        sock = MagicMock()
        sweep = asyncio.get_running_loop().create_task(asyncio.sleep(100))

        class _RaisingClient:
            async def stop(self):
                raise asyncio.CancelledError()

        await proxy._shutdown(_RaisingClient(), nfq, sock, sweep)
        nfq.unbind.assert_called_once()  # teardown was NOT aborted
        sock.close.assert_called_once()
        assert sweep.cancelled()


# --- branch-coverage gap tests (#2834) ----------------------------------------
#
# The 100% branch gate requires every branch OUTCOME exercised, not just every
# line. These classes drive the paths the original suites reached only via
# monkeypatched inner helpers (the real forward/respond/nxdomain wire paths,
# the consent client's connect loop, the best-effort exception swallows) or
# not at all (query_name/nxdomain_for, _resolve_ws_host, main). Where an
# outcome is structurally unreachable in-process (PID-1-only entry,
# never-exiting sweeper loops) the source carries `# pragma: no branch` /
# `# pragma: no cover` with a comment instead.


class _FakeQueryName:
    """Stub ``dns.message.from_wire`` to return a query ``msg`` for
    query_name/nxdomain_for tests (the dnspython stub module is the shared
    mutable surface the suite patches, per TestARecordsWithTtl)."""

    def __init__(self, proxy, monkeypatch, question=None, resp=None):
        self.resp = resp or types.SimpleNamespace(
            set_rcode=MagicMock(), to_wire=lambda: b"WIRE"
        )
        msg = types.SimpleNamespace(question=question if question is not None else [])
        monkeypatch.setattr(proxy.dns.message, "from_wire", lambda wire: msg)
        monkeypatch.setattr(proxy.dns.message, "make_response", lambda q: self.resp)


class TestWireHelpers:
    """query_name / nxdomain_for / _send_nxdomain (#2834): the wire-level
    helpers the routing tests monkeypatch away, driven directly."""

    def test_query_name_lowercases_and_strips_dot(self, proxy, monkeypatch):
        name = types.SimpleNamespace(
            name=types.SimpleNamespace(to_text=lambda: "Example.COM.")
        )
        _FakeQueryName(proxy, monkeypatch, question=[name])
        assert proxy.query_name(b"wire") == "example.com"

    def test_query_name_empty_question(self, proxy, monkeypatch):
        # A question-less DNS message (e.g. some resolver probes) -> "" (the
        # _decision gate then denies it, fail-closed).
        _FakeQueryName(proxy, monkeypatch, question=[])
        assert proxy.query_name(b"wire") == ""

    def test_nxdomain_for_sets_rcode(self, proxy, monkeypatch):
        fake = _FakeQueryName(proxy, monkeypatch)
        assert proxy.nxdomain_for(b"query") == b"WIRE"
        fake.resp.set_rcode.assert_called_once_with(proxy.dns.rcode.NXDOMAIN)

    def test_send_nxdomain_swallows_sendto_failure(self, proxy, monkeypatch):
        # Best-effort: a vanished client must not raise out of the recv loop.
        _FakeQueryName(proxy, monkeypatch)
        s = MagicMock()
        s.sendto.side_effect = OSError("client gone")
        proxy._send_nxdomain(s, b"q", ("127.0.0.1", 53))  # must not raise

    def test_send_nxdomain_sends_wire(self, proxy, monkeypatch):
        _FakeQueryName(proxy, monkeypatch)
        s = MagicMock()
        proxy._send_nxdomain(s, b"q", ("127.0.0.1", 53))
        s.sendto.assert_called_once_with(b"WIRE", ("127.0.0.1", 53))


class _FakeUpstreamSock:
    """A socket.socket stand-in for _forward_marked: records the MARK
    setsockopt + the blocking mode, closed by the caller's finally."""

    def __init__(self):
        self.opts = []
        self.blocking = None
        self.closed = False

    def setsockopt(self, level, opt, val):
        self.opts.append((level, opt, val))

    def setblocking(self, flag):
        self.blocking = flag

    def close(self):
        self.closed = True


class TestForwardMarked:
    """_forward_marked / _forward_and_learn / _forward_and_record (#2834):
    the real upstream-forward preamble (MARK'd non-blocking socket + bounded
    send/recv on the loop), driven with a fake socket + loop helpers."""

    async def test_success_returns_response_and_closes(self, proxy, monkeypatch):
        sock = _FakeUpstreamSock()
        monkeypatch.setattr(proxy.socket, "socket", lambda *a, **k: sock)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "sock_sendto", AsyncMock(return_value=len(b"q")))
        monkeypatch.setattr(
            loop, "sock_recvfrom", AsyncMock(return_value=(b"resp", ("9.9.9.9", 53)))
        )
        assert await proxy._forward_marked(b"q") == b"resp"
        # The loop-avoidance mark + non-blocking mode were applied, and the
        # socket was closed on success (the finally runs on every path).
        assert (socket.SOL_SOCKET, socket.SO_MARK, proxy.MARK) in [o for o in sock.opts]
        assert sock.blocking is False
        assert sock.closed

    async def test_upstream_failure_returns_none(self, proxy, monkeypatch):
        # Any upstream failure/timeout -> None (the caller then just drops
        # the query; the client's resolver retransmits).
        sock = _FakeUpstreamSock()
        monkeypatch.setattr(proxy.socket, "socket", lambda *a, **k: sock)

        async def _boom(*a, **k):
            raise OSError("upstream unreachable")

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "sock_sendto", _boom)
        assert await proxy._forward_marked(b"q") is None
        assert sock.closed  # closed on error too

    async def test_forward_and_learn_none_resp_skips_respond(self, proxy, monkeypatch):
        # Upstream failure: _forward_and_learn returns without touching the
        # client-facing socket.
        monkeypatch.setattr(
            proxy.resolve, "_forward_marked", AsyncMock(return_value=None)
        )
        respond = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_respond_allowed", respond)
        await proxy._forward_and_learn(MagicMock(), b"q", ("c", 1), "q.name", {443})
        respond.assert_not_awaited()

    async def test_forward_and_learn_resp_answers(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.resolve, "_forward_marked", AsyncMock(return_value=b"resp")
        )
        respond = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_respond_allowed", respond)
        s = MagicMock()
        await proxy._forward_and_learn(s, b"q", ("c", 1), "q.name", {443})
        respond.assert_awaited_once_with(s, b"resp", ("c", 1), "q.name", {443})

    async def test_forward_and_record_none_resp_skips_respond(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.resolve, "_forward_marked", AsyncMock(return_value=None)
        )
        respond = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_respond_recorded", respond)
        await proxy._forward_and_record(MagicMock(), b"q", ("c", 1), "q.name")
        respond.assert_not_awaited()

    async def test_forward_and_record_resp_answers(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.resolve, "_forward_marked", AsyncMock(return_value=b"resp")
        )
        respond = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_respond_recorded", respond)
        s = MagicMock()
        await proxy._forward_and_record(s, b"q", ("c", 1), "q.name")
        respond.assert_awaited_once_with(s, b"resp", ("c", 1), "q.name")


class TestRespondEdgePaths:
    """_respond_allowed / _respond_recorded edge outcomes (#2834): a
    response with no A records learns/records nothing but still answers, a
    parsing failure degrades to the same, and DEBUG prints the allow/resolve
    line."""

    async def test_no_a_records_still_sends(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy.resolve, "a_records_with_ttl", lambda wire: [])
        learn = MagicMock()
        monkeypatch.setattr(proxy.rules, "_learn_all", learn)
        s = MagicMock()
        await proxy._respond_allowed(s, b"resp", ("c", 1), "q", {443})
        learn.assert_not_called()
        s.sendto.assert_called_once()

    async def test_unparseable_response_degrades_to_no_records(
        self, proxy, monkeypatch
    ):
        def _boom(wire):
            raise ValueError("garbage wire")

        monkeypatch.setattr(proxy.resolve, "a_records_with_ttl", _boom)
        learn = MagicMock()
        monkeypatch.setattr(proxy.rules, "_learn_all", learn)
        s = MagicMock()
        await proxy._respond_allowed(s, b"resp", ("c", 1), "q", {443})
        learn.assert_not_called()
        s.sendto.assert_called_once()

    async def test_recorded_no_a_records_still_sends(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy.resolve, "a_records_with_ttl", lambda wire: [])
        record = MagicMock()
        monkeypatch.setattr(proxy.rules, "_record_hosts", record)
        s = MagicMock()
        await proxy._respond_recorded(s, b"resp", ("c", 1), "q")
        record.assert_not_called()
        s.sendto.assert_called_once()

    async def test_recorded_unparseable_response_degrades(self, proxy, monkeypatch):
        def _boom(wire):
            raise ValueError("garbage wire")

        monkeypatch.setattr(proxy.resolve, "a_records_with_ttl", _boom)
        record = MagicMock()
        monkeypatch.setattr(proxy.rules, "_record_hosts", record)
        s = MagicMock()
        await proxy._respond_recorded(s, b"resp", ("c", 1), "q")
        record.assert_not_called()
        s.sendto.assert_called_once()

    async def test_debug_prints_allow_line(self, proxy, monkeypatch, capsys):
        monkeypatch.setattr(proxy.resolve, "DEBUG", True)
        monkeypatch.setattr(
            proxy.resolve, "a_records_with_ttl", lambda w: [("1.2.3.4", 60)]
        )
        monkeypatch.setattr(proxy.rules, "_learn_all", lambda *a: None)
        await proxy._respond_allowed(MagicMock(), b"r", ("c", 1), "q.name", {443})
        assert "allow q.name" in capsys.readouterr().out

    async def test_debug_prints_resolve_line(self, proxy, monkeypatch, capsys):
        monkeypatch.setattr(proxy.resolve, "DEBUG", True)
        monkeypatch.setattr(
            proxy.resolve, "a_records_with_ttl", lambda w: [("1.2.3.4", 60)]
        )
        monkeypatch.setattr(proxy.rules, "_record_hosts", lambda *a: None)
        await proxy._respond_recorded(MagicMock(), b"r", ("c", 1), "q.name")
        assert "resolve q.name" in capsys.readouterr().out

    async def test_handle_packet_debug_prints_reject_line(
        self, proxy, monkeypatch, capsys
    ):
        monkeypatch.setattr(proxy.resolve, "DEBUG", True)
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "evil.test")
        monkeypatch.setattr(
            proxy.allowlist, "REJECT_SPECS", [("evil.test", None, proxy._EXACT)]
        )
        _FakeQueryName(proxy, monkeypatch)  # nxdomain_for wire
        await proxy._handle_packet(MagicMock(), b"q", ("c", 1), None)
        assert "reject evil.test" in capsys.readouterr().out

    async def test_handle_packet_debug_prints_deny_line(
        self, proxy, monkeypatch, capsys
    ):
        monkeypatch.setattr(proxy.resolve, "DEBUG", True)
        monkeypatch.setattr(proxy.resolve, "query_name", lambda wire: "other.test")
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(proxy.allowlist, "REJECT_SPECS", [])
        _FakeQueryName(proxy, monkeypatch)  # nxdomain_for wire
        await proxy._handle_packet(MagicMock(), b"q", ("c", 1), None)
        assert "deny  other.test" in capsys.readouterr().out


class TestAppTeardownHelpers:
    """app.py's best-effort teardown helpers (#2834): each swallows its
    resource's failure so one bad close can't abort the rest of _shutdown."""

    async def test_cancel_task_swallows_task_exception(self, proxy):
        async def _boom():
            raise ValueError("task blew up")

        t = asyncio.create_task(_boom())
        await proxy._cancel_task(t)  # must not raise
        assert t.done()

    async def test_cancel_task_none_is_noop(self, proxy):
        await proxy._cancel_task(None)  # no task (not started) -> no-op

    async def test_unbind_nfq_swallows_remove_reader_failure(self, proxy, monkeypatch):
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop, "remove_reader", MagicMock(side_effect=RuntimeError("no reader"))
        )
        nfq = MagicMock()
        proxy._unbind_nfq(nfq)  # sync + must not raise
        nfq.unbind.assert_called_once()  # teardown continued

    async def test_unbind_nfq_swallows_unbind_failure(self, proxy):
        nfq = MagicMock()
        nfq.get_fd = MagicMock(return_value=7)
        nfq.unbind = MagicMock(side_effect=RuntimeError("already unbound"))
        proxy._unbind_nfq(nfq)  # must not raise

    async def test_unbind_nfq_none_is_noop(self, proxy):
        proxy._unbind_nfq(None)  # no queue (static mode) -> no-op

    def test_close_quietly_swallows_close_failure(self, proxy):
        sock = MagicMock()
        sock.close = MagicMock(side_effect=OSError("already closed"))
        proxy._close_quietly(sock)  # must not raise

    async def test_close_quietly_closes(self, proxy):
        sock = MagicMock()
        proxy._close_quietly(sock)
        sock.close.assert_called_once()


class TestResolveWsHost:
    """_resolve_ws_host (#2485, #2834): the klangkd WS host resolved to an IP
    so the egress-accounting rule can exclude it; any failure returns None
    (printed) -- which defeats the idle timeout, so it is loud."""

    def test_resolves_host_to_ip(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy.app.socket, "gethostbyname", lambda host: "10.1.2.3")
        assert proxy._resolve_ws_host("http://klangkd:8443/ws/x") == "10.1.2.3"

    def test_unresolvable_host_returns_none_and_prints(
        self, proxy, monkeypatch, capsys
    ):
        def _boom(host):
            raise OSError("no such host")

        monkeypatch.setattr(proxy.app.socket, "gethostbyname", _boom)
        assert proxy._resolve_ws_host("http://klangkd/ws") is None
        assert "could not resolve the klangkd WS host" in capsys.readouterr().out

    def test_hostless_url_returns_none_and_prints(self, proxy, monkeypatch, capsys):
        # A URL with no host (e.g. a bare path) -> None, same loud warning.
        monkeypatch.setattr(proxy.app.socket, "gethostbyname", lambda host: "10.1.2.3")
        assert proxy._resolve_ws_host("not-a-url") is None
        assert "could not resolve the klangkd WS host" in capsys.readouterr().out


class TestAsyncMainLoopPaths:
    """_async_main's in-loop paths the SIGTERM tests don't reach (#2834):
    the recv->handle happy path, the unsupported-signal-backend fallback,
    the DEBUG shutdown line, and main()'s KeyboardInterrupt swallow."""

    def _stub_static_startup(self, proxy, monkeypatch, fake_sock):
        # Static mode (no consent URL) + stubbed iptables probes/sockets so
        # _async_main reaches its recv loop without real kernel access.
        monkeypatch.setattr(proxy.config, "CONSENT_URL", "")
        monkeypatch.setattr(proxy.rules, "check_mark", lambda: None)
        monkeypatch.setattr(proxy.socket, "socket", lambda *a, **k: fake_sock)

    async def test_recv_loop_dispatches_to_handle_packet(self, proxy, monkeypatch):
        # One received datagram is handed to _handle_packet, then the loop
        # parks again (the gate hang doubles as the "parked" state).
        fake_sock = MagicMock()
        self._stub_static_startup(proxy, monkeypatch, fake_sock)
        loop = asyncio.get_running_loop()
        gate = asyncio.Event()
        served = asyncio.Event()

        async def recvfrom(*a, **k):
            if not served.is_set():
                served.set()
                return b"q", ("127.0.0.1", 40000)
            await gate.wait()
            return b"", ("127.0.0.1", 0)

        monkeypatch.setattr(loop, "sock_recvfrom", recvfrom)
        handled = AsyncMock()
        monkeypatch.setattr(proxy.resolve, "_handle_packet", handled)
        task = asyncio.create_task(proxy._async_main())
        for _ in range(100):
            if served.is_set():
                break
            await asyncio.sleep(0.01)
        handled.assert_awaited_once_with(fake_sock, b"q", ("127.0.0.1", 40000), None)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_recv_failure_continues_loop(self, proxy, monkeypatch):
        # A transient recv error is swallowed and the loop keeps serving.
        fake_sock = MagicMock()
        self._stub_static_startup(proxy, monkeypatch, fake_sock)
        loop = asyncio.get_running_loop()
        calls = {"n": 0}
        gate = asyncio.Event()

        async def recvfrom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient recv failure")
            await gate.wait()
            return b"", ("127.0.0.1", 0)

        monkeypatch.setattr(loop, "sock_recvfrom", recvfrom)
        task = asyncio.create_task(proxy._async_main())
        for _ in range(100):
            if calls["n"] >= 1:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert calls["n"] >= 2  # the loop continued past the failure
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_unsupported_signal_backend_still_serves(self, proxy, monkeypatch):
        # add_signal_handler raising (non-main thread / unsupported loop)
        # must not stop the proxy from serving.
        fake_sock = MagicMock()
        self._stub_static_startup(proxy, monkeypatch, fake_sock)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            MagicMock(side_effect=NotImplementedError("no signals")),
        )
        gate = asyncio.Event()
        parked = asyncio.Event()

        async def recvfrom(*a, **k):
            parked.set()
            await gate.wait()
            return b"", ("127.0.0.1", 0)

        monkeypatch.setattr(loop, "sock_recvfrom", recvfrom)
        task = asyncio.create_task(proxy._async_main())
        for _ in range(100):
            if parked.is_set():
                break
            await asyncio.sleep(0.01)
        assert parked.is_set() and not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_sigterm_debug_line(self, proxy, monkeypatch, capsys):
        # DEBUG on: the CancelledError unwind prints the shutdown line.
        fake_sock = MagicMock()
        self._stub_static_startup(proxy, monkeypatch, fake_sock)
        monkeypatch.setattr(proxy.app, "DEBUG", True)
        loop = asyncio.get_running_loop()
        handlers = {}
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, cb, *a: handlers.__setitem__(sig, cb),
        )
        gate = asyncio.Event()
        monkeypatch.setattr(loop, "sock_recvfrom", lambda *a, **k: gate.wait())
        task = asyncio.create_task(proxy._async_main())
        for _ in range(100):
            if signal.SIGTERM in handlers:
                break
            await asyncio.sleep(0.01)
        handlers[signal.SIGTERM]()
        await asyncio.wait_for(task, timeout=2)
        assert "stop signal received, shutting down" in capsys.readouterr().out

    def test_main_runs_and_returns(self, proxy, monkeypatch):
        # main() is the PID-1 asyncio.run wrapper; driven with a no-op body.
        ran = []

        async def _noop():
            ran.append(True)

        monkeypatch.setattr(proxy.app, "_async_main", _noop)
        proxy.app.main()  # must return cleanly
        assert ran

    def test_main_swallows_keyboard_interrupt(self, proxy, monkeypatch):
        # Ctrl-C at the console (before podman's SIGTERM) exits quietly.
        async def _interrupt():
            raise KeyboardInterrupt

        monkeypatch.setattr(proxy.app, "_async_main", _interrupt)
        proxy.app.main()  # must not raise


class _FakeConsentWS:
    """A websockets connection stand-in: async-context manager + async
    iterator over scripted frames, recording sends."""

    def __init__(self, frames=()):
        self._frames = list(frames)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            for frame in self._frames:
                yield frame

        return _gen()


class TestConsentClientRunLoop:
    """SidecarConsentClient._run / stop / request edge outcomes (#2834): the
    connect-dispatch-reconnect loop (stubbed websockets.connect), the no-token
    retry, the fail-close exception paths, and stop()'s guarded teardown."""

    @staticmethod
    def _client(proxy, url="http://klangkd/ws", hold=60.0):
        return proxy.consent.SidecarConsentClient(url, "/nonexistent-token", hold)

    async def test_run_no_token_warns_and_retries(self, proxy, monkeypatch, capsys):
        c = self._client(proxy)
        monkeypatch.setattr(c, "_read_token", lambda: "")
        monkeypatch.setattr(proxy.consent, "DEBUG", True)

        # Fast-forward the 1s retry sleep and stop after the first retry.
        async def _fast_sleep(_s):
            c._stop = True

        monkeypatch.setattr(proxy.consent.asyncio, "sleep", _fast_sleep)
        await c._run()
        assert "workspace token not yet present" in capsys.readouterr().out

    async def test_run_connects_dispatches_and_resolves_verdict(
        self, proxy, monkeypatch
    ):
        # The happy path: connect (with the bearer header), receive a verdict
        # frame for a pending request, then stop between frames.
        c = self._client(proxy)
        monkeypatch.setattr(c, "_read_token", lambda: "tok")
        connect_args = []
        ws = _FakeConsentWS(
            frames=[
                json.dumps(
                    {
                        "type": "verdict",
                        "id": "v1",
                        "decision": "allow",
                        "duration": "5m",
                    }
                )
            ]
        )

        def _connect(*a, **k):
            connect_args.append((a, k))
            return ws

        monkeypatch.setattr(proxy.consent.websockets, "connect", _connect)
        fut = asyncio.get_running_loop().create_future()
        c._pending["v1"] = fut
        real_dispatch = c._dispatch

        async def _dispatch_then_stop(raw):
            await real_dispatch(raw)
            c._stop = True  # exit after the last frame instead of reconnecting

        monkeypatch.setattr(c, "_dispatch", _dispatch_then_stop)
        await c._run()
        assert connect_args[0][1]["additional_headers"] == {
            "Authorization": "Bearer tok"
        }
        assert fut.result() == ("allow", "5m")
        assert not c.connected  # finally cleared the connection state

    async def test_run_connect_error_backs_off_and_reconnects(self, proxy, monkeypatch):
        # klangkd down: the exception path logs the TYPE only, the finally
        # fail-closes pending requests, and the loop reconnects after backoff.
        c = self._client(proxy)
        monkeypatch.setattr(c, "_read_token", lambda: "tok")
        monkeypatch.setattr(proxy.consent, "DEBUG", True)
        attempts = {"n": 0}

        def _connect(*a, **k):
            attempts["n"] += 1
            raise OSError("klangkd down")

        monkeypatch.setattr(proxy.consent.websockets, "connect", _connect)
        sleeps = []

        async def _fast_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 2:
                c._stop = True  # exits via the while condition (post-backoff)

        monkeypatch.setattr(proxy.consent.asyncio, "sleep", _fast_sleep)
        await c._run()
        assert attempts["n"] >= 2  # reconnected after the backoff sleep
        assert sleeps[0] == 1.0  # initial backoff
        assert sleeps[1] == 2.0  # capped exponential backoff

    async def test_stop_swallows_ws_close_failure(self, proxy):
        # A wedged close handshake must not abort the rest of teardown.
        c = self._client(proxy)
        ws = MagicMock()
        ws.close = AsyncMock(side_effect=RuntimeError("wedged"))
        c._ws = ws
        await c.stop()  # must not raise
        ws.close.assert_awaited_once()

    async def test_stop_without_task_is_inert(self, proxy):
        c = self._client(proxy)
        await c.stop()  # never started -> no task to cancel, no raise

    async def test_dispatch_ignores_bad_payloads(self, proxy):
        c = self._client(proxy)
        await c._dispatch(b"\xff\xfe not json")  # unparseable -> ignored
        await c._dispatch(json.dumps([1, 2, 3]))  # not an object -> ignored
        assert c._pending == {}

    async def test_request_send_failure_fail_closes(self, proxy):
        # The WS dropped between the connected check and the send -> deny at
        # once (never hang the held SYN).
        c = self._client(proxy)
        c._connected.set()
        ws = MagicMock()
        ws.send = AsyncMock(side_effect=RuntimeError("socket gone"))
        c._ws = ws
        assert await c.request("evil.test", 443) == ("deny", "once")
        assert not c._pending  # the failed request was reaped

    async def test_request_timeout_fail_closes(self, proxy):
        c = self._client(proxy, hold=0.01)
        c._connected.set()
        ws = MagicMock()
        ws.send = AsyncMock()
        c._ws = ws
        # No verdict frame ever arrives -> the hold timeout denies.
        assert await c.request("evil.test", 443) == ("deny", "once")
        assert not c._pending

    def test_bump_activity_off_loop_is_noop(self, proxy):
        # Called from a non-async context (defensive): nothing to schedule.
        c = self._client(proxy)
        c._connected.set()  # pretend connected so the gate is reached
        c.bump_activity()  # no running loop -> silent return

    async def test_send_activity_without_ws_is_noop(self, proxy):
        c = self._client(proxy)
        c._ws = None
        await c._send_activity()  # WS dropped between gate and send -> return

    async def test_send_activity_swallows_send_failure(self, proxy):
        c = self._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock(side_effect=RuntimeError("socket gone"))
        c._ws = ws
        await c._send_activity()  # best-effort: must not raise

    async def test_sampler_swallows_tick_failure(self, proxy):
        # One bad tick (here: the bump itself raising) defers to the next
        # interval instead of killing the sampler task.
        import itertools

        class _BoomBump:
            def bump_activity(self):
                raise RuntimeError("bump exploded")

        counter = itertools.count()

        def get_bytes():
            return next(counter)

        task = asyncio.create_task(
            proxy._activity_sampler(_BoomBump(), get_bytes, 0.01)
        )
        await asyncio.sleep(0.08)  # several ticks
        assert not task.done()  # the failure was swallowed, sampling continues
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_handle_drop_rule_bad_host_acks_not_ok(self, proxy, monkeypatch):
        # A malformed revoke (no host / bad decision) acks ok=False so
        # klangkd can retry rather than consider it applied.
        c = self._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock()
        c._ws = ws
        await c._handle_drop_rule({"type": "drop_rule", "id": "r1"})
        sent = json.loads(ws.send.await_args.args[0])
        assert sent == {"type": "drop_ack", "id": "r1", "ok": False}

    async def test_handle_drop_rule_drop_failure_acks_not_ok(self, proxy, monkeypatch):
        c = self._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock()
        c._ws = ws
        monkeypatch.setattr(
            proxy.rules,
            "drop_for_host",
            lambda host, decision: (_ for _ in ()).throw(RuntimeError("iptables")),
        )
        await c._handle_drop_rule(
            {
                "type": "drop_rule",
                "id": "r2",
                "host": "evil.test",
                "decision": "allowed",
            }
        )
        sent = json.loads(ws.send.await_args.args[0])
        assert sent["ok"] is False

    async def test_handle_drop_rule_ack_send_failure_is_silent(
        self, proxy, monkeypatch
    ):
        # Best-effort ack: a dropped ack must not raise into _run's loop.
        c = self._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock(side_effect=RuntimeError("socket gone"))
        c._ws = ws
        monkeypatch.setattr(proxy.rules, "drop_for_host", lambda h, d: set())
        await c._handle_drop_rule(
            {"type": "drop_rule", "id": "r3", "host": "evil.test", "decision": "denied"}
        )


class TestNfqueueEdgePaths:
    """nfqueue.py branch outcomes (#2834): the transient drain failure, the
    non-TCP classification, the expired-verdict fallthrough, the deny-side
    session memory, and the verdict-application failure swallows."""

    async def test_drain_swallows_transient_failure(self, proxy):
        nfq = MagicMock()
        nfq.run = MagicMock(side_effect=RuntimeError("netlink hiccup"))
        proxy.nfqueue._drain(nfq)  # must not raise

    def test_classify_non_tcp_uses_destination_granularity(self, proxy):
        # A queued UDP packet (no SYN tuple) falls back to (dst, dport); the
        # source end is empty (parse_syn_tuple is TCP-only).
        payload = _ip_payload("10.2.3.4", 53, proto=17)
        assert proxy.nfqueue._classify_packet(payload) == (
            "",
            0,
            "10.2.3.4",
            53,
        )

    def test_classify_unparseable_returns_none(self, proxy):
        assert proxy.nfqueue._classify_packet(b"\x00" * 8) is None

    async def test_expired_cached_verdict_re_prompts(self, proxy, monkeypatch):
        # A cache entry past its TTL must NOT be reused: the SYN proceeds to
        # a fresh consent request (the cache only covers retransmits).
        proxy.state._VERDICT_CACHE.clear()
        proxy.nfqueue._INFLIGHT.clear()
        try:
            payload = _syn_payload("172.16.0.9", 40000, "10.2.3.4", 443, 7)
            flow = ("172.16.0.9", 40000, "10.2.3.4", 443)
            # Expired entry: verdict time in the past.
            proxy.state._VERDICT_CACHE[flow] = ("deny", time.time() - 1)
            client = MagicMock()
            client.connected = True
            client.request = AsyncMock(return_value=("deny", "once"))
            monkeypatch.setattr(proxy.nfqueue, "_host_for", lambda ip: "evil.test")
            monkeypatch.setattr(
                proxy.nfqueue, "_session_host_allows_ttl", lambda h, p: None
            )
            monkeypatch.setattr(
                proxy.nfqueue, "_session_host_denies_ttl", lambda h, p: None
            )
            monkeypatch.setattr(proxy.nfqueue.rules, "reject", lambda *a: None)
            pkt = MagicMock()
            pkt.get_payload = MagicMock(return_value=payload)
            proxy.nfqueue._cb(pkt, client)
            for _ in range(50):
                if pkt.drop.called:
                    break
                await asyncio.sleep(0.01)
            client.request.assert_called_once()  # fresh prompt, not the cache
            pkt.drop.assert_called_once()
        finally:
            proxy.state._VERDICT_CACHE.clear()
            proxy.nfqueue._INFLIGHT.clear()

    async def test_deny_session_verdict_is_remembered(self, proxy):
        # A timed deny adds an in-session host DENY (auto-deny retries).
        proxy.state._SESSION_HOST_DENIES.clear()
        try:
            proxy.nfqueue._remember_session_verdict("deny", "evil.test", 443, 300.0)
            assert proxy.state._SESSION_HOST_DENIES
            host, port, mode, _exp = proxy.state._SESSION_HOST_DENIES[0]
            assert (host, port, mode) == ("evil.test", 443, proxy._EXACT)
        finally:
            proxy.state._SESSION_HOST_DENIES.clear()

    async def test_remember_session_verdict_once_adds_nothing(self, proxy):
        # `once` is per-connection: no host memory.
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy.state._SESSION_HOST_DENIES.clear()
        proxy.nfqueue._remember_session_verdict("allow", "ok.test", 443, None)
        proxy.nfqueue._remember_session_verdict("deny", "evil.test", 443, None)
        assert not proxy.state._SESSION_HOST_ALLOWS
        assert not proxy.state._SESSION_HOST_DENIES

    async def test_remember_session_verdict_non_tcp_adds_nothing(self, proxy):
        proxy.state._SESSION_HOST_ALLOWS.clear()
        try:
            proxy.nfqueue._remember_session_verdict("allow", "ok.test", 0, 300.0)
            assert not proxy.state._SESSION_HOST_ALLOWS
        finally:
            proxy.state._SESSION_HOST_ALLOWS.clear()

    async def test_apply_verdict_allow_learn_failure_still_accepts(
        self, proxy, monkeypatch
    ):
        # A transient iptables failure installing the ACCEPT must not strand
        # the held SYN: the verdict still accepts this connection.
        monkeypatch.setattr(
            proxy.nfqueue.rules,
            "allow",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("iptables")),
        )
        pkt = MagicMock()
        await proxy.nfqueue._apply_verdict(
            pkt,
            ("s", 1, "d", 2),
            "10.2.3.4",
            443,
            "allow",
            300.0,
            asyncio.get_running_loop(),
        )
        pkt.accept.assert_called_once()

    async def test_apply_verdict_deny_non_tcp_just_drops(self, proxy):
        # port 0 (non-TCP): no RST to forge, no reject rule -- plain drop.
        pkt = MagicMock()
        await proxy.nfqueue._apply_verdict(
            pkt,
            ("s", 0, "d", 0),
            "10.2.3.4",
            0,
            "deny",
            300.0,
            asyncio.get_running_loop(),
        )
        pkt.drop.assert_called_once()

    async def test_apply_verdict_deny_rst_failure_still_drops(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.nfqueue.packets,
            "_send_rst",
            lambda payload: (_ for _ in ()).throw(RuntimeError("no raw sock")),
        )
        monkeypatch.setattr(proxy.nfqueue.rules, "reject", lambda *a: None)
        pkt = MagicMock()
        pkt.get_payload = MagicMock(return_value=b"x")
        await proxy.nfqueue._apply_verdict(
            pkt,
            ("s", 1, "d", 2),
            "10.2.3.4",
            443,
            "deny",
            300.0,
            asyncio.get_running_loop(),
        )
        pkt.drop.assert_called_once()

    async def test_apply_verdict_deny_reject_failure_still_drops(
        self, proxy, monkeypatch
    ):
        monkeypatch.setattr(proxy.nfqueue.packets, "_send_rst", lambda payload: None)
        monkeypatch.setattr(
            proxy.nfqueue.rules,
            "reject",
            lambda *a: (_ for _ in ()).throw(RuntimeError("iptables")),
        )
        pkt = MagicMock()
        pkt.get_payload = MagicMock(return_value=b"x")
        await proxy.nfqueue._apply_verdict(
            pkt,
            ("s", 1, "d", 2),
            "10.2.3.4",
            443,
            "deny",
            None,
            asyncio.get_running_loop(),
        )
        pkt.drop.assert_called_once()

    async def test_decide_and_verdict_bounds_the_cache(self, proxy, monkeypatch):
        # A denied-flow flood must not grow the cache unbounded: past 4096
        # entries the whole cache is dropped (allowed flows stop arriving).
        proxy.state._VERDICT_CACHE.clear()
        proxy.nfqueue._INFLIGHT.clear()
        try:
            flow = ("172.16.0.9", 40000, "10.2.3.4", 443)
            for i in range(4100):
                proxy.state._VERDICT_CACHE[("10.0.0.1", i, "d", 443)] = (
                    "deny",
                    time.time() + 60,
                )
            client = MagicMock()
            client.request = AsyncMock(return_value=("deny", "once"))
            monkeypatch.setattr(
                proxy.nfqueue, "_remember_session_verdict", lambda *a: None
            )
            monkeypatch.setattr(
                proxy.nfqueue,
                "_apply_verdict",
                AsyncMock(),
            )
            pkt = MagicMock()
            await proxy.nfqueue._decide_and_verdict(
                pkt, flow, "10.2.3.4", 443, "evil.test", client
            )
            assert len(proxy.state._VERDICT_CACHE) == 1  # cleared + this verdict
        finally:
            proxy.state._VERDICT_CACHE.clear()
            proxy.nfqueue._INFLIGHT.clear()

    async def test_deny_session_host_swallows_rst_failure(self, proxy, monkeypatch):
        monkeypatch.setattr(
            proxy.nfqueue.packets,
            "_send_rst",
            lambda payload: (_ for _ in ()).throw(RuntimeError("no raw sock")),
        )
        monkeypatch.setattr(proxy.nfqueue.rules, "reject", lambda *a: None)
        proxy.state._VERDICT_CACHE.clear()
        try:
            pkt = MagicMock()
            proxy.nfqueue._deny_session_host(
                pkt,
                ("s", 1, "d", 2),
                b"payload",
                "10.2.3.4",
                443,
                time.time(),
                30.0,
            )
            pkt.drop.assert_called_once()
        finally:
            proxy.state._VERDICT_CACHE.clear()


class TestRulesEdgePaths:
    """rules.py branch outcomes (#2834): the best-effort iptables swallows,
    the sweeper loop, the idempotent installs, and the startup probes."""

    def test_allow_all_ports_clears_stale_rejects(self, proxy, monkeypatch):
        # An all-ports allow supersedes per-port REJECTs; a failed remove
        # drops one rule, not the allow.
        proxy.rules._REJECTED.clear()
        proxy.rules._LEARNED.clear()
        try:
            monkeypatch.setattr(proxy.rules, "_remove_reject", MagicMock())
            monkeypatch.setattr(proxy.rules, "_install", lambda ip, port: None)
            proxy.rules._REJECTED[("1.2.3.4", 443, 0)] = time.time() + 60
            proxy.rules.allow("1.2.3.4", None, 60)
            assert not proxy.rules._REJECTED  # superseded reject reaped
        finally:
            proxy.rules._REJECTED.clear()
            proxy.rules._LEARNED.clear()

    def test_allow_all_ports_swallows_reject_remove_failure(self, proxy, monkeypatch):
        proxy.rules._REJECTED.clear()
        proxy.rules._LEARNED.clear()
        try:
            monkeypatch.setattr(
                proxy.rules,
                "_remove_reject",
                MagicMock(side_effect=RuntimeError("iptables")),
            )
            monkeypatch.setattr(proxy.rules, "_install", lambda ip, port: None)
            proxy.rules._REJECTED[("1.2.3.4", 443, 0)] = time.time() + 60
            proxy.rules.allow("1.2.3.4", None, 60)  # must not raise
        finally:
            proxy.rules._REJECTED.clear()
            proxy.rules._LEARNED.clear()

    def test_install_reject_skipped_when_rule_exists(self, proxy, monkeypatch):
        runs = []
        monkeypatch.setattr(
            proxy.rules.subprocess, "run", lambda *a, **k: runs.append(a)
        )
        monkeypatch.setattr(
            proxy.rules, "_reject_rule_exists", lambda ip, port, sport: True
        )
        proxy.rules._install_reject("1.2.3.4", 443)
        assert not runs  # no iptables fork for an existing rule

    def test_install_skipped_when_rule_exists(self, proxy, monkeypatch):
        runs = []
        monkeypatch.setattr(
            proxy.rules.subprocess, "run", lambda *a, **k: runs.append(a)
        )
        monkeypatch.setattr(proxy.rules, "_rule_exists", lambda ip, port: True)
        proxy.rules._install("1.2.3.4", 443)
        assert not runs  # no iptables fork for an existing rule

    def test_drop_for_host_swallows_remove_failure(self, proxy, monkeypatch):
        proxy.rules._LEARNED.clear()
        try:
            proxy.rules._LEARNED["1.2.3.4"] = {
                "expire": time.time() + 60,
                "rule_expire": time.time() + 60,
                "ports": {443},
                "host": "evil.test",
            }
            monkeypatch.setattr(
                proxy.rules,
                "_remove",
                MagicMock(side_effect=RuntimeError("iptables")),
            )
            targets = proxy.rules.drop_for_host("evil.test", "allowed")
            assert "1.2.3.4" in targets
            assert "evil.test" in targets
            assert not proxy.rules._LEARNED  # record dropped anyway
        finally:
            proxy.rules._LEARNED.clear()

    def test_drop_for_host_denied_swallows_reject_remove_failure(
        self, proxy, monkeypatch
    ):
        proxy.rules._REJECTED.clear()
        try:
            proxy.rules._REJECTED[("1.2.3.4", 443, 0)] = time.time() + 60
            monkeypatch.setattr(
                proxy.rules,
                "_remove_reject",
                MagicMock(side_effect=RuntimeError("iptables")),
            )
            proxy.rules.drop_for_host("1.2.3.4", "denied")  # must not raise
            assert not proxy.rules._REJECTED
        finally:
            proxy.rules._REJECTED.clear()

    def test_sweep_swallows_remove_failure(self, proxy, monkeypatch):
        proxy.rules._LEARNED.clear()
        try:
            now = time.time()
            proxy.rules._LEARNED["1.2.3.4"] = {
                "expire": now - 1,
                "rule_expire": now - 1,
                "ports": {443},
                "host": "x.test",
            }
            monkeypatch.setattr(
                proxy.rules,
                "_remove",
                MagicMock(side_effect=RuntimeError("iptables")),
            )
            assert proxy.rules.sweep_once(now) == [("1.2.3.4", {443})]
        finally:
            proxy.rules._LEARNED.clear()

    def test_sweep_defaults_to_wall_clock(self, proxy):
        proxy.rules._LEARNED.clear()
        proxy.rules._REJECTED.clear()
        assert proxy.rules.sweep_once() == []

    def test_sweep_reject_remove_failure(self, proxy, monkeypatch):
        proxy.rules._REJECTED.clear()
        try:
            now = time.time()
            proxy.rules._REJECTED[("1.2.3.4", 443, 0)] = now - 1
            monkeypatch.setattr(
                proxy.rules,
                "_remove_reject",
                MagicMock(side_effect=RuntimeError("iptables")),
            )
            proxy.rules.sweep_once(now)  # must not raise
            assert not proxy.rules._REJECTED
        finally:
            proxy.rules._REJECTED.clear()

    async def test_async_sweeper_swallows_sweep_failure(self, proxy, monkeypatch):
        # One failed sweep defers cleanup to the next tick; the task lives on.
        monkeypatch.setattr(proxy.rules, "SWEEP_INTERVAL", 0.01)
        monkeypatch.setattr(
            proxy.rules,
            "sweep_once",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("iptables")),
        )
        task = asyncio.create_task(proxy.rules._async_sweeper())
        await asyncio.sleep(0.08)
        assert not task.done()  # swallowed, kept sweeping
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_fmt_ports_all_when_none_present(self, proxy):
        assert proxy.rules._fmt_ports({None}) == "all"
        assert proxy.rules._fmt_ports({443, None}) == "all"  # None dominates
        assert proxy.rules._fmt_ports({443, 8443}) == "443,8443"

    def test_check_mark_passes_when_mark_settable(self, proxy, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(proxy.rules.socket, "socket", lambda *a, **k: fake)
        proxy.rules.check_mark()  # must not raise
        fake.close.assert_called_once()

    def test_check_mark_exits_without_cap_net_admin(self, proxy, monkeypatch):
        # No CAP_NET_ADMIN -> SO_MARK fails -> fail loud at startup (the
        # proxy's upstream forwards would loop back into itself).
        fake = MagicMock()
        fake.setsockopt = MagicMock(side_effect=OSError(1, "Operation not permitted"))
        monkeypatch.setattr(proxy.rules.socket, "socket", lambda *a, **k: fake)
        with pytest.raises(SystemExit) as exc:
            proxy.rules.check_mark()
        assert "CAP_NET_ADMIN" in str(exc.value)
        fake.close.assert_called_once()  # the probe socket was still closed


class TestAllowlistEntryTtls:
    """allowlist.py loop outcomes (#2834): empty-host guards and the
    just-expired race the belt-and-suspenders continue exists for."""

    def test_entry_ttl_empty_host_is_none(self, proxy):
        assert proxy._session_entry_ttl([], "", 443) is None

    def test_entry_ttl_skips_just_expired_entry(self, proxy):
        # The prune ran, but the entry expired in the microseconds between
        # its `now` and this read: skipped, not matched.
        lst = [("h", 443, proxy._EXACT, -1.0)]
        assert proxy._session_entry_ttl(lst, "h", 443) is None

    def test_rule_cap_skips_just_expired_allow(self, proxy, monkeypatch):
        # Same race on the cap path: an expired session allow contributes no
        # cap (None -> the DNS TTL is used).
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setattr(
            proxy.allowlist.time,
            "time",
            lambda: 100.0,
        )
        # Entry expires at 100.5 -- survives the prune's read at 100.0...
        proxy.state._SESSION_HOST_ALLOWS[:] = [("h", 443, proxy._EXACT, 100.5)]
        # ...but the cap loop's read happens "later": advance the clock.
        clock = iter([100.0, 101.0])
        monkeypatch.setattr(proxy.allowlist.time, "time", lambda: next(clock))
        try:
            assert proxy._session_allow_rule_cap("h") is None
        finally:
            proxy.state._SESSION_HOST_ALLOWS[:] = []


class TestPacketHelpers:
    """packets.py edge outcomes (#2834): non-IPv4/short payloads, the
    odd-length checksum pad, and the RST socket probe."""

    def test_ipv4_offsets_rejects_non_ipv4(self, proxy):
        assert proxy.packets._ipv4_offsets(b"\x55" + b"\x00" * 30) is None

    def test_ipv4_offsets_rejects_short_payload(self, proxy):
        assert proxy.packets._ipv4_offsets(b"\x45\x00") is None

    def test_ones_checksum_pads_odd_length(self, proxy):
        # RFC 1071: odd data is zero-padded; 3 bytes behave as 4.
        assert proxy.packets._ones_checksum(b"\x01\x02\x03") == (
            proxy.packets._ones_checksum(b"\x01\x02\x03\x00")
        )

    def test_check_rst_socket_opens_hdrincl_socket(self, proxy, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(proxy.packets.socket, "socket", lambda *a, **k: fake)
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
        try:
            proxy.packets.check_rst_socket()
            assert proxy.packets._RST_SOCK is fake
            fake.setblocking.assert_called_once_with(False)
        finally:
            monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)

    def test_check_rst_socket_degrades_without_net_raw(
        self, proxy, monkeypatch, capsys
    ):
        # Best-effort: no raw socket -> REJECT-only fast-refuse, logged.
        def _boom(*a, **k):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(proxy.packets.socket, "socket", _boom)
        monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)
        try:
            proxy.packets.check_rst_socket()  # must not raise
            assert "cannot open RST socket" in capsys.readouterr().out
        finally:
            monkeypatch.setattr(proxy.packets, "_RST_SOCK", None)


class TestBranchArcs:
    """The remaining single-arc gaps (#2834): each false/negative outcome of
    a guard the mainline tests only ever take the true side of."""

    # --- allowlist.parse_specs grammar edges ---

    def test_parse_specs_non_digit_port_is_host_part(self, proxy, monkeypatch):
        # "host:x" -- a non-digit port is not a port; the spec is kept
        # verbatim as the host (an all-ports EXACT entry).
        monkeypatch.setattr(proxy.allowlist, "SPECS", [])
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_ALLOW", "example.com:x")
        assert proxy.parse_specs() == [("example.com:x", None, proxy._EXACT)]

    def test_parse_specs_empty_after_strip_is_skipped(self, proxy, monkeypatch):
        # "." reduces to the empty host (INCLUSIVE strip) -> skipped; later
        # specs still parse.
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_ALLOW", ".,ok.com")
        assert proxy.parse_specs() == [("ok.com", None, proxy._EXACT)]

    def test_entry_ttl_keeps_max_across_matching_entries(self, proxy):
        # Two matching entries: the second (smaller) remaining TTL must not
        # lower the best -- the max is what gates on.
        now = time.time()
        lst = [
            ("h", 443, proxy._EXACT, now + 100),
            ("h", 443, proxy._EXACT, now + 50),
        ]
        assert 99 < proxy._session_entry_ttl(lst, "h", 443) <= 100

    # --- nfqueue port-0 (non-TCP) outcomes ---

    async def test_fail_fast_no_consent_non_tcp_just_drops(self, proxy, monkeypatch):
        # No port to RST/reject (non-TCP): the plain drop still fails fast
        # for the caller via the connection error, not a kernel retransmit.
        pkt = MagicMock()
        proxy.nfqueue._fail_fast_no_consent(pkt, b"payload", "10.2.3.4", 0)
        pkt.drop.assert_called_once()

    def test_apply_cached_deny_non_tcp_skips_rst(self, proxy):
        # A cached deny for a non-TCP flow: no RST to forge, just drop.
        pkt = MagicMock()
        proxy.nfqueue._apply_cached_verdict(pkt, ("deny", time.time() + 60), b"p", 0)
        pkt.drop.assert_called_once()

    async def test_deny_session_host_non_tcp_just_drops(self, proxy, monkeypatch):
        pkt = MagicMock()
        proxy.state._VERDICT_CACHE.clear()
        try:
            proxy.nfqueue._deny_session_host(
                pkt,
                ("s", 0, "d", 0),
                b"payload",
                "10.2.3.4",
                0,
                time.time(),
                30.0,
            )
            pkt.drop.assert_called_once()
        finally:
            proxy.state._VERDICT_CACHE.clear()

    def test_cb_without_client_drops(self, proxy, monkeypatch):
        # Pure-static mode (no consent configured): every queued SYN drops
        # (nothing to prompt; the allow-list ACCEPTs sit above NFQUEUE).
        pkt = MagicMock()
        pkt.get_payload = MagicMock(
            return_value=_syn_payload("172.16.0.9", 40000, "10.2.3.4", 443, 7)
        )
        proxy.nfqueue._cb(pkt, None)
        pkt.drop.assert_called_once()

    def test_remember_session_verdict_unknown_decision_adds_nothing(self, proxy):
        # Neither allow nor deny (a future/unknown verdict token): no host
        # memory is installed.
        proxy.state._SESSION_HOST_ALLOWS.clear()
        proxy.state._SESSION_HOST_DENIES.clear()
        try:
            proxy.nfqueue._remember_session_verdict("bogus", "h", 443, 300.0)
            assert not proxy.state._SESSION_HOST_ALLOWS
            assert not proxy.state._SESSION_HOST_DENIES
        finally:
            proxy.state._SESSION_HOST_ALLOWS.clear()
            proxy.state._SESSION_HOST_DENIES.clear()

    # --- packets IHL edge ---

    def test_ipv4_offsets_rejects_bad_ihl(self, proxy):
        # Version 4 but IHL < 5 (a malformed header): unparseable.
        assert proxy.packets._ipv4_offsets(b"\x41" + b"\x00" * 30) is None

    # --- consent client remaining arcs ---

    async def test_run_debug_prints_connected_line(self, proxy, monkeypatch, capsys):
        c = TestConsentClientRunLoop._client(proxy)
        monkeypatch.setattr(c, "_read_token", lambda: "tok")
        monkeypatch.setattr(proxy.consent, "DEBUG", True)
        ws = _FakeConsentWS(frames=[])

        async def _stop_after_connect():
            c._stop = True

        # Stop inside aiter exhaustion via a dispatch hook is unnecessary
        # here: zero frames -> the async for ends at once.
        monkeypatch.setattr(proxy.consent.websockets, "connect", lambda *a, **k: ws)
        real_sleep = asyncio.sleep

        async def _fast_sleep(s):
            if s >= 1.0:  # the reconnect backoff, not a tick
                c._stop = True
            await real_sleep(0)

        monkeypatch.setattr(proxy.consent.asyncio, "sleep", _fast_sleep)
        await c._run()
        assert "consent: connected to" in capsys.readouterr().out

    async def test_run_connect_error_without_debug(self, proxy, monkeypatch, capsys):
        # The exception path with DEBUG off: silent retry (no type leak).
        c = TestConsentClientRunLoop._client(proxy)
        monkeypatch.setattr(c, "_read_token", lambda: "tok")
        monkeypatch.setattr(
            proxy.consent.websockets,
            "connect",
            lambda *a, **k: (_ for _ in ()).throw(OSError("down")),
        )
        real_sleep = asyncio.sleep
        sleeps = {"n": 0}

        async def _fast_sleep(s):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                c._stop = True
            await real_sleep(0)

        monkeypatch.setattr(proxy.consent.asyncio, "sleep", _fast_sleep)
        await c._run()
        assert "connection error" not in capsys.readouterr().out

    async def test_handle_drop_rule_unknown_decision_skips_drop(
        self, proxy, monkeypatch
    ):
        # A well-formed host with an unknown decision token: no drop call,
        # no session-memory clear -- just the not-ok ack.
        c = TestConsentClientRunLoop._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock()
        c._ws = ws
        dropped = []
        monkeypatch.setattr(
            proxy.consent.rules, "drop_for_host", lambda h, d: dropped.append(h)
        )
        await c._handle_drop_rule(
            {"type": "drop_rule", "id": "r4", "host": "evil.test", "decision": "bogus"}
        )
        assert not dropped
        sent = json.loads(ws.send.await_args.args[0])
        assert sent["ok"] is False

    async def test_handle_drop_rule_without_ws_skips_ack(self, proxy, monkeypatch):
        # WS down when the revoke lands: the drop still runs (it is the
        # side effect), the ack is skipped.
        c = TestConsentClientRunLoop._client(proxy)
        c._ws = None
        dropped = []
        monkeypatch.setattr(
            proxy.consent.rules,
            "drop_for_host",
            lambda h, d: dropped.append(h) or set(),
        )
        await c._handle_drop_rule(
            {
                "type": "drop_rule",
                "id": "r5",
                "host": "evil.test",
                "decision": "allowed",
            }
        )  # must not raise on the skipped ack

    async def test_handle_drop_rule_success_acks_ok(self, proxy, monkeypatch):
        # The applied revoke acks ok=True exactly once the rules are gone.
        c = TestConsentClientRunLoop._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock()
        c._ws = ws
        monkeypatch.setattr(
            proxy.consent.rules, "drop_for_host", lambda h, d: {"1.2.3.4"}
        )
        await c._handle_drop_rule(
            {
                "type": "drop_rule",
                "id": "r6",
                "host": "evil.test",
                "decision": "allowed",
            }
        )
        sent = json.loads(ws.send.await_args.args[0])
        assert sent == {"type": "drop_ack", "id": "r6", "ok": True}

    def test_bump_activity_off_loop_is_noop_after_gate(self, proxy):
        # Past the connected+ws+flood gates with no running loop: silent
        # return (the NFQUEUE callback is sync; defensive only). The last
        # activity send is forced an hour back so the flood gate passes
        # deterministically even on a freshly-booted runner whose
        # monotonic clock is younger than the gate window.
        c = TestConsentClientRunLoop._client(proxy)
        c._connected.set()
        c._ws = MagicMock()  # past the first guard
        c._last_activity_send = time.monotonic() - 3600.0
        c.bump_activity()  # no running loop -> nothing scheduled

    async def test_fail_close_skips_done_futures(self, proxy):
        # An already-resolved pending (a verdict raced the disconnect) is
        # left alone; only the still-open one is fail-closed.
        c = proxy.consent.SidecarConsentClient(
            "http://klangkd/ws", "/nonexistent-token", 60.0
        )
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_result(("deny", "once"))
        open_ = loop.create_future()
        c._pending["a"] = done
        c._pending["b"] = open_
        c._fail_close_pending()
        assert open_.result() == ("deny", "once")
        assert not c._pending

    async def test_send_activity_success(self, proxy):
        c = TestConsentClientRunLoop._client(proxy)
        ws = MagicMock()
        ws.send = AsyncMock()
        c._ws = ws
        await c._send_activity()
        ws.send.assert_awaited_once_with(json.dumps({"type": "activity"}))
