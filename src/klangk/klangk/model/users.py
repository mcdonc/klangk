"""User accounts, handles, groups, external (OIDC) identities, agent user."""

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

from .base import Submodel


# Agent identity
AGENT_USER_ID = "00000000-0000-0000-0000-000000000001"
# Unseeded fallback handle/email (before seed_agent_user runs). Single
# source of truth for the agent identity used by get_agent_user and the
# migration-safe handle resolver in unique_handle (#1160). The agent
# *is* the klangk user (#2718): its handle matches the container's UNIX
# username, and the identity is fixed — not configurable, not editable.
_DEFAULT_AGENT_HANDLE = "klangk"
_DEFAULT_AGENT_EMAIL = "klangk@example.com"

# Public aliases (#2718): the fixed agent identity is API surface now
# (seeded by Lifecycle, referenced by docs/tests) — import these, not
# the underscore-prefixed names above.
AGENT_HANDLE = _DEFAULT_AGENT_HANDLE
AGENT_EMAIL = _DEFAULT_AGENT_EMAIL

# The instance-admin group (#2934, #2995): membership is the source
# of truth for "is an instance admin" (the /my-permissions is_admin
# flag). Named ``admins`` since #2934; migration 0020 renames legacy
# deployments' ``admin`` row. Import this instead of spelling the name.
ADMIN_GROUP_NAME = "admins"

# Group source markers (#2750): who created a ``groups`` row. Mirrors
# ``user_groups.source``. 'manual' is the default (human-created via the
# API, plus the boot-seeded admin/members groups and OIDC-synced groups);
# 'workspace-role' marks the four per-workspace role groups seeded by
# ``WorkspacesModel.seed_workspace_acl`` so global group lists can hide
# them and teardown can find them without reconstructing names.
GROUP_SOURCE_MANUAL = "manual"
GROUP_SOURCE_WORKSPACE_ROLE = "workspace-role"
GROUP_SOURCES = frozenset({GROUP_SOURCE_MANUAL, GROUP_SOURCE_WORKSPACE_ROLE})


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


class WorkspaceRoleScopeError(ValueError):
    """Raised when an operation would violate a per-workspace role
    group's scope (#2750).

    Role groups carry their workspace id in their name
    (``<role>-<workspace_id>``) and are identified by their ``source``
    marker plus that suffix — teardown, ownership transfer, and the ACL
    scope guard all parse it. Raised when an ACL write would grant a
    role group on anything other than its own workspace's resource, or
    when a rename would break the name↔workspace link. Guarded at the
    model choke points; a global handler translates this to HTTP 400,
    like ``AgentPrincipalError``.
    """


class AdminGroupProtectionError(ValueError):
    """Raised when a write would break the ``admins`` group's identity
    (#2995).

    The ``/my-permissions`` ``is_admin`` flag derives from membership
    in a group *named* ``admins`` — renaming that group away strips
    every instance-admin's status (and the inactivity sweep's admin
    exemption), while renaming another group onto the name (or deleting
    the real one and re-creating it) lets a delegated group manager
    mint an ``admins`` group of their own. Renames and deletes of the
    ``admins`` group are rejected at the model choke points; a global
    handler translates this to HTTP 400, like
    ``AgentPrincipalError``.
    """


# Cached agent user dict (populated after seeding).
agent_user_cache: dict | None = None


def clear_agent_cache() -> None:
    """Clear the cached agent user so the next lookup hits the DB."""
    global agent_user_cache
    agent_user_cache = None


HANDLE_RE = re.compile(r"^[a-z0-9._-]+$")
# `klangk` is the agent's fixed handle (#2718) — and it doubles as the
# container UNIX user / shared home name under #2169/#2717, so a human
# claiming it would collide with /home/klangk. Statically reserved.
RESERVED_HANDLES = frozenset({"work", ".users", "klangk"})
MAX_HANDLE_LEN = 32


def derive_handle(email: str) -> str:
    """Derive a handle from an email address local part."""
    local = email.split("@")[0] if "@" in email else email
    handle = re.sub(r"[^a-z0-9._-]", "", local.lower())
    if not handle:
        handle = "user"
    return handle[:MAX_HANDLE_LEN]


def _handle_rule_error(handle: str) -> str | None:
    """The dot-prefix, reserved-set, and charset static rules."""
    if handle.startswith("."):
        return "Handle cannot start with a dot"
    if handle in RESERVED_HANDLES:
        return f"'{handle}' is reserved"
    if not HANDLE_RE.match(handle):
        return (
            "Handle may only contain lowercase letters, digits,"
            " dots, dashes, and underscores"
        )
    return None


def validate_handle(handle: str) -> str | None:
    """Return an error message if the handle is invalid, else None.

    Note: this only checks *static* rules (length, charset, the fixed
    reserved set — which includes the agent's handle `klangk`, #2718).
    """
    if not handle:
        return "Handle cannot be empty"
    if len(handle) > MAX_HANDLE_LEN:
        return f"Handle must be {MAX_HANDLE_LEN} characters or fewer"
    return _handle_rule_error(handle)


async def _live_agent_handle(db) -> str:
    """The agent's current handle, or the default pre-seed.

    Resolves on the *passed* connection (not via the cached
    :func:`agent_handle`, which opens a fresh connection): callers run
    during DB migration where the ``handle`` column's schema change is
    uncommitted on *db* but invisible to a new connection.
    """
    cursor = await db.execute(
        "SELECT handle FROM users WHERE id = ?", (AGENT_USER_ID,)
    )
    row = await cursor.fetchone()
    if row and row[0]:
        return row[0]
    return _DEFAULT_AGENT_HANDLE


async def _handle_taken(db, candidate: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM users WHERE handle = ?", (candidate,)
    )
    return await cursor.fetchone() is not None


def _suffixed_handle(base: str, i: int) -> str:
    """``base-i``, truncated to fit the handle length cap."""
    candidate = f"{base}-{i}"
    if len(candidate) <= MAX_HANDLE_LEN:
        return candidate
    return f"{base[: MAX_HANDLE_LEN - len(str(i)) - 1]}-{i}"


async def unique_handle(db, base: str) -> str:
    """Return *base* if available, else append -2, -3, … until unique.

    The live agent handle is always treated as taken (#1160) — even if
    the agent row hasn't been seeded yet — so a derived handle never
    collides with ``/home/<agent_handle>``.
    """
    agent = await _live_agent_handle(db)
    # Try base, then base-2, base-3, …; skip the agent handle each time.
    candidate = base
    i = 1
    while i < 10000:
        if candidate != agent and not await _handle_taken(db, candidate):
            return candidate
        i += 1
        candidate = _suffixed_handle(base, i)
    return hash_fallback_handle(base)


def hash_fallback_handle(base: str) -> str:
    suffix = hashlib.sha256(base.encode()).hexdigest()[:8]
    return f"{base[: MAX_HANDLE_LEN - 9]}-{suffix}"


async def generate_handle(db, email: str) -> str:
    """Return a unique handle derived from *email* on connection *db*.

    This is the single shared handle generator. Every codepath that
    creates a user — :func:`create_user`, the email-verification
    register route, the admin invite route, and :func:`backfill_handles`
    — must go through here so handle derivation and uniqueness stay in
    sync (regression: #1256, where the email-verification routes did a
    raw ``INSERT`` with no handle and got ``NULL``).
    """
    return await unique_handle(db, derive_handle(email))


async def backfill_handles(db) -> None:
    """Assign handles to any users that don't have one yet."""
    cursor = await db.execute(
        "SELECT id, email FROM users WHERE handle IS NULL"
    )
    rows = await cursor.fetchall()
    for row in rows:
        handle = await generate_handle(db, row["email"])
        await db.execute(
            "UPDATE users SET handle = ? WHERE id = ?",
            (handle, row["id"]),
        )
    if rows:
        await db.commit()


_ADMIN_GROUP_SORT_COLUMNS = {
    "name": "name",
    "created": "created_at",
}


ADMIN_USER_SORT_COLUMNS = {
    "email": "email",
    "handle": "handle",
    "created": "created_at",
}


# Shared user-row SELECT fragment and mapper (#2551): every user lookup
# selects the same columns and builds the same dict; one definition keeps
# new columns from drifting between lookups.
_USER_COLUMNS = (
    "SELECT id, email, password_hash, verified, provider, external_id,"
    " handle, disabled, last_activity_at"
)


def _user_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "verified": bool(row["verified"]),
        "provider": row["provider"],
        "external_id": row["external_id"],
        "handle": row["handle"],
        "disabled": bool(row["disabled"]),
        "last_activity_at": row["last_activity_at"],
    }


def parse_user_ts(value: str | None) -> datetime | None:
    """Parse a users-table timestamp into an aware UTC datetime (#2588).

    The table mixes formats: ``created_at`` is SQLite's ``datetime('now')``
    (``2026-01-15 10:00:00``, naive), while ``last_login_at`` and
    ``last_activity_at`` are ``datetime.now(timezone.utc).isoformat()``
    (aware). Naive values are assumed UTC (that is how they are written).
    Unparseable values parse as None (the sweep skips a user whose
    timestamps cannot be judged).
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _user_is_inactive(row, cutoff) -> bool:
    """Whether a user's newest of last_activity/last_login/created (a
    never-used account ages from creation) predates *cutoff*."""
    stamps = [
        ts
        for ts in (
            parse_user_ts(row["last_activity_at"]),
            parse_user_ts(row["last_login_at"]),
            parse_user_ts(row["created_at"]),
        )
        if ts is not None
    ]
    return bool(stamps) and max(stamps) < cutoff


def _group_filter_clause(q: str | None, source: str | None) -> tuple:
    """WHERE clause + params for the group-list filters.

    *source* filters by the origin marker (#2750): ``None`` shows all,
    ``'manual'`` hides the seeded workspace-role groups,
    ``'workspace-role'`` shows only them.
    """
    where_parts: list[str] = []
    params: list = []
    if q:
        where_parts.append("name LIKE ?")
        params.append(f"%{q}%")
    if source is not None:
        where_parts.append("source = ?")
        params.append(source)
    if not where_parts:
        return "", []
    return f" WHERE {' AND '.join(where_parts)}", params


def _group_update_fields(name: str | None, description: str | None) -> dict:
    """The provided (non-None) update fields for update_group."""
    updates = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    return updates


def _exempt_from_inactivity_sweep(user_id: str, admin_ids: set[str]) -> bool:
    """The system agent and admins are never auto-disabled (#2588)."""
    return user_id == AGENT_USER_ID or user_id in admin_ids


class UsersModel(Submodel):
    """User/group/handle operations, resolved through ``app_state.db``.

    Constructed by :class:`~klangk.model.model.Model` and reached
    via ``app_state.model.users``. Reaches the DB through
    ``self.app.state.db`` (the single DB instance for the whole app).
    """

    async def get_agent_user(self) -> dict:
        """Return the agent user dict from DB, cached after first call."""
        global agent_user_cache
        if agent_user_cache is not None:
            return agent_user_cache
        user = await self.get_user_by_id(AGENT_USER_ID)
        if user is None:
            return {
                "id": AGENT_USER_ID,
                "email": _DEFAULT_AGENT_EMAIL,
                "handle": _DEFAULT_AGENT_HANDLE,
            }
        agent_user_cache = user
        return user

    async def agent_email(self) -> str:
        """Return the agent's email from the DB."""
        return (await self.get_agent_user())["email"]

    async def agent_handle(self) -> str:
        """Return the agent's handle from the DB."""
        return (await self.get_agent_user())["handle"]

    def clear_agent_cache(self) -> None:
        """Clear the cached agent user so the next lookup hits the DB."""
        global agent_user_cache
        agent_user_cache = None

    async def unique_handle(self, db, base: str) -> str:
        """Return a unique handle on the passed connection.

        Thin delegation to the module-level :func:`unique_handle` (the
        single implementation — the copies had drifted only in a
        docstring cross-reference): both the model path and the DB-migration
        path resolve the agent handle on the *passed* connection for the
        same uncommitted-schema reason (see that function's docstring).
        """
        return await unique_handle(db, base)

    async def generate_handle(self, db, email: str) -> str:
        """Return a unique handle derived from *email* on connection *db*.

        This is the single shared handle generator. Every codepath that
        creates a user — :meth:`create_user`, the email-verification
        register route, the admin invite route, and :meth:`backfill_handles`
        — must go through here so handle derivation and uniqueness stay in
        sync (regression: #1256, where the email-verification routes did a
        raw ``INSERT`` with no handle and got ``NULL``).
        """
        return await self.unique_handle(db, derive_handle(email))

    async def backfill_handles(self, db) -> None:
        """Assign handles to any users that don't have one yet."""
        cursor = await db.execute(
            "SELECT id, email FROM users WHERE handle IS NULL"
        )
        rows = await cursor.fetchall()
        for row in rows:
            handle = await self.generate_handle(db, row["email"])
            await db.execute(
                "UPDATE users SET handle = ? WHERE id = ?",
                (handle, row["id"]),
            )
        if rows:
            await db.commit()

    async def create_user(
        self,
        email: str,
        password_hash: str | None,
        verified: bool = False,
        provider: str = "local",
        external_id: str | None = None,
    ) -> dict:
        async with self.app.state.db.transaction() as db:
            user_id = str(uuid.uuid4())
            handle = await self.generate_handle(db, email)
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
        # #2569: auto-add to the members group if it exists.
        members_gid = getattr(self.app.state, "members_group_id", None)
        if members_gid:
            await self.add_user_to_group(user_id, members_gid)
        return {
            "id": user_id,
            "email": email,
            "handle": handle,
            "verified": verified,
            # Explicit so the dict never silently lacks the key the
            # ``ensure_not_disabled`` gate reads (#2588 review) — a
            # fresh user is enabled by definition.
            "disabled": False,
        }

    async def insert_unverified_user(
        self, db, user_id: str, email: str, password_hash: str
    ) -> str:
        """Insert an unverified user row with a generated handle.

        Runs on the **caller's** transaction so it composes with a follow-up
        verification-email send (see the module-level docstring). Returns
        the generated handle.
        """
        handle = await self.generate_handle(db, email)
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified, handle)"
            " VALUES (?, ?, ?, 0, ?)",
            (user_id, email, password_hash, handle),
        )
        return handle

    async def get_user_handle(self, user_id: str) -> str | None:
        """Return the handle for a user, or None if not found."""
        row = await self.app.state.db.fetchone(
            "SELECT handle FROM users WHERE id = ?", (user_id,)
        )
        return row["handle"] if row else None

    async def set_user_handle(self, user_id: str, handle: str) -> None:
        """Update a user's handle. Raises ValueError on invalid or conflict.

        The agent row is immutable (#2718): its handle is the fixed
        `klangk` identity — renaming it would break the constant
        `/home/klangk` service-session HOME (#2717).
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "The system agent's handle cannot be changed"
                " (fixed identity 'klangk', #2718)"
            )
        error = validate_handle(handle)
        if error:
            raise ValueError(error)
        async with self.app.state.db.transaction() as db:
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

    async def get_user_by_handle(self, handle: str) -> dict | None:
        """Find a user by handle."""
        row = await self.app.state.db.fetchone(
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
        self, provider: str, external_id: str
    ) -> dict | None:
        """Find a user by OIDC provider + external ID."""
        row = await self.app.state.db.fetchone(
            _USER_COLUMNS
            + " FROM users WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        )
        if row is None:
            return None
        return _user_row_to_dict(row)

    async def link_oidc_identity(
        self, user_id: str, provider: str, external_id: str
    ) -> None:
        """Link an OIDC identity to an existing user."""
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE users SET provider = ?, external_id = ? WHERE id = ?",
                (provider, external_id, user_id),
            )

    async def verify_user(self, user_id: str) -> bool:
        """Mark a user as verified. Returns True if updated, False if not found."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE users SET verified = 1 WHERE id = ?", (user_id,)
            )
            return cursor.rowcount > 0

    async def create_group(
        self,
        name: str,
        description: str | None = None,
        group_id: str | None = None,
        source: str = GROUP_SOURCE_MANUAL,
    ) -> dict:
        """Create a group. Returns the group dict.

        *source* marks who owns the row (#2750): ``manual`` for
        human-managed groups, ``workspace-role`` for the per-workspace
        role groups seeded by ``seed_workspace_acl``.
        """
        if source not in GROUP_SOURCES:
            raise ValueError(f"Invalid group source: {source!r}")
        async with self.app.state.db.transaction() as db:
            gid = group_id or str(uuid.uuid4())
            await db.execute(
                "INSERT INTO groups (id, name, description, source)"
                " VALUES (?, ?, ?, ?)",
                (gid, name, description, source),
            )
            return {
                "id": gid,
                "name": name,
                "description": description,
                "source": source,
            }

    async def _get_group_by(self, where: str, value: str) -> dict | None:
        """Fetch one group row matching *where* and map it to a dict."""
        row = await self.app.state.db.fetchone(
            "SELECT id, name, description, source, created_at"
            f" FROM groups WHERE {where}",
            (value,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "source": row["source"],
            "created_at": row["created_at"],
        }

    async def get_group_by_name(self, name: str) -> dict | None:
        """Find a group by name."""
        return await self._get_group_by("name = ?", name)

    async def get_group_by_id(self, group_id: str) -> dict | None:
        """Find a group by ID."""
        return await self._get_group_by("id = ?", group_id)

    async def list_groups(
        self,
        page: int = 1,
        page_size: int = 10,
        sort: str = "name",
        order: str = "asc",
        q: str | None = None,
        source: str | None = None,
    ) -> dict:
        """List groups with server-side pagination, sorting, and filtering.

        *source* filters by the origin marker (#2750): ``None`` shows all,
        ``'manual'`` hides the seeded workspace-role groups,
        ``'workspace-role'`` shows only them. Rows carry ``source``.
        """
        sort_col = _ADMIN_GROUP_SORT_COLUMNS.get(sort, "name")
        direction = "DESC" if order.lower() == "desc" else "ASC"
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        where_clause, params = _group_filter_clause(q, source)

        async with self.app.state.db.transaction() as db:
            count_cursor = await db.execute(
                f"SELECT COUNT(*) AS c FROM groups{where_clause}",  # noqa: S608
                params,
            )
            total = (await count_cursor.fetchone())["c"]

            cursor = await db.execute(
                "SELECT id, name, description, source, created_at"
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
                    "source": row["source"],
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

    async def delete_group(self, group_id: str) -> bool:
        """Delete a group. Returns True if deleted.

        Raises ``AdminGroupProtectionError`` for the ``admins`` group
        (#2995): deleting it would strip every instance-admin's
        ``is_admin`` (and the inactivity sweep's admin exemption), and
        the next boot would mint an empty replacement group.
        """
        await self._reject_admin_group_delete(group_id)
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM groups WHERE id = ?", (group_id,)
            )
            return cursor.rowcount > 0

    async def update_group(
        self,
        group_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> bool:
        """Update group name/description. Returns True if updated.

        Raises ``WorkspaceRoleScopeError`` if *name* is set on a
        ``workspace-role`` group (#2750): role groups are found by the
        source marker **plus** the workspace-id suffix of their name
        (teardown, ownership transfer, and the ACL scope guard all parse
        it), so a rename would orphan them on delete or point the guard
        at the wrong workspace. Descriptions stay editable. Raises
        ``AdminGroupProtectionError`` when the rename would move the
        ``admins`` name on or off a group (#2995).
        """
        await self._reject_role_group_rename(group_id, name)
        await self._reject_admin_group_rename(group_id, name)
        updates = _group_update_fields(name, description)
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [group_id]
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                f"UPDATE groups SET {set_clause} WHERE id = ?",  # noqa: S608
                values,
            )
            return cursor.rowcount > 0

    async def _reject_role_group_rename(
        self, group_id: str, name: str | None
    ) -> None:
        """Guard: a workspace-role group's name is system-managed (#2750)."""
        if name is None:
            return
        row = await self.app.state.db.fetchone(
            "SELECT source FROM groups WHERE id = ?", (group_id,)
        )
        if row is None or row["source"] != GROUP_SOURCE_WORKSPACE_ROLE:
            return
        raise WorkspaceRoleScopeError(
            "Workspace role group names are managed by the system"
            " and cannot be changed"
        )

    async def _group_name(self, group_id: str) -> str | None:
        """The group's current name, or None when the id is unknown."""
        row = await self.app.state.db.fetchone(
            "SELECT name FROM groups WHERE id = ?", (group_id,)
        )
        return row["name"] if row is not None else None

    async def _reject_admin_group_rename(
        self, group_id: str, name: str | None
    ) -> None:
        """Guard: the ``admins`` group's name is load-bearing (#2995).

        Renaming the group away flips every instance-admin's
        ``is_admin`` off; renaming another group onto the name mints a
        fake ``admins`` group. Both rejected; descriptions stay
        editable and a same-name no-op passes.
        """
        if name is None:
            return
        current = await self._group_name(group_id)
        if current is None:
            return
        if current == ADMIN_GROUP_NAME and name != ADMIN_GROUP_NAME:
            raise AdminGroupProtectionError(
                "The admins group cannot be renamed: instance-admin"
                " status derives from its name (#2995)"
            )
        if current != ADMIN_GROUP_NAME and name == ADMIN_GROUP_NAME:
            raise AdminGroupProtectionError(
                "The name 'admins' is reserved for the instance-admin"
                " group (#2995)"
            )

    async def _reject_admin_group_delete(self, group_id: str) -> None:
        """Guard: the ``admins`` group cannot be deleted (#2995) —
        ``is_admin`` derives from membership in it, so a delete strips
        every instance-admin's status and the next boot would mint an
        empty replacement."""
        if await self._group_name(group_id) == ADMIN_GROUP_NAME:
            raise AdminGroupProtectionError(
                "The admins group cannot be deleted: instance-admin"
                " status derives from membership in it (#2995)"
            )

    async def add_user_to_group(
        self, user_id: str, group_id: str, source: str = "manual"
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
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_groups (user_id, group_id, source)"
                " VALUES (?, ?, ?)",
                (user_id, group_id, source),
            )

    async def remove_user_from_group(
        self, user_id: str, group_id: str
    ) -> bool:
        """Remove a user from a group. Returns True if removed."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM user_groups WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            return cursor.rowcount > 0

    async def get_group_members(self, group_id: str) -> list[dict]:
        """List users in a group."""
        rows = await self.app.state.db.fetchall(
            "SELECT u.id, u.email, ug.source FROM users u"
            " JOIN user_groups ug ON u.id = ug.user_id"
            " WHERE ug.group_id = ?"
            " ORDER BY u.email",
            (group_id,),
        )
        return [
            {
                "id": row["id"],
                "email": row["email"],
                "source": row["source"],
            }
            for row in rows
        ]

    async def get_user_group_ids(self, user_id: str) -> list[str]:
        """Get all group IDs for a user."""
        rows = await self.app.state.db.fetchall(
            "SELECT group_id FROM user_groups WHERE user_id = ?",
            (user_id,),
        )
        return [row["group_id"] for row in rows]

    async def get_user_oidc_sync_group_ids(self, user_id: str) -> list[str]:
        """Get group IDs where membership source is 'oidc_sync'."""
        rows = await self.app.state.db.fetchall(
            "SELECT group_id FROM user_groups"
            " WHERE user_id = ? AND source = 'oidc_sync'",
            (user_id,),
        )
        return [row["group_id"] for row in rows]

    async def get_user_groups(self, user_id: str) -> list[dict]:
        """Get all groups a user belongs to."""
        rows = await self.app.state.db.fetchall(
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
            for row in rows
        ]

    async def get_user_by_email(self, email: str) -> dict | None:
        row = await self.app.state.db.fetchone(
            _USER_COLUMNS + " FROM users WHERE email = ?",
            (email,),
        )
        if row is None:
            return None
        return _user_row_to_dict(row)

    async def get_user_by_identifier(self, identifier: str) -> dict | None:
        """Resolve a user by email or handle (#616).

        Dispatches on whether *identifier* contains ``@``: emails always
        contain it and handles never do (the handle charset is
        ``[a-z0-9._-]``; see :func:`derive_handle`), so the two
        namespaces are syntactically disjoint and the dispatch is
        unambiguous. Returns the same full row shape as
        :meth:`get_user_by_email` (incl. ``password_hash``), so the login
        and workspace-share paths can verify a password / read ACL
        fields without a second lookup.
        """
        if "@" in identifier:
            return await self.get_user_by_email(identifier)
        row = await self.app.state.db.fetchone(
            _USER_COLUMNS + " FROM users WHERE handle = ?",
            (identifier,),
        )
        if row is None:
            return None
        return _user_row_to_dict(row)

    async def list_users(
        self,
        page: int = 1,
        page_size: int = 10,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        """List users with server-side pagination, sorting, and filtering."""
        sort_col = ADMIN_USER_SORT_COLUMNS.get(sort, "created_at")
        direction = "DESC" if order.lower() == "desc" else "ASC"
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size

        async with self.app.state.db.transaction() as db:
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
                "SELECT id, email, handle, verified, provider, created_at,"
                " disabled, last_login_at, last_activity_at"
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
                    "disabled": bool(row["disabled"]),
                    "last_login_at": row["last_login_at"],
                    "last_activity_at": row["last_activity_at"],
                }
                for row in await cursor.fetchall()
            ]
            return {
                "users": users,
                "page": page,
                "page_size": page_size,
                "total": total,
            }

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user. Returns True if deleted, False if not found.

        Raises ``AgentPrincipalError`` if the target is the system agent.
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError("Cannot delete the system agent user")
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM users WHERE id = ?", (user_id,)
            )
            return cursor.rowcount > 0

    async def update_email(self, user_id: str, email: str) -> None:
        """Update a user's email.

        Raises ``AgentPrincipalError`` if the target is the system agent.
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "Cannot change the email of the system agent user"
            )
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE users SET email = ? WHERE id = ?", (email, user_id)
            )

    async def update_password(self, user_id: str, password_hash: str) -> None:
        """Update a user's password hash and retire the old one (#2582).

        The **old** hash moves into the password history inside the same
        transaction (the current hash lives in ``users``; history holds
        previous passwords only). Raises ``AgentPrincipalError`` if the
        target is the system agent. A missing user is a silent no-op —
        callers translate (reset/change 404 via their own lookups) — and
        crucially records nothing, so a reset token for a since-deleted
        user cannot trip the history FK (#2611 review).
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "Cannot set a password on the system agent user"
            )
        count = self.app.state.settings.password_history_count
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            if row is None:  # deleted user: nothing to update or retire
                return
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )
            if count > 0 and row["password_hash"] is not None:
                await self._retire_password(
                    db, user_id, row["password_hash"], count
                )

    # --- password history (#2582; table from migration 0001) ---

    async def _retire_password(
        self, db, user_id: str, old_hash: str, count: int
    ) -> None:
        """Append *old_hash* to the history and prune to the window.

        Runs on the caller's transaction. *count* is the caller's
        snapshot of ``password_history_count`` (read once per
        transaction so a mid-flight SIGHUP swap cannot make the insert
        and the prune disagree — e.g. N→0 pruning the row just
        written). Pruning keeps the *count* most-recent retired hashes.
        """
        await db.execute(
            "INSERT INTO password_history (user_id, password_hash)"
            " VALUES (?, ?)",
            (user_id, old_hash),
        )  # noqa: S608
        await db.execute(
            "DELETE FROM password_history WHERE user_id = ?"
            " AND id NOT IN (SELECT id FROM password_history"
            " WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, count),
        )  # noqa: S608

    async def get_password_history(
        self, user_id: str, limit: int
    ) -> list[str]:
        """The *limit* most recent remembered hashes, newest first."""
        rows = await self.app.state.db.fetchall(
            "SELECT password_hash FROM password_history"
            " WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [row["password_hash"] for row in rows]

    async def get_password_hash(self, user_id: str) -> str | None:
        """The user's current password hash (``None`` for OIDC users)."""
        row = await self.app.state.db.fetchone(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        )
        return None if row is None else row["password_hash"]

    async def record_login(self, user_id: str) -> None:
        """Stamp the user's most recent successful login (#2583).

        Called from every session-minting auth path (password login, the
        OIDC callback, no-auth local login, and the auto-login after
        register/verify/reset/invite-accept). Token refreshes continue a
        session rather than establish one and do not stamp. Stored as a
        UTC ISO-8601 string; displayed to the user via ``GET /auth/me``.
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )

    async def record_activity(self, user_id: str) -> None:
        """Stamp the user's most recent authenticated API access (#2588).

        Called (throttled — see ``Auth.record_activity``) from the token
        auth choke points: the HTTP ``get_current_user`` dependencies and
        the WebSocket ``get_user_from_token`` path. A login also counts
        as activity, but ``record_login`` already stamps a fresher
        signal, so login paths do not call this. Stored as a UTC
        ISO-8601 string; read by :meth:`disable_inactive_users`.
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE users SET last_activity_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )

    async def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        """Enable/disable a user account (#2588). Returns True if updated.

        Raises ``AgentPrincipalError`` if the target is the system agent
        (it realizes its capabilities through in-container physical
        access, never a login — there is nothing to disable).
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "Cannot disable the system agent user"
                " (it does not authenticate; disabling is meaningless)"
            )
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE users SET disabled = ? WHERE id = ?",
                (int(disabled), user_id),
            )
            return cursor.rowcount > 0

    async def disable_inactive_users(self, days: int) -> list[dict]:
        """Disable accounts inactive for more than *days* days (#2588).

        Inactivity is judged by the newest of ``last_activity_at`` (API
        access), ``last_login_at``, and ``created_at`` (a never-used
        account ages from creation). Exempt: the system agent and
        members of the ``admin`` group — auto-disabling every operator
        on an idle deploy would lock the deployment out with no admin
        left to re-enable accounts. Returns the newly disabled users
        as ``[{"id", "email"}]``. ``days <= 0`` is a no-op (the
        sweep is disabled; callers guard, this stays honest).
        """
        if days <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # Resolve the admins group on the pool connection *before* the
        # transaction opens — no pool acquisition inside it (#2978).
        admin_group = await self.get_group_by_name(ADMIN_GROUP_NAME)
        async with self.app.state.db.transaction() as db:
            admin_ids = await self._admin_member_ids(db, admin_group)
            cursor = await db.execute(
                "SELECT id, email, created_at, last_login_at,"
                " last_activity_at FROM users WHERE disabled = 0"
            )
            disabled: list[dict] = []
            for row in await cursor.fetchall():
                if _exempt_from_inactivity_sweep(row["id"], admin_ids):
                    continue
                if not _user_is_inactive(row, cutoff):
                    continue
                await db.execute(
                    "UPDATE users SET disabled = 1 WHERE id = ?",
                    (row["id"],),
                )
                disabled.append({"id": row["id"], "email": row["email"]})
            return disabled

    async def _admin_member_ids(
        self, db, admin_group: dict | None
    ) -> set[str]:
        """IDs of ``admins`` group members (empty when unseeded).

        The group row is resolved by the caller (outside the sweep's
        transaction); only the membership query runs here, on *db*,
        sharing the sweep's snapshot.
        """
        if admin_group is None:
            return set()
        cursor = await db.execute(
            "SELECT user_id FROM user_groups WHERE group_id = ?",
            (admin_group["id"],),
        )
        return {row[0] for row in await cursor.fetchall()}

    async def get_user_by_id(self, user_id: str) -> dict | None:
        row = await self.app.state.db.fetchone(
            "SELECT id, email, handle, last_login_at, disabled,"
            " last_activity_at FROM users WHERE id = ?",
            (user_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "handle": row["handle"],
            "last_login_at": row["last_login_at"],
            "disabled": bool(row["disabled"]),
            "last_activity_at": row["last_activity_at"],
        }

    async def search_users(self, query: str, limit: int = 10) -> list[dict]:
        """Search users by email or handle prefix (#616)."""
        rows = await self.app.state.db.fetchall(
            "SELECT id, email, handle FROM users"
            " WHERE email LIKE ? OR handle LIKE ?"
            " ORDER BY email LIMIT ?",
            (f"{query}%", f"{query}%", limit),
        )
        return [
            {
                "id": row["id"],
                "email": row["email"],
                "handle": row["handle"],
            }
            for row in rows
        ]
