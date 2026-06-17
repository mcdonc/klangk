import json
import os
import re
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

# Chat message types
MSG_USER = 0
MSG_AGENT = 1
MSG_SYSTEM = 2

# Agent identity
AGENT_USER_ID = "00000000-0000-0000-0000-000000000001"
AGENT_EMAIL = os.environ.get("KLANGK_CHAT_AGENT_EMAIL", "MrBoops@example.com")
AGENT_HANDLE = os.environ.get("KLANGK_CHAT_AGENT_HANDLE", "MrBoops")
AGENT_MENTION = AGENT_HANDLE


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
                handle TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: make password_hash nullable, add OIDC columns, add handle.
        # SQLite can't ALTER COLUMN, so we recreate the table if needed.
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = {row[1]: row for row in await cursor.fetchall()}
        needs_recreate = False
        if "password_hash" in columns and columns["password_hash"][3]:
            # password_hash has NOT NULL — need to drop it for OIDC users
            needs_recreate = True
        if "provider" not in columns:
            needs_recreate = True
        if "handle" not in columns:
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
                    handle TEXT UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            # Copy existing data — old tables may lack some columns
            old_cols = list(columns.keys())
            shared = [
                c
                for c in old_cols
                if c
                in (
                    "id",
                    "email",
                    "password_hash",
                    "verified",
                    "created_at",
                    "handle",
                )
            ]
            cols_str = ", ".join(shared)
            await db.execute(
                f"INSERT INTO users_new ({cols_str})"  # noqa: S608
                f" SELECT {cols_str} FROM users"
            )
            await db.execute("DROP TABLE users")
            await db.execute("ALTER TABLE users_new RENAME TO users")
        # Backfill handles for existing users that don't have one.
        await _backfill_handles(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                container_id TEXT,
                num_ports INTEGER NOT NULL DEFAULT 5,  -- see container.DEFAULT_PORTS_PER_WORKSPACE
                image TEXT,  -- custom container image; NULL means use default
                default_command TEXT,  -- auto-run in terminal on connect
                mounts TEXT,  -- JSON array of host:container mount specs
                env TEXT,  -- JSON dict of custom environment variables
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS port_allocations (
                port INTEGER PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_groups (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                source TEXT NOT NULL DEFAULT 'manual',
                PRIMARY KEY (user_id, group_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS acl_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource TEXT NOT NULL,
                position INTEGER NOT NULL,
                action INTEGER NOT NULL,
                principal_type INTEGER NOT NULL,
                user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                group_id TEXT REFERENCES groups(id) ON DELETE CASCADE,
                system_principal INTEGER,  -- 0 = Everyone, 1 = Authenticated
                permission TEXT NOT NULL,
                UNIQUE(resource, position)
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
                message_type INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: add message_type column to existing chat_messages tables
        cursor = await db.execute("PRAGMA table_info(chat_messages)")
        chat_cols = {row[1] for row in await cursor.fetchall()}
        if "message_type" not in chat_cols:
            await db.execute(
                "ALTER TABLE chat_messages"
                " ADD COLUMN message_type INTEGER NOT NULL DEFAULT 0"
            )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_mentions (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_mentions_user
            ON chat_mentions(user_id, workspace_id)
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
        # Migration: drop legacy role and workspace_access tables
        for table in ("user_roles", "roles", "workspace_access"):
            await db.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
        await db.commit()
    finally:
        await db.close()


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
    return _hash_fallback_handle(base)  # pragma: no cover


def _hash_fallback_handle(base: str) -> str:  # pragma: no cover
    import hashlib

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


# ACL constants
ACTION_DENY = 0
ACTION_ALLOW = 1

PRINCIPAL_SYSTEM = 0
PRINCIPAL_USER = 1
PRINCIPAL_GROUP = 2

SYSTEM_EVERYONE = 0
SYSTEM_AUTHENTICATED = 1


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
        await db.commit()
        return {
            "id": user_id,
            "email": email,
            "handle": handle,
            "verified": verified,
        }
    finally:
        await db.close()


async def get_user_handle(user_id: str) -> str | None:
    """Return the handle for a user, or None if not found."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT handle FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["handle"] if row else None
    finally:
        await db.close()


async def set_user_handle(user_id: str, handle: str) -> None:
    """Update a user's handle. Raises ValueError on invalid or conflict."""
    error = validate_handle(handle)
    if error:
        raise ValueError(error)
    db = await get_db()
    try:
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
        await db.commit()
    finally:
        await db.close()


async def get_user_by_handle(handle: str) -> dict | None:
    """Find a user by handle."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, handle FROM users WHERE handle = ?",
            (handle,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "handle": row["handle"],
        }
    finally:
        await db.close()


async def get_user_by_external_id(
    provider: str, external_id: str
) -> dict | None:
    """Find a user by OIDC provider + external ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, password_hash, verified, provider,"
            " external_id, handle"
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
            "handle": row["handle"],
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


# --- Group operations ---


async def create_group(
    name: str, description: str | None = None, group_id: str | None = None
) -> dict:
    """Create a group. Returns the group dict."""
    db = await get_db()
    try:
        gid = group_id or str(uuid.uuid4())
        await db.execute(
            "INSERT INTO groups (id, name, description) VALUES (?, ?, ?)",
            (gid, name, description),
        )
        await db.commit()
        return {"id": gid, "name": name, "description": description}
    finally:
        await db.close()


async def get_group_by_name(name: str) -> dict | None:
    """Find a group by name."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, description, created_at"
            " FROM groups WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
        }
    finally:
        await db.close()


async def get_group_by_id(group_id: str) -> dict | None:
    """Find a group by ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, description, created_at"
            " FROM groups WHERE id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
        }
    finally:
        await db.close()


async def list_groups() -> list[dict]:
    """List all groups."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, description, created_at"
            " FROM groups ORDER BY name"
        )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "created_at": row["created_at"],
            }
            for row in await cursor.fetchall()
        ]
    finally:
        await db.close()


async def delete_group(group_id: str) -> bool:
    """Delete a group. Returns True if deleted."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM groups WHERE id = ?", (group_id,)
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


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
    db = await get_db()
    try:
        cursor = await db.execute(
            f"UPDATE groups SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def add_user_to_group(
    user_id: str, group_id: str, source: str = "manual"
) -> None:
    """Add a user to a group (idempotent)."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO user_groups (user_id, group_id, source)"
            " VALUES (?, ?, ?)",
            (user_id, group_id, source),
        )
        await db.commit()
    finally:
        await db.close()


async def remove_user_from_group(user_id: str, group_id: str) -> bool:
    """Remove a user from a group. Returns True if removed."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM user_groups WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_group_members(group_id: str) -> list[dict]:
    """List users in a group."""
    db = await get_db()
    try:
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
    finally:
        await db.close()


async def get_user_group_ids(user_id: str) -> list[str]:
    """Get all group IDs for a user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT group_id FROM user_groups WHERE user_id = ?",
            (user_id,),
        )
        return [row["group_id"] for row in await cursor.fetchall()]
    finally:
        await db.close()


async def get_user_oidc_sync_group_ids(user_id: str) -> list[str]:
    """Get group IDs where membership source is 'oidc_sync'."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT group_id FROM user_groups"
            " WHERE user_id = ? AND source = 'oidc_sync'",
            (user_id,),
        )
        return [row["group_id"] for row in await cursor.fetchall()]
    finally:
        await db.close()


async def get_user_groups(user_id: str) -> list[dict]:
    """Get all groups a user belongs to."""
    db = await get_db()
    try:
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
    finally:
        await db.close()


# --- ACL entry operations ---


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
    """Add an ACL entry. Returns the entry ID."""
    db = await get_db()
    try:
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
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def get_acl_entries(resource: str) -> list[dict]:
    """Get ACL entries for a resource, ordered by position."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE resource = ?"
            " ORDER BY position",
            (resource,),
        )
        return [
            {
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
            for row in await cursor.fetchall()
        ]
    finally:
        await db.close()


async def get_acl_entries_resolved(resource: str) -> list[dict]:
    """Get ACL entries with resolved principal names."""
    db = await get_db()
    try:
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
    finally:
        await db.close()


async def replace_acl_entries(resource: str, entries: list[dict]) -> None:
    """Replace all ACL entries for a resource."""
    db = await get_db()
    try:
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
        await db.commit()
    finally:
        await db.close()


async def delete_acl_entries_for_resource(resource: str) -> int:
    """Delete all ACL entries for a resource. Returns count deleted."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "DELETE FROM acl_entries WHERE resource = ?", (resource,)
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def get_acl_entries_by_principal_user(user_id: str) -> list[dict]:
    """Get all ACL entries referencing a specific user."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE principal_type = ? AND user_id = ?"
            " ORDER BY resource, position",
            (PRINCIPAL_USER, user_id),
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def get_acl_entries_by_principal_group(group_id: str) -> list[dict]:
    """Get all ACL entries referencing a specific group."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE principal_type = ? AND group_id = ?"
            " ORDER BY resource, position",
            (PRINCIPAL_GROUP, group_id),
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def get_acl_tree_summary() -> list[dict]:
    """Get all distinct resources with their ACE counts."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT resource, COUNT(*) as ace_count"
            " FROM acl_entries GROUP BY resource"
            " ORDER BY resource"
        )
        return [
            {"resource": row["resource"], "ace_count": row["ace_count"]}
            for row in await cursor.fetchall()
        ]
    finally:
        await db.close()


async def get_user_by_email(email: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, password_hash, verified, provider,"
            " external_id, handle"
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
            "handle": row["handle"],
        }
    finally:
        await db.close()


async def list_users() -> list[dict]:
    """List all users with their groups."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, verified, created_at FROM users ORDER BY created_at"
        )
        users = []
        for row in await cursor.fetchall():
            group_cursor = await db.execute(
                "SELECT g.id, g.name FROM groups g"
                " JOIN user_groups ug ON g.id = ug.group_id"
                " WHERE ug.user_id = ?",
                (row["id"],),
            )
            groups = [
                {"id": r["id"], "name": r["name"]}
                for r in await group_cursor.fetchall()
            ]
            users.append(
                {
                    "id": row["id"],
                    "email": row["email"],
                    "verified": bool(row["verified"]),
                    "created_at": row["created_at"],
                    "groups": groups,
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
            "SELECT id, email, handle FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "handle": row["handle"],
        }
    finally:
        await db.close()


async def search_users(query: str, limit: int = 10) -> list[dict]:
    """Search users by email prefix."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, email, handle FROM users"
            " WHERE email LIKE ? ORDER BY email LIMIT ?",
            (f"{query}%", limit),
        )
        return [
            {"id": row["id"], "email": row["email"], "handle": row["handle"]}
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
    """List workspaces shared with (but not owned by) this user via ACL.

    Finds workspaces where the user has access through either a direct
    user-level ACE or a group-level ACE on ``/workspaces/{id}``.
    """
    db = await get_db()
    try:
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
            " w.default_command, w.mounts, w.env, w.created_at,"
            " u.email AS owner_email"
            " FROM workspaces w"
            " JOIN acl_entries ae ON ae.resource = '/workspaces/' || w.id"
            " JOIN users u ON w.user_id = u.id"
            " WHERE ae.action = ? AND w.user_id != ?"
            "   AND ("
            f"    (ae.principal_type = {PRINCIPAL_USER} AND ae.user_id = ?)"
            f"    {group_clause}"
            "   )"
            " ORDER BY w.created_at",
            (ACTION_ALLOW, user_id, user_id, *group_ids),
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


async def get_workspace(
    workspace_id: str, user_id: str | None = None
) -> dict | None:
    """Get a workspace by ID.

    If user_id is provided, restricts to workspaces owned by that user.
    Access control for shared workspaces is handled by the ACL layer.
    """
    db = await get_db()
    try:
        if user_id is not None:
            cursor = await db.execute(
                "SELECT id, user_id, name, container_id, num_ports, image,"
                " default_command, mounts, env"
                " FROM workspaces WHERE id = ? AND user_id = ?",
                (workspace_id, user_id),
            )
        else:
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


async def get_workspace_members(workspace_id: str) -> list[dict]:
    """Get users who have been granted access to a workspace via ACL.

    Returns users with direct user-level ACEs on /workspaces/{id},
    excluding the workspace owner.
    """
    db = await get_db()
    try:
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

_MENTION_RE = re.compile(r"@(\S+)")


async def parse_mentions(
    db: aiosqlite.Connection, message: str, workspace_id: str
) -> list[str]:
    """Extract @email mentions from message text and resolve to user IDs.

    Returns a deduplicated list of user IDs for emails that belong to
    workspace members (including the owner).
    """
    candidates = _MENTION_RE.findall(message)
    if not candidates:
        return []
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        low = c.lower()
        if low not in seen:
            seen.add(low)
            unique.append(low)

    placeholders = ",".join("?" for _ in unique)
    cursor = await db.execute(
        "SELECT DISTINCT u.id, LOWER(u.email) AS email FROM users u"
        " JOIN acl_entries ae ON ae.user_id = u.id"
        " WHERE LOWER(u.email) IN (" + placeholders + ")"
        "   AND ae.resource = ?"
        "   AND ae.principal_type = ? AND ae.action = ?"
        " UNION"
        " SELECT w.user_id AS id, LOWER(u2.email) AS email"
        " FROM workspaces w JOIN users u2 ON u2.id = w.user_id"
        " WHERE w.id = ? AND LOWER(u2.email) IN (" + placeholders + ")",
        (
            *unique,
            f"/workspaces/{workspace_id}",
            PRINCIPAL_USER,
            ACTION_ALLOW,
            workspace_id,
            *unique,
        ),
    )
    rows = await cursor.fetchall()
    return [row["id"] for row in rows]


async def add_chat_message(
    workspace_id: str,
    user_id: str,
    user_email: str,
    message: str,
    message_type: int = MSG_USER,
) -> dict:
    """Store a chat message and return it."""
    db = await get_db()
    try:
        msg_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO chat_messages"
            " (id, workspace_id, user_id, user_email, message, message_type)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, workspace_id, user_id, user_email, message, message_type),
        )
        mentioned_user_ids = await parse_mentions(db, message, workspace_id)
        for uid in mentioned_user_ids:
            await db.execute(
                "INSERT INTO chat_mentions (id, message_id, user_id, workspace_id)"
                " VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), msg_id, uid, workspace_id),
            )
        await db.commit()
        cursor = await db.execute(
            "SELECT created_at FROM chat_messages WHERE id = ?", (msg_id,)
        )
        row = await cursor.fetchone()
        # Fetch the current handle for the sender.
        handle_cursor = await db.execute(
            "SELECT handle FROM users WHERE id = ?", (user_id,)
        )
        handle_row = await handle_cursor.fetchone()
        return {
            "id": msg_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "user_email": user_email,
            "user_handle": handle_row["handle"] if handle_row else None,
            "message": message,
            "message_type": message_type,
            "created_at": row["created_at"],
            "mentions": mentioned_user_ids,
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


async def get_chat_messages_before(
    workspace_id: str, before_id: str, limit: int = 50
) -> list[dict]:
    """Get older chat messages before a given message ID."""
    db = await get_db()
    try:
        # Get the created_at and rowid of the anchor message
        cursor = await db.execute(
            "SELECT created_at, rowid FROM chat_messages WHERE id = ?",
            (before_id,),
        )
        anchor = await cursor.fetchone()
        if anchor is None:
            return []
        anchor_ts = anchor["created_at"]
        anchor_rowid = anchor["rowid"]

        cursor = await db.execute(
            "SELECT c.id, c.workspace_id, c.user_id, c.user_email,"
            " c.message, c.message_type, c.created_at,"
            " u.handle AS user_handle"
            " FROM chat_messages c LEFT JOIN users u ON c.user_id = u.id"
            " WHERE c.workspace_id = ?"
            " AND (c.created_at < ? OR (c.created_at = ? AND c.rowid < ?))"
            " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
            (workspace_id, anchor_ts, anchor_ts, anchor_rowid, limit),
        )
        rows = await cursor.fetchall()
        messages = list(
            reversed(
                [
                    {
                        "id": row["id"],
                        "workspace_id": row["workspace_id"],
                        "user_id": row["user_id"],
                        "user_email": row["user_email"],
                        "user_handle": row["user_handle"],
                        "message": row["message"],
                        "message_type": row["message_type"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            )
        )
        if messages:
            msg_ids = [m["id"] for m in messages]
            placeholders = ",".join("?" for _ in msg_ids)
            cursor = await db.execute(
                "SELECT message_id, user_id FROM chat_mentions"
                " WHERE message_id IN (" + placeholders + ")",
                msg_ids,
            )
            mention_rows = await cursor.fetchall()
            mentions_by_msg: dict[str, list[str]] = {}
            for mr in mention_rows:
                mentions_by_msg.setdefault(mr["message_id"], []).append(
                    mr["user_id"]
                )
            for m in messages:
                m["mentions"] = mentions_by_msg.get(m["id"], [])
        return messages
    finally:
        await db.close()


async def get_chat_messages(workspace_id: str, limit: int = 50) -> list[dict]:
    """Get the most recent chat messages for a workspace."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT c.id, c.workspace_id, c.user_id, c.user_email,"
            " c.message, c.message_type, c.created_at, u.handle AS user_handle"
            " FROM chat_messages c LEFT JOIN users u ON c.user_id = u.id"
            " WHERE c.workspace_id = ?"
            " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
            (workspace_id, limit),
        )
        rows = await cursor.fetchall()
        messages = list(
            reversed(
                [
                    {
                        "id": row["id"],
                        "workspace_id": row["workspace_id"],
                        "user_id": row["user_id"],
                        "user_email": row["user_email"],
                        "user_handle": row["user_handle"],
                        "message": row["message"],
                        "message_type": row["message_type"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            )
        )
        if messages:
            msg_ids = [m["id"] for m in messages]
            placeholders = ",".join("?" for _ in msg_ids)
            cursor = await db.execute(
                "SELECT message_id, user_id FROM chat_mentions"
                " WHERE message_id IN (" + placeholders + ")",
                msg_ids,
            )
            mention_rows = await cursor.fetchall()
            mentions_by_msg: dict[str, list[str]] = {}
            for mr in mention_rows:
                mentions_by_msg.setdefault(mr["message_id"], []).append(
                    mr["user_id"]
                )
            for m in messages:
                m["mentions"] = mentions_by_msg.get(m["id"], [])
        return messages
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
