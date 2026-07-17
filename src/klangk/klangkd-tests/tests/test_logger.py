"""Tests for centralized logging configuration (#1467).

Logging is configured by two module-level functions in :mod:`klangk.logger`
(no state object):

- ``configure_defaults()`` — applied at import; INFO level, colored format,
  third-party silencing. Active before any ``app``/settings exists.
- ``configure(settings)`` — re-applies the level from ``settings.log_level``
  once settings are finalized (build_app), and again on every SIGHUP reload.
"""

import logging

import pytest

from _helpers import make_settings
from klangk import logger as logger_mod


@pytest.fixture
def clean_root():
    """Snapshot and restore the root logger (handlers + level) per test.

    These tests mutate the global root logger; they must not leak into sibling
    tests (xdist runs files in workers, but modules within a file share a
    process).
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield root
    for h in list(root.handlers):
        if getattr(h, "_klangk_log_handler", False):
            root.removeHandler(h)
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _klangk_handlers(root):
    return [
        h for h in root.handlers if getattr(h, "_klangk_log_handler", False)
    ]


def _make_settings(level=None):
    env = {}
    if level is not None:
        env["KLANGK_LOG_LEVEL"] = level
    return make_settings(env)


class TestLevelToInt:
    """The private level-string resolver (#1467)."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ],
    )
    def test_named_levels(self, name, expected):
        assert logger_mod._level_to_int(name) == expected

    def test_case_insensitive(self):
        assert logger_mod._level_to_int("debug") == logging.DEBUG
        assert logger_mod._level_to_int("WaRnInG") == logging.WARNING

    def test_numeric_string(self):
        assert logger_mod._level_to_int("20") == logging.INFO
        assert logger_mod._level_to_int("10") == logging.DEBUG

    def test_empty_or_none_falls_back_to_info(self):
        assert logger_mod._level_to_int("") == logging.INFO
        assert logger_mod._level_to_int(None) == logging.INFO

    def test_unknown_falls_back_to_info(self):
        # Settings validator rejects garbage at construction; this fallback
        # only defends a misconfigured live reload.
        assert logger_mod._level_to_int("verbose") == logging.INFO


class TestConfigureDefaults:
    """The pre-settings phase: logging is configured with defaults before any
    app/Settings exists (#1467), so logs emitted during KlangkSettings
    construction are formatted."""

    def test_configures_root_without_an_app(self, clean_root):
        # Start from a known state: no klangk handler (the module-level
        # configure_defaults() may have installed one at import).
        for h in list(clean_root.handlers):
            if getattr(h, "_klangk_log_handler", False):
                clean_root.removeHandler(h)
        assert _klangk_handlers(clean_root) == []
        # No app, no settings — yet the root logger gets a handler.
        logger_mod.configure_defaults()
        assert len(_klangk_handlers(clean_root)) == 1

    def test_default_level_is_info(self, clean_root):
        logger_mod.configure_defaults()
        assert clean_root.level == logging.INFO
        assert _klangk_handlers(clean_root)[0].level == logging.INFO

    def test_default_handler_is_colored(self, clean_root):
        logger_mod.configure_defaults()
        handler = _klangk_handlers(clean_root)[0]
        assert "\033[94m" in handler.formatter._fmt  # _LIGHT_BLUE

    def test_defaults_silence_third_party(self, clean_root):
        logger_mod.configure_defaults()
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING

    def test_defaults_idempotent(self, clean_root):
        logger_mod.configure_defaults()
        logger_mod.configure_defaults()
        assert len(_klangk_handlers(clean_root)) == 1

    def test_module_level_default_level_constant(self):
        """The import-time call uses this constant (coverage marks the
        module-level call line executed at import)."""
        assert logger_mod._DEFAULT_LEVEL == logging.INFO

    def test_settings_construction_logs_through_configured_root(
        self, clean_root, caplog
    ):
        """End-to-end: with defaults active, a log emitted during KlangkSettings
        construction is captured (the scenario #1467's two-phase design serves).
        """
        logger_mod.configure_defaults()
        with caplog.at_level(logging.WARNING, logger="klangk.settings"):
            # Constructing settings with a deprecated KLANGK_PROXY_PORT emits
            # a WARNING from a settings validator — proving the configured
            # root handles pre-app logging.
            from klangk.settings import KlangkSettings

            KlangkSettings(
                env={
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_PROXY_PORT": "9999",
                }
            )
        assert any(
            "KLANGK_PROXY_PORT is deprecated" in r.message
            for r in caplog.records
        )


class TestConfigure:
    """The settings-driven phase: configure(settings) re-applies the level from
    KLANGK_LOG_LEVEL, overriding the import-time defaults (#1467)."""

    def test_sets_root_level_from_settings(self, clean_root):
        logger_mod.configure(_make_settings("DEBUG"))
        assert clean_root.level == logging.DEBUG

    def test_overrides_defaults_level(self, clean_root):
        logger_mod.configure_defaults()
        assert clean_root.level == logging.INFO
        logger_mod.configure(_make_settings("WARNING"))
        assert clean_root.level == logging.WARNING

    def test_default_settings_level_is_info(self, clean_root):
        logger_mod.configure(_make_settings())
        assert clean_root.level == logging.INFO

    def test_accepts_numeric_level_string(self, clean_root):
        logger_mod.configure(_make_settings("10"))  # 10 == DEBUG
        assert clean_root.level == logging.DEBUG

    def test_handler_is_colored(self, clean_root):
        logger_mod.configure(_make_settings())
        handler = _klangk_handlers(clean_root)[0]
        assert isinstance(handler, logging.StreamHandler)
        assert "\033[94m" in handler.formatter._fmt  # _LIGHT_BLUE
        assert "\033[0m" in handler.formatter._fmt  # _RESET

    def test_third_party_loggers_silenced(self, clean_root):
        logger_mod.configure(_make_settings("DEBUG"))
        # Root is DEBUG but chatty libraries stay capped (central management,
        # one of the points of #1467).
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_configure_idempotent_no_stacking(self, clean_root):
        logger_mod.configure(_make_settings("INFO"))
        logger_mod.configure(_make_settings("DEBUG"))
        assert len(_klangk_handlers(clean_root)) == 1

    def test_reconfigure_reapplies_third_party_levels(self, clean_root):
        # Sabotage a third-party logger to prove configure resets it.
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logger_mod.configure(_make_settings("INFO"))
        logger_mod.configure(_make_settings("WARNING"))
        assert logging.getLogger("httpx").level == logging.WARNING
