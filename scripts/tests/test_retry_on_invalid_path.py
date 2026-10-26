"""Behavior tests for the devenv-eval retry wrapper (#2774 / #3594).

``retry-on-invalid-path.sh`` wraps every devenv-evaluating CI step
(devenv-setup's Prepare/Build, e2e-suite's, fuzz-daily's Build images).
devenv 2.2.x's embedded Nix fails eval as a per-eval coin flip — a
referenced store path is reported missing — in two wordings:

    error: path '/nix/store/<hash>-<name>' is not valid
    error: getting attributes of path "/nix/store/<hash>-<name>": No such file or directory

A single retry converts either to noise. Only these signatures retry;
everything else passes through with its original exit code.

These tests execute the real wrapper against a fake command that fails
with controlled messages and exit codes, counting attempts through a
state file — the ``test_podman_reexec_retry`` pattern. The
ANSI-colored variant matters: nix colorizes when it believes stderr is
a terminal, and run 40320711358's log carried raw SGR sequences between
the tokens.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = (
    _REPO_ROOT / ".github" / "actions" / "devenv-setup" / "scripts"
) / "retry-on-invalid-path.sh"

_ESC = "\033[31;1m"
_RESET = "\033[0m"

# Fake-command skeleton: counts attempts in $FAKE_COUNT, then runs the
# per-test probe. {probe} is the behavior under test.
_FAKE_TEMPLATE = """\
#!/usr/bin/env bash
n=$(($(cat "$FAKE_COUNT" 2>/dev/null || echo 0) + 1))
echo "$n" >"$FAKE_COUNT"
{probe}
"""

_FAIL_ONCE_TEMPLATE = """\
if [ "$n" -eq 1 ]; then
  printf '%s\\n' "{msg}" >&2
  exit 1
fi
echo "ok"
exit 0"""

_SIG_NOT_VALID = "error: path '/nix/store/abc123-thing' is not valid"
_SIG_GETTING_ATTRS = (
    "error: getting attributes of path "
    '"/nix/store/3r3xyh20f0hlhcbijn497l7yzigxf27w-builder.pl": '
    "No such file or directory"
)
_SIG_GETTING_ATTRS_ANSI = (
    f"{_ESC}error:{_RESET} getting attributes of path "
    f'{_ESC}"/nix/store/3r3x-builder.pl"{_RESET}: No such file or directory'
)
_UNRELATED = "error: something else entirely"


def _fake(tmpdir: str, body: str) -> str:
    """Materialize the fake command inside tmpdir; returns its path."""
    fake = Path(tmpdir) / "fake-cmd"
    fake.write_text(f"#!/usr/bin/env bash\n{body}")
    fake.chmod(0o755)
    return str(fake)


def _run_wrapper(fake: str) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run the real wrapper against the fake; returns (result, attempts)."""
    with tempfile.TemporaryDirectory() as run_dir:
        count = Path(run_dir) / "calls"
        result = subprocess.run(
            ["bash", str(_WRAPPER), fake],
            capture_output=True,
            text=True,
            env={**os.environ, "FAKE_COUNT": str(count)},
        )
        attempts = int(count.read_text()) if count.exists() else 0
        return result, attempts


def _probe(msg: str) -> str:
    return _FAIL_ONCE_TEMPLATE.format(msg=msg.replace('"', '\\"'))


def test_not_valid_wording_retries_once_and_succeeds():
    with tempfile.TemporaryDirectory() as tmp:
        result, attempts = _run_wrapper(
            _fake(tmp, _FAKE_TEMPLATE.format(probe=_probe(_SIG_NOT_VALID)))
        )
    assert result.returncode == 0, result.stderr
    assert attempts == 2
    assert "retrying once" in result.stdout


def test_getting_attributes_wording_retries_once_and_succeeds():
    with tempfile.TemporaryDirectory() as tmp:
        result, attempts = _run_wrapper(
            _fake(tmp, _FAKE_TEMPLATE.format(probe=_probe(_SIG_GETTING_ATTRS)))
        )
    assert result.returncode == 0, result.stderr
    assert attempts == 2
    assert "retrying once" in result.stdout


def test_unquoted_path_still_matches():
    """The quote classes are optional — an unquoted store path (nix
    wording varies by version) is the same signature, not a new one."""
    msg = "error: getting attributes of path /nix/store/abc-builder.pl: No such file or directory"
    with tempfile.TemporaryDirectory() as tmp:
        result, attempts = _run_wrapper(
            _fake(tmp, _FAKE_TEMPLATE.format(probe=_probe(msg)))
        )
    assert result.returncode == 0, result.stderr
    assert attempts == 2
    assert "retrying once" in result.stdout


def test_ansi_colored_signature_still_matches():
    with tempfile.TemporaryDirectory() as tmp:
        result, attempts = _run_wrapper(
            _fake(tmp, _FAKE_TEMPLATE.format(probe=_probe(_SIG_GETTING_ATTRS_ANSI)))
        )
    assert result.returncode == 0, result.stderr
    assert attempts == 2


def test_unrelated_failure_passes_through_untouched():
    unrelated = _FAKE_TEMPLATE.format(
        probe=f"printf '%s\\n' \"{_UNRELATED}\" >&2\nexit 42"
    )
    with tempfile.TemporaryDirectory() as tmp:
        result, attempts = _run_wrapper(_fake(tmp, unrelated))
    assert result.returncode == 42
    assert attempts == 1
    assert "retrying once" not in result.stdout


def test_signature_on_both_attempts_fails_after_one_retry():
    always = _FAKE_TEMPLATE.format(
        probe=f"printf '%s\\n' \"{_SIG_NOT_VALID}\" >&2\nexit 1"
    )
    with tempfile.TemporaryDirectory() as tmp:
        result, attempts = _run_wrapper(_fake(tmp, always))
    assert result.returncode == 1
    assert attempts == 2
