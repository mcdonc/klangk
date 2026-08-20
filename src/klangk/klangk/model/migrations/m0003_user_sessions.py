"""Migration 0003: user_sessions for concurrent-session limiting (#2585).

One row per active (unexpired, unblocklisted) access-token JTI. The table
is the server-side session registry the stateless-JWT design lacked: it
counts a user's concurrent sessions and identifies the oldest ones to
revoke when a login exceeds ``KLANGKD_MAX_SESSIONS_PER_USER``. Rows are
inserted at token issuance (``Auth.issue_token``), replaced on refresh,
deleted on logout, and lazily purged once their token's ``exp`` has
passed — nothing tracks pre-migration tokens, which stay valid until
they expire naturally.

``ON DELETE CASCADE`` keeps sessions from outliving their user.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            jti TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
    """)  # noqa: S608
    # Oldest-first scan per user is the eviction path
    # (``Auth._enforce_session_limit``); index it.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_sessions_user
        ON user_sessions(user_id, created_at, jti)
    """)


migration = Migration(3, "0003_user_sessions", apply)
