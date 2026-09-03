"""Migration 0028: one pending invitation per email (#3101).

The send-invitation route pre-checks ``get_pending_invitation_by_email``
and then inserts — not atomic, so two concurrent sends for the same
address both passed the pre-check and minted two pending invitations
(the route's 400 never fired). Fresh databases get the partial unique
index ``idx_invitations_pending_dedup`` from the baseline schema; this
migration backfills it onto deployed databases.

First it collapses any duplicate pending rows the race already created:
per email, the oldest pending invitation (by ``created_at``, id as
tiebreak) survives and the younger duplicates are flipped to
``revoked`` — the same terminal state an admin revoke produces, keeping
the audit trail while freeing the email's single pending slot. Accepted
and revoked history is untouched (the index covers only ``pending``).

Idempotent by construction: with at most one pending row per email the
UPDATE matches nothing, and the index uses IF NOT EXISTS.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        UPDATE invitations SET status = 'revoked'
        WHERE status = 'pending' AND id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY email
                    ORDER BY created_at, id
                ) AS rn
                FROM invitations
                WHERE status = 'pending'
            ) WHERE rn = 1
        )
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_invitations_pending_dedup"
        " ON invitations(email) WHERE status = 'pending'"
    )


migration = Migration(
    id=28,
    name="0028_invitations_pending_unique",
    apply=apply,
)
