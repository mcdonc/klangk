"""User invitations (beta/access-request lifecycle).

:class:`InvitationsModel` is the ``app_state``-owned form reached via
``app_state.model.invitations`` (#1563 / #1572). The module-level free
functions are the pre-existing ``_current_db`` ContextVar delegates, kept
as the backstop until #1578 dissolves the ContextVar.
``ADMIN_INVITATION_SORT_COLUMNS`` is a pure constant (module-level).
"""

import uuid
from datetime import datetime, timezone


# Whitelisted sort columns for the admin invitations list. Values are the
# SQL expressions to ORDER BY; the ``invited_by`` key sorts by the inviter's
# email (the joined ``users.email``), which is the value the UI displays.
ADMIN_INVITATION_SORT_COLUMNS = {
    "email": "i.email",
    "invited_by": "u.email",
    "created": "i.created_at",
}


class InvitationsModel:
    """Invitation lifecycle, resolved through ``app_state.db``.

    Reached via ``app_state.model.invitations``. Reaches the DB through
    ``self.app_state.db`` (the single DB instance for the whole app). The
    method bodies mirror the module-level free functions below (backstop);
    the duplication is temporary and removed in #1578 when the free
    functions are deleted.
    """

    def __init__(self, app_state):
        self.app_state = app_state

    def reconfigure(self, app_state) -> None:
        self.app_state = app_state

    async def create_invitation(self, email: str, invited_by: str) -> dict:
        """Create a new invitation. Returns the invitation dict."""
        async with self.app_state.db.transaction() as db:
            invitation_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO invitations (id, email, invited_by) VALUES (?, ?, ?)",
                (invitation_id, email, invited_by),
            )
            cursor = await db.execute(
                "SELECT created_at FROM invitations WHERE id = ?",
                (invitation_id,),
            )
            row = await cursor.fetchone()
            return {
                "id": invitation_id,
                "email": email,
                "invited_by": invited_by,
                "status": "pending",
                "created_at": row["created_at"],
            }

    async def get_invitation(self, invitation_id: str) -> dict | None:
        """Get an invitation by ID."""
        row = await self.app_state.db.fetchone(
            "SELECT id, email, invited_by, status, created_at, accepted_at"
            " FROM invitations WHERE id = ?",
            (invitation_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "invited_by": row["invited_by"],
            "status": row["status"],
            "created_at": row["created_at"],
            "accepted_at": row["accepted_at"],
        }

    async def get_pending_invitation_by_email(self, email: str) -> dict | None:
        """Get a pending invitation for the given email."""
        row = await self.app_state.db.fetchone(
            "SELECT id, email, invited_by, status, created_at, accepted_at"
            " FROM invitations WHERE email = ? AND status = 'pending'",
            (email,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "email": row["email"],
            "invited_by": row["invited_by"],
            "status": row["status"],
            "created_at": row["created_at"],
            "accepted_at": row["accepted_at"],
        }

    async def list_invitations(
        self,
        page: int = 1,
        page_size: int = 10,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        """List invitations with server-side pagination, sorting, and filtering."""
        sort_col = ADMIN_INVITATION_SORT_COLUMNS.get(sort, "i.created_at")
        direction = "DESC" if order.lower() == "desc" else "ASC"
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size

        async with self.app_state.db.transaction() as db:
            where_clause = ""
            params: list = []
            if q:
                where_clause = " WHERE i.email LIKE ?"
                params.append(f"%{q}%")

            count_cursor = await db.execute(
                f"SELECT COUNT(*) AS c FROM invitations i{where_clause}",
                params,
            )
            total = (await count_cursor.fetchone())["c"]

            pending_cursor = await db.execute(
                "SELECT COUNT(*) AS c FROM invitations WHERE status = 'pending'"
            )
            pending_count = (await pending_cursor.fetchone())["c"]

            cursor = await db.execute(
                "SELECT i.id, i.email, i.invited_by, i.status,"
                " i.created_at, i.accepted_at, u.email AS invited_by_email"
                " FROM invitations i"
                " JOIN users u ON i.invited_by = u.id"
                f"{where_clause}"
                f" ORDER BY {sort_col} {direction}, i.id"
                " LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            )
            invitations = [
                {
                    "id": row["id"],
                    "email": row["email"],
                    "invited_by": row["invited_by"],
                    "invited_by_email": row["invited_by_email"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "accepted_at": row["accepted_at"],
                }
                for row in await cursor.fetchall()
            ]
            return {
                "invitations": invitations,
                "page": page,
                "page_size": page_size,
                "total": total,
                "pending_count": pending_count,
            }

    async def mark_invitation_accepted(self, invitation_id: str) -> bool:
        """Mark an invitation as accepted. Returns True if updated."""
        async with self.app_state.db.transaction() as db:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cursor = await db.execute(
                "UPDATE invitations SET status = 'accepted', accepted_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (now, invitation_id),
            )
            return cursor.rowcount > 0

    async def revoke_invitation(self, invitation_id: str) -> bool:
        """Revoke a pending invitation. Returns True if updated."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE invitations SET status = 'revoked'"
                " WHERE id = ? AND status = 'pending'",
                (invitation_id,),
            )
            return cursor.rowcount > 0
