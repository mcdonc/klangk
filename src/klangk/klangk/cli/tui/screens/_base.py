"""Shared base classes and small utility screens for the TUI."""

from __future__ import annotations

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Input,
    ListItem,
    ListView,
    Static,
    Tabs,
)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no confirmation dialog. Dismisses with True on confirm."""

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
        yes_label: str = "Delete",
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


class DuplicateScreen(ModalScreen):
    """Prompt for a new name to duplicate a workspace under."""

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
            Static(Text(f"Duplicate '{self._source}' as:")),
            Input(value=f"{self._source}-copy", id="dup_name"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("Duplicate", id="ok", variant="primary"),
            ),
            id="dup_box",
        )

    def _commit(self) -> None:
        name = self.query_one("#dup_name", Input).value.strip()
        self.dismiss(name or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._commit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "dup_name":
            self._commit()
