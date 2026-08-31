"""Tests for the per-workspace netfilter egress-filter surface (#1365).

The hook model was superseded by the FQDN network sidecar (#2255); these
tests cover the pure validators, the host-resolver detection the sidecar's
proxy forwards through, and the slim :class:`NetFilter` settings surface
(``enabled`` / ``default_domains`` / ``resolvers``). The sidecar's own
ruleset is exercised end-to-end in ``test_network_sidecar_e2e.py``.
"""

import types

import pytest

from klangk import netfilter as nf
from _helpers import make_settings


def _app(
    default_domains=None, enabled=True, sidecar_image="klangk-network-sidecar"
):
    settings = make_settings({})
    # netfilter_enabled is the master switch; network_sidecar_image arms the
    # sidecar model. enabled() requires both.
    settings.netfilter_enabled = enabled
    settings.network_sidecar_image = sidecar_image
    if default_domains is not None:
        settings.netfilter_default_domains = default_domains
    return types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )


# --- pure validators ---


class TestParseAllowedDomains:
    def test_strips_and_dedupes_preserving_order(self):
        assert nf.parse_allowed_domains(
            ["github.com:443", " github.com:443 ", "pypi.org"]
        ) == ["github.com:443", "pypi.org"]

    def test_drops_empties(self):
        assert nf.parse_allowed_domains(["", "  ", "a.com"]) == ["a.com"]

    def test_cidr_dedupes_and_coexists_with_hosts(self):
        # #1935: CIDR specs dedup on the literal string (a CIDR kept as-typed
        # — no normalization), and a CIDR + host list parses in order.
        assert nf.parse_allowed_domains(
            ["10.0.0.0/8", "github.com:443", "10.0.0.0/8", "pypi.org"]
        ) == ["10.0.0.0/8", "github.com:443", "pypi.org"]

    @pytest.mark.parametrize(
        "spec",
        [
            "github.com",
            "github.com:443",
            "pypi.org:80",
            "10.0.0.1",
            "10.0.0.1:53",
            "sub.domain.example.com:8080",
            "a.com:65535",  # max valid port
            # #2256: leading "*." wildcards (subdomains only, + optional port).
            "*.pypi.org",
            "*.pypi.org:443",
            "*.double.example.com",  # wildcard on a multi-label base
            "*.pypi.org:65535",  # wildcard + max port
            # #2377: leading "." inclusive scope (apex + subdomains).
            ".pypi.org",
            ".pypi.org:443",
            ".double.example.com",
            # #1935: IPv4 CIDR ranges (with and without a port scope).
            "10.0.0.0/8",
            "10.0.0.0/8:443",
            "192.168.0.0/16",
            "172.16.0.0/12:80",
            "10.20.30.0/24:65535",  # CIDR scoped to max port
            "203.0.113.5/32",  # single-host CIDR (/32)
            "10.5.0.0/8",  # host bits set — accepted (iptables masks them)
        ],
    )
    def test_valid_specs(self, spec):
        assert nf.parse_allowed_domains([spec]) == [spec]

    @pytest.mark.parametrize(
        "spec",
        [
            "bad spec",  # whitespace
            "a.com:abc",  # non-numeric port
            "a.com:123456",  # port too long (>5 digits)
            "a.com:99999",  # port > 65535
            "[::1]",  # IPv6 literal — IPv6 disabled in containers (#1936)
            "[2001:db8::1]:443",  # bracketed IPv6 — no longer accepted
            "[::1]:70000",  # bracketed IPv6, port > 65535
            "/etc/passwd",  # slash but not a valid CIDR
            "-leading",  # leading hyphen rejected by host grammar
            "a.com/path",  # slash but not a valid CIDR
            # #2256: wildcards must be a leading "*." (dot required), and
            # a wildcard with no matchable base is invalid.
            "*pypi.org",  # missing the dot after *
            "*",  # bare wildcard — nothing to match
            "*.",  # wildcard with empty base
            ".",  # inclusive (leading dot) with empty base
            "a*.pypi.org",  # wildcard not at the leading position
            "pypi.org.*",  # wildcard at the wrong end
            # #1935: malformed CIDR specs.
            "10.0.0.0/33",  # prefix length > 32
            "10.0.0.0/",  # missing prefix length
            "10.0.0.0/abc",  # non-numeric prefix
            "10.0.0.0/-1",  # negative prefix
            "10.0.0.0/8:abc",  # CIDR with non-numeric port
            "10.0.0.0/8:99999",  # CIDR with port > 65535
            "10.0.0.0/8:70000",  # CIDR with port > 65535
            "2001:db8::/32",  # IPv6 CIDR — v6 disabled in containers (#1936)
            "not.a.cidr/24",  # slash but the IP literal is garbage
        ],
    )
    def test_invalid_specs_rejected(self, spec):
        with pytest.raises(ValueError):
            nf.parse_allowed_domains([spec])

    def test_error_lists_every_invalid_entry(self):
        with pytest.raises(ValueError) as exc:
            nf.parse_allowed_domains(["good.com", "bad spec", "also bad"])
        msg = str(exc.value)
        assert "bad spec" in msg
        assert "also bad" in msg
        assert "good.com" not in msg.split("Invalid")[1]

    # #1935 review: leading-zero handling must match Python's ipaddress
    # (which rejects leading-zero OCTETS to avoid octal ambiguity but
    # accepts leading-zero PREFIX lengths). These are the cases that
    # distinguish a faithful mirror from a hand-rolled regex.
    @pytest.mark.parametrize(
        "spec,valid",
        [
            ("010.0.0.0/8", False),  # leading-zero octet -> reject
            ("00.0.0.0/8", False),  # leading-zero octet -> reject
            ("10.0.0.0/08", True),  # leading-zero prefix -> accept (8)
            ("10.0.0.0/00", True),  # leading-zero prefix -> accept (0)
            ("0.0.0.0/0", True),  # allow-all is valid (warned, not rejected)
            ("0.0.0.0", True),  # bare zero octets are fine (no leading zero)
        ],
    )
    def test_leading_zero_handling_matches_ipaddress(self, spec, valid):
        if valid:
            assert nf.parse_allowed_domains([spec]) == [spec]
        else:
            with pytest.raises(ValueError):
                nf.parse_allowed_domains([spec])

    def test_allow_all_cidr_warns_but_accepted(self, caplog):
        # #1935 review: a /0 CIDR matches all IPv4 (effectively disabling
        # the filter). It is valid (not rejected) but earns a loud warning
        # so an operator can't stumble into it silently.
        with caplog.at_level("WARNING"):
            result = nf.parse_allowed_domains(["0.0.0.0/0", "github.com:443"])
        assert result == ["0.0.0.0/0", "github.com:443"]
        assert any("/0 CIDR" in r.message for r in caplog.records)

    def test_allow_all_cidr_warning_covers_host_bits(self, caplog):
        # 10.5.0.0/0 normalizes to 0.0.0.0/0 (prefixlen 0) — the warning
        # must fire for any /0 form, not just the canonical 0.0.0.0/0.
        with caplog.at_level("WARNING"):
            nf.parse_allowed_domains(["10.5.0.0/0:443"])
        assert any("/0 CIDR" in r.message for r in caplog.records)

    def test_non_allow_all_cidr_does_not_warn(self, caplog):
        # A normal CIDR (and a host spec) emits no /0 warning.
        with caplog.at_level("WARNING"):
            nf.parse_allowed_domains(["10.0.0.0/8", "github.com:443"])
        assert not any("/0 CIDR" in r.message for r in caplog.records)


# --- host resolver detection (the sidecar's proxy upstream) ---


class TestDetectHostResolvers:
    """Host DNS-resolver detection the sidecar's proxy forwards through (#1365)."""

    def test_is_ipv4_classifies(self):
        assert nf.is_ipv4("1.2.3.4")
        assert nf.is_ipv4("10.0.0.1")
        assert not nf.is_ipv4("::1")  # IPv6 excluded
        assert not nf.is_ipv4("host")  # not an address
        assert not nf.is_ipv4("")

    def test_nameservers_parses_ipv4_only(self, tmp_path):
        r = tmp_path / "resolv.conf"
        r.write_text(
            "# comment\n"
            "nameserver 1.1.1.1\n"
            "nameserver ::1\n"  # IPv6 -> skipped
            "nameserver 8.8.8.8\n"
            "search example.com\n"
        )
        assert nf.nameservers(str(r)) == ["1.1.1.1", "8.8.8.8"]

    def test_nameservers_missing_file_returns_empty(self, tmp_path):
        assert nf.nameservers(str(tmp_path / "nope")) == []

    def test_stub_uses_upstream(self, monkeypatch):
        def fake(path):
            return (
                ["127.0.0.53"]
                if path == "/etc/resolv.conf"
                else ["1.1.1.1", "8.8.8.8"]
            )

        monkeypatch.setattr(nf, "nameservers", fake)
        assert nf.detect_host_resolvers() == ["1.1.1.1", "8.8.8.8"]

    def test_stub_but_no_upstream_falls_back_to_empty(self, monkeypatch):
        # systemd-resolved stub present but the upstream file is empty/
        # missing: the stub (127.0.0.53) is filtered out -> no resolver.
        def fake(path):
            return ["127.0.0.53"] if path == "/etc/resolv.conf" else []

        monkeypatch.setattr(nf, "nameservers", fake)
        assert nf.detect_host_resolvers() == []

    def test_no_stub_returns_primary_minus_loopback(self, monkeypatch):
        monkeypatch.setattr(
            nf,
            "nameservers",
            lambda path: (
                ["1.1.1.1", "127.0.1.1", "8.8.8.8"]
                if path == "/etc/resolv.conf"
                else []
            ),
        )
        assert nf.detect_host_resolvers() == ["1.1.1.1", "8.8.8.8"]

    def test_dedup_preserve_order(self, monkeypatch):
        monkeypatch.setattr(
            nf,
            "nameservers",
            lambda path: (
                ["1.1.1.1", "8.8.8.8", "1.1.1.1"]
                if path == "/etc/resolv.conf"
                else []
            ),
        )
        assert nf.detect_host_resolvers() == ["1.1.1.1", "8.8.8.8"]


# --- NetFilter state object ---


class TestNetFilterDefaultDomains:
    def test_unset_returns_empty(self):
        assert nf.NetFilter(_app()).default_domains() == []

    def test_returns_settings_list(self):
        # The field is already validated + de-duped at construction; this
        # just surfaces it (and returns a copy so callers can't mutate it).
        nf_obj = nf.NetFilter(_app(default_domains=["b.io", "a.com:443"]))
        assert nf_obj.default_domains() == ["b.io", "a.com:443"]
        # Mutating the returned list does not leak into settings.
        got = nf_obj.default_domains()
        got.append("evil.io")
        assert nf_obj.default_domains() == ["b.io", "a.com:443"]

    def test_reflects_reloaded_settings(self):
        # reconfigure() points at a new app/state; the next read sees the
        # new settings (no stale cache).
        nf_obj = nf.NetFilter(_app(default_domains=["a.com"]))
        assert nf_obj.default_domains() == ["a.com"]
        nf_obj.reconfigure(_app(default_domains=["b.com"]))
        assert nf_obj.default_domains() == ["b.com"]


class TestNetFilterResolvers:
    def test_delegates_to_detect(self, monkeypatch):
        monkeypatch.setattr(
            nf, "detect_host_resolvers", lambda: ["1.1.1.1", "8.8.8.8"]
        )
        assert nf.NetFilter(_app()).resolvers() == ["1.1.1.1", "8.8.8.8"]

    def test_empty_when_none_detected(self, monkeypatch):
        monkeypatch.setattr(nf, "detect_host_resolvers", lambda: [])
        assert nf.NetFilter(_app()).resolvers() == []


class TestNetFilterEnabled:
    """enabled() = master switch on AND sidecar image configured (#2255)."""

    def test_disabled_when_master_switch_off(self):
        assert nf.NetFilter(_app(enabled=False)).enabled() is False

    def test_disabled_when_sidecar_image_unset(self):
        assert nf.NetFilter(_app(sidecar_image="")).enabled() is False

    def test_enabled_when_sidecar_configured(self):
        assert nf.NetFilter(_app()).enabled() is True
