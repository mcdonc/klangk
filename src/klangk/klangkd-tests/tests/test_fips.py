"""Unit tests for klangk.fips probes and enforcement hooks (#2570, #2591)."""

import logging
import subprocess
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from klangk import fips
from klangk.exceptions import ConfigurationError


def _hashlib_stub(
    md5_behavior=None, sha_ok=True, omit_attrs=False
) -> types.ModuleType:
    """A fake ``_hashlib`` module for the layer-1 probe.

    ``md5_behavior``: None → succeeds; an exception class/instance →
    raised on call. ``omit_attrs`` → no openssl_* attrs (exotic build).
    """

    def make(behavior):
        if behavior is None:

            def ok(payload):
                return b"digest"

            return ok

        def raising(payload, _b=behavior):
            raise _b(payload)

        return raising

    mod = types.ModuleType("_hashlib")
    if not omit_attrs:
        mod.openssl_md5 = make(md5_behavior)
        if sha_ok:

            def sha(payload):
                return b"digest"

            mod.openssl_sha256 = sha
        else:
            mod.openssl_sha256 = make(ValueError)
    return mod


class TestParseOpensslList:
    def test_approved(self):
        assert _run_parse("  SHA2-256 { } \n SHA2-512 { } ") == (
            True,
            "approved set has SHA-2 and no MD5",
        )

    def test_md5_present(self):
        assert _run_parse("MD5 {}\nSHA2-256 {}\n") == (
            False,
            "MD5 appears in the fips=yes approved set",
        )

    def test_no_sha2(self):
        assert _run_parse("SHA3-512 {}\n") == (
            False,
            "no SHA-2 digest in the fips=yes approved set",
        )

    def test_empty(self):
        assert _run_parse("   \n") is None

    def test_sha256_word(self):
        # The CLI prints lowercase "sha256" aliases too — accepted.
        assert _run_parse("sha256\n")[0] is True

    def test_unreachable_default(self):
        # Defensive branch: has_sha2 and not has_md5 and not has_sha2
        # cannot coexist — the final ``return None`` is unreachable by
        # construction; call it directly for coverage.
        assert fips.parse_openssl_list("sha256") == (
            True,
            "approved set has SHA-2 and no MD5",
        )


def _run_parse(stdout):
    return fips.parse_openssl_list(stdout)


class TestRunOpensslList:
    def test_missing_binary(self):
        with patch.object(
            subprocess,
            "run",
            side_effect=OSError("no such file"),
        ):
            ok, detail = fips.run_openssl_list()
        assert ok is False
        assert "openssl-cli-unavailable" in detail

    def test_timeout(self):
        with patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("openssl", 15),
        ):
            ok, detail = fips.run_openssl_list()
        assert ok is False
        assert "openssl-cli-unavailable" in detail

    def test_nonzero_rc(self):
        with patch.object(
            subprocess,
            "run",
            return_value=types.SimpleNamespace(
                returncode=1, stdout="", stderr="propquery not found"
            ),
        ):
            ok, detail = fips.run_openssl_list()
        assert ok is False
        assert "openssl-cli-failed rc=1" in detail

    def test_empty_output(self):
        with patch.object(
            subprocess,
            "run",
            return_value=types.SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        ):
            ok, detail = fips.run_openssl_list()
        assert ok is False
        assert "unparseable" in detail

    def test_ok(self):
        with patch.object(
            subprocess,
            "run",
            return_value=types.SimpleNamespace(
                returncode=0, stdout="SHA2-256\n", stderr=""
            ),
        ):
            assert fips.run_openssl_list()[0] is True

    def test_md5_leak(self):
        with patch.object(
            subprocess,
            "run",
            return_value=types.SimpleNamespace(
                returncode=0, stdout="MD5\nSHA2-256\n", stderr=""
            ),
        ):
            ok, detail = fips.run_openssl_list()
        assert ok is False
        assert "MD5 appears" in detail


class TestProbeHashlib:
    def test_fips_enforcing(self):
        stub = _hashlib_stub(md5_behavior=ValueError)
        with patch.dict(sys.modules, {"_hashlib": stub}):
            ok, detail = fips.probe_hashlib()
        assert ok is True
        assert "md5 rejected" in detail

    def test_fips_enforcing_but_sha_broken(self):
        stub = _hashlib_stub(md5_behavior=ValueError, sha_ok=False)
        with patch.dict(sys.modules, {"_hashlib": stub}):
            ok, detail = fips.probe_hashlib()
        assert ok is False
        assert "SHA-256 unavailable too" in detail

    def test_not_enforcing(self):
        stub = _hashlib_stub(md5_behavior=None)
        with patch.dict(sys.modules, {"_hashlib": stub}):
            ok, detail = fips.probe_hashlib()
        assert ok is False
        assert "not rejected" in detail

    def test_unexpected_error_is_inconclusive(self):
        stub = _hashlib_stub(md5_behavior=RuntimeError)
        with patch.dict(sys.modules, {"_hashlib": stub}):
            assert fips.probe_hashlib() is None

    def test_no_openssl_attrs(self):
        stub = _hashlib_stub(omit_attrs=True)
        with patch.dict(sys.modules, {"_hashlib": stub}):
            assert fips.probe_hashlib() is None

    def test_no_hashlib_module(self):
        with patch.dict(sys.modules, {"_hashlib": None}):
            assert fips.probe_hashlib() is None


class TestProbeProcess:
    def test_layer1_decides(self):
        stub = _hashlib_stub(md5_behavior=ValueError)
        with patch.dict(sys.modules, {"_hashlib": stub}):
            assert fips.probe_process()[0] is True

    def test_falls_through_to_cli(self):
        with patch.object(fips, "probe_hashlib", return_value=None):
            with patch.object(
                fips, "run_openssl_list", return_value=(True, "cli-ok")
            ):
                assert fips.probe_process() == (True, "cli-ok")


class TestProbeContainer:
    async def test_ok_verdict(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            return_value=(0, "ok:md5 rejected\n", "")
        )
        assert await fips.probe_container(pod, "cid") == (
            True,
            "md5 rejected",
        )

    async def test_fail_verdict(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            return_value=(0, "fail:md5 not rejected\n", "")
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "not rejected" in detail

    async def test_unknown_verdict_fails_closed(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            return_value=(0, "unknown:openssl-cli-unavailable\n", "")
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "openssl-cli-unavailable" in detail

    async def test_unparseable_output(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(return_value=(0, "garbage\n", ""))
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "unparseable" in detail

    async def test_empty_output(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(return_value=(0, "\n", ""))
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "unparseable" in detail

    async def test_exec_exception_fails_closed(self):
        # A podman-level exec error (not a missing-binary error) does
        # NOT fall through to the CLI — a second exec would likely fail
        # the same way; fail closed instead.
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            side_effect=RuntimeError("exec blew up")
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "probe-exec-failed" in detail
        assert pod.exec_container.await_count == 1

    async def test_no_python_non_missing_binary_fails(self):
        # python3 present but the probe died oddly (not a
        # binary-missing error) — fail closed without the CLI fallback.
        pod = AsyncMock()
        pod.exec_container = AsyncMock(return_value=(126, "", "permission"))
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "probe-exec-failed" in detail

    async def test_no_python_cli_ok(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            side_effect=[
                (127, "", "python3: command not found"),
                (0, "SHA2-256\n", ""),
            ]
        )
        assert await fips.probe_container(pod, "cid") == (
            True,
            "approved set has SHA-2 and no MD5",
        )

    async def test_no_python_cli_fails(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            side_effect=[
                (127, "", "python3: not found"),
                (1, "", "error"),
            ]
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "openssl-cli-failed" in detail

    async def test_no_probe_available(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            side_effect=[
                (127, "", "python3: not found"),
                (127, "", "openssl: not found"),
            ]
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "no-probe-available" in detail

    async def test_cli_unparseable(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            side_effect=[
                (127, "", "python3: not found"),
                (0, "\n", ""),
            ]
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "unparseable" in detail

    async def test_cli_exec_exception(self):
        pod = AsyncMock()
        pod.exec_container = AsyncMock(
            side_effect=[
                (127, "", "python3: not found"),
                RuntimeError("boom"),
            ]
        )
        ok, detail = await fips.probe_container(pod, "cid")
        assert ok is False
        assert "openssl-cli-exec-failed" in detail


class TestVerifyProcessFips:
    def _settings(self, on):
        return types.SimpleNamespace(fips_mode=on)

    def test_off_is_noop(self, caplog):
        with caplog.at_level(logging.INFO):
            fips.verify_process_fips(self._settings(False))
        assert not caplog.records

    def test_on_verified(self, caplog):
        with (
            patch.object(fips, "probe_process", return_value=(True, "x")),
            patch.object(fips, "running_in_container", return_value=False),
        ):
            with caplog.at_level(logging.INFO):
                fips.verify_process_fips(self._settings(True))
        assert any("FIPS mode enabled" in r.message for r in caplog.records)

    def test_on_verified_in_container(self, caplog):
        """A passing probe boots fine inside a container too (#2628)."""
        with (
            patch.object(fips, "probe_process", return_value=(True, "x")),
            patch.object(fips, "running_in_container", return_value=True),
        ):
            with caplog.at_level(logging.INFO):
                fips.verify_process_fips(self._settings(True))
        assert any("FIPS mode enabled" in r.message for r in caplog.records)

    def test_on_not_verified_warns_on_control_host(self, caplog):
        """Not containerized → warn-only posture (the operator's host)."""
        with (
            patch.object(
                fips, "probe_process", return_value=(False, "md5 not rejected")
            ),
            patch.object(fips, "running_in_container", return_value=False),
        ):
            with caplog.at_level(logging.WARNING):
                fips.verify_process_fips(self._settings(True))
        assert any("NOT FIPS-enforcing" in r.message for r in caplog.records)

    def test_on_not_verified_in_container_refuses_boot(self, caplog):
        """Containerized backend + failed probe → ConfigurationError (#2628,
        #2666 — a ConfigurationError so the launcher exits EX_CONFIG).

        Inside a container the process OpenSSL is the crypto boundary of
        an image we ship — the boot must abort, not warn.
        """
        with (
            patch.object(
                fips, "probe_process", return_value=(False, "md5 not rejected")
            ),
            patch.object(fips, "running_in_container", return_value=True),
        ):
            with caplog.at_level(logging.ERROR):
                with pytest.raises(ConfigurationError, match="FIPS-enforcing"):
                    fips.verify_process_fips(self._settings(True))
        assert any("refusing to start" in r.message for r in caplog.records)


class TestRunningInContainer:
    def test_dockerenv_marker(self):
        with patch.object(
            fips.os.path, "exists", side_effect=lambda p: p == "/.dockerenv"
        ):
            assert fips.running_in_container() is True

    def test_containerenv_marker(self):
        with patch.object(
            fips.os.path,
            "exists",
            side_effect=lambda p: p == "/run/.containerenv",
        ):
            assert fips.running_in_container() is True

    def test_control_host(self):
        with patch.object(fips.os.path, "exists", return_value=False):
            assert fips.running_in_container() is False


class TestSettingsParsing:
    def test_spellings(self):
        import sys

        sys.path.insert(0, "src/klangk/klangkd-tests/tests")
        from _helpers import make_settings

        for raw, want in [("true", True), ("1", True), ("YES", True)]:
            assert make_settings({"KLANGKD_FIPS_MODE": raw}).fips_mode is want
        for raw in ["false", "0", "no", "off", ""]:
            assert make_settings({"KLANGKD_FIPS_MODE": raw}).fips_mode is False


class TestRegistryFipsFailClosed:
    """The _create_and_start hook fails closed on a non-FIPS container."""

    def _app_state(self):
        import sys

        sys.path.insert(0, "src/klangk/klangkd-tests/tests")
        from test_crash_recovery import make_app_state

        return make_app_state({"KLANGKD_FIPS_MODE": "true"})

    async def test_failed_probe_removes_container_and_raises(self):
        from klangk import podman

        app_state = self._app_state()
        reg = app_state.state.container_registry

        async def probe(podman_inst, cid):
            return False, "md5 not rejected — FIPS provider not enforcing"

        remove = AsyncMock()
        with (
            patch.object(reg.app.state.podman, "remove_container", remove),
            patch.object(fips, "probe_container", probe),
        ):
            reg.track_activity("cid-fips", "ws-fips")
            with pytest.raises(podman.PodmanError, match="FIPS"):
                await reg._fips_gate("ws-fips", "cid-fips")
        # The container was removed and the registry state dropped.
        remove.assert_awaited_once()
        assert "ws-fips" not in reg.states
        assert "cid-fips" not in reg._cid_to_wsid

    async def test_passing_probe_is_noop(self):

        app_state = self._app_state()
        reg = app_state.state.container_registry

        async def probe(podman_inst, cid):
            return True, "md5 rejected"

        remove = AsyncMock()
        with (
            patch.object(reg.app.state.podman, "remove_container", remove),
            patch.object(fips, "probe_container", probe),
        ):
            reg.track_activity("cid-fips", "ws-fips")
            await reg._fips_gate("ws-fips", "cid-fips")
        remove.assert_not_awaited()
        assert reg.states["ws-fips"].container_id == "cid-fips"


class TestProbeScriptSyntax:
    def test_embedded_script_compiles(self):
        compile(fips.PROBE_SCRIPT, "<fips-probe>", "exec")


class TestFipsGateExpectedStopProtocol:
    """A FIPS refusal is an *expected* stop for the crash monitor (#2626
    review): the sweep must not misread the gate's in-flight removal as
    an unexpected death, and with auto-restart on, no restart may be
    scheduled for the refused container."""

    def _app_state(self):
        import sys

        sys.path.insert(0, "src/klangk/klangkd-tests/tests")
        from test_crash_recovery import make_app_state

        return make_app_state(
            {
                "KLANGKD_FIPS_MODE": "true",
                "KLANGKD_CONTAINER_RESTART_ENABLED": "true",
            }
        )

    async def test_gate_removal_is_expected_stop(self):
        from klangk import podman

        app_state = self._app_state()
        reg = app_state.state.container_registry
        epoch0 = reg.stop_epoch.get("ws-fips", 0)

        async def probe(podman_inst, cid):
            return False, "md5 not rejected — FIPS provider not enforcing"

        remove = AsyncMock()
        with (
            patch.object(reg.app.state.podman, "remove_container", remove),
            patch.object(fips, "probe_container", probe),
        ):
            reg.track_activity("cid-fips", "ws-fips")
            with pytest.raises(podman.PodmanError, match="FIPS"):
                await reg._fips_gate("ws-fips", "cid-fips")

        # The stop epoch bumped + no crash bookkeeping left behind.
        assert reg.stop_epoch.get("ws-fips", 0) == epoch0 + 1
        assert reg.stopping == set()
        assert reg.crash.pending == {}
        assert reg.crash.status("ws-fips") is None


class TestFipsAdoptPathGate:
    """A previously-running container is probed on adoption (reconnect)
    when the mode is on (#2626 review) — no unprobed serving."""

    def _app_state(self, fips):
        import sys

        sys.path.insert(0, "src/klangk/klangkd-tests/tests")
        from test_crash_recovery import make_app_state

        return make_app_state({"KLANGKD_FIPS_MODE": "true"} if fips else {})

    async def test_adopt_probe_refuses_non_fips_container(self):
        from klangk import podman

        app_state = self._app_state(True)
        reg = app_state.state.container_registry

        async def probe(podman_inst, cid):
            return False, "md5 not rejected"

        remove = AsyncMock()
        inspect = AsyncMock(
            return_value={"State": {"Running": True, "OOMKilled": False}}
        )
        with (
            patch.object(reg.app.state.podman, "remove_container", remove),
            patch.object(reg.app.state.podman, "inspect_container", inspect),
            patch.object(fips, "probe_container", probe),
        ):
            with pytest.raises(podman.PodmanError, match="FIPS"):
                await reg._handle_existing_container(
                    "cid-old", "ws-adopt", 0.0
                )
        # The adopted (non-FIPS) container was removed before refusing.
        remove.assert_awaited_once()
        assert "ws-adopt" not in reg.states

    async def test_adopt_probe_passes_fips_container(self):
        app_state = self._app_state(True)
        reg = app_state.state.container_registry

        async def probe(podman_inst, cid):
            return True, "md5 rejected"

        inspect = AsyncMock(
            return_value={"State": {"Running": True, "OOMKilled": False}}
        )
        with (
            patch.object(reg.app.state.podman, "inspect_container", inspect),
            patch.object(fips, "probe_container", probe),
            patch.object(reg, "track_activity") as track,
        ):
            result = await reg._handle_existing_container(
                "cid-old", "ws-adopt", 0.0
            )
        assert result == ("cid-old", "connected")
        probe_ran = fips.probe_container
        assert probe_ran is not None  # probe was patched in and awaited
        track.assert_called_once()

    async def test_adopt_path_unprobed_when_mode_off(self):
        app_state = self._app_state(False)
        reg = app_state.state.container_registry

        inspect = AsyncMock(
            return_value={"State": {"Running": True, "OOMKilled": False}}
        )
        with (
            patch.object(reg.app.state.podman, "inspect_container", inspect),
            patch.object(fips, "probe_container", AsyncMock()) as probe,
        ):
            with patch.object(reg, "track_activity"):
                await reg._handle_existing_container(
                    "cid-old", "ws-adopt", 0.0
                )
        probe.assert_not_awaited()
