"""Unit tests for the controlled-DNS e2e fixture helpers.

These cover the container-reclamation logic added for #2443 (the
``ctrl-dns-*`` fixtures carry no ``klangk.*`` labels, so klangkd's dead-owner
reaper cannot see them — ``cleanup_stale_containers`` is the only sweep path
for them). ``subprocess.run`` is faked so no real ``podman`` is needed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Make the e2e-tests dir importable (it's not on sys.path by default for the
# unit test suite) — same pattern as test_e2e_env.py.
_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e-tests"
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))

import _controlled_dns as cd  # type: ignore[import-not-found]  # noqa: E402


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["podman"], 0, stdout=stdout, stderr="")


def test_cleanup_removes_only_ctrl_dns_fixtures(monkeypatch):
    """Only ``ctrl-dns-target-*`` / ``ctrl-dns-dns-*`` are removed; the rest
    of ``podman ps`` is left alone, and each removal uses ``podman rm -f``."""
    invocations: list[list[str]] = []

    def fake_run(args, **kwargs):
        invocations.append(list(args))
        if args[1] == "ps":
            return _ok(
                "ctrl-dns-target-111\n"
                "klangk-net-smoke-abc\n"  # not a fixture -> ignored
                "ctrl-dns-dns-222\n"
                "someone-elses-container\n"
            )
        return _ok()  # podman rm -f

    monkeypatch.setattr(cd.subprocess, "run", fake_run)

    removed = cd.cleanup_stale_containers()

    assert removed == ["ctrl-dns-target-111", "ctrl-dns-dns-222"]
    # exactly one list + one rm per fixture, nothing else
    rms = [a for a in invocations if a[1] == "rm"]
    assert rms == [
        ["podman", "rm", "-f", "ctrl-dns-target-111"],
        ["podman", "rm", "-f", "ctrl-dns-dns-222"],
    ]


def test_cleanup_returns_empty_when_podman_absent(monkeypatch):
    """No podman on PATH -> nothing removed, no crash (partial dev env)."""

    def fake_run(args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.cleanup_stale_containers() == []


def test_cleanup_stops_on_rm_timeout(monkeypatch):
    """A mid-sweep ``podman rm`` timeout stops the sweep and returns whatever
    was removed so far (no crash, no hang)."""
    state = {"listed": False}

    def fake_run(args, **kwargs):
        if args[1] == "ps":
            state["listed"] = True
            return _ok("ctrl-dns-target-1\nctrl-dns-dns-2\n")
        # first rm succeeds, second times out
        if "ctrl-dns-dns-2" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=30)
        return _ok()

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    removed = cd.cleanup_stale_containers()
    assert removed == ["ctrl-dns-target-1"]  # second never appended


def test_cleanup_ps_timeout_returns_empty(monkeypatch):
    """A ``podman ps`` timeout means we never learn what to remove."""

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=30)

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.cleanup_stale_containers() == []


def test_cleanup_prefix_guard_avoids_substring_match(monkeypatch):
    """A container whose name merely *contains* ``ctrl-dns-`` (but is not a
    fixture) is not removed — the startswith prefix guard is the safety net
    over podman's substring ``--filter name=``."""
    removed_via_rm: list[str] = []

    def fake_run(args, **kwargs):
        if args[1] == "ps":
            return _ok("my-ctrl-dns-thing\nctrl-dns-target-9\n")
        removed_via_rm.append(args[-1])
        return _ok()

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd.cleanup_stale_containers() == ["ctrl-dns-target-9"]
    assert removed_via_rm == ["ctrl-dns-target-9"]


def test_probe_target_empty_on_ok(monkeypatch):
    """``_probe_target`` returns ``''`` when the probe container prints ``OK``,
    and the probe connects to every IP concurrently (one thread per IP) so a
    single iteration is bounded by the per-IP timeout, not N x it (#2473)."""
    captured: dict[str, str] = {}

    def fake_run(args, **kwargs):
        captured["script"] = args[-1]
        return _ok("OK")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    c = cd.ControlledDns()
    c._p.ips = ["10.88.0.200", "10.88.0.201"]
    assert c._probe_target() == ""
    assert "threading" in captured["script"]
    assert "create_connection" in captured["script"]


def test_probe_target_returns_bad_list(monkeypatch):
    """A probe that finds unreachable IPs returns the ``BAD ...`` line (so the
    readiness loop can retry)."""
    monkeypatch.setattr(
        cd.subprocess,
        "run",
        lambda *a, **k: _ok("BAD 10.88.0.201:TimeoutError"),
    )
    c = cd.ControlledDns()
    c._p.ips = ["10.88.0.200", "10.88.0.201"]
    assert c._probe_target() == "BAD 10.88.0.201:TimeoutError"


def test_probe_target_reports_subprocess_failure(monkeypatch):
    """A ``podman run`` timeout surfaces as ``'probe raised: ...'`` rather than
    crashing the readiness loop."""

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=30)

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    c = cd.ControlledDns()
    c._p.ips = ["10.88.0.200"]
    assert c._probe_target().startswith("probe raised:")


def test_wait_target_ready_retries_until_ok(monkeypatch):
    """Readiness retries: a target unreachable-then-reachable returns rather
    than raising — the deadline must outlast more than one probe (#2473)."""
    seq = ["BAD 10.88.0.200:TimeoutError", "OK"]
    calls = {"n": 0}

    def fake_run(args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return _ok(seq[min(i, len(seq) - 1)])

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    monkeypatch.setattr(cd.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cd.time, "time", lambda: 100.0)  # deadline never hit

    c = cd.ControlledDns()
    c._p.ips = ["10.88.0.200"]
    c._wait_target_ready(timeout=30.0)  # returns, does not raise
    assert calls["n"] >= 2  # retried at least once before success


def test_wait_target_ready_raises_when_never_reachable(monkeypatch):
    """If the target never becomes reachable the loop exhausts its deadline and
    raises (rather than silently succeeding or hanging)."""
    monkeypatch.setattr(
        cd.subprocess,
        "run",
        lambda *a, **k: _ok("BAD 10.88.0.200:TimeoutError"),
    )
    monkeypatch.setattr(cd.time, "sleep", lambda *_a, **_k: None)
    # time.time(): 100 for the deadline set + first while-check, then far in the
    # future so the next while-check sees the deadline exceeded and exits.
    times = [100.0, 100.0, 1e12]
    idx = {"i": 0}

    def fake_time():
        v = times[min(idx["i"], len(times) - 1)]
        idx["i"] += 1
        return v

    monkeypatch.setattr(cd.time, "time", fake_time)

    c = cd.ControlledDns()
    c._p.ips = ["10.88.0.200", "10.88.0.201"]
    with pytest.raises(RuntimeError, match="did not become reachable"):
        c._wait_target_ready(timeout=30.0)
