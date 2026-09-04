"""Migration 0033: per-session last-seen for the idle session timeout (#3151).

Adds ``last_seen_at`` to ``user_sessions`` — the session-level activity
clock the refresh seam checks against ``KLANGKD_SESSION_IDLE_TIMEOUT_MINUTES``
(STIG V-222389/390). Unlike ``users.last_activity_at`` (per-user, feeds the
days-scale dormant-account sweep), this is per-JTI and minutes-scale:
stamped (throttled) by authenticated HTTP requests and WebSocket frames,
never by the refresh endpoint itself (a refresh is the enforcement seam,
not activity — an idle client that only refreshes must still terminate).

Existing rows are backfilled from ``created_at`` so arming the feature on
a deployed database judges pre-arm sessions by their age-since-issuance
rather than crashing the parse. New rows stamp at issuance
(``SessionsModel.record_session``) and on every throttled touch
(``SessionsModel.touch_session``); a refresh deliberately carries the old
value forward (the same session continuing, minus refresh-is-activity).
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE user_sessions ADD COLUMN last_seen_at TEXT")
    await db.execute(
        "UPDATE user_sessions SET last_seen_at = created_at"
        " WHERE last_seen_at IS NULL"
    )


migration = Migration(
    id=30,
    name="0033_user_sessions_last_seen",
    apply=apply,
)
