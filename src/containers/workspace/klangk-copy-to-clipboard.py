#!/usr/bin/env python3
"""Copy text from stdin to the system clipboard.

Used by tmux's copy-pipe to make selections auto-copy to the user's
clipboard.

Two strategies, tried in order:

1. **Bridge** (web frontend): POST clipboard_write to the browser-delegate
   bridge endpoint, which tells the Flutter app to call Clipboard.setData.
2. **OSC 52** (klangk shell / any terminal): emit the OSC 52 escape
   sequence so the outer terminal emulator copies to the system clipboard.
   Supported by iTerm2, Windows Terminal, kitty, alacritty, foot, etc.
"""

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BRIDGE_URL = os.environ.get("KLANGK_BRIDGE_URL", "")


def _get_workspace_token():
    try:
        result = subprocess.run(
            ["klangk-workspace-token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _get_browser_id():
    try:
        result = subprocess.run(
            ["klangk-browser-id"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _try_bridge(text, browser_id):
    """Try to copy via the bridge. Returns True on success."""
    if not BRIDGE_URL or not browser_id:
        return False

    payload = json.dumps(
        {
            "action": "clipboard_write",
            "browser_id": browser_id,
            "text": text,
        }
    ).encode()

    headers = {"Content-Type": "application/json"}
    workspace_token = _get_workspace_token()
    if workspace_token:
        headers["Authorization"] = f"Bearer {workspace_token}"

    req = urllib.request.Request(
        f"{BRIDGE_URL}/api/v1/browser-delegate",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _osc52(text):
    """Emit OSC 52 escape sequence to set the system clipboard.

    Works in most modern terminal emulators.  Writes to the tmux
    parent terminal via the passthrough escape (DCS tmux; ... ST).
    """
    encoded = base64.b64encode(text.encode()).decode()
    # tmux requires DCS passthrough to forward OSC 52 to the outer terminal.
    if os.environ.get("TMUX"):
        seq = f"\033Ptmux;\033\033]52;c;{encoded}\033\033\\\033\\"
    else:
        seq = f"\033]52;c;{encoded}\033\\"
    # Write to the terminal, not stdout (which tmux has piped).
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(seq)
            tty.flush()
    except OSError:
        pass


def main():
    text = sys.stdin.read()
    if not text:
        sys.exit(0)

    browser_id = _get_browser_id()

    # "klangkshell" sentinel means we're in klangk shell (CLI), not a
    # browser — skip the bridge and go straight to OSC 52.
    if browser_id == "klangkshell":
        _osc52(text)
        return

    # Try bridge first (web frontend); fall back to OSC 52 (CLI terminal).
    if not _try_bridge(text, browser_id):
        _osc52(text)


if __name__ == "__main__":
    main()
