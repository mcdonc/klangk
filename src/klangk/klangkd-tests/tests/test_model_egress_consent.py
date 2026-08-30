"""Tests for ``EgressConsentModel`` and the ``egress_mode`` workspace field (#2239)."""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from klangk.model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_EXPIRED,
    DECISION_PENDING,
    DECISION_REVOKED,
    DURATIONS,
    DURATION_5M,
    DURATION_1W,
    DURATION_FOREVER,
    DURATION_TILRESTART,
)
from klangk.model.workspaces import (
    EGRESS_MODE_ALLOW,
    EGRESS_MODE_DEFAULT,
    EGRESS_MODE_INTERACTIVE,
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
    assert row["egress_mode"] == EGRESS_MODE_DEFAULT


async def test_workspace_create_interactive_mode(ws, user):
    row = await ws.create_workspace(
        user["id"], "interactive", egress_mode=EGRESS_MODE_INTERACTIVE
    )
    assert row["egress_mode"] == EGRESS_MODE_INTERACTIVE
    got = await ws.get_workspace(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_INTERACTIVE


async def test_workspace_create_allow_mode(ws, user):
    # #2406: "allow" is a valid egress_mode (default-permit).
    row = await ws.create_workspace(
        user["id"], "allow-mode", egress_mode=EGRESS_MODE_ALLOW
    )
    assert row["egress_mode"] == EGRESS_MODE_ALLOW
    got = await ws.get_workspace(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_ALLOW


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
    assert row["egress_mode"] == EGRESS_MODE_DEFAULT
    updated = await ws.update_workspace(
        row["id"], user["id"], egress_mode=EGRESS_MODE_INTERACTIVE
    )
    assert updated
    got = await ws.get_workspace(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_INTERACTIVE


async def test_workspace_update_egress_mode_to_allow(ws, user):
    # #2406: a workspace can be switched to allow mode via update.
    row = await ws.create_workspace(user["id"], "update-to-allow")
    updated = await ws.update_workspace(
        row["id"], user["id"], egress_mode=EGRESS_MODE_ALLOW
    )
    assert updated
    got = await ws.get_workspace(row["id"])
    assert got["egress_mode"] == EGRESS_MODE_ALLOW


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


async def test_record_static_denial_inserts_denied_no_human(ec, ws, user):
    w = await ws.create_workspace(user["id"], "static-denial-ws")
    req = await ec.record_static_denial(w["id"], "evil.com", 443)
    assert req["decision"] == DECISION_DENIED
    assert req["decided_by"] is None  # no human
    assert req["decided_at"] is not None  # decided immediately
    row = await ec.get_request(req["id"])
    assert row["decision"] == DECISION_DENIED
    assert row["decided_by"] is None


async def test_record_static_denial_dedup_per_destination(ec, ws, user):
    # One static denial per (workspace, host, port); a second call returns None.
    w = await ws.create_workspace(user["id"], "static-dedup-ws")
    first = await ec.record_static_denial(w["id"], "evil.com", 443)
    second = await ec.record_static_denial(w["id"], "evil.com", 443)
    assert first is not None
    assert second is None


async def test_record_static_allow_inserts_allowed_no_human(ec, ws, user):
    # #2406: allow mode records an off-list destination as allowed, no human,
    # immediately (the logging side effect of default-permit egress).
    w = await ws.create_workspace(user["id"], "static-allow-ws")
    req = await ec.record_static_allow(w["id"], "registry.npmjs.org", 443)
    assert req["decision"] == DECISION_ALLOWED
    assert req["decided_by"] is None  # no human
    assert req["decided_at"] is not None  # decided immediately
    row = await ec.get_request(req["id"])
    assert row["decision"] == DECISION_ALLOWED
    assert row["decided_by"] is None


async def test_record_static_allow_dedup_per_destination(ec, ws, user):
    # One allow-mode allow per (workspace, host, port); a second returns None.
    w = await ws.create_workspace(user["id"], "static-allow-dedup-ws")
    first = await ec.record_static_allow(w["id"], "registry.npmjs.org", 443)
    second = await ec.record_static_allow(w["id"], "registry.npmjs.org", 443)
    assert first is not None
    assert second is None


async def test_record_static_allow_distinct_from_denial(ec, ws, user):
    # The allow and denial static rows are independent (distinct dedup indexes):
    # the same (workspace, host, port) can carry both an allow-mode allow and
    # a static denial without colliding.
    w = await ws.create_workspace(user["id"], "static-allow-vs-deny-ws")
    allow = await ec.record_static_allow(w["id"], "dual.example", 443)
    denial = await ec.record_static_denial(w["id"], "dual.example", 443)
    assert allow is not None
    assert denial is not None
    assert allow["id"] != denial["id"]


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
    await ec.decide(req["id"], DECISION_DENIED, user["id"])
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
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"])
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
    result = await ec.decide(req["id"], DECISION_ALLOWED, user["id"])
    assert result["decision"] == DECISION_ALLOWED
    assert result["decided_by"] == user["id"]
    assert result["decided_at"] is not None


async def test_decide_deny(ec, ws, user):
    w = await ws.create_workspace(user["id"], "deny-ws")
    req = await ec.create_request(w["id"], "bad.com", 443)
    result = await ec.decide(req["id"], DECISION_DENIED, user["id"])
    assert result["decision"] == DECISION_DENIED


async def test_decide_invalid_decision_raises(ec, ws, user):
    w = await ws.create_workspace(user["id"], "bad-decision")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid decision"):
        await ec.decide(req["id"], "bogus", user["id"])


async def test_decide_records_duration(ec, ws, user):
    w = await ws.create_workspace(user["id"], "dur-ws")
    req = await ec.create_request(w["id"], "a.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"], "1d")
    row = await ec.app.state.db.fetchone(
        "SELECT duration FROM egress_consent WHERE id = ?", (req["id"],)
    )
    assert row["duration"] == "1d"


async def test_decide_default_duration_is_restart(ec, ws, user):
    w = await ws.create_workspace(user["id"], "dur-default")
    req = await ec.create_request(w["id"], "a.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"])
    row = await ec.app.state.db.fetchone(
        "SELECT duration FROM egress_consent WHERE id = ?", (req["id"],)
    )
    assert row["duration"] == "tilrestart"


async def test_decide_invalid_duration_raises(ec, ws, user):
    w = await ws.create_workspace(user["id"], "bad-dur")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid duration"):
        await ec.decide(req["id"], DECISION_ALLOWED, user["id"], "2d")


async def test_decide_pending_not_allowed_as_decision(ec, ws, user):
    """Can't 'decide' to set decision back to pending."""
    w = await ws.create_workspace(user["id"], "pend-decide")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid decision"):
        await ec.decide(req["id"], DECISION_PENDING, user["id"])


async def test_decide_expired_not_allowed_as_decision(ec, ws, user):
    """expired is for expire_pending(), not decide()."""
    w = await ws.create_workspace(user["id"], "exp-decide")
    req = await ec.create_request(w["id"], "a.com", 443)
    with pytest.raises(ValueError, match="Invalid decision"):
        await ec.decide(req["id"], DECISION_EXPIRED, user["id"])


async def test_decide_already_decided(ec, ws, user):
    w = await ws.create_workspace(user["id"], "double-ws")
    req = await ec.create_request(w["id"], "api.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"])
    # Second decide on same request returns None (no longer pending)
    result = await ec.decide(req["id"], DECISION_DENIED, user["id"])
    assert result is None


async def test_decide_missing(ec, user):
    result = await ec.decide("no-such-id", DECISION_ALLOWED, user["id"])
    assert result is None


async def test_expire_pending(ec, ws, user):
    w = await ws.create_workspace(user["id"], "expire-ws")
    req = await ec.create_request(w["id"], "slow.com", 443)
    assert await ec.expire_pending(req["id"])
    got = await ec.get_request(req["id"])
    assert got["decision"] == DECISION_EXPIRED
    assert got["decided_by"] is None  # auto-expired, no user
    # a timeout is a non-persistent deny: duration=once (#2328)
    row = await ec.app.state.db.fetchone(
        "SELECT duration FROM egress_consent WHERE id = ?", (req["id"],)
    )
    assert row["duration"] == "once"


async def test_expire_all_pending_reaps_only_pending(ec, ws, user):
    """Startup reaping: every still-pending row is an orphan (its in-memory
    hold died with the prior process), so expire them so the decider snapshot
    doesn't replay stale requests. Already-decided rows are left untouched.
    """
    w1 = await ws.create_workspace(user["id"], "reap1")
    w2 = await ws.create_workspace(user["id"], "reap2")
    p1 = await ec.create_request(w1["id"], "a.com", 443)
    p2 = await ec.create_request(w2["id"], "b.com", 80)
    decided = await ec.create_request(w1["id"], "c.com", 443)
    await ec.decide(decided["id"], DECISION_DENIED, user["id"])
    assert await ec.expire_all_pending() == 2
    assert (await ec.get_request(p1["id"]))["decision"] == DECISION_EXPIRED
    assert (await ec.get_request(p2["id"]))["decision"] == DECISION_EXPIRED
    assert (await ec.get_request(decided["id"]))["decision"] == DECISION_DENIED


async def test_expire_distinct_from_deny(ec, ws, user):
    """Expired and denied are distinguishable in the audit trail."""
    w = await ws.create_workspace(user["id"], "exp-vs-deny")
    r1 = await ec.create_request(w["id"], "a.com", 443)
    r2 = await ec.create_request(w["id"], "b.com", 80)
    await ec.expire_pending(r1["id"])
    await ec.decide(r2["id"], DECISION_DENIED, user["id"])

    g1 = await ec.get_request(r1["id"])
    g2 = await ec.get_request(r2["id"])
    assert g1["decision"] == DECISION_EXPIRED
    assert g2["decision"] == DECISION_DENIED
    assert g1["decided_by"] is None
    assert g2["decided_by"] == user["id"]


async def test_expire_already_decided(ec, ws, user):
    w = await ws.create_workspace(user["id"], "expire2-ws")
    req = await ec.create_request(w["id"], "fast.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"])
    assert not await ec.expire_pending(req["id"])


async def test_cascade_delete_on_workspace_delete(ec, ws, user):
    w = await ws.create_workspace(user["id"], "cascade-ws")
    await ec.create_request(w["id"], "a.com", 443)
    await ws.delete_workspace(w["id"], user["id"])
    assert await ec.list_requests(w["id"]) == []


# -- list_active (in-effect computation, #2335 slice A) --


async def test_list_active_groups_allowed_and_denied(ec, ws, user):
    w = await ws.create_workspace(user["id"], "active-grp")
    a = await ec.create_request(w["id"], "allow.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    d = await ec.create_request(w["id"], "deny.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], "forever")
    rows = await ec.list_active(w["id"])
    decisions = {r["dest_host"]: r["decision"] for r in rows}
    assert decisions == {
        "allow.com": DECISION_ALLOWED,
        "deny.com": DECISION_DENIED,
    }
    # each row carries its duration (the read fix, #2338)
    by_host = {r["dest_host"]: r for r in rows}
    assert by_host["allow.com"]["duration"] == "tilrestart"
    assert by_host["deny.com"]["duration"] == "forever"


async def test_list_active_excludes_once(ec, ws, user):
    # `once` is consumed by the single connection — never in effect.
    w = await ws.create_workspace(user["id"], "active-once")
    a = await ec.create_request(w["id"], "once.com")
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], "once")
    assert await ec.list_active(w["id"]) == []


async def test_list_active_time_bounded_within_and_past_window(
    ec, ws, user, app_state
):
    w = await ws.create_workspace(user["id"], "active-timed")
    fresh = await ec.create_request(w["id"], "fresh.com")
    await ec.decide(fresh["id"], DECISION_ALLOWED, user["id"], "5m")
    stale = await ec.create_request(w["id"], "stale.com")
    await ec.decide(stale["id"], DECISION_ALLOWED, user["id"], "5m")
    # Backdate the stale row past its 5m window.
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "UPDATE egress_consent SET decided_at = ? WHERE id = ?",
            (0.0, stale["id"]),
        )
    hosts = {r["dest_host"] for r in await ec.list_active(w["id"])}
    assert hosts == {"fresh.com"}


async def test_list_active_excludes_static_denial(ec, ws, user):
    # Static policy denials (decided_by NULL) are the allow-list's complement,
    # not actionable verdicts — excluded from the in-effect view.
    w = await ws.create_workspace(user["id"], "active-static")
    await ec.record_static_denial(w["id"], "evil.com", 443)
    assert await ec.list_active(w["id"]) == []


async def test_list_active_excludes_pending_and_expired(ec, ws, user):
    w = await ws.create_workspace(user["id"], "active-other")
    pending = await ec.create_request(w["id"], "pending.com")  # stays pending
    to_expire = await ec.create_request(w["id"], "expired.com")
    await ec.expire_pending(to_expire["id"])  # pending -> expired (no verdict)
    assert pending is not None
    # neither pending nor expired rows are in-effect verdicts
    assert await ec.list_active(w["id"]) == []


def test_duration_in_effect_unknown_and_null_duration():
    # Unknown / NULL duration: a verdict always sets one, so this only hits a
    # NULL (future migration) — fail-safe to not-in-effect.
    from klangk.model.egress_consent import EgressConsentModel

    assert EgressConsentModel._duration_in_effect(None, 1.0, 2.0) is False
    assert EgressConsentModel._duration_in_effect("bogus", 1.0, 2.0) is False
    # decided_at None (no decision recorded) -> not in effect.
    assert EgressConsentModel._duration_in_effect("5m", None, 2.0) is False
    # restart/forever are event-bounded, not time-bounded -> always in effect.
    assert (
        EgressConsentModel._duration_in_effect("tilrestart", 0.0, 999.0)
        is True
    )
    assert (
        EgressConsentModel._duration_in_effect("forever", 0.0, 999.0) is True
    )


# -- clear_tilrestart_duration (container-restart reaping, #2346) --


async def test_clear_tilrestart_duration_clears_allow_and_deny(ec, ws, user):
    # A restart allow AND a restart deny both die with the container.
    w = await ws.create_workspace(user["id"], "clr-rst")
    a = await ec.create_request(w["id"], "allow.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    d = await ec.create_request(w["id"], "deny.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], "tilrestart")
    count = await ec.clear_tilrestart_duration(w["id"])
    assert count == 2
    # Neither survives in list_active (the rule-management view).
    assert await ec.list_active(w["id"]) == []


async def test_clear_tilrestart_duration_leaves_other_durations(ec, ws, user):
    # forever / time-bounded (5m) / once are governed by their own lifetime,
    # not the container's -- left untouched.
    w = await ws.create_workspace(user["id"], "clr-keep")
    fa = await ec.create_request(w["id"], "forever-allow.com")
    await ec.decide(fa["id"], DECISION_ALLOWED, user["id"], "forever")
    fd = await ec.create_request(w["id"], "forever-deny.com")
    await ec.decide(fd["id"], DECISION_DENIED, user["id"], "forever")
    tb = await ec.create_request(w["id"], "timed.com")
    await ec.decide(tb["id"], DECISION_ALLOWED, user["id"], "5m")
    once = await ec.create_request(w["id"], "once.com")
    await ec.decide(once["id"], DECISION_ALLOWED, user["id"], "once")
    # one restart row to clear, to confirm only it is reaped.
    ra = await ec.create_request(w["id"], "restart.com")
    await ec.decide(ra["id"], DECISION_ALLOWED, user["id"], "tilrestart")

    count = await ec.clear_tilrestart_duration(w["id"])
    assert count == 1
    # forever (allow + deny) + the fresh 5m are still in effect; once excluded.
    hosts = {r["dest_host"] for r in await ec.list_active(w["id"])}
    assert hosts == {"forever-allow.com", "forever-deny.com", "timed.com"}


async def test_clear_tilrestart_duration_leaves_pending_and_static(
    ec, ws, user
):
    # Pending requests + static policy denials are not restart verdicts.
    w = await ws.create_workspace(user["id"], "clr-pend")
    await ec.create_request(w["id"], "pending.com", 443)  # stays pending
    await ec.record_static_denial(w["id"], "static.com", 443)
    count = await ec.clear_tilrestart_duration(w["id"])
    assert count == 0
    # Both rows still present (list_active excludes them, so check list_requests).
    hosts = {r["dest_host"] for r in await ec.list_requests(w["id"])}
    assert hosts == {"pending.com", "static.com"}


async def test_clear_tilrestart_duration_is_scoped_to_workspace(ec, ws, user):
    # Only the named workspace's restart rows are reaped.
    w1 = await ws.create_workspace(user["id"], "clr-a")
    w2 = await ws.create_workspace(user["id"], "clr-b")
    a = await ec.create_request(w1["id"], "a.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    b = await ec.create_request(w2["id"], "b.com", 443)
    await ec.decide(b["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    count = await ec.clear_tilrestart_duration(w1["id"])
    assert count == 1
    # w2's restart row survives.
    assert {r["dest_host"] for r in await ec.list_active(w2["id"])} == {
        "b.com"
    }
    assert await ec.list_active(w1["id"]) == []


async def test_clear_tilrestart_duration_no_rows_is_zero(ec, ws, user):
    # First-ever start: no restart rows -> no-op, returns 0.
    w = await ws.create_workspace(user["id"], "clr-empty")
    assert await ec.clear_tilrestart_duration(w["id"]) == 0


# -- revoke (#2339) --


async def test_revoke_flips_active_allow(ec, ws, user):
    w = await ws.create_workspace(user["id"], "revoke-allow")
    req = await ec.create_request(w["id"], "allow.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    row = await ec.revoke(req["id"], user["id"])
    assert row["decision"] == DECISION_REVOKED
    assert row["revoked_at"] is not None
    assert row["revoked_by"] == user["id"]
    # original verdict provenance preserved
    assert row["decided_by"] == user["id"]
    assert (await ec.get_request(req["id"]))["decision"] == DECISION_REVOKED


async def test_revoke_flips_active_deny(ec, ws, user):
    w = await ws.create_workspace(user["id"], "revoke-deny")
    req = await ec.create_request(w["id"], "deny.com")
    await ec.decide(req["id"], DECISION_DENIED, user["id"], "forever")
    assert (await ec.revoke(req["id"], user["id"]))[
        "decision"
    ] == DECISION_REVOKED


async def test_revoke_returns_none_for_non_verdict(ec, ws, user):
    w = await ws.create_workspace(user["id"], "revoke-pending")
    req = await ec.create_request(w["id"], "pending.com")  # still pending
    to_expire = await ec.create_request(w["id"], "expired.com")
    await ec.expire_pending(to_expire["id"])
    assert await ec.revoke(req["id"], user["id"]) is None  # pending
    assert await ec.revoke(to_expire["id"], user["id"]) is None  # expired
    assert (await ec.get_request(req["id"]))["decision"] == DECISION_PENDING


async def test_revoke_unknown_returns_none(ec, ws, user):
    await ws.create_workspace(user["id"], "revoke-unknown")  # schema setup
    assert await ec.revoke("nope", user["id"]) is None


async def test_revoke_idempotent_second_call_returns_none(ec, ws, user):
    w = await ws.create_workspace(user["id"], "revoke-idem")
    req = await ec.create_request(w["id"], "idem.com")
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    assert await ec.revoke(req["id"], user["id"]) is not None
    assert await ec.revoke(req["id"], user["id"]) is None  # already revoked


async def test_list_active_excludes_revoked(ec, ws, user):
    w = await ws.create_workspace(user["id"], "revoke-excluded")
    req = await ec.create_request(w["id"], "rev.com")
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"], "tilrestart")
    assert len(await ec.list_active(w["id"])) == 1
    await ec.revoke(req["id"], user["id"])
    assert await ec.list_active(w["id"]) == []


# -- DB-level integrity (CHECK constraints + partial unique index) --
#
# The CHECK constraints + partial unique index are the structural backstop:
# a code path that bypasses EgressConsentModel (raw SQL) still can't land a
# bad decision or a duplicate pending prompt. These prove the
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


async def test_db_check_accepts_null_and_legal_durations(
    ws, user, db, app_state
):
    """duration NULL (pending/static rows) + each legal value pass the CHECK."""
    w = await ws.create_workspace(user["id"], "chk-dur-ok")
    # iterate the single source of truth so the test cannot drift from the
    # CHECK (which is itself generated from DURATIONS, #2338)
    legal = [None, *DURATIONS]
    async with app_state.state.db.transaction() as conn:
        for i, duration in enumerate(legal):
            await conn.execute(
                "INSERT INTO egress_consent"
                " (id, workspace_id, dest_host, duration, requested_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"ok-d-{i}", w["id"], f"h{i}.com", duration, 0.0),
            )


async def test_db_check_rejects_invalid_duration(ws, user, db, app_state):
    w = await ws.create_workspace(user["id"], "chk-dur-ws")
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO egress_consent"
            " (id, workspace_id, dest_host, requested_at)"
            " VALUES (?, ?, ?, ?)",
            ("rd1", w["id"], "a.com", 0.0),
        )
        with pytest.raises(SAIntegrityError) as exc_info:
            await conn.execute(
                "UPDATE egress_consent SET duration = '99d' WHERE id = ?",
                ("rd1",),
            )
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)


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


async def test_init_db_rebuilds_egress_consent_to_add_duration_check(
    ws, user, app_state
):
    """A pre-existing egress_consent lacking the duration CHECK is rebuilt
    in place (#2338) — data preserved, constraint attached.

    Simulates the stale shape (duration column from an earlier ALTER, no
    CHECK) by dropping the well-formed table and recreating it without the
    constraint, then re-running init_db. The rebuild path copies the row
    across and attaches the CHECK; a bad duration is then rejected at the DB.
    """
    w = await ws.create_workspace(user["id"], "rebuild-ws")
    db = app_state.state.db
    # Replace the well-formed table with a stale shape (no duration CHECK),
    # keeping one real row referencing the real workspace + user (so the
    # rebuild's FK copy satisfies foreign_keys if enforced).
    async with db.transaction() as conn:
        await conn.execute("DROP TABLE egress_consent")
        await conn.execute(
            "CREATE TABLE egress_consent ("
            " id TEXT PRIMARY KEY, workspace_id TEXT, dest_host TEXT,"
            " dest_port INTEGER, pid INTEGER, process_name TEXT,"
            " decision TEXT, duration TEXT,"
            " requested_at REAL, decided_at REAL, decided_by TEXT)"
        )
        await conn.execute(
            "INSERT INTO egress_consent"
            " (id, workspace_id, dest_host, decision, duration,"
            "  requested_at, decided_at, decided_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "keep-1",
                w["id"],
                "preserved.com",
                "allowed",
                "1d",
                1.0,
                2.0,
                user["id"],
            ),
        )
    # Re-run init_db: the rebuild fires (no CHECK in sqlite_master), attaches
    # the CHECK, and copies the row across.
    await app_state.state.model.init_db()
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT dest_host, duration FROM egress_consent WHERE id = ?",
            ("keep-1",),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["dest_host"] == "preserved.com"
    assert row["duration"] == "1d"
    # The CHECK is now enforced.
    async with db.transaction() as conn:
        with pytest.raises(SAIntegrityError) as exc_info:
            await conn.execute(
                "UPDATE egress_consent SET duration = '99d' WHERE id = ?",
                ("keep-1",),
            )
    assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)


# -- active_verdict_for (consent-pause "respect existing verdict", #2332) --


async def test_active_verdict_for_returns_in_effect_deny(ec, ws, user):
    # A recorded in-effect deny for an exact (host, port) is returned so the
    # pause path can keep blocking it (the pause does not override verdicts).
    w = await ws.create_workspace(user["id"], "avf-deny")
    d = await ec.create_request(w["id"], "deny.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], DURATION_TILRESTART)
    row = await ec.active_verdict_for(w["id"], "deny.com", 443)
    assert row is not None
    assert row["decision"] == DECISION_DENIED
    assert row["dest_host"] == "deny.com"


async def test_active_verdict_for_returns_in_effect_allow(ec, ws, user):
    w = await ws.create_workspace(user["id"], "avf-allow")
    a = await ec.create_request(w["id"], "allow.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], DURATION_FOREVER)
    row = await ec.active_verdict_for(w["id"], "allow.com", 443)
    assert row is not None
    assert row["decision"] == DECISION_ALLOWED


async def test_active_verdict_for_none_when_no_verdict(ec, ws, user):
    # No recorded verdict -> None (the pause path auto-allows).
    w = await ws.create_workspace(user["id"], "avf-none")
    assert await ec.active_verdict_for(w["id"], "unseen.com", 443) is None


async def test_active_verdict_for_port_exact(ec, ws, user):
    # A verdict for host:443 does NOT match a lookup for host:80 (least
    # privilege: a deny on one port does not block another).
    w = await ws.create_workspace(user["id"], "avf-port")
    d = await ec.create_request(w["id"], "deny.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], DURATION_TILRESTART)
    assert await ec.active_verdict_for(w["id"], "deny.com", 80) is None
    assert await ec.active_verdict_for(w["id"], "deny.com", 443) is not None


async def test_active_verdict_for_excludes_expired(ec, ws, user, app_state):
    # A verdict whose timed window has elapsed is not in effect -> None.
    w = await ws.create_workspace(user["id"], "avf-expired")
    a = await ec.create_request(w["id"], "stale.com", 443)
    await ec.decide(a["id"], DECISION_DENIED, user["id"], DURATION_5M)
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "UPDATE egress_consent SET decided_at = ? WHERE id = ?",
            (0.0, a["id"]),
        )
    assert await ec.active_verdict_for(w["id"], "stale.com", 443) is None


async def test_active_verdict_for_most_recent_wins(ec, ws, user):
    # A later verdict for the same host:port overrides an earlier one (an
    # allow then a deny -> the deny is current).
    w = await ws.create_workspace(user["id"], "avf-recent")
    a = await ec.create_request(w["id"], "flip.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], DURATION_TILRESTART)
    d = await ec.create_request(w["id"], "flip.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], DURATION_TILRESTART)
    row = await ec.active_verdict_for(w["id"], "flip.com", 443)
    assert row["decision"] == DECISION_DENIED


async def test_active_verdict_for_elapsed_newer_allow_does_not_mask_older_deny(
    ec, ws, user, app_state
):
    # B2 regression (#2332): a newer-but-elapsed ALLOW must NOT hide an older
    # in-effect DENY -- the pause path must still see the deny and block. The
    # naive "ORDER BY decided_at DESC LIMIT 1" + single-row in-effect check
    # returned None here (auto-allowing a host the user denied).
    import time

    now = time.time()
    w = await ws.create_workspace(user["id"], "avf-b2")
    # older in-effect DENY (tilrestart), backdated to now-1000
    d = await ec.create_request(w["id"], "flip.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], DURATION_TILRESTART)
    # newer elapsed ALLOW (5m), backdated to now-500 -> past its window
    a = await ec.create_request(w["id"], "flip.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], DURATION_5M)
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "UPDATE egress_consent SET decided_at = ? WHERE id = ?",
            (now - 1000, d["id"]),
        )
        await conn.execute(
            "UPDATE egress_consent SET decided_at = ? WHERE id = ?",
            (now - 500, a["id"]),
        )
    row = await ec.active_verdict_for(w["id"], "flip.com", 443)
    assert row is not None
    assert row["decision"] == DECISION_DENIED


async def test_active_verdict_for_excludes_static_denial(ec, ws, user):
    # A static policy denial (decided_by NULL) is not an actionable verdict.
    w = await ws.create_workspace(user["id"], "avf-static")
    await ec.record_static_denial(w["id"], "policy.com", 443)
    assert await ec.active_verdict_for(w["id"], "policy.com", 443) is None


async def test_active_verdict_for_no_port(ec, ws, user):
    # A port-less verdict (NULL port) matches a port-less lookup.
    w = await ws.create_workspace(user["id"], "avf-noport")
    d = await ec.create_request(w["id"], "noport.com")  # dest_port NULL
    await ec.decide(d["id"], DECISION_DENIED, user["id"], DURATION_FOREVER)
    row = await ec.active_verdict_for(w["id"], "noport.com", None)
    assert row is not None
    assert row["decision"] == DECISION_DENIED
    # a port-specific lookup does not match a port-less verdict
    assert await ec.active_verdict_for(w["id"], "noport.com", 443) is None


# -- prune: retention + row cap (#2303) --

RETENTION_DEFAULT = 30  # matches Settings.egress_consent_retention_days


async def _backdate(
    app_state, row_id, *, requested=None, decided=None, revoked=None
):
    """Shift a row's timestamps into the past (retention-window tests)."""
    sets, params = [], []
    if requested is not None:
        sets.append("requested_at = ?")
        params.append(requested)
    if decided is not None:
        sets.append("decided_at = ?")
        params.append(decided)
    if revoked is not None:
        sets.append("revoked_at = ?")
        params.append(revoked)
    params.append(row_id)
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            f"UPDATE egress_consent SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
            tuple(params),
        )


async def test_prune_deletes_old_terminal_rows(ec, ws, user, app_state):
    """Everything terminal-and-not-in-effect past the window goes; rows that
    are enforcement state (in-effect verdicts) stay even when old."""
    import time as _time

    now = _time.time()
    old = now - (RETENTION_DEFAULT + 5) * 86400
    w = await ws.create_workspace(user["id"], "prune-terminal")
    # static policy denial, old
    await ec.record_static_denial(w["id"], "static-old.com", 443)
    # expired (timed-out pending), old
    exp = await ec.create_request(w["id"], "expired.com", 443)
    await ec.expire_pending(exp["id"])
    # revoked forever verdict (undone -> never in effect), old
    rev = await ec.create_request(w["id"], "revoked.com", 443)
    await ec.decide(rev["id"], DECISION_ALLOWED, user["id"], DURATION_FOREVER)
    await ec.revoke(rev["id"], user["id"])
    # elapsed timed verdict, old
    elapsed = await ec.create_request(w["id"], "elapsed.com", 443)
    await ec.decide(elapsed["id"], DECISION_DENIED, user["id"], DURATION_5M)
    # stale pending (dead for sure after the retention window), old
    stale = await ec.create_request(w["id"], "stale-pending.com", 443)
    # in-effect verdicts, equally old: must be KEPT
    forever = await ec.create_request(w["id"], "forever.com", 443)
    await ec.decide(
        forever["id"], DECISION_ALLOWED, user["id"], DURATION_FOREVER
    )
    tilrestart = await ec.create_request(w["id"], "tilrestart.com", 443)
    await ec.decide(
        tilrestart["id"], DECISION_DENIED, user["id"], DURATION_TILRESTART
    )
    for r in (exp, rev, elapsed, stale, forever, tilrestart):
        await _backdate(
            app_state, r["id"], requested=old, decided=old, revoked=old
        )
    # the static row has no pending stage; backdate its single timestamp pair
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "UPDATE egress_consent SET requested_at = ?, decided_at = ?"
            " WHERE workspace_id = ? AND dest_host = 'static-old.com'",
            (old, old, w["id"]),
        )

    deleted = await ec.prune(now=now)
    assert deleted == 5  # static-old, expired, revoked, elapsed, stale
    hosts = {r["dest_host"] for r in await ec.list_requests(w["id"], None)}
    assert hosts == {"forever.com", "tilrestart.com"}


async def test_prune_keeps_timed_verdict_still_in_window(
    ec, ws, user, app_state
):
    """A retention window shorter than the verdict duration must not delete a
    verdict still in effect (fail-safe: enforcement beats tidiness)."""
    import time as _time

    app_state.state.settings.egress_consent_retention_days = 1
    now = _time.time()
    w = await ws.create_workspace(user["id"], "prune-in-window")
    req = await ec.create_request(w["id"], "week.com", 443)
    await ec.decide(req["id"], DECISION_ALLOWED, user["id"], DURATION_1W)
    # decided 2 days ago: past the 1-day retention, inside the 1-week window
    await _backdate(app_state, req["id"], decided=now - 2 * 86400)

    assert await ec.prune(now=now) == 0
    row = await ec.get_request(req["id"])
    assert row is not None and row["decision"] == DECISION_ALLOWED


async def test_prune_fresh_rows_untouched(ec, ws, user):
    """Rows inside the window are never candidates."""
    w = await ws.create_workspace(user["id"], "prune-fresh")
    await ec.record_static_denial(w["id"], "fresh-static.com", 443)
    assert await ec.prune() == 0
    # row still there: the dedup index is still saturated
    assert (
        await ec.record_static_denial(w["id"], "fresh-static.com", 443) is None
    )


async def test_prune_disabled_when_both_zero(ec, ws, user, app_state):
    app_state.state.settings.egress_consent_retention_days = 0
    app_state.state.settings.egress_consent_row_cap = 0
    w = await ws.create_workspace(user["id"], "prune-off")
    await ec.record_static_denial(w["id"], "kept.com", 443)
    assert await ec.prune() == 0
    rows = await ec.list_requests(w["id"], None)
    assert len(rows) == 1 and rows[0]["dest_host"] == "kept.com"


async def test_prune_row_cap_trims_oldest_eligible(ec, ws, user, app_state):
    """Over-cap workspaces keep the newest rows + every in-effect verdict;
    live pending rows are never cap-pruned."""
    import time as _time

    app_state.state.settings.egress_consent_row_cap = 3
    now = _time.time()
    w = await ws.create_workspace(user["id"], "prune-cap")
    # 3 eligible elapsed verdicts, oldest first
    elapsed_hosts = ["old1.com", "old2.com", "old3.com", "new1.com"]
    for i, host in enumerate(elapsed_hosts):
        r = await ec.create_request(w["id"], host, 443)
        await ec.decide(r["id"], DECISION_DENIED, user["id"], DURATION_5M)
        await _backdate(app_state, r["id"], decided=now - 1000 + i * 10)
    # in-effect forever allow + a live pending: exempt from the cap
    keep = await ec.create_request(w["id"], "keep.com", 443)
    await ec.decide(keep["id"], DECISION_ALLOWED, user["id"], DURATION_FOREVER)
    live = await ec.create_request(w["id"], "live-pending.com", 443)
    assert await ec.count_pending(w["id"]) == 1  # only the live one

    deleted = await ec.prune(now=now)
    # total 6 rows, cap 3 -> 3 over: the 3 oldest eligible go (old1..old3)
    assert deleted == 3
    hosts = {r["dest_host"] for r in await ec.list_requests(w["id"], None)}
    assert hosts == {"new1.com", "keep.com", "live-pending.com"}
    assert await ec.get_request(live["id"]) is not None


async def test_prune_cap_never_drops_in_effect_rows(ec, ws, user, app_state):
    """Even far over cap, in-effect verdicts survive (enforcement state)."""
    app_state.state.settings.egress_consent_row_cap = 1
    w = await ws.create_workspace(user["id"], "prune-cap-keep")
    a = await ec.create_request(w["id"], "a.com", 443)
    await ec.decide(a["id"], DECISION_ALLOWED, user["id"], DURATION_FOREVER)
    d = await ec.create_request(w["id"], "d.com", 443)
    await ec.decide(d["id"], DECISION_DENIED, user["id"], DURATION_FOREVER)

    await ec.prune()
    hosts = {r["dest_host"] for r in await ec.list_requests(w["id"], None)}
    assert hosts == {"a.com", "d.com"}


async def test_prune_retention_zero_cap_active(ec, ws, user, app_state):
    """Retention off, cap on: only the cap pass runs."""
    import time as _time

    app_state.state.settings.egress_consent_retention_days = 0
    app_state.state.settings.egress_consent_row_cap = 2
    now = _time.time()
    w = await ws.create_workspace(user["id"], "prune-cap-only")
    kept = []
    for i, host in enumerate(["x.com", "y.com", "z.com"]):
        r = await ec.create_request(w["id"], host, 443)
        await ec.decide(r["id"], DECISION_ALLOWED, user["id"], DURATION_5M)
        await _backdate(app_state, r["id"], decided=now - 1000 + i * 10)
        kept.append(r["id"])
    assert await ec.prune(now=now) == 1  # only the oldest exceeds the cap
    hosts = {r["dest_host"] for r in await ec.list_requests(w["id"], None)}
    assert hosts == {"y.com", "z.com"}


async def test_prune_multi_chunk_delete(ec, ws, user, app_state):
    """The chunked _delete_ids path (>100 stale rows) deletes them all."""
    import time as _time

    now = _time.time()
    old = now - (RETENTION_DEFAULT + 5) * 86400
    w = await ws.create_workspace(user["id"], "prune-chunks")
    # 105 distinct static denials (dedup is per host), all past retention
    for i in range(105):
        await ec.record_static_denial(w["id"], f"host{i}.example.com", 443)
    async with app_state.state.db.transaction() as conn:
        await conn.execute(
            "UPDATE egress_consent SET requested_at = ?, decided_at = ?"
            " WHERE workspace_id = ?",
            (old, old, w["id"]),
        )

    assert await ec.prune(now=now) == 105  # two chunks (100 + 5)
    assert await ec.list_requests(w["id"], None) == []


async def test_prune_pending_toctou_guard(ec, ws, user, app_state):
    """A pending row snapshotted as prunable but decided before the DELETE
    must survive: the delete re-checks decision='pending' (TOCTOU guard)."""
    import time as _time

    now = _time.time()
    old = now - (RETENTION_DEFAULT + 5) * 86400
    w = await ws.create_workspace(user["id"], "prune-toctou")
    stale = await ec.create_request(w["id"], "decided-late.com", 443)
    await _backdate(app_state, stale["id"], requested=old)
    # Simulate the race: the retention snapshot already happened (the row
    # qualified as an old pending), and a decider lands a forever allow
    # before the sweep's DELETE runs.
    await ec.decide(
        stale["id"], DECISION_ALLOWED, user["id"], DURATION_FOREVER
    )
    # ... but the eligibility snapshot had it as pending; the guarded delete
    # re-checks and skips it:
    removed = await ec._delete_ids(
        [stale["id"]], require_decision=DECISION_PENDING
    )
    assert removed == 0
    row = await ec.get_request(stale["id"])
    assert row is not None and row["decision"] == DECISION_ALLOWED


@pytest.mark.asyncio
class TestPruneBranchGaps2834:
    async def test_prune_row_cap_zero_skips_cap_prune(self, ec, ws, user):
        # row_cap=0 disables the cap half (retention-only pruning): rows
        # past retention still go, nothing else does.
        ws_row = await ws.create_workspace(user["id"], "prune-nocap")
        import time as time_mod

        await ec.create_request(ws_row["id"], "9.9.9.9", 443)
        # Nothing is past retention; a zero cap must not delete anything.
        ec.app.state.settings.egress_consent_retention_days = 30
        ec.app.state.settings.egress_consent_row_cap = 0
        assert await ec.prune(now=time_mod.time()) == 0
