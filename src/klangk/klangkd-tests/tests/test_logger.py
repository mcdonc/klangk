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
import socket

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


def _make_settings(
    level=None,
    log_format=None,
    log_file=None,
    max_bytes=None,
    rotate=None,
    backup_count=None,
    data_dir=None,
):
    env = {}
    if level is not None:
        env["KLANGKD_LOG_LEVEL"] = level
    if log_format is not None:
        env["KLANGKD_LOG_FORMAT"] = log_format
    if log_file is not None:
        env["KLANGKD_LOG_FILE"] = log_file
    if max_bytes is not None:
        env["KLANGKD_LOG_FILE_MAX_BYTES"] = max_bytes
    if rotate is not None:
        env["KLANGKD_LOG_FILE_ROTATE"] = rotate
    if backup_count is not None:
        env["KLANGKD_LOG_FILE_BACKUP_COUNT"] = backup_count
    if data_dir is not None:
        env["KLANGKD_DATA_DIR"] = str(data_dir)
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
        # No instance id handed in -> host rides, instance omitted (#3330).
        assert set(payload) == {
            "timestamp",
            "level",
            "logger",
            "message",
            "host",
        }
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "klangk.test"
        assert payload["message"] == "watch out"
        assert payload["host"] == socket.gethostname()

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


class TestHostInstanceFields:
    """``host`` + ``instance`` on JSON records (#3330).

    A SIEM aggregating lines from several klangkd deployments needs to
    attribute each record: ``host`` is the emitting machine's hostname,
    ``instance`` the per-data-dir instance id — the same file-backed
    identity ``Util.resolve_instance_id`` owns, so log stream and audit
    trail (app.start/app.stop ``target_id``, #3329) correlate.
    """

    def _record(self, msg="m"):
        return logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, msg, (), None
        )

    def test_host_on_every_json_record(self):
        payload = json.loads(logger_mod.JsonFormatter().format(self._record()))
        assert payload["host"] == socket.gethostname()

    def test_instance_omitted_without_id(self):
        # Pre-settings formatters (and a data dir that cannot be resolved)
        # degrade: no id, so the field is absent — the line stays parseable.
        assert "instance" not in json.loads(
            logger_mod.JsonFormatter().format(self._record())
        )

    def test_instance_rides_the_formatter(self):
        payload = json.loads(
            logger_mod.JsonFormatter("iid-1").format(self._record())
        )
        assert payload["instance"] == "iid-1"

    def test_hostname_resolution_degrades_on_oserror(self, monkeypatch):
        def boom():
            raise OSError("no hostname")

        monkeypatch.setattr(logger_mod.socket, "gethostname", boom)
        assert logger_mod.resolve_hostname() == ""

    def test_resolve_instance_reads_existing_file(self, tmp_path):
        (tmp_path / "instance-id").write_text("  abc  \n")
        assert logger_mod.resolve_instance_id(str(tmp_path)) == "abc"

    def test_resolve_instance_creates_when_absent(self, tmp_path):
        data = tmp_path / "fresh"
        resolved = logger_mod.resolve_instance_id(str(data))
        assert (data / "instance-id").read_text() == resolved
        assert resolved  # a generated uuid, not empty

    def test_resolve_instance_degrades_on_unwritable_dir(self, tmp_path):
        # A data dir whose parent is a regular file: the mkdir fails and
        # the resolver degrades to "" instead of raising.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        assert logger_mod.resolve_instance_id(str(blocker / "d")) == ""

    def test_resolve_instance_degrades_on_nul_in_data_dir(self):
        # An embedded NUL raises ValueError (not OSError) from the path
        # calls — the catch set must cover it (fresh-eyes review #1).
        assert logger_mod.resolve_instance_id("bad\0dir") == ""

    def test_corrupt_instance_file_regenerates_not_raises(self, tmp_path):
        # A non-UTF-8 identity file raises UnicodeDecodeError (a ValueError)
        # from read_text — the reader degrades to None and the resolver
        # regenerates the file instead of raising into configure
        # (fresh-eyes review #1; Util's own resolver would raise here).
        path = tmp_path / "instance-id"
        path.write_bytes(b"\xff\xfe\x00bad")
        resolved = logger_mod.resolve_instance_id(str(tmp_path))
        assert resolved  # a fresh uuid
        assert path.read_text().strip() == resolved  # rewritten, valid utf-8

    def test_blank_instance_file_is_regenerated(self, tmp_path):
        # Whitespace-only file: treated as missing, regenerated — the same
        # semantics Util.resolve_instance_id promises for its file.
        (tmp_path / "instance-id").write_text("   \n")
        resolved = logger_mod.resolve_instance_id(str(tmp_path))
        assert resolved
        assert (tmp_path / "instance-id").read_text().strip() == resolved

    def test_read_instance_file_oserror_arm(self, tmp_path):
        # A directory at the instance-id path: the read fails -> None.
        d = tmp_path / "instance-id"
        d.mkdir()
        assert logger_mod.read_instance_file(d) is None

    def test_write_instance_file_oserror_arm(self, tmp_path, monkeypatch):
        # The atomic replace fails: "" and no file left at the target path.
        def boom(src, dst):
            raise OSError("denied")

        monkeypatch.setattr(logger_mod.os, "replace", boom)
        assert logger_mod.write_instance_file(tmp_path / "instance-id") == ""
        assert not (tmp_path / "instance-id").exists()

    def test_configure_stamps_console_json_records(
        self, clean_root, tmp_path, capsys
    ):
        data = tmp_path / "d1"
        data.mkdir()
        (data / "instance-id").write_text("iid-console")
        logger_mod.configure(_make_settings(log_format="json", data_dir=data))
        logging.getLogger("klangk.id.console").info("m")
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["instance"] == "iid-console"
        assert payload["host"] == socket.gethostname()

    def test_configure_stamps_file_sink_records(self, clean_root, tmp_path):
        data = tmp_path / "d2"
        data.mkdir()
        (data / "instance-id").write_text("iid-file")
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(
                log_format="text", log_file=str(target), data_dir=data
            )
        )
        logging.getLogger("klangk.id.file").info("m")
        payload = json.loads(target.read_text().strip())
        assert payload["instance"] == "iid-file"
        assert payload["host"] == socket.gethostname()

    def test_first_boot_creates_instance_file_and_stamps_it(
        self, clean_root, tmp_path, capsys
    ):
        # build_app runs configure(settings) before the lifespan's
        # Util.resolve_instance_id(): the logger creates the file, Util
        # then reads the same value back — agreement from the first line.
        data = tmp_path / "fresh" / "data"
        logger_mod.configure(_make_settings(log_format="json", data_dir=data))
        logging.getLogger("klangk.id.first").info("m")
        payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        on_disk = (data / "instance-id").read_text().strip()
        assert on_disk  # generated uuid, not empty
        assert payload["instance"] == on_disk

    def test_instance_field_matches_util_resolution(
        self, clean_root, tmp_path, capsys
    ):
        # Criterion #3330: the field equals the id Util resolves — the
        # audit-event target_id of app lifecycle rows (#3329).
        import types

        from klangk.util import Util

        data = tmp_path / "d3"
        settings = _make_settings(log_format="json", data_dir=data)
        logger_mod.configure(settings)
        logging.getLogger("klangk.id.util").info("m")
        payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        util = Util(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=settings)
            )
        )
        assert util.resolve_instance_id() == payload["instance"]

    def test_sighup_carries_the_process_live_instance_across_a_data_dir_flip(
        self, clean_root, tmp_path, capsys
    ):
        # data_dir is a non-reloadable setting: a SIGHUP that changes it
        # draws a requires-restart warning and leaves the DB, pidfile, and
        # container labels on the old dir. The reload path therefore passes
        # the process's live Util id into configure (lifecycle), so records
        # keep the identity everything else in the process uses — not the
        # refused config's phantom id (fresh-eyes review #2).
        old_data = tmp_path / "old"
        new_data = tmp_path / "new"
        old_data.mkdir()
        new_data.mkdir()
        (old_data / "instance-id").write_text("live-iid")
        (new_data / "instance-id").write_text("phantom-iid")
        logger_mod.configure(
            _make_settings(log_format="json", data_dir=old_data)
        )
        logging.getLogger("klangk.id.swap").info("before")
        # The reload seam: new settings naming a different data dir, with
        # the live instance id handed in (as apply_reloaded_settings does).
        reloaded = _make_settings(log_format="json", data_dir=new_data)
        logger_mod.configure(reloaded, "live-iid")
        logging.getLogger("klangk.id.swap").info("after")
        lines = [
            json.loads(x) for x in capsys.readouterr().err.strip().splitlines()
        ]
        assert [p["instance"] for p in lines] == ["live-iid", "live-iid"]

    def test_data_dir_change_applies_after_a_process_restart(
        self, clean_root, tmp_path, capsys
    ):
        # A data-dir change takes effect the honest way: the next process
        # start's build_app resolves from the (then effective) data dir.
        old_data = tmp_path / "old"
        new_data = tmp_path / "new"
        for d in (old_data, new_data):
            d.mkdir()
        (old_data / "instance-id").write_text("old-iid")
        (new_data / "instance-id").write_text("new-iid")
        logger_mod.configure(
            _make_settings(log_format="json", data_dir=old_data)
        )
        logging.getLogger("klangk.id.restart").info("old process")
        # Fresh process: plain configure(settings) — no id handed in.
        logger_mod.configure(
            _make_settings(log_format="json", data_dir=new_data)
        )
        logging.getLogger("klangk.id.restart").info("new process")
        lines = [
            json.loads(x) for x in capsys.readouterr().err.strip().splitlines()
        ]
        assert [p["instance"] for p in lines] == ["old-iid", "new-iid"]


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


class TestLogRotation:
    """In-app rotation of the KLANGKD_LOG_FILE sink (#3156): size
    (KLANGKD_LOG_FILE_MAX_BYTES) and time (KLANGKD_LOG_FILE_ROTATE)
    triggers, retention via KLANGKD_LOG_FILE_BACKUP_COUNT."""

    def test_no_triggers_never_rotates(self, clean_root, tmp_path):
        """The default (no triggers) keeps today's external-rotation-only
        posture: however many records, no <path>.N ever appears."""
        target = tmp_path / "k.jsonl"
        logger_mod.configure(_make_settings(log_file=str(target)))
        for i in range(20):
            logging.getLogger("klangk.rot").info("record %d", i)
        assert not (tmp_path / "k.jsonl.1").exists()
        assert len(target.read_text().splitlines()) == 20

    def test_size_trigger_rotates_and_keeps_backups(
        self, clean_root, tmp_path
    ):
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(
                log_file=str(target), max_bytes="200", backup_count="2"
            )
        )
        for i in range(10):
            logging.getLogger("klangk.rot").info("sized record %d", i)
        assert (tmp_path / "k.jsonl.1").exists()
        assert (tmp_path / "k.jsonl.2").exists()
        assert not (tmp_path / "k.jsonl.3").exists()  # retention honored
        # Every rotated file holds only whole JSON lines, newest in .1.
        for name in ("k.jsonl", "k.jsonl.1", "k.jsonl.2"):
            for line in (tmp_path / name).read_text().splitlines():
                json.loads(line)

    def test_size_trigger_zero_backups_discards(self, clean_root, tmp_path):
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(
                log_file=str(target), max_bytes="100", backup_count="0"
            )
        )
        for i in range(10):
            logging.getLogger("klangk.rot").info("record %d", i)
        assert not (tmp_path / "k.jsonl.1").exists()
        # base holds only the records since the last rollover
        lines = [json.loads(x) for x in target.read_text().splitlines()]
        assert len(lines) < 10
        assert lines[-1]["message"] == "record 9"

    def test_boundary_math_utc(self):
        """next_rotate_boundary: first UTC boundary strictly after ts —
        hourly/daily/weekly (Monday)/monthly, from 2026-01-01 13:45:30Z
        (a Thursday)."""
        from datetime import UTC as UTC_TZ, datetime as dt_cls

        ts = dt_cls(2026, 1, 1, 13, 45, 30, tzinfo=UTC_TZ).timestamp()

        def at(y, m, d, h=0):
            return dt_cls(y, m, d, h, tzinfo=UTC_TZ).timestamp()

        assert logger_mod.next_rotate_boundary(ts, "hourly") == at(
            2026, 1, 1, 14
        )
        assert logger_mod.next_rotate_boundary(ts, "daily") == at(2026, 1, 2)
        assert logger_mod.next_rotate_boundary(ts, "weekly") == at(2026, 1, 5)
        assert logger_mod.next_rotate_boundary(ts, "monthly") == at(2026, 2, 1)

    def test_time_trigger_rotates_on_passed_boundary(
        self, clean_root, tmp_path
    ):
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(log_file=str(target), rotate="hourly")
        )
        logging.getLogger("klangk.rot").info("before boundary")
        # Force the boundary into the past (a full hour need not elapse).
        (handler,) = _klangk_file_handlers(clean_root)
        handler.next_rollover_ts = 0.0
        logging.getLogger("klangk.rot").info("after boundary")
        assert json.loads((tmp_path / "k.jsonl.1").read_text())["message"] == (
            "before boundary"
        )
        lines = [json.loads(x) for x in target.read_text().splitlines()]
        assert [p["message"] for p in lines] == ["after boundary"]

    def test_both_triggers_either_fires(self, clean_root, tmp_path):
        """Size + time both armed: the size arm rotates even though the
        time boundary is an hour away."""
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(
                log_file=str(target), max_bytes="150", rotate="daily"
            )
        )
        for i in range(8):
            logging.getLogger("klangk.rot").info("both %d", i)
        assert (tmp_path / "k.jsonl.1").exists()

    def test_rollover_failure_suspends_not_crashes(
        self, clean_root, tmp_path, caplog, monkeypatch
    ):
        """A failing rename during rollover takes the suspend path: the log
        call never raises, one warning, later records skipped from the file,
        console stays live — same posture as a failed reopen."""
        target = tmp_path / "k.jsonl"
        logger_mod.configure(
            _make_settings(log_file=str(target), max_bytes="80")
        )
        logging.getLogger("klangk.rot").info("first")

        def boom(*args, **kwargs):
            raise OSError("rename denied")

        monkeypatch.setattr(logger_mod.os, "replace", boom)
        with caplog.at_level(logging.WARNING, logger="klangk.logger"):
            logging.getLogger("klangk.rot").info("triggers rollover")
            logging.getLogger("klangk.rot").info("after failure")
        (handler,) = _klangk_file_handlers(clean_root)
        assert handler._sink_broken is True
        assert (
            sum(
                "file logging suspended" in r.getMessage()
                for r in caplog.records
            )
            == 1
        )

    def test_rollover_direct_arms(self, tmp_path):
        """Direct handler calls cover arms emit can't reach: a rollover
        with no open stream, a zero-backup rollover with the base file
        already gone, and the size check against no stream (size 0)."""
        one = tmp_path / "one.jsonl"
        h = logger_mod.RotationSafeFileHandler(
            one, encoding="utf-8", backup_count=0
        )
        h.stream.close()
        h.stream = None
        h.do_rollover()  # no stream; existing base removed; reopened
        assert one.exists()
        h.close()

        two = tmp_path / "two.jsonl"
        h2 = logger_mod.RotationSafeFileHandler(
            two, encoding="utf-8", backup_count=0
        )
        h2.stream.close()
        h2.stream = None
        two.unlink()
        h2.do_rollover()  # no stream AND no base: neither arm fires
        assert two.exists()
        h2.close()

        three = tmp_path / "three.jsonl"
        h3 = logger_mod.RotationSafeFileHandler(
            three, encoding="utf-8", max_bytes=10
        )
        h3.stream.close()
        h3.stream = None
        record = logging.LogRecord(
            "klangk.test", logging.INFO, __file__, 1, "m", (), None
        )
        assert h3.should_rollover(record) is False  # no stream -> size 0
        h3.close()


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
