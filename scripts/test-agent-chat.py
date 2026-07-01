#!/usr/bin/env python3
"""Test the clanker agent via the live server's WebSocket + REST API.

Usage:
    python scripts/test-agent-chat.py [--server URL] [--workspace NAME]

Sends a sequence of chat messages including @clanker mentions and
follow-ups, then prints the agent's responses. Useful for iterating
on context assembly and prompt quality.
"""

import argparse
import asyncio
import json
import sys
import time

import httpx
import websockets


async def main():
    parser = argparse.ArgumentParser(description="Test agent chat")
    parser.add_argument("--server", default="http://localhost:8995", help="Server URL")
    parser.add_argument(
        "--workspace", default=None, help="Workspace name (default: first)"
    )
    parser.add_argument("--email", default="admin@plope.com", help="Login email")
    parser.add_argument("--password", default="admin", help="Login password")
    parser.add_argument(
        "--clear", action="store_true", help="Delete all chat messages first"
    )
    args = parser.parse_args()

    base = args.server.rstrip("/")

    # Login
    resp = httpx.post(
        f"{base}/auth/login",
        json={"email": args.email, "password": args.password},
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print(f"✓ Logged in as {args.email}")

    # Find workspace
    resp = httpx.get(f"{base}/workspaces", headers=headers)
    resp.raise_for_status()
    workspaces = resp.json()
    if not workspaces:
        print("No workspaces found")
        sys.exit(1)
    if args.workspace:
        ws = next((w for w in workspaces if w["name"] == args.workspace), None)
        if not ws:
            print(f"Workspace '{args.workspace}' not found")
            sys.exit(1)
    else:
        ws = workspaces[0]
    print(f"✓ Using workspace: {ws['name']} ({ws['id'][:12]})")

    # Clear chat if requested (via sqlite directly — no REST endpoint)
    if args.clear:
        import sqlite3
        import os

        db_path = os.path.join(os.environ.get("KLANGK_DATA_DIR", ""), "klangk.db")
        if os.path.exists(db_path):
            db = sqlite3.connect(db_path)
            n = db.execute("DELETE FROM chat_messages").rowcount
            db.commit()
            db.close()
            print(f"✓ Cleared {n} messages")
        else:
            print(f"⚠ DB not found at {db_path}, skipping clear")

    # Connect via WebSocket
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
    ws_url += f"/ws?token={token}"

    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as conn:
        # Connect to workspace
        await conn.send(
            json.dumps({"cmd": "workspace_connect", "workspaceId": ws["id"]})
        )
        resp = json.loads(await conn.recv())
        assert resp.get("type") == "container_ready", f"Unexpected: {resp}"
        print("✓ Connected to workspace")

        # Signal UI ready so container starts
        await conn.send(json.dumps({"cmd": "ui_ready"}))

        # Wait for container_ready (may already be running)
        deadline = time.monotonic() + 120
        ready = False
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=5)
            except asyncio.TimeoutError:
                # No more messages — container is probably already running
                print("✓ Container already running (no container_ready event)")
                ready = True
                break
            msg = json.loads(raw)
            if (
                msg.get("type") == "event"
                and isinstance(msg.get("event"), dict)
                and msg["event"].get("name") == "container_ready"
            ):
                print("✓ Container ready")
                ready = True
                break
        if not ready:
            print("✗ Container did not become ready")
            sys.exit(1)

        # Helper to send a chat message and wait for agent response
        async def chat(text: str, expect_agent: bool = True) -> str | None:
            print(f"\n→ {text}")
            await conn.send(json.dumps({"cmd": "chat_send", "message": text}))

            if not expect_agent:
                await asyncio.sleep(1)
                return None

            # Wait for agent response (chat_message with message_type=1)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(conn.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "chat_message" and msg.get("message_type") == 1:
                    response = msg.get("message", "")
                    print(f"← {response[:500]}")
                    return response
            print("← [TIMEOUT — no agent response]")
            return None

        # --- Test conversation ---
        print("\n" + "=" * 60)
        print("CONVERSATION TEST")
        print("=" * 60)

        # 1. Direct question
        r1 = await chat("@clanker what is 2 + 2?")

        # 2. Follow-up (no @mention — tests conversation continuity)
        r2 = await chat("and what is that times 10?")

        # 3. Another follow-up
        r3 = await chat("can you write a Python function that computes that?")

        # 4. New topic with @mention
        r4 = await chat("@clanker what files are in the home directory?")

        # 5. Follow-up about files
        r5 = await chat("create a file called test.txt with 'hello world'")

        # 6. Verify
        r6 = await chat("now read that file back to me")

        print("\n" + "=" * 60)
        print("RESULTS SUMMARY")
        print("=" * 60)
        responses = [
            ("2+2", r1),
            ("times 10", r2),
            ("python function", r3),
            ("list files", r4),
            ("create file", r5),
            ("read file", r6),
        ]
        for label, r in responses:
            status = "✓" if r and r != "I had nothing to say." else "✗"
            preview = (r or "None")[:80].replace("\n", " ")
            print(f"  {status} {label}: {preview}")


if __name__ == "__main__":
    asyncio.run(main())
