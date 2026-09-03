"""Migration 0008: the agent user is 'klangk' (#2718).

Renames any existing agent row to the fixed identity
(``klangk`` / ``klangk@example.com``) and relocates a human user who
already holds the ``klangk`` handle (it was never reserved before, so
a deployment may legitimately have one) to a unique alternative.

The seed path (``UsersModel.ensure_agent_user``, sequenced by
``Lifecycle.seed_agent_user``) reconciles the agent row itself on every
boot, so this migration's real job is the *human
collision*: it must run before the seed's pre-check, or startup fails
with "handle 'klangk' is already used by another user".

Volumes are untouched (#2717): old per-user ``.users/{AGENT_USER_ID}``
agent dirs are abandoned, not migrated.
"""

from klangk.model.migrations.base import Migration
from klangk.model.users import (
    AGENT_USER_ID,
    unique_handle,
)


async def apply(db) -> None:
    # A human holding 'klangk' gets bumped to a unique alternative
    # (klangk-2, klangk-3, ...). Runs before the agent-row rewrite so
    # the UNIQUE constraint on handle never trips.
    cursor = await db.execute(
        "SELECT id FROM users WHERE handle = ? AND id != ?",
        ("klangk", AGENT_USER_ID),
    )
    row = await cursor.fetchone()
    if row is not None:
        human_id = row[0]
        replacement = await unique_handle(db, "klangk")
        await db.execute(
            "UPDATE users SET handle = ? WHERE id = ?",
            (replacement, human_id),
        )

    # The agent row itself: fixed identity, idempotent upsert.
    await db.execute(
        "UPDATE users SET handle = ?, email = ? WHERE id = ?",
        ("klangk", "klangk@example.com", AGENT_USER_ID),
    )


migration = Migration(8, "0008_agent_user_klangk", apply)
