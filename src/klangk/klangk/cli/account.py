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
RESERVED_HANDLES = frozenset({"work", ".users"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DEFAULT_MIN_PASSWORD = 8


def validate_handle(handle: str) -> str | None:
    """Return an error message if *handle* is invalid, else ``None``.

    Same rules as the Flutter client and the server's static check
    (``klangk.model.users.validate_handle``): non-empty, length cap, no
    leading dot, not reserved, lowercase, ``[a-z0-9._-]+``. The checks are
    applied in the same order as the server so the error message for any
    given input matches.
    """
    handle = (handle or "").strip()
    if not handle:
        return "Handle cannot be empty"
    if len(handle) > MAX_HANDLE_LEN:
        return f"Handle must be {MAX_HANDLE_LEN} characters or fewer"
    if handle.startswith("."):
        return "Handle cannot start with a dot"
    if handle in RESERVED_HANDLES:
        return f"'{handle}' is reserved"
    if handle != handle.lower():
        return "Handle must be lowercase"
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


def password_requirements(server_url: str) -> dict:
    """Server-configured character-class counts (#2581).

    Mirrors ``password_min_length``: reads ``/api/v1/config``'s
    ``password_requirements`` (upper/lower/digit/special ints, 0 = no
    requirement) and falls back to all-zero (no requirements) when the
    server doesn't advertise them. The server enforces authoritatively.
    """
    config = fetch_config(server_url)
    reqs = (
        config.get("password_requirements")
        if isinstance(config, dict)
        else None
    )
    if not isinstance(reqs, dict):
        return {"upper": 0, "lower": 0, "digit": 0, "special": 0}
    out = {}
    for key in ("upper", "lower", "digit", "special"):
        try:
            out[key] = int(reqs.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def password_complexity_error(password: str, reqs: dict) -> str | None:
    """Local mirror of the server's complexity rule (#2581).

    Returns a human-readable error string when ``password`` fails the
    character-class counts in ``reqs`` (as returned by
    ``password_requirements``), else ``None``. Must stay in sync with
    ``auth.validate_password_complexity`` on the server — duplication is
    deliberate (CLI subpackage isolation; see AGENTS.md).
    """
    counts = {
        "uppercase letter": (
            sum(c.isupper() for c in password),
            int(reqs.get("upper") or 0),
        ),
        "lowercase letter": (
            sum(c.islower() for c in password),
            int(reqs.get("lower") or 0),
        ),
        "digit": (
            sum(c.isdigit() for c in password),
            int(reqs.get("digit") or 0),
        ),
        "special character": (
            sum(not c.isalnum() for c in password),
            int(reqs.get("special") or 0),
        ),
    }
    unmet = [
        f"at least {need} {name}{'s' if need != 1 else ''}"
        for name, (have, need) in counts.items()
        if have < need
    ]
    if unmet:
        return f"Password must contain {', '.join(unmet)}"
    return None
