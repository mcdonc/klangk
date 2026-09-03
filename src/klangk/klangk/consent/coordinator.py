"""Synchronous egress-consent hold/resolve coordinator (#2311).

#2308 makes "interactive" runtime state -- a workspace is interactive only
while >= 1 consent decider is registered. THIS module is what actually *holds*
a blocked egress connection in-flight for that decision: the sidecar's
egress-sidecar WebSocket calls :meth:`ConsentCoordinator.hold` per blocked
destination, and the coordinator returns a :class:`~asyncio.Future` that
resolves to the verdict once a decider decides (#2244 calls :meth:`resolve`),
the hold times out, or the coordinator shuts down. The sidecar relay task
awaits that Future and sends the verdict back over its socket (#2311).

Fail-closed throughout:

- no decider registered (or workspace not opted in) -> immediate static
  denial (no hold, no pending row, deny verdict at once); the sole
  exception is a live consent-pause window (#2332), and that too is
  honored only in interactive egress mode (#3080) -- a static workspace
  never auto-allows;
- decider too slow -> the per-hold timeout expires the row + denies;
- coordinator shutdown -> every in-flight hold is denied (a sidecar restart's
  in-process holds die with the process, and the orphaned pending rows are
  expired here).

No hold is ever left pending forever.

Owns only ``app`` (the app-ownership rule); timeout / rate-limit are read
live via property so a SIGHUP reload propagates.

Scope note: this is the klangkd coordination half of #2311. The sidecar's
kernel-level hold (suspending DNS queries, deferring NFQUEUE verdicts) + its
WS client land in a stacked follow-up; #2244 wires the decider fanout and
verdict reception to :meth:`resolve`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .egress import workspace_is_interactive, workspace_opted_in
from ..model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DECISION_PENDING,
    DURATION_DEFAULT,
    DURATION_FOREVER,
    DURATION_ONCE,
)
from ..model.workspaces import EGRESS_MODE_ALLOW

logger = logging.getLogger(__name__)

# Verdicts sent back to the sidecar over its WS relay (#2311).
VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"

# How long revoke() waits for the sidecar's drop-rule ack before giving up
# (fail-closed: leave the row enforced). The sidecar's rule-drop is a single
# iptables delete, so this is generous.
REVOKE_ACK_TIMEOUT = 5.0

# Consent-pause durations (#2332): how long interactive prompting is silenced
# workspace-wide. While paused, a destination with no allow-list rule and no
# in-effect recorded verdict is auto-allowed (no hold, no prompt); a recorded
# deny still blocks. A focused set for the TUI control; ``unpause`` clears it.
_PAUSE_SECONDS = {"15m": 900, "1h": 3600, "1d": 86400}


def _build_verdict(
    row: dict | None, decision: str, duration: str
) -> tuple[dict, str, dict | None, dict | None]:
    """(verdict, resolved_label, forever_allow_row, forever_deny_row) for a
    decided request. A `forever` verdict additionally captures its row for
    persistence: the allow mutates the workspace's allow-list (#2368) and the
    deny its deny-list (#2369) so either survives a container/sidecar restart;
    captured here, persisted after the Future is resolved (the deciding
    connection gets its in-memory ACCEPT first) and before the rules refresh
    (so the view reflects the new entry)."""
    if row is None:
        return (
            {"decision": VERDICT_DENY, "reason": "gone"},
            "expired",
            None,
            None,
        )
    if decision == "allowed":
        verdict = {
            "decision": VERDICT_ALLOW,
            "reason": "decided",
            "duration": duration,
        }
        forever_allow = row if duration == DURATION_FOREVER else None
        return verdict, "allowed", forever_allow, None
    verdict = {
        "decision": VERDICT_DENY,
        "reason": "decided",
        "duration": duration,
    }
    forever_deny = row if duration == DURATION_FOREVER else None
    return verdict, "denied", None, forever_deny


def _completed_verdict(verdict: dict) -> asyncio.Future:
    """A Future already resolved to *verdict* (a no-hold immediate
    answer)."""
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(verdict)
    return fut


def _hold_in_scope(hold: dict, decider_workspace: str | None) -> bool:
    """True when a held request is decidable by this caller: a
    workspace-scoped decider may only decide its own workspace's holds."""
    if decider_workspace is None:
        return True
    return hold["workspace_id"] == decider_workspace


def _forever_allow_entry(row: dict) -> str | None:
    """The ``host:port`` allowed_domains entry for a forever allow, or
    ``None`` when it must not be persisted: a missing host/workspace, or a
    port-less dest (e.g. an ICMP ping, dest_port 0) -- a port-less bare host
    would durably broaden one connection's consent to every port on that
    host (the sidecar treats a port-less spec as all-ports on the apex,
    since bare is now exact). The deciding connection still got its
    in-memory ACCEPT for this session; durability is simply withheld
    (#2368)."""
    host = row.get("dest_host")
    port = row.get("dest_port")
    workspace_id = row.get("workspace_id")
    if not host or not workspace_id:
        return None
    if not port:
        logger.info(
            "consent: forever allow of port-less dest %s ws=%s not "
            "persisted (a bare host would broaden to all-ports); "
            "session still works",
            host,
            str(workspace_id)[:8],
        )
        return None
    return f"{host}:{port}"


def _forever_deny_entry(row: dict) -> str | None:
    """The rejected_domains entry for a forever deny, or ``None`` when it
    must not be persisted. Unlike the allow side, a port-less deny IS
    persisted (as a bare host): the sidecar's reject enforcement is
    name-level (:func:`rejected_for` ignores port -- a rejected name is
    NXDOMAIN'd before resolution), so the whole host is the natural unit,
    and "block more" is the safe direction."""
    host = row.get("dest_host")
    port = row.get("dest_port")
    workspace_id = row.get("workspace_id")
    if not host or not workspace_id:
        return None
    return f"{host}:{port}" if port else host


def _by_decision(rows: list[dict], decision: str) -> list[dict]:
    """The in-effect rows carrying one decision."""
    return [r for r in rows if r["decision"] == decision]


def _pause_frame(until: float | None) -> dict | None:
    """The live pause window (#2332), read fresh so a self-expired pause
    shows as cleared."""
    if until is not None and until > time.time():
        return {"paused": True, "until": until}
    return None


class ConsentCoordinator:
    """In-process holds for egress requests awaiting a verdict (#2311).

    Constructed once in :func:`build_app` and stored on ``app.state``; holds
    are created on demand by :meth:`hold` (called by the sidecar WS endpoint)
    and resolved by :meth:`resolve` (the #2244 decider hook), a timeout, or
    :meth:`stop` (fail-close on shutdown).
    """

    def __init__(self, app) -> None:
        self.app = app
        # request_id -> {"future": Future, "workspace_id": str, "task": Task}
        self._holds: dict[str, dict] = {}

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def timeout(self) -> float:
        return self.app.state.settings.egress_consent_timeout

    @property
    def rate_limit(self) -> int:
        return self.app.state.settings.egress_consent_rate_limit

    def start(self) -> None:
        """Lifespan symmetry only -- holds are created on demand by :meth:`hold`.

        Kept so every ``app.state`` subsystem has a uniform start/stop pair.
        """

    async def stop(self) -> None:
        """Fail-close every in-flight hold (coordinator shutdown / sidecar gone).

        Each orphaned pending row is expired (audit) and its Future resolved
        deny, so a sidecar restart leaves no leaked allow and no hung relay.
        """
        for request_id in list(self._holds):
            hold = self._holds.get(request_id)
            if hold is not None:
                hold["task"].cancel()
            await self._fail_close(request_id, reason="shutdown")

    async def _allow_mode_verdict(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> asyncio.Future:
        """The default-permit shortcut verdict (#2406).

        egress_mode == allow is default-permit: record the off-list
        destination (logging -- mirrors how static records a denial via
        record_static_denial) and allow at once -- no hold, no prompt.
        Behaves as if an internal always-allow decider were registered, but
        is a short-circuit branch. rejected_domains is enforced earlier at
        the sidecar DNS layer (rejected_for -> NXDOMAIN), so a host reaching
        this gate is already not rejected. duration=tilrestart
        (DURATION_DEFAULT) so the sidecar learns the IP all-ports for its
        lifetime (no per-connection re-prompt); nothing persists to
        allowed_domains (this branch bypasses resolve())."""
        await self.app.state.model.egress_consent.record_static_allow(
            workspace_id, dst, dport
        )
        return _completed_verdict(
            {
                "decision": VERDICT_ALLOW,
                "reason": "allow_mode",
                "duration": DURATION_DEFAULT,
            }
        )

    async def _static_denial_verdict(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> asyncio.Future:
        """The non-interactive verdict: record the static denial and deny
        at once (no pending row, no hold, no timeout) -- the sidecar denies
        immediately."""
        await self.app.state.model.egress_consent.record_static_denial(
            workspace_id, dst, dport
        )
        return _completed_verdict(
            {"decision": VERDICT_DENY, "reason": "static"}
        )

    async def _paused_gate_verdict(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> asyncio.Future:
        """The #2332 paused-prompting verdict: a destination with an
        in-effect recorded DENY is still blocked (the pause does not
        override existing verdicts); everything else is auto-allowed (no
        hold, no prompt, no row -- the sidecar learns/accepts the SYN like
        a static allow). Allow-list rules are enforced earlier at the
        sidecar DNS layer, so a host reaching this gate is already not
        allow-listed."""
        verdict = await self._paused_verdict(workspace_id, dst, dport)
        return _completed_verdict(verdict)

    async def _interactive_hold(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> asyncio.Future:
        """The interactive path: a deny Future for a rate-limited or
        duplicate request, else the registered hold's verdict Future."""
        consent = self.app.state.model.egress_consent
        if await consent.count_pending(workspace_id) >= self.rate_limit:
            return _completed_verdict(
                {"decision": VERDICT_DENY, "reason": "rate_limited"}
            )
        request = await consent.create_request(workspace_id, dst, dport)
        if request is None:
            return _completed_verdict(
                {"decision": VERDICT_DENY, "reason": "duplicate"}
            )
        return self._register_hold(request)

    async def hold(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> asyncio.Future:
        """Gate-check a blocked egress and create a hold; return its verdict Future.

        - egress_mode allow -> record + allow at once (default-permit).
        - interactive mode + a live pause window -> the paused gate (#2332;
          honored only in interactive mode, #3080: a stale pause must not
          auto-allow egress in a static/allow workspace).
        - not interactive (no decider, or workspace not opted in) -> record a
          static denial and resolve the Future deny at once (no pending row,
          no hold, no timeout) -- the sidecar denies immediately.
        - interactive + decider, but the per-workspace pending cap is reached
          -> deny at once (flood bound; the hold is refused, not held).
        - a pending request for this (workspace, dst, dport) already exists
          (create_request dedup) -> deny the duplicate.
        - otherwise -> create the pending request, register the hold, arm its
          timeout, and fan out to the workspace's deciders (#2244 stub).
        """
        try:
            if await self._is_allow(workspace_id):
                return await self._allow_mode_verdict(workspace_id, dst, dport)
            if await self._is_paused(workspace_id):
                return await self._paused_gate_verdict(
                    workspace_id, dst, dport
                )
            if not await self.is_interactive(workspace_id):
                return await self._static_denial_verdict(
                    workspace_id, dst, dport
                )
            return await self._interactive_hold(workspace_id, dst, dport)
        except Exception:
            # A model/DB failure must not crash the relay or strand the hold:
            # fail-close to a deny verdict so the sidecar NXDOMAIN/DROPs. The
            # kernel keeps the connection blocked either way (fail-closed), but
            # this gives the sidecar a definitive deny instead of silence.
            logger.exception("consent: hold failed; fail-closing to deny")
            return _completed_verdict(
                {"decision": VERDICT_DENY, "reason": "error"}
            )

    async def pause(self, workspace_id: str, duration: str) -> dict:
        """Pause interactive consent prompting workspace-wide for ``duration`` (#2332).

        While paused, :meth:`hold` auto-allows any destination that has no
        allow-list rule and no in-effect recorded verdict (instead of holding
        it for a decider); a recorded deny still blocks. Sets
        ``consent_paused_until`` on the workspace to ``now + duration`` and
        broadcasts a refreshed ``egress_rules`` frame so every decider sees
        the pause window. The window is honored only while the workspace
        stays in interactive egress mode: an actual ``egress_mode`` switch
        clears it (#3080), the hold gate ignores it outside interactive mode
        regardless, and a pause set on a workspace since switched away from
        interactive is refused (``ok`` False). Returns
        ``{"ok": bool, "until": float | None}`` -- ``ok`` is False for an
        unknown duration, a non-interactive workspace, or a missing
        workspace.
        """
        secs = _PAUSE_SECONDS.get(duration)
        if secs is None:
            return {"ok": False, "until": None}
        if not await workspace_opted_in(self.app, workspace_id):
            # #3086 review: the pause is an interactive-mode affordance --
            # a lingering decider socket (connected before a mode switch)
            # must not store a new inert window that only confuses the
            # rules view; the hold gate would ignore it regardless.
            return {"ok": False, "until": None}
        until = time.time() + secs
        if not await self.app.state.model.workspaces.set_consent_pause(
            workspace_id, until
        ):
            return {"ok": False, "until": None}
        await self._broadcast_rules(workspace_id)
        return {"ok": True, "until": until}

    async def unpause(self, workspace_id: str) -> dict:
        """Clear the consent-pause window (#2332).

        Returns ``{"ok": bool}`` -- False if the workspace is missing. Always
        broadcasts a refreshed ``egress_rules`` frame on success so deciders
        drop the pause indicator.
        """
        ok = await self.app.state.model.workspaces.set_consent_pause(
            workspace_id, None
        )
        if ok:
            await self._broadcast_rules(workspace_id)
        return {"ok": ok}

    async def _is_paused(self, workspace_id: str) -> bool:
        """Is consent prompting paused for the workspace right now (#2332)?

        Reads ``consent_paused_until`` live and compares against ``now``, so a
        pause self-expires the moment its window elapses (no sweep needed for
        correctness -- the gate re-evaluates on every hold).

        #3080: honored only while the workspace is in interactive egress
        mode. A pause set in an interactive epoch must not auto-allow
        off-list egress after the workspace is switched to static (or
        allow) -- those modes answer before the pause is consulted, so a
        static workspace stays default-deny for the whole stale window.
        The decider-liveness half of interactivity is deliberately NOT
        required here: a decider pauses prompting and then walks away, so
        the window must keep auto-allowing after the decider disconnects
        (that is the pause's purpose), while the mode gate above still
        bounds it to opted-in workspaces.
        """
        if not await workspace_opted_in(self.app, workspace_id):
            return False
        until = await self.app.state.model.workspaces.get_consent_pause(
            workspace_id
        )
        return until is not None and until > time.time()

    async def _paused_verdict(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> dict:
        """Verdict for a held SYN while prompting is paused (#2332).

        A destination with an in-effect recorded DENY is still blocked (the
        pause respects existing verdicts); everything else is auto-allowed
        with a ``once`` duration -- the sidecar accepts the SYN (conntrack
        carries the connection) without learning the IP, so each NEW
        connection re-gates: once the pause elapses, new connections to the
        same host prompt again (none linger auto-allowed past the window).
        """
        row = await self.app.state.model.egress_consent.active_verdict_for(
            workspace_id, dst, dport
        )
        if row is not None and row["decision"] == DECISION_DENIED:
            return {
                "decision": VERDICT_DENY,
                "reason": "paused_deny",
                "duration": DURATION_ONCE,
            }
        return {
            "decision": VERDICT_ALLOW,
            "reason": "paused",
            "duration": DURATION_ONCE,
        }

    def _register_hold(self, request: dict) -> asyncio.Future:
        """Register a held request + arm its timeout + fan out (interactive path)."""
        request_id = request["id"]
        workspace_id = request["workspace_id"]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        task = asyncio.create_task(self._timeout(request_id))
        self._holds[request_id] = {
            "future": fut,
            "workspace_id": workspace_id,
            "task": task,
        }
        self._fanout(request)
        return fut

    async def _finish_resolve(
        self,
        request_id: str,
        hold: dict,
        verdict: dict,
        resolved: str,
        allow_row: dict | None,
        deny_row: dict | None,
    ) -> None:
        """Resolve the hold's Future, broadcast to co-deciders, persist
        any ``forever`` rules, and refresh the rules view when a verdict
        landed."""
        if not hold["future"].done():
            hold["future"].set_result(verdict)
        self._broadcast_resolved(request_id, hold["workspace_id"], resolved)
        if allow_row is not None:
            await self._persist_forever_allow(allow_row)
        if deny_row is not None:
            await self._persist_forever_deny(deny_row)
        if resolved in ("allowed", "denied"):
            # A new verdict entered the in-effect set: refresh the deciders'
            # rule-management view (#2335 slice A) without a reconnect.
            await self._broadcast_rules(hold["workspace_id"])

    async def resolve(
        self,
        request_id: str,
        decision: str,
        decided_by: str,
        duration: str = DURATION_DEFAULT,
        *,
        decider_workspace: str | None = None,
    ) -> dict | None:
        """Apply a decider verdict to a held request (the #2244 hook).

        Records the decision in SQLite (allowed/denied), cancels the hold's
        timeout, resolves its Future, and broadcasts an ``egress_resolved``
        frame so co-deciders drop it (first-decision-wins across N deciders).
        Returns the verdict dict relayed to the sidecar, or ``None`` if the
        request is not currently held (already resolved, timed out, never
        held) or -- defense-in-depth -- if a workspace-scoped decider tries to
        decide a request outside its workspace (``decider_workspace``).
        """
        hold = self._holds.get(request_id)
        if hold is None:
            return None
        if not _hold_in_scope(hold, decider_workspace):
            # Restore nothing (we only peeked); the hold stays for a decider
            # actually scoped to it.
            return None
        hold = self._holds.pop(request_id)
        hold["task"].cancel()
        forever_allow_row: dict | None = None
        forever_deny_row: dict | None = None
        try:
            row = await self.app.state.model.egress_consent.decide(
                request_id, decision, decided_by, duration
            )
        except Exception:
            # decide() failed (DB error). The hold's own timeout is already
            # cancelled, so we MUST resolve the Future ourselves -- otherwise
            # the sidecar relay awaits it forever ("no hold pending forever").
            # Fail-close to deny + broadcast so co-deciders drop it, and
            # best-effort expire the row: a hold-less pending row can never
            # be resolved by anyone yet still occupies a pending-cap slot
            # (#3081).
            logger.exception("consent: resolve failed; fail-closing to deny")
            await self._expire_stranded(request_id)
            verdict = {"decision": VERDICT_DENY, "reason": "error"}
            resolved = "expired"
        else:
            (
                verdict,
                resolved,
                forever_allow_row,
                forever_deny_row,
            ) = _build_verdict(row, decision, duration)
        await self._finish_resolve(
            request_id,
            hold,
            verdict,
            resolved,
            forever_allow_row,
            forever_deny_row,
        )
        return verdict

    async def _persist_forever_allow(self, row: dict) -> None:
        """Persist a ``forever`` allow by appending ``host:port`` to the
        workspace's ``allowed_domains`` (#2368).

        The network sidecar re-reads ``allowed_domains`` on (re)start, so a
        forever allow survives a container/sidecar restart (the deciding
        connection already got its in-memory ACCEPT from the verdict). The
        entry is the consented ``host:port`` (the port the decider was shown;
        least-privilege -- a durable record is not broadened to all-ports).

        Best-effort: any failure is logged + swallowed so it can never break
        the verdict path or the post-verdict rules refresh -- the session
        still works; only the cross-restart durability is at risk.

        Port-less verdicts (``dest_port`` falsy, e.g. an ICMP ping) are NOT
        persisted -- a port-less bare host would broaden to all-ports (apex
        only, since bare is now exact).
        Direct-IP ``dest_host`` values are likewise poor candidates (the
        DNS-based allow-list never re-matches an IP literal after restart),
        but are left as-is here rather than special-cased.

        A ``forever`` allow now lives in BOTH ``allowed_domains`` and an
        ``egress_consent`` audit row; revoking it must remove both (#2370).
        """
        entry = _forever_allow_entry(row)
        if entry is None:
            return
        workspace_id = row.get("workspace_id")
        try:
            added = await self.app.state.model.workspaces.add_allowed_domain(
                workspace_id, entry
            )
        except Exception:
            logger.exception(
                "consent: forever-allow persist failed (%s) ws=%s",
                entry,
                str(workspace_id)[:8],
            )
            return
        if added:
            logger.info(
                "consent: forever allow -> allowed_domains %s ws=%s",
                entry,
                str(workspace_id)[:8],
            )
        else:
            logger.warning(
                "consent: forever allow not persisted (%s) ws=%s "
                "(workspace missing or malformed); session unaffected",
                entry,
                str(workspace_id)[:8],
            )

    async def _persist_forever_deny(self, row: dict) -> None:
        """Persist a ``forever`` deny by appending ``host:port`` to the
        workspace's ``rejected_domains`` (#2369) -- the mirror of
        :meth:`_persist_forever_allow`.

        The network sidecar re-reads ``rejected_domains`` on (re)start and
        NXDOMAINs a rejected name unconditionally, so a forever deny survives
        a container/sidecar restart (the deciding connection already got its
        in-memory REJECT from the verdict).

        Unlike the allow side, a *port-less* deny IS persisted (as a bare host),
        not skipped: the sidecar's reject enforcement is name-level
        (:func:`rejected_for` ignores port -- a rejected name is NXDOMAIN'd
        before resolution), so the whole host is the natural unit of a deny, and
        "block more" is the safe direction (no over-privilege concern, unlike
        an all-ports allow). Withholding a port-less deny would make a
        ``forever`` deny silently non-durable across restart. A ported verdict
        is stored as ``host:port`` (port retained for symmetry + the audit row,
        not scoping). Best-effort (failures logged + swallowed). A ``forever``
        deny also lives in an ``egress_consent`` audit row; revoking it must
        remove both (#2370).
        """
        entry = _forever_deny_entry(row)
        if entry is None:
            return
        workspace_id = row.get("workspace_id")
        try:
            added = await self.app.state.model.workspaces.add_rejected_domain(
                workspace_id, entry
            )
        except Exception:
            logger.exception(
                "consent: forever-deny persist failed (%s) ws=%s",
                entry,
                str(workspace_id)[:8],
            )
            return
        if added:
            logger.info(
                "consent: forever deny -> rejected_domains %s ws=%s",
                entry,
                str(workspace_id)[:8],
            )
        else:
            logger.warning(
                "consent: forever deny not persisted (%s) ws=%s "
                "(workspace missing or malformed); session unaffected",
                entry,
                str(workspace_id)[:8],
            )

    def _retract_spec(self, row: dict, decision: str):
        """``(remove callable, entry)`` for the durable-list retract, or
        ``None`` when there is nothing to retract."""
        host = row.get("dest_host")
        port = row.get("dest_port")
        if decision == DECISION_ALLOWED:
            if not port:
                return None  # port-less allow was never persisted
            remove = self.app.state.model.workspaces.remove_allowed_domain
            return remove, f"{host}:{port}"
        remove = self.app.state.model.workspaces.remove_rejected_domain
        return remove, (f"{host}:{port}" if port else host)

    async def _remove_list_entry(
        self, remove, workspace_id: str, entry: str, decision: str
    ) -> None:
        """Best-effort removal of a durable list entry with its logs."""
        try:
            retracted = await remove(workspace_id, entry)
        except Exception:
            logger.exception(
                "consent: forever-%s retract failed (%s) ws=%s",
                decision,
                entry,
                str(workspace_id)[:8],
            )
            return
        if retracted:
            logger.info(
                "consent: forever %s retracted from list (%s) ws=%s",
                decision,
                entry,
                str(workspace_id)[:8],
            )

    async def _retract_forever_entry(self, row: dict, decision: str) -> None:
        """Retract the durable list entry a ``forever`` verdict added (#2370).

        The inverse of :meth:`_persist_forever_allow` /
        :meth:`_persist_forever_deny`: removes the consented host from
        ``allowed_domains`` (allow) or ``rejected_domains`` (deny) so the
        verdict does not re-apply on the next sidecar restart. The deciding
        connection's in-memory ACCEPT/REJECT + ``_SESSION_HOST_ALLOWS``/
        ``_VERDICT_CACHE`` were already cleared by the drop the caller sent.

        Best-effort (failures logged + swallowed): the row is already revoked,
        so a persistence failure only risks the entry re-applying on restart,
        not a broken revoke. A port-less allow was never persisted
        (:meth:`_persist_forever_allow` skips it), so there is nothing to
        retract; a port-less deny was persisted as a bare host, so retract that.
        """
        host = row.get("dest_host")
        workspace_id = row.get("workspace_id")
        if not host or not workspace_id:
            return
        spec = self._retract_spec(row, decision)
        if spec is None:
            return
        remove, entry = spec
        await self._remove_list_entry(remove, workspace_id, entry, decision)

    def _revocable(
        self, row: dict | None, decider_workspace: str | None
    ) -> bool:
        """True when the row is an active verdict inside the decider's
        scope."""
        if row is None or row["decision"] not in (
            DECISION_ALLOWED,
            DECISION_DENIED,
        ):
            return False
        if (
            decider_workspace is not None
            and row["workspace_id"] != decider_workspace
        ):
            return False
        return True

    async def _sidecar_acked_drop(
        self, workspace_id: str, host: str, decision: str
    ) -> bool:
        """True when the sidecar dropped its rule (or none is live).

        No live sidecar -> nothing is enforced (its in-memory rules die with
        it), so proceed. A live sidecar must ack first: else a
        connected-but-unresponsive sidecar could keep enforcing after the
        view says "revoked"."""
        fut = self.app.state.sidecar_connections.send_drop(
            workspace_id, host, decision
        )
        if fut is None:
            return True
        try:
            return await asyncio.wait_for(fut, REVOKE_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            return False

    async def revoke(
        self,
        request_id: str,
        revoked_by: str,
        *,
        decider_workspace: str | None = None,
    ) -> bool:
        """Revoke an active consent verdict (#2339).

        Drops the sidecar's learned rule for the verdict's host (immediate,
        not waiting for the duration/restart) and marks the row ``revoked``.
        Returns True if revoked; False if the request isn't an active verdict,
        is outside the decider's workspace (``decider_workspace``), or the
        sidecar never acked the drop. Fail-closed: a connected-but-
        unresponsive sidecar leaves the row enforced rather than falsely
        marking it revoked (the view must not claim "not in effect" while the
        rule still fires).
        """
        row = await self.app.state.model.egress_consent.get_request(request_id)
        if not self._revocable(row, decider_workspace):
            return False
        workspace_id = row["workspace_id"]
        host = row["dest_host"]
        decision = row["decision"]
        # NOTE (#2370): a `forever` verdict also lives in the workspace's
        # allowed_domains/rejected_domains (added in resolve, #2368/#2369),
        # which the sidecar re-reads on restart. The drop above cleared the
        # in-memory rules + _SESSION_HOST_ALLOWS/_VERDICT_CACHE; retract the durable
        # entry too (after the row is marked revoked) so the verdict does not
        # re-apply on the next sidecar restart.
        # Ask the sidecar to drop its rule for this host+decision.
        if not await self._sidecar_acked_drop(workspace_id, host, decision):
            return False
        if (
            await self.app.state.model.egress_consent.revoke(
                request_id, revoked_by
            )
            is None
        ):
            return False
        # #2370: retract the durable list entry a `forever` verdict added so it
        # does not re-apply on the next sidecar restart. Best-effort (failures
        # logged + swallowed): the row is already revoked and the in-memory
        # rules already dropped, so a failure only risks re-application on
        # restart, not a broken revoke.
        if row.get("duration") == DURATION_FOREVER:
            await self._retract_forever_entry(row, decision)
        await self._broadcast_rules(workspace_id)
        return True

    async def _timeout(self, request_id: str) -> None:
        """Auto-expire a held request after the timeout; fail-close on wake.

        ``resolve``/``stop`` **pop the hold before cancelling this task**, with
        no ``await`` between the pop and the cancel -- so a not-yet-woken
        timeout is cancelled (the ``except CancelledError`` returns) and never
        reaches ``_fail_close``. If this coroutine *does* wake past the sleep,
        no one popped in the meantime, so the hold is still present.
        ``_fail_close`` nonetheless pops first and no-ops if the hold is already
        gone (e.g. a ``stop`` racing this wake) -- that pop-``None`` guard is
        load-bearing; do not remove it.
        """
        try:
            await asyncio.sleep(self.timeout)
        except asyncio.CancelledError:
            return
        await self._fail_close(request_id, reason="timeout")

    async def _fail_close(self, request_id: str, *, reason: str) -> None:
        """Expire the pending row and resolve the Future deny (timeout/shutdown).

        Does NOT cancel the hold's own task -- the caller owns that (``_timeout``
        is the task itself; ``stop`` cancels before calling). Pop-first: if
        :meth:`resolve` already popped, this is a no-op.
        """
        hold = self._holds.pop(request_id, None)
        if hold is None:
            return
        await self._expire_stranded(request_id)
        if not hold["future"].done():
            hold["future"].set_result(
                {
                    "decision": VERDICT_DENY,
                    "reason": reason,
                    "duration": DURATION_ONCE,
                }
            )
        self._broadcast_resolved(request_id, hold["workspace_id"], "expired")

    async def _expire_stranded(self, request_id: str) -> None:
        """Best-effort expire of a pending row whose hold is gone (#3081).

        Called only after the hold was popped (``resolve``'s decide-failure
        arm, ``_fail_close``): a row left ``pending`` with no live hold can
        never be resolved -- ``snapshot()`` replays only rows still held,
        and a verdict for its id returns ``None`` -- yet ``count_pending``
        keeps counting it against the workspace's pending cap until the
        retention sweep (default 30 days) or the startup reaper, so enough
        of them wedge every new hold into ``rate_limited`` denials.
        Retried once -- a first failure is typically a transient DB error,
        and the retry re-runs after the failing transaction unwound. A
        second failure is logged and the row falls back to the startup
        reaper (``expire_all_pending``).
        """
        for attempt in (1, 2):
            try:
                await self.app.state.model.egress_consent.expire_pending(
                    request_id
                )
                return
            except Exception:
                logger.exception(
                    "consent: failed to expire held request %s (attempt %d)",
                    request_id[:8],
                    attempt,
                )

    def _fanout(self, request: dict) -> None:
        """Broadcast the pending request to the workspace's deciders (#2244).

        Pushes an ``egress_request`` frame to every live decider for the
        workspace. Until a decider responds with a verdict,
        the hold simply waits for its timeout (or a ``resolve`` call) -- the
        hold is correct end-to-end regardless: it fail-closes on timeout.
        """
        workspace_id = request["workspace_id"]
        self.app.state.consent_deciders.broadcast(
            workspace_id, self._request_frame(request)
        )
        logger.info(
            "consent hold created: ws=%s %s:%s id=%s",
            str(workspace_id)[:8],
            request.get("dest_host"),
            request.get("dest_port"),
            str(request.get("id"))[:8],
        )

    async def snapshot(self, workspace_id: str) -> list[dict]:
        """Pending-request frames for a newly-connected decider (replay).

        A decider that connects mid-flight sees the workspace's current holds
        so it can act on in-flight requests, not just ones created after it
        joined.
        """
        rows = await self.app.state.model.egress_consent.list_requests(
            workspace_id, decision=DECISION_PENDING
        )
        # Only rows still currently held: a request pending in the DB but
        # already popped from ``_holds`` (resolve/timeout in flight) must not
        # be replayed to a reconnecting decider -- its ``egress_resolved``
        # broadcast may have been lost on the prior (dead) connection, and
        # replaying it would re-add an already-resolved request that then
        # lingers with no further resolve to clear it (#2345 e2e flake).
        # ``_holds.pop`` is synchronous in resolve/timeout (before the DB
        # write + broadcast), so this membership check is race-free.
        return [
            self._request_frame(row)
            for row in rows
            if row["id"] in self._holds
        ]

    async def rules_frame(self, workspace_id: str) -> dict | None:
        """Build an ``egress_rules`` snapshot for a workspace (#2335 slice A).

        The in-effect consent verdicts (grouped allow/deny) + the static
        allow-list, for the decider's rule-management view. Returns None for a
        missing/deleted workspace (the caller skips the frame).
        """
        ws = await self.app.state.model.workspaces.get_workspace(workspace_id)
        if ws is None:
            return None
        return await self._rules_frame_for(ws, workspace_id)

    async def _rules_frame_for(self, ws: dict, workspace_id: str) -> dict:
        """The assembled frame: static lists, grouped in-effect verdicts,
        and the live pause window (#2332, read fresh so a self-expired pause
        shows as cleared)."""
        rows = await self.app.state.model.egress_consent.list_active(
            workspace_id
        )
        until = await self.app.state.model.workspaces.get_consent_pause(
            workspace_id
        )
        return {
            "type": "egress_rules",
            "workspace_id": workspace_id,
            "allow_list": ws.get("allowed_domains") or [],
            # #2370: surface the reject list alongside the allow list (#2340).
            "reject_list": ws.get("rejected_domains") or [],
            "allowed": _by_decision(rows, DECISION_ALLOWED),
            "denied": _by_decision(rows, DECISION_DENIED),
            "paused": _pause_frame(until),
        }

    @staticmethod
    def _request_frame(request: dict) -> dict:
        """Build an ``egress_request`` frame from a consent-request row."""
        return {
            "type": "egress_request",
            "workspace_id": request["workspace_id"],
            "request": request,
        }

    def _broadcast_resolved(
        self, request_id: str, workspace_id: str, decision: str
    ) -> None:
        """Tell the workspace's deciders a request is no longer pending.

        Sent on verdict (``resolve``) and timeout/shutdown (``_fail_close``)
        so co-deciders drop it from their queue (first-decision-wins).
        """
        self.app.state.consent_deciders.broadcast(
            workspace_id,
            {
                "type": "egress_resolved",
                "workspace_id": workspace_id,
                "request_id": request_id,
                "decision": decision,
            },
        )

    async def _broadcast_rules(self, workspace_id: str) -> None:
        """Push a refreshed ``egress_rules`` frame to the workspace's deciders.

        Called after a verdict lands (and, in slice C, after a revoke) so the
        deciders' rule-management view reflects the new in-effect set without a
        reconnect. Best-effort: a refresh failure (DB read) is logged +
        swallowed so it can never break the verdict path that called this --
        the sidecar already has its verdict.
        """
        try:
            frame = await self.rules_frame(workspace_id)
        except Exception:
            logger.exception("consent: rules refresh broadcast failed")
            return
        if frame is not None:
            self.app.state.consent_deciders.broadcast(workspace_id, frame)

    async def is_interactive(self, workspace_id: str) -> bool:
        """Interactive iff the workspace opted in AND a live decider exists (#2308)."""
        return await workspace_is_interactive(self.app, workspace_id)

    async def _is_allow(self, workspace_id: str) -> bool:
        """egress_mode == allow: default-permit (record + allow, no prompt) (#2406)."""
        ws = await self.app.state.model.workspaces.get_workspace(workspace_id)
        return bool(ws) and ws.get("egress_mode") == EGRESS_MODE_ALLOW
