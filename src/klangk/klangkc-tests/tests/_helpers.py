"""Shared helpers for the CLI unit-test suite.

Mirrors the daemon suite's ``klangkd-tests/tests/_helpers.py`` helper of the
same name (kept separate: the two suites share no imports, #2662).
"""

from __future__ import annotations

import atexit
import shutil
import tempfile

_mktemp_registry: list[str] = []


def rmtree_registered_temps() -> None:
    """Remove every dir :func:`tracked_mkdtemp` made (process exit, #2662)."""
    for path in _mktemp_registry:
        shutil.rmtree(path, ignore_errors=True)


def tracked_mkdtemp(prefix: str) -> str:
    """``tempfile.mkdtemp`` whose dir is removed at process exit (#2662).

    Prefer pytest's ``tmp_path`` for ordinary per-test scratch. Use this only
    where ``tmp_path`` can't serve: the short AF_UNIX socket paths macOS
    needs (#1983 — pytest tmp paths resolve through ``/private/var`` and can
    exceed the 104-char ``sun_path`` limit). The atexit sweep is the safety
    net for paths that skip fixture teardown (crashed runs); explicit
    ``shutil.rmtree`` teardown stays authoritative where one exists.
    """
    if not _mktemp_registry:
        atexit.register(rmtree_registered_temps)
    path = tempfile.mkdtemp(prefix=prefix)
    _mktemp_registry.append(path)
    return path
