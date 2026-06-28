"""User invitations (beta/access-request lifecycle)."""

import uuid
from datetime import datetime, timezone

from ._core import _fetchone, transaction


async def create_invitation(email: str, invited_by: str) -> dict:
    """Create a new invitation. Returns the invitation dict."""
    async with transaction() as db:
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


async def get_invitation(invitation_id: str) -> dict | None:
    """Get an invitation by ID."""
    row = await _fetchone(
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


async def get_pending_invitation_by_email(email: str) -> dict | None:
    """Get a pending invitation for the given email."""
    row = await _fetchone(
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


# Whitelisted sort columns for the admin invitations list. Values are the
# SQL expressions to ORDER BY; the ``invited_by`` key sorts by the inviter's
# email (the joined ``users.email``), which is the value the UI displays.
_ADMIN_INVITATION_SORT_COLUMNS = {
    "email": "i.email",
    "invited_by": "u.email",
    "created": "i.created_at",
}


async def list_invitations(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
) -> dict:
    """List invitations with server-side pagination, sorting, and filtering.

    Returns a paged envelope ``{"invitations", "page", "page_size",
    "total", "pending_count"}`` suitable for forwards/backwards paging.
    ``pending_count`` is the total number of pending invitations across the
    whole table (ignoring the ``q`` filter and pagination) so the admin UI
    can keep its "pending invitations" badge accurate on every page.

    Sort keys: ``email`` (invitee email), ``invited_by`` (inviter's email),
    ``created`` (creation time). An unknown ``sort`` is whitelisted against
    a static map and falls back to ``i.created_at`` (no string
    interpolation of user input). The ``q`` filter is a substring match on
    the invitee email. A trailing ``i.id`` tiebreaker keeps offset
    pagination deterministic when rows share the sort key.
    """
    sort_col = _ADMIN_INVITATION_SORT_COLUMNS.get(sort, "i.created_at")
    direction = "DESC" if order.lower() == "desc" else "ASC"
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    async with transaction() as db:
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

        # Global pending count for the UI badge — independent of the q
        # filter and the current page.
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


async def mark_invitation_accepted(invitation_id: str) -> bool:
    """Mark an invitation as accepted. Returns True if updated."""
    async with transaction() as db:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "UPDATE invitations SET status = 'accepted', accepted_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (now, invitation_id),
        )
        return cursor.rowcount > 0


async def revoke_invitation(invitation_id: str) -> bool:
    """Revoke a pending invitation. Returns True if updated."""
    async with transaction() as db:
        cursor = await db.execute(
            "UPDATE invitations SET status = 'revoked'"
            " WHERE id = ? AND status = 'pending'",
            (invitation_id,),
        )
        return cursor.rowcount > 0
