"""Chat messages and @handle mention resolution.

``ChatModel`` is the ``app_state``-owned form, reached via
``app_state.model.chat`` (#1563 / #1576). The module-level free functions
are the pre-existing ``_current_db`` ContextVar delegates, kept as the
backstop until #1578 dissolves the ContextVar. The message-type constants
(``MSG_USER`` / ``MSG_AGENT`` / ``MSG_SYSTEM``) and the ``MENTION_RE``
pattern stay module-level — they are imported as literal values by the
``wshandler`` package and ``api/chat.py``.
"""

import re
import uuid

from .db import Connection
from .acl import ACTION_ALLOW, PRINCIPAL_USER

# Chat message types
MSG_USER = 0
MSG_AGENT = 1
MSG_SYSTEM = 2

MENTION_RE = re.compile(r"@([a-zA-Z0-9._-]+)")


class ChatModel:
    """Chat data access, through ``app_state.db``.

    Reached via ``app_state.model.chat``. Reaches the DB through
    ``self.app_state.db`` (the single DB instance for the whole app). The
    method bodies mirror the module-level free functions below (backstop);
    the message-type constants and ``MENTION_RE`` stay module-level.
    """

    def __init__(self, app_state):
        self.app_state = app_state

    async def parse_mentions(
        self, db: Connection, message: str, workspace_id: str
    ) -> list[str]:
        """Extract @handle mentions from message text and resolve to user IDs.

        Returns a deduplicated list of user IDs for handles that belong to
        workspace members (including the owner). Takes the open connection
        so it can run inside the caller's transaction (``add_chat_message``
        resolves mentions and inserts them atomically with the message).
        """
        candidates = MENTION_RE.findall(message)
        if not candidates:
            return []
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for c in candidates:
            low = c.lower()
            if low not in seen:
                seen.add(low)
                unique.append(low)

        placeholders = ",".join("?" for _ in unique)
        cursor = await db.execute(
            "SELECT DISTINCT u.id FROM users u"
            " JOIN acl_entries ae ON ae.user_id = u.id"
            " WHERE LOWER(u.handle) IN (" + placeholders + ")"
            "   AND ae.resource = ?"
            "   AND ae.principal_type = ? AND ae.action = ?"
            " UNION"
            " SELECT w.user_id AS id"
            " FROM workspaces w JOIN users u2 ON u2.id = w.user_id"
            " WHERE w.id = ? AND LOWER(u2.handle) IN (" + placeholders + ")",
            (
                *unique,
                f"/workspaces/{workspace_id}",
                PRINCIPAL_USER,
                ACTION_ALLOW,
                workspace_id,
                *unique,
            ),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def add_chat_message(
        self,
        workspace_id: str,
        user_id: str,
        user_email: str,
        message: str,
        message_type: int = MSG_USER,
    ) -> dict:
        """Store a chat message and return it."""
        async with self.app_state.db.transaction() as db:
            msg_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO chat_messages"
                " (id, workspace_id, user_id, user_email, message, message_type)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    msg_id,
                    workspace_id,
                    user_id,
                    user_email,
                    message,
                    message_type,
                ),
            )
            mentioned_user_ids = await self.parse_mentions(
                db, message, workspace_id
            )
            for uid in mentioned_user_ids:
                await db.execute(
                    "INSERT INTO chat_mentions"
                    " (id, message_id, user_id, workspace_id)"
                    " VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), msg_id, uid, workspace_id),
                )
            cursor = await db.execute(
                "SELECT created_at FROM chat_messages WHERE id = ?", (msg_id,)
            )
            row = await cursor.fetchone()
            # Fetch the current handle for the sender.
            handle_cursor = await db.execute(
                "SELECT handle FROM users WHERE id = ?", (user_id,)
            )
            handle_row = await handle_cursor.fetchone()
            return {
                "id": msg_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "user_email": user_email,
                "user_handle": handle_row["handle"] if handle_row else None,
                "message": message,
                "message_type": message_type,
                "created_at": row["created_at"],
                "mentions": mentioned_user_ids,
            }

    async def delete_chat_message(self, message_id: str, user_id: str) -> bool:
        """Soft-delete a chat message by replacing its text.

        Only the author can delete their own messages.  The row is
        preserved so the history shows a placeholder.
        """
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE chat_messages SET message = '<message deleted by author>'"
                " WHERE id = ? AND user_id = ?",
                (message_id, user_id),
            )
            return cursor.rowcount > 0

    async def get_chat_messages_before(
        self, workspace_id: str, before_id: str, limit: int = 50
    ) -> list[dict]:
        """Get older chat messages before a given message ID."""
        async with self.app_state.db.transaction() as db:
            # Get the created_at and rowid of the anchor message
            cursor = await db.execute(
                "SELECT created_at, rowid FROM chat_messages WHERE id = ?",
                (before_id,),
            )
            anchor = await cursor.fetchone()
            if anchor is None:
                return []
            anchor_ts = anchor["created_at"]
            anchor_rowid = anchor["rowid"]

            cursor = await db.execute(
                "SELECT c.id, c.workspace_id, c.user_id, c.user_email,"
                " c.message, c.message_type, c.created_at,"
                " u.handle AS user_handle"
                " FROM chat_messages c LEFT JOIN users u ON c.user_id = u.id"
                " WHERE c.workspace_id = ?"
                " AND (c.created_at < ? OR (c.created_at = ? AND c.rowid < ?))"
                " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
                (workspace_id, anchor_ts, anchor_ts, anchor_rowid, limit),
            )
            rows = await cursor.fetchall()
            messages = list(
                reversed(
                    [
                        {
                            "id": row["id"],
                            "workspace_id": row["workspace_id"],
                            "user_id": row["user_id"],
                            "user_email": row["user_email"],
                            "user_handle": row["user_handle"],
                            "message": row["message"],
                            "message_type": row["message_type"],
                            "created_at": row["created_at"],
                        }
                        for row in rows
                    ]
                )
            )
            if messages:
                msg_ids = [m["id"] for m in messages]
                placeholders = ",".join("?" for _ in msg_ids)
                cursor = await db.execute(
                    "SELECT message_id, user_id FROM chat_mentions"
                    " WHERE message_id IN (" + placeholders + ")",
                    msg_ids,
                )
                mention_rows = await cursor.fetchall()
                mentions_by_msg: dict[str, list[str]] = {}
                for mr in mention_rows:
                    mentions_by_msg.setdefault(mr["message_id"], []).append(
                        mr["user_id"]
                    )
                for m in messages:
                    m["mentions"] = mentions_by_msg.get(m["id"], [])
            return messages

    async def get_chat_messages(
        self, workspace_id: str, limit: int = 50
    ) -> list[dict]:
        """Get the most recent chat messages for a workspace."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT c.id, c.workspace_id, c.user_id, c.user_email,"
                " c.message, c.message_type, c.created_at,"
                " u.handle AS user_handle"
                " FROM chat_messages c LEFT JOIN users u ON c.user_id = u.id"
                " WHERE c.workspace_id = ?"
                " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
                (workspace_id, limit),
            )
            rows = await cursor.fetchall()
            messages = list(
                reversed(
                    [
                        {
                            "id": row["id"],
                            "workspace_id": row["workspace_id"],
                            "user_id": row["user_id"],
                            "user_email": row["user_email"],
                            "user_handle": row["user_handle"],
                            "message": row["message"],
                            "message_type": row["message_type"],
                            "created_at": row["created_at"],
                        }
                        for row in rows
                    ]
                )
            )
            if messages:
                msg_ids = [m["id"] for m in messages]
                placeholders = ",".join("?" for _ in msg_ids)
                cursor = await db.execute(
                    "SELECT message_id, user_id FROM chat_mentions"
                    " WHERE message_id IN (" + placeholders + ")",
                    msg_ids,
                )
                mention_rows = await cursor.fetchall()
                mentions_by_msg: dict[str, list[str]] = {}
                for mr in mention_rows:
                    mentions_by_msg.setdefault(mr["message_id"], []).append(
                        mr["user_id"]
                    )
                for m in messages:
                    m["mentions"] = mentions_by_msg.get(m["id"], [])
            return messages
