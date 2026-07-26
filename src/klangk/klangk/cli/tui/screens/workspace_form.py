"""Workspace create and edit forms."""

from __future__ import annotations

import asyncio

import httpx

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Footer,
    Header,
    Input,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from ...client import AuthError, Workspace
from ...env import validate_env_entry
from ...mount import (
    validate_allowed_domain_spec,
    validate_mount_spec,
)
from ._base import ConfirmScreen, NonFocusableVerticalScroll, TabSkipMixin


class CreateWorkspaceScreen(TabSkipMixin, Screen):
    """Full-screen workspace create form (parity with Flutter
    ``CreateWorkspaceDialog``).

    Fields, top to bottom: name, container image (``Select`` populated
    from ``/api/v1/images``), a mounts list editor, an env list editor,
    an optional service shell command, an optional health-check command,
    and — only when the server permits it — an auto-start checkbox.
    Mounts/env are validated client-side (``validate_mount_spec`` /
    ``validate_env_entry``) exactly as the Flutter dialog and the CLI
    ``create`` command do.

    Images and the ``allow_autostart`` flag are fetched by the caller
    (``MainScreen.action_create``) and passed in, because ``self.app`` is
    not available until the screen is mounted.
    """

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    _TAB_ORDER = [
        "name",
        "image",
        "mount_input",
        "env_input",
        "allow_input",
        "auto_start",
        "cancel",
        "create",
    ]
    _LIST_TO_INPUT = {
        "mount_list": "mount_input",
        "env_list": "env_input",
        "allow_list": "allow_input",
    }

    def __init__(
        self,
        *,
        allowed: list[str],
        default: str,
        allow_autostart: bool,
    ) -> None:
        super().__init__()
        self._allowed = list(allowed)
        self._default = default or ""
        self._allow_autostart = bool(allow_autostart)
        self._mounts: list[str] = []
        self._env: dict[str, str] = {}
        self._allowed_domains: list[str] = []
        if self._allowed:
            # Select tuples are (prompt, value). Prompts are rich Text so an
            # image name containing brackets can't trigger markup parsing.
            self._select_options = [(Text(img), img) for img in self._allowed]
            self._select_value = (
                self._default if self._default in self._allowed else None
            )
        else:
            # Couldn't list images — offer a single inert placeholder so the
            # user can still create; the server applies its default image.
            self._select_options = [
                (Text("(server default)"), "(server default)")
            ]
            self._select_value = "(server default)"

    def compose(self) -> ComposeResult:
        if self._select_value is not None:
            image_select = Select(
                self._select_options, value=self._select_value, id="image"
            )
        else:
            # No valid default to preselect — leave the picker unselected
            # (the server applies its default image if none is chosen).
            image_select = Select(self._select_options, id="image")
        yield Header(show_clock=False)
        yield NonFocusableVerticalScroll(
            Static("New workspace", classes="title"),
            Static("", id="create_msg"),
            Horizontal(Static("Name"), Input(id="name"), classes="field-row"),
            Horizontal(Static("Image"), image_select, classes="field-row"),
            Static(
                "Mounts  (source:/container/path[:opts])",
                classes="editor-label",
            ),
            Horizontal(
                Input(
                    id="mount_input",
                    placeholder="/host/path:/container/path",
                ),
                Button("Add", id="add_mount"),
                Button("Remove", id="rm_mount"),
            ),
            OptionList(id="mount_list", classes="editor-list"),
            Static("Environment  (KEY=VALUE)", classes="editor-label"),
            Horizontal(
                Input(id="env_input", placeholder="KEY=VALUE"),
                Button("Add", id="add_env"),
                Button("Remove", id="rm_env"),
            ),
            OptionList(id="env_list", classes="editor-list"),
            Static(
                "Allowed Domains  (host or host:port; empty = unrestricted)",
                classes="editor-label",
            ),
            Horizontal(
                Input(id="allow_input", placeholder="github.com:443"),
                Button("Add", id="add_allow"),
                Button("Remove", id="rm_allow"),
            ),
            OptionList(id="allow_list", classes="editor-list"),
            Collapsible(
                Horizontal(
                    Static("Command"),
                    Input(id="command"),
                    classes="field-row",
                ),
                Horizontal(
                    Static("Health"),
                    Input(id="health_check"),
                    classes="field-row",
                ),
                title="Advanced",
            ),
            Checkbox("Auto start", id="auto_start"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Create", id="create", variant="primary"),
                classes="actions",
            ),
            id="create_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()

    def _skip_editors_on_tab(self) -> None:
        """Editor buttons stay out of the Tab cycle (#1783).

        Add is reachable via Enter in the input; Remove via mouse click.
        Lists stay focusable for Delete/"e" keyboard actions but Tab skips
        them via :class:`TabSkipMixin`.
        """
        for wid in (
            "add_mount",
            "rm_mount",
            "add_env",
            "rm_env",
            "add_allow",
            "rm_allow",
        ):
            self.query_one(f"#{wid}").can_focus = False

    def _msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#create_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- mounts list editor ---

    def _render_mounts(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        ol.clear_options()
        if not self._mounts:
            ol.add_option(Option(Text("(no mounts)"), id="", disabled=True))
            return
        for i, m in enumerate(self._mounts):
            ol.add_option(Option(Text(m), id=f"m{i}"))

    def _add_mount(self) -> None:
        inp = self.query_one("#mount_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_mount_spec(v)
        if err:
            self._msg(err, error=True)
            return
        self._mounts.append(v)
        inp.value = ""
        self._msg("")
        self._render_mounts()

    def _remove_mount(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._mounts):
            return
        del self._mounts[idx]
        self._render_mounts()

    # --- env list editor ---

    def _render_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        ol.clear_options()
        if not self._env:
            ol.add_option(Option(Text("(no env vars)"), id="", disabled=True))
            return
        for i, (k, val) in enumerate(self._env.items()):
            ol.add_option(Option(Text(f"{k}={val}"), id=f"e{i}"))

    def _add_env(self) -> None:
        inp = self.query_one("#env_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_env_entry(v)
        if err:
            self._msg(err, error=True)
            return
        key, _, value = v.partition("=")
        self._env[key] = value
        inp.value = ""
        self._msg("")
        self._render_env()

    def _remove_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        del self._env[keys[idx]]
        self._render_env()

    # --- allowed-domains list editor (#1745) ---

    def _render_allowed_domains(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        ol.clear_options()
        if not self._allowed_domains:
            ol.add_option(Option(Text("(unrestricted)"), id="", disabled=True))
            return
        for i, d in enumerate(self._allowed_domains):
            ol.add_option(Option(Text(d), id=f"a{i}"))

    def _add_allowed_domain(self) -> None:
        inp = self.query_one("#allow_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_allowed_domain_spec(v)
        if err:
            self._msg(err, error=True)
            return
        if v not in self._allowed_domains:
            self._allowed_domains.append(v)
        inp.value = ""
        self._msg("")
        self._render_allowed_domains()

    def _remove_allowed_domain(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._allowed_domains):
            return
        del self._allowed_domains[idx]
        self._render_allowed_domains()

    # --- create ---

    def _create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._msg("Name is required.", error=True)
            return
        sel = self.query_one("#image", Select)
        val = sel.value
        # Send only a real, non-default selection. When the server's default
        # isn't in the allowed list we start unselected (Select.BLANK), so an
        # untouched picker omits the image — matching the Flutter dialog.
        if (
            val is Select.BLANK
            or val is Select.NULL
            or not self._allowed
            or val == self._default
        ):
            image = None
        else:
            image = val
        command = self.query_one("#command", Input).value.strip() or None
        health_check = (
            self.query_one("#health_check", Input).value.strip() or None
        )
        auto = (
            self._allow_autostart
            and self.query_one("#auto_start", Checkbox).value
        )
        mounts = list(self._mounts) or None
        env = dict(self._env) or None
        allowed_domains = list(self._allowed_domains) or None
        self.run_worker(
            self._do_create_workspace(
                name,
                image,
                command,
                auto,
                mounts,
                env,
                health_check,
                allowed_domains,
            ),
            exit_on_error=False,
        )

    async def _do_create_workspace(
        self,
        name,
        image,
        command,
        auto,
        mounts,
        env,
        health_check,
        allowed_domains,
    ) -> None:
        try:
            ws = await asyncio.to_thread(
                self.app.tui_state.create_workspace,
                name,
                image=image,
                service_command=command,
                auto_start=auto,
                mounts=mounts,
                env=env,
                health_check=health_check,
                allowed_domains=allowed_domains,
            )
        except AuthError:
            self._msg("Session expired — please log in again.", error=True)
            return
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except (ValueError, KeyError):
                detail = exc.response.text or str(exc)
            self._msg(f"Failed to create: {detail}", error=True)
            return
        except Exception as exc:
            self._msg(f"Failed to create: {exc}", error=True)
            return
        self.dismiss(ws.name)

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel":
            self.dismiss(None)
        elif bid == "create":
            self._create()
        elif bid == "add_mount":
            self._add_mount()
        elif bid == "rm_mount":
            self._remove_mount()
        elif bid == "add_env":
            self._add_env()
        elif bid == "rm_env":
            self._remove_env()
        elif bid == "add_allow":
            self._add_allowed_domain()
        elif bid == "rm_allow":
            self._remove_allowed_domain()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        eid = event.input.id
        if eid == "mount_input":
            self._add_mount()
        elif eid == "env_input":
            self._add_env()
        elif eid == "allow_input":
            self._add_allowed_domain()
        elif eid in ("name", "command", "health_check"):
            self._create()


class EditWorkspaceScreen(TabSkipMixin, Screen):
    """Full-screen workspace edit form (parity with Flutter
    ``WorkspaceSettingsPanel``).

    Like :class:`CreateWorkspaceScreen` but pre-populated from an existing
    workspace, saving via a partial ``PUT``. Saving a change to a
    container-create-time field (image / mounts / env / service_command /
    allowed_domains) on a *running* workspace prompts a "restart needed to
    apply" offer (#1778, #1749); ``setup_state`` / ``health_check`` propagate
    live and never trigger it.
    """

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("delete", "remove_item", "Remove"),
        ("e", "edit_item", "Edit"),
    ]

    _TAB_ORDER = [
        "name",
        "image",
        "mount_input",
        "env_input",
        "allow_input",
        "auto_start",
        "cancel",
        "save",
    ]
    _LIST_TO_INPUT = {
        "mount_list": "mount_input",
        "env_list": "env_input",
        "allow_list": "allow_input",
    }

    def __init__(
        self,
        *,
        workspace: Workspace,
        allowed: list[str],
        default: str,
        allow_autostart: bool,
    ) -> None:
        super().__init__()
        self._ws = workspace
        self._allow_autostart = bool(allow_autostart)
        self._default = default or ""
        self._mounts: list[str] = list(workspace.mounts or [])
        self._env: dict[str, str] = dict(workspace.env or {})
        self._allowed_domains: list[str] = list(
            workspace.allowed_domains or []
        )
        # In-place editor state (#1778): when set, the next Add *replaces*
        # the item at this index/key instead of appending. Cleared on Add.
        self._editing_mount: int | None = None
        self._editing_env: str | None = None
        self._editing_allow: int | None = None
        # Image picker: include the workspace's current image even if it
        # isn't in the server's allowed list, pre-selected (untouched = no
        # change). Prompts are rich Text so bracket-laden names can't crash.
        cur = workspace.image or ""
        opts = list(allowed)
        if cur and cur not in opts:
            opts.append(cur)
        if opts:
            self._select_options = [(Text(i), i) for i in opts]
            self._select_value = (
                cur if cur in opts else (opts[0] if opts else None)
            )
        else:
            self._select_options = [(Text("(none)"), "(none)")]
            self._select_value = "(none)"

    def compose(self) -> ComposeResult:
        if self._select_value is not None:
            image_select = Select(
                self._select_options, value=self._select_value, id="image"
            )
        else:  # pragma: no cover
            image_select = Select(self._select_options, id="image")
        yield Header(show_clock=False)
        yield NonFocusableVerticalScroll(
            Static(Text(f"Edit workspace: {self._ws.name}"), classes="title"),
            Static("", id="edit_msg"),
            Horizontal(
                Static("Name"),
                Input(value=self._ws.name or "", id="name"),
                classes="field-row",
            ),
            Horizontal(Static("Image"), image_select, classes="field-row"),
            Static(
                "Mounts  (source:/container/path[:opts])",
                classes="editor-label",
            ),
            Horizontal(
                Input(
                    id="mount_input",
                    placeholder="/host/path:/container/path",
                ),
                Button("Add", id="add_mount"),
                Button("Remove", id="rm_mount"),
            ),
            OptionList(id="mount_list", classes="editor-list"),
            Static("Environment  (KEY=VALUE)", classes="editor-label"),
            Horizontal(
                Input(id="env_input", placeholder="KEY=VALUE"),
                Button("Add", id="add_env"),
                Button("Remove", id="rm_env"),
            ),
            OptionList(id="env_list", classes="editor-list"),
            Static(
                "Allowed Domains  (host or host:port; empty = unrestricted)",
                classes="editor-label",
            ),
            Horizontal(
                Input(id="allow_input", placeholder="github.com:443"),
                Button("Add", id="add_allow"),
                Button("Remove", id="rm_allow"),
            ),
            OptionList(id="allow_list", classes="editor-list"),
            Collapsible(
                Horizontal(
                    Static("Command"),
                    Input(value=self._ws.service_command or "", id="command"),
                    classes="field-row",
                ),
                Horizontal(
                    Static("Health"),
                    Input(
                        value=self._ws.health_check or "", id="health_check"
                    ),
                    classes="field-row",
                ),
                title="Advanced",
            ),
            Checkbox("Auto start", value=self._ws.auto_start, id="auto_start"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save", variant="primary"),
                classes="actions",
            ),
            id="edit_box",
        )
        yield Footer()

    def on_mount(self) -> None:
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()

    def _skip_editors_on_tab(self) -> None:
        """Editor buttons stay out of the Tab cycle (#1783).

        Add is reachable via Enter in the input; Remove via mouse click.
        Lists stay focusable for Delete/"e" keyboard actions but Tab skips
        them via :class:`TabSkipMixin`.
        """
        for wid in (
            "add_mount",
            "rm_mount",
            "add_env",
            "rm_env",
            "add_allow",
            "rm_allow",
        ):
            self.query_one(f"#{wid}").can_focus = False

    def _msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#edit_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- list editors: add / remove / in-place edit (#1778) ---

    def _render_mounts(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        ol.clear_options()
        if not self._mounts:
            ol.add_option(Option(Text("(no mounts)"), id="", disabled=True))
            return
        for i, m in enumerate(self._mounts):
            ol.add_option(Option(Text(m), id=f"m{i}"))

    def _add_mount(self) -> None:
        inp = self.query_one("#mount_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_mount_spec(v)
        if err:
            self._msg(err, error=True)
            return
        idx = self._editing_mount
        if idx is not None and 0 <= idx < len(self._mounts):
            self._mounts[idx] = v
            self._editing_mount = None
        else:
            self._mounts.append(v)
        inp.value = ""
        self._msg("")
        self._render_mounts()

    def _remove_mount(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._mounts):
            return
        del self._mounts[idx]
        self._editing_mount = None
        self._render_mounts()

    def _render_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        ol.clear_options()
        if not self._env:
            ol.add_option(Option(Text("(no env vars)"), id="", disabled=True))
            return
        for i, (k, val) in enumerate(self._env.items()):
            ol.add_option(Option(Text(f"{k}={val}"), id=f"e{i}"))

    def _add_env(self) -> None:
        inp = self.query_one("#env_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_env_entry(v)
        if err:
            self._msg(err, error=True)
            return
        key, _, value = v.partition("=")
        old = self._editing_env
        if old is not None:
            self._env.pop(old, None)
            self._editing_env = None
        self._env[key] = value
        inp.value = ""
        self._msg("")
        self._render_env()

    def _remove_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        del self._env[keys[idx]]
        self._editing_env = None
        self._render_env()

    def _render_allowed_domains(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        ol.clear_options()
        if not self._allowed_domains:
            ol.add_option(Option(Text("(unrestricted)"), id="", disabled=True))
            return
        for i, d in enumerate(self._allowed_domains):
            ol.add_option(Option(Text(d), id=f"a{i}"))

    def _add_allowed_domain(self) -> None:
        inp = self.query_one("#allow_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_allowed_domain_spec(v)
        if err:
            self._msg(err, error=True)
            return
        idx = self._editing_allow
        if idx is not None and 0 <= idx < len(self._allowed_domains):
            self._allowed_domains[idx] = v
            self._editing_allow = None
        elif v not in self._allowed_domains:
            self._allowed_domains.append(v)
        inp.value = ""
        self._msg("")
        self._render_allowed_domains()

    def _remove_allowed_domain(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._allowed_domains):
            return
        del self._allowed_domains[idx]
        self._editing_allow = None
        self._render_allowed_domains()

    # --- in-place edit: load the highlighted item into the input (#1778) ---

    def _edit_mount(self) -> None:
        ol = self.query_one("#mount_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._mounts):
            return
        self._editing_mount = idx
        inp = self.query_one("#mount_input", Input)
        inp.value = self._mounts[idx]
        inp.focus()
        self._msg("Editing mount — press Add to update.")

    def _edit_env(self) -> None:
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        key = keys[idx]
        self._editing_env = key
        inp = self.query_one("#env_input", Input)
        inp.value = f"{key}={self._env[key]}"
        inp.focus()
        self._msg("Editing env var — press Add to update.")

    def _edit_allowed_domain(self) -> None:
        ol = self.query_one("#allow_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._allowed_domains):
            return
        self._editing_allow = idx
        inp = self.query_one("#allow_input", Input)
        inp.value = self._allowed_domains[idx]
        inp.focus()
        self._msg("Editing allowed-domain — press Add to update.")

    # --- keyboard remove/edit of the focused OptionList (#1778) ---

    def action_remove_item(self) -> None:
        fid = getattr(self.focused, "id", None) if self.focused else None
        if fid == "mount_list":
            self._remove_mount()
        elif fid == "env_list":
            self._remove_env()
        elif fid == "allow_list":
            self._remove_allowed_domain()

    def action_edit_item(self) -> None:
        fid = getattr(self.focused, "id", None) if self.focused else None
        if fid == "mount_list":
            self._edit_mount()
        elif fid == "env_list":
            self._edit_env()
        elif fid == "allow_list":
            self._edit_allowed_domain()

    # --- save ---

    def _save(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self._msg("Name is required.", error=True)
            return
        sel = self.query_one("#image", Select)
        val = sel.value
        image = val if (val and val != "(none)") else None
        command = self.query_one("#command", Input).value.strip() or None
        health_check = (
            self.query_one("#health_check", Input).value.strip() or None
        )
        auto = (
            self._allow_autostart
            and self.query_one("#auto_start", Checkbox).value
        )
        mounts = list(self._mounts) or None
        env = dict(self._env) or None
        allowed_domains = list(self._allowed_domains) or None
        body = {
            "name": name,
            "image": image,
            "service_command": command,
            "health_check": health_check,
            "auto_start": auto,
            "mounts": mounts,
            "env": env,
            "allowed_domains": allowed_domains,
        }
        ws = self._ws
        orig_mounts = list(ws.mounts or []) or None
        orig_env = dict(ws.env or {}) or None
        orig_domains = list(ws.allowed_domains or []) or None
        restart_needed = bool(ws.running) and (
            (image or None) != (ws.image or None)
            or mounts != orig_mounts
            or env != orig_env
            or (command or None) != (ws.service_command or None)
            or allowed_domains != orig_domains
        )
        self.run_worker(
            self._do_save(name, body, ws, restart_needed),
            exit_on_error=False,
        )

    async def _do_save(self, name, body, ws, restart_needed) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.update_workspace, ws.id, **body
            )
        except AuthError:
            self._msg("Session expired — please log in again.", error=True)
            return
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text or str(exc)
            self._msg(f"Failed to save: {detail}", error=True)
            return
        except Exception as exc:
            self._msg(f"Failed to save: {exc}", error=True)
            return
        if restart_needed:

            def _after(restart: bool) -> None:
                if restart:
                    self.run_worker(
                        self._do_restart_after_save(ws.name, name),
                        exit_on_error=False,
                    )
                else:
                    self.dismiss(name)

            self.app.push_screen(
                ConfirmScreen(
                    "A running container is not affected by this edit. "
                    "Restart now to apply?",
                    yes_label="Restart",
                    yes_variant="warning",
                    no_label="Skip",
                ),
                _after,
            )
        else:
            self.dismiss(name)

    async def _do_restart_after_save(self, ws_name, dismiss_name) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, ws_name
            )
        except Exception as exc:
            self._msg(f"Saved, but restart failed: {exc}", error=True)
            return
        self.dismiss(dismiss_name)

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel":
            self.dismiss(False)
        elif bid == "save":
            self._save()
        elif bid == "add_mount":
            self._add_mount()
        elif bid == "rm_mount":
            self._remove_mount()
        elif bid == "add_env":
            self._add_env()
        elif bid == "rm_env":
            self._remove_env()
        elif bid == "add_allow":
            self._add_allowed_domain()
        elif bid == "rm_allow":
            self._remove_allowed_domain()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        eid = event.input.id
        if eid == "mount_input":
            self._add_mount()
        elif eid == "env_input":
            self._add_env()
        elif eid == "allow_input":
            self._add_allowed_domain()
        elif eid in ("name", "command", "health_check"):
            self._save()
