"""Unit tests for the e2e helpers in ``_e2e_env.py``.

These cover the hermeticity invariants of ``clean_env()`` that aren't easily
asserted from inside an e2e test (which would need to spawn a subprocess to
observe the leak). The most important one — pinning ``XDG_CONFIG_HOME`` /
``XDG_STATE_HOME`` to under a ``HOME`` override — guards against the
GitHub-Actions-ubuntu gotcha where the runner image exports
``XDG_CONFIG_HOME=/home/runner/.config`` in ``/etc/environment`` (a literal
path, not re-expanded per process). Without the pin, any code that correctly
reads the XDG var (the CLI post-#1646, the server post-#1644) writes outside
the tmpdir the test set up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the e2e-tests dir importable (it's not on sys.path by default for the
# unit test suite).
_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e-tests"
sys.path.insert(0, str(_E2E_DIR))

from _e2e_env import clean_env  # type: ignore[import-not-found]  # noqa: E402


def test_clean_env_strips_klangk_prefixed_vars(monkeypatch):
    """KLANGK_* / _KLANGK_* / KLANGKC_* / LOGFIRE_* are stripped from the baseline."""
    monkeypatch.setenv("KLANGK_SECRET", "leak")
    monkeypatch.setenv("_KLANGK_INTERNAL", "leak")
    monkeypatch.setenv("KLANGKC_DEBUG", "leak")
    monkeypatch.setenv("LOGFIRE_TOKEN", "leak")
    env = clean_env()
    assert "KLANGK_SECRET" not in env
    assert "_KLANGK_INTERNAL" not in env
    assert "KLANGKC_DEBUG" not in env
    assert "LOGFIRE_TOKEN" not in env


def test_clean_env_pins_xdg_to_home_override(monkeypatch):
    """A ``HOME=`` override pins XDG_CONFIG_HOME / XDG_STATE_HOME under it.

    Regression guard for the GitHub Actions ubuntu runner: the image sets
    ``XDG_CONFIG_HOME=/home/runner/.config`` in ``/etc/environment`` (literal,
    not re-expanded per process). Without this pin, the inherited value leaks
    past the ``HOME`` override and the CLI/server writes config outside the
    tmpdir the test set up (#1646 e2e failure on CI).
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/runner/.config")
    monkeypatch.setenv("XDG_STATE_HOME", "/home/runner/.local/state")
    monkeypatch.setenv("HOME", "/home/runner")
    env = clean_env(HOME="/tmp/pytest-test-home")
    assert env["HOME"] == "/tmp/pytest-test-home"
    assert env["XDG_CONFIG_HOME"] == "/tmp/pytest-test-home/.config"
    assert env["XDG_STATE_HOME"] == "/tmp/pytest-test-home/.local/state"


def test_clean_env_respects_explicit_xdg_override(monkeypatch):
    """An explicit XDG_* override wins over the HOME-derived default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/runner/.config")
    env = clean_env(
        HOME="/tmp/test-home",
        XDG_CONFIG_HOME="/custom/xdg-config",
    )
    assert env["XDG_CONFIG_HOME"] == "/custom/xdg-config"
    # XDG_STATE_HOME was not explicitly overridden, so it derives from HOME.
    assert env["XDG_STATE_HOME"] == "/tmp/test-home/.local/state"


def test_clean_env_no_home_override_does_not_pin_xdg(monkeypatch):
    """Without a HOME override, the inherited XDG vars pass through unchanged.

    Server e2e tests don't override HOME (they pin KLANGK_STATE_DIR /
    KLANGK_DATA_DIR explicitly), so they keep the inherited XDG vars — and
    that's fine because the KLANGK_* override wins in settings.py regardless.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", "/inherited/config")
    monkeypatch.setenv("XDG_STATE_HOME", "/inherited/state")
    env = clean_env(KLANGK_PORT="12345")
    assert env["XDG_CONFIG_HOME"] == "/inherited/config"
    assert env["XDG_STATE_HOME"] == "/inherited/state"
    assert "XDG_CONFIG_HOME" in env  # not pinned, not stripped


def test_clean_env_baseline_defaults_present():
    """The E2E baseline defaults are always set."""
    env = clean_env()
    assert env["_KLANGK_DISABLE_PROXY"] == "1"
    assert env["KLANGK_AUTH_MODES"] == "password"


def test_clean_env_auth_modes_override():
    """KLANGK_AUTH_MODES override wins over the baseline default."""
    env = clean_env(KLANGK_AUTH_MODES="none")
    assert env["KLANGK_AUTH_MODES"] == "none"


def test_clean_env_strips_env_at_call_time(monkeypatch):
    """The baseline env is snapshotted at call time, not import time.

    Setting a KLANGK_* var after import but before the call still gets
    stripped; setting a non-stripped var after import still gets included.
    """
    monkeypatch.setenv("KLANGK_LATE", "late-leak")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    env = clean_env()
    assert "KLANGK_LATE" not in env
    assert env.get("SOME_OTHER_VAR") == "kept"


# Silence the "imported but unused" lint for the os import (kept for parity
# with the module's other tests if future tests need it).
_ = os
