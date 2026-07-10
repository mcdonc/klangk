"""Shared fixtures for the CLI E2E suite.

These tests launch a real uvicorn server (``_start_server``) inheriting
``os.environ``, then drive ``klangkc`` against it. The production default for
``KLANGK_AUTH_MODES`` (unset, no OIDC) is ``none`` (#1374), which disables
password login — but this suite logs in with a password (``testpass``) and
exercises the full password auth flow. Pin the suite to ``password`` so the
default change doesn't break it.

Mirrors the unit-suite pin in ``src/backend/tests/conftest.py`` and the
backend E2E pin in ``src/backend/e2e-tests/conftest.py``. Per-test explicit env
overrides (``extra_env={"KLANGK_AUTH_MODES": ...}``) still win, since
``_start_server`` spreads ``**os.environ`` before ``extra_env``.
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _e2e_disable_nginx():
    """Suppress the lifespan's nginx watchdog across the CLI E2E suite.

    Mirrors the backend E2E fixture: these tests launch bare ``uvicorn`` and
    don't want the lifespan spawning nginx. Sets the internal, non-user-facing
    ``_KLANGK_DISABLE_NGINX`` kill switch.
    """
    old = os.environ.get("_KLANGK_DISABLE_NGINX")
    os.environ["_KLANGK_DISABLE_NGINX"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("_KLANGK_DISABLE_NGINX", None)
        else:
            os.environ["_KLANGK_DISABLE_NGINX"] = old


@pytest.fixture(scope="session", autouse=True)
def _e2e_default_auth_mode():
    """Pin E2E servers to ``password`` mode for the whole session.

    Session-scoped (not function-scoped ``monkeypatch``) because the server
    subprocess inherits ``os.environ`` at launch. Single-process suite
    (``-p no:xdist``), so no per-worker concern.
    """
    old = os.environ.get("KLANGK_AUTH_MODES")
    os.environ["KLANGK_AUTH_MODES"] = "password"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("KLANGK_AUTH_MODES", None)
        else:
            os.environ["KLANGK_AUTH_MODES"] = old
