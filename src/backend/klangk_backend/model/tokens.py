"""JWT token blocklist (revocation + refreshed-token handoff).

:class:`TokensModel` is the ``app_state``-owned form reached via
``app_state.model.tokens`` (#1563 / #1572). The module-level free functions
are the pre-existing ``_current_db`` ContextVar delegates, kept as the
backstop until #1578 dissolves the ContextVar — not-yet-converted callers
and tests keep working unchanged.
"""


class TokensModel:
    """Token-blocklist operations, resolved through ``app_state.db``.

    Constructed by :class:`~klangk_backend.model.model.Model` and reached
    via ``app_state.model.tokens``. Reaches the DB through
    ``self.app_state.db`` (the single DB instance for the whole app).
    """

    def __init__(self, app_state):
        self.app_state = app_state

    async def blocklist_token(
        self, jti: str, expires_at: str, new_token: str | None = None
    ) -> None:
        async with self.app_state.db.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO token_blocklist"
                " (jti, expires_at, new_token) VALUES (?, ?, ?)",
                (jti, expires_at, new_token),
            )

    async def is_token_blocklisted(self, jti: str) -> bool:
        row = await self.app_state.db.fetchone(
            "SELECT 1 FROM token_blocklist WHERE jti = ?",
            (jti,),
        )
        return row is not None

    async def get_refreshed_token(self, jti: str) -> str | None:
        """Return the replacement token for a refreshed JTI.

        The returned token is a full JWT whose own ``exp`` claim governs
        its validity — no additional expiry check is needed here.
        """
        row = await self.app_state.db.fetchone(
            "SELECT new_token FROM token_blocklist"
            " WHERE jti = ? AND new_token IS NOT NULL",
            (jti,),
        )
        return row[0] if row else None
