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

    ``state_dir`` is required (no default, #1461). ``data_dir`` defaults to
    ``<state_dir>/data`` and ``plugins_dir`` to ``<state_dir>/plugins``
    (#1506), but tests pass an explicit ``data_dir`` temp dir so they don't
    write a real DB into the derived location. Tests that pass an explicit
    env dict (bypassing ``os.environ``) get temp defaults for both so the
    validator passes; pass an explicit value in ``env`` to override.
    """
    env = dict(env or {})
    env.setdefault(
        "KLANGK_STATE_DIR", tempfile.mkdtemp(prefix="klangk-state-")
    )
    env.setdefault("KLANGK_DATA_DIR", tempfile.mkdtemp(prefix="klangk-data-"))
    return KlangkSettings(env=env, config_file=config_file)
