"""Unit tests for the sidecar readiness probe's stall handling (#3120).

``_follow_logs_until`` (``test_network_sidecar_e2e.py``) drives sidecar
readiness via snapshot-then-follow (#3062). On the CI runners the one-shot
``podman logs`` snapshot itself can wedge (20 s hang, SIGKILL) — the
resulting ``subprocess.TimeoutExpired`` used to escape every safety net
(deadline, stopped-container fast-fail, degrade-to-poll) and ERROR the
fixture. The probe now treats a wedged snapshot or state probe as "no data
this cycle" and retries within its deadline. These tests exercise that
logic hermetically by scripting the module's ``podman`` helper; real podman
is the e2e suite's job.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

# pytest 9 dropped the top-level ``pytest.Failed`` name; the class stays
# reachable as the exception owned by the ``pytest.fail`` factory.
Failed = pytest.fail.Exception

_E2E_FILE = os.path.realpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "e2e-tests",
        "test_network_sidecar_e2e.py",
    )
)


def load_probe_module(monkeypatch):
    """Import the e2e module under a throwaway name (cf. test_e2e_imports)."""
    monkeypatch.syspath_prepend(os.path.dirname(_E2E_FILE))
    spec = importlib.util.spec_from_file_location("netsc_e2e_probe", _E2E_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _wedge(*args, check=True, timeout=120):
    """A podman call that hangs until the subprocess timeout kills it."""
    raise subprocess.TimeoutExpired(["podman", *args], timeout)


class TestSnapshotStallContract:
    def test_logs_snapshot_wedge_yields_no_data_and_flag(self, monkeypatch):
        mod = load_probe_module(monkeypatch)
        monkeypatch.setattr(mod, "podman", _wedge)
        assert mod._logs_snapshot("nc") == ("", True)

    def test_container_state_wedge_yields_unknown(self, monkeypatch):
        mod = load_probe_module(monkeypatch)
        monkeypatch.setattr(mod, "podman", _wedge)
        assert mod._container_state("nc") == ("", True)

    def test_deadline_msg_counts_wedged_probes(self, monkeypatch):
        mod = load_probe_module(monkeypatch)
        assert mod._deadline_msg("not ready", 40, "log line", 2) == (
            "not ready within 40s (podman probes stalled 2x)\nlogs:\nlog line"
        )
        assert mod._deadline_msg("not ready", 40, "log line", 0) == (
            "not ready within 40s\nlogs:\nlog line"
        )


class TestFollowLogsUntil:
    def test_needle_in_history_never_follows(self, monkeypatch):
        mod = load_probe_module(monkeypatch)

        def fake_podman(*args, check=True, timeout=120):
            stdout = (
                "dns-proxy listening\n" if args[0] == "logs" else "running 0"
            )
            return subprocess.CompletedProcess(
                args, 0, stdout=stdout, stderr=""
            )

        monkeypatch.setattr(mod, "podman", fake_podman)

        def fail_follow(*args):
            raise AssertionError(
                "follow must not run when the snapshot already has the needle"
            )

        monkeypatch.setattr(mod, "_follow_once", fail_follow)
        mod._follow_logs_until("nc", "dns-proxy listening", 5, "not ready")

    def test_wedged_snapshot_and_probe_are_retried_not_raised(
        self, monkeypatch
    ):
        # #3120 regression: the first logs AND inspect calls wedge; the
        # probe must ride it out and find the needle on the next cycle.
        mod = load_probe_module(monkeypatch)
        calls = []

        def fake_podman(*args, check=True, timeout=120):
            calls.append(args[0])
            if calls.count(args[0]) == 1:
                raise subprocess.TimeoutExpired(["podman", *args], timeout)
            stdout = (
                "dns-proxy listening\n" if args[0] == "logs" else "running 0"
            )
            return subprocess.CompletedProcess(
                args, 0, stdout=stdout, stderr=""
            )

        monkeypatch.setattr(mod, "podman", fake_podman)
        monkeypatch.setattr(mod, "_follow_once", lambda *args: b"")
        mod._follow_logs_until("nc", "dns-proxy listening", 10, "not ready")
        assert calls.count("logs") == 2  # one wedge cycle, then success

    def test_persistent_wedge_fails_within_deadline_with_stall_note(
        self, monkeypatch
    ):
        mod = load_probe_module(monkeypatch)

        def fake_podman(*args, check=True, timeout=120):
            if args[0] == "logs":
                raise subprocess.TimeoutExpired(["podman", *args], timeout)
            return subprocess.CompletedProcess(
                args, 0, stdout="running 0", stderr=""
            )

        monkeypatch.setattr(mod, "podman", fake_podman)
        monkeypatch.setattr(mod, "_follow_once", lambda *args: b"")
        with pytest.raises(Failed, match=r"stalled 1x"):
            mod._follow_logs_until("nc", "dns-proxy listening", 0, "not ready")

    def test_exited_container_still_fails_fast(self, monkeypatch):
        mod = load_probe_module(monkeypatch)

        def fake_podman(*args, check=True, timeout=120):
            stdout = "" if args[0] == "logs" else "exited 1"
            return subprocess.CompletedProcess(
                args, 0, stdout=stdout, stderr=""
            )

        monkeypatch.setattr(mod, "podman", fake_podman)
        monkeypatch.setattr(mod, "_follow_once", lambda *args: b"")
        with pytest.raises(Failed, match="container exited"):
            mod._follow_logs_until("nc", "dns-proxy listening", 5, "not ready")


class StubFollow:
    """Just enough Popen surface for ``_reap_follow`` (stdout is self)."""

    def __init__(self, unkillable=False):
        self.unkillable = unkillable
        self.returncode = None
        self.killed = False
        self.closed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if not self.unkillable:
            self.returncode = -9

    def wait(self, timeout=None):
        if self.unkillable:
            raise subprocess.TimeoutExpired(["podman", "logs"], timeout)
        return self.returncode

    def close(self):
        self.closed = True

    @property
    def stdout(self):
        return self


class TestReapFollow:
    def test_already_exited_follow_is_just_closed(self, monkeypatch):
        mod = load_probe_module(monkeypatch)
        stub = StubFollow()
        stub.returncode = 0
        mod._reap_follow(stub)
        assert not stub.killed and stub.closed

    def test_reap_kills_and_closes(self, monkeypatch):
        mod = load_probe_module(monkeypatch)
        stub = StubFollow()
        mod._reap_follow(stub)
        assert stub.killed and stub.returncode == -9 and stub.closed

    def test_reap_abandons_unkillable_follow(self, monkeypatch):
        # D-state podman: kill lands, wait never completes — reap must
        # still close the pipe and return instead of hanging on it.
        mod = load_probe_module(monkeypatch)
        stub = StubFollow(unkillable=True)
        mod._reap_follow(stub)
        assert stub.killed and stub.closed
