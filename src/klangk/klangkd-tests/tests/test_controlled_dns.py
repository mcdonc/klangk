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
