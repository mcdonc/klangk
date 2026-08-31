"""Container identity: name construction and browser-delegate routing.

Merges the former naming and browsers submodules (#2858) — both are
registry-support helpers (workspace container name formatting, and the
browser_id → (workspace_id, sock) routing table extracted from
``ContainerRegistry`` in #972), each imported only by ``registry.py`` and
the package ``__init__``.
"""

import re


# ---------------------------------------------------------------------------
# Container names (moved verbatim from the former naming submodule).
# ---------------------------------------------------------------------------


def workspace_name_slug(name: str, *, limit: int = 24) -> str:
    """Sanitize a workspace name for embedding in a container name (#2286).

    Podman container names must match ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``. Lowercase,
    collapse runs of non-``[a-z0-9]`` to a single ``-``, trim the ends, and cap
    the length. Returns ``""`` for names that reduce to nothing (empty,
    all-symbols) — callers fall back to an id-only name. The slug is decorative:
    uniqueness always comes from the instance id + workspace id, never the slug.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip())
    return slug[:limit].strip("-")


def workspace_container_name(iid: str, workspace_id: str, slug: str) -> str:
    """The workspace container name: iid + slugified name + id[:8] (#2286).

    Falls back to an id-only name when the slug is empty (all-symbol / missing
    name). Uniqueness is iid + id; the slug is decorative. The network sidecar
    name (:meth:`ContainerRegistry.network_sidecar_name`) shares the id[:8]
    tail so an id-prefix grep matches the pair.
    """
    if slug:
        return f"klangk-{iid}-{slug}-{workspace_id[:8]}"
    return f"klangk-{iid}-{workspace_id[:8]}"


# ---------------------------------------------------------------------------
# Browser-delegate routing (moved verbatim from the former browsers
# submodule).
# ---------------------------------------------------------------------------


class BrowserRouter:
    """Browser-delegate routing: browser_id → (workspace_id, sock).

    Browser IDs are browser-generated UUIDs (sessionStorage) sent
    with terminal_start.  Unlike the old bridge tokens they survive
    browser refresh because the same sessionStorage UUID re-registers
    with the new WebSocket.

    Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self) -> None:
        self.browsers: dict[str, tuple[str, object | None]] = {}

    def register_browser(
        self, browser_id: str, workspace_id: str, sock: object
    ) -> None:
        """Register a browser ID for bridge routing.

        Idempotent: the same *browser_id* can re-register with a new
        *sock* after a browser refresh (sessionStorage keeps the ID).
        """
        self.browsers[browser_id] = (workspace_id, sock)

    def resolve_browser(self, browser_id: str) -> tuple[str, object] | None:
        """Look up (workspace_id, sock) for a browser ID."""
        return self.browsers.get(browser_id)

    def revoke_workspace_browsers(self, workspace_id: str) -> None:
        """Remove ALL browser registrations for a workspace.

        Called when a container is recreated or stopped.
        """
        to_remove = [
            bid
            for bid, (ws, _s) in self.browsers.items()
            if ws == workspace_id
        ]
        for bid in to_remove:
            del self.browsers[bid]

    def revoke_browser(self, sock: object) -> None:
        """Remove all browser registrations bound to a specific socket."""
        to_remove = [
            bid for bid, (_ws, s) in self.browsers.items() if s is sock
        ]
        for bid in to_remove:
            del self.browsers[bid]
