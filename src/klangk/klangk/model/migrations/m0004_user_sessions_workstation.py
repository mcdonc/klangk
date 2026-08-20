"""Migration 0004: workstation identity on user_sessions (#2586).

Adds ``source_ip`` and ``user_agent`` columns so each active session
records which workstation it was established from. That turns the
session registry (#2585) into an auditable trail: concurrent sessions
from different workstations can be detected at login time (an audit
record is logged by ``Auth._audit_concurrent_logons``) and queried
later via the admin sessions endpoint. Both columns are nullable —
rows written before this migration (and issuances where the effective
client IP could not be resolved) have an unknown workstation, which is
never reported as "different".
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE user_sessions ADD COLUMN source_ip TEXT")
    await db.execute("ALTER TABLE user_sessions ADD COLUMN user_agent TEXT")


migration = Migration(4, "0004_user_sessions_workstation", apply)
