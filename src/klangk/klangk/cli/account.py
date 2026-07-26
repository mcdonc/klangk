"""Account self-service: validation helpers shared by the CLI and TUI.

Mirrors the Flutter ``SettingsPage`` client-side checks (handle charset,
email format, password minimum) so both terminal surfaces reject the same
input before round-tripping to the server. The server re-validates
authoritatively in ``klangk.api.auth``; these functions are a fast-fail
UX layer only.

Stays within ``klangk.cli`` (CLI subpackage isolation rule): no imports
from the server package.
"""

from __future__ import annotations

import re

from .auth import fetch_config

# Mirror klangk.model.users (server). Duplicated here rather than imported
# because the CLI must not depend on the server package.
HANDLE_RE = re.compile(r"^[a-z0-9._-]+$")
MAX_HANDLE_LEN = 32
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DEFAULT_MIN_PASSWORD = 8


def validate_handle(handle: str) -> str | None:
    """Return an error message if *handle* is invalid, else ``None``.

    Same rules as the Flutter client and the server's static check:
    non-empty, lowercase, ``[a-z0-9._-]+``, and within the length cap.
    """
    handle = (handle or "").strip()
    if not handle:
        return "Handle cannot be empty"
    if handle != handle.lower():
        return "Handle must be lowercase"
    if len(handle) > MAX_HANDLE_LEN:
        return f"Handle must be {MAX_HANDLE_LEN} characters or fewer"
    if not HANDLE_RE.match(handle):
        return (
            "Handle may only contain lowercase letters, digits,"
            " dots, dashes, and underscores"
        )
    return None


def validate_email(email: str) -> str | None:
    """Return an error message if *email* is invalid, else ``None``."""
    email = (email or "").strip()
    if not email:
        return "Email cannot be empty"
    if not EMAIL_RE.match(email):
        return "Enter a valid email address"
    return None


def password_min_length(server_url: str) -> int:
    """Server-configured minimum password length, from ``/api/v1/config``.

    Falls back to ``_DEFAULT_MIN_PASSWORD`` (8) when the server doesn't
    advertise one — an unreachable/old server, or an unparseable value.
    The server still enforces its own floor authoritatively.
    """
    config = fetch_config(server_url)
    if isinstance(config, dict):
        try:
            return int(
                config.get("min_password_length") or _DEFAULT_MIN_PASSWORD
            )
        except (TypeError, ValueError):
            return _DEFAULT_MIN_PASSWORD
    return _DEFAULT_MIN_PASSWORD
