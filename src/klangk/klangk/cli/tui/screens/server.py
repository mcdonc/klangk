"""Server switch, add, and edit screens."""

from __future__ import annotations

import asyncio
from functools import partial

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from ...config import AliasConflictError
from ...transport import is_valid_server_spec
from .base import (
    ConfirmScreen,
    SpatialListView,
    StatusScreen,
    confirm_then,
)


class ServerSwitchScreen(StatusScreen):
    """Pick a known server alias to switch to."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        # Server-scoped keys are hidden from the Footer — their hints render
        # inline on the servers list header instead (#1872).
        Binding("e", "edit_server", "Edit", show=False),
        Binding("d", "delete_server", "Del", show=False),
    ]

    DEFAULT_CSS = """
    ServerSwitchScreen #server_header {
        height: auto;
        padding: 1 0;
    }
    ServerSwitchScreen #server_title {
        text-style: bold;
        color: $primary;
        width: auto;
    }
    ServerSwitchScreen #server_hints {
        width: 1fr;
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
                Static("Switch server", id="server_title"),
                Static(
                    "[e] edit  [d] delete", id="server_hints", markup=False
                ),
                id="server_header",
            ),
            Static("", id="switch_msg"),
            SpatialListView(id="server_options"),
            id="switch_box",
        )

    def on_mount(self) -> None:
        self._populate()

    def _populate(self) -> None:
        lv = self.query_one("#server_options", ListView)
        lv.clear()
        servers = self.app.tui_state.known_servers()
        msg = self.query_one("#switch_msg", Static)
        if not servers:
            msg.update("No servers configured. Use 'a' to add one.")
            return
        msg.update("")
        current = self.app.tui_state.current_url()
        for s in servers:
            mark = "*" if s.url == current else " "
            label = Text(
                f"{mark} {s.alias}  ({s.url})",
                overflow="ellipsis",
                no_wrap=True,
            )
            item = ListItem(Label(label), name=s.url)
            item.server_alias = s.alias
            lv.append(item)

    def action_edit_server(self) -> None:
        lv = self.query_one("#server_options", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        alias = getattr(child, "server_alias", "") or ""
        url = child.name or ""
        if not alias:
            return

        def _on_edit(result: str | bool) -> None:
            if result == "url_changed":
                self.app.server_changed()
            elif result:
                self._populate()

        self.app.push_screen(EditServerScreen(alias=alias, url=url), _on_edit)

    def action_delete_server(self) -> None:
        lv = self.query_one("#server_options", ListView)
        child = lv.highlighted_child
        if child is None:
            return
        url = child.name

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"),
            confirm_then(self, partial(self._do_delete_and_refresh, url)),
        )

    async def _do_delete_and_refresh(self, url: str) -> None:
        await asyncio.to_thread(self.app.tui_state.delete_server, url)
        self._populate()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        url = getattr(event.item, "name", "") or ""
        if url:
            self.run_worker(self._do_switch_server(url), exit_on_error=False)
        else:
            self.app.server_changed()

    async def _do_switch_server(self, url: str) -> None:
        msg = self.query_one("#switch_msg", Static)
        msg.update("Checking server…")
        status = await asyncio.to_thread(
            self.app.tui_state.validate_server_for_switch, url
        )
        if status == "unreachable":
            msg.update(
                Text(
                    "Cannot reach the server. Check that klangkd is running.",
                    style="red",
                )
            )
            return
        await asyncio.to_thread(self.app.tui_state.switch_server, url)
        if status == "auth_required":
            self.app.server_changed_needs_login()
            return
        self.app.server_changed()


# Retained for the login/auto-add path and pending #1763 (duplicate-alias
# handling); intentionally not surfaced as a MainScreen action yet.
class AddServerScreen(StatusScreen):
    """Add a new server alias and switch to it."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        yield Vertical(
            Static("Add server", classes="title"),
            Input(placeholder="Alias (e.g. prod)", id="alias"),
            Input(
                placeholder="URL (https://host or /path/to.sock)",
                id="url",
            ),
            Button("Add and switch", id="add", variant="primary"),
            Static("", id="add_msg"),
            id="add_box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self._add()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("alias", "url"):
            self._add()

    def _add(self) -> None:
        alias = self.query_one("#alias", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        msg = self.query_one("#add_msg", Static)
        if not alias or not url:
            msg.update(Text("Alias and URL are required.", style="red"))
            return
        if not is_valid_server_spec(url):
            msg.update(
                Text(
                    "URL must be http(s)://host or an absolute socket"
                    " path (/...).",
                    style="red",
                )
            )
            return
        self.run_worker(self._do_add_server(alias, url), exit_on_error=False)

    async def _do_add_server(self, alias: str, url: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.add_server, alias, url)
        except AliasConflictError:
            msg = self.query_one("#add_msg", Static)
            msg.update(
                Text(
                    f"Alias '{alias}' already exists. Choose a"
                    " different name or edit the existing entry.",
                    style="red",
                )
            )
            return
        self.app.server_changed()


class EditServerScreen(ModalScreen):
    """Edit an existing server alias and/or URL (#1762)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, alias: str, url: str) -> None:
        super().__init__()
        self._old_alias = alias
        self._old_url = url

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(Text(f"Edit server: {self._old_alias}"), classes="title"),
            Horizontal(
                Static("Alias"),
                Input(value=self._old_alias, id="alias"),
                classes="field-row",
            ),
            Horizontal(
                Static("URL"),
                Input(value=self._old_url, id="url"),
                classes="field-row",
            ),
            Static("", id="edit_srv_msg"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save", variant="primary"),
                classes="actions",
            ),
            id="edit_srv_box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("alias", "url"):
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _save(self) -> None:
        alias = self.query_one("#alias", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        msg = self.query_one("#edit_srv_msg", Static)
        if not alias or not url:
            msg.update(Text("Alias and URL are required.", style="red"))
            return
        if not is_valid_server_spec(url):
            msg.update(
                Text(
                    "URL must be http(s)://host or an absolute socket"
                    " path (/...).",
                    style="red",
                )
            )
            return
        self.run_worker(self._do_save(alias, url), exit_on_error=False)

    async def _do_save(self, alias: str, url: str) -> None:
        try:
            ok = await asyncio.to_thread(
                self.app.tui_state.update_server,
                self._old_alias,
                alias,
                url,
            )
        except Exception as exc:
            self.query_one("#edit_srv_msg", Static).update(
                Text(str(exc), style="red")
            )
            return
        if not ok:
            self.query_one("#edit_srv_msg", Static).update(
                Text("Server not found.", style="red")
            )
            return
        # Signal whether server_changed() should follow (URL changed →
        # the server list and main screen need a full refresh).
        self.dismiss("url_changed" if url != self._old_url else True)
