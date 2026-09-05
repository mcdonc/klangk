"""HTTP + WebSocket client for the Klangk backend."""

from __future__ import annotations


import asyncio
from contextlib import asynccontextmanager
import base64
import io
import json
import logging
import os
from pathlib import Path
import re
import select
import socket
from collections.abc import Callable
import struct
import sys
import termios
import tty
from dataclasses import dataclass

import time as _time

import httpx
import websockets

from .auth import fetch_config as _fetch_config
from .auth import local_login as _local_login
from .auth import refresh_token as refresh_token
from .transport import http_request, http_stream, ws_connect


def server_mode_is_none(server_url: str) -> bool:
    """True if the server's live auth mode is ``none`` (no-login).

    Probes ``/config`` on every call rather than trusting a cache: a mode
    switch (none <-> password/oidc) must take effect immediately, and the
    probe is one cheap GET only on a refresh-failure path, not every
    request (#1374). Returns False on any probe failure so non-none or
    unreachable servers keep their normal refresh-error behavior.
    """
    config = _fetch_config(server_url)
    return isinstance(config, dict) and config.get("auth_modes") == "none"


_WS_MAX_SIZE = int(os.environ.get("KLANGK_WEBSOCKET_MSG_SIZE_MAX", 2**24))

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
RESIZE_POLL_INTERVAL = 1.0  # seconds between terminal size checks
_RETRY_BACKOFF = 2.0  # seconds, doubled each retry

# Seconds to wait for container_ready. The wait spans the server's whole
# bring-up chain (create → start → readiness), so under load it can
# legitimately outrun 60s; overridable via env for the e2e runs that
# widen the chain's budgets on CI (#3064).
_WS_CONNECT_TIMEOUT = float(os.environ.get("KLANGKC_WS_CONNECT_TIMEOUT", "60"))
_HEARTBEAT_INTERVAL = 60  # seconds between terminal heartbeats
_STDIN_DRAIN_TIMEOUT = 2  # seconds to let exec stdin forwarder finish


def recv_exact(agent, target: int) -> bytes:
    """Read exactly *target* bytes (short when the agent closes early)."""
    buf = b""
    while len(buf) < target:
        chunk = agent.recv(target - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def query_local_ssh_agent(sock_path: str, data: bytes) -> bytes | None:
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
        header = recv_exact(agent, 4)
        if len(header) < 4:
            return None
        msg_len = struct.unpack(">I", header)[0]
        body = recv_exact(agent, msg_len)
        return header + body
    finally:
        agent.close()


async def wait_container_ready(
    ws: websockets.ClientConnection,
    workspace_id: str,
    timeout: float = _WS_CONNECT_TIMEOUT,
) -> dict:
    """Send workspace_connect and wait for container_ready, skipping broadcasts.

    The server may send broadcast messages before container_ready.
    This drains them rather than treating the first non-ready message
    as an error.

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


def transient_status(resp, attempt: int) -> bool:
    """True when a 502/503/504 response should be retried."""
    return (
        resp.status_code in (502, 503, 504) and attempt < _RETRY_ATTEMPTS - 1
    )


def sleep_backoff(backoff: float) -> float:
    """Sleep the backoff and return the next (doubled) one."""
    _time.sleep(backoff)
    return backoff * 2


def request_with_retry(
    server_spec: str,
    method: str,
    path: str,
    *,
    timeout: float = 60.0,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with retry on transient failures.

    Retries on ReadTimeout, ConnectTimeout, ConnectError, and 502/503/504
    responses with exponential backoff.
    """
    backoff = _RETRY_BACKOFF
    # Every iteration ends in return/raise/continue (the last attempt
    # returns or re-raises), so the loop never falls through its range:
    # the arc to loop exit is unreachable.
    for attempt in range(_RETRY_ATTEMPTS):  # pragma: no branch
        try:
            resp = http_request(
                server_spec, method, path, timeout=timeout, **kwargs
            )
            if transient_status(resp, attempt):
                logger.debug(
                    "HTTP %s %s returned %d, retrying in %.1fs",
                    method,
                    path,
                    resp.status_code,
                    backoff,
                )
                backoff = sleep_backoff(backoff)
                continue
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < _RETRY_ATTEMPTS - 1:
                logger.debug(
                    "HTTP %s %s failed (%s), retrying in %.1fs",
                    method,
                    path,
                    exc,
                    backoff,
                )
                backoff = sleep_backoff(backoff)
            else:
                raise


def download_total(resp) -> int | None:
    """Content-Length when present, else the server's size estimate."""
    if "content-length" in resp.headers:
        return int(resp.headers["content-length"])
    if "x-estimated-size" in resp.headers:
        return int(resp.headers["x-estimated-size"])
    return None


def write_chunks(resp, output: Path, on_progress, total: int | None) -> None:
    """Stream the response body to *output*, reporting progress."""
    downloaded = 0
    with open(output, "wb") as f:
        for chunk in resp.iter_bytes():
            f.write(chunk)
            downloaded += len(chunk)
            if on_progress:
                on_progress(downloaded, total)


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
    allowed_domains: list[str] | None = None
    rejected_domains: list[str] | None = None
    egress_mode: str | None = None
    # Home layout (#2169): True = each member gets a private
    # /home/.users/{id} home (via a /home/{handle} symlink); False = all
    # members share /home/klangk. Server default is True.
    per_handle_home: bool = True
    # Classification marking rendered as the persistent banner (#2768).
    # None/empty = inherit the deploy default
    # (KLANGKD_CLASSIFICATION_BANNER), resolved at display time.
    classification_banner: str | None = None
    owner_email: str | None = None
    running: bool = False
    health: str | None = None
    health_message: str | None = None
    service_started_at: float | None = None
    settings: dict | None = None


def get_terminal_size() -> tuple[int, int]:
    """Return (columns, rows) of the local terminal, or a sensible default."""
    if sys.stdin.isatty():
        size = os.get_terminal_size()
        return size.columns, size.lines
    return 80, 24


_REFRESH_MARGIN_SECONDS = 300  # refresh 5 minutes before expiry


def decode_token_claims(token: str) -> dict:
    """Decode a JWT's payload without verifying the signature.

    The CLI already trusts the token it holds (it came from a successful
    login), so signature verification adds nothing for read-only local
    use like reading the ``sub`` (user id) claim in ``klangk status``.
    Returns ``{}`` on any decode failure.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def token_expires_soon(token: str) -> bool:
    """Return True if *token* expires within ``_REFRESH_MARGIN_SECONDS``.

    Decodes the JWT payload without verifying the signature (no secret
    needed) and compares the ``exp`` claim against the current time.
    Returns ``False`` on any decode failure so callers fall through to
    the normal request path.
    """
    exp = decode_token_claims(token).get("exp")
    if exp is None:
        return False
    return _time.time() >= (exp - _REFRESH_MARGIN_SECONDS)


# The create-body field names accepted by create_workspace. The cli/
# isolation rule forbids importing the server's CreateWorkspaceRequest
# model, so the names are pinned here and checked on entry: pydantic
# ignores unknown keys, so a typo'd kwarg must fail client-side instead
# of silently dropping the field (#3048 review).
CREATE_FIELDS = frozenset(
    {
        "image",
        "service_command",
        "auto_start",
        "mounts",
        "env",
        "setup_state",
        "health_check",
        "allowed_domains",
        "egress_mode",
        "rejected_domains",
        "settings",
        "per_handle_home",
        "classification_banner",
    }
)


class KlangkClient:
    def __init__(
        self,
        server_url: str,
        token: str | None = None,
        step_up_prompt: Callable[[], str | None] | None = None,
    ):
        self.server_url = server_url
        self.token = token
        self._refreshed = False  # guard against infinite retry loops
        # #3196: sudo-mode support. When a privileged write is refused
        # with the server's machine-readable ``step_up_required`` 403,
        # this callback collects the user's password (or None to
        # cancel), the client confirms it via POST /auth/step-up, and
        # the original request is retried once. Interactive commands
        # wire a password prompt (cli.context.client); None (the
        # default) surfaces the server's error detail unchanged.
        self.step_up_prompt = step_up_prompt

    # --- HTTP helpers ---

    def _try_refresh(self) -> bool:
        """Attempt to refresh the current token.

        On success, updates ``self.token`` and returns ``True``.
        On refresh failure, if the server is in ``none`` (no-auth) mode,
        re-login is free (``/auth/local``), so retry that before giving up
        (#1374). The mode is probed live (not cached) so a recent mode
        switch takes effect immediately.
        """
        if not self.token:
            return False
        new_token = refresh_token(self.server_url, self.token)
        if new_token:
            self.token = new_token
            return True
        if server_mode_is_none(self.server_url):
            try:
                _email, token = _local_login(self.server_url)
            except SystemExit:
                return False
            self.token = token
            return True
        return False

    def _headers(self) -> dict[str, str]:
        if self.token and token_expires_soon(self.token):
            self._try_refresh()
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    @staticmethod
    def _is_step_up_required(resp: httpx.Response) -> bool:
        """True when *resp* is the machine-readable step-up 403 (#3196)."""
        if resp.status_code != 403:
            return False
        try:
            detail = resp.json().get("detail")
        except ValueError:
            return False
        return isinstance(detail, dict) and detail.get("error") == (
            "step_up_required"
        )

    def _confirm_step_up(self) -> bool:
        """Prompt for the password and confirm it with the server.

        Returns True when the server stamped the confirmation (the
        caller should retry its request). A cancelled prompt, a wrong
        password, or a server with the window disabled leaves the
        original 403 to surface via the caller's error handling.
        """
        if self.step_up_prompt is None:
            return False
        password = self.step_up_prompt()
        if not password:
            return False
        resp = request_with_retry(
            self.server_url,
            "POST",
            "/api/v1/auth/step-up",
            headers=self._headers(),
            json={"password": password},
        )
        return resp.status_code == 200

    def _step_up_retry(
        self, method: str, path: str, resp: httpx.Response, **kwargs
    ) -> httpx.Response:
        """Retry *resp*'s request once after a successful step-up.

        Returns the refusing response unchanged when it is not the
        step-up 403, the prompt was cancelled, or the confirmation
        failed — the caller's error handling then surfaces it.
        """
        if self._is_step_up_required(resp) and self._confirm_step_up():
            return request_with_retry(
                self.server_url,
                method,
                path,
                headers=self._headers(),
                **kwargs,
            )
        return resp

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        def send() -> httpx.Response:
            return request_with_retry(
                self.server_url,
                method,
                path,
                headers=self._headers(),
                **kwargs,
            )

        resp = send()
        if resp.status_code == 401 and not self._refreshed:
            self._refreshed = True
            if self._try_refresh():
                resp = send()
        return self._step_up_retry(method, path, resp, **kwargs)

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    # --- REST API ---

    def check_auth(self, resp: httpx.Response) -> None:
        """Raise AuthError if the server returned 401."""
        if resp.status_code == 401:
            raise AuthError("Session expired — run `klangk login`")

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
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()["handle"]

    def get_me(self) -> dict:
        """Return the current user's profile via ``GET /auth/me``.

        A dict with ``id``, ``email`` and ``handle``. Unlike the
        ``change_*`` methods, a 401 here genuinely means the session has
        expired, so ``check_auth`` maps it to the friendly
        "Session expired" error (mirroring ``get_handle``).
        """
        resp = self.get("/api/v1/auth/me")
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    def change_password(
        self, current_password: str, new_password: str
    ) -> None:
        """Change the current user's password via ``POST /auth/change-password``.

        Note: ``check_auth`` is intentionally skipped — this endpoint returns
        401 for a wrong current password, and we want the server's ``detail``
        ("Current password is incorrect") to surface rather than the generic
        session-expired message.
        """
        resp = self.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": current_password,
                "new_password": new_password,
            },
        )
        self._raise_for_status(resp)

    def change_email(self, email: str, password: str) -> None:
        """Change the current user's email via ``POST /auth/change-email``."""
        resp = self.post(
            "/api/v1/auth/change-email",
            json={"email": email, "password": password},
        )
        self._raise_for_status(resp)

    def change_handle(self, handle: str, password: str) -> str:
        """Change the current user's handle via ``POST /auth/change-handle``.

        Returns the handle the server accepted.
        """
        resp = self.post(
            "/api/v1/auth/change-handle",
            json={"handle": handle, "password": password},
        )
        self._raise_for_status(resp)
        return resp.json().get("handle", handle)

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

    def _fetch_page(self, path: str, params: dict) -> dict:
        """One authenticated page of the paginated list endpoint."""
        resp = self.get(path, params=params)
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    def _accumulate_pages(
        self, path: str, params: dict, all_pages: bool, shared: bool
    ) -> list[Workspace]:
        """Fetch one page (or follow pagination to the end)."""
        workspaces: list[Workspace] = []
        while True:
            body = self._fetch_page(path, params)
            for w in body["items"]:
                workspaces.append(self._workspace_from_json(w, shared=shared))
            if not all_pages or not body.get("has_more"):
                return workspaces
            params["offset"] = body["next_offset"]

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
        params: dict = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "order": order,
        }
        if q:
            params["q"] = q
        return self._accumulate_pages(path, params, all_pages, shared)

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
            allowed_domains=w.get("allowed_domains"),
            rejected_domains=w.get("rejected_domains"),
            egress_mode=w.get("egress_mode"),
            per_handle_home=bool(w.get("per_handle_home", True)),
            classification_banner=w.get("classification_banner"),
            service_started_at=w.get("service_started_at"),
            settings=w.get("settings"),
        )

    def create_workspace(self, name: str, **optional) -> Workspace:
        """Create a workspace (``POST /api/v1/workspaces``).

        Keyword fields are restricted to ``CREATE_FIELDS`` (they mirror
        the server's ``CreateWorkspaceRequest`` body schema; the
        ``klangk.cli`` isolation rule forbids importing the model).
        Unknown keys raise ``TypeError`` — the server would silently
        drop them. Falsy values are omitted — the server applies its
        defaults — except ``per_handle_home``, where ``None`` means
        "not chosen" and ``False`` is sent. Empty/None
        ``classification_banner`` inherits the deploy default marking
        (KLANGKD_CLASSIFICATION_BANNER); only a non-empty label sets the
        per-workspace override (#2768).
        """
        unknown = optional.keys() - CREATE_FIELDS
        if unknown:
            raise TypeError(
                "create_workspace() got unexpected keyword argument(s):"
                f" {', '.join(sorted(unknown))}"
            )
        body: dict = {"name": name}
        for key, value in optional.items():
            if value:
                body[key] = value
        # None = not chosen: the server applies the deploy default
        # (KLANGKD_PER_HANDLE_HOME) — same convention as egress_mode.
        if optional.get("per_handle_home") is not None:
            body["per_handle_home"] = optional["per_handle_home"]
        resp = self.post("/api/v1/workspaces", json=body)
        self.check_auth(resp)
        self._raise_for_status(resp)
        w = resp.json()
        return Workspace(
            id=w["id"], name=w["name"], created_at=w["created_at"]
        )

    def update_workspace(self, workspace_id: str, **fields) -> None:
        """Partial-update workspace fields (``PUT /api/v1/workspaces/<id>``).

        Only the provided keyword fields are sent. Returns ``None``; raises
        :class:`httpx.HTTPStatusError` on a non-2xx so callers (TUI/CLI) can
        surface the server's ``detail``. Used by the TUI edit form (#1778);
        the CLI ``edit`` command still PUTs inline (#1779 will adopt this).
        """
        resp = self.put(f"/api/v1/workspaces/{workspace_id}", json=fields)
        self.check_auth(resp)
        self._raise_for_status(resp)

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
        self.check_auth(resp)
        self._raise_for_status(resp)

    def list_images(self) -> dict:
        resp = self.get("/api/v1/images")
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    def config(self) -> dict:
        """Authed ``GET /api/v1/config``.

        Sends the persisted token, so the response includes the auth-gated
        fields (``netfilter_default_domains``, ``netfilter_enabled``) that
        the pre-auth :func:`fetch_config` helper can't see — the
        create-workspace form seeds its Netfilter tab from
        ``netfilter_default_domains`` (#1931).
        """
        resp = self.get("/api/v1/config")
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    def resolve_workspace(self, name: str) -> Workspace:
        """Find a workspace by name (owned or shared).

        Raises WorkspaceNotFoundError if not found.
        """
        return self._resolve_workspace(lambda w: w.name == name, name)

    def find_workspace_by_id(self, ws_id: str) -> Workspace:
        """Find a workspace by id (owned or shared).

        Raises WorkspaceNotFoundError if not found. Lets the TUI tell a
        rename from a deletion when a name-based resolve misses (#3065).
        """
        return self._resolve_workspace(lambda w: w.id == ws_id, ws_id)

    def _resolve_workspace(
        self, matches: Callable[[Workspace], bool], key: str
    ) -> Workspace:
        """Scan owned + shared workspaces for the first match.

        Shared fetch/match shape of :meth:`resolve_workspace` and
        :meth:`find_workspace_by_id`.
        """
        all_ws = self.list_workspaces(
            all_pages=True
        ) + self.list_shared_workspaces(all_pages=True)
        match = next((w for w in all_ws if matches(w)), None)
        if match is None:
            raise WorkspaceNotFoundError(key)
        return match

    def delete_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.delete(f"/api/v1/workspaces/{ws.id}")
        self.check_auth(resp)
        self._raise_for_status(resp)

    def list_workspace_members(self, name: str) -> list[dict]:
        ws = self.resolve_workspace(name)
        resp = self.get(f"/api/v1/workspaces/{ws.id}/members")
        self.check_auth(resp)
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
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    def remove_workspace_member(self, name: str, email: str) -> None:
        ws = self.resolve_workspace(name)
        resp = self.patch(
            f"/api/v1/workspaces/{ws.id}/roles",
            json={"email": email, "role": None},
        )
        self.check_auth(resp)
        if resp.status_code == 404:
            raise WorkspaceNotFoundError(
                f"User '{email}' is not a member of '{name}'"
            )
        self._raise_for_status(resp)

    def _post_workspace_action(self, workspace_id: str, action: str) -> None:
        """POST /api/v1/workspaces/{id}/{action} (start/stop/restart)."""
        resp = self.post(f"/api/v1/workspaces/{workspace_id}/{action}")
        self.check_auth(resp)
        self._raise_for_status(resp)

    def restart_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        self._post_workspace_action(ws.id, "restart")

    def restart_workspace_by_id(self, workspace_id: str) -> None:
        """Restart a workspace container by id.

        Id-based counterpart to :meth:`restart_workspace` for callers (the
        sandbox driver) that already hold the workspace id.
        """
        self._post_workspace_action(workspace_id, "restart")

    def stop_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        self._post_workspace_action(ws.id, "stop")

    def stop_workspace_by_id(self, workspace_id: str) -> None:
        """Stop a running workspace container by id.

        Id-based counterpart to :meth:`stop_workspace` for callers (the
        sandbox driver) that already hold the workspace id and want to
        skip the name→id resolution round-trip.
        """
        self._post_workspace_action(workspace_id, "stop")

    def start_workspace(self, name: str) -> None:
        ws = self.resolve_workspace(name)
        self._post_workspace_action(ws.id, "start")

    def duplicate_workspace(self, name: str, new_name: str) -> dict:
        ws = self.resolve_workspace(name)
        resp = self.post(
            f"/api/v1/workspaces/{ws.id}/duplicate",
            json={"name": new_name},
        )
        self.check_auth(resp)
        self._raise_for_status(resp)
        return resp.json()

    # --- terminals (workspace WebSocket; no REST endpoint) ---

    async def list_terminals(self, name: str) -> list[dict]:
        """Return this user's own terminal windows in workspace *name*.

        There is no REST endpoint for terminal windows, so this drives the
        workspace WebSocket: connect, wait for the container, open a
        terminal to enumerate windows, then stop. Returns ``[]`` on any
        failure so the TUI can degrade gracefully.
        """
        return await self.terminals(name)

    async def list_shared_terminals(self, name: str) -> list[dict]:
        """Return shared terminals visible in workspace *name*.

        Other users' shared windows plus the agent's ``service`` window —
        the same set the browser renders as shared tabs. Drives the
        workspace WebSocket and issues the ``list_shared_terminals``
        command (no terminal session needed). Returns ``[]`` on any
        failure (including the ``spectate-on-shared-terminals`` permission
        being absent) so the TUI degrades to an empty shared list.
        """
        try:
            ws = self.resolve_workspace(name)
            async with ws_connect(
                self.server_url, token=self.token or ""
            ) as conn:
                await wait_container_ready(conn, ws.id)
                await conn.send(json.dumps({"cmd": "ui_ready"}))
                await self._drain_until_ready(conn)
                await conn.send(json.dumps({"cmd": "list_shared_terminals"}))
                return await self._recv_shared_terminals(conn)
        except Exception:
            return []

    async def close_terminal(self, name: str, window_id: str) -> list[dict]:
        """Close terminal window *window_id* (@N) in *name*; return list."""
        return await self.terminals(name, close_window_id=window_id)

    async def create_terminal(
        self, name: str, window_name: str | None = None
    ) -> list[dict]:
        """Create a new terminal window in workspace *name*; return updated list.

        With no *window_name* the server names the window ``bash`` (matching
        window 0); names are display-only, so callers need not invent a
        unique label (#2192).
        """
        return await self.terminals(
            name, create_window=True, window_name=window_name
        )

    async def rename_terminal(
        self, name: str, index: int, new_name: str
    ) -> list[dict]:
        """Rename terminal window at *index* in workspace *name*; return list."""
        return await self.terminals(name, rename=(index, new_name))

    async def _maybe_close_window(
        self, conn, windows: list[dict], close_window_id
    ) -> list[dict]:
        """Close the requested window; the refreshed window list."""
        if close_window_id is not None and windows:
            await conn.send(
                json.dumps(
                    {
                        "cmd": "terminal_close_window",
                        "window_id": close_window_id,
                    }
                )
            )
            return await self._recv_windows(conn)
        return windows

    async def _maybe_create_window(
        self, conn, windows: list[dict], create_window: bool, window_name
    ) -> list[dict]:
        """Create a named window; the refreshed window list."""
        if create_window:
            cmd = {"cmd": "terminal_new_window"}
            # No name → server names the window "bash" (#2192).
            if window_name:
                cmd["name"] = window_name
            await conn.send(json.dumps(cmd))
            return await self._recv_windows(conn)
        return windows

    async def _maybe_rename_window(
        self, conn, windows: list[dict], rename
    ) -> list[dict]:
        """Rename the requested window; the refreshed window list."""
        if rename is not None and windows:
            idx, new_name = rename
            await conn.send(
                json.dumps(
                    {
                        "cmd": "terminal_rename_window",
                        "index": idx,
                        "name": new_name,
                    }
                )
            )
            return await self._recv_windows(conn)
        return windows

    async def terminals(
        self,
        name: str,
        *,
        close_window_id: str | None = None,
        create_window: bool = False,
        window_name: str | None = None,
        rename: tuple[int, str] | None = None,
    ) -> list[dict]:
        try:
            ws = self.resolve_workspace(name)
            async with ws_connect(
                self.server_url, token=self.token or ""
            ) as conn:
                await wait_container_ready(conn, ws.id)
                await conn.send(json.dumps({"cmd": "ui_ready"}))
                await self._drain_until_ready(conn)
                cols, rows = get_terminal_size()
                await conn.send(
                    json.dumps(
                        {
                            "cmd": "terminal_start",
                            "cols": cols,
                            "rows": rows,
                        }
                    )
                )
                windows = await self._recv_windows(conn)
                windows = await self._maybe_close_window(
                    conn, windows, close_window_id
                )
                windows = await self._maybe_create_window(
                    conn, windows, create_window, window_name
                )
                windows = await self._maybe_rename_window(
                    conn, windows, rename
                )
                await send_ignore_closed(
                    conn, json.dumps({"cmd": "terminal_stop"})
                )
                return windows
        except Exception:
            return []

    @staticmethod
    async def _drain_until_ready(conn, timeout: float = 30.0) -> None:
        """Read frames until the post-``ui_ready`` container_ready event."""
        await recv_until(conn, is_container_ready_event, timeout)

    @staticmethod
    async def _recv_windows(conn, timeout: float = 30.0) -> list[dict]:
        """Read frames until a ``terminal_windows`` frame arrives."""

        def _match(m):
            # Surface server errors immediately instead of looping
            # until the 30s timeout (#1966 review).
            if m.get("type") == "error":
                raise ConnectionError(m.get("message", "terminal error"))
            return m.get("type") == "terminal_windows"

        msg = await recv_until(conn, _match, timeout)
        return msg.get("windows") or []

    @staticmethod
    async def _recv_shared_terminals(
        conn, timeout: float = 30.0
    ) -> list[dict]:
        """Read frames until a ``shared_terminals`` frame arrives."""

        def _match(m):
            if m.get("type") == "error":
                raise ConnectionError(m.get("message", "terminal error"))
            return m.get("type") == "shared_terminals"

        msg = await recv_until(conn, _match, timeout)
        return msg.get("terminals") or []

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
        with http_stream(
            self.server_url,
            "GET",
            f"/api/v1/workspaces/{workspace_id}/export",
            headers=self._headers(),
            timeout=300.0,
        ) as resp:
            self.check_auth(resp)
            if not resp.is_success:
                resp.read()  # consume body so .text is available
                self._raise_for_status(resp)
            # Use Content-Length if available, otherwise fall back to
            # the server's compressed size estimate.
            total = download_total(resp)
            write_chunks(resp, output, on_progress, total)

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
                    # The wrapper is only constructed when on_progress is
                    # set (see import_workspace), so this guard is always
                    # true here -- its false arm is unreachable.
                    if on_progress:  # pragma: no branch
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
            resp = http_request(
                self.server_url,
                "POST",
                "/api/v1/workspaces/import",
                headers=self._headers(),
                files={"file": (archive.name, pf, "application/gzip")},
                params=params,
                timeout=300.0,
            )
        self.check_auth(resp)
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


async def recv_until(conn, predicate, timeout: float = 30.0):
    """Receive frames until *predicate(msg)* is true; return the msg.

    Shared bounded receive loop for the WebSocket command paths (#2546).
    Raises asyncio.TimeoutError when the deadline passes first. Callers
    that must surface server ``error`` frames immediately add the check
    to their predicate and raise from there.
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while True:
        remaining = end - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
        msg = json.loads(raw)
        if predicate(msg):
            return msg


def is_container_ready_event(msg) -> bool:
    """True for the post-``ui_ready`` container_ready event frame."""
    return (
        msg.get("type") == "event"
        and isinstance(msg.get("event"), dict)
        and msg["event"].get("name") == "container_ready"
    )


async def recv_json_messages(ws, timeout: float):
    """Yield decoded server messages until *timeout* elapses overall.

    The shared bounded receive loop of the terminal command paths
    (#2546): each caller breaks on the frame it needs (and buffers or
    ignores the rest) while one deadline caps the whole exchange.
    Raises asyncio.TimeoutError when the deadline passes first."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        yield json.loads(raw)


@asynccontextmanager
async def workspace_ws(server_spec, token, workspace_id, max_size=None):
    """Connected workspace WebSocket, ready for commands.

    Connects, sends ``workspace_connect``, and waits for
    ``container_ready`` — the shared preamble of every ws command path
    (#2546). Yields the connected socket.
    """
    async with ws_connect(server_spec, token=token, max_size=max_size) as ws:
        await wait_container_ready(ws, workspace_id)
        yield ws


async def send_ignore_closed(ws, msg: str) -> None:
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


def osc_or_dcs(data: bytes) -> bool:
    """OSC (\\e]) and DCS (\\eP) are always responses, never user input."""
    return data[1:2] in (b"]", b"P")


def csi_response(data: bytes) -> bool:
    """CSI responses: \\e[> (DA2), \\e[? (DA1/DECRPM)."""
    return data[1:2] == b"[" and len(data) > 2 and data[2:3] in (b">", b"?")


def is_terminal_response(data: bytes) -> bool:
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
    if osc_or_dcs(data):
        return True
    return csi_response(data)


def raw_stdin_mode() -> tuple[int, object | None]:
    """(fd, old_attrs) with stdin set raw; old_attrs None when not a tty."""
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        return fd, old
    except termios.error:
        return fd, None


def drain_rounds(fd: int) -> None:
    """Drain stdin for up to 500ms, checking every 50ms."""
    for _ in range(10):
        if select.select([fd], [], [], 0.05)[0]:
            os.read(fd, 4096)
            continue
        # No data for 50ms — but responses may still be in
        # flight. Wait one more round to be sure.
        if not select.select([fd], [], [], 0.1)[0]:
            break
        os.read(fd, 4096)


def drain_stdin() -> None:
    """Drain any pending bytes from stdin (terminal query responses).

    Terminal capability responses can arrive over several hundred
    milliseconds after tmux probes the terminal.  We drain in a loop
    with a generous timeout so late-arriving responses don't leak to
    the host shell as garbage commands.
    """
    try:
        fd, old = raw_stdin_mode()
        try:
            drain_rounds(fd)
        finally:
            if old is not None:
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


def agent_forward_available(forward_agent: bool, sock: str | None) -> bool:
    """True when forwarding was requested and the local agent socket exists."""
    return bool(forward_agent and sock and os.path.exists(sock))


async def wait_agent_started(ws) -> bool:
    """Wait for the ssh_agent_started ack (False on error/timeout)."""
    try:
        async for msg in recv_json_messages(ws, 10):
            if msg.get("type") == "ssh_agent_started":
                return True
            if msg.get("type") == "error":
                return False
    except asyncio.TimeoutError:
        pass  # proceed without agent forwarding
    return False


async def start_ssh_agent_forward(
    ws, forward_agent: bool
) -> tuple[bool, str | None]:
    """2a. Start SSH agent forwarding if requested and available.

    Waits for confirmation before the terminal starts so SSH_AUTH_SOCK
    is included in the shell environment. Returns (active, local_sock).
    """
    ssh_agent_active = False
    local_agent_sock = os.environ.get("SSH_AUTH_SOCK")
    if agent_forward_available(forward_agent, local_agent_sock):
        await ws.send(json.dumps({"cmd": "ssh_agent_start"}))
        ssh_agent_active = await wait_agent_started(ws)
    return ssh_agent_active, local_agent_sock


def absorb_output_frame(msg: dict, state: dict) -> None:
    """Buffer one terminal_output frame; raise on a server error frame."""
    if msg.get("type") == "error":
        raise ConnectionError(f"Server error: {msg.get('message', 'unknown')}")
    state["buffered_output"].append(msg.get("data", ""))


def startup_frame_result(msg: dict, state: dict, needs_shared: bool) -> bool:
    """One startup frame's effect on the drain state; True when done."""
    mtype = msg.get("type")
    if mtype in ("terminal_output", "error"):
        absorb_output_frame(msg, state)
        return False
    if mtype == "terminal_windows":
        state["own_windows"] = msg.get("windows", [])
        return not needs_shared
    if mtype == "shared_terminals":
        state["shared_terminals"] = msg.get("terminals", [])
        return needs_shared
    return False


async def drain_terminal_startup(
    ws, needs_shared: bool
) -> tuple[list[dict], list[dict], list[str]]:
    """3. Drain messages until window selection has what it needs.

    terminal_output may arrive before terminal_windows due to async
    output forwarding, so we buffer early output and don't stop until
    the window list is in. When joining a shared terminal
    (``handle:window_name``) we must ALSO wait for shared_terminals,
    which the server sends AFTER terminal_windows (see #1208):
    breaking on terminal_windows alone leaves shared_terminals empty
    and the join fails with "Shared terminal not found".
    """
    state: dict = {
        "own_windows": [],
        "shared_terminals": [],
        "buffered_output": [],
    }
    try:
        # recv_json_messages only ends by raising (deadline), so the
        # async-for can never complete normally — break/raise are the
        # only exits.
        async for msg in recv_json_messages(  # pragma: no branch
            ws, 30
        ):
            if startup_frame_result(msg, state, needs_shared):
                break
    except asyncio.TimeoutError:
        raise ConnectionError(
            "Terminal did not start within 30 seconds"
        ) from None
    return (
        state["own_windows"],
        state["shared_terminals"],
        state["buffered_output"],
    )


async def join_shared_terminal(
    ws, window: str, shared_terminals: list[dict]
) -> None:
    """3b (shared). Join "handle:window_name" (or "handle:@N" for an
    exact id).

    Names are not unique — dups are allowed (#2192) — so a name matching
    several windows under one owner is an error, not a silent first match.
    """
    match = match_shared_terminal(window, shared_terminals)
    await ws.send(
        json.dumps(
            {
                "cmd": "join_shared_terminal",
                "user_id": match["user_id"],
                "window_id": match["window_id"],
            }
        )
    )
    # Wait for terminal_started confirmation (the generator only ends
    # by raising — break/raise are the only exits).
    async for msg in recv_json_messages(  # pragma: no branch
        ws, 10
    ):
        if msg.get("type") == "terminal_started":
            break
        if msg.get("type") == "terminal_output":
            sys.stdout.write(msg.get("data", ""))
            sys.stdout.flush()
        if msg.get("type") == "error":
            raise ConnectionError(f"Failed to join: {msg.get('message')}")


def _shared_matches(
    shared_terminals: list[dict], owner_handle: str, key: str, win_ref: str
) -> list[dict]:
    """Shared terminals under one owner whose *key* matches *win_ref*."""
    return [
        t
        for t in shared_terminals
        if t.get("handle") == owner_handle and t.get(key) == win_ref
    ]


def _raise_ambiguous_shared(
    owner_handle: str, win_ref: str, matches: list[dict]
) -> None:
    ids = ", ".join(t["window_id"] for t in matches if t.get("window_id"))
    raise ConnectionError(
        f"Multiple shared terminals named "
        f"'{win_ref}' under '{owner_handle}'; "
        f"specify one by id (e.g. "
        f"{owner_handle}:{matches[0].get('window_id')}): "
        f"{ids}"
    )


def starts_disconnect_sequence(data: bytes, after_newline: bool) -> bool:
    """A ``~`` right after a newline starts the ~. disconnect sequence."""
    return data == b"~" and after_newline


def first_or_none(matches: list):
    """The first match, or None when empty."""
    return matches[0] if matches else None


def shared_name_match(
    shared_terminals: list[dict], owner_handle: str, win_ref: str
):
    """The single name match, raising when ambiguous (#2192)."""
    by_name = _shared_matches(
        shared_terminals, owner_handle, "window_name", win_ref
    )
    if len(by_name) > 1:
        _raise_ambiguous_shared(owner_handle, win_ref, by_name)
    return first_or_none(by_name)


def match_shared_terminal(window: str, shared_terminals: list[dict]) -> dict:
    """Resolve "handle:window_name" (or "handle:@N") to one shared
    terminal; ConnectionError when absent or ambiguous."""
    owner_handle, win_ref = window.split(":", 1)
    if win_ref.startswith("@"):
        by_id = _shared_matches(
            shared_terminals, owner_handle, "window_id", win_ref
        )
        match = first_or_none(by_id)
    else:
        match = shared_name_match(shared_terminals, owner_handle, win_ref)
    if match is None:
        raise ConnectionError(f"Shared terminal '{window}' not found")
    return match


def _raise_ambiguous_own(window: str, name_matches: list[dict]) -> None:
    ids = ", ".join(w["id"] for w in name_matches if w.get("id"))
    raise ConnectionError(
        f"Multiple terminals named '{window}'; specify one by id: {ids}"
    )


def window_by_id(own_windows: list[dict], window: str) -> list[dict]:
    """Own windows matching an @N id."""
    return [w for w in own_windows if w.get("id") == window]


def windows_by_name(own_windows: list[dict], window: str) -> list[dict]:
    """Own windows matching a name."""
    return [w for w in own_windows if w.get("name") == window]


def own_id_match(window: str, by_id: list[dict]) -> dict:
    """The exact @N match; ConnectionError when the window is gone."""
    if not by_id:
        raise ConnectionError(f"Window '{window}' no longer exists")
    return by_id[0]


def own_name_match(window: str, name_matches: list[dict]):
    """The single name match (None when absent), raising when ambiguous."""
    if len(name_matches) > 1:
        _raise_ambiguous_own(window, name_matches)
    return first_or_none(name_matches)


def match_own_window(window: str, own_windows: list[dict]) -> dict | None:
    """Resolve a @N id or name to an existing own window (None = absent)."""
    if window.startswith("@"):
        return own_id_match(window, window_by_id(own_windows, window))
    return own_name_match(window, windows_by_name(own_windows, window))


def own_window_frame(
    msg: dict, buffered_output: list[str], state: dict
) -> bool:
    """Absorb one create-window frame; True when the window list arrived."""
    mtype = msg.get("type")
    if mtype == "terminal_windows":
        state["windows"] = msg.get("windows", [])
        return True
    if mtype == "terminal_output":
        buffered_output.append(msg.get("data", ""))
    if mtype == "error":
        raise ConnectionError(f"Failed to create window: {msg.get('message')}")
    return False


def named_window(windows: list[dict], window: str):
    """The first window named *window* from a refreshed list."""
    return next(
        (w for w in windows if w.get("name") == window),
        None,
    )


async def create_own_window(
    ws, window: str, buffered_output: list[str]
) -> dict:
    """Create a named window and resolve it from the refreshed list."""
    await ws.send(
        json.dumps(
            {
                "cmd": "terminal_new_window",
                "name": window,
            }
        )
    )
    state: dict = {}
    # recv_json_messages only ends by raising (deadline), so the
    # async-for can never complete normally — break/raise are the only
    # exits (the arc to ``if match is None`` without a break is
    # unreachable).
    async for msg in recv_json_messages(  # pragma: no branch
        ws, 10
    ):
        if own_window_frame(msg, buffered_output, state):
            break
    match = named_window(state.get("windows", []), window)
    if match is None:
        raise ConnectionError(f"Window '{window}' not created")
    return match


async def select_own_window(
    ws, window: str, own_windows: list[dict], buffered_output: list[str]
) -> None:
    """3b (own). Select a window by id (@N) or by name.

    An id targets the exact tmux window and must never create a new one
    (#1954); a name selects an existing window or creates one with that
    name. Names are not unique (dups allowed, #2192), so a name matching
    several windows is an error rather than a silent first match —
    disambiguate with @N.
    """
    match = match_own_window(window, own_windows)
    if match is None:
        # Name with no match — create the window.
        match = await create_own_window(ws, window, buffered_output)
    await ws.send(
        json.dumps(
            {
                "cmd": "terminal_select_window",
                "window_id": match["id"],
            }
        )
    )


async def run_terminal_session(
    ws,
    server_spec: str,
    token: str,
    raw_mode: bool,
    ssh_agent_active: bool,
    local_agent_sock: str | None,
    cols: int,
    rows: int,
) -> None:
    """4. Put terminal in raw mode, run shell, restore.

    raw_mode path: tcgetattr + tty.setraw + _raw_mode_exit +
    terminal_stop.
    """
    if raw_mode:
        old_settings = _raw_mode_enter()
        tty.setraw(sys.stdin)
    # Use the original server spec for token refresh (works for both
    # TCP URLs and UDS socket paths).
    try:
        await run_shell(
            ws,
            cols,
            rows,
            ssh_agent_sock=local_agent_sock if ssh_agent_active else None,
            server_url=server_spec,
            token=token,
        )
    finally:
        if raw_mode:
            _raw_mode_exit(old_settings)
            reset_terminal()
        # Drain any terminal query responses still buffered in stdin
        # so they don't leak to the host shell after exit.
        drain_stdin()
        if ssh_agent_active:
            await send_ignore_closed(ws, json.dumps({"cmd": "ssh_agent_stop"}))
        await send_ignore_closed(ws, json.dumps({"cmd": "terminal_stop"}))


def needs_shared_window(window: str | None) -> bool:
    """True when the window target is a shared handle:name reference."""
    return window is not None and ":" in window


async def select_window(
    ws, window, own_windows, shared_terminals, buffered_output
) -> None:
    """3b. Join a shared terminal or select/create an own window."""
    if ":" in window:
        await join_shared_terminal(ws, window, shared_terminals)
    else:
        await select_own_window(ws, window, own_windows, buffered_output)


async def ws_shell(
    server_spec: str,
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
    async with ws_connect(server_spec, token=token, max_size=max_size) as ws:
        # 1. Connect to workspace
        await wait_container_ready(ws, workspace_id)

        # 2a. Start SSH agent forwarding if requested and available.
        ssh_agent_active, local_agent_sock = await start_ssh_agent_forward(
            ws, forward_agent
        )

        # 2b. Run pre-shell hook (sandbox setup) after agent forwarding
        # is active so that setup scripts can use SSH (e.g. git clone).
        if sandbox_setup is not None:
            await sandbox_setup(ws)

        # 2c. Start terminal
        cols, rows = get_terminal_size()
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
        (
            own_windows,
            shared_terminals,
            buffered_output,
        ) = await drain_terminal_startup(
            ws, needs_shared=needs_shared_window(window)
        )

        # 3b. Select window if requested.
        if window is not None:
            await select_window(
                ws, window, own_windows, shared_terminals, buffered_output
            )

        # Flush buffered terminal output from the startup drain.
        for text in buffered_output:
            sys.stdout.write(text)
        sys.stdout.flush()

        # 4. Put terminal in raw mode, run shell, restore
        await run_terminal_session(
            ws,
            server_spec,
            token,
            raw_mode,
            ssh_agent_active,
            local_agent_sock,
            cols,
            rows,
        )


class _ShellSession:
    """Shared I/O pump infrastructure for terminal and exec sessions.

    Owns the WebSocket, stop event, heartbeat loop, and SSH agent relay
    that both ``TerminalSession`` and ``ExecSession`` need.
    """

    def __init__(self, ws, ssh_agent_sock: str | None = None):
        self.ws = ws
        self.ssh_agent_sock = ssh_agent_sock
        self.stop = asyncio.Event()
        self.agent_queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def heartbeat_loop(self) -> None:
        """Send a heartbeat every :data:`_HEARTBEAT_INTERVAL` s until
        stopped."""
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(
                    self.stop.wait(), timeout=_HEARTBEAT_INTERVAL
                )
                return
            except asyncio.TimeoutError:
                pass
            if not self.stop.is_set():
                await self.ws.send(json.dumps({"cmd": "heartbeat"}))

    def dispatch_agent_response(self, data: dict) -> None:
        """Enqueue an ssh_agent_response message for the relay loop."""
        raw = base64.b64decode(data.get("data", ""))
        if raw:
            self.agent_queue.put_nowait(raw)

    async def relay_one(self, data: bytes) -> None:
        """Forward one agent request and send back its reply."""
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, query_local_ssh_agent, self.ssh_agent_sock, data
            )
            if response is not None:
                await self.ws.send(
                    json.dumps(
                        {
                            "cmd": "ssh_agent_data",
                            "data": base64.b64encode(response).decode("ascii"),
                        }
                    )
                )
        except (OSError, ConnectionError) as e:
            logger.warning("SSH agent relay: %s", e)

    async def ssh_agent_relay_loop(self) -> None:
        """Relay SSH agent protocol between container and local agent.

        Reads ssh_agent_response messages from the queue (put there by
        the stdout loop), forwards them to the local SSH agent socket,
        reads the agent's reply, and sends it back over the WebSocket.
        """
        if not self.ssh_agent_sock:
            return
        while not self.stop.is_set():
            try:
                data = await asyncio.wait_for(
                    self.agent_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            await self.relay_one(data)

    async def run(self) -> None:
        raise NotImplementedError


def refreshed_token(server_url: str, token: str) -> str | None:
    """A fresh token after a 4002 close (none-mode re-login included)."""
    new = refresh_token(server_url, token)
    if not new and server_mode_is_none(server_url):
        try:
            _email, new = _local_login(server_url)
        except SystemExit:
            new = None
    return new


def close_code(exc) -> int | None:
    """The close code from a ConnectionClosed (None when absent)."""
    return exc.rcvd.code if exc.rcvd else None


def container_stopped_event(data: dict) -> bool:
    """True for the container_stopped CUSTOM event."""
    event = data.get("event", {})
    return (
        event.get("type") == "CUSTOM"
        and event.get("name") == "container_stopped"
    )


class TerminalSession(_ShellSession):
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

    async def _stdin_ready(self, fd) -> bool:
        """True when stdin has data (200ms poll)."""
        ready, _, _ = await self._loop.run_in_executor(
            None, lambda: select.select([fd], [], [], 0.2)
        )
        return bool(ready)

    async def _tilde_pending(self, data: bytes, st: dict) -> str | None:
        """Handle a pending ~; "exit" when the ~. disconnect fired."""
        if st["saw_tilde"]:
            st["saw_tilde"] = False
            if await self._tilde_key(data):
                return "exit"
        return None

    async def _handle_byte(
        self, data: bytes, st: dict, fd
    ) -> tuple[str, bytes | None]:
        """One byte through the ~. state machine -> (disposition, payload).

        disposition: "exit" (disconnect fired — end the loop), "skip"
        (consumed locally — read the next byte), or "send" (payload
        carries the bytes to forward).
        """
        outcome = await self._tilde_pending(data, st)
        if outcome == "exit":
            return "exit", None
        if starts_disconnect_sequence(data, st["after_newline"]):
            st["saw_tilde"] = True
            st["after_newline"] = False
            return "skip", None
        st["after_newline"] = data in (b"\r", b"\n")
        if data == b"\x1b":
            data = await self._extend_escape_sequence(fd, data)
            if data is None:
                return "skip", None
        return "send", data

    async def _forward_input(self, fd, st: dict) -> bool:
        """Read and handle one stdin byte; False when the loop should exit."""
        try:
            data = await self._loop.run_in_executor(None, os.read, fd, 1)
            if not data:
                return False
            disposition, payload = await self._handle_byte(data, st, fd)
            if disposition == "exit":
                return False
        except (OSError, io.UnsupportedOperation):
            return False
        # The send sits outside the except scope, as in the original loop:
        # a send failure must propagate and tear down the other pumps.
        if disposition == "send":
            await self.ws.send(
                json.dumps(
                    {
                        "cmd": "terminal_input",
                        "data": payload.decode("utf-8", errors="replace"),
                    }
                )
            )
        return True

    async def stdin_loop(self) -> None:
        fd = self.stdin.fileno()
        st = {"after_newline": True, "saw_tilde": False}
        while not self.stop.is_set():
            if not await self._stdin_ready(fd):
                continue
            if not await self._forward_input(fd, st):
                return

    async def _tilde_key(self, data: bytes) -> bool:
        """Consume the pending ``~`` after a newline; True when the ~.
        disconnect sequence fired (caller returns)."""
        if data == b".":
            self.stdout.write("\r\nDisconnected.\r\n")
            self.stdout.flush()
            self.stop.set()
            await self.ws.close()
            return True
        await self.ws.send(json.dumps({"cmd": "terminal_input", "data": "~"}))
        return False

    async def _read_more(self, fd, size: int) -> bytes:
        """Read up to *size* more bytes from stdin."""
        return await self._loop.run_in_executor(None, os.read, fd, size)

    async def _drain_query_response(self, fd) -> None:
        """Drain the remaining bytes of a terminal query response."""
        for _ in range(10):
            if not select.select([fd], [], [], 0.02)[0]:
                break
            try:
                await self._read_more(fd, 256)
            except OSError:
                break

    async def _extend_escape_sequence(self, fd, data: bytes) -> bytes | None:
        """Extend a pending ESC into its full sequence; None when it is a
        terminal query response that gets drained locally instead of sent."""
        if select.select([fd], [], [], 0.05)[0]:
            more = await self._read_more(fd, 32)
            if more:
                data += more
        if not is_terminal_response(data):
            return data
        await self._drain_query_response(fd)
        return None

    def _write_terminal_output(self, text: str) -> None:
        """Write output; an [exited] marker gets the disconnect hint."""
        self.stdout.write(text)
        self.stdout.flush()
        if "[exited]" in text:
            self.stdout.write("\r\nPress Enter, then ~. to disconnect.\r\n")
            self.stdout.flush()

    async def _apply_shell_frame(self, msg: str) -> bool:
        """Handle one shell frame; True when the loop should stop."""
        data = json.loads(msg)
        if data.get("type") == "terminal_output":
            self._write_terminal_output(data["data"])
        elif data.get("type") == "ssh_agent_response":
            self.dispatch_agent_response(data)
        elif data.get("type") == "event":
            if container_stopped_event(data):
                logging.info("[container stopped]")
                return True
        return False

    async def stdout_loop(self) -> None:
        try:
            while not self.stop.is_set():
                msg = await self.ws.recv()
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="replace")
                if await self._apply_shell_frame(msg):
                    break
        except websockets.ConnectionClosed as exc:
            self._handle_disconnect(exc)
        self.stop.set()

    def _report_session_state(self, new: str | None) -> None:
        """Tell the user whether the session self-healed or expired."""
        if new:
            self.stdout.write(
                "\r\nSession refreshed."
                " Run your command again to reconnect.\r\n"
            )
        else:
            self.stdout.write(
                "\r\nSession expired. Run `klangk login`"
                " to re-authenticate.\r\n"
            )

    def _session_expired_line(self) -> None:
        """The session-expired hint (dead token, refresh not possible)."""
        self.stdout.write(
            "\r\nSession expired. Run `klangk login` to re-authenticate.\r\n"
        )

    def _handle_disconnect(self, exc) -> None:
        """Explain an unexpected close (and try a token refresh on 4002)."""
        if self.stop.is_set():
            return
        _code = close_code(exc)
        if _code == 4002 and self.token:
            self._report_session_state(
                refreshed_token(self.server_url, self.token)
            )
        elif _code in (4001, 4002):
            self._session_expired_line()
        else:
            self.stdout.write("\r\nServer disconnected.\r\n")
        self.stdout.flush()

    async def resize_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(
                    self.stop.wait(), timeout=RESIZE_POLL_INTERVAL
                )
                return
            except asyncio.TimeoutError:
                pass
            new_cols, new_rows = get_terminal_size()
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


async def cancel_task(task) -> None:
    """Cancel a task and await its end, ignoring cancellation."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class ExecSession(_ShellSession):
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

    def _stdin_fd(self) -> int | None:
        """stdin's fileno, or None when it has none (BytesIO etc.)."""
        try:
            return self.stdin.fileno()
        except (io.UnsupportedOperation, AttributeError):
            return None

    async def send_exec_input(self, data: bytes) -> None:
        """Send one exec_input chunk (base64)."""
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "exec_input",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
        )

    async def _forward_fd_stdin(self, fd: int) -> None:
        """Forward stdin by fd (select loop)."""
        while not self.stop.is_set():
            ready = await self._loop.run_in_executor(
                None,
                lambda: select.select([fd], [], [], 0.2)[0],
            )
            if not ready:
                continue
            data = await self._loop.run_in_executor(None, os.read, fd, 65536)
            if not data:
                break
            await self.send_exec_input(data)

    async def _forward_buffered_stdin(self) -> None:
        """Forward a non-fd stdin in one read."""
        data = self.stdin.read()
        if data:
            await self.send_exec_input(data)

    async def stdin_forward(self) -> None:
        if self.stdin is None:
            await self.ws.send(json.dumps({"cmd": "exec_close_stdin"}))
            return
        fd = self._stdin_fd()
        if fd is not None:
            await self._forward_fd_stdin(fd)
        else:
            await self._forward_buffered_stdin()
        await self.ws.send(json.dumps({"cmd": "exec_close_stdin"}))

    async def _write_exec_output(self, raw: bytes) -> None:
        """Write exec output to the fd-backed or plain stdout."""
        if self.stdout is None:
            return
        if self._has_stdout_fd:
            await self._loop.run_in_executor(
                None, os.write, self._stdout_fd, raw
            )
        else:
            self.stdout.write(raw)

    async def _apply_exec_frame(self, msg: str) -> bool:
        """Handle one exec frame; True when the session should stop."""
        data = json.loads(msg)
        mtype = data.get("type")
        if mtype == "exec_output":
            await self._write_exec_output(base64.b64decode(data["data"]))
        elif mtype == "ssh_agent_response":
            self.dispatch_agent_response(data)
        elif mtype == "exec_exit":
            self.exit_code = data.get("code", 0)
            return True
        elif mtype == "error":
            # Server-side nack (e.g. the #2706/#2712 exec-and-sync permission
            # gate on exec_start). stderr, never stdout: stdout
            # carries rsync's binary protocol when this session is
            # the sync transport, and rsync relays transport stderr
            # to the user's rsync output.
            print(
                f"klangk: {data.get('message', 'unknown error')}",
                file=sys.stderr,
            )
            self.exit_code = 1
            return True
        return False

    async def stdout_forward(self) -> None:
        while True:
            msg = await self.ws.recv()
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", errors="replace")
            if await self._apply_exec_frame(msg):
                break

    async def _await_output(self, stdout_task) -> None:
        """Await stdout (with the optional timeout); 124 on timeout."""
        try:
            if self.timeout is not None:
                await asyncio.wait_for(stdout_task, timeout=self.timeout)
            else:
                await stdout_task
        except asyncio.TimeoutError:
            self.exit_code = 124  # same as coreutils timeout(1)
            await cancel_task(stdout_task)

    async def _drain_stdin_forward(self, stdin_task) -> None:
        """Let the stdin forwarder finish (bounded), then cancel it."""
        try:
            await asyncio.wait_for(stdin_task, timeout=_STDIN_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            await cancel_task(stdin_task)

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
        await self._await_output(stdout_task)
        self.stop.set()
        await self._drain_stdin_forward(stdin_task)
        await cancel_task(heartbeat_task)
        await cancel_task(agent_task)

        await send_ignore_closed(self.ws, json.dumps({"cmd": "exec_stop"}))
        return self.exit_code


async def run_shell(
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
    session = TerminalSession(
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


async def exec_on_ws(
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
    interactive ``klangk exec`` entrypoint (ws_exec) overrides it to
    True. See #1041.
    """
    session = ExecSession(
        ws,
        command,
        stdin=stdin,
        stdout=stdout,
        timeout=timeout,
        login=login,
    )
    return await session.run()


async def ws_exec(
    server_spec: str,
    token: str,
    workspace_id: str,
    command: list[str],
    max_size: int = _WS_MAX_SIZE,
    login: bool = True,
) -> int:
    """Run a command interactively, piping real stdin/stdout.

    Returns the remote process exit code.  Defaults to ``login=True``
    (run as a bash login shell so ~/.profile is sourced, like a
    terminal -- #1041); ``klangk exec --raw`` and the rsync transport
    pass False for raw argv.
    """
    async with workspace_ws(
        server_spec, token, workspace_id, max_size=max_size
    ) as ws:
        return await exec_on_ws(
            ws,
            command,
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            login=login,
        )


async def ws_exec_piped(
    server_spec: str,
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
    async with workspace_ws(
        server_spec, token, workspace_id, max_size=max_size
    ) as ws:
        stdin_buf = io.BytesIO(stdin_data) if stdin_data else None
        stdout_buf = io.BytesIO()
        exit_code = await exec_on_ws(
            ws, command, stdin=stdin_buf, stdout=stdout_buf
        )
        return (
            exit_code,
            stdout_buf.getvalue().decode("utf-8", errors="replace"),
        )
