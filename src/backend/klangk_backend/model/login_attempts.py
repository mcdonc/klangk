"""Login-attempt tracking for brute-force protection.

Storage only; the sliding-window *decision* lives in ``auth.py``.
:class:`LoginAttemptsModel` is the ``app_state``-owned form reached via
``app_state.model.login_attempts`` (#1563 / #1572). The module-level free
functions are the pre-existing ``_current_db`` ContextVar delegates, kept
as the backstop until #1578 dissolves the ContextVar.
"""

from datetime import datetime, timezone


class LoginAttemptsModel:
    """Login-attempt storage, resolved through ``app_state.db``.

    Reached via ``app_state.model.login_attempts``. Reaches the DB through
    ``self.app_state.db`` (the single DB instance for the whole app).
    """

    def __init__(self, app_state):
        self.app_state = app_state

    def reconfigure(self, app_state) -> None:
        self.app_state = app_state

    async def record_failed_login(
        self, email: str, *, reset: bool = False
    ) -> None:
        """Record a failed login attempt for an email.

        Storage only; the sliding-window *decision* lives in ``auth.py``.
        """
        async with self.app_state.db.transaction() as db:
            now_iso = datetime.now(timezone.utc).isoformat()
            if reset:
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
        self, email: str
    ) -> dict[str, int | str | None] | None:
        """Return login attempt info for an email, or None if no attempts tracked."""
        row = await self.app_state.db.fetchone(
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

    async def set_login_lockout(self, email: str, locked_until: str) -> None:
        """Set the lockout time for an email after too many failed attempts."""
        async with self.app_state.db.transaction() as db:
            await db.execute(
                "UPDATE login_attempts SET locked_until = ? WHERE email = ?",
                (locked_until, email),
            )

    async def clear_login_attempts(self, email: str) -> None:
        """Clear all login attempts for an email (on successful login)."""
        async with self.app_state.db.transaction() as db:
            await db.execute(
                "DELETE FROM login_attempts WHERE email = ?", (email,)
            )
