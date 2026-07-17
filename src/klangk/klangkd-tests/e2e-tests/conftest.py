"""Shared fixtures for the backend E2E suite.

The E2E baseline defaults live in :mod:`_e2e_env` (:func:`clean_env`):
``KLANGK_AUTH_MODES=password`` (most suites exercise the password auth flow)
and ``_KLANGK_DISABLE_NGINX=1`` (bare-uvicorn launches; the lifespan's nginx
would fight a dev nginx). Every server launch builds its env via
``clean_env(...)`` — no ``os.environ`` spread, so stray vars can't leak
(#1526). Tests that need ``none`` mode pass ``KLANGK_AUTH_MODES="none"`` in
their ``clean_env()`` overrides.

Per-test timeout
----------------
The repo-wide ``--timeout=60`` (set in ``src/klangk/pyproject.toml`` addopts,
#1513) is sized for the *unit* suites. E2E tests spin up real podman
containers: bringup (``container_ready``) plus teardown (the workspace DELETE
that stops+removes the container) can legitimately exceed 60s on a loaded
runner, and the 60s cap races the teardown ``finally`` inside the test body
(the cap covers the whole call phase, body included). pytest-timeout then
kills the process mid-cleanup — surfacing as a spurious "Timeout (>60.0s)"
on a test whose assertion already passed (#1591).

``pytest_collection_modifyitems`` stamps a generous per-test timeout on every
E2E test that doesn't set its own, so container-spinning tests get headroom
without each author having to remember ``@pytest.mark.timeout(...)``. A
per-test ``timeout`` marker overrides the global ``--timeout`` (pytest-timeout
resolves the closest marker first).
"""

import pytest

# Real containers need real time: bringup + exec + teardown (stop+rm the
# podman container via the workspace DELETE) can run well past the unit
# suite's 60s cap on a loaded GitHub-hosted runner. 300s leaves ample
# headroom while still bounding a genuinely-hung test.
_E2E_TIMEOUT_SECONDS = 300


def pytest_collection_modifyitems(config, items):
    """Give every E2E test a generous per-test timeout (#1591).

    Only stamps tests without an explicit ``timeout`` marker, so a test
    that deliberately pins a tighter/looser budget keeps it.
    """
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_E2E_TIMEOUT_SECONDS))
