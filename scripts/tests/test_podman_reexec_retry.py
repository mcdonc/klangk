"""Tests for the rootless reexec retry guard (#3168).

``devenv tasks run klangk:build-workspace-image klangk:build-network-sidecar``
runs both tasks in parallel; on a fresh machine those are the first two
rootless podman invocations, and the concurrent first-time user-namespace /
storage initialization intermittently fails one of them with

    failed to reexec: Permission denied

reding the job before any test runs (CI run 33888969754). The fix in
``scripts/_podman_common.sh`` — ``klangk::run_podman`` — retries exactly that
signature once (after printing diagnostics) and passes every other failure
through untouched.

Contract tests pin the wiring (build scripts route their podman calls through
the wrapper; the helper keeps the signature, the single retry, and the
diagnostics probes); behavior tests execute the helper against a fake
``$KLANGKD_PODMAN_BIN`` that fails with controlled signatures and exit codes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PODMAN_COMMON = _REPO_ROOT / "scripts" / "_podman_common.sh"
_BUILD_SCRIPTS = [
    _REPO_ROOT / "scripts" / "build-workspace-image.sh",
    _REPO_ROOT / "scripts" / "build-network-sidecar.sh",
]

# Shared fake-podman skeleton: the diagnostics probes (``info``, ``unshare``)
# succeed silently and never touch the call counter, so CALLS counts only the
# wrapped command's attempts. {probe} is the behavior under test.
_FAKE_TEMPLATE = """\
#!/usr/bin/env bash
case "${{1:-}}" in
  info | unshare) exit 0 ;;
esac
n=$(($(cat "$FAKE_COUNT" 2>/dev/null || echo 0) + 1))
echo "$n" >"$FAKE_COUNT"
{probe}
"""

_FAIL_ONCE = _FAKE_TEMPLATE.format(
    probe="""if [ "$n" -eq 1 ]; then
  echo "failed to reexec: Permission denied" >&2
  exit 1
fi
echo "fake-podman-stdout"
exit 0"""
)

_ALWAYS_REEXEC = _FAKE_TEMPLATE.format(
    probe="""echo "failed to reexec: Permission denied" >&2
exit 1"""
)

_UNRELATED = _FAKE_TEMPLATE.format(
    probe="""echo "some unrelated podman error" >&2
exit 42"""
)

# Diagnostics-probe failure: the info probe exits 3, unshare exits 5 — the
# FAILED branches must report those rcs without failing the wrapper itself
# (the wrapped command still fails once with the signature, then succeeds).
_PROBES_FAIL = _FAIL_ONCE.replace(
    "  info | unshare) exit 0 ;;",
    "  info) exit 3 ;;\n  unshare) exit 5 ;;",
)


def _run_with_fake(fake_body: str) -> subprocess.CompletedProcess[str]:
    """Source the helper and run klangk::run_podman against a fake podman.

    The retry sleep is zeroed so the retry path is exercised without
    slowing the suite. Returns the bash process result; the inner script
    prints ``RC=<exit>`` and ``CALLS=<probe invocations>`` on stdout.
    """
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "podman-fake"
        fake.write_text(fake_body)
        fake.chmod(0o755)
        count = Path(td) / "count"
        script = "\n".join(
            [
                "set -euo pipefail",
                f'source "{_PODMAN_COMMON}"',
                "rc=0",
                "klangk::run_podman probe || rc=$?",
                'echo "RC=$rc"',
                f'echo "CALLS=$(cat {count} 2>/dev/null || echo 0)"',
            ]
        )
        env = {
            **os.environ,
            "KLANGKD_PODMAN_BIN": str(fake),
            "KLANGKBUILD_PODMAN_RETRY_SLEEP": "0",
            "FAKE_COUNT": str(count),
        }
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


# --- Contract tests -------------------------------------------------------


def test_build_scripts_route_podman_through_wrapper():
    """Both parallel-built images must go through klangk::run_podman.

    A bare ``"$PODMAN" build`` in either script re-opens the #3168 race:
    the two build tasks run in parallel and are the machine's first
    rootless podman invocations.
    """
    for script in _BUILD_SCRIPTS:
        text = script.read_text()
        assert "klangk::run_podman build" in text, (
            f"{script.name} no longer builds via klangk::run_podman — the "
            f"concurrent-first-init reexec race can red CI again (#3168)"
        )
        assert '"$PODMAN" build' not in text, (
            f"{script.name} has a bare podman build bypassing the #3168 retry"
        )


def test_helper_signature_single_retry():
    """The retry must stay: signature-matched, exactly one retry."""
    text = _PODMAN_COMMON.read_text()
    assert 'REEXEC_SIGNATURE="failed to reexec:"' in text, (
        "the #3168 reexec signature grep is gone from _podman_common.sh"
    )
    assert "for attempt in 1 2" in text, (
        "klangk::run_podman must make at most two attempts (#3168) — an "
        "unbounded retry loop would hang CI on a persistent failure"
    )
    assert "${KLANGKBUILD_PODMAN_RETRY_SLEEP:-5}" in text, (
        "the retry backoff must stay overridable (tests zero it)"
    )


def test_helper_diagnostics_probes():
    """The probes named in #3168 must stay in the diagnostics."""
    text = _PODMAN_COMMON.read_text()
    for probe in (
        "info >/dev/null",
        "unshare true >/dev/null",
        "max_user_namespaces",
        "subuid",
    ):
        assert probe in text, (
            f"diagnostics probe {probe!r} missing from _podman_common.sh — "
            f"a recurrence would no longer be attributable (#3168)"
        )


# --- Behavior tests -------------------------------------------------------


def test_reexec_failure_retried_once():
    """A reexec-signature failure is retried once and then succeeds."""
    proc = _run_with_fake(_FAIL_ONCE)
    assert "RC=0" in proc.stdout, proc.stderr
    assert "CALLS=2" in proc.stdout, proc.stderr
    assert "::warning::" in proc.stderr
    assert "failed to reexec: Permission denied" in proc.stderr


def test_diagnostics_printed_before_retry():
    """Diagnostics precede the retry so a hard failure stays attributable."""
    proc = _run_with_fake(_FAIL_ONCE)
    err = proc.stderr
    assert "podman info: ok" in err
    assert "podman unshare true: ok (userns creatable)" in err
    assert err.index("podman reexec diagnostics") < err.index("::warning::")


def test_stdout_stays_clean():
    """The wrapper keeps stdout clean (podman output streams on stderr)."""
    proc = _run_with_fake(_FAIL_ONCE)
    assert proc.stdout.splitlines() == ["RC=0", "CALLS=2"]


def test_unrelated_failure_not_retried():
    """Any other failure passes through: same rc, no retry, no diagnostics."""
    proc = _run_with_fake(_UNRELATED)
    assert "RC=42" in proc.stdout, proc.stderr
    assert "CALLS=1" in proc.stdout, proc.stderr
    assert "::warning::" not in proc.stderr
    assert "podman reexec diagnostics" not in proc.stderr


def test_persistent_reexec_fails_loudly_after_retry():
    """A reexec failure that persists exits nonzero after exactly one retry."""
    proc = _run_with_fake(_ALWAYS_REEXEC)
    assert "RC=1" in proc.stdout, proc.stderr
    assert "CALLS=2" in proc.stdout, proc.stderr
    assert "::error::" in proc.stderr


def test_failing_diagnostics_probes_do_not_break_wrapper():
    """FAILED probes report their rc; the retry still runs and succeeds."""
    proc = _run_with_fake(_PROBES_FAIL)
    assert "podman info: FAILED rc=3" in proc.stderr
    assert "podman unshare true: FAILED rc=5" in proc.stderr
    assert "RC=0" in proc.stdout, proc.stderr
    assert "CALLS=2" in proc.stdout, proc.stderr
