"""Main screen: workspace list with live status feed."""

from __future__ import annotations

import asyncio
import json
from functools import partial
import datetime
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.dom import NoMatches
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from ...client import AuthError, WorkspaceNotFoundError, decode_token_claims
from ...auth import refresh_token as _refresh_token
from ...transport import ws_connect
from .base import (
    CheatsheetScreen,
    ConfirmScreen,
    DuplicateScreen,
    InputScreen,
    StatusScreen,
    TransferScreen,
    WorkspaceListView,
    confirm_then,
)
from .server import ServerSwitchScreen
from .workspace_form import (
    CreateWorkspaceScreen,
    open_edit_screen,
)

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN = 600  # refresh 10 minutes before expiry
_TOKEN_REFRESH_POLL = 60  # check every 60 seconds

# Auto-reconnect for the workspaces list when the backend is unreachable
# (#2012). Mirrors the Flutter WS client
# (src/frontend/lib/ws/ws_client.dart: ``_scheduleReconnect`` / ``_backoffDelay``):
# bounded exponential backoff with jitter and a hard attempt cap, after which
# we give up and tell the user to switch server / restart.
_MAX_RECONNECT_ATTEMPTS = 25
_MAX_BACKOFF_SECONDS = 5

# Indirection so tests can advance the WS reconnect loop without real delays
# (and without patching the global ``asyncio.sleep``, which Textual's own
# event loop depends on).
_reconnect_sleep = asyncio.sleep


class ServerUnreachable(Exception):
    """The backend could not be reached at the transport layer.

    Distinct from :class:`AuthError` (expired session — the server *is* up)
    and from an actually-empty result (server up, no workspaces). Raised by
    the workspaces-list fetch so the UI can show a "server down" state
    instead of a misleading empty list.
    """


def _is_unreachable(exc: BaseException) -> bool:
    """True for transport-layer failures (server down / unreachable).

    Covers httpx connect/timeout/protocol errors and raw socket errors, but
    *not* ``AuthError`` or ``httpx.HTTPStatusError`` — those mean the server
    responded, so it is reachable.
    """
    return isinstance(exc, (httpx.TransportError, OSError))


def _server_schedule_line(schedule: dict) -> str:
    """Status-line text for the next pending server action (#2661).

    Shows fire time (local) plus a coarse remaining duration, e.g.
    ``server: stop at 23:00 (in 1h 12m)`` / ``server: recycle at 23:00
    (in 1h 12m)``.
    """
    action = str(schedule.get("action") or "action")
    raw = str(schedule.get("fire_at") or "")
    try:
        fire_at = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return f"server: {action} scheduled"
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(
            tzinfo=datetime.datetime.now().astimezone().tzinfo
        )
    fire_at = fire_at.astimezone()
    remaining = fire_at - datetime.datetime.now().astimezone()
    total = max(0, int(remaining.total_seconds()))
    if total >= 3600:
        left = f"{total // 3600}h {(total % 3600) // 60}m"
    elif total >= 60:
        left = f"{total // 60}m"
    else:
        left = f"{total}s"
    return f"server: {action} at {fire_at:%H:%M} (in {left})"


def _reconnect_backoff(attempt: int) -> float:
    """Backoff delay (seconds) for reconnect *attempt* (1-based).

    Ported from ``WsClient._backoffDelay``: an exponential base capped at
    ``_MAX_BACKOFF_SECONDS``, halved with random jitter so retries spread out
    instead of stampeding a just-restarted server.
    """
    base = min(1 << attempt, _MAX_BACKOFF_SECONDS)
    jitter = random.random() * base
    return (base + jitter) / 2.0


async def run_token_refresh_loop(state) -> str:
    """Proactively refresh the access token before it expires.

    Returns ``"expired"`` if the refresh failed (caller should redirect
    to login), or ``"no_token"`` if credentials disappeared.
    Runs indefinitely until the token can't be refreshed.
    """
    while True:
        await asyncio.sleep(_TOKEN_REFRESH_POLL)
        url = state.current_url()
        token = state.token()
        if not url or not token:
            return "no_token"
        exp = decode_token_claims(token).get("exp")
        if exp is None:
            continue
        remaining = exp - time.time()
        if remaining > _TOKEN_REFRESH_MARGIN:
            continue
        logger.debug("Token expires in %.0fs, refreshing", remaining)
        new = await asyncio.to_thread(_refresh_token, url, token)
        if new:
            logger.debug("Token refreshed proactively")
        else:
            # Refresh failed — but a concurrent refresher (e.g. the CLI's
            # background thread) may already have rotated the token and got
            # this one blocklisted. Re-read state: if the token changed,
            # keep running instead of forcing a logout (#1882 review).
            if state.token() != token:
                logger.debug("Token rotated concurrently; not expiring")
                continue
            logger.warning("Proactive token refresh failed")
            return "expired"


class MainScreen(StatusScreen):
    """The TUI home: a two-page workspace list (owned / shared) + status bar,
    with a live WS feed. Selecting a workspace opens its detail screen."""

    DEFAULT_CSS = """
    .ws-row {
        height: 1;
    }
    .ws-name {
        width: 1fr;
    }
    .ws-id {
        width: 10;
        padding-left: 1;
        color: $text-muted;
    }
    .ws-date {
        width: 12;
        padding-left: 2;
        text-align: right;
        color: $text-muted;
    }
    .ws_hints {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    #filter_bar {
        dock: bottom;
        height: 1;
        background: $boost;
    }
    #filter_input {
        width: 1fr;
        border: none;
        height: 1;
        padding: 0;
        background: $boost;
    }
    #sort_btn {
        width: auto;
        min-width: 20;
        border: none;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    BINDINGS = [
        ("c", "switch_server", "Switch server"),
        ("n", "create", "New"),
        ("i", "import", "Import"),
        ("l", "logout", "Logout"),
        ("slash", "focus_filter", "Filter"),
        ("o", "cycle_sort", "Sort"),
        # Per-workspace actions act on the highlighted row of the active
        # list. Hidden from the Footer; their hints render inline above the
        # list (#1878), mirroring the terminal-action pattern on the detail
        # screen (#1860). `s` matches the detail screen's Stop/Start.
        Binding("r", "restart", "Restart", show=False),
        Binding("s", "stop", "Stop/Start", show=False),
        Binding("u", "duplicate", "Dup", show=False),
        Binding("d", "delete", "Del", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("?", "cheatsheet", "Keys", show=False),
    ]

    def compose(self) -> ComposeResult:
        # Header + tabs + status dock (StatusBar over Footer) come from
        # StatusScreen (#2689); the filter bar is yielded LAST so that —
        # since docked-bottom widgets overlap rather than stack — it paints
        # on top of the Footer row when shown. It is hidden by default and
        # toggled by `/` (#1764).
        yield from super().compose()
        with Horizontal(id="filter_bar"):
            yield Input(
                placeholder="Filter by name… (/ to focus, Esc to clear)",
                id="filter_input",
            )
            yield Button("sort: created ▼", id="sort_btn", variant="default")

    def compose_body(self) -> ComposeResult:
        with TabbedContent(id="ws_tabs"):
            with TabPane("Owned by me", id="owned_pane"):
                yield Static("", classes="ws_hints")
                yield WorkspaceListView(id="owned_list")
            with TabPane("Shared to me", id="shared_pane"):
                yield Static("", classes="ws_hints")
                yield WorkspaceListView(id="shared_list")

    # Sort keys matching Flutter defaults: created desc.
    SORT_KEYS = ("created", "name", "running")

    # Status-broadcast types that never write the status-bar live segment
    # (#2690): each already has a dedicated UI surface — `container_status`
    # patches the running dot, `workspaces_changed` refreshes the lists,
    # `terminals_changed` / `service_health` update the detail screen — so
    # mirroring them in the bar too left a stale `live: container_status`
    # after a stop/recycle drain and let routine broadcasts clobber the
    # #2661 schedule countdown. Genuinely unhandled types keep the
    # `live: <type>` debug pulse.
    _STATUS_SILENT_EVENTS = frozenset(
        {
            "container_status",
            "workspaces_changed",
            "terminals_changed",
            "service_health",
        }
    )

    def on_mount(self) -> None:
        self.app.title = "Klangk: Workspaces"
        self._initial_focus_done = False
        self._sort_key = "created"
        self._sort_asc = False
        self._owned_all: list = []
        self._shared_all: list = []
        self._filter_text = ""
        # Reachability state (#2012, #2052). The status WebSocket connection
        # lifecycle is the single reachability signal: ``_server_unreachable``
        # is True while the WS can't stay connected; ``_reconnect_attempt``
        # counts consecutive WS connection losses; ``_gave_up`` is set when the
        # WS reconnect loop exhausts its attempts.
        self._server_unreachable = False
        self._reconnect_attempt = 0
        self._gave_up = False
        # Server-switch teardown for the status WS (#2704): a switch sets
        # this event so a parked status-loop iteration drops the old
        # server's connection and re-dials the new one immediately. Sticky
        # (stays set until the next loop iteration clears it), so a switch
        # that lands between iterations is not lost.
        self._ws_drop = asyncio.Event()
        # The status-WS loop worker, or None when no loop is running (never
        # started on an unauthenticated mount). Kept so
        # ``ensure_status_ws_worker`` can restart the loop after it gave up
        # and a server switch reuses this screen (#2704).
        self._status_worker = None
        # True only while the status WS is actively connected (set in
        # ``_on_ws_connected``, cleared when the connection drops). Lets a
        # transport-failed REST list fetch distinguish a real outage (WS also
        # down) from a transient REST blip while the backend is reachable
        # (#2052).
        self._ws_connected = False
        # Latest ``container_status`` running state per workspace id (#2032).
        # Recorded on every broadcast and re-applied onto each fresh fetch in
        # ``_populate_workspaces``, so a refresh whose snapshot raced behind a
        # start/stop broadcast can't regress the list's running dot. This is
        # the fix for the lost-update: the fetch runs as an ``await`` (network
        # I/O), so a broadcast landing mid-fetch mutates the *old* snapshot
        # object and would be clobbered when the refresh installs a stale one;
        # the overlay carries the freshest state across that boundary.
        # Intentionally unpruned — entries are ``uuid -> bool`` and bounded by
        # the distinct workspaces seen this session; ids are never reused, so a
        # stale entry (a since-deleted workspace) simply never matches a fresh
        # object and is inert. (Pruning against ``_ws_by_id`` would be wrong:
        # it would drop a pending entry for a workspace that hasn't appeared
        # in any fetch yet.)
        self._running_overlay: dict[str, bool] = {}
        # The user's last successful login, rendered for the status bar
        # (#2583). Fetched once on mount by ``_load_last_login`` (a
        # blocking /auth/me hit, so it runs in a worker), stored on the
        # App as ``app.last_login`` so every StatusScreen's bar renders
        # it (#2689), and None until that returns. Re-fetched by
        # ``reload_last_login`` after a server switch — the App reuses
        # this screen there, so on_mount does not re-run and the old
        # server's value would linger next to the new identity.
        self.app.last_login = None
        self.query_one("#filter_bar").display = False
        self._refresh_action_hints()
        # One-time list load. There is no reachability heartbeat and no REST
        # poll thereafter (#2052): the status WS connection lifecycle — started
        # just below — is the single reachability signal, and ``workspaces_changed``
        # broadcasts plus the WS reconnect keep the list fresh.
        self.refresh_lists()
        if self.app.tui_state.is_authenticated():
            self._status_worker = self.app.run_worker(
                self._status_loop, name="status-ws"
            )
            self.app.run_worker(self._token_refresh_loop, name="token-refresh")
            self.app.run_worker(
                self._load_last_login,
                name="last-login",
                # Own group: exclusive cancels the group's other workers,
                # and in the default group that was status-ws +
                # token-refresh — the mount-time last-login fetch killed
                # both background loops at spawn (#2612 regression; the
                # status WS, its events, and the #2661 host countdown
                # never started).
                group="last-login",
                exclusive=True,
                exit_on_error=False,
            )

    def reload_last_login(self) -> None:
        """Drop the shown last login and re-fetch it (#2583).

        Called by ``App.server_changed``: that path reuses this screen,
        so without this the status bar would keep showing the previous
        server's (possibly another user's) login time beside the new
        server/user identity. Exclusive, so a still-running on-mount
        fetch for the old server is cancelled rather than racing this
        one.
        """
        self.app.last_login = None
        self._refresh_status()
        if self.app.tui_state.is_authenticated():
            self.app.run_worker(
                self._load_last_login,
                name="last-login",
                group="last-login",
                exclusive=True,
                exit_on_error=False,
            )

    async def _load_last_login(self) -> None:
        """Fetch the user's last successful login once for the status
        bar (#2583).

        ``TuiState.last_login_at`` does a blocking ``/auth/me`` round
        trip, so it runs on a thread. The formatted stamp is stored on
        the App (``app.last_login``) — every StatusScreen renders it
        (#2689) — and the App-wide refresh updates whichever screen is
        on top.
        """
        iso = await asyncio.to_thread(self.app.tui_state.last_login_at)
        if iso:
            self.app.last_login = self._fmt_login_ts(iso)
            self._refresh_status()

    def on_tabbed_content_tab_activated(self, event) -> None:
        """Focus the first workspace row when switching tabs (#1792)."""
        self._focus_visible_list()
        # The highlighted row (and thus the Stop/Start label) changes with
        # the tab — re-render even if the new pane has no rows to highlight.
        self._refresh_action_hints()

    # --- filter / sort ---

    def action_focus_filter(self) -> None:
        self.query_one("#filter_bar").display = True
        self.query_one("#filter_input", Input).focus()

    def action_cycle_sort(self) -> None:
        """Cycle through sort modes: key↓ → key↑ → next-key↓ → …"""
        keys = self.SORT_KEYS
        idx = keys.index(self._sort_key) if self._sort_key in keys else 0
        if not self._sort_asc:
            # currently descending → flip to ascending (same key)
            self._sort_asc = True
        else:
            # currently ascending → advance to next key, descending
            self._sort_key = keys[(idx + 1) % len(keys)]
            self._sort_asc = False
        self._update_sort_label()
        self._apply_filter()

    def _update_sort_label(self) -> None:
        arrow = "▲" if self._sort_asc else "▼"
        try:
            self.query_one(
                "#sort_btn", Button
            ).label = f"sort: {self._sort_key} {arrow}"
        except NoMatches:  # pragma: no cover
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sort_btn":
            self.action_cycle_sort()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter_input":
            self._filter_text = event.value.strip().lower()
            self._apply_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter_input":
            self._focus_visible_list()

    @staticmethod
    def _sort_key_name(ws) -> str:
        return (getattr(ws, "name", "") or "").lower()

    @staticmethod
    def _sort_key_created(ws) -> str:
        return getattr(ws, "created_at", "") or ""

    @staticmethod
    def _sort_key_running(ws) -> int:
        # 0 for running (sorts first when ascending), 1 for stopped.
        return 0 if getattr(ws, "running", False) else 1

    _SORT_KEY_FUNCS: dict = {
        "name": _sort_key_name,
        "created": _sort_key_created,
        "running": _sort_key_running,
    }

    def _sort_workspaces(self, workspaces: list) -> list:
        """Sort workspace list according to current sort state."""
        key = self._SORT_KEY_FUNCS.get(self._sort_key, self._sort_key_created)
        return sorted(workspaces, key=key, reverse=not self._sort_asc)

    @staticmethod
    def _matches(ws, q: str) -> bool:
        """Whether a workspace matches the filter query — by name or id.

        Matching the full id also covers the 8-char prefix shown on the row
        (the prefix is a substring of the id) (#1911).
        """
        return (
            q in (getattr(ws, "name", "") or "").lower()
            or q in (str(getattr(ws, "id", "") or "")).lower()
        )

    def _apply_filter(self) -> None:
        """Re-populate both lists from cached data with filter + sort."""
        q = self._filter_text
        owned = self._owned_all
        shared = self._shared_all
        if q:
            owned = [ws for ws in owned if self._matches(ws, q)]
            shared = [ws for ws in shared if self._matches(ws, q)]
        owned = self._sort_workspaces(owned)
        shared = self._sort_workspaces(shared)
        empty = "(no matches)" if q else "(no workspaces)"
        self._populate("#owned_list", owned, empty_label=empty)
        self._populate("#shared_list", shared, empty_label=empty)
        self._refresh_action_hints()

    def _focus_visible_list(self) -> None:
        """Focus the first item in the visible workspace list (#1792)."""
        lv = self._active_list()
        if lv is not None and lv.query(ListItem):
            lv.focus()
            if lv.index is None:
                lv.index = 0

    def on_key(self, event) -> None:
        # Escape in the filter input: clear text or hide bar and return.
        if event.key == "escape" and isinstance(self.focused, Input):
            inp = self.query_one("#filter_input", Input)
            if inp.value:
                inp.value = ""
            else:
                self.query_one("#filter_bar").display = False
                self._focus_visible_list()
            event.stop()
            return
        # Down from the tab strip drops into the active workspace list (#1781).
        if event.key == "down" and isinstance(self.focused, Tabs):
            lv = self._active_list()
            if lv is not None:
                event.stop()
                lv.focus()
                if lv.index is None:
                    lv.index = 0

    def action_switch_server(self) -> None:
        self.app.push_screen(ServerSwitchScreen())

    # --- per-workspace actions (act on the highlighted row, #1878) ---

    def _active_list(self):
        """The WorkspaceListView in the currently active tab pane, or None.

        ``TabbedContent`` toggles ``display`` on the ``TabPane`` (the list's
        *parent*), not on the list itself — ``lv.display`` is True for every
        list regardless of tab — so the active list is the one whose parent
        pane is displayed. Keying off ``lv.display`` silently targets the
        Owned list even on the Shared tab (#1879 review).
        """
        for lv in self.query(WorkspaceListView):
            if lv.parent is not None and lv.parent.display:
                return lv
        return None

    def _highlighted_item(self):
        lv = self._active_list()
        return lv.highlighted_child if lv is not None else None

    def _highlighted_ws(self):
        """The workspace object for the highlighted row, or None."""
        item = self._highlighted_item()
        if item is None:
            return None
        wid = str(getattr(item, "workspace_id", "") or "")
        by_id = getattr(self, "_ws_by_id", {}) or {}
        if wid and wid in by_id:
            return by_id[wid]
        name = getattr(item, "name", "") or ""
        for ws in list(self._owned_all) + list(self._shared_all):
            if getattr(ws, "name", "") == name:
                return ws
        return None

    def _require_highlighted(self) -> str | None:
        """Return the highlighted workspace name, or flash a hint + None."""
        item = self._highlighted_item()
        name = getattr(item, "name", "") or "" if item is not None else ""
        if not name:
            self._flash("Select a workspace first.")
            return None
        return name

    def _refresh_action_hints(self) -> None:
        """Re-render the inline per-workspace hint bar.

        The Stop/Start label tracks the highlighted workspace's running
        state so it never offers the wrong action (#1878).
        """
        ws = self._highlighted_ws()
        toggle = "stop" if (ws is not None and ws.running) else "start"
        text = (
            f"[\u21b5 open]  [r restart]  [s {toggle}]"
            "  [u dup]  [d del]  [e edit]"
        )
        for hints in self.query(".ws_hints"):
            hints.update(Text(text))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._refresh_action_hints()

    def _flash(self, message: str) -> None:
        """Show a transient message in the status bar's 'extra' slot.

        The next status event overwrites it. There's no dedicated message
        line on this screen (unlike the detail screen's #detail_msg), so we
        borrow the status 'extra' channel that already shows transient flags.
        """
        self.app.live_extra = message
        self._refresh_status()

    def action_restart(self) -> None:
        name = self._require_highlighted()
        if not name:
            return

        self.app.push_screen(
            ConfirmScreen(
                f"Restart '{name}'? This ends active terminal sessions"
                " and recreates the container.",
                yes_label="Restart",
                yes_variant="warning",
            ),
            confirm_then(self, partial(self._do_restart, name)),
        )

    async def _do_lifecycle(
        self, verb: str, name: str, *, refresh_hints: bool
    ) -> None:
        """Run a workspace lifecycle op off-thread and flash the result.

        *verb* is capitalized ("Restart"/"Stop"/"Start"); the operation
        runs in a worker thread via ``asyncio.to_thread`` so the textual
        event loop stays responsive.
        """
        try:
            method = getattr(self.app.tui_state, f"{verb.lower()}_workspace")
            await asyncio.to_thread(method, name)
        except Exception as exc:
            self._flash(f"{verb} failed: {exc}")
            return
        self._flash(f"{verb} requested for '{name}'.")
        self.app.refresh_workspaces()
        if refresh_hints:
            self._refresh_action_hints()

    async def _do_restart(self, name: str) -> None:
        await self._do_lifecycle("Restart", name, refresh_hints=False)

    def action_stop(self) -> None:
        name = self._require_highlighted()
        if not name:
            return
        ws = self._highlighted_ws()
        if ws is not None and ws.running:
            self.app.push_screen(
                ConfirmScreen(
                    f"Stop '{name}'? This ends active terminal sessions.",
                    yes_label="Stop",
                    yes_variant="warning",
                ),
                confirm_then(self, partial(self._do_stop, name)),
            )
        else:
            self.run_worker(self._do_start(name), exit_on_error=False)

    async def _do_stop(self, name: str) -> None:
        await self._do_lifecycle("Stop", name, refresh_hints=True)

    async def _do_start(self, name: str) -> None:
        await self._do_lifecycle("Start", name, refresh_hints=True)

    def action_duplicate(self) -> None:
        name = self._require_highlighted()
        if not name:
            return

        def _on_dup(new_name: str | None) -> None:
            if not new_name:
                return
            self.run_worker(
                self._do_duplicate(name, new_name), exit_on_error=False
            )

        self.app.push_screen(DuplicateScreen(name), _on_dup)

    async def _do_duplicate(self, src: str, new_name: str) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.duplicate_workspace, src, new_name
            )
        except Exception as exc:
            self._flash(f"Duplicate failed: {exc}")
            return
        self._flash(f"Duplicated '{src}' -> '{new_name}'.")
        self.app.refresh_workspaces()

    def action_delete(self) -> None:
        name = self._require_highlighted()
        if not name:
            return

        self.app.push_screen(
            ConfirmScreen(
                f"Delete '{name}'? This permanently deletes the workspace"
                " and its container.",
            ),
            confirm_then(self, partial(self._do_delete, name)),
        )

    async def _do_delete(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.delete_workspace, name)
        except Exception as exc:
            self._flash(f"Delete failed: {exc}")
            return
        self._flash(f"Deleted '{name}'.")
        self.app.refresh_workspaces()

    def action_edit(self) -> None:
        name = self._require_highlighted()
        if not name:
            return
        self.run_worker(self._do_edit(name), exit_on_error=False)

    async def _do_edit(self, name: str) -> None:
        state = self.app.tui_state
        try:
            ws = await asyncio.to_thread(state.find_workspace, name)
        except WorkspaceNotFoundError:
            self._flash(f"Workspace '{name}' not found.")
            return
        except AuthError:
            self.app.session_expired()
            return
        except Exception as exc:
            self._flash(f"Could not load workspace: {exc}")
            return
        await open_edit_screen(self, state, ws, self._on_edited)

    def _on_edited(self, result) -> None:
        if result:
            self.refresh_lists()

    def action_logout(self) -> None:
        self.app.do_logout()

    def action_cheatsheet(self) -> None:
        """Open the ``?`` keyboard cheatsheet modal (#1802)."""
        self.app.push_screen(CheatsheetScreen(self._cheatsheet_sections()))

    @staticmethod
    def _cheatsheet_sections() -> list[tuple[str, list[tuple[str, str]]]]:
        """Keybindings shown in the cheatsheet, grouped by context (#1802).

        Hand-written (not derived from ``BINDINGS``) so the display labels
        read cleanly — e.g. ``Enter`` / ``↑ ↓`` rather than the raw binding
        key strings. Kept in sync with the bindings above by the TUI tests,
        which assert each key appears.
        """
        return [
            (
                "Navigation",
                [
                    ("↑ ↓", "Move rows; cross the tab strip (Up from row 1)"),
                    ("Tab", "Cycle focus (Shift+Tab back)"),
                    ("Enter", "Open the highlighted workspace"),
                ],
            ),
            (
                "Workspaces",
                [
                    ("c", "Switch server"),
                    ("n", "New workspace"),
                    ("i", "Import from archive"),
                    ("o", "Cycle sort order"),
                    ("/", "Filter by name or id"),
                    ("l", "Log out"),
                ],
            ),
            (
                "Highlighted row",
                [
                    ("e", "Edit workspace"),
                    ("r", "Restart"),
                    ("s", "Stop / Start"),
                    ("u", "Duplicate"),
                    ("d", "Delete"),
                ],
            ),
        ]

    def action_create(self) -> None:
        self.run_worker(self._do_create, exit_on_error=False)

    def action_import(self) -> None:
        """Import a workspace from a .tar.gz archive with upload progress (#1758)."""
        self.app.push_screen(
            InputScreen(
                "Import workspace from (.tar.gz):",
                ok_label="Import",
            ),
            self._on_import_path,
        )

    def _on_import_path(self, path: str | None) -> None:
        if not path:
            return
        archive = Path(path)
        if not archive.exists():
            self._flash(f"Import failed: file not found: {path}")
            return
        state = self.app.tui_state

        def make_call(on_progress):
            return state.import_workspace(archive, on_progress=on_progress)

        self.app.push_screen(
            TransferScreen(
                f"Importing '{archive.name}'…",
                make_call,
                f"Imported '{archive.name}'",
            ),
            self._on_import_done,
        )

    def _on_import_done(self, result: tuple[bool, str]) -> None:
        ok, msg = result
        self._flash(msg)
        if ok:
            self.refresh_lists()

    async def _do_create(self) -> None:
        state = self.app.tui_state
        try:
            data = await asyncio.to_thread(state.list_images)
            default = data.get("default", "") or ""
            allowed = list(data.get("allowed") or [])
            nix_available = data.get("nix_available") is True
            sudo_available = data.get("sudo_available") is True
        except AuthError:
            self.app.session_expired()
            return
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("Could not fetch image list: %s", exc)
            default, allowed = "", []
            nix_available = False
            sudo_available = False
        try:
            allow_autostart = await asyncio.to_thread(state.allow_autostart)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("Could not fetch autostart config: %s", exc)
            allow_autostart = False
        try:
            default_domains = await asyncio.to_thread(
                state.default_allowed_domains
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("Could not fetch default allowed domains: %s", exc)
            default_domains = []
        # #2721: the deploy's home-layout default, pre-reflected by the
        # form's Per-handle home checkbox. None (fetch failed) hides the
        # checkbox and omits the field, so the server applies its own
        # default — never a silently forced layout (#2737 review).
        try:
            default_per_handle_home = await asyncio.to_thread(
                state.default_per_handle_home
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("Could not fetch home-layout default: %s", exc)
            default_per_handle_home = None
        self.app.push_screen(
            CreateWorkspaceScreen(
                allowed=allowed,
                default=default,
                allow_autostart=allow_autostart,
                default_allowed_domains=default_domains,
                nix_available=nix_available,
                default_per_handle_home=default_per_handle_home,
                sudo_available=sudo_available,
            ),
            self._on_created,
        )

    def _on_created(self, name: str | None) -> None:
        """Refresh the list after a create, then offer to open the new
        workspace's detail screen (its terminal list lives there). A live
        in-TUI PTY shell is a future step — #1748's 'open shell' bullet is
        satisfied here as 'open workspace', not a live shell."""
        if not name:
            return
        self.refresh_lists()

        def _offer(open_it: bool) -> None:
            if open_it:
                # Deferred: genuine cycle — workspace_detail imports
                # MainScreen; see the other WorkspaceDetailScreen import
                # in this module for the full note.
                # allow-deferred-import
                from .workspace_detail import WorkspaceDetailScreen

                self.app.push_screen(WorkspaceDetailScreen(name))

        self.app.push_screen(
            ConfirmScreen(
                f"Created '{name}'. Open workspace now?",
                yes_label="Open",
                yes_variant="primary",
                no_label="Later",
            ),
            _offer,
        )

    # --- list population ---

    def refresh_lists(self) -> None:
        """Kick off an async refresh (non-blocking)."""
        self.run_worker(
            self._refresh_lists_async,
            name="refresh-lists",
            exit_on_error=False,
            exclusive=True,
        )

    async def _refresh_lists_async(self) -> None:
        try:
            owned, shared = await self._fetch_lists()
        except AuthError:
            # Session is dead — clear the lists and surface the app-wide
            # session-expired overlay (#2025). No inline label: the overlay
            # is the single, unmissable signal across every page.
            self._owned_all = []
            self._shared_all = []
            for sel in ("#owned_list", "#shared_list"):
                self._populate(sel, [])
            self._refresh_status()
            self.app.session_expired()
            return
        except ServerUnreachable:
            # The WS connection lifecycle is the single reachability signal
            # (#2052). A transport failure on this REST fetch only means the
            # server is down if the WS agrees: if the WS is currently
            # connected the backend is reachable and this is a transient REST
            # blip, so keep the last list (the next broadcast / reconnect
            # refreshes it) rather than falsely flagging the server down.
            if not self._ws_connected:
                self._enter_unreachable()
            return
        # Success — clear any prior unreachable state and render the lists.
        # ``_populate_workspaces`` re-applies ``_running_overlay`` so a stale
        # snapshot can't regress a just-observed start/stop (#2032).
        self._exit_unreachable()
        self._populate_workspaces(owned, shared)

    async def _fetch_lists(self) -> tuple[list, list]:
        """Fetch owned + shared workspace lists.

        Raises :class:`AuthError` on an expired session or
        :class:`ServerUnreachable` when the backend can't be reached at the
        transport layer (server down). Anything else the backend returns is
        a real (possibly empty) result.
        """
        owned = await self._safe_list(owned=True)
        shared = await self._safe_list(owned=False)
        return owned, shared

    def _populate_workspaces(self, owned: list, shared: list) -> None:
        """Cache + render a freshly fetched set of workspace lists."""
        # Re-apply the latest container_status running state so a fetch that
        # raced behind a start/stop broadcast can't regress the list (#2032).
        self._apply_running_overlay(owned + shared)
        self._owned_all = owned
        self._shared_all = shared
        self._ws_by_id = {}
        for ws in owned + shared:
            wid = str(getattr(ws, "id", "") or "")
            if wid:
                self._ws_by_id[wid] = ws
        self._apply_filter()
        self._refresh_status()
        if not self._initial_focus_done:
            self._initial_focus_done = True
            self._focus_visible_list()

    def _apply_running_overlay(self, workspaces: list) -> None:
        """Overlay the latest ``container_status`` running state onto a fresh
        fetch (#2032).

        A ``container_status`` broadcast can land while a list refresh is in
        flight; the fetch may return a snapshot taken before the start/stop,
        so without this overlay the list would briefly flip back to the stale
        state (a running dot disagreeing with the detail screen).
        """
        if not self._running_overlay:
            return
        for ws in workspaces:
            wid = str(getattr(ws, "id", "") or "")
            if wid and wid in self._running_overlay:
                ws.running = self._running_overlay[wid]

    def _render_unreachable(self, label: str) -> None:
        """Show *label* as the only row in both workspace lists."""
        self._owned_all = []
        self._shared_all = []
        for sel in ("#owned_list", "#shared_list"):
            self._populate(sel, [], empty_label=label)
        self._refresh_status()

    def _enter_unreachable(self) -> None:
        """Mark the backend unreachable and surface the overlay (idempotent).

        The status WS connection lifecycle is the single reachability signal
        (#2052): this is called from the WS reconnect loop (on a sustained
        connection loss) and from a transport-failed list fetch. Per-attempt
        status-bar / overlay text is refreshed by the caller via
        :meth:`_refresh_unreachable_display` so the attempt counter advances.
        """
        if self._server_unreachable:
            return
        self._server_unreachable = True
        self._render_unreachable("(server unreachable — retrying…)")
        self._refresh_unreachable_display()

    def _unreachable_status_extra(self) -> str:
        """Status-bar suffix for the current reconnect attempt (#2052)."""
        if self._reconnect_attempt <= 0:
            return "server: unreachable, reconnecting…"
        return (
            f"server: unreachable, retrying "
            f"(attempt {self._reconnect_attempt}/"
            f"{_MAX_RECONNECT_ATTEMPTS})…"
        )

    def _refresh_unreachable_display(self) -> None:
        """Refresh the status-bar extra + overlay message for the current
        reconnect attempt. Called on entry and on every WS reconnect retry so
        the attempt counter in the UI advances (#2052)."""
        self.app.live_extra = self._unreachable_status_extra()
        self._refresh_status()
        self.app.set_server_down(self._down_overlay_message())

    def _exit_unreachable(self) -> None:
        """Backend reachable again — clear the down state.

        Resets the reconnect counter on a confirmed connection. Note a
        flapping backend (one that completes the WS handshake then drops)
        resets on every connect, so it stays in the silent grace retry and
        never reaches ``_MAX_RECONNECT_ATTEMPTS`` — the cap is hit only by a
        backend that can't establish a connection at all (#2052).
        """
        self._reconnect_attempt = 0
        self._gave_up = False
        if self._server_unreachable:
            self._server_unreachable = False
            self.app.live_extra = ""
            self._refresh_status()
            self.app.clear_server_down()

    def _down_overlay_message(self, gave_up: bool = False) -> str:
        """Text for the app-wide server-down overlay (#2012)."""
        if gave_up:
            return (
                "⛔ Server down\n\n"
                "Couldn't reach the backend after repeated attempts."
                " Reconnect paused — switch server or restart to retry.\n"
                "[c] switch server   [Esc] dismiss"
            )
        if self._reconnect_attempt <= 0:
            return (
                "⏳ Server unreachable\n\nReconnecting…\n"
                "The page will reload when the backend returns.\n"
                "[c] switch server   [Esc] dismiss"
            )
        return (
            f"⏳ Server unreachable\n\nReconnecting "
            f"(attempt {self._reconnect_attempt}/"
            f"{_MAX_RECONNECT_ATTEMPTS})…\n"
            "The page will reload when the backend returns.\n"
            "[c] switch server   [Esc] dismiss"
        )

    async def _safe_list(self, *, owned: bool) -> list:
        state = self.app.tui_state
        try:
            return await asyncio.to_thread(
                state.list_owned_workspaces
                if owned
                else state.list_shared_workspaces
            )
        except AuthError:
            raise
        except Exception as exc:
            if _is_unreachable(exc):
                raise ServerUnreachable(
                    str(exc) or "server unreachable"
                ) from exc
            # A non-transport failure (e.g. a decode error) with the server
            # reachable: preserve the historical "treat as empty" behaviour
            # rather than mislabel a healthy account as down.
            logger.debug(
                "workspace list fetch failed (non-transport): %s", exc
            )
            return []

    @staticmethod
    def _fmt_login_ts(iso: str) -> str:
        """Render a UTC ISO login timestamp in the local timezone."""
        try:
            return (
                datetime.datetime.fromisoformat(iso)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
        except (ValueError, TypeError):
            return ""

    @staticmethod
    def _compact_date(raw: str) -> str:
        """Format a created_at timestamp as a compact date string."""
        try:
            dt = datetime.datetime.fromisoformat(raw)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return ""

    @staticmethod
    def _fmt_name(ws) -> Text:
        health = f" ({ws.health})" if ws.health else ""
        dot = ("●", "green") if ws.running else ("●", "red")
        return Text.assemble(dot, f" {ws.name}{health}")

    @staticmethod
    def _fmt_date(ws) -> Text:
        raw = getattr(ws, "created_at", None)
        if raw:
            date = MainScreen._compact_date(raw)
            if date:
                return Text(date, style="dim")
        return Text("")

    @staticmethod
    def _fmt_id(ws) -> Text:
        """Short id (first 8 chars) for list rows — a prefix of the full
        id shown on the detail screen, so a row can be matched to its
        detail without consuming much horizontal space (#1899)."""
        wid = str(getattr(ws, "id", "") or "")
        return Text(wid[:8], style="dim") if wid else Text("")

    def _populate(
        self,
        selector: str,
        workspaces: list,
        *,
        empty_label: str = "(no workspaces)",
    ) -> None:
        lv = self.query_one(selector, ListView)
        lv.clear()
        if not workspaces:
            lv.append(ListItem(Label(Text(empty_label)), name=""))
            return
        for ws in workspaces:
            name_label = Label(self._fmt_name(ws), classes="ws-name")
            id_label = Label(self._fmt_id(ws), classes="ws-id")
            date_label = Label(self._fmt_date(ws), classes="ws-date")
            item = ListItem(
                Horizontal(name_label, id_label, date_label, classes="ws-row"),
                name=ws.name,
            )
            wid = str(getattr(ws, "id", "") or "")
            if wid:
                item.workspace_id = wid  # for live status updates
            lv.append(item)

    def _refresh_status(self) -> None:
        """Refresh the StatusBar on every mounted screen (#2689).

        The status WS handler lives on this screen, which stays mounted
        underneath everything pushed above it — but the live segments it
        writes to ``app.live_extra`` (host notices, the #2661 countdown,
        reachability flags) must render on whatever screen is current
        (detail, forms, login), so delegate to the App-wide refresh.
        """
        self.app.refresh_status()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Deferred: genuine cycle (workspace_detail imports MainScreen;
        # screens/__init__ imports main). Both modules load textual at
        # module scope anyway, so this defers cycle resolution, not weight.
        # allow-deferred-import
        from .workspace_detail import WorkspaceDetailScreen

        name = getattr(event.item, "name", "") or ""
        if name:
            self.app.push_screen(WorkspaceDetailScreen(name))

    def drop_status_connection(self) -> None:
        """Tear down the current status-WS connection promptly (#2704).

        Called by ``App.server_changed`` / ``App.server_changed_needs_login``
        after the active server changed: without this, the status loop stays
        parked inside ``listen_for_status`` against the *old* server until
        that server drops the connection on its own, so reachability and
        live-update signaling (#2052) keep tracking a server the user has
        already left. Setting the event makes the parked iteration cancel
        its listener (closing the WS) and re-dial with the new URL/token.
        """
        self._ws_drop.set()

    def ensure_status_ws_worker(self) -> None:
        """(Re)start the status-WS loop worker (#2704).

        The loop terminates itself once it exhausts
        ``_MAX_RECONNECT_ATTEMPTS`` — the give-up overlay tells the user to
        "switch server … to reconnect" — and a switch reuses this screen
        (no re-mount, so ``on_mount`` doesn't run). Without a restart here
        the switched-to server would get no live updates and no WS
        reachability signal until the TUI is restarted.
        """
        if self._status_worker is None or self._status_worker.is_finished:
            self._status_worker = self.app.run_worker(
                self._status_loop, name="status-ws"
            )

    async def _wait_drop_or(self, awaiting: asyncio.Future) -> bool:
        """Await ``awaiting`` unless a server-switch drop fires first.

        Returns ``True`` when the drop won — ``awaiting`` is cancelled and
        its outcome abandoned (the switch supersedes it) — and ``False``
        when ``awaiting`` completed on its own (its outcome is then
        inspected by the caller). Races both the listener task and the
        reconnect backoff sleep, so a switch interrupts either (#2704).
        """
        waiter = asyncio.create_task(self._ws_drop.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, awaiting}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # The loop itself was cancelled while parked in the race (app
            # shutdown): reap the waiter so it doesn't outlive this frame.
            if not waiter.done():
                waiter.cancel()
            raise
        if waiter in done:
            if not awaiting.done():
                awaiting.cancel()
            elif not awaiting.cancelled():
                # Both finished on the same tick: retrieve (and discard)
                # the outcome so asyncio doesn't warn it was never
                # retrieved. The drop supersedes it either way.
                awaiting.exception()
            return True
        waiter.cancel()
        return False

    def _status_loop_creds(self) -> tuple[str, str] | None:
        """The freshest (url, token) for the next dial, or None when either
        is gone (session expired) or the screen was popped (logout / server
        switch — stop reconnecting)."""
        state = self.app.tui_state
        url = state.current_url()
        token = state.token()
        if not url or not token:
            self.app.session_expired()
            return None
        if self not in self.app.screen_stack:
            return None
        return url, token

    async def _run_status_listener(self, url: str, token: str) -> str:
        """Spawn one status-WS listener and await its end. Returns
        "switched" (a server switch dropped it — re-dial immediately,
        #2704), "expired" (auth failure — session expired), or "closed"
        (connection over: clean close or error)."""
        listener = asyncio.create_task(
            listen_for_status(
                url,
                token,
                on_event=self._on_status_event,
                on_connect=self._on_ws_connected,
            )
        )
        try:
            if await self._wait_drop_or(listener):
                # Server switch: the old connection is torn down
                # (#2704). Re-dial against the new server on the next
                # iteration — this is not an outage, so no attempt
                # accounting, no overlay, no backoff.
                self._ws_connected = False
                return "switched"
            exc = listener.exception()
            if isinstance(exc, AuthError):
                self.app.session_expired()
                return "expired"
            if exc is not None:
                logger.debug(
                    "Status WS error (attempt %d): %s",
                    self._reconnect_attempt + 1,
                    exc,
                )
            return "closed"
        finally:
            # Loop cancelled outright (app shutdown) with the listener
            # still running: don't leave the WS dangling.
            if not listener.done():
                listener.cancel()

    async def _status_loop(self) -> None:
        """Single reachability signal: maintain the status WS and drive the
        unreachable overlay from its lifecycle (#2052).

        On a sustained connection loss the reconnect loop surfaces the
        overlay with bounded backoff and a hard attempt cap; on (re)connect
        it clears the overlay and refreshes the list. There is no REST
        reachability poll — the WS protocol pings (lowered to 10 s / 10 s)
        detect a wedged / half-open connection, and a reconnect-triggered
        list refresh catches a REST-only degradation lazily.

        A server switch interrupts the parked listener (and any backoff
        sleep) via ``drop_status_connection`` so the next iteration re-dials
        the new server immediately (#2704).
        """
        state = self.app.tui_state
        if not state.current_url() or not state.token():
            return
        while True:
            # Re-read BOTH url and token every iteration: a server switch
            # reuses this screen (App.server_changed -> refresh_lists, no
            # re-mount), so a url captured once at mount kept dialing the
            # previous server with the new server's token -- guaranteed
            # auth rejection, 25 reconnect attempts, and a false "server
            # down" overlay while REST on the new server worked fine
            # (#2029 audit).
            creds = self._status_loop_creds()
            if creds is None:
                return
            url, token = creds
            # Consume any drop request still set from a previous iteration
            # *after* the URL/token re-read above: a stale drop's only
            # purpose was interruption, and the freshest config was just
            # captured, so clearing it here keeps the new connection from
            # being torn down the instant it is made.
            self._ws_drop.clear()
            outcome = await self._run_status_listener(url, token)
            if outcome == "switched":
                continue
            if outcome == "expired":
                return
            # The connection is over (clean close or error) — the backend is
            # no longer WS-reachable from this screen's point of view.
            self._ws_connected = False
            # Connection lost (clean close or error). Reconnect with bounded
            # backoff; the top-of-iteration guard catches a screen pop that
            # lands during the backoff sleep.
            delay_outcome = await self._wait_reconnect_delay()
            if delay_outcome == "switched":
                # Switch during the backoff sleep: skip the rest of the
                # delay and re-dial the new server now (#2704).
                continue
            if delay_outcome == "exit":
                return

    async def _wait_reconnect_delay(self) -> str:
        """Account the reconnect attempt, render the retry/gave-up state,
        and sleep the backoff delay. Returns "switched" (a server switch
        dropped the sleep — re-dial the new server now, #2704), "exit"
        (gave up or the screen was popped), or "elapsed" (retry)."""
        # Screen popped (logout / server switch) — stop reconnecting.
        if self not in self.app.screen_stack:
            return "exit"
        self._reconnect_attempt += 1
        if self._reconnect_attempt > _MAX_RECONNECT_ATTEMPTS:
            self._give_up_reconnect()
            return "exit"
        delay = _reconnect_backoff(self._reconnect_attempt)
        if self._reconnect_attempt == 1:
            # Grace: a transient drop / clean close (server restart, idle
            # timeout) gets one silent quick retry before the overlay
            # appears, so a healthy restart doesn't flash "server down".
            self.app.live_extra = "status: reconnecting…"
            self._refresh_status()
        else:
            self._enter_unreachable()
            self._refresh_unreachable_display()
        if await self._wait_drop_or(
            asyncio.create_task(_reconnect_sleep(delay))
        ):
            return "switched"
        return "elapsed"

    def _give_up_reconnect(self) -> None:
        """The reconnect cap was hit: render the gave-up state and tell the
        app the server is down."""
        self._gave_up = True
        self._server_unreachable = False
        self._render_unreachable(
            "(server down — switch server or restart to reconnect)"
        )
        self.app.live_extra = "server: down (gave up reconnecting)"
        self._refresh_status()
        self.app.set_server_down(self._down_overlay_message(gave_up=True))

    def _on_ws_connected(self) -> None:
        """The status WS (re)connected — the backend is reachable again.

        Called once per connection by :func:`listen_for_status` (via its
        ``on_connect`` callback) before any frames are read. Clears the
        unreachable overlay, resets the reconnect counter, and re-fetches
        the list so a reconnect boundary also catches a REST-only
        degradation (#2052).
        """
        self._ws_connected = True
        self._exit_unreachable()
        self.refresh_lists()

    async def _token_refresh_loop(self) -> None:
        """Proactively refresh the access token before it expires."""
        result = await run_token_refresh_loop(self.app.tui_state)
        if result in ("expired", "no_token"):
            self.app.session_expired()

    def _apply_server_lifecycle_event(self, etype: str, event: dict) -> bool:
        """#2527/#2661: host lifecycle notices become a human-readable
        status line; notification only — the reconnect loop (silent first
        retry, backoff, unreachable overlay) is untouched, so a
        restart/shutdown never visually impedes reconnection. Returns True
        when the event was consumed."""
        if etype == "host_shutdown":
            self.app.live_extra = "server: shutting down"
            self._refresh_status()
            self.app.notify("Server is shutting down", severity="warning")
            return True
        if etype == "server_recycle":
            phase = str(event.get("phase") or "")
            word = {
                "draining": "preparing to recycle",
                "recycling": "recycling",
            }.get(phase, "recycling")
            self.app.live_extra = f"server: {word}"
            self._refresh_status()
            self.app.notify(f"Server is {word}")
            return True
        if etype == "host_started":
            self.app.live_extra = "server: back up"
            self._refresh_status()
            return True
        if etype == "server_schedule":
            self._apply_server_schedule_event(event)
            return True
        if etype == "server_schedule_fired":
            action = str(event.get("action") or "action")
            self.app.live_extra = f"server: scheduled {action} running"
            self._refresh_status()
            self.app.notify(
                f"Scheduled server {action} is happening now",
                severity="warning",
            )
            return True
        return False

    def _apply_server_schedule_event(self, event: dict) -> None:
        """#2661: pending server stop/recycle — show the next one as
        a status line with fire time + remaining. The countdown text
        is refreshed by the scheduler's periodic snapshot (every
        ~30s); precise-enough ticking without a local timer."""
        schedules = event.get("schedules") or []
        if not schedules:
            if self.app.live_extra and self.app.live_extra.startswith(
                "server:"
            ):
                self.app.live_extra = ""
                self._refresh_status()
            return
        next_up = schedules[0]
        self.app.live_extra = _server_schedule_line(next_up)
        self._refresh_status()

    def _on_status_event(self, event: dict) -> None:
        etype = event.get("type", "event")
        if self._apply_server_lifecycle_event(etype, event):
            return
        # #2690: silent types keep the current segment (a pending #2661
        # countdown survives routine container_status traffic); only
        # genuinely unhandled types take the debug pulse.
        if etype not in self._STATUS_SILENT_EVENTS:
            self.app.live_extra = f"live: {etype}"
            self._refresh_status()
        if etype == "workspaces_changed":
            self.refresh_lists()
        elif etype == "container_status":
            self._update_running(
                str(event.get("workspace_id") or ""),
                bool(event.get("running")),
            )
        self._forward_status_to_detail(event)

    def _update_running(self, workspace_id: str, running: bool) -> None:
        """Update a single workspace's ● icon in-place (#1791).

        ``container_status`` events fire on start/stop; we patch the list
        item's label without re-fetching the whole list (which would lose
        selection and scroll position). The freshest running state is also
        recorded in ``_running_overlay`` so a list refresh whose snapshot
        raced behind this broadcast can't regress it (#2032).
        """
        # Record the freshest running state regardless of whether the
        # workspace is currently in the snapshot — it may land in the next
        # refresh, where _populate_workspaces will re-apply it.
        if workspace_id:
            self._running_overlay[workspace_id] = running
        ws = getattr(self, "_ws_by_id", {}).get(workspace_id)
        if ws is None:
            return
        ws.running = running
        for sel in ("#owned_list", "#shared_list"):
            lv = self.query_one(sel, ListView)
            for item in lv.query(ListItem):
                if getattr(item, "workspace_id", None) == workspace_id:
                    try:
                        item.query_one(".ws-name").update(self._fmt_name(ws))
                    except NoMatches:
                        pass
                    break
        # The highlighted row's running state may have changed — refresh
        # the Stop/Start hint label so it never offers the wrong action.
        self._refresh_action_hints()

    def _forward_status_to_detail(self, event: dict) -> None:
        """Mirror a live status broadcast onto an open detail screen."""
        # Deferred: genuine cycle (workspace_detail imports MainScreen;
        # screens/__init__ imports main). Both modules load textual at
        # module scope anyway, so this defers cycle resolution, not weight.
        # allow-deferred-import
        from .workspace_detail import WorkspaceDetailScreen

        for screen in reversed(self.app.screen_stack):
            if isinstance(screen, WorkspaceDetailScreen):
                screen.apply_status_event(event)
                break


# ---------------------------------------------------------------------------
# WebSocket status listener (#2052; moved verbatim from the former tui/ws
# submodule — sole consumer was this screen).
# ---------------------------------------------------------------------------

# Protocol-level liveness for the TUI's status WS (#2052). The client pings
# the server every ``_WS_PING_INTERVAL`` seconds and expects a pong within
# ``_WS_PING_TIMEOUT``; a wedged / half-open connection is dropped within
# ~interval+timeout, which ``_status_loop`` turns into the unreachable
# overlay. Tighter than the ``websockets`` default (20/20) so a silent drop
# surfaces in ~20s with no REST polling.
_WS_PING_INTERVAL = 10
_WS_PING_TIMEOUT = 10


async def listen_for_status(
    server_url: str,
    token: str,
    on_event: Callable[[dict], object],
    *,
    on_connect: Callable[[], object] | None = None,
    max_size: int | None = None,
) -> None:
    """Connect to ``/ws`` and call ``on_event(event)`` for each broadcast.

    ``on_connect`` (if given) is invoked once after the connection is
    established, before any frames are read — the TUI uses it to clear the
    unreachable overlay and refresh the workspace list on (re)connect (#2052).

    Non-JSON and non-object frames are skipped (the server occasionally
    sends control/ack frames). Callback exceptions are isolated: a bug in
    ``on_event``/``on_connect`` is logged and swallowed rather than tearing
    down the connection — an exception escaping this listener reads as a
    connection loss to ``_status_loop``, which would churn reconnects and
    replay the failure forever (#2029 audit, same isolation rule as the
    consent decider's pump).
    """
    async with ws_connect(
        server_url,
        token=token,
        max_size=max_size,
        ping_interval=_WS_PING_INTERVAL,
        ping_timeout=_WS_PING_TIMEOUT,
    ) as ws:
        if on_connect is not None:
            try:
                on_connect()
            except Exception:  # noqa: BLE001
                logger.exception("status WS on_connect callback failed")
        async for raw in ws:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            try:
                on_event(event)
            except Exception:  # noqa: BLE001
                logger.exception("status WS event callback failed")
