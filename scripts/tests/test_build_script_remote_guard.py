"""Tests for the build scripts' remote-plugin env-var guard (#1691).

The build scripts (``scripts/flutterbuildweb.sh``,
``scripts/build-workspace-image.sh``) wrap ``update_plugins.py`` and default
to ``--local-only`` — skipping git-sourced plugins — unless
``KLANGK_BUILD_INCLUDE_REMOTE=1`` is set. This is the workaround for the
upstream ag-ui LFS-object gap that broke every CI build: the only remote
plugin today is soliplex (whose transitive ``ag_ui`` git dep has a missing
LFS object on the remote), and soliplex is dormant by default anyway, so
CI builds don't need it compiled in.

These are contract tests — they grep the scripts for the guard so a future
edit that removes it (without intending to) is loud. The actual skip
behavior is covered by ``test_update_plugins.py::TestLocalOnlyFlag``.
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
    """Every build script that calls update_plugins.py gates remote plugins
    behind KLANGK_BUILD_INCLUDE_REMOTE=1 (#1691).

    Without this guard, a default CI build clones every git-sourced plugin
    (today: soliplex), and any upstream-LFS gap takes the whole build down.
    """
    for script in _BUILD_SCRIPTS:
        text = script.read_text()
        assert "update_plugins.py" in text, (
            f"{script.name} no longer calls update_plugins.py — "
            f"guard test is stale, investigate"
        )
        assert "KLANGK_BUILD_INCLUDE_REMOTE" in text, (
            f"{script.name} calls update_plugins.py without the "
            f"KLANGK_BUILD_INCLUDE_REMOTE guard — a default CI build will "
            f"clone remote plugins (incl. soliplex) and re-break #1691"
        )
        assert "--local-only" in text, (
            f"{script.name} references the env var but doesn't pass "
            f"--local-only to update_plugins.py"
        )


def test_build_scripts_default_to_local_only():
    """The default (env var unset) must skip remote plugins.

    The guard's polarity matters: the *default* must be the safe one (skip
    remote), with the opt-in (include remote) being the explicit override.
    A future edit that flips the polarity (e.g. defaulting to fetching
    remote plugins, with an env var to skip) would re-break #1691.
    """
    for script in _BUILD_SCRIPTS:
        text = script.read_text()
        # The conditional adds --local-only UNLESS the env var is "1".
        # Match the pattern: if [ "${KLANGK_BUILD_INCLUDE_REMOTE:-0}" != "1" ]
        # (the :-0 default makes unset → "0" → != "1" → true → add --local-only).
        assert "KLANGK_BUILD_INCLUDE_REMOTE:-0" in text, (
            f"{script.name} doesn't default KLANGK_BUILD_INCLUDE_REMOTE to '0' "
            f"— the polarity may be flipped, making remote-fetch the default "
            f"(re-breaks #1691)"
        )
        assert '!= "1"' in text, (
            f"{script.name} doesn't compare against '1' — polarity may be "
            f"flipped (re-breaks #1691)"
        )
