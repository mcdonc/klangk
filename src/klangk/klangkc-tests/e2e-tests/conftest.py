"""Shared fixtures for the CLI E2E suite.

The E2E baseline defaults live in :mod:`_e2e_env` (:func:`clean_env`):
``KLANGKD_AUTH_MODES=password`` and (for UDS-direct suites)
``_KLANGKD_DISABLE_PROXY=1``. The CLI suite launches real ``klangkd`` with
the proxy in front (TCP) because the ``klangk`` CLI it drives has no UDS mode
(#1525); the suites' ``_start_server`` helpers wrap
:mod:`_e2e_server` accordingly. No ``os.environ`` spread — stray vars
can't leak (#1526).

Per-test timeout
----------------
Mirrors the backend E2E ``conftest.py``: the repo-wide ``--timeout=60``
is sized for the unit suites, but these tests spin up real podman
containers (bringup + teardown can exceed 60s on a loaded runner, #1591).
``pytest_collection_modifyitems`` stamps a generous per-test timeout on
every E2E test that doesn't set its own.
"""

import os
import sys

import pytest

# #3064: the CLI's WS-connect wait spans the server's whole bring-up chain
# (create → start → readiness), whose budgets widen to 240s on CI. Widen
# the client wait to match before any test module imports the client (the
# TUI suites run it in-process, so a child-env stamp alone can't reach it).
if os.environ.get("CI"):
    os.environ.setdefault("KLANGKC_WS_CONNECT_TIMEOUT", "240")

# Failure-diagnosability (#2623): attach what the file-streamed klangkd
# logs recorded during a test to that test's failure report. The hooks live
# in ``_e2e_logs`` inside the backend E2E suite's dir (the same shared
# harness this suite's test files already import ``_e2e_server`` from), so
# put that dir on sys.path and re-export the hooks here for pytest to find.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "klangkd-tests", "e2e-tests"
    ),
)
from _e2e_logs import (  # noqa: F401,E402
    pytest_runtest_makereport,
    pytest_runtest_setup,
)

# NOTE: the hooks only attach logs for servers launched with a file
# ``log_path`` — which is ``start_server``'s default since #2623, so every
# server this suite (and the backend e2e suite) launches is covered.

_E2E_TIMEOUT_SECONDS = 300


def pytest_collection_modifyitems(config, items):
    """Give every CLI E2E test a generous per-test timeout (#1591).

    Only stamps tests without an explicit ``timeout`` marker, so a test
    that deliberately pins a tighter/looser budget keeps it.
    """
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_E2E_TIMEOUT_SECONDS))
