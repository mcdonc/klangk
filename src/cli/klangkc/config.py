"""CLI configuration storage (~/.config/klangk/cli.yaml)."""

from __future__ import annotations


import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path

_CONFIG_PATH = Path.home() / ".config" / "klangk" / "cli.yaml"


@dataclass
class ServerConfig:
    url: str = "http://localhost:8995"


@dataclass
class AuthConfig:
    token: str | None = None
    email: str | None = None


@dataclass
class CLIConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    forward_agent: bool | None = None

    @classmethod
    def load(cls) -> CLIConfig:
        if not _CONFIG_PATH.exists():
            return cls()
        text = _CONFIG_PATH.read_text()
        data = yaml.safe_load(text) or {}
        return cls(
            server=ServerConfig(
                url=data.get("server", {}).get("url", "http://localhost:8995")
            ),
            auth=AuthConfig(
                token=data.get("auth", {}).get("token"),
                email=data.get("auth", {}).get("email"),
            ),
            forward_agent=data.get("forward-agent"),
        )

    def save(self) -> None:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "server": {"url": self.server.url},
            "auth": {
                k: v
                for k, v in (
                    ("token", self.auth.token),
                    ("email", self.auth.email),
                )
                if v is not None
            },
        }
        content = yaml.dump(data, default_flow_style=False)
        _CONFIG_PATH.write_text(content)
        os.chmod(_CONFIG_PATH, 0o600)
