"""HMAC integrity tagging for audit records (#3174) — write side only.

Covers the ``audit_hmac`` module (opt-in key resolution, canonical
serialization, tag computation), the HMAC-on-insert paths in
``container_events`` and ``egress_consent``, and the migration that
adds the ``hmac`` column. klangkd only writes tags (opt-in via
``KLANGKD_AUDIT_HMAC_KEY``); verification is an external consumer's
job (off-host backup, auditor tooling).
"""

import pytest

from klangk.model.audit_hmac import (
    _canonical_pairs,
    compute_container_event_hmac,
    compute_egress_consent_hmac,
    resolve_audit_hmac_key,
)
from klangk.model.container_events import EVENT_START
from klangk.model.egress_consent import (
    DECISION_ALLOWED,
    _restamp,
)


@pytest.fixture(autouse=True)
def audit_hmac_test_key(app_state):
    """Tagging is opt-in (#3174): configure an explicit key for every
    test in this module unless a test overrides it to None."""
    app_state.state.settings.audit_hmac_key = "test-audit-key"


class TestKeyResolution:
    def test_explicit_key_used_when_set(self):
        class FakeSettings:
            audit_hmac_key = "my-explicit-key"
            jwt_secret = "ignored"

        assert resolve_audit_hmac_key(FakeSettings()) == b"my-explicit-key"

    def test_unset_key_disables_tagging(self):
        class FakeSettings:
            audit_hmac_key = None
            jwt_secret = "the-jwt-secret"

        # No derivation from the JWT secret — tagging is fully opt-in.
        assert resolve_audit_hmac_key(FakeSettings()) is None

    def test_empty_string_key_disables_tagging(self):
        class FakeSettings:
            audit_hmac_key = ""
            jwt_secret = "s"

        assert resolve_audit_hmac_key(FakeSettings()) is None

    def test_compute_returns_none_when_no_key(self):
        class FakeSettings:
            audit_hmac_key = None
            jwt_secret = "s"

        row = {"id": 1}
        assert compute_container_event_hmac(FakeSettings(), row) is None
        assert compute_egress_consent_hmac(FakeSettings(), row) is None


class TestCanonicalSerialization:
    """The encoding must be injective: distinct field values must
    never serialize identically (a NULL sentinel or delimiter
    collision would make two different rows tag identically)."""

    def test_none_is_distinct_from_the_literal_marker(self):
        cols = ["process_name", "dest_host"]
        null_row = {"process_name": None, "dest_host": "x"}
        marker_row = {"process_name": "n", "dest_host": "x"}
        assert _canonical_pairs("t", null_row, cols) != _canonical_pairs(
            "t", marker_row, cols
        )

    def test_none_is_distinct_from_nil_literal(self):
        cols = ["process_name"]
        assert _canonical_pairs("t", {"process_name": None}, cols) != (
            _canonical_pairs("t", {"process_name": "<nil>"}, cols)
        )

    def test_delimiter_characters_cannot_splice_fields(self):
        cols = ["a", "b"]
        # A crafted value containing the separators must not deserialize
        # into the same payload as a different honest split.
        crafted = {"a": "x=5\0b=y", "b": None}
        honest = {"a": "x", "b": "5\0b=y"}
        assert _canonical_pairs("t", crafted, cols) != _canonical_pairs(
            "t", honest, cols
        )


class TestComputeContainerEventHmac:
    def test_deterministic(self):
        class S:
            audit_hmac_key = "k"
            jwt_secret = "j"

        row = {
            "id": 1,
            "workspace_id": "ws-1",
            "event": "start",
            "actor_type": "user",
            "actor_id": "u1",
            "cause": "api",
            "container_id": "cid-1",
            "container_role": "workspace",
            "network_namespace": None,
            "created_at": 1000.0,
        }
        tag1 = compute_container_event_hmac(S(), row)
        tag2 = compute_container_event_hmac(S(), row)
        assert tag1 == tag2
        assert len(tag1) == 64  # sha256 hex

    def test_different_data_different_tag(self):
        class S:
            audit_hmac_key = "k"
            jwt_secret = "j"

        row = {
            "id": 1,
            "workspace_id": "ws-1",
            "event": "start",
            "actor_type": "user",
            "actor_id": "u1",
            "cause": "api",
            "container_id": "cid-1",
            "container_role": "workspace",
            "network_namespace": None,
            "created_at": 1000.0,
        }
        tag1 = compute_container_event_hmac(S(), row)
        row["cause"] = "idle_timeout"
        tag2 = compute_container_event_hmac(S(), row)
        assert tag1 != tag2


class TestContainerEventsHmac:
    async def test_record_stores_hmac(self, app_state, db):
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        rows = await events.list_events()
        row = rows[0]
        assert row["hmac"] is not None
        assert row["hmac"] == compute_container_event_hmac(
            app_state.state.settings, row
        )

    async def test_no_key_writes_no_hmac(self, app_state, db):
        events = app_state.state.model.container_events
        app_state.state.settings.audit_hmac_key = None
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        rows = await events.list_events()
        assert rows[0]["hmac"] is None


class TestEgressConsentHmac:
    async def test_create_request_stores_hmac(self, app_state, db, workspace):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "example.com", 443)
        assert row is not None
        assert row["hmac"] is not None
        assert row["hmac"] == compute_egress_consent_hmac(
            app_state.state.settings, row
        )

    async def test_static_denial_stores_hmac(self, app_state, db, workspace):
        ec = app_state.state.model.egress_consent
        row = await ec.record_static_denial(workspace["id"], "bad.com", 443)
        assert row is not None
        assert row["hmac"] is not None

    async def test_static_allow_stores_hmac(self, app_state, db, workspace):
        ec = app_state.state.model.egress_consent
        row = await ec.record_static_allow(workspace["id"], "safe.com", 443)
        assert row is not None
        assert row["hmac"] is not None

    async def test_decide_recomputes_hmac(
        self, app_state, db, workspace, user
    ):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "example.com", 443)
        original_hmac = row["hmac"]
        decided = await ec.decide(row["id"], DECISION_ALLOWED, user["id"])
        assert decided is not None
        assert decided["hmac"] != original_hmac
        assert decided["hmac"] == compute_egress_consent_hmac(
            app_state.state.settings, decided
        )

    async def test_revoke_recomputes_hmac(
        self, app_state, db, workspace, user
    ):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "example.com", 443)
        decided = await ec.decide(row["id"], DECISION_ALLOWED, user["id"])
        decided_hmac = decided["hmac"]
        revoked = await ec.revoke(row["id"], user["id"])
        assert revoked is not None
        assert revoked["hmac"] != decided_hmac
        assert revoked["hmac"] == compute_egress_consent_hmac(
            app_state.state.settings, revoked
        )

    async def test_no_key_writes_no_hmac(self, app_state, db, workspace, user):
        ec = app_state.state.model.egress_consent
        app_state.state.settings.audit_hmac_key = None
        row = await ec.create_request(workspace["id"], "example.com", 443)
        assert row is not None
        assert row["hmac"] is None
        decided = await ec.decide(row["id"], DECISION_ALLOWED, user["id"])
        assert decided is not None
        assert decided["hmac"] is None

    async def test_restamp_missing_row_returns_none(self, app_state, db):
        """The re-stamp helper's miss path: a row that no longer
        exists surfaces as None instead of raising."""
        async with app_state.state.db.transaction() as conn:
            row = await _restamp(conn, app_state.state.settings, "no-such-id")
        assert row is None

    async def test_expire_pending_restamps_hmac(
        self, app_state, db, workspace
    ):
        """expire_pending mutates decision/decided_at — the HMAC must
        be recomputed so the row still carries a valid tag."""
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "expire.com", 443)
        original_hmac = row["hmac"]
        assert await ec.expire_pending(row["id"]) is True
        refreshed = await ec.get_request(row["id"])
        assert refreshed["hmac"] is not None
        assert refreshed["hmac"] != original_hmac
        assert refreshed["hmac"] == compute_egress_consent_hmac(
            app_state.state.settings, refreshed
        )

    async def test_expire_all_pending_restamps_hmac(
        self, app_state, db, workspace
    ):
        """expire_all_pending is a bulk path — every expired row must
        carry a valid tag afterwards."""
        ec = app_state.state.model.egress_consent
        ws_id = workspace["id"]
        await ec.create_request(ws_id, "a.com", 80)
        await ec.create_request(ws_id, "b.com", 443)
        assert await ec.expire_all_pending() == 2
        rows = await ec.list_requests(workspace_id=ws_id)
        for row in rows:
            assert row["hmac"] is not None
            assert row["hmac"] == compute_egress_consent_hmac(
                app_state.state.settings, row
            )


class TestMigration:
    async def test_hmac_column_exists(self, app_state, db):
        """The migration adds hmac columns to both audit tables."""
        for table in ("container_events", "egress_consent"):
            info = await app_state.state.db.fetchall(
                f"PRAGMA table_info({table})"
            )
            col_names = {row[1] for row in info}
            assert "hmac" in col_names, f"{table} missing hmac column"


class TestOffsiteContract:
    """The documented offsite recompute recipe (docs/reference/
    audit-integrity.md): a stdlib-only checker reading the raw
    klangk.db must reproduce every stored tag. If this fails, the
    writer's serialization drifted from the published contract."""

    CE_COLUMNS = [
        "id",
        "workspace_id",
        "event",
        "actor_type",
        "actor_id",
        "cause",
        "container_id",
        "container_role",
        "network_namespace",
        "created_at",
    ]
    EC_COLUMNS = [
        "id",
        "workspace_id",
        "dest_host",
        "dest_port",
        "pid",
        "process_name",
        "decision",
        "duration",
        "requested_at",
        "decided_at",
        "decided_by",
        "revoked_at",
        "revoked_by",
    ]

    def _payload(self, table, row, columns):
        parts = [table]
        for col in columns:
            val = row[col]
            if val is None:
                parts.append(f"{col}=n")
            else:
                sv = str(val)
                parts.append(f"{col}={len(sv)}:{sv}")
        return "\0".join(parts).encode()

    def _stored_tags(self, path, table, columns):
        import hashlib
        import hmac as hmac_mod
        import sqlite3

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        names = ", ".join(columns)
        for row in conn.execute(f"SELECT {names}, hmac FROM {table}"):
            d = dict(zip(columns + ["hmac"], row))
            expected = hmac_mod.new(
                b"test-audit-key",
                self._payload(table, d, columns),
                hashlib.sha256,
            ).hexdigest()
            yield d["id"], d["hmac"], expected
        conn.close()

    async def test_offsite_recompute_matches_stored_tags(
        self, app_state, db, workspace, user
    ):
        events = app_state.state.model.container_events
        ec = app_state.state.model.egress_consent
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        row = await ec.create_request(workspace["id"], "a.com", 80)
        await ec.decide(row["id"], DECISION_ALLOWED, user["id"])
        path = app_state.state.db.db_path
        for table, columns in (
            ("container_events", self.CE_COLUMNS),
            ("egress_consent", self.EC_COLUMNS),
        ):
            checked = 0
            for row_id, stored, expected in self._stored_tags(
                path, table, columns
            ):
                assert stored == expected, (
                    f"{table} id={row_id}: offsite recompute mismatch"
                )
                checked += 1
            assert checked >= 1, f"{table}: nothing tagged"
