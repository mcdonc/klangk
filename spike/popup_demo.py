"""Throwaway spike (#2385): mockup of the *persistent* consent-decider monitor.

Shows what ``klangk consent-decide`` would look like docked in a tmux popup
while you shell: connected status + the global duration selector, sitting in a
"waiting" state, then SIMULATES a held egress request arriving after a few
seconds to demonstrate the live update. ``a`` allows it (resolves), ``q`` hides
the popup (Ctrl-b p brings it back). Not production code -- DELETE after spike.
"""

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

WORKSPACE = "openclaw"
SELECTED = "tilrestart"
DURATIONS = ["once", "5m", "15m", "1h", "1d", "1w", "tilrestart", "forever"]


def _selector() -> str:
    # Selected duration highlighted, like the TUI's accent button.
    return "  ".join(
        f"[reverse]{d}[/reverse]" if d == SELECTED else d for d in DURATIONS
    )


class MonitorDemoApp(App):
    """Persistent consent-decider monitor mockup."""

    CSS = "Static { height: auto; }"
    BINDINGS = [("a", "allow", "Allow"), ("q", "quit", "Hide")]

    def __init__(self) -> None:
        super().__init__()
        self._hold: str | None = None
        self._secs = 0

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(id="title"),
            Static(_selector(), id="dur"),
            Static(id="list"),
            Static("a allow   q hide  (Ctrl-b p to reopen)", id="foot"),
        )

    def on_mount(self) -> None:
        self._render()
        self.set_timer(5, self._hold_arrives)  # simulate a held egress request
        self.set_interval(1, self._tick)

    def _hold_arrives(self) -> None:
        self._hold = "api.stripe.com:443  (curl)"
        self._secs = 120
        self._render()

    def _tick(self) -> None:
        if self._hold is not None and self._secs > 0:
            self._secs -= 1
            self._render()

    def action_allow(self) -> None:
        # 'allow' resolves the held request with the selected duration.
        self._hold = None
        self._secs = 0
        self._render()

    def _render(self) -> None:
        self.query_one("#title", Static).update(
            f"[bold]consent-decide · {WORKSPACE}[/bold]   connected"
        )
        body = (
            "No held requests — waiting…"
            if self._hold is None
            else f"▸ {self._hold}\n  expires in {self._secs}s"
        )
        self.query_one("#list", Static).update(body)


if __name__ == "__main__":
    MonitorDemoApp().run()
