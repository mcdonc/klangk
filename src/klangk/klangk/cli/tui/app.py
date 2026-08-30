"""The klangk textual TUI app and entry point."""

from __future__ import annotations

import asyncio

from textual.app import App
from textual.screen import Screen
from textual.theme import Theme

from .screens import (
    AddServerScreen,
    CreateWorkspaceScreen,
    EditWorkspaceScreen,
    LoginScreen,
    MainScreen,
    ServerDownScreen,
    ServerSwitchScreen,
    SessionExpiredScreen,
    StatusScreen,
    WorkspaceDetailScreen,
)
from .state import TuiState

# ---------------------------------------------------------------------------
# Theme.
#
# The custom `klangk` theme echoes the Flutter web UI's GitHub-dark-inspired
# palette (src/frontend/lib/theme/colors.dart) and is the app DEFAULT, keeping
# the TUI visually consistent with the web UI. Textual's built-in themes stay
# available for users who prefer them: `ansi-light`/`ansi-dark` restrict
# themselves to the terminal's 16 ANSI colors, so they respect the user's
# actual terminal palette / shell theme and render correctly on a
# light-background terminal (#1904); flipping the default back to `klangk`
# just changes which one is selected out of the box (#2003).
#
# klangk theme tokens:
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
    /* Shared status-row chrome (#2689): every full-page screen composes a
    bottom #status_dock holding the StatusBar directly above the Footer.
    Two bottom-docked siblings fully overlap in Textual (same edge row,
    last-mounted paints on top), which left the StatusBar hidden under
    the Footer since #1875 — the container docks once; the children flow
    inside. These rules live in the App CSS, not StatusScreen's
    DEFAULT_CSS, because Textual scopes DEFAULT_CSS to the defining
    class's type name and that scope only follows a screen's FIRST base
    chain — on LoginScreen(SpatialNavScreen, StatusScreen) and the
    TabSkipMixin forms the scoped rules never matched, so the dock grew
    to 1fr and squeezed the login form's server list to one row. */
    #status_dock {
        dock: bottom;
        height: auto;
    }
    #status_dock StatusBar,
    #status_dock Footer {
        dock: none;
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
    /* A checkbox in a field-row keeps its content-sized (auto) width —
    without this the row's fixed label column bleeds onto it (#2721). */
    .field-row > Checkbox {
        width: auto;
    }
    /* Editor rows (Input + Add/Remove buttons): keep the Input fractional so
    the buttons always fit inside the pane (#1891) — a greedy default-width
    Input otherwise pushes Add/Remove past the tab pane's clip region, where
    they render but can't be clicked. */
    #mount_input, #env_input, #allow_input {
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
        # App-level StatusBar state (#2689): ``live_extra`` carries the
        # live segment (host notices, the #2661 schedule countdown,
        # reachability flags) written by the MainScreen's status WS
        # handler; ``last_login`` is the formatted stamp fetched once by
        # MainScreen's last-login worker. Both live here — not on any one
        # screen — so every StatusScreen renders the same status row.
        self.live_extra = ""
        self.last_login: str | None = None
        self._expiring = False
        # App-wide server-down overlay (#2012). ``_server_down_screen`` is the
        # shown modal (None when hidden); ``_server_down_dismissed`` is set when
        # the user closes it so the reconnect loop won't re-pop it until the
        # backend is reachable again (``clear_server_down`` resets it).
        self._server_down_screen: ServerDownScreen | None = None
        self._server_down_dismissed = False
        # App-wide session-expired overlay (#2025). ``_session_expired_screen``
        # is the shown modal (None when hidden); ``_expiring`` guards against
        # re-entry while it's up and during the redirect to login.
        self._session_expired_screen: SessionExpiredScreen | None = None
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

    def _sync_chrome(self) -> None:
        """After any navigation: mirror the active screen in the window
        title (#1778) and refresh every StatusBar (#2689).

        Textual fires no re-mount on pop-back, so the App owns both syncs —
        the pattern ``_sync_title`` established. Refreshing on every
        push/pop keeps the status line populated on the newly-shown screen
        without per-screen refresh work.
        """
        self._sync_title()
        self.refresh_status()

    def refresh_status(self) -> None:
        """Refresh the StatusBar on every mounted full-page screen (#2689).

        ``live_extra`` / ``last_login`` are App-level state updated by the
        MainScreen's status WS handler and last-login worker (MainScreen
        stays mounted underneath every screen pushed above it). Refreshing
        every mounted StatusScreen — rather than only the top one — keeps
        the line live on whichever screen is current and leaves lower
        screens fresh for pop-back.
        """
        for screen in self.screen_stack:
            if isinstance(screen, StatusScreen):
                screen.refresh_status_bar()

    def push_screen(self, screen, *args, **kwargs):
        result = super().push_screen(screen, *args, **kwargs)
        self.call_after_refresh(self._sync_chrome)
        return result

    def pop_screen(self):
        result = super().pop_screen()
        self.call_after_refresh(self._sync_chrome)
        return result

    # --- navigation hooks used by screens ---

    def login_succeeded(self) -> None:
        self.pop_screen()  # LoginScreen
        self.push_screen(MainScreen())

    def do_logout(self) -> None:
        async def _logout() -> None:
            await asyncio.to_thread(self.tui_state.logout)
            # Logout clears the token; drop the parked status-WS listener
            # so the loop wakes, sees no token, and exits instead of
            # lingering on the old server's connection — a parked WS would
            # keep firing events (toasts, live_extra) after logout, and a
            # later re-login would spawn a second concurrent status WS
            # (#2704). Sequence: drop AFTER logout (so the loop exits via
            # the no-token branch rather than re-dialing), BEFORE the pop
            # (so the screen is still in the stack if the loop checks).
            main = next(
                (s for s in self.screen_stack if isinstance(s, MainScreen)),
                None,
            )
            if main is not None:
                main.drop_status_connection()
            self.pop_screen()  # MainScreen
            self.live_extra = ""
            self.last_login = None
            self.push_screen(LoginScreen())

        self.run_worker(_logout, exit_on_error=False)

    def _pop_above(self, target: Screen) -> bool:
        """Pop every screen above ``target`` (leaving it on top), safely.

        Replacement for the ``while top is not target: self.pop_screen()``
        idiom (#2034). It computes the screens to remove from a snapshot up
        front and pops exactly that fixed set, so the teardown is bounded by
        the snapshot rather than re-evaluated against the live stack every
        iteration.

        The ``ScreenStackError`` safety comes from two places, neither of
        which is the in-loop check: (1) the early ``target not in stack``
        return — the old loop kept popping because textual's implicit base
        screen is never the ``MainScreen`` it was searching for, then raised
        trying to pop that base; and (2) ``target`` itself is never in the
        snapshot, so it is never popped and the stack never empties past it.

        Returns ``True`` if ``target`` was in the stack (and is now on top),
        ``False`` if it was absent (nothing was popped).
        """
        if target not in self.screen_stack:
            return False
        to_remove: list[Screen] = []
        # The membership guard above means the reversed walk always hits
        # target (identity compare), so the loop can never exhaust: the
        # arc to the pop loop without a break is unreachable.
        for screen in reversed(self.screen_stack):  # pragma: no branch
            if screen is target:
                break
            to_remove.append(screen)
        for screen in to_remove:
            # Defensive: ``pop_screen`` is synchronous, so today the live top
            # always equals the next planned screen and the loop ends by
            # exhausting ``to_remove``. The check keeps it correct if the
            # stack is ever changed between iterations (e.g. a re-entrant
            # pop) — stop rather than pop a screen we didn't plan to, and
            # never index an empty stack.
            if not self.screen_stack or self.screen_stack[-1] is not screen:
                break
            self.pop_screen()
        return bool(self.screen_stack) and self.screen_stack[-1] is target

    def server_changed(self) -> None:
        """Pop back to the MainScreen and refresh it after a server change."""
        main = next(
            (s for s in self.screen_stack if isinstance(s, MainScreen)), None
        )
        if main is None:
            # No MainScreen is reachable. This is reachable, not just
            # defensive: the server-switch / add-server workers run
            # fire-and-forget and are NOT cancelled when their screen is
            # popped, so one can resume after a concurrent session-expiry
            # teardown has already removed the MainScreen. Clear down to the
            # base and push a fresh MainScreen — pushing on top of the
            # current stack would strand the login screen underneath it and
            # corrupt the next logout (#2034).
            # Textual keeps >=1 screen mounted; an empty stack here is
            # the defensive teardown-race case (#2034), not a state a
            # running app reaches.
            if self.screen_stack:  # pragma: no branch
                self._pop_above(self.screen_stack[0])
            self.push_screen(MainScreen())
            return
        self._pop_above(main)
        # The status-WS loop is parked inside its listener against the old
        # server — without this drop it stays there until that server drops
        # the connection itself, so reachability and live updates keep
        # tracking the previous server (#2704). Also restarts the loop if it
        # had given up reconnecting: the give-up overlay promises "switch
        # server … to reconnect".
        main.drop_status_connection()
        main.ensure_status_ws_worker()
        main.refresh_lists()
        # The reused screen still shows the previous server's last-login
        # stamp; drop it and re-fetch for the new identity (#2583).
        main.reload_last_login()

    def server_changed_needs_login(self) -> None:
        """Switch server then show LoginScreen (invalid/missing creds)."""
        # Same WS teardown as ``server_changed`` (#2704): the popped
        # MainScreen's loop exits via its stack guard only *between*
        # connections, so without this drop the old server's WS would stay
        # open until the server closed it. No restart here — the login flow
        # pushes a fresh MainScreen that mounts its own workers.
        main = next(
            (s for s in self.screen_stack if isinstance(s, MainScreen)), None
        )
        if main is not None:
            main.drop_status_connection()
        # Tear down every screen above the base, then push login. The
        # ``target not in stack`` early return in ``_pop_above`` is what
        # prevents the ScreenStackError the old pop-until-MainScreen loop hit
        # when MainScreen wasn't in the stack (#2034).
        if self.screen_stack:  # pragma: no branch
            self._pop_above(self.screen_stack[0])
        self.push_screen(LoginScreen())

    def session_expired(self) -> None:
        """Show the app-wide session-expired overlay when the access token is
        irrecoverably dead (#2025).

        Replaces the old easy-to-miss inline label + fleeting toast with a
        prominent centered overlay (mirroring ``ServerDownScreen``). Pushed at
        the app level, so it covers whatever screen is current — workspaces
        list, workspace detail, create/edit form — giving one uniform signal
        on every page.

        Re-entrancy-safe: the status loop, token-refresh loop, and workspace
        loads can all detect auth failure near-simultaneously. ``_expiring``
        is set synchronously before the overlay is pushed, so only the first
        call shows it; the rest bail out.
        """
        if self._expiring or isinstance(self.screen, LoginScreen):
            return
        self._expiring = True
        self._session_expired_screen = SessionExpiredScreen()
        self.push_screen(self._session_expired_screen)

    def confirm_session_expired(self) -> None:
        """User acknowledged the expiry overlay — log out and go to login.

        Called by :class:`SessionExpiredScreen` (button / ``Enter`` / ``Esc``).
        Dismisses the overlay, then runs the logout + redirect in a worker so
        credential I/O doesn't block the UI thread.
        """
        screen = self._session_expired_screen
        self._session_expired_screen = None
        if screen is not None and screen in self.screen_stack:
            screen.dismiss(None)

        async def _expire() -> None:
            try:
                await asyncio.to_thread(self.tui_state.logout)
                # Same parked-listener teardown as ``do_logout`` (#2704):
                # logout cleared the token, so the dropped loop exits via
                # its no-token branch instead of re-dialing the old server.
                main = next(
                    (
                        s
                        for s in self.screen_stack
                        if isinstance(s, MainScreen)
                    ),
                    None,
                )
                if main is not None:
                    main.drop_status_connection()
                # Clear every screen above the base, then show login.
                # ``_pop_above`` pops a fixed snapshot, so the teardown is
                # bounded regardless of what a concurrent worker does between
                # this call and the push (#2034).
                if self.screen_stack:  # pragma: no branch
                    self._pop_above(self.screen_stack[0])
                self.live_extra = ""
                self.last_login = None
                self.push_screen(LoginScreen())
            finally:
                self._expiring = False

        self.run_worker(_expire, exit_on_error=False)

    def refresh_workspaces(self) -> None:
        """Refresh the workspace list on the MainScreen (if present)."""
        for screen in reversed(self.screen_stack):
            if isinstance(screen, MainScreen):
                screen.refresh_lists()
                return

    # --- app-wide server-down overlay (#2012) ---

    def set_server_down(self, message: str) -> None:
        """Show (or update) the global server-down overlay over the active page.

        Pushed at the app level, so it covers whatever screen is current —
        workspaces list, workspace detail, create/edit form, … — giving one
        uniform signal on every page. A no-op if the user already dismissed
        it for this outage.
        """
        if self._server_down_dismissed:
            return
        if self._server_down_screen is None:
            self._server_down_screen = ServerDownScreen(message)
            self.push_screen(self._server_down_screen)
        else:
            self._server_down_screen.set_message(message)

    def clear_server_down(self) -> None:
        """Hide the overlay once the backend is reachable again."""
        self._server_down_dismissed = False
        screen = self._server_down_screen
        self._server_down_screen = None
        if screen is not None and screen in self.screen_stack:
            screen.dismiss(None)

    def dismiss_server_down(self) -> None:
        """User closed the overlay — don't re-show until recovery."""
        self._server_down_dismissed = True
        self._server_down_screen = None

    def server_down_switch_server(self) -> None:
        """ "c" from the overlay: close it and open the server-switch screen."""
        self._server_down_dismissed = True
        screen = self._server_down_screen
        self._server_down_screen = None
        if screen is not None and screen in self.screen_stack:
            screen.dismiss(None)
        self.push_screen(ServerSwitchScreen())


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
    "StatusScreen",
    "TuiState",
    "run_tui",
]
