#!/usr/bin/env python3
"""Git credential helper that delegates to the user's browser.

Called by git as: git-credential-klangk get|store|erase

On "get": reads protocol/host/path from stdin, POSTs to the bridge,
and prints username/password to stdout if the browser responds.
On "store"/"erase": forwards to the bridge so the frontend can
update or clear its credential cache.

Falls through (exit 1) when the bridge is unreachable or the user
cancels, letting git try the next configured helper or prompt.
"""

import json
import os
import sys
import urllib.request
import urllib.error

BRIDGE_URL = os.environ.get("KLANGK_BRIDGE_URL", "")
BRIDGE_TOKEN = os.environ.get("KLANGK_BRIDGE_TOKEN", "")
WORKSPACE_TOKEN = os.environ.get("KLANGK_WORKSPACE_TOKEN", "")
TIMEOUT = int(os.environ.get("KLANGK_BRIDGE_TIMEOUT_SECONDS", "30"))


def read_credential_input():
    """Read git credential protocol from stdin (key=value lines)."""
    cred = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        if "=" in line:
            key, value = line.split("=", 1)
            cred[key] = value
    return cred


def post_bridge(operation, cred):
    """POST to the browser-delegate bridge. Returns parsed JSON or None."""
    payload = {
        "action": "git_credential",
        "token": BRIDGE_TOKEN,
        "operation": operation,
        "protocol": cred.get("protocol", ""),
        "host": cred.get("host", ""),
    }
    if cred.get("path"):
        payload["path"] = cred["path"]
    if cred.get("username"):
        payload["username"] = cred["username"]
    if cred.get("password"):
        payload["password"] = cred["password"]

    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if WORKSPACE_TOKEN:
        headers["Authorization"] = f"Bearer {WORKSPACE_TOKEN}"

    req = urllib.request.Request(
        f"{BRIDGE_URL}/api/browser-delegate",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def main():
    operation = sys.argv[1] if len(sys.argv) > 1 else ""

    if not BRIDGE_URL or not BRIDGE_TOKEN:
        sys.exit(1)

    cred = read_credential_input()

    if operation == "get":
        resp = post_bridge("get", cred)
        if not resp:
            sys.exit(1)

        username = resp.get("username", "")
        password = resp.get("password", "")
        if not username or not password:
            sys.exit(1)

        print(f"username={username}")
        print(f"password={password}")

    elif operation in ("store", "erase"):
        post_bridge(operation, cred)

    # Unknown operations: exit silently.


if __name__ == "__main__":
    main()
