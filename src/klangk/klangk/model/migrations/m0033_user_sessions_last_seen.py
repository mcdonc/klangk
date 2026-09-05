"""Migration 0033: per-session last-seen for the idle session timeout (#3151).

Adds two columns to ``user_sessions``:

- ``last_seen_at`` — the session-level activity clock the refresh seam
  checks against ``KLANGKD_SESSION_IDLE_TIMEOUT_MINUTES``. Unlike
  ``users.last_activity_at`` (per-user, feeds the
  days-scale dormant-account sweep), this is per-session and
  minutes-scale: stamped (throttled) by authenticated HTTP requests and
  WebSocket frames, never by the refresh endpoint itself (a refresh is
  the enforcement seam, not activity — an idle client that only
  refreshes must still terminate).
- ``session_id`` — a **stable** identity for the session row. The row's
  primary key (``jti``) is rekeyed on every token refresh
  (``replace_session``), so a WebSocket that pinned its connect-time JTI
  would stamp a row that no longer exists — its activity would silently
  vanish and an actively-used terminal session would idle out. Frames
  stamp by ``session_id``, which survives the rotation.

Existing rows are backfilled: ``last_seen_at`` from ``created_at`` (so
arming the feature on a deployed database judges pre-arm sessions by
their age-since-issuance) and ``session_id`` from ``jti`` (a stable
value per row; later refreshes carry it forward through the UPDATE).
New rows mint a fresh UUID at issuance (``SessionsModel.record_session``).
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE user_sessions ADD COLUMN last_seen_at TEXT")
    await db.execute("ALTER TABLE user_sessions ADD COLUMN session_id TEXT")
    await db.execute(
        "UPDATE user_sessions SET last_seen_at = created_at"
        " WHERE last_seen_at IS NULL"
    )
    await db.execute(
        "UPDATE user_sessions SET session_id = jti WHERE session_id IS NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_session"
        " ON user_sessions(session_id)"
    )


migration = Migration(
    id=30,
    name="0033_user_sessions_last_seen",
    apply=apply,
)
