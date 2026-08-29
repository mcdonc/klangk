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
from typing import NamedTuple

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


class PasswordPolicy(NamedTuple):
    """Server-advertised password policy (#2581): length + class counts."""

    min_length: int
    requirements: dict

    def complexity_error(self, password: str) -> str | None:
        """Local mirror of the server's complexity rule.

        Returns a human-readable error string when ``password`` fails the
        character-class counts, else ``None``. Must stay in sync with
        ``auth.validate_password_complexity`` on the server — duplication
        is deliberate (CLI subpackage isolation; see AGENTS.md). Classes
        are ASCII (A-Z, a-z, 0-9, everything else special), matching the
        server and the web UI.
        """
        counts = _class_requirements_met(password, self.requirements)
        unmet = [
            f"at least {need} {name}{'s' if need != 1 else ''}"
            for name, (have, need) in counts.items()
            if need > 0 and have < need
        ]
        if unmet:
            return f"Password must contain {', '.join(unmet)}"
        return None


def _class_requirements_met(password: str, requirements: dict) -> dict:
    """name -> (have, need) for each ASCII character class (A-Z, a-z, 0-9,
    everything else special) — matching the server's classes."""
    upper = sum(1 for c in password if "A" <= c <= "Z")
    lower = sum(1 for c in password if "a" <= c <= "z")
    digit = sum(1 for c in password if "0" <= c <= "9")
    special = len(password) - upper - lower - digit
    return {
        "uppercase letter": (upper, requirements.get("upper", 0)),
        "lowercase letter": (lower, requirements.get("lower", 0)),
        "digit": (digit, requirements.get("digit", 0)),
        "special character": (special, requirements.get("special", 0)),
    }


def password_policy(server_url: str) -> PasswordPolicy:
    """Fetch the server's password policy from ``/api/v1/config``.

    One fetch covers length + character-class counts. Falls back to
    permissive defaults (length 8, no class requirements) when the
    server is unreachable, old, or advertises unparseable values — the
    server enforces its own policy authoritatively either way.
    """
    config = fetch_config(server_url)
    min_length = _DEFAULT_MIN_PASSWORD
    requirements = {"upper": 0, "lower": 0, "digit": 0, "special": 0}
    if isinstance(config, dict):
        try:
            min_length = int(
                config.get("min_password_length") or _DEFAULT_MIN_PASSWORD
            )
        except (TypeError, ValueError):
            pass
        reqs = config.get("password_requirements")
        if isinstance(reqs, dict):
            for key in requirements:
                try:
                    requirements[key] = int(reqs.get(key) or 0)
                except (TypeError, ValueError):
                    pass
    return PasswordPolicy(min_length, requirements)
