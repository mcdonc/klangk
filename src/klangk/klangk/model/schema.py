"""Database schema creation and in-place migrations (``init_db``)."""

from .acl import PRINCIPAL_USER
from .egress_consent import DECISIONS, DURATIONS
from .migrations import run_migrations
from .users import AGENT_USER_ID, backfill_handles

# The duration + decision CHECK value lists are generated from the single
# sources of truth (DURATIONS / DECISIONS) so the DB constraints cannot drift
# from the Python enums (#2338, #2339).
_DURATION_CHECK_VALUES = ", ".join(f"'{d}'" for d in sorted(DURATIONS))
_DECISION_CHECK_VALUES = ", ".join(f"'{d}'" for d in sorted(DECISIONS))


async def init_users_table(db) -> None:
    """users table: baseline CREATE, the pre-migrations-era table
    rebuild (nullable password_hash / provider / handle columns),
    the handle backfill, and the agent-user guard triggers (E)/(F).
    """
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


async def init_workspaces_table(db) -> None:
    """workspaces table: baseline CREATE plus every ADD-COLUMN /
    RENAME-COLUMN migration for databases created before the
    column shipped.
    """
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
            -- comma-joined host[:port] specs; NULL = unrestricted
            -- egress (#1365)
            allowed_domains TEXT,
            -- comma-joined host[:port] specs; NULL = no static
            -- deny-list (#2367). A rejected name is NXDOMAIN'd
            -- unconditionally (no resolution, no SYN, no prompt).
            rejected_domains TEXT,
            -- egress filtering mode: 'static' (immutable allow-list
            -- at create time, the default) or 'interactive'
            -- (prompt on first connection to unknown host, #2239).
            egress_mode TEXT NOT NULL DEFAULT 'static',
            -- JSON dict of per-workspace behavioral overrides
            -- (idle_timeout, bridge_timeout, cpu_limit,
            -- memory_limit, pids_limit, ...). NULL = no overrides;
            -- missing keys fall back to the deploy-wide default
            -- (#864). Structural fields (image/mounts/env/...
            -- and the behavioral allowed_domains) stay as their own
            -- columns.
            settings TEXT,
            -- #2332: epoch second until which interactive consent
            -- prompting is paused workspace-wide (NULL = not paused).
            -- While now < this, a destination with no allow-list rule
            -- and no in-effect recorded verdict is auto-allowed instead
            -- of held for a decider; a recorded deny still blocks.
            consent_paused_until REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, name),
            -- (C) the system agent must never own a workspace.
            CHECK (user_id != '{AGENT_USER_ID}')
        )
    """)  # noqa: S608
    # Migration: add auto_start column to existing workspaces tables
    cursor = await db.execute("PRAGMA table_info(workspaces)")
    ws_cols = {row[1] for row in await cursor.fetchall()}
    await migrate_workspaces_columns(db, ws_cols)


async def migrate_workspaces_columns(db, ws_cols: set) -> None:
    """Every workspaces-table column migration for databases created
    before the column shipped (ADD COLUMN, plus the legacy rename)."""

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
    adds = [
        # Migration: add auto_start column to existing workspaces tables
        (
            "auto_start",
            "ALTER TABLE workspaces"
            " ADD COLUMN auto_start INTEGER NOT NULL DEFAULT 0",
        ),
        # Migration: add setup_state column (#1033). Defaults to
        # 'complete' so existing workspaces (already set up in their
        # persisted volumes) keep firing their service command.
        (
            "setup_state",
            "ALTER TABLE workspaces"
            " ADD COLUMN setup_state TEXT NOT NULL DEFAULT 'complete'",
        ),
        # Migration: add health_check column (#1015). NULL by default
        # so existing workspaces keep the no-health-monitoring behavior.
        (
            "health_check",
            "ALTER TABLE workspaces ADD COLUMN health_check TEXT",
        ),
        # Migration: add mounts/env columns (#1264). These are in the
        # CREATE TABLE above but had no ADD COLUMN migration, so DBs
        # created before they shipped lacked them and errored on any
        # read/write of mounts/env. NULL by default (no mounts/overrides).
        ("mounts", "ALTER TABLE workspaces ADD COLUMN mounts TEXT"),
        ("env", "ALTER TABLE workspaces ADD COLUMN env TEXT"),
        # Migration: add allowed_domains column (#1365). NULL by default
        # so existing workspaces keep unrestricted outbound networking;
        # the filter is opt-in per workspace AND per deploy.
        (
            "allowed_domains",
            "ALTER TABLE workspaces ADD COLUMN allowed_domains TEXT",
        ),
        # Migration: add rejected_domains column (#2367). NULL by default
        # so existing workspaces keep no static deny-list; the operator opts
        # in per workspace.
        (
            "rejected_domains",
            "ALTER TABLE workspaces ADD COLUMN rejected_domains TEXT",
        ),
        # Migration: add settings column (#864). NULL by default so
        # existing workspaces keep inheriting every deploy-wide default
        # (no per-workspace overrides). One JSON bag holds every
        # behavioral override (idle_timeout, bridge_timeout, cpu_limit,
        # memory_limit, pids_limit, ...) so future settings need no new
        # column/migration. Structural fields stay as dedicated columns.
        ("settings", "ALTER TABLE workspaces ADD COLUMN settings TEXT"),
        # Migration: add egress_mode column (#2239). 'static' = immutable
        # allow-list at create time (today's behavior); 'interactive' =
        # prompt on first connection to unknown host.
        (
            "egress_mode",
            "ALTER TABLE workspaces"
            " ADD COLUMN egress_mode TEXT NOT NULL DEFAULT 'static'",
        ),
        # Migration: add consent_paused_until column (#2332). NULL by default
        # so existing workspaces keep prompting normally; the pause is opt-in
        # via the decider TUI control.
        (
            "consent_paused_until",
            "ALTER TABLE workspaces ADD COLUMN consent_paused_until REAL",
        ),
    ]
    for column, ddl in adds:
        if column not in ws_cols:
            await db.execute(ddl)


async def init_core_tables(db) -> None:
    """Baseline CREATEs for port_allocations, groups, user_groups,
    acl_entries, token_blocklist (+new_token migration),
    login_attempts, and invitations.
    """
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


async def init_egress_consent_table(db) -> None:
    """egress_consent table (#2239): baseline CREATE, the rebuild
    that attaches the revoked decision + audit columns (#2339),
    and the dedup/partial indexes.
    """
    # Interactive egress consent (#2239). Tracks blocked outbound
    # connections that need human approval. CHECK constraints enforce
    # the decision state machine at the storage layer — the same
    # DB-backstop philosophy as the agent-user triggers above.
    await db.execute(f"""
        CREATE TABLE IF NOT EXISTS egress_consent (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            dest_host TEXT NOT NULL,
            dest_port INTEGER,
            pid INTEGER,
            process_name TEXT,
            decision TEXT NOT NULL DEFAULT 'pending'
                CHECK (decision IN ({_DECISION_CHECK_VALUES})),
            duration TEXT
                CHECK (duration IS NULL OR duration IN
                    ({_DURATION_CHECK_VALUES})),
            requested_at REAL NOT NULL,
            decided_at REAL,
            decided_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            revoked_at REAL,
            revoked_by TEXT REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    # egress_consent: add the `revoked` decision (#2339) + the
    # `revoked_at`/`revoked_by` audit columns (and attach the CHECKs). The
    # table may already exist from #2338 (has the duration CHECK but not
    # `revoked`); rebuild it either way so the shape is universal. Detected
    # via sqlite_master (PRAGMA table_info doesn't expose CHECK/columns).
    # Data is copied across (mirrors the users rebuild above); the partial
    # unique indexes are recreated by the CREATE INDEX statements below.
    ec_sql_cur = await db.execute(
        "SELECT sql FROM sqlite_master"
        " WHERE type='table' AND name='egress_consent'"
    )
    ec_sql_row = await ec_sql_cur.fetchone()
    if ec_sql_row and "revoked_at" not in ec_sql_row[0]:
        await db.execute(f"""
            CREATE TABLE egress_consent_new (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                dest_host TEXT NOT NULL,
                dest_port INTEGER,
                pid INTEGER,
                process_name TEXT,
                decision TEXT NOT NULL DEFAULT 'pending'
                    CHECK (decision IN ({_DECISION_CHECK_VALUES})),
                duration TEXT
                    CHECK (duration IS NULL OR duration IN
                        ({_DURATION_CHECK_VALUES})),
                requested_at REAL NOT NULL,
                decided_at REAL,
                decided_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                revoked_at REAL,
                revoked_by TEXT REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        ec_info = await db.execute("PRAGMA table_info(egress_consent)")
        ec_old_cols = {r[1] for r in await ec_info.fetchall()}
        ec_shared = [
            c
            for c in (
                "id",
                "workspace_id",
                "dest_host",
                "dest_port",
                "pid",
                "process_name",
                "decision",
                "duration",
                "requested_at",
                "decided_at",
                "decided_by",
                "revoked_at",
                "revoked_by",
            )
            if c in ec_old_cols
        ]
        ec_cols_str = ", ".join(ec_shared)
        await db.execute(
            f"INSERT INTO egress_consent_new ({ec_cols_str})"  # noqa: S608
            f" SELECT {ec_cols_str} FROM egress_consent"
        )
        await db.execute("DROP TABLE egress_consent")
        await db.execute(
            "ALTER TABLE egress_consent_new RENAME TO egress_consent"
        )
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_egress_consent_workspace
        ON egress_consent(workspace_id, decision)
    """)
    # At most one pending request per (workspace, host, port). The
    # partial index makes INSERT OR IGNORE the atomic dedup path,
    # eliminating the TOCTOU between has_pending() and create_request().
    # COALESCE maps NULL port to -1 because SQLite treats NULLs as
    # distinct in unique indexes.
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_consent_pending_dedup
        ON egress_consent(workspace_id, dest_host, COALESCE(dest_port, -1))
        WHERE decision = 'pending'
    """)
    # At most one static-mode denial per (workspace, host, port). Static
    # mode denies by policy with no human (decided_by NULL); dedup so a
    # flooding workspace can't spam denial rows (#2242).
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_consent_static_dedup
        ON egress_consent(workspace_id, dest_host, COALESCE(dest_port, -1))
        WHERE decision = 'denied' AND decided_by IS NULL
    """)
    # At most one allow-mode allow per (workspace, host, port). Allow mode
    # (#2406) permits off-list egress by policy with no human (decided_by
    # NULL) + records it; dedup so a flooding workspace can't spam allow
    # rows (mirrors the static-denial dedup above).
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_consent_static_allow_dedup
        ON egress_consent(workspace_id, dest_host, COALESCE(dest_port, -1))
        WHERE decision = 'allowed' AND decided_by IS NULL
    """)


async def init_db(db) -> None:
    """Create/migrate the schema on the given connection.

    ``db`` is a raw connection (``await app_state.db.get_db()``) acquired by
    the caller — typically :meth:`Model.init_db`, which pulls it from the
    single owned ``app_state.db``. ``init_db`` owns the commit and closes
    the connection. Requiring the connection (rather than reaching for an
    ambient one) is the #1551 fix: the old env-only lazy
    ``DB(KlangkSettings(os.environ))`` fallback built a different DB than
    the server, which is the divergence #1551 describes. With it gone, every
    path reaches the one ``app.state.db`` (#1578).
    """
    try:
        await init_users_table(db)
        await init_workspaces_table(db)
        await init_core_tables(db)
        await init_egress_consent_table(db)
        # Migration: drop legacy role and workspace_access tables
        for table in ("user_roles", "roles", "workspace_access"):
            await db.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
        # Ordered, once-only migrations (#30). The CREATE TABLE pile above
        # is the historical baseline; every schema change from here on is a
        # numbered migration in klangk.model.migrations package instead.
        await run_migrations(db)
        # Final commit — NOT owned by the runner: the baseline blocks above
        # (users rebuild, egress_consent rebuild, backfill_handles) issue
        # DML (INSERT ... SELECT / UPDATE) that opens an implicit sqlite3
        # transaction; later DDL in the same block rides inside it. Without
        # this commit, db.close() rolls the whole rebuild back (silently).
        await db.commit()
    finally:
        await db.close()
