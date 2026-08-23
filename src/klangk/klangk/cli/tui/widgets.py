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
        last_login: str | None = None,
    ) -> None:
        # The live `extra` segment (host notices, the #2661 schedule
        # countdown) renders FIRST when set: it is the time-sensitive
        # bit, and appending it last let it fall off the right edge of
        # a typical terminal once server/user/last-login (~76 cols)
        # had claimed the row — an invisible countdown on the very
        # screens that need it.
        text = ""
        if extra:
            text += f"{extra}"
        text += (
            f"{'   |   ' if text else ''}server: {server or '(none)'}"
            f"   |   user: {user or '(not logged in)'}"
        )
        if last_login:
            text += f"   |   last login: {last_login}"
        # Render literally — server URL / user / live `extra` may contain
        # bracket characters that would otherwise be parsed as markup.
        self.update(Text(text))
