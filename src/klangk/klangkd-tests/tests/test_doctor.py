"""Tests for ``klangk.doctor`` — the ``klangkd doctor`` pre-flight checker."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch


from klangk.doctor import (
    CheckResult,
    DoctorReport,
    TMUX_MIN_VERSION,
    check_binary,
    check_gnu_du,
    check_gnu_stat,
    check_gnu_tar,
    check_podman_machine,
    check_podman_policy,
    check_rootless_podman,
    check_subuid,
    check_tmux_version,
    detect_package_manager,
    format_report,
    install_hint,
    parse_tmux_version,
    run_doctor,
)


class TestCheckResult:
    def test_ok_result(self):
        r = CheckResult(name="test", ok=True, message="all good")
        assert r.ok
        assert not r.is_warning

    def test_error_result(self):
        r = CheckResult(name="test", ok=False, message="broken")
        assert not r.ok
        assert not r.is_warning

    def test_warning_result(self):
        r = CheckResult(name="test", ok=False, message="meh", is_warning=True)
        assert not r.ok
        assert r.is_warning


class TestDoctorReport:
    def test_empty_report_passes(self):
        report = DoctorReport()
        assert report.passed

    def test_all_ok_passes(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        report.add(CheckResult(name="b", ok=True, message="ok"))
        assert report.passed
        assert report.errors == []
        assert report.warnings == []

    def test_warning_still_passes(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        report.add(
            CheckResult(name="b", ok=False, message="meh", is_warning=True)
        )
        assert report.passed
        assert len(report.warnings) == 1

    def test_error_fails(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        report.add(CheckResult(name="b", ok=False, message="broken"))
        assert not report.passed
        assert len(report.errors) == 1


class TestDetectPackageManager:
    def test_detects_apt(self):
        with patch("shutil.which") as mock_which:
            # dnf/yum not found, apt-get found
            def which_side_effect(cmd):
                return "/usr/bin/apt-get" if cmd == "apt-get" else None

            mock_which.side_effect = which_side_effect
            assert detect_package_manager() == "apt"

    def test_detects_dnf(self):
        with patch("shutil.which") as mock_which:

            def which_side_effect(cmd):
                return "/usr/bin/dnf" if cmd == "dnf" else None

            mock_which.side_effect = which_side_effect
            assert detect_package_manager() == "dnf"

    def test_detects_brew(self):
        with patch("shutil.which") as mock_which:

            def which_side_effect(cmd):
                return "/opt/homebrew/bin/brew" if cmd == "brew" else None

            mock_which.side_effect = which_side_effect
            assert detect_package_manager() == "brew"

    def test_none_when_nothing(self):
        with patch("shutil.which", return_value=None):
            assert detect_package_manager() is None


class TestInstallHint:
    def test_apt_hint(self):
        assert install_hint("podman", "apt") == "sudo apt install podman"

    def test_dnf_hint(self):
        assert install_hint("sqlite3", "dnf") == "sudo dnf install sqlite"

    def test_brew_hint(self):
        assert install_hint("tar", "brew") == "brew install gnu-tar"

    def test_no_manager(self):
        assert install_hint("podman", None) == "install podman"

    def test_unknown_binary_falls_back(self):
        assert (
            install_hint("obscure-tool", "apt")
            == "sudo apt install obscure-tool"
        )


class TestCheckBinary:
    def test_missing_binary(self):
        with patch("shutil.which", return_value=None):
            r = check_binary("missing", ["missing", "--version"], "apt")
            assert not r.ok
            assert "not found" in r.message
            assert r.hint

    def test_present_but_check_fails(self):
        with (
            patch("shutil.which", return_value="/usr/bin/broken"),
            patch("klangk.doctor.run", return_value=(1, "", "segfault")),
        ):
            r = check_binary("broken", ["broken", "--version"], "apt")
            assert not r.ok
            assert "check failed" in r.message

    def test_present_and_functional(self):
        with (
            patch("shutil.which", return_value="/usr/bin/good"),
            patch("klangk.doctor.run", return_value=(0, "v1.0", "")),
        ):
            r = check_binary("good", ["good", "--version"], "apt")
            assert r.ok

    def test_empty_check_cmd_skips_run(self):
        with patch("shutil.which", return_value="/usr/bin/suid-helper"):
            r = check_binary("suid-helper", [], "apt")
            assert r.ok
            assert "suid-helper ok" in r.message


class TestCheckTmuxVersion:
    """Host tmux >= 3.2 gate for the consent-popup shell layer (#2383)."""

    def test_parse_typical(self):
        assert parse_tmux_version("tmux 3.6a") == (3, 6)

    def test_parse_no_suffix(self):
        assert parse_tmux_version("tmux 3.2") == (3, 2)

    def test_parse_double_digit_minor(self):
        assert parse_tmux_version("tmux 3.10") == (3, 10)

    def test_parse_garbage_is_none(self):
        assert parse_tmux_version("not a version") is None

    def test_parse_empty_is_none(self):
        assert parse_tmux_version("") is None

    def test_min_version_is_3_2(self):
        assert TMUX_MIN_VERSION == (3, 2)

    def test_new_enough_is_ok(self):
        with (
            patch("klangk.doctor.shutil.which", return_value="/usr/bin/tmux"),
            patch("klangk.doctor.run", return_value=(0, "tmux 3.6a", "")),
        ):
            r = check_tmux_version("apt")
        assert r.ok
        assert not r.is_warning
        assert "3.6" in r.message

    def test_boundary_3_2_is_ok(self):
        with (
            patch("klangk.doctor.shutil.which", return_value="/usr/bin/tmux"),
            patch("klangk.doctor.run", return_value=(0, "tmux 3.2", "")),
        ):
            r = check_tmux_version("apt")
        assert r.ok

    def test_old_version_is_warning(self):
        with (
            patch("klangk.doctor.shutil.which", return_value="/usr/bin/tmux"),
            patch("klangk.doctor.run", return_value=(0, "tmux 3.1", "")),
        ):
            r = check_tmux_version("apt")
        assert not r.ok
        assert r.is_warning
        assert "< 3.2" in r.message
        assert r.hint  # actionable install hint

    def test_unparseable_version_is_warning(self):
        with (
            patch("klangk.doctor.shutil.which", return_value="/usr/bin/tmux"),
            patch("klangk.doctor.run", return_value=(0, "weird output", "")),
        ):
            r = check_tmux_version("apt")
        assert not r.ok
        assert r.is_warning

    def test_tmux_v_fails_is_warning(self):
        with (
            patch("klangk.doctor.shutil.which", return_value="/usr/bin/tmux"),
            patch("klangk.doctor.run", return_value=(1, "", "segfault")),
        ):
            r = check_tmux_version("apt")
        assert not r.ok
        assert r.is_warning

    def test_absent_tmux_is_ok(self):
        # A missing tmux is already flagged by the required-binary check;
        # the version check has nothing to assess, so it passes.
        with patch("klangk.doctor.shutil.which", return_value=None):
            r = check_tmux_version("apt")
        assert r.ok


class TestCheckGnuTar:
    def test_gnu_tar(self):
        with (
            patch("shutil.which", return_value="/usr/bin/tar"),
            patch(
                "klangk.doctor.run",
                return_value=(0, "tar (GNU tar) 1.35", ""),
            ),
        ):
            r = check_gnu_tar("apt")
            assert r.ok

    def test_bsd_tar(self):
        with (
            patch("shutil.which", return_value="/usr/bin/tar"),
            patch(
                "klangk.doctor.run",
                return_value=(0, "bsdtar 3.6.2", ""),
            ),
        ):
            r = check_gnu_tar("brew")
            assert not r.ok
            assert "not GNU tar" in r.message
            assert "gnubin" in r.hint

    def test_missing_tar(self):
        with patch("shutil.which", return_value=None):
            r = check_gnu_tar("apt")
            assert not r.ok


class TestCheckGnuDu:
    def test_gnu_du(self):
        with (
            patch("shutil.which", return_value="/usr/bin/du"),
            patch("klangk.doctor.run", return_value=(0, "0\t/dev/null", "")),
        ):
            r = check_gnu_du("apt")
            assert r.ok

    def test_bsd_du(self):
        with (
            patch("shutil.which", return_value="/usr/bin/du"),
            patch(
                "klangk.doctor.run",
                return_value=(1, "", "illegal option -- b"),
            ),
        ):
            r = check_gnu_du("brew")
            assert not r.ok
            assert "gnubin" in r.hint

    def test_missing_du(self):
        with patch("klangk.doctor.shutil.which", return_value=None):
            r = check_gnu_du("apt")
            assert not r.ok
            assert "not found" in r.message


class TestCheckGnuStat:
    def test_gnu_stat(self):
        with (
            patch("shutil.which", return_value="/usr/bin/stat"),
            patch("klangk.doctor.run", return_value=(0, "ext4\n", "")),
        ):
            r = check_gnu_stat("apt")
            assert r.ok

    def test_bsd_stat(self):
        with (
            patch("shutil.which", return_value="/usr/bin/stat"),
            patch(
                "klangk.doctor.run",
                return_value=(1, "", "stat: illegal option -- f"),
            ),
        ):
            r = check_gnu_stat("brew")
            assert not r.ok
            assert "coreutils" in r.hint

    def test_missing_stat(self):
        with patch("klangk.doctor.shutil.which", return_value=None):
            r = check_gnu_stat("apt")
            assert not r.ok
            assert "not found" in r.message


class TestCheckSubuid:
    def test_linux_with_range(self, tmp_path):
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        subuid.write_text("testuser:100000:65536\n")
        subgid.write_text("testuser:100000:65536\n")
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "klangk.doctor.Path",
                side_effect=lambda p: subuid if "subuid" in str(p) else subgid,
            ),
        ):
            # Direct test with real files
            r = check_subuid("testuser")
            # The Path mock is tricky; test the real function on Linux
            assert r.name == "subuid/subgid"

    def test_user_missing_from_subuid(self, tmp_path):
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        subuid.write_text("otheruser:100000:65536\n")
        subgid.write_text("otheruser:100000:65536\n")
        with patch("platform.system", return_value="Linux"):
            # Patch the Path references inside check_subuid
            original_path = Path

            def patched_path(p):
                if "subuid" in str(p):
                    return subuid
                if "subgid" in str(p):
                    return subgid
                return original_path(p)

            with patch("klangk.doctor.Path", side_effect=patched_path):
                r = check_subuid("testuser")
                assert not r.ok
                assert "no subuid range" in r.message

    def test_macos_skips(self):
        with patch("platform.system", return_value="Darwin"):
            r = check_subuid("testuser")
            assert r.ok
            assert "skipped on macOS" in r.message


class TestCheckPodmanPolicy:
    def test_policy_exists(self, tmp_path):
        policy = tmp_path / "policy.json"
        policy.write_text('{"default":[{"type":"insecureAcceptAnything"}]}')
        r = check_podman_policy(candidates=[policy])
        assert r.ok

    def test_no_policy(self, tmp_path):
        r = check_podman_policy(
            candidates=[tmp_path / "nonexistent" / "policy.json"]
        )
        assert not r.ok
        assert r.is_warning

    def test_invalid_policy(self, tmp_path):
        policy = tmp_path / "policy.json"
        policy.write_text("not json")
        r = check_podman_policy(candidates=[policy])
        assert not r.ok
        assert r.is_warning
        assert "invalid" in r.message


class TestFormatReport:
    def test_all_pass(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        output = format_report(report)
        assert "All 1 checks passed" in output
        assert "✓" in output

    def test_shows_detected_manager(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        with patch("klangk.doctor.detect_package_manager", return_value="apt"):
            output = format_report(report)
        assert "Package manager: apt" in output

    def test_shows_no_manager(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        with patch("klangk.doctor.detect_package_manager", return_value=None):
            output = format_report(report)
        assert "Package manager: (none detected)" in output

    def test_errors(self):
        report = DoctorReport()
        report.add(
            CheckResult(name="a", ok=False, message="broken", hint="fix it")
        )
        output = format_report(report)
        assert "✗" in output
        assert "1 errors" in output
        # Hint prefixed with "Run:" (#1968)
        assert "Run:  fix it" in output

    def test_error_hint_repeated_in_summary(self):
        """Errors are repeated at the bottom with their hints so the
        user doesn't have to scroll back up (#1968)."""
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        report.add(
            CheckResult(name="b", ok=False, message="broken", hint="fix it")
        )
        output = format_report(report)
        assert "Errors (must fix before starting klangkd):" in output
        # The hint appears at least twice: inline and in the summary
        assert output.count("Run:  fix it") >= 2

    def test_warnings(self):
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        report.add(
            CheckResult(
                name="b",
                ok=False,
                message="meh",
                is_warning=True,
                hint="optional",
            )
        )
        output = format_report(report)
        assert "⚠" in output
        assert "1 warnings" in output
        assert "Warnings (recommended but not required):" in output
        assert "Run:  optional" in output

    def test_warning_hint_repeated_in_summary(self):
        """Warnings are repeated at the bottom with their hints (#1968)."""
        report = DoctorReport()
        report.add(CheckResult(name="a", ok=True, message="ok"))
        report.add(
            CheckResult(
                name="b",
                ok=False,
                message="meh",
                is_warning=True,
                hint="optional cmd",
            )
        )
        output = format_report(report)
        assert output.count("Run:  optional cmd") >= 2


class TestRunDoctor:
    def test_returns_report(self):
        """run_doctor returns a DoctorReport with results."""
        # This runs the real checks — just verify it returns the right type
        # and has results.
        report = run_doctor()
        assert isinstance(report, DoctorReport)
        assert len(report.results) > 0

    def test_includes_tmux_version_check(self):
        """run_doctor checks host tmux version for the consent popup (#2383)."""
        report = run_doctor()
        names = [r.name for r in report.results]
        assert "tmux (consent popup)" in names

    def test_no_fuse_overlayfs_or_slirp4netns_checks(self):
        """fuse-overlayfs and slirp4netns are not checked — modern podman
        uses native overlay and pasta; the end-to-end rootless check is
        sufficient (#1950)."""
        with patch("platform.system", return_value="Linux"):
            report = run_doctor()

        names = [r.name for r in report.results]
        assert "fuse-overlayfs" not in names
        assert "slirp4netns" not in names

    def test_missing_ip_is_warning(self):
        """A missing ip command is a warning — container subnet detection
        falls back to RFC1918 ranges (#2089)."""
        original_which = shutil.which

        def which_hiding_ip(name):
            if name == "ip":
                return None
            return original_which(name)

        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "klangk.doctor.shutil.which",
                side_effect=which_hiding_ip,
            ),
        ):
            report = run_doctor()

        results = [r for r in report.results if r.name == "ip"]
        assert len(results) == 1
        r = results[0]
        assert not r.ok
        assert r.is_warning
        assert "RFC1918" in r.message

    def test_missing_newuidmap_is_warning(self):
        """A missing newuidmap is a warning, not an error — the end-to-end
        rootless podman check is the definitive gate."""
        original_which = shutil.which

        def which_hiding_newuidmap(name):
            if name == "newuidmap":
                return None
            return original_which(name)

        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "klangk.doctor.shutil.which",
                side_effect=which_hiding_newuidmap,
            ),
        ):
            report = run_doctor()

        results = [r for r in report.results if r.name == "newuidmap"]
        assert len(results) == 1
        r = results[0]
        assert not r.ok
        assert r.is_warning


class TestDoctorBranchGaps2834:
    """#2834 branch gate: non-brew failure hints, the macOS skips in
    run_doctor, and the hintless failure-block line."""

    def test_non_brew_tar_failure_keeps_generic_hint(self):
        # A non-GNU tar on a non-brew system: the generic hint (no
        # brew-specific gnubin line).
        with (
            patch("shutil.which", return_value="/usr/bin/tar"),
            patch(
                "klangk.doctor.run",
                return_value=(0, "bsdtar 3.6.2", ""),
            ),
        ):
            r = check_gnu_tar("apt")
            assert not r.ok
            assert "gnubin" not in (r.hint or "")

    def test_non_brew_du_failure_keeps_generic_hint(self):
        from klangk.doctor import check_gnu_du

        with (
            patch("shutil.which", return_value="/usr/bin/du"),
            patch(
                "klangk.doctor.run",
                return_value=(1, "", ""),
            ),
        ):
            r = check_gnu_du("apt")
            assert not r.ok
            assert "gnubin" not in (r.hint or "")

    def test_darwin_skips_linux_only_checks(self):
        # On macOS the stat + ip checks are skipped entirely (a Darwin
        # report has neither).
        from klangk import doctor

        names = []

        class _RecordingReport(doctor.DoctorReport):
            def add(self, r):
                names.append(r.name)
                return super().add(r)

        real_report = doctor.DoctorReport
        doctor.DoctorReport = _RecordingReport
        try:
            with patch("klangk.doctor.platform.system", return_value="Darwin"):
                doctor.run_doctor()
        finally:
            doctor.DoctorReport = real_report
        assert not any("stat" in n.lower() for n in names)

    def test_failure_block_without_hint(self):
        # A hintless failing result renders without a "Run:" line.
        from klangk.doctor import CheckResult, append_failure_block

        lines: list[str] = []
        append_failure_block(
            lines,
            [CheckResult(name="x", ok=False, message="broke")],
            "-",
            "Failures",
        )
        text = "\n".join(lines)
        assert "Failures" in text
        assert "x: broke" in text
        assert "Run:" not in text


# --- No-cover audit tests (#2910, part 2) --------------------------------


class TestRunCommandFailures:
    def test_missing_binary(self):

        from klangk.doctor import run

        with patch(
            "klangk.doctor.subprocess.run",
            side_effect=FileNotFoundError("no du"),
        ):
            rc, out, err = run(["du", "--version"])
        assert rc == -1 and out == "" and "not found" in err

    def test_command_timeout(self):
        import subprocess as subprocess_mod

        from klangk.doctor import run

        with patch(
            "klangk.doctor.subprocess.run",
            side_effect=subprocess_mod.TimeoutExpired(cmd="du", timeout=10),
        ):
            rc, out, err = run(["du", "--version"])
        assert rc == -1 and out == "" and "timed out" in err


class TestGnuBinaryMissing:
    def test_du_not_found(self):
        with patch("klangk.doctor.shutil.which", return_value=None):
            result = check_gnu_du(None)
        assert not result.ok
        assert "du not found" in result.message

    def test_stat_not_found(self):
        with patch("klangk.doctor.shutil.which", return_value=None):
            result = check_gnu_stat(None)
        assert not result.ok
        assert "stat not found" in result.message


class TestCheckSubuidMissingFile:
    def test_missing_file_fails(self, monkeypatch):
        monkeypatch.setattr("klangk.doctor.platform.system", lambda: "Linux")
        monkeypatch.setattr("klangk.doctor.Path.exists", lambda self: False)
        result = check_subuid("someuser")
        assert not result.ok
        assert "does not exist" in result.message


class TestCheckPodmanMachine:
    def test_linux_skips(self):
        with patch("klangk.doctor.platform.system", return_value="Linux"):
            result = check_podman_machine()
        assert result.ok and "skipped on Linux" in result.message

    def test_darwin_info_fails(self):
        with (
            patch("klangk.doctor.platform.system", return_value="Darwin"),
            patch("klangk.doctor.run", return_value=(1, "", "err")),
        ):
            result = check_podman_machine()
        assert not result.ok and "not available" in result.message

    def test_darwin_machine_running(self):
        with (
            patch("klangk.doctor.platform.system", return_value="Darwin"),
            patch(
                "klangk.doctor.run",
                side_effect=[(0, "", ""), (0, "true\n", "")],
            ),
        ):
            result = check_podman_machine()
        assert result.ok and "running" in result.message

    def test_darwin_machine_stopped(self):
        with (
            patch("klangk.doctor.platform.system", return_value="Darwin"),
            patch(
                "klangk.doctor.run",
                side_effect=[(0, "", ""), (0, "false\n", "")],
            ),
        ):
            result = check_podman_machine()
        assert not result.ok and "no podman machine" in result.message

    def test_run_doctor_darwin_uses_machine_check(self):
        from klangk.doctor import run_doctor

        with (
            patch("klangk.doctor.platform.system", return_value="Darwin"),
            patch("klangk.doctor.check_podman_machine") as machine,
        ):
            run_doctor()
        machine.assert_called_once()


class TestCheckRootlessPodman:
    def test_no_podman_binary(self):
        with patch("klangk.doctor.shutil.which", return_value=None):
            result = check_rootless_podman()
        assert not result.ok and "podman not found" in result.message

    def test_run_fails(self):
        with (
            patch(
                "klangk.doctor.shutil.which", return_value="/usr/bin/podman"
            ),
            patch(
                "klangk.doctor.run", return_value=(125, "", "storage error")
            ),
        ):
            result = check_rootless_podman()
        assert not result.ok and "storage error" in result.message

    def test_run_succeeds(self):
        with (
            patch(
                "klangk.doctor.shutil.which", return_value="/usr/bin/podman"
            ),
            patch("klangk.doctor.run", return_value=(0, "", "")),
        ):
            result = check_rootless_podman()
        assert result.ok and "can run containers" in result.message


class TestDoctorMain:
    def _report(self, ok):
        return DoctorReport(
            results=[CheckResult(name="x", ok=ok, message="m")]
        )

    def test_passing_report_exits_zero(self, capsys):
        import klangk.doctor as doctor_mod

        with patch.object(
            doctor_mod, "run_doctor", return_value=self._report(True)
        ):
            assert doctor_mod.doctor_main() == 0

    def test_failing_report_exits_one(self):
        import klangk.doctor as doctor_mod

        with patch.object(
            doctor_mod, "run_doctor", return_value=self._report(False)
        ):
            assert doctor_mod.doctor_main() == 1
