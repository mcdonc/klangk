"""``klangkd doctor`` — pre-flight dependency and configuration checker (#1612).

Checks required external binaries, rootless podman, and common
misconfigurations. Reports what's missing or broken with actionable
install hints per detected package manager.

Design: capabilities first, never platform predictions. Every check runs
on every host; only the package-hint table varies by detected manager.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    is_warning: bool = False  # False = error (must fix), True = warning
    hint: str = ""


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def passed(self) -> bool:
        return all(r.ok or r.is_warning for r in self.results)

    @property
    def errors(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and not r.is_warning]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.is_warning]


# ---------------------------------------------------------------------------
# Package manager detection (by binary presence, not /etc/os-release)
# ---------------------------------------------------------------------------

# Ordered by specificity: dnf before yum (Fedora ships both), apt-get
# before apt (scripts prefer apt-get for non-interactive use).
_MANAGER_PROBE_ORDER = [
    ("dnf", "dnf"),
    ("yum", "yum"),
    ("apt-get", "apt"),
    ("zypper", "zypper"),
    ("apk", "apk"),
    ("pacman", "pacman"),
    ("brew", "brew"),
]


def detect_package_manager() -> str | None:
    """Return the package manager command name, or None."""
    for binary, name in _MANAGER_PROBE_ORDER:
        if shutil.which(binary):
            return name
    return None


# ---------------------------------------------------------------------------
# Package hint tables (binary → package name per manager)
# ---------------------------------------------------------------------------

# Each entry: binary_name → {manager → package_name}
_PACKAGE_HINTS: dict[str, dict[str, str]] = {
    "podman": {
        "dnf": "podman",
        "yum": "podman",
        "apt": "podman",
        "brew": "podman",
        "pacman": "podman",
        "zypper": "podman",
        "apk": "podman",
    },
    "caddy": {
        "dnf": "caddy",
        "yum": "caddy",
        "apt": "caddy",
        "brew": "caddy",
        "pacman": "caddy",
    },
    "sqlite3": {
        "dnf": "sqlite",
        "yum": "sqlite",
        "apt": "sqlite3",
        "brew": "sqlite3",
        "pacman": "sqlite",
    },
    "rsync": {
        "dnf": "rsync",
        "apt": "rsync",
        "brew": "rsync",
    },
    "git": {
        "dnf": "git",
        "apt": "git",
        "brew": "git",
        "pacman": "git",
    },
    "tmux": {
        "dnf": "tmux",
        "apt": "tmux",
        "brew": "tmux",
        "pacman": "tmux",
    },
    "gzip": {
        "dnf": "gzip",
        "apt": "gzip",
        "brew": "gzip",
    },
    "tar": {
        "dnf": "tar",
        "apt": "tar",
        "brew": "gnu-tar",
    },
    "openssl": {
        "dnf": "openssl",
        "apt": "openssl",
        "brew": "openssl",
    },
    "du": {
        "dnf": "coreutils",
        "apt": "coreutils",
        "brew": "coreutils",
    },
    "stat": {
        "dnf": "coreutils",
        "apt": "coreutils",
        "brew": "coreutils",
        "zypper": "coreutils",
        "pacman": "coreutils",
    },
    # Rootless podman prereqs
    "newuidmap": {
        "dnf": "shadow-utils",
        "apt": "uidmap",
    },
    # Container subnet auto-detection (`ip -4 addr show`, #2089).
    # The binary ships as `iproute` on the Red Hat family (dnf/yum) and
    # `iproute2` everywhere else — brew covers Linuxbrew hosts (the formula
    # is Linux-only, but the check is Darwin-skipped anyway).
    "ip": {
        "dnf": "iproute",
        "yum": "iproute",
        "apt": "iproute2",
        "zypper": "iproute2",
        "apk": "iproute2",
        "pacman": "iproute2",
        "brew": "iproute2",
    },
}


def install_hint(binary: str, manager: str | None) -> str:
    """Return an install command string for a missing binary."""
    if manager is None:
        return f"install {binary}"
    hints = _PACKAGE_HINTS.get(binary, {})
    pkg = hints.get(manager, binary)
    if manager == "brew":
        return f"brew install {pkg}"
    if manager == "apt":
        return f"sudo apt install {pkg}"
    if manager == "pacman":
        # pacman has no "install" subcommand — the package action is -S.
        return f"sudo pacman -S {pkg}"
    return f"sudo {manager} install {pkg}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return -1, "", f"{cmd[0]}: timed out"


def check_binary(
    name: str, check_cmd: list[str], manager: str | None
) -> CheckResult:
    """Check a binary is on PATH and functional.

    If *check_cmd* is empty, only verify the binary is on PATH (useful
    for suid helpers like ``newuidmap`` that can't be invoked directly).
    """
    path = shutil.which(name)
    if not path:
        return CheckResult(
            name=name,
            ok=False,
            message=f"{name} not found on PATH",
            hint=install_hint(name, manager),
        )
    if not check_cmd:
        return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")
    rc, _out, err = run(check_cmd)
    if rc != 0:
        return CheckResult(
            name=name,
            ok=False,
            message=f"{name} found at {path} but check failed: {err.strip()[:200]}",
            hint=install_hint(name, manager),
        )
    return CheckResult(name=name, ok=True, message=f"{name} ok ({path})")


# tmux display-popup (used by the TUI consent-decider popup over the
# shell, #2383) landed in 3.2. Below this the shell layer falls back to a
# plain attach, so an old/unknown host tmux is a warning, not an error.
TMUX_MIN_VERSION = (3, 2)


def parse_tmux_version(out: str) -> tuple[int, int] | None:
    """Parse ``tmux -V`` output (e.g. ``"tmux 3.6a"``) -> ``(3, 6)``.

    Returns None when no ``MAJOR.MINOR`` pair is present (unparseable /
    unexpected output) so the caller can flag it as an unknown-version
    warning rather than guessing.
    """
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _tmux_warning(message: str, manager: str | None) -> CheckResult:
    """A consent-popup warning result carrying the tmux install hint."""
    return CheckResult(
        name="tmux (consent popup)",
        ok=False,
        is_warning=True,
        message=message,
        hint=install_hint("tmux", manager),
    )


def _tmux_probe_text(out: str) -> str:
    """The raw ``tmux -V`` text for a warning message (or a note that
    the probe itself failed)."""
    return out.strip()[:40] or "tmux -V failed"


def check_tmux_version(manager: str | None) -> CheckResult:
    """Check host tmux is new enough for the consent-popup shell layer (#2383).

    A missing tmux is already reported as an error by the required-binary
    check above; here that case is a passing no-op (no version to check).
    A present-but-old or unparseable version is a warning: the shell layer
    degrades to a plain attach, so doctor must not hard-fail the host.
    """
    if not shutil.which("tmux"):
        return CheckResult(
            name="tmux (consent popup)",
            ok=True,
            message="no host tmux (reported by the tmux check above)",
        )
    rc, out, _err = run(["tmux", "-V"])
    ver = parse_tmux_version(out) if rc == 0 else None
    if ver is None:
        return _tmux_warning(
            f"tmux version unparseable ({_tmux_probe_text(out)});"
            " consent popup needs >= 3.2",
            manager,
        )
    label = ".".join(map(str, ver))
    if ver < TMUX_MIN_VERSION:
        return _tmux_warning(
            f"tmux {label} < 3.2 — consent popup unavailable"
            " (plain shell attach used)",
            manager,
        )
    return CheckResult(
        name="tmux (consent popup)",
        ok=True,
        message=f"tmux {label} ok for consent popup (>= 3.2)",
    )


def check_gnu_tar(manager: str | None) -> CheckResult:
    """Check that tar is GNU tar (not BSD)."""
    path = shutil.which("tar")
    if not path:
        return CheckResult(
            name="tar (GNU)",
            ok=False,
            message="tar not found on PATH",
            hint=install_hint("tar", manager),
        )
    rc, out, _err = run(["tar", "--version"])
    if rc != 0 or "GNU" not in out:
        hint = install_hint("tar", manager)
        if manager == "brew":
            hint = (
                "brew install gnu-tar (klangkd auto-discovers the "
                "gnubin path at startup)"
            )
        return CheckResult(
            name="tar (GNU)",
            ok=False,
            message="tar is not GNU tar (workspace archives need --transform)",
            hint=hint,
        )
    return CheckResult(
        name="tar (GNU)", ok=True, message=f"GNU tar ok ({path})"
    )


def check_gnu_du(manager: str | None) -> CheckResult:
    """Check that du supports -b (GNU coreutils)."""
    path = shutil.which("du")
    if not path:
        return CheckResult(
            name="du (GNU)",
            ok=False,
            message="du not found on PATH",
            hint=install_hint("du", manager),
        )
    rc, _out, _err = run(["du", "-b", "/dev/null"])
    if rc != 0:
        hint = install_hint("du", manager)
        if manager == "brew":
            hint = (
                "brew install coreutils (klangkd auto-discovers the "
                "gnubin path at startup)"
            )
        return CheckResult(
            name="du (GNU)",
            ok=False,
            message="du does not support -b (need GNU coreutils)",
            hint=hint,
        )
    return CheckResult(name="du (GNU)", ok=True, message=f"GNU du ok ({path})")


def check_gnu_stat(manager: str | None) -> CheckResult:
    """Check that stat is GNU coreutils (supports ``-f -c %T`` for fstype).

    The nix btrfs-snapshot backend uses ``stat -f -c %T <seed>`` to verify the
    seed path is on btrfs (``Nix.ensure_btrfs``). GNU coreutils only — BSD
    ``stat`` parses ``-f`` differently. Linux-only in ``run_doctor`` (the btrfs
    backend doesn't apply on macOS).
    """
    path = shutil.which("stat")
    if not path:
        return CheckResult(
            name="stat (GNU)",
            ok=False,
            message="stat not found on PATH",
            hint=install_hint("stat", manager),
        )
    rc, out, _err = run(["stat", "-f", "-c", "%T", "/"])
    if rc != 0 or not out.strip():
        return CheckResult(
            name="stat (GNU)",
            ok=False,
            message="stat does not support -f -c %T (need GNU coreutils)",
            hint=install_hint("stat", manager),
        )
    return CheckResult(
        name="stat (GNU)", ok=True, message=f"GNU stat ok ({path})"
    )


def _user_range_present(content: str, user: str) -> bool:
    """True when a subuid/subgid file carries a range line for *user*."""
    return any(line.startswith(f"{user}:") for line in content.splitlines())


def _missing_subuid_result(
    user: str, path_name: str, label: str | None
) -> CheckResult:
    """A failed subuid/subgid result with the usermod fix hint.

    A ``None`` label means the file itself is missing, not just the
    user's range.
    """
    message = (
        f"{path_name} does not exist"
        if label is None
        else f"no {label} range for user '{user}' in {path_name}"
    )
    return CheckResult(
        name="subuid/subgid",
        ok=False,
        message=message,
        hint=(
            f"sudo usermod --add-subuids 100000-165535 "
            f"--add-subgids 100000-165535 {user}"
        ),
    )


def check_subuid(user: str) -> CheckResult:
    """Check /etc/subuid has a range for the given user."""
    if platform.system() == "Darwin":
        return CheckResult(
            name="subuid/subgid",
            ok=True,
            message="skipped on macOS (podman machine handles mapping)",
        )
    for path_name, label in [
        ("/etc/subuid", "subuid"),
        ("/etc/subgid", "subgid"),
    ]:
        p = Path(path_name)
        if not p.exists():
            return _missing_subuid_result(user, path_name, None)
        if not _user_range_present(p.read_text(), user):
            return _missing_subuid_result(user, path_name, label)
    return CheckResult(
        name="subuid/subgid",
        ok=True,
        message=f"subuid/subgid ranges found for {user}",
    )


def check_podman_policy(
    candidates: list[Path] | None = None,
) -> CheckResult:
    """Check that a container policy file exists."""
    if candidates is None:
        candidates = [
            Path.home() / ".config" / "containers" / "policy.json",
            Path("/etc/containers/policy.json"),
        ]
    for p in candidates:
        if p.exists():
            try:
                json.loads(p.read_text())
                return CheckResult(
                    name="podman policy",
                    ok=True,
                    message=f"policy file ok ({p})",
                )
            except (json.JSONDecodeError, OSError) as exc:
                return CheckResult(
                    name="podman policy",
                    ok=False,
                    is_warning=True,
                    message=f"policy file at {p} is invalid: {exc}",
                )
    user_policy = Path.home() / ".config" / "containers" / "policy.json"
    return CheckResult(
        name="podman policy",
        ok=False,
        is_warning=True,
        message="no container policy file found",
        hint=f'mkdir -p {user_policy.parent} && echo \'{{"default":[{{"type":"insecureAcceptAnything"}}]}}\' > {user_policy}',
    )


def check_podman_machine() -> CheckResult:
    """macOS: check podman machine is running."""
    if platform.system() != "Darwin":
        return CheckResult(
            name="podman machine",
            ok=True,
            message="skipped on Linux (rootless podman, no VM needed)",
        )
    rc, out, err = run(["podman", "machine", "info"])
    if rc != 0:
        return CheckResult(
            name="podman machine",
            ok=False,
            message="podman machine not available",
            hint="podman machine init && podman machine start",
        )
    # Check if a machine is running
    rc2, out2, _err2 = run(
        ["podman", "machine", "list", "--format", "{{.Running}}"]
    )
    if rc2 == 0 and "true" in out2.lower():
        return CheckResult(
            name="podman machine",
            ok=True,
            message="podman machine is running",
        )
    return CheckResult(
        name="podman machine",
        ok=False,
        message="no podman machine is running",
        hint="podman machine start",
    )


# Port 80 is always part of an armed ACME config (HTTP-01 challenge + the
# HTTP→HTTPS redirect). Port 443 only is when the browser listener actually
# uses it: with KLANGKD_PORT=8443 caddy binds :80 and :8443 — never 443 —
# so a 443 probe there is a false positive against some other service
# (#3192 review).
_AUTO_HTTPS_PORTS = (80, 443)


def _armed_ports(browser_port: str | None) -> tuple[int, ...]:
    """The ACME ports this deployment actually binds."""
    if browser_port in (None, "", "443"):
        return _AUTO_HTTPS_PORTS
    return (80,)


def port_bind_error(port: int) -> str | None:
    """Why the ACME port <port> would not bind, or None when it would.

    Binds (and immediately closes) an IPv4 socket on the privileged port
    Caddy's auto-HTTPS listeners bind, so permission denials and
    conflicts surface here instead of as a mid-boot ACME failure.
    Probes loopback specifically: EACCES for a privileged port is
    per-port (not per-interface), and a wildcard or loopback listener on
    the port still collides — only a listener bound to a *different
    specific* interface on the same port escapes the probe, a shape no
    klangkd deployment creates. IPv6 is not probed (Caddy binds it
    alongside; the IPv4 result is the signal). No ``SO_REUSEADDR``: a
    lingering TIME_WAIT socket on the port reads as in-use — re-run
    doctor after a few seconds before trusting a busy-port failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return None
    except OSError as exc:
        return f"{port}: {exc.strerror or exc}"
    finally:
        sock.close()


def _internal_tls_issuer(issuer: str | None) -> bool:
    """True when the armed listener uses the internal issuer (no
    privileged ACME ports to check, #3192)."""
    return (issuer or "").strip().lower() == "internal"


def _auto_https_ok_result(hostname: str) -> CheckResult:
    """The passing result when every ACME port binds (#3192)."""
    return CheckResult(
        name="auto-https ports",
        ok=True,
        message=(
            f"ports 80/443 bindable — ACME issuance for {hostname} can proceed"
        ),
    )


def _auto_https_failure_result(
    hostname: str, failures: list[str]
) -> CheckResult:
    """The error-grade result naming each unbindable port (#3192).

    In armed mode the auto-HTTPS listeners (:80 redirect/challenge, :443)
    are part of the config caddy loads, so an unbindable port does not
    degrade to "HTTPS broken, HTTP fine" — caddy refuses the whole load
    and the watchdog aborts. The proxy is down entirely, browser and
    egress alike, which is why this is error-grade, not a warning.
    """
    return CheckResult(
        name="auto-https ports",
        ok=False,
        message=(
            f"KLANGKD_TLS_HOSTNAME={hostname} but cannot bind: "
            + "; ".join(failures)
            + ". With automatic TLS armed these ports are part of the "
            "proxy config: caddy refuses to load it and the klangkd "
            "proxy (browser AND container-egress listeners) will not "
            "start until the port is free or caddy may bind privileged "
            "ports."
        ),
        hint=(
            "sudo setcap 'cap_net_bind_service=+ep' "
            "$(readlink -f $(which caddy))"
        ),
    )


def _unbindable_acme_ports(browser_port: str | None) -> list[str]:
    """The armed ACME ports that cannot be bound (``"port: reason"``
    strings; empty when every port binds)."""
    failures = []
    for port in _armed_ports(browser_port):
        reason = port_bind_error(port)
        if reason is not None:
            failures.append(reason)
    return failures


def check_auto_https_ports(
    hostname: str | None,
    issuer: str | None = None,
    browser_port: str | None = None,
) -> CheckResult | None:
    """Automatic-TLS pre-flight (#3192): ports 80/443 must be bindable.

    Runs only when ``KLANGKD_TLS_HOSTNAME`` arms automatic TLS with the
    ACME issuer (env-var detection — doctor is a standalone pre-flight
    command with no config plumbed in; a YAML-configured deployment
    exports the vars when running doctor). The internal issuer needs no
    privileged ports (no redirect, no ACME challenge), so it is skipped.
    Failures are error-grade: the auto-HTTPS listeners are part of the
    armed proxy config, so an unbindable 80/443 keeps caddy from loading
    it at all — the klangkd proxy does not start (browser and
    container-egress listeners alike), not merely "no certificate".
    """
    if not hostname or _internal_tls_issuer(issuer):
        return None
    failures = _unbindable_acme_ports(browser_port)
    if not failures:
        return _auto_https_ok_result(hostname)
    return _auto_https_failure_result(hostname, failures)


def check_rootless_podman() -> CheckResult:
    """Verify rootless podman can actually run a container."""
    if not shutil.which("podman"):
        return CheckResult(
            name="rootless podman",
            ok=False,
            message="podman not found (skipping rootless check)",
        )
    rc, _out, err = run(
        ["podman", "run", "--rm", "docker.io/library/busybox:latest", "true"],
        timeout=120.0,
    )
    if rc == 0:
        return CheckResult(
            name="rootless podman",
            ok=True,
            message="rootless podman can run containers",
        )
    return CheckResult(
        name="rootless podman",
        ok=False,
        message=f"rootless podman failed: {err.strip()[:300]}",
        hint="check subuid/subgid, fuse-overlayfs, and podman policy",
    )


# ---------------------------------------------------------------------------
# Main doctor logic
# ---------------------------------------------------------------------------

# Required binaries and their capability checks.
_REQUIRED_BINARIES: list[tuple[str, list[str]]] = [
    ("podman", ["podman", "info"]),
    ("caddy", ["caddy", "version"]),
    ("sqlite3", ["sqlite3", ":memory:", "SELECT 1;"]),
    ("rsync", ["rsync", "--version"]),
    ("git", ["git", "--version"]),
    ("tmux", ["tmux", "-V"]),
    ("gzip", ["gzip", "--version"]),
    ("openssl", ["openssl", "version"]),
]


def _check_ip_command(report: DoctorReport, manager: str | None) -> None:
    """Optional ``ip`` check for container subnet auto-detection (#2089);
    a missing binary downgrades to a warning with the fallback note."""
    ip_result = check_binary("ip", ["ip", "-V"], manager)
    if not ip_result.ok:
        ip_result.is_warning = True
        ip_result.message = (
            "ip not found — container subnet auto-detection will fall "
            "back to broad RFC1918 ranges (172.16/12 + 10/8). Install "
            "the iproute/iproute2 package for your distro for precise "
            "detection, or set KLANGKD_CONTAINER_SUBNETS explicitly."
        )
    report.add(ip_result)


def _check_linux_prereqs(
    report: DoctorReport, user: str, manager: str | None
) -> None:
    """Linux-only rootless prereqs: newuidmap (warning-grade when
    missing) and the subuid/subgid ranges.

    fuse-overlayfs and slirp4netns are no longer checked — modern podman
    (4.x+) uses native kernel overlayfs and pasta respectively; the
    end-to-end rootless check below validates that storage and
    networking actually work (#1950).
    """
    result = check_binary("newuidmap", [], manager)
    if not result.ok:
        result.is_warning = True
    report.add(result)
    report.add(check_subuid(user))


def run_doctor(*, verbose: bool = False) -> DoctorReport:
    """Run all doctor checks and return the report."""
    report = DoctorReport()
    manager = detect_package_manager()
    user = os.environ.get("USER", os.environ.get("LOGNAME", "unknown"))

    # 1. Required binaries
    for name, check_cmd in _REQUIRED_BINARIES:
        report.add(check_binary(name, check_cmd, manager))

    # 1b. tmux version for the consent-popup shell layer (#2383): display-popup
    # needs >= 3.2; below it the shell falls back to a plain attach (warning).
    report.add(check_tmux_version(manager))

    # GNU tar and GNU du (special: must verify GNU, not just present)
    report.add(check_gnu_tar(manager))
    report.add(check_gnu_du(manager))
    if platform.system() != "Darwin":
        # GNU stat: only the nix btrfs-snapshot backend needs
        # `stat -f -c %T` (Linux-only); not required on macOS.
        report.add(check_gnu_stat(manager))
        # Optional: ip command for container subnet auto-detection (#2089)
        _check_ip_command(report, manager)

    # 2. Rootless podman prereqs
    if platform.system() != "Darwin":
        _check_linux_prereqs(report, user, manager)
    else:
        report.add(check_podman_machine())

    # 3. Configuration checks
    report.add(check_podman_policy())

    # 4. End-to-end rootless podman (the definitive check)
    report.add(check_rootless_podman())

    # 5. Automatic TLS (#3192): when KLANGKD_TLS_HOSTNAME arms auto-HTTPS
    # with the ACME issuer, the ACME ports must be bindable. Skipped when
    # unarmed or on the internal issuer (no privileged ports needed).
    tls_result = check_auto_https_ports(
        os.environ.get("KLANGKD_TLS_HOSTNAME"),
        issuer=os.environ.get("KLANGKD_TLS_ISSUER"),
        browser_port=os.environ.get("KLANGKD_PORT"),
    )
    if tls_result is not None:
        report.add(tls_result)

    return report


def _append_result_line(lines: list[str], r) -> None:
    """One result with its ✓/⚠/✗ marker, plus the fix hint when present."""
    if r.ok:
        lines.append(f"  ✓ {r.name}: {r.message}")
    elif r.is_warning:
        lines.append(f"  ⚠ {r.name}: {r.message}")
    else:
        lines.append(f"  ✗ {r.name}: {r.message}")
    if r.hint:
        lines.append("")
        lines.append(f"    Run:  {r.hint}")
        lines.append("")


def append_failure_block(
    lines: list[str], results: list, marker: str, heading: str
) -> None:
    """A repeated failure block with fix hints (#1968)."""
    if not results:
        return
    lines.append(heading)
    lines.append("")
    for r in results:
        lines.append(f"  {marker} {r.name}: {r.message}")
        if r.hint:
            lines.append(f"    Run:  {r.hint}")
        lines.append("")


def _report_header(lines: list[str]) -> None:
    """The doctor banner with the detected package manager."""
    lines.append("klangkd doctor")
    lines.append("=" * 40)
    manager = detect_package_manager()
    if manager:
        lines.append(f"Package manager: {manager}")
    else:
        lines.append("Package manager: (none detected)")
    lines.append("")


def _finish_report(lines: list[str], report: DoctorReport) -> str:
    """Append the summary tally and, when anything failed, repeat each
    failure with its fix so the user doesn't have to scroll back up
    through a long check list (#1968)."""
    errors = report.errors
    warnings = report.warnings
    ok_count = len(report.results) - len(errors) - len(warnings)
    if not errors and not warnings:
        lines.append(f"All {ok_count} checks passed.")
        return "\n".join(lines)
    if errors:
        lines.append(
            f"{ok_count} passed, {len(errors)} errors,"
            f" {len(warnings)} warnings"
        )
    else:
        lines.append(f"{ok_count} passed, {len(warnings)} warnings")
    lines.append("")
    append_failure_block(
        lines, errors, "✗", "Errors (must fix before starting klangkd):"
    )
    append_failure_block(
        lines, warnings, "⚠", "Warnings (recommended but not required):"
    )
    return "\n".join(lines)


def format_report(report: DoctorReport) -> str:
    """Format a doctor report for terminal output."""
    lines: list[str] = []
    _report_header(lines)
    for r in report.results:
        _append_result_line(lines, r)
    lines.append("")
    return _finish_report(lines, report)


def doctor_main(verbose: bool = False) -> int:
    """Run doctor and print results. Returns exit code."""
    report = run_doctor(verbose=verbose)
    print(format_report(report))
    return 0 if report.passed else 1
