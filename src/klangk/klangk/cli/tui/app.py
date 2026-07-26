"""The klangk textual TUI app and entry point."""

from __future__ import annotations

import asyncio

from textual.app import App
from textual.theme import Theme

from .screens import (
    AddServerScreen,
    CreateWorkspaceScreen,
    EditWorkspaceScreen,
    LoginScreen,
    MainScreen,
    ServerSwitchScreen,
    WorkspaceDetailScreen,
)
from .state import TuiState

# ---------------------------------------------------------------------------
# Klangk theme — echoes the Flutter web UI's GitHub-dark-inspired palette
# (src/frontend/lib/theme/colors.dart).
#
# Tokens:
#   primary   — green (#238636): primary actions (login, create, start)
#   secondary — blue (#58A6FF): links, focus, informational
#   accent    — yellow (#F5C518): brand highlight
#   warning   — amber (#D29922): caution actions (restart, stop)
#   error     — red (#F85149): destructive actions (delete), errors
#   success   — green (#238636): healthy / running indicators
#   background — dark canvas (#0D1117)
#   surface   — card/panel surface (#161B22)
#   panel     — overlay panels (#1C2128)
# ---------------------------------------------------------------------------
KLANGK_THEME = Theme(
    name="klangk",
    primary="#238636",
    secondary="#58A6FF",
    accent="#F5C518",
    warning="#D29922",
    error="#F85149",
    success="#238636",
    background="#0D1117",
    surface="#161B22",
    panel="#1C2128",
    dark=True,
)


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
    #login_box, #switch_box, #add_box, #detail_box, #dup_box {
        width: 96;
        max-width: 90%;
        padding: 0 2;
    }
    /* Create/edit forms are scrollable: they fill the viewport and scroll
    when content overflows (mouse wheel, Page Up/Down). (#1783) */
    #create_box, #edit_box {
        width: 96;
        max-width: 90%;
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }
    /* Login-screen headers: the server status line and the notice line
    below it. Formerly #server_line had a 1-row bottom margin that left a
    blank row between it and the server picker — dropped so the list hugs
    the status line (#1865). Emphasized with the theme accent + bold so
    they read as headers. CSS (not markup) is used because "Server: {url}"
    carries a dynamic URL that must not be markup-parsed. */
    #server_line, #notice {
        color: $accent;
        text-style: bold;
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
    /* The workspace-list filter input is a borderless single-line field
    (height 1), not an underlined form input. App CSS outranks widget
    DEFAULT_CSS by origin, so this override must live here: without it the
    global `Input { border-top: blank }` above gives the field a 1-row top
    border and — at height:1 — zero rows for its text, so the field renders
    nothing even though filtering works (#1764). */
    #filter_input, #filter_input:focus {
        border: none;
    }
    /* Match the Select dropdown to the underline-style inputs: drop the
    side borders (the "shadows") so it aligns cleanly with adjacent fields. */
    SelectCurrent, Select:focus > SelectCurrent {
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
    /* Compact form rows: label + field side-by-side (#1783). */
    .field-row {
        height: auto;
    }
    /* Ensure all form Horizontals fit their children (#1783). */
    #create_box Horizontal, #edit_box Horizontal {
        height: auto;
    }
    .field-row > Static {
        width: 14;
        padding: 0 1 0 0;
        content-align: right middle;
        color: $text-muted;
    }
    .field-row > Input, .field-row > Select {
        width: 1fr;
    }
    /* Bounded list editors (#1783). */
    .editor-list {
        height: 4;
        max-height: 6;
    }
    /* Section labels for each editor group (#1783). */
    .editor-label {
        color: $text-muted;
        text-style: bold;
        margin-top: 1;
    }
    Collapsible {
        height: auto;
        padding: 0;
        margin-bottom: 1;
    }
    /* Match the collapsed Collapsible header to the 3-row input height (#1783). */
    Collapsible.-collapsed {
        height: 3;
        border-top: none;
        padding: 0;
        margin-top: 1;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, state: TuiState) -> None:
        super().__init__()
        self.tui_state = state
        self.live_extra = ""
        self._expiring = False
        self.register_theme(KLANGK_THEME)
        self.theme = "klangk"

    def on_mount(self) -> None:
        self.title = "Klangk"
        if self.tui_state.is_authenticated():
            self.push_screen(MainScreen())
        else:
            self.push_screen(LoginScreen())

    def _sync_title(self) -> None:
        """Mirror the active screen in the window title (#1778).

        "Klangk: workspace <name>" on the detail/edit screen, "Klangk:
        Workspaces" on the list. Run after every push/pop (via the overrides
        below) so returning from detail/edit to the list resets it too —
        textual fires no show hook on pop-back, so the App owns the sync.
        Other screens (login, server switch, …) leave the title unchanged.
        """
        screen = self.screen
        if isinstance(screen, WorkspaceDetailScreen):
            self.title = f"Klangk: workspace {screen._name}"
        elif isinstance(screen, EditWorkspaceScreen):
            self.title = f"Klangk: workspace {screen._ws.name}"
        elif isinstance(screen, MainScreen):
            self.title = "Klangk: Workspaces"

    def push_screen(self, screen, *args, **kwargs):
        result = super().push_screen(screen, *args, **kwargs)
        self.call_after_refresh(self._sync_title)
        return result

    def pop_screen(self):
        result = super().pop_screen()
        self.call_after_refresh(self._sync_title)
        return result

    # --- navigation hooks used by screens ---

    def login_succeeded(self) -> None:
        self.pop_screen()  # LoginScreen
        self.push_screen(MainScreen())

    def do_logout(self) -> None:
        async def _logout() -> None:
            await asyncio.to_thread(self.tui_state.logout)
            self.pop_screen()  # MainScreen
            self.live_extra = ""
            self.push_screen(LoginScreen())

        self.run_worker(_logout, exit_on_error=False)

    def server_changed(self) -> None:
        """Pop back to the MainScreen and refresh it after a server change."""
        while self.screen_stack and not isinstance(
            self.screen_stack[-1], MainScreen
        ):
            self.pop_screen()
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.refresh_lists()

    def server_changed_needs_login(self) -> None:
        """Switch server then show LoginScreen (invalid/missing creds)."""
        while self.screen_stack and not isinstance(
            self.screen_stack[-1], MainScreen
        ):
            self.pop_screen()
        if self.screen_stack and isinstance(self.screen_stack[-1], MainScreen):
            self.pop_screen()
        self.push_screen(LoginScreen())

    def session_expired(self) -> None:
        """Redirect to login when the access token is irrecoverably dead.

        Re-entrancy-safe: the status loop, token-refresh loop, and
        workspace loads can all detect auth failure near-simultaneously.
        ``_expiring`` is set synchronously before the worker spawns, so
        only the first call runs the redirect; the rest bail out.
        """
        if self._expiring or isinstance(self.screen, LoginScreen):
            return
        self._expiring = True

        async def _expire() -> None:
            try:
                await asyncio.to_thread(self.tui_state.logout)
                while len(self.screen_stack) > 1:
                    self.pop_screen()
                self.live_extra = ""
                self.push_screen(LoginScreen())
                self.notify(
                    "Session expired — please log in again.",
                    severity="warning",
                    timeout=8,
                )
            finally:
                self._expiring = False

        self.run_worker(_expire, exit_on_error=False)

    def refresh_workspaces(self) -> None:
        """Refresh the workspace list on the MainScreen (if present)."""
        for screen in reversed(self.screen_stack):
            if isinstance(screen, MainScreen):
                screen.refresh_lists()
                return


def run_tui(server_url: str | None = None) -> None:
    """Launch the interactive TUI (called only in an interactive terminal)."""
    KlangkApp(TuiState(server_url)).run()


# Re-export for convenience / tests.
__all__ = [
    "AddServerScreen",
    "CreateWorkspaceScreen",
    "EditWorkspaceScreen",
    "KlangkApp",
    "LoginScreen",
    "MainScreen",
    "ServerSwitchScreen",
    "TuiState",
    "run_tui",
]
