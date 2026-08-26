"""Workspace create and edit forms."""

from __future__ import annotations

import asyncio
import math

import httpx

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)
from textual.widgets.option_list import Option

from ...client import AuthError, Workspace
from ...env import validate_env_entry
from ...mount import (
    validate_allowed_domain_spec,
    validate_mount_spec,
)
from ._base import (
    ConfirmScreen,
    NonFocusableVerticalScroll,
    StatusScreen,
    TabSkipMixin,
)


def _collect_settings(screen: Screen) -> dict | None:
    """Read the resource-limit inputs and return a settings dict, or None.

    Raises ``ValueError`` (field-named) on invalid input so the form can
    show an inline error instead of crashing the app — ``int(raw)``/``float(raw)``
    used to propagate out of the button handler (#2029 audit). The ``float``
    field also rejects NaN/Inf: the server's positive-number check
    (``f <= 0``) passes NaN, and podman later rejects ``--cpus nan`` at
    container start with a cryptic error long after submit (#2029 review).
    """
    settings: dict = {}
    raw = screen.query_one("#idle_timeout", Input).value.strip()
    if raw:
        try:
            settings["idle_timeout"] = int(raw)
        except ValueError:
            raise ValueError(
                f"Idle timeout must be a whole number of seconds: {raw!r}"
            ) from None
    raw = screen.query_one("#cpu_limit", Input).value.strip()
    if raw:
        try:
            cpu = float(raw)
        except ValueError:
            raise ValueError(
                f"CPU limit must be a number (e.g. 2.0): {raw!r}"
            ) from None
        if not math.isfinite(cpu):
            raise ValueError(
                f"CPU limit must be a finite number (e.g. 2.0): {raw!r}"
            )
        settings["cpu_limit"] = cpu
    raw = screen.query_one("#memory_limit", Input).value.strip()
    if raw:
        settings["memory_limit"] = raw
    raw = screen.query_one("#pids_limit", Input).value.strip()
    if raw:
        try:
            settings["pids_limit"] = int(raw)
        except ValueError:
            raise ValueError(
                f"PIDs limit must be a whole number: {raw!r}"
            ) from None
    raw = screen.query_one("#tmp_size", Input).value.strip()
    if raw:
        settings["tmp_size"] = raw
    return settings or None


# Egress-mode picker options for the create/edit Netfilter tab (#2409).
# The CLI is an isolated client (AGENTS.md "CLI subpackage isolation") and
# cannot import the server's EGRESS_MODE_* constants, so the mode strings are
# duplicated here -- they are part of the public HTTP API contract
# (api/workspaces.py CreateWorkspaceRequest / UpdateWorkspaceRequest).
EGRESS_MODE_INTERACTIVE = "interactive"
EGRESS_MODE_STATIC = "static"
EGRESS_MODE_ALLOW = "allow"
EGRESS_MODE_DEFAULT = EGRESS_MODE_INTERACTIVE
_EGRESS_MODE_OPTIONS = [
    (Text("interactive (ask first)"), EGRESS_MODE_INTERACTIVE),
    (Text("static (deny + record)"), EGRESS_MODE_STATIC),
    (Text("allow (default-permit)"), EGRESS_MODE_ALLOW),
]


class CreateWorkspaceScreen(TabSkipMixin, StatusScreen):
    """Full-screen workspace create form (parity with Flutter
    ``CreateWorkspaceDialog``).

    Fields are grouped under a ``TabbedContent`` with five panes — General
    (name / image / auto-start), Mounts, Environment, Netfilter (allowed
    domains), and Advanced (service command / health check) — so each
    logical category has its own tab instead of one long scroll (#1891).
    Cancel / Create and the ``#create_msg`` status line live *outside* the
    tab content so they stay visible regardless of which tab is active.

    The container image comes from a ``Select`` populated from
    ``/api/v1/images``; mounts/env are validated client-side
    (``validate_mount_spec`` / ``validate_env_entry``) exactly as the
    Flutter dialog and the CLI ``create`` command do.

    Images and the ``allow_autostart`` flag are fetched by the caller
    (``MainScreen.action_create``) and passed in, because ``self.app`` is
    not available until the screen is mounted.
    """

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    _TAB_ORDER = [
        "name",
        "image",
        "auto_start",
        "nix",
        "mount_input",
        "env_input",
        "egress_mode",
        "allow_input",
        "reject_input",
        "idle_timeout",
        "cpu_limit",
        "memory_limit",
        "pids_limit",
        "tmp_size",
        "per_handle_home",
        "command",
        "health_check",
        "cancel",
        "create",
    ]
    _LIST_TO_INPUT = {
        "mount_list": "mount_input",
        "env_list": "env_input",
        "allow_list": "allow_input",
        "reject_list": "reject_input",
    }
    # Spatial nav around the form tab strip (#1891): the first focusable
    # field of each pane — Down from the strip lands here, Up returns.
    _PANE_FIRST_FIELD = {
        "general_pane": "name",
        "mounts_pane": "mount_input",
        "env_pane": "env_input",
        "netfilter_pane": "egress_mode",
        "resources_pane": "idle_timeout",
        "advanced_pane": "command",
    }

    def __init__(
        self,
        *,
        allowed: list[str],
        default: str,
        allow_autostart: bool,
        default_allowed_domains: list[str] | None = None,
        default_rejected_domains: list[str] | None = None,
        nix_available: bool = False,
        default_per_handle_home: bool | None = None,
    ) -> None:
        super().__init__()
        self._allowed = list(allowed)
        self._default = default or ""
        self._allow_autostart = bool(allow_autostart)
        # #2233: per-workspace nix toggle (Mount /nix dir). Shown only when
        # the server has a nix backend, matching the create dialog.
        self._nix_available = bool(nix_available)
        self._mounts: list[str] = []
        self._env: dict[str, str] = {}
        # Seed the Netfilter list with the deploy default
        # (KLANGKD_NETFILTER_DEFAULT_DOMAINS) so the TUI create form matches
        # the Flutter dialog — a starting set the user can edit/remove (#1931).
        self._allowed_domains: list[str] = list(default_allowed_domains or [])
        # #2386: the static deny-list (rejected_domains), mirroring the
        # allow-list editor. No deploy default (only allow has one).
        self._rejected_domains: list[str] = list(
            default_rejected_domains or []
        )
        # #2721: home-layout default (KLANGKD_PER_HANDLE_HOME) fetched by
        # the caller from /config, so the checkbox starts on the server's
        # default — an untouched form submits exactly what a silent POST
        # would get. None = unknown (fetch failed): the checkbox is hidden
        # and the field omitted, so the server applies its own default —
        # never a silently forced layout (#2737 review).
        self._default_per_handle_home = default_per_handle_home
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
        # Header / status dock (StatusBar + Footer) come from StatusScreen
        # (#2689).
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        if self._select_value is not None:
            image_select = Select(
                self._select_options, value=self._select_value, id="image"
            )
        else:
            # No valid default to preselect — leave the picker unselected
            # (the server applies its default image if none is chosen).
            image_select = Select(self._select_options, id="image")
        with NonFocusableVerticalScroll(id="create_box"):
            yield Static("New workspace", classes="title")
            yield Static("", id="create_msg")
            with TabbedContent(id="form_tabs"):
                with TabPane("General", id="general_pane"):
                    yield Horizontal(
                        Static("Name"), Input(id="name"), classes="field-row"
                    )
                    yield Horizontal(
                        Static("Image"), image_select, classes="field-row"
                    )
                    yield Checkbox("Auto start", id="auto_start")
                    yield Checkbox("Mount /nix dir", id="nix")
                with TabPane("Mounts", id="mounts_pane"):
                    yield Static(
                        "Mounts  (source:/container/path[:opts])",
                        classes="editor-label",
                    )
                    yield Horizontal(
                        Input(
                            id="mount_input",
                            placeholder="/host/path:/container/path",
                        ),
                        Button("Add", id="add_mount"),
                        Button("Remove", id="rm_mount"),
                    )
                    yield OptionList(id="mount_list", classes="editor-list")
                with TabPane("Environment", id="env_pane"):
                    yield Static(
                        "Environment  (KEY=VALUE)", classes="editor-label"
                    )
                    yield Horizontal(
                        Input(id="env_input", placeholder="KEY=VALUE"),
                        Button("Add", id="add_env"),
                        Button("Remove", id="rm_env"),
                    )
                    yield OptionList(id="env_list", classes="editor-list")
                with TabPane("Netfilter", id="netfilter_pane"):
                    yield Horizontal(
                        Static("Egress mode"),
                        Select(
                            _EGRESS_MODE_OPTIONS,
                            value=EGRESS_MODE_DEFAULT,
                            id="egress_mode",
                        ),
                        classes="field-row",
                    )
                    yield Static(
                        "Allowed Domains  "
                        "(host or host:port; empty = unrestricted)",
                        classes="editor-label",
                    )
                    yield Horizontal(
                        Input(id="allow_input", placeholder="github.com:443"),
                        Button("Add", id="add_allow"),
                        Button("Remove", id="rm_allow"),
                    )
                    yield OptionList(id="allow_list", classes="editor-list")
                    yield Static(
                        "Rejected Domains  "
                        "(host or host:port; NXDOMAIN'd unconditionally)",
                        classes="editor-label",
                    )
                    yield Horizontal(
                        Input(
                            id="reject_input", placeholder="evil.example.com"
                        ),
                        Button("Add", id="add_reject"),
                        Button("Remove", id="rm_reject"),
                    )
                    yield OptionList(id="reject_list", classes="editor-list")
                with TabPane("Resources", id="resources_pane"):
                    yield Horizontal(
                        Static("Idle timeout (s)"),
                        Input(
                            id="idle_timeout",
                            placeholder="seconds (0 = never)",
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("CPU limit"),
                        Input(id="cpu_limit", placeholder="e.g. 2.0"),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("Memory limit"),
                        Input(
                            id="memory_limit",
                            placeholder="e.g. 4g, 512m",
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("PIDs limit"),
                        Input(id="pids_limit", placeholder="e.g. 512"),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("/tmp size"),
                        Input(id="tmp_size", placeholder="e.g. 2g, 512m"),
                        classes="field-row",
                    )
                with TabPane("Advanced", id="advanced_pane"):
                    yield Checkbox(
                        "Per-handle home (off = shared /home/klangk)",
                        value=bool(self._default_per_handle_home),
                        id="per_handle_home",
                    )
                    yield Horizontal(
                        Static("Command"),
                        Input(id="command"),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("Health"),
                        Input(id="health_check"),
                        classes="field-row",
                    )
            yield Horizontal(
                Button("Cancel", id="cancel"),
                Button("Create", id="create", variant="primary"),
                classes="actions",
            )

    def on_mount(self) -> None:
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        # The nix toggle is shown only when the server has a nix backend
        # (#2233); otherwise hidden + disabled so Tab skips it.
        nix_cb = self.query_one("#nix", Checkbox)
        nix_cb.display = self._nix_available
        nix_cb.disabled = not self._nix_available
        # The home-layout toggle is hidden when the deploy default is
        # unknown (fetch failure): an offered choice we can't pre-reflect
        # would pin a possibly-wrong value, so the field is omitted and
        # the server applies its own default (#2737 review).
        phh_cb = self.query_one("#per_handle_home", Checkbox)
        phh_cb.display = self._default_per_handle_home is not None
        phh_cb.disabled = self._default_per_handle_home is None
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()
        self._render_rejected_domains()
        # General tab is active on entry — focus Name so the user can start
        # typing immediately (the tab strip is one Up away, #1891).
        self.query_one("#name", Input).focus()

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
            "add_reject",
            "rm_reject",
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

    # --- rejected-domains list editor (#2386, mirrors allowed-domains) ---

    def _render_rejected_domains(self) -> None:
        ol = self.query_one("#reject_list", OptionList)
        ol.clear_options()
        if not self._rejected_domains:
            ol.add_option(Option(Text("(none)"), id="", disabled=True))
            return
        for i, d in enumerate(self._rejected_domains):
            ol.add_option(Option(Text(d), id=f"r{i}"))

    def _add_rejected_domain(self) -> None:
        inp = self.query_one("#reject_input", Input)
        v = inp.value.strip()
        if not v:
            return
        # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
        err = validate_allowed_domain_spec(v, allow_cidr=False)
        if err:
            self._msg(err, error=True)
            return
        if v not in self._rejected_domains:
            self._rejected_domains.append(v)
        inp.value = ""
        self._msg("")
        self._render_rejected_domains()

    def _remove_rejected_domain(self) -> None:
        ol = self.query_one("#reject_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._rejected_domains):
            return
        del self._rejected_domains[idx]
        self._render_rejected_domains()

    # --- tab + keyboard navigation (#1891) ---

    def _active_tab(self) -> str:
        """The id of the currently active form tab pane."""
        return self.query_one("#form_tabs", TabbedContent).active

    def _focus_first_in_active_pane(self) -> None:
        """Focus the first field of the active tab pane (Down from strip)."""
        first = self._PANE_FIRST_FIELD.get(self._active_tab(), "name")
        self.query_one(f"#{first}").focus()

    def on_key(self, event) -> None:
        # Spatial nav around the form tab strip (AGENTS.md "TUI spatial
        # navigation"): Down from the strip drops into the active pane's
        # first field; Up from that field returns to the strip. Left/Right
        # on the strip switch tabs (Textual built-in). Tab/Shift-Tab still
        # cycles fields via TabSkipMixin (#1783).
        focused = self.focused
        if isinstance(focused, Tabs) and event.key == "down":
            event.stop()
            self._focus_first_in_active_pane()
            return
        if event.key == "up":
            fid = getattr(focused, "id", None)
            first = self._PANE_FIRST_FIELD.get(self._active_tab(), "name")
            if fid == first:
                event.stop()
                self.query_one(Tabs).focus()
                return
        super().on_key(event)

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
        # #2721: sent whenever the deploy default was known — the
        # checkbox's initial state IS the server default, so an untouched
        # form submits it unchanged. Unknown default (hidden checkbox):
        # omitted, and the server applies its own.
        phh_cb = self.query_one("#per_handle_home", Checkbox)
        per_handle_home = phh_cb.value if phh_cb.display else None
        mounts = list(self._mounts) or None
        env = dict(self._env) or None
        allowed_domains = list(self._allowed_domains) or None
        rejected_domains = list(self._rejected_domains) or None
        egress_mode = self.query_one("#egress_mode", Select).value
        try:
            settings = _collect_settings(self)
        except ValueError as exc:
            self._msg(str(exc), error=True)
            return
        if self._nix_available and self.query_one("#nix", Checkbox).value:
            settings = {**(settings or {}), "nix": True}
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
                rejected_domains,
                settings,
                egress_mode,
                per_handle_home,
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
        rejected_domains,
        settings,
        egress_mode,
        per_handle_home,
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
                rejected_domains=rejected_domains,
                settings=settings,
                egress_mode=egress_mode,
                per_handle_home=per_handle_home,
            )
        except AuthError:
            self.app.session_expired()
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
        elif bid == "add_reject":
            self._add_rejected_domain()
        elif bid == "rm_reject":
            self._remove_rejected_domain()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        eid = event.input.id
        if eid == "mount_input":
            self._add_mount()
        elif eid == "env_input":
            self._add_env()
        elif eid == "allow_input":
            self._add_allowed_domain()
        elif eid == "reject_input":
            self._add_rejected_domain()
        elif eid in (
            "name",
            "command",
            "health_check",
            "idle_timeout",
            "cpu_limit",
            "memory_limit",
            "pids_limit",
            "tmp_size",
        ):
            self._create()


class EditWorkspaceScreen(TabSkipMixin, StatusScreen):
    """Full-screen workspace edit form (parity with Flutter
    ``WorkspaceSettingsPanel``).

    Fields are grouped under a ``TabbedContent`` with five panes — General
    (name / image / auto-start), Mounts, Environment, Netfilter (allowed
    domains), and Advanced (service command / health check) — so each
    logical category has its own tab instead of one long scroll (#1891).
    Save / Cancel, the status line (``#edit_msg``), and the restart-needed
    prompt live *outside* the tab content so they stay visible regardless
    of which tab is active.

    Pre-populated from an existing workspace, saving via a partial ``PUT``.
    Saving a change to a container-create-time field (image / mounts / env /
    service_command / allowed_domains) on a *running* workspace prompts a
    "restart needed to apply" offer (#1778, #1749); ``setup_state`` /
    ``health_check`` propagate live and never trigger it.
    """

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("delete", "remove_item", "Remove"),
        ("e", "edit_item", "Edit"),
    ]

    _TAB_ORDER = [
        "name",
        "image",
        "auto_start",
        "nix",
        "mount_input",
        "env_input",
        "egress_mode",
        "allow_input",
        "reject_input",
        "idle_timeout",
        "cpu_limit",
        "memory_limit",
        "pids_limit",
        "tmp_size",
        "per_handle_home",
        "command",
        "health_check",
        "cancel",
        "save",
    ]
    _LIST_TO_INPUT = {
        "mount_list": "mount_input",
        "env_list": "env_input",
        "allow_list": "allow_input",
        "reject_list": "reject_input",
    }
    # Spatial nav around the form tab strip (#1891): the first focusable
    # field of each pane — Down from the strip lands here, Up returns.
    _PANE_FIRST_FIELD = {
        "general_pane": "name",
        "mounts_pane": "mount_input",
        "env_pane": "env_input",
        "netfilter_pane": "egress_mode",
        "resources_pane": "idle_timeout",
        "advanced_pane": "command",
    }
    # Delete/'e' act on the list under the active tab (#1891). The
    # netfilter pane holds two lists (allow + reject, #2386), so it is NOT
    # in this table -- :meth:`_list_handlers` dispatches it on focus instead.
    _PANE_LIST_HANDLER = {
        "mounts_pane": ("_remove_mount", "_edit_mount"),
        "env_pane": ("_remove_env", "_edit_env"),
    }

    def __init__(
        self,
        *,
        workspace: Workspace,
        allowed: list[str],
        default: str,
        allow_autostart: bool,
        nix_available: bool = False,
    ) -> None:
        super().__init__()
        self._ws = workspace
        self._allow_autostart = bool(allow_autostart)
        self._default = default or ""
        # #2233: per-workspace nix toggle (Mount /nix dir). Shown only when
        # the server has a nix backend, matching the edit panel / create dialog.
        self._nix_available = bool(nix_available)
        self._mounts: list[str] = list(workspace.mounts or [])
        self._env: dict[str, str] = dict(workspace.env or {})
        self._allowed_domains: list[str] = list(
            workspace.allowed_domains or []
        )
        # #2386: the static deny-list, seeded from the workspace.
        self._rejected_domains: list[str] = list(
            workspace.rejected_domains or []
        )
        # #2409: the workspace's egress mode, seeded for the Netfilter
        # picker. Falls back to the deploy default when unset.
        self._egress_mode: str = workspace.egress_mode or EGRESS_MODE_DEFAULT
        # #2721: home layout, seeded from the workspace. Mutable (#2719):
        # a flip applies from the next connect/start.
        self._per_handle_home: bool = bool(workspace.per_handle_home)
        # In-place editor state (#1778): when set, the next Add *replaces*
        # the item at this index/key instead of appending. Cleared on Add.
        self._editing_mount: int | None = None
        self._editing_env: str | None = None
        self._editing_allow: int | None = None
        self._editing_reject: int | None = None
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
        # Header / status dock (StatusBar + Footer) come from StatusScreen
        # (#2689).
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        if self._select_value is not None:
            image_select = Select(
                self._select_options, value=self._select_value, id="image"
            )
        else:  # pragma: no cover
            image_select = Select(self._select_options, id="image")
        with NonFocusableVerticalScroll(id="edit_box"):
            yield Static(
                Text(f"Edit workspace: {self._ws.name}"), classes="title"
            )
            yield Static("", id="edit_msg")
            with TabbedContent(id="form_tabs"):
                with TabPane("General", id="general_pane"):
                    yield Horizontal(
                        Static("Name"),
                        Input(value=self._ws.name or "", id="name"),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("Image"), image_select, classes="field-row"
                    )
                    yield Checkbox(
                        "Auto start",
                        value=self._ws.auto_start,
                        id="auto_start",
                    )
                    yield Checkbox(
                        "Mount /nix dir",
                        value=bool((self._ws.settings or {}).get("nix")),
                        id="nix",
                    )
                with TabPane("Mounts", id="mounts_pane"):
                    yield Static(
                        "Mounts  (source:/container/path[:opts])",
                        classes="editor-label",
                    )
                    yield Horizontal(
                        Input(
                            id="mount_input",
                            placeholder="/host/path:/container/path",
                        ),
                        Button("Add", id="add_mount"),
                        Button("Remove", id="rm_mount"),
                    )
                    yield OptionList(id="mount_list", classes="editor-list")
                with TabPane("Environment", id="env_pane"):
                    yield Static(
                        "Environment  (KEY=VALUE)", classes="editor-label"
                    )
                    yield Horizontal(
                        Input(id="env_input", placeholder="KEY=VALUE"),
                        Button("Add", id="add_env"),
                        Button("Remove", id="rm_env"),
                    )
                    yield OptionList(id="env_list", classes="editor-list")
                with TabPane("Netfilter", id="netfilter_pane"):
                    yield Horizontal(
                        Static("Egress mode"),
                        Select(
                            _EGRESS_MODE_OPTIONS,
                            value=self._egress_mode,
                            id="egress_mode",
                        ),
                        classes="field-row",
                    )
                    yield Static(
                        "Allowed Domains  "
                        "(host or host:port; empty = unrestricted)",
                        classes="editor-label",
                    )
                    yield Horizontal(
                        Input(id="allow_input", placeholder="github.com:443"),
                        Button("Add", id="add_allow"),
                        Button("Remove", id="rm_allow"),
                    )
                    yield OptionList(id="allow_list", classes="editor-list")
                    yield Static(
                        "Rejected Domains  "
                        "(host or host:port; NXDOMAIN'd unconditionally)",
                        classes="editor-label",
                    )
                    yield Horizontal(
                        Input(
                            id="reject_input", placeholder="evil.example.com"
                        ),
                        Button("Add", id="add_reject"),
                        Button("Remove", id="rm_reject"),
                    )
                    yield OptionList(id="reject_list", classes="editor-list")
                with TabPane("Resources", id="resources_pane"):
                    _s = self._ws.settings or {}
                    yield Horizontal(
                        Static("Idle timeout (s)"),
                        Input(
                            value=str(_s["idle_timeout"])
                            if "idle_timeout" in _s
                            else "",
                            id="idle_timeout",
                            placeholder="seconds (0 = never)",
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("CPU limit"),
                        Input(
                            value=str(_s["cpu_limit"])
                            if "cpu_limit" in _s
                            else "",
                            id="cpu_limit",
                            placeholder="e.g. 2.0",
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("Memory limit"),
                        Input(
                            value=str(_s.get("memory_limit", "")),
                            id="memory_limit",
                            placeholder="e.g. 4g, 512m",
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("PIDs limit"),
                        Input(
                            value=str(_s["pids_limit"])
                            if "pids_limit" in _s
                            else "",
                            id="pids_limit",
                            placeholder="e.g. 512",
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("/tmp size"),
                        Input(
                            value=str(_s.get("tmp_size", "")),
                            id="tmp_size",
                            placeholder="e.g. 2g, 512m",
                        ),
                        classes="field-row",
                    )
                with TabPane("Advanced", id="advanced_pane"):
                    yield Checkbox(
                        "Per-handle home (off = shared /home/klangk)",
                        value=self._per_handle_home,
                        id="per_handle_home",
                    )
                    yield Horizontal(
                        Static("Command"),
                        Input(
                            value=self._ws.service_command or "", id="command"
                        ),
                        classes="field-row",
                    )
                    yield Horizontal(
                        Static("Health"),
                        Input(
                            value=self._ws.health_check or "",
                            id="health_check",
                        ),
                        classes="field-row",
                    )
            yield Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save", variant="primary"),
                classes="actions",
            )

    def on_mount(self) -> None:
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        # The nix toggle is shown only when the server has a nix backend
        # (#2233); otherwise hidden + disabled so Tab skips it.
        nix_cb = self.query_one("#nix", Checkbox)
        nix_cb.display = self._nix_available
        nix_cb.disabled = not self._nix_available
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()
        self._render_rejected_domains()
        # General tab is active on entry — focus Name so the user can start
        # typing immediately (the tab strip is one Up away, #1891).
        self.query_one("#name", Input).focus()

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
            "add_reject",
            "rm_reject",
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

    # --- rejected-domains list editor (#2386, mirrors allowed-domains) ---

    def _render_rejected_domains(self) -> None:
        ol = self.query_one("#reject_list", OptionList)
        ol.clear_options()
        if not self._rejected_domains:
            ol.add_option(Option(Text("(none)"), id="", disabled=True))
            return
        for i, d in enumerate(self._rejected_domains):
            ol.add_option(Option(Text(d), id=f"r{i}"))

    def _add_rejected_domain(self) -> None:
        inp = self.query_one("#reject_input", Input)
        v = inp.value.strip()
        if not v:
            return
        # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
        err = validate_allowed_domain_spec(v, allow_cidr=False)
        if err:
            self._msg(err, error=True)
            return
        idx = self._editing_reject
        if idx is not None and 0 <= idx < len(self._rejected_domains):
            self._rejected_domains[idx] = v
            self._editing_reject = None
        elif v not in self._rejected_domains:
            self._rejected_domains.append(v)
        inp.value = ""
        self._msg("")
        self._render_rejected_domains()

    def _remove_rejected_domain(self) -> None:
        ol = self.query_one("#reject_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._rejected_domains):
            return
        del self._rejected_domains[idx]
        self._editing_reject = None
        self._render_rejected_domains()

    def _edit_rejected_domain(self) -> None:
        ol = self.query_one("#reject_list", OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(self._rejected_domains):
            return
        self._editing_reject = idx
        inp = self.query_one("#reject_input", Input)
        inp.value = self._rejected_domains[idx]
        inp.focus()
        self._msg("Editing rejected-domain — press Add to update.")

    # --- tab + keyboard navigation (#1891) ---

    def _active_tab(self) -> str:
        """The id of the currently active form tab pane."""
        return self.query_one("#form_tabs", TabbedContent).active

    def _focus_first_in_active_pane(self) -> None:
        """Focus the first field of the active tab pane (Down from strip)."""
        first = self._PANE_FIRST_FIELD.get(self._active_tab(), "name")
        self.query_one(f"#{first}").focus()

    def on_key(self, event) -> None:
        # Spatial nav around the form tab strip (AGENTS.md "TUI spatial
        # navigation"): Down from the strip drops into the active pane's
        # first field; Up from that field returns to the strip. Left/Right
        # on the strip switch tabs (Textual built-in). Tab/Shift-Tab still
        # cycles fields via TabSkipMixin (#1783).
        focused = self.focused
        if isinstance(focused, Tabs) and event.key == "down":
            event.stop()
            self._focus_first_in_active_pane()
            return
        if event.key == "up":
            fid = getattr(focused, "id", None)
            first = self._PANE_FIRST_FIELD.get(self._active_tab(), "name")
            if fid == first:
                event.stop()
                self.query_one(Tabs).focus()
                return
        super().on_key(event)

    # --- keyboard remove/edit of the active tab's list (#1778, #1891) ---

    def _list_handlers(self) -> tuple[str, str] | None:
        """(remove, edit) handlers for the active pane's focused list.

        Most panes hold one list, so :data:`_PANE_LIST_HANDLER` keys on the
        pane. The netfilter pane holds TWO (#2386: allow + reject), so dispatch
        on the focused widget -- Delete/'e' acts on whichever list owns focus,
        defaulting to the allow list (the pane's first field).
        """
        pane = self._active_tab()
        if pane == "netfilter_pane":
            # Two lists share this pane (#2386): dispatch on the focused
            # widget. Focus on a reject *input* (not the list) falls through
            # to the allow default -- harmless, because an Input consumes
            # Delete/'e' before the Screen binding fires, so these actions
            # never run while typing in an input.
            focused = self.focused.id if self.focused else None
            if focused == "reject_list":
                return ("_remove_rejected_domain", "_edit_rejected_domain")
            return ("_remove_allowed_domain", "_edit_allowed_domain")
        return self._PANE_LIST_HANDLER.get(pane)

    def action_remove_item(self) -> None:
        handler = self._list_handlers()
        if handler:
            getattr(self, handler[0])()

    def action_edit_item(self) -> None:
        handler = self._list_handlers()
        if handler:
            getattr(self, handler[1])()

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
        rejected_domains = list(self._rejected_domains) or None
        egress_mode = self.query_one("#egress_mode", Select).value
        # #2721: home layout is mutable and applies from the next
        # connect/start — never a restart-needed field (open sessions
        # keep their layout until they end).
        per_handle_home = self.query_one("#per_handle_home", Checkbox).value
        try:
            settings = _collect_settings(self)
        except ValueError as exc:
            self._msg(str(exc), error=True)
            return
        # #2233: emit an explicit nix value (True/False) whenever the
        # toggle is shown. PUT settings is a full-replace bag, so we must
        # carry the checkbox state — including False — to actually turn
        # the mount off (omitting the key leaves the stale bag untouched).
        # Seed from the existing bag first so API-only keys the form does
        # not represent (e.g. bridge_timeout) survive the full-replace
        # instead of being silently wiped.
        if self._nix_available:
            settings = {
                **(self._ws.settings or {}),
                **(settings or {}),
                "nix": bool(self.query_one("#nix", Checkbox).value),
            }
        body = {
            "name": name,
            "image": image,
            "service_command": command,
            "health_check": health_check,
            "auto_start": auto,
            "mounts": mounts,
            "env": env,
            "allowed_domains": allowed_domains,
            "rejected_domains": rejected_domains,
            "egress_mode": egress_mode,
            "per_handle_home": per_handle_home,
        }
        if settings is not None:
            body["settings"] = settings
        ws = self._ws
        orig_mounts = list(ws.mounts or []) or None
        orig_env = dict(ws.env or {}) or None
        orig_domains = list(ws.allowed_domains or []) or None
        orig_rejected = list(ws.rejected_domains or []) or None
        restart_needed = bool(ws.running) and (
            (image or None) != (ws.image or None)
            or mounts != orig_mounts
            or env != orig_env
            or (command or None) != (ws.service_command or None)
            or allowed_domains != orig_domains
            or rejected_domains != orig_rejected
            # #2409: egress_mode is a container-create-time field (it sets
            # up --network container:<sidecar>), so a change needs a restart.
            or egress_mode != (ws.egress_mode or EGRESS_MODE_DEFAULT)
            # #2233: the per-workspace /nix mount is set up at create
            # time, so toggling it on a running workspace needs a restart.
            or (
                self._nix_available
                and (settings or {}).get("nix", False)
                != bool((ws.settings or {}).get("nix"))
            )
        )
        self.run_worker(
            self._do_save(name, body, ws, restart_needed),
            exit_on_error=False,
        )

    def _safe_dismiss(self, result) -> None:
        """Dismiss this form only when it is still on the screen stack.

        ``Screen.dismiss`` unconditionally pops the top screen, so an
        unguarded dismiss from an in-flight save worker whose form was
        already popped underneath it (the workspace was deleted and the
        status reload popped it, #2029 review round 2) would eat the
        MainScreen below and leave a blank base screen. No-op instead.
        """
        if self in self.app.screen_stack:
            self.dismiss(result)

    async def _do_save(self, name, body, ws, restart_needed) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.update_workspace, ws.id, **body
            )
        except AuthError:
            self.app.session_expired()
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
                    self._safe_dismiss(name)

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
            self._safe_dismiss(name)

    async def _do_restart_after_save(self, ws_name, dismiss_name) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace, ws_name
            )
        except Exception as exc:
            self._msg(f"Saved, but restart failed: {exc}", error=True)
            return
        self._safe_dismiss(dismiss_name)

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
        elif bid == "add_reject":
            self._add_rejected_domain()
        elif bid == "rm_reject":
            self._remove_rejected_domain()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        eid = event.input.id
        if eid == "mount_input":
            self._add_mount()
        elif eid == "env_input":
            self._add_env()
        elif eid == "allow_input":
            self._add_allowed_domain()
        elif eid == "reject_input":
            self._add_rejected_domain()
        elif eid in (
            "name",
            "command",
            "health_check",
            "idle_timeout",
            "cpu_limit",
            "memory_limit",
            "pids_limit",
        ):
            self._save()
