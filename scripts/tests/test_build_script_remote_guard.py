"""Tests for the build scripts' remote-feature env-var guard.

The build scripts (``scripts/flutterbuildweb.sh``,
``scripts/build-workspace-image.sh``) wrap ``update_features.py`` and default
to ``--local-only`` — skipping git-sourced features — unless
``KLANGKBUILD_BUILD_INCLUDE_REMOTE=1`` is set. This keeps CI off the network and
resilient to upstream failures: the policy dates to #1691, when a remote
feature's transitive git dep had a missing LFS object that broke every CI
build. Today every feature in ``features.yaml`` is a local path entry
(soliplex was vendored in #1686), so the skip is a no-op — but the gate
stays as the generic remote-feature policy for any future ``git:`` entry, so
that adding one doesn't silently make CI start fetching over the network.

These are contract tests — they grep the scripts for the guard so a future
edit that removes it (without intending to) is loud. The actual skip
behavior is covered by ``test_update_features.py::TestLocalOnlyFlag``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sure the scripts directory is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_BUILD_SCRIPTS = [
    _SCRIPTS_DIR / "flutterbuildweb.sh",
    _SCRIPTS_DIR / "build-workspace-image.sh",
]


def test_build_scripts_check_env_var():
    """Every build script that calls update_features.py gates git-sourced
    features behind KLANGKBUILD_BUILD_INCLUDE_REMOTE=1.

    Without this guard, a default CI build clones every git-sourced feature
    declared in features.yaml, and any upstream failure (a missing LFS
    object, a pushed-but-broken tag, …) takes the whole build down.
    """
    for script in _BUILD_SCRIPTS:
        text = script.read_text()
        assert "update_features.py" in text, (
            f"{script.name} no longer calls update_features.py — "
            f"guard test is stale, investigate"
        )
        assert "KLANGKBUILD_BUILD_INCLUDE_REMOTE" in text, (
            f"{script.name} calls update_features.py without the "
            f"KLANGKBUILD_BUILD_INCLUDE_REMOTE guard — a default CI build will "
            f"clone git-sourced features and can be broken by any upstream "
            f"failure (the original failure mode was #1691)"
        )
        assert "--local-only" in text, (
            f"{script.name} references the env var but doesn't pass "
            f"--local-only to update_features.py"
        )


def test_build_scripts_default_to_local_only():
    """The default (env var unset) must skip git-sourced features.

    The guard's polarity matters: the *default* must be the safe one (skip
    remote), with the opt-in (include remote) being the explicit override.
    A future edit that flips the polarity (e.g. defaulting to fetching
    git-sourced features, with an env var to skip) would re-expose CI to
    upstream failures (the original failure mode was #1691).
    """
    for script in _BUILD_SCRIPTS:
        text = script.read_text()
        # The conditional adds --local-only UNLESS the env var is "1".
        # Match the pattern: if [ "${KLANGKBUILD_BUILD_INCLUDE_REMOTE:-0}" != "1" ]
        # (the :-0 default makes unset → "0" → != "1" → true → add --local-only).
        assert "KLANGKBUILD_BUILD_INCLUDE_REMOTE:-0" in text, (
            f"{script.name} doesn't default KLANGKBUILD_BUILD_INCLUDE_REMOTE to '0' "
            f"— the polarity may be flipped, making remote-fetch the default "
            f"(re-exposes CI to upstream failures)"
        )
        assert '!= "1"' in text, (
            f"{script.name} doesn't compare against '1' — polarity may be "
            f"flipped (re-exposes CI to upstream failures)"
        )
