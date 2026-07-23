"""Shared test helpers for the backend unit-test suite.

Importable from any test module (``from _helpers import make_settings``).
Kept out of ``conftest.py`` because some call sites construct settings at
module import time (module-level constants), where pytest fixtures are not
yet available.
"""

from __future__ import annotations

import tempfile

from klangk.settings import KlangkSettings

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
    env.setdefault(
        "KLANGKD_STATE_DIR", tempfile.mkdtemp(prefix="klangk-state-")
    )
    env.setdefault("KLANGKD_DATA_DIR", tempfile.mkdtemp(prefix="klangk-data-"))
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
