"""WebSocket handler: auth, workspace routing, terminal/exec/bridge.

Historically all WebSocket handling lived in a single ~3000-line
``wshandler.py``.  That module has been split into per-concern
submodules (``constants``, ``safe_websocket``, ``session``,
``controllers``, ``connection``, ``dispatch``, ``helpers``).

This package re-exports every public (and the few private) names from
those submodules so existing call sites keep working unchanged, e.g.::

    from . import wshandler
    from .wshandler import handle_websocket, Connection
"""

# Re-export sibling modules so that existing patch targets like
# ``klangk.wshandler.auth`` and ``wshandler.model`` keep
# working — the old monolith imported them at module level.
from .. import auth as auth  # noqa: F401
from .. import container as container  # noqa: F401
from .. import model as model  # noqa: F401
from .. import podman as podman  # noqa: F401
from .. import terminal as terminal  # noqa: F401
from .. import workspaces as workspaces  # noqa: F401

from .constants import (
    MAX_INPUT_SIZE as MAX_INPUT_SIZE,
    SEND_QUEUE_SIZE as SEND_QUEUE_SIZE,
    WS_DEBUG as WS_DEBUG,
    log_ws_msg as log_ws_msg,
)
from .safe_websocket import (
    SafeWebSocket as SafeWebSocket,
    SlowClientError as SlowClientError,
    WS_ERRORS as WS_ERRORS,
    broadcast_to_set as broadcast_to_set,
)
from .session import (
    WebSocketState as WebSocketState,
    WorkspaceSession as WorkspaceSession,
)
from .controllers import (
    ExecController as ExecController,
    SharedTerminalController as SharedTerminalController,
    SshAgentForwarder as SshAgentForwarder,
    TerminalController as TerminalController,
)
from .connection import Connection as Connection
from .decider import handle_consent_decider as handle_consent_decider
from .sidecar import handle_egress_sidecar as handle_egress_sidecar
from .dispatch import (
    _WS_CONNECTION_COMMANDS as _WS_CONNECTION_COMMANDS,
    _WS_STATE_COMMANDS as _WS_STATE_COMMANDS,
    handle_websocket as handle_websocket,
)
from .helpers import (
    format_container_info as format_container_info,
    format_idle_timeout as format_idle_timeout,
    get_shared_terminals as get_shared_terminals,
    send_event as send_event,
    disconnect_all_websockets as disconnect_all_websockets,
    disconnect_user as disconnect_user,
    refresh_user_handle as refresh_user_handle,
    reset_workspace_state as reset_workspace_state,
    send_error as send_error,
)
