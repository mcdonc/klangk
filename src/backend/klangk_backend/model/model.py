"""The ``Model(app_state)`` composition root for the data-access layer.

Composes the per-domain sub-objects so call sites reach data access through
a single owned instance — ``app_state.model.tokens.blocklist_token(...)`` —
exactly like every other ``X(app_state)`` subsystem. Each sub-object takes
``app_state`` and keeps it (reaching the DB via ``self.app_state.db``), so
every code path that opens the DB (lifespan, request handlers, startup
seed) uses the same resolved value — the one ``app.state.db`` the app was
built with (#1563, #1551).

Foundation (#1572): only the four standalone domains are composed here
(``tokens``, ``login_attempts``, ``invitations``, ``ports``). ``users``
(#1573), ``acl`` (#1574), and ``workspaces`` (#1575) are added; the
remaining domains (``chat``) are added in their own issues; until then
they're still reached via the module-level free functions + the
``_current_db`` ContextVar backstop in ``model/db.py``.
"""

from contextlib import asynccontextmanager

from .acl import ACLModel
from .chat import ChatModel
from .ports import PortsModel
from .tokens import TokensModel
from .login_attempts import LoginAttemptsModel
from .invitations import InvitationsModel
from .users import UsersModel
from .workspaces import WorkspacesModel
from .schema import init_db


class Model:
    """Owned data-access root: composes per-domain sub-objects.

    Constructed once at startup (``app.state.model = Model(app_state)``).
    Each sub-object takes ``app_state`` and reaches ``self.app_state.db``,
    so there is a single DB instance for the whole app — no implicit
    cross-task state (#1563).
    """

    def __init__(self, app_state):
        self.app_state = app_state
        self.tokens = TokensModel(app_state)
        self.login_attempts = LoginAttemptsModel(app_state)
        self.invitations = InvitationsModel(app_state)
        self.ports = PortsModel(app_state)
        self.users = UsersModel(app_state)
        self.acl = ACLModel(app_state)
        self.workspaces = WorkspacesModel(app_state)
        self.chat = ChatModel(app_state)

    @asynccontextmanager
    async def transaction(self):
        """Auto-commit-on-clean-exit transaction on this model's DB."""
        async with self.app_state.db.transaction() as db:
            yield db

    async def fetchone(self, query: str, params: tuple = ()):
        """Run a single-row SELECT and return the row, or ``None``."""
        return await self.app_state.db.fetchone(query, params)

    async def get_db(self):
        """Acquire a raw connection. Caller commits/rolls back/closes."""
        return await self.app_state.db.get_db()

    async def init_db(self):
        """Create/migrate the schema on this model's owned DB.

        Pulls a raw connection from ``self.app_state.db`` and hands it to
        :func:`init_db` (which commits + closes it). The schema bootstrap
        reaches the same single DB instance as every request path — there
        is no ambient/connectionless path (#1578, #1551).
        """
        db = await self.app_state.db.get_db()
        try:
            await init_db(db)
        finally:
            await db.close()
