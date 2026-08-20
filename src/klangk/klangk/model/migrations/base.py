"""Migration contract shared by the runner and every migration module."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from klangk.model.db import Connection


@dataclass(frozen=True)
class Migration:
    """One schema migration: *id* (contiguous, 1-based), *name* (unique,
    used in logs/records — frozen once shipped), and *apply*, an async
    callable taking the database connection. Production passes the
    SQLAlchemy async wrapper (:class:`klangk.model.db.Connection`);
    tests may pass a raw ``aiosqlite.Connection`` — both provide the
    same ``execute``/``commit``/``rollback`` surface."""

    id: int
    name: str
    apply: Callable[["Connection"], Awaitable[None]]
