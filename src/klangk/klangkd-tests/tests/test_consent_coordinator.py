"""Tests for :mod:`klangk.consent.coordinator` -- the synchronous hold/resolve
coordinator (#2311) -- and the egress-sidecar WebSocket endpoint that drives it.

The coordinator gate-checks each blocked egress (hold iff interactive + a live
decider, else static deny), holds the request in-process (a Future) until a
verdict (#2244 ``resolve``), a timeout, or shutdown, and fail-closes throughout.
"""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect

from klangk import auth
from klangk.consent.coordinator import ConsentCoordinator

FULL_WS = "aaaa1111bbbb-cccc-dddd-eeee-ffffffffffff"


def request(req_id="rid-1", host="1.2.3.4", port=443):
    return {
        "id": req_id,
        "workspace_id": FULL_WS,
        "dest_host": host,
        "dest_port": port,
        "decision": "pending",
    }


def _app(
    *,
    timeout: float = 30.0,
    rate_limit: int = 50,
    count_pending: int = 0,
    request=None,
    egress_mode: str = "interactive",
    workspace_exists: bool = True,
    has_decider: bool = True,
    decide_row=None,
    pending_rows=None,
    active_rows=None,
    allowed_domains=None,
    rejected_domains=None,
):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        egress_consent_timeout=timeout,
        egress_consent_rate_limit=rate_limit,
    )
    egress_consent = AsyncMock()
    egress_consent.count_pending = AsyncMock(return_value=count_pending)
    egress_consent.create_request = AsyncMock(return_value=request)
    egress_consent.record_static_denial = AsyncMock(return_value=_denial())
    egress_consent.record_static_allow = AsyncMock(return_value=_allow())
    egress_consent.decide = AsyncMock(return_value=decide_row)
    egress_consent.expire_pending = AsyncMock(return_value=True)
    egress_consent.list_requests = AsyncMock(return_value=pending_rows or [])
    egress_consent.list_active = AsyncMock(return_value=active_rows or [])
    # #2332: the pause path's "respect existing verdict" lookup. Default None
    # (no in-effect verdict) so the existing not-paused behavior is unchanged.
    egress_consent.active_verdict_for = AsyncMock(return_value=None)
    workspaces = AsyncMock()
    workspaces.get_workspace = AsyncMock(
        return_value=(
            {
                "egress_mode": egress_mode,
                "consent_paused_until": None,
                "allowed_domains": allowed_domains,
                "rejected_domains": rejected_domains,
            }
            if workspace_exists
            else None
        )
    )
    workspaces.add_allowed_domain = AsyncMock(return_value=True)
    workspaces.add_rejected_domain = AsyncMock(return_value=True)
    workspaces.remove_allowed_domain = AsyncMock(return_value=True)
    workspaces.remove_rejected_domain = AsyncMock(return_value=True)
    # #2332: consent-pause state. Defaults: not paused (None), set succeeds.
    workspaces.get_consent_pause = AsyncMock(return_value=None)
    workspaces.set_consent_pause = AsyncMock(return_value=True)
    app.state.model = types.SimpleNamespace(
        egress_consent=egress_consent, workspaces=workspaces
    )
    app.state.consent_deciders = types.SimpleNamespace(
        has_decider=lambda workspace_id: has_decider,
        broadcast=Mock(return_value=0),
    )
    # #2339: the revoke path pushes a drop-rule to a workspace's sidecar via
    # this registry. Default send_drop -> None (no live sidecar).
    app.state.sidecar_connections = types.SimpleNamespace(
        send_drop=Mock(return_value=None)
    )
    return app


def _denial():
    return {
        "id": "sid",
        "workspace_id": FULL_WS,
        "dest_host": "1.2.3.4",
        "dest_port": 443,
        "decision": "denied",
        "decided_by": None,
    }


def _allow():
    """A recorded allow-mode allow row (mirrors :func:`_denial`) (#2406)."""
    return {
        "id": "aid",
        "workspace_id": FULL_WS,
        "dest_host": "1.2.3.4",
        "dest_port": 443,
        "decision": "allowed",
        "decided_by": None,
    }


def _active_row(req_id="rid-1", decision="allowed", host="1.2.3.4"):
    """An in-effect verdict row for revoke tests (#2339)."""
    return {
        "id": req_id,
        "workspace_id": FULL_WS,
        "dest_host": host,
        "dest_port": 443,
        "decision": decision,
        "duration": "tilrestart",
    }


class TestConsentCoordinatorRevoke:
    async def test_revoke_no_sidecar_marks_revoked(self):
        # No live sidecar -> nothing to drop -> mark revoked + refresh the view.
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.egress_consent.revoke.assert_awaited_once_with(
            "rid-1", "a@x"
        )
        app.state.sidecar_connections.send_drop.assert_called_once_with(
            FULL_WS, "1.2.3.4", "allowed"
        )

    async def test_revoke_sidecar_acked_marks_revoked(self):
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(True)
        app.state.sidecar_connections.send_drop = Mock(return_value=fut)
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True

    async def test_revoke_sidecar_no_ack_returns_false(self):
        # A connected sidecar that doesn't ack ok -> leave enforced (fail-closed).
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()
        )
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(False)
        app.state.sidecar_connections.send_drop = Mock(return_value=fut)
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is False
        app.state.model.egress_consent.revoke.assert_not_awaited()

    async def test_revoke_sidecar_ack_timeout_returns_false(self, monkeypatch):
        # A sidecar that never acks -> wait_for times out -> leave enforced.
        import klangk.consent.coordinator as cc

        monkeypatch.setattr(cc, "REVOKE_ACK_TIMEOUT", 0.05)
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()
        )
        # a Future that never resolves
        app.state.sidecar_connections.send_drop = Mock(
            return_value=asyncio.get_running_loop().create_future()
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is False
        app.state.model.egress_consent.revoke.assert_not_awaited()

    async def test_revoke_unknown_request_returns_false(self):
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=None
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("nope", "a@x") is False
        app.state.sidecar_connections.send_drop.assert_not_called()

    async def test_revoke_not_active_verdict_returns_false(self):
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row(decision="pending")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is False
        app.state.sidecar_connections.send_drop.assert_not_called()

    async def test_revoke_already_revoked_row_is_idempotent(self):
        # #3083, the wider window: the concurrent winner committed before
        # our first read, so the row already reads revoked -> idempotent
        # success, not a misleading "revoke failed" ack. The winner already
        # dropped the rule + retracted, so no second drop is sent.
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        app.state.model.egress_consent.revoke = AsyncMock(return_value=None)
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.sidecar_connections.send_drop.assert_not_called()
        app.state.model.egress_consent.revoke.assert_not_awaited()

    async def test_revoke_already_revoked_outside_scope_returns_false(self):
        # A revoked row OUTSIDE the decider's workspace stays a refusal.
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        app.state.model.egress_consent.revoke = AsyncMock(return_value=None)
        coord = ConsentCoordinator(app)
        assert (
            await coord.revoke("rid-1", "a@x", decider_workspace="other")
            is False
        )
        app.state.sidecar_connections.send_drop.assert_not_called()

    async def test_revoke_outside_decider_workspace_returns_false(self):
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()
        )
        coord = ConsentCoordinator(app)
        assert (
            await coord.revoke("rid-1", "a@x", decider_workspace="other")
            is False
        )
        app.state.sidecar_connections.send_drop.assert_not_called()

    async def test_revoke_model_returns_none_returns_false(self):
        # race: the row changed under us -> model.revoke returns None AND the
        # re-read shows the row is NOT revoked (e.g. it flipped back to
        # pending) -> failure (the row may still be enforced).
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()
        )
        app.state.model.egress_consent.revoke = AsyncMock(return_value=None)
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is False

    async def test_revoke_losing_race_with_duplicate_is_idempotent(self):
        # #3083: two deciders revoke the same verdict concurrently; the
        # loser's model.revoke UPDATE matches nothing (the winner flipped the
        # row) -> re-read shows the row already revoked -> idempotent success,
        # not a misleading "revoke failed -- still in effect" ack.
        app = _app()
        active = _active_row()
        revoked = _active_row(decision="revoked")
        app.state.model.egress_consent.get_request = AsyncMock(
            side_effect=[active, revoked]
        )
        app.state.model.egress_consent.revoke = AsyncMock(return_value=None)
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        assert app.state.model.egress_consent.get_request.await_count == 2

    async def test_revoke_forever_allow_retracts_allowed_domains(self):
        # #2370: revoking a forever allow retracts the durable allowed_domains
        # entry (host:port) so it does not re-apply on the next sidecar restart.
        # The in-memory rules were cleared by the drop (mocked None here).
        app = _app()
        row = _active_row(decision="allowed")
        row["duration"] = "forever"
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )
        app.state.model.workspaces.remove_rejected_domain.assert_not_awaited()

    async def test_revoke_forever_deny_retracts_rejected_domains(self):
        # The deny mirror: revoking a forever deny retracts rejected_domains.
        app = _app()
        row = _active_row(decision="denied")
        row["duration"] = "forever"
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_rejected_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )
        app.state.model.workspaces.remove_allowed_domain.assert_not_awaited()

    async def test_revoke_forever_deny_portless_retracts_bare_host(self):
        # A port-less forever deny was persisted as a bare host; retract that.
        app = _app()
        row = _active_row(decision="denied")
        row["duration"] = "forever"
        row["dest_port"] = 0
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_rejected_domain.assert_awaited_once_with(
            FULL_WS,
            "1.2.3.4",  # bare host (no port)
        )

    async def test_revoke_forever_allow_portless_skips_retract(self):
        # A port-less forever allow was never persisted (resolve skips it), so
        # revoke has nothing to retract -- but the revoke still succeeds.
        app = _app()
        row = _active_row(decision="allowed")
        row["duration"] = "forever"
        row["dest_port"] = 0
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_not_awaited()
        app.state.model.workspaces.remove_rejected_domain.assert_not_awaited()

    async def test_revoke_forever_retract_failure_does_not_break_revoke(self):
        # Best-effort: a persistence failure is logged + swallowed; the row is
        # already revoked, so the revoke still returns True.
        app = _app()
        row = _active_row(decision="allowed")
        row["duration"] = "forever"
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        app.state.model.workspaces.remove_allowed_domain = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True

    async def test_revoke_non_forever_does_not_retract(self):
        # A tilrestart/once verdict has no durable entry -- nothing to retract.
        app = _app()
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=_active_row()  # duration tilrestart
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_not_awaited()
        app.state.model.workspaces.remove_rejected_domain.assert_not_awaited()

    async def test_revoke_forever_missing_host_skips_retract(self):
        # Defensive: a forever row missing dest_host retracts nothing, but the
        # revoke still succeeds (the row is revoked; only durability is skipped).
        app = _app()
        row = _active_row(decision="allowed")
        row["duration"] = "forever"
        row["dest_host"] = None
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_not_awaited()

    def _forever_app(self, decision="allowed", survivors=()):
        """An app whose get_request returns an active ``forever`` row whose
        revoke succeeds, with ``survivors`` as the in-effect rows
        ``list_active`` reports (#3083 shared-entry tests)."""
        app = _app(active_rows=list(survivors))
        row = _active_row(decision=decision)
        row["duration"] = "forever"
        app.state.model.egress_consent.get_request = AsyncMock(
            side_effect=[row, row]
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        return app, row

    async def test_revoke_forever_keeps_entry_shared_with_survivor(self):
        # #3083: two forever allows for the same host:port share ONE
        # allowed_domains entry; revoking one row must NOT retract it while
        # the survivor is still in effect.
        survivor = _active_row(req_id="rid-2")
        survivor["duration"] = "forever"
        app, _row = self._forever_app(survivors=[survivor])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_not_awaited()

    async def test_revoke_forever_retracts_when_survivor_differs(self):
        # The survivor is for a different port -> a different durable entry
        # -> the revoked row's entry is still retracted.
        survivor = _active_row(req_id="rid-2")
        survivor["duration"] = "forever"
        survivor["dest_port"] = 8443
        app, _row = self._forever_app(survivors=[survivor])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )

    async def test_revoke_forever_retracts_when_survivor_not_forever(self):
        # A tilrestart survivor never owned the durable entry (only forever
        # verdicts persist one) -> the revoked row's entry is retracted.
        survivor = _active_row(req_id="rid-2")  # duration tilrestart
        app, _row = self._forever_app(survivors=[survivor])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )

    async def test_revoke_forever_retracts_when_survivor_other_decision(self):
        # A forever DENY survivor maps to rejected_domains, a different list
        # -> the revoked allow's entry is still retracted.
        survivor = _active_row(req_id="rid-2", decision="denied")
        survivor["duration"] = "forever"
        app, _row = self._forever_app(survivors=[survivor])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )

    async def test_revoke_forever_deny_shared_entry_kept(self):
        # The deny mirror of the shared-entry guard.
        survivor = _active_row(req_id="rid-2", decision="denied")
        survivor["duration"] = "forever"
        app, _row = self._forever_app(decision="denied", survivors=[survivor])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_rejected_domain.assert_not_awaited()

    async def test_revoke_forever_ignores_survivor_with_same_id(self):
        # Defense-in-depth: a survivor carrying the SAME id as the revoked
        # row (a stale concurrent read) is filtered out -- the entry is
        # retracted, not kept alive by the row being revoked.
        stale = _active_row(req_id="rid-1")
        stale["duration"] = "forever"
        app, _row = self._forever_app(survivors=[stale])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )

    async def test_revoke_forever_ignores_portless_allow_survivor(self):
        # A port-less forever-allow survivor maps to NO durable entry (it
        # was never persisted), so it cannot keep the revoked row's entry
        # alive -- the entry is retracted.
        survivor = _active_row(req_id="rid-2")
        survivor["duration"] = "forever"
        survivor["dest_port"] = 0
        app, _row = self._forever_app(survivors=[survivor])
        coord = ConsentCoordinator(app)
        assert await coord.revoke("rid-1", "a@x") is True
        app.state.model.workspaces.remove_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )


class TestConsentCoordinatorGate:
    async def test_no_decider_records_static_and_denies_at_once(self):
        app = _app(has_decider=False)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = fut.result()
        assert verdict == {"decision": "deny", "reason": "static"}
        app.state.model.egress_consent.record_static_denial.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 443
        )
        app.state.model.egress_consent.create_request.assert_not_awaited()
        assert coord._holds == {}

    async def test_not_opted_in_denies_as_static(self):
        app = _app(egress_mode="static", has_decider=True)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result()["reason"] == "static"

    async def test_allow_mode_records_and_allows_at_once(self):
        # #2406: egress_mode == allow is default-permit. It records the
        # off-list destination (logging) and allows at once -- no hold, no
        # prompt -- behaving as if an internal always-allow decider were
        # registered. rejected_domains is enforced earlier at the sidecar
        # DNS layer, not here.
        app = _app(egress_mode="allow", has_decider=False)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = fut.result()
        assert verdict["decision"] == "allow"
        assert verdict["reason"] == "allow_mode"
        # tilrestart so the sidecar learns the IP for its lifetime (no
        # per-connection re-prompt).
        assert verdict["duration"] == "tilrestart"
        app.state.model.egress_consent.record_static_allow.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 443
        )
        app.state.model.egress_consent.create_request.assert_not_awaited()
        app.state.model.egress_consent.record_static_denial.assert_not_awaited()
        assert coord._holds == {}

    async def test_allow_mode_ignores_presence_of_decider(self):
        # A registered external decider is irrelevant to allow mode (allow
        # mode refuses deciders; it auto-allows). Even with a live decider
        # present, the gate short-circuits to allow + record, never holding.
        app = _app(egress_mode="allow", has_decider=True, request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = fut.result()
        assert verdict["decision"] == "allow"
        assert verdict["reason"] == "allow_mode"
        app.state.model.egress_consent.create_request.assert_not_awaited()
        assert coord._holds == {}

    async def test_rate_limited_denies_without_hold(self):
        app = _app(count_pending=50, rate_limit=50, request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "rate_limited"}
        app.state.model.egress_consent.create_request.assert_not_awaited()
        assert coord._holds == {}

    async def test_duplicate_pending_denies(self):
        app = _app(request=None)  # create_request dedup -> None
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "duplicate"}
        assert coord._holds == {}

    async def test_interactive_with_decider_creates_hold(self):
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert not fut.done()  # held, awaiting verdict
        assert "rid-1" in coord._holds
        app.state.model.egress_consent.create_request.assert_awaited_once()

    async def test_hold_reads_the_workspace_row_once(self):
        # #3083: all three gates (allow mode, pause, interactivity) are
        # served by ONE get_workspace read -- previously the allow gate and
        # the interactivity predicate each fetched the row (and the pause
        # gate a third read via get_consent_pause).
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.model.workspaces.get_workspace.assert_awaited_once_with(
            FULL_WS
        )
        app.state.model.workspaces.get_consent_pause.assert_not_awaited()

    async def test_rate_limit_zero_means_unlimited(self):
        # #3083: 0 disables the cap (matching the retention knobs), NOT
        # "deny every hold" -- even with pending rows over any count.
        app = _app(count_pending=999, rate_limit=0, request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert not fut.done()  # held, not rate-limited
        assert "rid-1" in coord._holds

    async def test_hold_db_error_fail_closes_to_deny(self):
        # a DB/model failure during the static-deny recording must not crash the
        # caller or strand the hold -- hold() returns a resolved deny verdict.
        app = _app(egress_mode="static", has_decider=True)
        app.state.model.egress_consent.record_static_denial = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "error"}
        assert coord._holds == {}

    async def test_hold_interactive_db_error_fail_closes_to_deny(self):
        # same resilience on the interactive path (create_request raises).
        app = _app(request=request())
        app.state.model.egress_consent.create_request = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "error"}
        assert coord._holds == {}


class TestConsentCoordinatorFanout:
    async def test_hold_broadcasts_egress_request_to_deciders(self):
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.assert_called_once()
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert ws_arg == FULL_WS
        assert frame["type"] == "egress_request"
        assert frame["workspace_id"] == FULL_WS
        assert frame["request"]["id"] == "rid-1"

    async def test_resolve_broadcasts_egress_resolved(self):
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        await coord.resolve("rid-1", "allowed", "a@x")
        # the egress_resolved frame is among the broadcasts (resolve also pushes
        # a refreshed egress_rules frame, #2335 slice A)
        frames = [
            c.args[1]
            for c in app.state.consent_deciders.broadcast.call_args_list
        ]
        assert {
            "type": "egress_resolved",
            "workspace_id": FULL_WS,
            "request_id": "rid-1",
            "decision": "allowed",
        } in frames

    async def test_resolve_broadcasts_rules_after_verdict(self):
        # A verdict landing refreshes the deciders' in-effect rules view
        # (#2335 slice A).
        row = request()
        row["decision"] = "allowed"
        row["duration"] = (
            "tilrestart"  # a real in-effect duration (once would be excluded)
        )
        app = _app(
            request=request(),
            decide_row=row,
            active_rows=[row],
            allowed_domains=["static.example.com"],
        )
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        await coord.resolve("rid-1", "allowed", "a@x")
        app.state.model.egress_consent.list_active.assert_awaited_once_with(
            FULL_WS
        )
        frames = [
            c.args[1]
            for c in app.state.consent_deciders.broadcast.call_args_list
        ]
        rules = [f for f in frames if f["type"] == "egress_rules"]
        assert len(rules) == 1
        assert rules[0]["workspace_id"] == FULL_WS
        assert rules[0]["allow_list"] == ["static.example.com"]
        assert [r["dest_host"] for r in rules[0]["allowed"]] == ["1.2.3.4"]
        assert rules[0]["denied"] == []
        assert rules[0]["paused"] is None

    async def test_resolve_rejects_verdict_outside_decider_workspace(self):
        # defense-in-depth: a workspace-scoped decider may not decide another
        # workspace's request; the hold stays for a scoped decider.
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "a@x", decider_workspace="other-ws"
        )
        assert verdict is None
        assert "rid-1" in coord._holds  # hold untouched
        app.state.model.egress_consent.decide.assert_not_awaited()

    async def test_timeout_broadcasts_expired(self):
        app = _app(timeout=0.05, request=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        await asyncio.sleep(0.12)
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert frame["type"] == "egress_resolved"
        assert frame["decision"] == "expired"

    async def test_broadcast_rules_swallows_refresh_failure(self):
        # A rules-frame DB failure during the post-verdict refresh must not
        # break resolve (best-effort: the sidecar already has its verdict).
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.model.workspaces.get_workspace = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        # must not raise -- the verdict path is unaffected by the refresh
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict["decision"] == "allow"

    async def test_snapshot_replays_pending_requests(self):
        rows = [request("a"), request("b")]
        app = _app(pending_rows=rows)
        coord = ConsentCoordinator(app)
        # Only DB-pending rows that are also currently held are replayed (a
        # row popped from _holds mid-resolve must not re-linger on a
        # reconnect, #2345). Seed _holds with both so they're considered held.
        coord._holds.update({"a": {}, "b": {}})
        frames = await coord.snapshot(FULL_WS)
        assert [f["request"]["id"] for f in frames] == ["a", "b"]
        assert all(f["type"] == "egress_request" for f in frames)
        app.state.model.egress_consent.list_requests.assert_awaited_once_with(
            FULL_WS, decision="pending"
        )

    async def test_snapshot_excludes_resolved_rows_not_in_holds(self):
        # A row still pending in the DB but popped from _holds (resolve/timeout
        # in flight -- its egress_resolved broadcast may be lost on a dead
        # connection) must NOT be replayed to a reconnecting decider, or it
        # lingers as already-resolved (#2345 e2e flake). _holds.pop is
        # synchronous in resolve, so the membership check is race-free.
        rows = [request("a"), request("b")]
        app = _app(pending_rows=rows)
        coord = ConsentCoordinator(app)
        coord._holds.update({"a": {}})  # only "a" still held; "b" resolved
        frames = await coord.snapshot(FULL_WS)
        assert [f["request"]["id"] for f in frames] == ["a"]


class TestConsentCoordinatorRules:
    async def test_rules_frame_groups_active_and_allow_list(self):
        allowed = {
            "id": "a1",
            "workspace_id": FULL_WS,
            "dest_host": "allow.com",
            "dest_port": 443,
            "decision": "allowed",
            "duration": "tilrestart",
        }
        denied = {
            "id": "d1",
            "workspace_id": FULL_WS,
            "dest_host": "deny.com",
            "dest_port": 443,
            "decision": "denied",
            "duration": "forever",
        }
        app = _app(
            active_rows=[allowed, denied],
            allowed_domains=["static.example.com"],
            rejected_domains=["bad.example.com"],
        )
        coord = ConsentCoordinator(app)
        frame = await coord.rules_frame(FULL_WS)
        assert frame == {
            "type": "egress_rules",
            "workspace_id": FULL_WS,
            "allow_list": ["static.example.com"],
            "reject_list": ["bad.example.com"],
            "allowed": [allowed],
            "denied": [denied],
            "paused": None,
        }
        app.state.model.egress_consent.list_active.assert_awaited_once_with(
            FULL_WS
        )

    async def test_rules_frame_none_for_missing_workspace(self):
        app = _app(workspace_exists=False)
        coord = ConsentCoordinator(app)
        assert await coord.rules_frame(FULL_WS) is None
        app.state.model.egress_consent.list_active.assert_not_awaited()

    async def test_rules_frame_includes_active_pause_window(self):
        # #2332: a live pause window is surfaced in the egress_rules frame so
        # deciders render the pause indicator + remaining time. The window
        # comes off the fetched workspace row (#3083 single-read design).
        import time

        app = _app()
        until = time.time() + 900
        app.state.model.workspaces.get_workspace = AsyncMock(
            return_value={
                "egress_mode": "interactive",
                "consent_paused_until": until,
            }
        )
        coord = ConsentCoordinator(app)
        frame = await coord.rules_frame(FULL_WS)
        assert frame["paused"] == {"paused": True, "until": until}
        app.state.model.workspaces.get_consent_pause.assert_not_awaited()

    async def test_rules_frame_expired_pause_is_none(self):
        # A pause whose window has elapsed reads as not-paused (self-expiry).
        app = _app()
        app.state.model.workspaces.get_workspace = AsyncMock(
            return_value={
                "egress_mode": "interactive",
                "consent_paused_until": 1.0,  # far in the past
            }
        )
        coord = ConsentCoordinator(app)
        frame = await coord.rules_frame(FULL_WS)
        assert frame["paused"] is None


class TestConsentCoordinatorPause:
    async def test_pause_sets_window_and_broadcasts(self):
        # pause("15m") -> set_consent_pause(now+900) + a refreshed rules frame.
        import time

        before = time.time()
        app = _app()
        coord = ConsentCoordinator(app)
        result = await coord.pause(FULL_WS, "15m")
        after = time.time()
        assert result["ok"] is True
        assert before + 900 <= result["until"] <= after + 900
        app.state.model.workspaces.set_consent_pause.assert_awaited_once()
        args = app.state.model.workspaces.set_consent_pause.call_args.args
        assert args[0] == FULL_WS
        assert before + 900 <= args[1] <= after + 900
        # a refreshed egress_rules frame was broadcast to the deciders
        frames = [
            c.args[1]
            for c in app.state.consent_deciders.broadcast.call_args_list
        ]
        assert any(f["type"] == "egress_rules" for f in frames)

    async def test_pause_1d_sets_a_day_window(self):
        # The TUI's largest window (1d) maps to 86400s.
        import time

        before = time.time()
        app = _app()
        coord = ConsentCoordinator(app)
        result = await coord.pause(FULL_WS, "1d")
        assert result["ok"] is True
        assert before + 86400 <= result["until"] <= before + 86400 + 5

    async def test_pause_unknown_duration_nacks(self):
        app = _app()
        coord = ConsentCoordinator(app)
        result = await coord.pause(FULL_WS, "2h")
        assert result == {"ok": False, "until": None}
        app.state.model.workspaces.set_consent_pause.assert_not_awaited()
        app.state.consent_deciders.broadcast.assert_not_called()

    async def test_pause_missing_workspace_nacks(self):
        # set_consent_pause returns False (workspace missing) -> nack, no
        # broadcast.
        app = _app()
        app.state.model.workspaces.set_consent_pause = AsyncMock(
            return_value=False
        )
        coord = ConsentCoordinator(app)
        result = await coord.pause(FULL_WS, "1h")
        assert result == {"ok": False, "until": None}
        app.state.consent_deciders.broadcast.assert_not_called()

    async def test_pause_refuses_outside_interactive_mode(self):
        # #3086 review: the pause is an interactive-mode affordance -- a
        # decider connected before a switch to static must not store a new
        # inert window (the hold gate would ignore it regardless).
        app = _app(egress_mode="static")
        coord = ConsentCoordinator(app)
        result = await coord.pause(FULL_WS, "1h")
        assert result == {"ok": False, "until": None}
        app.state.model.workspaces.set_consent_pause.assert_not_awaited()
        app.state.consent_deciders.broadcast.assert_not_called()

    async def test_unpause_clears_and_broadcasts(self):
        app = _app()
        coord = ConsentCoordinator(app)
        result = await coord.unpause(FULL_WS)
        assert result == {"ok": True}
        app.state.model.workspaces.set_consent_pause.assert_awaited_once_with(
            FULL_WS, None
        )
        frames = [
            c.args[1]
            for c in app.state.consent_deciders.broadcast.call_args_list
        ]
        assert any(f["type"] == "egress_rules" for f in frames)

    async def test_unpause_missing_workspace(self):
        app = _app()
        app.state.model.workspaces.set_consent_pause = AsyncMock(
            return_value=False
        )
        coord = ConsentCoordinator(app)
        assert await coord.unpause(FULL_WS) == {"ok": False}
        app.state.consent_deciders.broadcast.assert_not_called()


class TestConsentCoordinatorHoldPaused:
    def _paused_app(self, *, egress_mode="interactive", **kwargs):
        """An app whose workspace row carries a live pause window (#2332;
        since #3083 hold() reads the pause off the workspace row, not via
        get_consent_pause -- the row's egress_mode carries the #3080
        interactive-only pause gate)."""
        import time

        app = _app(**kwargs)
        app.state.model.workspaces.get_workspace = AsyncMock(
            return_value={
                "egress_mode": egress_mode,
                "consent_paused_until": time.time() + 600,
            }
        )
        return app

    async def test_paused_auto_allows_no_hold(self):
        # #2332: while paused, a destination with no in-effect verdict is
        # auto-allowed at once -- no hold, no pending row, no static denial.
        app = self._paused_app(request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = fut.result()
        assert verdict["decision"] == "allow"
        assert verdict["reason"] == "paused"
        app.state.model.egress_consent.create_request.assert_not_awaited()
        app.state.model.egress_consent.record_static_denial.assert_not_awaited()
        assert coord._holds == {}

    async def test_paused_respects_recorded_deny(self):
        # A recorded in-effect deny still blocks while paused (the pause does
        # not override existing verdicts).
        app = self._paused_app(request=request())
        app.state.model.egress_consent.active_verdict_for = AsyncMock(
            return_value={
                "id": "rid-1",
                "workspace_id": FULL_WS,
                "dest_host": "1.2.3.4",
                "dest_port": 443,
                "decision": "denied",
            }
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = fut.result()
        assert verdict["decision"] == "deny"
        assert verdict["reason"] == "paused_deny"
        app.state.model.egress_consent.active_verdict_for.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 443
        )

    async def test_paused_allow_verdict_still_allows(self):
        # An in-effect ALLOW verdict is respected too (allow either way).
        app = self._paused_app(request=request())
        app.state.model.egress_consent.active_verdict_for = AsyncMock(
            return_value={
                "id": "rid-1",
                "workspace_id": FULL_WS,
                "dest_host": "1.2.3.4",
                "dest_port": 443,
                "decision": "allowed",
            }
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result()["decision"] == "allow"

    async def test_stale_pause_in_static_mode_fails_closed(self):
        # #3080: a pause set while interactive must not auto-allow after
        # the workspace is switched to static -- the static denial wins
        # (the pause is an interactive-mode affordance only). The row
        # carries a LIVE pause window alongside the static mode.
        app = self._paused_app(
            egress_mode="static", request=request(), has_decider=True
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "static"}
        app.state.model.egress_consent.record_static_denial.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 443
        )
        # the pause-path lookup was never consulted
        app.state.model.egress_consent.active_verdict_for.assert_not_awaited()

    async def test_paused_survives_decider_disconnect(self):
        # The decider-liveness half of interactivity is deliberately NOT
        # required for the pause (#3080 note): a decider pauses prompting
        # and walks away -- the window keeps auto-allowing (the mode is
        # still interactive) instead of flipping every off-list
        # destination to static denials.
        app = self._paused_app(request=request(), has_decider=False)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result()["reason"] == "paused"
        app.state.model.egress_consent.record_static_denial.assert_not_awaited()

    async def test_expired_pause_falls_through_to_normal_gate(self):
        # A pause whose window elapsed is not paused -> the normal interactive
        # gate applies (here: a decider is present -> hold).
        app = _app(request=request())
        app.state.model.workspaces.get_workspace = AsyncMock(
            return_value={
                "egress_mode": "interactive",
                "consent_paused_until": 1.0,  # past
            }
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert not fut.done()  # held for a decider, not auto-allowed
        assert "rid-1" in coord._holds
        # the pause-path lookup was NOT consulted (pause did not engage)
        app.state.model.egress_consent.active_verdict_for.assert_not_awaited()

    async def test_paused_db_error_fail_closes_to_deny(self):
        # A model failure in the pause path must not strand the hold -- the
        # outer guard fail-closes to deny.
        app = self._paused_app(request=request())
        app.state.model.egress_consent.active_verdict_for = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "error"}
        assert coord._holds == {}


class TestConsentCoordinatorResolve:
    async def test_resolve_allow_records_and_releases(self):
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve("rid-1", "allowed", "a@x", duration="1d")
        assert verdict == {
            "decision": "allow",
            "reason": "decided",
            "duration": "1d",
        }
        assert fut.result() == {
            "decision": "allow",
            "reason": "decided",
            "duration": "1d",
        }
        app.state.model.egress_consent.decide.assert_awaited_once_with(
            "rid-1", "allowed", "a@x", "1d"
        )
        assert coord._holds == {}

    async def test_resolve_deny_releases_deny(self):
        row = request()
        row["decision"] = "denied"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve("rid-1", "denied", "a@x", duration="15m")
        assert verdict == {
            "decision": "deny",
            "reason": "decided",
            "duration": "15m",
        }
        assert fut.result()["decision"] == "deny"

    async def test_resolve_unknown_returns_none(self):
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        assert await coord.resolve("nope", "allowed", "a@x") is None

    async def test_resolve_after_decide_returns_none_fail_closes(self):
        # decide() returns None (row no longer pending -- concurrent expiry):
        # the hold fail-closes to deny.
        app = _app(request=request(), decide_row=None)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict == {"decision": "deny", "reason": "gone"}
        assert fut.result()["decision"] == "deny"

    async def test_resolve_cancels_the_timeout(self):
        app = _app(timeout=0.05, request=request(), decide_row=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        await coord.resolve("rid-1", "allowed", "a@x")
        await asyncio.sleep(0.12)  # past the would-be timeout
        # timeout cancelled -> the row was NOT expired
        app.state.model.egress_consent.expire_pending.assert_not_awaited()
        assert fut.result()["decision"] == "allow"

    async def test_resolve_awaits_cancelled_timeout_task(self):
        # #3083: resolve cancels AND awaits the hold's timeout task before
        # moving on -- here the task already started and is parked in its
        # sleep, so the cancel lands inside its except arm (normal return).
        app = _app(request=request(), decide_row=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        await asyncio.sleep(0)  # let the timeout task take its first step
        task = coord._holds["rid-1"]["task"]
        await coord.resolve("rid-1", "allowed", "a@x")
        assert task.done()
        assert not task.cancelled()  # its except-CancelledError arm returned

    async def test_resolve_awaits_task_cancelled_before_it_started(self):
        # The timeout task never started (hold() ran without yielding to the
        # loop); the cancel kills the coroutine before its body engages, the
        # task ends CANCELLED, and resolve must swallow the CancelledError
        # from awaiting it (the arm of _cancel_hold_task's except).
        app = _app(request=request(), decide_row=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        task = coord._holds["rid-1"]["task"]
        assert not task.done()
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict["decision"] == "allow"
        assert task.done()
        assert task.cancelled()

    async def test_resolve_fail_closes_when_decide_raises(self):
        # decide() raising (DB error) must not orphan the Future -- the hold's
        # timeout is already cancelled, so resolve fail-closes to deny itself.
        app = _app(request=request())
        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict == {"decision": "deny", "reason": "error"}
        assert fut.done() and fut.result()["decision"] == "deny"
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert frame["type"] == "egress_resolved"
        assert frame["decision"] == "expired"
        assert coord._holds == {}  # hold gone, no orphan
        # #3081: the stranded row is expired best-effort, not left pending
        # invisible-but-counted against the workspace's pending cap.
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )

    async def test_resolve_expire_retry_recovers_transient_error(self):
        # #3081: decide() raises AND the first expire attempt fails
        # transiently -- the single retry lands, the row does not linger
        # pending for the retention window.
        app = _app(request=request())
        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=[RuntimeError("db locked"), True]
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict == {"decision": "deny", "reason": "error"}
        assert app.state.model.egress_consent.expire_pending.await_count == 2
        assert all(
            call.args == ("rid-1",)
            for call in app.state.model.egress_consent.expire_pending.await_args_list
        )
        assert fut.result()["decision"] == "deny"

    async def test_resolve_expire_double_failure_still_resolves_deny(self):
        # #3081: decide() raises AND both expire attempts fail -- the Future
        # still resolves deny (the expire runs after _finish_resolve, so its
        # failure can never hang the relay); the row falls back to the
        # startup reaper.
        app = _app(request=request())
        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict == {"decision": "deny", "reason": "error"}
        assert fut.done() and fut.result()["decision"] == "deny"
        assert app.state.model.egress_consent.expire_pending.await_count == 2

    async def test_resolve_error_arm_resolves_future_before_expire(self):
        # The stranded expire is awaited only AFTER the Future is resolved:
        # a cancellation delivered during the (best-effort) expire can no
        # longer hang the sidecar relay awaiting the Future.
        app = _app(request=request())
        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        fut_done_at_expire: list[bool] = []

        async def _expire(request_id):
            fut_done_at_expire.append(fut.done())

        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=_expire
        )
        verdict = await coord.resolve("rid-1", "allowed", "a@x")
        assert verdict == {"decision": "deny", "reason": "error"}
        assert fut_done_at_expire == [True]

    async def test_resolve_cancelled_during_decide_fail_closes_future(self):
        # #3089: a CancelledError delivered while decide() is in flight is
        # not an Exception -- without the BaseException arm it would escape
        # with the hold popped and its timeout cancelled, leaving nothing
        # able to resolve the Future: the sidecar relay awaits it (shielded,
        # no timeout of its own) forever. The Future must be fail-closed
        # deny + the stranded row expired, then the cancellation re-raised.
        app = _app(request=request())
        entered = asyncio.Event()
        fut_done_at_expire: list[bool] = []

        async def _hanging_decide(*args):
            entered.set()
            await asyncio.Event().wait()  # suspend until cancelled

        async def _expire(request_id):
            fut_done_at_expire.append(fut.done())

        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=_hanging_decide
        )
        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=_expire
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        task = asyncio.create_task(coord.resolve("rid-1", "allowed", "a@x"))
        await entered.wait()  # resolve is now suspended inside decide()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # the Future was fail-closed BEFORE the row expire ran, and the
        # cancellation still propagated
        assert fut_done_at_expire == [True]
        assert fut.result() == {
            "decision": "deny",
            "reason": "error",
            "duration": "once",
        }
        frames = [
            c.args[1]
            for c in app.state.consent_deciders.broadcast.call_args_list
        ]
        assert {
            "type": "egress_resolved",
            "workspace_id": FULL_WS,
            "request_id": "rid-1",
            "decision": "expired",
        } in frames
        assert coord._holds == {}  # no orphaned hold, no unresolvable Future

    async def test_resolve_cancelled_with_preset_future_skips_set_result(self):
        # The done-guard on the cancel path: a Future already resolved (a
        # racing timeout's set_result) keeps its first verdict; the cancel
        # arm only broadcasts + expires, then re-raises.
        app = _app(request=request())
        entered = asyncio.Event()

        async def _hanging_decide(*args):
            entered.set()
            await asyncio.Event().wait()

        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=_hanging_decide
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        fut.set_result(
            {"decision": "deny", "reason": "timeout", "duration": "once"}
        )
        task = asyncio.create_task(coord.resolve("rid-1", "allowed", "a@x"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fut.result()["reason"] == "timeout"  # not clobbered
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )

    async def test_resolve_cancelled_during_timeout_reap_fail_closes(self):
        # #3089, the other await past the pop: a cancellation landing while
        # the timeout task is being reaped (before decide() is even called)
        # must also fail-close the Future -- the hold is already popped and
        # its timeout cancelled, so nothing else could resolve it.
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        reaping = asyncio.Event()

        async def _hanging_reap(task):
            reaping.set()
            await asyncio.Event().wait()

        coord._cancel_hold_task = _hanging_reap
        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=AssertionError("decide must not run")
        )
        task = asyncio.create_task(coord.resolve("rid-1", "allowed", "a@x"))
        await reaping.wait()  # resolve is now suspended reaping the timeout
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fut.result() == {
            "decision": "deny",
            "reason": "error",
            "duration": "once",
        }
        app.state.model.egress_consent.decide.assert_not_awaited()
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )
        assert coord._holds == {}

    async def test_resolve_cancelled_at_reap_composes_guard_and_rescue(self):
        # The real guard->rescue composition, no monkeypatch: the caller is
        # cancelled at the reap await while the timeout task ends NOT
        # cancelled -> the swallow-guard in _cancel_hold_task re-raises ->
        # resolve's rescue fail-closes the popped hold.
        import contextlib

        app = _app(request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        orig_task = coord._holds["rid-1"]["task"]
        completing = asyncio.Event()

        async def _slow_swallowing_timeout():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0)  # one more tick before completing
                completing.set()
                return  # ends NOT cancelled (like _timeout's except arm)

        child = asyncio.create_task(_slow_swallowing_timeout())
        await asyncio.sleep(0)  # child now parked in its sleep
        coord._holds["rid-1"]["task"] = child
        task = asyncio.create_task(coord.resolve("rid-1", "allowed", "a@x"))
        # resolve popped the hold, cancelled + is reaping the swapped child
        await completing.wait()
        task.cancel()  # resolve is queued-to-resume: the _must_cancel path
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.done() and not child.cancelled()
        assert fut.result() == {
            "decision": "deny",
            "reason": "error",
            "duration": "once",
        }
        app.state.model.egress_consent.decide.assert_not_awaited()
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )
        # reap the displaced original timeout task (no pending-task noise)
        orig_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await orig_task

    async def test_resolve_cancelled_after_decide_keeps_committed_verdict(
        self,
    ):
        # decide() committed (row allowed) and the cancellation lands during
        # the finish sequence's persist await: the already-set Future keeps
        # the REAL verdict (the relay gets the allow, not a retroactive
        # deny), no contradictory "expired" broadcast follows the real one,
        # and the rescue's expire is a no-op on the non-pending row.
        row = request()
        row["decision"] = "allowed"
        row["duration"] = "forever"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        persisting = asyncio.Event()

        async def _hanging_persist(*args):
            persisting.set()
            await asyncio.Event().wait()

        app.state.model.workspaces.add_allowed_domain = AsyncMock(
            side_effect=_hanging_persist
        )
        task = asyncio.create_task(
            coord.resolve("rid-1", "allowed", "a@x", duration="forever")
        )
        await persisting.wait()  # Future set; resolve suspended in persist
        app.state.consent_deciders.broadcast.reset_mock()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fut.result()["decision"] == "allow"  # committed verdict kept
        frames = [
            c.args[1]
            for c in app.state.consent_deciders.broadcast.call_args_list
        ]
        assert not any(
            f.get("type") == "egress_resolved"
            and f.get("decision") == "expired"
            for f in frames
        )
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )

    async def test_concurrent_resolves_first_decision_wins(self):
        # two deciders resolve the same hold concurrently: exactly one wins
        # (one decide() write), the other is a no-op (returns None).
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        v1, v2 = await asyncio.gather(
            coord.resolve("rid-1", "allowed", "a@x"),
            coord.resolve("rid-1", "denied", "b@x"),
        )
        winners = [v for v in (v1, v2) if v is not None]
        assert len(winners) == 1  # exactly one winner
        assert (
            app.state.model.egress_consent.decide.await_count == 1
        )  # one DB write

    async def test_forever_allow_appends_host_port_to_allowed_domains(self):
        # A `forever` allow persists by appending the consented host:port to
        # the workspace's allowed_domains (#2368) -- least-privilege (the port
        # the decider was shown), lowercased + deduped by the model.
        row = request()  # host 1.2.3.4, port 443
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "a@x", duration="forever"
        )
        assert verdict == {
            "decision": "allow",
            "reason": "decided",
            "duration": "forever",
        }
        assert fut.result()["decision"] == "allow"
        app.state.model.workspaces.add_allowed_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )
        app.state.model.workspaces.add_rejected_domain.assert_not_awaited()

    async def test_timed_allow_does_not_mutate_allowed_domains(self):
        # Only `forever` mutates allowed_domains; a timed allow ("1d") is a
        # plain in-memory learn (no list mutation).
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        await coord.resolve("rid-1", "allowed", "a@x", duration="1d")
        app.state.model.workspaces.add_allowed_domain.assert_not_awaited()

    async def test_forever_allow_persist_failure_does_not_break_verdict(self):
        # A persistence failure (model raises) is swallowed: the verdict is
        # still allow and the rules refresh still fires (best-effort
        # durability; the session's in-memory ACCEPT already covers it).
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        app.state.model.workspaces.add_allowed_domain = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "a@x", duration="forever"
        )
        assert verdict["decision"] == "allow"
        app.state.model.workspaces.add_allowed_domain.assert_awaited_once()

    async def test_forever_allow_missing_host_skips_persist(self):
        # No host in the row -> nothing to persist; the verdict still lands
        # (best-effort durability; the session's in-memory ACCEPT covers it).
        row = request()
        row["decision"] = "allowed"
        row["dest_host"] = None
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "a@x", duration="forever"
        )
        assert verdict["decision"] == "allow"
        app.state.model.workspaces.add_allowed_domain.assert_not_awaited()

    async def test_forever_allow_not_persisted_still_succeeds(self):
        # add_allowed_domain returns False (workspace missing / malformed):
        # logged as a warning, but the verdict still lands.
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        app.state.model.workspaces.add_allowed_domain = AsyncMock(
            return_value=False
        )
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "a@x", duration="forever"
        )
        assert verdict["decision"] == "allow"
        app.state.model.workspaces.add_allowed_domain.assert_awaited_once()

    async def test_forever_deny_appends_host_port_to_rejected_domains(self):
        # A `forever` deny persists by appending the consented host:port to
        # the workspace's rejected_domains (#2369) -- the mirror of the allow
        # side. It must NOT touch allowed_domains (a deny is never an allow).
        row = request()  # host 1.2.3.4, port 443
        row["decision"] = "denied"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "denied", "a@x", duration="forever"
        )
        assert verdict == {
            "decision": "deny",
            "reason": "decided",
            "duration": "forever",
        }
        assert fut.result()["decision"] == "deny"
        app.state.model.workspaces.add_rejected_domain.assert_awaited_once_with(
            FULL_WS, "1.2.3.4:443"
        )
        app.state.model.workspaces.add_allowed_domain.assert_not_awaited()

    async def test_forever_deny_persist_failure_does_not_break_verdict(self):
        # Mirror of the allow side: a persistence failure (model raises) is
        # swallowed -- the verdict is still deny (best-effort durability).
        row = request()
        row["decision"] = "denied"
        app = _app(request=request(), decide_row=row)
        app.state.model.workspaces.add_rejected_domain = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "denied", "a@x", duration="forever"
        )
        assert verdict["decision"] == "deny"
        app.state.model.workspaces.add_rejected_domain.assert_awaited_once()

    async def test_forever_deny_missing_host_skips_persist(self):
        row = request()
        row["decision"] = "denied"
        row["dest_host"] = None
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "denied", "a@x", duration="forever"
        )
        assert verdict["decision"] == "deny"
        app.state.model.workspaces.add_rejected_domain.assert_not_awaited()

    async def test_forever_deny_not_persisted_still_succeeds(self):
        # add_rejected_domain returns False (workspace missing/malformed):
        # logged as a warning, but the verdict still lands.
        row = request()
        row["decision"] = "denied"
        app = _app(request=request(), decide_row=row)
        app.state.model.workspaces.add_rejected_domain = AsyncMock(
            return_value=False
        )
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "denied", "a@x", duration="forever"
        )
        assert verdict["decision"] == "deny"
        app.state.model.workspaces.add_rejected_domain.assert_awaited_once()

    async def test_forever_deny_portless_persists_bare_host(self):
        # Unlike the allow side, a port-less deny IS persisted -- as a bare
        # host: reject enforcement is name-level (port ignored), so blocking
        # the whole host is the safe, natural unit of a deny, and withholding
        # it would make a `forever` deny silently non-durable across restart.
        row = request()
        row["decision"] = "denied"
        row["dest_port"] = 0
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "denied", "a@x", duration="forever"
        )
        assert verdict["decision"] == "deny"
        app.state.model.workspaces.add_rejected_domain.assert_awaited_once_with(
            FULL_WS,
            "1.2.3.4",  # bare host (no port)
        )

    async def test_forever_allow_portless_not_persisted(self):
        # A port-less verdict (e.g. ICMP, dest_port 0) is NOT persisted -- a
        # bare host would broaden to all-ports + subdomains (#2371 review).
        # The deciding connection still gets its in-memory ACCEPT (verdict
        # allow); only durability is withheld.
        row = request()
        row["decision"] = "allowed"
        row["dest_port"] = 0
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "a@x", duration="forever"
        )
        assert verdict["decision"] == "allow"
        app.state.model.workspaces.add_allowed_domain.assert_not_awaited()

    async def test_forever_allow_blocked_outside_decider_workspace(self):
        # defense-in-depth: a workspace-scoped decider may not decide another
        # workspace's request -> no verdict, no allowed_domains mutation.
        row = request()
        row["decision"] = "allowed"
        app = _app(request=request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1",
            "allowed",
            "a@x",
            duration="forever",
            decider_workspace="other",
        )
        assert verdict is None
        app.state.model.egress_consent.decide.assert_not_awaited()
        app.state.model.workspaces.add_allowed_domain.assert_not_awaited()


class TestConsentCoordinatorTimeout:
    async def test_timeout_expires_and_denies(self):
        app = _app(timeout=0.05, request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await fut
        assert verdict == {
            "decision": "deny",
            "reason": "timeout",
            "duration": "once",
        }
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )
        assert coord._holds == {}

    async def test_timeout_noops_if_resolved_first(self):
        # resolve wins the race -> the timeout task is cancelled before wake.
        app = _app(timeout=0.05, request=request(), decide_row=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        await coord.resolve("rid-1", "allowed", "a@x")
        await asyncio.sleep(0.12)
        app.state.model.egress_consent.expire_pending.assert_not_awaited()
        assert fut.result()["decision"] == "allow"

    async def test_fail_close_on_unknown_id_is_noop(self):
        app = _app()
        coord = ConsentCoordinator(app)
        # defensive: fail-closing a hold that is already gone does nothing.
        await coord._fail_close("never-held", reason="timeout")
        app.state.model.egress_consent.expire_pending.assert_not_awaited()

    async def test_timeout_still_denies_when_expire_raises(self):
        # expire_pending failing must not strand the hold -- it logs + still
        # resolves the Future deny. Both attempts fail (#3081 adds one
        # retry); the row then falls back to the startup reaper.
        app = _app(timeout=0.05, request=request())
        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await fut
        assert verdict == {
            "decision": "deny",
            "reason": "timeout",
            "duration": "once",
        }
        assert app.state.model.egress_consent.expire_pending.await_count == 2
        assert all(
            call.args == ("rid-1",)
            for call in app.state.model.egress_consent.expire_pending.await_args_list
        )

    async def test_timeout_expire_retry_recovers_transient_error(self):
        # #3081: the first expire attempt fails transiently -- the retry
        # lands and the row does not linger pending with no live hold.
        app = _app(timeout=0.05, request=request())
        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=[RuntimeError("db locked"), True]
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await fut
        assert verdict["decision"] == "deny"
        assert app.state.model.egress_consent.expire_pending.await_count == 2
        assert coord._holds == {}


class TestConsentCoordinatorStop:
    async def test_stop_fail_closes_all_holds(self):
        app = _app(request=request("r1"))
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        # second hold with a distinct request id
        app.state.model.egress_consent.create_request = AsyncMock(
            return_value=request("r2")
        )
        await coord.hold(FULL_WS, "5.6.7.8", 80)
        assert len(coord._holds) == 2
        await coord.stop()
        assert coord._holds == {}
        expired = {
            call.args[0]
            for call in app.state.model.egress_consent.expire_pending.await_args_list
        }
        assert expired == {"r1", "r2"}

    async def test_stop_awaits_cancelled_timeout_tasks(self):
        # #3083: stop() cancels AND awaits each hold's timeout task, so no
        # pending task outlives the coordinator (no "Task was destroyed but
        # it is pending" noise when the loop closes right after).
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        task = coord._holds["rid-1"]["task"]
        await coord.stop()
        assert task.done()  # cancelled AND reaped before stop returned

    async def test_stop_awaits_task_cancelled_before_it_started(self):
        # The timeout task never got a first step (hold() ran without
        # yielding); cancelling it kills the coroutine before its body's
        # except-CancelledError engages, so the task ends CANCELLED -- the
        # await must swallow that, and the hold still fail-closes.
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        task = coord._holds["rid-1"]["task"]
        await coord.stop()
        assert task.done()
        assert task.cancelled()
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )

    async def test_cancel_hold_task_reraises_callers_cancellation(self):
        # If the CALLER is cancelled while awaiting a child that ends NOT
        # cancelled, the cancellation must propagate -- swallowing it would
        # eat the caller's cancellation and unbalance its cancel count.
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        swallowed = asyncio.Event()

        async def _swallowing_child():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                swallowed.set()
                return  # end NOT cancelled (mirrors _timeout's except arm)

        child = asyncio.create_task(_swallowing_child())
        await asyncio.sleep(0)  # child now suspended in its sleep
        wrapper = asyncio.create_task(coord._cancel_hold_task(child))
        await swallowed.wait()  # child reaped our cancel and returned
        wrapper.cancel()  # wrapper is queued-to-resume: _must_cancel is set
        with pytest.raises(asyncio.CancelledError):
            await wrapper
        assert child.done() and not child.cancelled()

    async def test_cancel_hold_task_swallows_poisoned_task_exception(self):
        # A timeout task that died from a REAL exception (a bug in the
        # timeout path) must not abort stop()'s fail-close loop for the
        # remaining holds: the exception is logged and the reap continues.
        app = _app(request=request())
        coord = ConsentCoordinator(app)

        async def _poisoned_child():
            raise RuntimeError("poisoned")

        child = asyncio.create_task(_poisoned_child())
        await asyncio.sleep(0)  # child completed, exception stored
        await coord._cancel_hold_task(child)  # must not raise
        assert child.done() and not child.cancelled()

    async def test_stop_is_idempotent(self):
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        await coord.stop()
        await coord.stop()
        assert coord._holds == {}


class TestConsentCoordinatorReconfigure:
    async def test_reconfigure_swaps_app(self):
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        new_app = _app(timeout=1.0, request=request())
        coord.reconfigure(new_app)
        assert coord.app is new_app
        assert coord.timeout == 1.0


# --- egress-sidecar WebSocket endpoint --------------------------------------


class _FakeWS:
    """Minimal stand-in for a fastapi WebSocket for handler-level tests.

    Incoming messages come from an asyncio.Queue (``feed``), so a test can
    push an egress event, let the relay task run, then push a disconnect --
    mirroring the real sidecar's send-then-wait-for-verdict ordering.
    """

    def __init__(self, params: dict, headers: dict | None = None):
        self.query_params = params
        self.headers = headers or {}
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.accepted = False
        self.closed: tuple | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    async def send_json(self, data: dict) -> None:
        self.sent.append(json.dumps(data))

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def feed(self, item) -> None:
        await self._incoming.put(item)


def _sidecar_app(token_result=FULL_WS):
    """App with mocked workspace-token decode + a mock coordinator."""
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()

    def _decode(token):
        return token_result

    app.state.auth = types.SimpleNamespace(decode_workspace_token=_decode)
    coord = AsyncMock()
    app.state.consent_coordinator = coord
    # #2339: the handler registers/deregisters the sidecar socket + resolves
    # drop-acks via this registry.
    app.state.sidecar_connections = Mock()
    return app, coord


class TestEgressSidecarWS:
    async def test_missing_token_rejected(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app()
        ws = _FakeWS({})
        await handle_egress_sidecar(ws, app)
        assert ws.closed == (4001, "Missing token")
        assert ws.accepted is False

    async def test_authorization_header_token_accepted(self):
        # egress path (#2319): the JWT rides in the Authorization header (the
        # sidecar sends `Bearer <jwt>` so the egress site's forward_auth sees
        # it), not the ?token= query param. Both paths must authenticate.
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"decision": "deny", "reason": "static"})
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({}, headers={"authorization": "Bearer hdr-tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {
                    "type": "egress",
                    "id": "loc1",
                    "dst": "1.2.3.4",
                    "dport": 443,
                }
            )
        )
        await asyncio.sleep(0.05)
        coord.hold.assert_awaited_once_with(FULL_WS, "1.2.3.4", 443)
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_activity_frame_bumps_idle_timer(self):
        # #2479: a {type:activity} frame from the sidecar bumps the workspace's
        # idle timer so an egress-only workload (whose traffic bypasses klangkd)
        # is not reaped by the idle timeout.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app()
        state = types.SimpleNamespace(record_activity=Mock())
        app.state.container_registry = types.SimpleNamespace(
            states={FULL_WS: state}
        )
        ws = _FakeWS({"token": FULL_WS})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(json.dumps({"type": "activity"}))
        await asyncio.sleep(0.02)
        state.record_activity.assert_called_once_with()
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_activity_frame_no_tracked_state_is_noop(self):
        # #2479: an activity frame for a workspace with no live ContainerState
        # (container not started / already reaped) must not raise -- there is
        # simply nothing to bump.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app()
        app.state.container_registry = types.SimpleNamespace(states={})
        ws = _FakeWS({"token": FULL_WS})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(json.dumps({"type": "activity"}))
        await asyncio.sleep(0.02)
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_drop_ack_resolves_pending(self):
        # a drop_ack frame from the sidecar -> resolve_ack on the registry (#2339)
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app()
        ws = _FakeWS({"token": FULL_WS})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps({"type": "drop_ack", "id": "ack-1", "ok": True})
        )
        await asyncio.sleep(0.02)
        app.state.sidecar_connections.resolve_ack.assert_called_once_with(
            "ack-1", True
        )
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_invalid_token_rejected(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app(token_result=None)
        ws = _FakeWS({"token": "bad"})
        await handle_egress_sidecar(ws, app)
        assert ws.closed == (4001, "Invalid token")

    async def test_expired_token_rejected(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app(token_result=auth.Auth.WORKSPACE_TOKEN_EXPIRED)
        ws = _FakeWS({"token": "stale"})
        await handle_egress_sidecar(ws, app)
        assert ws.closed == (4002, "Token expired")

    async def test_static_egress_relays_deny_immediately(self):
        # coordinator returns an already-resolved deny Future (static path)
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"decision": "deny", "reason": "static"})
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {
                    "type": "egress",
                    "id": "loc1",
                    "dst": "1.2.3.4",
                    "dport": 443,
                }
            )
        )
        await asyncio.sleep(
            0.05
        )  # let the relay call hold + flush the verdict
        coord.hold.assert_awaited_once_with(FULL_WS, "1.2.3.4", 443)
        assert [json.loads(m) for m in ws.sent] == [
            {
                "type": "verdict",
                "id": "loc1",
                "decision": "deny",
                "duration": "once",
            }
        ]
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_held_egress_relays_verdict_when_resolved(self):
        # coordinator returns a pending Future the test resolves -> allow
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {"type": "egress", "id": "loc1", "dst": "9.9.9.9", "dport": 53}
            )
        )
        await asyncio.sleep(0.05)  # relay is now awaiting the verdict Future
        fut.set_result(
            {"decision": "allow", "reason": "decided", "duration": "1d"}
        )
        await asyncio.sleep(0.05)  # relay sends the verdict
        assert [json.loads(m) for m in ws.sent] == [
            {
                "type": "verdict",
                "id": "loc1",
                "decision": "allow",
                "duration": "1d",
            }
        ]
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_non_egress_and_invalid_json_ignored(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"decision": "deny", "reason": "static"})
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed("not-json")
        await ws.feed(json.dumps({"type": "ping"}))
        # valid JSON but not a dict (string / int / list / bool) must be
        # ignored, not crash the handler with AttributeError on .get().
        await ws.feed(json.dumps("hello"))
        await ws.feed(json.dumps(123))
        await ws.feed(json.dumps([1, 2]))
        await ws.feed(json.dumps(True))
        await ws.feed(
            json.dumps({"type": "egress", "id": 5, "dst": "1.2.3.4"})
        )  # bad id (not a str)
        await ws.feed(
            json.dumps(
                {"type": "egress", "id": "ok", "dst": "1.2.3.4", "dport": "x"}
            )
        )  # bad dport (not an int)
        await ws.feed(
            json.dumps(
                {"type": "egress", "id": "ok", "dst": "1.2.3.4", "dport": True}
            )
        )  # bad dport (bool, not an int -- isinstance(True, int) is True)
        await ws.feed(
            json.dumps({"type": "egress", "id": "ok", "dst": "1.2.3.4"})
        )
        await asyncio.sleep(0.05)
        await ws.feed(WebSocketDisconnect())
        await handler
        # only the well-formed egress event reached the coordinator
        coord.hold.assert_awaited_once_with(FULL_WS, "1.2.3.4", None)

    async def test_disconnect_cancels_in_flight_relay(self):
        # a held egress whose relay never resolves: disconnect cancels it,
        # and no verdict is sent.
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {
                    "type": "egress",
                    "id": "loc1",
                    "dst": "1.2.3.4",
                    "dport": 443,
                }
            )
        )
        await asyncio.sleep(0.05)  # relay is now awaiting the Future
        await ws.feed(RuntimeError())  # disconnect mid-hold
        await handler
        assert ws.sent == []  # relay cancelled before it could send
        assert not fut.done()  # the coordinator's hold Future is untouched


class TestConsentRevokeIntegration3083:
    """Real DB + real model: the #3083 revoke nits end-to-end."""

    async def _interactive_ws(self, app_state, user, name):
        from klangk.model.workspaces import EGRESS_MODE_INTERACTIVE

        _wire_coordinator_extras(app_state)
        return await app_state.state.model.workspaces.create_workspace(
            user["id"], name, egress_mode=EGRESS_MODE_INTERACTIVE
        )

    async def test_shared_forever_entry_survives_one_revoke(
        self, app_state, user
    ):
        # #3083: two forever allows for one host:port share a single
        # allowed_domains entry. Revoking one verdict keeps the entry (the
        # survivor still needs it); revoking the survivor retracts it.
        ws = await self._interactive_ws(app_state, user, "shared-forever")
        ec = app_state.state.model.egress_consent
        coord = ConsentCoordinator(app_state)
        ids = []
        for _ in range(2):
            # hold -> resolve `forever` (the path that persists the entry)
            await coord.hold(ws["id"], "shared.example.com", 443)
            req_id = next(iter(coord._holds))
            verdict = await coord.resolve(
                req_id, "allowed", user["id"], duration="forever"
            )
            assert verdict["decision"] == "allow"
            ids.append(req_id)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["allowed_domains"] == ["shared.example.com:443"]
        # revoke ONE of the two -> the shared entry must survive
        assert await coord.revoke(ids[0], user["id"]) is True
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["allowed_domains"] == ["shared.example.com:443"]
        active = await ec.list_active(ws["id"])
        assert [r["id"] for r in active] == [ids[1]]
        # revoke the survivor -> now the entry is retracted
        assert await coord.revoke(ids[1], user["id"]) is True
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["allowed_domains"] == []
        assert await ec.list_active(ws["id"]) == []

    async def test_concurrent_duplicate_revokes_both_ack_success(
        self, app_state, user
    ):
        # #3083: two deciders revoke the same verdict concurrently: both
        # revokes ack success (the loser's is idempotent), never a
        # misleading "revoke failed -- still in effect".
        ws = await self._interactive_ws(app_state, user, "dup-revoke")
        ec = app_state.state.model.egress_consent
        coord = ConsentCoordinator(app_state)
        await coord.hold(ws["id"], "dup.example.com", 443)
        req_id = next(iter(coord._holds))
        await coord.resolve(req_id, "allowed", user["id"], "tilrestart")
        results = await asyncio.gather(
            coord.revoke(req_id, user["id"]),
            coord.revoke(req_id, user["id"]),
        )
        assert results == [True, True]
        fresh = await ec.get_request(req_id)
        assert fresh["decision"] == "revoked"
        assert await ec.list_active(ws["id"]) == []


# --- pause integration: real model round-trip (#2332) ------------------------


def _wire_coordinator_extras(app_state):
    """Add the coordinator's non-model deps (settings, deciders, sidecar)."""
    app_state.state.settings.egress_consent_timeout = 30.0
    app_state.state.settings.egress_consent_rate_limit = 50
    app_state.state.consent_deciders = types.SimpleNamespace(
        has_decider=lambda workspace_id: True,
        broadcast=Mock(return_value=0),
    )
    app_state.state.sidecar_connections = types.SimpleNamespace(
        send_drop=Mock(return_value=None)
    )


class TestConsentCoordinatorPauseIntegration:
    """Real DB + real model: pause persists and gates hold() end-to-end (#2332)."""

    async def test_pause_then_hold_auto_allows(self, app_state, user):
        # With a real DB, pause() must persist consent_paused_until so a later
        # hold() reads it and auto-allows (no pending request created).
        from klangk.model.workspaces import EGRESS_MODE_INTERACTIVE

        _wire_coordinator_extras(app_state)
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "pause-int", egress_mode=EGRESS_MODE_INTERACTIVE
        )
        coord = ConsentCoordinator(app_state)
        assert (await coord.pause(ws["id"], "15m"))["ok"] is True
        fut = await coord.hold(ws["id"], "newhost.example.com", 443)
        verdict = fut.result()
        assert verdict["decision"] == "allow"
        assert verdict["reason"] == "paused"
        # no pending request row was created (the pause suppressed the hold)
        pending = await app_state.state.model.egress_consent.list_requests(
            ws["id"], decision="pending"
        )
        assert pending == []

    async def test_pause_survives_a_verdict_resolve(self, app_state, user):
        # #2332 regression: resolving a held request rebroadcasts egress_rules;
        # the pause window must still be reported (the highlight must not
        # clear). Hold first (not paused), pause, then resolve.
        from klangk.model.workspaces import EGRESS_MODE_INTERACTIVE

        _wire_coordinator_extras(app_state)
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "pause-resolve", egress_mode=EGRESS_MODE_INTERACTIVE
        )
        coord = ConsentCoordinator(app_state)
        # 1. hold a destination (creates a pending request + in-memory hold)
        fut = await coord.hold(ws["id"], "holdhost.example.com", 443)
        assert not fut.done()  # held for a decider
        held_id = next(iter(coord._holds))
        # 2. pause (sets the window)
        assert (await coord.pause(ws["id"], "1h"))["ok"] is True
        # 3. resolve the held request -> rebroadcasts egress_rules
        await coord.resolve(held_id, "allowed", user["id"], duration="1h")
        # 4. the pause is still set: rules_frame reports it
        frame = await coord.rules_frame(ws["id"])
        assert frame["paused"] is not None
        assert frame["paused"]["paused"] is True
        # 5. a NEW hold after the resolve still auto-allows (pause durable)
        fut2 = await coord.hold(ws["id"], "other.example.com", 80)
        assert fut2.result()["reason"] == "paused"

    async def test_mode_switch_to_static_ends_pause_and_fails_closed(
        self, app_state, user
    ):
        # #3080: the reported bug -- pause while interactive, then switch
        # the workspace to static. Every off-list hold must deny as static
        # (not auto-allow on the stale window), the stored window is
        # cleared by the mode write, and switching back to interactive
        # does not resurrect it (prompting resumes).
        from klangk.model.workspaces import (
            EGRESS_MODE_INTERACTIVE,
            EGRESS_MODE_STATIC,
        )

        _wire_coordinator_extras(app_state)
        wsm = app_state.state.model.workspaces
        ws = await wsm.create_workspace(
            user["id"], "pause-stale", egress_mode=EGRESS_MODE_INTERACTIVE
        )
        coord = ConsentCoordinator(app_state)
        assert (await coord.pause(ws["id"], "1d"))["ok"] is True
        assert (
            await wsm.update_workspace(
                ws["id"], user["id"], egress_mode=EGRESS_MODE_STATIC
            )
            is True
        )
        verdict = (
            await coord.hold(ws["id"], "offlist.example.com", 443)
        ).result()
        assert verdict == {"decision": "deny", "reason": "static"}
        assert await wsm.get_consent_pause(ws["id"]) is None
        # a lingering decider cannot store a fresh window on the now-static
        # workspace either (#3086 review)
        assert (await coord.pause(ws["id"], "1h"))["ok"] is False
        assert await wsm.get_consent_pause(ws["id"]) is None
        # back to interactive: no resurrection -- the hold is held for a
        # decider instead of auto-allowed
        assert (
            await wsm.update_workspace(
                ws["id"], user["id"], egress_mode=EGRESS_MODE_INTERACTIVE
            )
            is True
        )
        fut = await coord.hold(ws["id"], "other.example.com", 443)
        assert not fut.done()
        assert coord._holds

    async def test_paused_respects_a_real_recorded_deny(self, app_state, user):
        # I2 (#2332): the security property -- a recorded in-effect deny still
        # blocks while paused -- driven through the REAL model (not a mock).
        # Records a deny, pauses, then holds the denied host and asserts the
        # verdict is deny (paused_deny), NOT an auto-allow.
        from klangk.model.workspaces import EGRESS_MODE_INTERACTIVE

        _wire_coordinator_extras(app_state)
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "pause-deny-int", egress_mode=EGRESS_MODE_INTERACTIVE
        )
        ec = app_state.state.model.egress_consent
        coord = ConsentCoordinator(app_state)
        # record an in-effect deny for evil.example.com:443
        req = await ec.create_request(ws["id"], "evil.example.com", 443)
        await ec.decide(req["id"], "denied", user["id"], "tilrestart")
        # pause the workspace
        assert (await coord.pause(ws["id"], "15m"))["ok"] is True
        # hold the denied host -> deny (paused_deny), NOT auto-allowed
        fut = await coord.hold(ws["id"], "evil.example.com", 443)
        verdict = fut.result()
        assert verdict["decision"] == "deny"
        assert verdict["reason"] == "paused_deny"
        # a different, never-decided host is still auto-allowed
        fut2 = await coord.hold(ws["id"], "unseen.example.com", 443)
        assert fut2.result()["reason"] == "paused"


class TestConsentFrameShapeIntegration:
    """#3082: live fanout and connect-time replay carry one request shape.

    Drives the REAL model: the ``egress_request`` frame ``_fanout`` sends on
    hold() and the frame ``snapshot()`` returns to a reconnecting decider must
    carry the same ``request`` key set, so no client can observe a
    delivery-path-dependent shape.
    """

    async def test_fanout_and_snapshot_frames_share_request_shape(
        self, app_state, user
    ):
        from klangk.model.workspaces import EGRESS_MODE_INTERACTIVE

        _wire_coordinator_extras(app_state)
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "shape-int", egress_mode=EGRESS_MODE_INTERACTIVE
        )
        coord = ConsentCoordinator(app_state)
        await coord.hold(ws["id"], "live.example.com", 443)
        live = [
            c.args[1]
            for c in app_state.state.consent_deciders.broadcast.call_args_list
            if c.args[1]["type"] == "egress_request"
        ]
        assert len(live) == 1
        replayed = await coord.snapshot(ws["id"])
        assert len(replayed) == 1
        assert set(live[0]["request"]) == set(replayed[0]["request"])
        # the unset lifecycle columns are present-and-None on both paths
        for frame in (live[0], replayed[0]):
            assert frame["request"]["duration"] is None
            assert frame["request"]["revoked_at"] is None
            assert frame["request"]["revoked_by"] is None


class TestCoordinatorBranchGaps2834:
    """#2834 branch gate: hold/fail-close/refresh outcomes the mainline
    revoke + stop tests only take one side of."""

    async def test_stop_skips_hold_removed_mid_iteration(self):
        # fail_all iterates a snapshot; a hold resolved+removed while an
        # earlier one is being fail-closed is simply skipped (its task
        # cancel is skipped, its _fail_close is a no-op).
        app = _app(request=request("r1"))
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.model.egress_consent.create_request = AsyncMock(
            return_value=request("r2")
        )
        await coord.hold(FULL_WS, "5.6.7.8", 80)
        real_fail_close = coord._fail_close

        async def _fail_close_expecting_one(request_id, reason="shutdown"):
            # When the FIRST hold is fail-closed, the second vanishes (a
            # racing resolve) -- the snapshot's second entry is then None.
            if request_id == "r1":
                coord._holds.pop("r2", None)
            return await real_fail_close(request_id, reason=reason)

        coord._fail_close = _fail_close_expecting_one
        await coord.stop()  # must not raise on the vanished r2
        assert coord._holds == {}

    async def test_resolve_with_already_done_future_skips_set_result(self):
        # A hold whose future was already resolved (a racing timeout)
        # must not be overwritten by the decide path -- the verdict that
        # landed first wins.
        app = _app(request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        fut.set_result(
            {"decision": "deny", "reason": "timeout", "duration": "once"}
        )
        await coord.resolve("rid-1", "allowed", "a@x")
        assert fut.result()["decision"] == "deny"  # not clobbered

    async def test_fail_close_with_already_done_future_skips_set_result(self):
        # Same race on the timeout path: an already-resolved future keeps
        # its first verdict; the expire bookkeeping still runs.
        app = _app(timeout=0.05, request=request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        fut.set_result(
            {"decision": "allow", "reason": "decided", "duration": "once"}
        )
        verdict = await fut
        assert verdict["decision"] == "allow"  # timeout did not clobber
        await asyncio.sleep(0.12)
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )

    async def test_revoke_retract_false_is_silent(self, caplog):
        # The domain-list remove reports nothing removed (the row was
        # already retracted): no info log, revoke still succeeds.
        app = _app()
        forever_row = _active_row()
        forever_row["duration"] = "forever"
        app.state.model.egress_consent.get_request = AsyncMock(
            return_value=forever_row
        )
        app.state.model.egress_consent.revoke = AsyncMock(
            return_value=_active_row(decision="revoked")
        )
        app.state.model.workspaces.remove_allowed_domain = AsyncMock(
            return_value=False
        )
        coord = ConsentCoordinator(app)
        with caplog.at_level("INFO"):
            assert await coord.revoke("rid-1", "a@x") is True
        assert not any(
            "retracted from list" in r.message for r in caplog.records
        )

    async def test_refresh_rules_none_frame_skips_broadcast(self):
        # A missing workspace returns no frame; nothing is broadcast (the
        # decider keeps its last view).
        app = _app()
        coord = ConsentCoordinator(app)
        coord.rules_frame = AsyncMock(return_value=None)
        app.state.consent_deciders.broadcast = Mock()
        await coord._broadcast_rules(FULL_WS)
        app.state.consent_deciders.broadcast.assert_not_called()
