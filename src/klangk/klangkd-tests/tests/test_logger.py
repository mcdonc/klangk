"""Tests for centralized logging configuration (#1467).

Logging is configured by two module-level functions in :mod:`klangk.logger`
(no state object):

- ``configure_defaults()`` — applied at import; INFO level, colored format,
  third-party silencing. Active before any ``app``/settings exists.
- ``configure(settings)`` — re-applies the level from ``settings.log_level``
  and the format from ``settings.log_format`` once settings are finalized
  (build_app), and again on every SIGHUP reload.
"""

import json
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


def _make_settings(level=None, log_format=None):
    env = {}
    if level is not None:
        env["KLANGKD_LOG_LEVEL"] = level
    if log_format is not None:
        env["KLANGKD_LOG_FORMAT"] = log_format
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
        assert logger_mod.level_to_int(name) == expected

    def test_case_insensitive(self):
        assert logger_mod.level_to_int("debug") == logging.DEBUG
        assert logger_mod.level_to_int("WaRnInG") == logging.WARNING

    def test_numeric_string(self):
        assert logger_mod.level_to_int("20") == logging.INFO
        assert logger_mod.level_to_int("10") == logging.DEBUG

    def test_empty_or_none_falls_back_to_info(self):
        assert logger_mod.level_to_int("") == logging.INFO
        assert logger_mod.level_to_int(None) == logging.INFO

    def test_unknown_falls_back_to_info(self):
        # Settings validator rejects garbage at construction; this fallback
        # only defends a misconfigured live reload.
        assert logger_mod.level_to_int("verbose") == logging.INFO


class TestFormatIsJson:
    """The private format-string discriminator (#3156)."""

    def test_json_in_any_case(self):
        assert logger_mod.format_is_json("json")
        assert logger_mod.format_is_json("JSON")
        assert logger_mod.format_is_json(" Json ")

    def test_text_or_garbage_is_not_json(self):
        assert not logger_mod.format_is_json("text")
        assert not logger_mod.format_is_json("syslog")
        assert not logger_mod.format_is_json("")
        assert not logger_mod.format_is_json(None)


class TestJsonFormatter:
    """The one-object-per-line JSON formatter for SIEM ingestion (#3156)."""

    def _format_record(self, record):
        return json.loads(logger_mod.JsonFormatter().format(record))

    def test_payload_fields(self):
        record = logging.LogRecord(
            "klangk.test",
            logging.WARNING,
            __file__,
            1,
            "watch %s",
            ("out",),
            None,
        )
        payload = self._format_record(record)
        assert set(payload) == {"timestamp", "level", "logger", "message"}
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "klangk.test"
        assert payload["message"] == "watch out"

    def test_timestamp_is_iso8601_utc(self):
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, "m", (), None
        )
        ts = self._format_record(record)["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_exc_info_included_when_present(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "klangk.test",
                logging.ERROR,
                __file__,
                1,
                "failed",
                (),
                sys.exc_info(),
            )
        payload = self._format_record(record)
        assert "RuntimeError: boom" in payload["exc_info"]

    def test_no_exc_info_key_without_exception(self):
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, "m", (), None
        )
        assert "exc_info" not in self._format_record(record)

    def test_none_exc_info_tuple_is_not_an_exception(self):
        # ``exc_info=True`` outside an except block yields (None, None, None);
        # that must not emit an ``exc_info`` field.
        record = logging.LogRecord(
            "klangk.test",
            logging.INFO,
            __file__,
            1,
            "m",
            (),
            (None, None, None),
        )
        assert "exc_info" not in self._format_record(record)

    def test_bad_percent_format_still_emits_valid_json(self):
        # ``getMessage()`` raises on mismatched %-args; the formatter must
        # degrade to the raw msg so every line stays a parseable object
        # (the SIEM contract, #3156) instead of a handleError traceback line.
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, "n=%d", ("x",), None
        )
        payload = self._format_record(record)
        assert payload["message"] == "n=%d"

    def test_newlines_in_message_do_not_break_one_line_contract(self):
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, "a\nb", (), None
        )
        line = logger_mod.JsonFormatter().format(record)
        assert "\n" not in line
        assert json.loads(line)["message"] == "a\nb"

    def test_safe_exception_fallback_when_formatexception_raises(self):
        # formatException() itself never raises on a real exc_info; sabotage
        # it to prove safe_exception still returns a string (repr of the
        # exception value) instead of propagating.
        try:
            raise ValueError("nope")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "klangk.test",
                logging.ERROR,
                __file__,
                1,
                "failed",
                (),
                sys.exc_info(),
            )
            fmt = logger_mod.JsonFormatter()
            fmt.formatException = lambda exc_info: 1 / 0
            assert fmt.safe_exception(record) == repr(record.exc_info[1])


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
        assert logger_mod.DEFAULT_LEVEL == logging.INFO

    def test_settings_construction_logs_through_configured_root(
        self, clean_root, caplog
    ):
        """End-to-end: with defaults active, a log emitted during KlangkSettings
        construction is captured (the scenario #1467's two-phase design serves).
        """
        logger_mod.configure_defaults()
        with caplog.at_level(logging.WARNING, logger="klangk.settings"):
            # Constructing settings with a deprecated KLANGKD_PROXY_PORT emits
            # a WARNING from a settings validator — proving the configured
            # root handles pre-app logging.
            from klangk.settings import KlangkSettings

            KlangkSettings(
                env={
                    "KLANGKD_STATE_DIR": "/tmp/state",
                    "KLANGKD_PROXY_PORT": "9999",
                }
            )
        assert any(
            "KLANGKD_PROXY_PORT is deprecated" in r.message
            for r in caplog.records
        )


class TestConfigure:
    """The settings-driven phase: configure(settings) re-applies the level from
    KLANGKD_LOG_LEVEL, overriding the import-time defaults (#1467)."""

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

    def test_litellm_loggers_do_not_propagate(self, clean_root):
        """LiteLLM loggers have propagate=False so their records don't
        reach klangk's root handler (double-logging, #2087)."""
        logger_mod.configure(_make_settings())
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            assert logging.getLogger(name).propagate is False

    def test_reconfigure_reapplies_third_party_levels(self, clean_root):
        # Sabotage a third-party logger to prove configure resets it.
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logger_mod.configure(_make_settings("INFO"))
        logger_mod.configure(_make_settings("WARNING"))
        assert logging.getLogger("httpx").level == logging.WARNING


class TestConfigureFormat:
    """The settings-driven format switch: KLANGKD_LOG_FORMAT selects the
    colored console format or JSON output (#3156), reloadable like the
    level (SIGHUP goes through the same configure(settings) seam)."""

    def test_json_settings_install_json_formatter(self, clean_root):
        logger_mod.configure(_make_settings(log_format="json"))
        handler = _klangk_handlers(clean_root)[0]
        assert isinstance(handler.formatter, logger_mod.JsonFormatter)

    def test_text_settings_install_colored_formatter(self, clean_root):
        logger_mod.configure(_make_settings(log_format="text"))
        handler = _klangk_handlers(clean_root)[0]
        assert "\033[94m" in handler.formatter._fmt  # _LIGHT_BLUE

    def test_json_output_is_one_object_per_line_no_ansi(
        self, clean_root, capsys
    ):
        """End-to-end: a record from a named (propagating) logger comes out
        of stderr as a single parseable JSON line, free of ANSI codes."""
        logger_mod.configure(_make_settings(log_format="json"))
        logging.getLogger("klangk.sink.test").warning("siem %s", "ready")
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "klangk.sink.test"
        assert payload["message"] == "siem ready"
        assert "\033" not in line

    def test_switch_is_idempotent_no_stacking(self, clean_root):
        # text → json → text, like a SIGHUP format flip: one handler, with
        # the formatter of the last-applied settings.
        logger_mod.configure(_make_settings(log_format="text"))
        logger_mod.configure(_make_settings(log_format="json"))
        logger_mod.configure(_make_settings(log_format="text"))
        handlers = _klangk_handlers(clean_root)
        assert len(handlers) == 1
        assert "\033[94m" in handlers[0].formatter._fmt

    def test_defaults_stay_text(self, clean_root):
        # The pre-settings phase has no settings to read; text is the
        # documented conservative default (see DEFAULT_FORMAT).
        logger_mod.configure_defaults()
        handler = _klangk_handlers(clean_root)[0]
        assert "\033[94m" in handler.formatter._fmt
