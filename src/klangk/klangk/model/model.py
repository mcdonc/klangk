"""The ``Model(app_state)`` composition root for the data-access layer.

Composes the per-domain sub-objects so call sites reach data access through
a single owned instance — ``app_state.model.tokens.blocklist_token(...)`` —
exactly like every other ``X(app_state)`` subsystem. Each sub-object takes
``app_state`` and keeps it (reaching the DB via ``self.app.state.db``), so
every code path that opens the DB (lifespan, request handlers, startup
seed) uses the same resolved value — the one ``app.state.db`` the app was
built with (#1563, #1551).

Foundation (#1572): only the four standalone domains are composed here
(``tokens``, ``login_attempts``, ``invitations``, ``ports``). ``users``
(#1573), ``acl`` (#1574), and ``workspaces`` (#1575) are added; they're
reached via the module-level free functions + the
``_current_db`` ContextVar backstop in ``model/db.py``.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from .acl import ACLModel
from .audit_events import AuditEventsModel
from .audit_forward import AuditForwardModel
from .base import Submodel
from .container_events import ContainerEventsModel
from .egress_consent import EgressConsentModel
from .merged_events import MergedEventsModel
from .server_schedules import ServerSchedulesModel
from .ports import PortsModel
from .sessions import SessionsModel
from .invitations import InvitationsModel
from .users import UsersModel
from .workspaces import WorkspacesModel
from .schema import init_db


# ---------------------------------------------------------------------------
# The two trivial domains (moved verbatim from the former tokens and
# login_attempts submodules — their only importer was this composition
# root; #2858).
# ---------------------------------------------------------------------------


class TokensModel(Submodel):
    """Token-blocklist operations, resolved through ``app_state.db``.

    Constructed by :class:`~klangk.model.model.Model` and reached
    via ``app_state.model.tokens``. Reaches the DB through
    ``self.app.state.db`` (the single DB instance for the whole app).
    """

    async def blocklist_token(
        self, jti: str, expires_at: str, new_token: str | None = None
    ) -> None:
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO token_blocklist"
                " (jti, expires_at, new_token) VALUES (?, ?, ?)",
                (jti, expires_at, new_token),
            )

    async def is_token_blocklisted(self, jti: str) -> bool:
        row = await self.app.state.db.fetchone(
            "SELECT 1 FROM token_blocklist WHERE jti = ?",
            (jti,),
        )
        return row is not None

    async def get_refreshed_token(self, jti: str) -> str | None:
        """Return the replacement token for a refreshed JTI.

        The returned token is a full JWT whose own ``exp`` claim governs
        its validity — no additional expiry check is needed here.
        """
        row = await self.app.state.db.fetchone(
            "SELECT new_token FROM token_blocklist"
            " WHERE jti = ? AND new_token IS NOT NULL",
            (jti,),
        )
        return row[0] if row else None


class LoginAttemptsModel(Submodel):
    """Login-attempt storage, resolved through ``app_state.db``.

    Reached via ``app_state.model.login_attempts``. Reaches the DB through
    ``self.app.state.db`` (the single DB instance for the whole app).
    """

    async def record_failed_login(
        self, email: str, *, reset: bool = False
    ) -> None:
        """Record a failed login attempt for an email.

        Storage only; the sliding-window *decision* lives in ``auth.py``.
        """
        async with self.app.state.db.transaction() as db:
            now_iso = datetime.now(timezone.utc).isoformat()
            if reset:
                await db.execute(
                    """INSERT INTO login_attempts
                       (email, attempt_count, first_attempt_at)
                       VALUES (?, 1, ?)
                       ON CONFLICT(email) DO UPDATE SET
                       attempt_count = 1,
                       first_attempt_at = excluded.first_attempt_at,
                       locked_until = NULL""",
                    (email, now_iso),
                )
            else:
                await db.execute(
                    """INSERT INTO login_attempts (email, attempt_count, first_attempt_at)
                       VALUES (?, 1, ?) ON CONFLICT(email) DO UPDATE SET
                       attempt_count = attempt_count + 1""",
                    (email, now_iso),
                )

    async def get_login_attempt_info(
        self, email: str
    ) -> dict[str, int | str | None] | None:
        """Return login attempt info for an email, or None if no attempts tracked."""
        row = await self.app.state.db.fetchone(
            "SELECT attempt_count, first_attempt_at, locked_until"
            " FROM login_attempts WHERE email = ?",
            (email,),
        )
        if row is None:
            return None
        return {
            "attempt_count": row["attempt_count"],
            "first_attempt_at": row["first_attempt_at"],
            "locked_until": row["locked_until"],
        }

    async def set_login_lockout(self, email: str, locked_until: str) -> None:
        """Set the lockout time for an email after too many failed attempts."""
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE login_attempts SET locked_until = ? WHERE email = ?",
                (locked_until, email),
            )

    async def clear_login_attempts(self, email: str) -> None:
        """Clear all login attempts for an email (on successful login)."""
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "DELETE FROM login_attempts WHERE email = ?", (email,)
            )


class Model:
    """Owned data-access root: composes per-domain sub-objects.

    Constructed once at startup (``app.state.model = Model(app_state)``).
    Each sub-object takes ``app_state`` and reaches ``self.app.state.db``,
    so there is a single DB instance for the whole app — no implicit
    cross-task state (#1563).
    """

    def __init__(self, app):
        self.app = app
        self.tokens = TokensModel(app)
        self.sessions = SessionsModel(app)
        self.login_attempts = LoginAttemptsModel(app)
        self.invitations = InvitationsModel(app)
        self.ports = PortsModel(app)
        self.users = UsersModel(app)
        self.acl = ACLModel(app)
        self.workspaces = WorkspacesModel(app)
        self.egress_consent = EgressConsentModel(app)
        self.container_events = ContainerEventsModel(app)
        self.audit_events = AuditEventsModel(app)
        self.audit_forward = AuditForwardModel(app)
        self.merged_events = MergedEventsModel(app)
        self.server_schedules = ServerSchedulesModel(app)

    def reconfigure(self, app) -> None:
        self.app = app
        for sub in (
            self.tokens,
            self.sessions,
            self.login_attempts,
            self.invitations,
            self.ports,
            self.users,
            self.acl,
            self.workspaces,
            self.egress_consent,
            self.container_events,
            self.audit_events,
            self.audit_forward,
            self.merged_events,
            self.server_schedules,
        ):
            sub.reconfigure(app)

    @asynccontextmanager
    async def transaction(self):
        """Auto-commit-on-clean-exit transaction on this model's DB."""
        async with self.app.state.db.transaction() as db:
            yield db

    async def fetchone(self, query: str, params: tuple = ()):
        """Run a single-row SELECT and return the row, or ``None``."""
        return await self.app.state.db.fetchone(query, params)

    async def get_db(self):
        """Acquire a raw connection. Caller commits/rolls back/closes."""
        return await self.app.state.db.get_db()

    async def init_db(self):
        """Create/migrate the schema on this model's owned DB.

        Pulls a raw connection from ``self.app.state.db`` and hands it to
        :func:`init_db` (which commits + closes it). The schema bootstrap
        reaches the same single DB instance as every request path — there
        is no ambient/connectionless path (#1578, #1551).
        """
        db = await self.app.state.db.get_db()
        try:
            await init_db(db)
        finally:
            await db.close()
