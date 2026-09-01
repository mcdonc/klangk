"""Active-session registry for concurrent-session limiting (#2585) and
concurrent-logon auditing (#2586).

Storage only; the limit *decision* (who to revoke, blocklisting the
evicted JTIs) lives in :mod:`klangk.auth`. A row exists per issued
access-token JTI; it is inserted at issuance, replaced on refresh (the
old JTI is blocklisted), deleted on logout, and lazily purged once the
token's ``exp`` has passed — so ``user_sessions`` never tracks a token
that is already dead. Each row also records the workstation the
session was established from (effective client IP + user agent), so
concurrent sessions from different workstations can be detected at
login and audited later.
"""

from datetime import datetime, timezone

from .base import Submodel


class SessionsModel(Submodel):
    """Active-session storage, resolved through ``app_state.db``.

    Constructed by :class:`~klangk.model.model.Model` and reached via
    ``app_state.model.sessions``. Reaches the DB through
    ``self.app.state.db`` (the single DB instance for the whole app).
    """

    async def record_session(
        self,
        user_id: str,
        jti: str,
        expires_at: str,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Insert a session row for a freshly issued token.

        ``expires_at`` is the token's ``exp`` as a UTC ISO string (the
        same form the token blocklist stores), used by
        :meth:`purge_expired` and handed back for blocklisting on
        eviction. ``source_ip``/``user_agent`` record the workstation
        the session was established from (#2586); ``None`` means
        unknown (never reported as a *different* workstation).
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT OR REPLACE INTO user_sessions"
                " (jti, user_id, expires_at, source_ip, user_agent)"
                " VALUES (?, ?, ?, ?, ?)",
                (jti, user_id, expires_at, source_ip, user_agent),
            )

    async def replace_session(
        self,
        old_jti: str,
        user_id: str,
        new_jti: str,
        expires_at: str,
    ) -> None:
        """Swap a refreshed token's JTI for its replacement, atomically.

        A refresh is the *same* session continuing under a new token, so
        the row is UPDATEd in place — the new JTI inherits the row (slot,
        and its original ``created_at``, so a session that keeps
        refreshing stays the "oldest" for eviction) and the user's
        session count does not grow on refresh. When *old_jti* has no
        row (a token issued before #2585 landed), the new JTI is simply
        inserted.
        """
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE user_sessions SET jti = ?, expires_at = ?"
                " WHERE jti = ?",
                (new_jti, expires_at, old_jti),
            )
            if cursor.rowcount == 0:
                await db.execute(
                    "INSERT OR REPLACE INTO user_sessions"
                    " (jti, user_id, expires_at) VALUES (?, ?, ?)",
                    (new_jti, user_id, expires_at),
                )

    async def remove_session(self, jti: str) -> None:
        """Delete the session row for a logged-out (blocklisted) JTI."""
        await self.remove_sessions([jti])

    async def remove_sessions(self, jtis: list[str]) -> None:
        """Delete the session rows for revoked (blocklisted) JTIs.

        Used by the session-limit eviction path after the victims have
        been blocklisted. A JTI with no row (a pre-#2585 token) is a
        no-op.
        """
        if not jtis:
            return
        placeholders = ", ".join("?" for _ in jtis)
        async with self.app.state.db.transaction() as db:
            await db.execute(
                f"DELETE FROM user_sessions WHERE jti IN ({placeholders})",  # noqa: S608
                tuple(jtis),
            )

    async def purge_expired(self) -> int:
        """Delete every session row whose token has expired.

        Returns the number of rows deleted. Called on every issuance (by
        :meth:`klangk.auth.Auth.issue_token`) so dead sessions stop
        counting toward the limit and the table stays bounded without a
        background sweeper. ISO-UTC strings compare lexicographically.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM user_sessions WHERE expires_at <= ?",
                (now_iso,),
            )
            return cursor.rowcount

    async def list_sessions(self, user_id: str) -> list[dict]:
        """Return a user's session rows, oldest first.

        ``rowid`` breaks ``created_at`` ties (datetime('now') has
        second granularity; burst logins would otherwise tie) with the
        later insert ordering as the newer session. Each row carries
        the workstation identity (``source_ip``/``user_agent``) for
        concurrent-logon auditing (#2586).
        """
        rows = await self.app.state.db.fetchall(
            "SELECT jti, expires_at, source_ip, user_agent, created_at"
            " FROM user_sessions WHERE user_id = ?"
            " ORDER BY created_at, rowid",
            (user_id,),
        )
        return [
            {
                "jti": row[0],
                "expires_at": row[1],
                "source_ip": row[2],
                "user_agent": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]
