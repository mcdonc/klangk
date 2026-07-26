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
    TabbedContent,
    TabPane,
    Tabs,
)

from ... import account as _account_mod
from ..state import LoginError
from ._base import (
    ConfirmScreen,
    SpatialNavScreen,
)


class AccountScreen(SpatialNavScreen):
    """Account self-service: change password / handle / email (#1753).

    The three credential forms live in a :class:`TabbedContent` — one tab
    per concern (Password / Handle / Email) — with the ``#profile`` header
    pinned above the tabs so the current ``@handle`` / email stays visible
    regardless of which tab is active (#1898). Mirrors the Flutter
    ``SettingsPage`` semantics: same client-side validation (handle charset,
    email format, password minimum from ``/api/v1/config``) and a confirm
    dialog for the handle change (it affects the terminal home directory and
    how others see you in chat).

    Spatial navigation (#1781): Up/Down walks the active tab's field chain;
    Up from the first field returns focus to the tab strip, Left/Right on
    the strip switches tabs (native :class:`Tabs` binding), and Down from
    the strip enters the active tab — no focus traps.
    """

    BINDINGS = [
        Binding("up", "spatial_up", show=False),
        Binding("down", "spatial_down", show=False),
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    # Per-tab focus chains (reading order), keyed by TabPane id. The active
    # tab's chain is exposed via the :attr:`SPATIAL_CHAIN` property so the
    # inherited spatial Up/Down handler walks only the visible tab (#1898).
    _TAB_CHAINS: dict[str, list[str]] = {
        "pw_pane": ["pw_current", "pw_new", "pw_confirm", "pw_submit"],
        "handle_pane": ["handle_new", "handle_pw", "handle_submit"],
        "email_pane": ["email_new", "email_pw", "email_submit"],
    }

    DEFAULT_CSS = """
    AccountScreen { align: center top; }
    #account_box {
        width: 96;
        max-width: 90%;
        height: auto;
        padding: 0 2;
    }
    #profile { padding: 1 0; text-style: bold; color: $text-secondary; }
    #acct_tabs { height: auto; }
    AccountScreen TabPane { padding: 1 0; height: auto; }
    .acct-section { height: auto; }
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

    @property
    def SPATIAL_CHAIN(self) -> list[str]:
        """Fields of the active tab, in reading order (#1898)."""
        return self._TAB_CHAINS.get(self._active_tab_id(), [])

    def _active_tab_id(self) -> str:
        return self.query_one("#acct_tabs", TabbedContent).active or ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        # #account_box is a direct child of the screen (no
        # VerticalScroll wrapper): a VerticalScroll's own up/down scroll
        # bindings would sit between the focused Input and the screen's
        # spatial_up/spatial_down and silently swallow arrow keys
        # (#1898). The tabbed layout is short enough to need no scroll.
        with Vertical(id="account_box"):
            # Profile header is pinned ABOVE the tabs so the current
            # @handle / email stays visible regardless of the active
            # tab (#1898).
            yield Static("", id="profile")
            with TabbedContent(id="acct_tabs"):
                with TabPane("Password", id="pw_pane"):
                    yield Vertical(
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
                    )
                with TabPane("Handle", id="handle_pane"):
                    yield Vertical(
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
                    )
                with TabPane("Email", id="email_pane"):
                    yield Vertical(
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
                    )
        yield Footer()

    # --- spatial navigation across tabs (#1898) ---

    def action_spatial_up(self) -> None:
        """Up within a tab walks its chain; from the first field, Up
        returns focus to the tab strip so Left/Right can switch tabs
        (#1781, #1898).
        """
        chain = self.SPATIAL_CHAIN
        fid = getattr(self.focused, "id", None) if self.focused else None
        if not fid or fid not in chain:
            return
        pos = chain.index(fid)
        if pos > 0:
            self.query_one(f"#{chain[pos - 1]}").focus()
        else:
            # Top of a tab → return focus to the tab strip.
            self.query_one(Tabs).focus()

    def on_key(self, event) -> None:
        """Down from the tab strip enters the first field of the active
        tab. Left/Right on the strip switch tabs natively (handled by
        :class:`Tabs`' own bindings)."""
        if event.key == "down" and isinstance(self.focused, Tabs):
            chain = self.SPATIAL_CHAIN
            if chain:
                event.stop()
                self.query_one(f"#{chain[0]}").focus()

    def on_tabbed_content_tab_activated(self, event) -> None:
        """Focus the first field of the newly-active tab (#1792, #1898).

        Mirrors the workspace-list screen: switching tabs drops you into
        the pane's content rather than stranding focus on the strip.
        """
        chain = self.SPATIAL_CHAIN
        if chain:
            self.query_one(f"#{chain[0]}").focus()

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
