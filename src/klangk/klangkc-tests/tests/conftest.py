"""CLI unit test configuration."""

import pytest

# No COVERAGE_CORE pin: branch coverage (#2834) requires the C tracer
# core (Python 3.13's sys.monitoring has no branch events, so sysmon
# can't measure branches and falls back with a warning that
# filterwarnings=error turns fatal). See the repo-root pyproject's
# [tool.coverage.run] for the full rationale.


@pytest.fixture(autouse=True)
def _isolate_cli_state(tmp_path, monkeypatch):
    """Redirect CLI config/state to a per-test tmp dir for every klangkc-test.

    ``klangk.cli.config`` resolves ``_CONFIG_PATH`` / ``_STATE_PATH`` at
    *import* time from ``XDG_CONFIG_HOME`` / ``XDG_STATE_HOME`` (with
    ~/.config / ~/.local/state fallbacks), so setting the env vars later
    has no effect — the module globals are patched directly, and the
    ``_cfg()`` / ``_state()`` caches in ``klangk.cli.context`` are reset so
    the next call re-loads from the isolated path.

    Without this, a unit test that invokes the real CLI in-process (e.g.
    ``CliRunner().invoke(app, ["logout"])``) reads the developer's live
    ``klangk-state.yaml`` and POSTs a real logout to their running server,
    revoking the live TUI's token (#1900).
    """
    import klangk.cli.config as _cfg
    import klangk.cli.context as _ctx

    monkeypatch.setattr(_cfg, "_CONFIG_PATH", tmp_path / "klangk.yaml")
    monkeypatch.setattr(_cfg, "_STATE_PATH", tmp_path / "klangk-state.yaml")
    monkeypatch.setattr(_ctx, "_cfg_cache", None)
    monkeypatch.setattr(_ctx, "_state_cache", None)


@pytest.fixture(autouse=True)
def _stub_tui_background_loops(monkeypatch):
    """Patch the long-running TUI background workers so tests never wait.

    ``listen_for_status`` and ``run_token_refresh_loop`` are async
    coroutines that run indefinitely in the real app. Tests that need
    custom behaviour can override these via their own ``monkeypatch``
    calls (later patches win).
    """
    from klangk.cli.tui.screens import main as _scr_main

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(_scr_main, "listen_for_status", _noop)
    monkeypatch.setattr(_scr_main, "run_token_refresh_loop", _noop)
