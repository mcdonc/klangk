"""StatusBar widget for the klangk TUI."""

from __future__ import annotations

from textual.widgets import Static

from rich.text import Text


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
    ) -> None:
        text = (
            f"server: {server or '(none)'}"
            f"   |   user: {user or '(not logged in)'}"
        )
        if extra:
            text += f"   |   {extra}"
        # Render literally — server URL / user / live `extra` may contain
        # bracket characters that would otherwise be parsed as markup.
        self.update(Text(text))
