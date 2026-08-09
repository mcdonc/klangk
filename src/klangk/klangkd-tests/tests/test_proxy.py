"""Unit tests for the network sidecar's DNS proxy (``src/containers/network/proxy.py``).

``proxy.py`` is a standalone sidecar script — it lives under
``src/containers/network/`` (NOT in the ``klangk`` package, so it is not
coverage-gated) and is normally only exercised end-to-end by the real-podman
e2e. These tests import it as a module and drive its helpers directly.
``main()`` only runs under ``__name__ == "__main__"``, so importing is safe
(module level just reads env vars + builds the allow-list).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

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
    (a_records etc.), so the stubs only need to exist, not work.
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


class TestAllowedGate:
    """The allow/deny gate (``allowed``) is the core security check. A
    suffix-match regression here (e.g. a bare ``endswith(h)``) would wrongly
    admit ``evilgithub.com`` for an allow-listed ``github.com``. Pin the exact /
    subdomain / boundary semantics so a refactor can't silently weaken it (only
    the real-podman e2e covered it before)."""

    @pytest.mark.parametrize(
        "qname, expected",
        [
            ("github.com", True),  # exact match
            ("api.github.com", True),  # subdomain matches
            ("a.b.c.github.com", True),  # deep subdomain matches
            ("evilgithub.com", False),  # boundary: NOT a subdomain
            ("notgithub.com", False),  # boundary
            ("github.com.attacker.test", False),  # prefix-of, not a suffix
            ("evil.test", False),  # unrelated
        ],
    )
    def test_suffix_boundary(self, proxy, monkeypatch, qname, expected):
        # The dot boundary ("." + h) is what stops evilgithub.com matching
        # github.com. A bare endswith(h) would flip the False cases to True.
        monkeypatch.setattr(proxy, "ALLOWED", ["github.com"])
        assert proxy.allowed(qname) == expected

    def test_multiple_specs_any_match(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "ALLOWED", ["github.com", "pypi.org"])
        assert proxy.allowed("api.github.com") is True
        assert proxy.allowed("files.pythonhosted.org") is False
        assert proxy.allowed("github.com") is True

    def test_empty_allow_list_denies_all(self, proxy, monkeypatch):
        monkeypatch.setattr(proxy, "ALLOWED", [])
        assert proxy.allowed("anything.test") is False
        assert proxy.allowed("github.com") is False

    def test_case_sensitive_relies_on_caller_lowercasing(
        self, proxy, monkeypatch
    ):
        # allowed() compares verbatim; query_name()/host_specs() lowercase at
        # the edges. Pin that contract: a mixed-case qname does NOT match a
        # lowercased spec, so the lowercasing must not be dropped upstream.
        monkeypatch.setattr(proxy, "ALLOWED", ["github.com"])
        assert proxy.allowed("GitHub.Com") is False
        assert proxy.allowed("API.GITHUB.COM") is False


class TestRespondAllowedSwallowsFailures:
    """#2278: a transient failure in allow_ip or sendto must drop only the one
    response, not kill the proxy (an escaped raise would take down PID 1,
    leaving learned ACCEPT rules in place with DNS dead — a partial
    fail-open)."""

    def test_allow_ip_failure_does_not_propagate(self, proxy, monkeypatch):
        # allow_ip shells out to iptables; a transient failure there must not
        # escape _respond_allowed.
        monkeypatch.setattr(proxy, "a_records", lambda wire: ["1.2.3.4"])

        def _boom(ip):
            raise RuntimeError("iptables transient failure")

        monkeypatch.setattr(proxy, "allow_ip", _boom)
        s = MagicMock()
        # Must not raise.
        proxy._respond_allowed(s, b"resp", ("127.0.0.1", 1234), "allowed.test")

    def test_sendto_failure_does_not_propagate(self, proxy, monkeypatch):
        # sendto to a vanished client must not escape _respond_allowed.
        monkeypatch.setattr(proxy, "a_records", lambda wire: ["1.2.3.4"])
        monkeypatch.setattr(proxy, "allow_ip", lambda ip: None)  # no iptables
        s = MagicMock()
        s.sendto.side_effect = OSError("client gone")
        # Must not raise.
        proxy._respond_allowed(s, b"resp", ("127.0.0.1", 1234), "allowed.test")

    def test_happy_path_still_sends(self, proxy, monkeypatch):
        # Sanity: with no failure, _respond_allowed learns the IPs + sends.
        monkeypatch.setattr(
            proxy, "a_records", lambda wire: ["1.2.3.4", "5.6.7.8"]
        )
        learned = []
        monkeypatch.setattr(proxy, "allow_ip", learned.append)
        s = MagicMock()
        proxy._respond_allowed(s, b"resp", ("127.0.0.1", 1234), "allowed.test")
        assert learned == ["1.2.3.4", "5.6.7.8"]
        s.sendto.assert_called_once_with(b"resp", ("127.0.0.1", 1234))
