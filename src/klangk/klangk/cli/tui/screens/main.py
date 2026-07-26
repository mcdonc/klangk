"""Main screen: workspace list with live status feed."""

from __future__ import annotations

import asyncio
import datetime
import logging

import httpx

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.dom import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    TabbedContent,
    TabPane,
    Tabs,
)

from ...client import AuthError
from ..widgets import StatusBar
from ..ws import listen_for_status
from ._base import ConfirmScreen, WorkspaceListView

logger = logging.getLogger(__name__)


class MainScreen(Screen):
    """The TUI home: a two-page workspace list (owned / shared) + status bar,
    with a live WS feed. Selecting a workspace opens its detail screen."""

    DEFAULT_CSS = """
    .ws-row {
        height: 1;
    }
    .ws-name {
        width: 1fr;
    }
    .ws-date {
        width: auto;
        text-align: right;
        color: $text-muted;
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
        ("s", "switch_server", "Switch server"),
        ("n", "create", "New"),
        ("a", "account", "Account"),
        ("l", "logout", "Logout"),
        ("slash", "focus_filter", "Filter"),
        ("o", "cycle_sort", "Sort"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="ws_tabs"):
            yield TabPane("Owned by me", WorkspaceListView(id="owned_list"))
            yield TabPane("Shared to me", WorkspaceListView(id="shared_list"))
        yield StatusBar(id="status")
        yield Footer()
        # Filter bar is yielded LAST so that — since docked-bottom widgets
        # overlap rather than stack — it paints on top of the Footer row
        # when shown. It is hidden by default and toggled by `/` (#1764).
        with Horizontal(id="filter_bar"):
            yield Input(
                placeholder="Filter by name… (/ to focus, Esc to clear)",
                id="filter_input",
            )
            yield Button("sort: created ▼", id="sort_btn", variant="default")

    # Sort keys matching Flutter defaults: created desc.
    SORT_KEYS = ("created", "name")

    def on_mount(self) -> None:
        self.app.title = "Klangk: Workspaces"
        self._initial_focus_done = False
        self._sort_key = "created"
        self._sort_asc = False
        self._owned_all: list = []
        self._shared_all: list = []
        self._filter_text = ""
        self.query_one("#filter_bar").display = False
        self.refresh_lists()
        if self.app.tui_state.is_authenticated():
            self.app.run_worker(self._status_loop, name="status-ws")

    def on_tabbed_content_tab_activated(self, event) -> None:
        """Focus the first workspace row when switching tabs (#1792)."""
        self._focus_visible_list()

    # --- filter / sort ---

    def action_focus_filter(self) -> None:
        self.query_one("#filter_bar").display = True
        self.query_one("#filter_input", Input).focus()

    def action_cycle_sort(self) -> None:
        """Cycle through sort modes: created↓ → created↑ → name↑ → name↓."""
        if self._sort_key == "created" and not self._sort_asc:
            self._sort_asc = True
        elif self._sort_key == "created" and self._sort_asc:
            self._sort_key = "name"
            self._sort_asc = True
        elif self._sort_key == "name" and self._sort_asc:
            self._sort_asc = False
        else:
            self._sort_key = "created"
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

    def _sort_workspaces(self, workspaces: list) -> list:
        """Sort workspace list according to current sort state."""
        key = (
            self._sort_key_name
            if self._sort_key == "name"
            else self._sort_key_created
        )
        return sorted(workspaces, key=key, reverse=not self._sort_asc)

    def _apply_filter(self) -> None:
        """Re-populate both lists from cached data with filter + sort."""
        q = self._filter_text
        owned = self._owned_all
        shared = self._shared_all
        if q:
            owned = [
                ws
                for ws in owned
                if q in (getattr(ws, "name", "") or "").lower()
            ]
            shared = [
                ws
                for ws in shared
                if q in (getattr(ws, "name", "") or "").lower()
            ]
        owned = self._sort_workspaces(owned)
        shared = self._sort_workspaces(shared)
        empty = "(no matches)" if q else "(no workspaces)"
        self._populate("#owned_list", owned, empty_label=empty)
        self._populate("#shared_list", shared, empty_label=empty)

    def _focus_visible_list(self) -> None:
        """Focus the first item in the visible workspace list (#1792)."""
        for lv in self.query(WorkspaceListView):
            if lv.display and lv.query(ListItem):
                lv.focus()
                if lv.index is None:
                    lv.index = 0
                return

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
            for lv in self.query(WorkspaceListView):
                if lv.display:
                    event.stop()
                    lv.focus()
                    if lv.index is None:
                        lv.index = 0
                    break

    def action_switch_server(self) -> None:
        from .server import ServerSwitchScreen  # noqa: allow-deferred-import

        self.app.push_screen(ServerSwitchScreen())

    def action_account(self) -> None:
        from .account import AccountScreen  # noqa: allow-deferred-import

        self.app.push_screen(AccountScreen())

    def action_logout(self) -> None:
        self.app.do_logout()

    def action_create(self) -> None:
        self.run_worker(self._do_create, exit_on_error=False)

    async def _do_create(self) -> None:
        from .workspace_form import CreateWorkspaceScreen  # noqa: allow-deferred-import

        state = self.app.tui_state
        try:
            data = await asyncio.to_thread(state.list_images)
            default = data.get("default", "") or ""
            allowed = list(data.get("allowed") or [])
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("Could not fetch image list: %s", exc)
            default, allowed = "", []
        try:
            allow_autostart = await asyncio.to_thread(state.allow_autostart)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.debug("Could not fetch autostart config: %s", exc)
            allow_autostart = False
        self.app.push_screen(
            CreateWorkspaceScreen(
                allowed=allowed,
                default=default,
                allow_autostart=allow_autostart,
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
                from .workspace_detail import WorkspaceDetailScreen  # noqa: allow-deferred-import

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
            exit_on_error=False,
            exclusive=True,
        )

    async def _refresh_lists_async(self) -> None:
        self._ws_by_id: dict[str, object] = {}
        try:
            owned = await self._safe_list(owned=True)
            shared = await self._safe_list(owned=False)
        except AuthError:
            self._owned_all = []
            self._shared_all = []
            for sel in ("#owned_list", "#shared_list"):
                self._populate(
                    sel, [], empty_label="(session expired — re-login)"
                )
            self._refresh_status()
            return
        self._owned_all = owned
        self._shared_all = shared
        for ws in owned + shared:
            wid = str(getattr(ws, "id", "") or "")
            if wid:
                self._ws_by_id[wid] = ws
        self._apply_filter()
        self._refresh_status()
        if not self._initial_focus_done:
            self._initial_focus_done = True
            self._focus_visible_list()

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
        except Exception:
            return []

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
            date_label = Label(self._fmt_date(ws), classes="ws-date")
            item = ListItem(
                Horizontal(name_label, date_label, classes="ws-row"),
                name=ws.name,
            )
            wid = str(getattr(ws, "id", "") or "")
            if wid:
                item.workspace_id = wid  # for live status updates
            lv.append(item)

    def _refresh_status(self) -> None:
        state = self.app.tui_state
        try:
            self.query_one("#status", StatusBar).set_state(
                server=state.current_url(),
                user=state.email() or "(unknown)",
                extra=self.app.live_extra,
            )
        except NoMatches:
            pass  # Widget not mounted yet; status will refresh on mount.

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        from .workspace_detail import WorkspaceDetailScreen  # noqa: allow-deferred-import

        name = getattr(event.item, "name", "") or ""
        if name:
            self.app.push_screen(WorkspaceDetailScreen(name))

    async def _status_loop(self) -> None:
        state = self.app.tui_state
        url = state.current_url()
        token = state.token()
        if not url or not token:
            return
        try:
            await listen_for_status(url, token, on_event=self._on_status_event)
        except Exception:
            # Best-effort: the TUI stays usable if the status stream dies.
            self.app.live_extra = (
                "status: disconnected (switch server to reconnect)"
            )
            self._refresh_status()

    def _on_status_event(self, event: dict) -> None:
        etype = event.get("type", "event")
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

        ``container_status`` events fire on start/stop; we patch the
        list item's label without re-fetching the whole list (which
        would lose selection and scroll position).
        """
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
                    return

    def _forward_status_to_detail(self, event: dict) -> None:
        """Mirror a live status broadcast onto an open detail screen."""
        from .workspace_detail import WorkspaceDetailScreen  # noqa: allow-deferred-import

        for screen in reversed(self.app.screen_stack):
            if isinstance(screen, WorkspaceDetailScreen):
                screen.apply_status_event(event)
                break
