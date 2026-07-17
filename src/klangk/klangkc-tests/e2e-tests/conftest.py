"""Shared fixtures for the CLI E2E suite.

The E2E baseline defaults live in :mod:`_e2e_env` (:func:`clean_env`):
``KLANGK_AUTH_MODES=password`` and (for UDS-direct suites)
``_KLANGK_DISABLE_PROXY=1``. The CLI suite launches real ``klangkd`` with
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

import pytest

_E2E_TIMEOUT_SECONDS = 300


def pytest_collection_modifyitems(config, items):
    """Give every CLI E2E test a generous per-test timeout (#1591).

    Only stamps tests without an explicit ``timeout`` marker, so a test
    that deliberately pins a tighter/looser budget keeps it.
    """
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_E2E_TIMEOUT_SECONDS))
