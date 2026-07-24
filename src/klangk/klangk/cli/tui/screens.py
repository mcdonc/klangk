"""Screens for the klangk TUI: login, main shell, server switch/add.

Navigation between screens is driven by methods on ``KlangkApp``
(``login_succeeded`` / ``do_logout`` / ``server_changed``); screens stay
free of cross-screen coupling and reach state through ``self.app.tui_state``.
"""

from __future__ import annotations

from urllib.parse import urlparse

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from .state import LoginError
from ..transport import is_valid_server_spec
from .widgets import StatusBar
from .ws import listen_for_status


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

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.message),
            Horizontal(
                Button("Cancel", id="no"),
                Button("Delete", id="yes", variant="error"),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class LoginScreen(Screen):
    """Credential screen that also picks the server to log into.

    A fresh user with no server configured can pick a known alias, select
    the co-located default UDS, or type a URL (which is saved as a new
    alias) — then authenticate. Once a server is active the screen
    adapts to its auth mode: ``none`` → auto no-auth login; ``oidc`` →
    SSO hand-off (browser); ``password``/``both`` → email/handle +
    password form; ``unreachable`` → diagnostic.
    """

    BINDINGS = [("d", "delete_server", "Delete server")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("", id="server_line"),
            OptionList(id="server_options"),
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
            "No server selected. Pick one above or enter a URL,"
            " then press 'Use server'."
        )
        self._disable_credentials()

    # --- server picker ---

    def _populate_servers(self) -> None:
        ol = self.query_one("#server_options", OptionList)
        ol.clear_options()
        current = self.app.tui_state.current_url()
        known = self.app.tui_state.known_servers()
        known_urls = {s.url for s in known}
        for s in known:
            mark = "*" if s.url == current else " "
            ol.add_option(Option(f"{mark} {s.alias}  ({s.url})", id=s.url))
        uds = self.app.tui_state.default_uds()
        # Only offer the auto-detected default UDS if no alias already covers
        # it (otherwise it would duplicate the persisted alias row).
        if uds and uds != current and uds not in known_urls:
            ol.add_option(Option(f"  Local klangkd (UDS)  ({uds})", id=uds))

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
        cfg = self.app.tui_state.cfg()
        if raw in cfg.servers:
            # Known alias — switch to its URL.
            self.app.tui_state.switch_server(cfg.servers[raw].url)
        elif is_valid_server_spec(raw):
            # A new server (URL or UDS path) — save it as an alias so it can
            # be re-selected later.
            self.app.tui_state.add_server(self._derive_alias(raw), raw)
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
        ol = self.query_one("#server_options", OptionList)
        idx = ol.highlighted
        if idx is None:
            self._set_message("Select a server to delete.", error=True)
            return
        url = ol.get_option_at_index(idx).id

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            if self.app.tui_state.delete_server(url):
                self._set_message("Server deleted.")
            else:
                self._set_message("Not a saved alias.", error=True)
            self._populate_servers()
            if self.app.tui_state.current_url() is None:
                self._show_no_server()
            else:
                self._setup_auth()

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"), _on_confirm
        )

    # --- auth-mode setup ---

    def _setup_auth(self) -> None:
        state = self.app.tui_state
        mode = state.auth_mode()
        self.query_one("#server_line", Static).update(
            f"Server: {state.current_url()}"
        )
        self._enable_credentials()
        notice = self.query_one("#notice", Static)
        if mode == "none":
            notice.update("No-auth server — logging in…")
            # Defer the (possibly screen-pushing) login so we don't push
            # during this screen's own mount.
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
        rendered = f"[red]{text}[/red]" if error else text
        self.query_one("#message", Static).update(rendered)

    # --- login arms ---

    def _attempt_none(self) -> None:
        try:
            self.app.tui_state.login_none()
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
        try:
            self.app.tui_state.login_password(identifier, password)
        except LoginError as exc:
            self._set_message(f"Login failed: {exc}", error=True)
            return
        self.app.login_succeeded()

    def _attempt_oidc(self) -> None:
        providers = self.app.tui_state.oidc_providers()
        if not providers:
            self._set_message("No SSO provider configured.", error=True)
            return
        provider_id = providers[0]["id"]
        try:
            self.app.tui_state.oidc_login(provider_id)
        except LoginError as exc:
            self._set_message(f"SSO failed: {exc}", error=True)
            return
        if self.app.tui_state.is_authenticated():
            self.app.login_succeeded()
        else:
            self._set_message("SSO did not complete.")

    # --- event handlers ---

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self._choose_server(event.option.id)

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


class MainScreen(Screen):
    """The TUI home: a two-page workspace list (owned / shared) + status bar,
    with a live WS feed. Selecting a workspace opens its detail screen."""

    BINDINGS = [
        ("s", "switch_server", "Switch server"),
        ("l", "logout", "Logout"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="ws_tabs"):
            yield TabPane("Owned by me", OptionList(id="owned_list"))
            yield TabPane("Shared to me", OptionList(id="shared_list"))
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_lists()
        if self.app.tui_state.is_authenticated():
            self.app.run_worker(self._status_loop, name="status-ws")

    def action_switch_server(self) -> None:
        self.app.push_screen(ServerSwitchScreen())

    def action_logout(self) -> None:
        self.app.do_logout()

    # --- list population ---

    def refresh_lists(self) -> None:
        self._populate("#owned_list", self._safe_list(owned=True))
        self._populate("#shared_list", self._safe_list(owned=False))
        self._refresh_status()

    def _safe_list(self, *, owned: bool) -> list:
        state = self.app.tui_state
        try:
            return (
                state.list_owned_workspaces()
                if owned
                else state.list_shared_workspaces()
            )
        except Exception:
            return []

    @staticmethod
    def _fmt(ws) -> str:
        mark = ">" if ws.running else "."
        health = f" ({ws.health})" if ws.health else ""
        return f"{mark} {ws.name}{health}"

    def _populate(self, selector: str, workspaces: list) -> None:
        ol = self.query_one(selector, OptionList)
        ol.clear_options()
        if not workspaces:
            ol.add_option(Option("(no workspaces)", id="", disabled=True))
            return
        for ws in workspaces:
            ol.add_option(Option(self._fmt(ws), id=ws.name))

    def _refresh_status(self) -> None:
        state = self.app.tui_state
        self.query_one("#status", StatusBar).set_state(
            server=state.current_url(),
            user=state.email() or "(unknown)",
            extra=self.app.live_extra,
        )

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        name = event.option.id
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
            self.app.live_extra = "status: disconnected"
            self._refresh_status()

    def _on_status_event(self, event: dict) -> None:
        etype = event.get("type", "event")
        self.app.live_extra = f"live: {etype}"
        self._refresh_status()
        if etype == "workspaces_changed":
            self.refresh_lists()


class WorkspaceDetailScreen(Screen):
    """Read-only workspace detail + restart / duplicate / delete actions."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "restart", "Restart"),
        ("d", "duplicate", "Duplicate"),
        ("x", "delete", "Delete"),
    ]

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self._ws = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("", id="detail_title"),
            Static("", id="detail_body"),
            Static("", id="detail_msg"),
            id="detail_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        try:
            self._ws = self.app.tui_state.find_workspace(self._name)
        except Exception:
            self._ws = None
        self._display()

    def _display(self) -> None:
        self.query_one("#detail_title", Static).update(
            f"Workspace: {self._name}"
        )
        ws = self._ws
        body = self.query_one("#detail_body", Static)
        if ws is None:
            body.update("Could not load workspace.")
            return
        lines = [
            f"running: {'yes' if ws.running else 'no'}",
            f"health: {ws.health or '-'}",
        ]
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
        if ws.owner_email:
            lines.append(f"owner: {ws.owner_email}")
        body.update("\n".join(lines))

    def _msg(self, text: str, *, error: bool = False) -> None:
        rendered = f"[red]{text}[/red]" if error else text
        self.query_one("#detail_msg", Static).update(rendered)

    # --- actions ---

    def action_restart(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.app.tui_state.restart_workspace(self._name)
            except Exception as exc:
                self._msg(f"Restart failed: {exc}", error=True)
                return
            self._msg("Restart requested.")
            self.app.refresh_workspaces()

        self.app.push_screen(
            ConfirmScreen(
                f"Restart '{self._name}'? This ends active terminal"
                " sessions and recreates the container."
            ),
            _on_confirm,
        )

    def action_delete(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.app.tui_state.delete_workspace(self._name)
            except Exception as exc:
                self._msg(f"Delete failed: {exc}", error=True)
                return
            self.app.pop_screen()  # back to the list
            self.app.refresh_workspaces()

        self.app.push_screen(
            ConfirmScreen(
                f"Delete '{self._name}'? This permanently deletes the"
                " workspace and its container."
            ),
            _on_confirm,
        )

    def action_duplicate(self) -> None:
        self.app.push_screen(DuplicateScreen(self._name), self._on_duplicate)

    def _on_duplicate(self, new_name: str | None) -> None:
        if not new_name:
            return
        try:
            self.app.tui_state.duplicate_workspace(self._name, new_name)
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
            Static(f"Duplicate '{self._source}' as:"),
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


class ServerSwitchScreen(Screen):
    """Pick a known server alias to switch to."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("d", "delete_server", "Delete"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("Switch server", classes="title"),
            Static("", id="switch_msg"),
            OptionList(id="server_options"),
            id="switch_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        ol = self.query_one("#server_options", OptionList)
        ol.clear_options()
        servers = self.app.tui_state.known_servers()
        msg = self.query_one("#switch_msg", Static)
        if not servers:
            msg.update("No servers configured. Use 'a' to add one.")
            return
        msg.update("")
        current = self.app.tui_state.current_url()
        for s in servers:
            mark = "*" if s.url == current else " "
            ol.add_option(Option(f"{mark} {s.alias}  ({s.url})", id=s.url))

    def action_delete_server(self) -> None:
        ol = self.query_one("#server_options", OptionList)
        idx = ol.highlighted
        if idx is None:
            return
        url = ol.get_option_at_index(idx).id

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.app.tui_state.delete_server(url)
            self._populate()

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"), _on_confirm
        )

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        url = event.option.id
        if url:
            self.app.tui_state.switch_server(url)
        self.app.server_changed()


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
        self.app.tui_state.add_server(alias, url)
        self.app.server_changed()
