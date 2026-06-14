"""HTTP + WebSocket client for the Klangk backend."""

from __future__ import annotations


import asyncio
import io
import json
import logging
import os
from pathlib import Path
import select
import sys
import termios
import tty
from dataclasses import dataclass

import httpx
import websockets

from .config import CLIConfig

_WS_MAX_SIZE = int(os.environ.get("KLANGK_WS_MSG_SIZE_MAX", 2**24))


@dataclass
class Workspace:
    id: str
    name: str
    created_at: str
    image: str | None = None
    default_command: str | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None
    owner_email: str | None = None


def _get_terminal_size() -> tuple[int, int]:
    """Return (columns, rows) of the local terminal, or a sensible default."""
    if sys.stdin.isatty():
        size = os.get_terminal_size()
        return size.columns, size.lines
    return 80, 24


class KlangkClient:
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
            timeout=30.0,
            **kwargs,
        )

    def post(self, path: str, **kwargs) -> httpx.Response:  # pragma: no cover
        return httpx.post(
            f"{self.cfg.server.url}{path}",
            headers=self._headers(),
            timeout=30.0,
            **kwargs,
        )

    def put(self, path: str, **kwargs) -> httpx.Response:  # pragma: no cover
        return httpx.put(
            f"{self.cfg.server.url}{path}",
            headers=self._headers(),
            timeout=30.0,
            **kwargs,
        )

    def delete(
        self, path: str, **kwargs
    ) -> httpx.Response:  # pragma: no cover
        return httpx.delete(
            f"{self.cfg.server.url}{path}",
            headers=self._headers(),
            timeout=30.0,
            **kwargs,
        )

    # --- REST API ---

    def _check_auth(self, resp: httpx.Response) -> None:
        """Raise AuthError if the server returned 401."""
        if resp.status_code == 401:
            raise AuthError("Session expired — run `klangk login`")

    def list_workspaces(self) -> list[Workspace]:
        resp = self.get("/workspaces")
        self._check_auth(resp)
        resp.raise_for_status()
        raw = resp.json()
        return [
            Workspace(
                id=w["id"],
                name=w["name"],
                created_at=w["created_at"],
                image=w.get("image"),
                default_command=w.get("default_command"),
                mounts=w.get("mounts"),
                env=w.get("env"),
            )
            for w in raw
        ]

    def list_shared_workspaces(self) -> list[Workspace]:
        resp = self.get("/workspaces/shared")
        self._check_auth(resp)
        resp.raise_for_status()
        raw = resp.json()
        return [
            Workspace(
                id=w["id"],
                name=w["name"],
                created_at=w["created_at"],
                image=w.get("image"),
                default_command=w.get("default_command"),
                mounts=w.get("mounts"),
                env=w.get("env"),
                owner_email=w.get("owner_email"),
            )
            for w in raw
        ]

    def create_workspace(  # pragma: no cover
        self,
        name: str,
        image: str | None = None,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> Workspace:
        body: dict = {"name": name}
        if image:
            body["image"] = image
        if mounts:
            body["mounts"] = mounts
        if env:
            body["env"] = env
        resp = self.post("/workspaces", json=body)
        self._check_auth(resp)
        resp.raise_for_status()
        w = resp.json()
        return Workspace(
            id=w["id"], name=w["name"], created_at=w["created_at"]
        )

    def list_images(self) -> dict:  # pragma: no cover
        resp = self.get("/images")
        self._check_auth(resp)
        resp.raise_for_status()
        return resp.json()

    def resolve_workspace(self, name: str) -> Workspace:
        """Find a workspace by name (owned or shared).

        Raises WorkspaceNotFoundError if not found.
        """
        all_ws = self.list_workspaces() + self.list_shared_workspaces()
        match = next((w for w in all_ws if w.name == name), None)
        if match is None:
            raise WorkspaceNotFoundError(name)
        return match

    def delete_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.delete(f"/workspaces/{ws.id}")
        self._check_auth(resp)
        if not resp.is_success:
            logging.error("Failed to delete workspace: %s", resp.text)
            sys.exit(1)

    def export_workspace(
        self,
        workspace_id: str,
        output: Path,
        on_progress=None,
    ) -> None:
        """Download a workspace archive to a file.

        on_progress(bytes_so_far, total_bytes) is called for each chunk.
        total_bytes is None if the server didn't send Content-Length.
        """
        with httpx.stream(
            "GET",
            f"{self.cfg.server.url}/workspaces/{workspace_id}/export",
            headers=self._headers(),
            timeout=300.0,
        ) as resp:
            self._check_auth(resp)
            if not resp.is_success:
                resp.read()  # consume body so .text is available
                resp.raise_for_status()
            # Use Content-Length if available, otherwise fall back to
            # the server's compressed size estimate.
            if "content-length" in resp.headers:
                total = int(resp.headers["content-length"])
            elif "x-estimated-size" in resp.headers:
                total = int(resp.headers["x-estimated-size"])
            else:
                total = None
            downloaded = 0
            with open(output, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)

    def import_workspace(
        self, archive: Path, name: str | None = None, on_progress=None
    ) -> Workspace:
        """Upload a workspace archive and create a new workspace.

        on_progress(bytes_so_far, total_bytes) is called as bytes are read.
        """
        params = {}
        if name:
            params["name"] = name
        total = archive.stat().st_size

        class _ProgressFile:
            """Wraps a file to track read progress."""

            def __init__(self, f):
                self._f = f
                self._read = 0

            def read(self, size=-1):
                data = self._f.read(size)
                if data:
                    self._read += len(data)
                    if on_progress:
                        on_progress(self._read, total)
                return data

            def seek(
                self, *args
            ):  # pragma: no cover — called by httpx multipart
                self._read = 0
                return self._f.seek(*args)

            def tell(self):  # pragma: no cover — called by httpx multipart
                return self._f.tell()

        with open(archive, "rb") as f:
            pf = _ProgressFile(f) if on_progress else f
            resp = httpx.post(
                f"{self.cfg.server.url}/workspaces/import",
                headers=self._headers(),
                files={"file": (archive.name, pf, "application/gzip")},
                params=params,
                timeout=300.0,
            )
        self._check_auth(resp)
        resp.raise_for_status()
        w = resp.json()
        return Workspace(
            id=w["id"], name=w["name"], created_at=w["created_at"]
        )


class WorkspaceNotFoundError(Exception):
    pass


class AuthError(Exception):
    pass


# --- Shell session ---


async def _send_ignore_closed(ws, msg: str) -> None:  # pragma: no cover
    """Send a WebSocket message, ignoring errors if the connection is closed."""
    try:
        await ws.send(msg)
    except (websockets.ConnectionClosed, OSError):
        pass


def _raw_mode_enter() -> object:
    """Enter raw mode on stdin.  Returns opaque old-settings object."""
    return termios.tcgetattr(sys.stdin)


def _raw_mode_exit(old_settings: object) -> None:
    """Restore terminal from a previous _raw_mode_enter call."""
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


def reset_terminal() -> None:
    """Reset terminal state: disable mouse tracking, show cursor.

    Called on disconnect to clean up modes that container apps
    (Pi, nano, etc.) may have enabled.
    """
    sys.stdout.write(
        "\x1b[?1000l"  # disable mouse click tracking
        "\x1b[?1002l"  # disable mouse button tracking
        "\x1b[?1003l"  # disable all mouse tracking
        "\x1b[?1006l"  # disable SGR mouse mode
        "\x1b[?25h"  # show cursor
    )
    sys.stdout.flush()


async def _ws_shell(
    ws_url: str,
    token: str,
    workspace_id: str,
    raw_mode: bool = True,
    command_override: str | None = None,
    window: str | None = None,
) -> None:
    """Run the interactive PTY shell over WebSocket.

    raw_mode controls whether stdin is placed in raw (cbreak) mode.
    Pass False in tests or when stdin is not a real terminal.
    command_override, if set, overrides the workspace default command.
    window, if set, selects a specific window by name. Use
    ``handle:window_name`` to join another user's shared window.
    """
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=_WS_MAX_SIZE
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

        # 2. Start terminal
        cols, rows = _get_terminal_size()
        start_msg: dict = {"cmd": "terminal_start", "cols": cols, "rows": rows}
        if command_override is not None:
            start_msg["commandOverride"] = command_override
        await ws.send(json.dumps(start_msg))

        # 3. Drain messages until the first terminal_output (the shell prompt).
        # Along the way, collect terminal_windows and shared_terminals for
        # window selection.
        own_windows: list[dict] = []
        shared_terminals: list[dict] = []
        try:
            deadline = asyncio.get_event_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "terminal_windows":
                    own_windows = msg.get("windows", [])
                elif msg.get("type") == "shared_terminals":
                    shared_terminals = msg.get("terminals", [])
                elif msg.get("type") == "terminal_output":
                    sys.stdout.write(msg.get("data", ""))
                    sys.stdout.flush()
                    break
                elif msg.get("type") == "error":  # pragma: no cover
                    raise ConnectionError(
                        f"Server error: {msg.get('message', 'unknown')}"
                    )
        except asyncio.TimeoutError:  # pragma: no cover
            raise ConnectionError(
                "Terminal did not start within 30 seconds"
            ) from None

        # 3b. Select window if requested.
        if window is not None:
            if ":" in window:
                # Shared terminal: "handle:window_name"
                owner_handle, win_name = window.split(":", 1)
                match = next(
                    (
                        t
                        for t in shared_terminals
                        if t.get("handle") == owner_handle
                        and t.get("window_name") == win_name
                    ),
                    None,
                )
                if match is None:
                    raise ConnectionError(
                        f"Shared terminal '{window}' not found"
                    )
                await ws.send(
                    json.dumps(
                        {
                            "cmd": "join_shared_terminal",
                            "user_id": match["user_id"],
                            "window_id": match["window_id"],
                        }
                    )
                )
                # Wait for terminal_started confirmation
                deadline = asyncio.get_event_loop().time() + 10
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:  # pragma: no cover
                        raise asyncio.TimeoutError
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    if msg.get("type") == "terminal_started":
                        break
                    if msg.get("type") == "terminal_output":
                        sys.stdout.write(msg.get("data", ""))
                        sys.stdout.flush()
                    if msg.get("type") == "error":
                        raise ConnectionError(
                            f"Failed to join: {msg.get('message')}"
                        )
            else:
                # Own window by name
                match = next(
                    (w for w in own_windows if w.get("name") == window),
                    None,
                )
                if match is None:
                    raise ConnectionError(f"Window '{window}' not found")
                await ws.send(
                    json.dumps(
                        {
                            "cmd": "terminal_select_window",
                            "index": match["index"],
                        }
                    )
                )

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
                reset_terminal()
        await _send_ignore_closed(  # pragma: no cover
            ws, json.dumps({"cmd": "terminal_stop"})
        )


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
                # os.read runs in the executor, never on the event loop:
                # a false-positive select (e.g. a racing reader, or a test
                # that stubs select) must not be able to block stdout/resize/
                # heartbeat while this read waits for a byte.
                data = await loop.run_in_executor(None, os.read, fd, 1)
                if not data:  # EOF on stdin
                    return
                # If the first byte is ESC, read the rest of the escape
                # sequence as a single unit. Without this, the sequence
                # gets split across WebSocket messages and the terminal
                # can't interpret arrow keys, etc.
                if data == b"\x1b":
                    # Brief wait for the rest of the sequence
                    if select.select([fd], [], [], 0.05)[0]:
                        more = await loop.run_in_executor(
                            None, os.read, fd, 32
                        )
                        if more:
                            data += more
            except (OSError, io.UnsupportedOperation):  # pragma: no cover
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
            stdout.write("\r\nServer disconnected.\r\n")
            stdout.flush()
        stop_event.set()

    async def resize_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
                return  # pragma: no cover
            except asyncio.TimeoutError:
                pass
            new_cols, new_rows = _get_terminal_size()
            if new_cols != _current_cols[0] or new_rows != _current_rows[0]:
                _current_cols[0] = new_cols
                _current_rows[0] = new_rows
                await _send_resize()

    async def heartbeat_loop() -> None:  # pragma: no cover
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                pass
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
        f"{ws_url}?token={token}", max_size=_WS_MAX_SIZE
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
                # Offload the read so a false-positive select cannot wedge
                # the event loop (and with it stdout_forward) on a blocking
                # os.read. See stdin_loop in _run_shell for the same pattern.
                data = await loop.run_in_executor(None, os.read, 0, 65536)
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
                    # Offload: when the downstream consumer (e.g. rsync over
                    # `klangk exec`) is slow, the stdout pipe fills and this
                    # write blocks. On the event loop that would also stall
                    # stdin_forward and the heartbeat — a sync deadlock.
                    await loop.run_in_executor(None, os.write, 1, raw)
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
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                    return
                except asyncio.TimeoutError:
                    pass
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
        except asyncio.CancelledError:  # pragma: no cover
            pass

        await ws.send(json.dumps({"cmd": "exec_stop"}))
        return exit_code
