"""Workspace CRUD, members, and shared-workspace listings."""

import json
import uuid
from datetime import datetime, timezone

from .acl import ACTION_ALLOW, PRINCIPAL_GROUP, PRINCIPAL_USER
from .users import AGENT_USER_ID, AgentPrincipalError

# Must match the DB default and container.DEFAULT_PORTS_PER_WORKSPACE.
DEFAULT_PORTS_PER_WORKSPACE = 5

# Per-workspace role groups created for every workspace. The key is the
# group-name suffix appended to ``<suffix>-<workspace_id>``; the value is the
# ordered list of permissions granted to that group on ``/workspaces/{id}``.
# Seeded atomically with the row in :meth:`WorkspacesModel.create_workspace_with_acl`
# so a failure mid-seed can never leave orphaned ACEs/groups (#128).
_ROLE_GROUP_PERMISSIONS: dict[str, list[str]] = {
    "owners": ["*"],
    "coders": [
        "terminal",
        "code-in-isolation",
        "spectate-on-shared-terminals",
        "files",
        "chat",
    ],
    "collaborators": [
        "terminal",
        "code-in-isolation",
        "code-in-shared-terminals",
        "spectate-on-shared-terminals",
        "share-terminals",
        "files",
        "chat",
    ],
    "spectators": [
        "terminal",
        "spectate-on-shared-terminals",
    ],
}

# setup_state lifecycle values (#1033). A workspace always holds
# exactly one. Descriptive, not proscriptive: created in whichever
# state matches reality.
SETUP_STATE_PENDING = "pending"
SETUP_STATE_COMPLETE = "complete"
SETUP_STATE_FAILED = "failed"
SETUP_STATES = frozenset(
    {SETUP_STATE_PENDING, SETUP_STATE_COMPLETE, SETUP_STATE_FAILED}
)

# Whitelisted sort columns for workspace list queries. Values are the
# real column names; the prefix (e.g. "w.") is applied by the caller.
SORT_COLUMNS = {"created": "created_at", "name": "name"}


def sort_order_clause(sort: str, order: str, prefix: str = "") -> str:
    """Build a deterministic ORDER BY clause for paginated workspace lists.

    ``sort`` is whitelisted against ``SORT_COLUMNS``; ``order`` is
    coerced to ASC/DESC. The ``id`` tiebreaker uses the same direction so
    offset pagination stays stable when rows share the sort key.
    """
    col = SORT_COLUMNS.get(sort, "created_at")
    p = f"{prefix}." if prefix else ""
    direction = "DESC" if order.lower() == "desc" else "ASC"
    return f"ORDER BY {p}{col} {direction}, {p}id {direction}"


class WorkspacesModel:
    """Workspace CRUD/members/listings, resolved through ``app_state.db``.

    Constructed by :class:`~klangk_backend.model.model.Model` and reached
    via ``app_state.model.workspaces``. Reaches the DB through
    ``self.app.state.db`` (the single DB instance for the whole app).

    The ``db``-param private helpers (:meth:`_insert_workspace_row` /
    :meth:`_seed_workspace_acl`) take a caller-supplied connection so they
    can run inside a larger transaction (the atomic create-with-ACL path);
    they do not reach for ``self.app.state.db`` themselves — the atomicity
    constraint (the owner ACE + role groups must commit/roll back with the
    row insert) is load-bearing (#128).
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def _insert_workspace_row(
        self,
        db,
        user_id: str,
        name: str,
        image: str | None,
        service_command: str | None,
        auto_start: bool,
        mounts: list[str] | None,
        env: dict[str, str] | None,
        setup_state: str,
        health_check: str | None,
    ) -> dict:
        """INSERT a workspace row on ``db`` and return the new workspace dict.

        Runs on the caller's connection so it can participate in a larger
        transaction (see :meth:`create_workspace_with_acl`). Does not commit.
        """
        workspace_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mounts_json = json.dumps(mounts) if mounts else None
        env_json = json.dumps(env) if env else None
        await db.execute(
            "INSERT INTO workspaces"
            " (id, user_id, name, image, service_command, auto_start,"
            " setup_state, health_check, mounts, env, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                user_id,
                name,
                image,
                service_command,
                1 if auto_start else 0,
                setup_state,
                health_check,
                mounts_json,
                env_json,
                created_at,
            ),
        )
        return {
            "id": workspace_id,
            "user_id": user_id,
            "name": name,
            "image": image,
            "service_command": service_command,
            "auto_start": auto_start,
            "setup_state": setup_state,
            "health_check": health_check,
            "mounts": mounts,
            "env": env,
            "num_ports": DEFAULT_PORTS_PER_WORKSPACE,
            "created_at": created_at,
        }

    async def _seed_workspace_acl(self, db, ws: dict, user_id: str) -> None:
        """Seed the owner ACE and per-workspace role groups on ``db``.

        Writes the owner ``Allow`` ACE at position 0, then creates the four
        role groups (``owners``/``coders``/``collaborators``/``spectators``)
        with their permission ACEs at incrementing positions, and adds the
        creator to the ``owners`` group. Runs on the caller's connection so
        it commits/rolls back with the surrounding transaction. Must stay
        in sync with :meth:`delete_workspace`'s teardown.
        """
        resource = f"/workspaces/{ws['id']}"
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource,
                0,
                ACTION_ALLOW,
                PRINCIPAL_USER,
                user_id,
                None,
                None,
                "*",
            ),
        )
        pos = 1
        for suffix, perms in _ROLE_GROUP_PERMISSIONS.items():
            group_name = f"{suffix}-{ws['id']}"
            group_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO groups (id, name, description) VALUES (?, ?, ?)",
                (
                    group_id,
                    group_name,
                    f"{group_name} for workspace {ws['name']}",
                ),
            )
            for perm in perms:
                await db.execute(
                    "INSERT INTO acl_entries"
                    " (resource, position, action, principal_type,"
                    " user_id, group_id, system_principal, permission)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource,
                        pos,
                        ACTION_ALLOW,
                        PRINCIPAL_GROUP,
                        None,
                        group_id,
                        None,
                        perm,
                    ),
                )
                pos += 1
            if suffix == "owners":
                await db.execute(
                    "INSERT OR IGNORE INTO user_groups"
                    " (user_id, group_id, source) VALUES (?, ?, ?)",
                    (user_id, group_id, "manual"),
                )

    async def create_workspace_with_acl(
        self,
        user_id: str,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_state: str = SETUP_STATE_COMPLETE,
        health_check: str | None = None,
    ) -> dict:
        """Create a workspace row AND seed its owner ACE + role groups.

        The row insert and the ACL/group seeding run in a **single
        transaction**, so any failure rolls the whole thing back — no
        orphaned row, ACEs, or role groups (#128). Directory creation and
        port allocation happen later in the service layer
        (:func:`workspaces.create_workspace`); port-allocation failure is
        cleaned up by :meth:`delete_workspace`, which removes everything
        this function wrote.
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "The system agent cannot own a workspace (seeding it a"
                " wildcard owner ACE + owners-group membership makes its"
                " UUID a privileged principal) — system agent"
            )
        if setup_state not in SETUP_STATES:
            raise ValueError(f"Invalid setup_state: {setup_state!r}")
        async with self.app.state.db.transaction() as db:
            ws = await self._insert_workspace_row(
                db,
                user_id,
                name,
                image=image,
                service_command=service_command,
                auto_start=auto_start,
                mounts=mounts,
                env=env,
                setup_state=setup_state,
                health_check=health_check,
            )
            await self._seed_workspace_acl(db, ws, user_id)
            return ws

    async def create_workspace(
        self,
        user_id: str,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_state: str = SETUP_STATE_COMPLETE,
        health_check: str | None = None,
    ) -> dict:
        """Insert a workspace row only (no ACL seeding).

        Prefer :meth:`create_workspace_with_acl` for normal workspace
        creation — it seeds the owner ACE and role groups atomically and is
        what the service layer uses. This row-only primitive is kept for
        callers that manage ACLs separately.
        """
        if setup_state not in SETUP_STATES:
            raise ValueError(f"Invalid setup_state: {setup_state!r}")
        async with self.app.state.db.transaction() as db:
            return await self._insert_workspace_row(
                db,
                user_id,
                name,
                image=image,
                service_command=service_command,
                auto_start=auto_start,
                mounts=mounts,
                env=env,
                setup_state=setup_state,
                health_check=health_check,
            )

    async def list_workspaces(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        """List a page of workspaces owned by ``user_id``.

        Returns a pagination envelope:
        ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
        ``sort`` (``created``/``name``) and ``order`` (``asc``/``desc``) are
        whitelisted; ``q`` filters by name substring. The ``id`` tiebreaker
        keeps offset pagination deterministic.
        """
        order_by = sort_order_clause(sort, order)
        where = "WHERE user_id = ?"
        params: list = [user_id]
        if q:
            where += " AND name LIKE '%' || ? || '%'"
            params.append(q)
        params.extend([limit + 1, offset])
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, name, container_id, image, service_command,"
                " auto_start, setup_state, health_check, mounts, env,"
                " created_at"
                " FROM workspaces"
                f" {where} {order_by} LIMIT ? OFFSET ?",
                tuple(params),
            )
            rows = await cursor.fetchall()
            items = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "container_id": row["container_id"],
                    "image": row["image"],
                    "service_command": row["service_command"],
                    "auto_start": bool(row["auto_start"]),
                    "setup_state": row["setup_state"],
                    "health_check": row["health_check"],
                    "mounts": json.loads(row["mounts"])
                    if row["mounts"]
                    else None,
                    "env": json.loads(row["env"]) if row["env"] else None,
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
            has_more = len(items) > limit
            items = items[:limit]
            return {
                "items": items,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            }

    async def list_shared_workspaces(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        """List a page of workspaces shared with (not owned by) this user.

        Access is granted through either a direct user-level ACE or a
        group-level ACE on ``/workspaces/{id}``. Returns a pagination
        envelope: ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
        ``sort``/``order``/``q`` as in :meth:`list_workspaces`.
        """
        order_by = sort_order_clause(sort, order, prefix="w")
        name_filter = " AND w.name LIKE '%' || ? || '%'" if q else ""
        async with self.app.state.db.transaction() as db:
            group_ids = await self.app.state.model.users.get_user_group_ids(
                user_id
            )
            group_placeholders = ",".join("?" for _ in group_ids)
            group_clause = (
                f" OR (ae.principal_type = {PRINCIPAL_GROUP}"
                f" AND ae.group_id IN ({group_placeholders}))"
                if group_ids
                else ""
            )
            cursor = await db.execute(
                "SELECT DISTINCT w.id, w.name, w.container_id, w.image,"
                " w.service_command, w.auto_start, w.setup_state,"
                " w.health_check, w.mounts, w.env, w.created_at,"
                " u.email AS owner_email"
                " FROM workspaces w"
                " JOIN acl_entries ae ON ae.resource = '/workspaces/' || w.id"
                " JOIN users u ON w.user_id = u.id"
                " WHERE ae.action = ? AND w.user_id != ?"
                "   AND ("
                f"    (ae.principal_type = {PRINCIPAL_USER} AND ae.user_id = ?)"
                f"    {group_clause}"
                "   )"
                f"{name_filter}"
                f" {order_by} LIMIT ? OFFSET ?",
                (
                    ACTION_ALLOW,
                    user_id,
                    user_id,
                    *group_ids,
                    *([q] if q else []),
                    limit + 1,
                    offset,
                ),
            )
            rows = await cursor.fetchall()
            items = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "container_id": row["container_id"],
                    "image": row["image"],
                    "service_command": row["service_command"],
                    "auto_start": bool(row["auto_start"]),
                    "setup_state": row["setup_state"],
                    "health_check": row["health_check"],
                    "mounts": json.loads(row["mounts"])
                    if row["mounts"]
                    else None,
                    "env": json.loads(row["env"]) if row["env"] else None,
                    "created_at": row["created_at"],
                    "owner_email": row["owner_email"],
                }
                for row in rows
            ]
            has_more = len(items) > limit
            items = items[:limit]
            return {
                "items": items,
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            }

    async def get_workspace(
        self, workspace_id: str, user_id: str | None = None
    ) -> dict | None:
        """Get a workspace by ID.

        If user_id is provided, restricts to workspaces owned by that user.
        Access control for shared workspaces is handled by the ACL layer.
        """
        async with self.app.state.db.transaction() as db:
            if user_id is not None:
                cursor = await db.execute(
                    "SELECT id, user_id, name, container_id, num_ports, image,"
                    " service_command, auto_start, setup_state, health_check,"
                    " mounts, env"
                    " FROM workspaces WHERE id = ? AND user_id = ?",
                    (workspace_id, user_id),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, user_id, name, container_id, num_ports, image,"
                    " service_command, auto_start, setup_state, health_check,"
                    " mounts, env"
                    " FROM workspaces WHERE id = ?",
                    (workspace_id,),
                )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "name": row["name"],
                "container_id": row["container_id"],
                "num_ports": row["num_ports"],
                "image": row["image"],
                "service_command": row["service_command"],
                "auto_start": bool(row["auto_start"]),
                "setup_state": row["setup_state"],
                "health_check": row["health_check"],
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
            }

    async def get_workspace_by_id(self, workspace_id: str) -> dict | None:
        """Get a workspace by ID without access control (for admin use)."""
        row = await self.app.state.db.fetchone(
            "SELECT id, user_id, name, container_id, num_ports, image,"
            " service_command, setup_state, health_check, mounts, env"
            " FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "container_id": row["container_id"],
            "num_ports": row["num_ports"],
            "image": row["image"],
            "service_command": row["service_command"],
            "setup_state": row["setup_state"],
            "health_check": row["health_check"],
            "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
            "env": json.loads(row["env"]) if row["env"] else None,
        }

    async def get_workspace_members(self, workspace_id: str) -> list[dict]:
        """Get users who have been granted access to a workspace via ACL.

        Returns users with direct user-level ACEs on /workspaces/{id},
        excluding the workspace owner.
        """
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT DISTINCT u.id, u.email, u.handle FROM users u"
                " JOIN acl_entries ae ON ae.user_id = u.id"
                " JOIN workspaces w ON w.id = ?"
                " WHERE ae.resource = ? AND ae.principal_type = ?"
                "   AND ae.action = ? AND u.id != w.user_id"
                " ORDER BY u.email",
                (
                    workspace_id,
                    f"/workspaces/{workspace_id}",
                    PRINCIPAL_USER,
                    ACTION_ALLOW,
                ),
            )
            return [
                {
                    "id": row["id"],
                    "email": row["email"],
                    "handle": row["handle"],
                }
                for row in await cursor.fetchall()
            ]

    async def delete_workspace(self, workspace_id: str, user_id: str) -> bool:
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM workspaces WHERE id = ? AND user_id = ?",
                (workspace_id, user_id),
            )
            if cursor.rowcount == 0:
                return False
            # Clean up ACL entries for this workspace
            resource = f"/workspaces/{workspace_id}"
            await db.execute(
                "DELETE FROM acl_entries WHERE resource = ?", (resource,)
            )
            # Clean up per-workspace role groups and their memberships
            role_suffixes = ["owners", "coders", "collaborators", "spectators"]
            for suffix in role_suffixes:
                group_name = f"{suffix}-{workspace_id}"
                cursor_g = await db.execute(
                    "SELECT id FROM groups WHERE name = ?", (group_name,)
                )
                row = await cursor_g.fetchone()
                if row:
                    group_id = row["id"]
                    await db.execute(
                        "DELETE FROM user_groups WHERE group_id = ?",
                        (group_id,),
                    )
                    await db.execute(
                        "DELETE FROM acl_entries WHERE group_id = ?",
                        (group_id,),
                    )
                    await db.execute(
                        "DELETE FROM groups WHERE id = ?", (group_id,)
                    )
            # Clean up port allocations
            await db.execute(
                "DELETE FROM port_allocations WHERE workspace_id = ?",
                (workspace_id,),
            )
            # Clean up chat messages
            await db.execute(
                "DELETE FROM chat_messages WHERE workspace_id = ?",
                (workspace_id,),
            )
            return True

    async def update_workspace_container(
        self, workspace_id: str, container_id: str | None
    ) -> None:
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE workspaces SET container_id = ? WHERE id = ?",
                (container_id, workspace_id),
            )

    async def update_workspace(
        self,
        workspace_id: str,
        user_id: str,
        **fields: str | None,
    ) -> bool:
        """Update workspace fields. Only provided fields are changed."""
        allowed = {
            "name",
            "image",
            "service_command",
            "auto_start",
            "setup_state",
            "health_check",
            "mounts",
            "env",
        }
        to_set = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "setup_state":
                if v not in SETUP_STATES:
                    raise ValueError(f"Invalid setup_state: {v!r}")
                to_set[k] = v
            elif k in ("mounts", "env"):
                to_set[k] = json.dumps(v) if v is not None else None
            elif k == "auto_start":
                to_set[k] = 1 if v else 0
            else:
                to_set[k] = v
        if not to_set:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in to_set)
        values = list(to_set.values()) + [workspace_id, user_id]
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                f"UPDATE workspaces SET {set_clause}"  # noqa: S608
                " WHERE id = ? AND user_id = ?",
                values,
            )
            return cursor.rowcount > 0

    async def transfer_workspace(
        self,
        workspace_id: str,
        new_owner_id: str,
    ) -> dict | None:
        """Transfer workspace ownership to a different user.

        Updates the workspace ``user_id``, the owner ACE (position 0), and
        the ``owners`` role group membership atomically.  Returns the
        updated workspace dict, or ``None`` if the workspace does not exist.

        Raises ``ValueError`` if the new owner already owns a workspace
        with the same name (violating the UNIQUE constraint) or if the
        target is the system agent.
        """
        if new_owner_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "Cannot transfer a workspace to the system agent"
            )

        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, user_id, name FROM workspaces WHERE id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            old_owner_id = row["user_id"]
            ws_name = row["name"]

            if old_owner_id == new_owner_id:
                raise ValueError("Target user is already the owner")

            # Check UNIQUE(user_id, name) won't be violated.
            dup = await db.execute(
                "SELECT 1 FROM workspaces"
                " WHERE user_id = ? AND name = ? AND id != ?",
                (new_owner_id, ws_name, workspace_id),
            )
            if await dup.fetchone():
                raise ValueError(
                    f"Target user already owns a workspace named {ws_name!r}"
                )

            # 1. Update workspace owner.
            await db.execute(
                "UPDATE workspaces SET user_id = ? WHERE id = ?",
                (new_owner_id, workspace_id),
            )

            # 2. Update the owner ACE (position 0) to point at the new owner.
            resource = f"/workspaces/{workspace_id}"
            await db.execute(
                "UPDATE acl_entries SET user_id = ?"
                " WHERE resource = ? AND position = 0"
                " AND principal_type = ?",
                (new_owner_id, resource, PRINCIPAL_USER),
            )

            # 3. Swap owners-group membership: remove old owner, add new.
            owners_group_name = f"owners-{workspace_id}"
            g_cursor = await db.execute(
                "SELECT id FROM groups WHERE name = ?",
                (owners_group_name,),
            )
            g_row = await g_cursor.fetchone()
            if g_row:
                group_id = g_row["id"]
                await db.execute(
                    "DELETE FROM user_groups WHERE user_id = ? AND group_id = ?",
                    (old_owner_id, group_id),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO user_groups"
                    " (user_id, group_id, source) VALUES (?, ?, ?)",
                    (new_owner_id, group_id, "manual"),
                )

        return await self.get_workspace_by_id(workspace_id)

    async def get_user_workspaces_with_containers(
        self, user_id: str
    ) -> list[dict]:
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, container_id FROM workspaces WHERE user_id = ? AND container_id IS NOT NULL",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [
                {"id": row["id"], "container_id": row["container_id"]}
                for row in rows
            ]

    async def list_auto_start_workspaces(self) -> list[dict]:
        """List all workspaces with auto_start enabled."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, user_id, name, container_id, num_ports, image,"
                " service_command, auto_start, setup_state, health_check,"
                " mounts, env"
                " FROM workspaces WHERE auto_start = 1",
            )
            rows = await cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "name": row["name"],
                    "container_id": row["container_id"],
                    "num_ports": row["num_ports"],
                    "image": row["image"],
                    "service_command": row["service_command"],
                    "auto_start": True,
                    "setup_state": row["setup_state"],
                    "health_check": row["health_check"],
                    "mounts": json.loads(row["mounts"])
                    if row["mounts"]
                    else None,
                    "env": json.loads(row["env"]) if row["env"] else None,
                }
                for row in rows
            ]
