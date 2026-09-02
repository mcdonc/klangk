"""Contract tests for the pip-audit allowlist and runner (#2856).

The allowlist (``scripts/pip-audit-ignore.txt``) is the single place where
a pip-audit finding may be accepted; the runner (``scripts/pip-audit.sh``)
expands it into ``--ignore-vuln`` flags. These tests pin the format so an
entry can't silently degrade into an unjustified suppression: every ID
needs a justification comment block above it plus an unexpired re-review
date, and the workflow must stay free of inline ignore flags.
"""

import datetime as dt
import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
ALLOWLIST = SCRIPTS_DIR / "pip-audit-ignore.txt"
RUNNER = SCRIPTS_DIR / "pip-audit.sh"
WORKFLOW = SCRIPTS_DIR.parent / ".github/workflows/python-deps-audit.yml"

# Advisory IDs pip-audit accepts (PYSEC-*, GHSA-*, plus alias schemes).
ID_RE = re.compile(r"^(?:GHSA|PYSEC|OSV|CVE)-[0-9A-Za-z-]+$")
REVIEW_RE = re.compile(r"^#\s*Re-review:\s*(\d{4}-\d{2}-\d{2})\s*$")


def parse_entries(text):
    """Yield ``(id, comment_lines)`` per allowlist entry.

    Comment lines accumulate into the block of the next ID line; a blank
    line detaches the preceding comments (file headers, entry spacing)
    from it. This is the same shape scripts/pip-audit.sh relies on when
    it strips comments to build ``--ignore-vuln`` flags.
    """
    block = []
    for line in text.splitlines():
        if line.startswith("#"):
            block.append(line)
        elif line.strip():
            yield line.strip(), block
            block = []
        else:
            block = []
    return


def entries():
    return list(parse_entries(ALLOWLIST.read_text()))


def review_dates(comment_lines):
    return [m.group(1) for m in map(REVIEW_RE.match, comment_lines) if m]


def assert_ids_well_formed(ids: list) -> None:
    """Every advisory ID matches the ID grammar."""
    for vuln_id in ids:
        assert ID_RE.match(vuln_id), f"malformed advisory ID: {vuln_id}"


def test_ids_are_well_formed_and_unique():
    ids = [vuln_id for vuln_id, _ in entries()]
    assert ids, "allowlist is empty — drop the file and its wiring instead"
    assert_ids_well_formed(ids)
    assert len(ids) == len(set(ids)), "duplicate advisory ID in allowlist"


def test_every_entry_has_a_justification_comment():
    for vuln_id, block in entries():
        rationale = [line for line in block if not REVIEW_RE.match(line)]
        assert rationale, f"{vuln_id}: no justification comment above the ID"


def test_every_entry_has_an_unexpired_review_date():
    today = dt.datetime.now(dt.timezone.utc).date()
    for vuln_id, block in entries():
        dates = review_dates(block)
        assert len(dates) == 1, f"{vuln_id}: expected exactly one Re-review date"
        when = dt.date.fromisoformat(dates[0])
        assert when >= today, (
            f"{vuln_id}: re-review date {when} has passed — re-review the "
            "advisory, then bump it (or drop the entry and fix the dep)"
        )


def test_runner_consumes_the_allowlist():
    """scripts/pip-audit.sh must read IDs from the file, not hardcode them."""
    assert "pip-audit-ignore.txt" in RUNNER.read_text()


def test_workflow_has_no_inline_ignores():
    """Acceptance criterion (#2856): no --ignore-vuln flags in the workflow."""
    assert "--ignore-vuln" not in WORKFLOW.read_text()
