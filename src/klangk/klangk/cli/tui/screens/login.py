"""Login screen: server picker + credential form."""

from __future__ import annotations

import asyncio
from functools import partial
from urllib.parse import urlparse

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from ..state import LoginError, PasswordExpiredError
from ...config import AliasConflictError, ConfigUnreadableError
from ...transport import is_valid_server_spec
from .base import (
    ButtonRowModalScreen,
    ConfirmScreen,
    ServerListView,
    SpatialNavScreen,
    StatusScreen,
    confirm_then,
)


def append_known_servers(lv, known, current) -> None:
    """Append one row per known server alias."""
    for s in known:
        lv.append(ListItem(Label(server_row_label(s, current)), name=s.url))


def first_provider_id(providers) -> str | None:
    """The first well-formed provider entry's id, or None.

    A malformed payload (non-dict entry, missing/non-string/empty id)
    degrades to None instead of a KeyError crash (#2029 audit). An
    empty string would dial /auth/oidc//login -> 404 (#2029 review).
    """
    return next(
        (
            p["id"]
            for p in providers
            if isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"]
        ),
        None,
    )


def server_row_label(s, current) -> Text:
    """One known-server row (the active server marked with *)."""
    mark = "*" if s.url == current else " "
    return Text(
        f"{mark} {s.alias}  ({s.url})",
        overflow="ellipsis",
        no_wrap=True,
    )


def uds_row_label(uds: str) -> Text:
    """The auto-detected local-klangkd UDS row."""
    return Text(
        f"  Local klangkd (UDS)  ({uds})",
        overflow="ellipsis",
        no_wrap=True,
    )


def should_offer_uds(uds, current, known_urls) -> bool:
    """Offer the default UDS only when no alias already covers it."""
    return bool(uds and uds != current and uds not in known_urls)


class ExpiredPasswordScreen(SpatialNavScreen, ButtonRowModalScreen):
    """Set-new-password dialog for an expired password (#3177).

    Pushed by the login screen when the server answers 403
    ``password_expired``. Reuses the credentials the user just typed
    (the current password is the ownership proof), asks for the
    replacement twice, and hands the result to
    ``tui_state.change_expired_password`` — success lands in
    ``login_succeeded`` like any login.
    """

    _BUTTONS = ["cancel", "rotate"]
    _INPUT = "confirm_password"
    SPATIAL_CHAIN = ["new_password", "confirm_password", "cancel", "rotate"]

    DEFAULT_CSS = """
    ExpiredPasswordScreen { align: center middle; }
    ExpiredPasswordScreen > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 0 2;
        border: round $primary;
        background: $panel;
    }
    ExpiredPasswordScreen Input {
        margin: 0 0 1 0;
    }
    ExpiredPasswordScreen Horizontal {
        align-horizontal: right;
        height: auto;
    }
    """

    def __init__(self, exc: PasswordExpiredError) -> None:
        super().__init__()
        self._exc = exc

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(self._exc.args[0] if self._exc.args else "")),
            Input(
                placeholder="New password",
                id="new_password",
                password=True,
            ),
            Input(
                placeholder="Confirm new password",
                id="confirm_password",
                password=True,
            ),
            Static("", id="expired_message"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Set new password", id="rotate", variant="primary"),
            ),
        )

    def on_mount(self) -> None:
        self.query_one("#new_password", Input).focus()

    def _set_message(self, text: str, *, error: bool = False) -> None:
        self.query_one("#expired_message", Static).update(
            Text(text, style="red" if error else "")
        )

    def _commit(self) -> None:
        """The rotate action (Enter in the confirm input or the button)."""
        new = self.query_one("#new_password", Input).value
        confirm = self.query_one("#confirm_password", Input).value
        if not new or new != confirm:
            self._set_message(
                "Enter the new password twice (entries must match).",
                error=True,
            )
            return
        self.run_worker(self._do_rotate(new), exit_on_error=False)

    async def _do_rotate(self, new_password: str) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.change_expired_password,
                self._exc.identifier,
                self._exc.password,
                new_password,
            )
        except LoginError as exc:
            self._set_message(f"Could not set password: {exc}", error=True)
            return
        self.dismiss(None)
        self.app.login_succeeded()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rotate":
            self._commit()
        else:
            self._dismiss_cancel()


class LoginScreen(SpatialNavScreen, StatusScreen):
    """Credential screen that also picks the server to log into.

    A fresh user with no server configured can pick a known alias, select
    the co-located default UDS, or type a URL (which is saved as a new
    alias) — then authenticate. Once a server is active the screen
    adapts to its auth mode: ``none`` → auto no-auth login; ``oidc`` →
    SSO hand-off (browser); ``password``/``both`` → email/handle +
    password form; ``unreachable`` → diagnostic.
    """

    # Server-scoped keys are hidden from the Footer — their hints render
    # inline on the server list header instead (#1890), matching the
    # workspace-detail / switch-server screens.
    BINDINGS = [
        Binding("d", "delete_server", "Delete server", show=False),
    ]  # spatial nav via SpatialNavScreen mixin
    SPATIAL_CHAIN = [
        "server_input",
        "use_server",
        "identifier",
        "password",
        "login",
    ]
    SPATIAL_UP_EXIT = "server_options"

    DEFAULT_CSS = """
    LoginScreen #server_header {
        height: auto;
        padding: 0;
        margin: 0;
    }
    LoginScreen #server_line {
        width: 1fr;
    }
    LoginScreen #server_hints {
        width: auto;
        text-align: right;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        # Header / status dock (StatusBar + Footer) come from StatusScreen
        # (#2689).
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        yield Vertical(
            Horizontal(
                Static("", id="server_line"),
                Static("[d] delete", id="server_hints", markup=False),
                id="server_header",
            ),
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

    def on_mount(self) -> None:
        self._populate_servers()
        if self.app.tui_state.current_url() is not None:
            self._setup_auth()
        else:
            self._show_no_server()

    def _show_no_server(self) -> None:
        self.query_one("#server_line", Static).update(
            "No server selected — pick one or enter a URL below."
        )
        self._set_oidc_visible(False)
        self._disable_credentials()

    # --- server picker ---

    def _populate_servers(self) -> None:
        lv = self.query_one("#server_options", ListView)
        lv.clear()
        current = self.app.tui_state.current_url()
        known = self.app.tui_state.known_servers()
        known_urls = {s.url for s in known}
        append_known_servers(lv, known, current)
        uds = self.app.tui_state.default_uds()
        # Only offer the auto-detected default UDS if no alias already covers
        # it (otherwise it would duplicate the persisted alias row).
        if should_offer_uds(uds, current, known_urls):
            lv.append(ListItem(Label(uds_row_label(uds)), name=uds))
        # Autofocus the first server entry (#1826).
        if lv.query(ListItem):
            lv.focus()
            # clear() above resets the index, so this is defensive against
            # a textual version that preserves it across repopulation.
            if lv.index is None:  # pragma: no branch
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
                except (AliasConflictError, ConfigUnreadableError) as exc:
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

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"),
            confirm_then(self, partial(self._do_delete_server, url)),
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
        # Hide the SSO button during the probe so it doesn't flash on then
        # off when the server turns out not to offer OIDC (#1864).
        self._set_oidc_visible(False)
        self.run_worker(self._setup_auth_async, exit_on_error=False)

    async def _setup_auth_async(self) -> None:
        state = self.app.tui_state
        mode = await asyncio.to_thread(state.auth_mode)
        self.query_one("#server_line", Static).update(
            f"Server: {state.current_url()}"
        )
        # The StatusBar's server segment tracks the same pick (#2689).
        self.app.refresh_status()
        self._enable_credentials()
        notice = self.query_one("#notice", Static)
        # The SSO button is only meaningful when the server offers OIDC.
        self._set_oidc_visible(mode in {"oidc", "both"})
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

    def _set_oidc_visible(self, visible: bool) -> None:
        # Show/hide the SSO button. Hidden entirely (not just disabled) when
        # the server doesn't offer OIDC, so it takes no layout space (#1864).
        self.query_one("#oidc", Button).display = visible

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
        except PasswordExpiredError as exc:
            self.app.push_screen(ExpiredPasswordScreen(exc))
            return
        except LoginError as exc:
            self._set_message(f"Login failed: {exc}", error=True)
            return
        self.app.login_succeeded()

    def _attempt_oidc(self) -> None:
        self.run_worker(self._do_login_oidc, exit_on_error=False)

    async def _do_login_oidc(self) -> None:
        providers = await asyncio.to_thread(self.app.tui_state.oidc_providers)
        provider_id = first_provider_id(providers)
        if provider_id is None:
            self._set_message("No SSO provider configured.", error=True)
            return
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
