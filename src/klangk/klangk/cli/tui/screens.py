"""Screens for the klangk TUI: login, main shell, server switch/add.

Navigation between screens is driven by methods on ``KlangkApp``
(``login_succeeded`` / ``do_logout`` / ``server_changed``); screens stay
free of cross-screen coupling and reach state through ``self.app.tui_state``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

from .state import LoginError
from .widgets import Sidebar, StatusBar
from .ws import listen_for_status


class LoginScreen(Screen):
    """Credential screen that adapts to the server's auth mode.

    ``none`` → auto no-auth login; ``oidc`` → SSO hand-off (browser);
    ``password``/``both`` → email/handle + password form; ``unreachable``
    → diagnostic message. The password form and SSO button are
    ``disabled`` (not hidden) for non-applicable modes so the screen
    stays simple to render and test.
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("klangk", classes="title"),
            Static("", id="notice"),
            Input(placeholder="Email or handle", id="identifier"),
            Input(placeholder="Password", id="password", password=True),
            Button("Log in", id="login", variant="primary"),
            Button("Log in via browser (SSO)", id="oidc"),
            Static("", id="message"),
            id="login_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        state = self.app.tui_state
        mode = state.auth_mode()
        notice = self.query_one("#notice", Static)
        if mode == "none":
            # Reached only if the app-level no-auth auto-login failed.
            notice.update("No-auth login failed — check the server.")
            self._disable_form()
            return
        if mode == "unreachable":
            notice.update(
                "Cannot reach the server. Check the URL or that klangkd"
                " is running."
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

    def _disable_form(self) -> None:
        self.query_one("#identifier", Input).disabled = True
        self.query_one("#password", Input).disabled = True
        self.query_one("#login", Button).disabled = True

    def _set_message(self, text: str, *, error: bool = False) -> None:
        rendered = f"[red]{text}[/red]" if error else text
        self.query_one("#message", Static).update(rendered)

    # --- login arms ---

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login":
            self._attempt_password()
        elif event.button.id == "oidc":
            self._attempt_oidc()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("identifier", "password"):
            self._attempt_password()


class MainScreen(Screen):
    """The app shell: sidebar + content + status bar, with a live WS feed."""

    BINDINGS = [
        ("s", "switch_server", "Switch server"),
        ("a", "add_server", "Add server"),
        ("l", "logout", "Logout"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Horizontal(
            Sidebar(id="sidebar"),
            Vertical(
                Static("klangk", classes="title"),
                Static("", id="content"),
                id="main",
            ),
        )
        yield StatusBar(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_view()
        if self.app.tui_state.is_authenticated():
            self.app.run_worker(self._status_loop, name="status-ws")

    def action_switch_server(self) -> None:
        self.app.push_screen(ServerSwitchScreen())

    def action_add_server(self) -> None:
        self.app.push_screen(AddServerScreen())

    def action_logout(self) -> None:
        self.app.do_logout()

    def refresh_view(self) -> None:
        state = self.app.tui_state
        self.query_one("#sidebar", Sidebar).set_items(
            [
                "klangk",
                "",
                "[s] switch server",
                "[a] add server",
                "[l] logout",
                "[q] quit",
            ]
        )
        server = state.current_url()
        user = state.email() or "(unknown)"
        self.query_one("#status", StatusBar).set_state(
            server=server, user=user, extra=self.app.live_extra
        )
        body = (
            f"Server: {server or '(none)'}\n"
            f"User: {user}\n\n"
            "Live workspace/container status is streaming. "
            "Workspace screens arrive in later issues (#1747+)."
        )
        self.query_one("#content", Static).update(body)

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
            self.refresh_view()

    def _on_status_event(self, event: dict) -> None:
        etype = event.get("type", "event")
        self.app.live_extra = f"live: {etype}"
        self.refresh_view()


class ServerSwitchScreen(Screen):
    """Pick a known server alias to switch to."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

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
        servers = self.app.tui_state.known_servers()
        if not servers:
            self.query_one("#switch_msg", Static).update(
                "No servers configured. Use 'a' to add one."
            )
            return
        current = self.app.tui_state.current_url()
        ol = self.query_one("#server_options", OptionList)
        for s in servers:
            mark = "*" if s.url == current else " "
            ol.add_option(Option(f"{mark} {s.alias}  ({s.url})", id=s.url))

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
        self.app.tui_state.add_server(alias, url)
        self.app.server_changed()
