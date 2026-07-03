"""Login-attempt tracking for brute-force protection.

Storage only; the sliding-window *decision* lives in ``auth.py``.
"""

from datetime import datetime, timezone

from .db import fetchone, transaction


async def record_failed_login(email: str, *, reset: bool = False) -> None:
    """Record a failed login attempt for an email.

    Storage only; the sliding-window *decision* lives in ``auth.py``.
    With ``reset=False`` the existing attempt count is incremented (or a
    new row is inserted with count 1).  With ``reset=True`` the count is
    reset to 1, ``first_attempt_at`` moved to now, and any stale
    ``locked_until`` cleared — used when the prior failure fell outside
    ``LOGIN_LOCKOUT_WINDOW`` so old failures stop counting.
    """
    async with transaction() as db:
        now_iso = datetime.now(timezone.utc).isoformat()
        if reset:
            # Upsert a fresh count, clearing any stale lockout.  INSERT ...
            # ON CONFLICT DO UPDATE keeps this a single statement.
            await db.execute(
                """INSERT INTO login_attempts
                   (email, attempt_count, first_attempt_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(email) DO UPDATE SET
                   attempt_count = 1,
                   first_attempt_at = excluded.first_attempt_at,
                   locked_until = NULL""",
                (email, now_iso),
            )
        else:
            await db.execute(
                """INSERT INTO login_attempts (email, attempt_count, first_attempt_at)
                   VALUES (?, 1, ?) ON CONFLICT(email) DO UPDATE SET
                   attempt_count = attempt_count + 1""",
                (email, now_iso),
            )


async def get_login_attempt_info(
    email: str,
) -> dict[str, int | str | None] | None:
    """Return login attempt info for an email, or None if no attempts tracked."""
    row = await fetchone(
        "SELECT attempt_count, first_attempt_at, locked_until"
        " FROM login_attempts WHERE email = ?",
        (email,),
    )
    if row is None:
        return None
    return {
        "attempt_count": row["attempt_count"],
        "first_attempt_at": row["first_attempt_at"],
        "locked_until": row["locked_until"],
    }


async def set_login_lockout(email: str, locked_until: str) -> None:
    """Set the lockout time for an email after too many failed attempts."""
    async with transaction() as db:
        await db.execute(
            "UPDATE login_attempts SET locked_until = ? WHERE email = ?",
            (locked_until, email),
        )


async def clear_login_attempts(email: str) -> None:
    """Clear all login attempts for an email (on successful login)."""
    async with transaction() as db:
        await db.execute(
            "DELETE FROM login_attempts WHERE email = ?", (email,)
        )
