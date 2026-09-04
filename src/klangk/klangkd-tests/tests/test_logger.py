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
import logging.handlers

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
        if getattr(h, "_klangk_log_handler", False) or getattr(
            h, "_klangk_log_file_handler", False
        ):
            root.removeHandler(h)
            h.close()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _klangk_handlers(root):
    return [
        h for h in root.handlers if getattr(h, "_klangk_log_handler", False)
    ]


def _klangk_file_handlers(root):
    return [
        h
        for h in root.handlers
        if getattr(h, "_klangk_log_file_handler", False)
    ]


def _make_settings(level=None, log_format=None, log_file=None):
    env = {}
    if level is not None:
        env["KLANGKD_LOG_LEVEL"] = level
    if log_format is not None:
        env["KLANGKD_LOG_FORMAT"] = log_format
    if log_file is not None:
        env["KLANGKD_LOG_FILE"] = log_file
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

    def test_str_raising_msg_falls_back_to_repr(self):
        # ``getMessage()``'s first step is ``str(msg)``; a msg whose
        # ``__str__`` raises defeats both it and the ``str(msg)`` fallback,
        # so the nested ``repr(msg)`` arm is what keeps ``format()`` from
        # ever raising (fresh-eyes review finding on #3156).
        class Hostile:
            def __str__(self):
                raise RuntimeError("no str for you")

        obj = Hostile()
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, obj, (), None
        )
        payload = self._format_record(record)
        assert payload["message"] == repr(obj)

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
        # uvicorn.access stays INFO (SIEM keys on per-request records,
        # #3156); the chatty libs stay capped.
        assert logging.getLogger("uvicorn.access").level == logging.INFO
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
        # one of the points of #1467) — except uvicorn.access, kept at INFO
        # so a SIEM can key on per-request records (#3156).
        assert logging.getLogger("uvicorn.access").level == logging.INFO
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


class TestLogFileSink:
    """KLANGKD_LOG_FILE — an always-JSON file sink alongside the console
    handler, so stdout can stay text while the file feeds a SIEM (#3156)."""

    def test_file_handler_installed_with_json_formatter(
        self, clean_root, tmp_path
    ):
        target = tmp_path / "k.jsonl"
        logger_mod.configure(_make_settings(log_file=str(target)))
        (handler,) = _klangk_file_handlers(clean_root)
        assert isinstance(handler.formatter, logger_mod.JsonFormatter)
        assert len(_klangk_handlers(clean_root)) == 1  # console stays

    def test_console_text_while_file_is_json(
        self, clean_root, tmp_path, capsys
    ):
        """The operator's ask: stdout human-readable, file machine-parseable."""
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(log_format="text", log_file=str(target))
        )
        logging.getLogger("klangk.sink.file").warning("dual %s", "stream")
        console = capsys.readouterr().err
        assert "\033[94m" in console  # colored text on stderr
        (line,) = target.read_text().splitlines()
        payload = json.loads(line)
        assert payload["message"] == "dual stream"
        assert payload["level"] == "WARNING"

    def test_console_json_and_file_json_too(
        self, clean_root, tmp_path, capsys
    ):
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(log_format="json", log_file=str(target))
        )
        logging.getLogger("klangk.sink.file2").info("both json")
        json.loads(capsys.readouterr().err.strip())  # console: valid JSON
        (line,) = target.read_text().splitlines()
        assert json.loads(line)["message"] == "both json"

    def test_unset_log_file_installs_no_file_handler(self, clean_root):
        logger_mod.configure(_make_settings())
        assert _klangk_file_handlers(clean_root) == []

    def test_path_flip_closes_old_sink_opens_new(
        self, clean_root, tmp_path, capsys
    ):
        """SIGHUP changing KLANGKD_LOG_FILE: old file keeps its records, new
        file gets the later ones, exactly one file handler stays attached,
        and the old sink's stream is actually closed (no fd leak across
        reloads)."""
        old = tmp_path / "old.jsonl"
        new = tmp_path / "new.jsonl"
        logger_mod.configure(_make_settings(log_file=str(old)))
        logging.getLogger("klangk.sink.flip").info("to old")
        (old_handler,) = _klangk_file_handlers(clean_root)
        # FileHandler.close() flushes, then nulls self.stream before closing
        # it — hold the stream object itself to assert the fd was released.
        old_stream = old_handler.stream
        logger_mod.configure(_make_settings(log_file=str(new)))
        logging.getLogger("klangk.sink.flip").info("to new")
        assert len(_klangk_file_handlers(clean_root)) == 1
        assert old_stream.closed
        assert old_handler not in clean_root.handlers
        assert json.loads(old.read_text())["message"] == "to old"
        assert json.loads(new.read_text())["message"] == "to new"

    def test_external_rotation_reopens_new_file(self, clean_root, tmp_path):
        """WatchedFileHandler semantics: a rename-style rotation changes the
        inode, and the sink follows the new file at the configured path
        without a SIGHUP (the reason for not using a plain FileHandler)."""
        target = tmp_path / "k.jsonl"
        rotated = tmp_path / "k.jsonl.1"
        logger_mod.configure(_make_settings(log_file=str(target)))
        logging.getLogger("klangk.sink.rotate").info("before")
        target.replace(rotated)  # external rotation: rename
        logging.getLogger("klangk.sink.rotate").info("after")
        assert "before" in rotated.read_text()
        assert "after" in target.read_text()  # sink recreated + followed it

    def test_reopen_failure_suspends_sink_not_the_call_site(
        self, clean_root, tmp_path, caplog
    ):
        """Hostile rotation — the path becomes a directory: the log call
        must not raise (``WatchedFileHandler.emit`` runs the reopen outside
        the stdlib try/except), exactly one warning is emitted, later
        records are dropped from the file while the console stays live, and
        a reconfigure to the same path (a SIGHUP) heals the sink."""
        target = tmp_path / "k.jsonl"
        settings = _make_settings(log_file=str(target))
        logger_mod.configure(settings)
        logging.getLogger("klangk.sink.hostile").info("good")
        target.unlink()
        target.mkdir()  # stat succeeds, inode differs -> reopen -> OSError
        with caplog.at_level(logging.WARNING, logger="klangk.logger"):
            logging.getLogger("klangk.sink.hostile").info("hostile")
            logging.getLogger("klangk.sink.hostile").info("still hostile")
        (handler,) = _klangk_file_handlers(clean_root)
        assert handler._sink_broken is True
        assert (
            sum(
                "file logging suspended" in r.getMessage()
                for r in caplog.records
            )
            == 1  # one-shot: the re-entrant warning didn't recurse
        )
        # Heal: clear the hostile path and reload settings (SIGHUP path).
        target.rmdir()
        logger_mod.configure(settings)
        logging.getLogger("klangk.sink.hostile").info("healed")
        assert "healed" in target.read_text()

    def test_uvicorn_records_share_the_configured_format(
        self, clean_root, tmp_path, capsys
    ):
        """serve() starts uvicorn with log_config=None (main.py) so uvicorn's
        loggers carry no handlers of their own and propagate to the root
        handler — startup/access records must come out JSON like everything
        else (the gap this follow-up closes)."""
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(log_format="json", log_file=str(target))
        )
        logging.getLogger("uvicorn.error").info("Started server process [1]")
        logging.getLogger("uvicorn.access").info(
            "GET /api/v1/config 200"  # simplified access record
        )
        err = capsys.readouterr().err
        lines = [json.loads(x) for x in err.strip().splitlines()]
        assert {p["logger"] for p in lines} == {
            "uvicorn.error",
            "uvicorn.access",
        }
        file_lines = [json.loads(x) for x in target.read_text().splitlines()]
        assert {p["logger"] for p in file_lines} == {
            "uvicorn.error",
            "uvicorn.access",
        }

    def test_broken_path_at_reconfigure_warns_not_raises(
        self, clean_root, tmp_path, caplog, monkeypatch
    ):
        """A path that validated at construction but whose open fails at a
        SIGHUP reconfigure: warn and keep the console stream live instead of
        tearing the reload down (construction-time validation handles the
        fail-fast case). Unlinking the file isn't enough — an append-open
        recreates it — so the open itself is sabotaged."""
        target = tmp_path / "gone.jsonl"
        settings = _make_settings(log_file=str(target))

        def boom(*args, **kwargs):
            raise OSError("gone")

        monkeypatch.setattr(logger_mod, "RotationSafeFileHandler", boom)
        with caplog.at_level(logging.WARNING, logger="klangk.logger"):
            logger_mod.configure(settings)
        assert _klangk_file_handlers(clean_root) == []
        assert len(_klangk_handlers(clean_root)) == 1  # console still up
        assert any("KLANGKD_LOG_FILE" in r.message for r in caplog.records)

    def test_recursion_error_still_propagates(self, tmp_path, monkeypatch):
        """RecursionError is the one exception the stdlib emit contract
        re-raises; the suspension arm must not swallow it (silencing it
        would just move the loop) and must not trip the broken flag."""
        target = tmp_path / "k.jsonl"
        handler = logger_mod.RotationSafeFileHandler(target, encoding="utf-8")
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, "m", (), None
        )

        def raise_recursion(self, record):
            raise RecursionError("too deep")

        monkeypatch.setattr(
            logging.handlers.WatchedFileHandler, "emit", raise_recursion
        )
        try:
            with pytest.raises(RecursionError):
                handler.emit(record)
        finally:
            handler.close()
        assert handler._sink_broken is False


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
