"""Main screen: workspace list with live status feed."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path

import httpx

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
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
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from ...client import AuthError, WorkspaceNotFoundError, decode_token_claims
from ...auth import refresh_token as _refresh_token
from ..widgets import StatusBar
from ..ws import listen_for_status
from ._base import (
    ConfirmScreen,
    InputScreen,
    TransferScreen,
    WorkspaceListView,
)

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN = 600  # refresh 10 minutes before expiry
_TOKEN_REFRESH_POLL = 60  # check every 60 seconds


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
        ("a", "account", "Account"),
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
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="ws_tabs"):
            with TabPane("Owned by me", id="owned_pane"):
                yield Static("", classes="ws_hints")
                yield WorkspaceListView(id="owned_list")
            with TabPane("Shared to me", id="shared_pane"):
                yield Static("", classes="ws_hints")
                yield WorkspaceListView(id="shared_list")
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
        self._refresh_action_hints()
        self.refresh_lists()
        if self.app.tui_state.is_authenticated():
            self.app.run_worker(self._status_loop, name="status-ws")
            self.app.run_worker(self._token_refresh_loop, name="token-refresh")

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
        from .server import ServerSwitchScreen  # noqa: allow-deferred-import

        self.app.push_screen(ServerSwitchScreen())

    def action_account(self) -> None:
        from .account import AccountScreen  # noqa: allow-deferred-import

        self.app.push_screen(AccountScreen())

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

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_restart(name), exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Restart '{name}'? This ends active terminal sessions"
                " and recreates the container.",
                yes_label="Restart",
                yes_variant="warning",
            ),
            _on_confirm,
        )

    async def _do_restart(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.restart_workspace, name)
        except Exception as exc:
            self._flash(f"Restart failed: {exc}")
            return
        self._flash(f"Restart requested for '{name}'.")
        self.app.refresh_workspaces()

    def action_stop(self) -> None:
        name = self._require_highlighted()
        if not name:
            return
        ws = self._highlighted_ws()
        if ws is not None and ws.running:

            def _on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                self.run_worker(self._do_stop(name), exit_on_error=False)

            self.app.push_screen(
                ConfirmScreen(
                    f"Stop '{name}'? This ends active terminal sessions.",
                    yes_label="Stop",
                    yes_variant="warning",
                ),
                _on_confirm,
            )
        else:
            self.run_worker(self._do_start(name), exit_on_error=False)

    async def _do_stop(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.stop_workspace, name)
        except Exception as exc:
            self._flash(f"Stop failed: {exc}")
            return
        self._flash(f"Stop requested for '{name}'.")
        self.app.refresh_workspaces()
        self._refresh_action_hints()

    async def _do_start(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.start_workspace, name)
        except Exception as exc:
            self._flash(f"Start failed: {exc}")
            return
        self._flash(f"Start requested for '{name}'.")
        self.app.refresh_workspaces()
        self._refresh_action_hints()

    def action_duplicate(self) -> None:
        from ._base import DuplicateScreen  # noqa: allow-deferred-import

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

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_delete(name), exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Delete '{name}'? This permanently deletes the workspace"
                " and its container.",
            ),
            _on_confirm,
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
        from .workspace_form import EditWorkspaceScreen  # noqa: allow-deferred-import

        state = self.app.tui_state
        try:
            ws = await asyncio.to_thread(state.find_workspace, name)
        except WorkspaceNotFoundError:
            self._flash(f"Workspace '{name}' not found.")
            return
        except Exception as exc:
            self._flash(f"Could not load workspace: {exc}")
            return
        try:
            data = await asyncio.to_thread(state.list_images)
            default = data.get("default", "") or ""
            allowed = list(data.get("allowed") or [])
        except Exception:
            default, allowed = "", []
        try:
            allow_autostart = await asyncio.to_thread(state.allow_autostart)
        except Exception:
            allow_autostart = False
        self.app.push_screen(
            EditWorkspaceScreen(
                workspace=ws,
                allowed=allowed,
                default=default,
                allow_autostart=allow_autostart,
            ),
            self._on_edited,
        )

    def _on_edited(self, result) -> None:
        if result:
            self.refresh_lists()

    def action_logout(self) -> None:
        self.app.do_logout()

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
        retries = 0
        max_retries = 3
        while retries <= max_retries:
            token = state.token()
            if not token:
                self.app.session_expired()
                return
            try:
                await listen_for_status(
                    url, token, on_event=self._on_status_event
                )
                # Clean close (server restart, idle timeout) — reconnect.
                retries += 1
                self.app.live_extra = "status: reconnecting…"
                self._refresh_status()
                await asyncio.sleep(2)
                continue
            except AuthError:
                self.app.session_expired()
                return
            except Exception as exc:
                # Transient error — back off (exponential) and retry.
                retries += 1
                if retries > max_retries:
                    logger.debug(
                        "Status WS gave up after %d retries: %s",
                        max_retries,
                        exc,
                    )
                    break
                self.app.live_extra = "status: reconnecting…"
                self._refresh_status()
                await asyncio.sleep(min(2 * (2 ** (retries - 1)), 30))
                continue
        self.app.live_extra = (
            "status: disconnected (switch server to reconnect)"
        )
        self._refresh_status()

    async def _token_refresh_loop(self) -> None:
        """Proactively refresh the access token before it expires."""
        result = await run_token_refresh_loop(self.app.tui_state)
        if result in ("expired", "no_token"):
            self.app.session_expired()

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
                    break
        # The highlighted row's running state may have changed — refresh
        # the Stop/Start hint label so it never offers the wrong action.
        self._refresh_action_hints()

    def _forward_status_to_detail(self, event: dict) -> None:
        """Mirror a live status broadcast onto an open detail screen."""
        from .workspace_detail import WorkspaceDetailScreen  # noqa: allow-deferred-import

        for screen in reversed(self.app.screen_stack):
            if isinstance(screen, WorkspaceDetailScreen):
                screen.apply_status_event(event)
                break
