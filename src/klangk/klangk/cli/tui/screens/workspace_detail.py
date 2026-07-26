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
from textual.screen import Screen
from textual.widgets import (
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
)

from ...client import AuthError, WorkspaceNotFoundError
from ._base import (
    ConfirmScreen,
    DuplicateScreen,
    InputScreen,
    SpatialListView,
    TransferScreen,
)

logger = logging.getLogger(__name__)


class WorkspaceDetailScreen(Screen):
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
        Binding("delete", "delete_terminal", "Del term", show=False),
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
    WorkspaceDetailScreen #detail_body {
        margin-top: 1;
    }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self._ws = None
        self._terminals: list[dict] = []
        self._missing = False
        self._load_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Horizontal(
                Static("Terminals", id="term_label"),
                Static("[n] new  [⌿] delete", id="term_hints", markup=False),
                id="term_header",
            ),
            SpatialListView(id="term_list"),
            Static("", id="detail_body"),
            Static("", id="detail_msg"),
            id="detail_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._mount_async, exit_on_error=False)
        self._uptime_timer = self.set_interval(5, self._tick_uptime)

    async def _mount_async(self) -> None:
        await self._load()
        if self._ws is not None and not self._ws.running:
            await self._start_if_stopped()
        self.run_worker(self._load_terminals, exit_on_error=False)

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
            self._ws = None
            self._missing = False
            self._load_error = "Session expired — please log in again."
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
            Binding("delete", "delete_terminal", "Del term", show=False),
        ]

    def _display(self) -> None:
        ws = self._ws
        body = self.query_one("#detail_body", Static)
        if ws is None:
            body.update(Text(self._load_error or "Could not load workspace."))
            return
        # Toggle the 's' binding label between Stop / Start.
        self.BINDINGS = self._bindings_list("Stop" if ws.running else "Start")
        self.refresh_bindings()
        # Render the detail as a two-column table so every value lines up in
        # the same column regardless of label length (#1910). Wrapped in Text()
        # so values that look like markup (e.g. an image named "[img]") render
        # literally instead of being parsed as Textual markup.
        width = self.size.width or 80
        body.update(Text(self._render_detail(self._detail_rows(ws), width)))

    @staticmethod
    def _detail_rows(ws) -> list[tuple[str, str]]:
        """Build the (label, value) rows shown in the workspace detail table."""
        rows: list[tuple[str, str]] = [
            ("id", str(ws.id)),
            ("running", "yes" if ws.running else "no"),
            ("health", ws.health or "-"),
        ]
        if ws.running and ws.service_started_at:
            elapsed = int(time.time() - ws.service_started_at)
            if elapsed >= 0:
                parts = []
                days, rem = divmod(elapsed, 86400)
                hours, rem = divmod(rem, 3600)
                minutes, _ = divmod(rem, 60)
                if days:
                    parts.append(f"{days}d")
                if hours:
                    parts.append(f"{hours}h")
                parts.append(f"{minutes}m")
                rows.append(("uptime", " ".join(parts)))
        if ws.health_message:
            rows.append(("health note", ws.health_message))
        if ws.image:
            rows.append(("image", ws.image))
        if ws.service_command:
            rows.append(("service command", ws.service_command))
        if ws.health_check:
            rows.append(("health check", ws.health_check))
        rows.append(("auto-start", "on" if ws.auto_start else "off"))
        if ws.mounts:
            rows.append(("mounts", "\n".join(str(m) for m in ws.mounts)))
        if ws.env:
            rows.append(
                (
                    "environment",
                    "\n".join(f"{k}={v}" for k, v in ws.env.items()),
                )
            )
        if ws.allowed_domains:
            rows.append(
                (
                    "allowed domains",
                    "\n".join(str(d) for d in ws.allowed_domains),
                )
            )
        if ws.owner_email:
            rows.append(("owner", ws.owner_email))
        return rows

    @staticmethod
    def _render_detail(rows: list[tuple[str, str]], width: int) -> str:
        """Render (label, value) rows as an aligned two-column table string.

        The label column auto-sizes to the longest label, so every value
        starts at the same column; the value column folds long values to fit
        ``width`` instead of running off the right edge."""
        table = Table(
            show_header=False,
            box=None,
            pad_edge=False,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("label", no_wrap=True)
        table.add_column("value", overflow="fold", ratio=1)
        for label, value in rows:
            table.add_row(label, value)
        buf = StringIO()
        Console(
            file=buf,
            width=width,
            force_terminal=False,
            no_color=True,
            highlight=False,
            markup=False,
        ).print(table, end="")
        return buf.getvalue()

    def _tick_uptime(self) -> None:
        """Refresh the display periodically to update the uptime counter."""
        if (
            self._ws is not None
            and self._ws.running
            and self._ws.service_started_at
        ):
            self._display()

    def _msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#detail_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    def action_edit(self) -> None:
        if self._ws is None:
            return
        self.run_worker(self._do_edit, exit_on_error=False)

    async def _do_edit(self) -> None:
        from .workspace_form import EditWorkspaceScreen  # noqa: allow-deferred-import

        state = self.app.tui_state
        try:
            data = await asyncio.to_thread(state.list_images)
            default = data.get("default", "") or ""
            allowed = list(data.get("allowed") or [])
        except Exception:
            default, allowed = "", []
        try:
            allow_autostart = await asyncio.to_thread(state.allow_autostart)
        except Exception:
            allow_autostart = False
        self.app.push_screen(
            EditWorkspaceScreen(
                workspace=self._ws,
                allowed=allowed,
                default=default,
                allow_autostart=allow_autostart,
            ),
            self._on_edited,
        )

    def _on_edited(self, result: str | bool | None) -> None:
        if isinstance(result, str):
            self._name = result
        if result:
            self.run_worker(self._reload_after_edit, exit_on_error=False)

    async def _reload_after_edit(self) -> None:
        from .main import MainScreen  # noqa: allow-deferred-import

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
        if self._ws is None:
            return
        etype = event.get("type")
        ws_id = str(getattr(self._ws, "id", "") or "")
        eid = str(event.get("workspace_id") or "")
        if eid and ws_id and eid != ws_id:
            return  # event for a different workspace
        if etype == "workspaces_changed":
            self.run_worker(self._reload_on_status, exit_on_error=False)
            return
        if etype == "terminals_changed":
            # A terminal was added / removed / renamed from another surface.
            # The event carries the window list (like the Flutter UI's
            # terminal_windows push), so update directly instead of
            # re-enumerating via a terminal_start round-trip (#1894).
            windows = event.get("windows")
            if isinstance(windows, list):
                # Adopt the pushed list verbatim. Events are serialized
                # per status-WS connection, so out-of-order arrival (a
                # close broadcast beating its create) is not expected;
                # the payload type check above also makes the push path
                # resilient to a malformed payload (fall back to fetch).
                self._terminals = windows
                self._render_terminals()
            else:
                # No payload (older server) or malformed -- fall back to a
                # fetch, preserving the resilience of the old poll path.
                self.run_worker(self._load_terminals, exit_on_error=False)
            return
        if etype == "container_status":
            self._ws.running = bool(event.get("running"))
            if "service_started_at" in event:
                self._ws.service_started_at = event["service_started_at"]
        elif etype == "service_health":
            self._ws.running = bool(event.get("running", self._ws.running))
            self._ws.health = (
                "healthy" if event.get("healthy") else "unhealthy"
            )
            msg = event.get("health_message")
            if msg is not None:
                self._ws.health_message = msg
        else:
            return
        self._display()

    async def _reload_on_status(self) -> None:
        await self._load()
        if self._missing:
            self.app.pop_screen()

    # --- terminals (own) ---

    async def _load_terminals(self) -> None:
        try:
            windows = await self.app.tui_state.list_terminals(self._name)
        except Exception:
            windows = []
        self._terminals = windows or []
        self._render_terminals()

    def _render_terminals(self) -> None:
        lv = self.query_one("#term_list", ListView)
        lv.clear()
        if not self._terminals:
            lv.append(ListItem(Label(Text("(no terminals)")), name=""))
            return
        for w in self._terminals:
            idx = w.get("index", "")
            name = w.get("name") or idx
            lv.append(ListItem(Label(Text(f"{idx}  {name}")), name=str(idx)))
        # Autofocus the first terminal (#1808).
        lv.focus()
        if lv.index is None:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Suspend the TUI and spawn ``klangk shell`` for the selected terminal."""
        terminal = getattr(event.item, "name", "") or ""
        if not terminal or self._ws is None:
            return
        cmd = [sys.executable, "-m", "klangk.cli.main"]
        server = self.app.tui_state.current_url()
        if server:
            cmd += ["--server", server]
        cmd += ["shell", self._name, terminal]
        with self.app.suspend():
            subprocess.run(cmd)

    def action_delete_terminal(self) -> None:
        lv = self.query_one("#term_list", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        if not child.name:
            return
        if len(self._terminals) <= 1:
            self._msg("Can't delete the last terminal.", error=True)
            return
        index = int(child.name)
        self.run_worker(self._do_delete_terminal(index), exit_on_error=False)

    async def _do_delete_terminal(self, index: int) -> None:
        self._msg(f"Deleting terminal {index}…")
        try:
            windows = await self.app.tui_state.close_terminal(
                self._name, index
            )
        except Exception as exc:
            self._msg(f"Delete failed: {exc}", error=True)
            return
        if not windows:
            # The last terminal is protected client-side, so an empty result
            # here means the close/refresh failed — don't claim success.
            self._msg(
                "Delete failed — could not refresh terminals.", error=True
            )
            return
        self._terminals = windows
        self._render_terminals()
        self._msg(f"Deleted terminal {index}.")

    def action_new_terminal(self) -> None:
        if self._ws is None:
            return
        self.run_worker(self._do_new_terminal, exit_on_error=False)

    async def _do_new_terminal(self) -> None:
        # Pick a name that doesn't collide with existing terminal names.
        existing = {w.get("name", "") for w in self._terminals}
        for i in range(len(self._terminals), 100):
            candidate = f"term-{i}"
            if candidate not in existing:
                break
        else:
            candidate = f"term-{len(self._terminals)}"  # pragma: no cover

        self._msg(f"Creating terminal '{candidate}'…")
        try:
            windows = await self.app.tui_state.create_terminal(
                self._name, candidate
            )
        except Exception as exc:
            self._msg(f"Create failed: {exc}", error=True)
            return
        if not windows:
            self._msg(
                "Create failed — could not refresh terminals.", error=True
            )
            return
        self._terminals = windows
        self._render_terminals()
        self._msg(f"Created terminal '{candidate}'.")

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
