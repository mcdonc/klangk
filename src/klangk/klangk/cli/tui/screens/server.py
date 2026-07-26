"""Server switch, add, and edit screens."""

from __future__ import annotations

import asyncio

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from ...config import AliasConflictError
from ...transport import is_valid_server_spec
from ._base import ConfirmScreen, SpatialListView


class ServerSwitchScreen(Screen):
    """Pick a known server alias to switch to."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("e", "edit_server", "Edit"),
        ("d", "delete_server", "Delete"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Vertical(
            Static("Switch server", classes="title"),
            Static("", id="switch_msg"),
            SpatialListView(id="server_options"),
            id="switch_box",
        )
        yield Footer()

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

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(
                    self._do_delete_and_refresh(url), exit_on_error=False
                )

        self.app.push_screen(
            ConfirmScreen(f"Delete server {url}?"), _on_confirm
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
                "[red]Cannot reach the server. "
                "Check that klangkd is running.[/red]"
            )
            return
        await asyncio.to_thread(self.app.tui_state.switch_server, url)
        if status == "auth_required":
            self.app.server_changed_needs_login()
            return
        self.app.server_changed()


# Retained for the login/auto-add path and pending #1763 (duplicate-alias
# handling); intentionally not surfaced as a MainScreen action yet.
class AddServerScreen(Screen):
    """Add a new server alias and switch to it."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
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
        yield Footer()

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
            msg.update("[red]Alias and URL are required.[/red]")
            return
        if not is_valid_server_spec(url):
            msg.update(
                "[red]URL must be http(s)://host or an absolute socket"
                " path (/...).[/red]"
            )
            return
        self.run_worker(self._do_add_server(alias, url), exit_on_error=False)

    async def _do_add_server(self, alias: str, url: str) -> None:
        try:
            await asyncio.to_thread(self.app.tui_state.add_server, alias, url)
        except AliasConflictError:
            msg = self.query_one("#add_msg", Static)
            msg.update(
                f"[red]Alias '{alias}' already exists. Choose a"
                " different name or edit the existing entry.[/red]"
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
            msg.update("[red]Alias and URL are required.[/red]")
            return
        if not is_valid_server_spec(url):
            msg.update(
                "[red]URL must be http(s)://host or an absolute socket"
                " path (/...).[/red]"
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
                "[red]Server not found.[/red]"
            )
            return
        # Signal whether server_changed() should follow (URL changed →
        # the server list and main screen need a full refresh).
        self.dismiss("url_changed" if url != self._old_url else True)
