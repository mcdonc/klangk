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
    _WS_DEBUG as _WS_DEBUG,
    _agent_conversations as _agent_conversations,
    _agent_tasks as _agent_tasks,
    cancel_agent_task as cancel_agent_task,
    drop_agent_task_if_current as drop_agent_task_if_current,
    log_ws_msg as log_ws_msg,
    bridge_idle_timeout as bridge_idle_timeout,
    clear_agent_mention_state as clear_agent_mention_state,
)
from .safe_websocket import (
    SafeWebSocket as SafeWebSocket,
    SlowClientError as SlowClientError,
    _WS_ERRORS as _WS_ERRORS,
    broadcast_to_set as broadcast_to_set,
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
    addresses_other_user as addresses_other_user,
    get_agent_mention_re as get_agent_mention_re,
    handle_agent_mention as handle_agent_mention,
    mentions_agent as mentions_agent,
)
from .dispatch import (
    _WS_CONNECTION_COMMANDS as _WS_CONNECTION_COMMANDS,
    _WS_STATE_COMMANDS as _WS_STATE_COMMANDS,
    handle_websocket as handle_websocket,
)
from .helpers import (
    format_container_info as format_container_info,
    format_idle_timeout as format_idle_timeout,
    get_presence_list as get_presence_list,
    get_shared_terminals as get_shared_terminals,
    send_event as send_event,
    disconnect_all_websockets as disconnect_all_websockets,
    refresh_user_handle as refresh_user_handle,
    reset_workspace_state as reset_workspace_state,
    send_error as send_error,
)
