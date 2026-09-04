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


# Ordered (fails, error) checks — the same order as the server's
# ``validate_handle`` so the error message for any input matches.
HANDLE_CHECKS = [
    (
        lambda h: not h,
        lambda h: "Handle cannot be empty",
    ),
    (
        lambda h: len(h) > MAX_HANDLE_LEN,
        lambda h: f"Handle must be {MAX_HANDLE_LEN} characters or fewer",
    ),
    (
        lambda h: h.startswith("."),
        lambda h: "Handle cannot start with a dot",
    ),
    (
        lambda h: h in RESERVED_HANDLES,
        lambda h: f"'{h}' is reserved",
    ),
    (
        lambda h: h != h.lower(),
        lambda h: "Handle must be lowercase",
    ),
    (
        lambda h: not HANDLE_RE.match(h),
        lambda h: (
            "Handle may only contain lowercase letters, digits,"
            " dots, dashes, and underscores"
        ),
    ),
]


def validate_handle(handle: str) -> str | None:
    """Return an error message if *handle* is invalid, else ``None``.

    Same rules as the Flutter client and the server's static check
    (``klangk.model.users.validate_handle``): non-empty, length cap, no
    leading dot, not reserved, lowercase, ``[a-z0-9._-]+``. The checks are
    applied in the same order as the server so the error message for any
    given input matches.
    """
    handle = (handle or "").strip()
    for fails, error in HANDLE_CHECKS:
        if fails(handle):
            return error(handle)
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
    """Server-advertised password policy (#2581, #3173): length, class
    counts, and the min-changed-character rule."""

    min_length: int
    requirements: dict
    min_changed: int = 0

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
        unmet = unmet_class_messages(counts)
        if unmet:
            return f"Password must contain {', '.join(unmet)}"
        return None

    def changed_error(self, current: str, new: str) -> str | None:
        """Local mirror of the server's min-changed rule (#3173).

        Returns the same error string the server's 400 would carry when
        the edit distance between ``current`` and ``new`` is below
        ``min_changed`` (STIG V-222541), else ``None``. Only the server
        has both passwords at change time, so this is a fast-fail UX
        layer over the authoritative check.
        """
        if self.min_changed <= 0:
            return None
        if password_edit_distance(current, new) < self.min_changed:
            return (
                f"New password must change at least {self.min_changed} "
                "characters from the current password"
            )
        return None


def password_edit_distance(old: str, new: str) -> int:
    """Levenshtein distance in code points (STIG V-222541, #3173).

    Mirrors ``klangk.auth.password_edit_distance`` on the server —
    duplicated rather than imported because the CLI must not depend on
    the server package (AGENTS.md). Substitutions, insertions, and
    deletions each count as one changed character.
    """
    prev = list(range(len(new) + 1))
    for i, old_char in enumerate(old, start=1):
        cur = [i] + [0] * len(new)
        for j, new_char in enumerate(new, start=1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (old_char != new_char),
            )
        prev = cur
    return prev[-1]


def unmet_class_messages(counts: dict) -> list[str]:
    """Human-readable messages for each unmet character-class requirement."""
    return [
        f"at least {need} {name}{'s' if need != 1 else ''}"
        for name, (have, need) in counts.items()
        if need > 0 and have < need
    ]


def class_count(password: str, low: str, high: str) -> int:
    """How many characters fall in the ``[low, high]`` ASCII range."""
    return sum(1 for c in password if low <= c <= high)


def _class_requirements_met(password: str, requirements: dict) -> dict:
    """name -> (have, need) for each ASCII character class (A-Z, a-z, 0-9,
    everything else special) — matching the server's classes."""
    upper = class_count(password, "A", "Z")
    lower = class_count(password, "a", "z")
    digit = class_count(password, "0", "9")
    special = len(password) - upper - lower - digit
    return {
        "uppercase letter": (upper, requirements.get("upper", 0)),
        "lowercase letter": (lower, requirements.get("lower", 0)),
        "digit": (digit, requirements.get("digit", 0)),
        "special character": (special, requirements.get("special", 0)),
    }


_DEFAULT_REQUIREMENTS = {"upper": 0, "lower": 0, "digit": 0, "special": 0}


def default_policy() -> PasswordPolicy:
    """The permissive fallback policy (length 8, no class requirements)."""
    return PasswordPolicy(_DEFAULT_MIN_PASSWORD, dict(_DEFAULT_REQUIREMENTS))


def parsed_min_length(config: dict) -> int:
    """The advertised min password length, or the default when unparseable."""
    try:
        return int(config.get("min_password_length") or _DEFAULT_MIN_PASSWORD)
    except (TypeError, ValueError):
        return _DEFAULT_MIN_PASSWORD


def parsed_min_changed(config: dict) -> int:
    """The advertised min changed-character count, 0 when unparseable."""
    try:
        return max(0, int(config.get("password_min_changed") or 0))
    except (TypeError, ValueError):
        return 0


def parsed_requirements(config: dict) -> dict:
    """The advertised class requirements (0s when absent or unparseable)."""
    requirements = dict(_DEFAULT_REQUIREMENTS)
    reqs = config.get("password_requirements")
    if not isinstance(reqs, dict):
        return requirements
    for key in requirements:
        try:
            requirements[key] = int(reqs.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return requirements


def password_policy(server_url: str) -> PasswordPolicy:
    """Fetch the server's password policy from ``/api/v1/config``.

    One fetch covers length + character-class counts. Falls back to
    permissive defaults (length 8, no class requirements) when the
    server is unreachable, old, or advertises unparseable values — the
    server enforces its own policy authoritatively either way.
    """
    config = fetch_config(server_url)
    if not isinstance(config, dict):
        return default_policy()
    return PasswordPolicy(
        parsed_min_length(config),
        parsed_requirements(config),
        parsed_min_changed(config),
    )
