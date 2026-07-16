"""ACL entries (access-control list rows) and principal/action constants.

``ACLModel`` is the ``app_state``-owned form, reached via
``app_state.model.acl`` (#1563 / #1574). The module-level free functions
are the pre-existing ``_current_db`` ContextVar delegates, kept as the
backstop until #1578 dissolves the ContextVar. The principal/action
constants and the pure ``row_to_acl_entry`` helper stay module-level —
they are imported as literal values by ``chat.py``, ``workspaces.py``,
and ``schema.py``.
"""

from .db import transaction
from .users import AGENT_USER_ID, AgentPrincipalError

# ACL constants
ACTION_DENY = 0
ACTION_ALLOW = 1

PRINCIPAL_SYSTEM = 0
PRINCIPAL_USER = 1
PRINCIPAL_GROUP = 2

SYSTEM_EVERYONE = 0
SYSTEM_AUTHENTICATED = 1


def row_to_acl_entry(row) -> dict:
    """Map an acl_entries row to the dict shape callers expect."""
    return {
        "id": row["id"],
        "resource": row["resource"],
        "position": row["position"],
        "action": row["action"],
        "principal_type": row["principal_type"],
        "user_id": row["user_id"],
        "group_id": row["group_id"],
        "system_principal": row["system_principal"],
        "permission": row["permission"],
    }


class ACLModel:
    """ACL data access, through ``app_state.db``.

    Reached via ``app_state.model.acl``. Reaches the DB through
    ``self.app_state.db`` (the single DB instance for the whole app).
    The method bodies mirror the module-level free functions below
    (backstop); the constants and the pure ``row_to_acl_entry`` helper
    stay module-level.
    """

    def __init__(self, app_state):
        self.app_state = app_state

    async def add_acl_entry(
        self,
        resource: str,
        position: int,
        action: int,
        permission: str,
        principal_type: int,
        user_id: str | None = None,
        group_id: str | None = None,
        system_principal: int | None = None,
    ) -> int:
        """Add an ACL entry. Returns the entry ID.

        Raises ``AgentPrincipalError`` if the entry would make the system
        agent a user principal.
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "The system agent cannot hold ACL entries"
                " (global fixed UUID — granting it cross-workspace"
                " blast radius)."
            )
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  user_id, group_id, system_principal, permission)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resource,
                    position,
                    action,
                    principal_type,
                    user_id,
                    group_id,
                    system_principal,
                    permission,
                ),
            )
            return cursor.lastrowid

    async def get_acl_entries(self, resource: str) -> list[dict]:
        """Get ACL entries for a resource, ordered by position."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, resource, position, action, principal_type,"
                " user_id, group_id, system_principal, permission"
                " FROM acl_entries WHERE resource = ?"
                " ORDER BY position",
                (resource,),
            )
            return [row_to_acl_entry(row) for row in await cursor.fetchall()]

    async def get_acl_entries_map(
        self,
        resources: list[str],
    ) -> dict[str, list[dict]]:
        """Fetch ACL entries for many resources in a single query.

        Returns a ``{resource: [entries]}`` map (entries ordered by position).
        Resources with no entries are present as empty lists. This exists so
        callers that check many resources/permissions (e.g. ``my_permissions``)
        don't open one DB connection per resource — see ``acl.check_permission``
        which previously caused ~300 sequential connection-per-query reads.
        """
        resources = list(dict.fromkeys(resources))  # de-dup, keep order
        result: dict[str, list[dict]] = {r: [] for r in resources}
        if not resources:
            return result
        placeholders = ",".join("?" for _ in resources)
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, resource, position, action, principal_type,"
                " user_id, group_id, system_principal, permission"
                " FROM acl_entries WHERE resource IN"
                f" ({placeholders}) ORDER BY position",
                tuple(resources),
            )
            for row in await cursor.fetchall():
                result.setdefault(row["resource"], []).append(
                    row_to_acl_entry(row)
                )
        return result

    async def get_acl_entries_resolved(self, resource: str) -> list[dict]:
        """Get ACL entries with resolved principal names."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT ae.id, ae.resource, ae.position, ae.action,"
                " ae.principal_type, ae.user_id, ae.group_id,"
                " ae.system_principal, ae.permission,"
                " u.email AS user_email, g.name AS group_name"
                " FROM acl_entries ae"
                " LEFT JOIN users u ON ae.user_id = u.id"
                " LEFT JOIN groups g ON ae.group_id = g.id"
                " WHERE ae.resource = ?"
                " ORDER BY ae.position",
                (resource,),
            )
            results = []
            for row in await cursor.fetchall():
                entry = {
                    "id": row["id"],
                    "resource": row["resource"],
                    "position": row["position"],
                    "action": row["action"],
                    "principal_type": row["principal_type"],
                    "permission": row["permission"],
                }
                pt = row["principal_type"]
                if pt == PRINCIPAL_SYSTEM:
                    sp = row["system_principal"]
                    entry["principal"] = (
                        "Everyone"
                        if sp == SYSTEM_EVERYONE
                        else "Authenticated"
                    )
                    entry["system_principal"] = sp
                elif pt == PRINCIPAL_USER:
                    entry["principal"] = row["user_email"] or row["user_id"]
                    entry["user_id"] = row["user_id"]
                elif pt == PRINCIPAL_GROUP:
                    entry["principal"] = row["group_name"] or row["group_id"]
                    entry["group_id"] = row["group_id"]
                results.append(entry)
            return results

    async def replace_acl_entries(
        self, resource: str, entries: list[dict]
    ) -> None:
        """Replace all ACL entries for a resource.

        Raises ``AgentPrincipalError`` if any entry would make the system
        agent a user principal. This is the second writer into
        ``acl_entries`` (a raw INSERT, fed request-body ``user_id`` by the
        PUT-acl endpoints) and must be guarded alongside
        :meth:`add_acl_entry` so neither writer can make the agent a
        principal.
        """
        for entry in entries:
            if (
                entry.get("principal_type") == PRINCIPAL_USER
                and entry.get("user_id") == AGENT_USER_ID
            ):
                raise AgentPrincipalError(
                    "The system agent cannot hold ACL entries"
                    " (global fixed UUID — granting it cross-workspace"
                    " blast radius)."
                )
        async with self.app_state.db.transaction() as db:
            await db.execute(
                "DELETE FROM acl_entries WHERE resource = ?", (resource,)
            )
            for entry in entries:
                await db.execute(
                    "INSERT INTO acl_entries"
                    " (resource, position, action, principal_type,"
                    "  user_id, group_id, system_principal, permission)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource,
                        entry["position"],
                        entry["action"],
                        entry["principal_type"],
                        entry.get("user_id"),
                        entry.get("group_id"),
                        entry.get("system_principal"),
                        entry["permission"],
                    ),
                )

    async def delete_acl_entries_for_resource(self, resource: str) -> int:
        """Delete all ACL entries for a resource. Returns count deleted."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM acl_entries WHERE resource = ?", (resource,)
            )
            return cursor.rowcount

    async def get_acl_entries_by_principal_user(
        self, user_id: str
    ) -> list[dict]:
        """Get all ACL entries referencing a specific user."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, resource, position, action, principal_type,"
                " user_id, group_id, system_principal, permission"
                " FROM acl_entries WHERE principal_type = ? AND user_id = ?"
                " ORDER BY resource, position",
                (PRINCIPAL_USER, user_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_acl_entries_by_principal_group(
        self, group_id: str
    ) -> list[dict]:
        """Get all ACL entries referencing a specific group."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, resource, position, action, principal_type,"
                " user_id, group_id, system_principal, permission"
                " FROM acl_entries WHERE principal_type = ? AND group_id = ?"
                " ORDER BY resource, position",
                (PRINCIPAL_GROUP, group_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_acl_tree_summary(self) -> list[dict]:
        """Get all distinct resources with their ACE counts."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT resource, COUNT(*) as ace_count"
                " FROM acl_entries GROUP BY resource"
                " ORDER BY resource"
            )
            return [
                {"resource": row["resource"], "ace_count": row["ace_count"]}
                for row in await cursor.fetchall()
            ]


# --- ContextVar-backed free functions (backstop; removed in #1578) ---


async def add_acl_entry(
    resource: str,
    position: int,
    action: int,
    permission: str,
    principal_type: int,
    user_id: str | None = None,
    group_id: str | None = None,
    system_principal: int | None = None,
) -> int:
    """Add an ACL entry. Returns the entry ID.

    Raises ``AgentPrincipalError`` if the entry would make the system
    agent a user principal.
    """
    if user_id == AGENT_USER_ID:
        raise AgentPrincipalError(
            "The system agent cannot hold ACL entries"
            " (global fixed UUID — granting it cross-workspace"
            " blast radius)."
        )
    async with transaction() as db:
        cursor = await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type,"
            "  user_id, group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource,
                position,
                action,
                principal_type,
                user_id,
                group_id,
                system_principal,
                permission,
            ),
        )
        return cursor.lastrowid


async def get_acl_entries(resource: str) -> list[dict]:
    """Get ACL entries for a resource, ordered by position."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE resource = ?"
            " ORDER BY position",
            (resource,),
        )
        return [row_to_acl_entry(row) for row in await cursor.fetchall()]


async def get_acl_entries_map(
    resources: list[str],
) -> dict[str, list[dict]]:
    """Fetch ACL entries for many resources in a single query.

    Returns a ``{resource: [entries]}`` map (entries ordered by position).
    Resources with no entries are present as empty lists. This exists so
    callers that check many resources/permissions (e.g. ``my_permissions``)
    don't open one DB connection per resource — see ``acl.check_permission``
    which previously caused ~300 sequential connection-per-query reads.
    """
    resources = list(dict.fromkeys(resources))  # de-dup, keep order
    result: dict[str, list[dict]] = {r: [] for r in resources}
    if not resources:
        return result
    placeholders = ",".join("?" for _ in resources)
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE resource IN"
            f" ({placeholders}) ORDER BY position",
            tuple(resources),
        )
        for row in await cursor.fetchall():
            result.setdefault(row["resource"], []).append(
                row_to_acl_entry(row)
            )
    return result


async def get_acl_entries_resolved(resource: str) -> list[dict]:
    """Get ACL entries with resolved principal names."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT ae.id, ae.resource, ae.position, ae.action,"
            " ae.principal_type, ae.user_id, ae.group_id,"
            " ae.system_principal, ae.permission,"
            " u.email AS user_email, g.name AS group_name"
            " FROM acl_entries ae"
            " LEFT JOIN users u ON ae.user_id = u.id"
            " LEFT JOIN groups g ON ae.group_id = g.id"
            " WHERE ae.resource = ?"
            " ORDER BY ae.position",
            (resource,),
        )
        results = []
        for row in await cursor.fetchall():
            entry = {
                "id": row["id"],
                "resource": row["resource"],
                "position": row["position"],
                "action": row["action"],
                "principal_type": row["principal_type"],
                "permission": row["permission"],
            }
            pt = row["principal_type"]
            if pt == PRINCIPAL_SYSTEM:
                sp = row["system_principal"]
                entry["principal"] = (
                    "Everyone" if sp == SYSTEM_EVERYONE else "Authenticated"
                )
                entry["system_principal"] = sp
            elif pt == PRINCIPAL_USER:
                entry["principal"] = row["user_email"] or row["user_id"]
                entry["user_id"] = row["user_id"]
            elif pt == PRINCIPAL_GROUP:
                entry["principal"] = row["group_name"] or row["group_id"]
                entry["group_id"] = row["group_id"]
            results.append(entry)
        return results


async def replace_acl_entries(resource: str, entries: list[dict]) -> None:
    """Replace all ACL entries for a resource.

    Raises ``AgentPrincipalError`` if any entry would make the system
    agent a user principal. This is the second writer into
    ``acl_entries`` (a raw INSERT, fed request-body ``user_id`` by the
    PUT-acl endpoints) and must be guarded alongside :func:`add_acl_entry`
    so neither writer can make the agent a principal.
    """
    for entry in entries:
        if (
            entry.get("principal_type") == PRINCIPAL_USER
            and entry.get("user_id") == AGENT_USER_ID
        ):
            raise AgentPrincipalError(
                "The system agent cannot hold ACL entries"
                " (global fixed UUID — granting it cross-workspace"
                " blast radius)."
            )
    async with transaction() as db:
        await db.execute(
            "DELETE FROM acl_entries WHERE resource = ?", (resource,)
        )
        for entry in entries:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  user_id, group_id, system_principal, permission)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resource,
                    entry["position"],
                    entry["action"],
                    entry["principal_type"],
                    entry.get("user_id"),
                    entry.get("group_id"),
                    entry.get("system_principal"),
                    entry["permission"],
                ),
            )


async def delete_acl_entries_for_resource(resource: str) -> int:
    """Delete all ACL entries for a resource. Returns count deleted."""
    async with transaction() as db:
        cursor = await db.execute(
            "DELETE FROM acl_entries WHERE resource = ?", (resource,)
        )
        return cursor.rowcount


async def get_acl_entries_by_principal_user(user_id: str) -> list[dict]:
    """Get all ACL entries referencing a specific user."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE principal_type = ? AND user_id = ?"
            " ORDER BY resource, position",
            (PRINCIPAL_USER, user_id),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_acl_entries_by_principal_group(group_id: str) -> list[dict]:
    """Get all ACL entries referencing a specific group."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE principal_type = ? AND group_id = ?"
            " ORDER BY resource, position",
            (PRINCIPAL_GROUP, group_id),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_acl_tree_summary() -> list[dict]:
    """Get all distinct resources with their ACE counts."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT resource, COUNT(*) as ace_count"
            " FROM acl_entries GROUP BY resource"
            " ORDER BY resource"
        )
        return [
            {"resource": row["resource"], "ace_count": row["ace_count"]}
            for row in await cursor.fetchall()
        ]
