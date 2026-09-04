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
  ``settings.log_level`` (``KLANGKD_LOG_LEVEL``) *and the output format* from
  ``settings.log_format`` (``KLANGKD_LOG_FORMAT``: ``text`` — the colored
  console format — or ``json`` — one JSON object per line for SIEM ingestion,
  #3156), overriding the import-time defaults. Idempotent, so it is also the
  **SIGHUP reconfigure** path: :func:`klangk.main.Lifecycle.apply_reloaded_settings`
  calls it right after the settings swap (before the subsystem loop, so warnings
  the loop emits use the new level/format).

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

import json
import logging
from datetime import UTC, datetime

__all__ = ["configure", "configure_defaults"]


def level_to_int(value: str) -> int:
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
DEFAULT_LEVEL = logging.INFO

# The format applied by ``configure_defaults()`` (the pre-settings phase).
# ``configure(settings)`` overrides it with ``settings.log_format`` once
# settings are constructed. Pre-settings output is always human-readable
# text: the JSON choice lives in settings, which do not exist yet at import.
DEFAULT_FORMAT = "text"


def format_is_json(value: str | None) -> bool:
    """Whether a ``KLANGKD_LOG_FORMAT`` value selects JSON output.

    The :class:`~klangk.settings.KlangkSettings` ``log_format`` validator
    normalizes to ``text``/``json`` and rejects garbage at construction, so
    this loose comparison only defends a misconfigured live reload (same
    posture as :func:`level_to_int`'s INFO fallback).
    """
    return (value or "").strip().lower() == "json"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for SIEM ingestion (#3156).

    Hand-rolled (no ``python-json-logger`` dependency): emits ``timestamp``
    (ISO-8601 UTC), ``level``, ``logger``, and ``message`` — plus ``exc_info``
    when the record carries an exception. Covers everything that reaches the
    root handler, klangk's own loggers and third-party ones alike, so the
    whole stream is uniform and free of ANSI color codes.

    Formatting never raises: a record whose ``%``-args don't match its format
    string would make ``getMessage()`` raise, and the stdlib ``handleError``
    fallback would dump a raw traceback line into the stream — one more line
    a SIEM parser cannot ingest. The helpers below degrade to a best-effort
    message so every emitted line stays a parseable JSON object (#3156).
    """

    def safe_message(self, record: logging.LogRecord) -> str:
        """``getMessage()``, degrading to the raw msg on a bad %-format."""
        try:
            return record.getMessage()
        except Exception:
            return str(record.msg)

    def has_exception(self, record: logging.LogRecord) -> bool:
        """Whether the record carries a real exception (not ``(None,)*3``)."""
        return bool(record.exc_info) and record.exc_info[0] is not None

    def safe_exception(self, record: logging.LogRecord) -> str:
        """``formatException()``, degrading to ``repr(value)`` on failure."""
        try:
            return self.formatException(record.exc_info)
        except Exception:
            return repr(record.exc_info[1])

    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, tz=UTC)
        payload = {
            "timestamp": created.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": self.safe_message(record),
        }
        if self.has_exception(record):
            payload["exc_info"] = self.safe_exception(record)
        return json.dumps(payload)


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


def make_formatter(log_format: str) -> logging.Formatter:
    """Build the root-handler formatter for a ``KLANGKD_LOG_FORMAT`` value.

    ``json`` (and its case variants) gets :class:`JsonFormatter`; anything
    else — the default ``text`` included — gets the colored console format.
    """
    if format_is_json(log_format):
        return JsonFormatter()
    return logging.Formatter(_FORMAT, datefmt=_DATEFMT)


def _apply(level: int, log_format: str = DEFAULT_FORMAT) -> None:
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
    handler.setFormatter(make_formatter(log_format))
    handler.setLevel(level)
    root.addHandler(handler)

    root.setLevel(level)

    for name, lvl in _THIRD_PARTY_LEVELS.items():
        logging.getLogger(name).setLevel(lvl)

    # LiteLLM attaches its own handlers; stop propagation so its log
    # records don't also reach klangk's root handler (double-logging,
    # #2087).
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(name).propagate = False


def configure_defaults() -> None:
    """Configure root logging with default (pre-settings) values.

    Applied once at this module's import (see the module-level call below), so
    logging is formatted from the very first log call — including during
    ``KlangkSettings`` construction, which runs before any ``app`` exists.
    Idempotent. :func:`configure` later overrides the level from
    ``KLANGKD_LOG_LEVEL`` and the format from ``KLANGKD_LOG_FORMAT``.
    """
    _apply(DEFAULT_LEVEL, DEFAULT_FORMAT)


def configure(settings) -> None:
    """Re-apply configuration from finalized settings.

    Called in :func:`klangk.main.build_app` (once settings are constructed) and
    again on every SIGHUP reload (after the settings swap, before the subsystem
    reconfigure loop) so ``KLANGKD_LOG_LEVEL`` and ``KLANGKD_LOG_FORMAT`` take
    effect without a process restart (#1587). Reads ``settings.log_level`` and
    ``settings.log_format`` live; idempotent.
    """
    _apply(level_to_int(settings.log_level), settings.log_format)


# Configure sensible defaults at import so logging is formatted from the very
# first log call — including during ``KlangkSettings`` construction, which runs
# before any ``app`` exists. ``configure(settings)`` (in ``build_app``) later
# overrides the level from ``KLANGKD_LOG_LEVEL``. (#1467)
configure_defaults()
