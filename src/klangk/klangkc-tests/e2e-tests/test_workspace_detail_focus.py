"""Regression tests for #1956: workspace-detail terminal-list focus/selection.

The original bug is a real-terminal focus race that ``run_test()`` cannot
reproduce (it flushes the message queue synchronously). These tests therefore
lock in the *structural invariants* of the fix in
``WorkspaceDetailScreen``:

* On entry the Terminals list is non-empty (a placeholder row) and the first
  row is highlighted (``index == 0``) while terminals are still loading, so
  the keyboard is never dead during the container auto-start window.
* The list is focused on entry.
* Once terminals load, the first terminal is the selected row and the list
  holds focus.

Drives the real screen with a faked TuiState (no backend/podman). The
``list_terminals`` fake blocks on an ``asyncio.Event`` so the pre-load
(placeholder) phase is observable.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from textual.widgets import Header, ListView

from klangk.cli.client import Workspace
from klangk.cli.tui.app import KlangkApp
from klangk.cli.tui.screens.workspace_detail import WorkspaceDetailScreen
from klangk.cli.tui.state import TuiState

WS_NAME = "focus-ws"
REAL_TERMINALS = [
    {"index": 0, "name": "bash", "id": "@0"},
    {"index": 1, "name": "logs", "id": "@1"},
]


def _harness(app) -> None:
    """Test-only dodges for run_test(): the Header title-watcher race,
    unsupported suspend(), and refresh_workspaces() (would hit network)."""
    Header._on_mount = lambda self, event: None  # type: ignore[assignment]
    app._sync_title = lambda: None  # type: ignore[assignment]
    app.suspend = lambda: contextlib.nullcontext()  # type: ignore[assignment]
    app.refresh_workspaces = lambda: None  # type: ignore[assignment]


def _make_state(gate: asyncio.Event) -> TuiState:
    st = TuiState()
    ws = Workspace(
        id="ws-1",
        name=WS_NAME,
        created_at="2026-01-01T00:00:00Z",
        running=True,  # skip _start_if_stopped; we gate terminals instead
    )
    st.is_authenticated = lambda: False
    st.find_workspace = lambda name: ws  # type: ignore[assignment]

    async def _list_terminals(name):
        await gate.wait()
        return list(REAL_TERMINALS)

    st.list_terminals = _list_terminals  # type: ignore[assignment]
    return st


async def _wait_for_screen(pilot, screen_type, attr="#term_list"):
    for _ in range(80):
        await pilot.pause()
        if isinstance(pilot.app.screen, screen_type):
            try:
                pilot.app.screen.query_one(attr, ListView)
                return
            except Exception:
                pass


@pytest.mark.asyncio
async def test_concurrent_renders_do_not_duplicate_rows(tmp_path):
    """#1956: adding/removing a terminal fires _render_terminals from BOTH the
    action handler and the backend's terminals_changed broadcast. Without
    serialization the two clear/extend/mount cycles interleave on the same
    ListView and duplicate every row (the "two copies of the terminal list"
    bug). The render lock must serialize them."""
    gate = asyncio.Event()
    app = KlangkApp(_make_state(gate))
    _harness(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(WorkspaceDetailScreen(WS_NAME))
        await _wait_for_screen(pilot, WorkspaceDetailScreen)
        screen = app.screen
        screen.terminals = list(REAL_TERMINALS)
        # Fire two renders concurrently (as the action + broadcast would).
        await asyncio.gather(
            screen._render_terminals(), screen._render_terminals()
        )
        for _ in range(10):
            await pilot.pause()
        items = screen.query_one("#term_list", ListView).query("ListItem")
        assert len(items) == len(REAL_TERMINALS), (
            f"rows duplicated by concurrent renders: {len(items)} "
            f"expected {len(REAL_TERMINALS)}"
        )


@pytest.mark.asyncio
async def test_placeholder_highlighted_and_focused_while_loading(tmp_path):
    """Pre-load: list shows a highlighted placeholder and has focus (#1956)."""
    gate = asyncio.Event()
    app = KlangkApp(_make_state(gate))
    _harness(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(WorkspaceDetailScreen(WS_NAME))
        await _wait_for_screen(pilot, WorkspaceDetailScreen)

        lv = app.screen.query_one("#term_list", ListView)
        items = lv.query("ListItem")
        # Exactly the placeholder row; it is not a real terminal (name == "").
        assert len(items) == 1, (
            "expected placeholder row before terminals load"
        )
        assert getattr(items.first(), "name", None) == ""
        # The placeholder is the highlighted/selected row, and the list is focused.
        assert lv.index == 0, (
            "placeholder must be selected by default (index 0)"
        )
        assert getattr(app.focused, "id", None) == "term_list", (
            "terminals list must be focused on entry (focus trap, #1956)"
        )

        # Release the terminal load; first terminal becomes the selected row.
        gate.set()
        for _ in range(80):
            await pilot.pause()
            names = [getattr(it, "name", "") for it in lv.query("ListItem")]
            if any(n for n in names):
                break
        assert lv.index == 0, "first terminal must be selected after load"
        assert getattr(app.focused, "id", None) == "term_list"
        items = lv.query("ListItem")
        assert len(items) == len(REAL_TERMINALS)
        # The first row must actually carry the highlight (not just index==0):
        # setting index before the appended items mount leaves no row
        # highlighted (#1956 "both terminals grey").
        assert items[0].highlighted is True
        assert items[1].highlighted is False


@pytest.mark.asyncio
async def test_focus_reasserted_after_moving_away(tmp_path):
    """_focus_term_list re-focuses the list when called on the active screen
    (the mechanism on_show uses to recover focus after a modal closes)."""
    gate = asyncio.Event()
    app = KlangkApp(_make_state(gate))
    _harness(app)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(WorkspaceDetailScreen(WS_NAME))
        await _wait_for_screen(pilot, WorkspaceDetailScreen)
        await pilot.pause()

        screen = app.screen
        # Move focus off the list (e.g. user Tabbed/clicked away).
        screen.query_one("Footer").focus()
        await pilot.pause()
        # Re-show path re-asserts focus on the list.
        screen._focus_term_list()
        await pilot.pause()
        assert getattr(app.focused, "id", None) == "term_list"
