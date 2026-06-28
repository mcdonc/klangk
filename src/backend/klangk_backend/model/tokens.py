"""JWT token blocklist (revocation + refreshed-token handoff)."""

from ._core import _fetchone, transaction


async def blocklist_token(
    jti: str, expires_at: str, new_token: str | None = None
) -> None:
    async with transaction() as db:
        await db.execute(
            "INSERT OR IGNORE INTO token_blocklist"
            " (jti, expires_at, new_token) VALUES (?, ?, ?)",
            (jti, expires_at, new_token),
        )


async def is_token_blocklisted(jti: str) -> bool:
    row = await _fetchone(
        "SELECT 1 FROM token_blocklist WHERE jti = ?",
        (jti,),
    )
    return row is not None


async def get_refreshed_token(jti: str) -> str | None:
    """Return the replacement token for a refreshed JTI.

    The returned token is a full JWT whose own ``exp`` claim governs
    its validity — no additional expiry check is needed here.
    """
    row = await _fetchone(
        "SELECT new_token FROM token_blocklist"
        " WHERE jti = ? AND new_token IS NOT NULL",
        (jti,),
    )
    return row[0] if row else None
