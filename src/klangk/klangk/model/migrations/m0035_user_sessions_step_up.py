"""Migration 0035: per-session step-up timestamp (#3196).

Adds ``user_sessions.stepped_up_at`` — when the session's owner last
confirmed their password at ``POST /auth/step-up`` (sudo-mode
reauthentication). Privileged admin writes gated by
``klangk.stepup`` compare this stamp against
``KLANGKD_STEP_UP_WINDOW_MINUTES``; outside the window the write is
refused with a machine-readable 403 until the password is confirmed
again.

NULL (the migration default, and the value every fresh row starts
with) means "never confirmed" — no backfill is needed or correct:
arming the feature on a deployed database must not retroactively
credit existing sessions with a confirmation they never made. The
stamp is per-session (per row), not per-user: a confirmation on one
workstation's session must not unlock another session of the same
user. ``replace_session`` carries the column across the per-refresh
JTI rekeying (the UPDATE keeps untouched columns), so a refresh does
not silently demote a stepped-up session; logout/revocation deletes
the row, ending the elevated state with the session.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE user_sessions ADD COLUMN stepped_up_at TEXT")


migration = Migration(
    id=35,
    name="0035_user_sessions_step_up",
    apply=apply,
)
