"""Shared constants and small helpers used across the wshandler package.

This module is the dependency root of the wshandler package: it has no
intra-package imports, so any sibling module can import from here
without creating a cycle.  Objects that would otherwise create circular
dependencies between sibling modules are placed here.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

# Plain debug flag (not a KlangkSettings field): read straight from the
# environment, no file:/cmd: resolution (#1516).
WS_DEBUG = bool(os.environ.get("KLANGKD_WEBSOCKET_DEBUG"))

# Max size for terminal/exec input data (base64-decoded bytes).
# Matches uvicorn's --ws-max-size (16 MB) so the app-level cap isn't
# stricter than the transport cap — see #1257.
MAX_INPUT_SIZE = 16777216

# Max outbound messages before we declare the client too slow and close.
SEND_QUEUE_SIZE = 256


# ---------------------------------------------------------------------------
# log_ws_msg lives here (not in helpers) to break the
# helpers → session → helpers cycle.  It only needs WS_DEBUG and logging.
# ---------------------------------------------------------------------------


def log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (KLANGKD_WEBSOCKET_DEBUG=1)."""
    if not WS_DEBUG:
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
agent_conversations: dict[str, dict] = {}

# Active agent tasks per workspace, for abort support.
agent_tasks: dict[str, asyncio.Task] = {}


def cancel_agent_task(workspace_id: str) -> None:
    """Cancel and drop any in-flight agent run for a workspace.

    There is a single agent-run slot per workspace, so a new run must
    supersede (cancel) any still-running one — otherwise the earlier
    task is orphaned and can no longer be reached by an abort.
    """
    task = agent_tasks.pop(workspace_id, None)
    if task is not None and not task.done():
        task.cancel()


def drop_agent_task_if_current(workspace_id: str) -> None:
    """Remove the workspace's agent-task entry only if it is *this* run.

    A superseding mention cancels the prior run and installs a newer
    task, so a finishing (or cancelled) run must not pop an entry that
    now belongs to its replacement.
    """
    if agent_tasks.get(workspace_id) is asyncio.current_task():
        agent_tasks.pop(workspace_id, None)


def clear_agent_mention_state() -> None:
    """Cancel all in-flight agent runs and drop conversation context.

    Used by the SIGHUP runtime-restart path to avoid orphaned LLM
    requests and stale conversation state outliving their containers.
    Mirrors the per-workspace cleanup in ``reset_workspace`` but across
    every workspace at once.
    """
    for ws_id in list(agent_tasks):
        cancel_agent_task(ws_id)
    agent_conversations.clear()
