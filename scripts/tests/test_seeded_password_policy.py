"""Contract test: every KLANGKD_DEFAULT_PASSWORD literal booted by CI
scripts must clear the boot-time password-policy gate (#2581, #3341).

The gate (`lifecycle._password_policy_errors`, wired into
`seed_default_user`) refuses to start klangkd when the seeded admin
password violates KLANGKD_MIN_PASSWORD_LENGTH (default 8) or the
character-class counts. Scripts that boot a real server in password mode
are the CI entry points — a short literal there fails only at smoke time,
after a full wheel build (#3341). This test scans scripts/ and
.github/workflows/ for password literals and validates each against the
real validator, so the failure lands in seconds on the PR, not 10 minutes
into the dist-smoke job.

A file may loosen the policy for itself by setting
KLANGKD_MIN_PASSWORD_LENGTH (scripts/fuzz-api.py sets 1 for its throwaway
fuzz server); the scan honors that override. Password-class overrides
(KLANGKD_PASSWORD_REQUIRE_*) are not honored — none of the scripts use
them; extend _policy_env if one ever does.
"""

import re
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Directories whose scripts boot real klangkd processes from env literals.
_SCAN_DIRS = (_REPO_ROOT / "scripts", _REPO_ROOT / ".github" / "workflows")
_SCAN_SUFFIXES = {".sh", ".py", ".yml", ".yaml"}

# KEY=value (shell / env stanza) and "KEY": value (Python dict) forms.
_ASSIGN_RE = re.compile(r"""KLANGKD_DEFAULT_PASSWORD=("[^"]*"|'[^']*'|[^\s"'\\]+)""")
_QUOTE_RE = re.compile(r"^(['\"])(.*)\1$")
_DICT_RE = re.compile(r'"KLANGKD_DEFAULT_PASSWORD"\s*:\s*"([^"]*)"')
# ${KLANGKD_DEFAULT_PASSWORD:-fallback} — validate the fallback too.
_FALLBACK_RE = re.compile(r"\$\{KLANGKD_DEFAULT_PASSWORD:-([^}]*)\}")
# A same-file policy loosening (the only sanctioned escape hatch).
_MIN_LEN_RE = re.compile(r'KLANGKD_MIN_PASSWORD_LENGTH"?\s*[=:]\s*"?(\d+)"?')


def _is_literal(value: str) -> bool:
    """True for a plaintext literal (a leading $ / { is an indirection)."""
    return bool(value) and not value.startswith(("$", "{"))


def _unquote(raw: str) -> str:
    """*raw* without its surrounding quote pair, when it has one."""
    match = _QUOTE_RE.match(raw)
    return match.group(2) if match else raw


def _literal_values(text: str) -> list[str]:
    """Every KLANGKD_DEFAULT_PASSWORD literal assigned in *text*."""
    found = list(_DICT_RE.findall(text)) + _FALLBACK_RE.findall(text)
    found += [_unquote(m.group(1)) for m in _ASSIGN_RE.finditer(text)]
    return [value for value in found if _is_literal(value)]


def _min_length_for(text: str) -> int:
    """The file's own KLANGKD_MIN_PASSWORD_LENGTH (default 8)."""
    matches = _MIN_LEN_RE.findall(text)
    return int(matches[0]) if matches else 8


def _is_scannable(path: Path) -> bool:
    """True for files the scan reads (test fixtures are excluded)."""
    return (
        path.suffix in _SCAN_SUFFIXES
        and path.is_file()
        and _REPO_ROOT / "scripts" / "tests" not in path.parents
    )


def _file_literals(path: Path) -> list[tuple[Path, str, int]]:
    """(relpath, password, min_length) rows for one scanned file."""
    text = path.read_text()
    if "KLANGKD_DEFAULT_PASSWORD" not in text:
        return []
    min_length = _min_length_for(text)
    rel = path.relative_to(_REPO_ROOT)
    return [(rel, value, min_length) for value in _literal_values(text)]


def _scanned_literals() -> list[tuple[Path, str, int]]:
    """(relpath, password, min_length) for every scanned literal."""
    rows = []
    for scan_dir in _SCAN_DIRS:
        for path in sorted(scan_dir.rglob("*")):
            if _is_scannable(path):
                rows.extend(_file_literals(path))
    return rows


def _policy_errors(password: str, min_length: int) -> list[str]:
    """The real validator's verdict on *password*."""
    pytest.importorskip("klangk.settings")
    from klangk.lifecycle import _password_policy_errors
    from klangk.settings import KlangkSettings

    state_dir = tempfile.mkdtemp(prefix="pw-state-")
    data_dir = tempfile.mkdtemp(prefix="pw-data-")
    env = {
        "KLANGKD_STATE_DIR": state_dir,
        "KLANGKD_DATA_DIR": data_dir,
        "KLANGKD_MIN_PASSWORD_LENGTH": str(min_length),
    }
    return _password_policy_errors(KlangkSettings(env=env), password)


def _violation_lines(rows: list[tuple[Path, str, int]]) -> list[str]:
    """Formatted violation strings for the failing rows."""
    lines = []
    for path, value, min_length in rows:
        errors = _policy_errors(value, min_length)
        if errors:
            lines.append(f"{path}: '{value}' — {errors[0]}")
    return lines


def test_seeded_passwords_satisfy_policy():
    """Every KLANGKD_DEFAULT_PASSWORD literal clears the boot gate."""
    rows = _scanned_literals()
    assert rows, "scan found no passwords — update the regexes"
    violations = _violation_lines(rows)
    assert not violations, "\n".join(violations)


def test_no_short_fallback_defaults():
    """${KLANGKD_DEFAULT_PASSWORD:-...} fallbacks clear the gate too."""
    demo = _REPO_ROOT / "src" / "frontend" / "e2e-tests" / "demo"
    for path in sorted(demo.rglob("*.sh")):
        for fallback in _FALLBACK_RE.findall(path.read_text()):
            assert not _policy_errors(fallback, 8), (path, fallback)
