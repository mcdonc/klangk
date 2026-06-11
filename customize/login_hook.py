"""OIDC login hook: require an existing account or invitation.

Login is allowed if the user already has an account, or if their email
has a 'pending' or 'accepted' invitation.  Revoked invitations do not count.

Usage:
    KLANGK_OIDC_LOGIN_HOOK=login_hook.require_invitation
"""

from klangk_backend.model import get_db


async def require_invitation(provider, claims, email, tokens):
    """Reject login unless the user has an account or an invitation.

    If the most recent invitation is revoked, login is blocked even if
    the user already has an account.  Re-inviting someone (creating a
    new pending invitation) overrides a previous revocation.
    """
    db = await get_db()
    try:
        # Check the most recent invitation
        cursor = await db.execute(
            "SELECT status FROM invitations WHERE email = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (email,),
        )
        row = await cursor.fetchone()

        if row is not None:
            if row["status"] == "revoked":
                raise PermissionError(f"Invitation for {email} has been revoked")
            # Most recent invitation is pending or accepted — allow
            return None

        # No invitation at all — allow if the user already has an account
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE email = ?",
            (email,),
        )
        if await cursor.fetchone():
            return None
    finally:
        await db.close()

    raise PermissionError(f"No account or invitation found for {email}")
