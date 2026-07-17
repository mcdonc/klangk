"""Centralized logging configuration owned by ``app.state`` (#1467).

Previously logging was configured by an import-time ``logging.basicConfig(...)``
call in ``main.py``'s module body. That had three problems:

1. **Import-order-dependent.** ``basicConfig`` is a no-op if the root logger
   already has handlers, so whichever module imported ``main`` first won.
2. **No settings integration.** Level/format were hardcoded, with no home for
   a ``KLANGK_LOG_LEVEL`` knob.
3. **No third-party logger management.** uvicorn, sqlalchemy, httpx, ... each
   got their own logger with no central place to silence them.

:class:`Logger` is the owned state object that fixes all three. It is
constructed once in :func:`klangk.main.build_app` and hung on
``app.state.logger`` — the same ``X(app)`` composition-root pattern every
other subsystem (``Util``, ``Model``, ``Container``, ...) uses. It owns:

- the single :class:`~logging.StreamHandler` on the root logger (with the
  colored format previously hardcoded in ``main.py``),
- the root level, read live from ``settings.log_level`` (``KLANGK_LOG_LEVEL``),
- central silencing of chatty third-party loggers.

Per-module ``logger = logging.getLogger(__name__)`` calls elsewhere in the
package are **kept** — those obtain *named logger handles* (not configuration)
that propagate to the root logger :class:`Logger` configures. They are the
idiomatic Python pattern and cannot be replaced with ``app.state.logger``
references: several modules (notably :mod:`klangk.settings`) log during
construction, before any ``app`` exists. Centralizing the *configuration* in
one deliberate setup point is what the composition-root refactor (#1426)
calls for; the named-handle pattern is orthogonal and correct.

Live reload: :meth:`reconfigure` re-applies the level from a freshly-reloaded
settings object, so ``KLANGK_LOG_LEVEL`` takes effect on a SIGHUP restart
without a process restart (#1587). It is a member of the
``_apply_reloaded_settings`` subsystem list in :mod:`klangk.main`.
"""

from __future__ import annotations

import logging

__all__ = ["Logger"]


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


class Logger:
    """Owned logging-configuration state object (``app.state.logger``).

    Constructed once in :func:`klangk.main.build_app` and stored on
    ``app.state.logger``. Owns the root logger's handler, format, level, and
    third-party silencing. Reads ``settings.log_level`` live so a SIGHUP
    reload of ``KLANGK_LOG_LEVEL`` propagates without a process restart.
    """

    # The colored console format, moved here from ``main.py``'s module scope
    # (where it lived next to the now-removed ``logging.basicConfig`` call).
    _LIGHT_BLUE = "\033[94m"
    _RESET = "\033[0m"
    _FORMAT = (
        f"{_LIGHT_BLUE}%(asctime)s %(levelname)s:%(name)s:%(message)s{_RESET}"
    )
    _DATEFMT = "%H:%M:%S"

    # Third-party loggers managed centrally (logger name -> level). These are
    # libraries klangk depends on that log at their own verbosity by default
    # and would drown klangk's own INFO output. Levels are re-applied on every
    # configure()/reconfigure() so an operator raising ``KLANGK_LOG_LEVEL``
    # to DEBUG still gets a quiet chatty-library surface unless they raise
    # these explicitly via a future per-logger override.
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

    def __init__(self, app):
        self.app = app
        # The handler this instance installed on the root logger, tracked so
        # reconfigure() can replace rather than stack. See configure() for
        # the cross-instance dedup that also keeps repeated construction
        # (e.g. per-test app_state mocks) from stacking handlers.
        self._handler: logging.Handler | None = None
        self.configure()

    def reconfigure(self, app) -> None:
        """Re-apply configuration against a freshly-reloaded ``app``.

        Called by the SIGHUP reload path in :mod:`klangk.main` alongside
        every other subsystem's ``reconfigure``. Re-reads ``log_level`` off
        the new settings and re-applies the root level (and third-party
        levels), so ``KLANGK_LOG_LEVEL`` takes effect without a process
        restart (#1587).
        """
        self.app = app
        self.configure()

    @property
    def _level(self) -> int:
        """The root level, resolved live from settings at call time."""
        return _level_to_int(self.app.state.settings.log_level)

    def configure(self) -> None:
        """Configure the root logger: handler, format, level, third-party.

        Idempotent across instances: any handler previously tagged by a
        :class:`Logger` is removed from the root logger before the new one
        is added, so repeated construction (a fresh ``Logger`` per test app,
        or a reconfigure) never stacks duplicate handlers. The handler is
        tagged via a private attribute so this dedup is robust to other
        handlers on the root (pytest's ``caplog`` handler, operator-added
        handlers, ...).
        """
        root = logging.getLogger()
        level = self._level

        # Drop any pre-existing klangk-tagged handler(s) so we never stack.
        for handler in list(root.handlers):
            if getattr(handler, "_klangk_log_handler", False):
                root.removeHandler(handler)

        handler = logging.StreamHandler()
        # Private tag for cross-instance dedup (see the loop above).
        handler._klangk_log_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter(self._FORMAT, datefmt=self._DATEFMT)
        )
        handler.setLevel(level)
        root.addHandler(handler)
        self._handler = handler

        root.setLevel(level)

        for name, lvl in self._THIRD_PARTY_LEVELS.items():
            logging.getLogger(name).setLevel(lvl)
