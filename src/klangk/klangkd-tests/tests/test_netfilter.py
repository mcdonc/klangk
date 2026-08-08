"""Tests for the per-workspace netfilter egress-filter surface (#1365)."""

import json
import os
import shutil
import stat
import subprocess
import sys
import types
from unittest import mock

import pytest

from klangk import netfilter as nf
from _helpers import make_settings


def _app(hooks_dir=None, default_domains=None, enabled=True, tmp_path=None):
    settings = make_settings({})
    # #1774: netfilter_enabled defaults True (the production default); pass
    # enabled=False to exercise the master-switch-off path.
    settings.netfilter_enabled = enabled
    if hooks_dir is not None:
        settings.netfilter_hooks_dir = hooks_dir
    if default_domains is not None:
        settings.netfilter_default_domains = default_domains
    return types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )


# --- pure validators / renderers ---


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


class TestRenderRulesAnnotation:
    def test_comma_joined(self):
        assert (
            nf.render_rules_annotation(["github.com:443", "pypi.org"])
            == "github.com:443,pypi.org"
        )

    def test_single(self):
        assert nf.render_rules_annotation(["a.com"]) == "a.com"


class TestRenderHookJson:
    def test_points_at_absolute_script_and_gates_on_annotation(self, tmp_path):
        script = str(tmp_path / "klangk-netfilter.sh")
        data = json.loads(nf.render_hook_json(script))
        assert data["hook"]["path"] == os.path.abspath(script)
        assert data["stages"] == ["createContainer"]
        # The annotations gate makes the hook fire ONLY for containers
        # that carry the annotation — an unrestricted workspace is never
        # filtered.
        assert nf.ANNOTATION_KEY in data["annotations"]


# --- NetFilter state object ---


class TestNetFilterHooksDir:
    def test_disabled_via_setting_returns_none(self):
        # #1774: netfilter_enabled=False fully disables — no hooks dir.
        assert nf.NetFilter(_app(enabled=False)).hooks_dir() is None

    def test_unset_hooks_dir_defaults_to_state_dir_subdir(self):
        # #1774: with netfilter enabled (the default) and no explicit hooks
        # dir, it resolves to <state_dir>/oci-hooks.
        app = _app()
        state_dir = app.state.settings.state_dir
        assert nf.NetFilter(app).hooks_dir() == os.path.realpath(
            os.path.join(state_dir, nf.DEFAULT_HOOKS_SUBDIR)
        )

    def test_creates_missing_dir(self, tmp_path):
        path = str(tmp_path / "nested" / "hooks")
        assert nf.NetFilter(
            _app(hooks_dir=path)
        ).hooks_dir() == os.path.realpath(path)
        assert os.path.isdir(path)

    def test_unwritable_returns_none(self, tmp_path, monkeypatch):
        path = str(tmp_path / "hooks")
        settings = make_settings({})
        settings.netfilter_hooks_dir = path

        def boom(*a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(os, "makedirs", boom)
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=settings)
        )
        assert nf.NetFilter(app).hooks_dir() is None


class TestNetFilterInstallHooks:
    def test_disabled_is_noop(self):
        # #1774: netfilter_enabled=False -> install_hooks is a noop.
        assert nf.NetFilter(_app(enabled=False)).install_hooks() is None

    def test_writes_script_and_json(self, tmp_path, monkeypatch):
        # On macOS, install_hooks() tries to SSH into the podman VM;
        # force Linux so the local-only path is exercised (#1983).
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        path = str(tmp_path / "hooks")
        installed = nf.NetFilter(_app(hooks_dir=path)).install_hooks()
        assert installed == os.path.realpath(path)
        script = os.path.join(path, nf.HOOK_SCRIPT_NAME)
        jsonf = os.path.join(path, nf.HOOK_JSON_NAME)
        assert os.path.isfile(script)
        # Executable so the OCI runtime can invoke it.
        mode = stat.S_IMODE(os.lstat(script).st_mode)
        assert mode & 0o111
        with open(script) as f:
            assert "klangk.netfilter.rules" in f.read()
        with open(jsonf) as f:
            data = json.load(f)
        assert data["hook"]["path"] == os.path.abspath(script)

    def test_idempotent(self, tmp_path, monkeypatch):
        # On macOS, install_hooks() tries to SSH into the podman VM;
        # force Linux so the local-only path is exercised (#1983).
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()
        # Second call re-writes without error.
        assert nf_obj.install_hooks() == os.path.realpath(path)

    def test_write_failure_returns_none(self, tmp_path, monkeypatch):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        original_open = open

        def flaky_open(p, *a, **kw):
            if str(p).endswith(nf.HOOK_SCRIPT_NAME):
                raise OSError("disk full")
            return original_open(p, *a, **kw)

        monkeypatch.setattr("builtins.open", flaky_open)
        assert nf_obj.install_hooks() is None


class TestNetFilterCreateKwargs:
    @pytest.fixture(autouse=True)
    def _isolate_host_resolvers(self, monkeypatch):
        # create_kwargs() detects the host's DNS resolvers; neutralize that
        # here so the annotation/dns assertions are host-independent. Tests
        # that exercise the resolver path re-patch with a known list.
        monkeypatch.setattr(nf, "_detect_host_resolvers", lambda: [])

    def test_no_domains_returns_all_none(self):
        assert nf.NetFilter(_app()).create_kwargs(None) == (
            None,
            None,
            None,
            None,
        )
        assert nf.NetFilter(_app()).create_kwargs([]) == (
            None,
            None,
            None,
            None,
        )

    def test_domains_disabled_via_setting_warns_and_fail_opens(self, caplog):
        # #1774: netfilter_enabled=False -> fail open with a loud warning.
        app = _app(enabled=False)
        with caplog.at_level("WARNING"):
            result = nf.NetFilter(app).create_kwargs(["github.com:443"])
        assert result == (None, None, None, None)
        assert any("UNRESTRICTED" in r.message for r in caplog.records)

    def test_domains_with_hooks_dir_returns_annotation_path_and_cap_drop(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        # Patch to Linux so install_hooks() doesn't try SSH and
        # create_kwargs() returns hooks_dirs (the Linux behavior this test
        # was written for; macOS-specific behavior is tested separately).
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()  # arm the hook so create_kwargs trusts the dir
        ann, hooks, cap_drop, _dns = nf_obj.create_kwargs(
            ["github.com:443", "pypi.org"]
        )
        assert ann == {nf.ANNOTATION_KEY: "github.com:443,pypi.org"}
        # #1770: the klangk hooks dir is followed by the standard default
        # hook dirs so --hooks-dir doesn't silently disable operator
        # createContainer hooks.
        assert hooks == [os.path.realpath(path), *nf.STANDARD_HOOK_DIRS]
        # A filtered container drops NET_ADMIN so the entrypoint can't
        # flush the ruleset (#1773).
        assert cap_drop == ["NET_ADMIN"]

    def test_workspace_overrides_deploy_default(self, tmp_path):
        # A non-empty workspace list replaces the default (no merge).
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(
            _app(hooks_dir=path, default_domains=["default.com", "a.io"])
        )
        nf_obj.install_hooks()
        ann, _, cap_drop, _dns = nf_obj.create_kwargs(["ws.com:443"])
        assert ann == {nf.ANNOTATION_KEY: "ws.com:443"}
        assert cap_drop == ["NET_ADMIN"]

    def test_empty_workspace_inherits_deploy_default(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(
            _app(hooks_dir=path, default_domains=["default.com", "a.io"])
        )
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()
        ann, hooks, _, _dns = nf_obj.create_kwargs(None)
        assert ann == {nf.ANNOTATION_KEY: "default.com,a.io"}
        assert hooks == [os.path.realpath(path), *nf.STANDARD_HOOK_DIRS]

        # Same for an explicit empty list (None and [] both inherit).
        ann2, _, _, _dns = nf_obj.create_kwargs([])
        assert ann2 == {nf.ANNOTATION_KEY: "default.com,a.io"}

    def test_default_present_but_netfilter_disabled_warns(self, caplog):
        app = _app(default_domains=["default.com"], enabled=False)
        with caplog.at_level("WARNING"):
            result = nf.NetFilter(app).create_kwargs(None)
        assert result == (None, None, None, None)
        assert any("UNRESTRICTED" in r.message for r in caplog.records)

    def test_configured_but_not_installed_fail_opens(self, tmp_path, caplog):
        # #1771: the hooks dir exists but the hook files were never written
        # (partial install_hooks failure). create_kwargs must NOT hand
        # podman the dir; it fails open with a distinct loud warning.
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        # hooks_dir() makedirs the dir, but no hook files are installed.
        with caplog.at_level("WARNING"):
            result = nf_obj.create_kwargs(["github.com:443"])
        assert result == (None, None, None, None)
        assert any(
            "not installed or is stale" in r.message for r in caplog.records
        )

    @pytest.mark.parametrize("fname", [nf.HOOK_SCRIPT_NAME, nf.HOOK_JSON_NAME])
    def test_stale_hook_files_fail_opens(self, tmp_path, caplog, fname):
        # #1771: either hook file stale (old version) — script OR json — the
        # content mismatch must be detected and treated as not-armed.
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()
        with open(os.path.join(path, fname), "w") as f:
            f.write("# stale old hook\n")
        with caplog.at_level("WARNING"):
            result = nf_obj.create_kwargs(["github.com:443"])
        assert result == (None, None, None, None)
        assert any(
            "not installed or is stale" in r.message for r in caplog.records
        )

    def test_resolvers_added_to_annotation_and_dns(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            nf, "_detect_host_resolvers", lambda: ["1.1.1.1", "8.8.8.8"]
        )
        nf_obj.install_hooks()
        ann, _hooks, _cap, dns = nf_obj.create_kwargs(["github.com:443"])
        assert ann[nf.ANNOTATION_RESOLVERS_KEY] == "1.1.1.1,8.8.8.8"
        assert dns == ["1.1.1.1", "8.8.8.8"]

    def test_no_resolvers_annotation_when_detection_empty(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()
        ann, _h, _c, dns = nf_obj.create_kwargs(["github.com:443"])
        assert nf.ANNOTATION_RESOLVERS_KEY not in ann
        assert dns == []


class TestDetectHostResolvers:
    """Host DNS-resolver detection for the egress hook (#1365)."""

    def test_is_ipv4_classifies(self):
        assert nf._is_ipv4("1.2.3.4")
        assert nf._is_ipv4("10.0.0.1")
        assert not nf._is_ipv4("::1")  # IPv6 excluded
        assert not nf._is_ipv4("host")  # not an address
        assert not nf._is_ipv4("")

    def test_nameservers_parses_ipv4_only(self, tmp_path):
        r = tmp_path / "resolv.conf"
        r.write_text(
            "# comment\n"
            "nameserver 1.1.1.1\n"
            "nameserver ::1\n"  # IPv6 -> skipped
            "nameserver 8.8.8.8\n"
            "search example.com\n"
        )
        assert nf._nameservers(str(r)) == ["1.1.1.1", "8.8.8.8"]

    def test_nameservers_missing_file_returns_empty(self, tmp_path):
        assert nf._nameservers(str(tmp_path / "nope")) == []

    def test_stub_uses_upstream(self, monkeypatch):
        def fake(path):
            return (
                ["127.0.0.53"]
                if path == "/etc/resolv.conf"
                else ["1.1.1.1", "8.8.8.8"]
            )

        monkeypatch.setattr(nf, "_nameservers", fake)
        assert nf._detect_host_resolvers() == ["1.1.1.1", "8.8.8.8"]

    def test_stub_but_no_upstream_falls_back_to_empty(self, monkeypatch):
        # systemd-resolved stub present but the upstream file is empty/
        # missing: the stub (127.0.0.53) is filtered out -> no resolver.
        def fake(path):
            return ["127.0.0.53"] if path == "/etc/resolv.conf" else []

        monkeypatch.setattr(nf, "_nameservers", fake)
        assert nf._detect_host_resolvers() == []

    def test_no_stub_returns_primary_minus_loopback(self, monkeypatch):
        monkeypatch.setattr(
            nf,
            "_nameservers",
            lambda path: (
                ["1.1.1.1", "127.0.1.1", "8.8.8.8"]
                if path == "/etc/resolv.conf"
                else []
            ),
        )
        assert nf._detect_host_resolvers() == ["1.1.1.1", "8.8.8.8"]

    def test_dedup_preserves_order(self, monkeypatch):
        monkeypatch.setattr(
            nf,
            "_nameservers",
            lambda path: (
                ["1.1.1.1", "8.8.8.8", "1.1.1.1"]
                if path == "/etc/resolv.conf"
                else []
            ),
        )
        assert nf._detect_host_resolvers() == ["1.1.1.1", "8.8.8.8"]


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


class TestNetFilterEnabled:
    def test_disabled_when_netfilter_enabled_false(self):
        # #1774: the master switch off -> not armed.
        assert nf.NetFilter(_app(enabled=False)).enabled() is False

    def test_enabled_when_installed(self, tmp_path):
        # #1771: armed requires the hook to be installed, not just the dir
        # configured.
        nf_obj = nf.NetFilter(_app(hooks_dir=str(tmp_path / "h")))
        nf_obj.install_hooks()
        assert nf_obj.enabled() is True

    def test_not_enabled_when_configured_but_not_installed(self, tmp_path):
        # The dir exists but no hook files -> not armed (#1771).
        assert (
            nf.NetFilter(_app(hooks_dir=str(tmp_path / "h"))).enabled()
            is False
        )

    def test_not_enabled_when_hook_files_stale(self, tmp_path):
        # Files present but content is stale -> not armed (#1771).
        path = str(tmp_path / "h")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()
        with open(os.path.join(path, nf.HOOK_SCRIPT_NAME), "w") as f:
            f.write("# stale\n")
        assert nf_obj.enabled() is False


# --- the OCI hook script, actually executed ---
#
# The hook's iptables ruleset IS the security enforcement, and a string-only
# assertion ("klangk.netfilter.rules" in f.read()) lets every argv-splitting
# and IPv6-parsing bug ship undetected (#1365 review: B1/B2 both escaped
# because HOOK_SCRIPT had zero executable coverage). These tests run it for
# real against synthetic OCI state with shimmed nsenter/iptables/getent.


def _state(
    rules,
    *,
    with_pid=True,
    egress_mode=None,
    container_id=None,
    resolvers=None,
):
    """Build synthetic OCI container state JSON for the hook.

    ``rules`` is the ``klangk.netfilter.rules`` annotation value, or ``None``
    to omit the annotation (early-exit path). ``with_pid=False`` omits ``pid``
    (the other early-exit path). Otherwise ``pid`` is the running process's
    id; the hook uses it only to read the container's /etc/hosts for the
    backend gateway (overridable via KLANGK_NETFILTER_HOSTS in tests).
    ``egress_mode`` sets the ``klangk.netfilter.egress_mode`` annotation
    (#2239). ``container_id`` sets the top-level ``id`` field (the
    authoritative container id the hook truncates to 12 chars for the NFLOG
    prefix). ``resolvers`` sets the ``klangk.netfilter.resolvers`` annotation
    (the comma-separated DNS IPs the hook allows on :53, #1365).
    """
    s = {}
    if container_id is not None:
        s["id"] = container_id
    else:
        # Default: a 64-char hex id (realistic podman container id).
        s["id"] = "abc123def456" + "0" * 52
    if with_pid:
        s["pid"] = os.getpid()
    if rules is not None:
        annotations = {nf.ANNOTATION_KEY: rules}
        if egress_mode is not None:
            annotations[nf.ANNOTATION_EGRESS_MODE_KEY] = egress_mode
        if resolvers is not None:
            annotations[nf.ANNOTATION_RESOLVERS_KEY] = resolvers
        s["annotations"] = annotations
    return json.dumps(s)


def _run_hook(
    tmp_path,
    state,
    getent_map=None,
    resolv=None,
    hosts=None,
    sysctl_rc=0,
):
    """Execute ``nf.HOOK_SCRIPT`` against ``state``; return recorded iptables
    invocations (each a ``list[str]`` of argv).

    ``nsenter``/``iptables``/``getent`` are shimmed on a prepended PATH dir
    so the hook runs without root or a real netns. ``getent_map`` maps a host
    to its resolved IPs (newline-separated via the shim); a host absent from
    the map resolves to itself (deterministic, and enough to test argv).
    ``resolv``/``hosts`` are the contents of the container's
    /etc/resolv.conf and /etc/hosts the hook reads (via env-var path
    overrides); both default to empty so per-destination assertions stay
    clean — the dedicated DNS/gateway tests pass content.
    """
    getent_map = getent_map or {}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "iptables.log"
    map_file = tmp_path / "getent.map"
    resolv_file = tmp_path / "resolv.conf"
    hosts_file = tmp_path / "hosts"

    map_file.write_text(
        "\n".join(f"{h}|{','.join(ips)}" for h, ips in getent_map.items())
        + "\n"
    )
    resolv_file.write_text(resolv or "")
    hosts_file.write_text(hosts or "")
    # getent ahosts <host> shim: emit realistic `IP STREAM host` rows (real
    # getent prints "<ip> STREAM <host>" / DGRAM / RAW, NOT a bare IP), so a
    # resolve()-regex anchored to end-of-line gets caught here instead of in
    # production (#1937 review). Resolve from the | map, else echo the host.
    (bin_dir / "getent").write_text(
        "#!/bin/sh\n"
        f'map="{map_file}"\n'
        'host="$2"\n'
        'if [ -f "$map" ]; then\n'
        '  while IFS="|" read -r h ips; do\n'
        '    if [ "$h" = "$host" ]; then\n'
        '      printf "%s\\n" "$ips" | tr "," "\\n" \\\n'
        '        | while IFS= read -r ip; do printf "%s STREAM %s\\n" "$ip" "$host"; done\n'
        "      exit 0\n"
        "    fi\n"
        '  done < "$map"\n'
        "fi\n"
        'printf "%s STREAM %s\\n" "$host" "$host"\n'
    )
    (bin_dir / "getent").chmod(0o755)
    # nsenter shim: drop --net, then dispatch on the command token. The
    # hook drives three commands through nsenter — iptables (v4 rules),
    # ip6tables, and sysctl (#1936 disables IPv6 in the container netns) —
    # so a blind "shift twice, exec iptables" would force ip6tables/sysctl
    # argv through the iptables recorder and corrupt the v4 assertions.
    (bin_dir / "nsenter").write_text(
        "#!/bin/sh\n"
        "shift  # --net=/proc/.../ns/net\n"
        'cmd="$1"; shift\n'
        'case "$cmd" in\n'
        '  iptables) exec iptables "$@" ;;\n'
        '  ip6tables) exec ip6tables "$@" ;;\n'
        '  sysctl) exec sysctl "$@" ;;\n'
        '  *) echo "nsenter shim: unknown command $cmd" >&2; exit 1 ;;\n'
        "esac\n"
    )
    (bin_dir / "nsenter").chmod(0o755)
    # iptables shim: record argv, one arg per line, blank line between calls.
    (bin_dir / "iptables").write_text(
        "#!/bin/sh\n"
        f'rec="{record}"\n'
        'for a in "$@"; do\n'
        '  printf "%s\\n" "$a" >>"$rec"\n'
        "done\n"
        'printf "\\n" >>"$rec"\n'
        "exit 0\n"
    )
    (bin_dir / "iptables").chmod(0o755)
    # ip6tables shim (#1936): records to its own log so v6 calls don't
    # pollute the v4 iptables assertions.
    ip6_record = tmp_path / "ip6tables.log"
    (bin_dir / "ip6tables").write_text(
        "#!/bin/sh\n"
        f'rec="{ip6_record}"\n'
        'for a in "$@"; do\n'
        '  printf "%s\\n" "$a" >>"$rec"\n'
        "done\n"
        'printf "\\n" >>"$rec"\n'
        "exit 0\n"
    )
    (bin_dir / "ip6tables").chmod(0o755)
    # sysctl shim (#1936): records the disable_ipv6 argv, exits sysctl_rc
    # (default 0; tests pass sysctl_rc=1 to exercise the hook's fallback to
    # ip6tables DROP when the sysctl write fails — #1937 review).
    sysctl_record = tmp_path / "sysctl.log"
    (bin_dir / "sysctl").write_text(
        "#!/bin/sh\n"
        f'rec="{sysctl_record}"\n'
        'for a in "$@"; do\n'
        '  printf "%s\\n" "$a" >>"$rec"\n'
        "done\n"
        'printf "\\n" >>"$rec"\n'
        f"exit {sysctl_rc}\n"
    )
    (bin_dir / "sysctl").chmod(0o755)
    # sudo shim (#1959): the macOS/rootless hook prefixes iptables/nsenter/
    # sysctl with ``$SUDO`` ("sudo" when non-root). Real ``sudo`` resolves
    # commands via its own secure_path and bypasses the shims above — so
    # the real nsenter/iptables would run (touching the host netns, and on
    # some runners hanging on a password prompt). A passthrough sudo keeps
    # the shims on the prepended PATH in play.
    (bin_dir / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (bin_dir / "sudo").chmod(0o755)

    hook = bin_dir / "klangk-netfilter.sh"
    hook.write_text(nf.HOOK_SCRIPT)
    hook.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # Point the hook at our temp resolv.conf/hosts/hostname instead of
    # /proc/$pid/root.
    env["KLANGK_NETFILTER_RESOLV"] = str(resolv_file)
    env["KLANGK_NETFILTER_HOSTS"] = str(hosts_file)
    sh = shutil.which("sh") or "/bin/sh"
    proc = subprocess.run(
        [sh, str(hook)],
        input=state,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"hook exited {proc.returncode}\nstderr:\n{proc.stderr}"
    )
    # Capture stderr so tests can assert the hook's warning echoes (e.g. the
    # sysctl-failed fallback message) — #1937 review.
    (tmp_path / "hook.stderr").write_text(proc.stderr)
    if not record.exists():
        return []
    calls = []
    for block in record.read_text().split("\n\n"):
        args = block.split("\n")
        if args == [""]:
            continue
        calls.append(args)
    return calls


def _accept_rules(calls):
    """The per-destination ACCEPT invocations: [-A, OUTPUT, -d, <ip>, ...]."""
    return [c for c in calls if c[:3] == ["-A", "OUTPUT", "-d"]]


def _calls_from(log_path):
    """Parse a shim's blank-line-separated argv log into a list of calls."""
    if not log_path.exists():
        return []
    calls = []
    for block in log_path.read_text().split("\n\n"):
        args = block.split("\n")
        if args == [""]:
            continue
        calls.append(args)
    return calls


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="Hook script requires /proc and iptables/nsenter (Linux-only)",
)
class TestHookScriptExecutable:
    def test_host_port_emits_split_argv(self, tmp_path):
        # B1 regression: the ACCEPT rule must reach iptables as separate
        # argv entries (-d <ip> -p tcp --dport <port> -j ACCEPT), not one
        # blob. The IFS=',' bug collapsed the whole rule into a single
        # rejected argument.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        assert _accept_rules(calls) == [
            [
                "-A",
                "OUTPUT",
                "-d",
                "140.82.112.4",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
        ]

    def test_default_drop_policy_set(self, tmp_path):
        # The fail-closed posture: OUTPUT policy is DROP before any ACCEPT.
        calls = _run_hook(
            tmp_path,
            _state("a.example"),
            getent_map={"a.example": ["1.2.3.4"]},
        )
        assert ["-P", "OUTPUT", "DROP"] in calls

    def test_multi_ip_host_emits_one_rule_per_ip(self, tmp_path):
        # B1 compounding bug: under IFS=',' getent's newline-separated output
        # collapsed into one garbage IP. Each resolved IP must get its own
        # correctly-split ACCEPT rule.
        calls = _run_hook(
            tmp_path,
            _state("multi.example:443"),
            getent_map={"multi.example": ["1.1.1.1", "2.2.2.2"]},
        )
        assert _accept_rules(calls) == [
            [
                "-A",
                "OUTPUT",
                "-d",
                "1.1.1.1",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
            [
                "-A",
                "OUTPUT",
                "-d",
                "2.2.2.2",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
        ]

    def test_multiple_specs_all_applied_in_order(self, tmp_path):
        # The whole CSV is split and each spec yields its rules.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443,pypi.org,10.0.0.1"),
            getent_map={
                "github.com": ["140.82.112.4"],
                "pypi.org": ["151.101.0.0"],
            },
        )
        dests = [c[3] for c in _accept_rules(calls)]
        assert dests == ["140.82.112.4", "151.101.0.0", "10.0.0.1"]

    def test_host_without_port_allows_all_ports(self, tmp_path):
        calls = _run_hook(
            tmp_path,
            _state("pypi.org"),
            getent_map={"pypi.org": ["151.101.0.0"]},
        )
        assert _accept_rules(calls) == [
            ["-A", "OUTPUT", "-d", "151.101.0.0", "-j", "ACCEPT"],
        ]

    def test_no_annotation_is_noop(self, tmp_path):
        # No rules annotation → the hook exits before touching iptables.
        assert _run_hook(tmp_path, _state(None)) == []

    def test_no_pid_still_applies_core_rules(self, tmp_path):
        # pid is only used for the backend-gateway /etc/hosts read; the OCI
        # runtime runs the createContainer hook INSIDE the container netns,
        # so the core ruleset applies even without a pid (the gateway read
        # is skipped via its -r guard).
        calls = _run_hook(tmp_path, _state("a.com:443", with_pid=False))
        assert ["-P", "OUTPUT", "DROP"] in calls

    # --- I1: DNS must be pinned to the container's resolvers, not blanket ---

    def test_dns_allowed_only_to_resolv_nameservers(self, tmp_path):
        # I1 regression: :53 used to be ACCEPTed to ANY destination (an
        # exfil / DNS-tunneling channel). Now it's allowed only to the
        # resolvers in the `klangk.netfilter.resolvers` annotation (the
        # container's own resolvers, mirrored by the server because the
        # createContainer hook can't read the container's resolv.conf).
        calls = _run_hook(
            tmp_path,
            _state("github.com:443", resolvers="1.1.1.1,8.8.8.8"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        dns = [c for c in calls if "--dport" in c and "53" in c and "-p" in c]
        # One udp + one tcp rule per nameserver, each pinned to that IP.
        for ns in ("1.1.1.1", "8.8.8.8"):
            for proto in ("udp", "tcp"):
                assert [
                    "-A",
                    "OUTPUT",
                    "-p",
                    proto,
                    "--dport",
                    "53",
                    "-d",
                    ns,
                    "-j",
                    "ACCEPT",
                ] in dns
        # No blanket :53 rule (one without a -d destination) survives.
        assert not any("-d" not in c for c in dns)

    def test_no_dns_allow_when_no_resolvers(self, tmp_path):
        # With no resolvers annotation, DNS is fully blocked (fail-closed),
        # never falling back to the old blanket :53 ACCEPT.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),  # no resolvers annotation
            getent_map={"github.com": ["140.82.112.4"]},
        )
        assert not any("--dport" in c and "53" in c for c in calls)

    # --- I7: gateway resolved from the container's /etc/hosts, not getent ---

    def test_gateway_allowed_from_hosts_file(self, tmp_path):
        # I7 regression: host.containers.internal used to be resolved via the
        # host netns getent (where the name doesn't exist) → no gateway rule →
        # the workspace couldn't reach its LLM proxy / browser delegate / chat
        # bridge. Now it's read from the container's /etc/hosts.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
            hosts=("127.0.0.1 localhost\n10.0.2.2 host.containers.internal\n"),
        )
        assert ["-A", "OUTPUT", "-d", "10.0.2.2", "-j", "ACCEPT"] in calls

    def test_gateway_absent_when_not_in_hosts(self, tmp_path):
        # No host.containers.internal entry → no gateway rule. Confirms the
        # hook doesn't fall back to a host-netns getent (which would either
        # silently produce nothing or resolve the wrong IP).
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
            hosts="127.0.0.1 localhost\n",
        )
        assert not any("-d" in c and "10.0.2.2" in c for c in calls)

    # --- #1936: IPv6 disabled in the container netns (v4-only egress) ---

    def test_ipv6_output_default_dropped(self, tmp_path):
        # Belt-and-suspenders: ip6tables OUTPUT policy is DROP regardless of
        # whether the sysctl write took, so v6 egress is default-denied even
        # on a deploy where the sysctl knob can't be written.
        _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        assert ["-P", "OUTPUT", "DROP"] in _calls_from(
            tmp_path / "ip6tables.log"
        )

    def test_aaaa_records_filtered_from_resolution(self, tmp_path):
        # A dual-stack host yields both A and AAAA records from getent; the
        # hook emits an ACCEPT rule ONLY for the v4 address. v6 is disabled
        # in the container (#1936) AND iptables is v4-only (it would reject
        # `-d <v6>` and log noise), so AAAA records are filtered out.
        calls = _run_hook(
            tmp_path,
            _state("dual.example:443"),
            getent_map={"dual.example": ["140.82.112.4", "2001:db8::1"]},
        )
        assert _accept_rules(calls) == [
            [
                "-A",
                "OUTPUT",
                "-d",
                "140.82.112.4",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
        ]
        # No v6 ACCEPT attempt reached iptables (no noise, no leak).
        assert not any("2001:db8::1" in c for c in calls)

    # --- #1935: CIDR ranges emitted directly, never resolved ---

    def test_cidr_spec_emits_range_rule_unresolved(self, tmp_path):
        # A CIDR is emitted directly as -d <ip>/<plen> with NO DNS/getent
        # resolution (a CIDR isn't a hostname; `getent ahosts "10.0.0.0/8"`
        # returns nothing, so routing it through resolve() would silently
        # drop the rule). The getent map deliberately maps the CIDR string
        # to a bogus IP — if the hook resolved it, the rule would target
        # 9.9.9.9 instead of the range.
        calls = _run_hook(
            tmp_path,
            _state("10.0.0.0/8"),
            getent_map={"10.0.0.0/8": ["9.9.9.9"]},
        )
        assert _accept_rules(calls) == [
            ["-A", "OUTPUT", "-d", "10.0.0.0/8", "-j", "ACCEPT"],
        ]
        # The bogus mapped IP never reached iptables — confirms resolve()
        # was not called for the CIDR.
        assert not any("9.9.9.9" in c for c in calls)

    def test_cidr_spec_with_port_scopes_dport(self, tmp_path):
        # 10.0.0.0/8:443 restricts the range to tcp/443 only. Same
        # no-resolution contract: the getent map maps the full spec string
        # to a bogus IP that must NOT appear in the emitted rule.
        calls = _run_hook(
            tmp_path,
            _state("10.0.0.0/8:443"),
            getent_map={"10.0.0.0/8:443": ["9.9.9.9"]},
        )
        assert _accept_rules(calls) == [
            [
                "-A",
                "OUTPUT",
                "-d",
                "10.0.0.0/8",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
        ]
        assert not any("9.9.9.9" in c for c in calls)

    def test_cidr_and_host_specs_coexist_in_order(self, tmp_path):
        # A mixed allow-list applies the CIDR (no resolution) and the host
        # (resolved) in spec order, each as its own ACCEPT rule.
        calls = _run_hook(
            tmp_path,
            _state("10.0.0.0/8,github.com:443"),
            getent_map={
                "10.0.0.0/8": ["9.9.9.9"],  # must be ignored
                "github.com": ["140.82.112.4"],
            },
        )
        assert _accept_rules(calls) == [
            ["-A", "OUTPUT", "-d", "10.0.0.0/8", "-j", "ACCEPT"],
            [
                "-A",
                "OUTPUT",
                "-d",
                "140.82.112.4",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
        ]

    def test_cidr_host_bits_emitted_as_typed(self, tmp_path):
        # The validator accepts host bits set (10.5.0.0/8, strict=False) and
        # keeps the spec as-typed — the hook emits exactly that string
        # (iptables masks host bits correctly either way, so no
        # normalization is needed).
        calls = _run_hook(
            tmp_path,
            _state("10.5.0.0/8"),
            getent_map={"10.5.0.0/8": ["9.9.9.9"]},
        )
        assert _accept_rules(calls) == [
            ["-A", "OUTPUT", "-d", "10.5.0.0/8", "-j", "ACCEPT"],
        ]


# --- interactive egress consent mode (#2239) ---


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="Hook script requires /proc and iptables/nsenter (Linux-only)",
)
class TestHookScriptInteractiveMode:
    """Tests for the interactive egress mode NFLOG rule (#2239)."""

    def test_static_mode_no_nflog_rule(self, tmp_path):
        # Static mode (default) must NOT add an NFLOG rule — identical to
        # pre-#2239 behavior.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443", egress_mode="static"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        nflog_calls = [c for c in calls if "NFLOG" in c]
        assert nflog_calls == []

    def test_static_mode_no_explicit_drop(self, tmp_path):
        # Static mode must NOT add an explicit -A OUTPUT -j DROP — the
        # default OUTPUT policy handles it. An explicit DROP would be
        # redundant and misleading.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443", egress_mode="static"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        explicit_drops = [
            c for c in calls if c == ["-A", "OUTPUT", "-j", "DROP"]
        ]
        assert explicit_drops == []

    def test_no_egress_mode_annotation_no_nflog_rule(self, tmp_path):
        # Missing annotation (old containers) must not add NFLOG.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        nflog_calls = [c for c in calls if "NFLOG" in c]
        assert nflog_calls == []

    def test_interactive_mode_adds_nflog_and_drop(self, tmp_path):
        cid = "abc123def456" + "0" * 52
        calls = _run_hook(
            tmp_path,
            _state(
                "github.com:443",
                egress_mode="interactive",
                container_id=cid,
            ),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        # The NFLOG rule should be present with rate limiting and the
        # container-id-based prefix (first 12 chars).
        nflog_calls = [c for c in calls if "NFLOG" in c]
        assert len(nflog_calls) == 1
        nf_call = nflog_calls[0]
        assert "-A" in nf_call
        assert "OUTPUT" in nf_call
        assert "--limit" in nf_call
        assert "5/sec" in nf_call
        assert "--limit-burst" in nf_call
        assert "20" in nf_call
        assert "--nflog-prefix" in nf_call
        # Prefix uses first 12 chars of the container id
        prefix_idx = nf_call.index("--nflog-prefix") + 1
        assert nf_call[prefix_idx] == "klangk-egress:abc123def456:"
        assert "--nflog-group" in nf_call
        assert str(nf.NFLOG_GROUP) in nf_call
        # An explicit DROP should follow the NFLOG
        drop_calls = [c for c in calls if c == ["-A", "OUTPUT", "-j", "DROP"]]
        assert len(drop_calls) == 1

    def test_interactive_mode_drop_after_nflog(self, tmp_path):
        # The explicit DROP must come AFTER the NFLOG rule in the chain.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443", egress_mode="interactive"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        nflog_idx = next(i for i, c in enumerate(calls) if "NFLOG" in c)
        drop_idx = next(
            i
            for i, c in enumerate(calls)
            if c == ["-A", "OUTPUT", "-j", "DROP"]
        )
        assert drop_idx > nflog_idx

    def test_interactive_mode_accept_before_nflog(self, tmp_path):
        # ACCEPT rules for seed domains must come BEFORE the NFLOG rule
        # so permitted traffic is never logged as blocked.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443", egress_mode="interactive"),
            getent_map={"github.com": ["140.82.112.4"]},
        )
        accept_indices = [
            i for i, c in enumerate(calls) if "-A" in c and "ACCEPT" in c
        ]
        nflog_idx = next(i for i, c in enumerate(calls) if "NFLOG" in c)
        assert all(a < nflog_idx for a in accept_indices), (
            "All ACCEPT rules must precede the NFLOG rule"
        )

    def test_interactive_mode_seed_rules_still_applied(self, tmp_path):
        # Pre-approved allowed_domains are still installed as ACCEPT rules
        # even in interactive mode.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443,pypi.org", egress_mode="interactive"),
            getent_map={
                "github.com": ["140.82.112.4"],
                "pypi.org": ["151.101.0.0"],
            },
        )
        accept = _accept_rules(calls)
        dests = [c[3] for c in accept]
        assert "140.82.112.4" in dests
        assert "151.101.0.0" in dests

    def test_interactive_mode_truncates_long_container_id(self, tmp_path):
        # A full 64-char container id is truncated to 12 chars in the
        # NFLOG prefix.
        long_id = "a1b2c3d4e5f6" + "9" * 52
        calls = _run_hook(
            tmp_path,
            _state(
                "a.example:443",
                egress_mode="interactive",
                container_id=long_id,
            ),
            getent_map={"a.example": ["1.2.3.4"]},
        )
        nflog_calls = [c for c in calls if "NFLOG" in c]
        assert len(nflog_calls) == 1
        prefix_idx = nflog_calls[0].index("--nflog-prefix") + 1
        assert nflog_calls[0][prefix_idx] == "klangk-egress:a1b2c3d4e5f6:"

    def test_interactive_mode_missing_container_id(self, tmp_path):
        # If the OCI state has no "id" field, the prefix falls back to
        # "unknown".
        calls = _run_hook(
            tmp_path,
            _state(
                "a.example:443",
                egress_mode="interactive",
                container_id="",
            ),
            getent_map={"a.example": ["1.2.3.4"]},
        )
        nflog_calls = [c for c in calls if "NFLOG" in c]
        assert len(nflog_calls) == 1
        prefix_idx = nflog_calls[0].index("--nflog-prefix") + 1
        assert nflog_calls[0][prefix_idx] == "klangk-egress:unknown:"

    def test_bogus_egress_mode_no_nflog(self, tmp_path):
        # A typo like "intreactive" must NOT install NFLOG — the hook
        # fails closed (no observability, just drop via policy).
        calls = _run_hook(
            tmp_path,
            _state("a.example:443", egress_mode="intreactive"),
            getent_map={"a.example": ["1.2.3.4"]},
        )
        nflog_calls = [c for c in calls if "NFLOG" in c]
        assert nflog_calls == []
        explicit_drops = [
            c for c in calls if c == ["-A", "OUTPUT", "-j", "DROP"]
        ]
        assert explicit_drops == []


class TestCreateKwargsEgressMode:
    """Tests for create_kwargs with egress_mode parameter (#2239)."""

    def test_static_mode_no_egress_mode_annotation(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()
        ann, _, _, _ = nf_obj.create_kwargs(
            ["github.com:443"], egress_mode="static"
        )
        assert nf.ANNOTATION_EGRESS_MODE_KEY not in ann

    def test_interactive_mode_adds_egress_mode_annotation(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()
        ann, _, _, _ = nf_obj.create_kwargs(
            ["github.com:443"], egress_mode="interactive"
        )
        assert ann[nf.ANNOTATION_EGRESS_MODE_KEY] == "interactive"
        assert ann[nf.ANNOTATION_KEY] == "github.com:443"

    def test_default_egress_mode_is_static(self, tmp_path, monkeypatch):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()
        ann, _, _, _ = nf_obj.create_kwargs(["github.com:443"])
        assert nf.ANNOTATION_EGRESS_MODE_KEY not in ann

    def test_bogus_egress_mode_raises(self, tmp_path, monkeypatch):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        monkeypatch.setattr(nf.platform, "system", lambda: "Linux")
        nf_obj.install_hooks()
        with pytest.raises(ValueError, match="intreactive"):
            nf_obj.create_kwargs(["github.com:443"], egress_mode="intreactive")


# --- macOS / podman machine support (#1959) ---


class TestInstallHooksInVM:
    """Tests for _install_hooks_in_vm (macOS podman machine hook install)."""

    def test_install_hooks_calls_vm_installer_on_darwin(self, tmp_path):
        # On macOS, install_hooks() copies hooks into the VM after the
        # local write.
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        with mock.patch(
            "klangk.netfilter.platform.system", return_value="Darwin"
        ):
            with mock.patch.object(
                nf_obj, "_install_hooks_in_vm", return_value=True
            ) as m:
                result = nf_obj.install_hooks()
        assert result == os.path.realpath(path)
        m.assert_called_once()

    def test_install_hooks_returns_none_when_vm_install_fails(self, tmp_path):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        with mock.patch(
            "klangk.netfilter.platform.system", return_value="Darwin"
        ):
            with mock.patch.object(
                nf_obj, "_install_hooks_in_vm", return_value=False
            ):
                result = nf_obj.install_hooks()
        assert result is None

    def test_install_hooks_skips_vm_on_linux(self, tmp_path):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        with mock.patch(
            "klangk.netfilter.platform.system", return_value="Linux"
        ):
            with mock.patch.object(nf_obj, "_install_hooks_in_vm") as m:
                result = nf_obj.install_hooks()
        assert result == os.path.realpath(path)
        m.assert_not_called()

    def test_vm_installer_runs_podman_machine_ssh(self, tmp_path):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with mock.patch(
            "klangk.netfilter.subprocess.run", return_value=completed
        ) as m:
            assert nf_obj._install_hooks_in_vm() is True
        m.assert_called_once()
        args = m.call_args
        cmd = args[0][0]
        assert cmd[0] == "podman"
        assert cmd[1] == "machine"
        assert cmd[2] == "ssh"
        # The installer script is piped via stdin
        installer = args[1].get("input") or args.kwargs.get("input")
        assert "klangk-netfilter.sh" in installer
        assert "klangk-netfilter.json" in installer
        # VM paths, not host paths
        assert nf.VM_HOOKS_SCRIPT_DIR in installer
        assert nf.VM_HOOKS_JSON_DIR in installer

    def test_vm_installer_logs_error_on_ssh_failure(self, tmp_path, caplog):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stderr="connection refused"
        )
        with mock.patch(
            "klangk.netfilter.subprocess.run", return_value=completed
        ):
            with caplog.at_level("ERROR"):
                assert nf_obj._install_hooks_in_vm() is False
        assert any("podman machine" in r.message for r in caplog.records)

    def test_vm_installer_handles_timeout(self, tmp_path, caplog):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        with mock.patch(
            "klangk.netfilter.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="podman", timeout=30),
        ):
            with caplog.at_level("ERROR"):
                assert nf_obj._install_hooks_in_vm() is False
        assert any("podman machine" in r.message for r in caplog.records)

    def test_vm_hook_json_points_to_vm_script_path(self, tmp_path):
        # The hook JSON written into the VM must reference the VM-internal
        # script path, not the macOS host path.
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with mock.patch(
            "klangk.netfilter.subprocess.run", return_value=completed
        ) as m:
            nf_obj._install_hooks_in_vm()
        installer = m.call_args[1].get("input") or m.call_args.kwargs.get(
            "input"
        )
        vm_script_path = f"{nf.VM_HOOKS_SCRIPT_DIR}/{nf.HOOK_SCRIPT_NAME}"
        expected_json = nf.render_hook_json(
            vm_script_path, stage="createRuntime"
        )
        assert expected_json in installer


class TestCreateKwargsMacOS:
    """Tests for create_kwargs macOS behavior (#1959)."""

    def test_macos_omits_hooks_dirs(self, tmp_path):
        # On macOS, --hooks-dir is silently ignored in remote mode.
        # create_kwargs must return None for hooks_dirs.
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()
        with mock.patch(
            "klangk.netfilter.platform.system", return_value="Darwin"
        ):
            ann, hooks_dirs, cap_drop, _dns = nf_obj.create_kwargs(
                ["github.com:443"]
            )
        assert ann is not None
        assert hooks_dirs is None
        assert cap_drop == ["NET_ADMIN"]

    def test_linux_includes_hooks_dirs(self, tmp_path):
        # On Linux, hooks_dirs includes the klangk dir + standard dirs.
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()
        with mock.patch(
            "klangk.netfilter.platform.system", return_value="Linux"
        ):
            ann, hooks_dirs, cap_drop, _dns = nf_obj.create_kwargs(
                ["github.com:443"]
            )
        assert ann is not None
        assert hooks_dirs == [os.path.realpath(path), *nf.STANDARD_HOOK_DIRS]
        assert cap_drop == ["NET_ADMIN"]


class TestAllowBackendGateway:
    """Post-start backend-gateway allow-rule for filtered containers (#1365)."""

    def _nf(self, pod):
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(podman=pod, settings=make_settings({}))
        )
        return nf.NetFilter(app)

    async def test_inserts_accept_rule_for_resolved_gateway(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(
                return_value=(0, "169.254.1.2 host.containers.internal\n", "")
            ),
            inspect_container=mock.AsyncMock(
                return_value={"State": {"Pid": 12345}}
            ),
            run=mock.AsyncMock(return_value=(0, "", "")),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is True
        pod.run.assert_awaited_once()
        args = pod.run.call_args.args[0]
        assert args[:3] == ["unshare", "--", "nsenter"]
        assert "-t" in args and "12345" in args
        assert "169.254.1.2" in args
        assert "iptables" in args and "-I" in args and "ACCEPT" in args

    async def test_no_resolve_returns_false(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(return_value=(1, "", "no host")),
            inspect_container=mock.AsyncMock(),
            run=mock.AsyncMock(),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is False
        pod.run.assert_not_awaited()

    async def test_empty_output_returns_false(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(return_value=(0, "", "")),
            inspect_container=mock.AsyncMock(),
            run=mock.AsyncMock(),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is False
        pod.run.assert_not_awaited()

    async def test_exec_failure_returns_false(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(
                side_effect=nf.PodmanError(500, "boom")
            ),
            inspect_container=mock.AsyncMock(),
            run=mock.AsyncMock(),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is False
        pod.run.assert_not_awaited()

    async def test_insert_failure_returns_false(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(
                return_value=(0, "10.0.0.1 host.containers.internal\n", "")
            ),
            inspect_container=mock.AsyncMock(
                return_value={"State": {"Pid": 99}}
            ),
            run=mock.AsyncMock(
                side_effect=nf.PodmanError(500, "insert failed")
            ),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is False

    async def test_zero_pid_returns_false(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(
                return_value=(0, "10.0.0.1 host.containers.internal\n", "")
            ),
            inspect_container=mock.AsyncMock(
                return_value={"State": {"Pid": 0}}
            ),
            run=mock.AsyncMock(),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is False
        pod.run.assert_not_awaited()

    async def test_missing_container_returns_false(self):
        pod = types.SimpleNamespace(
            exec_container=mock.AsyncMock(
                return_value=(0, "10.0.0.1 host.containers.internal\n", "")
            ),
            inspect_container=mock.AsyncMock(return_value=None),
            run=mock.AsyncMock(),
        )
        assert await self._nf(pod).allow_backend_gateway("cid") is False
        pod.run.assert_not_awaited()
