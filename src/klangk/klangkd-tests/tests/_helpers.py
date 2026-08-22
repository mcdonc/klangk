"""Shared test helpers for the backend unit-test suite.

Importable from any test module (``from _helpers import make_settings``).
Kept out of ``conftest.py`` because some call sites construct settings at
module import time (module-level constants), where pytest fixtures are not
yet available.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

from klangk.settings import KlangkSettings

# Per-test DB holder (#1578). The autouse ``temp_data_dir`` fixture builds a
_mktemp_registry: list[str] = []


def rmtree_registered_temps() -> None:
    """Remove every dir :func:`tracked_mkdtemp` made (process exit, #2662)."""
    for path in _mktemp_registry:
        shutil.rmtree(path, ignore_errors=True)


def tracked_mkdtemp(prefix: str) -> str:
    """``tempfile.mkdtemp`` whose dir is removed at process exit (#2662).

    Prefer pytest's ``tmp_path`` for ordinary per-test scratch. Use this only
    where ``tmp_path`` can't serve: settings built at module import time
    (before fixtures set ``KLANGKD_*_DIR``) and the short AF_UNIX socket paths
    macOS needs (#1983 — pytest tmp paths resolve through ``/private/var``
    and can exceed the 104-char ``sun_path`` limit). The atexit sweep is the
    safety net for paths that skip fixture teardown (crashed runs); explicit
    ``shutil.rmtree`` teardown stays authoritative where one exists.
    """
    if not _mktemp_registry:
        atexit.register(rmtree_registered_temps)
    path = tempfile.mkdtemp(prefix=prefix)
    _mktemp_registry.append(path)
    return path


# Per-test DB holder (#1578). The autouse ``temp_data_dir`` fixture builds a
# DB from the per-test settings and stashes it here (``set_test_db``); the
# ``app_state`` fixture and ``wire_db_and_model`` read it via ``get_test_db``
# so every ``app_state.db`` in the test points at the same schema-bearing
# instance. This replaces the pre-#1578 ``_current_db`` ContextVar, which
# is gone (its env-only lazy fallback was the #1551 divergence path).
# Cleared on fixture teardown.
_test_db = None


def set_test_db(db) -> None:
    """Stash the per-test DB so ``wire_db_and_model`` can reuse it."""
    global _test_db
    _test_db = db


def get_test_db():
    """Return the per-test DB, or raise ``LookupError`` if none is set."""
    if _test_db is None:
        raise LookupError("no per-test DB; call set_test_db first")
    return _test_db


def reset_test_db() -> None:
    """Clear the per-test DB (fixture teardown)."""
    global _test_db
    _test_db = None


def make_settings(
    env: dict | None = None, config_file: str | None = None
) -> KlangkSettings:
    """Build ``KlangkSettings`` for a test, injecting required dirs if absent.

    ``state_dir`` and ``data_dir`` are required (no defaults, #1461). Tests
    that pass an explicit env dict (bypassing ``os.environ``) must include
    both, or they get temp defaults so the validator passes. Pass an explicit
    value in ``env`` to override.
    """
    env = dict(env or {})
    # Fall back to os.environ (set by the autouse temp_data_dir fixture)
    # before creating temp dirs. Tracked so module-import-time call sites
    # (which run before any fixture) can't orphan them (#2662).
    env.setdefault(
        "KLANGKD_STATE_DIR",
        os.environ.get("KLANGKD_STATE_DIR")
        or tracked_mkdtemp("klangk-state-"),
    )
    env.setdefault(
        "KLANGKD_DATA_DIR",
        os.environ.get("KLANGKD_DATA_DIR") or tracked_mkdtemp("klangk-data-"),
    )
    # On macOS the default socket paths derived from state_dir may exceed the
    # 104-char AF_UNIX sun_path limit because tempfile paths resolve through
    # /private/var/folders/... Set short socket paths so the settings
    # validator passes (#1983).
    if sys.platform == "darwin":
        _sock_dir = tracked_mkdtemp("ks-")
        env.setdefault("KLANGKD_SOCKET", os.path.join(_sock_dir, "k.sock"))
        env.setdefault(
            "KLANGKD_CADDY_ADMIN_SOCKET",
            os.path.join(_sock_dir, "caddy.sock"),
        )
    return KlangkSettings(env=env, config_file=config_file)


def wire_db_and_model(app) -> None:
    """Attach ``db`` + ``model`` + ``acl`` to a test ``app.state`` namespace.

    App code reaches the converted domains (tokens, login_attempts,
    invitations, ports) via ``app.state.model.<domain>.<method>``, which
    resolves ``self.app.state.db``. The FastAPI permission layer
    (``ACL(app)``, #1577) reaches ``app.state.model.{users,acl}``, so
    any test that builds a mock app and exercises a request / WebSocket
    path must wire ``acl`` too.

    Reuses the per-test DB (the autouse ``temp_data_dir`` fixture builds
    one and runs ``init_db`` against it) so ``app.state.db`` is the *same*
    schema-bearing instance the rest of the test reaches — not a fresh DB
    on a different temp path (which would hit "no such table"). Idempotent:
    skips re-wiring when already present.
    """
    from klangk.model import Model
    from klangk.model.db import DB
    from klangk.acl import ACL

    state = app.state
    if getattr(state, "db", None) is None:
        try:
            state.db = get_test_db()
        except LookupError:
            state.db = DB(app)
    if getattr(state, "model", None) is None:
        state.model = Model(app)
    if getattr(state, "acl", None) is None:
        state.acl = ACL(app)
    if getattr(state, "features", None) is None:
        # #1977: agent.is_disabled + seed_agent_user read app.state.features
        # (is_enabled + frontend_config). A benign default mock so test app
        # states that don't care about features still satisfy those reads;
        # tests that do (e.g. the agent-disabled / config suites) configure
        # it explicitly.
        from unittest.mock import MagicMock

        features = MagicMock()
        features.is_enabled.return_value = True
        features.frontend_config.return_value = {}
        state.features = features
