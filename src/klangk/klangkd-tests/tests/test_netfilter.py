"""Tests for the per-workspace netfilter egress-filter surface (#1365)."""

import json
import os
import shutil
import stat
import subprocess
import types

import pytest

from klangk import netfilter as nf
from _helpers import make_settings


def _app(hooks_dir=None, default_domains=None, tmp_path=None):
    settings = make_settings({})
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

    @pytest.mark.parametrize(
        "spec",
        [
            "github.com",
            "github.com:443",
            "pypi.org:80",
            "10.0.0.1",
            "10.0.0.1:53",
            "sub.domain.example.com:8080",
            "[::1]",
            "[2001:db8::1]:443",
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
            "/etc/passwd",  # slash
            "-leading",  # leading hyphen rejected by host grammar
            "a.com/path",
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
    def test_unset_returns_none(self):
        assert nf.NetFilter(_app()).hooks_dir() is None

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
        assert nf.NetFilter(_app()).install_hooks() is None

    def test_writes_script_and_json(self, tmp_path):
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

    def test_idempotent(self, tmp_path):
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
    def test_no_domains_returns_none_triplet(self):
        assert nf.NetFilter(_app()).create_kwargs(None) == (None, None, None)
        assert nf.NetFilter(_app()).create_kwargs([]) == (None, None, None)

    def test_domains_without_hooks_dir_warns_and_fail_opens(self, caplog):
        app = _app()  # netfilter disabled
        with caplog.at_level("WARNING"):
            result = nf.NetFilter(app).create_kwargs(["github.com:443"])
        assert result == (None, None, None)
        assert any("UNRESTRICTED" in r.message for r in caplog.records)

    def test_domains_with_hooks_dir_returns_annotation_path_and_cap_drop(
        self, tmp_path
    ):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()  # arm the hook so create_kwargs trusts the dir
        ann, hooks, cap_drop = nf_obj.create_kwargs(
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
        ann, _, cap_drop = nf_obj.create_kwargs(["ws.com:443"])
        assert ann == {nf.ANNOTATION_KEY: "ws.com:443"}
        assert cap_drop == ["NET_ADMIN"]

    def test_empty_workspace_inherits_deploy_default(self, tmp_path):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(
            _app(hooks_dir=path, default_domains=["default.com", "a.io"])
        )
        nf_obj.install_hooks()
        ann, hooks, _ = nf_obj.create_kwargs(None)
        assert ann == {nf.ANNOTATION_KEY: "default.com,a.io"}
        assert hooks == [os.path.realpath(path), *nf.STANDARD_HOOK_DIRS]

        # Same for an explicit empty list (None and [] both inherit).
        ann2, _, _ = nf_obj.create_kwargs([])
        assert ann2 == {nf.ANNOTATION_KEY: "default.com,a.io"}

    def test_default_present_but_netfilter_disabled_warns(self, caplog):
        app = _app(default_domains=["default.com"])  # no hooks dir
        with caplog.at_level("WARNING"):
            result = nf.NetFilter(app).create_kwargs(None)
        assert result == (None, None, None)
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
        assert result == (None, None, None)
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
        assert result == (None, None, None)
        assert any(
            "not installed or is stale" in r.message for r in caplog.records
        )


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
    def test_disabled_when_hooks_dir_unset(self):
        assert nf.NetFilter(_app()).enabled() is False

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


def _state(rules, *, with_pid=True):
    """Build synthetic OCI container state JSON for the hook.

    ``rules`` is the ``klangk.netfilter.rules`` annotation value, or ``None``
    to omit the annotation (early-exit path). ``with_pid=False`` omits ``pid``
    (the other early-exit path). Otherwise ``pid`` is the running process's
    id so the hook's ``[ -e /proc/$pid/ns/net ]`` guard passes — the nsenter
    shim ignores the path anyway.
    """
    s = {}
    if with_pid:
        s["pid"] = os.getpid()
    if rules is not None:
        s["annotations"] = {nf.ANNOTATION_KEY: rules}
    return json.dumps(s)


def _run_hook(tmp_path, state, getent_map=None, resolv=None, hosts=None):
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
    # getent ahosts <host> shim: resolve from the | map, else echo the host.
    (bin_dir / "getent").write_text(
        "#!/bin/sh\n"
        f'map="{map_file}"\n'
        'host="$2"\n'
        'if [ -f "$map" ]; then\n'
        '  while IFS="|" read -r h ips; do\n'
        '    if [ "$h" = "$host" ]; then\n'
        '      printf "%s\\n" "$ips" | tr "," "\\n"\n'
        "      exit 0\n"
        "    fi\n"
        '  done < "$map"\n'
        "fi\n"
        'printf "%s\\n" "$host"\n'
    )
    (bin_dir / "getent").chmod(0o755)
    # nsenter shim: drop the --net flag + the "iptables" token, then re-exec
    # the iptables shim with the remaining args (the real netns is irrelevant
    # to what we're asserting, which is the argv the hook builds).
    (bin_dir / "nsenter").write_text(
        "#!/bin/sh\n"
        "shift  # --net=/proc/.../ns/net\n"
        'shift  # "iptables"\n'
        'exec iptables "$@"\n'
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

    hook = bin_dir / "klangk-netfilter.sh"
    hook.write_text(nf.HOOK_SCRIPT)
    hook.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # Point the hook at our temp resolv.conf/hosts instead of /proc/$pid/root.
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

    def test_bracketed_ipv6_with_port(self, tmp_path):
        # B2 regression: [2001:db8::1]:443 is blessed by the API validator
        # but the hook's ':' suffix-splitting mangled it (_host="[2001").
        # Brackets must be stripped and the port parsed.
        calls = _run_hook(
            tmp_path,
            _state("[2001:db8::1]:443"),
            getent_map={"2001:db8::1": ["2001:db8::1"]},
        )
        assert _accept_rules(calls) == [
            [
                "-A",
                "OUTPUT",
                "-d",
                "2001:db8::1",
                "-p",
                "tcp",
                "--dport",
                "443",
                "-j",
                "ACCEPT",
            ],
        ]

    def test_bracketed_ipv6_without_port(self, tmp_path):
        # B2: [::1] (no port) — brackets stripped, no --dport emitted.
        calls = _run_hook(
            tmp_path,
            _state("[::1]"),
            getent_map={"::1": ["::1"]},
        )
        assert _accept_rules(calls) == [
            ["-A", "OUTPUT", "-d", "::1", "-j", "ACCEPT"],
        ]

    def test_multiple_specs_all_applied_in_order(self, tmp_path):
        # The whole CSV is split and each spec yields its rules.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443,pypi.org,[::1]"),
            getent_map={
                "github.com": ["140.82.112.4"],
                "pypi.org": ["151.101.0.0"],
            },
        )
        dests = [c[3] for c in _accept_rules(calls)]
        assert dests == ["140.82.112.4", "151.101.0.0", "::1"]

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

    def test_no_pid_is_noop(self, tmp_path):
        # No init pid → same early exit (no netns to install into).
        assert _run_hook(tmp_path, _state("a.com:443", with_pid=False)) == []

    # --- I1: DNS must be pinned to the container's resolvers, not blanket ---

    def test_dns_allowed_only_to_resolv_nameservers(self, tmp_path):
        # I1 regression: :53 used to be ACCEPTed to ANY destination (an
        # exfil / DNS-tunneling channel). Now it's allowed only to the
        # nameservers in the container's /etc/resolv.conf.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
            resolv="nameserver 1.1.1.1\nnameserver 8.8.8.8\n",
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
        # With no nameservers configured, DNS is fully blocked (fail-closed),
        # never falling back to the old blanket :53 ACCEPT.
        calls = _run_hook(
            tmp_path,
            _state("github.com:443"),
            getent_map={"github.com": ["140.82.112.4"]},
            resolv="",  # no nameservers
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
