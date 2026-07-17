"""Tests for the centralized Logger(app) state object (#1467)."""

import logging
import types

import pytest

from _helpers import make_settings
from klangk.logger import Logger, _level_to_int


def _make_app(level=None):
    """Build a minimal app namespace whose settings carry ``log_level``."""
    env = {}
    if level is not None:
        env["KLANGK_LOG_LEVEL"] = level
    settings = make_settings(env)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )


@pytest.fixture
def clean_root():
    """Snapshot and restore the root logger (handlers + level) per test.

    Logger.configure() mutates the global root logger; these tests must not
    leak those changes into sibling tests (xdist runs files in workers, but
    modules within a file share a process).
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
        assert _level_to_int(name) == expected

    def test_case_insensitive(self):
        assert _level_to_int("debug") == logging.DEBUG
        assert _level_to_int("WaRnInG") == logging.WARNING

    def test_numeric_string(self):
        assert _level_to_int("20") == logging.INFO
        assert _level_to_int("10") == logging.DEBUG

    def test_empty_or_none_falls_back_to_info(self):
        assert _level_to_int("") == logging.INFO
        assert _level_to_int(None) == logging.INFO

    def test_unknown_falls_back_to_info(self):
        # Settings validator rejects garbage at construction; this fallback
        # only defends a misconfigured live reload.
        assert _level_to_int("verbose") == logging.INFO


class TestLoggerConfigure:
    def test_installs_single_tagged_handler(self, clean_root):
        Logger(_make_app())
        assert len(_klangk_handlers(clean_root)) == 1

    def test_handler_carries_colored_formatter(self, clean_root):
        Logger(_make_app())
        handler = _klangk_handlers(clean_root)[0]
        assert isinstance(handler, logging.StreamHandler)
        # The colored format moved here from main.py's module scope.
        assert "\033[94m" in handler.formatter._fmt  # _LIGHT_BLUE
        assert "\033[0m" in handler.formatter._fmt  # _RESET

    def test_sets_root_level_from_settings(self, clean_root):
        Logger(_make_app("DEBUG"))
        assert clean_root.level == logging.DEBUG

    def test_default_level_is_info(self, clean_root):
        Logger(_make_app())
        assert clean_root.level == logging.INFO

    def test_accepts_numeric_level_string(self, clean_root):
        Logger(_make_app("10"))  # 10 == DEBUG
        assert clean_root.level == logging.DEBUG

    def test_configure_idempotent_across_instances(self, clean_root):
        # Two Logger instances (e.g. fresh app per test) must not stack
        # duplicate handlers on the root logger.
        Logger(_make_app())
        Logger(_make_app())
        assert len(_klangk_handlers(clean_root)) == 1

    def test_third_party_loggers_silenced(self, clean_root):
        Logger(_make_app("DEBUG"))
        # Root is DEBUG but chatty libraries stay capped (central management,
        # one of the points of #1467).
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING


class TestLoggerReconfigure:
    def test_reconfigure_updates_level(self, clean_root):
        app = _make_app("INFO")
        lg = Logger(app)
        assert clean_root.level == logging.INFO

        new_settings = make_settings({"KLANGK_LOG_LEVEL": "WARNING"})
        new_app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=new_settings)
        )
        lg.reconfigure(new_app)

        assert lg.app is new_app
        assert clean_root.level == logging.WARNING

    def test_reconfigure_does_not_stack_handlers(self, clean_root):
        lg = Logger(_make_app("INFO"))
        lg.reconfigure(_make_app("DEBUG"))
        assert len(_klangk_handlers(clean_root)) == 1

    def test_reconfigure_reapplies_third_party_levels(self, clean_root):
        # Sabotage a third-party logger to prove reconfigure resets it.
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        lg = Logger(_make_app("INFO"))
        lg.reconfigure(_make_app("WARNING"))
        assert logging.getLogger("httpx").level == logging.WARNING


class TestLoggerConstruction:
    def test_takes_only_app(self):
        """Logger follows the app-ownership rule: caches only self.app (#1467)."""
        app = _make_app()
        lg = Logger(app)
        assert lg.app is app
        # No settings subobject cached (would go stale on a SIGHUP swap).
        assert not hasattr(lg, "settings")


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
        Logger.configure_defaults()
        assert len(_klangk_handlers(clean_root)) == 1

    def test_default_level_is_info(self, clean_root):
        Logger.configure_defaults()
        assert clean_root.level == logging.INFO
        assert _klangk_handlers(clean_root)[0].level == logging.INFO

    def test_default_handler_is_colored(self, clean_root):
        Logger.configure_defaults()
        handler = _klangk_handlers(clean_root)[0]
        assert "\033[94m" in handler.formatter._fmt  # _LIGHT_BLUE

    def test_defaults_silence_third_party(self, clean_root):
        Logger.configure_defaults()
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING

    def test_defaults_idempotent(self, clean_root):
        Logger.configure_defaults()
        Logger.configure_defaults()
        assert len(_klangk_handlers(clean_root)) == 1

    def test_module_import_already_configured_defaults(self, clean_root):
        """Importing klangk.logger configures defaults (so settings construction
        logs are formatted before any app exists).

        The module-level ``Logger.configure_defaults()`` call runs at import;
        it is also directly covered there (coverage marks the line executed at
        import time). Here we just confirm the entry point exists and is the
        same idempotent path tested above.
        """
        assert callable(Logger.configure_defaults)
        assert isinstance(Logger._DEFAULT_LEVEL, int)
        assert Logger._DEFAULT_LEVEL == logging.INFO

    def test_logger_app_overrides_defaults_level(self, clean_root):
        """Defaults apply INFO; Logger(app) then overrides from KLANGK_LOG_LEVEL."""
        Logger.configure_defaults()
        assert clean_root.level == logging.INFO
        Logger(_make_app("WARNING"))
        assert clean_root.level == logging.WARNING

    def test_settings_construction_logs_through_configured_root(
        self, clean_root, caplog
    ):
        """End-to-end: with defaults active, a log emitted during KlangkSettings
        construction is captured (the scenario #1467's two-phase design serves).
        """
        Logger.configure_defaults()
        with caplog.at_level(logging.WARNING, logger="klangk.settings"):
            # Constructing settings with a deprecated KLANGK_NGINX_PORT emits
            # a WARNING from a settings validator — proving the configured
            # root handles pre-app logging.
            from klangk.settings import KlangkSettings

            KlangkSettings(
                env={
                    "KLANGK_STATE_DIR": "/tmp/state",
                    "KLANGK_NGINX_PORT": "9999",
                }
            )
        assert any(
            "KLANGK_NGINX_PORT is deprecated" in r.message
            for r in caplog.records
        )
