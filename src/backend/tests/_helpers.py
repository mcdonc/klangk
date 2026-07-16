"""Shared test helpers for the backend unit-test suite.

Importable from any test module (``from _helpers import make_settings``).
Kept out of ``conftest.py`` because some call sites construct settings at
module import time (module-level constants), where pytest fixtures are not
yet available.
"""

from __future__ import annotations

import tempfile

from klangk_backend.settings import KlangkSettings


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
        "KLANGK_STATE_DIR", tempfile.mkdtemp(prefix="klangk-state-")
    )
    env.setdefault("KLANGK_DATA_DIR", tempfile.mkdtemp(prefix="klangk-data-"))
    return KlangkSettings(env=env, config_file=config_file)


def wire_db_and_model(state) -> None:
    """Attach ``db`` + ``model`` + ``acl`` to a test ``app_state`` namespace.

    App code reaches the converted domains (tokens, login_attempts,
    invitations, ports) via ``app_state.model.<domain>.<method>``, which
    resolves ``self.app_state.db``. The FastAPI permission layer
    (``ACL(app_state)``, #1577) reaches ``app_state.model.{users,acl}``, so
    any test that builds an ``app_state`` namespace and exercises a request
    / WebSocket path must wire ``acl`` too. Every test that builds an
    ``app_state`` namespace and constructs an owned instance that touches
    those domains (``Auth``, ``ContainerRegistry``, …) must wire all three.

    Reuses the ContextVar-bound DB when one exists (the autouse
    ``temp_data_dir`` fixture binds it and runs ``init_db`` against it), so
    ``app_state.db`` is the *same* schema-bearing instance the rest of the
    test reaches via the backstop — not a fresh DB on a different temp path
    (which would hit "no such table"). Idempotent: skips re-wiring when
    already present.
    """
    from klangk_backend.model import Model
    from klangk_backend.model.db import DB, get_current_db
    from klangk_backend.acl import ACL

    if getattr(state, "db", None) is None:
        try:
            state.db = get_current_db()
        except LookupError:
            state.db = DB(state.settings)
    if getattr(state, "model", None) is None:
        state.model = Model(state)
    if getattr(state, "acl", None) is None:
        state.acl = ACL(state)
