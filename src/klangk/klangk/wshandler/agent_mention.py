"""Agent-mention detection and routing."""

import asyncio
import logging
import re

from ..agent import AgentProcessDied, ephemeral_system_message
from ..model import AGENT_USER_ID, MSG_AGENT, MSG_USER
from .constants import (
    agent_conversations as agent_conversations,
    agent_tasks as agent_tasks,
    cancel_agent_task as cancel_agent_task,
    drop_agent_task_if_current as drop_agent_task_if_current,
)
from .session import WebSocketState

logger = logging.getLogger(__name__)


# _ANY_MENTION_RE matches any @mention; the agent-specific regex is
# compiled fresh per call (see get_agent_mention_re).
_ANY_MENTION_RE = re.compile(r"(?:^|(?<=\s))@\S+")


def get_agent_mention_re(handle: str) -> re.Pattern:
    """Return the compiled agent mention regex for *handle*.

    The agent's handle can change at runtime (DB update), so this is
    compiled fresh on every call rather than cached. Regex compilation
    is cheap, and caching would return a stale pattern after a rename.
    """
    return re.compile(
        r"(?:^|(?<=\s))@" + re.escape(handle) + r"(?:\s|$)",
        re.IGNORECASE,
    )


async def mentions_agent(text: str, app) -> bool:
    """Return True if the message text mentions the agent."""
    handle = await app.state.model.users.agent_handle()
    return bool(get_agent_mention_re(handle).search(text))


async def addresses_other_user(text: str, app) -> bool:
    """Return True if the message is directed at someone else.

    A message that *starts* with ``@someone`` (not the agent) is
    considered addressed to that person, breaking the follow-up
    conversation with the agent.
    """
    m = _ANY_MENTION_RE.match(text.lstrip())
    if not m:
        return False
    mention = m.group().lstrip("@").lower()
    handle = await app.state.model.users.agent_handle()
    return mention != handle.lower()


def asker_context_header(
    user_id: str | None,
    user_handle: str | None,
    user_home: str | None,
) -> str | None:
    """Build the ``[Asking user: ...]`` header that resolves "my".

    The chat agent has no user identity of its own (it runs as the
    ``klangk`` service user, with no ``KLANGKWS_USER_ID``), so "my
    history" / "my terminal" is ambiguous in a multi-collaborator
    workspace. Injecting the asking user's identity lets the agent
    target the right per-user tmux session (named after ``user_id``)
    and home directory. Returns ``None`` when no identity was provided
    (the caller then omits the header).
    """
    if not user_id:
        return None
    parts = [f"id {user_id}"]
    if user_handle:
        parts.append(f"handle {user_handle}")
    if user_home:
        parts.append(f"home {user_home}")
    return (
        "[Asking user: "
        + ", ".join(parts)
        + '. Their interactive terminals are tmux session "'
        + user_id
        + '"; "my"/"my history" refers to that session.]'
    )


async def handle_agent_mention(
    sockets: WebSocketState,
    workspace_id: str,
    container_id: str,
    user_text: str,
    *,
    user_id: str | None = None,
    user_handle: str | None = None,
    user_home: str | None = None,
) -> None:
    """Handle an @agent mention by sending the prompt to Pi RPC.

    *user_id* / *user_handle* / *user_home* identify the asking user so
    the agent can resolve "my" (its own process has no user identity).
    Omitted when ``None`` (e.g. from older call sites); the header is
    then not injected and "my" is left for the agent to disambiguate.
    """

    agent_handle = await sockets.app.state.model.users.agent_handle()
    agent_re = get_agent_mention_re(agent_handle)
    prompt = agent_re.sub("", user_text).strip()
    if not prompt:
        prompt = "Hello!"

    # Inject the asking user's identity so the agent can target the
    # asker's own tmux session / home when they say "my".
    asker_header = asker_context_header(user_id, user_handle, user_home)
    if asker_header:
        prompt = f"{asker_header}\n\n{prompt}"

    # Include messages from OTHER users since the agent's last response
    # as context.  The current user's message is already the prompt;
    # we only need to show interjections from other participants that
    # Pi hasn't seen (since Pi's multi-turn history only has the
    # conversation between the mentioning user and itself).
    recent = await sockets.app.state.model.chat.get_chat_messages(
        workspace_id, limit=50
    )
    chronological = recent
    last_agent_idx = -1
    for i, m in enumerate(chronological):
        if m.get("message_type", 0) == MSG_AGENT:
            last_agent_idx = i
    # Messages from other users (not the current prompt sender)
    other_msgs = [
        m
        for m in chronological[last_agent_idx + 1 :]
        if m.get("message_type", 0) == MSG_USER
        and m.get("message", "").strip() != user_text.strip()
    ]
    if other_msgs:
        context_lines = [
            f"{m.get('user_email', 'unknown')}: {m.get('message', '')}"
            for m in other_msgs
        ]
        context = "\n".join(context_lines)
        prompt = f"[Other participants said:\n{context}]\n\n{prompt}"

    agent_email = await sockets.app.state.model.users.agent_email()

    # Notify clients the agent is thinking
    session = sockets.get_session(workspace_id)
    if session:
        session.broadcast(
            {
                "type": "agent_thinking",
                "thinking": True,
                "name": agent_handle,
            }
        )

    try:
        pi = await sockets.app.state.agents.get_session(workspace_id)
        response_text = await pi.send_prompt(prompt)
    except asyncio.CancelledError:  # pragma: no cover
        response_text = "Stopped."
    except AgentProcessDied:
        logger.warning("Agent process died for workspace %s", workspace_id)
        # Broadcast an ephemeral (non-persisted) system message — agent
        # presence transitions are container-lifecycle events and must
        # not linger in chat history (no symmetric persisted "connected").
        sys_msg = ephemeral_system_message(
            workspace_id,
            agent_email,
            agent_handle,
            f"{agent_handle} has disconnected",
        )
        session = sockets.get_session(workspace_id)
        if session:
            session.broadcast({"type": "agent_thinking", "thinking": False})
            session.broadcast({"type": "chat_message", **sys_msg})
        drop_agent_task_if_current(workspace_id)
        return
    except Exception:
        logger.exception("Agent error for workspace %s", workspace_id)
        response_text = (
            "Sorry, I encountered an error processing your request."
        )

    agent_msg = await sockets.app.state.model.chat.add_chat_message(
        workspace_id,
        AGENT_USER_ID,
        agent_email,
        response_text,
        message_type=MSG_AGENT,
    )
    session = sockets.get_session(workspace_id)
    if session:
        session.broadcast({"type": "agent_thinking", "thinking": False})
        session.broadcast({"type": "chat_message", **agent_msg})
    drop_agent_task_if_current(workspace_id)
