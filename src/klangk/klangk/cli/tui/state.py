"""Live state bridge for the klangk TUI.

Reads ``CLIConfig`` / ``CLIState`` fresh on every access (no stale
snapshots), mirroring the server-side ``app``-ownership discipline so a
server switch or an external ``klangk login`` is reflected immediately.

Stays within ``klangk.cli`` (isolation rule): only stdlib, third-party
deps, and sibling ``cli`` modules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..auth import (
    _UNREACHABLE,
    _oidc_browser_login,
    fetch_config,
    local_login,
)
from ..client import KlangkClient, Workspace
from ..config import (
    CLIConfig,
    CLIState,
    add_server_to_config,
    default_server_uds_path,
    remove_server_from_config,
    update_server_in_config,
)
from ..transport import http_request

logger = logging.getLogger(__name__)


class LoginError(Exception):
    """Raised when an in-TUI login attempt fails."""


@dataclass(frozen=True)
class ServerInfo:
    """A known server alias + URL from klangk.yaml."""

    alias: str
    url: str


class TuiState:
    """Live bridge to CLIConfig / CLIState / KlangkClient.

    Config and state are re-loaded on every call rather than cached at
    construction, so the TUI never acts on a stale snapshot after a
    server switch, an external login, or a token refresh.
    """

    def __init__(self, server_url: str | None = None) -> None:
        # ``--server`` override; otherwise the active server from state.
        self._server_override = server_url

    # --- fresh config / state each call ---

    def cfg(self) -> CLIConfig:
        return CLIConfig.load()

    def state(self) -> CLIState:
        return CLIState.load()

    def current_url(self) -> str | None:
        if self._server_override is not None:
            return self._server_override
        active = self.state().active_server
        if active is not None:
            return active
        # Single-host convenience (#1676): a co-located klangkd's default
        # UDS is usable with no `klangk login` step, so a fresh user can log
        # in straight from the TUI.
        uds = default_server_uds_path()
        if Path(uds).exists():
            return uds
        return None

    def default_uds(self) -> str | None:
        """The co-located klangkd default UDS, if its socket exists."""
        uds = default_server_uds_path()
        return uds if Path(uds).exists() else None

    def known_servers(self) -> list[ServerInfo]:
        return [
            ServerInfo(alias=alias, url=entry.url)
            for alias, entry in self.cfg().servers.items()
        ]

    def token(self) -> str | None:
        url = self.current_url()
        if url is None:
            return None
        return self.state().get_token(url)

    def email(self) -> str | None:
        url = self.current_url()
        if url is None:
            return None
        return self.state().get_email(url)

    def is_authenticated(self) -> bool:
        url = self.current_url()
        tok = self.token()
        if tok is None:
            logger.debug(
                "is_authenticated=False: url=%s, token=%s",
                url,
                "present" if tok else "None",
            )
        return tok is not None

    def client(self) -> KlangkClient:
        return KlangkClient(self.current_url(), self.token())

    # --- workspaces ---

    def list_owned_workspaces(self) -> list[Workspace]:
        return self.client().list_workspaces(all_pages=True)

    def list_shared_workspaces(self) -> list[Workspace]:
        return self.client().list_shared_workspaces(all_pages=True)

    def find_workspace(self, name: str) -> Workspace:
        return self.client().resolve_workspace(name)

    def restart_workspace(self, name: str) -> None:
        self.client().restart_workspace(name)

    def stop_workspace(self, name: str) -> None:
        self.client().stop_workspace(name)

    def start_workspace(self, name: str) -> None:
        self.client().start_workspace(name)

    def delete_workspace(self, name: str) -> None:
        self.client().delete_workspace(name)

    def duplicate_workspace(self, name: str, new_name: str) -> dict:
        return self.client().duplicate_workspace(name, new_name)

    def create_workspace(
        self,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        health_check: str | None = None,
        allowed_domains: list[str] | None = None,
    ) -> Workspace:
        return self.client().create_workspace(
            name,
            image=image,
            service_command=service_command,
            auto_start=auto_start,
            mounts=mounts,
            env=env,
            health_check=health_check,
            allowed_domains=allowed_domains,
        )

    def update_workspace(self, workspace_id: str, **fields) -> None:
        """Partial-update a workspace's fields (used by the TUI edit form, #1778)."""
        self.client().update_workspace(workspace_id, **fields)

    def list_images(self) -> dict:
        return self.client().list_images()

    async def list_terminals(self, name: str) -> list[dict]:
        return await self.client().list_terminals(name)

    async def close_terminal(self, name: str, index: int) -> list[dict]:
        return await self.client().close_terminal(name, index)

    async def create_terminal(self, name: str, window_name: str) -> list[dict]:
        return await self.client().create_terminal(name, window_name)

    # --- auth mode (probed live via /config) ---

    def auth_mode(self) -> str:
        """``none`` / ``password`` / ``oidc`` / ``both`` / ``unreachable``."""
        url = self.current_url()
        if url is None:
            return "password"
        config = fetch_config(url)
        if config == _UNREACHABLE:
            return "unreachable"
        if not isinstance(config, dict):
            return "password"
        return config.get("auth_modes", "password")

    def oidc_providers(self) -> list[dict]:
        url = self.current_url()
        if url is None:
            return []
        config = fetch_config(url)
        if not isinstance(config, dict):
            return []
        return list(config.get("oidc_providers") or [])

    def allow_autostart(self) -> bool:
        """Whether the server permits per-workspace auto-start.

        Derived from ``allow_autostart`` in ``/api/v1/config`` (the same
        field the Flutter UI gates its checkbox on). Defaults to False on
        any failure so the TUI never offers a setting the server rejects.
        """
        url = self.current_url()
        if url is None:
            return False
        config = fetch_config(url)
        if not isinstance(config, dict):
            return False
        # Strict: the server serializes a Python bool, so require True exactly
        # (a string like "false" must not coerce to True).
        return config.get("allow_autostart") is True

    # --- login arms ---

    def login_password(self, identifier: str, password: str) -> str:
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            resp = http_request(
                url,
                "POST",
                "/api/v1/auth/login",
                json={"identifier": identifier, "password": password},
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise LoginError(f"could not reach server: {exc}") from None
        if resp.status_code != 200:
            detail = f"HTTP {resp.status_code}"
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise LoginError(detail)
        token = resp.json().get("access_token")
        if not token:
            raise LoginError("server returned no access token")
        state = self.state()
        state.set_credentials(url, identifier, token)
        state.save()
        return identifier

    def login_none(self) -> str:
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            email, token = local_login(url)
        except SystemExit as exc:
            raise LoginError("no-auth login failed") from exc
        state = self.state()
        state.set_credentials(url, email, token)
        state.save()
        return email

    def oidc_login(self, provider_id: str) -> None:
        """Delegate to the existing browser-based OIDC flow."""
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            _oidc_browser_login(url, provider_id, self.state())
        except SystemExit as exc:
            raise LoginError("OIDC login failed") from exc

    # --- account self-service (#1753) ---

    def get_me(self) -> dict:
        """Return the current user's profile ({id, email, handle})."""
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            return self.client().get_me()
        except httpx.HTTPStatusError as exc:
            raise LoginError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise LoginError(f"could not reach server: {exc}") from None

    def change_password(
        self, current_password: str, new_password: str
    ) -> None:
        """Change the current user's password."""
        if self.current_url() is None:
            raise LoginError("No server configured")
        try:
            self.client().change_password(current_password, new_password)
        except httpx.HTTPStatusError as exc:
            raise LoginError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise LoginError(f"could not reach server: {exc}") from None

    def change_handle(self, handle: str, password: str) -> str:
        """Change the current user's handle; returns the accepted handle."""
        if self.current_url() is None:
            raise LoginError("No server configured")
        try:
            accepted = self.client().change_handle(handle, password)
        except httpx.HTTPStatusError as exc:
            raise LoginError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise LoginError(f"could not reach server: {exc}") from None
        return accepted

    def change_email(self, email: str, password: str) -> None:
        """Change the current user's email and re-key cached credentials.

        The JWT subject is the user id, so the stored token stays valid;
        only the key it's filed under in klangk-state.yaml changes.
        """
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            self.client().change_email(email, password)
        except httpx.HTTPStatusError as exc:
            raise LoginError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise LoginError(f"could not reach server: {exc}") from None
        old = self.state().get_email(url)
        if old is not None and old != email:
            state = self.state()
            state.rename_user(url, old, email)
            state.save()

    def logout(self) -> None:
        url = self.current_url()
        state = self.state()
        if url is not None:
            state.clear_credentials(url)
            state.save()

    # --- server switching / adding ---

    def validate_server_for_switch(self, url: str) -> str:
        """Pre-flight check before switching to *url*.

        Returns ``"ok"``, ``"unreachable"``, or ``"auth_required"``.
        """
        config = fetch_config(url)
        if config == _UNREACHABLE:
            return "unreachable"
        if not isinstance(config, dict):
            return "unreachable"
        auth_mode = config.get("auth_modes", "password")
        if auth_mode == "none":
            return "ok"
        token = self.state().get_token(url)
        if token is None:
            return "auth_required"
        try:
            resp = http_request(
                url,
                "GET",
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            if resp.status_code == 401:
                return "auth_required"
        except httpx.HTTPError:
            return "unreachable"
        return "ok"

    def switch_server(self, url: str) -> None:
        state = self.state()
        state.active_server = url
        state.save()

    def add_server(
        self, alias: str, url: str, user: str | None = None
    ) -> None:
        add_server_to_config(alias, url, user)
        state = self.state()
        state.active_server = url
        state.save()

    def update_server(
        self,
        old_alias: str,
        new_alias: str,
        url: str,
        user: str | None = None,
    ) -> bool:
        """Update an existing server entry.

        Returns True if the alias was found and updated. If the URL changed
        and this was the active server, the active pointer is updated too.
        """
        cfg = self.cfg()
        old_entry = cfg.servers.get(old_alias)
        old_url = old_entry.url if old_entry else None
        if not update_server_in_config(old_alias, new_alias, url, user):
            return False
        state = self.state()
        if old_url and state.active_server == old_url:
            state.active_server = url
            state.save()
        return True

    def delete_server(self, url: str) -> bool:
        """Delete the alias pointing at *url*.

        Returns True if an alias was removed. If it was the active server,
        the active pointer is cleared (so ``current_url`` falls back to the
        default UDS or None) rather than left dangling.
        """
        cfg = self.cfg()
        aliases = [a for a, e in cfg.servers.items() if e.url == url]
        if not aliases:
            return False
        for a in aliases:
            remove_server_from_config(a)
        state = self.state()
        if state.active_server == url:
            state.active_server = None
            state.save()
        return True
