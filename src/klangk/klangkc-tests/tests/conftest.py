"""CLI unit test configuration."""

import os

import pytest

# Use sysmon coverage engine for pytest-xdist compatibility — mirrors the
# server suite's conftest (src/klangk/klangkd-tests/tests/conftest.py) so
# coverage is tracked correctly across xdist workers (#1526).
os.environ.setdefault("COVERAGE_CORE", "sysmon")


@pytest.fixture(autouse=True)
def _isolate_cli_state(tmp_path, monkeypatch):
    """Redirect CLI config/state to a per-test tmp dir for every klangkc-test.

    ``klangk.cli.config`` resolves ``_CONFIG_PATH`` / ``_STATE_PATH`` at
    *import* time from ``XDG_CONFIG_HOME`` / ``XDG_STATE_HOME`` (with
    ~/.config / ~/.local/state fallbacks), so setting the env vars later
    has no effect — the module globals are patched directly, and the
    ``_cfg()`` / ``_state()`` caches in ``klangk.cli.main`` are reset so
    the next call re-loads from the isolated path.

    Without this, a unit test that invokes the real CLI in-process (e.g.
    ``CliRunner().invoke(app, ["logout"])``) reads the developer's live
    ``klangk-state.yaml`` and POSTs a real logout to their running server,
    revoking the live TUI's token (#1900).
    """
    import klangk.cli.config as _cfg
    import klangk.cli.main as _main

    monkeypatch.setattr(_cfg, "_CONFIG_PATH", tmp_path / "klangk.yaml")
    monkeypatch.setattr(_cfg, "_STATE_PATH", tmp_path / "klangk-state.yaml")
    monkeypatch.setattr(_main, "_cfg_cache", None)
    monkeypatch.setattr(_main, "_state_cache", None)
