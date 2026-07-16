"""Shared fixtures for the CLI E2E suite.

The E2E baseline defaults (``KLANGK_AUTH_MODES=password``, ``_KLANGK_DISABLE_NGINX=1``)
live in :mod:`_e2e_env` (:func:`clean_env`), imported by each suite's
``_start_server``. No ``os.environ`` spread — stray vars can't leak (#1526).
"""
