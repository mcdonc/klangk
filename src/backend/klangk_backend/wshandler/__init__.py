"""WebSocket handler: auth, workspace routing, terminal/exec/bridge.

Historically all WebSocket handling lived in a single ~3000-line
``wshandler.py``.  That module has been split into per-concern
submodules (``_constants``, ``safe_websocket``, ``session``,
``controllers``, ``connection``, ``agent_mention``, ``dispatch``,
``helpers``).

This package re-exports every public (and the few private) names from
those submodules so existing call sites keep working unchanged, e.g.::

    from . import wshandler
    wshandler.state.get_session(...)
    from .wshandler import handle_websocket, Connection
"""

# Re-export sibling modules so that existing patch targets like
# ``klangk_backend.wshandler.auth`` and ``wshandler.model`` keep
# working — the old monolith imported them at module level.
from .. import acl as _acl  # noqa: F401
from .. import agent as agent  # noqa: F401
from .. import auth as auth  # noqa: F401
from .. import container as container  # noqa: F401
from .. import model as model  # noqa: F401
from .. import podman as podman  # noqa: F401
from .. import terminal as terminal  # noqa: F401
from .. import workspaces as workspaces  # noqa: F401

from ._constants import (
    _MAX_INPUT_SIZE as _MAX_INPUT_SIZE,
    _SEND_QUEUE_SIZE as _SEND_QUEUE_SIZE,
    _WS_RECEIVE_TIMEOUT as _WS_RECEIVE_TIMEOUT,
    _WS_DEBUG as _WS_DEBUG,
    _agent_conversations as _agent_conversations,
    _agent_tasks as _agent_tasks,
    _cancel_agent_task as _cancel_agent_task,
    _drop_agent_task_if_current as _drop_agent_task_if_current,
    _log_ws_msg as _log_ws_msg,
    bridge_idle_timeout as bridge_idle_timeout,
)
from .safe_websocket import (
    ReceiveTimeoutError as ReceiveTimeoutError,
    SafeWebSocket as SafeWebSocket,
    SlowClientError as SlowClientError,
    _WS_ERRORS as _WS_ERRORS,
    _broadcast_to_set as _broadcast_to_set,
)
from .session import (
    WebSocketState as WebSocketState,
    WorkspaceSession as WorkspaceSession,
    state as state,
)
from .controllers import (
    ExecController as ExecController,
    SharedTerminalController as SharedTerminalController,
    SshAgentForwarder as SshAgentForwarder,
    TerminalController as TerminalController,
)
from .connection import Connection as Connection
from .agent_mention import (
    _ANY_MENTION_RE as _ANY_MENTION_RE,
    _addresses_other_user as _addresses_other_user,
    _get_agent_mention_re as _get_agent_mention_re,
    _handle_agent_mention as _handle_agent_mention,
    _mentions_agent as _mentions_agent,
)
from .dispatch import (
    _WS_CONNECTION_COMMANDS as _WS_CONNECTION_COMMANDS,
    _WS_STATE_COMMANDS as _WS_STATE_COMMANDS,
    handle_websocket as handle_websocket,
)
from .helpers import (
    _format_container_info as _format_container_info,
    _format_idle_timeout as _format_idle_timeout,
    _get_presence_list as _get_presence_list,
    _get_shared_terminals as _get_shared_terminals,
    _send_event as _send_event,
    refresh_user_handle as refresh_user_handle,
    reset_workspace_state as reset_workspace_state,
    send_error as send_error,
)
