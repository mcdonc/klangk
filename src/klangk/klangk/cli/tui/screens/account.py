"""Account self-service screen: password / handle / email changes."""

from __future__ import annotations

import asyncio

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Static,
)

from ... import account as _account_mod
from ..state import LoginError
from ._base import (
    ConfirmScreen,
    NonFocusableVerticalScroll,
    SpatialNavScreen,
)


class AccountScreen(SpatialNavScreen):
    """Account self-service: change password / handle / email (#1753).

    Mirrors the Flutter ``SettingsPage`` — current @handle + email from
    ``GET /auth/me``, the same client-side validation (handle charset, email
    format, password minimum from ``/api/v1/config``), and a confirm dialog
    for the handle change (it affects the terminal home directory and how
    others see you in chat).
    """

    BINDINGS = [
        Binding("up", "spatial_up", show=False),
        Binding("down", "spatial_down", show=False),
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    # Focus chain for spatial Up/Down (reading order).
    SPATIAL_CHAIN = [
        "pw_current",
        "pw_new",
        "pw_confirm",
        "pw_submit",
        "handle_new",
        "handle_pw",
        "handle_submit",
        "email_new",
        "email_pw",
        "email_submit",
    ]

    DEFAULT_CSS = """
    AccountScreen { align: center top; }
    #account_box {
        width: 96;
        max-width: 90%;
        height: auto;
        padding: 0 2;
    }
    #account_scroll { padding: 0 0 1 0; }
    #profile { padding: 1 0; text-style: bold; color: $text-secondary; }
    .acct-section {
        height: auto;
        padding: 1 0;
        border-top: solid $border-blurred;
    }
    .acct-title {
        text-style: bold;
        color: $primary;
        padding: 0 0 1 0;
    }
    .acct-msg {
        height: 1;
        color: $text-muted;
    }
    .acct-msg.error { color: $error; }
    .acct-actions { align-horizontal: right; height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with NonFocusableVerticalScroll(id="account_scroll"):
            yield Vertical(
                Static("", id="profile"),
                # --- Change password ---
                Vertical(
                    Static("Change password", classes="acct-title"),
                    Input(
                        placeholder="Current password",
                        id="pw_current",
                        password=True,
                    ),
                    Input(
                        placeholder="New password",
                        id="pw_new",
                        password=True,
                    ),
                    Input(
                        placeholder="Confirm new password",
                        id="pw_confirm",
                        password=True,
                    ),
                    Static("", id="pw_msg", classes="acct-msg"),
                    Horizontal(
                        Button("Update password", id="pw_submit"),
                        classes="acct-actions",
                    ),
                    classes="acct-section",
                ),
                # --- Change handle ---
                Vertical(
                    Static("Change handle", classes="acct-title"),
                    Input(placeholder="New handle", id="handle_new"),
                    Input(
                        placeholder="Password (to confirm)",
                        id="handle_pw",
                        password=True,
                    ),
                    Static("", id="handle_msg", classes="acct-msg"),
                    Horizontal(
                        Button("Update handle", id="handle_submit"),
                        classes="acct-actions",
                    ),
                    classes="acct-section",
                ),
                # --- Change email ---
                Vertical(
                    Static("Change email", classes="acct-title"),
                    Input(placeholder="New email", id="email_new"),
                    Input(
                        placeholder="Password (to confirm)",
                        id="email_pw",
                        password=True,
                    ),
                    Static("", id="email_msg", classes="acct-msg"),
                    Horizontal(
                        Button("Update email", id="email_submit"),
                        classes="acct-actions",
                    ),
                    classes="acct-section",
                ),
                id="account_box",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.app.title = "Klangk: Account"
        self._current_handle = ""
        self._current_email = ""
        self.run_worker(self._load_profile, exit_on_error=False)

    async def _load_profile(self) -> None:
        try:
            me = await asyncio.to_thread(self.app.tui_state.get_me)
        except LoginError as exc:
            self.query_one("#profile", Static).update(
                Text(str(exc), style="red")
            )
            return
        self._current_handle = me.get("handle") or ""
        self._current_email = me.get("email") or ""
        self._render_profile()

    def _render_profile(self) -> None:
        self.query_one("#profile", Static).update(
            Text(f"{self._current_email}  @{self._current_handle}")
        )

    def _set_msg(
        self, section: str, text: str, *, error: bool = False
    ) -> None:
        msg = self.query_one(f"#{section}_msg", Static)
        msg.update(Text(text, style="red" if error else ""))
        msg.set_class(error, "error")

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "pw_submit": self._submit_password,
            "handle_submit": self._submit_handle,
            "email_submit": self._submit_email,
        }
        action = actions.get(event.button.id)
        if action:
            action()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in a section's last field submits that section.
        routes = {
            "pw_confirm": self._submit_password,
            "handle_pw": self._submit_handle,
            "email_pw": self._submit_email,
        }
        action = routes.get(event.input.id)
        if action:
            action()

    # --- password ---

    def _submit_password(self) -> None:
        current = self.query_one("#pw_current", Input).value
        new = self.query_one("#pw_new", Input).value
        confirm = self.query_one("#pw_confirm", Input).value
        if not current or not new:
            self._set_msg(
                "pw", "Current and new password are required.", error=True
            )
            return
        if new != confirm:
            self._set_msg("pw", "Passwords do not match.", error=True)
            return
        # The minimum-length check needs /api/v1/config (a network call),
        # so it runs in the worker rather than blocking the event loop (#1869).
        self.run_worker(
            self._do_change_password(current, new), exit_on_error=False
        )

    async def _do_change_password(self, current: str, new: str) -> None:
        url = self.app.tui_state.current_url() or ""
        min_len = await asyncio.to_thread(
            _account_mod.password_min_length, url
        )
        if len(new) < min_len:
            self._set_msg(
                "pw",
                f"Password must be at least {min_len} characters.",
                error=True,
            )
            return
        try:
            await asyncio.to_thread(
                self.app.tui_state.change_password, current, new
            )
        except LoginError as exc:
            self._set_msg("pw", str(exc), error=True)
            return
        for wid in ("pw_current", "pw_new", "pw_confirm"):
            self.query_one(f"#{wid}", Input).value = ""
        self._set_msg("pw", "Password updated.")

    # --- handle ---

    def _submit_handle(self) -> None:
        new = self.query_one("#handle_new", Input).value.strip()
        pw = self.query_one("#handle_pw", Input).value
        err = _account_mod.validate_handle(new)
        if err:
            self._set_msg("handle", err, error=True)
            return
        if not pw:
            self._set_msg("handle", "Password is required.", error=True)
            return
        # Don't capture the password in the closure — re-read it in the
        # callback so it isn't held while the confirm dialog is open, and
        # clear the field if the user cancels (#1869).
        self.app.push_screen(
            ConfirmScreen(
                f"Change your handle from @{self._current_handle} to @{new}?",
                yes_label="Change",
                yes_variant="warning",
            ),
            lambda confirmed: self._on_handle_confirmed(confirmed, new),
        )

    def _on_handle_confirmed(self, confirmed: bool, new: str) -> None:
        if not confirmed:
            self.query_one("#handle_pw", Input).value = ""
            return
        password = self.query_one("#handle_pw", Input).value
        self.run_worker(
            self._do_change_handle(new, password), exit_on_error=False
        )

    async def _do_change_handle(self, new: str, password: str) -> None:
        try:
            accepted = await asyncio.to_thread(
                self.app.tui_state.change_handle, new, password
            )
        except LoginError as exc:
            self._set_msg("handle", str(exc), error=True)
            return
        self._current_handle = accepted or new
        self.query_one("#handle_new", Input).value = ""
        self.query_one("#handle_pw", Input).value = ""
        self._set_msg("handle", f"Handle updated to @{self._current_handle}.")
        self._render_profile()

    # --- email ---

    def _submit_email(self) -> None:
        new = self.query_one("#email_new", Input).value.strip()
        pw = self.query_one("#email_pw", Input).value
        err = _account_mod.validate_email(new)
        if err:
            self._set_msg("email", err, error=True)
            return
        if not pw:
            self._set_msg("email", "Password is required.", error=True)
            return
        self.run_worker(self._do_change_email(new, pw), exit_on_error=False)

    async def _do_change_email(self, new: str, password: str) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.change_email, new, password
            )
        except LoginError as exc:
            self._set_msg("email", str(exc), error=True)
            return
        self._current_email = new
        self.query_one("#email_new", Input).value = ""
        self.query_one("#email_pw", Input).value = ""
        self._set_msg("email", "Email updated. Check your inbox to verify it.")
        self._render_profile()
