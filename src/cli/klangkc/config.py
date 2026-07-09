"""CLI configuration and state (~/.config/klangk/)."""

from __future__ import annotations


import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_CONFIG_DIR = Path.home() / ".config" / "klangk"
_CONFIG_PATH = _CONFIG_DIR / "cli.yaml"
_STATE_PATH = _CONFIG_DIR / "state.yaml"


_DEFAULT_WS_MAX_SIZE = 2**24  # 16 MB


@dataclass
class ServerEntry:
    """A named server in cli.yaml."""

    url: str
    user: str | None = None
    forward_agent: bool | None = None
    ws_max_size: int | None = None


@dataclass
class CLIConfig:
    """Parsed cli.yaml — user-edited, never written by the CLI."""

    forward_agent: bool | None = None
    ws_max_size: int | None = None
    servers: dict[str, ServerEntry] = field(default_factory=dict)

    @classmethod
    def load(cls) -> CLIConfig:
        if not _CONFIG_PATH.exists():
            return cls()
        text = _CONFIG_PATH.read_text()
        data = yaml.safe_load(text) or {}
        servers: dict[str, ServerEntry] = {}
        for name, entry in (data.get("servers") or {}).items():
            if not isinstance(entry, dict) or "url" not in entry:
                continue
            servers[name] = ServerEntry(
                url=entry["url"],
                user=entry.get("user"),
                forward_agent=entry.get("forward-agent"),
                ws_max_size=entry.get("ws-max-size"),
            )
        return cls(
            forward_agent=data.get("forward-agent"),
            ws_max_size=data.get("ws-max-size"),
            servers=servers,
        )

    def resolve_server(self, name_or_url: str) -> str:
        """Resolve a server alias to a URL, or return the URL as-is."""
        if name_or_url in self.servers:
            return self.servers[name_or_url].url
        return name_or_url

    def get_user(self, server_url: str) -> str | None:
        """Return default user for a server URL, or None."""
        for entry in self.servers.values():
            if entry.url == server_url and entry.user is not None:
                return entry.user
        return None

    def get_forward_agent(self, server_url: str) -> bool | None:
        """Return forward-agent for a server URL, falling back to global."""
        for entry in self.servers.values():
            if entry.url == server_url and entry.forward_agent is not None:
                return entry.forward_agent
        return self.forward_agent

    def get_ws_max_size(self, server_url: str) -> int:
        """Return ws-max-size for a server URL, falling back to global."""
        for entry in self.servers.values():
            if entry.url == server_url and entry.ws_max_size is not None:
                return entry.ws_max_size
        return self.ws_max_size or _DEFAULT_WS_MAX_SIZE


def seed_config(server_url: str, user: str | None = None) -> None:
    """Create cli.yaml with an initial server entry if it doesn't exist."""
    if _CONFIG_PATH.exists():
        return
    parsed = urlparse(server_url)
    alias = parsed.hostname or "default"
    entry: dict = {"url": server_url}
    if user:
        entry["user"] = user
    data = {"servers": {alias: entry}}
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False))


@dataclass
class UserEntry:
    """Per-user credentials within a server in state.yaml."""

    token: str | None = None


@dataclass
class ServerState:
    """Per-server state in state.yaml."""

    active_user: str | None = None
    users: dict[str, UserEntry] = field(default_factory=dict)


@dataclass
class CLIState:
    """Parsed state.yaml — auto-managed by the CLI."""

    active_server: str | None = None
    servers: dict[str, ServerState] = field(default_factory=dict)

    @classmethod
    def load(cls) -> CLIState:
        if not _STATE_PATH.exists():
            return cls()
        text = _STATE_PATH.read_text()
        data = yaml.safe_load(text) or {}
        active = data.get("active-server")
        servers: dict[str, ServerState] = {}
        for key, val in data.items():
            if key == "active-server":
                continue
            if not isinstance(val, dict):
                continue
            users: dict[str, UserEntry] = {}
            for uname, uval in (val.get("users") or {}).items():
                if isinstance(uval, dict):
                    users[uname] = UserEntry(token=uval.get("token"))
            servers[key] = ServerState(
                active_user=val.get("active-user"),
                users=users,
            )
        return cls(active_server=active, servers=servers)

    def save(self) -> None:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data: dict = {}
        if self.active_server is not None:
            data["active-server"] = self.active_server
        for url, ss in self.servers.items():
            server_data: dict = {}
            if ss.active_user is not None:
                server_data["active-user"] = ss.active_user
            users_data: dict = {}
            for uname, ue in ss.users.items():
                if ue.token is not None:
                    users_data[uname] = {"token": ue.token}
            if users_data:
                server_data["users"] = users_data
            if server_data:
                data[url] = server_data
        content = yaml.dump(data, default_flow_style=False)
        _STATE_PATH.write_text(content)
        os.chmod(_STATE_PATH, 0o600)

    def get_token(self, server_url: str) -> str | None:
        """Return the token for the active user on a server."""
        ss = self.servers.get(server_url)
        if not ss or not ss.active_user:
            return None
        ue = ss.users.get(ss.active_user)
        return ue.token if ue else None

    def get_email(self, server_url: str) -> str | None:
        """Return the active user (email/handle) for a server."""
        ss = self.servers.get(server_url)
        return ss.active_user if ss else None

    def set_credentials(self, server_url: str, user: str, token: str) -> None:
        """Store a token for a user on a server, set as active."""
        if server_url not in self.servers:
            self.servers[server_url] = ServerState()
        ss = self.servers[server_url]
        ss.users[user] = UserEntry(token=token)
        ss.active_user = user
        self.active_server = server_url

    def clear_credentials(self, server_url: str) -> None:
        """Clear all credentials for a server."""
        if server_url in self.servers:
            del self.servers[server_url]
        if self.active_server == server_url:
            self.active_server = None
