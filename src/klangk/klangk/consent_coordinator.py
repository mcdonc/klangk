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

from .consent import workspace_is_interactive

logger = logging.getLogger(__name__)

# Verdicts sent back to the sidecar over its WS relay (#2311).
VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"


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
        scope: str | None,
        decided_by: str,
    ) -> dict | None:
        """Apply a decider verdict to a held request (the #2244 hook).

        Records the decision in SQLite (allowed/denied), cancels the hold's
        timeout, and resolves its Future. Returns the verdict dict relayed to
        the sidecar, or ``None`` if the request is not currently held (already
        resolved, timed out, or never held).
        """
        hold = self._holds.pop(request_id, None)
        if hold is None:
            return None
        hold["task"].cancel()
        row = await self.app.state.model.egress_consent.decide(
            request_id, decision, scope, decided_by
        )
        if row is None:
            verdict = {"decision": VERDICT_DENY, "reason": "gone"}
        elif decision == "allowed":
            verdict = {"decision": VERDICT_ALLOW, "reason": "decided"}
        else:
            verdict = {"decision": VERDICT_DENY, "reason": "decided"}
        if not hold["future"].done():
            hold["future"].set_result(verdict)
        return verdict

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
                {"decision": VERDICT_DENY, "reason": reason}
            )

    def _fanout(self, request: dict) -> None:
        """Broadcast the pending request to the workspace's deciders (#2244).

        Stub: until #2244 wires the decider WebSocket fanout, a hold simply
        waits for its timeout (or a ``resolve`` call). The hold is correct
        end-to-end without the fanout -- it fail-closes on timeout.
        """
        logger.info(
            "consent hold created: ws=%s %s:%s id=%s (fanout via #2244)",
            str(request.get("workspace_id"))[:8],
            request.get("dest_host"),
            request.get("dest_port"),
            str(request.get("id"))[:8],
        )

    async def _is_interactive(self, workspace_id: str) -> bool:
        """Interactive iff the workspace opted in AND a live decider exists (#2308)."""
        return await workspace_is_interactive(self.app, workspace_id)
