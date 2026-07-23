"""Centralized logging configuration (#1467).

Previously logging was configured by an import-time ``logging.basicConfig(...)``
call in ``main.py``'s module body. That had three problems:

1. **Import-order-dependent.** ``basicConfig`` is a no-op if the root logger
   already has handlers, so whichever module imported ``main`` first won.
2. **No settings integration.** Level/format were hardcoded, with no home for
   a ``KLANGKD_LOG_LEVEL`` knob.
3. **No third-party logger management.** uvicorn, sqlalchemy, httpx, ... each
   got their own logger with no central place to silence them.

This module is the single, central, idempotent setup point. It exposes two
module-level functions (no state object):

- :func:`configure_defaults` — applied once at *this module's import* (see the
  module-level call at the bottom). Installs the colored console handler on
  the root logger at INFO, with central third-party silencing. This means
  logging is formatted from the very first log call — including during
  :class:`~klangk.settings.KlangkSettings` construction, which runs *before*
  any ``app`` exists (the settings validators and the ``file:``/``cmd:``
  indirection resolver log).
- :func:`configure` — called once settings are finalized (in
  :func:`klangk.main.build_app`) to re-apply the level from
  ``settings.log_level`` (``KLANGKD_LOG_LEVEL``), overriding the import-time
  defaults. Idempotent, so it is also the **SIGHUP reconfigure** path:
  :func:`klangk.main.Lifecycle._apply_reloaded_settings` calls it right after
  the settings swap (before the subsystem loop, so warnings the loop emits use
  the new level).

Both reach the same private :func:`_apply`, which removes any prior
klangk-tagged handler before adding the new one — so repeated calls (fresh
``configure`` per test app, a HUP reload) never stack duplicate handlers, and
the dedup is robust to other handlers on the root (pytest's ``caplog``,
operator-added handlers, ...).

Emission is unchanged and stays idiomatic: per-module
``logger = logging.getLogger(__name__)`` everywhere. Those obtain named
handles that propagate to the root logger this module configures; centralizing
the *configuration* is what the composition-root refactor (#1426) calls for.
"""

from __future__ import annotations

import logging

__all__ = ["configure", "configure_defaults"]


def _level_to_int(value: str) -> int:
    """Resolve a log-level string to a numeric level.

    Accepts a level name (case-insensitive: ``"debug"``, ``"INFO"``, ...) or
    a numeric string (``"20"``). Unknown values fall back to ``INFO`` — the
    :class:`~klangk.settings.KlangkSettings` ``log_level`` validator rejects
    garbage at construction, so this fallback only defends a misconfigured
    live reload.
    """
    v = (value or "INFO").strip().upper()
    if v.isdigit():
        return int(v)
    named = getattr(logging, v, None)
    if isinstance(named, int):
        return named
    return logging.INFO


# The colored console format, moved here from ``main.py``'s module scope (where
# it lived next to the now-removed ``logging.basicConfig`` call).
_LIGHT_BLUE = "\033[94m"
_RESET = "\033[0m"
_FORMAT = (
    f"{_LIGHT_BLUE}%(asctime)s %(levelname)s:%(name)s:%(message)s{_RESET}"
)
_DATEFMT = "%H:%M:%S"

# The level applied by ``configure_defaults()`` (the pre-settings phase).
# ``configure(settings)`` overrides it with ``settings.log_level`` once settings
# are constructed.
_DEFAULT_LEVEL = logging.INFO

# Third-party loggers managed centrally (logger name -> level). These are
# libraries klangk depends on that log at their own verbosity by default and
# would drown klangk's own INFO output. Levels are re-applied on every
# configure() so an operator raising ``KLANGKD_LOG_LEVEL`` to DEBUG still gets a
# quiet chatty-library surface unless they raise these explicitly via a future
# per-logger override.
_THIRD_PARTY_LEVELS: dict[str, int | str] = {
    # uvicorn's startup/error logs are useful; per-request access logs are
    # noisy at default verbosity.
    "uvicorn": "INFO",
    "uvicorn.error": "INFO",
    "uvicorn.access": "WARNING",
    # SQLAlchemy engine emits every query at INFO when unchecked.
    "sqlalchemy.engine": "WARNING",
    # httpx/httpcore log every request/connection at INFO.
    "httpx": "WARNING",
    "httpcore": "WARNING",
    # watchfiles spams detection/rust internals at INFO.
    "watchfiles": "WARNING",
    # asyncio debug chatter.
    "asyncio": "WARNING",
}


def _apply(level: int) -> None:
    """Install/replace the klangk root handler at ``level`` + silence 3rd-party.

    Shared by :func:`configure_defaults` (pre-settings, default level) and
    :func:`configure` (settings-driven level). Idempotent: any handler
    previously tagged by this module is removed from the root logger before
    the new one is added, so repeated calls never stack duplicate handlers.
    The handler is tagged via a private attribute so this dedup is robust to
    other handlers on the root (pytest's ``caplog`` handler, operator-added
    handlers, ...).
    """
    root = logging.getLogger()

    # Drop any pre-existing klangk-tagged handler(s) so we never stack.
    for handler in list(root.handlers):
        if getattr(handler, "_klangk_log_handler", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler()
    # Private tag for cross-call dedup (see the loop above).
    handler._klangk_log_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.setLevel(level)
    root.addHandler(handler)

    root.setLevel(level)

    for name, lvl in _THIRD_PARTY_LEVELS.items():
        logging.getLogger(name).setLevel(lvl)


def configure_defaults() -> None:
    """Configure root logging with default (pre-settings) values.

    Applied once at this module's import (see the module-level call below), so
    logging is formatted from the very first log call — including during
    ``KlangkSettings`` construction, which runs before any ``app`` exists.
    Idempotent. :func:`configure` later overrides the level from
    ``KLANGKD_LOG_LEVEL``.
    """
    _apply(_DEFAULT_LEVEL)


def configure(settings) -> None:
    """Re-apply configuration from finalized settings.

    Called in :func:`klangk.main.build_app` (once settings are constructed) and
    again on every SIGHUP reload (after the settings swap, before the subsystem
    reconfigure loop) so ``KLANGKD_LOG_LEVEL`` takes effect without a process
    restart (#1587). Reads ``settings.log_level`` live; idempotent.
    """
    _apply(_level_to_int(settings.log_level))


# Configure sensible defaults at import so logging is formatted from the very
# first log call — including during ``KlangkSettings`` construction, which runs
# before any ``app`` exists. ``configure(settings)`` (in ``build_app``) later
# overrides the level from ``KLANGKD_LOG_LEVEL``. (#1467)
configure_defaults()
