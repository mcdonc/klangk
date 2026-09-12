"""Tests for the devenv eval retry wrapper (#2775, extended in #3442).

``devenv`` 2.2.x evaluates through an embedded Nix with a use-after-free in
libexpr-c (upstream cachix/devenv#3064, analyzed in #2774). The failure is a
per-eval coin flip with two observed signatures:

    error: path '/nix/store/<hash>-<name>' is not valid
    × Failed to get shell attribute:

The second surfaced in #3442 — the shell-attribute evaluation died on a
task assertion ("The 'exports' option for a task can only be set when
'package' is a bash package") whose pinned inputs were identical to green
sibling runs minutes earlier on the same commit, so the corrupted result,
not the config, was at fault.

The fix in ``.github/actions/devenv-setup/scripts/retry-devenv-eval.sh`` —
retrying exactly once per signature and passing every other failure through
untouched — is pinned here by contract tests (both flavors of reference
point at the script; the script keeps its two signatures and single retry)
and behavior tests (a fake command fails once with each signature, then
succeeds; unrelated failures and persistent failures pass through).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RETRY_SCRIPT = (
    _REPO_ROOT
    / ".github"
    / "actions"
    / "devenv-setup"
    / "scripts"
    / "retry-devenv-eval.sh"
)
_RETIRING_CONSUMERS = [
    _REPO_ROOT / ".github" / "actions" / "devenv-setup" / "action.yml",
    _REPO_ROOT / ".github" / "actions" / "e2e-suite" / "action.yml",
    _REPO_ROOT / ".github" / "workflows" / "super-e2e.yml",
]

# Fake wrapped command skeleton: counts invocations in $FAKE_COUNT so CALLS
# reports how many times the wrapper attempted the command. {probe} is the
# behavior under test.
_FAKE_TEMPLATE = """\
#!/usr/bin/env bash
n=$(($(cat "$FAKE_COUNT" 2>/dev/null || echo 0) + 1))
echo "$n" >"$FAKE_COUNT"
{probe}
"""

_FAIL_ONCE_INVALID_PATH = _FAKE_TEMPLATE.format(
    probe="""if [ "$n" -eq 1 ]; then
  echo "error: path '/nix/store/abc123-source' is not valid" >&2
  exit 1
fi
echo "fake-devenv-stdout"
exit 0"""
)

_FAIL_ONCE_SHELL_ATTRIBUTE = _FAKE_TEMPLATE.format(
    probe="""if [ "$n" -eq 1 ]; then
  printf '%s\\n' \
    '✖ Evaluating shell in 459ms (failed)' \
    '× Failed to get shell attribute:' \
    '… while evaluating the attribute '"'"'assertion'"'"'' \
    '   message = "The '"'"'exports'"'"' option for a task can only be set when '"'"'package'"'"' is a bash package."' >&2
  exit 1
fi
echo "fake-devenv-stdout"
exit 0"""
)

_ALWAYS_INVALID_PATH = _FAKE_TEMPLATE.format(
    probe="""echo "error: path '/nix/store/abc123-source' is not valid" >&2
exit 1"""
)

_ALWAYS_SHELL_ATTRIBUTE_ANSI = _FAKE_TEMPLATE.format(
    probe="""printf '%s\\n' \
    $'\\x1b[31m ×\\x1b[0m Failed to get shell attribute:' >&2
exit 1"""
)

_UNRELATED = _FAKE_TEMPLATE.format(
    probe="""echo "some unrelated devenv error" >&2
exit 42"""
)


def _run_with_fake(fake_body: str) -> subprocess.CompletedProcess[str]:
    """Run the wrapper around a fake devenv whose failure is scripted.

    Returns the wrapper process result; the fake prints ``CALLS=<n>`` on
    stdout only when it reaches its success path, so callers assert both
    on the wrapper's exit code and on the number of attempts.
    """
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "devenv-fake"
        fake.write_text(fake_body)
        fake.chmod(0o755)
        count = Path(td) / "count"
        script = "\n".join(
            [
                "rc=0",
                f'"{_RETRY_SCRIPT}" "{fake}" shell -- true || rc=$?',
                'echo "RC=$rc"',
                f'echo "CALLS=$(cat {count} 2>/dev/null || echo 0)"',
            ]
        )
        env = {**os.environ, "FAKE_COUNT": str(count)}
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )


def _stdout_value(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


# --- Contract tests -------------------------------------------------------


def test_consumers_reference_the_wrapper():
    """Every devenv-evaluating step must invoke the retry wrapper.

    A bare ``devenv shell``/``devenv tasks run`` in a consumer re-opens the
    eval coin flip: #3442 killed a scheduled run before any test executed.
    """
    for consumer in _RETIRING_CONSUMERS:
        text = consumer.read_text()
        assert "retry-devenv-eval.sh" in text, (
            f"{consumer} no longer routes devenv through the retry wrapper — "
            f"the eval UAF coin flip can red CI again (#2774/#3442)"
        )


def test_wrapper_signatures_and_single_retry():
    """The wrapper must keep both signatures and the single retry."""
    text = _RETRY_SCRIPT.read_text()
    assert "is not valid" in text, (
        "the #2774 'path ... is not valid' signature is gone from the wrapper"
    )
    assert "Failed to get shell attribute" in text, (
        "the #3442 'Failed to get shell attribute' signature is gone from the wrapper"
    )
    assert '"$attempt" -eq 1' in text, (
        "the retry must stay signature-matched and fire exactly once"
    )
    assert "attempt=3" not in text and '"$attempt" -eq 2' not in text, (
        "the wrapper retries more than once — update these tests if that is "
        "a deliberate policy change"
    )


# --- Behavior tests -------------------------------------------------------


def test_invalid_path_failure_retried_once_and_succeeds():
    result = _run_with_fake(_FAIL_ONCE_INVALID_PATH)
    assert result.returncode == 0, result.stderr
    assert _stdout_value(result.stdout, "RC") == "0"
    assert _stdout_value(result.stdout, "CALLS") == "2"
    assert "::warning::" in result.stdout


def test_shell_attribute_failure_retried_once_and_succeeds():
    result = _run_with_fake(_FAIL_ONCE_SHELL_ATTRIBUTE)
    assert result.returncode == 0, result.stderr
    assert _stdout_value(result.stdout, "RC") == "0"
    assert _stdout_value(result.stdout, "CALLS") == "2"
    assert "::warning::" in result.stdout


def test_persistent_invalid_path_failure_passes_through():
    result = _run_with_fake(_ALWAYS_INVALID_PATH)
    assert _stdout_value(result.stdout, "RC") == "1"
    assert _stdout_value(result.stdout, "CALLS") == "2"


def test_ansi_wrapped_signature_still_matches():
    """nix colorizes when it thinks stderr is a terminal; the match must not
    depend on that (#3442's log lines carry SGR sequences)."""
    result = _run_with_fake(_ALWAYS_SHELL_ATTRIBUTE_ANSI)
    assert _stdout_value(result.stdout, "CALLS") == "2"


def test_unrelated_failure_passes_through_untouched():
    result = _run_with_fake(_UNRELATED)
    assert _stdout_value(result.stdout, "RC") == "42"
    assert _stdout_value(result.stdout, "CALLS") == "1"
    assert "::warning::" not in result.stdout


def test_success_path_runs_once():
    result = _run_with_fake(_FAKE_TEMPLATE.format(probe="exit 0"))
    assert _stdout_value(result.stdout, "RC") == "0"
    assert _stdout_value(result.stdout, "CALLS") == "1"
