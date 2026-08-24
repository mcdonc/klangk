"""Shared constants and small helpers used across the wshandler package.

This module is the dependency root of the wshandler package: it has no
intra-package imports, so any sibling module can import from here
without creating a cycle.  Objects that would otherwise create circular
dependencies between sibling modules are placed here.
"""

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
