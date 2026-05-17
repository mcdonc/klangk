import os

import aiosqlite
import uuid
from pathlib import Path

_data_dir = Path(os.environ.get("BARK_DATA_DIR", str(Path.home() / ".bark" / "data")))
DB_PATH = _data_dir / "bark.db"


async def _get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db() -> None:
    db = await _get_db()
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                container_id TEXT,
                num_ports INTEGER NOT NULL DEFAULT 5,  -- see container_manager.DEFAULT_PORTS_PER_WORKSPACE
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
            CREATE TABLE IF NOT EXISTS token_blocklist (
                jti TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tool_args TEXT,
                tool_output TEXT,
                is_complete INTEGER NOT NULL DEFAULT 0,
                is_queued INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_workspace
            ON messages(workspace_id, id)
        """)
        await db.commit()
    finally:
        await db.close()


async def create_user(username: str, password_hash: str) -> dict:
    db = await _get_db()
    try:
        user_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
            (user_id, username, password_hash),
        )
        await db.commit()
        return {"id": user_id, "username": username}
    finally:
        await db.close()


async def get_user_by_username(username: str) -> dict | None:
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "password_hash": row["password_hash"],
        }
    finally:
        await db.close()


async def get_user_by_id(user_id: str) -> dict | None:
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row["id"], "username": row["username"]}
    finally:
        await db.close()


# Workspace operations


async def create_workspace(user_id: str, name: str) -> dict:
    db = await _get_db()
    try:
        workspace_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO workspaces (id, user_id, name) VALUES (?, ?, ?)",
            (workspace_id, user_id, name),
        )
        await db.commit()
        from . import container_manager

        return {
            "id": workspace_id,
            "user_id": user_id,
            "name": name,
            "num_ports": container_manager.DEFAULT_PORTS_PER_WORKSPACE,
        }
    finally:
        await db.close()


async def list_workspaces(user_id: str) -> list[dict]:
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, container_id, created_at FROM workspaces WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "container_id": row["container_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def get_workspace(workspace_id: str, user_id: str) -> dict | None:
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id, user_id, name, container_id, num_ports FROM workspaces WHERE id = ? AND user_id = ?",
            (workspace_id, user_id),
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
        }
    finally:
        await db.close()


async def add_port_allocations(workspace_id: str, ports: list[int]) -> None:
    """Allocate ports to a workspace. Raises IntegrityError on conflict."""
    db = await _get_db()
    try:
        for port in ports:
            await db.execute(
                "INSERT INTO port_allocations (port, workspace_id) VALUES (?, ?)",
                (port, workspace_id),
            )
        await db.commit()
    finally:
        await db.close()


async def remove_port_allocations(workspace_id: str, ports: list[int]) -> None:
    """Remove specific port allocations from a workspace."""
    db = await _get_db()
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
    db = await _get_db()
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
    db = await _get_db()
    try:
        cursor = await db.execute("SELECT port FROM port_allocations")
        rows = await cursor.fetchall()
        return {row["port"] for row in rows}
    finally:
        await db.close()


async def delete_workspace(workspace_id: str, user_id: str) -> bool:
    db = await _get_db()
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
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE workspaces SET container_id = ? WHERE id = ?",
            (container_id, workspace_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user_workspaces_with_containers(user_id: str) -> list[dict]:
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id, container_id FROM workspaces WHERE user_id = ? AND container_id IS NOT NULL",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [{"id": row["id"], "container_id": row["container_id"]} for row in rows]
    finally:
        await db.close()


# Token blocklist


async def blocklist_token(jti: str, expires_at: str) -> None:
    db = await _get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO token_blocklist (jti, expires_at) VALUES (?, ?)",
            (jti, expires_at),
        )
        await db.commit()
    finally:
        await db.close()


async def is_token_blocklisted(jti: str) -> bool:
    db = await _get_db()
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


async def save_message(
    workspace_id: str,
    entry_type: str,
    content: str,
    tool_args: str | None = None,
    tool_output: str | None = None,
    is_complete: bool = False,
    is_queued: bool = False,
) -> int:
    db = await _get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO messages (workspace_id, entry_type, content, tool_args, tool_output, is_complete, is_queued)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                workspace_id,
                entry_type,
                content,
                tool_args,
                tool_output,
                1 if is_complete else 0,
                1 if is_queued else 0,
            ),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def get_messages(workspace_id: str) -> list[dict]:
    db = await _get_db()
    try:
        cursor = await db.execute(
            """SELECT id, entry_type, content, tool_args, tool_output, is_complete, is_queued, created_at
               FROM messages WHERE workspace_id = ? ORDER BY id""",
            (workspace_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "entry_type": row["entry_type"],
                "content": row["content"],
                "tool_args": row["tool_args"],
                "tool_output": row["tool_output"],
                "is_complete": bool(row["is_complete"]),
                "is_queued": bool(row["is_queued"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def delete_workspace_messages(workspace_id: str) -> None:
    db = await _get_db()
    try:
        await db.execute("DELETE FROM messages WHERE workspace_id = ?", (workspace_id,))
        await db.commit()
    finally:
        await db.close()
