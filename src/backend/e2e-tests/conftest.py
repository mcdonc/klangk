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
