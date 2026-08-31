"""Shared base classes and small utility screens for the TUI."""

from __future__ import annotations

import asyncio
from typing import TypeVar

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.dom import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    ProgressBar,
    Static,
    Tabs,
)


_ScreenResult = TypeVar("_ScreenResult")


class StatusBar(Static):
    """One-line bottom bar: current server, user, and live-state flag."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def set_state(
        self,
        *,
        server: str | None,
        user: str | None,
        extra: str = "",
        last_login: str | None = None,
    ) -> None:
        # The live `extra` segment (host notices, the #2661 schedule
        # countdown) renders FIRST when set: it is the time-sensitive
        # bit, and appending it last let it fall off the right edge of
        # a typical terminal once server/user/last-login (~76 cols)
        # had claimed the row — an invisible countdown on the very
        # screens that need it.
        text = ""
        if extra:
            text += f"{extra}"
        text += (
            f"{'   |   ' if text else ''}server: {server or '(none)'}"
            f"   |   user: {user or '(not logged in)'}"
        )
        if last_login:
            text += f"   |   last login: {last_login}"
        # Render literally — server URL / user / live `extra` may contain
        # bracket characters that would otherwise be parsed as markup.
        self.update(Text(text))


class ButtonRowModalScreen(ModalScreen[_ScreenResult]):
    """Spatial arrow navigation for a modal dialog with an optional input
    field above a horizontal row of buttons (#2016).

    Left/Right move between sibling buttons in reading order; Down from
    the input enters the first button; Up from a button returns to the
    input. Arrows are always sufficient to reach every option — Tab /
    Shift-Tab remains a fallback (no focus traps, per AGENTS.md "TUI
    spatial navigation (no focus traps)").
    """

    # Button ids left-to-right (reading order); optionally the id of an
    # input field sitting above the row.
    _BUTTONS: list[str] = []
    _INPUT: str | None = None

    BINDINGS = [
        Binding("left", "btn_left", show=False),
        Binding("right", "btn_right", show=False),
        Binding("up", "btn_up", show=False),
        Binding("down", "btn_down", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def _focus_id(self, widget_id: str) -> None:
        self.query_one(f"#{widget_id}").focus()

    def _focused_id(self) -> str | None:
        focused = self.focused
        return focused.id if focused is not None else None

    def _btn_step(self, step: int) -> None:
        fid = self._focused_id()
        if fid in self._BUTTONS:
            pos = self._BUTTONS.index(fid)
            nxt = pos + step
            if 0 <= nxt < len(self._BUTTONS):
                self._focus_id(self._BUTTONS[nxt])

    def action_btn_left(self) -> None:
        self._btn_step(-1)

    def action_btn_right(self) -> None:
        self._btn_step(1)

    def action_btn_up(self) -> None:
        if self._focused_id() in self._BUTTONS and self._INPUT:
            self._focus_id(self._INPUT)

    def action_btn_down(self) -> None:
        if self._focused_id() == self._INPUT and self._BUTTONS:
            self._focus_id(self._BUTTONS[0])

    def action_cancel(self) -> None:
        """Escape cancels the dialog (#2016)."""
        self._dismiss_cancel()

    def _dismiss_cancel(self) -> None:
        """Dismiss as Cancel. Subclasses override to return their cancel
        value (False for ConfirmScreen; None for Input/Duplicate)."""
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """OK commits, anything else cancels. Subclasses with other
        button sets override (ConfirmScreen dismisses by which button
        it was)."""
        if event.button.id == "ok":
            self._commit()
        elif event.button.id == "cancel":
            self._dismiss_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the input field is an OK."""
        if event.input.id == self._INPUT:
            self._commit()


def confirm_then(screen, work):
    """A ConfirmScreen callback that runs *work* on *screen*'s worker when
    confirmed (the shared "confirm, then run the action" shape).

    *work* is a zero-arg coroutine factory (a bound method, or
    ``functools.partial``), evaluated only on confirm.
    """

    def _on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        screen.run_worker(work(), exit_on_error=False)

    return _on_confirm


class ConfirmScreen(ButtonRowModalScreen[bool]):
    """A yes/no confirmation dialog. Dismisses with True on confirm.

    Arrows move between Cancel / confirm (Left/Right) — no Tab needed
    (#2016). Escape dismisses as Cancel (False).
    """

    _BUTTONS = ["no", "yes"]

    def _dismiss_cancel(self) -> None:
        self.dismiss(False)

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
        yes_label: str = "Del",
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


class StatusScreen(Screen):
    """Full-page screen with the shared status-row chrome (#2689).

    Composes ``Header`` + :meth:`compose_body` + a bottom ``#status_dock``
    holding the ``StatusBar`` (``#status``) directly above the ``Footer`` —
    the chrome ``MainScreen`` always had — so the ``server / user / last
    login / live extra`` line is constant across navigation instead of
    disappearing the moment you drill into a workspace.

    The bar renders App-level state (``app.live_extra``, ``app.last_login``)
    via :meth:`refresh_status_bar`. Freshness is driven from the App:
    ``KlangkApp.refresh_status`` re-renders every mounted ``StatusScreen``
    after each push/pop (the ``_sync_chrome`` pattern — screens don't re-run
    on pop-back) and whenever a status WS event updates ``live_extra`` on
    the MainScreen underneath. Subclasses only implement
    :meth:`compose_body`; an ``on_show`` override needs no ``super()``
    chaining because the base defines none.
    """

    # NOTE: the #status_dock chrome (dock: bottom, height: auto, and the
    # StatusBar/Footer `dock: none` override) lives in KlangkApp.CSS, not
    # here — Textual scopes DEFAULT_CSS to the defining class's type name,
    # and that scope only follows a screen's FIRST base chain. On
    # LoginScreen(SpatialNavScreen, StatusScreen) and the TabSkipMixin
    # forms these rules never matched: the dock grew to 1fr and squeezed
    # the screen body (the login server list collapsed to one row). App
    # CSS is unscoped and outranks every DEFAULT_CSS.

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield from self.compose_body()
        with Vertical(id="status_dock"):
            yield StatusBar(id="status")
            yield Footer()

    def compose_body(self) -> ComposeResult:
        """The screen's content between the Header and the status dock."""
        yield from ()

    def refresh_status_bar(self) -> None:
        """Render App-level status state into this screen's ``#status``."""
        try:
            self.query_one("#status", StatusBar).set_state(
                server=self.app.tui_state.current_url(),
                # None email (login screen) renders as "(not logged in)".
                user=self.app.tui_state.email(),
                extra=self.app.live_extra,
                last_login=self.app.last_login,
            )
        except NoMatches:
            pass  # Widget not mounted yet; refreshed on next nav/mount.


class ServerDownScreen(ModalScreen[None]):
    """Dimmed overlay shown app-wide when the backend is unreachable (#2012).

    Mirrors the Flutter disconnected overlay (``workspace_overlays.dart``):
    a centered panel over a dimmed background with a live reconnect status.
    Shown by ``KlangkApp.set_server_down`` over whatever page is active, so a
    drop is signalled uniformly on every screen, not just the workspaces
    page. ``Esc`` dismisses it for the current outage (the underlying page
    keeps its own inline indicator); ``c`` jumps to switch-server.
    """

    DEFAULT_CSS = """
    ServerDownScreen { align: center middle; }
    ServerDownScreen > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $panel;
    }
    ServerDownScreen Static {
        text-align: center;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_overlay", "Back", show=False),
        Binding("c", "switch_server", "Switch server", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(Static(Text(self._message), id="server_down_msg"))

    def set_message(self, message: str) -> None:
        """Update the live reconnect status without re-mounting."""
        self._message = message
        try:
            self.query_one("#server_down_msg", Static).update(Text(message))
        except NoMatches:  # pragma: no cover - not yet mounted
            pass

    def action_dismiss_overlay(self) -> None:
        # Tell the app not to re-show until recovery, then pop self.
        self.app.dismiss_server_down()
        self.dismiss(None)

    def action_switch_server(self) -> None:
        self.app.server_down_switch_server()


class SessionExpiredScreen(ModalScreen[None]):
    """Dimmed overlay shown app-wide when the session has expired (#2025).

    Mirrors :class:`ServerDownScreen`: a centered panel over a dimmed
    background. The access token is irrecoverably dead, so the only action
    is to re-login — ``Enter`` / ``Esc`` / the button all dismiss the overlay
    and redirect to the login screen. Shown by ``KlangkApp.session_expired``
    over whatever page is active (workspaces list, workspace detail,
    create/edit form), so an expired session is signalled uniformly on every
    screen instead of a missable one-line inline label + a fleeting toast.
    """

    DEFAULT_CSS = """
    SessionExpiredScreen { align: center middle; }
    SessionExpiredScreen > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $panel;
    }
    SessionExpiredScreen Static {
        text-align: center;
    }
    SessionExpiredScreen Horizontal {
        align-horizontal: center;
        height: auto;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "proceed", "Log in again", show=False),
        Binding("enter", "proceed", "Log in again", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                Text(
                    "Session expired\n\n"
                    "Your access token is no longer valid.\n"
                    "Please log in again."
                ),
                id="session_expired_msg",
            ),
            Horizontal(
                Button("Log in again", id="proceed", variant="primary"),
            ),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proceed":
            self.action_proceed()

    def on_mount(self) -> None:
        # Focus the action button so Enter confirms immediately.
        self.query_one("#proceed", Button).focus()

    def action_proceed(self) -> None:
        self.app.confirm_session_expired()


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


class WorkspaceListView(SpatialListView):
    """Workspace list — Up from the first row returns to the tab strip."""

    SPATIAL_UP_TARGET = Tabs


class DuplicateScreen(ButtonRowModalScreen):
    """Prompt for a new name to duplicate a workspace under.

    Down from the name input enters the button row; Left/Right move
    between Cancel / Dup (#2016).
    """

    _BUTTONS = ["cancel", "ok"]
    _INPUT = "dup_name"

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
            Static(Text(f"Dup '{self._source}' as:")),
            Input(value=f"{self._source}-copy", id="dup_name"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Dup", id="ok", variant="primary"),
            ),
            id="dup_box",
        )

    def _commit(self) -> None:
        name = self.query_one("#dup_name", Input).value.strip()
        self.dismiss(name or None)


def _human_bytes(n: float) -> str:
    """Format a byte count as e.g. ``"12.3 MB"``."""
    value = float(n)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _fmt_transfer(done: float, total: float | None) -> str:
    """Render a progress counter, tolerating an unknown total."""
    d = _human_bytes(done)
    return f"{d} / {_human_bytes(total)}" if total else f"{d} (size unknown)"


class InputScreen(ButtonRowModalScreen):
    """Generic single-line input prompt (title + default + OK/Cancel).

    Dismisses with the trimmed value on OK, or ``None`` on cancel (#1758).
    Down from the input enters the button row; Left/Right move between
    Cancel / OK (#2016).
    """

    _BUTTONS = ["cancel", "ok"]
    _INPUT = "inp_value"

    DEFAULT_CSS = """
    InputScreen { align: center middle; }
    InputScreen > Vertical {
        width: 64; max-width: 90%; padding: 0 2;
        border: round $primary; background: $panel;
    }
    InputScreen Horizontal {
        align-horizontal: right; height: auto; padding-top: 1;
    }
    """

    def __init__(
        self,
        title: str,
        default: str = "",
        ok_label: str = "OK",
        select_on_focus: bool = True,
    ) -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._ok_label = ok_label
        # When False the field doesn't select-all on focus; typing then
        # appends to the default (what rename wants) instead of replacing
        # it (#2020).
        self._select_on_focus = select_on_focus

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(self._title)),
            Input(
                value=self._default,
                id="inp_value",
                select_on_focus=self._select_on_focus,
            ),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button(self._ok_label, id="ok", variant="primary"),
            ),
            id="inp_box",
        )

    def on_mount(self) -> None:
        inp = self.query_one("#inp_value", Input)
        inp.focus()
        # Without select-on-focus, park the cursor at the end so typing
        # appends to the default (rename) rather than prepending (#2020).
        if not self._select_on_focus:
            inp.cursor_position = len(inp.value)

    def _commit(self) -> None:
        val = self.query_one("#inp_value", Input).value.strip()
        self.dismiss(val or None)


class CheatsheetScreen(ModalScreen):
    """A ``?`` keyboard cheatsheet modal (#1802).

    Lists the active screen's keybindings grouped by context. Dismissed
    with Escape or ``?`` (pressed again). Construct with ``sections`` — a
    list of ``(group_title, [(display_key, description), ...])``; each
    screen supplies its own (see ``MainScreen._cheatsheet_sections`` /
    ``WorkspaceDetailScreen._cheatsheet_sections``) so the content adapts
    to the current screen.
    """

    DEFAULT_CSS = """
    CheatsheetScreen { align: center middle; }
    CheatsheetScreen > Vertical {
        width: 72;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $panel;
    }
    CheatsheetScreen #cs_title { text-style: bold; margin-bottom: 1; }
    CheatsheetScreen .cs_group {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    CheatsheetScreen .cs_row { height: 1; }
    CheatsheetScreen .cs_key {
        width: 12;
        min-width: 12;
        color: $text-muted;
        text-style: bold;
    }
    CheatsheetScreen .cs_desc { width: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("?", "dismiss_modal", "Close", show=False),
    ]

    def __init__(
        self, sections: list[tuple[str, list[tuple[str, str]]]]
    ) -> None:
        super().__init__()
        self._sections = sections

    def compose(self) -> ComposeResult:
        children: list = [Static(Text("Keyboard shortcuts"), id="cs_title")]
        for title, items in self._sections:
            children.append(Static(Text(title), classes="cs_group"))
            for key, desc in items:
                children.append(
                    Horizontal(
                        Static(Text(key), classes="cs_key"),
                        Static(Text(desc), classes="cs_desc"),
                        classes="cs_row",
                    )
                )
        yield Vertical(*children, id="cs_box")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class TransferScreen(ModalScreen):
    """Runs a blocking transfer (export/import) in a worker thread while
    showing a live progress bar (#1758).

    ``make_call`` is invoked once as ``make_call(on_progress)`` inside
    ``asyncio.to_thread``; ``on_progress`` is ``on_progress(done_bytes,
    total_bytes_or_None)``. The screen dismisses with ``(True, success_msg)``
    on completion or ``(False, error_text)`` on failure.
    """

    DEFAULT_CSS = """
    TransferScreen { align: center middle; }
    TransferScreen > Vertical {
        width: 64; max-width: 90%; padding: 1 2;
        border: round $primary; background: $panel;
    }
    TransferScreen #xfer_title { text-style: bold; margin-bottom: 1; }
    TransferScreen #xfer_status { color: $text-muted; margin-top: 1; }
    """

    def __init__(
        self,
        title: str,
        make_call,
        success_msg: str,
    ) -> None:
        super().__init__()
        self._title = title
        self._make_call = make_call
        self._success_msg = success_msg

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(self._title), id="xfer_title"),
            ProgressBar(id="xfer_bar"),
            Static(Text("Starting…"), id="xfer_status"),
            id="xfer_box",
        )

    def on_mount(self) -> None:
        self.run_worker(self._run, exit_on_error=False)

    async def _run(self) -> None:
        def on_progress(done, total):
            # Fires inside the worker thread — hop back to the UI thread.
            self.app.call_from_thread(self._update, done, total)

        try:
            await asyncio.to_thread(self._make_call, on_progress)
        except Exception as exc:  # noqa: BLE001 — surface any failure
            self.dismiss((False, str(exc)))
            return
        self.dismiss((True, self._success_msg))

    def _update(self, done: float, total: float | None) -> None:
        bar = self.query_one("#xfer_bar", ProgressBar)
        if total:
            bar.update(total=total, progress=done)
        else:
            # Unknown length — keep the bar full and let the byte counter
            # carry the real progress, mirroring the CLI's estimate trick.
            bar.update(total=max(done, 1), progress=done)
        self.query_one("#xfer_status", Static).update(
            Text(_fmt_transfer(done, total))
        )
