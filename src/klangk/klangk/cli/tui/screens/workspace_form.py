"""Workspace create and edit forms."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

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
from ...mount import (
    validate_allowed_domain_spec,
    validate_mount_spec,
)
from .base import (
    ConfirmScreen,
    NonFocusableVerticalScroll,
    StatusScreen,
    TabSkipMixin,
)


def _int_setting(
    screen: Screen, input_id: str, error_template: str
) -> int | None:
    """Read an integer input; the templated ValueError on junk input."""
    raw = screen.query_one(f"#{input_id}", Input).value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(error_template.format(raw=raw)) from None


def _cpu_setting(screen: Screen) -> float | None:
    """Read the CPU-limit input; rejects NaN/Inf (#2029 review)."""
    raw = screen.query_one("#cpu_limit", Input).value.strip()
    if not raw:
        return None
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
    return cpu


def set_if_present(settings: dict, key: str, value) -> None:
    """Record *value* under *key* when it is not None."""
    if value is not None:
        settings[key] = value


def text_setting(screen: Screen, input_id: str) -> str | None:
    """A text input's stripped value (None when empty)."""
    raw = screen.query_one(f"#{input_id}", Input).value.strip()
    return raw or None


def collect_settings(screen: Screen) -> dict | None:
    """Read the resource-limit inputs and return a settings dict, or None.

    Raises ``ValueError`` (field-named) on invalid input so the form can
    show an inline error instead of crashing the app — ``int(raw)``/``float(raw)``
    used to propagate out of the button handler (#2029 audit). The ``float``
    field also rejects NaN/Inf: the server's positive-number check
    (``f <= 0``) passes NaN, and podman later rejects ``--cpus nan`` at
    container start with a cryptic error long after submit (#2029 review).
    """
    settings: dict = {}
    idle = _int_setting(
        screen,
        "idle_timeout",
        "Idle timeout must be a whole number of seconds: {raw!r}",
    )
    set_if_present(settings, "idle_timeout", idle)
    set_if_present(settings, "cpu_limit", _cpu_setting(screen))
    set_if_present(
        settings, "memory_limit", text_setting(screen, "memory_limit")
    )
    pids = _int_setting(
        screen,
        "pids_limit",
        "PIDs limit must be a whole number: {raw!r}",
    )
    set_if_present(settings, "pids_limit", pids)
    set_if_present(settings, "tmp_size", text_setting(screen, "tmp_size"))
    return settings or None


# Shared add/remove editor buttons for both form screens (#1891); the
# handler name strings dispatch like _PANE_LIST_HANDLER does.
_EDITOR_BUTTON_HANDLERS = {
    "add_mount": "_add_mount",
    "rm_mount": "_remove_mount",
    "add_env": "_add_env",
    "rm_env": "_remove_env",
    "add_allow": "_add_allowed_domain",
    "rm_allow": "_remove_allowed_domain",
    "add_reject": "_add_rejected_domain",
    "rm_reject": "_remove_rejected_domain",
}


def dispatch_editor_button(screen, bid: str) -> None:
    """Run the shared editor button handler, if bid names one."""
    handler = _EDITOR_BUTTON_HANDLERS.get(bid)
    if handler:
        getattr(screen, handler)()


# Enter in an editor input is an Add (mirrors _EDITOR_BUTTON_HANDLERS).
_EDITOR_INPUT_HANDLERS = {
    "mount_input": "_add_mount",
    "env_input": "_add_env",
    "allow_input": "_add_allowed_domain",
    "reject_input": "_add_rejected_domain",
}

# Enter in any of these scalar inputs submits the form. Shared by the
# create and edit forms — the two lists drifted once already (the edit
# form lost tmp_size, #3096), so keep one literal.
_SCALAR_SUBMIT_IDS = (
    "name",
    "command",
    "health_check",
    "classification_banner",
    "idle_timeout",
    "cpu_limit",
    "memory_limit",
    "pids_limit",
    "tmp_size",
)


def compose_general_pane(
    image_select, *, name="", auto_start=False, nix=False, allow_sudo=False
) -> ComposeResult:
    """The General tab: identity + the three deploy-gated toggles.

    Shared by the create and edit forms (#1891, #2904) — the panes build
    the same widget tree; the seeds differ (create defaults vs the
    workspace's current values). #3046: the sudo toggle defaults to
    unchecked (lock down) — the user opts in to sudo."""
    with TabPane("General", id="general_pane"):
        yield Horizontal(
            Static("Name"), Input(value=name, id="name"), classes="field-row"
        )
        yield Horizontal(Static("Image"), image_select, classes="field-row")
        yield Checkbox("Auto start", value=auto_start, id="auto_start")
        yield Checkbox("Mount /nix dir", value=nix, id="nix")
        yield Checkbox(
            "Allow sudo (uncheck to lock down)",
            value=allow_sudo,
            id="allow_sudo",
        )


def compose_mounts_pane() -> ComposeResult:
    """The Mounts tab: string-list editor (shared by both forms)."""
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


def compose_environment_pane() -> ComposeResult:
    """The Environment tab: KEY=VALUE list editor (shared by both forms)."""
    with TabPane("Environment", id="env_pane"):
        yield Static("Environment  (KEY=VALUE)", classes="editor-label")
        yield Horizontal(
            Input(id="env_input", placeholder="KEY=VALUE"),
            Button("Add", id="add_env"),
            Button("Remove", id="rm_env"),
        )
        yield OptionList(id="env_list", classes="editor-list")


def compose_netfilter_pane(egress_mode: str) -> ComposeResult:
    """The Netfilter tab: egress-mode picker + allow/reject editors
    (shared by both forms)."""
    with TabPane("Netfilter", id="netfilter_pane"):
        yield Horizontal(
            Static("Egress mode"),
            Select(
                _EGRESS_MODE_OPTIONS,
                value=egress_mode,
                id="egress_mode",
            ),
            classes="field-row",
        )
        yield Static(
            "Allowed Domains  (host or host:port; empty = unrestricted)",
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
            Input(id="reject_input", placeholder="evil.example.com"),
            Button("Add", id="add_reject"),
            Button("Remove", id="rm_reject"),
        )
        yield OptionList(id="reject_list", classes="editor-list")


def compose_resources_pane(seeded: dict[str, str]) -> ComposeResult:
    """The Resources tab: the five limit inputs (shared by both forms;
    *seeded* carries the edit form's current values, all-empty on
    create)."""
    with TabPane("Resources", id="resources_pane"):
        yield Horizontal(
            Static("Idle timeout (s)"),
            Input(
                value=seeded["idle_timeout"],
                id="idle_timeout",
                placeholder="seconds (0 = never)",
            ),
            classes="field-row",
        )
        yield Horizontal(
            Static("CPU limit"),
            Input(
                value=seeded["cpu_limit"],
                id="cpu_limit",
                placeholder="e.g. 2.0",
            ),
            classes="field-row",
        )
        yield Horizontal(
            Static("Memory limit"),
            Input(
                value=seeded["memory_limit"],
                id="memory_limit",
                placeholder="e.g. 4g, 512m",
            ),
            classes="field-row",
        )
        yield Horizontal(
            Static("PIDs limit"),
            Input(
                value=seeded["pids_limit"],
                id="pids_limit",
                placeholder="e.g. 512",
            ),
            classes="field-row",
        )
        yield Horizontal(
            Static("/tmp size"),
            Input(
                value=seeded["tmp_size"],
                id="tmp_size",
                placeholder="e.g. 2g, 512m",
            ),
            classes="field-row",
        )


def compose_advanced_pane(
    *,
    per_handle_home: bool,
    classification_banner: str = "",
    service_command: str = "",
    health_check: str = "",
) -> ComposeResult:
    """The Advanced tab: home layout, marking, service command, health
    check (shared by both forms; the seeds differ)."""
    with TabPane("Advanced", id="advanced_pane"):
        yield Horizontal(
            Static("Home layout"),
            Checkbox(
                "per-handle (off = shared home)",
                value=per_handle_home,
                id="per_handle_home",
            ),
            classes="field-row",
        )
        yield Horizontal(
            Static("Marking"),
            Input(
                value=classification_banner,
                id="classification_banner",
                placeholder=("e.g. CUI (empty = server default)"),
            ),
            classes="field-row",
        )
        yield Horizontal(
            Static("Command"),
            Input(value=service_command, id="command"),
            classes="field-row",
        )
        yield Horizontal(
            Static("Health"),
            Input(value=health_check, id="health_check"),
            classes="field-row",
        )


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


def render_form_list(
    screen, selector: str, items, fmt, empty_label: str, id_prefix: str
) -> None:
    """Render a form list editor's OptionList: cleared, then one option per
    item (or the disabled empty placeholder when there are none)."""
    ol = screen.query_one(selector, OptionList)
    ol.clear_options()
    if not items:
        ol.add_option(Option(Text(empty_label), id="", disabled=True))
        return
    for i, item in enumerate(items):
        ol.add_option(Option(Text(fmt(item)), id=f"{id_prefix}{i}"))


async def edit_images(state) -> tuple[str, list[str]] | None:
    """(default, allowed) images for the edit form; None on auth failure."""
    try:
        data = await asyncio.to_thread(state.list_images)
    except AuthError:
        return None
    except Exception:
        return "", []
    return data.get("default", "") or "", list(data.get("allowed") or [])


async def edit_toggles(state) -> tuple[bool, bool] | None:
    """The deploy nix/sudo toggles; None on auth failure."""
    try:
        return await asyncio.to_thread(state.deploy_toggles)
    except AuthError:
        return None
    except Exception:
        return False, False


async def edit_autostart(state) -> bool | None:
    """The deploy autostart flag; None on auth failure."""
    try:
        return await asyncio.to_thread(state.allow_autostart)
    except AuthError:
        return None
    except Exception:
        return False


async def open_edit_screen(screen, state, workspace, on_edited) -> None:
    """Load image/autostart metadata off-thread and open the edit form.

    Shared by the workspace-list (main) and workspace-detail screens.
    An image-listing failure degrades to empty defaults (the form's
    image field then accepts free text); an autostart or toggle
    failure leaves them disabled; ``AuthError`` ends the session via
    ``session_expired``.
    """
    images = await edit_images(state)
    if images is None:
        screen.app.session_expired()
        return
    default, allowed = images
    # #2974: deploy-level nix/sudo toggles moved from the images
    # payload to the authenticated-only /config fields.
    toggles = await edit_toggles(state)
    if toggles is None:
        screen.app.session_expired()
        return
    nix_available, sudo_available = toggles
    allow_autostart = await edit_autostart(state)
    if allow_autostart is None:
        screen.app.session_expired()
        return
    screen.app.push_screen(
        EditWorkspaceScreen(
            workspace=workspace,
            allowed=allowed,
            default=default,
            allow_autostart=allow_autostart,
            nix_available=nix_available,
            sudo_available=sudo_available,
        ),
        on_edited,
    )


def editing_index(screen, editing_attr) -> int | None:
    """The in-place-edit index attribute, when set."""
    return getattr(screen, editing_attr) if editing_attr else None


def insert_list_entry(
    entries: list, v: str, idx, replacing: bool, dedupe: bool
) -> None:
    """Replace the entry at *idx* in place, or append (dedupe-guarded)."""
    if replacing:
        entries[idx] = v
    elif not dedupe or v not in entries:
        entries.append(v)


def clear_editing_index(screen, editing_attr, replacing: bool) -> None:
    """Clear the in-place-edit index after a successful replace."""
    if replacing and editing_attr:
        setattr(screen, editing_attr, None)


class WorkspaceFormMixin:
    """Shared body of the create/edit workspace form screens: the four list
    renderers (same widget ids + backing attributes) and the tab strip's
    spatial navigation (#1891, AGENTS.md)."""

    # Down from the tab strip enters the active pane's first field; Up from
    # that field returns to the strip. Shared by both forms (identical panes).
    _PANE_FIRST_FIELD = {
        "general_pane": "name",
        "mounts_pane": "mount_input",
        "env_pane": "env_input",
        "netfilter_pane": "egress_mode",
        "resources_pane": "idle_timeout",
        "advanced_pane": "command",
    }

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

    def _render_mounts(self) -> None:
        render_form_list(
            self, "#mount_list", self._mounts, str, "(no mounts)", "m"
        )

    def _render_env(self) -> None:
        render_form_list(
            self,
            "#env_list",
            list(self._env.items()),
            lambda kv: f"{kv[0]}={kv[1]}",
            "(no env vars)",
            "e",
        )

    def _render_allowed_domains(self) -> None:
        render_form_list(
            self,
            "#allow_list",
            self._allowed_domains,
            str,
            "(unrestricted)",
            "a",
        )

    def _render_rejected_domains(self) -> None:
        render_form_list(
            self, "#reject_list", self._rejected_domains, str, "(none)", "r"
        )

    # --- shared string-list editor actions (mounts / allowed /
    # rejected domains); the env editor stays bespoke (dict-keyed,
    # #1778 in-place edits track a key rather than an index) ---

    def _add_list_entry(
        self,
        input_id: str,
        entries: list[str],
        validate: Callable[[str], str | None],
        render: Callable[[], None],
        *,
        dedupe: bool = False,
        editing_attr: str | None = None,
    ) -> None:
        """Add the input's value to *entries*, then re-render.

        With *editing_attr* naming an in-place-edit index attribute
        (EditWorkspaceScreen, #1778), a valid index updates that entry in
        place instead of appending. *dedupe* skips the append when the
        value is already present.
        """
        inp = self.query_one(input_id, Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate(v)
        if err:
            self.msg(err, error=True)
            return
        idx = editing_index(self, editing_attr)
        replacing = idx is not None and 0 <= idx < len(entries)
        insert_list_entry(entries, v, idx, replacing, dedupe)
        clear_editing_index(self, editing_attr, replacing)
        inp.value = ""
        self.msg("")
        render()

    def _remove_list_entry(
        self,
        option_list_id: str,
        entries: list[str],
        render: Callable[[], None],
        editing_attr: str | None = None,
    ) -> None:
        """Delete the highlighted entry from *entries*, then re-render.

        Clears the in-place-edit index (*editing_attr*) so a pending edit
        does not retarget the shifted indices.
        """
        ol = self.query_one(option_list_id, OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(entries):
            return
        del entries[idx]
        if editing_attr:
            setattr(self, editing_attr, None)
        render()

    def _edit_list_entry(
        self,
        option_list_id: str,
        input_id: str,
        entries: list[str],
        editing_attr: str,
        label: str,
    ) -> None:
        """Load the highlighted entry into the input for in-place editing
        (#1778). *label* names the entry kind in the status message."""
        ol = self.query_one(option_list_id, OptionList)
        idx = ol.highlighted
        if idx is None or not 0 <= idx < len(entries):
            return
        setattr(self, editing_attr, idx)
        inp = self.query_one(input_id, Input)
        inp.value = entries[idx]
        inp.focus()
        self.msg(f"Editing {label} — press Add to update.")

    # --- env (dict-keyed) editor actions, the analogue of the string
    # list editors above; #1778 in-place edits track a key rather than
    # an index, so the edit form passes its _editing_env attr ---

    def add_env_entry(self, editing_attr: str | None = None) -> None:
        """Add the env input's KEY=VALUE to the map, then re-render.

        With *editing_attr* naming an in-place-edit key attribute
        (EditWorkspaceScreen, #1778), a pending edit of another key is
        removed first so the update lands under the (possibly renamed)
        new key."""
        inp = self.query_one("#env_input", Input)
        v = inp.value.strip()
        if not v:
            return
        err = validate_env_entry(v)
        if err:
            self.msg(err, error=True)
            return
        key, _, value = v.partition("=")
        if editing_attr:
            old = getattr(self, editing_attr)
            if old is not None:
                self._env.pop(old, None)
                setattr(self, editing_attr, None)
        self._env[key] = value
        inp.value = ""
        self.msg("")
        self._render_env()

    def remove_env_entry(self, editing_attr: str | None = None) -> None:
        """Delete the highlighted env key from the map, then re-render.

        Clears a pending in-place edit (*editing_attr*) along with it."""
        ol = self.query_one("#env_list", OptionList)
        idx = ol.highlighted
        keys = list(self._env)
        if idx is None or not 0 <= idx < len(keys):
            return
        del self._env[keys[idx]]
        if editing_attr:
            setattr(self, editing_attr, None)
        self._render_env()

    # --- shared on_mount / event dispatch (create + edit) ---

    def image_select(self) -> Select:
        """The image picker: preselected when a default is available,
        blank otherwise (the server applies its default image)."""
        if self._select_value is not None:
            return Select(
                self._select_options, value=self._select_value, id="image"
            )
        return Select(self._select_options, id="image")

    def form_on_mount(self) -> None:
        """Shared on_mount: apply deploy-gated toggle visibility, seed
        the list editors, and focus Name (General is the entry tab).
        Screens with extra toggles (create's home-layout default)
        handle theirs before/after calling this."""
        shown = self._allow_autostart
        cb = self.query_one("#auto_start", Checkbox)
        cb.display = shown
        cb.disabled = not shown
        # The nix toggle is shown only when the server has a nix backend
        # (#2233); otherwise hidden + disabled so Tab skips it.
        nix_cb = self.query_one("#nix", Checkbox)
        nix_cb.display = self._nix_available
        nix_cb.disabled = not self._nix_available
        # #2017: the sudo toggle is shown only when the deploy allows
        # sudo; hidden otherwise (the knob could only ever be a no-op).
        sudo_cb = self.query_one("#allow_sudo", Checkbox)
        sudo_cb.display = self._sudo_available
        sudo_cb.disabled = not self._sudo_available
        self._skip_editors_on_tab()
        self._render_mounts()
        self._render_env()
        self._render_allowed_domains()
        self._render_rejected_domains()
        # General tab is active on entry — focus Name so the user can
        # start typing immediately (the tab strip is one Up away, #1891).
        self.query_one("#name", Input).focus()

    def handle_form_button(
        self, event: Button.Pressed, *, submit_id: str, cancel_result, submit
    ) -> None:
        """Shared button dispatch: cancel dismisses (with
        *cancel_result*), the screen's submit button runs *submit*, and
        anything else falls through to the editor buttons."""
        bid = event.button.id
        if bid == "cancel":
            self.dismiss(cancel_result)
            return
        if bid == submit_id:
            submit()
            return
        dispatch_editor_button(self, bid)

    def handle_form_input_submitted(
        self, event: Input.Submitted, *, submit_ids, submit
    ) -> None:
        """Shared Enter dispatch: an editor input Adds its entry; a
        scalar input in *submit_ids* submits the form."""
        eid = event.input.id
        action = _EDITOR_INPUT_HANDLERS.get(eid)
        if action is not None:
            getattr(self, action)()
        elif eid in submit_ids:
            submit()

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


def create_picker_options(
    allowed: list[str], default: str
) -> tuple[list, object]:
    """(options, preselected value) for the create form's image picker."""
    if allowed:
        # Select tuples are (prompt, value). Prompts are rich Text so an
        # image name containing brackets can't trigger markup parsing.
        options = [(Text(img), img) for img in allowed]
        return options, default if default in allowed else None
    # Couldn't list images — offer a single inert placeholder so the
    # user can still create; the server applies its default image.
    return (
        [(Text("(server default)"), "(server default)")],
        "(server default)",
    )


def cleared_text(screen: Screen, input_id: str) -> str | None:
    """A form text input's stripped value, None when empty."""
    return screen.query_one(f"#{input_id}", Input).value.strip() or None


def normalized_list(items) -> list | None:
    """A copied list, empty (or None) -> None — the body representation."""
    return list(items or []) or None


def normalized_dict(d) -> dict | None:
    """A copied dict, empty (or None) -> None — the body representation."""
    return dict(d or {}) or None


def cleared(value):
    """The value normalized to None when empty."""
    return value or None


def http_error_detail(exc: httpx.HTTPStatusError) -> str:
    """The server's error detail from an HTTPStatusError response."""
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return exc.response.text or str(exc)


def merged_toggles(settings: dict | None, extra: dict) -> dict:
    """The settings bag with one toggle merged in."""
    return {**(settings or {}), **extra}


def create_toggles(screen, settings: dict | None) -> dict:
    """The create payload's settings bag with the shown toggles applied."""
    if screen._nix_available and screen.query_one("#nix", Checkbox).value:
        settings = merged_toggles(settings, {"nix": True})
    # #2017/#3047: always emit an explicit sudo value when the toggle
    # is shown — an absent key means OFF (the bag is the sole posture
    # source; the deploy flag is only a ceiling). Unchecked locks the
    # workspace down; checked opts in.
    if screen._sudo_available:
        settings = merged_toggles(
            settings,
            {
                "allow_sudo": bool(
                    screen.query_one("#allow_sudo", Checkbox).value
                )
            },
        )
    return settings


class CreateWorkspaceScreen(WorkspaceFormMixin, TabSkipMixin, StatusScreen):
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
        "allow_sudo",
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
        "classification_banner",
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
        sudo_available: bool = False,
    ) -> None:
        super().__init__()
        self._allowed = list(allowed)
        self._default = default or ""
        self._allow_autostart = bool(allow_autostart)
        # #2233: per-workspace nix toggle (Mount /nix dir). Shown only when
        # the server has a nix backend, matching the create dialog.
        self._nix_available = bool(nix_available)
        # #2017: per-workspace sudo lock-down toggle. The deploy-wide
        # allow_sudo is a ceiling, so the toggle is only meaningful when
        # the server allows sudo; hidden otherwise (sudo is off for every
        # workspace regardless of the checkbox).
        self._sudo_available = bool(sudo_available)
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
        self._select_options, self._select_value = create_picker_options(
            self._allowed, self._default
        )

    def compose(self) -> ComposeResult:
        # Header / status dock (StatusBar + Footer) come from StatusScreen
        # (#2689).
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        with NonFocusableVerticalScroll(id="create_box"):
            yield Static("New workspace", classes="title")
            yield Static("", id="create_msg")
            with TabbedContent(id="form_tabs"):
                yield from compose_general_pane(self.image_select())
                yield from compose_mounts_pane()
                yield from compose_environment_pane()
                yield from compose_netfilter_pane(EGRESS_MODE_DEFAULT)
                yield from compose_resources_pane(_seeded_setting_values({}))
                yield from compose_advanced_pane(
                    per_handle_home=bool(self._default_per_handle_home),
                )
            yield Horizontal(
                Button("Cancel", id="cancel"),
                Button("Create", id="create", variant="primary"),
                classes="actions",
            )

    def on_mount(self) -> None:
        self.form_on_mount()
        # The home-layout toggle is hidden when the deploy default is
        # unknown (fetch failure): an offered choice we can't pre-reflect
        # would pin a possibly-wrong value, so the field is omitted and
        # the server applies its own default (#2737 review).
        phh_cb = self.query_one("#per_handle_home", Checkbox)
        phh_cb.display = self._default_per_handle_home is not None
        phh_cb.disabled = self._default_per_handle_home is None

    def msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#create_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- mounts list editor ---

    def _add_mount(self) -> None:
        self._add_list_entry(
            "#mount_input",
            self._mounts,
            validate_mount_spec,
            self._render_mounts,
        )

    def _remove_mount(self) -> None:
        self._remove_list_entry(
            "#mount_list", self._mounts, self._render_mounts
        )

    # --- env list editor ---

    def _add_env(self) -> None:
        self.add_env_entry()

    def _remove_env(self) -> None:
        self.remove_env_entry()

    # --- allowed-domains list editor (#1745) ---

    def _add_allowed_domain(self) -> None:
        self._add_list_entry(
            "#allow_input",
            self._allowed_domains,
            validate_allowed_domain_spec,
            self._render_allowed_domains,
            dedupe=True,
        )

    def _remove_allowed_domain(self) -> None:
        self._remove_list_entry(
            "#allow_list", self._allowed_domains, self._render_allowed_domains
        )

    # --- rejected-domains list editor (#2386, mirrors allowed-domains) ---

    def _add_rejected_domain(self) -> None:
        # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
        self._add_list_entry(
            "#reject_input",
            self._rejected_domains,
            lambda spec: validate_allowed_domain_spec(spec, allow_cidr=False),
            self._render_rejected_domains,
            dedupe=True,
        )

    def _remove_rejected_domain(self) -> None:
        self._remove_list_entry(
            "#reject_list",
            self._rejected_domains,
            self._render_rejected_domains,
        )

    # --- tab + keyboard navigation (#1891) ---

    # --- create ---

    def _selected_image(self) -> str | None:
        """The image picker's value, or None for no real selection.

        Send only a real, non-default selection. When the server's default
        isn't in the allowed list we start unselected (Select.BLANK), so an
        untouched picker omits the image — matching the Flutter dialog.
        """
        val = self.query_one("#image", Select).value
        if (
            val is Select.BLANK
            or val is Select.NULL
            or not self._allowed
            or val == self._default
        ):
            return None
        return val

    def _create_payload(self) -> dict:
        """Gather the non-name form fields into create-workspace values
        (empty strings/lists -> None, matching the Flutter dialog)."""
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
        return {
            "image": self._selected_image(),
            "command": cleared_text(self, "command"),
            "health_check": cleared_text(self, "health_check"),
            "auto": auto,
            "per_handle_home": per_handle_home,
            # #2768: free-text classification marking; empty = inherit the
            # deploy default (KLANGKD_CLASSIFICATION_BANNER).
            "classification_banner": cleared_text(
                self, "classification_banner"
            ),
            "mounts": normalized_list(self._mounts),
            "env": normalized_dict(self._env),
            "allowed_domains": normalized_list(self._allowed_domains),
            "rejected_domains": normalized_list(self._rejected_domains),
            "egress_mode": self.query_one("#egress_mode", Select).value,
        }

    def _create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.msg("Name is required.", error=True)
            return
        p = self._create_payload()
        try:
            settings = collect_settings(self)
        except ValueError as exc:
            self.msg(str(exc), error=True)
            return
        settings = create_toggles(self, settings)
        self.run_worker(
            self._do_create_workspace(
                name,
                p["image"],
                p["command"],
                p["auto"],
                p["mounts"],
                p["env"],
                p["health_check"],
                p["allowed_domains"],
                p["rejected_domains"],
                settings,
                p["egress_mode"],
                p["per_handle_home"],
                p["classification_banner"],
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
        classification_banner,
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
                classification_banner=classification_banner,
            )
        except AuthError:
            self.app.session_expired()
            return
        except httpx.HTTPStatusError as exc:
            self.msg(f"Failed to create: {http_error_detail(exc)}", error=True)
            return
        except Exception as exc:
            self.msg(f"Failed to create: {exc}", error=True)
            return
        self.dismiss(ws.name)

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.handle_form_button(
            event,
            submit_id="create",
            cancel_result=None,
            submit=self._create,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.handle_form_input_submitted(
            event,
            submit_ids=_SCALAR_SUBMIT_IDS,
            submit=self._create,
        )


def current_image(workspace) -> str:
    """The workspace's image ("" when unset)."""
    return workspace.image or ""


def picker_selection(opts: list, cur: str):
    """The preselected image: the current one, else the first option."""
    if cur in opts:
        return cur
    return opts[0] if opts else None


def _edit_picker_options(
    allowed: list[str], workspace: Workspace
) -> tuple[list, str | None]:
    """Image-picker (options, preselected value) for the edit form.

    Includes the workspace's current image even if it isn't in the
    server's allowed list, pre-selected (untouched = no change). Prompts
    are rich Text so bracket-laden names can't crash.
    """
    cur = current_image(workspace)
    opts = list(allowed)
    if cur and cur not in opts:
        opts.append(cur)
    if opts:
        return (
            [(Text(i), i) for i in opts],
            picker_selection(opts, cur),
        )
    return [(Text("(none)"), "(none)")], "(none)"


def _seeded_setting_values(settings: dict) -> dict[str, str]:
    """Resource-input seed strings from the settings bag (absent = '')."""
    return {
        "idle_timeout": str(settings["idle_timeout"])
        if "idle_timeout" in settings
        else "",
        "cpu_limit": str(settings["cpu_limit"])
        if "cpu_limit" in settings
        else "",
        "memory_limit": str(settings.get("memory_limit", "")),
        "pids_limit": str(settings["pids_limit"])
        if "pids_limit" in settings
        else "",
        "tmp_size": str(settings.get("tmp_size", "")),
    }


def seeded_lists(workspace) -> dict:
    """The edit form's list seeds (mounts/env/domains) from the workspace."""
    return {
        "mounts": list(workspace.mounts or []),
        "env": dict(workspace.env or {}),
        "allowed_domains": list(workspace.allowed_domains or []),
        "rejected_domains": list(workspace.rejected_domains or []),
    }


def seeded_form_state(workspace) -> dict:
    """The edit form's list/mode seeds from the workspace."""
    state = seeded_lists(workspace)
    state["egress_mode"] = workspace.egress_mode or EGRESS_MODE_DEFAULT
    return state


def save_image_value(image) -> str | None:
    """The image value for the save body (placeholder/empty -> None)."""
    return image if (image and image != "(none)") else None


def edit_general_seeds(ws) -> dict:
    """The General-pane seeds for the edit form."""
    return dict(
        name=ws.name or "",
        auto_start=ws.auto_start,
        nix=bool((ws.settings or {}).get("nix")),
        allow_sudo=bool((ws.settings or {}).get("allow_sudo", False)),
    )


def edit_advanced_seeds(ws) -> dict:
    """The Advanced-pane seeds for the edit form."""
    return dict(
        classification_banner=ws.classification_banner or "",
        service_command=ws.service_command or "",
        health_check=ws.health_check or "",
    )


def scalar_fields_changed(body: dict, ws) -> bool:
    """Whether image / service_command / egress_mode changed
    (both sides cleared-normalized, #1778/#1749/#2409)."""
    return (
        cleared(body["image"]) != cleared(ws.image)
        or cleared(body["service_command"]) != cleared(ws.service_command)
        or body["egress_mode"] != (ws.egress_mode or EGRESS_MODE_DEFAULT)
    )


def list_fields_changed(body: dict, orig: dict) -> bool:
    """Whether any list field differs from its normalized snapshot."""
    return (
        body["mounts"] != orig["mounts"]
        or body["env"] != orig["env"]
        or body["allowed_domains"] != orig["allowed_domains"]
        or body["rejected_domains"] != orig["rejected_domains"]
    )


def nix_changed(available: bool, settings: dict, old: dict) -> bool:
    """Whether the create-time /nix mount toggle flipped (#2233)."""
    return available and settings.get("nix", False) != bool(old.get("nix"))


def sudo_changed(available: bool, settings: dict, old: dict) -> bool:
    """Whether the create-time sudo posture flipped (#2017/#3047).

    Absent means OFF on both sides — an absent key already reads as
    locked-down, so storing an explicit False is not a flip."""
    return available and settings.get("allow_sudo", False) != bool(
        old.get("allow_sudo", False)
    )


class EditWorkspaceScreen(WorkspaceFormMixin, TabSkipMixin, StatusScreen):
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
        "allow_sudo",
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
        "classification_banner",
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
        sudo_available: bool = False,
    ) -> None:
        super().__init__()
        self._ws = workspace
        self._allow_autostart = bool(allow_autostart)
        self._default = default or ""
        # #2233: per-workspace nix toggle (Mount /nix dir). Shown only when
        # the server has a nix backend, matching the edit panel / create dialog.
        self._nix_available = bool(nix_available)
        # #2017: per-workspace sudo lock-down toggle, seeded from the bag
        # (#3046: absent = False — the UI reads an unspecified bag as
        # locked-down, matching the create default). Hidden unless the
        # deploy allows sudo (the knob can only lock down below that).
        self._sudo_available = bool(sudo_available)
        self._mounts: list[str]
        seeds = seeded_form_state(workspace)
        self._mounts = seeds["mounts"]
        self._env = seeds["env"]
        self._allowed_domains = seeds["allowed_domains"]
        # #2386: the static deny-list, seeded from the workspace.
        self._rejected_domains = seeds["rejected_domains"]
        # #2409: the workspace's egress mode, seeded for the Netfilter
        # picker. Falls back to the deploy default when unset.
        self._egress_mode = seeds["egress_mode"]
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
        self._select_options, self._select_value = _edit_picker_options(
            allowed, workspace
        )

    def compose(self) -> ComposeResult:
        # Header / status dock (StatusBar + Footer) come from StatusScreen
        # (#2689).
        yield from super().compose()

    def compose_body(self) -> ComposeResult:
        general = edit_general_seeds(self._ws)
        advanced = edit_advanced_seeds(self._ws)
        with NonFocusableVerticalScroll(id="edit_box"):
            yield Static(
                Text(f"Edit workspace: {self._ws.name}"), classes="title"
            )
            yield Static("", id="edit_msg")
            with TabbedContent(id="form_tabs"):
                yield from compose_general_pane(
                    self.image_select(),
                    name=general["name"],
                    auto_start=general["auto_start"],
                    nix=general["nix"],
                    allow_sudo=general["allow_sudo"],
                )
                yield from compose_mounts_pane()
                yield from compose_environment_pane()
                yield from compose_netfilter_pane(self._egress_mode)
                yield from compose_resources_pane(
                    _seeded_setting_values(self._ws.settings or {})
                )
                yield from compose_advanced_pane(
                    per_handle_home=self._per_handle_home,
                    classification_banner=advanced["classification_banner"],
                    service_command=advanced["service_command"],
                    health_check=advanced["health_check"],
                )
            yield Horizontal(
                Button("Cancel", id="cancel"),
                Button("Save", id="save", variant="primary"),
                classes="actions",
            )

    def on_mount(self) -> None:
        self.form_on_mount()

    def msg(self, text: str, *, error: bool = False) -> None:
        self.query_one("#edit_msg", Static).update(
            Text(text, style="red" if error else "")
        )

    # --- list editors: add / remove / in-place edit (#1778) ---

    def _add_mount(self) -> None:
        self._add_list_entry(
            "#mount_input",
            self._mounts,
            validate_mount_spec,
            self._render_mounts,
            editing_attr="_editing_mount",
        )

    def _remove_mount(self) -> None:
        self._remove_list_entry(
            "#mount_list",
            self._mounts,
            self._render_mounts,
            editing_attr="_editing_mount",
        )

    def _add_env(self) -> None:
        self.add_env_entry(editing_attr="_editing_env")

    def _remove_env(self) -> None:
        self.remove_env_entry(editing_attr="_editing_env")

    def _add_allowed_domain(self) -> None:
        self._add_list_entry(
            "#allow_input",
            self._allowed_domains,
            validate_allowed_domain_spec,
            self._render_allowed_domains,
            dedupe=True,
            editing_attr="_editing_allow",
        )

    def _remove_allowed_domain(self) -> None:
        self._remove_list_entry(
            "#allow_list",
            self._allowed_domains,
            self._render_allowed_domains,
            editing_attr="_editing_allow",
        )

    # --- in-place edit: load the highlighted item into the input (#1778) ---

    def _edit_mount(self) -> None:
        self._edit_list_entry(
            "#mount_list",
            "#mount_input",
            self._mounts,
            "_editing_mount",
            "mount",
        )

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
        self.msg("Editing env var — press Add to update.")

    def _edit_allowed_domain(self) -> None:
        self._edit_list_entry(
            "#allow_list",
            "#allow_input",
            self._allowed_domains,
            "_editing_allow",
            "allowed-domain",
        )

    # --- rejected-domains list editor (#2386, mirrors allowed-domains) ---

    def _add_rejected_domain(self) -> None:
        # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
        self._add_list_entry(
            "#reject_input",
            self._rejected_domains,
            lambda spec: validate_allowed_domain_spec(spec, allow_cidr=False),
            self._render_rejected_domains,
            dedupe=True,
            editing_attr="_editing_reject",
        )

    def _remove_rejected_domain(self) -> None:
        self._remove_list_entry(
            "#reject_list",
            self._rejected_domains,
            self._render_rejected_domains,
            editing_attr="_editing_reject",
        )

    def _edit_rejected_domain(self) -> None:
        self._edit_list_entry(
            "#reject_list",
            "#reject_input",
            self._rejected_domains,
            "_editing_reject",
            "rejected-domain",
        )

    # --- tab + keyboard navigation (#1891) ---

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

    def _save_field_values(self) -> dict:
        """The scalar widget reads for the PUT body (empties -> None)."""
        image = self.query_one("#image", Select).value
        return {
            "image": save_image_value(image),
            "service_command": cleared_text(self, "command"),
            "health_check": cleared_text(self, "health_check"),
            "auto_start": self._allow_autostart
            and self.query_one("#auto_start", Checkbox).value,
            # #2721: home layout is mutable and applies from the next
            # connect/start — never a restart-needed field (open sessions
            # keep their layout until they end).
            "per_handle_home": self.query_one(
                "#per_handle_home", Checkbox
            ).value,
            # #2768: classification marking. Always sent (full-replace like
            # the other PUT fields): an emptied field clears the override so
            # the workspace inherits the deploy default again. Display-time
            # only — never a restart-needed field.
            "classification_banner": cleared_text(
                self, "classification_banner"
            ),
            "egress_mode": self.query_one("#egress_mode", Select).value,
        }

    def _merged_save_settings(self) -> dict:
        """The settings bag for the PUT body: collect_settings merged over
        the existing bag, plus the shown nix/sudo toggles."""
        settings = collect_settings(self)
        # PUT settings is a full-replace bag, so seed from the existing
        # bag unconditionally — API-only keys the form does not represent
        # (e.g. bridge_timeout) and toggle-gated keys (nix, allow_sudo)
        # whose toggles are hidden on this deploy must survive the save
        # instead of being silently wiped (#2017 review).
        merged = {
            **(self._ws.settings or {}),
            **(settings or {}),
        }
        # #2233: emit an explicit nix value whenever the toggle is shown —
        # including False, to actually turn the mount off (omitting the
        # key would leave the stale bag untouched).
        if self._nix_available:
            merged["nix"] = bool(self.query_one("#nix", Checkbox).value)
        # #2017: same for the sudo posture — an explicit value whenever
        # the toggle is shown, so a check-to-revert actually clears a
        # stored lock-down. True follows the deploy posture (the server
        # setting stays the ceiling).
        if self._sudo_available:
            merged["allow_sudo"] = bool(
                self.query_one("#allow_sudo", Checkbox).value
            )
        return merged

    def _save_body(self, name: str) -> dict | None:
        """Gather the form fields into a PUT body; None on invalid input
        (the error has already been shown via ``msg``)."""
        try:
            merged_settings = self._merged_save_settings()
        except ValueError as exc:
            self.msg(str(exc), error=True)
            return None
        body = {
            "name": name,
            **self._save_field_values(),
            "mounts": normalized_list(self._mounts),
            "env": normalized_dict(self._env),
            "allowed_domains": normalized_list(self._allowed_domains),
            "rejected_domains": normalized_list(self._rejected_domains),
        }
        if merged_settings:
            body["settings"] = merged_settings
        return body

    @staticmethod
    def _orig_list_fields(ws) -> dict:
        """The workspace's list fields, normalized the way the body
        represents them (empty -> None) so a plain != detects a change."""
        return {
            "mounts": normalized_list(ws.mounts),
            "env": normalized_dict(ws.env),
            "allowed_domains": normalized_list(ws.allowed_domains),
            "rejected_domains": normalized_list(ws.rejected_domains),
        }

    def _create_time_fields_changed(self, body: dict, ws) -> bool:
        """Whether any top-level create-time field differs (#1778, #1749).

        image / service_command normalize both sides to None-when-empty;
        the list fields compare against their normalized snapshot."""
        orig = self._orig_list_fields(ws)
        return scalar_fields_changed(body, ws) or list_fields_changed(
            body, orig
        )

    def _settings_changed_since_create(self, body: dict, ws) -> bool:
        """Whether a create-time settings-bag key differs.

        #2233: the per-workspace /nix mount is set up at create time, so
        toggling it on a running workspace needs a restart. #2017: the
        sudoers rule is written at container-create time, so a posture
        flip needs a restart to take effect."""
        settings = body.get("settings") or {}
        old = ws.settings or {}
        return nix_changed(self._nix_available, settings, old) or sudo_changed(
            self._sudo_available, settings, old
        )

    def _restart_needed_after_save(self, body: dict) -> bool:
        """True if a create-time field changed on a running workspace."""
        ws = self._ws
        if not ws.running:
            return False
        return self._create_time_fields_changed(
            body, ws
        ) or self._settings_changed_since_create(body, ws)

    def save(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.msg("Name is required.", error=True)
            return
        body = self._save_body(name)
        if body is None:
            return
        ws = self._ws
        self.run_worker(
            self.do_save(
                name, body, ws, self._restart_needed_after_save(body)
            ),
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

    def prompt_restart_after_save(self, ws_id, dismiss_name) -> None:
        """Ask whether to restart the running container to apply the save."""

        def _after(restart: bool) -> None:
            if restart:
                self.run_worker(
                    self._do_restart_after_save(ws_id, dismiss_name),
                    exit_on_error=False,
                )
            else:
                self._safe_dismiss(dismiss_name)

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

    async def do_save(self, name, body, ws, restart_needed) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.update_workspace, ws.id, **body
            )
        except AuthError:
            self.app.session_expired()
            return
        except httpx.HTTPStatusError as exc:
            self.msg(f"Failed to save: {http_error_detail(exc)}", error=True)
            return
        except Exception as exc:
            self.msg(f"Failed to save: {exc}", error=True)
            return
        if restart_needed:
            # Restart by id: the PUT above may have renamed the workspace,
            # and names are only unique per owner — a shared workspace
            # renamed onto another visible workspace's name would otherwise
            # restart the wrong container (#3096).
            self.prompt_restart_after_save(ws.id, name)
        else:
            self._safe_dismiss(name)

    async def _do_restart_after_save(self, ws_id, dismiss_name) -> None:
        try:
            await asyncio.to_thread(
                self.app.tui_state.restart_workspace_by_id, ws_id
            )
        except Exception as exc:
            self.msg(f"Saved, but restart failed: {exc}", error=True)
            return
        self._safe_dismiss(dismiss_name)

    # --- event handlers ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.handle_form_button(
            event,
            submit_id="save",
            cancel_result=False,
            submit=self.save,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.handle_form_input_submitted(
            event,
            submit_ids=_SCALAR_SUBMIT_IDS,
            submit=self.save,
        )


# ---------------------------------------------------------------------------
# Client-side env-entry validation (moved verbatim from the former cli/env
# submodule — sole consumer was this screen).
# ---------------------------------------------------------------------------


def validate_env_entry(spec: str) -> str | None:
    """Validate a ``KEY=VALUE`` environment variable entry.

    Returns None if valid, or an error message string if invalid.
    Mirrors the Flutter ``CreateWorkspaceDialog`` rule: the entry must
    contain ``=`` and have a non-empty key (the part before the first
    ``=``). The value may be empty.
    """
    if "=" not in spec:
        return f"Invalid env {spec!r}: expected KEY=VALUE"
    key, _, _ = spec.partition("=")
    if not key:
        return f"Invalid env {spec!r}: key cannot be empty"
    return None
