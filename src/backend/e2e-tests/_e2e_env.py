"""Shared hermetic env helper for E2E test suites (#1526).

Every E2E suite that launches a subprocess (``runtestserver.py``, ``klangkd``,
or ``klangkc``) must build the child's env from :func:`clean_env`, **not**
``{**os.environ, ...}``. A stray ``KLANGK_*`` var in the CI runner's env
(or one leaked by a prior test) silently becomes the child's config and can
change test results — ``clean_env`` strips all config-affecting prefixes so
the child sees only what the test explicitly sets.

Strips (case-insensitive prefix match): ``KLANGK``, ``_KLANGK``,
``KLANGKC``, ``LOGFIRE``. OS-essential vars (``PATH``, ``HOME``, Nix-specific
``LOCALE_ARCHIVE`` / ``NIX_LD`` / etc.) are preserved so the subprocess can
actually run.
"""

from __future__ import annotations

import os

# Prefixes whose vars are stripped: any env var starting with one of these
# (case-insensitive) is config or debug state that must not leak from the
# ambient env into a test subprocess.
_STRIP_PREFIXES = ("KLANGK", "_KLANGK", "KLANGKC", "LOGFIRE")

# Build-infra vars that locate *artifacts the test must use* (the workspace
# container image, the built plugin packages, the version stamp) — their
# values are produced by devenv's ``klangk:build-workspace-image`` task, not
# by any test, and every E2E server subprocess needs the real ones. These are
# forwarded from the ambient env deliberately (not stripped) so the server
# finds the built image/plugins. They are not test config — overriding one in
# a ``clean_env(...)`` call still wins.
_INFRA_VARS = (
    "KLANGK_PLUGINS_DIR",
    "KLANGK_IMAGE_NAME",
    "KLANGK_VERSION_FILE",
)


def clean_env(**overrides: str) -> dict[str, str]:
    """Return a hermetic env dict for a test subprocess.

    Starts from ``os.environ`` with every config-affecting var stripped, then
    applies ``overrides`` (the test's explicit KLANGK_* / LOGFIRE_* keys).
    The baseline includes ``_KLANGK_DISABLE_NGINX=1`` and
    ``KLANGK_AUTH_MODES=password`` (the E2E default — most suites exercise the
    password auth flow); pass ``KLANGK_AUTH_MODES="none"`` in overrides to
    opt into no-auth mode.

    Tests should call::

        env = clean_env(
            KLANGK_PORT=port,
            KLANGK_DATA_DIR=data_dir,
            ...
        )
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(_STRIP_PREFIXES)
    }
    # Forward the build-infra vars (image / plugins / version stamp) so the
    # server subprocess finds the artifacts devenv built. See _INFRA_VARS.
    for name in _INFRA_VARS:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    # E2E baseline defaults.
    env["_KLANGK_DISABLE_NGINX"] = "1"
    env.setdefault("KLANGK_AUTH_MODES", "password")
    env.update(overrides)
    return env
