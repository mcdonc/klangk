"""Tests for client-side mount spec validation."""

from klangk.cli.tui.screens.workspace_form import validate_env_entry
from klangk.cli.mount import (
    validate_allowed_domain_spec,
    validate_mount_spec,
)


class TestValidateMountSpec:
    def test_valid_source_dest(self):
        assert validate_mount_spec("/host/path:/container/path") is None

    def test_valid_with_options(self):
        assert validate_mount_spec("/host:/container:ro") is None
        assert validate_mount_spec("/host:/container:ro,z") is None

    def test_valid_named_volume(self):
        assert validate_mount_spec("myvolume:/data") is None

    def test_too_few_parts(self):
        result = validate_mount_spec("nocolon")
        assert result is not None
        assert "expected source:dest" in result

    def test_too_many_parts(self):
        result = validate_mount_spec("a:b:c:d")
        assert result is not None
        assert "expected source:dest" in result

    def test_empty_source(self):
        result = validate_mount_spec(":/container")
        assert result is not None
        assert "source is empty" in result

    def test_relative_dest(self):
        result = validate_mount_spec("/host:relative")
        assert result is not None
        assert "must be absolute" in result

    def test_unknown_option(self):
        result = validate_mount_spec("/host:/container:bogus")
        assert result is not None
        assert "unknown option" in result


class TestValidateEnvEntry:
    def test_valid_key_value(self):
        assert validate_env_entry("FOO=bar") is None

    def test_valid_empty_value(self):
        assert validate_env_entry("EMPTY=") is None

    def test_value_may_contain_equals(self):
        # only the first '=' splits key from value
        assert validate_env_entry("PATH=/usr:/bin") is None

    def test_missing_equals(self):
        result = validate_env_entry("NOEQUALS")
        assert result is not None
        assert "KEY=VALUE" in result

    def test_empty_key(self):
        result = validate_env_entry("=val")
        assert result is not None
        assert "key cannot be empty" in result


class TestValidateAllowedDomainSpec:
    def test_valid_host(self):
        assert validate_allowed_domain_spec("github.com") is None

    def test_valid_host_port(self):
        assert validate_allowed_domain_spec("github.com:443") is None
        assert validate_allowed_domain_spec("pypi.org:80") is None

    def test_valid_ipv4(self):
        assert validate_allowed_domain_spec("10.0.0.1") is None
        assert validate_allowed_domain_spec("10.0.0.1:53") is None

    def test_valid_cidr(self):
        # #1935: IPv4 CIDR ranges (with and without a port scope) are
        # accepted client-side, mirroring the server.
        assert validate_allowed_domain_spec("10.0.0.0/8") is None
        assert validate_allowed_domain_spec("10.0.0.0/8:443") is None
        assert validate_allowed_domain_spec("192.168.0.0/16") is None
        assert validate_allowed_domain_spec("172.16.0.0/12:80") is None
        assert validate_allowed_domain_spec("203.0.113.5/32") is None

    def test_rejects_ipv6_bracket_literals(self):
        # IPv6 is disabled inside filtered containers (#1936), so bracketed
        # v6 literals are no longer accepted.
        assert validate_allowed_domain_spec("[::1]") is not None
        assert validate_allowed_domain_spec("[2001:db8::1]:443") is not None
        assert validate_allowed_domain_spec("[::1]:443") is not None

    def test_rejects_empty(self):
        assert validate_allowed_domain_spec("") is not None
        assert "empty" in validate_allowed_domain_spec("")
        assert validate_allowed_domain_spec("   ") is not None

    def test_rejects_whitespace(self):
        assert validate_allowed_domain_spec("bad spec") is not None

    def test_rejects_bad_cidr(self):
        # #1935: a slash routes to the CIDR check; a malformed CIDR is
        # rejected with a precise message.
        assert validate_allowed_domain_spec("10.0.0.0/33") is not None
        assert validate_allowed_domain_spec("10.0.0.0/") is not None
        assert validate_allowed_domain_spec("10.0.0.0/abc") is not None
        assert validate_allowed_domain_spec("a.com/path") is not None
        # IPv6 CIDRs are rejected (v6 disabled in containers, #1936).
        assert validate_allowed_domain_spec("2001:db8::/32") is not None

    def test_rejects_cidr_bad_port(self):
        # #1935: a CIDR scoped to an invalid port is rejected.
        assert validate_allowed_domain_spec("10.0.0.0/8:abc") is not None
        assert validate_allowed_domain_spec("10.0.0.0/8:99999") is not None

    def test_rejects_non_numeric_port(self):
        assert validate_allowed_domain_spec("a.com:abc") is not None

    def test_strips_whitespace(self):
        assert validate_allowed_domain_spec("  github.com:443  ") is None
        assert validate_allowed_domain_spec("  10.0.0.0/8  ") is None

    def test_allow_cidr_false_rejects_cidr_for_rejected_domains(self):
        # #2386: rejected_domains is name-level (NXDOMAIN), so a CIDR is
        # meaningless and is rejected up front when allow_cidr=False.
        err = validate_allowed_domain_spec("10.0.0.0/8", allow_cidr=False)
        assert err is not None
        assert "CIDR" in err
        assert validate_allowed_domain_spec("10.0.0.0/8:443", allow_cidr=False)
        # A plain host / host:port is still accepted.
        assert (
            validate_allowed_domain_spec("evil.example.com", allow_cidr=False)
            is None
        )
        assert (
            validate_allowed_domain_spec(
                "evil.example.com:443", allow_cidr=False
            )
            is None
        )

    def test_allow_cidr_default_true_unchanged(self):
        # The default (allow) still accepts CIDRs.
        assert validate_allowed_domain_spec("10.0.0.0/8") is None
