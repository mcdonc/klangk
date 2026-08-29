"""Workspace detail screen: read-only detail + actions."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.dom import NoMatches
from textual.widgets import (
    Label,
    ListItem,
    ListView,
    Static,
)

from ...client import AuthError, WorkspaceNotFoundError
from ..marking import effective_marking, marking_style
from ._base import (
    CheatsheetScreen,
    ConfirmScreen,
    DuplicateScreen,
    InputScreen,
    SpatialListView,
    StatusScreen,
    TransferScreen,
)
from .workspace_form import EditWorkspaceScreen

logger = logging.getLogger(__name__)


def format_uptime(elapsed: int) -> str:
    """Render whole seconds as e.g. ``2d 3h 5m`` (largest non-zero units)."""
    parts = []
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def optional_detail_rows(ws, deploy_banner: str) -> list[tuple[str, str]]:
    """The conditional (label, value) rows for the workspace detail table."""
    rows: list[tuple[str, str]] = []
    _append_str_rows(
        rows,
        ws,
        [
            ("health note", "health_message"),
            ("image", "image"),
            ("service command", "service_command"),
            ("health check", "health_check"),
        ],
    )
    rows.append(("auto-start", "on" if ws.auto_start else "off"))
    banner = effective_marking(
        getattr(ws, "classification_banner", None), deploy_banner
    )
    if banner:
        rows.append(("classification", banner))
    _append_joined_rows(
        rows,
        ws,
        [
            ("mounts", "mounts"),
            ("environment", "env"),
            ("allowed domains", "allowed_domains"),
            ("rejected domains", "rejected_domains"),
        ],
    )
    _append_str_rows(rows, ws, [("owner", "owner_email")])
    return rows


def _append_str_rows(
    rows: list[tuple[str, str]], ws, fields: list[tuple[str, str]]
) -> None:
    """One row per truthy plain-string field."""
    for label, attr in fields:
        value = getattr(ws, attr)
        if value:
            rows.append((label, value))


def _append_joined_rows(
    rows: list[tuple[str, str]], ws, fields: list[tuple[str, str]]
) -> None:
    """One newline-joined row per truthy list/dict field (a dict renders
    as ``k=v`` lines)."""
    for label, attr in fields:
        value = getattr(ws, attr)
        if not value:
            continue
        if isinstance(value, dict):
            rows.append(
                (label, "\n".join(f"{k}={v}" for k, v in value.items()))
            )
        else:
            rows.append((label, "\n".join(str(v) for v in value)))


class WorkspaceDetailScreen(StatusScreen):
    """Read-only workspace detail + restart / duplicate / delete actions."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("e", "edit", "Edit"),
        Binding("r", "restart", "Restart"),
        Binding("s", "stop", "Stop"),
        Binding("u", "duplicate", "Dup"),
        Binding("d", "delete", "Del ws"),
        Binding("x", "export", "Export"),
        # Terminal-scoped keys are hidden from the Footer — their hints
        # are shown inline on the Terminals list header instead (#1860).
        Binding("n", "new_terminal", "New term", show=False),
        Binding("m", "rename_terminal", "Rename term", show=False),
        Binding("t", "delete_terminal", "Del term", show=False),
        Binding("?", "cheatsheet", "Keys", show=False),
    ]

    DEFAULT_CSS = """
    WorkspaceDetailScreen #term_header {
        height: 1;
        margin-bottom: 0;
    }
    WorkspaceDetailScreen #term_label {
        text-style: bold;
        width: auto;
    }
    WorkspaceDetailScreen #term_hints {
        width: 1fr;
        text-align: right;
        color: $text-muted;
    }
    WorkspaceDetailScreen #term_list {
        height: auto;
        max-height: 14;
    }
    WorkspaceDetailScreen #shared_header {
        height: 1;
        margin-top: 1;
        margin-bottom: 0;
    }
    WorkspaceDetailScreen #shared_label {
        text-style: bold;
        width: auto;
        color: $accent;
    }
    WorkspaceDetailScreen #shared_term_list {
        height: auto;
        max-height: 10;
    }
    WorkspaceDetailScreen #detail_body {
        margin-top: 1;
    }
    /* #2768: the classification marking line. Full-width, centered,
    auto-height so a marking longer than the terminal width wraps to
    further lines instead of being clipped (an unreadable marking is not
    a marking). ``display: none`` when no marking is configured
    (workspace override or deploy default) so no row is reserved — the
    #2768 clarification. */
    WorkspaceDetailScreen #marking_bar {
        text-align: center;
        display: none;
    }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self._ws = None
        self._terminals: list[dict] = []
        # Shared terminals visible to this user in the workspace (others'
        # shared windows + the agent's service window). Loaded alongside
        # own terminals so the detail page can list + launch them (#2164).
        self._shared_terminals: list[dict] = []
        self._missing = False
        self._load_error: str | None = None
        # #2768: the deploy-wide default classification marking, fetched
        # once (off the event loop) so the marking bar can fall back to it
        # for a workspace without its own override. "" = none/unreachable.
        self._deploy_banner = ""
        # Serializes terminal-list renders. Adding/removing a terminal fires
        # _render_terminals from BOTH the action handler and the backend's
        # terminals_changed broadcast; without a lock those two concurrent
        # clear/extend/mount cycles interleave on the same ListView and
        # corrupt the DOM (rows un-highlighted, #1956).
        self._render_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        # Header / status dock (StatusBar + Footer) come from StatusScreen
        # (#2689) — the server/user/last-login/live line stays visible
        # while inside a workspace, including the #2661 host countdown.
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        # Spatial nav (#1781): Down off the last own-terminal row enters
        # the shared-terminal list; Up off its first row returns to the
        # own-terminal list. Tab remains a fallback.
        term_list = SpatialListView(id="term_list")
        term_list.SPATIAL_DOWN_TARGET = "#shared_term_list"
        shared_list = SpatialListView(id="shared_term_list")
        shared_list.SPATIAL_UP_TARGET = "#term_list"
        yield Vertical(
            Static("", id="marking_bar"),
            Horizontal(
                Static("Terminals", id="term_label"),
                Static(
                    "[n] new  [m] rename  [t] delete",
                    id="term_hints",
                    markup=False,
                ),
                id="term_header",
            ),
            term_list,
            Horizontal(
                Static("Shared terminals", id="shared_label"),
                id="shared_header",
            ),
            shared_list,
            Static("", id="detail_body"),
            Static("", id="detail_msg"),
            id="detail_box",
        )

    def on_mount(self) -> None:
        self.run_worker(self._mount_async, exit_on_error=False)
        self._uptime_timer = self.set_interval(5, self._tick_uptime)
        # Put a placeholder row in the list immediately and focus it. The real
        # terminal list is only rendered by _render_terminals, which runs AFTER
        # _mount_async — i.e. after a possibly multi-second container
        # auto-start. Without this, the list is empty and unfocused for that
        # whole window, so the keyboard (Tab/arrows/Enter) is dead and the
        # user cannot reach or select the initial terminal (#1956).
        lv = self.query_one("#term_list", ListView)
        lv.append(ListItem(Label(Text("(loading terminals…)")), name=""))
        if lv.index is None:
            lv.index = 0  # highlight the placeholder so the list is selected by default
        # Seed the shared-terminal list too, so spatial Down off the last
        # own-terminal row always lands on a real row (#2164).
        slv = self.query_one("#shared_term_list", ListView)
        slv.append(ListItem(Label(Text("(loading shared…)")), name=""))
        if slv.index is None:
            slv.index = 0
        self.call_after_refresh(self._focus_term_list)

    def on_show(self) -> None:
        # Re-assert focus on the terminals list every time this screen is
        # shown. Focusing in on_mount alone does not survive Textual's
        # screen-activation focus transfer (the #1956 grab was being lost on
        # entry, leaving the list unreachable via keyboard). on_show also
        # fires after a foreground modal (edit form, confirm dialog) is
        # dismissed, returning focus to the list.
        self._focus_term_list()

    def _focus_term_list(self) -> None:
        """Focus #term_list when this screen is the active one.

        Skipped while a modal sits on top (app.screen is not self) so a
        background terminals_changed reload never yanks focus out of the
        foreground dialog. Also skipped when the shared-terminal list
        already has focus, so the 5s uptime tick / a reload doesn't pull
        the user off it (#2164). The list is the screen's primary
        interactive widget, so default to it otherwise.
        """
        if self.app.screen is not self:
            return
        focused = self.focused
        if focused is not None and getattr(focused, "id", None) in (
            "term_list",
            "shared_term_list",
        ):
            return
        self.query_one("#term_list", ListView).focus()

    async def _refresh_deploy_banner(self) -> None:
        """(Re-)fetch the deploy-default marking (#2768).

        A plain HTTP call, run off the event loop so a slow/unreachable
        server cannot stall the screen (same posture as _load_terminals).
        Called on mount, on every workspaces-changed push, and after an
        edit — the deploy default can change under a live screen (a SIGHUP
        settings reload), and the marking bar must fall back to the
        current value, not a stale snapshot. A failure degrades to ""
        (no deploy marking; the workspace's own marking still renders).
        """
        try:
            self._deploy_banner = await asyncio.to_thread(
                self.app.tui_state.default_classification_banner
            )
        except Exception:  # noqa: BLE001 — degrade to no deploy marking
            self._deploy_banner = ""

    async def _mount_async(self) -> None:
        await self._refresh_deploy_banner()
        await self._load()
        if self._ws is not None and not self._ws.running:
            await self._start_if_stopped()
        self.run_worker(self._load_terminals, exit_on_error=False)
        self.run_worker(self._load_shared_terminals, exit_on_error=False)

    async def _load(self) -> None:
        try:
            self._ws = await asyncio.to_thread(
                self.app.tui_state.find_workspace, self._name
            )
            self._missing = False
            self._load_error = None
        except WorkspaceNotFoundError:
            self._ws = None
            self._missing = True
            self._load_error = None
        except AuthError:
            # Session is dead — surface the app-wide overlay (#2025) instead
            # of a small inline message. The screen stays mounted under the
            # overlay until the user acknowledges it and is redirected.
            self._ws = None
            self._missing = False
            self._load_error = None
            self.app.session_expired()
        except Exception as exc:
            self._ws = None
            self._missing = False
            self._load_error = f"Could not load workspace: {exc}"
            logger.warning("Workspace load failed: %s", exc)
        self._display()

    async def _start_if_stopped(self) -> None:
        """Auto-start a stopped workspace container on visit.

        The Flutter UI does this via its WebSocket ``connectWorkspace``
        call; the TUI replicates the behaviour with a restart request.
        """
        self._msg("Starting container…")
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Auto-start failed: {exc}", error=True)
            return
        await self._load()
        self._msg("Container started.")
        self.app.refresh_workspaces()

    @staticmethod
    def _bindings_list(stop_label: str = "Stop") -> list[Binding]:
        """Workspace-scoped keybindings for the Footer.

        The ``s`` label toggles Stop/Start (#1791). Terminal-scoped keys
        (new / delete term) are hidden here — their hints render inline on
        the Terminals list header instead (#1860).
        """
        return [
            Binding("escape", "app.pop_screen", "Back"),
            Binding("e", "edit", "Edit"),
            Binding("r", "restart", "Restart"),
            Binding("s", "stop", stop_label),
            Binding("u", "duplicate", "Dup"),
            Binding("d", "delete", "Del ws"),
            Binding("n", "new_terminal", "New term", show=False),
            Binding("m", "rename_terminal", "Rename term", show=False),
            Binding("t", "delete_terminal", "Del term", show=False),
            # `?` must survive the per-display BINDINGS rebuild so the
            # cheatsheet stays reachable after _display() runs on mount
            # (#1802).
            Binding("?", "cheatsheet", "Keys", show=False),
        ]

    def _display(self) -> None:
        # Re-assert focus on the terminals list while this screen is active.
        # _display runs on every load and on the 5s uptime tick, so a focus
        # steal during the first-visit auto-start event storm (which left the
        # terminal row green-then-grey, #1956) is recovered within moments.
        # No-op while a modal is on top (guarded in _focus_term_list).
        self._focus_term_list()
        self._render_marking_bar()
        ws = self._ws
        body = self.query_one("#detail_body", Static)
        if ws is None:
            body.update(Text(self._load_error or "Could not load workspace."))
            return
        # Toggle the 's' binding label between Stop / Start.
        self.BINDINGS = self._bindings_list("Stop" if ws.running else "Start")
        self.refresh_bindings()
        # Render the detail as a two-column table so every value lines up in
        # the same column regardless of label length (#1910). Parsed with
        # Text.from_ansi (not Text()) so the zebra row backgrounds (#2193)
        # survive; from_ansi reads ANSI escapes only, so values that look like
        # markup (e.g. an image named "[img]") still render literally rather
        # than being parsed as Textual markup.
        #
        # Render at the body widget's *actual* content width, not the screen
        # width: the screen has horizontal chrome (borders/margins/scroll
        # gutter), so #detail_body is markedly narrower than self.size.width.
        # Rendering at the wider screen width produces full-width lines that
        # the narrower Static then re-wraps, which destroys the value column's
        # hanging indent — wrapped continuation lines fall back to the left
        # margin under the labels (#2190).
        width = (
            body.container_size.width
            or body.size.width
            or self.size.width
            or 80
        )
        body.update(
            Text.from_ansi(
                self._render_detail(
                    self._detail_rows(ws, self._deploy_banner), width
                )
            )
        )

    def _render_marking_bar(self) -> None:
        """Render (or hide) the classification marking line (#2768).

        The STIG posture: the marking is persistent — visible on every
        render of the detail screen, top of the screen, color-coded. With
        no effective marking (no workspace override, no deploy default)
        the bar is hidden entirely and reserves no row.
        """
        banner = effective_marking(
            getattr(self._ws, "classification_banner", None)
            if self._ws is not None
            else None,
            self._deploy_banner,
        )
        try:
            bar = self.query_one("#marking_bar", Static)
        except NoMatches:  # pragma: no cover - not yet mounted
            return
        if not banner:
            bar.display = False
            return
        bar.display = True
        # Text (not markup) so a label containing brackets stays literal.
        bar.update(Text(banner, style=marking_style(banner)))

    @staticmethod
    def _detail_rows(ws, deploy_banner: str = "") -> list[tuple[str, str]]:
        """Build the (label, value) rows shown in the workspace detail table."""
        rows: list[tuple[str, str]] = [
            ("id", str(ws.id)),
            ("running", "yes" if ws.running else "no"),
            ("health", ws.health or "-"),
        ]
        if ws.running and ws.service_started_at:
            elapsed = int(time.time() - ws.service_started_at)
            if elapsed >= 0:
                rows.append(("uptime", format_uptime(elapsed)))
        rows.extend(optional_detail_rows(ws, deploy_banner))
        return rows

    @staticmethod
    def _render_detail(rows: list[tuple[str, str]], width: int) -> str:
        """Render (label, value) rows as an aligned two-column table string.

        The label column auto-sizes to the longest label, so every value
        starts at the same column; the value column folds long values to fit
        ``width`` instead of running off the right edge. Rows are
        zebra-striped (alternating ``surface`` background) for readability
        (#2193); the caller must parse the result with ``Text.from_ansi``
        so the row backgrounds survive into the ``Static``."""
        # row_styles cycles once per add_row, so a multi-line value (service
        # command, mounts) keeps one background for the whole logical row
        # rather than striping per wrapped line (#2193). The stripe is the
        # theme `surface` (#161B22) over the screen `background` (#0D1117);
        # see KLANGK_THEME in app.py.
        table = Table(
            show_header=False,
            box=None,
            pad_edge=False,
            padding=(0, 1),
            expand=True,
            row_styles=("", "on #161B22"),
        )
        # Key names (left column) are right-aligned and bold so each
        # label's right edge lines up just before its value (#2193).
        table.add_column("label", no_wrap=True, justify="right", style="bold")
        table.add_column("value", overflow="fold", ratio=1)
        for label, value in rows:
            table.add_row(label, value)
        buf = StringIO()
        # Emit truecolor ANSI so the row backgrounds survive into the
        # Static via Text.from_ansi (#2193); markup=False keeps values like
        # "[img]" literal, highlight=False disables Rich auto-highlighting.
        Console(
            file=buf,
            width=width,
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            markup=False,
        ).print(table, end="")
        # Strip trailing whitespace per line: Rich pads each row to the full
        # table width, and a line exactly at the Static's width can still get
        # re-wrapped by a 1-char gutter, which would drop the hanging indent.
        # Content-width lines never trigger that re-wrap (#2190).
        return "\n".join(line.rstrip() for line in buf.getvalue().splitlines())

    def _tick_uptime(self) -> None:
        """Refresh the display periodically to update the uptime counter."""
        if (
            self._ws is not None
            and self._ws.running
            and self._ws.service_started_at
        ):
            self._display()

    def _msg(self, text: str, *, error: bool = False) -> None:
        """Show transient operational feedback on the detail screen (#2019).

        Errors persist inline (red) so the user can read why an action failed
        — consistent with the export-failure path (#1758). Success /
        in-progress feedback is shown as an auto-dismissing toast instead of
        lingering on the page: the terminals list (one selected) already
        signals container readiness, so a persistent status line is noise.
        """
        if error:
            self.query_one("#detail_msg", Static).update(
                Text(text, style="red")
            )
        else:
            self.app.notify(text)

    def action_edit(self) -> None:
        if self._ws is None:
            return
        self.run_worker(self._do_edit, exit_on_error=False)

    async def _do_edit(self) -> None:
        state = self.app.tui_state
        try:
            data = await asyncio.to_thread(state.list_images)
            default = data.get("default", "") or ""
            allowed = list(data.get("allowed") or [])
            nix_available = data.get("nix_available") is True
            sudo_available = data.get("sudo_available") is True
        except AuthError:
            self.app.session_expired()
            return
        except Exception:
            default, allowed = "", []
            nix_available = False
            sudo_available = False
        try:
            allow_autostart = await asyncio.to_thread(state.allow_autostart)
        except AuthError:
            self.app.session_expired()
            return
        except Exception:
            allow_autostart = False
        self.app.push_screen(
            EditWorkspaceScreen(
                workspace=self._ws,
                allowed=allowed,
                default=default,
                allow_autostart=allow_autostart,
                nix_available=nix_available,
                sudo_available=sudo_available,
            ),
            self._on_edited,
        )

    def _on_edited(self, result: str | bool | None) -> None:
        if isinstance(result, str):
            self._name = result
        if result:
            self.run_worker(self._reload_after_edit, exit_on_error=False)

    async def _reload_after_edit(self) -> None:
        # Deferred: main.py imports this module (push_screen on row select),
        # so a module-scope ``from .main import MainScreen`` would execute
        # while main is partially initialized depending on entry order —
        # a genuine import cycle. Only this method needs the name, at call
        # time, when both modules are fully loaded.
        from .main import MainScreen  # allow-deferred-import

        await self._refresh_deploy_banner()
        await self._load()
        try:
            self.app.query_one(MainScreen).refresh_lists()
        except NoMatches:
            pass

    def apply_status_event(self, event: dict) -> None:
        """Update running/health from a live status broadcast.

        Only applies when the event is for this workspace; ``workspaces_changed``
        re-fetches. User-derived text is rendered via ``Text`` so bracket
        characters in names/messages never trigger markup parsing.
        """
        if not self._status_event_applies(event):
            return
        etype = event.get("type")
        if etype == "workspaces_changed":
            self.run_worker(self._reload_on_status, exit_on_error=False)
            return
        if etype == "terminals_changed":
            self._apply_terminals_changed(event)
            return
        if etype == "container_status":
            self._apply_container_status(event)
        elif etype == "service_health":
            self._apply_service_health(event)
        else:
            return
        self._display()

    def _status_event_applies(self, event: dict) -> bool:
        """False when no workspace is mounted or the event is for a
        different workspace."""
        if self._ws is None:
            return False
        ws_id = str(getattr(self._ws, "id", "") or "")
        eid = str(event.get("workspace_id") or "")
        return not (eid and ws_id and eid != ws_id)

    def _apply_terminals_changed(self, event: dict) -> None:
        """A terminal was added / removed / renamed from another surface.
        The event carries the window list (like the Flutter UI's
        terminal_windows push), so update directly instead of
        re-enumerating via a terminal_start round-trip (#1894)."""
        windows = event.get("windows")
        if isinstance(windows, list):
            # Adopt the pushed list verbatim. Events are serialized
            # per status-WS connection, so out-of-order arrival (a
            # close broadcast beating its create) is not expected;
            # the payload type check above also makes the push path
            # resilient to a malformed payload (fall back to fetch).
            self._terminals = windows
            self.run_worker(self._render_terminals(), exit_on_error=False)
        else:
            # No payload (older server) or malformed -- fall back to a
            # fetch, preserving the resilience of the old poll path.
            self.run_worker(self._load_terminals, exit_on_error=False)

    def _apply_container_status(self, event: dict) -> None:
        """Running flag + start-stamp adoption from a container_status
        event; a (re)start triggers the full reload (#1924)."""
        was_running = self._ws.running
        old_started = self._ws.service_started_at
        self._ws.running = bool(event.get("running"))
        # Type-check before adopting: a malformed payload (string stamp)
        # would crash _tick_uptime's ``time.time() - started`` math
        # later (#2029 audit).
        started = event.get("service_started_at")
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            self._ws.service_started_at = started
        # A container start or restart invalidates everything — uptime
        # resets, health resets, terminal sessions are gone. Do a full
        # reload so all detail-screen items reflect the new state (#1924).
        if self._ws.running and (
            not was_running or self._ws.service_started_at != old_started
        ):
            self.run_worker(self._reload_on_restart, exit_on_error=False)

    def _apply_service_health(self, event: dict) -> None:
        """Health state from a service_health event."""
        self._ws.running = bool(event.get("running", self._ws.running))
        self._ws.health = "healthy" if event.get("healthy") else "unhealthy"
        msg = event.get("health_message")
        if msg is not None:
            self._ws.health_message = msg

    async def _reload_on_status(self) -> None:
        # A workspaces_changed push re-resolves the marking (the workspace
        # row re-fetch below) — refresh the deploy default too, so a
        # SIGHUP-reloaded KLANGKD_CLASSIFICATION_BANNER re-marks here as
        # well, not just on fresh screen mounts (#2768 review).
        await self._refresh_deploy_banner()
        await self._load()
        if self._missing:
            # The workspace was deleted out from under this screen. Pop any
            # modal sitting on top first (edit form / confirm dialog), then
            # self -- a bare pop_screen() would dismiss only the TOP screen,
            # silently closing the user's dialog while leaving the dead
            # detail page mounted (#2029 audit).
            self.app._pop_above(self)
            if self.app.screen is self:
                self.app.pop_screen()

    async def _reload_on_restart(self) -> None:
        """Full reload after a container start/restart (#1924).

        Re-fetches workspace metadata (uptime, health, running state) and
        the terminal list so every item on the detail screen reflects the
        new container.
        """
        await self._load()
        if not self._missing:
            await self._load_terminals()

    # --- terminals (own) ---

    async def _load_terminals(self) -> None:
        try:
            windows = await self.app.tui_state.list_terminals(self._name)
        except AuthError:
            self.app.session_expired()
            return
        except Exception:
            windows = []
        self._terminals = windows or []
        await self._render_terminals()

    async def _render_terminals(self) -> None:
        # Serialize: adding/removing a terminal fires this from BOTH the
        # action handler and the backend's terminals_changed broadcast;
        # without a lock the two clear/extend/mount cycles interleave on the
        # same ListView and corrupt the DOM (rows un-highlighted, #1956).
        async with self._render_lock:
            lv = self.query_one("#term_list", ListView)
            await lv.clear()
            if not self._terminals:
                items = [ListItem(Label(Text("(no terminals)")), name="")]
            else:
                items = [
                    ListItem(
                        Label(
                            Text(
                                f"{w.get('index', '')}  "
                                f"{w.get('name') or w.get('index', '')}"
                            )
                        ),
                        name=str(w.get("index", "")),
                    )
                    for w in self._terminals
                ]
            mount = lv.extend(items)
            # Keep focus on the list; skipped when a modal is open over this
            # screen so a background terminals_changed reload never yanks
            # focus out of the foreground dialog (#1956).
            if self.app.screen is self:
                self._focus_term_list()
            # Await the mount BEFORE setting the default index. Setting
            # index=0 synchronously fires the highlight watcher before the
            # new ListItems exist, so no row would be highlighted (the #1956
            # "both terminals grey, Down makes the second green" symptom).
            # After mount the first row highlights correctly.
            await mount
            if lv.index is None:
                lv.index = 0

    async def _load_shared_terminals(self) -> None:
        """Fetch the workspace's shared terminals (others' shared windows +
        the agent's service window) and render them (#2164)."""
        try:
            terminals = await self.app.tui_state.list_shared_terminals(
                self._name
            )
        except AuthError:
            self.app.session_expired()
            return
        except Exception:
            terminals = []
        # Exclude my own shared windows — they're already in the own-terminals
        # list above; showing them again here would be noise. The service
        # window and other users' shared windows stay. ``current_user_id``
        # does a synchronous /auth/me fetch, so run it off the event loop
        # (run_worker runs the coroutine on the loop, thread=False) to
        # avoid freezing the TUI on the first detail-page open (#2164
        # review).
        my_id = await asyncio.to_thread(self._current_user_id)
        self._shared_terminals = [
            t for t in (terminals or []) if t.get("user_id") != my_id
        ]
        await self._render_shared_terminals()

    async def _render_shared_terminals(self) -> None:
        async with self._render_lock:
            lv = self.query_one("#shared_term_list", ListView)
            await lv.clear()
            if not self._shared_terminals:
                items = [ListItem(Label(Text("(none)")), name="")]
            else:
                items = [
                    ListItem(
                        Label(Text(self._shared_terminal_label(t))),
                        # Key by the join target the shell expects:
                        # ``handle:window_name``.
                        name=self._shared_terminal_key(t),
                    )
                    for t in self._shared_terminals
                ]
            mount = lv.extend(items)
            await mount
            if lv.index is None:
                lv.index = 0

    @staticmethod
    def _shared_terminal_label(t: dict) -> str:
        # The agent's service window is presented distinctly, mirroring the
        # browser's "Service" tab (#1159).
        if t.get("is_service"):
            return f"Service  ({t.get('window_name') or '?'})"
        handle = t.get("handle") or "?"
        win = t.get("window_name") or "?"
        return f"{handle}: {win}"

    @staticmethod
    def _shared_terminal_key(t: dict) -> str:
        return f"{t.get('handle') or '?'}:{t.get('window_name') or '?'}"

    def _current_user_id(self) -> str | None:
        """The authenticated user's id, for filtering own shared windows.

        Cached on the app's tui_state (fetched once via /auth/me). Returns
        None if it can't be resolved, in which case nothing is filtered.
        """
        return self.app.tui_state.current_user_id()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Spawn ``klangk shell`` for the selected terminal.

        Inline in this terminal (TUI suspended) by default, or in a new
        terminal window when ``terminal-open-cmd`` is configured (#2685)."""
        if event.control.id == "shared_term_list":
            self._launch_shared_terminal(event)
            return
        terminal = getattr(event.item, "name", "") or ""
        if not terminal or self._ws is None:
            return
        # The list item is keyed by the window INDEX, but the shell must
        # target the window by its stable id (@N) so the backend selects
        # the existing window instead of creating a duplicate named after
        # the index (#1954). If no id can be resolved — stale list, or a
        # server contract violation (terminal.list_windows always sets
        # id) — refuse to spawn and refresh instead: falling back to the
        # raw index would reproduce the duplicate-window bug.
        target = self._window_id_for(terminal)
        if target is None:
            self._msg(
                "Terminal no longer exists — refreshing list.", error=True
            )
            self.run_worker(self._load_terminals, exit_on_error=False)
            return
        completed = self._launch_shell(self._shell_argv(target))
        if completed is not None and completed.returncode != 0:
            # The shell exited non-zero — most likely the window was
            # deleted server-side between list refresh and selection.
            # Refresh so the dead row self-heals instead of failing
            # identically on every re-select (#1955 review).
            self.run_worker(self._load_terminals, exit_on_error=False)

    def _shell_argv(self, target: str) -> list[str]:
        """The ``klangk shell`` argv for a terminal target on this workspace."""
        cmd = [sys.executable, "-m", "klangk.cli.main"]
        server = self.app.tui_state.current_url()
        if server:
            cmd += ["--server", server]
        return cmd + ["shell", self._name, target]

    def _launch_shell(
        self, cmd: list[str]
    ) -> subprocess.CompletedProcess | None:
        """Run the ``klangk shell`` argv, externally when configured.

        With a terminal-open command configured (#2685: ``terminal-open-cmd``
        in klangk.yaml or ``KLANGKC_TERMINAL_OPEN_CMD``), spawn the shell in
        a new terminal window via the configured argv (``term_cmd + cmd`` —
        terminal emulators like konsole take the command as trailing args
        after ``-e``). The TUI stays up — no suspend. Returns None on that
        path: launchers like konsole always exit 0 and return immediately
        (or when the window closes), so there is no exit code worth acting
        on. Output is discarded — the launcher runs in its own window, and
        a launcher that fails after exec'ing (e.g. no DISPLAY server) would
        otherwise spray its abort message across the TUI's screen. Only an
        ``OSError`` from Popen itself (command missing / not executable)
        triggers the fallback: the error is shown inline and the inline
        launch is deferred via ``set_timer`` so the message paints before
        suspend() blanks the screen (#2686 review).

        Unset — current behavior: suspend the TUI, clear the primary
        screen buffer (which still shows whatever was there before the
        TUI launched, #2010), run inline, return the CompletedProcess so
        the caller can act on a non-zero exit.
        """
        term_cmd = self.app.tui_state.cfg().get_terminal_open_cmd()
        if term_cmd:
            try:
                # Own session so the new window outlives the TUI / this
                # command's process group.
                subprocess.Popen(
                    [*term_cmd, *cmd],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return None
            except OSError as exc:
                self._msg(
                    f"terminal-open-cmd failed ({exc}) —"
                    " opening in this terminal instead.",
                    error=True,
                )
                # Defer one refresh cycle so the message above renders
                # before suspend() takes over the screen.
                self.call_after_refresh(lambda: self._launch_shell_inline(cmd))
                return None
        return self._launch_shell_inline(cmd)

    def _launch_shell_inline(
        self, cmd: list[str]
    ) -> subprocess.CompletedProcess:
        """Suspend the TUI and run the shell argv in this terminal."""
        with self.app.suspend():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            return subprocess.run(cmd)

    def _launch_shared_terminal(self, event: ListView.Selected) -> None:
        """Join the selected shared terminal via ``klangk shell <ws> <handle>:<win>``.

        Reuses the existing ``join_shared_terminal`` server path (the same
        one the browser uses) — no new server code (#2164). The list item
        is keyed by the join target, so it's passed straight through.
        """
        target = getattr(event.item, "name", "") or ""
        if not target or self._ws is None:
            return
        # A shared window named with a colon would confuse the
        # ``handle:window`` parser; refuse rather than mis-target.
        if ":" not in target or target.count(":") != 1:
            self._msg("Invalid shared terminal — refreshing.", error=True)
            self.run_worker(self._load_shared_terminals, exit_on_error=False)
            return
        completed = self._launch_shell(self._shell_argv(target))
        if completed is not None and completed.returncode != 0:
            # The shared window may have been unshared/closed server-side
            # between refresh and selection — refresh the shared list.
            self.run_worker(self._load_shared_terminals, exit_on_error=False)

    def _window_id_for(self, key: str) -> str | None:
        """Resolve a list-item key (window index) to the window's id (@N).

        Returns the id when the key is an index present in the current
        window list. Returns None otherwise — callers must NOT spawn a
        shell with None, since falling back to the raw index would
        reproduce the #1954 duplicate-window bug.
        """
        try:
            idx = int(key)
        except (TypeError, ValueError):
            return None
        for w in self._terminals:
            if w.get("index") == idx:
                wid = w.get("id")
                if wid:
                    return str(wid)
                # Matched by index but the server omitted the window id —
                # a contract violation (terminal.list_windows always sets
                # id from tmux's #{window_id}). Log loudly rather than
                # silently degrade to the index (#1955 review).
                logger.warning(
                    "Terminal index %s has no window id; refusing to "
                    "select by index (would create a duplicate, #1954).",
                    idx,
                )
                return None
        return None

    def _own_list_focused(self) -> bool:
        """True when #term_list (not the shared list) has focus.

        The [n]/[m]/[t] bindings are own-terminal actions; they must not
        fire while the shared-terminal list is focused, or they'd act on
        the own list's stale highlighted row (e.g. delete the wrong
        terminal) — a footgun once two lists share the screen (#2164).
        """
        focused = self.focused
        return focused is not None and getattr(focused, "id", None) == (
            "term_list"
        )

    def action_delete_terminal(self) -> None:
        if not self._own_list_focused():
            return
        lv = self.query_one("#term_list", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        if not child.name:
            return
        if len(self._terminals) <= 1:
            self._msg("Can't delete the last terminal.", error=True)
            return
        # Target the window by its stable id (@N), not the row index —
        # a stale list could otherwise close the wrong window (#1965).
        window_id = self._window_id_for(child.name)
        if window_id is None:
            self._msg(
                "Terminal no longer exists — refreshing list.", error=True
            )
            self.run_worker(self._load_terminals, exit_on_error=False)
            return
        label = self._terminal_label_for(child.name)
        self.run_worker(
            self._do_delete_terminal(window_id, label), exit_on_error=False
        )

    async def _do_delete_terminal(
        self, window_id: str, label: str | None = None
    ) -> None:
        # label is a friendly name for messages; falls back to the id
        # when not supplied (e.g. direct test calls).
        display = label if label is not None else window_id
        self.app.notify(f"Deleting terminal {display}…")
        try:
            windows = await self.app.tui_state.close_terminal(
                self._name, window_id
            )
        except Exception as exc:
            self.app.notify(
                f"Delete failed: {exc}", severity="error", timeout=8
            )
            # The id may no longer exist server-side — refresh so the
            # dead row self-heals instead of failing on every retry (#1965).
            await self._load_terminals()
            return
        if not windows:
            # The last terminal is protected client-side, so an empty result
            # here means the close/refresh failed — don't claim success.
            self.app.notify(
                "Delete failed — could not refresh terminals.",
                severity="error",
                timeout=8,
            )
            await self._load_terminals()
            return
        self._terminals = windows
        await self._render_terminals()
        self.app.notify(f"Deleted terminal {display}.")

    def _terminal_label_for(self, key: str) -> str:
        """Friendly label for a list row: the window name, or the key."""
        try:
            idx = int(key)
        except (TypeError, ValueError):
            return key
        for w in self._terminals:
            if w.get("index") == idx:
                return str(w.get("name") or idx)
        return key

    def action_rename_terminal(self) -> None:
        if not self._own_list_focused():
            return
        lv = self.query_one("#term_list", ListView)
        child = lv.highlighted_child
        if child is None or not child.name:
            return
        # The rename controller targets the window by INDEX (tmux
        # rename-window -t session:INDEX), not the @N id used by select /
        # delete. The list row key *is* the index (#1965).
        index = int(child.name)
        current = self._terminal_label_for(child.name)

        def _on_rename(new_name: str | None) -> None:
            # InputScreen dismisses None on cancel/escape (#2016); an
            # unchanged name also skips the round-trip.
            if not new_name or new_name == current:
                return
            self.run_worker(
                self._do_rename_terminal(index, new_name),
                exit_on_error=False,
            )

        self.app.push_screen(
            InputScreen(
                f"Rename '{current}' to:",
                default=current,
                ok_label="Rename",
                select_on_focus=False,
            ),
            _on_rename,
        )

    async def _do_rename_terminal(self, index: int, new_name: str) -> None:
        try:
            windows = await self.app.tui_state.rename_terminal(
                self._name, index, new_name
            )
        except Exception as exc:
            self.app.notify(
                f"Rename failed: {exc}", severity="error", timeout=8
            )
            # Stale index — refresh so the row self-heals (#1965).
            await self._load_terminals()
            return
        if not windows:
            self.app.notify(
                "Rename failed — could not refresh terminals.",
                severity="error",
                timeout=8,
            )
            await self._load_terminals()
            return
        self._terminals = windows
        await self._render_terminals()
        self.app.notify(f"Renamed terminal to '{new_name}'.")

    def action_new_terminal(self) -> None:
        if not self._own_list_focused():
            return
        if self._ws is None:
            return
        self.run_worker(self._do_new_terminal, exit_on_error=False)

    async def _do_new_terminal(self) -> None:
        # No name → the server names the window "bash", matching window 0
        # and the tmux status-bar "+". Names are display-only, so there's
        # no need to invent a unique sequential label (#2192).
        self.app.notify("Creating terminal…")
        try:
            windows = await self.app.tui_state.create_terminal(self._name)
        except Exception as exc:
            self.app.notify(
                f"Create failed: {exc}", severity="error", timeout=8
            )
            return
        if not windows:
            self.app.notify(
                "Create failed — could not refresh terminals.",
                severity="error",
                timeout=8,
            )
            return
        self._terminals = windows
        await self._render_terminals()
        self.app.notify("Created terminal.")

    # --- actions ---

    def action_restart(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_restart, exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Restart '{self._name}'? This ends active terminal"
                " sessions and recreates the container.",
                yes_label="Restart",
                yes_variant="warning",
            ),
            _on_confirm,
        )

    async def _do_restart(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Restart failed: {exc}", error=True)
            return
        if self._ws is not None:
            self._ws.service_started_at = time.time()
            self._display()
        self._msg("Restart requested.")
        self.app.refresh_workspaces()

    def action_stop(self) -> None:
        if self._ws is None:
            return
        if self._ws.running:
            self._confirm_stop()
        else:
            self.run_worker(self._do_start, exit_on_error=False)

    def _confirm_stop(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_stop, exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Stop '{self._name}'? This ends active terminal sessions.",
                yes_label="Stop",
                yes_variant="warning",
            ),
            _on_confirm,
        )

    async def _do_stop(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.stop_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Stop failed: {exc}", error=True)
            return
        if self._ws is not None:
            self._ws.running = False
            self._ws.service_started_at = None
            self._display()
        self._msg("Stop requested.")
        self.app.refresh_workspaces()

    async def _do_start(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.start_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Start failed: {exc}", error=True)
            return
        if self._ws is not None:
            self._ws.running = True
            self._ws.service_started_at = time.time()
            self._display()
        self._msg("Start requested.")
        self.app.refresh_workspaces()

    def action_delete(self) -> None:
        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            self.run_worker(self._do_delete, exit_on_error=False)

        self.app.push_screen(
            ConfirmScreen(
                f"Delete '{self._name}'? This permanently deletes the"
                " workspace and its container."
            ),
            _on_confirm,
        )

    async def _do_delete(self) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.delete_workspace, self._name
            )
        except Exception as exc:
            self._msg(f"Delete failed: {exc}", error=True)
            return
        # Guarded: the delete's own workspaces_changed broadcast can pop
        # this screen before the worker resumes (#2029 review round 2) —
        # an unguarded pop would eat the MainScreen below.
        if self in self.app.screen_stack:
            self.app.pop_screen()  # back to the list
        self.app.refresh_workspaces()

    def action_duplicate(self) -> None:
        self.app.push_screen(DuplicateScreen(self._name), self._on_duplicate)

    def _on_duplicate(self, new_name: str | None) -> None:
        if not new_name:
            return
        self.run_worker(self._do_duplicate(new_name), exit_on_error=False)

    async def _do_duplicate(self, new_name: str) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.duplicate_workspace, self._name, new_name
            )
        except Exception as exc:
            self._msg(f"Duplicate failed: {exc}", error=True)
            return
        self._msg(f"Duplicated as '{new_name}'.")
        self.app.refresh_workspaces()

    def action_export(self) -> None:
        """Export this workspace to a .tar.gz (admin only) with progress (#1758)."""
        self.app.push_screen(
            InputScreen(
                f"Export '{self._name}' to:",
                default=f"{self._name}.tar.gz",
                ok_label="Export",
            ),
            self._on_export,
        )

    def action_cheatsheet(self) -> None:
        """Open the ``?`` keyboard cheatsheet modal (#1802)."""
        self.app.push_screen(CheatsheetScreen(self._cheatsheet_sections()))

    @staticmethod
    def _cheatsheet_sections() -> list[tuple[str, list[tuple[str, str]]]]:
        """Keybindings shown in the cheatsheet, grouped by context (#1802).

        Hand-written display labels (see MainScreen._cheatsheet_sections
        for the rationale); the TUI tests assert each key appears.
        """
        return [
            (
                "Navigation",
                [
                    ("Esc", "Back to workspace list"),
                    ("Enter", "Open a terminal shell"),
                ],
            ),
            (
                "Workspace",
                [
                    ("e", "Edit"),
                    ("r", "Restart"),
                    ("s", "Stop / Start"),
                    ("u", "Duplicate"),
                    ("d", "Delete workspace"),
                    ("x", "Export archive"),
                ],
            ),
            (
                "Terminals",
                [
                    ("n", "New terminal"),
                    ("m", "Rename terminal"),
                    ("t", "Delete terminal"),
                ],
            ),
        ]

    def _on_export(self, path: str | None) -> None:
        if not path:
            return
        state = self.app.tui_state
        # Resolve to an absolute path so the completion toast reports
        # exactly where the archive landed on disk — a relative input
        # like "x.tar.gz" is written under the TUI's CWD, which isn't
        # obvious without the resolved path (#1758).
        full_path = str(Path(path).expanduser().resolve())

        def make_call(on_progress):
            state.export_workspace(self._name, Path(full_path), on_progress)

        self.app.push_screen(
            TransferScreen(
                f"Exporting '{self._name}'…",
                make_call,
                full_path,
            ),
            self._on_export_done,
        )

    def _on_export_done(self, result: tuple[bool, str]) -> None:
        ok, payload = result
        if ok:
            # payload is the resolved absolute filesystem path — toast it
            # so the user can find / copy the archive location.
            self.app.notify(f"Exported to {payload}", timeout=10)
        else:
            # payload is the error text; show it inline on the detail screen.
            self._msg(payload, error=True)
