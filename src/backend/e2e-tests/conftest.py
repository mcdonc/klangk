"""Shared fixtures for the backend E2E suite.

These tests launch a real uvicorn server (subprocess) inheriting ``os.environ``.
The production default for ``KLANGK_AUTH_MODES`` (unset, no OIDC) is ``none``
(#1374), which disables password login/registration — but the E2E suite
exercises the password auth flow almost exclusively (login, register, lockout,
ACL via ``_auth_headers``). Pin the suite to ``password`` so the default change
doesn't break it.

Mirrors the unit-suite pin in ``src/backend/tests/conftest.py``. Per-test
explicit env overrides still win: a test that sets ``"KLANGK_AUTH_MODES": "none"``
in its server env dict (after the ``**os.environ`` spread) overrides this —
see ``TestNginxAuthLocalAcl``.
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _e2e_disable_nginx():
    """Suppress the lifespan's nginx watchdog across the E2E suite.

    nginx ownership is unconditional in real runs, but these tests launch the
    backend via bare ``uvicorn`` and (in dev) another nginx already holds
    ``KLANGK_NGINX_PORT``; a second spawned by the lifespan would fight it for
    the port (and on CI, loop forever). Sets the internal, non-user-facing
    ``_KLANGK_DISABLE_NGINX`` kill switch — same as the unit-suite fixture.
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
    subprocess inherits ``os.environ`` at launch — the value must be set once,
    before any server fixture starts, and persist for the run. Single-process
    suite (``-p no:xdist``), so there's no per-worker concern.
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
