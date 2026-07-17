"""Data-access layer.

Historically all database access — users, workspaces, ACL, chat, ports,
login attempts, and the schema — lived in a single ~2000-line
``model.py``.  That module has been split into per-domain submodules
(``db``, ``schema``, ``users``, ``acl``, ``workspaces``, ``ports``,
``chat``, ``login_attempts``, ``tokens``, ``invitations``), and each
domain is exposed as an ``XModel(app_state)`` method-bearing class
composed under :class:`~klangk.model.model.Model`.

Call sites reach data access through the owned instance —
``app_state.model.users.create_user(...)`` — so there is one ``DB`` for
the whole app (``app_state.db``) and no implicit cross-task state
(#1563, #1578). The module-level ContextVar backstop
(``_current_db`` / ``set_current_db`` / ``transaction`` / ``fetchone``
/ ``get_db``) and the per-domain free-function duplicates are gone
(#1578): the only module-level names re-exported here are the constants
and the pure helpers (which never touched a connection) plus the
composition root (``Model``) and the schema bootstrap (``init_db``).
"""

from .db import (
    DB,
    Connection,
    CursorResult,
    Row,
    logger,
)
from .schema import init_db
from .model import Model
from .users import (
    AGENT_USER_ID,
    AgentPrincipalError,
    ADMIN_USER_SORT_COLUMNS,
    HANDLE_RE,
    MAX_HANDLE_LEN,
    RESERVED_HANDLES,
    clear_agent_cache,
    derive_handle,
    generate_handle,
    hash_fallback_handle,
    unique_handle,
    backfill_handles,
    validate_handle,
)
from .acl import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    PRINCIPAL_USER,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
    row_to_acl_entry,
)
from .workspaces import (
    DEFAULT_PORTS_PER_WORKSPACE,
    SORT_COLUMNS,
    sort_order_clause,
    SETUP_STATE_COMPLETE,
    SETUP_STATE_FAILED,
    SETUP_STATE_PENDING,
    SETUP_STATES,
)
from .ports import (
    MAX_PORT,
    free_port,
    port_in_use,
    scan_free_ports,
)
from .chat import (
    MSG_AGENT,
    MSG_SYSTEM,
    MSG_USER,
    MENTION_RE,
)
from .invitations import ADMIN_INVITATION_SORT_COLUMNS

__all__ = (
    # db
    "DB",
    "Connection",
    "CursorResult",
    "Row",
    "logger",
    # schema
    "init_db",
    # composition root
    "Model",
    # users — constants + pure/db-param helpers (methods live on Model.users)
    "AGENT_USER_ID",
    "AgentPrincipalError",
    "ADMIN_USER_SORT_COLUMNS",
    "HANDLE_RE",
    "MAX_HANDLE_LEN",
    "RESERVED_HANDLES",
    "clear_agent_cache",
    "derive_handle",
    "generate_handle",
    "hash_fallback_handle",
    "unique_handle",
    "backfill_handles",
    "validate_handle",
    # acl — constants + pure helper
    "ACTION_ALLOW",
    "ACTION_DENY",
    "PRINCIPAL_GROUP",
    "PRINCIPAL_SYSTEM",
    "PRINCIPAL_USER",
    "SYSTEM_AUTHENTICATED",
    "SYSTEM_EVERYONE",
    "row_to_acl_entry",
    # workspaces — constants + pure helper
    "DEFAULT_PORTS_PER_WORKSPACE",
    "SORT_COLUMNS",
    "sort_order_clause",
    "SETUP_STATE_COMPLETE",
    "SETUP_STATE_FAILED",
    "SETUP_STATE_PENDING",
    "SETUP_STATES",
    # ports — constants + pure socket probe (re-exported via util too)
    "MAX_PORT",
    "port_in_use",
    "free_port",
    "scan_free_ports",
    # chat — message-type constants + mention regex
    "MSG_AGENT",
    "MSG_SYSTEM",
    "MSG_USER",
    "MENTION_RE",
    # invitations — sort columns
    "ADMIN_INVITATION_SORT_COLUMNS",
)
