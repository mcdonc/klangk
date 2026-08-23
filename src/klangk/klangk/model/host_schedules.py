"""Pending host shutdown/restart schedules (#2661).

Rows live only while *pending* — the scheduler deletes a row when it fires
or when it is cancelled — so the table is the authoritative "what host
actions are scheduled" set across klangkd restarts.

:class:`HostSchedulesModel` is the ``app_state``-owned form reached via
``app_state.model.host_schedules``, following the same ``app``-only
ownership rule as the sibling models (#1563).
"""

import uuid
from datetime import datetime, timezone

_VALID_ACTIONS = ("shutdown", "restart")


def normalize_action(action: str) -> str:
    """Validate and normalize a schedule action.

    Raises ``ValueError`` for anything but ``shutdown`` / ``restart`` so
    the API layer can turn it into a 422 without the model guessing.
    """
    value = (action or "").strip().lower()
    if value not in _VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {', '.join(_VALID_ACTIONS)}; got {action!r}"
        )
    return value


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "action": row["action"],
        "fire_at": row["fire_at"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


class HostSchedulesModel:
    """CRUD for pending host schedules, resolved through ``app_state.db``."""

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def create_schedule(
        self, action: str, fire_at: datetime, created_by: str
    ) -> dict:
        """Insert a pending schedule and return its dict.

        *fire_at* must be timezone-aware (naive datetimes are ambiguous
        about which clock they mean); the value is stored as UTC ISO-8601
        so the scheduler compares apples to apples on every boot.
        """
        action = normalize_action(action)
        if fire_at.tzinfo is None:
            raise ValueError("fire_at must be timezone-aware")
        schedule_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT INTO host_schedules"
                " (id, action, fire_at, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    schedule_id,
                    action,
                    fire_at.astimezone(timezone.utc).isoformat(),
                    created_by,
                    created_at.isoformat(),
                ),
            )
        return {
            "id": schedule_id,
            "action": action,
            "fire_at": fire_at.astimezone(timezone.utc).isoformat(),
            "created_by": created_by,
            "created_at": created_at.isoformat(),
        }

    async def pending_schedules(self) -> list[dict]:
        """Every pending schedule, soonest first."""
        rows = await self.app.state.db.fetchall(
            "SELECT id, action, fire_at, created_by, created_at"
            " FROM host_schedules ORDER BY fire_at"
        )
        return [_row_to_dict(row) for row in rows]

    async def cancel_schedule(self, schedule_id: str) -> bool:
        """Delete a pending schedule. Returns False when it doesn't exist."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM host_schedules WHERE id = ?", (schedule_id,)
            )
            return bool(cursor.rowcount)

    async def delete_schedule(self, schedule_id: str) -> None:
        """Unconditionally drop a row (used after it fires)."""
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "DELETE FROM host_schedules WHERE id = ?", (schedule_id,)
            )
