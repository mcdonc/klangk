"""User accounts, handles, groups, external (OIDC) identities, agent user."""

import hashlib
import re
import uuid

from ._core import _fetchone, transaction

# Agent identity
AGENT_USER_ID = "00000000-0000-0000-0000-000000000001"


class AgentPrincipalError(ValueError):
    """Raised when an operation would make the agent an ACL principal.

    The system agent realizes its capabilities through in-container
    physical access, never ACL principalship (the "physical not
    principal" rule). Granting it a role, group membership, or ACE entry
    makes its global fixed UUID a privileged principal — a skeleton key
    if ever forgeable. Guarded at the model choke points
    (``add_user_to_group``, ``add_acl_entry``, ``delete_user``,
    ``update_password``); a global handler translates this to HTTP 400.
    Subclasses ``ValueError`` for compatibility with existing handlers.
    """


# Cached agent user dict (populated after seeding).
_agent_user_cache: dict | None = None


def clear_agent_cache() -> None:
    """Clear the cached agent user so the next lookup hits the DB."""
    global _agent_user_cache
    _agent_user_cache = None


async def get_agent_user() -> dict:
    """Return the agent user dict from DB, cached after first call."""
    global _agent_user_cache
    if _agent_user_cache is not None:
        return _agent_user_cache
    user = await get_user_by_id(AGENT_USER_ID)
    if user is None:
        # Fallback before seeding has run (should not happen at runtime).
        return {
            "id": AGENT_USER_ID,
            "email": "clanker@example.com",
            "handle": "clanker",
        }
    _agent_user_cache = user
    return user


async def agent_email() -> str:
    """Return the agent's email from the DB."""
    return (await get_agent_user())["email"]


async def agent_handle() -> str:
    """Return the agent's handle from the DB."""
    return (await get_agent_user())["handle"]


# --- Handle helpers ---

_HANDLE_RE = re.compile(r"^[a-z0-9._-]+$")
_RESERVED_HANDLES = frozenset({"work", ".users"})
_MAX_HANDLE_LEN = 32


def derive_handle(email: str) -> str:
    """Derive a handle from an email address local part."""
    local = email.split("@")[0] if "@" in email else email
    handle = re.sub(r"[^a-z0-9._-]", "", local.lower())
    if not handle:
        handle = "user"
    return handle[:_MAX_HANDLE_LEN]


def validate_handle(handle: str) -> str | None:
    """Return an error message if the handle is invalid, else None."""
    if not handle:
        return "Handle cannot be empty"
    if len(handle) > _MAX_HANDLE_LEN:
        return f"Handle must be {_MAX_HANDLE_LEN} characters or fewer"
    if handle.startswith("."):
        return "Handle cannot start with a dot"
    if handle in _RESERVED_HANDLES:
        return f"'{handle}' is reserved"
    if not _HANDLE_RE.match(handle):
        return (
            "Handle may only contain lowercase letters, digits,"
            " dots, dashes, and underscores"
        )
    return None


async def _unique_handle(db, base: str) -> str:
    """Return *base* if available, else append -2, -3, … until unique."""
    cursor = await db.execute("SELECT 1 FROM users WHERE handle = ?", (base,))
    if await cursor.fetchone() is None:
        return base
    for i in range(2, 10000):
        candidate = f"{base}-{i}"
        if len(candidate) > _MAX_HANDLE_LEN:
            candidate = f"{base[: _MAX_HANDLE_LEN - len(str(i)) - 1]}-{i}"
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE handle = ?", (candidate,)
        )
        if await cursor.fetchone() is None:
            return candidate
    return _hash_fallback_handle(base)


def _hash_fallback_handle(base: str) -> str:
    suffix = hashlib.sha256(base.encode()).hexdigest()[:8]
    return f"{base[: _MAX_HANDLE_LEN - 9]}-{suffix}"


async def _backfill_handles(db) -> None:
    """Assign handles to any users that don't have one yet."""
    cursor = await db.execute(
        "SELECT id, email FROM users WHERE handle IS NULL"
    )
    rows = await cursor.fetchall()
    for row in rows:
        base = derive_handle(row["email"])
        handle = await _unique_handle(db, base)
        await db.execute(
            "UPDATE users SET handle = ? WHERE id = ?",
            (handle, row["id"]),
        )
    if rows:
        await db.commit()


async def create_user(
    email: str,
    password_hash: str | None,
    verified: bool = False,
    provider: str = "local",
    external_id: str | None = None,
) -> dict:
    async with transaction() as db:
        user_id = str(uuid.uuid4())
        base = derive_handle(email)
        handle = await _unique_handle(db, base)
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified,"
            " provider, external_id, handle) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                email,
                password_hash,
                int(verified),
                provider,
                external_id,
                handle,
            ),
        )
        return {
            "id": user_id,
            "email": email,
            "handle": handle,
            "verified": verified,
        }


async def get_user_handle(user_id: str) -> str | None:
    """Return the handle for a user, or None if not found."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT handle FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["handle"] if row else None


async def set_user_handle(user_id: str, handle: str) -> None:
    """Update a user's handle. Raises ValueError on invalid or conflict."""
    error = validate_handle(handle)
    if error:
        raise ValueError(error)
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id FROM users WHERE handle = ? AND id != ?",
            (handle, user_id),
        )
        if await cursor.fetchone():
            raise ValueError(f"'{handle}' is already taken")
        await db.execute(
            "UPDATE users SET handle = ? WHERE id = ?",
            (handle, user_id),
        )


async def get_user_by_handle(handle: str) -> dict | None:
    """Find a user by handle."""
    row = await _fetchone(
        "SELECT id, email, handle FROM users WHERE handle = ?",
        (handle,),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "handle": row["handle"],
    }


async def get_user_by_external_id(
    provider: str, external_id: str
) -> dict | None:
    """Find a user by OIDC provider + external ID."""
    row = await _fetchone(
        "SELECT id, email, password_hash, verified, provider,"
        " external_id, handle"
        " FROM users WHERE provider = ? AND external_id = ?",
        (provider, external_id),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "verified": bool(row["verified"]),
        "provider": row["provider"],
        "external_id": row["external_id"],
        "handle": row["handle"],
    }


async def link_oidc_identity(
    user_id: str, provider: str, external_id: str
) -> None:
    """Link an OIDC identity to an existing user."""
    async with transaction() as db:
        await db.execute(
            "UPDATE users SET provider = ?, external_id = ? WHERE id = ?",
            (provider, external_id, user_id),
        )


async def verify_user(user_id: str) -> bool:
    """Mark a user as verified. Returns True if updated, False if not found."""
    async with transaction() as db:
        cursor = await db.execute(
            "UPDATE users SET verified = 1 WHERE id = ?", (user_id,)
        )
        return cursor.rowcount > 0


# --- Group operations ---


async def create_group(
    name: str, description: str | None = None, group_id: str | None = None
) -> dict:
    """Create a group. Returns the group dict."""
    async with transaction() as db:
        gid = group_id or str(uuid.uuid4())
        await db.execute(
            "INSERT INTO groups (id, name, description) VALUES (?, ?, ?)",
            (gid, name, description),
        )
        return {"id": gid, "name": name, "description": description}


async def get_group_by_name(name: str) -> dict | None:
    """Find a group by name."""
    row = await _fetchone(
        "SELECT id, name, description, created_at FROM groups WHERE name = ?",
        (name,),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
    }


async def get_group_by_id(group_id: str) -> dict | None:
    """Find a group by ID."""
    row = await _fetchone(
        "SELECT id, name, description, created_at FROM groups WHERE id = ?",
        (group_id,),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
    }


_ADMIN_GROUP_SORT_COLUMNS = {
    "name": "name",
    "created": "created_at",
}


async def list_groups(
    page: int = 1,
    page_size: int = 10,
    sort: str = "name",
    order: str = "asc",
    q: str | None = None,
) -> dict:
    """List groups with server-side pagination, sorting, and filtering.

    Returns a paged envelope ``{"groups", "page", "page_size", "total"}``
    suitable for forwards/backwards paging. Callers that still want a
    bare list (e.g. the non-admin ``GET /groups`` endpoint) can read
    ``result["groups"]``.
    """
    sort_col = _ADMIN_GROUP_SORT_COLUMNS.get(sort, "name")
    direction = "DESC" if order.lower() == "desc" else "ASC"
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    async with transaction() as db:
        where_clause = ""
        params: list = []
        if q:
            where_clause = " WHERE name LIKE ?"
            params.append(f"%{q}%")

        count_cursor = await db.execute(
            f"SELECT COUNT(*) AS c FROM groups{where_clause}",
            params,
        )
        total = (await count_cursor.fetchone())["c"]

        cursor = await db.execute(
            "SELECT id, name, description, created_at"
            f" FROM groups{where_clause}"
            f" ORDER BY {sort_col} {direction}, id"
            " LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        groups = [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
            }
            for row in await cursor.fetchall()
        ]
        return {
            "groups": groups,
            "page": page,
            "page_size": page_size,
            "total": total,
        }


async def delete_group(group_id: str) -> bool:
    """Delete a group. Returns True if deleted."""
    async with transaction() as db:
        cursor = await db.execute(
            "DELETE FROM groups WHERE id = ?", (group_id,)
        )
        return cursor.rowcount > 0


async def update_group(
    group_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    """Update group name/description. Returns True if updated."""
    updates = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [group_id]
    async with transaction() as db:
        cursor = await db.execute(
            f"UPDATE groups SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        return cursor.rowcount > 0


async def add_user_to_group(
    user_id: str, group_id: str, source: str = "manual"
) -> None:
    """Add a user to a group (idempotent).

    Raises ``AgentPrincipalError`` if the target is the system agent.
    """
    if user_id == AGENT_USER_ID:
        raise AgentPrincipalError(
            "The system agent cannot be added to groups"
            " (global fixed UUID — granting it cross-workspace"
            " blast radius)."
        )
    async with transaction() as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_groups (user_id, group_id, source)"
            " VALUES (?, ?, ?)",
            (user_id, group_id, source),
        )


async def remove_user_from_group(user_id: str, group_id: str) -> bool:
    """Remove a user from a group. Returns True if removed."""
    async with transaction() as db:
        cursor = await db.execute(
            "DELETE FROM user_groups WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        )
        return cursor.rowcount > 0


async def get_group_members(group_id: str) -> list[dict]:
    """List users in a group."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT u.id, u.email, ug.source FROM users u"
            " JOIN user_groups ug ON u.id = ug.user_id"
            " WHERE ug.group_id = ?"
            " ORDER BY u.email",
            (group_id,),
        )
        return [
            {"id": row["id"], "email": row["email"], "source": row["source"]}
            for row in await cursor.fetchall()
        ]


async def get_user_group_ids(user_id: str) -> list[str]:
    """Get all group IDs for a user."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT group_id FROM user_groups WHERE user_id = ?",
            (user_id,),
        )
        return [row["group_id"] for row in await cursor.fetchall()]


async def get_user_oidc_sync_group_ids(user_id: str) -> list[str]:
    """Get group IDs where membership source is 'oidc_sync'."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT group_id FROM user_groups"
            " WHERE user_id = ? AND source = 'oidc_sync'",
            (user_id,),
        )
        return [row["group_id"] for row in await cursor.fetchall()]


async def get_user_groups(user_id: str) -> list[dict]:
    """Get all groups a user belongs to."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT g.id, g.name, g.description FROM groups g"
            " JOIN user_groups ug ON g.id = ug.group_id"
            " WHERE ug.user_id = ?"
            " ORDER BY g.name",
            (user_id,),
        )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
            }
            for row in await cursor.fetchall()
        ]


async def get_user_by_email(email: str) -> dict | None:
    row = await _fetchone(
        "SELECT id, email, password_hash, verified, provider,"
        " external_id, handle"
        " FROM users WHERE email = ?",
        (email,),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "verified": bool(row["verified"]),
        "provider": row["provider"],
        "external_id": row["external_id"],
        "handle": row["handle"],
    }


_ADMIN_USER_SORT_COLUMNS = {
    "email": "email",
    "handle": "handle",
    "created": "created_at",
}


async def list_users(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
) -> dict:
    """List users with server-side pagination, sorting, and filtering.

    Returns a paged envelope ``{"users", "page", "page_size", "total"}``
    suitable for forwards/backwards paging. Per-user groups are no longer
    included — the list view doesn't need them and fetching them was an
    N+1 query (one per user). Group membership is available via the
    ``GET /admin/groups/{id}/members`` endpoint.
    """
    sort_col = _ADMIN_USER_SORT_COLUMNS.get(sort, "created_at")
    direction = "DESC" if order.lower() == "desc" else "ASC"
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    async with transaction() as db:
        where_clause = ""
        params: list = []
        if q:
            where_clause = " WHERE email LIKE ?"
            params.append(f"%{q}%")

        count_cursor = await db.execute(
            f"SELECT COUNT(*) AS c FROM users{where_clause}",
            params,
        )
        total = (await count_cursor.fetchone())["c"]

        cursor = await db.execute(
            "SELECT id, email, handle, verified, provider, created_at"
            f" FROM users{where_clause}"
            f" ORDER BY {sort_col} {direction}, id"
            " LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        users = [
            {
                "id": row["id"],
                "email": row["email"],
                "handle": row["handle"],
                "verified": bool(row["verified"]),
                "provider": row["provider"],
                "created_at": row["created_at"],
            }
            for row in await cursor.fetchall()
        ]
        return {
            "users": users,
            "page": page,
            "page_size": page_size,
            "total": total,
        }


async def delete_user(user_id: str) -> bool:
    """Delete a user. Returns True if deleted, False if not found.

    Raises ``AgentPrincipalError`` if the target is the system agent.
    """
    if user_id == AGENT_USER_ID:
        raise AgentPrincipalError("Cannot delete the system agent user")
    async with transaction() as db:
        cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


async def update_email(user_id: str, email: str) -> None:
    """Update a user's email."""
    async with transaction() as db:
        await db.execute(
            "UPDATE users SET email = ? WHERE id = ?", (email, user_id)
        )


async def update_password(user_id: str, password_hash: str) -> None:
    """Update a user's password hash.

    Raises ``AgentPrincipalError`` if the target is the system agent
    (the agent must never have a password).
    """
    if user_id == AGENT_USER_ID:
        raise AgentPrincipalError(
            "Cannot set a password on the system agent user"
        )
    async with transaction() as db:
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )


async def get_user_by_id(user_id: str) -> dict | None:
    row = await _fetchone(
        "SELECT id, email, handle FROM users WHERE id = ?",
        (user_id,),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "handle": row["handle"],
    }


async def search_users(query: str, limit: int = 10) -> list[dict]:
    """Search users by email prefix."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, email, handle FROM users"
            " WHERE email LIKE ? ORDER BY email LIMIT ?",
            (f"{query}%", limit),
        )
        return [
            {"id": row["id"], "email": row["email"], "handle": row["handle"]}
            for row in await cursor.fetchall()
        ]
