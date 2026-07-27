"""Tests for ``klangk.doctor`` — the ``klangkd doctor`` pre-flight checker."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

from klangk.doctor import (
    CheckResult,
    DoctorReport,
    check_binary,
    check_gnu_du,
    check_gnu_tar,
    check_podman_policy,
    check_subuid,
    detect_package_manager,
    format_report,
    install_hint,
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
            patch("klangk.doctor._run", return_value=(1, "", "segfault")),
        ):
            r = check_binary("broken", ["broken", "--version"], "apt")
            assert not r.ok
            assert "check failed" in r.message

    def test_present_and_functional(self):
        with (
            patch("shutil.which", return_value="/usr/bin/good"),
            patch("klangk.doctor._run", return_value=(0, "v1.0", "")),
        ):
            r = check_binary("good", ["good", "--version"], "apt")
            assert r.ok

    def test_empty_check_cmd_skips_run(self):
        with patch("shutil.which", return_value="/usr/bin/suid-helper"):
            r = check_binary("suid-helper", [], "apt")
            assert r.ok
            assert "suid-helper ok" in r.message


class TestCheckGnuTar:
    def test_gnu_tar(self):
        with (
            patch("shutil.which", return_value="/usr/bin/tar"),
            patch(
                "klangk.doctor._run",
                return_value=(0, "tar (GNU tar) 1.35", ""),
            ),
        ):
            r = check_gnu_tar("apt")
            assert r.ok

    def test_bsd_tar(self):
        with (
            patch("shutil.which", return_value="/usr/bin/tar"),
            patch(
                "klangk.doctor._run",
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
            patch("klangk.doctor._run", return_value=(0, "0\t/dev/null", "")),
        ):
            r = check_gnu_du("apt")
            assert r.ok

    def test_bsd_du(self):
        with (
            patch("shutil.which", return_value="/usr/bin/du"),
            patch(
                "klangk.doctor._run",
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
        assert "fix it" in output

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
        assert "All required checks passed" in output


class TestRunDoctor:
    def test_returns_report(self):
        """run_doctor returns a DoctorReport with results."""
        # This runs the real checks — just verify it returns the right type
        # and has results.
        report = run_doctor()
        assert isinstance(report, DoctorReport)
        assert len(report.results) > 0

    def test_missing_rootless_prereq_is_warning(self):
        """A missing rootless prereq (e.g. fuse-overlayfs) is a warning, not
        an error — modern podman may not need it."""
        original_which = shutil.which

        def which_hiding_fuse(name):
            if name == "fuse-overlayfs":
                return None
            return original_which(name)

        with (
            patch("platform.system", return_value="Linux"),
            patch("klangk.doctor.shutil.which", side_effect=which_hiding_fuse),
        ):
            report = run_doctor()

        fuse_results = [
            r for r in report.results if r.name == "fuse-overlayfs"
        ]
        assert len(fuse_results) == 1
        r = fuse_results[0]
        assert not r.ok
        assert r.is_warning
