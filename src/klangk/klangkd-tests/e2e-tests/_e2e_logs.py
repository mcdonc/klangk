"""Attach file-streamed klangkd server logs to failing e2e reports (#2623).

An e2e server launched with ``log_path`` streams its combined output to a
file inside the data dir, and ``stop_server`` deletes that dir — so when a
test failed, the only record of what the *server* did (tracebacks, the last
message before an abrupt WebSocket drop, slow-podman warnings) vanished with
teardown. The CLI E2E ``test_web_sees_cli_created_windows`` failure (#2622
run) was undiagnosable for exactly this reason: the client-side
``ConnectionClosedError`` said the socket died, but nothing showed why.

This module is imported by the e2e conftests (klangkd-tests and
klangkc-tests), which re-export its two hooks so pytest registers them on
the conftest itself:

* ``pytest_runtest_setup`` snapshots the byte size of every live log (from
  :func:`_e2e_server.active_log_paths`) at test start.
* ``pytest_runtest_makereport`` appends each log's growth during the test
  to the failure report as a section, capped to the final ``_MAX_TAIL_BYTES``.
  A server whose class-scoped fixture only started during the failing test's
  own setup has no snapshot — for those, the capped tail is attached instead
  (this is the ``-k`` single-test case; under a full-class run every test
  after the first gets a precise slice).

Only file-streamed logs are attachable; pipe-captured servers are drained
solely at process exit and stay invisible (see ``_e2e_server``).
"""

from __future__ import annotations

import os

import pytest

from _e2e_server import active_log_paths

# Upper bound on bytes attached per server log. The test-window slice is
# usually far smaller; the cap only guards a runaway log from flooding the
# CI failure output, keeping the tail (the part nearest the failure).
_MAX_TAIL_BYTES = 256 * 1024

_OFFSETS = pytest.StashKey[dict[str, int]]


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Record where each live server log ends, before the test runs."""
    snapshot: dict[str, int] = {}
    for path in active_log_paths():
        try:
            snapshot[path] = os.path.getsize(path)
        except OSError:
            snapshot[path] = 0
    item.stash[_OFFSETS] = snapshot


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On failure, attach what each server logged during this test."""
    outcome = yield
    rep = outcome.get_result()
    if not rep.failed:
        return
    snapshot = item.stash.get(_OFFSETS, None) or {}
    seen = set(snapshot)
    for path, offset in snapshot.items():
        text = read_log_since(path, offset)
        if text:
            rep.sections.append((f"klangkd log: {path}", text))
    # Servers whose fixture started during this test's setup (no snapshot:
    # the setup hook ran before the fixture) still get their tail attached —
    # coarser than a slice, but the section exists precisely when the test
    # under diagnosis is the first one to touch the server (-k runs).
    for path in active_log_paths():
        if path in seen:
            continue
        text = read_log_since(path, 0)
        if text:
            rep.sections.append((f"klangkd log tail: {path}", text))


def read_log_since(path: str, offset: int) -> str:
    """Read a log from *offset* to EOF, capped to the last ``_MAX_TAIL_BYTES``.

    Returns ``""`` when the file is gone or has not grown past *offset*
    (a teardown that stopped the server mid-test deletes the log — nothing
    to attach then). Binary read + ``errors="replace"`` so a multi-byte
    UTF-8 sequence split at the boundary cannot raise.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    start = max(offset, 0)
    if size <= start:
        return ""
    if size - start > _MAX_TAIL_BYTES:
        start = size - _MAX_TAIL_BYTES
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read().decode(errors="replace")
    except OSError:
        return ""
