"""Built-in audit-record forwarding to a syslog/SIEM target (#3252).

Covers the target settings (defaults, valid values, the fail-loud
startup aborts), the syslog target parser + RFC 5424 line format, the
``audit_forward_state`` watermark model (read / advance / upsert /
readers for all three sources), and the
:class:`~klangk.audit_forward.AuditForwarder` sweep semantics:
in-order at-least-once delivery, resume-after-restart, the bounded
batch, failure backoff + cooldown, both target families (a real local
TCP receiver for syslog), the ``/audit`` status surface, and the
reconfigure (SIGHUP) reset.
"""

import asyncio
import json
import ssl
import time
import types
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

import test_api
from httpx import ASGITransport, AsyncClient

from _helpers import make_settings
from klangk.audit_forward import (
    FORWARD_INTERVAL_SECONDS,
    SYSLOG_HOSTNAME,
    AuditForwarder,
    close_writer_quietly,
    escape_sd_value,
    format_syslog_line,
    parse_syslog_target,
    record_timestamp,
    rfc3339_utc,
    sanitize_hostname,
    ssl_context_for,
    validate_forward_url,
)
from klangk.model.audit_forward import SOURCE_AUDIT_EVENTS, AuditForwardModel
from klangk.settings import KlangkSettings

# test_api's app fixture, re-bound under a module-local name (the
# test_audit_events.py pattern) for the /audit route-level tests.
api_app = test_api.app


@pytest.fixture
async def api_client(api_app):
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_audit_events(app, count, first=1):
    """Insert *count* audit_events rows (ids first..first+count-1)."""
    for i in range(first, first + count):
        await app.state.model.audit_events.record(
            "user.create",
            actor_id=f"u{i}",
            detail={"n": i} if i % 2 else None,
        )


async def _seed_consent_row(app):
    """One egress_consent row (needs its FK'd user + workspace); each
    call mints a distinct user so repeated seeds coexist."""
    import uuid

    model = app.state.model
    user = await model.users.create_user(
        f"forward-{uuid.uuid4().hex[:8]}@example.com", None
    )
    workspace = await model.workspaces.create_workspace(user["id"], "fwd")
    request = await model.egress_consent.create_request(
        workspace["id"], "example.com", 443
    )
    assert request is not None
    return request


def _forwarder(app_state, env):
    """A forwarder over the per-test app state with *env* applied."""
    app_state.state.settings = make_settings(env)
    return AuditForwarder(app_state)


class TestSettings:
    def test_defaults(self):
        settings = make_settings({})
        assert settings.audit_forward_url is None
        assert settings.audit_forward_syslog is None

    def test_url_set(self):
        settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem.corp/ingest"}
        )
        assert settings.audit_forward_url == "https://siem.corp/ingest"

    def test_url_blank_is_off(self):
        settings = make_settings({"KLANGKD_AUDIT_FORWARD_URL": "  "})
        assert settings.audit_forward_url is None

    def test_url_bad_scheme_aborts_startup(self):
        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUDIT_FORWARD_URL": "ftp://siem.corp"})

    def test_url_no_host_aborts_startup(self):
        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUDIT_FORWARD_URL": "https://"})

    def test_url_non_string_aborts_startup(self, tmp_path):
        """A YAML non-string value reaches the validator raw."""
        config = tmp_path / "k.yaml"
        config.write_text(
            "state_dir: " + str(tmp_path / "state") + "\n"
            "data_dir: " + str(tmp_path / "data") + "\n"
            "audit_forward_url: 123\n"
        )
        with pytest.raises(ValidationError):
            KlangkSettings(
                env={"KLANGKD_DATA_DIR": str(tmp_path / "data")},
                config_file=str(config),
            )

    def test_syslog_set(self):
        settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_SYSLOG": "tls://siem.corp:6514"}
        )
        assert settings.audit_forward_syslog == "tls://siem.corp:6514"

    def test_syslog_blank_is_off(self):
        settings = make_settings({"KLANGKD_AUDIT_FORWARD_SYSLOG": ""})
        assert settings.audit_forward_syslog is None

    def test_syslog_bad_scheme_aborts_startup(self):
        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUDIT_FORWARD_SYSLOG": "udp://siem:514"})

    def test_syslog_no_host_aborts_startup(self):
        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUDIT_FORWARD_SYSLOG": "tcp://"})

    def test_syslog_non_string_aborts_startup(self, tmp_path):
        config = tmp_path / "k.yaml"
        config.write_text(
            "state_dir: " + str(tmp_path / "state") + "\n"
            "data_dir: " + str(tmp_path / "data") + "\n"
            "audit_forward_syslog: 7\n"
        )
        with pytest.raises(ValidationError):
            KlangkSettings(
                env={"KLANGKD_DATA_DIR": str(tmp_path / "data")},
                config_file=str(config),
            )

    def test_header_set(self):
        settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_HEADER": "Authorization: Bearer t0k"}
        )
        assert settings.audit_forward_header == "Authorization: Bearer t0k"

    def test_header_blank_is_off(self):
        settings = make_settings({"KLANGKD_AUDIT_FORWARD_HEADER": " "})
        assert settings.audit_forward_header is None

    def test_header_without_value_aborts_startup(self):
        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUDIT_FORWARD_HEADER": "Authorization"})

    def test_header_bad_name_aborts_startup(self):
        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_AUDIT_FORWARD_HEADER": "Bad Name: value"})

    def test_header_non_string_aborts_startup(self, tmp_path):
        config = tmp_path / "k.yaml"
        config.write_text(
            "state_dir: " + str(tmp_path / "state") + "\n"
            "data_dir: " + str(tmp_path / "data") + "\n"
            "audit_forward_header: 5\n"
        )
        with pytest.raises(ValidationError):
            KlangkSettings(
                env={"KLANGKD_DATA_DIR": str(tmp_path / "data")},
                config_file=str(config),
            )

    def test_both_targets_may_be_configured(self):
        settings = make_settings(
            {
                "KLANGKD_AUDIT_FORWARD_URL": "https://siem.corp/ingest",
                "KLANGKD_AUDIT_FORWARD_SYSLOG": "tcp://siem.corp",
            }
        )
        assert settings.audit_forward_url is not None
        assert settings.audit_forward_syslog is not None


class TestTargetParsing:
    def test_bare_host_defaults_to_tcp_514(self):
        assert parse_syslog_target("siem.corp") == ("tcp", "siem.corp", 514)

    def test_explicit_port(self):
        assert parse_syslog_target("tcp://siem.corp:1514") == (
            "tcp",
            "siem.corp",
            1514,
        )

    def test_tls_defaults_to_6514(self):
        assert parse_syslog_target("tls://siem.corp") == (
            "tls",
            "siem.corp",
            6514,
        )

    def test_scheme_case_insensitive(self):
        assert parse_syslog_target("TLS://siem.corp")[0] == "tls"

    def test_bad_scheme_raises(self):
        with pytest.raises(ValueError, match="tcp"):
            parse_syslog_target("udp://siem.corp")

    def test_missing_host_raises(self):
        with pytest.raises(ValueError, match="host"):
            parse_syslog_target("tcp://:514")

    def test_validate_forward_url_accepts_http(self):
        validate_forward_url("http://127.0.0.1:9999/collect")

    def test_validate_forward_url_rejects_non_http(self):
        with pytest.raises(ValueError, match="http"):
            validate_forward_url("syslog://siem.corp")


class TestSyslogFormat:
    def test_sanitize_hostname(self):
        assert sanitize_hostname("my host ") == "my-host"
        assert sanitize_hostname("") == "-"

    def test_rfc3339_utc(self):
        assert rfc3339_utc(0.0) == "1970-01-01T00:00:00.000Z"

    def test_record_timestamp_prefers_created_at(self):
        assert (
            record_timestamp({"created_at": 5.0, "requested_at": 1.0}) == 5.0
        )
        assert record_timestamp({"requested_at": 2.0}) == 2.0
        assert record_timestamp({}) == 0.0

    def test_escape_sd_value(self):
        assert escape_sd_value('a"b]c\\d') == 'a\\"b\\]c\\\\d'

    def test_format_syslog_line_shape(self):
        record = {
            "source": "audit_events",
            "forward_cursor": 7,
            "event": "user.create",
            "detail": {"n": 1},
            "created_at": 1704067200.0,
        }
        line = format_syslog_line(record)
        assert line.startswith("<110>1 2024-01-01T00:00:00.000Z ")
        assert " " + SYSLOG_HOSTNAME + " klangkd " in line
        assert ' audit_events [klangk source="audit_events" cursor="7"] ' in (
            line
        )
        message = json.loads(line.rsplit(" ", 1)[1])
        assert message["event"] == "user.create"

    def test_ssl_context_for(self):
        assert isinstance(ssl_context_for("tls"), ssl.SSLContext)
        assert ssl_context_for("tcp") is None

    async def test_close_writer_quietly_swallows_close_errors(self):
        writer = types.SimpleNamespace(
            close=lambda: None,
            wait_closed=AsyncMock(side_effect=RuntimeError("reset")),
        )
        await close_writer_quietly(writer)  # must not raise
        writer.wait_closed.assert_awaited_once()


class TestModel:
    async def test_watermark_defaults_to_zero(self, app_state, db):
        model = app_state.state.model.audit_forward
        assert await model.watermark("audit_events") == 0

    async def test_advance_then_advance_again_upserts(self, app_state, db):
        model = app_state.state.model.audit_forward
        await model.advance("audit_events", 3)
        assert await model.watermark("audit_events") == 3
        await model.advance("audit_events", 9)
        assert await model.watermark("audit_events") == 9

    async def test_rows_after_all_three_sources(self, app_state, db):
        model = app_state.state.model
        await _seed_audit_events(app_state, 3)
        await model.container_events.record("ws-a", "start", "api")
        await _seed_consent_row(app_state)

        forward_model = model.audit_forward
        events = await forward_model.rows_after("audit_events", 0, 50)
        assert [row["id"] for row in events] == [1, 2, 3]
        assert [row["forward_cursor"] for row in events] == [1, 2, 3]
        assert events[0]["detail"] == {"n": 1}  # odd ids seeded a detail
        assert events[1]["detail"] is None  # even ids seeded detail=None

        containers = await forward_model.rows_after("container_events", 0, 50)
        assert len(containers) == 1
        assert containers[0]["event"] == "start"
        assert containers[0]["forward_cursor"] == 1

        consent = await forward_model.rows_after("egress_consent", 0, 50)
        assert len(consent) == 1
        assert consent[0]["dest_host"] == "example.com"
        assert consent[0]["forward_cursor"] == 1

    async def test_rows_after_respects_cursor_and_limit(self, app_state, db):
        await _seed_audit_events(app_state, 5)
        model = app_state.state.model.audit_forward
        rows = await model.rows_after("audit_events", 2, 2)
        assert [row["id"] for row in rows] == [3, 4]

    async def test_pending_count_tracks_cursor(self, app_state, db):
        await _seed_audit_events(app_state, 3)
        model = app_state.state.model.audit_forward
        assert await model.pending_count("audit_events", 0) == 3
        assert await model.pending_count("audit_events", 2) == 1


@pytest.fixture
async def forwarder(app_state, db):
    """An unconfigured forwarder over the per-test app state."""
    return AuditForwarder(app_state)


class TestUnconfigured:
    async def test_sweep_is_a_noop(self, app_state, forwarder):
        await _seed_audit_events(app_state, 3)
        with patch("klangk.audit_forward.post_json", new=AsyncMock()) as post:
            await forwarder.sweep()
        post.assert_not_awaited()

    async def test_status_disabled(self, forwarder):
        assert await forwarder.status() == {"enabled": False}

    async def test_targets_empty(self, forwarder):
        assert forwarder.targets() == []

    async def test_interval_is_short(self, forwarder):
        assert forwarder.interval == FORWARD_INTERVAL_SECONDS

    async def test_no_watermark_rows_written(self, app_state, forwarder, db):
        await forwarder.sweep()
        assert (
            await app_state.state.model.audit_forward.watermark(
                SOURCE_AUDIT_EVENTS
            )
            == 0
        )


class TestUrlDelivery:
    async def test_delivers_in_order_and_advances(self, app_state, forwarder):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 3)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        post.assert_awaited_once()
        url, payload = post.await_args[0][:2]
        assert url == "https://siem/ingest"
        records = payload["records"]
        assert [r["id"] for r in records] == [1, 2, 3]
        assert all(r["source"] == "audit_events" for r in records)
        status = await forwarder.status()
        assert status["enabled"] and status["healthy"]
        assert status["pending"]["audit_events"] == 0
        assert status["last_success_at"] is not None

    async def test_sweep_with_no_new_rows_reports_nothing(
        self, app_state, forwarder
    ):
        """A sweep that ships nothing must not touch the health state
        (no delivery happened — no evidence either way)."""
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        post.assert_not_awaited()
        status = await forwarder.status()
        assert status["healthy"] is True

    async def test_empty_backlog_sweep_clears_stale_failure(
        self, app_state, forwarder
    ):
        """A failed sweep followed by a clean sweep over an empty
        backlog clears healthy=false (nothing is queued — the
        degradation is resolved)."""
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        forwarder.record_failure(
            ("url", "https://siem/ingest"), RuntimeError("was down")
        )
        forwarder.target_cooldown_until.clear()  # backoff elapsed
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        post.assert_not_awaited()
        status = await forwarder.status()
        assert status["healthy"] is True

    async def test_second_sweep_ships_only_new_rows(
        self, app_state, forwarder
    ):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await _seed_audit_events(app_state, 2)
            await forwarder.sweep()
            await _seed_audit_events(app_state, 2, first=3)
            await forwarder.sweep()
        second = post.await_args_list[1].args[1]["records"]
        assert [r["id"] for r in second] == [3, 4]

    async def test_restart_resumes_from_persisted_watermark(
        self, app_state, forwarder
    ):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await _seed_audit_events(app_state, 3)
            await forwarder.sweep()
            restarted = AuditForwarder(app_state)
            await _seed_audit_events(app_state, 2, first=4)
            await restarted.sweep()
        records = post.await_args_list[-1].args[1]["records"]
        assert [r["id"] for r in records] == [4, 5]

    async def test_batch_is_bounded(self, app_state, forwarder):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 3)
        post = AsyncMock()
        with (
            patch("klangk.audit_forward.post_json", new=post),
            patch("klangk.audit_forward.FORWARD_BATCH_LIMIT", 2),
        ):
            await forwarder.sweep()
            first_records = post.await_args_list[0].args[1]["records"]
            assert [r["id"] for r in first_records] == [1, 2]
            status = await forwarder.status()
            assert status["pending"]["audit_events"] == 1
            # The rest drains on the next sweep (a full batch first,
            # then the remainder).
            await forwarder.sweep()
            last_records = post.await_args_list[-1].args[1]["records"]
            assert [r["id"] for r in last_records] == [3]

    async def test_container_events_and_consent_ship_too(
        self, app_state, forwarder
    ):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        model = app_state.state.model
        await _seed_audit_events(app_state, 1)
        await model.container_events.record("ws-a", "start", "api")
        await _seed_consent_row(app_state)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        sources = [
            call.args[1]["records"][0]["source"]
            for call in post.await_args_list
        ]
        assert set(sources) == {
            "audit_events",
            "container_events",
            "egress_consent",
        }


class TestNoReuse:
    """Migration 0037's reason for existing (#3252 review): SQLite
    reuses the highest rowid after a delete, which would silently skip
    a reused-id row past the watermark. The AUTOINCREMENT ids and the
    consent forward_seq never reuse."""

    async def test_audit_events_ids_never_reuse(self, app_state, db):
        await _seed_audit_events(app_state, 3)
        async with app_state.state.db.transaction() as tx:
            await tx.execute("DELETE FROM audit_events WHERE id = 3")
        await _seed_audit_events(app_state, 1, first=3)  # id must be 4
        model = app_state.state.model.audit_forward
        rows = await model.rows_after("audit_events", 3, 10)
        assert [row["id"] for row in rows] == [4]

    async def test_container_events_ids_never_reuse(self, app_state, db):
        model = app_state.state.model
        await model.container_events.record("ws-a", "start", "api")  # id 1
        await model.container_events.record("ws-a", "stop", "api")  # id 2
        async with app_state.state.db.transaction() as tx:
            await tx.execute("DELETE FROM container_events WHERE id = 2")
        await model.container_events.record("ws-b", "start", "api")
        rows = await model.audit_forward.rows_after("container_events", 2, 10)
        assert [row["id"] for row in rows] == [3]

    async def test_consent_forward_seq_never_reuses(self, app_state, db):
        """clear_tilrestart_duration / cascade deletes can remove the
        newest consent row; the next insert's forward_seq must still
        advance past the deleted one."""
        model = app_state.state.model
        await _seed_consent_row(app_state)
        rows = await model.audit_forward.rows_after("egress_consent", 0, 10)
        assert [row["forward_cursor"] for row in rows] == [1]
        async with app_state.state.db.transaction() as tx:
            await tx.execute("DELETE FROM egress_consent")
        await _seed_consent_row(app_state)  # forward_seq 2, not 1
        rows = await model.audit_forward.rows_after("egress_consent", 1, 10)
        assert [row["forward_cursor"] for row in rows] == [2]

    async def test_deleted_max_rows_are_never_skipped(
        self, app_state, forwarder
    ):
        """End-to-end: forward everything, delete the newest row, add a
        new row — the forwarder must see the new one."""
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 2)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
            async with app_state.state.db.transaction() as tx:
                await tx.execute("DELETE FROM audit_events WHERE id = 2")
            await _seed_audit_events(app_state, 1, first=2)
            await forwarder.sweep()
        last = post.await_args.args[1]["records"]
        assert [r["id"] for r in last] == [3]

    async def test_corrupt_detail_ships_raw(self, app_state, db, caplog):
        """A tampered/corrupt detail blob ships as its raw string — one
        bad row never wedges the source behind it."""
        import logging

        await _seed_audit_events(app_state, 1)
        async with app_state.state.db.transaction() as tx:
            await tx.execute(
                "UPDATE audit_events SET detail = ? WHERE id = 1",
                ("{not json",),
            )
        rows = await app_state.state.model.audit_forward.rows_after(
            "audit_events", 0, 10
        )
        assert rows[0]["detail"] == "{not json"
        assert any(
            "not valid JSON" in r.message
            for r in caplog.records
            if r.levelno == logging.WARNING
        )


class TestFailureAndBackoff:
    async def test_failure_keeps_rows_queued_and_surfaces(
        self, app_state, forwarder
    ):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 3)
        post = AsyncMock(side_effect=RuntimeError("network gone"))
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
            status = await forwarder.status()
        assert post.await_count == 1
        assert status["healthy"] is False
        assert status["last_error"] == "RuntimeError"
        assert status["last_failure_at"] is not None
        assert status["pending"]["audit_events"] == 3

    async def test_cooldown_skips_the_sweep(self, app_state, forwarder):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 1)
        post = AsyncMock(side_effect=RuntimeError("down"))
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
            await forwarder.sweep()  # inside the cooldown: no attempt
        assert post.await_count == 1

    async def test_retry_after_backoff_redelivers_at_least_once(
        self, app_state, forwarder
    ):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 3)
        post = AsyncMock(side_effect=RuntimeError("down"))
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        post.side_effect = None
        forwarder.target_cooldown_until.clear()  # backoff elapsed
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        records = post.await_args.args[1]["records"]
        assert [r["id"] for r in records] == [1, 2, 3]  # full replay
        status = await forwarder.status()
        assert status["healthy"] is True
        assert status["pending"]["audit_events"] == 0
        assert forwarder.target_failures == {}

    def test_backoff_doubles_and_caps(self, forwarder):
        target = ("url", "https://siem/ingest")
        forwarder.target_failures[target[1]] = 1
        assert forwarder.backoff_seconds(target) == FORWARD_INTERVAL_SECONDS
        forwarder.target_failures[target[1]] = 2
        assert (
            forwarder.backoff_seconds(target) == FORWARD_INTERVAL_SECONDS * 2
        )
        forwarder.target_failures[target[1]] = 99
        assert forwarder.backoff_seconds(target) == 300.0

    async def test_reconfigure_resets_retry_state(self, app_state, forwarder):
        forwarder.record_failure(("url", "https://siem/x"), RuntimeError("x"))
        forwarder.reconfigure(app_state)
        assert forwarder.healthy is True
        assert forwarder.target_failures == {}
        assert forwarder.target_cooldown_until == {}
        assert forwarder.last_error is None

    async def test_dead_target_backoff_does_not_throttle_healthy_one(
        self, app_state, db
    ):
        """Per-target cooldowns: with syslog down and url healthy, the
        url target keeps its full sweep cadence (its backoff budget
        stays clean)."""
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        refused_port = probe.getsockname()[1]
        probe.close()
        app_state.state.settings = make_settings(
            {
                "KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest",
                "KLANGKD_AUDIT_FORWARD_SYSLOG": (
                    f"tcp://127.0.0.1:{refused_port}"
                ),
            }
        )
        forwarder = AuditForwarder(app_state)
        await _seed_audit_events(app_state, 1)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
            await _seed_audit_events(app_state, 1, first=2)
            await forwarder.sweep()  # immediate: syslog's cooldown
            # applies to syslog only
        # The healthy target delivered the new row on the very next
        # sweep — the dead target's backoff never delayed it.
        assert post.await_count == 2
        second = post.await_args_list[1].args[1]["records"]
        assert [r["id"] for r in second] == [2]
        assert forwarder.target_failures == {
            f"tcp://127.0.0.1:{refused_port}": 1
        }

    async def test_pending_count_failure_reports_none(
        self, app_state, forwarder
    ):
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        with patch.object(
            AuditForwardModel,
            "pending_count",
            AsyncMock(side_effect=RuntimeError("db gone")),
        ):
            status = await forwarder.status()
        assert status["pending"]["audit_events"] is None


async def wait_for_lines(receiver, count, deadline_seconds=5.0):
    """Poll the receiver until *count* lines arrived (bounded)."""
    deadline = time.monotonic() + deadline_seconds
    while len(receiver.lines) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


class _SyslogServer:
    """A local RFC 5424-over-TCP receiver capturing raw lines."""

    def __init__(self):
        self.lines: list[str] = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(
            self._on_client, "127.0.0.1", 0
        )
        port = self.server.sockets[0].getsockname()[1]
        return port

    async def _on_client(self, reader, writer):
        data = await reader.read()
        self.lines.extend(line.decode() for line in data.split(b"\n") if line)
        writer.close()
        await writer.wait_closed()

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class TestSyslogDelivery:
    async def test_delivers_rfc5424_lines(self, app_state, forwarder):
        receiver = _SyslogServer()
        port = await receiver.start()
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_SYSLOG": f"tcp://127.0.0.1:{port}"}
        )
        try:
            await _seed_audit_events(app_state, 2)
            await forwarder.sweep()
            await wait_for_lines(receiver, 2)
        finally:
            await receiver.stop()
        assert len(receiver.lines) == 2
        assert receiver.lines[0].startswith("<110>1 ")
        assert (
            ' audit_events [klangk source="audit_events" cursor="1"] '
            in receiver.lines[0]
        )
        message = json.loads(receiver.lines[0].rsplit(" ", 1)[1])
        assert message["id"] == 1
        key = forwarder.state_key(
            "audit_events", ("syslog", f"tcp://127.0.0.1:{port}")
        )
        assert await app_state.state.model.audit_forward.watermark(key) == 2

    async def test_down_receiver_records_failure(self, app_state, db):
        # Bind then close: a port with no listener refuses connections.
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_SYSLOG": f"tcp://127.0.0.1:{port}"}
        )
        forwarder = AuditForwarder(app_state)
        await _seed_audit_events(app_state, 1)
        await forwarder.sweep()
        status = await forwarder.status()
        assert status["healthy"] is False
        assert status["pending"]["audit_events"] == 1


class TestBothTargets:
    async def test_each_target_receives_every_record(
        self, app_state, forwarder
    ):
        receiver = _SyslogServer()
        port = await receiver.start()
        app_state.state.settings = make_settings(
            {
                "KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest",
                "KLANGKD_AUDIT_FORWARD_SYSLOG": f"tcp://127.0.0.1:{port}",
            }
        )
        post = AsyncMock()
        try:
            with patch("klangk.audit_forward.post_json", new=post):
                await _seed_audit_events(app_state, 2)
                await forwarder.sweep()
                await wait_for_lines(receiver, 2)
        finally:
            await receiver.stop()
        records = post.await_args.args[1]["records"]
        assert [r["id"] for r in records] == [1, 2]
        assert len(receiver.lines) == 2
        model = app_state.state.model.audit_forward
        assert (
            await model.watermark(
                forwarder.state_key(
                    "audit_events", ("url", "https://siem/ingest")
                )
            )
            == 2
        )
        assert (
            await model.watermark(
                forwarder.state_key(
                    "audit_events", ("syslog", f"tcp://127.0.0.1:{port}")
                )
            )
            == 2
        )

    async def test_dead_target_does_not_block_the_healthy_one(
        self, app_state, db
    ):
        """Per-target cursors: the syslog target refuses, but the url
        target still delivers and advances its own cursor. The sweep
        records the syslog failure (one backoff), and the /audit
        pending depth reflects the slowest target."""
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        refused_port = probe.getsockname()[1]
        probe.close()
        app_state.state.settings = make_settings(
            {
                "KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest",
                "KLANGKD_AUDIT_FORWARD_SYSLOG": (
                    f"tcp://127.0.0.1:{refused_port}"
                ),
            }
        )
        forwarder = AuditForwarder(app_state)
        await _seed_audit_events(app_state, 2)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        post.assert_awaited_once()
        records = post.await_args.args[1]["records"]
        assert [r["id"] for r in records] == [1, 2]
        model = app_state.state.model.audit_forward
        assert (
            await model.watermark(
                forwarder.state_key(
                    "audit_events", ("url", "https://siem/ingest")
                )
            )
            == 2
        )
        assert (
            await model.watermark(
                forwarder.state_key(
                    "audit_events",
                    ("syslog", f"tcp://127.0.0.1:{refused_port}"),
                )
            )
            == 0
        )
        status = await forwarder.status()
        assert status["healthy"] is False
        assert status["pending"]["audit_events"] == 2

    async def test_reconfigured_target_replays_the_backlog(
        self, app_state, forwarder
    ):
        """A new target hashes to a new cursor key and starts from
        zero — it receives the retained rows (at-least-once)."""
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        await _seed_audit_events(app_state, 2)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
            app_state.state.settings = make_settings(
                {"KLANGKD_AUDIT_FORWARD_URL": "https://siem2/ingest"}
            )
            await forwarder.sweep()
        replay = post.await_args.args[1]["records"]
        assert [r["id"] for r in replay] == [1, 2]

    async def test_syslog_timeout_aborts_a_wedged_receiver(
        self, app_state, db
    ):
        """Real wedge (#3252 second review): a receiver that accepts
        and never reads. The write blocks in drain() past the socket
        buffers; the timeout cancels it, the abort discards the unsent
        buffer, and wait_for actually raises — the sweep records the
        failure instead of hanging forever with /audit showing
        healthy. No patching of the code under test: the receiver and
        the oversized payload are real."""

        release = asyncio.Event()

        async def hold(reader, writer):
            # Accept and never read (a real wedge); the test's teardown
            # releases the handler so server.wait_closed() can finish.
            await release.wait()

        server = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        app_state.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_SYSLOG": f"tcp://127.0.0.1:{port}"}
        )
        forwarder = AuditForwarder(app_state)
        await _seed_audit_events(app_state, 64)
        big_line = "x" * 1_000_000
        try:
            with (
                patch(
                    "klangk.audit_forward.format_syslog_line",
                    return_value=big_line,
                ),
                patch(
                    "klangk.audit_forward.FORWARD_SYSLOG_TIMEOUT_SECONDS", 0.2
                ),
            ):
                await forwarder.sweep()
        finally:
            release.set()
            server.close()
            await server.wait_closed()
        status = await forwarder.status()
        assert status["healthy"] is False
        assert status["last_error"] == "TimeoutError"

    async def test_header_reaches_the_url_target(self, app_state, forwarder):
        app_state.state.settings = make_settings(
            {
                "KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest",
                "KLANGKD_AUDIT_FORWARD_HEADER": (
                    "Authorization: Splunk abc123"
                ),
            }
        )
        await _seed_audit_events(app_state, 1)
        post = AsyncMock()
        with patch("klangk.audit_forward.post_json", new=post):
            await forwarder.sweep()
        kwargs = post.await_args.kwargs
        assert kwargs["headers"] == {"Authorization": "Splunk abc123"}

    def test_url_headers_none_when_unset(self, forwarder):
        assert forwarder.url_headers() is None


class TestAuditEndpoint:
    async def test_forwarding_key_absent_when_unconfigured(
        self, api_app, api_client
    ):
        api_app.state.audit_forwarder = AuditForwarder(api_app)
        resp = await api_client.get("/audit")
        assert resp.status_code == 200
        assert resp.json() == {"write_failures": 0, "fail_closed": False}

    async def test_no_forwarder_at_all_keeps_legacy_shape(
        self, api_app, api_client
    ):
        """A minimal app without the forwarder state (the getattr
        guard) answers with the pre-#3252 body."""
        del api_app.state.audit_forwarder
        resp = await api_client.get("/audit")
        assert resp.status_code == 200
        assert resp.json() == {"write_failures": 0, "fail_closed": False}

    async def test_forwarding_status_when_configured(
        self, api_app, api_client
    ):
        api_app.state.settings = make_settings(
            {"KLANGKD_AUDIT_FORWARD_URL": "https://siem/ingest"}
        )
        forwarder = AuditForwarder(api_app)
        forwarder.record_failure(
            ("url", "https://siem/ingest"), RuntimeError("down")
        )
        api_app.state.audit_forwarder = forwarder
        resp = await api_client.get("/audit")
        forwarding = resp.json()["forwarding"]
        assert forwarding["enabled"] is True
        assert forwarding["healthy"] is False
        assert forwarding["last_error"] == "RuntimeError"
        assert forwarding["targets"] == ["url"]
