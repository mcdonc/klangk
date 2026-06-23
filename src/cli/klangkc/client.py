"""HTTP + WebSocket client for the Klangk backend."""

from __future__ import annotations


import asyncio
import base64
import io
import json
import logging
import os
from pathlib import Path
import re
import select
import socket
import struct
import sys
import termios
import tty
from dataclasses import dataclass

import time as _time

import httpx
import websockets


_WS_MAX_SIZE = int(os.environ.get("KLANGK_WS_MSG_SIZE_MAX", 2**24))

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 2.0  # seconds, doubled each retry

_WS_CONNECT_TIMEOUT = 60  # seconds to wait for workspace_ready


async def _wait_workspace_ready(
    ws: websockets.ClientConnection,
    workspace_id: str,
    timeout: float = _WS_CONNECT_TIMEOUT,
) -> dict:
    """Send workspace_connect and wait for workspace_ready, skipping broadcasts.

    The server may send broadcast messages (e.g. presence_list from eager
    agent startup) before workspace_ready.  This drains them rather than
    treating the first non-ready message as an error.

    Returns the workspace_ready payload.
    """
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("Timed out waiting for workspace_ready")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        resp = json.loads(raw)
        if resp.get("type") == "workspace_ready":
            return resp
        if resp.get("type") == "error":
            raise ConnectionError(f"Connection failed: {resp}")


def _request_with_retry(
    method: str,
    url: str,
    *,
    timeout: float = 60.0,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with retry on transient failures.

    Retries on ReadTimeout, ConnectTimeout, ConnectError, and 502/503/504
    responses with exponential backoff.
    """
    backoff = _RETRY_BACKOFF
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = httpx.request(method, url, timeout=timeout, **kwargs)
            if (
                resp.status_code in (502, 503, 504)
                and attempt < _RETRY_ATTEMPTS - 1
            ):
                logger.debug(
                    "HTTP %s %s returned %d, retrying in %.1fs",
                    method,
                    url,
                    resp.status_code,
                    backoff,
                )
                _time.sleep(backoff)
                backoff *= 2
                continue
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < _RETRY_ATTEMPTS - 1:
                logger.debug(
                    "HTTP %s %s failed (%s), retrying in %.1fs",
                    method,
                    url,
                    exc,
                    backoff,
                )
                _time.sleep(backoff)
                backoff *= 2
            else:
                raise


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
    def __init__(self, server_url: str, token: str | None = None):
        self.server_url = server_url
        self.token = token

    # --- HTTP helpers ---

    def _headers(self) -> dict[str, str]:
        token = self.token or ""
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str, **kwargs) -> httpx.Response:
        return _request_with_retry(
            "GET",
            f"{self.server_url}{path}",
            headers=self._headers(),
            **kwargs,
        )

    def post(self, path: str, **kwargs) -> httpx.Response:
        return _request_with_retry(
            "POST",
            f"{self.server_url}{path}",
            headers=self._headers(),
            **kwargs,
        )

    def put(self, path: str, **kwargs) -> httpx.Response:
        return _request_with_retry(
            "PUT",
            f"{self.server_url}{path}",
            headers=self._headers(),
            **kwargs,
        )

    def patch(self, path: str, **kwargs) -> httpx.Response:
        return _request_with_retry(
            "PATCH",
            f"{self.server_url}{path}",
            headers=self._headers(),
            **kwargs,
        )

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return _request_with_retry(
            "DELETE",
            f"{self.server_url}{path}",
            headers=self._headers(),
            **kwargs,
        )

    # --- REST API ---

    def _check_auth(self, resp: httpx.Response) -> None:
        """Raise AuthError if the server returned 401."""
        if resp.status_code == 401:
            raise AuthError("Session expired — run `klangkc login`")

    def get_handle(self) -> str:
        """Return the current user's handle via ``GET /auth/me``."""
        resp = self.get("/api/v1/auth/me")
        self._check_auth(resp)
        resp.raise_for_status()
        return resp.json()["handle"]

    def list_workspaces(self) -> list[Workspace]:
        resp = self.get("/api/v1/workspaces")
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
        resp = self.get("/api/v1/workspaces/shared")
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

    def create_workspace(
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
        resp = self.post("/api/v1/workspaces", json=body)
        self._check_auth(resp)
        resp.raise_for_status()
        w = resp.json()
        return Workspace(
            id=w["id"], name=w["name"], created_at=w["created_at"]
        )

    def list_images(self) -> dict:
        resp = self.get("/api/v1/images")
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
        resp = self.delete(f"/api/v1/workspaces/{ws.id}")
        self._check_auth(resp)
        resp.raise_for_status()

    def list_workspace_members(self, name: str) -> list[dict]:
        ws = self.resolve_workspace(name)
        resp = self.get(f"/api/v1/workspaces/{ws.id}/members")
        self._check_auth(resp)
        resp.raise_for_status()
        return resp.json()

    def add_workspace_member(
        self, name: str, email: str, role: str = "coders"
    ) -> dict:
        ws = self.resolve_workspace(name)
        resp = self.patch(
            f"/api/v1/workspaces/{ws.id}/roles",
            json={"email": email, "role": role},
        )
        self._check_auth(resp)
        resp.raise_for_status()
        return resp.json()

    def remove_workspace_member(self, name: str, email: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.patch(
            f"/api/v1/workspaces/{ws.id}/roles",
            json={"email": email, "role": None},
        )
        self._check_auth(resp)
        if resp.status_code == 404:
            raise WorkspaceNotFoundError(
                f"User '{email}' is not a member of '{name}'"
            )
        resp.raise_for_status()

    def restart_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.post(f"/api/v1/workspaces/{ws.id}/restart")
        self._check_auth(resp)
        resp.raise_for_status()

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
            f"{self.server_url}/api/v1/workspaces/{workspace_id}/export",
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
                f"{self.server_url}/api/v1/workspaces/import",
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


async def _send_ignore_closed(ws, msg: str) -> None:
    """Send a WebSocket message, ignoring errors if the connection is closed."""
    try:
        await ws.send(msg)
    except (websockets.ConnectionClosed, OSError):
        pass


# Patterns matching terminal query responses that arrive on stdin when
# tmux probes the terminal's capabilities on attach.  These are NOT user
# input and must be filtered before forwarding to terminal_input, or tmux
# echoes them as visible garbage.
#
# Matched responses:
#   DA1:     ESC [ ? <digits;...> c
#   DA2:     ESC [ > <digits;...> c
#   DSR:     ESC [ <digits;...> n
#   DECRPM:  ESC [ ? <digits;...> y   (or $ y)
#   OSC:     ESC ] <digits> ; <payload> ST   (ST = ESC \ or BEL)
#   XTVER:   ESC [ > | <payload> ST
_TERMINAL_RESPONSE_RE = re.compile(
    rb"\x1b\[[\?>]?[\d;]*[cnySy]"  # CSI responses (DA1/DA2/DSR/DECRPM)
    rb"|\x1b\][\d]+;[^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC responses
    rb"|\x1b\[>\|[^\x1b]*\x1b\\"  # XTVERSION
    rb"|\x1bP[^\x1b]*\x1b\\"  # DCS responses
)


def _is_terminal_response(data: bytes) -> bool:
    """True if *data* looks like a terminal query response, not user input.

    Terminal responses start with ESC followed by ] (OSC), P (DCS), or
    [ then > or ? (DA2/DA1/DECRPM).  User-typed escape sequences start
    with ESC [ followed by a letter (arrow keys, function keys) without
    the > or ? prefix that characterizes responses.
    """
    if len(data) < 3 or data[0:1] != b"\x1b":
        return False
    # Fast path: OSC (\e]) and DCS (\eP) are always responses, never
    # user input.
    if data[1:2] in (b"]", b"P"):
        return True
    # CSI responses: \e[> (DA2), \e[? (DA1/DECRPM)
    if data[1:2] == b"[" and len(data) > 2 and data[2:3] in (b">", b"?"):
        return True
    return False


def _drain_stdin() -> None:
    """Drain any pending bytes from stdin (terminal query responses).

    Terminal capability responses can arrive over several hundred
    milliseconds after tmux probes the terminal.  We drain in a loop
    with a generous timeout so late-arriving responses don't leak to
    the host shell as garbage commands.
    """
    try:
        fd = sys.stdin.fileno()
        # Put stdin in raw mode temporarily so responses don't echo
        # and aren't line-buffered.
        try:
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            restore = True
        except termios.error:
            restore = False
        try:
            # Drain for up to 500ms total, checking every 50ms.
            for _ in range(10):
                if select.select([fd], [], [], 0.05)[0]:
                    os.read(fd, 4096)
                else:
                    # No data for 50ms — but responses may still be in
                    # flight. Wait one more round to be sure.
                    if not select.select([fd], [], [], 0.1)[0]:
                        break
                    os.read(fd, 4096)
        finally:
            if restore:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (OSError, io.UnsupportedOperation):
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
        "\x1b[?1049l"  # exit alternate screen
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
    forward_agent: bool = False,
    sandbox_setup=None,
    max_size: int = _WS_MAX_SIZE,
) -> None:
    """Run the interactive PTY shell over WebSocket.

    raw_mode controls whether stdin is placed in raw (cbreak) mode.
    Pass False in tests or when stdin is not a real terminal.
    command_override, if set, overrides the workspace default command.
    window, if set, selects a specific window by name. Use
    ``handle:window_name`` to join another user's shared window.
    sandbox_setup, if set, is an async callable(ws) invoked after the
    workspace is ready but before the terminal starts.  Used by
    ``sandbox`` to run copy/setup on the same connection.
    """
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=max_size
    ) as ws:
        # 1. Connect to workspace
        await _wait_workspace_ready(ws, workspace_id)

        # 2a. Start SSH agent forwarding if requested and available.
        ssh_agent_active = False
        local_agent_sock = os.environ.get("SSH_AUTH_SOCK")
        _debug_agent = os.environ.get("KLANGKC_DEBUG_SSH_AGENT", "")
        if _debug_agent:  # pragma: no cover
            _agent_log = os.path.expanduser("~/.klangkc-ssh-agent.log")
            _fh = logging.FileHandler(_agent_log, mode="w")
            _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(_fh)
            logger.setLevel(logging.DEBUG)
            logger.info(
                "[ssh-agent] forward_agent=%s, SSH_AUTH_SOCK=%s",
                forward_agent,
                local_agent_sock,
            )
        if (
            forward_agent
            and local_agent_sock
            and os.path.exists(local_agent_sock)
        ):
            if _debug_agent:  # pragma: no cover
                logger.info("[ssh-agent] sending ssh_agent_start")
            await ws.send(json.dumps({"cmd": "ssh_agent_start"}))
            # Wait for confirmation before starting the terminal so
            # SSH_AUTH_SOCK is included in the shell environment.
            try:
                deadline = asyncio.get_event_loop().time() + 10
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:  # pragma: no cover
                        break
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    if _debug_agent:  # pragma: no cover
                        logger.info(
                            "[ssh-agent] during wait: %s", msg.get("type")
                        )
                    if msg.get("type") == "ssh_agent_started":
                        ssh_agent_active = True
                        if _debug_agent:  # pragma: no cover
                            logger.info(
                                "[ssh-agent] started, socket=%s",
                                msg.get("socket"),
                            )
                        break
                    if msg.get("type") == "error":
                        if _debug_agent:  # pragma: no cover
                            logger.info(
                                "[ssh-agent] error: %s", msg.get("message")
                            )
                        break
            except asyncio.TimeoutError:
                if _debug_agent:  # pragma: no cover
                    logger.info("[ssh-agent] timed out waiting for start")
                pass  # proceed without agent forwarding

        # 2b. Run pre-shell hook (sandbox setup) after agent forwarding
        # is active so that setup scripts can use SSH (e.g. git clone).
        if sandbox_setup is not None:
            await sandbox_setup(ws)

        # 2c. Start terminal
        cols, rows = _get_terminal_size()
        start_msg: dict = {
            "cmd": "terminal_start",
            "cols": cols,
            "rows": rows,
            "browser_id": "klangkshell",
        }
        if command_override is not None:
            start_msg["commandOverride"] = command_override
        await ws.send(json.dumps(start_msg))

        # 3. Drain messages until we have terminal_windows (needed for
        # window selection).  terminal_output may arrive before
        # terminal_windows due to async output forwarding, so we buffer
        # early output and don't stop until the window list is in.
        own_windows: list[dict] = []
        shared_terminals: list[dict] = []
        buffered_output: list[str] = []
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
                    break
                elif msg.get("type") == "shared_terminals":
                    shared_terminals = msg.get("terminals", [])
                elif msg.get("type") == "terminal_output":
                    buffered_output.append(msg.get("data", ""))
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
                    # Create the window if it doesn't exist.
                    await ws.send(
                        json.dumps(
                            {
                                "cmd": "terminal_new_window",
                                "name": window,
                            }
                        )
                    )
                    deadline = asyncio.get_event_loop().time() + 10
                    while True:
                        remaining = deadline - asyncio.get_event_loop().time()
                        if remaining <= 0:  # pragma: no cover
                            raise asyncio.TimeoutError
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=remaining
                        )
                        msg = json.loads(raw)
                        if msg.get("type") == "terminal_windows":
                            new_windows = msg.get("windows", [])
                            match = next(
                                (
                                    w
                                    for w in new_windows
                                    if w.get("name") == window
                                ),
                                None,
                            )
                            break
                        if msg.get("type") == "terminal_output":
                            buffered_output.append(msg.get("data", ""))
                        if msg.get("type") == "error":
                            raise ConnectionError(
                                f"Failed to create window: "
                                f"{msg.get('message')}"
                            )
                    if match is None:  # pragma: no cover
                        raise ConnectionError(f"Window '{window}' not created")
                await ws.send(
                    json.dumps(
                        {
                            "cmd": "terminal_select_window",
                            "index": match["index"],
                        }
                    )
                )

        # Flush buffered terminal output from the startup drain.
        for text in buffered_output:
            sys.stdout.write(text)
        sys.stdout.flush()

        # 4. Put terminal in raw mode, run shell, restore
        # raw_mode path: tcgetattr + tty.setraw + _raw_mode_exit + terminal_stop  # pragma: no cover
        if raw_mode:
            old_settings = _raw_mode_enter()
            tty.setraw(sys.stdin)
        try:
            await _run_shell(
                ws,
                cols,
                rows,
                ssh_agent_sock=local_agent_sock if ssh_agent_active else None,
            )
        finally:
            if raw_mode:
                _raw_mode_exit(old_settings)
                reset_terminal()
            # Drain any terminal query responses still buffered in stdin
            # so they don't leak to the host shell after exit.
            _drain_stdin()
            if ssh_agent_active:
                await _send_ignore_closed(
                    ws, json.dumps({"cmd": "ssh_agent_stop"})
                )
            await _send_ignore_closed(ws, json.dumps({"cmd": "terminal_stop"}))


async def _run_shell(
    ws,
    cols: int,
    rows: int,
    stdin: io.RawIOBase | None = None,
    stdout: io.TextIOBase | None = None,
    ssh_agent_sock: str | None = None,
) -> None:
    """Run stdin/stdout forwarding loop with SIGWINCH support.

    ssh_agent_sock: path to the local SSH agent socket. When set, an
    additional relay loop forwards SSH agent protocol messages between
    the container and the local agent.
    stdin/stdout default to sys.stdin.buffer / sys.stdout when None.
    Pass explicit streams in tests to avoid mutating globals.
    """
    _debug_agent = os.environ.get("KLANGKC_DEBUG_SSH_AGENT", "")
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
        # SSH-style escape: after Enter (or at start), ~ then . disconnects.
        after_newline = True
        saw_tilde = False
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
                # Escape sequence: ~. after Enter disconnects (like SSH)
                if saw_tilde:
                    saw_tilde = False
                    if data == b".":
                        stdout.write("\r\nDisconnected.\r\n")
                        stdout.flush()
                        stop_event.set()
                        # Close the WS so stdout_loop's recv() unblocks.
                        await ws.close()
                        return
                    # Not a disconnect — send the buffered ~ and this byte
                    await ws.send(
                        json.dumps({"cmd": "terminal_input", "data": "~"})
                    )
                    # Fall through to send current byte normally
                if data == b"~" and after_newline:
                    saw_tilde = True
                    after_newline = False
                    continue
                after_newline = data in (b"\r", b"\n")
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
                    # Filter terminal query responses that the user's
                    # terminal sends in reply to tmux's capability probes.
                    # These arrive on stdin but are not user input — sending
                    # them as terminal_input causes tmux to echo them as
                    # visible garbage.  If we detect a response prefix,
                    # drain any remaining bytes (the payload may arrive
                    # in a subsequent chunk).
                    if _is_terminal_response(data):
                        # Drain trailing response bytes that may still
                        # be in the buffer (payload + string terminator).
                        for _ in range(10):
                            if not select.select([fd], [], [], 0.02)[0]:
                                break
                            try:
                                await loop.run_in_executor(
                                    None, os.read, fd, 256
                                )
                            except OSError:  # pragma: no cover
                                break
                        continue
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

    # Queue for SSH agent response messages (filled by stdout_loop,
    # consumed by ssh_agent_relay_loop).
    agent_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def stdout_loop() -> None:
        try:
            while not stop_event.is_set():
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="replace")
                data = json.loads(msg)
                if data.get("type") == "terminal_output":
                    text = data["data"]
                    stdout.write(text)
                    stdout.flush()
                    if "[exited]" in text:
                        stdout.write(
                            "\r\nPress Enter, then ~. to disconnect.\r\n"
                        )
                        stdout.flush()
                elif data.get("type") == "ssh_agent_response":
                    raw = base64.b64decode(data.get("data", ""))
                    if raw:
                        if _debug_agent:  # pragma: no cover
                            logger.info(
                                "[ssh-agent] got %d bytes from backend",
                                len(raw),
                            )
                        await agent_queue.put(raw)
                elif data.get("type") == "event":
                    event = data.get("event", {})
                    if (
                        event.get("type") == "CUSTOM"
                        and event.get("name") == "container_stopped"
                    ):
                        logging.info("[container stopped]")
                        stop_event.set()
                        break
        except websockets.ConnectionClosed as exc:
            if not stop_event.is_set():
                _code = exc.rcvd.code if exc.rcvd else None
                if _code in (4001, 4002):
                    stdout.write(
                        "\r\nSession expired. Run `klangkc login` to"
                        " re-authenticate.\r\n"
                    )
                else:
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
        elapsed = 0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
                return
            except asyncio.TimeoutError:
                pass
            elapsed += 1
            if elapsed >= 60 and not stop_event.is_set():
                elapsed = 0
                await ws.send(json.dumps({"cmd": "heartbeat"}))

    async def ssh_agent_relay_loop() -> None:
        """Relay SSH agent protocol between container and local agent.

        Reads ssh_agent_response messages from the queue (put there by
        stdout_loop), forwards them to the local SSH agent socket, reads
        the agent's reply, and sends it back over the WebSocket.
        """
        if not ssh_agent_sock:  # pragma: no cover
            return
        if _debug_agent:  # pragma: no cover
            logger.info(
                "[ssh-agent] relay loop started, local sock=%s",
                ssh_agent_sock,
            )
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(agent_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if _debug_agent:  # pragma: no cover
                logger.info(
                    "[ssh-agent] relay: got %d bytes from queue", len(data)
                )
            try:
                # Connect to local agent, forward data, read response.
                agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    agent.connect(ssh_agent_sock)
                    if _debug_agent:  # pragma: no cover
                        logger.info(
                            "[ssh-agent] relay: connected to local agent"
                        )
                    agent.sendall(data)
                    if _debug_agent:  # pragma: no cover
                        logger.info(
                            "[ssh-agent] relay: sent %d bytes to local agent",
                            len(data),
                        )
                    # SSH agent protocol: 4-byte big-endian length prefix
                    # followed by the message body.
                    header = b""
                    while len(header) < 4:
                        chunk = agent.recv(4 - len(header))
                        if not chunk:  # pragma: no cover
                            break
                        header += chunk
                    if len(header) == 4:
                        msg_len = struct.unpack(">I", header)[0]
                        if _debug_agent:  # pragma: no cover
                            logger.info(
                                "[ssh-agent] relay: agent response header, "
                                "body_len=%d",
                                msg_len,
                            )
                        body = b""
                        while len(body) < msg_len:
                            chunk = agent.recv(msg_len - len(body))
                            if not chunk:  # pragma: no cover
                                break
                            body += chunk
                        response = header + body
                        if _debug_agent:  # pragma: no cover
                            logger.info(
                                "[ssh-agent] relay: sending %d bytes back "
                                "to backend",
                                len(response),
                            )
                        await ws.send(
                            json.dumps(
                                {
                                    "cmd": "ssh_agent_data",
                                    "data": base64.b64encode(response).decode(
                                        "ascii"
                                    ),
                                }
                            )
                        )
                    elif _debug_agent:  # pragma: no cover
                        logger.info(
                            "[ssh-agent] relay: incomplete header (%d bytes)",
                            len(header),
                        )
                finally:
                    agent.close()
            except (OSError, ConnectionError) as e:
                logger.warning("SSH agent relay: %s", e)

    tasks = [stdin_loop(), stdout_loop(), resize_loop(), heartbeat_loop()]
    if ssh_agent_sock:
        tasks.append(ssh_agent_relay_loop())
    await asyncio.gather(*tasks)


async def _exec_on_ws(
    ws,
    command: list[str],
    stdin: io.RawIOBase | None = None,
    stdout: io.RawIOBase | None = None,
) -> int:
    """Run a command on an already-connected WebSocket.

    The caller must have already connected and called
    ``_wait_workspace_ready``.

    *stdin*: file-like to read input from.  ``None`` closes stdin
    immediately.  For a real terminal pass ``sys.stdin.buffer``; for
    programmatic use pass ``io.BytesIO(data)``.

    *stdout*: file-like to write output to.  ``None`` discards output.
    For a real terminal pass ``sys.stdout.buffer``; for capture pass
    an ``io.BytesIO()``.

    Returns the remote process exit code.
    """
    loop = asyncio.get_event_loop()

    await ws.send(json.dumps({"cmd": "exec_start", "command": command}))

    exit_code = 1
    stop = asyncio.Event()

    async def stdin_forward() -> None:
        if stdin is None:
            await ws.send(json.dumps({"cmd": "exec_close_stdin"}))
            return
        try:
            fd = stdin.fileno()
            has_fd = True
        except (io.UnsupportedOperation, AttributeError):
            has_fd = False

        if has_fd:
            while not stop.is_set():
                ready = await loop.run_in_executor(
                    None,
                    lambda: select.select([fd], [], [], 0.2)[0],
                )
                if not ready:  # pragma: no cover
                    continue
                data = await loop.run_in_executor(None, os.read, fd, 65536)
                if not data:
                    break
                await ws.send(  # pragma: no cover
                    json.dumps(
                        {
                            "cmd": "exec_input",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    )
                )
        else:
            data = stdin.read()
            if data:
                await ws.send(
                    json.dumps(
                        {
                            "cmd": "exec_input",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    )
                )
        await ws.send(json.dumps({"cmd": "exec_close_stdin"}))

    async def stdout_forward() -> None:
        nonlocal exit_code
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):  # pragma: no cover
                msg = msg.decode("utf-8", errors="replace")
            data = json.loads(msg)
            if data.get("type") == "exec_output":
                raw = base64.b64decode(data["data"])
                if stdout is not None:
                    if has_stdout_fd:
                        # Use os.write for real fds to avoid buffering
                        # issues (rsync needs unbuffered output).
                        await loop.run_in_executor(
                            None, os.write, stdout_fd, raw
                        )
                    else:
                        stdout.write(raw)
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

    stdout_fd = -1
    try:
        if stdout is not None:
            stdout_fd = stdout.fileno()
    except (io.UnsupportedOperation, AttributeError):
        pass
    has_stdout_fd = stdout_fd >= 0

    stdout_task = asyncio.create_task(stdout_forward())
    stdin_task = asyncio.create_task(stdin_forward())
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    await stdout_task
    stop.set()
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


async def _ws_exec(
    ws_url: str,
    token: str,
    workspace_id: str,
    command: list[str],
    max_size: int = _WS_MAX_SIZE,
) -> int:
    """Run a command interactively, piping real stdin/stdout.

    Returns the remote process exit code.
    """
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=max_size
    ) as ws:
        await _wait_workspace_ready(ws, workspace_id)
        return await _exec_on_ws(
            ws, command, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer
        )


async def _ws_exec_piped(
    ws_url: str,
    token: str,
    workspace_id: str,
    command: list[str],
    stdin_data: bytes | None = None,
    max_size: int = _WS_MAX_SIZE,
) -> tuple[int, str]:
    """Run a command, optionally piping *stdin_data*, capture stdout.

    Returns ``(exit_code, stdout_text)``.  Does not touch real
    stdin/stdout — designed for programmatic use (file copy, setup).
    """
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=max_size
    ) as ws:
        await _wait_workspace_ready(ws, workspace_id)
        stdin_buf = io.BytesIO(stdin_data) if stdin_data else None
        stdout_buf = io.BytesIO()
        exit_code = await _exec_on_ws(
            ws, command, stdin=stdin_buf, stdout=stdout_buf
        )
        return (
            exit_code,
            stdout_buf.getvalue().decode("utf-8", errors="replace"),
        )
