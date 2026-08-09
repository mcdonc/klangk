"""Tests for ``EgressConsentModel`` and the ``egress_mode`` workspace field (#2239)."""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from klangk.model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DECISION_PENDING,
    SCOPE_ONCE,
    SCOPE_WORKSPACE,
)
from klangk.model.workspaces import (
    EGRESS_MODE_INTERACTIVE,
    EGRESS_MODE_STATIC,
)


@pytest.fixture
async def ec(app_state, db):
    """``app_state.state.model.egress_consent`` with schema initialized."""
    return app_state.state.model.egress_consent


@pytest.fixture
async def ws(app_state, db):
    return app_state.state.model.workspaces


# -- egress_mode on workspaces --


async def test_workspace_default_egress_mode(ws, user):
    row = await ws.create_workspace(user["id"], "default-mode")
    assert row["egress_mode"] == EGRESS_MODE_STATIC


async def test_workspace_create_interactive_mode(ws, user):
    row = await ws.create_workspace(
        user["id"], "interactive", egress_mode=EGRESS_MODE_INTERACTIVE
    )
    assert row["egress_mode"] == EGRESS_MODE_INTERACTIVE
    got = await ws.get_workspace(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_INTERACTIVE


async def test_workspace_create_invalid_egress_mode(ws, user):
    with pytest.raises(ValueError, match="Invalid egress_mode"):
        await ws.create_workspace(user["id"], "bad-mode", egress_mode="bogus")


async def test_workspace_create_with_acl_invalid_egress_mode(ws, user):
    with pytest.raises(ValueError, match="Invalid egress_mode"):
        await ws.create_workspace_with_acl(
            user["id"], "bad-mode", egress_mode="bogus"
        )


async def test_workspace_update_egress_mode(ws, user):
    row = await ws.create_workspace(user["id"], "update-mode")
    assert row["egress_mode"] == EGRESS_MODE_STATIC
    updated = await ws.update_workspace(
        row["id"], user["id"], egress_mode=EGRESS_MODE_INTERACTIVE
    )
    assert updated
    got = await ws.get_workspace(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_INTERACTIVE


async def test_workspace_update_invalid_egress_mode(ws, user):
    row = await ws.create_workspace(user["id"], "bad-update")
    with pytest.raises(ValueError, match="Invalid egress_mode"):
        await ws.update_workspace(row["id"], user["id"], egress_mode="nope")


async def test_egress_mode_in_list_workspaces(ws, user):
    await ws.create_workspace(
        user["id"], "listed", egress_mode=EGRESS_MODE_INTERACTIVE
    )
    result = await ws.list_workspaces(user["id"])
    item = result["items"][0]
    assert item["egress_mode"] == EGRESS_MODE_INTERACTIVE


async def test_egress_mode_in_get_workspace_by_id(ws, user):
    row = await ws.create_workspace(
        user["id"], "by-id", egress_mode=EGRESS_MODE_INTERACTIVE
    )
    got = await ws.get_workspace_by_id(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_INTERACTIVE


# -- egress_consent CRUD --


async def test_create_request(ec, ws, user):
    w = await ws.create_workspace(user["id"], "consent-ws")
    req = await ec.create_request(w["id"], "api.example.com", 443)
    assert req["workspace_id"] == w["id"]
    assert req["dest_host"] == "api.example.com"
    assert req["dest_port"] == 443
    assert req["decision"] == DECISION_PENDING
    assert req["decided_by"] is None
    assert req["pid"] is None
    assert req["process_name"] is None


async def test_create_request_with_process_info(ec, ws, user):
    w = await ws.create_workspace(user["id"], "proc-ws")
    req = await ec.create_request(
        w["id"], "example.com", 80, pid=1234, process_name="curl"
    )
    assert req["pid"] == 1234
    assert req["process_name"] == "curl"


async def test_create_request_no_port(ec, ws, user):
    w = await ws.create_workspace(user["id"], "noport-ws")
    req = await ec.create_request(w["id"], "example.com")
    assert req["dest_port"] is None


async def test_create_request_dedup_returns_none(ec, ws, user):
    """Duplicate pending request for same (workspace, host, port) returns None."""
    w = await ws.create_workspace(user["id"], "dedup-ws")
    first = await ec.create_request(w["id"], "api.com", 443)
    assert first is not None
    second = await ec.create_request(w["id"], "api.com", 443)
    assert second is None
    # Only one row exists
    assert await ec.count_pending(w["id"]) == 1


async def test_create_request_dedup_no_port(ec, ws, user):
    """Dedup works for portless requests too."""
    w = await ws.create_workspace(user["id"], "dedup-noport")
    assert await ec.create_request(w["id"], "a.com") is not None
    assert await ec.create_request(w["id"], "a.com") is None


async def test_create_request_dedup_different_port_allowed(ec, ws, user):
    """Same host but different port is a distinct request."""
    w = await ws.create_workspace(user["id"], "dedup-diffport")
    assert await ec.create_request(w["id"], "a.com", 443) is not None
    assert await ec.create_request(w["id"], "a.com", 80) is not None
    assert await ec.count_pending(w["id"]) == 2


async def test_create_request_after_decision_allows_new_pending(ec, ws, user):
    """After a request is decided, a new pending for the same dest is allowed."""
    w = await ws.create_workspace(user["id"], "re-request")
    req = await ec.create_request(w["id"], "a.com", 443)
    await ec.decide(req["id"], DECISION_DENIED, None, user["id"])
    # The old one is no longer pending, so a new one should succeed
    new = await ec.create_request(w["id"], "a.com", 443)
    assert new is not None
    assert new["id"] != req["id"]


async def test_get_request(ec, ws, user):
    w = await ws.create_workspace(user["id"], "get-ws")
    req = await ec.create_request(w["id"], "example.com", 443)
    got = await ec.get_request(req["id"])
    assert got["id"] == req["id"]
    assert got["dest_host"] == "example.com"


async def test_get_request_missing(ec):
    assert await ec.get_request("nonexistent") is None


async def test_list_requests(ec, ws, user):
    w = await ws.create_workspace(user["id"], "list-ws")
    await ec.create_request(w["id"], "a.com", 443)
    await ec.create_request(w["id"], "b.com", 80)
    items = await ec.list_requests(w["id"])
    assert len(items) == 2
    # Most recent first
    assert items[0]["dest_host"] == "b.com"


async def test_list_requests_filtered(ec, ws, user):
    w = await ws.create_workspace(user["id"], "filter-ws")
    req = await ec.create_request(w["id"], "a.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, SCOPE_ONCE, user["id"])
    await ec.create_request(w["id"], "b.com", 80)

    pending = await ec.list_requests(w["id"], decision=DECISION_PENDING)
    assert len(pending) == 1
    assert pending[0]["dest_host"] == "b.com"

    allowed = await ec.list_requests(w["id"], decision=DECISION_ALLOWED)
    assert len(allowed) == 1
    assert allowed[0]["dest_host"] == "a.com"


async def test_count_pending(ec, ws, user):
    w = await ws.create_workspace(user["id"], "count-ws")
    assert await ec.count_pending(w["id"]) == 0
    await ec.create_request(w["id"], "a.com", 443)
    await ec.create_request(w["id"], "b.com", 80)
    assert await ec.count_pending(w["id"]) == 2


async def test_has_pending(ec, ws, user):
    w = await ws.create_workspace(user["id"], "has-ws")
    assert not await ec.has_pending(w["id"], "a.com", 443)
    await ec.create_request(w["id"], "a.com", 443)
    assert await ec.has_pending(w["id"], "a.com", 443)
    assert not await ec.has_pending(w["id"], "a.com", 80)


async def test_has_pending_no_port(ec, ws, user):
    w = await ws.create_workspace(user["id"], "has-noport")
    await ec.create_request(w["id"], "a.com")
    assert await ec.has_pending(w["id"], "a.com", None)
    assert not await ec.has_pending(w["id"], "a.com", 443)


async def test_decide_allow(ec, ws, user):
    w = await ws.create_workspace(user["id"], "decide-ws")
    req = await ec.create_request(w["id"], "api.com", 443)
    result = await ec.decide(
        req["id"], DECISION_ALLOWED, SCOPE_WORKSPACE, user["id"]
    )
    assert result["decision"] == DECISION_ALLOWED
    assert result["scope"] == SCOPE_WORKSPACE
    assert result["decided_by"] == user["id"]
    assert result["decided_at"] is not None


async def test_decide_deny(ec, ws, user):
    w = await ws.create_workspace(user["id"], "deny-ws")
    req = await ec.create_request(w["id"], "bad.com", 443)
    result = await ec.decide(req["id"], DECISION_DENIED, None, user["id"])
    assert result["decision"] == DECISION_DENIED


async def test_decide_invalid_decision_raises(ec, ws, user):
    w = await ws.create_workspace(user["id"], "bad-decision")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid decision"):
        await ec.decide(req["id"], "bogus", SCOPE_ONCE, user["id"])


async def test_decide_pending_not_allowed_as_decision(ec, ws, user):
    """Can't 'decide' to set decision back to pending."""
    w = await ws.create_workspace(user["id"], "pend-decide")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid decision"):
        await ec.decide(req["id"], DECISION_PENDING, None, user["id"])


async def test_decide_expired_not_allowed_as_decision(ec, ws, user):
    """expired is for expire_pending(), not decide()."""
    w = await ws.create_workspace(user["id"], "exp-decide")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid decision"):
        await ec.decide(req["id"], DECISION_EXPIRED, None, user["id"])


async def test_decide_invalid_scope_raises(ec, ws, user):
    w = await ws.create_workspace(user["id"], "bad-scope")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid scope"):
        await ec.decide(req["id"], DECISION_ALLOWED, "nonsense", user["id"])


async def test_decide_already_decided(ec, ws, user):
    w = await ws.create_workspace(user["id"], "double-ws")
    req = await ec.create_request(w["id"], "api.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, SCOPE_ONCE, user["id"])
    # Second decide on same request returns None (no longer pending)
    result = await ec.decide(req["id"], DECISION_DENIED, None, user["id"])
    assert result is None


async def test_decide_missing(ec, user):
    result = await ec.decide(
        "no-such-id", DECISION_ALLOWED, SCOPE_ONCE, user["id"]
    )
    assert result is None


async def test_expire_pending(ec, ws, user):
    w = await ws.create_workspace(user["id"], "expire-ws")
    req = await ec.create_request(w["id"], "slow.com", 443)
    assert await ec.expire_pending(req["id"])
    got = await ec.get_request(req["id"])
    assert got["decision"] == DECISION_EXPIRED
    assert got["decided_by"] is None  # auto-expired, no user


async def test_expire_distinct_from_deny(ec, ws, user):
    """Expired and denied are distinguishable in the audit trail."""
    w = await ws.create_workspace(user["id"], "exp-vs-deny")
    r1 = await ec.create_request(w["id"], "a.com", 443)
    r2 = await ec.create_request(w["id"], "b.com", 80)
    await ec.expire_pending(r1["id"])
    await ec.decide(r2["id"], DECISION_DENIED, None, user["id"])

    g1 = await ec.get_request(r1["id"])
    g2 = await ec.get_request(r2["id"])
    assert g1["decision"] == DECISION_EXPIRED
    assert g2["decision"] == DECISION_DENIED
    assert g1["decided_by"] is None
    assert g2["decided_by"] == user["id"]


async def test_expire_already_decided(ec, ws, user):
    w = await ws.create_workspace(user["id"], "expire2-ws")
    req = await ec.create_request(w["id"], "fast.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, SCOPE_ONCE, user["id"])
    assert not await ec.expire_pending(req["id"])


async def test_delete_for_workspace(ec, ws, user):
    w = await ws.create_workspace(user["id"], "del-ws")
    await ec.create_request(w["id"], "a.com", 443)
    await ec.create_request(w["id"], "b.com", 80)
    count = await ec.delete_for_workspace(w["id"])
    assert count == 2
    assert await ec.list_requests(w["id"]) == []


async def test_cascade_delete_on_workspace_delete(ec, ws, user):
    w = await ws.create_workspace(user["id"], "cascade-ws")
    await ec.create_request(w["id"], "a.com", 443)
    await ws.delete_workspace(w["id"], user["id"])
    assert await ec.list_requests(w["id"]) == []


# -- DB-level integrity (CHECK constraints + partial unique index) --
#
# The CHECK constraints + partial unique index are the structural backstop:
# a code path that bypasses EgressConsentModel (raw SQL) still can't land a
# bad decision/scope or a duplicate pending prompt. These prove the
# constraints are enforced at the storage layer, independent of decide()'s
# Python validation (#2251). They mirror the idiom in
# test_main.test_users_handle_has_unique_constraint.


async def test_db_check_rejects_invalid_decision_on_insert(
    ws, user, db, app_state
):
    w = await ws.create_workspace(user["id"], "chk-ins-ws")
    async with app_state.state.db.transaction() as conn:
        with pytest.raises(SAIntegrityError) as exc_info:
            await conn.execute(
                "INSERT INTO egress_consent"
                " (id, workspace_id, dest_host, decision, requested_at)"
                " VALUES (?, ?, ?, 'bogus', ?)",
                ("r1", w["id"], "a.com", 0.0),
            )
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)


async def test_db_check_rejects_invalid_decision_on_update(
    ws, user, db, app_state
):
    w = await ws.create_workspace(user["id"], "chk-upd-ws")
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO egress_consent"
            " (id, workspace_id, dest_host, requested_at)"
            " VALUES (?, ?, ?, ?)",
            ("r2", w["id"], "a.com", 0.0),
        )
        with pytest.raises(SAIntegrityError) as exc_info:
            await conn.execute(
                "UPDATE egress_consent SET decision = 'bogus' WHERE id = ?",
                ("r2",),
            )
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)


async def test_db_check_rejects_invalid_scope(ws, user, db, app_state):
    w = await ws.create_workspace(user["id"], "chk-scope-ws")
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO egress_consent"
            " (id, workspace_id, dest_host, requested_at)"
            " VALUES (?, ?, ?, ?)",
            ("r3", w["id"], "a.com", 0.0),
        )
        with pytest.raises(SAIntegrityError) as exc_info:
            await conn.execute(
                "UPDATE egress_consent SET scope = 'nonsense' WHERE id = ?",
                ("r3",),
            )
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)


async def test_db_check_accepts_null_and_legal_scopes(ws, user, db, app_state):
    """scope NULL (the default) + each legal value pass the CHECK."""
    w = await ws.create_workspace(user["id"], "chk-scope-ok")
    legal = [None, "once", "workspace", "deploy"]
    async with app_state.state.db.transaction() as conn:
        for i, scope in enumerate(legal):
            await conn.execute(
                "INSERT INTO egress_consent"
                " (id, workspace_id, dest_host, scope, requested_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"ok-{i}", w["id"], f"h{i}.com", scope, 0.0),
            )


async def test_db_partial_unique_index_rejects_duplicate_pending(
    ws, user, db, app_state
):
    """The partial unique index is the real backstop behind INSERT OR IGNORE:
    a plain second INSERT of a pending for the same (workspace, host, port)
    raises — dedup is structural, not just app-level (#2251)."""
    w = await ws.create_workspace(user["id"], "chk-uniq-ws")
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO egress_consent"
            " (id, workspace_id, dest_host, dest_port, requested_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("u1", w["id"], "a.com", 443, 0.0),
        )
        with pytest.raises(SAIntegrityError) as exc_info:
            await conn.execute(
                "INSERT INTO egress_consent"
                " (id, workspace_id, dest_host, dest_port, requested_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("u2", w["id"], "a.com", 443, 0.0),
            )
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)
