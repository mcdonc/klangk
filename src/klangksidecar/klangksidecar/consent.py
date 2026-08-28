"""Persistent WS client to klangkd's /ws/egress-sidecar (#2311 half B, #2450).

One socket per workspace, reconnected on drop. request() sends an egress frame
and awaits the matching verdict; fail-close -> deny so the workspace never
hangs. _handle_drop_rule applies klangkd revocations (#2339).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
import websockets

from . import allowlist, rules
from .config import ACTIVITY_GATE_S, DEBUG
from .state import _VERDICT_CACHE, _clear_verdict_cache

# ---------------------------------------------------------------------------
# Interactive consent: egress-sidecar WS client + hold paths (#2311 half B).
# ---------------------------------------------------------------------------


def _jittered_gate() -> float:
    """The activity flood gate, jittered to 0.5x-1.0x of :data:`ACTIVITY_GATE_S`.

    Full-jitter (the AWS-backoff idiom): the suppression window lands randomly
    in [0.5x, 1.0x] of the base, so the per-workspace send cadence drifts and a
    fleet of sidecars never herds onto a synchronized frame rate against
    klangkd. The floor stays at half the base, so the window never collapses.
    """
    return ACTIVITY_GATE_S * random.uniform(0.5, 1.0)


def _ws_url(consent_url: str) -> str:
    """Derive the ``/ws/egress-sidecar`` WS URL from the consent HTTP URL.

    ``http(s)://host:port/...`` -> ``ws(s)://host:port/ws/egress-sidecar``.
    Reuses :data:`CONSENT_URL`'s scheme+host so no new env var is needed; the
    sidecar leg of the consent contract (``sidecar <-WS-> klangkd``).
    """
    if consent_url.startswith("https://"):
        host = consent_url[len("https://") :].split("/", 1)[0]
        return f"wss://{host}/ws/egress-sidecar"
    if consent_url.startswith("http://"):
        host = consent_url[len("http://") :].split("/", 1)[0]
        return f"ws://{host}/ws/egress-sidecar"
    return consent_url  # already ws(s)://, or an exotic scheme used verbatim


class SidecarConsentClient:
    """Persistent WS client to klangkd's ``/ws/egress-sidecar`` (#2311 half B).

    One socket per workspace, opened at startup, reconnected (exponential
    backoff) on drop. :meth:`request` sends an egress frame and awaits the
    matching verdict frame by id. **Fail-close**: a down connection or a
    timed-out verdict resolves to ``"deny"`` immediately, so the proxy
    NXDOMAIN/DROPs (today's static behavior) when klangkd or the decider is
    unreachable -- the workspace never hangs on a pending connection.

    All ``_pending`` state lives on the event-loop thread (coroutines +
    ``_run``'s receive loop); the NFQUEUE consumer is itself loop-driven
    (``get_fd`` + ``add_reader``) and calls :meth:`request` directly, so it
    never crosses threads or touches ``_pending`` from outside the loop.
    """

    def __init__(self, consent_url: str, token_path: str, hold_timeout: float) -> None:
        self._url = _ws_url(consent_url)
        self._token_path = token_path
        self._hold_timeout = hold_timeout
        self._ws = None  # current websockets connection, or None
        self._connected = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}  # id -> Future
        self._stop = False
        self._no_token_warned = False
        self._task: asyncio.Task | None = None
        # Idle-activity flood gate (#2479): monotonic timestamp of the last
        # ``{type:activity}`` frame sent. 0.0 so the first event always forwards.
        self._last_activity_send = 0.0
        self._activity_tasks: set[asyncio.Task] = set()
        # DNS-layer outcome reporting (#2304): hosts already reported this WS
        # session (see :meth:`record_dns`). Cleared on reconnect -- a fresh
        # session against a possibly restarted klangkd re-reports once per
        # host (the DB-side dedup index still collapses repeats).
        self._dns_reported: set[str] = set()
        self._dns_tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                # CancelledError first (#2657): the cancel we just issued
                # makes the awaited task raise it wherever it is parked
                # (reconnect backoff, token-retry sleep), and it is a
                # BaseException (3.8+) that `except Exception` alone let
                # escape -- aborting _shutdown (nfq.unbind/sock.close
                # skipped) and dumping a raw traceback on every workspace
                # removal whose WS was down. Same guard shape _shutdown
                # uses for its sweep/sampler awaits (app.py).
                pass
        self._fail_close_pending()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            token = self._read_token()
            if not token:
                # Token file not present yet (workspace JWT not written). Retry
                # without escalating backoff (expected at startup).
                if DEBUG and not self._no_token_warned:
                    print(
                        "consent: workspace token not yet present; retrying",
                        flush=True,
                    )
                    self._no_token_warned = True
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    additional_headers={"Authorization": "Bearer " + token},
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self._no_token_warned = False
                    backoff = 1.0
                    if DEBUG:
                        print(f"consent: connected to {self._url}", flush=True)
                    async for raw in ws:
                        await self._dispatch(raw)
            except Exception as exc:
                # Log the exception TYPE only -- the connection carries the
                # workspace JWT (Authorization header), and a websockets
                # exception could embed request details (#2309). The websockets
                # package logger is capped at WARNING on import so its DEBUG
                # request-line can't leak either.
                if DEBUG:
                    print(
                        f"consent: connection error: {type(exc).__name__}",
                        flush=True,
                    )
            finally:
                # Disconnected (clean close, error, or crash): the sidecar's
                # held connections die with it (fail-close). Any in-flight
                # request resolves to deny so its caller NXDOMAIN/DROPs.
                self._ws = None
                self._connected.clear()
                self._fail_close_pending()
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)  # capped exponential backoff

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "verdict":
            self._apply_verdict(msg)
        elif mtype == "drop_rule":
            await self._handle_drop_rule(msg)

    def _apply_verdict(self, msg: dict) -> None:
        vid = msg.get("id")
        decision = msg.get("decision")
        if not isinstance(vid, str):
            return
        fut = self._pending.pop(vid, None)
        if fut is not None and not fut.done():
            # Any non-"allow" verdict (deny, expired, malformed) -> deny.
            token = decision if decision == "allow" else "deny"
            duration = msg.get("duration") or "once"
            fut.set_result((token, duration))

    async def _handle_drop_rule(self, msg: dict) -> None:
        """klangkd asked us to drop a host's rules (revocation, #2339).

        Runs :func:`drop_for_host` off the loop (it forks iptables) and acks
        back so klangkd marks the verdict revoked only once the rule is gone.
        """
        ack_id = msg.get("id")
        host = msg.get("host")
        decision = msg.get("decision")
        ok = False
        if isinstance(host, str) and decision in ("allowed", "denied"):
            # #2370: close the session-allow gates BEFORE the executor window.
            # While drop_for_host forks iptables off-loop (~tens of ms), a racing
            # SYN/DNS could read _SESSION_HOST_ALLOWS and re-install a fresh
            # ACCEPT (the host's remaining allow TTL) the revoke never clears.
            # _drop_session_hosts runs on the loop (loop-only structure) and
            # makes _cb's _session_host_allows_ttl + ports_for deny during the
            # window. (A deny never adds to _SESSION_HOST_ALLOWS, so skip it
            # there.)
            if decision == "allowed":
                allowlist._drop_session_hosts(host)
            elif decision == "denied":
                # Clear the host-scoped deny memory (#2446) BEFORE drop_for_host
                # forks iptables, so a SYN arriving during that window re-prompts
                # instead of staying auto-denied (mirror of the allow revoke).
                allowlist._drop_session_denies(host)
            try:
                ips = await asyncio.get_running_loop().run_in_executor(
                    None, rules.drop_for_host, host, decision
                )
                # Drop the host's cached verdicts on the loop AFTER the rule
                # removal (loop-only dict). A cache hit only pkt.accept()s a
                # retransmit -- it does not re-install an ACCEPT -- so this is
                # safe after the drop and needs no window protection.
                _clear_verdict_cache(ips)
                # The ack means "the rule is dropped" (#2339); the loop-state
                # clears above are pure dict ops and don't affect it.
                ok = True
            except Exception:
                ok = False
        if isinstance(ack_id, str) and self._ws is not None:
            try:
                await self._ws.send(
                    json.dumps({"type": "drop_ack", "id": ack_id, "ok": ok})
                )
            except Exception:
                pass

    async def request(self, dst: str, dport: int | None) -> tuple[str, str]:
        """Send an egress frame + await the verdict. Fail-close -> ``("deny",
        "once")``.

        Returns ``(decision, duration)`` where decision is ``"allow"`` or
        ``"deny"`` and duration is the verdict's duration token (#2328). A
        down connection returns the fail-close pair at once (no frame sent); a
        timed-out or disconnected-in-flight request resolves the same way.
        """
        if not self._connected.is_set() or self._ws is None:
            return "deny", "once"
        lid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[lid] = fut
        frame = json.dumps({"type": "egress", "id": lid, "dst": dst, "dport": dport})
        try:
            await self._ws.send(frame)
        except Exception:
            self._pending.pop(lid, None)
            return "deny", "once"
        try:
            return await asyncio.wait_for(fut, self._hold_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(lid, None)
            return "deny", "once"

    def bump_activity(self) -> None:
        """Best-effort, flood-gated idle-activity signal to klangkd (#2479).

        Called from the DNS proxy (every query) and the NFQUEUE consumer (every
        queued connection SYN) so klangkd's idle timer reflects egress-only
        workloads, whose traffic bypasses the daemon entirely and would
        otherwise be reaped by the idle timeout. Throttled by a jittered flood
        gate (:func:`_jittered_gate` -- 0.5x-1.0x of :data:`ACTIVITY_GATE_S`):
        a connect-heavy workload (a build, a crawler) generates a bounded,
        desynchronized frame rate, while the first event after a quiet period
        (>= the jittered window since the last send) forwards at once so a
        single connect after a long idle stretch resets the timer promptly. A
        dropped / disconnected send is silent -- activity signaling must never
        break egress. Sync-safe: the NFQUEUE callback runs on the loop thread
        but is itself sync, so the coroutine WS send is scheduled as a task.
        """
        if not self._connected.is_set() or self._ws is None:
            return
        now = time.monotonic()
        if now - self._last_activity_send < _jittered_gate():
            return
        self._last_activity_send = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not on a loop (defensive) -> nothing to schedule
        t = loop.create_task(self._send_activity())
        self._activity_tasks.add(t)  # strong ref so the send isn't GC'd mid-flight
        t.add_done_callback(self._activity_tasks.discard)

    async def _send_activity(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"type": "activity"}))
        except Exception:
            pass  # best-effort: a dropped frame just delays the next idle bump

    # Cap on the DNS-outcome dedup set (#2304): bounds memory (and frames for
    # new hosts) under a hostile resolver storm. Past the cap, hosts already
    # reported keep their dedup; new hosts are simply not reported this
    # session (their egress still flows/gets gated normally -- only the audit
    # frame is dropped). Mirrors _VERDICT_CACHE's 4096 discipline in state.py.
    _DNS_REPORT_CAP = 4096

    def record_dns(self, decision: str, host: str) -> None:
        """Best-effort DNS-layer egress outcome report to klangkd (#2304).

        The DNS proxy sees every FQDN egress attempt and now ALWAYS reports
        the outcome -- allowed (allow-list / in-session allow) or denied
        (reject-list / static off-list NXDOMAIN) -- so full egress auditing
        is unconditional, not an opt-in setting. klangkd records a
        policy-decided row (decided_by NULL) per (workspace, host); this
        sidecar-side dedup (one frame per host per WS session) keeps a query
        storm from re-sending hosts the DB-side unique index would collapse
        anyway. Cleared on reconnect (fresh session). Sync-safe: called from
        the DNS loop's coroutines, schedules the WS send as a task. Never
        raises -- audit must not break egress.
        """
        if not self._connected.is_set() or self._ws is None:
            return
        if host in self._dns_reported:
            return
        if len(self._dns_reported) >= self._DNS_REPORT_CAP:
            return
        self._dns_reported.add(host)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not on a loop (defensive) -> nothing to schedule
        t = loop.create_task(self._send_dns(decision, host))
        self._dns_tasks.add(t)  # strong ref so the send isn't GC'd mid-flight
        t.add_done_callback(self._dns_tasks.discard)

    async def _send_dns(self, decision: str, host: str) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(
                json.dumps({"type": "egress_dns", "decision": decision, "host": host})
            )
        except Exception:
            pass  # best-effort: a dropped frame skips this host's audit row

    def _fail_close_pending(self) -> None:
        # A lost connection is a fresh session against a (possibly restarted)
        # coordinator, so prior verdicts must not be trusted: in-flight flows
        # re-prompt after reconnect instead of being silently re-allowed/denied
        # by a stale cached verdict (#2326 review).
        _VERDICT_CACHE.clear()
        # Fresh session (#2304) also resets the DNS-outcome dedup: the next
        # connection may face a restarted klangkd whose egress_consent rows
        # may have been pruned, so each host's outcome is re-reported once.
        self._dns_reported.clear()
        for lid in list(self._pending):
            fut = self._pending.pop(lid, None)
            if fut is not None and not fut.done():
                fut.set_result(("deny", "once"))

    def _read_token(self) -> str:
        try:
            with open(self._token_path) as f:
                return f.read().strip()
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# Idle-activity sampler (#2485): poll the workspace-egress byte counter and
# bump the idle timer on real traffic (long-lived / UDP flows the #2481 DNS+SYN
# hooks miss). The per-tick logic is factored into _activity_delta so it is
# unit-testable without driving the infinite loop.
# ---------------------------------------------------------------------------


def _safe_bytes(get_bytes) -> int:
    """Read the accounting counter; 0 on any failure (so a transient read
    error or a missing rule reads as a flat baseline, never as activity)."""
    try:
        n = get_bytes()
    except Exception:
        return 0
    try:
        return int(n)
    except (TypeError, ValueError):
        return 0


def _activity_delta(get_bytes, prev: int) -> tuple[bool, int]:
    """One sample tick's logic (#2485), factored out for unit tests.

    Returns ``(bumped, new_prev)``: ``bumped`` is True iff the workspace-egress
    byte counter advanced since the last tick (real traffic), in which case
    the caller bumps the idle timer. A counter that did NOT advance (quiet) or
    that RESET (rule re-added / sidecar restart -> counter wraps back below
    prev) re-baselines without bumping, so a reset can never masquerade as a
    burst of activity.
    """
    cur = _safe_bytes(get_bytes)
    return cur > prev, cur


async def _activity_sampler(client, get_bytes, interval: float) -> None:
    """Background task: sample the workspace-egress byte counter and, on a
    positive delta, call ``client.bump_activity`` (#2485).

    ``get_bytes`` -> int is the (mockable) counter reader
    (:func:`klangksidecar.rules.acct_bytes`); ``interval`` is the sample cadence
    (= :data:`ACTIVITY_GATE_S`, so one tick yields at most one activity frame
    per window -- the send itself is flood-gated by ``bump_activity``). The
    per-tick work is exactly one counter read; the kernel does all the
    per-packet accounting. The read runs OFF the event loop via
    ``run_in_executor`` (like :func:`klangksidecar.rules._async_sweeper` runs
    :func:`sweep_once`), because the production reader forks iptables and would
    otherwise stall the DNS recv loop once per interval. Best-effort, like the
    #2481 hooks: a read failure re-baselines (:func:`_activity_delta`) and
    ``bump_activity`` is silent on a down WS, so sampling never breaks egress.
    Per-tick errors are swallowed so one bad tick doesn't kill the task.
    """
    loop = asyncio.get_running_loop()
    _, prev = await loop.run_in_executor(None, _activity_delta, get_bytes, 0)
    while True:
        await asyncio.sleep(interval)
        try:
            bumped, prev = await loop.run_in_executor(
                None, _activity_delta, get_bytes, prev
            )
            if bumped:
                client.bump_activity()
        except Exception:
            pass  # a transient tick failure defers to the next interval
