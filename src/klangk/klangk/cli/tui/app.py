"""The klangk textual TUI app and entry point."""

from __future__ import annotations

from textual.app import App

from .screens import (
    AddServerScreen,
    LoginScreen,
    MainScreen,
    ServerSwitchScreen,
)
from .state import LoginError, TuiState


class KlangkApp(App):
    """Interactive TUI over the existing klangk client."""

    CSS = """
    .title {
        text-style: bold;
        color: $primary;
        padding: 1 0;
    }
    Screen {
        align: center top;
    }
    #login_box {
        width: 64;
        max-width: 90%;
        padding: 1 2;
    }
    #switch_box, #add_box {
        width: 70;
        max-width: 90%;
        padding: 1 2;
    }
    #main {
        padding: 1 2;
        width: 1fr;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, state: TuiState) -> None:
        super().__init__()
        self.tui_state = state
        # Latest live-status annotation shown in the status bar.
        self.live_extra = ""

    def on_mount(self) -> None:
        if self.tui_state.is_authenticated():
            self.push_screen(MainScreen())
            return
        # No-auth servers log in for free (#1374); do it before pushing any
        # screen so we land directly on the main shell instead of flashing a
        # login screen whose on_mount would have to push mid-mount.
        if self.tui_state.auth_mode() == "none":
            try:
                self.tui_state.login_none()
            except LoginError:
                pass
            if self.tui_state.is_authenticated():
                self.push_screen(MainScreen())
                return
        self.push_screen(LoginScreen())

    # --- navigation hooks used by screens ---

    def login_succeeded(self) -> None:
        self.pop_screen()  # LoginScreen
        self.push_screen(MainScreen())

    def do_logout(self) -> None:
        self.tui_state.logout()
        self.pop_screen()  # MainScreen
        self.live_extra = ""
        self.push_screen(LoginScreen())

    def server_changed(self) -> None:
        """Pop back to the MainScreen and refresh it after a server change."""
        while self.screen_stack and not isinstance(
            self.screen_stack[-1], MainScreen
        ):
            self.pop_screen()
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.refresh_view()


def run_tui(server_url: str | None = None) -> None:
    """Launch the interactive TUI (called only in an interactive terminal)."""
    KlangkApp(TuiState(server_url)).run()


# Re-export for convenience / tests.
__all__ = [
    "AddServerScreen",
    "KlangkApp",
    "LoginScreen",
    "MainScreen",
    "ServerSwitchScreen",
    "TuiState",
    "run_tui",
]
