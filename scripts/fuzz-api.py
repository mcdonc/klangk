#!/usr/bin/env python3
"""API fuzz tester for klangk.

Starts a klangk backend server, then sends randomized API requests with
fuzzed values for a configurable duration.  Watches responses and server
logs for anomalies (5xx errors, unhandled exceptions, crashes).

Usage:
    scripts/fuzz-api.py [--duration MINUTES] [--seed SEED]   # fuzz
    scripts/fuzz-api.py --check                                # drift gate

The script:
  - Starts its own klangkd server in a subprocess (bound to a Unix socket
    in a temp state dir; the proxy suppressed) and talks to it over the UDS
  - Logs in as admin and uses the token for authenticated requests
  - Generates random payloads: valid-ish values, boundary values, type
    confusion, oversized strings, unicode, null bytes, nested objects
  - Fires requests at every known endpoint with fuzzed parameters
  - Collects all 5xx responses and stderr output from the server
  - Prints a summary report at the end
  - ``--check`` diffs the declared ENDPOINTS against the live router's
    OpenAPI schema (no server started) and exits 1 on drift

Exit code 0 = no anomalies (or, with --check, no drift), 1 = anomalies / drift.
"""

import argparse
import asyncio
import io
import json
import logging
import os
import random
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import httpx

from fuzzlib import configure_logging, draw_seed, uds_login

logger = logging.getLogger("fuzz")

# ---------------------------------------------------------------------------
# Fuzz value generators
# ---------------------------------------------------------------------------


def fuzz_string(rng: random.Random) -> str:
    """Return a random string — sometimes nasty."""
    generators = [
        # normal short string
        lambda: "".join(
            rng.choices(string.ascii_letters + string.digits, k=rng.randint(1, 20))
        ),
        # empty
        lambda: "",
        # very long
        lambda: "A" * rng.randint(1000, 50000),
        # unicode
        lambda: "".join(
            chr(rng.randint(0x0100, 0xFFFF)) for _ in range(rng.randint(1, 100))
        ),
        # null bytes
        lambda: "hello\x00world",
        # newlines / control chars
        lambda: "line1\nline2\r\nline3\ttab",
        # SQL injection attempt
        lambda: "'; DROP TABLE users; --",
        # XSS attempt
        lambda: '<script>alert("xss")</script>',
        # path traversal
        lambda: "../../etc/passwd",
        # format string
        lambda: "%s%s%s%s%s%n",
        # JSON in string
        lambda: '{"nested": true}',
        # email-like
        lambda: f"fuzz{rng.randint(0, 9999)}@example.com",
        # just whitespace
        lambda: "   \t\n  ",
        # emoji
        lambda: "🔥💀🎉" * rng.randint(1, 20),
    ]
    return rng.choice(generators)()


def fuzz_int(rng: random.Random):
    """Return a random int-ish value — sometimes wrong type."""
    generators = [
        lambda: rng.randint(0, 100),
        lambda: 0,
        lambda: -1,
        lambda: -999999,
        lambda: 2**31 - 1,
        lambda: 2**63 - 1,
        lambda: rng.randint(1, 10),
    ]
    return rng.choice(generators)()


def fuzz_value(rng: random.Random):
    """Return a random value of any JSON type."""
    generators = [
        lambda: fuzz_string(rng),
        lambda: fuzz_int(rng),
        lambda: rng.random() * 1000,
        lambda: rng.choice([True, False]),
        lambda: None,
        lambda: [fuzz_string(rng) for _ in range(rng.randint(0, 5))],
        lambda: {fuzz_string(rng): fuzz_string(rng) for _ in range(rng.randint(0, 3))},
    ]
    return rng.choice(generators)()


def fuzz_email(rng: random.Random) -> str:
    choices = [
        f"fuzz{rng.randint(0, 99999)}@example.com",
        "",
        "not-an-email",
        "@",
        "a" * 500 + "@example.com",
        "valid@example.com",
        "<script>@evil.com",
    ]
    return rng.choice(choices)


def fuzz_password(rng: random.Random) -> str:
    choices = [
        "validpass123",
        "",
        "a",
        "A" * 10000,
        "pass\x00word",
        "🔑🔑🔑",
    ]
    return rng.choice(choices)


def fuzz_path(rng: random.Random) -> str:
    """Return a random file path — sometimes malicious."""
    generators = [
        # normal absolute path
        lambda: (
            "/home/klangk/"
            + "".join(rng.choices(string.ascii_lowercase, k=rng.randint(1, 10)))
            + ".txt"
        ),
        # root
        lambda: "/",
        # relative (should be rejected)
        lambda: "../../etc/passwd",
        # null byte
        lambda: "/home/\x00evil",
        # very long component
        lambda: "/" + "a" * rng.randint(200, 500),
        # shell metacharacters
        lambda: "/home/; rm -rf /",
        lambda: "/home/$(whoami)",
        lambda: "/home/`id`",
        lambda: "/home/file | cat /etc/shadow",
        # flag injection
        lambda: "/-rf",
        lambda: "/--help",
        # newlines
        lambda: "/home/file\nworld",
        # empty
        lambda: "",
        # deep nesting
        lambda: "/" + "/".join("d" for _ in range(rng.randint(10, 100))),
        # unicode
        lambda: (
            "/home/"
            + "".join(
                chr(rng.randint(0x0100, 0xFFFF)) for _ in range(rng.randint(1, 20))
            )
        ),
        # spaces
        lambda: "/home/my file (1).txt",
        # dot segments
        lambda: "/home/./work/../../../etc/shadow",
        # double slash
        lambda: "//home//work",
        # just a dot
        lambda: ".",
    ]
    return rng.choice(generators)()


def fuzz_uuid(rng: random.Random) -> str:
    choices = [
        str(uuid.uuid4()),
        "not-a-uuid",
        "",
        "00000000-0000-0000-0000-000000000000",
        "../../../etc/passwd",
        "'; DROP TABLE users; --",
    ]
    return rng.choice(choices)


def fuzz_body(rng: random.Random, schema: dict[str, str]) -> dict:
    """Build a fuzzed JSON body from a schema like {"email": "email", "password": "password"}."""
    generators = {
        "email": fuzz_email,
        "password": fuzz_password,
        "path": fuzz_path,
        "string": fuzz_string,
        "uuid": fuzz_uuid,
        "int": fuzz_int,
        "value": fuzz_value,
    }
    body = {}
    for key, kind in schema.items():
        # Sometimes omit fields
        if rng.random() < 0.15:
            continue
        # Sometimes add with wrong type
        if rng.random() < 0.1:
            body[key] = fuzz_value(rng)
        else:
            body[key] = generators.get(kind, fuzz_string)(rng)
    # Sometimes add extra unknown fields
    if rng.random() < 0.2:
        body[fuzz_string(rng)] = fuzz_value(rng)
    return body


def fuzz_query(rng: random.Random, params: dict[str, str]) -> dict:
    """Build fuzzed query parameters."""
    result = {}
    generators = {
        "path": fuzz_path,
        "string": fuzz_string,
        "uuid": fuzz_uuid,
        "int": fuzz_int,
    }
    for key, kind in params.items():
        if rng.random() < 0.15:
            continue
        result[key] = str(generators.get(kind, fuzz_string)(rng))
    return result


# ---------------------------------------------------------------------------
# Endpoint definitions
# ---------------------------------------------------------------------------

# Each entry: (method, path_template, body_schema_or_None, query_params_or_None)
# path_template can contain {workspace_id}, {member_id}, etc.

P = "/api/v1"  # all routes except /health are under this prefix

ENDPOINTS: list[tuple[str, str, dict | None, dict | None]] = [
    # Public (root_router — no prefix)
    ("GET", "/health", None, None),
    ("GET", "/empty", None, None),  # OAuth callback landing page
    # Public (router — /api/v1 prefix)
    ("GET", f"{P}/version", None, None),
    ("GET", f"{P}/config", None, None),
    # Auth — no token
    ("POST", f"{P}/auth/register", {"email": "email", "password": "password"}, None),
    ("POST", f"{P}/auth/login", {"email": "email", "password": "password"}, None),
    ("GET", f"{P}/auth/verify", None, {"token": "string"}),
    (
        "POST",
        f"{P}/auth/resend-verification",
        {"email": "email", "password": "password"},
        None,
    ),
    ("POST", f"{P}/auth/forgot-password", {"email": "email"}, None),
    (
        "POST",
        f"{P}/auth/reset-password",
        {"token": "string", "password": "password"},
        None,
    ),
    (
        "POST",
        f"{P}/auth/accept-invite",
        {"token": "string", "password": "password"},
        None,
    ),
    # Auth — with token
    ("POST", f"{P}/auth/local", None, None),  # no-auth mode; 403 in password mode
    ("POST", f"{P}/auth/refresh", None, None),
    (
        "POST",
        f"{P}/auth/change-password",
        {"current_password": "password", "new_password": "password"},
        None,
    ),
    (
        "POST",
        f"{P}/auth/change-email",
        {"email": "email", "password": "password"},
        None,
    ),
    (
        "POST",
        f"{P}/auth/change-handle",
        {"handle": "string", "password": "password"},
        None,
    ),
    ("GET", f"{P}/auth/me", None, None),
    ("POST", f"{P}/auth/logout", None, None),
    # OIDC
    ("GET", f"{P}/auth/oidc/{{provider_id}}/login", None, None),
    (
        "GET",
        f"{P}/auth/oidc/{{provider_id}}/callback",
        None,
        {"code": "string", "state": "string"},
    ),
    # Auth — workspace token
    ("GET", f"{P}/auth/verify-workspace-token", None, {"token": "string"}),
    # Workspaces
    ("GET", f"{P}/workspaces", None, None),
    ("GET", f"{P}/workspaces/shared", None, None),
    (
        "POST",
        f"{P}/workspaces",
        {"name": "string", "image": "string", "service_command": "string"},
        None,
    ),
    (
        "PUT",
        f"{P}/workspaces/{{workspace_id}}",
        {"name": "string", "image": "string"},
        None,
    ),
    (
        "PATCH",
        f"{P}/workspaces/{{workspace_id}}/settings",
        {"idle_timeout": "int", "cpu_limit": "float"},
        None,
    ),
    ("DELETE", f"{P}/workspaces/{{workspace_id}}", None, None),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/duplicate",
        {"name": "string"},
        None,
    ),
    ("POST", f"{P}/workspaces/{{workspace_id}}/restart", None, None),
    ("POST", f"{P}/workspaces/{{workspace_id}}/start", None, None),
    ("POST", f"{P}/workspaces/{{workspace_id}}/stop", None, None),
    ("GET", f"{P}/workspaces/{{workspace_id}}/status", None, None),
    ("GET", f"{P}/workspaces/{{workspace_id}}/export", None, None),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/transfer",
        {"email": "email"},
        None,
    ),  # transfer ownership
    ("POST", f"{P}/workspaces/import", None, None),  # multipart upload
    # Workspace members
    ("GET", f"{P}/workspaces/{{workspace_id}}/members", None, None),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/members",
        {"email": "email"},
        None,
    ),
    (
        "DELETE",
        f"{P}/workspaces/{{workspace_id}}/members/{{member_id}}",
        None,
        None,
    ),
    ("GET", f"{P}/workspaces/{{workspace_id}}/roles", None, None),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/roles/{{role}}",
        {"email": "email"},
        None,
    ),
    (
        "DELETE",
        f"{P}/workspaces/{{workspace_id}}/roles/{{role}}/{{member_id}}",
        None,
        None,
    ),
    (
        "PATCH",
        f"{P}/workspaces/{{workspace_id}}/roles",
        {"email": "email", "role": "string"},
        None,
    ),
    ("GET", f"{P}/workspaces/{{workspace_id}}/groups", None, None),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/groups",
        {"group_id": "uuid"},
        None,
    ),
    (
        "DELETE",
        f"{P}/workspaces/{{workspace_id}}/groups/{{group_id}}",
        None,
        None,
    ),
    ("GET", f"{P}/workspaces/{{workspace_id}}/acl", None, None),
    (
        "PUT",
        f"{P}/workspaces/{{workspace_id}}/acl",
        {"entries": "value"},
        None,
    ),
    # Files
    (
        "GET",
        f"{P}/workspaces/{{workspace_id}}/files",
        None,
        {"path": "path"},
    ),
    (
        "GET",
        f"{P}/workspaces/{{workspace_id}}/files/content",
        None,
        {"path": "path"},
    ),
    (
        "DELETE",
        f"{P}/workspaces/{{workspace_id}}/files",
        None,
        {"path": "path"},
    ),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/files/rename",
        {"old_path": "path", "new_path": "path"},
        None,
    ),
    (
        "GET",
        f"{P}/workspaces/{{workspace_id}}/files/download",
        None,
        {"path": "path"},
    ),
    (
        "POST",
        f"{P}/workspaces/{{workspace_id}}/files/upload",
        None,
        {"path": "path"},
    ),  # multipart upload
    # Images and volumes
    ("GET", f"{P}/images", None, None),
    ("GET", f"{P}/volumes", None, None),
    ("POST", f"{P}/volumes", {"name": "string"}, None),
    ("DELETE", f"{P}/volumes/{{name}}", None, None),
    # Users
    ("GET", f"{P}/users/search", None, {"q": "string"}),
    ("GET", f"{P}/my-permissions", None, {"resource": "string"}),
    # Groups: the authenticated listing; the management surface at
    # /groups is covered in the section below (#2944).
    ("GET", f"{P}/groups", None, None),
    # Admin
    ("GET", f"{P}/users", None, None),
    (
        "POST",
        f"{P}/users",
        {"email": "email", "password": "password"},
        None,
    ),
    ("DELETE", f"{P}/users/{{user_id}}", None, None),
    (
        "PATCH",
        f"{P}/users/{{user_id}}",
        {"email": "email", "password": "password", "handle": "string"},
        None,
    ),
    (
        "GET",
        f"{P}/users/{{user_id}}/workspaces",
        None,
        {"limit": "int", "offset": "int"},
    ),
    ("POST", f"{P}/users/{{user_id}}/unlockout", None, None),
    ("GET", f"{P}/users/{{user_id}}/sessions", None, None),
    ("GET", f"{P}/groups", None, None),
    (
        "POST",
        f"{P}/groups",
        {"name": "string", "description": "string"},
        None,
    ),
    (
        "PATCH",
        f"{P}/groups/{{group_id}}",
        {"name": "string", "description": "string"},
        None,
    ),
    ("DELETE", f"{P}/groups/{{group_id}}", None, None),
    ("GET", f"{P}/groups/{{group_id}}/members", None, None),
    (
        "POST",
        f"{P}/groups/{{group_id}}/members",
        {"user_id": "uuid"},
        None,
    ),
    (
        "DELETE",
        f"{P}/groups/{{group_id}}/members/{{user_id}}",
        None,
        None,
    ),
    ("GET", f"{P}/invitations", None, None),
    ("POST", f"{P}/invitations", {"email": "email"}, None),
    ("DELETE", f"{P}/invitations/{{invitation_id}}", None, None),
    ("POST", f"{P}/invitations/{{invitation_id}}/resend", None, None),
    # Container lifecycle events history (#2923): `limit`/`offset` page
    # through newest-first rows, `workspace_id` narrows to one workspace;
    # the fuzzer's out-of-range ints mostly hit the 422 paths, which is
    # the point.
    (
        "GET",
        f"{P}/events",
        None,
        {"limit": "int", "offset": "int", "workspace_id": "string"},
    ),
    # Server scheduling (#2661). `action` is "stop"|"recycle"; `at` is
    # absolute ISO-8601 and `in_seconds` a positive delay — the fuzzer's
    # generic values mostly hit the 422 paths, which is the point.
    ("GET", f"{P}/server/schedule", None, None),
    (
        "POST",
        f"{P}/server/schedule",
        {"action": "string", "at": "string", "in_seconds": "int"},
        None,
    ),
    ("DELETE", f"{P}/server/schedule/{{schedule_id}}", None, None),
    ("GET", f"{P}/acl/tree", None, None),
    ("GET", f"{P}/acl/by-principal/user/{{user_id}}", None, None),
    ("GET", f"{P}/acl/by-principal/group/{{group_id}}", None, None),
    ("GET", f"{P}/acl/resource", None, {"resource": "string"}),
    ("PUT", f"{P}/acl/resource", {"entries": "value"}, None),
    # Browser delegate
    ("POST", f"{P}/browser-delegate", {"action": "string", "data": "value"}, None),
    (
        "POST",
        f"{P}/browser-delegate/stream",
        {"action": "string", "data": "value"},
        None,
    ),
    # LLM proxy (#2072)
    ("GET", "/llm-proxy/models", None, None),
    (
        "POST",
        "/llm-proxy/chat/completions",
        {"model": "string", "messages": [{"role": "user", "content": "string"}]},
        None,
    ),
    # Test endpoints
    ("GET", f"{P}/test/idle-timeout", None, None),
    ("POST", f"{P}/test/set-idle-timeout", {"seconds": "int"}, None),
    ("GET", f"{P}/test/workspace-token/{{workspace_id}}", None, None),
    ("GET", f"{P}/test/browsers/{{workspace_id}}", None, None),
    # Misc: send garbage to random paths
    ("GET", "/{random_path}", None, None),
    ("POST", "/{random_path}", {"data": "value"}, None),
]

ROLES = ["owners", "coders", "collaborators", "spectators", "invalid-role"]


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------


class TeeReader:
    """Read a stream in a background thread, printing each line to stderr
    and buffering the full output for later analysis."""

    def __init__(self, stream: io.RawIOBase):
        self._buf: list[str] = []
        self._thread = threading.Thread(target=self.run, args=(stream,), daemon=True)
        self._thread.start()

    def run(self, stream):
        for raw_line in stream:
            line = raw_line.decode(errors="replace")
            sys.stderr.write(f"[server] {line}")
            sys.stderr.flush()
            self._buf.append(line)

    def join(self, timeout: float = 5):
        self._thread.join(timeout=timeout)

    @property
    def output(self) -> str:
        return "".join(self._buf)


def start_server(data_dir: str) -> tuple[subprocess.Popen, TeeReader, str]:
    """Start a klangkd server as a subprocess, bound to a Unix socket.

    ``klangkd`` is the real server launcher (``--config none`` = env-only);
    it always binds a UDS at ``settings.socket`` (default
    ``<state_dir>/klangk.sock``, overridable via ``KLANGKD_SOCKET`` — #1542)
    and would normally start the proxy child, so ``_KLANGKD_DISABLE_PROXY``
    suppresses it — the fuzzer talks to the backend directly over the
    socket. ``KLANGKD_TEST_MODE`` registers the ``/api/v1/test/*`` routes the
    fuzzer also exercises.

    Returns (process, tee_reader, uds_path) — the tee_reader streams server
    stderr to the terminal in real time and captures it for the report.
    ``uds_path`` is the resolved socket path (read back from settings, not
    recomputed, so a ``KLANGKD_SOCKET`` override is honored).
    """
    env = {
        **os.environ,
        "KLANGKD_STATE_DIR": data_dir,
        "KLANGKD_DEFAULT_USER": "admin@example.com",
        "KLANGKD_DEFAULT_PASSWORD": "admin",
        "KLANGKD_JWT_SECRET": "fuzz-test-secret",
        "KLANGKD_MIN_PASSWORD_LENGTH": "1",
        "KLANGKD_AUTH_MODES": "password",
        "KLANGKD_TEST_MODE": "1",
        # Suppress proxy spawn (the fuzzer hits the backend UDS directly).
        "_KLANGKD_DISABLE_PROXY": "1",
        # Disable features that need external services
        "KLANGKD_IMAGE_PULL_POLICY": "never",
    }
    # Resolve the socket path from the same settings the server will use
    # (honors KLANGKD_SOCKET; defaults to <state_dir>/klangk.sock). Imported
    # lazily so ``--check`` (which never starts a server) needs no backend.
    from klangk.settings import KlangkSettings

    settings = KlangkSettings(
        {k: v for k, v in env.items() if k.startswith("KLANGKD_")},
        config_file="none",
    )
    uds_path = settings.socket
    proc = subprocess.Popen(
        ["klangkd", "--config", "none"],
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    tee = TeeReader(proc.stderr)
    return proc, tee, uds_path


def wait_for_server(uds_path: str, timeout: float = 30) -> None:
    """Poll /health until the server is up (over the UDS)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=uds_path),
                base_url="http://klangkd",
                timeout=2,
            ) as c:
                if c.get("/health").status_code == 200:
                    return
        except (httpx.ConnectError, httpx.HTTPError):
            pass
        time.sleep(0.3)
    raise TimeoutError("Server did not start in time")


# ---------------------------------------------------------------------------
# Anomaly tracking
# ---------------------------------------------------------------------------


def truncated_body(body: dict) -> str:
    """The body serialized for the report, truncated past 200 chars."""
    body_str = json.dumps(body, default=str)
    if len(body_str) > 200:
        return body_str[:200] + "..."
    return body_str


class AnomalyTracker:
    def __init__(self):
        self.server_errors: list[dict] = []  # 5xx responses
        self.timeouts: list[dict] = []
        self.connection_errors: list[dict] = []
        self.requests_sent = 0
        self.by_status: dict[int, int] = {}

    def record(
        self,
        method: str,
        path: str,
        status: int | None,
        error: str | None = None,
        body: dict | None = None,
    ):
        self.requests_sent += 1
        if status is not None:
            self.by_status[status] = self.by_status.get(status, 0) + 1
        entry = {
            "method": method,
            "path": path,
            "status": status,
            "error": error,
            "time": time.strftime("%H:%M:%S"),
        }
        if body is not None:
            entry["body"] = truncated_body(body)
        self.classify_entry(entry, status, error)

    def classify_entry(
        self, entry: dict, status: int | None, error: str | None
    ) -> None:
        """Route one recorded entry to its bucket: 5xx -> server_errors,
        timeout -> timeouts, connection -> connection_errors."""
        if status is not None and status >= 500:
            self.server_errors.append(entry)
            logger.warning("5xx: %s %s → %d", entry["method"], entry["path"], status)
        elif error == "timeout":
            self.timeouts.append(entry)
        elif error == "connection":
            self.connection_errors.append(entry)

    def report(self, stderr_output: str) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("FUZZ TEST REPORT")
        lines.append("=" * 60)
        lines.append(f"Total requests sent: {self.requests_sent}")
        lines.append("")
        lines.append("Status code distribution:")
        for status in sorted(self.by_status):
            lines.append(f"  {status}: {self.by_status[status]}")
        lines.append("")

        _report_server_errors(lines, self.server_errors)
        _report_simple_counts(
            lines,
            [
                ("TIMEOUTS", self.timeouts, None),
                (
                    "CONNECTION ERRORS",
                    self.connection_errors,
                    "(server may have crashed)",
                ),
            ],
        )
        exception_lines = _stderr_anomaly_lines(stderr_output)
        _report_stderr_anomalies(lines, exception_lines)

        lines.append("")
        has_anomalies = bool(
            self.server_errors or self.connection_errors or exception_lines
        )
        if has_anomalies:
            lines.append("RESULT: ANOMALIES FOUND")
        else:
            lines.append("RESULT: CLEAN ✓")
        lines.append("=" * 60)
        return "\n".join(lines)


def _report_server_errors(lines: list[str], errors: list[dict]) -> None:
    """The 5xx section: first 50 entries with bodies, then a remainder
    count."""
    lines.append("")
    if not errors:
        lines.append("SERVER ERRORS (5xx): 0 ✓")
        return
    lines.append(f"SERVER ERRORS (5xx): {len(errors)}")
    for e in errors[:50]:
        lines.append(f"  [{e['time']}] {e['method']} {e['path']} → {e['status']}")
        if "body" in e:
            lines.append(f"    body: {e['body']}")
    if len(errors) > 50:
        lines.append(f"  ... and {len(errors) - 50} more")


def _report_simple_counts(
    lines: list[str], sections: list[tuple[str, list, str | None]]
) -> None:
    """The timeout / connection-error count sections (one leading blank
    before the group)."""
    for i, (label, entries, note) in enumerate(sections):
        if i == 0:
            lines.append("")
        if entries:
            lines.append(f"{label}: {len(entries)}")
            if note:
                lines.append(f"  {note}")
        else:
            lines.append(f"{label}: 0 ✓")


_STDERR_ANOMALY_KEYWORDS = [
    "traceback",
    "error",
    "exception",
    "unhandled",
    "segfault",
    "killed",
    "fatal",
]


def stderr_line_is_anomaly(line: str) -> bool:
    """Does a server-stderr line look like an unhandled exception?
    Expected/normal log lines (INFO/WARNING/password noise) are filtered out."""
    low = line.lower()
    return (
        any(kw in low for kw in _STDERR_ANOMALY_KEYWORDS)
        and "INFO" not in line
        and "WARNING" not in line
        and "password" not in low
    )


def _stderr_anomaly_lines(stderr_output: str) -> list[str]:
    """Server-stderr lines that look like unhandled exceptions, with
    expected/normal log lines (INFO/WARNING/password noise) filtered out."""
    return [
        line
        for line in stderr_output.strip().splitlines()
        if stderr_line_is_anomaly(line)
    ]


def _report_stderr_anomalies(lines: list[str], exception_lines: list) -> None:
    """The stderr-anomaly section: first 30 lines, then a remainder count."""
    lines.append("")
    if not exception_lines:
        lines.append("SERVER STDERR ANOMALIES: 0 ✓")
        return
    lines.append(f"SERVER STDERR ANOMALIES: {len(exception_lines)}")
    for exc_line in exception_lines[:30]:
        lines.append(f"  {exc_line.rstrip()}")
    if len(exception_lines) > 30:
        lines.append(f"  ... and {len(exception_lines) - 30} more")


# ---------------------------------------------------------------------------
# Fuzzer
# ---------------------------------------------------------------------------


def create_fixture_id(
    uds_path: str,
    headers: dict,
    url: str,
    payload: dict,
    ok_codes: tuple[int, ...] = (200,),
) -> str | None:
    """POST one fixture (workspace / user / group); its id, or None."""
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=uds_path),
            base_url="http://klangkd",
            timeout=10,
        ) as c:
            r = c.post(url, json=payload, headers=headers)
            if r.status_code in ok_codes:
                return r.json()["id"]
    except Exception:
        pass
    return None


def collect_fixture_ids(
    uds_path: str,
    headers: dict,
    url: str,
    payloads: list[dict],
    ok_codes: tuple[int, ...] = (200,),
) -> list[str]:
    """POST each payload fixture; collect the ids that came back."""
    ids = []
    for payload in payloads:
        fid = create_fixture_id(uds_path, headers, url, payload, ok_codes)
        if fid:
            ids.append(fid)
    return ids


def seed_fuzz_fixtures(
    uds_path: str, token: str
) -> tuple[list[str], list[str], list[str]]:
    """Pre-create workspaces / users / groups so fuzzed paths have real
    ids to target. Returns (workspace_ids, user_ids, group_ids)."""
    headers = {"Authorization": f"Bearer {token}"}
    workspace_ids = collect_fixture_ids(
        uds_path,
        headers,
        "/api/v1/workspaces",
        [{"name": f"fuzz-ws-{i}"} for i in range(2)],
    )
    user_ids = collect_fixture_ids(
        uds_path,
        headers,
        "/api/v1/users",
        [
            {"email": f"fuzzuser{i}@example.com", "password": "fuzzpass"}
            for i in range(3)
        ],
    )
    group_ids = collect_fixture_ids(
        uds_path,
        headers,
        "/api/v1/groups",
        [{"name": f"fuzz-group-{i}"} for i in range(2)],
        ok_codes=(200, 201),
    )
    return workspace_ids, user_ids, group_ids


def _fill_id_placeholder(
    rng: random.Random, path: str, placeholder: str, ids: list[str]
) -> str:
    """Substitute one id placeholder: a known id, a random known id, or a
    fresh fuzzed uuid when none are known."""
    if placeholder not in path:
        return path
    return path.replace(
        placeholder,
        rng.choice(ids + [fuzz_uuid(rng)]) if ids else fuzz_uuid(rng),
    )


_NAMED_PLACEHOLDER_DRAWS = {
    "{role}": lambda rng: rng.choice(ROLES),
    "{invitation_id}": fuzz_uuid,
    "{name}": fuzz_string,
    "{provider_id}": fuzz_string,
}


def fuzz_random_path(rng: random.Random) -> str:
    """A synthetic random multi-segment path for the catch-all endpoints."""
    segments = rng.randint(1, 4)
    return "/" + "/".join(fuzz_string(rng) for _ in range(segments))


def fill_named_placeholders(rng: random.Random, path: str) -> str:
    """Substitute the non-id placeholders ({role} from the known role
    list, {invitation_id}/{name}/{provider_id} as fuzzed values) and the
    synthetic {random_path} catch-all."""
    for placeholder, draw in _NAMED_PLACEHOLDER_DRAWS.items():
        if placeholder in path:
            path = path.replace(placeholder, draw(rng))
    if "{random_path}" in path:
        path = fuzz_random_path(rng)
    return path


def fill_path_params(
    rng: random.Random,
    path_template: str,
    workspace_ids: list[str],
    user_ids: list[str],
    group_ids: list[str],
) -> str:
    """Substitute fuzzed values for the ``{param}`` placeholders."""
    path = path_template
    for placeholder, ids in (
        ("{workspace_id}", workspace_ids),
        ("{member_id}", user_ids),
        ("{user_id}", user_ids),
        ("{group_id}", group_ids),
    ):
        path = _fill_id_placeholder(rng, path, placeholder, ids)
    return fill_named_placeholders(rng, path)


def fuzz_request_headers(rng: random.Random, headers: dict) -> dict:
    """Decide auth: sometimes send no token, bad token, or valid token,
    and sometimes a garbage content-type."""
    auth_choice = rng.random()
    if auth_choice < 0.1:
        req_headers = {}
    elif auth_choice < 0.2:
        req_headers = {"Authorization": "Bearer invalid-token-xxx"}
    else:
        req_headers = headers

    # Sometimes send garbage content-type
    if rng.random() < 0.05:
        req_headers["Content-Type"] = rng.choice(
            [
                "text/plain",
                "application/xml",
                "multipart/form-data",
                "",
            ]
        )
    return req_headers


def fuzz_request_args(body, params, req_headers: dict) -> dict:
    """The httpx request kwargs: the JSON body as ``json=``, or raw
    ``content=`` when the fuzzed headers override the content-type."""
    if not body:
        return {"params": params}
    if "Content-Type" in req_headers:
        return {"content": json.dumps(body).encode(), "params": params}
    return {"json": body, "params": params}


async def send_fuzz_request(
    client: httpx.AsyncClient,
    tracker: AnomalyTracker,
    method: str,
    path: str,
    body,
    params,
    req_headers: dict,
) -> None:
    """Fire one fuzzed request and record the outcome."""
    args = fuzz_request_args(body, params, req_headers)
    try:
        r = await client.request(method, path, headers=req_headers, **args)
        tracker.record(method, path, r.status_code, body=body)
    except httpx.TimeoutException:
        tracker.record(method, path, None, error="timeout", body=body)
    except httpx.ConnectError:
        tracker.record(method, path, None, error="connection", body=body)
        # Server might have crashed — wait a bit
        await asyncio.sleep(2)
    except Exception as exc:
        tracker.record(method, path, None, error=str(exc), body=body)


def maybe_relogin(tracker: AnomalyTracker, headers: dict, uds_path: str) -> None:
    """Periodically re-login in case the token was invalidated (checked
    every 200 requests); on failure continue with the old/no token."""
    if tracker.requests_sent % 200 != 0:
        return
    try:
        new_token = uds_login(uds_path, "admin@example.com", "admin")
        headers["Authorization"] = f"Bearer {new_token}"
    except Exception:
        pass  # will continue with old or no token


async def run_fuzz(
    uds_path: str,
    token: str,
    duration_minutes: float,
    seed: int,
    tracker: AnomalyTracker,
):
    rng = random.Random(seed)
    deadline = time.monotonic() + duration_minutes * 60

    headers = {"Authorization": f"Bearer {token}"}
    workspace_ids, user_ids, group_ids = seed_fuzz_fixtures(uds_path, token)

    transport = httpx.AsyncHTTPTransport(uds=uds_path)

    logger.info(
        "Fuzzing with seed=%d, workspaces=%d, users=%d, groups=%d",
        seed,
        len(workspace_ids),
        len(user_ids),
        len(group_ids),
    )

    async with httpx.AsyncClient(
        transport=transport, base_url="http://klangkd", timeout=10
    ) as client:
        while time.monotonic() < deadline:
            # Pick a random endpoint
            method, path_template, body_schema, query_schema = rng.choice(ENDPOINTS)

            # Fill in path parameters
            path = fill_path_params(
                rng, path_template, workspace_ids, user_ids, group_ids
            )

            # Build body
            body = fuzz_body(rng, body_schema) if body_schema else None

            # Build query params
            params = fuzz_query(rng, query_schema) if query_schema else None

            req_headers = fuzz_request_headers(rng, headers)

            await send_fuzz_request(
                client, tracker, method, path, body, params, req_headers
            )

            # Small delay to avoid overwhelming
            await asyncio.sleep(rng.uniform(0.01, 0.1))

            # Periodically re-login in case the token was invalidated
            maybe_relogin(tracker, headers, uds_path)


# ---------------------------------------------------------------------------
# Endpoint drift check (--check mode)
# ---------------------------------------------------------------------------


def _fuzzed_routes() -> set[tuple[str, str]]:
    """The (method, path) set the fuzzer declares, excluding the
    synthetic ``/{random_path}`` catch-all (not a real route)."""
    out = set()
    for method, path, _body, _query in ENDPOINTS:
        if "{random_path}" in path:
            continue
        out.add((method.upper(), path))
    return out


def _backend_routes() -> set[tuple[str, str]]:
    """The (method, path) set the live router declares, read from the
    FastAPI OpenAPI schema (``build_app`` with ``KLANGKD_TEST_MODE=1`` so
    the ``/api/v1/test/*`` routes are included)."""
    # The test-mode gate in api/__init__.py reads os.environ directly (not
    # the KlangkSettings env dict), so set it on os.environ before build_app
    # imports/registers the test routes.
    os.environ["KLANGKD_TEST_MODE"] = "1"
    os.environ["_KLANGKD_DISABLE_PROXY"] = "1"
    env = {
        **os.environ,
        "KLANGKD_STATE_DIR": tempfile.mkdtemp(prefix="klangk-check-"),
        "KLANGKD_JWT_SECRET": "check-secret",
    }
    from klangk.main import build_app
    from klangk.settings import KlangkSettings

    app = build_app(KlangkSettings(env))
    routes: set[tuple[str, str]] = set()
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            routes.add((method.upper(), path))
    return routes


def print_route_diff(missing: list, extra: list) -> None:
    """Print the missing/extra route sets from the drift diff."""
    if missing:
        print("\nMISSING from fuzzer (real routes not fuzzed):")
        for m, p in missing:
            print(f"  {m:6} {p}")
    if extra:
        print("\nEXTRA in fuzzer (fuzzed, no matching route):")
        for m, p in extra:
            print(f"  {m:6} {p}")


def check_endpoints() -> int:
    """Diff ENDPOINTS against the live router; fail on drift (#1536).

    Prints the missing/extra sets and returns 0 if they match, 1 otherwise.
    Run as ``scripts/fuzz-api.py --check`` — a cheap, server-less gate for
    CI so the fuzzer's hand-curated route list can't silently fall behind
    the backend again.
    """
    backend = _backend_routes()
    fuzzed = _fuzzed_routes()
    missing = sorted(backend - fuzzed, key=lambda x: x[1])
    extra = sorted(fuzzed - backend, key=lambda x: x[1])
    print(f"backend routes: {len(backend)}  fuzzer endpoints: {len(fuzzed)}")
    print_route_diff(missing, extra)
    if not missing and not extra:
        print("No drift ✓")
        return 0
    return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def stop_server(proc: subprocess.Popen, tee: TeeReader) -> None:
    """SIGTERM the server (SIGKILL after a 10s grace) + drain the tee."""
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    tee.join()


def fuzz_session(tracker: AnomalyTracker, args, seed: int, data_dir: str) -> str:
    """Start the server, log in, run the fuzz loop, tear down. Returns the
    captured server stderr."""
    logger.info("Starting klangkd")
    proc, tee, uds_path = start_server(data_dir)
    try:
        wait_for_server(uds_path)
        logger.info("Server is up")

        token = uds_login(uds_path, "admin@example.com", "admin")
        logger.info("Logged in as admin")

        logger.info(
            "Starting fuzz run: duration=%g min, seed=%d",
            args.duration,
            seed,
        )
        asyncio.run(run_fuzz(uds_path, token, args.duration, seed, tracker))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fuzz runner error")
    finally:
        stop_server(proc, tee)
    return tee.output


def write_server_log(stderr_data: str) -> None:
    """Write the full server log for later inspection."""
    log_path = os.path.join(os.path.dirname(__file__), "..", "fuzz-server.log")
    with open(log_path, "w") as f:
        f.write(stderr_data)
    logger.info("Server stderr saved to %s", log_path)


def fuzz_exit(tracker: AnomalyTracker) -> int:
    """Exit code: 1 on 5xx or connection errors (the anomalies that gate
    CI), 0 otherwise."""
    return 1 if bool(tracker.server_errors or tracker.connection_errors) else 0


def main():
    parser = argparse.ArgumentParser(description="API fuzz tester for klangk")
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Don't fuzz: diff ENDPOINTS against the live router's OpenAPI "
            "schema and exit 1 on drift. Server-less; for CI."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30,
        help="Duration in minutes (default: 30)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed (default: random)",
    )
    args = parser.parse_args()

    configure_logging()

    if args.check:
        sys.exit(check_endpoints())

    seed = draw_seed(args.seed)

    tracker = AnomalyTracker()

    with tempfile.TemporaryDirectory(prefix="klangk-fuzz-") as data_dir:
        stderr_data = fuzz_session(tracker, args, seed, data_dir)

    print(tracker.report(stderr_data))
    write_server_log(stderr_data)
    sys.exit(fuzz_exit(tracker))


if __name__ == "__main__":
    main()
