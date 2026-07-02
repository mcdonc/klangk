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

from .auth import refresh_token as _refresh_token


_WS_MAX_SIZE = int(os.environ.get("KLANGK_WS_MSG_SIZE_MAX", 2**24))

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 2.0  # seconds, doubled each retry

_WS_CONNECT_TIMEOUT = 60  # seconds to wait for container_ready


def _query_local_ssh_agent(sock_path: str, data: bytes) -> bytes | None:
    """Send *data* to the local SSH agent and return its response.

    Connects to the Unix socket at *sock_path*, writes *data*, then
    reads one SSH agent protocol message (4-byte big-endian length
    prefix followed by the message body).  Returns the full response
    (header + body) or ``None`` on failure.
    """
    agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        agent.connect(sock_path)
        agent.sendall(data)
        header = b""
        while len(header) < 4:
            chunk = agent.recv(4 - len(header))
            if not chunk:  # pragma: no cover
                break
            header += chunk
        if len(header) < 4:  # pragma: no cover
            return None
        msg_len = struct.unpack(">I", header)[0]
        body = b""
        while len(body) < msg_len:
            chunk = agent.recv(msg_len - len(body))
            if not chunk:  # pragma: no cover
                break
            body += chunk
        return header + body
    finally:
        agent.close()


async def _wait_container_ready(
    ws: websockets.ClientConnection,
    workspace_id: str,
    timeout: float = _WS_CONNECT_TIMEOUT,
) -> dict:
    """Send workspace_connect and wait for container_ready, skipping broadcasts.

    The server may send broadcast messages (e.g. presence_list from eager
    agent startup) before container_ready.  This drains them rather than
    treating the first non-ready message as an error.

    Returns the container_ready payload.
    """
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("Timed out waiting for container_ready")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        resp = json.loads(raw)
        if resp.get("type") == "container_ready":
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
    service_command: str | None = None
    auto_start: bool = False
    mounts: list[str] | None = None
    env: dict[str, str] | None = None
    health_check: str | None = None
    owner_email: str | None = None
    running: bool = False
    health: str | None = None
    health_message: str | None = None


def _get_terminal_size() -> tuple[int, int]:
    """Return (columns, rows) of the local terminal, or a sensible default."""
    if sys.stdin.isatty():
        size = os.get_terminal_size()
        return size.columns, size.lines
    return 80, 24


_REFRESH_MARGIN_SECONDS = 300  # refresh 5 minutes before expiry


def _token_expires_soon(token: str) -> bool:
    """Return True if *token* expires within ``_REFRESH_MARGIN_SECONDS``.

    Decodes the JWT payload without verifying the signature (no secret
    needed) and compares the ``exp`` claim against the current time.
    Returns ``False`` on any decode failure so callers fall through to
    the normal request path.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if exp is None:
            return False
        return _time.time() >= (exp - _REFRESH_MARGIN_SECONDS)
    except Exception:
        return False


class KlangkClient:
    def __init__(self, server_url: str, token: str | None = None):
        self.server_url = server_url
        self.token = token
        self._refreshed = False  # guard against infinite retry loops

    # --- HTTP helpers ---

    def _try_refresh(self) -> bool:
        """Attempt to refresh the current token.

        On success, updates ``self.token`` and returns ``True``.
        """
        if not self.token:
            return False
        new_token = _refresh_token(self.server_url, self.token)
        if new_token:
            self.token = new_token
            return True
        return False

    def _headers(self) -> dict[str, str]:
        if self.token and _token_expires_soon(self.token):
            self._try_refresh()
        token = self.token or ""
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = _request_with_retry(
            method,
            f"{self.server_url}{path}",
            headers=self._headers(),
            **kwargs,
        )
        if resp.status_code == 401 and not self._refreshed:
            self._refreshed = True
            if self._try_refresh():
                resp = _request_with_retry(
                    method,
                    f"{self.server_url}{path}",
                    headers=self._headers(),
                    **kwargs,
                )
        return resp

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> httpx.Response:
        return self._request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs) -> httpx.Response:
        return self._request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self._request("DELETE", path, **kwargs)

    # --- REST API ---

    def _check_auth(self, resp: httpx.Response) -> None:
        """Raise AuthError if the server returned 401."""
        if resp.status_code == 401:
            raise AuthError("Session expired — run `klangkc login`")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Like ``resp.raise_for_status()`` but includes the server's
        error detail in the exception message when available."""
        if 200 <= resp.status_code < 300:
            return
        detail = ""
        try:
            body = resp.json()
            detail = body.get("detail", "")
        except Exception:
            pass
        if detail:
            raise httpx.HTTPStatusError(
                f"{resp.status_code}: {detail}",
                request=resp.request,
                response=resp,
            )
        resp.raise_for_status()

    def get_handle(self) -> str:
        """Return the current user's handle via ``GET /auth/me``."""
        resp = self.get("/api/v1/auth/me")
        self._check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()["handle"]

    def list_workspaces(
        self,
        limit: int = 10,
        offset: int = 0,
        all_pages: bool = False,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> list[Workspace]:
        """List workspaces owned by the current user.

        By default returns a single page (10 items). Pass ``all_pages=True``
        to page through every workspace. ``sort`` (``created``/``name``),
        ``order`` (``asc``/``desc``) and ``q`` (name substring) mirror the
        API query params.
        """
        return self._list_paginated(
            "/api/v1/workspaces",
            limit=limit,
            offset=offset,
            all_pages=all_pages,
            shared=False,
            sort=sort,
            order=order,
            q=q,
        )

    def list_shared_workspaces(
        self,
        limit: int = 10,
        offset: int = 0,
        all_pages: bool = False,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> list[Workspace]:
        """List workspaces shared with the current user."""
        return self._list_paginated(
            "/api/v1/workspaces/shared",
            limit=limit,
            offset=offset,
            all_pages=all_pages,
            shared=True,
            sort=sort,
            order=order,
            q=q,
        )

    def _list_paginated(
        self,
        path: str,
        *,
        limit: int,
        offset: int,
        all_pages: bool,
        shared: bool,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> list[Workspace]:
        workspaces: list[Workspace] = []
        params: dict = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        while True:
            resp = self.get(path, params=params)
            self._check_auth(resp)
            self._raise_for_status(resp)
            body = resp.json()
            for w in body["items"]:
                workspaces.append(self._workspace_from_json(w, shared=shared))
            if not all_pages or not body.get("has_more"):
                break
            params["offset"] = body["next_offset"]
        return workspaces

    @staticmethod
    def _workspace_from_json(w: dict, *, shared: bool) -> Workspace:
        return Workspace(
            id=w["id"],
            name=w["name"],
            created_at=w["created_at"],
            image=w.get("image"),
            service_command=w.get("service_command"),
            auto_start=bool(w.get("auto_start", False)),
            mounts=w.get("mounts"),
            env=w.get("env"),
            health_check=w.get("health_check"),
            owner_email=w.get("owner_email") if shared else None,
            running=bool(w.get("running", False)),
            health=w.get("health"),
            health_message=w.get("health_message"),
        )

    def create_workspace(
        self,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_state: str | None = None,
        health_check: str | None = None,
    ) -> Workspace:
        body: dict = {"name": name}
        if image:
            body["image"] = image
        if service_command:
            body["service_command"] = service_command
        if auto_start:
            body["auto_start"] = True
        if mounts:
            body["mounts"] = mounts
        if env:
            body["env"] = env
        if setup_state:
            body["setup_state"] = setup_state
        if health_check:
            body["health_check"] = health_check
        resp = self.post("/api/v1/workspaces", json=body)
        self._check_auth(resp)
        self._raise_for_status(resp)
        w = resp.json()
        return Workspace(
            id=w["id"], name=w["name"], created_at=w["created_at"]
        )

    def set_setup_state(self, workspace_id: str, setup_state: str) -> None:
        """Update a workspace's setup_state lifecycle field (#1033).

        Used by the sandbox driver to mark pending before running
        setup.sh and complete/failed after it returns. Safe to call
        from an async context via ``asyncio.to_thread``.
        """
        resp = self.put(
            f"/api/v1/workspaces/{workspace_id}",
            json={"setup_state": setup_state},
        )
        self._check_auth(resp)
        self._raise_for_status(resp)

    def list_images(self) -> dict:
        resp = self.get("/api/v1/images")
        self._check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    def resolve_workspace(self, name: str) -> Workspace:
        """Find a workspace by name (owned or shared).

        Raises WorkspaceNotFoundError if not found.
        """
        all_ws = self.list_workspaces(
            all_pages=True
        ) + self.list_shared_workspaces(all_pages=True)
        match = next((w for w in all_ws if w.name == name), None)
        if match is None:
            raise WorkspaceNotFoundError(name)
        return match

    def delete_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.delete(f"/api/v1/workspaces/{ws.id}")
        self._check_auth(resp)
        self._raise_for_status(resp)

    def list_workspace_members(self, name: str) -> list[dict]:
        ws = self.resolve_workspace(name)
        resp = self.get(f"/api/v1/workspaces/{ws.id}/members")
        self._check_auth(resp)
        self._raise_for_status(resp)
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
        self._raise_for_status(resp)
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
        self._raise_for_status(resp)

    def restart_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.post(f"/api/v1/workspaces/{ws.id}/restart")
        self._check_auth(resp)
        self._raise_for_status(resp)

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
                self._raise_for_status(resp)
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
        self._raise_for_status(resp)
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
    window: str | None = None,
    forward_agent: bool = False,
    sandbox_setup=None,
    max_size: int = _WS_MAX_SIZE,
) -> None:
    """Run the interactive PTY shell over WebSocket.

    raw_mode controls whether stdin is placed in raw (cbreak) mode.
    Pass False in tests or when stdin is not a real terminal.
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
        await _wait_container_ready(ws, workspace_id)

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
        await ws.send(
            json.dumps(
                {
                    "cmd": "terminal_start",
                    "cols": cols,
                    "rows": rows,
                    "browser_id": "klangkshell",
                }
            )
        )

        # 3. Drain messages until we have what window selection needs.
        # terminal_output may arrive before terminal_windows due to async
        # output forwarding, so we buffer early output and don't stop until
        # the window list is in. When joining a shared terminal
        # (``handle:window_name``) we must ALSO wait for shared_terminals,
        # which the server sends AFTER terminal_windows (see #1208):
        # breaking on terminal_windows alone leaves shared_terminals empty
        # and the join fails with "Shared terminal not found".
        own_windows: list[dict] = []
        shared_terminals: list[dict] = []
        buffered_output: list[str] = []
        needs_shared = window is not None and ":" in window
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
                    if not needs_shared:
                        break
                elif msg.get("type") == "shared_terminals":
                    shared_terminals = msg.get("terminals", [])
                    if needs_shared:
                        break
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
                            "window_id": match["id"],
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
        # Derive the HTTP base URL from the WebSocket URL for token refresh.
        _http_url = ws_url.replace("wss://", "https://").replace(
            "ws://", "http://"
        )
        if _http_url.endswith("/ws"):
            _http_url = _http_url[:-3]
        try:
            await _run_shell(
                ws,
                cols,
                rows,
                ssh_agent_sock=local_agent_sock if ssh_agent_active else None,
                server_url=_http_url,
                token=token,
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


class _ShellSession:
    """Shared I/O pump infrastructure for terminal and exec sessions.

    Owns the WebSocket, stop event, heartbeat loop, and SSH agent relay
    that both ``_TerminalSession`` and ``_ExecSession`` need.
    """

    def __init__(self, ws, ssh_agent_sock: str | None = None):
        self.ws = ws
        self.ssh_agent_sock = ssh_agent_sock
        self.stop = asyncio.Event()
        self.agent_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._debug_agent = os.environ.get("KLANGKC_DEBUG_SSH_AGENT", "")

    async def heartbeat_loop(self) -> None:  # pragma: no cover
        """Send a heartbeat every 60 s until stopped."""
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                pass
            if not self.stop.is_set():
                await self.ws.send(json.dumps({"cmd": "heartbeat"}))

    def dispatch_agent_response(self, data: dict) -> None:
        """Enqueue an ssh_agent_response message for the relay loop."""
        raw = base64.b64decode(data.get("data", ""))
        if raw:
            if self._debug_agent:  # pragma: no cover
                logger.info("[ssh-agent] got %d bytes from backend", len(raw))
            self.agent_queue.put_nowait(raw)

    async def ssh_agent_relay_loop(self) -> None:
        """Relay SSH agent protocol between container and local agent.

        Reads ssh_agent_response messages from the queue (put there by
        the stdout loop), forwards them to the local SSH agent socket,
        reads the agent's reply, and sends it back over the WebSocket.
        """
        if not self.ssh_agent_sock:
            return
        if self._debug_agent:  # pragma: no cover
            logger.info(
                "[ssh-agent] relay loop started, local sock=%s",
                self.ssh_agent_sock,
            )
        while not self.stop.is_set():
            try:
                data = await asyncio.wait_for(
                    self.agent_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            if self._debug_agent:  # pragma: no cover
                logger.info(
                    "[ssh-agent] relay: got %d bytes from queue", len(data)
                )
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None, _query_local_ssh_agent, self.ssh_agent_sock, data
                )
                if response is not None:
                    if self._debug_agent:  # pragma: no cover
                        logger.info(
                            "[ssh-agent] relay: sending %d bytes back "
                            "to backend",
                            len(response),
                        )
                    await self.ws.send(
                        json.dumps(
                            {
                                "cmd": "ssh_agent_data",
                                "data": base64.b64encode(response).decode(
                                    "ascii"
                                ),
                            }
                        )
                    )
                elif self._debug_agent:  # pragma: no cover
                    logger.info("[ssh-agent] relay: no response from agent")
            except (OSError, ConnectionError) as e:
                logger.warning("SSH agent relay: %s", e)

    async def run(self) -> None:  # pragma: no cover
        raise NotImplementedError


class _TerminalSession(_ShellSession):
    """Interactive PTY-over-WebSocket I/O pump."""

    def __init__(
        self,
        ws,
        cols: int,
        rows: int,
        stdin: io.RawIOBase | None = None,
        stdout: io.TextIOBase | None = None,
        ssh_agent_sock: str | None = None,
        server_url: str | None = None,
        token: str | None = None,
    ):
        super().__init__(ws, ssh_agent_sock)
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout
        self._cols = cols
        self._rows = rows
        self._loop = asyncio.get_event_loop()
        self.server_url = server_url
        self.token = token

    async def _send_resize(self) -> None:
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "terminal_resize",
                    "cols": self._cols,
                    "rows": self._rows,
                }
            )
        )

    async def stdin_loop(self) -> None:
        fd = self.stdin.fileno()
        after_newline = True
        saw_tilde = False
        while not self.stop.is_set():
            ready, _, _ = await self._loop.run_in_executor(
                None, lambda: select.select([fd], [], [], 0.2)
            )
            if not ready:
                continue
            try:
                data = await self._loop.run_in_executor(None, os.read, fd, 1)
                if not data:
                    return
                if saw_tilde:
                    saw_tilde = False
                    if data == b".":
                        self.stdout.write("\r\nDisconnected.\r\n")
                        self.stdout.flush()
                        self.stop.set()
                        await self.ws.close()
                        return
                    await self.ws.send(
                        json.dumps({"cmd": "terminal_input", "data": "~"})
                    )
                if data == b"~" and after_newline:
                    saw_tilde = True
                    after_newline = False
                    continue
                after_newline = data in (b"\r", b"\n")
                if data == b"\x1b":
                    if select.select([fd], [], [], 0.05)[0]:
                        more = await self._loop.run_in_executor(
                            None, os.read, fd, 32
                        )
                        if more:
                            data += more
                    if _is_terminal_response(data):
                        for _ in range(10):
                            if not select.select([fd], [], [], 0.02)[0]:
                                break
                            try:
                                await self._loop.run_in_executor(
                                    None, os.read, fd, 256
                                )
                            except OSError:  # pragma: no cover
                                break
                        continue
            except (OSError, io.UnsupportedOperation):  # pragma: no cover
                return
            await self.ws.send(
                json.dumps(
                    {
                        "cmd": "terminal_input",
                        "data": data.decode("utf-8", errors="replace"),
                    }
                )
            )

    async def stdout_loop(self) -> None:
        try:
            while not self.stop.is_set():
                msg = await self.ws.recv()
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="replace")
                data = json.loads(msg)
                if data.get("type") == "terminal_output":
                    text = data["data"]
                    self.stdout.write(text)
                    self.stdout.flush()
                    if "[exited]" in text:
                        self.stdout.write(
                            "\r\nPress Enter, then ~. to disconnect.\r\n"
                        )
                        self.stdout.flush()
                elif data.get("type") == "ssh_agent_response":
                    self.dispatch_agent_response(data)
                elif data.get("type") == "event":
                    event = data.get("event", {})
                    if (
                        event.get("type") == "CUSTOM"
                        and event.get("name") == "container_stopped"
                    ):
                        logging.info("[container stopped]")
                        self.stop.set()
                        break
        except websockets.ConnectionClosed as exc:
            if not self.stop.is_set():
                _code = exc.rcvd.code if exc.rcvd else None
                if _code == 4002 and self.token:
                    new = _refresh_token(self.server_url, self.token)
                    if new:
                        self.stdout.write(
                            "\r\nSession refreshed."
                            " Run your command again to reconnect.\r\n"
                        )
                    else:
                        self.stdout.write(
                            "\r\nSession expired. Run `klangkc login`"
                            " to re-authenticate.\r\n"
                        )
                elif _code in (4001, 4002):
                    self.stdout.write(
                        "\r\nSession expired. Run `klangkc login` to"
                        " re-authenticate.\r\n"
                    )
                else:
                    self.stdout.write("\r\nServer disconnected.\r\n")
                self.stdout.flush()
        self.stop.set()

    async def resize_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
                return  # pragma: no cover
            except asyncio.TimeoutError:
                pass
            new_cols, new_rows = _get_terminal_size()
            if new_cols != self._cols or new_rows != self._rows:
                self._cols = new_cols
                self._rows = new_rows
                await self._send_resize()

    async def run(self) -> None:
        coros = [
            self.stdin_loop(),
            self.stdout_loop(),
            self.resize_loop(),
            self.heartbeat_loop(),
        ]
        if self.ssh_agent_sock:
            coros.append(self.ssh_agent_relay_loop())
        tasks = [asyncio.create_task(c) for c in coros]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class _ExecSession(_ShellSession):
    """Non-interactive command execution over WebSocket."""

    def __init__(
        self,
        ws,
        command: list[str],
        stdin: io.RawIOBase | None = None,
        stdout: io.RawIOBase | None = None,
        timeout: int | None = None,
        login: bool = True,
    ):
        local_sock = os.environ.get("SSH_AUTH_SOCK")
        sock = (
            local_sock if local_sock and os.path.exists(local_sock) else None
        )
        super().__init__(ws, sock)
        self.command = command
        self.stdin = stdin
        self.stdout = stdout
        self.timeout = timeout
        # ``login`` (default True): run the command as a bash login shell
        # so it sources ~/.profile, matching a terminal (#1041). Set
        # False for programmatic transports (rsync) that must not source
        # startup files.
        self.login = login
        self.exit_code = 1
        self._loop = asyncio.get_event_loop()
        self._stdout_fd = -1
        try:
            if self.stdout is not None:
                self._stdout_fd = self.stdout.fileno()
        except (io.UnsupportedOperation, AttributeError):
            pass
        self._has_stdout_fd = self._stdout_fd >= 0

    async def stdin_forward(self) -> None:
        if self.stdin is None:
            await self.ws.send(json.dumps({"cmd": "exec_close_stdin"}))
            return
        try:
            fd = self.stdin.fileno()
            has_fd = True
        except (io.UnsupportedOperation, AttributeError):
            has_fd = False

        if has_fd:
            while not self.stop.is_set():
                ready = await self._loop.run_in_executor(
                    None,
                    lambda: select.select([fd], [], [], 0.2)[0],
                )
                if not ready:  # pragma: no cover
                    continue
                data = await self._loop.run_in_executor(
                    None, os.read, fd, 65536
                )
                if not data:
                    break
                await self.ws.send(  # pragma: no cover
                    json.dumps(
                        {
                            "cmd": "exec_input",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    )
                )
        else:
            data = self.stdin.read()
            if data:
                await self.ws.send(
                    json.dumps(
                        {
                            "cmd": "exec_input",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    )
                )
        await self.ws.send(json.dumps({"cmd": "exec_close_stdin"}))

    async def stdout_forward(self) -> None:
        while True:
            msg = await self.ws.recv()
            if isinstance(msg, bytes):  # pragma: no cover
                msg = msg.decode("utf-8", errors="replace")
            data = json.loads(msg)
            if data.get("type") == "exec_output":
                raw = base64.b64decode(data["data"])
                if self.stdout is not None:
                    if self._has_stdout_fd:
                        await self._loop.run_in_executor(
                            None, os.write, self._stdout_fd, raw
                        )
                    else:
                        self.stdout.write(raw)
            elif data.get("type") == "ssh_agent_response":
                self.dispatch_agent_response(data)
            elif data.get("type") == "exec_exit":
                self.exit_code = data.get("code", 0)
                break
            elif data.get("type") == "error":  # pragma: no cover
                logging.error(
                    "Server error: %s",
                    data.get("message", "unknown"),
                )
                self.exit_code = 1
                break

    async def run(self) -> int:
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "exec_start",
                    "command": self.command,
                    "login": self.login,
                }
            )
        )

        stdout_task = asyncio.create_task(self.stdout_forward())
        stdin_task = asyncio.create_task(self.stdin_forward())
        heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        agent_task = asyncio.create_task(self.ssh_agent_relay_loop())
        try:
            if self.timeout is not None:
                await asyncio.wait_for(stdout_task, timeout=self.timeout)
            else:
                await stdout_task
        except asyncio.TimeoutError:
            self.exit_code = 124  # same as coreutils timeout(1)
            stdout_task.cancel()
            try:
                await stdout_task
            except asyncio.CancelledError:
                pass
        self.stop.set()
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
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass

        await _send_ignore_closed(self.ws, json.dumps({"cmd": "exec_stop"}))
        return self.exit_code


async def _run_shell(
    ws,
    cols: int,
    rows: int,
    stdin: io.RawIOBase | None = None,
    stdout: io.TextIOBase | None = None,
    ssh_agent_sock: str | None = None,
    server_url: str | None = None,
    token: str | None = None,
) -> None:
    """Run stdin/stdout forwarding loop with SIGWINCH support."""
    session = _TerminalSession(
        ws,
        cols,
        rows,
        stdin=stdin,
        stdout=stdout,
        ssh_agent_sock=ssh_agent_sock,
        server_url=server_url,
        token=token,
    )
    await session.run()


async def _exec_on_ws(
    ws,
    command: list[str],
    stdin: io.RawIOBase | None = None,
    stdout: io.RawIOBase | None = None,
    timeout: int | None = None,
    login: bool = False,
) -> int:
    """Run a command on an already-connected WebSocket.

    Returns the remote process exit code.  ``login`` defaults to False
    (raw argv) -- this is the low-level primitive used by setup/file-copy
    paths that already build their own ``sh -c`` command; the
    interactive ``klangkc exec`` entrypoint (_ws_exec) overrides it to
    True. See #1041.
    """
    session = _ExecSession(
        ws,
        command,
        stdin=stdin,
        stdout=stdout,
        timeout=timeout,
        login=login,
    )
    return await session.run()


async def _ws_exec(
    ws_url: str,
    token: str,
    workspace_id: str,
    command: list[str],
    max_size: int = _WS_MAX_SIZE,
    login: bool = True,
) -> int:
    """Run a command interactively, piping real stdin/stdout.

    Returns the remote process exit code.  Defaults to ``login=True``
    (run as a bash login shell so ~/.profile is sourced, like a
    terminal -- #1041); ``klangkc exec --raw`` and the rsync transport
    pass False for raw argv.
    """
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=max_size
    ) as ws:
        await _wait_container_ready(ws, workspace_id)
        return await _exec_on_ws(
            ws,
            command,
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            login=login,
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
        await _wait_container_ready(ws, workspace_id)
        stdin_buf = io.BytesIO(stdin_data) if stdin_data else None
        stdout_buf = io.BytesIO()
        exit_code = await _exec_on_ws(
            ws, command, stdin=stdin_buf, stdout=stdout_buf
        )
        return (
            exit_code,
            stdout_buf.getvalue().decode("utf-8", errors="replace"),
        )
