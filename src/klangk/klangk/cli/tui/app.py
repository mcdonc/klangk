"""The klangk textual TUI app and entry point."""

from __future__ import annotations

from textual.app import App

from .screens import (
    AddServerScreen,
    LoginScreen,
    MainScreen,
    ServerSwitchScreen,
)
from .state import TuiState


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
        width: 96;
        max-width: 90%;
        padding: 0 2;
    }
    #switch_box, #add_box {
        width: 104;
        max-width: 90%;
        padding: 2 2;
    }
    #main {
        padding: 1 2;
        width: 1fr;
    }
    /* A little air under the server status line, before the picker. */
    #server_line {
        margin-bottom: 1;
    }
    /* Right-align button rows; don't let them expand vertically (avoids a
    large gap below the button before the next field). */
    .actions {
        align-horizontal: right;
        height: auto;
    }
    /* Underline-style entry fields: invisible top border + no side borders,
    keeping the default height so the text stays vertically centered on the
    middle row, with only the bottom border showing as an underline. */
    Input, Input:focus {
        border-top: blank;
        border-left: none;
        border-right: none;
    }
    /* Give the server picker a visible top/bottom rule (no side borders) so
    its width matches the fields without inset side bars. */
    OptionList {
        border: tall $border-blurred;
        border-left: none;
        border-right: none;
    }
    /* Compact buttons: drop the min-width so they hug their labels instead
    of padding out to a fixed width. */
    Button {
        min-width: 0;
        padding: 0 1;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, state: TuiState) -> None:
        super().__init__()
        self.tui_state = state
        # Latest live-status annotation shown in the status bar.
        self.live_extra = ""

    def on_mount(self) -> None:
        self.title = "Klangk"
        if self.tui_state.is_authenticated():
            self.push_screen(MainScreen())
        else:
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
