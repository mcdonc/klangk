"""HMAC integrity protection for audit records (#3174).

Covers the ``audit_hmac`` module (key derivation, canonical
serialization, compute/verify), the HMAC-on-insert paths in
``container_events`` and ``egress_consent``, the per-table
``verify_integrity`` method, and the migration that adds the ``hmac``
column.
"""

import hmac as _hmac
import hashlib


from klangk.model.audit_hmac import (
    TAMPER_REPORT_CAP,
    _canonical_pairs,
    _resolve_key,
    compute_container_event_hmac,
    compute_egress_consent_hmac,
    verify_hmac,
)
from klangk.model.container_events import EVENT_START, EVENT_STOP, CAUSE_STOP
from klangk.model.egress_consent import (
    DECISION_ALLOWED,
    _restamp,
)


class TestKeyDerivation:
    def test_explicit_key_used_when_set(self):
        class FakeSettings:
            audit_hmac_key = "my-explicit-key"
            jwt_secret = "ignored"

        assert _resolve_key(FakeSettings()) == b"my-explicit-key"

    def test_derived_from_jwt_secret_when_unset(self):
        class FakeSettings:
            audit_hmac_key = None
            jwt_secret = "the-jwt-secret"

        key = _resolve_key(FakeSettings())
        expected = _hmac.new(
            b"the-jwt-secret", b"klangk-audit-hmac-v1", hashlib.sha256
        ).digest()
        assert key == expected

    def test_empty_string_key_derives_from_jwt(self):
        class FakeSettings:
            audit_hmac_key = ""
            jwt_secret = "s"

        key = _resolve_key(FakeSettings())
        expected = _hmac.new(
            b"s", b"klangk-audit-hmac-v1", hashlib.sha256
        ).digest()
        assert key == expected


class TestCanonicalSerialization:
    """The encoding must be injective: distinct field values must
    never serialize identically (fresh-eyes review of #3174 — a NULL
    sentinel or delimiter collision would be an undetectable tamper
    class)."""

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


class TestVerifyHmac:
    def test_matching_tags(self):
        assert verify_hmac("abc123", "abc123") is True

    def test_mismatched_tags(self):
        assert verify_hmac("abc123", "xyz789") is False

    def test_none_stored_always_fails(self):
        assert verify_hmac(None, "abc123") is False

    def test_blob_stored_fails_instead_of_raising(self):
        # A tamperer writing a BLOB into the TEXT column must not crash
        # the verifier (hmac.compare_digest would TypeError).
        assert verify_hmac(b"\x00", "abc123") is False

    def test_non_ascii_stored_fails_instead_of_raising(self):
        assert verify_hmac("caf\u00e9", "abc123") is False

    def test_int_stored_fails(self):
        assert verify_hmac(5, "abc123") is False


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
        await events.record("ws-a", EVENT_START, "api", actor_id="u1")
        rows = await events.list_events()
        assert len(rows) == 1
        assert rows[0]["hmac"] is not None
        assert len(rows[0]["hmac"]) == 64

    async def test_stored_hmac_verifies(self, app_state, db):
        events = app_state.state.model.container_events
        await events.record(
            "ws-a", EVENT_START, "api", actor_id="u1", container_id="cid-1"
        )
        rows = await events.list_events()
        row = rows[0]
        expected = compute_container_event_hmac(app_state.state.settings, row)
        assert verify_hmac(row["hmac"], expected)

    async def test_verify_integrity_clean(self, app_state, db):
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        await events.record("ws-a", EVENT_STOP, CAUSE_STOP, container_id="c2")
        result = await events.verify_integrity()
        assert result["total"] == 2
        assert result["verified"] == 2
        assert result["no_hmac"] == 0
        assert result["tampered"] == []

    async def test_verify_integrity_detects_tamper(self, app_state, db):
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        rows = await events.list_events()
        row_id = rows[0]["id"]
        # Tamper with the row
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE container_events SET cause = 'hacked'"
                " WHERE container_id = 'c1'"
            )
        result = await events.verify_integrity()
        assert result["total"] == 1
        assert result["verified"] == 0
        assert result["tampered_total"] == 1
        assert result["tampered"] == [{"id": row_id, "workspace_id": "ws-a"}]

    async def test_blob_hmac_is_tampered_not_crash(self, app_state, db):
        """A tamperer writing a BLOB into the hmac column must be
        reported, not crash the whole verification pass."""
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE container_events SET hmac = x'00'"
                " WHERE container_id = 'c1'"
            )
        result = await events.verify_integrity()
        assert result["tampered_total"] == 1
        assert result["no_hmac"] == 0

    async def test_tampered_list_is_capped(self, app_state, db):
        """The tampered id list truncates at TAMPER_REPORT_CAP; the
        full count still travels in tampered_total."""
        events = app_state.state.model.container_events
        for i in range(TAMPER_REPORT_CAP + 5):
            await events.record(
                "ws-a", EVENT_START, "api", container_id=f"c{i}"
            )
        async with app_state.state.db.transaction() as conn:
            await conn.execute("UPDATE container_events SET cause = 'hacked'")
        result = await events.verify_integrity()
        assert result["tampered_total"] == TAMPER_REPORT_CAP + 5
        assert len(result["tampered"]) == TAMPER_REPORT_CAP
        assert result["tampered_truncated"] is True

    async def test_verify_integrity_null_hmac_is_no_hmac(self, app_state, db):
        """Rows without an HMAC (pre-migration) are counted as no_hmac."""
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="c1")
        # Clear the HMAC to simulate a pre-migration row
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE container_events SET hmac = NULL"
                " WHERE container_id = 'c1'"
            )
        result = await events.verify_integrity()
        assert result["no_hmac"] == 1
        assert result["tampered"] == []


class TestEgressConsentHmac:
    async def test_create_request_stores_hmac(self, app_state, db, workspace):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "example.com", 443)
        assert row is not None
        assert row["hmac"] is not None
        assert len(row["hmac"]) == 64

    async def test_create_request_hmac_verifies(
        self, app_state, db, workspace
    ):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "example.com", 443)
        expected = compute_egress_consent_hmac(app_state.state.settings, row)
        assert verify_hmac(row["hmac"], expected)

    async def test_static_denial_stores_hmac(self, app_state, db, workspace):
        ec = app_state.state.model.egress_consent
        row = await ec.record_static_denial(workspace["id"], "evil.com", 80)
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
        assert decided["hmac"] is not None
        assert decided["hmac"] != original_hmac
        expected = compute_egress_consent_hmac(
            app_state.state.settings, decided
        )
        assert verify_hmac(decided["hmac"], expected)

    async def test_revoke_recomputes_hmac(
        self, app_state, db, workspace, user
    ):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "example.com", 443)
        decided = await ec.decide(row["id"], DECISION_ALLOWED, user["id"])
        decided_hmac = decided["hmac"]
        revoked = await ec.revoke(row["id"], user["id"])
        assert revoked is not None
        assert revoked["hmac"] is not None
        assert revoked["hmac"] != decided_hmac
        expected = compute_egress_consent_hmac(
            app_state.state.settings, revoked
        )
        assert verify_hmac(revoked["hmac"], expected)

    async def test_verify_integrity_clean(self, app_state, db, workspace):
        ec = app_state.state.model.egress_consent
        ws_id = workspace["id"]
        await ec.create_request(ws_id, "a.com", 80)
        await ec.record_static_denial(ws_id, "b.com", 443)
        result = await ec.verify_integrity()
        assert result["total"] == 2
        assert result["verified"] == 2
        assert result["no_hmac"] == 0
        assert result["tampered"] == []

    async def test_verify_integrity_detects_tamper(
        self, app_state, db, workspace
    ):
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "a.com", 80)
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE egress_consent SET dest_host = 'hacked.com'"
                " WHERE id = ?",
                (row["id"],),
            )
        result = await ec.verify_integrity()
        assert result["tampered"] == [
            {"id": row["id"], "workspace_id": workspace["id"]}
        ]

    async def test_verify_integrity_null_marker_impersonation_detected(
        self, app_state, db, workspace
    ):
        """Flipping a NULL column to the serialization's marker value
        must not verify clean (fresh-eyes review of #3174: a NULL
        sentinel collision would be an undetectable tamper class)."""
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "a.com", 80)
        assert row["process_name"] is None
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE egress_consent SET process_name = 'n' WHERE id = ?",
                (row["id"],),
            )
        result = await ec.verify_integrity()
        assert result["tampered_total"] == 1
        assert result["tampered"] == [
            {"id": row["id"], "workspace_id": workspace["id"]}
        ]

    async def test_restamp_missing_row_returns_none(self, app_state, db):
        """The re-stamp helper's miss path: a row that no longer
        exists surfaces as None instead of raising."""
        async with app_state.state.db.transaction() as conn:
            row = await _restamp(conn, app_state.state.settings, "no-such-id")
        assert row is None

    async def test_verify_integrity_null_hmac_is_no_hmac(
        self, app_state, db, workspace
    ):
        """Rows without an HMAC (pre-migration) are counted as no_hmac."""
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "a.com", 80)
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE egress_consent SET hmac = NULL WHERE id = ?",
                (row["id"],),
            )
        result = await ec.verify_integrity()
        assert result["no_hmac"] == 1
        assert result["tampered"] == []

    async def test_expire_pending_restamps_hmac(
        self, app_state, db, workspace
    ):
        """expire_pending mutates decision/decided_at — the HMAC must
        be recomputed so the row still verifies."""
        ec = app_state.state.model.egress_consent
        row = await ec.create_request(workspace["id"], "expire.com", 443)
        original_hmac = row["hmac"]
        assert await ec.expire_pending(row["id"]) is True
        result = await ec.verify_integrity()
        assert result["verified"] == 1
        assert result["tampered"] == []
        # The tag must have changed (different decision + decided_at).
        refreshed = await ec.get_request(row["id"])
        assert refreshed["hmac"] != original_hmac

    async def test_expire_all_pending_restamps_hmac(
        self, app_state, db, workspace
    ):
        """expire_all_pending is a bulk path — every expired row must
        carry a valid HMAC afterwards."""
        ec = app_state.state.model.egress_consent
        ws_id = workspace["id"]
        await ec.create_request(ws_id, "a.com", 80)
        await ec.create_request(ws_id, "b.com", 443)
        assert await ec.expire_all_pending() == 2
        result = await ec.verify_integrity()
        assert result["verified"] == 2
        assert result["tampered"] == []


class TestMigration:
    async def test_hmac_column_exists(self, app_state, db):
        """The migration adds hmac columns to both audit tables."""
        for table in ("container_events", "egress_consent"):
            info = await app_state.state.db.fetchall(
                f"PRAGMA table_info({table})"
            )
            col_names = {row[1] for row in info}
            assert "hmac" in col_names, f"{table} missing hmac column"
