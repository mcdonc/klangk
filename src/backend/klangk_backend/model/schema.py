"""Database schema creation and in-place migrations (``init_db``)."""

from ._core import get_db
from .users import _backfill_handles


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
                num_ports INTEGER NOT NULL DEFAULT 5,
                image TEXT,  -- custom container image; NULL means use default
                default_command TEXT,  -- auto-run in terminal on connect
                auto_start INTEGER NOT NULL DEFAULT 0,  -- start on server boot
                mounts TEXT,  -- JSON array of host:container mount specs
                env TEXT,  -- JSON dict of custom environment variables
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            )
        """)
        # Migration: add auto_start column to existing workspaces tables
        cursor = await db.execute("PRAGMA table_info(workspaces)")
        ws_cols = {row[1] for row in await cursor.fetchall()}
        if "auto_start" not in ws_cols:
            await db.execute(
                "ALTER TABLE workspaces"
                " ADD COLUMN auto_start INTEGER NOT NULL DEFAULT 0"
            )
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
                expires_at TEXT NOT NULL,
                new_token TEXT
            )
        """)
        # Migration: add new_token column to existing token_blocklist tables
        cursor = await db.execute("PRAGMA table_info(token_blocklist)")
        bl_cols = {row[1] for row in await cursor.fetchall()}
        if "new_token" not in bl_cols:  # pragma: no cover
            await db.execute(
                "ALTER TABLE token_blocklist ADD COLUMN new_token TEXT"
            )
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
