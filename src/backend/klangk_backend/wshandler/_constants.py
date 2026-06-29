"""Shared constants and small helpers used across the wshandler package.

This module is the dependency root of the wshandler package: it has no
intra-package imports, so any sibling module can import from here
without creating a cycle.  Objects that would otherwise create circular
dependencies between sibling modules are placed here.
"""

import asyncio
import json
import logging

from ..util import resolve_env_value

logger = logging.getLogger(__name__)

_WS_DEBUG = bool(resolve_env_value("KLANGK_WS_DEBUG"))

# Max size for terminal/exec input data (base64-decoded bytes).
_MAX_INPUT_SIZE = 65536

# Max outbound messages before we declare the client too slow and close.
_SEND_QUEUE_SIZE = 256

# Seconds to wait for any inbound WebSocket message before assuming the
# connection is dead.  The client sends heartbeats every 60 s, so 90 s
# gives 50 % headroom.  This is a belt-and-suspenders guard on top of
# uvicorn's protocol-level --ws-ping-interval / --ws-ping-timeout.
_WS_RECEIVE_TIMEOUT = 90


def bridge_idle_timeout() -> float:
    """Max seconds between streamed browser chunks before giving up.

    Bounds the gap between chunks (not the total query duration), so a
    long-but-progressing stream never times out.  Override with
    KLANGK_BRIDGE_TIMEOUT_SECONDS.
    """
    raw = resolve_env_value("KLANGK_BRIDGE_TIMEOUT_SECONDS")
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0


# ---------------------------------------------------------------------------
# _log_ws_msg lives here (not in helpers) to break the
# helpers → session → helpers cycle.  It only needs _WS_DEBUG and logging.
# ---------------------------------------------------------------------------


def _log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (KLANGK_WS_DEBUG=1)."""
    if not _WS_DEBUG:
        return
    msg_type = msg.get("type") or msg.get("cmd") or "?"
    # Truncate terminal_output/terminal_input data to avoid log spam
    if msg_type in ("terminal_output", "terminal_input"):
        data = msg.get("data", "")
        preview = repr(data[:80]) + ("..." if len(data) > 80 else "")
        who = f" [{user['email']}]" if user else ""
        logger.debug("WS %s%s: %s data=%s", direction, who, msg_type, preview)
    else:
        who = f" [{user['email']}]" if user else ""
        logger.debug("WS %s%s: %s", direction, who, json.dumps(msg)[:200])


# ---------------------------------------------------------------------------
# Agent-task state lives here (not in agent_mention) to break the
# session → agent_mention → session cycle.
# ---------------------------------------------------------------------------

# Per-workspace agent conversation state.
# user_id: who started the conversation
# time: monotonic timestamp of the last agent exchange
# interjected: True after a different human spoke
_agent_conversations: dict[str, dict] = {}

# Active agent tasks per workspace, for abort support.
_agent_tasks: dict[str, asyncio.Task] = {}


def _cancel_agent_task(workspace_id: str) -> None:
    """Cancel and drop any in-flight agent run for a workspace.

    There is a single agent-run slot per workspace, so a new run must
    supersede (cancel) any still-running one — otherwise the earlier
    task is orphaned and can no longer be reached by an abort.
    """
    task = _agent_tasks.pop(workspace_id, None)
    if task is not None and not task.done():
        task.cancel()


def _drop_agent_task_if_current(workspace_id: str) -> None:
    """Remove the workspace's agent-task entry only if it is *this* run.

    A superseding mention cancels the prior run and installs a newer
    task, so a finishing (or cancelled) run must not pop an entry that
    now belongs to its replacement.
    """
    if _agent_tasks.get(workspace_id) is asyncio.current_task():
        _agent_tasks.pop(workspace_id, None)
