"""CLI configuration and state.

The CLI's config and state live under the XDG config / state trees, in a
``klangk`` subdir (distinct from the server's ``klangkd`` tree — different
audiences, different shapes; the CLI's state is a few hundred bytes of
user tokens, the server's is GB-scale DBs + UDS). See #1646.

- ``$XDG_CONFIG_HOME/klangk/klangk.yaml`` — user-edited config (servers,
  preferences). Read with the XDG fallback (~/.config).
- ``$XDG_STATE_HOME/klangk/klangk-state.yaml`` — disposable app-managed state
  (login tokens + active server). Read with the XDG fallback (~/.local/state).
"""

from __future__ import annotations


import contextlib
import logging
import os
import shlex
import tempfile
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _xdg_config_home() -> Path:
    """Return ``$XDG_CONFIG_HOME`` with the documented unset fallback.

    Per the XDG base-dir spec, an unset ``XDG_CONFIG_HOME`` resolves to
    ``~/.config``. Applies on Linux *and* macOS (no ~/Library special-case,
    matching the server's #1607 cross-platform note).
    """
    return Path(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    )


def _xdg_state_home() -> Path:
    """Return ``$XDG_STATE_HOME`` with the documented unset fallback.

    Per the XDG base-dir spec, an unset ``XDG_STATE_HOME`` resolves to
    ``~/.local/state``. Linux *and* macOS.
    """
    return Path(
        os.environ.get("XDG_STATE_HOME")
        or os.path.expanduser("~/.local/state")
    )


# The CLI's XDG subdir (the binary name ``klangk``).
_CLI_SUBDIR = "klangk"

CONFIG_PATH = _xdg_config_home() / _CLI_SUBDIR / "klangk.yaml"
STATE_PATH = _xdg_state_home() / _CLI_SUBDIR / "klangk-state.yaml"


DEFAULT_WS_MAX_SIZE = 2**24  # 16 MB


# Envvar override for ``terminal-open-cmd`` — ``KLANGKC_<FIELD>``
# convention (#2685). Set (non-empty) it wins over the yaml value.
TERMINAL_OPEN_CMD_ENV = "KLANGKC_TERMINAL_OPEN_CMD"


# Server-side XDG subdir + socket filename, mirrored from
# ``settings.py`` (``_XDG_SUBDIR = "klangkd"``; socket =
# ``<state_dir>/klangk.sock``) so the CLI can locate a co-located
# ``klangkd``'s default UDS without importing from the server package
# (``klangk.cli`` isolation rule). Named constants make the mirroring
# grep-able if the server renames either.
_SERVER_XDG_SUBDIR = "klangkd"
_SOCKET_NAME = "klangk.sock"


def default_server_uds_path() -> str:
    """Return the UDS path a co-located ``klangkd`` binds by default.

    Mirrors the server's derivation so a single-host ``klangkd`` +
    ``klangk`` works with no ``klangk login`` step (#1676). Resolution
    order, matching the server:

    1. ``KLANGK_SOCKET`` — if an explicit *plain absolute* path (not a
       ``file:``/``cmd:`` indirection, which the server resolves by
       running a cmd / reading a file and the CLI can't reproduce), the
       server binds exactly there, so return it directly.
    2. ``KLANGK_STATE_DIR/klangk.sock`` when ``KLANGK_STATE_DIR`` is set.
    3. ``$XDG_STATE_HOME/klangkd/klangk.sock`` (→ ~/.local/state/klangkd/…).

    Replicated in ``klangk.cli`` — not imported from the server — because
    the CLI runs in a different environment (``klangk.cli`` isolation
    rule). The ``file:``/``cmd:`` ``KLANGK_SOCKET`` indirection case is
    not reproduced; operators who relocate the socket that way still need
    a one-time ``klangk login``.
    """
    explicit = os.environ.get("KLANGK_SOCKET")
    if explicit and explicit.startswith("/"):
        # An absolute value is a plain path the server binds verbatim;
        # file:/cmd: indirections don't start with "/" and fall through.
        return explicit
    state_dir = os.environ.get("KLANGK_STATE_DIR")
    if not state_dir:
        state_dir = os.path.join(str(_xdg_state_home()), _SERVER_XDG_SUBDIR)
    return os.path.join(state_dir, _SOCKET_NAME)


def _split_terminal_cmd(text: str) -> list[str] | None:
    """shlex-split a terminal command string, ignoring syntax errors.

    An unbalanced quote raises ``ValueError`` out of ``shlex.split`` — a
    one-character typo in klangk.yaml / the envvar must not crash every
    CLI command that loads the config (or the TUI mid-message-handler),
    so the value degrades to None (inline shell) instead (#2686 review).
    """
    try:
        return shlex.split(text)
    except ValueError:
        return None


def terminal_cmd_from_str(value: str) -> list[str] | None:
    """A shell-string terminal-open-cmd, shlex-split."""
    value = value.strip()
    return _split_terminal_cmd(value) if value else None


def terminal_cmd_from_list(value: list) -> list[str] | None:
    """A list-of-strings terminal-open-cmd, copied (None when mistyped)."""
    if not all(isinstance(v, str) for v in value):
        return None
    return list(value) if value else None


def parse_terminal_open_cmd(value) -> list[str] | None:
    """Normalize a ``terminal-open-cmd`` yaml value to an argv list.

    Accepts a shell string (``"konsole --hold -e"``, split with shlex) or
    a list of strings (``[konsole, --hold, -e]``). Empty, wrong-typed, and
    unparseable values are ignored (None) so a bad edit degrades to the
    inline shell instead of crashing the CLI/TUI (#2685).
    """
    if isinstance(value, str):
        return terminal_cmd_from_str(value)
    if isinstance(value, list):
        return terminal_cmd_from_list(value)
    return None


@dataclass
class ServerEntry:
    """A named server in klangk.yaml."""

    url: str
    user: str | None = None
    forward_agent: bool | None = None
    ws_max_size: int | None = None


def parse_server_entries(data: dict) -> dict[str, ServerEntry]:
    """ServerEntry map from the klangk.yaml ``servers`` section."""
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
    return servers


@dataclass
class CLIConfig:
    """Parsed klangk.yaml — user-edited, never written by the CLI."""

    forward_agent: bool | None = None
    ws_max_size: int | None = None
    terminal_open_cmd: list[str] | None = None
    servers: dict[str, ServerEntry] = field(default_factory=dict)

    @classmethod
    def load(cls) -> CLIConfig:
        data = load_yaml_config()
        return cls(
            forward_agent=data.get("forward-agent"),
            ws_max_size=data.get("ws-max-size"),
            terminal_open_cmd=parse_terminal_open_cmd(
                data.get("terminal-open-cmd")
            ),
            servers=parse_server_entries(data),
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
        return self.ws_max_size or DEFAULT_WS_MAX_SIZE

    def get_terminal_open_cmd(self) -> list[str] | None:
        """Return the argv that opens a new terminal window, or None.

        ``KLANGKC_TERMINAL_OPEN_CMD`` (a shell string) wins over the yaml
        ``terminal-open-cmd`` value, so a single ``export`` redirects TUI
        shell launches without editing klangk.yaml (#2685). An empty or
        unset envvar falls through to the file value; None means "not
        configured" — launch the shell inline (current behavior).
        """
        env = os.environ.get(TERMINAL_OPEN_CMD_ENV, "").strip()
        if env:
            return _split_terminal_cmd(env)
        return self.terminal_open_cmd


def ensure_config() -> None:
    """Create a default klangk.yaml if one doesn't already exist.

    Called early on every CLI invocation so the config file is always
    present, even before the user logs in.
    """
    if CONFIG_PATH.exists():
        return
    header = (
        "# SSH agent forwarding is ON by default so your workspace can use\n"
        "# your loaded SSH keys (e.g. git push). Set it to false here (or\n"
        "# per-server) if you don't trust the workspace: while forwarded,\n"
        "# anyone who can reach the agent socket on the remote host can\n"
        "# authenticate as you with your loaded keys for the session.\n"
        "# See docs/features/ssh-agent-forwarding.md.\n"
        "forward-agent: true\n\n"
    )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_PATH.write_text(header + "servers: {}\n")


def seed_config(server_url: str, user: str | None = None) -> None:
    """Create klangk.yaml with an initial server entry if it doesn't exist."""
    if CONFIG_PATH.exists():
        return
    parsed = urlparse(server_url)
    alias = parsed.hostname or "default"
    entry: dict = {"url": server_url}
    if user:
        entry["user"] = user
    # forward-agent defaults to true (a generated config sets it on globally)
    # so a workspace can use the operator's loaded SSH keys; the header below
    # notes how to disable it for an untrusted workspace (#1923).
    servers_yaml = yaml.dump(
        {"servers": {alias: entry}}, default_flow_style=False
    )
    header = (
        "# SSH agent forwarding is ON by default so your workspace can use\n"
        "# your loaded SSH keys (e.g. git push). Set it to false here (or\n"
        "# per-server) if you don't trust the workspace: while forwarded,\n"
        "# anyone who can reach the agent socket on the remote host can\n"
        "# authenticate as you with your loaded keys for the session.\n"
        "# See docs/features/ssh-agent-forwarding.md.\n"
        "forward-agent: true\n\n"
    )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_PATH.write_text(header + servers_yaml)


def load_yaml_config() -> dict:
    """The parsed klangk.yaml (empty when absent or not a mapping).

    klangk.yaml is user-edited: a document that is valid YAML but not a
    mapping (a stray list, a bare string) must not crash every CLI
    command with ``AttributeError`` — it degrades to an empty config
    (#3094), the same degrade-not-crash rule as a bad
    ``terminal-open-cmd`` (#2685). An *unparseable* document (YAML
    syntax error) degrades the same way, with a one-line warning so the
    user learns the file was ignored (#3111).
    """
    if not CONFIG_PATH.exists():
        return {}
    data = _safe_load_yaml(CONFIG_PATH)
    return data if isinstance(data, dict) else {}


def _safe_load_yaml(path: Path):
    """``yaml.safe_load`` for a user-facing file (None on parse error).

    A YAML syntax error must not crash every CLI command with a raw
    ``yaml.YAMLError`` traceback (#3111) — it degrades to an empty
    document (None, which the callers coerce to ``{}``), with a
    one-line warning routed through the logging last-resort handler
    (stderr) so the user learns the file was ignored.
    """
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        logging.getLogger(__name__).warning(
            "%s: YAML parse error ignored, treating as empty: %s",
            path.name,
            detail,
        )
        return None


def add_server_to_config(
    alias: str, server_url: str, user: str | None = None
) -> None:
    """Add a named server entry in klangk.yaml.

    Unlike ``seed_config`` (one-shot, only when the file is absent), this
    merges into an existing user config so the TUI can add a server alias
    interactively without clobbering the rest of the file. klangk.yaml
    remains user-owned; this is the one managed write, used only by the
    TUI's add-server flow.

    Raises ``AliasConflictError`` if *alias* already exists — callers
    must catch the error and surface it to the user (#1763).
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = load_yaml_config()
    servers = data.get("servers") or {}
    if alias in servers:
        raise AliasConflictError(f"Alias '{alias}' already exists.")
    entry: dict = {"url": server_url}
    if user:
        entry["user"] = user
    servers[alias] = entry
    data["servers"] = servers
    CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False))


class AliasConflictError(Exception):
    """Raised when renaming a server alias to one that already exists."""


def check_alias_renaming(
    old_alias: str, new_alias: str, servers: dict
) -> None:
    """Reject a rename onto an existing alias (same-alias updates pass)."""
    if old_alias != new_alias and new_alias in servers:
        raise AliasConflictError(f"Alias '{new_alias}' already exists.")


def replaced_server_entry(existing, server_url: str, user) -> dict:
    """The updated server entry, preserving fields the edit form omits
    (forward-agent, ws-max-size)."""
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry["url"] = server_url
    if user:
        entry["user"] = user
    return entry


def update_server_in_config(
    old_alias: str,
    new_alias: str,
    server_url: str,
    user: str | None = None,
) -> bool:
    """Update an existing server entry in klangk.yaml.

    If *old_alias* differs from *new_alias* the entry is renamed.
    Returns True if the alias was found and updated, False otherwise.
    Raises ``AliasConflictError`` if *new_alias* already exists under
    a different key.
    """
    if not CONFIG_PATH.exists():
        return False
    data = load_yaml_config()
    servers = data.get("servers") or {}
    if old_alias not in servers:
        return False
    check_alias_renaming(old_alias, new_alias, servers)
    entry = replaced_server_entry(servers[old_alias], server_url, user)
    if old_alias != new_alias:
        del servers[old_alias]
    servers[new_alias] = entry
    data["servers"] = servers
    CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False))
    return True


def remove_server_from_config(alias: str) -> bool:
    """Remove a named server entry from klangk.yaml.

    Returns True if the alias was present and removed, False otherwise.
    The counterpart to ``add_server_to_config`` (TUI delete-server flow).
    """
    if not CONFIG_PATH.exists():
        return False
    data = load_yaml_config()
    servers = data.get("servers") or {}
    if alias not in servers:
        return False
    del servers[alias]
    data["servers"] = servers
    CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False))
    return True


def load_yaml_state() -> dict:
    """The parsed klangk-state.yaml (empty when absent or not a mapping).

    klangk-state.yaml is written by ``CLIState.save()`` and never
    hand-edited — but corruption is realistic (an interrupted write, a
    stray edit): a document that is valid YAML but not a mapping, or
    not valid YAML at all, must not crash every CLI command — it
    degrades to an empty state so commands run unauthenticated and
    ``klangk login`` works as the repair flow (#3111).
    """
    if not STATE_PATH.exists():
        return {}
    data = _safe_load_yaml(STATE_PATH)
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace *path* with *content* (mode 0600).

    Writes to a temp file in the same directory, then ``os.replace`` —
    an interrupted write (crash, kill, full disk) can only lose the
    temp file, never leave a truncated state file behind (#3111).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass
class UserEntry:
    """Per-user credentials within a server in klangk-state.yaml."""

    token: str | None = None


@dataclass
class ServerState:
    """Per-server state in klangk-state.yaml."""

    active_user: str | None = None
    users: dict[str, UserEntry] = field(default_factory=dict)


def parse_user_entries(val: dict) -> dict[str, UserEntry]:
    """UserEntry map from one state server section."""
    users: dict[str, UserEntry] = {}
    for uname, uval in (val.get("users") or {}).items():
        if isinstance(uval, dict):
            users[uname] = UserEntry(token=uval.get("token"))
    return users


def parse_server_states(data: dict) -> dict[str, ServerState]:
    """ServerState map from the klangk-state.yaml data."""
    servers: dict[str, ServerState] = {}
    for key, val in data.items():
        if key == "active-server":
            continue
        if not isinstance(val, dict):
            continue
        servers[key] = ServerState(
            active_user=val.get("active-user"),
            users=parse_user_entries(val),
        )
    return servers


def server_state_data(ss: ServerState) -> dict:
    """One ServerState as its klangk-state.yaml section ({} when empty)."""
    server_data: dict = {}
    if ss.active_user is not None:
        server_data["active-user"] = ss.active_user
    users_data = {
        uname: {"token": ue.token}
        for uname, ue in ss.users.items()
        if ue.token is not None
    }
    if users_data:
        server_data["users"] = users_data
    return server_data


@dataclass
class CLIState:
    """Parsed klangk-state.yaml — auto-managed by the CLI."""

    active_server: str | None = None
    servers: dict[str, ServerState] = field(default_factory=dict)

    @classmethod
    def load(cls) -> CLIState:
        data = load_yaml_state()
        return cls(
            active_server=data.get("active-server"),
            servers=parse_server_states(data),
        )

    def save(self) -> None:
        data: dict = {}
        if self.active_server is not None:
            data["active-server"] = self.active_server
        for url, ss in self.servers.items():
            server_data = server_state_data(ss)
            if server_data:
                data[url] = server_data
        _atomic_write(STATE_PATH, yaml.dump(data, default_flow_style=False))

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

    def rename_user(
        self, server_url: str, old_user: str, new_user: str
    ) -> None:
        """Re-key cached credentials after a self-service email change.

        The JWT's subject is the user id (not the email), so the stored
        token stays valid across an email change — only the key it's filed
        under changes. No parallel store: the entry moves in place (#1753).

        Mutates the in-memory state only; the caller must ``save()`` to
        persist (same convention as ``set_credentials``).
        """
        ss = self.servers.get(server_url)
        if not ss or old_user not in ss.users:
            return
        ss.users[new_user] = ss.users.pop(old_user)
        if ss.active_user == old_user:
            ss.active_user = new_user

    def clear_credentials(self, server_url: str) -> None:
        """Clear all credentials for a server."""
        if server_url in self.servers:
            del self.servers[server_url]
        if self.active_server == server_url:
            self.active_server = None
