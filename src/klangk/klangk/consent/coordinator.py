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
  denial (no hold, no pending row, deny verdict at once);
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

from .egress import workspace_is_interactive
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
_REVOKE_ACK_TIMEOUT = 5.0

# Consent-pause durations (#2332): how long interactive prompting is silenced
# workspace-wide. While paused, a destination with no allow-list rule and no
# in-effect recorded verdict is auto-allowed (no hold, no prompt); a recorded
# deny still blocks. A focused set for the TUI control; ``unpause`` clears it.
_PAUSE_SECONDS = {"15m": 900, "1h": 3600, "1d": 86400}


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

    async def hold(
        self, workspace_id: str, dst: str, dport: int | None
    ) -> asyncio.Future:
        """Gate-check a blocked egress and create a hold; return its verdict Future.

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
        loop = asyncio.get_running_loop()
        try:
            if await self._is_allow(workspace_id):
                # #2406: egress_mode == allow is default-permit. Record the
                # off-list destination (logging -- mirrors how static records
                # a denial via record_static_denial) and allow at once -- no
                # hold, no prompt. Behaves as if an internal always-allow
                # decider were registered, but is a short-circuit branch (no
                # decider object, no consent round-trip beyond the verdict).
                # rejected_domains is enforced earlier at the sidecar DNS layer
                # (rejected_for -> NXDOMAIN), so a host reaching this gate is
                # already not rejected. duration=tilrestart (DURATION_DEFAULT)
                # so the sidecar learns the IP all-ports for its lifetime (no
                # per-connection re-prompt); nothing persists to
                # allowed_domains (this branch bypasses resolve()).
                await self.app.state.model.egress_consent.record_static_allow(
                    workspace_id, dst, dport
                )
                fut = loop.create_future()
                fut.set_result(
                    {
                        "decision": VERDICT_ALLOW,
                        "reason": "allow_mode",
                        "duration": DURATION_DEFAULT,
                    }
                )
                return fut
            if await self._is_paused(workspace_id):
                # #2332: prompting is paused workspace-wide. A destination
                # with an in-effect recorded DENY is still blocked (the pause
                # does not override existing verdicts); everything else is
                # auto-allowed (no hold, no prompt -- the sidecar
                # learns/accepts the SYN like a static allow). Allow-list
                # rules are enforced earlier at the sidecar DNS layer, so a
                # host reaching this gate is already not allow-listed.
                verdict = await self._paused_verdict(workspace_id, dst, dport)
                if verdict["decision"] == VERDICT_ALLOW:
                    # #2304: an auto-allow is still an observed egress
                    # outcome -- record it (policy, decided_by NULL) so
                    # auditing is unconditional. A paused-deny is a replay
                    # of a verdict whose row already exists; no new row.
                    try:
                        model = self.app.state.model.egress_consent
                        await model.record_static_allow(
                            workspace_id, dst, dport
                        )
                    except Exception:
                        logger.exception(
                            "consent: paused-allow recording failed"
                        )
                fut = loop.create_future()
                fut.set_result(verdict)
                return fut
            if not await self._is_interactive(workspace_id):
                await self.app.state.model.egress_consent.record_static_denial(
                    workspace_id, dst, dport
                )
                fut: asyncio.Future = loop.create_future()
                fut.set_result({"decision": VERDICT_DENY, "reason": "static"})
                return fut
            consent = self.app.state.model.egress_consent
            if await consent.count_pending(workspace_id) >= self.rate_limit:
                fut = loop.create_future()
                fut.set_result(
                    {"decision": VERDICT_DENY, "reason": "rate_limited"}
                )
                return fut
            request = await consent.create_request(workspace_id, dst, dport)
            if request is None:
                fut = loop.create_future()
                fut.set_result(
                    {"decision": VERDICT_DENY, "reason": "duplicate"}
                )
                return fut
            return self._register_hold(request)
        except Exception:
            # A model/DB failure must not crash the relay or strand the hold:
            # fail-close to a deny verdict so the sidecar NXDOMAIN/DROPs. The
            # kernel keeps the connection blocked either way (fail-closed), but
            # this gives the sidecar a definitive deny instead of silence.
            logger.exception("consent: hold failed; fail-closing to deny")
            fut = loop.create_future()
            fut.set_result({"decision": VERDICT_DENY, "reason": "error"})
            return fut

    async def pause(self, workspace_id: str, duration: str) -> dict:
        """Pause interactive consent prompting workspace-wide for ``duration`` (#2332).

        While paused, :meth:`hold` auto-allows any destination that has no
        allow-list rule and no in-effect recorded verdict (instead of holding
        it for a decider); a recorded deny still blocks. Sets
        ``consent_paused_until`` on the workspace to ``now + duration`` and
        broadcasts a refreshed ``egress_rules`` frame so every decider sees
        the pause window. Returns ``{"ok": bool, "until": float | None}`` --
        ``ok`` is False for an unknown duration or a missing workspace.
        """
        secs = _PAUSE_SECONDS.get(duration)
        if secs is None:
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
        """
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
        if (
            decider_workspace is not None
            and hold["workspace_id"] != decider_workspace
        ):
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
            # Fail-close to deny + broadcast so co-deciders drop it; the
            # pending row is left for GC.
            logger.exception("consent: resolve failed; fail-closing to deny")
            verdict = {"decision": VERDICT_DENY, "reason": "error"}
            resolved = "expired"
        else:
            if row is None:
                verdict = {"decision": VERDICT_DENY, "reason": "gone"}
                resolved = "expired"
            elif decision == "allowed":
                verdict = {
                    "decision": VERDICT_ALLOW,
                    "reason": "decided",
                    "duration": duration,
                }
                resolved = "allowed"
                # A `forever` allow persists by mutating the workspace's
                # allow-list (#2368) so it survives a container/sidecar
                # restart. Captured here, persisted after the Future is
                # resolved (the deciding connection gets its in-memory ACCEPT
                # first) and before the rules refresh (so the view reflects
                # the new entry).
                if duration == DURATION_FOREVER:
                    forever_allow_row = row
            else:
                verdict = {
                    "decision": VERDICT_DENY,
                    "reason": "decided",
                    "duration": duration,
                }
                resolved = "denied"
                # A `forever` deny persists by mutating the workspace's
                # deny-list (#2369) -- the mirror of the allow side above.
                if duration == DURATION_FOREVER:
                    forever_deny_row = row
        if not hold["future"].done():
            hold["future"].set_result(verdict)
        self._broadcast_resolved(request_id, hold["workspace_id"], resolved)
        if forever_allow_row is not None:
            await self._persist_forever_allow(forever_allow_row)
        if forever_deny_row is not None:
            await self._persist_forever_deny(forever_deny_row)
        if resolved in ("allowed", "denied"):
            # A new verdict entered the in-effect set: refresh the deciders'
            # rule-management view (#2335 slice A) without a reconnect.
            await self._broadcast_rules(hold["workspace_id"])
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
        host = row.get("dest_host")
        port = row.get("dest_port")
        workspace_id = row.get("workspace_id")
        if not host or not workspace_id:
            return
        if not port:
            # Port-scoped only (#2368): a port-less verdict (e.g. an ICMP
            # ping, dest_port 0) must not be persisted as a bare host -- the
            # sidecar treats a port-less spec as all-ports on the apex (bare is
            # exact), durably broadening one connection's consent to every port
            # on that host. The deciding connection still gets its in-memory
            # ACCEPT for this session; durability is simply withheld.
            logger.info(
                "consent: forever allow of port-less dest %s ws=%s not "
                "persisted (a bare host would broaden to all-ports); "
                "session still works",
                host,
                str(workspace_id)[:8],
            )
            return
        entry = f"{host}:{port}"
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
        host = row.get("dest_host")
        port = row.get("dest_port")
        workspace_id = row.get("workspace_id")
        if not host or not workspace_id:
            return
        entry = f"{host}:{port}" if port else host
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
        port = row.get("dest_port")
        workspace_id = row.get("workspace_id")
        if not host or not workspace_id:
            return
        if decision == DECISION_ALLOWED:
            if not port:
                return  # port-less allow was never persisted
            entry = f"{host}:{port}"
            remove = self.app.state.model.workspaces.remove_allowed_domain
        else:
            entry = f"{host}:{port}" if port else host
            remove = self.app.state.model.workspaces.remove_rejected_domain
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
        if row is None or row["decision"] not in (
            DECISION_ALLOWED,
            DECISION_DENIED,
        ):
            return False
        workspace_id = row["workspace_id"]
        if decider_workspace is not None and workspace_id != decider_workspace:
            return False
        host = row["dest_host"]
        decision = row["decision"]
        # NOTE (#2370): a `forever` verdict also lives in the workspace's
        # allowed_domains/rejected_domains (added in resolve, #2368/#2369),
        # which the sidecar re-reads on restart. The drop above cleared the
        # in-memory rules + _SESSION_HOST_ALLOWS/_VERDICT_CACHE; retract the durable
        # entry too (after the row is marked revoked) so the verdict does not
        # re-apply on the next sidecar restart.
        # Ask the sidecar to drop its rule for this host+decision. No live
        # sidecar -> nothing is enforced (its in-memory rules die with it), so
        # proceed to mark revoked. A live sidecar must ack first: else a
        # connected-but-unresponsive sidecar could keep enforcing after the
        # view says "revoked".
        fut = self.app.state.sidecar_connections.send_drop(
            workspace_id, host, decision
        )
        if fut is not None:
            try:
                ok = await asyncio.wait_for(fut, _REVOKE_ACK_TIMEOUT)
            except asyncio.TimeoutError:
                ok = False
            if not ok:
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
        try:
            await self.app.state.model.egress_consent.expire_pending(
                request_id
            )
        except Exception:
            logger.exception(
                "consent: failed to expire held request %s", request_id[:8]
            )
        if not hold["future"].done():
            hold["future"].set_result(
                {
                    "decision": VERDICT_DENY,
                    "reason": reason,
                    "duration": DURATION_ONCE,
                }
            )
        self._broadcast_resolved(request_id, hold["workspace_id"], "expired")

    def _fanout(self, request: dict) -> None:
        """Broadcast the pending request to the workspace's deciders (#2244).

        Pushes an ``egress_request`` frame to every live decider for the
        workspace (and deploy-wide). Until a decider responds with a verdict,
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

    async def snapshot(self, workspace_id: str | None) -> list[dict]:
        """Pending-request frames for a newly-connected decider (replay).

        A decider that connects mid-flight sees the workspace's current holds
        so it can act on in-flight requests, not just ones created after it
        joined. Deploy-wide deciders (``workspace_id`` None) get no snapshot
        for now -- a cross-workspace pending list is a follow-up. Caveat: a
        deploy-only decider connecting after holds exist will not see them, so
        those holds time out fail-closed; they still receive NEW holds live via
        :meth:`_fanout`.
        """
        if workspace_id is None:
            return []
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

    async def rules_frame(self, workspace_id: str | None) -> dict | None:
        """Build an ``egress_rules`` snapshot for a workspace (#2335 slice A).

        The in-effect consent verdicts (grouped allow/deny) + the static
        allow-list, for the decider's rule-management view. Returns None for a
        missing/deleted workspace (the caller skips the frame); deploy-wide
        deciders (workspace None) get no frame for now (a cross-workspace view
        is a follow-up, matching :meth:`snapshot`).
        """
        if workspace_id is None:
            return None
        ws = await self.app.state.model.workspaces.get_workspace(workspace_id)
        if ws is None:
            return None
        rows = await self.app.state.model.egress_consent.list_active(
            workspace_id
        )
        until = await self.app.state.model.workspaces.get_consent_pause(
            workspace_id
        )
        now = time.time()
        paused = (
            {"paused": True, "until": until}
            if until is not None and until > now
            else None
        )
        return {
            "type": "egress_rules",
            "workspace_id": workspace_id,
            "allow_list": ws.get("allowed_domains") or [],
            # #2370: surface the reject list alongside the allow list (#2340).
            "reject_list": ws.get("rejected_domains") or [],
            "allowed": [r for r in rows if r["decision"] == DECISION_ALLOWED],
            "denied": [r for r in rows if r["decision"] == DECISION_DENIED],
            # #2332 (pause control): the live pause window, or None when not
            # paused (read fresh so a self-expired pause shows as cleared).
            "paused": paused,
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

    async def _is_interactive(self, workspace_id: str) -> bool:
        """Interactive iff the workspace opted in AND a live decider exists (#2308)."""
        return await workspace_is_interactive(self.app, workspace_id)

    async def _is_allow(self, workspace_id: str) -> bool:
        """egress_mode == allow: default-permit (record + allow, no prompt) (#2406)."""
        ws = await self.app.state.model.workspaces.get_workspace(workspace_id)
        return bool(ws) and ws.get("egress_mode") == EGRESS_MODE_ALLOW

    async def record_dns_event(
        self, workspace_id: str, decision: str, host: str
    ) -> None:
        """Record a DNS-layer egress outcome from the sidecar (#2304).

        The sidecar's DNS proxy reports every outcome it itself decides --
        ``allowed`` (allow-list / in-session-allowed name forwarded + learned)
        or ``denied`` (reject-listed name NXDOMAIN'd) -- unconditionally, so
        full egress auditing needs no opt-in setting. Routed to the existing
        static policy rows (``decided_by`` NULL, port NULL): one row per
        (workspace, host) via the dedup indexes, pruned by the retention
        sweeper like any other terminal row. Best-effort -- a recording
        failure is logged and swallowed (an audit gap must never break the
        relay loop or the sidecar's egress path).
        """
        try:
            consent = self.app.state.model.egress_consent
            if decision == DECISION_ALLOWED:
                await consent.record_static_allow(workspace_id, host, None)
            elif decision == DECISION_DENIED:
                await consent.record_static_denial(workspace_id, host, None)
        except Exception:
            logger.exception(
                "consent: dns egress recording failed (%s %s)",
                decision,
                str(host)[:60],
            )
