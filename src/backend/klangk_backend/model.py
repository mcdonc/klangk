import json
import socket
from contextlib import asynccontextmanager

import aiosqlite
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .util import resolve_env_secret

_data_dir = Path(
    resolve_env_secret(
        "KLANGK_DATA_DIR", str(Path.home() / ".klangk" / "data")
    )
)
DB_PATH = _data_dir / "klangk.db"


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 15000")
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT 'local',
                external_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: make password_hash nullable and add OIDC columns.
        # SQLite can't ALTER COLUMN, so we recreate the table if needed.
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = {row[1]: row for row in await cursor.fetchall()}
        needs_recreate = False
        if "password_hash" in columns and columns["password_hash"][3]:
            # password_hash has NOT NULL — need to drop it for OIDC users
            needs_recreate = True
        if "provider" not in columns:
            needs_recreate = True
        if needs_recreate:
            await db.execute("""
                CREATE TABLE users_new (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT 'local',
                    external_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            # Copy existing data — old tables may lack some columns
            old_cols = list(columns.keys())
            shared = [
                c
                for c in old_cols
                if c
                in ("id", "email", "password_hash", "verified", "created_at")
            ]
            cols_str = ", ".join(shared)
            await db.execute(
                f"INSERT INTO users_new ({cols_str})"  # noqa: S608
                f" SELECT {cols_str} FROM users"
            )
            await db.execute("DROP TABLE users")
            await db.execute("ALTER TABLE users_new RENAME TO users")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                container_id TEXT,
                num_ports INTEGER NOT NULL DEFAULT 5,  -- see container.DEFAULT_PORTS_PER_WORKSPACE
                image TEXT,  -- custom Docker image; NULL means use default
                default_command TEXT,  -- auto-run in terminal on connect
                mounts TEXT,  -- JSON array of host:container mount specs
                env TEXT,  -- JSON dict of custom environment variables
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workspace_access (
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (workspace_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS port_allocations (
                port INTEGER PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS roles (
                name TEXT PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_roles (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_name TEXT NOT NULL REFERENCES roles(name) ON DELETE CASCADE,
                PRIMARY KEY (user_id, role_name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS token_blocklist (
                jti TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                user_email TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                email TEXT PRIMARY KEY,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                first_attempt_at TEXT NOT NULL,
                locked_until TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                invited_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                accepted_at TEXT
            )
        """)
        await db.commit()
    finally:
        await db.close()


@asynccontextmanager
async def transaction():
    """Async context manager with transaction semantics.

    Commits on clean exit, rolls back on exception.
    """
    db = await get_db()
    try:
        yield db
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    finally:
        await db.close()


async def create_user(
    email: str,
    password_hash: str | None,
    verified: bool = False,
    provider: str = "local",
    external_id: str | None = None,
) -> dict:
    db = await get_db()
    try:
        user_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified,"
            " provider, external_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                email,
                password_hash,
                int(verified),
                provider,
                external_id,
            ),
        )
        await db.commit()
        return {"id": user_id, "email": email, "verified": verified}
    finally:
        await db.close()


async def get_user_by_external_id(
    provider: str, external_id: str
) -> dict | None:
    """Find a user by OIDC provider + external ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, password_hash, verified, provider, external_id"
            " FROM users WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "password_hash": row["password_hash"],
            "verified": bool(row["verified"]),
            "provider": row["provider"],
            "external_id": row["external_id"],
        }
    finally:
        await db.close()


async def link_oidc_identity(
    user_id: str, provider: str, external_id: str
) -> None:
    """Link an OIDC identity to an existing user."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET provider = ?, external_id = ? WHERE id = ?",
            (provider, external_id, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def verify_user(user_id: str) -> bool:
    """Mark a user as verified. Returns True if updated, False if not found."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE users SET verified = 1 WHERE id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def ensure_role(name: str) -> None:
    """Create a role if it doesn't exist."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO roles (name) VALUES (?)", (name,)
        )
        await db.commit()
    finally:
        await db.close()


async def assign_role(user_id: str, role_name: str) -> None:
    """Assign a role to a user (idempotent)."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO user_roles (user_id, role_name) VALUES (?, ?)",
            (user_id, role_name),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user_roles(user_id: str) -> list[str]:
    """Get all roles for a user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT role_name FROM user_roles WHERE user_id = ?", (user_id,)
        )
        rows = await cursor.fetchall()
        return [row["role_name"] for row in rows]
    finally:
        await db.close()


async def get_user_by_email(email: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, password_hash, verified, provider, external_id"
            " FROM users WHERE email = ?",
            (email,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "password_hash": row["password_hash"],
            "verified": bool(row["verified"]),
            "provider": row["provider"],
            "external_id": row["external_id"],
        }
    finally:
        await db.close()


async def list_users() -> list[dict]:
    """List all users with their roles."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, verified, created_at FROM users ORDER BY created_at"
        )
        users = []
        for row in await cursor.fetchall():
            role_cursor = await db.execute(
                "SELECT role_name FROM user_roles WHERE user_id = ?",
                (row["id"],),
            )
            roles = [r["role_name"] for r in await role_cursor.fetchall()]
            users.append(
                {
                    "id": row["id"],
                    "email": row["email"],
                    "verified": bool(row["verified"]),
                    "created_at": row["created_at"],
                    "roles": roles,
                }
            )
        return users
    finally:
        await db.close()


async def delete_user(user_id: str) -> bool:
    """Delete a user. Returns True if deleted, False if not found."""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def remove_role(user_id: str, role_name: str) -> bool:
    """Remove a role from a user. Returns True if removed."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM user_roles WHERE user_id = ? AND role_name = ?",
            (user_id, role_name),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def update_email(user_id: str, email: str) -> None:
    """Update a user's email."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET email = ? WHERE id = ?", (email, user_id)
        )
        await db.commit()
    finally:
        await db.close()


async def update_password(user_id: str, password_hash: str) -> None:
    """Update a user's password hash."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user_by_id(user_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "email": row["email"]}
    finally:
        await db.close()


async def search_users(query: str, limit: int = 10) -> list[dict]:
    """Search users by email prefix."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email FROM users WHERE email LIKE ? ORDER BY email LIMIT ?",
            (f"{query}%", limit),
        )
        return [
            {"id": row["id"], "email": row["email"]}
            for row in await cursor.fetchall()
        ]
    finally:
        await db.close()


# Workspace operations


async def create_workspace(
    user_id: str,
    name: str,
    image: str | None = None,
    default_command: str | None = None,
    mounts: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    db = await get_db()
    try:
        workspace_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mounts_json = json.dumps(mounts) if mounts else None
        env_json = json.dumps(env) if env else None
        await db.execute(
            "INSERT INTO workspaces"
            " (id, user_id, name, image, default_command, mounts,"
            " env, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                user_id,
                name,
                image,
                default_command,
                mounts_json,
                env_json,
                created_at,
            ),
        )
        await db.commit()
        from . import container

        return {
            "id": workspace_id,
            "user_id": user_id,
            "name": name,
            "image": image,
            "default_command": default_command,
            "mounts": mounts,
            "env": env,
            "num_ports": container.DEFAULT_PORTS_PER_WORKSPACE,
            "created_at": created_at,
        }
    finally:
        await db.close()


async def list_workspaces(user_id: str) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, container_id, image, default_command,"
            " mounts, env, created_at FROM workspaces"
            " WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "container_id": row["container_id"],
                "image": row["image"],
                "default_command": row["default_command"],
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def list_shared_workspaces(user_id: str) -> list[dict]:
    """List workspaces shared with (but not owned by) this user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT w.id, w.name, w.container_id, w.image,"
            " w.default_command, w.mounts, w.env, w.created_at,"
            " u.email AS owner_email"
            " FROM workspaces w"
            " JOIN workspace_access wa ON w.id = wa.workspace_id"
            " JOIN users u ON w.user_id = u.id"
            " WHERE wa.user_id = ?"
            " ORDER BY w.created_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "container_id": row["container_id"],
                "image": row["image"],
                "default_command": row["default_command"],
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
                "created_at": row["created_at"],
                "owner_email": row["owner_email"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def get_workspace(workspace_id: str, user_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, name, container_id, num_ports, image,"
            " default_command, mounts, env"
            " FROM workspaces WHERE id = ? AND (user_id = ? OR id IN ("
            "   SELECT workspace_id FROM workspace_access WHERE user_id = ?"
            " ))",
            (workspace_id, user_id, user_id),
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
            "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
            "env": json.loads(row["env"]) if row["env"] else None,
        }
    finally:
        await db.close()


async def get_workspace_by_id(workspace_id: str) -> dict | None:
    """Get a workspace by ID without access control (for admin use)."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, name, container_id, num_ports, image,"
            " default_command, mounts, env"
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
            "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
            "env": json.loads(row["env"]) if row["env"] else None,
        }
    finally:
        await db.close()


async def share_workspace(workspace_id: str, user_id: str) -> None:
    """Grant a user access to a workspace."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO workspace_access (workspace_id, user_id) VALUES (?, ?)",
            (workspace_id, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def unshare_workspace(workspace_id: str, user_id: str) -> None:
    """Revoke a user's access to a workspace."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM workspace_access WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_workspace_members(workspace_id: str) -> list[dict]:
    """Get users who have been granted access to a workspace."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT u.id, u.email FROM users u"
            " JOIN workspace_access wa ON u.id = wa.user_id"
            " WHERE wa.workspace_id = ?"
            " ORDER BY u.email",
            (workspace_id,),
        )
        return [
            {"id": row["id"], "email": row["email"]}
            for row in await cursor.fetchall()
        ]
    finally:
        await db.close()


async def add_port_allocations(workspace_id: str, ports: list[int]) -> None:
    """Allocate ports to a workspace. Raises IntegrityError on conflict."""
    db = await get_db()
    try:
        for port in ports:
            await db.execute(
                "INSERT INTO port_allocations (port, workspace_id) VALUES (?, ?)",
                (port, workspace_id),
            )
        await db.commit()
    finally:
        await db.close()


def _port_in_use(port: int) -> bool:
    """Check if a port is bound at the OS level."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


async def find_and_allocate_ports(
    workspace_id: str, count: int, start: int
) -> list[int]:
    """Atomically find free ports and allocate them in a single transaction."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT port FROM port_allocations")
        rows = await cursor.fetchall()
        used = {row["port"] for row in rows}

        ports = []
        port = start
        while len(ports) < count:
            if port not in used and not _port_in_use(port):
                ports.append(port)
            port += 1

        for p in ports:
            await db.execute(
                "INSERT INTO port_allocations (port, workspace_id) VALUES (?, ?)",
                (p, workspace_id),
            )
        await db.commit()
        return ports
    finally:
        await db.close()


async def remove_port_allocations(workspace_id: str, ports: list[int]) -> None:
    """Remove specific port allocations from a workspace."""
    db = await get_db()
    try:
        for port in ports:
            await db.execute(
                "DELETE FROM port_allocations WHERE port = ? AND workspace_id = ?",
                (port, workspace_id),
            )
        await db.commit()
    finally:
        await db.close()


async def get_workspace_ports(workspace_id: str) -> list[int]:
    """Return all allocated ports for a workspace, sorted."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT port FROM port_allocations WHERE workspace_id = ? ORDER BY port",
            (workspace_id,),
        )
        rows = await cursor.fetchall()
        return [row["port"] for row in rows]
    finally:
        await db.close()


async def get_all_allocated_ports() -> set[int]:
    """Return all allocated port numbers across all workspaces."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT port FROM port_allocations")
        rows = await cursor.fetchall()
        return {row["port"] for row in rows}
    finally:
        await db.close()


async def delete_workspace(workspace_id: str, user_id: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM workspaces WHERE id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def update_workspace_container(
    workspace_id: str, container_id: str | None
) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE workspaces SET container_id = ? WHERE id = ?",
            (container_id, workspace_id),
        )
        await db.commit()
    finally:
        await db.close()


async def update_workspace(
    workspace_id: str,
    user_id: str,
    **fields: str | None,
) -> bool:
    """Update workspace fields. Only provided fields are changed."""
    allowed = {"name", "image", "default_command", "mounts", "env"}
    to_set = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("mounts", "env"):
            to_set[k] = json.dumps(v) if v is not None else None
        else:
            to_set[k] = v
    if not to_set:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in to_set)
    values = list(to_set.values()) + [workspace_id, user_id]
    db = await get_db()
    try:
        cursor = await db.execute(
            f"UPDATE workspaces SET {set_clause}"  # noqa: S608
            " WHERE id = ? AND user_id = ?",
            values,
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_user_workspaces_with_containers(user_id: str) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, container_id FROM workspaces WHERE user_id = ? AND container_id IS NOT NULL",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": row["id"], "container_id": row["container_id"]}
            for row in rows
        ]
    finally:
        await db.close()


# Token blocklist


async def blocklist_token(jti: str, expires_at: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO token_blocklist (jti, expires_at) VALUES (?, ?)",
            (jti, expires_at),
        )
        await db.commit()
    finally:
        await db.close()


async def is_token_blocklisted(jti: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT 1 FROM token_blocklist WHERE jti = ?",
            (jti,),
        )
        row = await cursor.fetchone()
        return row is not None
    finally:
        await db.close()


# Message history


# Login attempt tracking (brute-force protection)


async def record_failed_login(email: str) -> None:
    """Record a failed login attempt for an email. Resets after the window."""
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        # Try to update existing row
        await db.execute(
            """INSERT INTO login_attempts (email, attempt_count, first_attempt_at)
               VALUES (?, 1, ?) ON CONFLICT(email) DO UPDATE SET
               attempt_count = attempt_count + 1""",
            (email, now),
        )
        await db.commit()
    finally:
        await db.close()


async def get_login_attempt_info(
    email: str,
) -> dict[str, int | str | None] | None:
    """Return login attempt info for an email, or None if no attempts tracked."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT attempt_count, first_attempt_at, locked_until
               FROM login_attempts WHERE email = ?""",
            (email,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "attempt_count": row["attempt_count"],
            "first_attempt_at": row["first_attempt_at"],
            "locked_until": row["locked_until"],
        }
    finally:
        await db.close()


async def set_login_lockout(email: str, locked_until: str) -> None:
    """Set the lockout time for an email after too many failed attempts."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE login_attempts SET locked_until = ? WHERE email = ?",
            (locked_until, email),
        )
        await db.commit()
    finally:
        await db.close()


async def clear_login_attempts(email: str) -> None:
    """Clear all login attempts for an email (on successful login)."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM login_attempts WHERE email = ?", (email,)
        )
        await db.commit()
    finally:
        await db.close()


# Chat messages


async def add_chat_message(
    workspace_id: str, user_id: str, user_email: str, message: str
) -> dict:
    """Store a chat message and return it."""
    db = await get_db()
    try:
        msg_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO chat_messages (id, workspace_id, user_id, user_email, message)"
            " VALUES (?, ?, ?, ?, ?)",
            (msg_id, workspace_id, user_id, user_email, message),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT created_at FROM chat_messages WHERE id = ?", (msg_id,)
        )
        row = await cursor.fetchone()
        return {
            "id": msg_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "user_email": user_email,
            "message": message,
            "created_at": row["created_at"],
        }
    finally:
        await db.close()


async def delete_chat_message(message_id: str, user_id: str) -> bool:
    """Soft-delete a chat message by replacing its text.

    Only the author can delete their own messages.  The row is
    preserved so the history shows a placeholder.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE chat_messages SET message = '<message deleted by author>'"
            " WHERE id = ? AND user_id = ?",
            (message_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_chat_messages(workspace_id: str, limit: int = 50) -> list[dict]:
    """Get the most recent chat messages for a workspace."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, workspace_id, user_id, user_email, message, created_at"
            " FROM chat_messages WHERE workspace_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (workspace_id, limit),
        )
        rows = await cursor.fetchall()
        # Return in chronological order (oldest first)
        return list(
            reversed(
                [
                    {
                        "id": row["id"],
                        "workspace_id": row["workspace_id"],
                        "user_id": row["user_id"],
                        "user_email": row["user_email"],
                        "message": row["message"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            )
        )
    finally:
        await db.close()


# Invitations


async def create_invitation(email: str, invited_by: str) -> dict:
    """Create a new invitation. Returns the invitation dict."""
    db = await get_db()
    try:
        invitation_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO invitations (id, email, invited_by) VALUES (?, ?, ?)",
            (invitation_id, email, invited_by),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT created_at FROM invitations WHERE id = ?",
            (invitation_id,),
        )
        row = await cursor.fetchone()
        return {
            "id": invitation_id,
            "email": email,
            "invited_by": invited_by,
            "status": "pending",
            "created_at": row["created_at"],
        }
    finally:
        await db.close()


async def get_invitation(invitation_id: str) -> dict | None:
    """Get an invitation by ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, invited_by, status, created_at, accepted_at"
            " FROM invitations WHERE id = ?",
            (invitation_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "invited_by": row["invited_by"],
            "status": row["status"],
            "created_at": row["created_at"],
            "accepted_at": row["accepted_at"],
        }
    finally:
        await db.close()


async def get_pending_invitation_by_email(email: str) -> dict | None:
    """Get a pending invitation for the given email."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, invited_by, status, created_at, accepted_at"
            " FROM invitations WHERE email = ? AND status = 'pending'",
            (email,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "invited_by": row["invited_by"],
            "status": row["status"],
            "created_at": row["created_at"],
            "accepted_at": row["accepted_at"],
        }
    finally:
        await db.close()


async def list_invitations() -> list[dict]:
    """List all invitations, most recent first."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT i.id, i.email, i.invited_by, i.status,"
            " i.created_at, i.accepted_at, u.email AS invited_by_email"
            " FROM invitations i"
            " JOIN users u ON i.invited_by = u.id"
            " ORDER BY i.created_at DESC"
        )
        return [
            {
                "id": row["id"],
                "email": row["email"],
                "invited_by": row["invited_by"],
                "invited_by_email": row["invited_by_email"],
                "status": row["status"],
                "created_at": row["created_at"],
                "accepted_at": row["accepted_at"],
            }
            for row in await cursor.fetchall()
        ]
    finally:
        await db.close()


async def mark_invitation_accepted(invitation_id: str) -> bool:
    """Mark an invitation as accepted. Returns True if updated."""
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "UPDATE invitations SET status = 'accepted', accepted_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (now, invitation_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def revoke_invitation(invitation_id: str) -> bool:
    """Revoke a pending invitation. Returns True if updated."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE invitations SET status = 'revoked'"
            " WHERE id = ? AND status = 'pending'",
            (invitation_id,),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()
