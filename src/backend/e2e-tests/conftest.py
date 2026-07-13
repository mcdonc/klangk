"""Shared fixtures for the backend E2E suite.

The E2E baseline defaults live in :mod:`_e2e_env` (:func:`clean_env`):
``KLANGK_AUTH_MODES=password`` (most suites exercise the password auth flow)
and ``_KLANGK_DISABLE_NGINX=1`` (bare-uvicorn launches; the lifespan's nginx
would fight a dev nginx). Every server launch builds its env via
``clean_env(...)`` — no ``os.environ`` spread, so stray vars can't leak
(#1526). Tests that need ``none`` mode pass ``KLANGK_AUTH_MODES="none"`` in
their ``clean_env()`` overrides.
"""
