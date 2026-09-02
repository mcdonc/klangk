"""Live state bridge for the klangk TUI.

Reads ``CLIConfig`` / ``CLIState`` fresh on every access (no stale
snapshots), mirroring the server-side ``app``-ownership discipline so a
server switch or an external ``klangk login`` is reflected immediately.

Stays within ``klangk.cli`` (isolation rule): only stdlib, third-party
deps, and sibling ``cli`` modules.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import config as cli_config
from ..auth import (
    UNREACHABLE,
    oidc_browser_login,
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


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """Cheap change-detection stamp (mtime_ns, size) for a state file.

    None when the file is absent (a missing state file is CLIState.load's
    default-instance case). Read at call time off the ``config`` module
    attribute so tests (and a future env change) can retarget the path.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class LoginError(Exception):
    """Raised when an in-TUI login attempt fails."""


@dataclass(frozen=True)
class ServerInfo:
    """A known server alias + URL from klangk.yaml."""

    alias: str
    url: str


class TuiState:
    """Live bridge to CLIConfig / CLIState / KlangkClient.

    Config and state are re-checked on every call rather than cached at
    construction, so the TUI never acts on a stale snapshot after a server
    switch, an external login, or a token refresh. The state YAML is
    served from an mtime+size stamp cache: a StatusBar refresh reads it
    3+ times per event on the UI thread, and an unconditional
    read+parse per call made every push/pop and every status WS event pay
    three full file loads (#2029 audit). The stamp check keeps the
    freshness contract — any write (ours via save(), an external
    ``klangk login``, an editor) changes the stamp — while repeated reads
    between changes are free. Callers may mutate the returned CLIState
    (the load→mutate→save pattern); every save() in this class writes the
    new object straight back into the cache.
    """

    def __init__(self, server_url: str | None = None) -> None:
        # ``--server`` override; otherwise the active server from state.
        self._server_override = server_url
        # Cached /auth/me profile for the active server, so the detail
        # screen can filter a user's own shared windows out of the shared
        # list and the main screen can show the last login (#2583)
        # without a /me hit on every render. Refetched on server switch.
        self._me: dict | None = None
        self._me_url: str | None = None
        # Stamp cache for CLIState (#2029 audit; see class docstring).
        # ``_state_cache is None`` forces the first load; afterwards the
        # (mtime_ns, size) stamp comparison drives reloads.
        self._state_cache: CLIState | None = None
        self._state_stamp: tuple[int, int] | None = None
        # Guards the stamp cache AND every load→mutate→save→sync sequence
        # (#2029 review round 2): mutators run on textual worker threads
        # while the UI thread calls state() constantly, and a torn
        # two-field update (one writer's object paired with another
        # writer's stamp) pinned a stale object forever — the exact
        # wrong-server symptom this class exists to prevent. Reentrant so
        # state()/_save_state can nest inside a mutator's single hold;
        # NEVER held across network I/O (the HTTP calls finish before the
        # mutators acquire it).
        self._state_lock = threading.RLock()

    # --- fresh config / state each call ---

    def cfg(self) -> CLIConfig:
        return CLIConfig.load()

    def state(self) -> CLIState:
        """The CLI's state, served from the stamp cache (see class docstring).

        Accepted staleness window: a write that lands within the same
        mtime tick AND keeps the file size identical to the loaded version
        is missed (same strategy CPython uses for ``.pyc`` validity). Not
        realistic for this file — a credential swap changes its size, and
        every save in this class syncs the cache — but documented so the
        tradeoff is a decision, not an accident (#2029 review).
        """
        with self._state_lock:
            stamp = _file_stamp(cli_config.STATE_PATH)
            if self._state_cache is None or stamp != self._state_stamp:
                self._state_cache = CLIState.load()
                self._state_stamp = stamp
            return self._state_cache

    def _save_state(self, state: CLIState) -> None:
        """Save *state* and sync the stamp cache; drop the cache on failure.

        Must be called with ``_state_lock`` held so a mutator's whole
        load→mutate→save→sync sequence is atomic — two writers interleaving
        inside that sequence produced a torn (stale object, fresh stamp)
        pair that state() served forever (#2029 review round 2). A failed
        save drops the cache: the in-memory mutation exists nowhere on
        disk and must not be served as phantom credentials/state (the
        same rule oidc_login's unconditional drop follows).
        """
        try:
            state.save()
        except Exception:
            self._drop_state_cache()
            raise
        self._sync_state_cache(state)

    def _drop_state_cache(self) -> None:
        """Invalidate the stamp cache; the next ``state()`` reloads disk."""
        with self._state_lock:
            self._state_cache = None
            self._state_stamp = None

    def _sync_state_cache(self, state: CLIState) -> None:
        """Write a just-saved CLIState back into the stamp cache.

        Called only from :meth:`_save_state` with ``_state_lock`` held (the
        save changed the file and our mutation changed the object, so adopt
        both without waiting for the next stamp mismatch).
        """
        self._state_cache = state
        self._state_stamp = _file_stamp(cli_config.STATE_PATH)

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

    def _me_profile(self) -> dict | None:
        """The active server's ``GET /auth/me`` profile (cached).

        Fetched once per active server (cached like the old
        ``current_user_id`` fetch, #2164: keyed by URL, cleared on logout
        so a re-login as a different identity isn't served the previous
        user's profile). Returns None if it can't be resolved (no token,
        unreachable) so callers can degrade (e.g. skip filtering).
        """
        url = self.current_url()
        if url is None:
            return None
        if self._me is not None and self._me_url == url:
            return self._me
        token = self.token()
        if token is None:
            return None
        try:
            resp = http_request(
                url,
                "GET",
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            self._me = data
            self._me_url = url
            return data
        return None

    def current_user_id(self) -> str | None:
        """The authenticated user's id for the active server (cached)."""
        me = self._me_profile()
        return me["id"] if me is not None else None

    def last_login_at(self) -> str | None:
        """The user's last successful login (UTC ISO) for the active
        server, or None when unknown (#2583)."""
        me = self._me_profile()
        value = me.get("last_login_at") if me is not None else None
        return value if isinstance(value, str) else None

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

    def export_workspace(
        self,
        name: str,
        output: Path,
        on_progress=None,
    ) -> None:
        """Export a workspace to ``output`` (a .tar.gz path).

        ``on_progress(bytes_so_far, total_bytes_or_None)`` fires per chunk;
        ``total_bytes`` is ``None`` when the server omits ``Content-Length``.
        """
        ws = self.client().resolve_workspace(name)
        self.client().export_workspace(ws.id, output, on_progress=on_progress)

    def import_workspace(
        self,
        archive: Path,
        name: str | None = None,
        on_progress=None,
    ) -> Workspace:
        """Upload ``archive`` (a .tar.gz) and return the created workspace.

        ``on_progress(bytes_so_far, total_bytes)`` fires as bytes are read.
        """
        return self.client().import_workspace(
            archive, name=name, on_progress=on_progress
        )

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
        rejected_domains: list[str] | None = None,
        settings: dict | None = None,
        egress_mode: str | None = None,
        per_handle_home: bool | None = None,
        classification_banner: str | None = None,
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
            rejected_domains=rejected_domains,
            settings=settings,
            egress_mode=egress_mode,
            per_handle_home=per_handle_home,
            classification_banner=classification_banner,
        )

    def update_workspace(self, workspace_id: str, **fields) -> None:
        """Partial-update a workspace's fields (used by the TUI edit form, #1778)."""
        self.client().update_workspace(workspace_id, **fields)

    def list_images(self) -> dict:
        return self.client().list_images()

    def deploy_toggles(self) -> tuple[bool, bool]:
        """Deploy-level nix/sudo availability (#2974).

        Moved off the ``/images`` payload to the authenticated-only
        ``/config`` fields — deployment config, not image data. Strict
        bool checks (``allow_autostart`` precedent). Raises on failure;
        callers fall back to ``(False, False)`` so the form still opens.
        """
        cfg = self.client().config()
        return (
            cfg.get("nix_available") is True,
            cfg.get("sudo_available") is True,
        )

    def default_allowed_domains(self) -> list[str]:
        """Deploy-wide netfilter allow-list (``KLANGKD_NETFILTER_DEFAULT_DOMAINS``)
        used to seed the create form's Netfilter tab (#1931).

        Auth-gated on ``/api/v1/config`` (absent from the pre-auth payload),
        so this uses the authed client — ``fetch_config`` would not see it.
        Mirrors the Flutter dialog's ``defaultAllowedDomains``. Raises on a
        transport/server error; ``do_create`` catches that and falls back
        to an empty list so the form still opens.
        """
        cfg = self.client().config()
        doms = cfg.get("netfilter_default_domains")
        return list(doms) if isinstance(doms, list) else []

    async def list_terminals(self, name: str) -> list[dict]:
        return await self.client().list_terminals(name)

    async def list_shared_terminals(self, name: str) -> list[dict]:
        return await self.client().list_shared_terminals(name)

    async def close_terminal(self, name: str, window_id: str) -> list[dict]:
        return await self.client().close_terminal(name, window_id)

    async def create_terminal(
        self, name: str, window_name: str | None = None
    ) -> list[dict]:
        return await self.client().create_terminal(name, window_name)

    async def rename_terminal(
        self, name: str, index: int, new_name: str
    ) -> list[dict]:
        return await self.client().rename_terminal(name, index, new_name)

    # --- auth mode (probed live via /config) ---

    def auth_mode(self) -> str:
        """``none`` / ``password`` / ``oidc`` / ``both`` / ``unreachable``."""
        url = self.current_url()
        if url is None:
            return "password"
        config = fetch_config(url)
        if config == UNREACHABLE:
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

    def default_per_handle_home(self) -> bool | None:
        """Deploy default home layout for NEW workspaces (#2721).

        ``default_per_handle_home`` in ``/api/v1/config``
        (KLANGKD_PER_HANDLE_HOME). The create form's checkbox pre-reflects
        this so an untouched form submits the server's default.

        Returns ``None`` when the default is UNKNOWN (no server / fetch
        failure): the caller then hides the checkbox and OMITS the field,
        so the server applies its own default — a config hiccup can
        never silently force a layout onto a shared-home deploy (#2737
        review). A fetched config that merely LACKS the key (old server)
        returns ``True`` — per-handle is the historical behavior those
        servers implement.
        """
        url = self.current_url()
        if url is None:
            return None
        config = fetch_config(url)
        if not isinstance(config, dict):
            return None
        val = config.get("default_per_handle_home")
        return True if val is None else bool(val)

    def default_classification_banner(self) -> str:
        """Deploy-wide default classification marking (#2768).

        ``default_classification_banner`` in ``/api/v1/config"
        (KLANGKD_CLASSIFICATION_BANNER; pre-auth payload). Empty string —
        the default — when unset or unreachable: the caller renders no
        banner and reserves no screen space.
        """
        url = self.current_url()
        if url is None:
            return ""
        config = fetch_config(url)
        if not isinstance(config, dict):
            return ""
        val = config.get("default_classification_banner")
        return val.strip() if isinstance(val, str) else ""

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
        with self._state_lock:
            state = self.state()
            state.set_credentials(url, identifier, token)
            self._save_state(state)
        return identifier

    def login_none(self) -> str:
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            email, token = local_login(url)
        except SystemExit as exc:
            raise LoginError("no-auth login failed") from exc
        with self._state_lock:
            state = self.state()
            state.set_credentials(url, email, token)
            self._save_state(state)
        return email

    def oidc_login(self, provider_id: str) -> None:
        """Delegate to the existing browser-based OIDC flow."""
        url = self.current_url()
        if url is None:
            raise LoginError("No server configured")
        try:
            oidc_browser_login(url, provider_id, self.state())
        except SystemExit as exc:
            raise LoginError("OIDC login failed") from exc
        finally:
            # The browser flow mutates + saves the state object itself
            # (#2029 review): if its save() failed (disk full, quota) the
            # file never changed, but the mutated object IS our cached one
            # — phantom credentials served forever. Drop the cache either
            # way; the next state() reloads exactly what is on disk.
            self._drop_state_cache()

    def logout(self) -> None:
        url = self.current_url()
        with self._state_lock:
            state = self.state()
            if url is not None:
                state.clear_credentials(url)
                self._save_state(state)
        # Drop the cached /auth/me profile so a re-login as a different
        # identity on the same server isn't served the previous user's
        # profile (#2164 review: the cache is keyed by URL, not identity).
        self._me = None
        self._me_url = None

    # --- server switching / adding ---

    def validate_server_for_switch(self, url: str) -> str:
        """Pre-flight check before switching to *url*.

        Returns ``"ok"``, ``"unreachable"``, or ``"auth_required"``.
        """
        config = fetch_config(url)
        if config == UNREACHABLE:
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
        with self._state_lock:
            state = self.state()
            state.active_server = url
            self._save_state(state)

    def add_server(
        self, alias: str, url: str, user: str | None = None
    ) -> None:
        add_server_to_config(alias, url, user)
        with self._state_lock:
            state = self.state()
            state.active_server = url
            self._save_state(state)

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
        with self._state_lock:
            state = self.state()
            if old_url and state.active_server == old_url:
                state.active_server = url
                self._save_state(state)
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
        with self._state_lock:
            state = self.state()
            if state.active_server == url:
                state.active_server = None
                self._save_state(state)
        return True
