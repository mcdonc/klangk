"""Translates Pi RPC events into AG-UI protocol events."""

import uuid


def translate_event(pi_event: dict, workspace_id: str) -> list[dict]:
    """Convert a Pi RPC event into one or more AG-UI events.

    Returns a list because some Pi events map to multiple AG-UI events
    (e.g., tool_execution_end -> TOOL_CALL_END + TOOL_CALL_RESULT).
    """
    event_type = pi_event.get("type")
    agui_events = []

    if event_type == "session":
        # Pi session init event — no direct AG-UI equivalent, skip
        pass

    elif event_type == "agent_start":
        agui_events.append(
            {
                "type": "RUN_STARTED",
                "threadId": workspace_id,
                "runId": str(uuid.uuid4()),
            }
        )

    elif event_type == "agent_end":
        agui_events.append(
            {
                "type": "RUN_FINISHED",
                "threadId": workspace_id,
            }
        )

    elif event_type == "turn_start":
        agui_events.append(
            {
                "type": "STEP_STARTED",
                "stepName": "turn",
            }
        )

    elif event_type == "turn_end":
        agui_events.append(
            {
                "type": "STEP_FINISHED",
                "stepName": "turn",
            }
        )

    elif event_type == "message_start":
        msg = pi_event.get("message", {})
        agui_events.append(
            {
                "type": "TEXT_MESSAGE_START",
                "messageId": msg.get("id", str(uuid.uuid4())),
                "role": "assistant",
            }
        )

    elif event_type == "message_update":
        msg = pi_event.get("message", {})
        assistant_event = pi_event.get("assistantMessageEvent", {})
        delta_type = assistant_event.get("type")
        message_id = msg.get("id", str(uuid.uuid4()))

        if delta_type == "text_delta":
            agui_events.append(
                {
                    "type": "TEXT_MESSAGE_CONTENT",
                    "messageId": message_id,
                    "delta": assistant_event.get("delta", ""),
                }
            )
        elif delta_type == "thinking_delta":
            agui_events.append(
                {
                    "type": "REASONING_MESSAGE_CONTENT",
                    "messageId": message_id,
                    "delta": assistant_event.get("delta", ""),
                }
            )

    elif event_type == "message_end":
        msg = pi_event.get("message", {})
        agui_events.append(
            {
                "type": "TEXT_MESSAGE_END",
                "messageId": msg.get("id", str(uuid.uuid4())),
            }
        )

    elif event_type == "tool_execution_start":
        tool_call_id = pi_event.get("toolCallId", str(uuid.uuid4()))
        # Format args for display
        args = pi_event.get("args", {})
        args_str = ""
        if isinstance(args, dict):
            args_str = " ".join(f"{k}={v}" for k, v in args.items())
        agui_events.append(
            {
                "type": "TOOL_CALL_START",
                "toolCallId": tool_call_id,
                "toolCallName": pi_event.get("toolName", "unknown"),
                "toolCallArgs": args_str,
                "parentMessageId": pi_event.get("messageId"),
            }
        )

    elif event_type == "tool_execution_update":
        tool_call_id = pi_event.get("toolCallId", str(uuid.uuid4()))
        # Extract text from partialResult.content[].text
        delta = _extract_content_text(pi_event.get("partialResult", {}))
        if delta:
            agui_events.append(
                {
                    "type": "TOOL_CALL_ARGS",
                    "toolCallId": tool_call_id,
                    "delta": delta,
                }
            )

    elif event_type == "tool_execution_end":
        tool_call_id = pi_event.get("toolCallId", str(uuid.uuid4()))
        agui_events.append(
            {
                "type": "TOOL_CALL_END",
                "toolCallId": tool_call_id,
            }
        )
        # Extract text from result.content[].text
        result_text = _extract_content_text(pi_event.get("result", {}))
        agui_events.append(
            {
                "type": "TOOL_CALL_RESULT",
                "toolCallId": tool_call_id,
                "content": result_text,
                "role": "tool",
            }
        )

        # Emit file_changed CUSTOM event for tools that may modify files
        tool_name = pi_event.get("toolName", "")
        if tool_name in ("write", "edit"):
            agui_events.append(
                {
                    "type": "CUSTOM",
                    "name": "file_changed",
                    "value": {"path": ".", "action": "modified"},
                }
            )
        elif tool_name == "bash" and _bash_likely_creates_files(pi_event):
            agui_events.append(
                {
                    "type": "CUSTOM",
                    "name": "file_changed",
                    "value": {"path": ".", "action": "modified"},
                }
            )

    elif event_type == "error":
        agui_events.append(
            {
                "type": "RUN_ERROR",
                "message": pi_event.get("message", "Unknown error"),
                "code": pi_event.get("code"),
            }
        )

    elif event_type == "extension_ui_request":
        # Forward extension UI requests to the frontend for client-side handling
        agui_events.append(
            {
                "type": "CUSTOM",
                "name": "extension_ui_request",
                "value": pi_event,
            }
        )

    else:
        # Forward unknown events as CUSTOM
        agui_events.append(
            {
                "type": "CUSTOM",
                "name": f"pi_{event_type}",
                "value": pi_event,
            }
        )

    return agui_events


_FILE_CREATING_PATTERNS = [
    "mkdir",
    "touch",
    "cp ",
    "mv ",
    "ln ",
    "rm ",
    "cat <<",
    "cat>",
    "cat >",
    "tee ",
    "echo >",
    "echo>>",
    "printf >",
    "cargo new",
    "cargo init",
    "npm init",
    "npx create-",
    "npm create",
    "flutter create",
    "dart create",
    "pip install",
    "pip3 install",
    "git clone",
    "git init",
    "wget ",
    "curl -o",
    "curl -O",
    "curl --output",
    "tar ",
    "unzip ",
    "gunzip ",
    "cmake ",
    "make install",
    "python -m venv",
    "python3 -m venv",
    "virtualenv ",
    "rustup ",
    "cargo install",
    "go mod init",
    "go get ",
    "> ",
    ">> ",
]


def _bash_likely_creates_files(pi_event: dict) -> bool:
    """Check if a bash command is likely to create or modify files."""
    args = pi_event.get("args", {})
    if isinstance(args, dict):
        command = args.get("command", "")
    elif isinstance(args, str):
        command = args
    else:
        return False
    command_lower = command.lower()
    return any(pattern in command_lower for pattern in _FILE_CREATING_PATTERNS)


def _extract_content_text(container: dict | str | None) -> str:
    """Extract text from Pi's content structure: {content: [{type: 'text', text: '...'}]}."""
    if container is None:
        return ""
    if isinstance(container, str):
        return container
    content = container.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _extract_file_path(pi_event: dict) -> str | None:
    """Try to extract a file path from a tool execution event."""
    args = pi_event.get("args", {})
    if isinstance(args, dict):
        return args.get("path") or args.get("file_path")
    return None
