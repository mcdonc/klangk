"""Container-to-chat route: post a chat message from a workspace using a workspace JWT."""

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from pydantic import BaseModel

from .. import (
    model,
)
from ._common import get_app_state_dep
from ._common import (
    require_workspace_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class WorkspaceChatRequest(BaseModel):
    message: str


@router.post("/workspaces/post-chat-message")
async def workspace_chat(
    body: WorkspaceChatRequest,
    workspace_id: str = Depends(require_workspace_token),
    app_state=Depends(get_app_state_dep),
):
    """Post a chat message from a container using a workspace JWT.

    The message is stored as MSG_AGENT and broadcast to all connected
    WebSocket subscribers in the workspace.
    """
    workspace = await app_state.model.workspaces.get_workspace_by_id(
        workspace_id
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    chat_msg = await model.add_chat_message(
        workspace_id,
        "agent",
        "agent",
        text,
        message_type=model.MSG_AGENT,
    )
    session = app_state.sockets.get_session(workspace_id)
    if session:
        session.broadcast({"type": "chat_message", **chat_msg})
    return chat_msg


# --- Admin endpoints (require admin role) ---
