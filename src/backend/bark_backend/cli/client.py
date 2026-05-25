"""HTTP + WebSocket client for the Bark backend."""

from __future__ import annotations


import asyncio
import io
import json
import logging
import os
import select
import sys
import termios
import tty
from dataclasses import dataclass

import httpx
import websockets

from .config import CLIConfig


@dataclass
class Workspace:
    id: str
    name: str
    created_at: str


def _get_terminal_size() -> tuple[int, int]:
    """Return (columns, rows) of the local terminal, or a sensible default."""
    if sys.stdin.isatty():
        size = os.get_terminal_size()
        return size.columns, size.lines
    return 80, 24


class BarkClient:
    def __init__(self, cfg: CLIConfig):
        self.cfg = cfg

    # --- HTTP helpers ---

    def _headers(self) -> dict[str, str]:
        token = self.cfg.auth.token or ""
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str, **kwargs) -> httpx.Response:  # pragma: no cover
        return httpx.get(
            f"{self.cfg.server.url}{path}",
            headers=self._headers(),
            timeout=15.0,
            **kwargs,
        )

    def post(self, path: str, **kwargs) -> httpx.Response:  # pragma: no cover
        return httpx.post(
            f"{self.cfg.server.url}{path}",
            headers=self._headers(),
            timeout=15.0,
            **kwargs,
        )

    def delete(
        self, path: str, **kwargs
    ) -> httpx.Response:  # pragma: no cover
        return httpx.delete(
            f"{self.cfg.server.url}{path}",
            headers=self._headers(),
            timeout=15.0,
            **kwargs,
        )

    # --- REST API ---

    def list_workspaces(self) -> list[Workspace]:
        resp = self.get("/workspaces")
        if resp.status_code == 401:
            raise AuthError("Not logged in — run `bark login`")
        resp.raise_for_status()
        raw = resp.json()
        # (workspace listing is tested via test_list_workspaces_empty, test_resolve_workspace_by_name, etc.)
        return [
            Workspace(id=w["id"], name=w["name"], created_at=w["created_at"])
            for w in raw
        ]

    def create_workspace(self, name: str) -> Workspace:  # pragma: no cover
        resp = self.post("/workspaces", params={"name": name})
        if resp.status_code == 401:
            raise AuthError("Not logged in — run `bark login`")
        resp.raise_for_status()
        w = resp.json()
        return Workspace(
            id=w["id"], name=w["name"], created_at=w["created_at"]
        )

    def resolve_workspace(self, name: str) -> Workspace:
        """Find a workspace by name. Raises WorkspaceNotFoundError if not found."""
        ws = self.list_workspaces()
        match = next((w for w in ws if w.name == name), None)
        if match is None:
            raise WorkspaceNotFoundError(name)
        return match

    def delete_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.delete(f"/workspaces/{ws.id}")
        if resp.status_code == 401:
            raise AuthError("Not logged in — run `bark login`")
        if not resp.is_success:
            logging.error("Failed to delete workspace: %s", resp.text)
            sys.exit(1)


class WorkspaceNotFoundError(Exception):
    pass


class AuthError(Exception):
    pass


# --- Shell session ---


def _raw_mode_enter() -> object:
    """Enter raw mode on stdin.  Returns opaque old-settings object."""
    return termios.tcgetattr(sys.stdin)


def _raw_mode_exit(old_settings: object) -> None:
    """Restore terminal from a previous _raw_mode_enter call."""
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


async def _ws_shell(
    ws_url: str,
    token: str,
    workspace_id: str,
    raw_mode: bool = True,
) -> None:
    """Run the interactive PTY shell over WebSocket.

    raw_mode controls whether stdin is placed in raw (cbreak) mode.
    Pass False in tests or when stdin is not a real terminal.
    """
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=2**20
    ) as ws:
        # 1. Connect to workspace
        await ws.send(
            json.dumps(
                {"cmd": "workspace_connect", "workspaceId": workspace_id}
            )
        )
        resp = json.loads(await ws.recv())
        if resp.get("type") != "workspace_ready":
            raise ConnectionError(f"Connection failed: {resp}")

        # 1b. Signal UI is ready so the backend can deliver pending status.
        await ws.send(json.dumps({"cmd": "ui_ready"}))

        # 2. Start terminal
        cols, rows = _get_terminal_size()
        await ws.send(
            json.dumps({"cmd": "terminal_start", "cols": cols, "rows": rows})
        )

        # 3. Drain messages until the first terminal_output (the clear sequence).
        # Timeout prevents hanging if the container fails to start a shell.
        try:
            deadline = asyncio.get_event_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "terminal_output":
                    break
                if msg.get("type") == "error":  # pragma: no cover
                    raise ConnectionError(
                        f"Server error: {msg.get('message', 'unknown')}"
                    )
        except asyncio.TimeoutError:  # pragma: no cover
            raise ConnectionError(
                "Terminal did not start within 30 seconds"
            ) from None

        # 4. Put terminal in raw mode, run shell, restore
        # raw_mode path: tcgetattr + tty.setraw + _raw_mode_exit + terminal_stop  # pragma: no cover
        if raw_mode:
            old_settings = _raw_mode_enter()
            tty.setraw(sys.stdin)
        try:
            await _run_shell(ws, cols, rows)
        finally:
            if raw_mode:
                _raw_mode_exit(old_settings)
        await ws.send(json.dumps({"cmd": "terminal_stop"}))  # pragma: no cover


async def _run_shell(
    ws,
    cols: int,
    rows: int,
    stdin: io.RawIOBase | None = None,
    stdout: io.TextIOBase | None = None,
) -> None:
    """Run stdin/stdout forwarding loop with SIGWINCH support.

    stdin/stdout default to sys.stdin.buffer / sys.stdout when None.
    Pass explicit streams in tests to avoid mutating globals.
    """
    if stdin is None:
        stdin = sys.stdin.buffer
    if stdout is None:
        stdout = sys.stdout
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    _current_cols = [cols]
    _current_rows = [rows]

    async def _send_resize() -> None:
        await ws.send(
            json.dumps(
                {
                    "cmd": "terminal_resize",
                    "cols": _current_cols[0],
                    "rows": _current_rows[0],
                }
            )
        )

    async def stdin_loop() -> None:
        fd = stdin.fileno()
        while not stop_event.is_set():
            # select() with a 0.2s timeout keeps us responsive to stop_event
            # without burning CPU. When stop_event fires we exit within 0.2s.
            ready, _, _ = await loop.run_in_executor(
                None, lambda: select.select([fd], [], [], 0.2)
            )
            if not ready:
                continue
            try:
                data = stdin.read(1)
            except (OSError, io.UnsupportedOperation):  # pragma: no cover
                return
            if not data:  # EOF on stdin
                return
            await ws.send(
                json.dumps(
                    {
                        "cmd": "terminal_input",
                        "data": data.decode("utf-8", errors="replace"),
                    }
                )
            )

    async def stdout_loop() -> None:
        try:
            while not stop_event.is_set():
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="replace")
                data = json.loads(msg)
                if data.get("type") == "terminal_output":
                    stdout.write(data["data"])
                    stdout.flush()
                elif data.get("type") == "event":
                    event = data.get("event", {})
                    if (
                        event.get("type") == "CUSTOM"
                        and event.get("name") == "container_stopped"
                    ):
                        logging.info("[container stopped]")
                        stop_event.set()
                        break
        except websockets.ConnectionClosed:
            logging.info("[connection lost]")
        stop_event.set()

    async def resize_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(1)
            new_cols, new_rows = _get_terminal_size()
            if new_cols != _current_cols[0] or new_rows != _current_rows[0]:
                _current_cols[0] = new_cols
                _current_rows[0] = new_rows
                await _send_resize()

    async def heartbeat_loop() -> None:  # pragma: no cover
        while not stop_event.is_set():
            await asyncio.sleep(60)
            if not stop_event.is_set():
                await ws.send(json.dumps({"cmd": "heartbeat"}))

    await asyncio.gather(
        stdin_loop(), stdout_loop(), resize_loop(), heartbeat_loop()
    )


async def _ws_exec(
    ws_url: str,
    token: str,
    workspace_id: str,
    command: list[str],
) -> int:
    """Run a command in the container over WebSocket, piping stdin/stdout.

    Returns the remote process exit code.
    """
    import base64

    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=2**20
    ) as ws:
        # 1. Connect to workspace
        await ws.send(
            json.dumps(
                {"cmd": "workspace_connect", "workspaceId": workspace_id}
            )
        )
        resp = json.loads(await ws.recv())
        if resp.get("type") != "workspace_ready":
            raise ConnectionError(f"Connection failed: {resp}")

        await ws.send(json.dumps({"cmd": "ui_ready"}))

        # 2. Start exec session
        await ws.send(json.dumps({"cmd": "exec_start", "command": command}))

        # 3. Pipe stdin/stdout
        loop = asyncio.get_event_loop()
        exit_code = 1

        stop = asyncio.Event()

        async def stdin_forward() -> None:
            while not stop.is_set():
                ready = await loop.run_in_executor(
                    None, lambda: select.select([0], [], [], 0.2)[0]
                )
                if not ready:  # pragma: no cover
                    continue
                data = os.read(0, 65536)
                if not data:
                    await ws.send(json.dumps({"cmd": "exec_close_stdin"}))
                    break
                await ws.send(  # pragma: no cover
                    json.dumps(
                        {
                            "cmd": "exec_input",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    )
                )

        async def stdout_forward() -> None:
            nonlocal exit_code
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):  # pragma: no cover
                    msg = msg.decode("utf-8", errors="replace")
                data = json.loads(msg)
                if data.get("type") == "exec_output":
                    raw = base64.b64decode(data["data"])
                    os.write(1, raw)
                elif data.get("type") == "exec_exit":
                    exit_code = data.get("code", 0)
                    break
                elif data.get("type") == "error":  # pragma: no cover
                    logging.error(
                        "Server error: %s",
                        data.get("message", "unknown"),
                    )
                    exit_code = 1
                    break

        async def heartbeat_loop() -> None:  # pragma: no cover
            while not stop.is_set():
                await asyncio.sleep(60)
                if not stop.is_set():
                    await ws.send(json.dumps({"cmd": "heartbeat"}))

        # stdout_forward drives the lifecycle — when it receives
        # exec_exit, it sets stop so stdin_forward exits promptly.
        stdout_task = asyncio.create_task(stdout_forward())
        stdin_task = asyncio.create_task(stdin_forward())
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        await stdout_task
        stop.set()
        # stdin_forward exits within 0.2s thanks to select timeout
        try:
            await asyncio.wait_for(stdin_task, timeout=2)
        except asyncio.TimeoutError:  # pragma: no cover
            stdin_task.cancel()
            try:
                await stdin_task
            except asyncio.CancelledError:
                pass
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        await ws.send(json.dumps({"cmd": "exec_stop"}))
        return exit_code
