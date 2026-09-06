"""Active-session registry for concurrent-session limiting (#2585),
concurrent-logon auditing (#2586), and the idle session timeout
(#3151).

Storage only; the limit *decision* (who to revoke, blocklisting the
evicted JTIs) lives in :mod:`klangk.auth`. A row exists per issued
access-token JTI; it is inserted at issuance, replaced on refresh (the
old JTI is blocklisted), deleted on logout, and lazily purged once the
token's ``exp`` has passed — so ``user_sessions`` never tracks a token
that is already dead. Each row also records the workstation the
session was established from (effective client IP + user agent), so
concurrent sessions from different workstations can be detected at
login and audited later, plus the idle-timeout clock (#3151):
``last_seen_at`` (stamped by real traffic, never by refresh) and
``session_id`` — a stable identity that survives the per-refresh JTI
rekeying, so a WebSocket pinned at connect time keeps stamping the
live row across token rotations — plus ``stepped_up_at`` (#3196):
when the session's owner last confirmed their password for sudo-mode
(step-up) reauthentication. NULL = never confirmed; the stamp is per
session and dies with the row (logout/revocation), and
``replace_session`` carries it across the refresh rekeying.
"""

import uuid
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
        ``last_seen_at`` starts at issuance (#3151) — a fresh login is
        activity by definition — and the row gets a fresh ``session_id``
        (stable across the per-refresh JTI rekeying, so WebSocket frames
        can keep stamping the live row).
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT OR REPLACE INTO user_sessions"
                " (jti, user_id, expires_at, source_ip, user_agent,"
                " last_seen_at, session_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    jti,
                    user_id,
                    expires_at,
                    source_ip,
                    user_agent,
                    datetime.now(timezone.utc).isoformat(),
                    str(uuid.uuid4()),
                ),
            )

    async def get_session_id(self, jti: str | None) -> str | None:
        """The stable ``session_id`` of the row currently keyed by *jti*
        (#3151), or ``None`` when no such row exists — including a
        ``None`` *jti* (a pre-#2585 token with no JTI binds no row;
        callers treat that as fail-open).

        Resolved once per WebSocket connect and pinned on the
        Connection; because ``session_id`` survives the refresh rekey,
        later frame stamps keep reaching the row after the connect-time
        JTI is rotated away.
        """
        row = await self.app.state.db.fetchone(
            "SELECT session_id FROM user_sessions WHERE jti = ?",
            (jti,),
        )
        return row[0] if row is not None else None

    async def touch_session_by_sid(self, session_id: str) -> None:
        """Stamp the session's ``last_seen_at`` to now, by stable id
        (#3151) — the WebSocket frame path.

        Keyed by ``session_id`` rather than ``jti`` so a long-lived
        socket keeps stamping the live row across token rotations. An
        unknown id (row deleted by logout/revocation) is a silent
        no-op, same as the jti form.
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE user_sessions SET last_seen_at = ?"
                " WHERE session_id = ?",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )

    async def touch_session(self, jti: str) -> None:
        """Stamp the session's ``last_seen_at`` to now (#3151).

        Called (throttled — see ``Auth.record_session_activity``) from
        the authenticated choke points: HTTP requests carrying the
        token and inbound WebSocket frames. Deliberately NOT called by
        the refresh endpoint: a refresh is the enforcement seam, not
        activity, or an idle client that only refreshes would never
        time out. A JTI with no row (pre-#2585 token) is a no-op.
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE jti = ?",
                (datetime.now(timezone.utc).isoformat(), jti),
            )

    async def get_workstation(
        self, jti: str
    ) -> tuple[str | None, str | None] | None:
        """The ``(source_ip, user_agent)`` recorded for the session row
        keyed by *jti*, or ``None`` when no row exists (#3194).

        The recorded workstation is what a session was *established*
        from (at issuance, #2586) and survives refresh rekeying (the
        row is UPDATEd in place), so the binding predicate compares
        every later presentation against it. ``None`` (no row — a
        pre-#2585 token) means the binding cannot be judged; the
        caller fails open, the same posture as ``get_last_seen``.
        """
        row = await self.app.state.db.fetchone(
            "SELECT source_ip, user_agent FROM user_sessions WHERE jti = ?",
            (jti,),
        )
        if row is None:
            return None
        return row[0], row[1]

    async def get_last_seen(self, jti: str) -> str | None:
        """The session row's ``last_seen_at`` (UTC ISO string), or
        ``None`` when no row exists (#3151).

        ``None`` means the idle window cannot be judged (a pre-#2585
        token with no session row); the caller fails open — same
        posture as every other session-row-tolerant path.
        """
        row = await self.app.state.db.fetchone(
            "SELECT last_seen_at FROM user_sessions WHERE jti = ?",
            (jti,),
        )
        return row[0] if row is not None else None

    async def get_stepped_up_at(self, jti: str) -> str | None:
        """The session row's ``stepped_up_at`` (UTC ISO string), or
        ``None`` when no row exists or the session never confirmed
        its password (#3196).

        ``None`` means the step-up gate cannot be satisfied — the
        caller fails closed (an unstamped session is simply not
        stepped up), unlike the idle-timeout reads which fail open.
        """
        row = await self.app.state.db.fetchone(
            "SELECT stepped_up_at FROM user_sessions WHERE jti = ?",
            (jti,),
        )
        return row[0] if row is not None else None

    async def stamp_step_up(self, jti: str) -> bool:
        """Record a password confirmation on the session row (#3196).

        Returns ``True`` when a row was stamped, ``False`` when *jti*
        has no session row (a token that somehow bypassed
        ``issue_token``/refresh row creation — nothing to stamp, and
        the step-up check fails closed for that token anyway).
        """
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE user_sessions SET stepped_up_at = ? WHERE jti = ?",
                (datetime.now(timezone.utc).isoformat(), jti),
            )
            return cursor.rowcount > 0

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
        ``session_id``, and its original ``created_at``, so a session
        that keeps refreshing stays the "oldest" for eviction) and the
        user's session count does not grow on refresh.
        ``last_seen_at`` is deliberately carried over untouched (#3151):
        a refresh is not session activity. So is ``stepped_up_at``
        (#3196): a refresh is the same session continuing, not a new
        login — a confirmed step-up must survive the rekeying, or every
        refresh would demote the session back to unconfirmed. When
        *old_jti* has no row (a token issued before #2585 landed), the
        new JTI is simply inserted — with a fresh ``session_id`` and ``last_seen_at``
        (issuance is activity), never a permanently unjudgeable NULL.
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
                    " (jti, user_id, expires_at, last_seen_at, session_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        new_jti,
                        user_id,
                        expires_at,
                        datetime.now(timezone.utc).isoformat(),
                        str(uuid.uuid4()),
                    ),
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
            "SELECT jti, expires_at, source_ip, user_agent, created_at,"
            " last_seen_at"
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
                "last_seen_at": row[5],
            }
            for row in rows
        ]
