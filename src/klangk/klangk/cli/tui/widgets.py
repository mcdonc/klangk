"""StatusBar widget for the klangk TUI."""

from __future__ import annotations

from textual.widgets import Static


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
        self.update(text)
