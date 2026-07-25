"""Screens for the klangk TUI: login, main shell, server switch/add.

Navigation between screens is driven by methods on ``KlangkApp``
(``login_succeeded`` / ``do_logout`` / ``server_changed``); screens stay
free of cross-screen coupling and reach state through ``self.app.tui_state``.
"""

from __future__ import annotations

import asyncio
import datetime
import subprocess
import sys
import time
from urllib.parse import urlparse

import logging

import httpx

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.dom import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)
from textual.widgets.option_list import Option

from .state import LoginError
from ..client import AuthError, Workspace, WorkspaceNotFoundError
from ..config import AliasConflictError
from ..env import validate_env_entry
from ..mount import (
    validate_allowed_domain_spec,
    validate_mount_spec,
)
from ..transport import is_valid_server_spec
from .widgets import StatusBar
from .ws import listen_for_status

logger = logging.getLogger(__name__)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no confirmation dialog. Dismisses with True on confirm."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 0 2;
        border: round $primary;
        background: $panel;
    }
    ConfirmScreen Horizontal {
        align-horizontal: right;
        height: auto;
    }
    """

    def __init__(
        self,
        message: str,
        *,
        yes_label: str = "Delete",
        yes_variant: str = "error",
        no_label: str = "Cancel",
    ) -> None:
        super().__init__()
        self.message = message
        self._yes_label = yes_label
        self._yes_variant = yes_variant
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(self.message)),
            Horizontal(
                Button(self._no_label, id="no"),
                Button(self._yes_label, id="yes", variant=self._yes_variant),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class SpatialListView(ListView):
    """A ListView that releases focus at its top/bottom boundaries to a
    target widget, enabling spatial navigation without Tab (#1781).

    Subclasses (or instances) declare ``SPATIAL_UP_TARGET`` and/or
    ``SPATIAL_DOWN_TARGET`` — a widget type or CSS selector that receives
    focus when Up is pressed at the first row or Down at the last.
    """

    SPATIAL_UP_TARGET = None
    SPATIAL_DOWN_TARGET = None

    def action_cursor_up(self) -> None:
        if self.index in (0, None) and self.SPATIAL_UP_TARGET:
            self.screen.query_one(self.SPATIAL_UP_TARGET).focus()
        else:
            super().action_cursor_up()

    def action_cursor_down(self) -> None:
        items = list(self.query(ListItem))
        if (
            self.index is not None
            and items
            and self.index >= len(items) - 1
            and self.SPATIAL_DOWN_TARGET
        ):
            self.screen.query_one(self.SPATIAL_DOWN_TARGET).focus()
        else:
            super().action_cursor_down()


class ServerListView(SpatialListView):
    """Server picker — Down from the last row enters the URL input."""

    SPATIAL_DOWN_TARGET = "#server_input"


class SpatialNavScreen(Screen):
    """Screen mixin for spatial Up/Down navigation between a chain of
    widgets (inputs, buttons) in reading order (#1781).

    Declare ``SPATIAL_CHAIN`` (widget ids, top-to-bottom) and optionally
    ``SPATIAL_UP_EXIT`` (the widget id to focus when Up is pressed at the
    top of the chain). The mixin handles the rest — no per-screen
    ``on_key`` body needed.
    """

    SPATIAL_CHAIN: list[str] = []
    SPATIAL_UP_EXIT: str | None = None

    BINDINGS = [
        Binding("up", "spatial_up", show=False),
        Binding("down", "spatial_down", show=False),
    ]

    def action_spatial_up(self) -> None:
        fid = getattr(self.focused, "id", None) if self.focused else None
        if not fid or fid not in self.SPATIAL_CHAIN:
            return
        pos = self.SPATIAL_CHAIN.index(fid)
        if pos > 0:
            self.query_one(f"#{self.SPATIAL_CHAIN[pos - 1]}").focus()
        elif self.SPATIAL_UP_EXIT:
            self.query_one(f"#{self.SPATIAL_UP_EXIT}").focus()

    def action_spatial_down(self) -> None:
        fid = getattr(self.focused, "id", None) if self.focused else None
        if not fid or fid not in self.SPATIAL_CHAIN:
            return
        pos = self.SPATIAL_CHAIN.index(fid)
        if pos < len(self.SPATIAL_CHAIN) - 1:
            self.query_one(f"#{self.SPATIAL_CHAIN[pos + 1]}").focus()


class NonFocusableVerticalScroll(VerticalScroll):
    """VerticalScroll that stays out of the keyboard-focus cycle.

    Plain ``VerticalScroll`` has ``can_focus = True``, which inserts the
    container itself into the Tab order.  We only want the form *fields*
    focusable, not the scroll pane.  (#1783)
    """

    can_focus = False


class TabSkipMixin:
    """Cycle Tab through a primary field set, skipping editor buttons/lists.

    Editor OptionLists remain focusable (for Delete / "e" keyboard actions)
    but are mapped to their entry-input position so Tab jumps input-to-input.
    (#1783)
    """

    _TAB_ORDER: list[str] = []
    _LIST_TO_INPUT: dict[str, str] = {}

    def on_key(self, event) -> None:
        if event.key not in ("tab", "shift+tab"):
            return
        fid = getattr(self.focused, "id", None) if self.focused else None
        base = self._LIST_TO_INPUT.get(fid, fid)
        if base not in self._TAB_ORDER:
            return
        event.stop()
        idx = self._TAB_ORDER.index(base)
        step = 1 if event.key == "tab" else -1
        n = len(self._TAB_ORDER)
        for i in range(1, n):
            nxt = (idx + step * i) % n
            target = self.query_one(f"#{self._TAB_ORDER[nxt]}")
            if target.display and not target.disabled:
                target.focus()
                return


# Aliased for readability at call-sites that don't care about focusability.


class LoginScreen(SpatialNavScreen):
    """Credential screen that also picks the server to log into.

    A fresh user with no server configured can pick a known alias, select
    the co-located default UDS, or type a URL (which is saved as a new
    alias) — then authenticate. Once a server is active the screen
    adapts to its auth mode: ``none`` → auto no-auth login; ``oidc`` →
    SSO hand-off (browser); ``password``/``both`` → email/handle +
    password form; ``unreachable`` → diagnostic.
    """

    BINDINGS = [
        ("d", "delete_server", "Delete server")
    ]  # spatial nav via SpatialNavScreen mixin
    SPATIAL_CHAIN = [
        "server_input",
        "use_server",
        "identifier",
        "password",
        "login",
    ]
    SPATIAL_UP_EXIT = "server_options"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("", id="server_line"),
            ServerListView(id="server_options"),
            Input(
                placeholder=("Server URL or alias (e.g. https://host, prod)"),
                id="server_input",
            ),
            Horizontal(
                Button("Use server", id="use_server"),
                classes="actions",
            ),
            Static("", id="notice"),
            Input(placeholder="Email or handle", id="identifier"),
            Input(placeholder="Password", id="password", password=True),
            Horizontal(
                Button("Log in via browser (SSO)", id="oidc"),
                Button("Log in", id="login", variant="primary"),
                classes="actions",
            ),
            Static("", id="message"),
            id="login_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate_servers()
        if self.app.tui_state.current_url() is not None:
            self._setup_auth()
        else:
            self._show_no_server()

    def _show_no_server(self) -> None:
        self.query_one("#server_line", Static).update(
            "No server selected. Pick one below or enter a URL,"
            " then press 'Use server'."
        )
        self._disable_credentials()

    # --- server picker ---

    def _populate_servers(self) -> None:
        lv = self.query_one("#server_options", ListView)
        lv.clear()
        current = self.app.tui_state.current_url()
        known = self.app.tui_state.known_servers()
        known_urls = {s.url for s in known}
        for s in known:
            mark = "*" if s.url == current else " "
            label = Text(
                f"{mark} {s.alias}  ({s.url})",
                overflow="ellipsis",
                no_wrap=True,
            )
            lv.append(ListItem(Label(label), name=s.url))
        uds = self.app.tui_state.default_uds()
        # Only offer the auto-detected default UDS if no alias already covers
        # it (otherwise it would duplicate the persisted alias row).
        if uds and uds != current and uds not in known_urls:
            label = Text(
                f"  Local klangkd (UDS)  ({uds})",
                overflow="ellipsis",
                no_wrap=True,
            )
            lv.append(ListItem(Label(label), name=uds))
        # Autofocus the first server entry (#1826).
        if lv.query(ListItem):
            lv.focus()
            if lv.index is None:
                lv.index = 0

    @staticmethod
    def _derive_alias(raw: str) -> str:
        if "://" in raw:
            host = urlparse(raw).hostname
            if host:
                return host
        name = raw.rstrip("/").split("/")[-1]
        return name or "server"

    def _choose_server(self, raw: str | None) -> None:
        raw = (raw or "").strip()
        if not raw:
            self._set_message("Enter a server URL or alias.", error=True)
            return
        self.run_worker(self._do_choose_server(raw), exit_on_error=False)

    async def _do_choose_server(self, raw: str) -> None:
        cfg = await asyncio.to_thread(self.app.tui_state.cfg)
        if raw in cfg.servers:
            await asyncio.to_thread(
                self.app.tui_state.switch_server, cfg.servers[raw].url
            )
        elif is_valid_server_spec(raw):
            # If a server with the derived alias already exists, switch to it
            # instead of trying to add a duplicate (#1849).
            alias = self._derive_alias(raw)
            if alias in cfg.servers:
                await asyncio.to_thread(
                    self.app.tui_state.switch_server, cfg.servers[alias].url
                )
            else:
                try:
                    await asyncio.to_thread(
                        self.app.tui_state.add_server,
                        alias,
                        raw,
                    )
                except AliasConflictError as exc:
                    self._set_message(str(exc), error=True)
                    return
        else:
            self._set_message(
                "Enter a server URL (https://host), a socket path"
                " (/...), or a known alias.",
                error=True,
            )
            return
        self.query_one("#server_input", Input).value = ""
        self._set_message("")
        self._populate_servers()
        self._setup_auth()

    def action_delete_server(self) -> None:
        lv = self.query_one("#server_options", ListView)
        child = lv.highlighted_child
        if child is None:
            self._set_message("Select a server to delete.", error=True)
            return
        url = child.name

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_delete_server(url), exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"), _on_confirm
        )

    async def _do_delete_server(self, url: str) -> None:
        deleted = await asyncio.to_thread(
            self.app.tui_state.delete_server, url
        )
        if deleted:
            self._set_message("Server deleted.")
        else:
            self._set_message("Not a saved alias.", error=True)
        self._populate_servers()
        if self.app.tui_state.current_url() is None:
            self._show_no_server()
        else:
            self._setup_auth()

    # --- auth-mode setup ---

    def _setup_auth(self) -> None:
        self.run_worker(self._setup_auth_async, exit_on_error=False)

    async def _setup_auth_async(self) -> None:
        state = self.app.tui_state
        mode = await asyncio.to_thread(state.auth_mode)
        self.query_one("#server_line", Static).update(
            f"Server: {state.current_url()}"
        )
        self._enable_credentials()
        notice = self.query_one("#notice", Static)
        if mode == "none":
            notice.update("No-auth server — logging in…")
            self.call_after_refresh(self._attempt_none)
            return
        if mode == "unreachable":
            notice.update(
                "Cannot reach the server. Pick another or check klangkd."
            )
            self._disable_form()
            return
        if mode == "oidc":
            notice.update(
                "This server uses single sign-on. Click 'Log in via browser'."
            )
            self._disable_form()
            return
        # password / both
        notice.update("Enter your credentials.")
        self.query_one("#oidc", Button).disabled = True

    def _disable_credentials(self) -> None:
        # No server chosen: disable the whole credential area.
        self.query_one("#identifier", Input).disabled = True
        self.query_one("#password", Input).disabled = True
        self.query_one("#login", Button).disabled = True
        self.query_one("#oidc", Button).disabled = True

    def _enable_credentials(self) -> None:
        self.query_one("#identifier", Input).disabled = False
        self.query_one("#password", Input).disabled = False
        self.query_one("#login", Button).disabled = False
        self.query_one("#oidc", Button).disabled = False

    def _disable_form(self) -> None:
        # Server set but not password-authable (oidc/unreachable): disable
        # the password form, leave the SSO button usable.
        self.query_one("#identifier", Input).disabled = True
        self.query_one("#password", Input).disabled = True
        self.query_one("#login", Button).disabled = True

    def _set_message(self, text: str, *, error: bool = False) -> None:
        self.query_one("#message", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- login arms ---

    def _attempt_none(self) -> None:
        self.run_worker(self._do_login_none, exit_on_error=False)

    async def _do_login_none(self) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.login_none)
        except LoginError as exc:
            self._set_message(f"No-auth login failed: {exc}", error=True)
            return
        self.app.login_succeeded()

    def _attempt_password(self) -> None:
        identifier = self.query_one("#identifier", Input).value.strip()
        password = self.query_one("#password", Input).value
        if not identifier or not password:
            self._set_message(
                "Email/handle and password are required.", error=True
            )
            return
        self.run_worker(
            self._do_login_password(identifier, password),
            exit_on_error=False,
        )

    async def _do_login_password(self, identifier: str, password: str) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.login_password, identifier, password
            )
        except LoginError as exc:
            self._set_message(f"Login failed: {exc}", error=True)
            return
        self.app.login_succeeded()

    def _attempt_oidc(self) -> None:
        self.run_worker(self._do_login_oidc, exit_on_error=False)

    async def _do_login_oidc(self) -> None:
        providers = await asyncio.to_thread(self.app.tui_state.oidc_providers)
        if not providers:
            self._set_message("No SSO provider configured.", error=True)
            return
        provider_id = providers[0]["id"]
        try:
            await asyncio.to_thread(self.app.tui_state.oidc_login, provider_id)
        except LoginError as exc:
            self._set_message(f"SSO failed: {exc}", error=True)
            return
        if self.app.tui_state.is_authenticated():
            self.app.login_succeeded()
        else:
            self._set_message("SSO did not complete.")

    # --- event handlers ---

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._choose_server(getattr(event.item, "name", "") or "")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "use_server":
            self._choose_server(self.query_one("#server_input", Input).value)
        elif event.button.id == "login":
            self._attempt_password()
        elif event.button.id == "oidc":
            self._attempt_oidc()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "server_input":
            self._choose_server(event.input.value)
        elif event.input.id in ("identifier", "password"):
            self._attempt_password()


class WorkspaceListView(SpatialListView):
    """Workspace list — Up from the first row returns to the tab strip."""

    SPATIAL_UP_TARGET = Tabs


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
    """

    BINDINGS = [
        ("s", "switch_server", "Switch server"),
        ("n", "create", "New"),
        ("l", "logout", "Logout"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="ws_tabs"):
            yield TabPane("Owned by me", WorkspaceListView(id="owned_list"))
            yield TabPane("Shared to me", WorkspaceListView(id="shared_list"))
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.app.title = "Klangk: Workspaces"
        self._initial_focus_done = False
        self.refresh_lists()
        if self.app.tui_state.is_authenticated():
            self.app.run_worker(self._status_loop, name="status-ws")

    def on_tabbed_content_tab_activated(self, event) -> None:
        """Focus the first workspace row when switching tabs (#1792)."""
        self._focus_visible_list()

    def _focus_visible_list(self) -> None:
        """Focus the first item in the visible workspace list (#1792)."""
        for lv in self.query(WorkspaceListView):
            if lv.display and lv.query(ListItem):
                lv.focus()
                if lv.index is None:
                    lv.index = 0
                return

    def on_key(self, event) -> None:
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
        self.app.push_screen(ServerSwitchScreen())

    def action_logout(self) -> None:
        self.app.do_logout()

    def action_create(self) -> None:
        self.run_worker(self._do_create, exit_on_error=False)

    async def _do_create(self) -> None:
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
            for sel in ("#owned_list", "#shared_list"):
                self._populate(
                    sel, [], empty_label="(session expired — re-login)"
                )
            self._refresh_status()
            return
        self._populate("#owned_list", owned)
        for ws in owned:
            wid = str(getattr(ws, "id", "") or "")
            if wid:
                self._ws_by_id[wid] = ws
        self._populate("#shared_list", shared)
        for ws in shared:
            wid = str(getattr(ws, "id", "") or "")
            if wid:
                self._ws_by_id[wid] = ws
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
        for screen in reversed(self.app.screen_stack):
            if isinstance(screen, WorkspaceDetailScreen):
                screen.apply_status_event(event)
                break


class WorkspaceDetailScreen(Screen):
    """Read-only workspace detail + restart / duplicate / delete actions."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("e", "edit", "Edit"),
        ("r", "restart", "Restart"),
        ("s", "stop", "Stop"),
        ("d", "duplicate", "Duplicate"),
        ("x", "delete", "Delete"),
        ("delete", "delete_terminal", "Del term"),
    ]

    DEFAULT_CSS = """
    WorkspaceDetailScreen #term_label {
        text-style: bold;
        margin-bottom: 0;
    }
    WorkspaceDetailScreen #term_list {
        height: auto;
        max-height: 14;
    }
    WorkspaceDetailScreen #detail_body {
        margin-top: 1;
    }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self._ws = None
        self._terminals: list[dict] = []
        self._missing = False
        self._load_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("Terminals", id="term_label"),
            SpatialListView(id="term_list"),
            Static("", id="detail_body"),
            Static("", id="detail_msg"),
            id="detail_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._mount_async, exit_on_error=False)
        self._uptime_timer = self.set_interval(5, self._tick_uptime)

    async def _mount_async(self) -> None:
        await self._load()
        if self._ws is not None and not self._ws.running:
            await self._start_if_stopped()
        self.run_worker(self._load_terminals, exit_on_error=False)

    async def _load(self) -> None:
        try:
            self._ws = await asyncio.to_thread(
                self.app.tui_state.find_workspace, self._name
            )
            self._missing = False
            self._load_error = None
        except WorkspaceNotFoundError:
            self._ws = None
            self._missing = True
            self._load_error = None
        except AuthError:
            self._ws = None
            self._missing = False
            self._load_error = "Session expired — please log in again."
        except Exception:
            self._ws = None
            self._missing = False
            self._load_error = None
        self._display()

    async def _start_if_stopped(self) -> None:
        """Auto-start a stopped workspace container on visit.

        The Flutter UI does this via its WebSocket ``connectWorkspace``
        call; the TUI replicates the behaviour with a restart request.
        """
        self._msg("Starting container…")
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Auto-start failed: {exc}", error=True)
            return
        await self._load()
        self._msg("Container started.")
        self.app.refresh_workspaces()

    @staticmethod
    def _bindings_list(stop_label: str = "Stop") -> list:
        """Bindings with a dynamic label for the stop/start key (#1791)."""
        return [
            ("escape", "app.pop_screen", "Back"),
            ("e", "edit", "Edit"),
            ("r", "restart", "Restart"),
            ("s", "stop", stop_label),
            ("d", "duplicate", "Duplicate"),
            ("x", "delete", "Delete"),
            ("delete", "delete_terminal", "Del term"),
        ]

    def _display(self) -> None:
        ws = self._ws
        body = self.query_one("#detail_body", Static)
        if ws is None:
            body.update(Text(self._load_error or "Could not load workspace."))
            return
        # Toggle the 's' binding label between Stop / Start.
        self.BINDINGS = [
            Binding(*b)
            for b in self._bindings_list("Stop" if ws.running else "Start")
        ]
        self.refresh_bindings()
        lines = [
            f"running: {'yes' if ws.running else 'no'}",
            f"health: {ws.health or '-'}",
        ]
        if ws.running and ws.service_started_at:
            elapsed = int(time.time() - ws.service_started_at)
            if elapsed >= 0:
                parts = []
                days, rem = divmod(elapsed, 86400)
                hours, rem = divmod(rem, 3600)
                minutes, _ = divmod(rem, 60)
                if days:
                    parts.append(f"{days}d")
                if hours:
                    parts.append(f"{hours}h")
                parts.append(f"{minutes}m")
                lines.append(f"uptime: {' '.join(parts)}")
        if ws.health_message:
            lines.append(f"health note: {ws.health_message}")
        if ws.image:
            lines.append(f"image: {ws.image}")
        if ws.service_command:
            lines.append(f"service command: {ws.service_command}")
        if ws.health_check:
            lines.append(f"health check: {ws.health_check}")
        lines.append(f"auto-start: {'on' if ws.auto_start else 'off'}")
        if ws.mounts:
            lines.append("mounts:")
            lines.extend(f"  {m}" for m in ws.mounts)
        if ws.env:
            lines.append("environment:")
            lines.extend(f"  {k}={v}" for k, v in ws.env.items())
        if ws.allowed_domains:
            lines.append("allowed domains:")
            lines.extend(f"  {d}" for d in ws.allowed_domains)
        if ws.owner_email:
            lines.append(f"owner: {ws.owner_email}")
        body.update(Text("\n".join(lines)))

    def _tick_uptime(self) -> None:
        """Refresh the display periodically to update the uptime counter."""
        if (
            self._ws is not None
            and self._ws.running
            and self._ws.service_started_at
        ):
            self._display()

    def _msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#detail_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    def action_edit(self) -> None:
        if self._ws is None:
            return
        self.run_worker(self._do_edit, exit_on_error=False)

    async def _do_edit(self) -> None:
        state = self.app.tui_state
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
                workspace=self._ws,
                allowed=allowed,
                default=default,
                allow_autostart=allow_autostart,
            ),
            self._on_edited,
        )

    def _on_edited(self, result: str | bool | None) -> None:
        if isinstance(result, str):
            self._name = result
        if result:
            self.run_worker(self._reload_after_edit, exit_on_error=False)

    async def _reload_after_edit(self) -> None:
        await self._load()
        try:
            self.app.query_one(MainScreen).refresh_lists()
        except NoMatches:
            pass

    def apply_status_event(self, event: dict) -> None:
        """Update running/health from a live status broadcast.

        Only applies when the event is for this workspace; ``workspaces_changed``
        re-fetches. User-derived text is rendered via ``Text`` so bracket
        characters in names/messages never trigger markup parsing.
        """
        if self._ws is None:
            return
        etype = event.get("type")
        ws_id = str(getattr(self._ws, "id", "") or "")
        eid = str(event.get("workspace_id") or "")
        if eid and ws_id and eid != ws_id:
            return  # event for a different workspace
        if etype == "workspaces_changed":
            self.run_worker(self._reload_on_status, exit_on_error=False)
            return
        if etype == "container_status":
            self._ws.running = bool(event.get("running"))
            if "service_started_at" in event:
                self._ws.service_started_at = event["service_started_at"]
        elif etype == "service_health":
            self._ws.running = bool(event.get("running", self._ws.running))
            self._ws.health = (
                "healthy" if event.get("healthy") else "unhealthy"
            )
            msg = event.get("health_message")
            if msg is not None:
                self._ws.health_message = msg
        else:
            return
        self._display()

    async def _reload_on_status(self) -> None:
        await self._load()
        if self._missing:
            self.app.pop_screen()

    # --- terminals (own) ---

    async def _load_terminals(self) -> None:
        try:
            windows = await self.app.tui_state.list_terminals(self._name)
        except Exception:
            windows = []
        self._terminals = windows or []
        self._render_terminals()

    def _render_terminals(self) -> None:
        lv = self.query_one("#term_list", ListView)
        lv.clear()
        if not self._terminals:
            lv.append(ListItem(Label(Text("(no terminals)")), name=""))
            return
        for w in self._terminals:
            idx = w.get("index", "")
            name = w.get("name") or idx
            lv.append(ListItem(Label(Text(f"{idx}  {name}")), name=str(idx)))
        # Autofocus the first terminal (#1808).
        lv.focus()
        if lv.index is None:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Suspend the TUI and spawn ``klangk shell`` for the selected terminal."""
        terminal = getattr(event.item, "name", "") or ""
        if not terminal or self._ws is None:
            return
        cmd = [sys.executable, "-m", "klangk.cli.main"]
        server = self.app.tui_state.current_url()
        if server:
            cmd += ["--server", server]
        cmd += ["shell", self._name, terminal]
        with self.app.suspend():
            subprocess.run(cmd)

    def action_delete_terminal(self) -> None:
        lv = self.query_one("#term_list", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        if not child.name:
            return
        if len(self._terminals) <= 1:
            self._msg("Can't delete the last terminal.", error=True)
            return
        index = int(child.name)
        self.run_worker(self._do_delete_terminal(index), exit_on_error=False)

    async def _do_delete_terminal(self, index: int) -> None:
        try:
            windows = await self.app.tui_state.close_terminal(
                self._name, index
            )
        except Exception as exc:
            self._msg(f"Delete failed: {exc}", error=True)
            return
        if not windows:
            # The last terminal is protected client-side, so an empty result
            # here means the close/refresh failed — don't claim success.
            self._msg(
                "Delete failed — could not refresh terminals.", error=True
            )
            return
        self._terminals = windows
        self._render_terminals()
        self._msg(f"Deleted terminal {index}.")

    # --- actions ---

    def action_restart(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_restart, exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Restart '{self._name}'? This ends active terminal"
                " sessions and recreates the container.",
                yes_label="Restart",
                yes_variant="warning",
            ),
            _on_confirm,
        )

    async def _do_restart(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Restart failed: {exc}", error=True)
            return
        if self._ws is not None:
            self._ws.service_started_at = time.time()
            self._display()
        self._msg("Restart requested.")
        self.app.refresh_workspaces()

    def action_stop(self) -> None:
        if self._ws is None:
            return
        if self._ws.running:
            self._confirm_stop()
        else:
            self.run_worker(self._do_start, exit_on_error=False)

    def _confirm_stop(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_stop, exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Stop '{self._name}'? This ends active terminal sessions.",
                yes_label="Stop",
                yes_variant="warning",
            ),
            _on_confirm,
        )

    async def _do_stop(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.stop_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Stop failed: {exc}", error=True)
            return
        if self._ws is not None:
            self._ws.running = False
            self._ws.service_started_at = None
            self._display()
        self._msg("Stop requested.")
        self.app.refresh_workspaces()

    async def _do_start(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.start_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Start failed: {exc}", error=True)
            return
        if self._ws is not None:
            self._ws.running = True
            self._ws.service_started_at = time.time()
            self._display()
        self._msg("Start requested.")
        self.app.refresh_workspaces()

    def action_delete(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_delete, exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Delete '{self._name}'? This permanently deletes the"
                " workspace and its container."
            ),
            _on_confirm,
        )

    async def _do_delete(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.delete_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Delete failed: {exc}", error=True)
            return
        self.app.pop_screen()  # back to the list
        self.app.refresh_workspaces()

    def action_duplicate(self) -> None:
        self.app.push_screen(DuplicateScreen(self._name), self._on_duplicate)

    def _on_duplicate(self, new_name: str | None) -> None:
        if not new_name:
            return
        self.run_worker(self._do_duplicate(new_name), exit_on_error=False)

    async def _do_duplicate(self, new_name: str) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.duplicate_workspace, self._name, new_name
            )
        except Exception as exc:
            self._msg(f"Duplicate failed: {exc}", error=True)
            return
        self._msg(f"Duplicated as '{new_name}'.")
        self.app.refresh_workspaces()


class DuplicateScreen(ModalScreen):
    """Prompt for a new name to duplicate a workspace under."""

    DEFAULT_CSS = """
    DuplicateScreen { align: center middle; }
    DuplicateScreen > Vertical {
        width: 64; max-width: 90%; padding: 0 2;
        border: round $primary; background: $panel;
    }
    DuplicateScreen Horizontal {
        align-horizontal: right; height: auto; padding-top: 1;
    }
    """

    def __init__(self, source_name: str) -> None:
        super().__init__()
        self._source = source_name

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(f"Duplicate '{self._source}' as:")),
            Input(value=f"{self._source}-copy", id="dup_name"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Duplicate", id="ok", variant="primary"),
            ),
            id="dup_box",
        )

    def _commit(self) -> None:
        name = self.query_one("#dup_name", Input).value.strip()
        self.dismiss(name or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._commit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "dup_name":
            self._commit()


class CreateWorkspaceScreen(TabSkipMixin, Screen):
    """Full-screen workspace create form (parity with Flutter
    ``CreateWorkspaceDialog``).

    Fields, top to bottom: name, container image (``Select`` populated
    from ``/api/v1/images``), a mounts list editor, an env list editor,
    an optional service shell command, an optional health-check command,
    and — only when the server permits it — an auto-start checkbox.
    Mounts/env are validated client-side (``validate_mount_spec`` /
    ``validate_env_entry``) exactly as the Flutter dialog and the CLI
    ``create`` command do.

    Images and the ``allow_autostart`` flag are fetched by the caller
    (``MainScreen.action_create``) and passed in, because ``self.app`` is
    not available until the screen is mounted.
    """

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    _TAB_ORDER = [
        "name",
        "image",
        "mount_input",
        "env_input",
        "allow_input",
        "auto_start",
        "cancel",
        "create",
    ]
    _LIST_TO_INPUT = {
        "mount_list": "mount_input",
        "env_list": "env_input",
        "allow_list": "allow_input",
    }

    def __init__(
        self,
        *,
        allowed: list[str],
        default: str,
        allow_autostart: bool,
    ) -> None:
        super().__init__()
        self._allowed = list(allowed)
        self._default = default or ""
        self._allow_autostart = bool(allow_autostart)
        self._mounts: list[str] = []
        self._env: dict[str, str] = {}
        self._allowed_domains: list[str] = []
        if self._allowed:
            # Select tuples are (prompt, value). Prompts are rich Text so an
            # image name containing brackets can't trigger markup parsing.
            self._select_options = [(Text(img), img) for img in self._allowed]
            self._select_value = (
                self._default if self._default in self._allowed else None
            )
        else:
            # Couldn't list images — offer a single inert placeholder so the
            # user can still create; the server applies its default image.
            self._select_options = [
                (Text("(server default)"), "(server default)")
            ]
            self._select_value = "(server default)"

    def compose(self) -> ComposeResult:
        if self._select_value is not None:
            image_select = Select(
                self._select_options, value=self._select_value, id="image"
            )
        else:
            # No valid default to preselect — leave the picker unselected
            # (the server applies its default image if none is chosen).
            image_select = Select(self._select_options, id="image")
        yield Header(show_clock=False)
        yield NonFocusableVerticalScroll(
            Static("New workspace", classes="title"),
            Static("", id="create_msg"),
            Horizontal(Static("Name"), Input(id="name"), classes="field-row"),
            Horizontal(Static("Image"), image_select, classes="field-row"),
            Static(
                "Mounts  (source:/container/path[:opts])",
                classes="editor-label",
            ),
            Horizontal(
                Input(
                    id="mount_input",
                    placeholder="/host/path:/container/path",
                ),
                Button("Add", id="add_mount"),
                Button("Remove", id="rm_mount"),
            ),
            OptionList(id="mount_list", classes="editor-list"),
            Static("Environment  (KEY=VALUE)", classes="editor-label"),
            Horizontal(
                Input(id="env_input", placeholder="KEY=VALUE"),
                Button("Add", id="add_env"),
                Button("Remove", id="rm_env"),
            ),
            OptionList(id="env_list", classes="editor-list"),
            Static(
                "Allowed Domains  (host or host:port; empty = unrestricted)",
                classes="editor-label",
            ),
            Horizontal(
                Input(id="allow_input", placeholder="github.com:443"),
                Button("Add", id="add_allow"),
                Button("Remove", id="rm_allow"),
            ),
            OptionList(id="allow_list", classes="editor-list"),
            Collapsible(
                Horizontal(
                    Static("Command"),
                    Input(id="command"),
                    classes="field-row",
                ),
                Horizontal(
                    Static("Health"),
                    Input(id="health_check"),
                    classes="field-row",
                ),
                title="Advanced",
            ),
            Checkbox("Auto start", id="auto_start"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Create", id="create", variant="primary"),
                classes="actions",
            ),
            id="create_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()

    def _skip_editors_on_tab(self) -> None:
        """Editor buttons stay out of the Tab cycle (#1783).

        Add is reachable via Enter in the input; Remove via mouse click.
        Lists stay focusable for Delete/"e" keyboard actions but Tab skips
        them via :class:`TabSkipMixin`.
        """
        for wid in (
            "add_mount",
            "rm_mount",
            "add_env",
            "rm_env",
            "add_allow",
            "rm_allow",
        ):
            self.query_one(f"#{wid}").can_focus = False

    def _msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#create_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- mounts list editor ---

    def _render_mounts(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        ol.clear_options()
        if not self._mounts:
            ol.add_option(Option(Text("(no mounts)"), id="", disabled=True))
            return
        for i, m in enumerate(self._mounts):
            ol.add_option(Option(Text(m), id=f"m{i}"))

    def _add_mount(self) -> None:
        inp = self.query_one("#mount_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_mount_spec(v)
        if err:
            self._msg(err, error=True)
            return
        self._mounts.append(v)
        inp.value = ""
        self._msg("")
        self._render_mounts()

    def _remove_mount(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._mounts):
            return
        del self._mounts[idx]
        self._render_mounts()

    # --- env list editor ---

    def _render_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        ol.clear_options()
        if not self._env:
            ol.add_option(Option(Text("(no env vars)"), id="", disabled=True))
            return
        for i, (k, val) in enumerate(self._env.items()):
            ol.add_option(Option(Text(f"{k}={val}"), id=f"e{i}"))

    def _add_env(self) -> None:
        inp = self.query_one("#env_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_env_entry(v)
        if err:
            self._msg(err, error=True)
            return
        key, _, value = v.partition("=")
        self._env[key] = value
        inp.value = ""
        self._msg("")
        self._render_env()

    def _remove_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        del self._env[keys[idx]]
        self._render_env()

    # --- allowed-domains list editor (#1745) ---

    def _render_allowed_domains(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        ol.clear_options()
        if not self._allowed_domains:
            ol.add_option(Option(Text("(unrestricted)"), id="", disabled=True))
            return
        for i, d in enumerate(self._allowed_domains):
            ol.add_option(Option(Text(d), id=f"a{i}"))

    def _add_allowed_domain(self) -> None:
        inp = self.query_one("#allow_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_allowed_domain_spec(v)
        if err:
            self._msg(err, error=True)
            return
        if v not in self._allowed_domains:
            self._allowed_domains.append(v)
        inp.value = ""
        self._msg("")
        self._render_allowed_domains()

    def _remove_allowed_domain(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._allowed_domains):
            return
        del self._allowed_domains[idx]
        self._render_allowed_domains()

    # --- create ---

    def _create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._msg("Name is required.", error=True)
            return
        sel = self.query_one("#image", Select)
        val = sel.value
        # Send only a real, non-default selection. When the server's default
        # isn't in the allowed list we start unselected (Select.BLANK), so an
        # untouched picker omits the image — matching the Flutter dialog.
        if (
            val is Select.BLANK
            or val is Select.NULL
            or not self._allowed
            or val == self._default
        ):
            image = None
        else:
            image = val
        command = self.query_one("#command", Input).value.strip() or None
        health_check = (
            self.query_one("#health_check", Input).value.strip() or None
        )
        auto = (
            self._allow_autostart
            and self.query_one("#auto_start", Checkbox).value
        )
        mounts = list(self._mounts) or None
        env = dict(self._env) or None
        allowed_domains = list(self._allowed_domains) or None
        self.run_worker(
            self._do_create_workspace(
                name,
                image,
                command,
                auto,
                mounts,
                env,
                health_check,
                allowed_domains,
            ),
            exit_on_error=False,
        )

    async def _do_create_workspace(
        self,
        name,
        image,
        command,
        auto,
        mounts,
        env,
        health_check,
        allowed_domains,
    ) -> None:
        try:
            ws = await asyncio.to_thread(
                self.app.tui_state.create_workspace,
                name,
                image=image,
                service_command=command,
                auto_start=auto,
                mounts=mounts,
                env=env,
                health_check=health_check,
                allowed_domains=allowed_domains,
            )
        except AuthError:
            self._msg("Session expired — please log in again.", error=True)
            return
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except (ValueError, KeyError):
                detail = exc.response.text or str(exc)
            self._msg(f"Failed to create: {detail}", error=True)
            return
        except Exception as exc:
            self._msg(f"Failed to create: {exc}", error=True)
            return
        self.dismiss(ws.name)

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel":
            self.dismiss(None)
        elif bid == "create":
            self._create()
        elif bid == "add_mount":
            self._add_mount()
        elif bid == "rm_mount":
            self._remove_mount()
        elif bid == "add_env":
            self._add_env()
        elif bid == "rm_env":
            self._remove_env()
        elif bid == "add_allow":
            self._add_allowed_domain()
        elif bid == "rm_allow":
            self._remove_allowed_domain()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        eid = event.input.id
        if eid == "mount_input":
            self._add_mount()
        elif eid == "env_input":
            self._add_env()
        elif eid == "allow_input":
            self._add_allowed_domain()
        elif eid in ("name", "command", "health_check"):
            self._create()


class EditWorkspaceScreen(TabSkipMixin, Screen):
    """Full-screen workspace edit form (parity with Flutter
    ``WorkspaceSettingsPanel``).

    Like :class:`CreateWorkspaceScreen` but pre-populated from an existing
    workspace, saving via a partial ``PUT``. Saving a change to a
    container-create-time field (image / mounts / env / service_command /
    allowed_domains) on a *running* workspace prompts a "restart needed to
    apply" offer (#1778, #1749); ``setup_state`` / ``health_check`` propagate
    live and never trigger it.
    """

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("delete", "remove_item", "Remove"),
        ("e", "edit_item", "Edit"),
    ]

    _TAB_ORDER = [
        "name",
        "image",
        "mount_input",
        "env_input",
        "allow_input",
        "auto_start",
        "cancel",
        "save",
    ]
    _LIST_TO_INPUT = {
        "mount_list": "mount_input",
        "env_list": "env_input",
        "allow_list": "allow_input",
    }

    def __init__(
        self,
        *,
        workspace: Workspace,
        allowed: list[str],
        default: str,
        allow_autostart: bool,
    ) -> None:
        super().__init__()
        self._ws = workspace
        self._allow_autostart = bool(allow_autostart)
        self._default = default or ""
        self._mounts: list[str] = list(workspace.mounts or [])
        self._env: dict[str, str] = dict(workspace.env or {})
        self._allowed_domains: list[str] = list(
            workspace.allowed_domains or []
        )
        # In-place editor state (#1778): when set, the next Add *replaces*
        # the item at this index/key instead of appending. Cleared on Add.
        self._editing_mount: int | None = None
        self._editing_env: str | None = None
        self._editing_allow: int | None = None
        # Image picker: include the workspace's current image even if it
        # isn't in the server's allowed list, pre-selected (untouched = no
        # change). Prompts are rich Text so bracket-laden names can't crash.
        cur = workspace.image or ""
        opts = list(allowed)
        if cur and cur not in opts:
            opts.append(cur)
        if opts:
            self._select_options = [(Text(i), i) for i in opts]
            self._select_value = (
                cur if cur in opts else (opts[0] if opts else None)
            )
        else:
            self._select_options = [(Text("(none)"), "(none)")]
            self._select_value = "(none)"

    def compose(self) -> ComposeResult:
        if self._select_value is not None:
            image_select = Select(
                self._select_options, value=self._select_value, id="image"
            )
        else:  # pragma: no cover
            image_select = Select(self._select_options, id="image")
        yield Header(show_clock=False)
        yield NonFocusableVerticalScroll(
            Static(Text(f"Edit workspace: {self._ws.name}"), classes="title"),
            Static("", id="edit_msg"),
            Horizontal(
                Static("Name"),
                Input(value=self._ws.name or "", id="name"),
                classes="field-row",
            ),
            Horizontal(Static("Image"), image_select, classes="field-row"),
            Static(
                "Mounts  (source:/container/path[:opts])",
                classes="editor-label",
            ),
            Horizontal(
                Input(
                    id="mount_input",
                    placeholder="/host/path:/container/path",
                ),
                Button("Add", id="add_mount"),
                Button("Remove", id="rm_mount"),
            ),
            OptionList(id="mount_list", classes="editor-list"),
            Static("Environment  (KEY=VALUE)", classes="editor-label"),
            Horizontal(
                Input(id="env_input", placeholder="KEY=VALUE"),
                Button("Add", id="add_env"),
                Button("Remove", id="rm_env"),
            ),
            OptionList(id="env_list", classes="editor-list"),
            Static(
                "Allowed Domains  (host or host:port; empty = unrestricted)",
                classes="editor-label",
            ),
            Horizontal(
                Input(id="allow_input", placeholder="github.com:443"),
                Button("Add", id="add_allow"),
                Button("Remove", id="rm_allow"),
            ),
            OptionList(id="allow_list", classes="editor-list"),
            Collapsible(
                Horizontal(
                    Static("Command"),
                    Input(value=self._ws.service_command or "", id="command"),
                    classes="field-row",
                ),
                Horizontal(
                    Static("Health"),
                    Input(
                        value=self._ws.health_check or "", id="health_check"
                    ),
                    classes="field-row",
                ),
                title="Advanced",
            ),
            Checkbox("Auto start", value=self._ws.auto_start, id="auto_start"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save", variant="primary"),
                classes="actions",
            ),
            id="edit_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()

    def _skip_editors_on_tab(self) -> None:
        """Editor buttons stay out of the Tab cycle (#1783).

        Add is reachable via Enter in the input; Remove via mouse click.
        Lists stay focusable for Delete/"e" keyboard actions but Tab skips
        them via :class:`TabSkipMixin`.
        """
        for wid in (
            "add_mount",
            "rm_mount",
            "add_env",
            "rm_env",
            "add_allow",
            "rm_allow",
        ):
            self.query_one(f"#{wid}").can_focus = False

    def _msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#edit_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- list editors: add / remove / in-place edit (#1778) ---

    def _render_mounts(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        ol.clear_options()
        if not self._mounts:
            ol.add_option(Option(Text("(no mounts)"), id="", disabled=True))
            return
        for i, m in enumerate(self._mounts):
            ol.add_option(Option(Text(m), id=f"m{i}"))

    def _add_mount(self) -> None:
        inp = self.query_one("#mount_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_mount_spec(v)
        if err:
            self._msg(err, error=True)
            return
        idx = self._editing_mount
        if idx is not None and 0 <= idx < len(self._mounts):
            self._mounts[idx] = v
            self._editing_mount = None
        else:
            self._mounts.append(v)
        inp.value = ""
        self._msg("")
        self._render_mounts()

    def _remove_mount(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._mounts):
            return
        del self._mounts[idx]
        self._editing_mount = None
        self._render_mounts()

    def _render_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        ol.clear_options()
        if not self._env:
            ol.add_option(Option(Text("(no env vars)"), id="", disabled=True))
            return
        for i, (k, val) in enumerate(self._env.items()):
            ol.add_option(Option(Text(f"{k}={val}"), id=f"e{i}"))

    def _add_env(self) -> None:
        inp = self.query_one("#env_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_env_entry(v)
        if err:
            self._msg(err, error=True)
            return
        key, _, value = v.partition("=")
        old = self._editing_env
        if old is not None:
            self._env.pop(old, None)
            self._editing_env = None
        self._env[key] = value
        inp.value = ""
        self._msg("")
        self._render_env()

    def _remove_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        del self._env[keys[idx]]
        self._editing_env = None
        self._render_env()

    def _render_allowed_domains(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        ol.clear_options()
        if not self._allowed_domains:
            ol.add_option(Option(Text("(unrestricted)"), id="", disabled=True))
            return
        for i, d in enumerate(self._allowed_domains):
            ol.add_option(Option(Text(d), id=f"a{i}"))

    def _add_allowed_domain(self) -> None:
        inp = self.query_one("#allow_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_allowed_domain_spec(v)
        if err:
            self._msg(err, error=True)
            return
        idx = self._editing_allow
        if idx is not None and 0 <= idx < len(self._allowed_domains):
            self._allowed_domains[idx] = v
            self._editing_allow = None
        elif v not in self._allowed_domains:
            self._allowed_domains.append(v)
        inp.value = ""
        self._msg("")
        self._render_allowed_domains()

    def _remove_allowed_domain(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._allowed_domains):
            return
        del self._allowed_domains[idx]
        self._editing_allow = None
        self._render_allowed_domains()

    # --- in-place edit: load the highlighted item into the input (#1778) ---

    def _edit_mount(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._mounts):
            return
        self._editing_mount = idx
        inp = self.query_one("#mount_input", Input)
        inp.value = self._mounts[idx]
        inp.focus()
        self._msg("Editing mount — press Add to update.")

    def _edit_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        key = keys[idx]
        self._editing_env = key
        inp = self.query_one("#env_input", Input)
        inp.value = f"{key}={self._env[key]}"
        inp.focus()
        self._msg("Editing env var — press Add to update.")

    def _edit_allowed_domain(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._allowed_domains):
            return
        self._editing_allow = idx
        inp = self.query_one("#allow_input", Input)
        inp.value = self._allowed_domains[idx]
        inp.focus()
        self._msg("Editing allowed-domain — press Add to update.")

    # --- keyboard remove/edit of the focused OptionList (#1778) ---

    def action_remove_item(self) -> None:
        fid = getattr(self.focused, "id", None) if self.focused else None
        if fid == "mount_list":
            self._remove_mount()
        elif fid == "env_list":
            self._remove_env()
        elif fid == "allow_list":
            self._remove_allowed_domain()

    def action_edit_item(self) -> None:
        fid = getattr(self.focused, "id", None) if self.focused else None
        if fid == "mount_list":
            self._edit_mount()
        elif fid == "env_list":
            self._edit_env()
        elif fid == "allow_list":
            self._edit_allowed_domain()

    # --- save ---

    def _save(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._msg("Name is required.", error=True)
            return
        sel = self.query_one("#image", Select)
        val = sel.value
        image = val if (val and val != "(none)") else None
        command = self.query_one("#command", Input).value.strip() or None
        health_check = (
            self.query_one("#health_check", Input).value.strip() or None
        )
        auto = (
            self._allow_autostart
            and self.query_one("#auto_start", Checkbox).value
        )
        mounts = list(self._mounts) or None
        env = dict(self._env) or None
        allowed_domains = list(self._allowed_domains) or None
        body = {
            "name": name,
            "image": image,
            "service_command": command,
            "health_check": health_check,
            "auto_start": auto,
            "mounts": mounts,
            "env": env,
            "allowed_domains": allowed_domains,
        }
        ws = self._ws
        orig_mounts = list(ws.mounts or []) or None
        orig_env = dict(ws.env or {}) or None
        orig_domains = list(ws.allowed_domains or []) or None
        restart_needed = bool(ws.running) and (
            (image or None) != (ws.image or None)
            or mounts != orig_mounts
            or env != orig_env
            or (command or None) != (ws.service_command or None)
            or allowed_domains != orig_domains
        )
        self.run_worker(
            self._do_save(name, body, ws, restart_needed),
            exit_on_error=False,
        )

    async def _do_save(self, name, body, ws, restart_needed) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.update_workspace, ws.id, **body
            )
        except AuthError:
            self._msg("Session expired — please log in again.", error=True)
            return
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text or str(exc)
            self._msg(f"Failed to save: {detail}", error=True)
            return
        except Exception as exc:
            self._msg(f"Failed to save: {exc}", error=True)
            return
        if restart_needed:

            def _after(restart: bool) -> None:
                if restart:
                    self.run_worker(
                        self._do_restart_after_save(ws.name, name),
                        exit_on_error=False,
                    )
                else:
                    self.dismiss(name)

            self.app.push_screen(
                ConfirmScreen(
                    "A running container is not affected by this edit. "
                    "Restart now to apply?",
                    yes_label="Restart",
                    yes_variant="warning",
                    no_label="Skip",
                ),
                _after,
            )
        else:
            self.dismiss(name)

    async def _do_restart_after_save(self, ws_name, dismiss_name) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, ws_name
            )
        except Exception as exc:
            self._msg(f"Saved, but restart failed: {exc}", error=True)
            return
        self.dismiss(dismiss_name)

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel":
            self.dismiss(False)
        elif bid == "save":
            self._save()
        elif bid == "add_mount":
            self._add_mount()
        elif bid == "rm_mount":
            self._remove_mount()
        elif bid == "add_env":
            self._add_env()
        elif bid == "rm_env":
            self._remove_env()
        elif bid == "add_allow":
            self._add_allowed_domain()
        elif bid == "rm_allow":
            self._remove_allowed_domain()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        eid = event.input.id
        if eid == "mount_input":
            self._add_mount()
        elif eid == "env_input":
            self._add_env()
        elif eid == "allow_input":
            self._add_allowed_domain()
        elif eid in ("name", "command", "health_check"):
            self._save()


class ServerSwitchScreen(Screen):
    """Pick a known server alias to switch to."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("e", "edit_server", "Edit"),
        ("d", "delete_server", "Delete"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("Switch server", classes="title"),
            Static("", id="switch_msg"),
            SpatialListView(id="server_options"),
            id="switch_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        lv = self.query_one("#server_options", ListView)
        lv.clear()
        servers = self.app.tui_state.known_servers()
        msg = self.query_one("#switch_msg", Static)
        if not servers:
            msg.update("No servers configured. Use 'a' to add one.")
            return
        msg.update("")
        current = self.app.tui_state.current_url()
        for s in servers:
            mark = "*" if s.url == current else " "
            label = Text(
                f"{mark} {s.alias}  ({s.url})",
                overflow="ellipsis",
                no_wrap=True,
            )
            item = ListItem(Label(label), name=s.url)
            item.server_alias = s.alias
            lv.append(item)

    def action_edit_server(self) -> None:
        lv = self.query_one("#server_options", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        alias = getattr(child, "server_alias", "") or ""
        url = child.name or ""
        if not alias:
            return

        def _on_edit(result: str | bool) -> None:
            if result == "url_changed":
                self.app.server_changed()
            elif result:
                self._populate()

        self.app.push_screen(EditServerScreen(alias=alias, url=url), _on_edit)

    def action_delete_server(self) -> None:
        lv = self.query_one("#server_options", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        url = child.name

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(
                    self._do_delete_and_refresh(url), exit_on_error=False
                )

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"), _on_confirm
        )

    async def _do_delete_and_refresh(self, url: str) -> None:
        await asyncio.to_thread(self.app.tui_state.delete_server, url)
        self._populate()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        url = getattr(event.item, "name", "") or ""
        if url:
            self.run_worker(self._do_switch_server(url), exit_on_error=False)
        else:
            self.app.server_changed()

    async def _do_switch_server(self, url: str) -> None:
        await asyncio.to_thread(self.app.tui_state.switch_server, url)
        self.app.server_changed()


# Retained for the login/auto-add path and pending #1763 (duplicate-alias
# handling); intentionally not surfaced as a MainScreen action yet.
class AddServerScreen(Screen):
    """Add a new server alias and switch to it."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("Add server", classes="title"),
            Input(placeholder="Alias (e.g. prod)", id="alias"),
            Input(
                placeholder="URL (https://host or /path/to.sock)",
                id="url",
            ),
            Button("Add and switch", id="add", variant="primary"),
            Static("", id="add_msg"),
            id="add_box",
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self._add()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("alias", "url"):
            self._add()

    def _add(self) -> None:
        alias = self.query_one("#alias", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        msg = self.query_one("#add_msg", Static)
        if not alias or not url:
            msg.update("[red]Alias and URL are required.[/red]")
            return
        if not is_valid_server_spec(url):
            msg.update(
                "[red]URL must be http(s)://host or an absolute socket"
                " path (/...).[/red]"
            )
            return
        self.run_worker(self._do_add_server(alias, url), exit_on_error=False)

    async def _do_add_server(self, alias: str, url: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.add_server, alias, url)
        except AliasConflictError:
            msg = self.query_one("#add_msg", Static)
            msg.update(
                f"[red]Alias '{alias}' already exists. Choose a"
                " different name or edit the existing entry.[/red]"
            )
            return
        self.app.server_changed()


class EditServerScreen(ModalScreen):
    """Edit an existing server alias and/or URL (#1762)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, alias: str, url: str) -> None:
        super().__init__()
        self._old_alias = alias
        self._old_url = url

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(f"Edit server: {self._old_alias}"), classes="title"),
            Horizontal(
                Static("Alias"),
                Input(value=self._old_alias, id="alias"),
                classes="field-row",
            ),
            Horizontal(
                Static("URL"),
                Input(value=self._old_url, id="url"),
                classes="field-row",
            ),
            Static("", id="edit_srv_msg"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save", variant="primary"),
                classes="actions",
            ),
            id="edit_srv_box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("alias", "url"):
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _save(self) -> None:
        alias = self.query_one("#alias", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        msg = self.query_one("#edit_srv_msg", Static)
        if not alias or not url:
            msg.update("[red]Alias and URL are required.[/red]")
            return
        if not is_valid_server_spec(url):
            msg.update(
                "[red]URL must be http(s)://host or an absolute socket"
                " path (/...).[/red]"
            )
            return
        self.run_worker(self._do_save(alias, url), exit_on_error=False)

    async def _do_save(self, alias: str, url: str) -> None:
        try:
            ok = await asyncio.to_thread(
                self.app.tui_state.update_server,
                self._old_alias,
                alias,
                url,
            )
        except Exception as exc:
            self.query_one("#edit_srv_msg", Static).update(
                Text(str(exc), style="red")
            )
            return
        if not ok:
            self.query_one("#edit_srv_msg", Static).update(
                "[red]Server not found.[/red]"
            )
            return
        # Signal whether server_changed() should follow (URL changed →
        # the server list and main screen need a full refresh).
        self.dismiss("url_changed" if url != self._old_url else True)
