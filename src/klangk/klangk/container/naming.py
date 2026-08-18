"""Container-name helpers (issue #2542 split of the old container.py)."""

import re


def _workspace_name_slug(name: str, *, limit: int = 24) -> str:
    """Sanitize a workspace name for embedding in a container name (#2286).

    Podman container names must match ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``. Lowercase,
    collapse runs of non-``[a-z0-9]`` to a single ``-``, trim the ends, and cap
    the length. Returns ``""`` for names that reduce to nothing (empty,
    all-symbols) — callers fall back to an id-only name. The slug is decorative:
    uniqueness always comes from the instance id + workspace id, never the slug.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip())
    return slug[:limit].strip("-")


def _workspace_container_name(iid: str, workspace_id: str, slug: str) -> str:
    """The workspace container name: iid + slugified name + id[:8] (#2286).

    Falls back to an id-only name when the slug is empty (all-symbol / missing
    name). Uniqueness is iid + id; the slug is decorative. The network sidecar
    name (:meth:`ContainerRegistry._network_sidecar_name`) shares the id[:8]
    tail so an id-prefix grep matches the pair.
    """
    if slug:
        return f"klangk-{iid}-{slug}-{workspace_id[:8]}"
    return f"klangk-{iid}-{workspace_id[:8]}"
