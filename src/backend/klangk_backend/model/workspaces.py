"""Workspace CRUD, members, and shared-workspace listings."""

import json
import uuid
from datetime import datetime, timezone

from ._core import _fetchone, transaction
from .acl import ACTION_ALLOW, PRINCIPAL_GROUP, PRINCIPAL_USER
from .users import get_user_group_ids

# Must match the DB default and container.DEFAULT_PORTS_PER_WORKSPACE.
_DEFAULT_PORTS_PER_WORKSPACE = 5


async def create_workspace(
    user_id: str,
    name: str,
    image: str | None = None,
    default_command: str | None = None,
    auto_start: bool = False,
    mounts: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    async with transaction() as db:
        workspace_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mounts_json = json.dumps(mounts) if mounts else None
        env_json = json.dumps(env) if env else None
        await db.execute(
            "INSERT INTO workspaces"
            " (id, user_id, name, image, default_command, auto_start,"
            " mounts, env, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                user_id,
                name,
                image,
                default_command,
                1 if auto_start else 0,
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
            "default_command": default_command,
            "auto_start": auto_start,
            "mounts": mounts,
            "env": env,
            "num_ports": _DEFAULT_PORTS_PER_WORKSPACE,
            "created_at": created_at,
        }


# Whitelisted sort columns for workspace list queries. Values are the
# real column names; the prefix (e.g. "w.") is applied by the caller.
_SORT_COLUMNS = {"created": "created_at", "name": "name"}


def _sort_order_clause(sort: str, order: str, prefix: str = "") -> str:
    """Build a deterministic ORDER BY clause for paginated workspace lists.

    ``sort`` is whitelisted against ``_SORT_COLUMNS``; ``order`` is
    coerced to ASC/DESC. The ``id`` tiebreaker uses the same direction so
    offset pagination stays stable when rows share the sort key.
    """
    col = _SORT_COLUMNS.get(sort, "created_at")
    p = f"{prefix}." if prefix else ""
    direction = "DESC" if order.lower() == "desc" else "ASC"
    return f"ORDER BY {p}{col} {direction}, {p}id {direction}"


async def list_workspaces(
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
    order_by = _sort_order_clause(sort, order)
    where = "WHERE user_id = ?"
    params: list = [user_id]
    if q:
        where += " AND name LIKE '%' || ? || '%'"
        params.append(q)
    params.extend([limit, offset])
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, name, container_id, image, default_command,"
            " auto_start, mounts, env, created_at FROM workspaces"
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
                "default_command": row["default_command"],
                "auto_start": bool(row["auto_start"]),
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        has_more = len(items) == limit
        return {
            "items": items,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }


async def list_shared_workspaces(
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
    ``sort``/``order``/``q`` as in :func:`list_workspaces`.
    """
    order_by = _sort_order_clause(sort, order, prefix="w")
    name_filter = " AND w.name LIKE '%' || ? || '%'" if q else ""
    async with transaction() as db:
        group_ids = await get_user_group_ids(user_id)
        group_placeholders = ",".join("?" for _ in group_ids)
        group_clause = (
            f" OR (ae.principal_type = {PRINCIPAL_GROUP}"
            f" AND ae.group_id IN ({group_placeholders}))"
            if group_ids
            else ""
        )
        cursor = await db.execute(
            "SELECT DISTINCT w.id, w.name, w.container_id, w.image,"
            " w.default_command, w.auto_start, w.mounts, w.env, w.created_at,"
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
                limit,
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
                "default_command": row["default_command"],
                "auto_start": bool(row["auto_start"]),
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
                "created_at": row["created_at"],
                "owner_email": row["owner_email"],
            }
            for row in rows
        ]
        has_more = len(items) == limit
        return {
            "items": items,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }


async def get_workspace(
    workspace_id: str, user_id: str | None = None
) -> dict | None:
    """Get a workspace by ID.

    If user_id is provided, restricts to workspaces owned by that user.
    Access control for shared workspaces is handled by the ACL layer.
    """
    async with transaction() as db:
        if user_id is not None:
            cursor = await db.execute(
                "SELECT id, user_id, name, container_id, num_ports, image,"
                " default_command, auto_start, mounts, env"
                " FROM workspaces WHERE id = ? AND user_id = ?",
                (workspace_id, user_id),
            )
        else:
            cursor = await db.execute(
                "SELECT id, user_id, name, container_id, num_ports, image,"
                " default_command, auto_start, mounts, env"
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
            "default_command": row["default_command"],
            "auto_start": bool(row["auto_start"]),
            "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
            "env": json.loads(row["env"]) if row["env"] else None,
        }


async def get_workspace_by_id(workspace_id: str) -> dict | None:
    """Get a workspace by ID without access control (for admin use)."""
    row = await _fetchone(
        "SELECT id, user_id, name, container_id, num_ports, image,"
        " default_command, mounts, env"
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
        "default_command": row["default_command"],
        "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
        "env": json.loads(row["env"]) if row["env"] else None,
    }


async def get_workspace_members(workspace_id: str) -> list[dict]:
    """Get users who have been granted access to a workspace via ACL.

    Returns users with direct user-level ACEs on /workspaces/{id},
    excluding the workspace owner.
    """
    async with transaction() as db:
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
            {"id": row["id"], "email": row["email"], "handle": row["handle"]}
            for row in await cursor.fetchall()
        ]


async def delete_workspace(workspace_id: str, user_id: str) -> bool:
    async with transaction() as db:
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
    workspace_id: str, container_id: str | None
) -> None:
    async with transaction() as db:
        await db.execute(
            "UPDATE workspaces SET container_id = ? WHERE id = ?",
            (container_id, workspace_id),
        )


async def update_workspace(
    workspace_id: str,
    user_id: str,
    **fields: str | None,
) -> bool:
    """Update workspace fields. Only provided fields are changed."""
    allowed = {
        "name",
        "image",
        "default_command",
        "auto_start",
        "mounts",
        "env",
    }
    to_set = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("mounts", "env"):
            to_set[k] = json.dumps(v) if v is not None else None
        elif k == "auto_start":
            to_set[k] = 1 if v else 0
        else:
            to_set[k] = v
    if not to_set:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in to_set)
    values = list(to_set.values()) + [workspace_id, user_id]
    async with transaction() as db:
        cursor = await db.execute(
            f"UPDATE workspaces SET {set_clause}"  # noqa: S608
            " WHERE id = ? AND user_id = ?",
            values,
        )
        return cursor.rowcount > 0


async def get_user_workspaces_with_containers(user_id: str) -> list[dict]:
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, container_id FROM workspaces WHERE user_id = ? AND container_id IS NOT NULL",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": row["id"], "container_id": row["container_id"]}
            for row in rows
        ]


async def list_auto_start_workspaces() -> list[dict]:
    """List all workspaces with auto_start enabled."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id, user_id, name, container_id, num_ports, image,"
            " default_command, auto_start, mounts, env"
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
                "default_command": row["default_command"],
                "auto_start": True,
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
            }
            for row in rows
        ]
