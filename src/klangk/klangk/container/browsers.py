"""Browser-delegate routing: browser_id -> (workspace_id, sock) (#2542 split)."""


class BrowserRouter:
    """Browser-delegate routing: browser_id → (workspace_id, sock).

    Browser IDs are browser-generated UUIDs (sessionStorage) sent
    with terminal_start.  Unlike the old bridge tokens they survive
    browser refresh because the same sessionStorage UUID re-registers
    with the new WebSocket.

    Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self) -> None:
        self._browsers: dict[str, tuple[str, object | None]] = {}

    def register_browser(
        self, browser_id: str, workspace_id: str, sock: object
    ) -> None:
        """Register a browser ID for bridge routing.

        Idempotent: the same *browser_id* can re-register with a new
        *sock* after a browser refresh (sessionStorage keeps the ID).
        """
        self._browsers[browser_id] = (workspace_id, sock)

    def resolve_browser(self, browser_id: str) -> tuple[str, object] | None:
        """Look up (workspace_id, sock) for a browser ID."""
        return self._browsers.get(browser_id)

    def revoke_workspace_browsers(self, workspace_id: str) -> None:
        """Remove ALL browser registrations for a workspace.

        Called when a container is recreated or stopped.
        """
        to_remove = [
            bid
            for bid, (ws, _s) in self._browsers.items()
            if ws == workspace_id
        ]
        for bid in to_remove:
            del self._browsers[bid]

    def revoke_browser(self, sock: object) -> None:
        """Remove all browser registrations bound to a specific socket."""
        to_remove = [
            bid for bid, (_ws, s) in self._browsers.items() if s is sock
        ]
        for bid in to_remove:
            del self._browsers[bid]
