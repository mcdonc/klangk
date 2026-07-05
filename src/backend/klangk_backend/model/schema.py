"""Database schema creation and in-place migrations (``init_db``)."""

from .db import get_db
from .acl import PRINCIPAL_USER
from .users import AGENT_USER_ID, backfill_handles


async def init_db() -> None:
    db = await get_db()
    try:
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT 'local',
                external_id TEXT,
                handle TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                -- (D) the system agent must never carry a password.
                CHECK (id != '{AGENT_USER_ID}' OR password_hash IS NULL)
            )
        """)  # noqa: S608
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
            await db.execute(f"""
                CREATE TABLE users_new (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT 'local',
                    external_id TEXT,
                    handle TEXT UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    CHECK (id != '{AGENT_USER_ID}' OR password_hash IS NULL)
                )
            """)  # noqa: S608
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
        await backfill_handles(db)
        # --- Data-model belt-and-suspenders for the system agent (#1135) ---
        # The function-layer AgentPrincipalError guards are the *friendly*
        # choke point (typed error, HTTP 400). These schema constraints are
        # the *terminal* backstop: they fire at the DB regardless of which
        # Python function wrote the row, so a raw-SQL writer (the exact bug
        # class the re-audit found twice: replace_acl_entries, the seed
        # path) cannot make the agent an ACL principal, mutate its identity,
        # or delete it. The agent UUID is a fixed, source-published constant,
        # so it can be baked in here. (E,F are triggers because CHECK cannot
        # express "row must exist" or compare OLD vs NEW.)
        # (E) the agent row must never be deleted.
        await db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS agent_user_cannot_be_deleted
            BEFORE DELETE ON users
            FOR EACH ROW
            WHEN OLD.id = '{AGENT_USER_ID}'
            BEGIN
                SELECT RAISE(ABORT, 'Cannot delete the system agent user');
            END
        """)  # noqa: S608
        # (F) the agent's identity columns are immutable: it must stay
        # provider='system' with no linked OIDC identity (external_id),
        # which is the #1145 skeleton-key vector. email is intentionally
        # NOT guarded here -- it is legitimately re-seeded from env at boot
        # (ON CONFLICT DO UPDATE SET email); its policy lives at the fn
        # layer (#1145).
        _agent_identity_msg = (
            "Cannot mutate the system agent identity columns"
            " (provider/external_id are the OIDC-link columns)"
        )
        # The message is interpolated as a single quoted SQL literal so
        # SQLite sees one string token -- SQLite does not concatenate
        # adjacent literals (unlike Python) and pre-3.x rejects `||` inside
        # RAISE(), so both ways of splitting it are syntax errors there.
        await db.execute(f"""
            CREATE TRIGGER IF NOT EXISTS agent_user_identity_immutable
            BEFORE UPDATE OF provider, external_id ON users
            FOR EACH ROW
            WHEN OLD.id = '{AGENT_USER_ID}'
            BEGIN
                SELECT RAISE(ABORT, '{_agent_identity_msg}');
            END
        """)  # noqa: S608
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                container_id TEXT,
                num_ports INTEGER NOT NULL DEFAULT 5,
                image TEXT,  -- custom container image; NULL means use default
                service_command TEXT,  -- auto-run in terminal on connect
                auto_start INTEGER NOT NULL DEFAULT 0,  -- start on server boot
                -- setup lifecycle: pending (setup expected/running) /
                -- complete (prereqs met, service cmd may fire) / failed.
                -- Descriptive, not proscriptive: a workspace is created
                -- in whichever state matches reality (see #1033).
                setup_state TEXT NOT NULL DEFAULT 'complete',
                -- shell command polled via `podman exec` to gauge
                -- service health inside the container (see #1015).
                -- NULL means no health monitoring.
                health_check TEXT,
                mounts TEXT,  -- JSON array of host:container mount specs
                env TEXT,  -- JSON dict of custom environment variables
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name),
                -- (C) the system agent must never own a workspace.
                CHECK (user_id != '{AGENT_USER_ID}')
            )
        """)  # noqa: S608
        # Migration: add auto_start column to existing workspaces tables
        cursor = await db.execute("PRAGMA table_info(workspaces)")
        ws_cols = {row[1] for row in await cursor.fetchall()}
        # Migration: rename default_command -> service_command (#1203).
        # The on-disk column is renamed in place (SQLite >= 3.25 supports
        # ALTER TABLE ... RENAME COLUMN). Fresh installs already create the
        # column as service_command (see CREATE TABLE above), so this only
        # touches databases that still carry the legacy default_command column.
        if "default_command" in ws_cols and "service_command" not in ws_cols:
            await db.execute(
                "ALTER TABLE workspaces"
                " RENAME COLUMN default_command TO service_command"
            )
        if "auto_start" not in ws_cols:
            await db.execute(
                "ALTER TABLE workspaces"
                " ADD COLUMN auto_start INTEGER NOT NULL DEFAULT 0"
            )
        # Migration: add setup_state column (#1033). Defaults to
        # 'complete' so existing workspaces (already set up in their
        # persisted volumes) keep firing their service command.
        if "setup_state" not in ws_cols:
            await db.execute(
                "ALTER TABLE workspaces"
                " ADD COLUMN setup_state TEXT NOT NULL DEFAULT 'complete'"
            )
        # Migration: add health_check column (#1015). NULL by default
        # so existing workspaces keep the no-health-monitoring behavior.
        if "health_check" not in ws_cols:
            await db.execute(
                "ALTER TABLE workspaces ADD COLUMN health_check TEXT"
            )
        # Migration: add mounts/env columns (#1264). These are in the
        # CREATE TABLE above but had no ADD COLUMN migration, so DBs
        # created before they shipped lacked them and errored on any
        # read/write of mounts/env. NULL by default (no mounts/overrides).
        if "mounts" not in ws_cols:
            await db.execute("ALTER TABLE workspaces ADD COLUMN mounts TEXT")
        if "env" not in ws_cols:
            await db.execute("ALTER TABLE workspaces ADD COLUMN env TEXT")
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
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS user_groups (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                source TEXT NOT NULL DEFAULT 'manual',
                PRIMARY KEY (user_id, group_id),
                -- (B) the system agent must never be a group member
                -- (role grants, group-member adds, OIDC group sync).
                CHECK (user_id != '{AGENT_USER_ID}')
            )
        """)  # noqa: S608
        await db.execute(f"""
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
                UNIQUE(resource, position),
                -- (A) the system agent must never hold a user-principal ACE
                -- (covers both writers: add_acl_entry and replace_acl_entries).
                CHECK (NOT (principal_type = {PRINCIPAL_USER}
                            AND user_id = '{AGENT_USER_ID}'))
            )
        """)  # noqa: S608
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS instance_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Migration: drop legacy role and workspace_access tables
        for table in ("user_roles", "roles", "workspace_access"):
            await db.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
        await db.commit()
    finally:
        await db.close()
