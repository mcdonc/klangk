"""Tests for the build scripts' remote-feature env-var guard.

The feature-staging path — ``update_features.py`` plus its remote-feature
gate — lives in two places:

- inline in ``scripts/flutterbuildweb.sh``;
- in the shared helper ``klangk::stage_features`` in
  ``scripts/_podman_common.sh`` (used by ``build-workspace-image.sh``;
  ``build-fips-image.sh`` layers onto a base image and stages nothing,
  #2626).

Both default to ``--local-only`` — skipping git-sourced features — unless
``KLANGKBUILD_BUILD_INCLUDE_REMOTE=1`` is set. This keeps CI off the
network and resilient to upstream failures: the policy dates to #1691,
when a remote feature's transitive git dep had a missing LFS object that
broke every CI build. Today every feature in ``features.yaml`` is a local
path entry (soliplex was vendored in #1686), so the skip is a no-op — but
the gate stays as the generic remote-feature policy for any future
``git:`` entry, so that adding one doesn't silently make CI start
fetching over the network.

These are contract tests — they grep the scripts for the guard so a
future edit that removes it (without intending to) is loud. When the
helper moved from build-workspace-image.sh to _podman_common.sh (#2626,
#2629) the guard moved with it; the assertions now target the helper's
body directly, plus one consumer assertion per staging script. The
actual skip behavior is covered by
``test_update_features.py::TestLocalOnlyFlag``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sure the scripts directory is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
# Scripts that stage the feature payload inline (their guard must be in
# their own text).
_INLINE_GUARD_SCRIPTS = [_SCRIPTS_DIR / "flutterbuildweb.sh"]
# Scripts that stage the feature payload via the shared helper — the
# guard lives in the helper; each consumer must actually call it.
_HELPER = _SCRIPTS_DIR / "_podman_common.sh"
_HELPER_CONSUMER_SCRIPTS = [_SCRIPTS_DIR / "build-workspace-image.sh"]


def _assert_calls_update_features(where: Path, text: str) -> None:
    """The script calls update_features.py behind the env guard, with
    --local-only."""
    assert "update_features.py" in text, (
        f"{where.name} no longer calls update_features.py — "
        f"guard test is stale, investigate"
    )
    assert "KLANGKBUILD_BUILD_INCLUDE_REMOTE" in text, (
        f"{where.name} calls update_features.py without the "
        f"KLANGKBUILD_BUILD_INCLUDE_REMOTE guard — a default CI build will "
        f"clone git-sourced features and can be broken by any upstream "
        f"failure (the original failure mode was #1691)"
    )
    assert "--local-only" in text, (
        f"{where.name} references the env var but doesn't pass "
        f"--local-only to update_features.py"
    )


def _assert_guard_polarity(where: Path, text: str) -> None:
    """The polarity guard: the default (env var unset) must skip remote."""
    assert "KLANGKBUILD_BUILD_INCLUDE_REMOTE:-0" in text, (
        f"{where.name} doesn't default KLANGKBUILD_BUILD_INCLUDE_REMOTE to '0' "
        f"— the polarity may be flipped, making remote-fetch the default "
        f"(re-exposes CI to upstream failures)"
    )
    assert '!= "1"' in text, (
        f"{where.name} doesn't compare against '1' — polarity may be "
        f"flipped (re-exposes CI to upstream failures)"
    )


def _assert_guard_pattern(where: Path, text: str) -> None:
    """The four-line remote-feature guard contract, wherever it lives."""
    _assert_calls_update_features(where, text)
    _assert_guard_polarity(where, text)


def test_build_scripts_check_env_var():
    """Every feature-staging path gates git-sourced features behind
    KLANGKBUILD_BUILD_INCLUDE_REMOTE=1.

    Without this guard, a default CI build clones every git-sourced feature
    declared in features.yaml, and any upstream failure (a missing LFS
    object, a pushed-but-broken tag, …) takes the whole build down.
    """
    for script in _INLINE_GUARD_SCRIPTS:
        _assert_guard_pattern(script, script.read_text())
    # The shared helper carries the guard for its consumers.
    _assert_guard_pattern(_HELPER, _HELPER.read_text())


def test_build_scripts_default_to_local_only():
    """The default (env var unset) must skip git-sourced features.

    The guard's polarity matters: the *default* must be the safe one (skip
    remote), with the opt-in (include remote) being the explicit override.
    A future edit that flips the polarity (e.g. defaulting to fetching
    git-sourced features, with an env var to skip) would re-expose CI to
    upstream failures (the original failure mode was #1691).
    """
    # Polarity is asserted inside _assert_guard_pattern (the :-0 default
    # and the != "1" comparison); the split of scripts vs helper is
    # covered by the other tests, so this contract is the same check
    # through the shared assertion helper.
    test_build_scripts_check_env_var()


def test_helper_consumers_call_stage_features():
    """A script that stages features must go through the guarded helper.

    Bypassing ``klangk::stage_features`` (hand-rolling an
    ``update_features.py`` call) would silently drop the remote-feature
    gate — this pins every staging consumer to the helper.
    """
    for script in _HELPER_CONSUMER_SCRIPTS:
        text = script.read_text()
        assert "klangk::stage_features" in text, (
            f"{script.name} stages the feature payload without calling "
            f"klangk::stage_features — hand-rolled staging drops the "
            f"KLANGKBUILD_BUILD_INCLUDE_REMOTE guard (#2629)"
        )


def test_fips_build_does_not_stage_features():
    """The FIPS variant stages nothing (Dockerfile.fips has no features
    COPY — the base image carries them), so it must NOT call the staging
    helper; a stray call would waste a payload build per FIPS image build
    (#2626 review nit)."""
    script = _SCRIPTS_DIR / "build-fips-image.sh"
    if not script.exists():  # pragma: no cover — ships with #2626
        return
    text = script.read_text()
    assert "klangk::stage_features" not in text, (
        "build-fips-image.sh should not stage features (Dockerfile.fips "
        "has no features COPY); remove the stray staging call"
    )
    assert "update_features.py" not in text, (
        "build-fips-image.sh must not hand-roll update_features.py "
        "(no features are staged for the FIPS variant)"
    )
