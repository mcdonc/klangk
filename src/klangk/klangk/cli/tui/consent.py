"""Synchronous egress-consent decider TUI (#2310).

A standalone Textual app launched by ``klangk consent-decide <workspace>``.
It connects to the server's ``/ws/consent-decider`` stream (#2244), shows the
workspace's held egress requests (a snapshot on connect, then live), and lets
the deciding user accept/deny each one *while the sidecar holds it* (#2311).
The verdict is sent back over the same socket and applied to that exact held
connection (accept -> it proceeds; deny/timeout -> it fails). Stays within
``klangk.cli`` (isolation rule): only stdlib, third-party deps, and sibling
``cli`` modules.

The protocol/state logic lives in :class:`ConsentDeciderController` (pure,
no Textual) so it is unit-testable without the TUI harness; the
:class:`ConsentDeciderApp` is a thin view that owns the WS worker + renders.

Liveness: a decider that goes silent is reaped by the server after
``consent_decider_timeout`` (45s), which reverts the workspace to static
allow-list (fail-closed). This client pings every ``_PING_INTERVAL`` (well
under that) to stay registered. If the connection drops, it reconnects with
backoff and refreshes the JWT on an auth close (4001/4002); while disconnected
it is deregistered, so held requests auto-deny on timeout -- never silently
allowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace

import websockets
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, ListItem, ListView, Static

from ..auth import refresh_token
from ..transport import ws_connect

logger = logging.getLogger(__name__)

# Decision values mirror the server (model/egress_consent.py); the CLI
# is isolated from the server package, so they are duplicated here (#2309 rule).
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
# Duration tokens mirror the server (model/egress_consent.py); duplicated here
# per CLI isolation. Ordered for the TUI selector; default is `tilrestart` (#2328).
DURATION_ONCE = "once"
DURATION_5M = "5m"
DURATION_15M = "15m"
DURATION_1H = "1h"
DURATION_1D = "1d"
DURATION_1W = "1w"
DURATION_TILRESTART = "tilrestart"
DURATION_FOREVER = "forever"
DURATIONS = (
    DURATION_ONCE,
    DURATION_5M,
    DURATION_15M,
    DURATION_1H,
    DURATION_1D,
    DURATION_1W,
    DURATION_TILRESTART,
    DURATION_FOREVER,
)
DURATION_DEFAULT = DURATION_TILRESTART

# Seconds each *timed* duration adds to ``decided_at`` (mirror of the server's
# ``_DURATION_SECONDS``; duplicated per CLI isolation). ``once`` is consumed by
# the single connection and ``tilrestart``/``forever`` have no fixed expiry, so
# they are absent -- a rule with one of those (or None) has no countdown.
_DURATION_SECONDS = {
    DURATION_5M: 300,
    DURATION_15M: 900,
    DURATION_1H: 3600,
    DURATION_1D: 86400,
    DURATION_1W: 604800,
}


def _fmt_duration(secs: float) -> str:
    """Compact remaining-time label: ``5m``, ``2h``, ``3d``, ``1w`` (#2335 B)."""
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 604800:
        return f"{s // 86400}d"
    return f"{s // 604800}w"


# Pings must beat the server's consent_decider_timeout (45s) or the decider is
# reaped and the workspace reverts to static. 15s leaves margin. NOTE: if an
# operator lowers KLANGKD_CONSENT_DECIDER_TIMEOUT below ~20s this can no longer
# keep up -- the client cannot learn the server's value.
_PING_INTERVAL = 15.0

# Reconnect backoff (seconds). Caps the spin on a repeatedly-dropping server.
_RECONNECT_DELAYS = (1.0, 2.0, 5.0)

# How long a flashed message (send failure, server error) stays on the status
# line before the periodic refresh overwrites it.
_FLASH_TTL = 5.0

# Frame-application outcomes returned by ConsentDeciderController.apply_frame.
ADDED = (
    "added"  # new held request (snapshot or live); payload = ConsentRequest
)
RESOLVED = (
    "resolved"  # request gone (decided/timed out); payload = (id, decision)
)
RULES = "rules"  # refreshed in-effect rules snapshot; payload = EgressRules
PONG = "pong"
ERROR = "error"  # server rejected a verdict; payload = message str
IGNORED = "ignore"  # non-JSON / unknown frame
REVOKE_ACK = (  # server replied to a revoke (#2339/#2341); payload=(id, ok)
    "revoke_ack"
)


@dataclass(frozen=True, slots=True)
class ConsentRequest:
    """One held egress request awaiting a verdict."""

    id: str
    workspace_id: str
    dest_host: str
    dest_port: int | None
    process_name: str | None
    pid: int | None
    requested_at: float


@dataclass(frozen=True, slots=True)
class ConsentRule:
    """One in-effect consent verdict (allow or deny) for the rules view (#2335)."""

    id: str
    dest_host: str
    dest_port: int | None
    process_name: str | None
    decision: str
    duration: str | None
    decided_at: float | None
    decided_by: str | None


@dataclass(frozen=True, slots=True)
class PauseState:
    """Pause window from the ``egress_rules`` frame (#2332, not yet landed).

    ``until`` is the epoch second the pause ends, or None for an indefinite
    pause (e.g. until restart). The frame's ``paused`` field is None today;
    when #2332 lands it is expected to be ``{"paused": True, "until": ...}``.
    """

    until: float | None


@dataclass(frozen=True, slots=True)
class EgressRules:
    """Parsed ``egress_rules`` frame: the workspace's in-effect decisions.

    ``allowed``/``denied`` are ordered newest-decided-first (matching the
    backend's ``list_active`` ``ORDER BY decided_at DESC``); ``allow_list`` is
    the static ``allowed_domains`` config, order preserved; ``paused`` is None
    unless filtering is actually paused (#2332).
    """

    workspace_id: str
    allow_list: tuple[str, ...]
    allowed: tuple[ConsentRule, ...]
    denied: tuple[ConsentRule, ...]
    paused: PauseState | None


def make_verdict(
    request_id: str, decision: str, duration: str = DURATION_DEFAULT
) -> str:
    """Build an outbound verdict frame (JSON string) for a held request."""
    return json.dumps(
        {
            "type": "verdict",
            "request_id": request_id,
            "decision": decision,
            "duration": duration,
        }
    )


def make_ping() -> str:
    """Build an outbound liveness ping frame (JSON string)."""
    return json.dumps({"type": "ping"})


def make_revoke(request_id: str) -> str:
    """Build an outbound revoke frame (JSON string) for an active verdict (#2341).

    Asks the server (#2339) to drop the verdict's sidecar rule and mark the row
    ``revoked``. The row leaves the list only once the server's ``revoke_ack``
    confirms success -- never optimistically, so a still-enforced rule is never
    hidden from the decider.
    """
    return json.dumps({"type": "revoke", "request_id": request_id})


class ConsentDeciderController:
    """Pure state machine over the consent-decider WS protocol (#2310).

    Owns the pending-request map and the frame parser/verdict builders so the
    Textual app stays a thin view and the logic is unit-testable in isolation.
    ``hold_timeout`` is the server's ``egress_consent_timeout`` (the countdown
    the UI shows); the server is the source of truth (it auto-denies at the
    real timeout) -- this is only a UX hint, defaulting to the server default.

    The clock defaults to :func:`time.time` because the server stamps
    ``requested_at`` with ``time.time()`` (epoch wall-clock); the countdown
    math is only meaningful when both timestamps share that domain.
    """

    def __init__(
        self,
        hold_timeout: float = 120.0,
        *,
        clock=time.time,
    ) -> None:
        self.hold_timeout = hold_timeout
        self._clock = clock
        self.pending: dict[str, ConsentRequest] = {}
        # Latest in-effect rules snapshot (#2335 slice A frame). None until the
        # first ``egress_rules`` frame lands (on connect) -- the rules screen
        # renders an empty state until then.
        self.rules: EgressRules | None = None

    def apply_frame(self, raw: str) -> tuple[str, object]:
        """Parse + apply one inbound server frame.

        Returns ``(outcome, payload)``: for ``ADDED`` payload is the
        :class:`ConsentRequest`; for ``RESOLVED`` it is ``(request_id,
        decision)``; for ``ERROR`` a message string; otherwise ``None``.
        Malformed / non-JSON / unknown frames yield ``(IGNORED, None)`` and
        leave state untouched.
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return IGNORED, None
        if not isinstance(msg, dict):
            return IGNORED, None
        mtype = msg.get("type")
        if mtype == "egress_request":
            req = _parse_request(msg.get("request"))
            if req is None:
                return IGNORED, None
            self.pending[req.id] = req
            return ADDED, req
        if mtype == "egress_resolved":
            rid = msg.get("request_id")
            if isinstance(rid, str):
                self.pending.pop(rid, None)
            return RESOLVED, (rid, msg.get("decision"))
        if mtype == "egress_rules":
            rules = _parse_rules(msg)
            if rules is None:
                return IGNORED, None
            self.rules = rules
            return RULES, rules
        if mtype == "revoke_ack":
            # #2341: server confirmed a revoke. On success drop the row from the
            # cached snapshot (idempotent -- the server also pushes a refreshed
            # ``egress_rules``); on failure leave it enforced. ``request_id`` may
            # be absent on a malformed frame -> rid None, no mutation.
            rid = msg.get("request_id")
            ok = bool(msg.get("ok"))
            if ok and isinstance(rid, str) and self.rules is not None:
                self.rules = replace(
                    self.rules,
                    allowed=tuple(
                        r for r in self.rules.allowed if r.id != rid
                    ),
                    denied=tuple(r for r in self.rules.denied if r.id != rid),
                )
            return REVOKE_ACK, (
                rid if isinstance(rid, str) else None,
                ok,
            )
        if mtype == "pong":
            return PONG, None
        if mtype == "error":
            return ERROR, str(msg.get("message", ""))
        return IGNORED, None

    def ordered(self) -> list[ConsentRequest]:
        """Pending requests oldest-first (stable UI ordering)."""
        return sorted(self.pending.values(), key=lambda r: r.requested_at)

    def remaining(self, req: ConsentRequest) -> float:
        """Seconds until this hold's countdown hits zero (clamped at 0)."""
        return max(0.0, req.requested_at + self.hold_timeout - self._clock())

    def rule_remaining(self, rule: ConsentRule) -> float | None:
        """Seconds left on a timed verdict, or None if it has no fixed expiry.

        None covers ``tilrestart``/``forever`` (open-ended), ``once`` (consumed,
        never in ``list_active``), and unknown/NULL durations -- the rules view
        shows ``until restart``/``forever`` for those instead of a countdown.
        """
        if rule.decided_at is None:
            return None
        secs = _DURATION_SECONDS.get(rule.duration)
        if secs is None:
            return None
        return max(0.0, rule.decided_at + secs - self._clock())

    def pause_remaining(self, rules: EgressRules) -> float | None:
        """Seconds left in the pause window, or None if not paused / indefinite."""
        if rules.paused is None or rules.paused.until is None:
            return None
        return max(0.0, rules.paused.until - self._clock())

    def reset(self) -> None:
        """Drop all pending requests + the cached rules snapshot.

        Called at the start of each (re)connection: the server's snapshot (and
        the ``egress_rules`` frame that follows it) is authoritative for
        currently-held requests and in-effect rules, so rows that resolved /
        elapsed while we were disconnected -- and thus never sent us an
        ``egress_resolved`` / refreshed ``egress_rules`` -- must not linger as
        stale entries after a reconnect or a klangkd restart.
        """
        self.pending.clear()
        self.rules = None


def _parse_request(obj: object) -> ConsentRequest | None:
    """Build a :class:`ConsentRequest` from a frame's ``request`` object.

    Returns ``None`` on a shape that can't be acted on (missing id/workspace).
    """
    if not isinstance(obj, dict):
        return None
    rid = obj.get("id")
    wid = obj.get("workspace_id")
    if not isinstance(rid, str) or not isinstance(wid, str):
        return None
    requested_at = obj.get("requested_at")
    if not isinstance(requested_at, (int, float)):
        requested_at = 0
    port = obj.get("dest_port")
    pid = obj.get("pid")
    return ConsentRequest(
        id=rid,
        workspace_id=wid,
        dest_host=str(obj.get("dest_host") or ""),
        dest_port=int(port) if isinstance(port, (int, float)) else None,
        process_name=obj.get("process_name"),
        # bool is an int subclass -- exclude it so pid=True doesn't yield 1.
        pid=pid
        if isinstance(pid, int) and not isinstance(pid, bool)
        else None,
        requested_at=float(requested_at),
    )


def _parse_rules(msg: dict) -> EgressRules | None:
    """Build an :class:`EgressRules` from an ``egress_rules`` frame (#2335).

    Returns ``None`` only if the frame lacks a ``workspace_id`` (unusable); a
    missing/malformed ``allow_list``/``allowed``/``denied`` degrades to empty
    rather than dropping the whole frame. Rows that fail to parse are skipped.
    """
    wid = msg.get("workspace_id")
    if not isinstance(wid, str):
        return None
    raw_allow = msg.get("allow_list")
    allow_list = (
        tuple(str(d) for d in raw_allow) if isinstance(raw_allow, list) else ()
    )
    allowed = [
        r for r in (_parse_rule(o) for o in msg.get("allowed") or []) if r
    ]
    denied = [
        r for r in (_parse_rule(o) for o in msg.get("denied") or []) if r
    ]
    # Newest-decided-first (matches backend list_active ORDER BY decided_at
    # DESC); rows with no decided_at sort last. Stable for ties.
    allowed.sort(key=_rule_sort_key)
    denied.sort(key=_rule_sort_key)
    return EgressRules(
        workspace_id=wid,
        allow_list=allow_list,
        allowed=tuple(allowed),
        denied=tuple(denied),
        paused=_parse_pause(msg.get("paused")),
    )


def _parse_rule(obj: object) -> ConsentRule | None:
    """Build a :class:`ConsentRule` from one row of an ``egress_rules`` frame."""
    if not isinstance(obj, dict):
        return None
    decided_at = obj.get("decided_at")
    port = obj.get("dest_port")
    duration = obj.get("duration")
    return ConsentRule(
        id=str(obj.get("id") or ""),
        dest_host=str(obj.get("dest_host") or ""),
        dest_port=int(port)
        if isinstance(port, (int, float)) and not isinstance(port, bool)
        else None,
        process_name=obj.get("process_name"),
        decision=str(obj.get("decision") or ""),
        duration=duration if isinstance(duration, str) else None,
        decided_at=float(decided_at)
        if isinstance(decided_at, (int, float))
        and not isinstance(decided_at, bool)
        else None,
        decided_by=obj.get("decided_by"),
    )


def _parse_pause(obj: object) -> PauseState | None:
    """Parse the ``paused`` field of an ``egress_rules`` frame (#2332).

    The field is None today (pause control not landed). When #2332 lands it is
    expected to be ``{"paused": bool, "until": epoch|None}``; this returns
    None unless filtering is actually paused, so the rules screen renders no
    pause section until then (graceful degradation).
    """
    if not isinstance(obj, dict) or obj.get("paused") is not True:
        return None
    until = obj.get("until")
    return PauseState(
        until=float(until)
        if isinstance(until, (int, float)) and not isinstance(until, bool)
        else None
    )


def _rule_sort_key(rule: ConsentRule) -> tuple[int, float]:
    """Sort key: decided rows first (newest first), undecided last."""
    if rule.decided_at is None:
        return (1, 0.0)
    return (0, -rule.decided_at)


class ConsentDeciderApp(App):
    """Textual app: live queue of held egress requests + accept/deny.

    The WS worker (:meth:`_ws_loop`) connects, pings for liveness, feeds each
    inbound frame to the controller, and sends verdicts on keypress. A 1s
    interval refreshes the countdowns. The app is a thin view over
    :class:`ConsentDeciderController`; all protocol logic lives there.
    """

    CSS = """
    Screen { layout: vertical; }
    #status { padding: 0 1; background: $panel; color: $text-muted; }
    #requests { height: 1fr; }
    #requests ListItem { height: 2; }
    #empty { padding: 1 2; color: $text-muted; }
    .req-host { color: $text; }
    #requests Button { height: 1; border: none; padding: 0 1; }
    #duration-selector { width: auto; height: 1; }
    /* Non-selected duration buttons carry no background -- not even when focused
    (#2360). The first button grabs initial focus on mount; without an explicit
    transparent :focus it rendered with the white focus background, so it read
    as "selected" alongside the real ``dur-sel`` default (``tilrestart``). The
    ``dur-sel`` rules are qualified under ``#duration-selector`` so they outrank
    these ID-based transparent rules on specificity (an unqualified ``.dur-sel``
    loses to ``#duration-selector Button:focus`` and the accent vanishes). */
    #duration-selector Button { width: auto; min-width: 0; height: 1; border: none; padding: 0 1; background: transparent; }
    #duration-selector Button:focus { background: transparent; }
    #duration-selector .dur-sel { background: $accent; color: $background; }
    #duration-selector .dur-sel:focus { background: $accent; color: $background; }
    """

    BINDINGS = [
        ("a", "allow", "Allow"),
        ("d", "deny", "Deny"),
        ("r", "rules", "Rules"),
        ("q", "quit", "Quit"),
    ]

    def __init__(  # noqa: PLR0913
        self,
        server_url: str,
        token: str,
        workspace_id: str,
        workspace_name: str,
        *,
        hold_timeout: float = 120.0,
        max_size: int | None = None,
        ping_interval: float = _PING_INTERVAL,
        reconnect_delays: tuple[float, ...] = _RECONNECT_DELAYS,
    ) -> None:
        super().__init__()
        self.server_url = server_url
        self.token = token
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.max_size = max_size
        self.ping_interval = ping_interval
        self.reconnect_delays = reconnect_delays
        self.controller = ConsentDeciderController(hold_timeout=hold_timeout)
        self._ws = None
        self._connected = False
        self._stop = False
        self._flash_msg = ""
        self._flash_until = 0.0
        self._duration = DURATION_DEFAULT

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        # Global duration selector (default `tilrestart`): click to choose; selecting
        # does NOT submit -- only a row's Allow/Deny submits with this duration.
        yield Horizontal(*self._duration_buttons(), id="duration-selector")
        with Vertical():
            yield ListView(id="requests")
            yield Static("No held requests — connected, waiting.", id="empty")
        yield Footer()

    def _duration_buttons(self) -> list[Button]:
        btns = []
        for d in DURATIONS:
            b = Button(
                d,
                id=f"dur-{d}",
                classes=("dur-sel" if d == self._duration else ""),
            )
            b.duration = d  # type: ignore[attr-defined]
            btns.append(b)
        return btns

    def on_mount(self) -> None:
        self.title = f"consent-decide · {self.workspace_name}"
        self.run_worker(
            self._ws_loop, exclusive=True, group="ws", exit_on_error=False
        )
        self.set_interval(1.0, self._refresh)

    # -- WS worker ---------------------------------------------------------

    async def _ws_loop(self) -> None:
        """Connect -> pump -> reconnect loop, until :attr:`_stop` is set.

        On any error (connect failure, mid-stream drop) the connection is
        dropped and, after backoff, re-attempted. An auth-related close
        (expired/invalid token, 4001/4002) refreshes the JWT first so the
        next attempt authenticates (mirrors ``monitor``). While disconnected
        the decider is deregistered server-side, so in-flight holds auto-deny
        on their own timeout (fail-closed) -- never silently allowed.
        """
        attempt = 0
        while not self._stop:
            auth_close = False
            try:
                async with ws_connect(
                    self.server_url,
                    token=self.token,
                    max_size=self.max_size,
                    path="/ws/consent-decider",
                    query={"workspace": self.workspace_id},
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    attempt = 0
                    self._refresh()
                    auth_close = await self._pump(ws)
            except Exception as e:  # noqa: BLE001
                logger.debug("consent-decide ws dropped: %s", e)
            finally:
                self._ws = None
                self._connected = False
            if self._stop:
                break
            if auth_close:
                new = await self._refresh_token()
                if new:
                    self.token = new
            attempt += 1
            await asyncio.sleep(self._backoff(attempt))
            self._refresh()

    async def _refresh_token(self) -> str | None:
        """Refresh the JWT off the event loop (the HTTP call is blocking)."""
        try:
            return await asyncio.to_thread(
                refresh_token, self.server_url, self.token
            )
        except Exception:
            logger.debug("consent-decide token refresh failed")
            return None

    def _backoff(self, attempt: int) -> float:
        delays = self.reconnect_delays
        if not delays:
            return 0.0
        return delays[min(attempt - 1, len(delays) - 1)]

    async def _pump(self, ws) -> bool:
        """Read frames until the socket closes; ping in parallel.

        Returns True if the close was auth-related (token 4001/4002) so the
        caller can refresh the JWT. Render exceptions are isolated: a UI bug
        must never tear down the transport (which would replay the snapshot
        and re-trigger the bug in a tight reconnect loop).
        """
        ping = asyncio.create_task(self._ping_loop(ws))
        # The server's snapshot (sent immediately on connect) is authoritative
        # for currently-held requests: drop anything stale from a prior session
        # so rows that resolved while disconnected don't linger as (0s) ghosts.
        # Refresh now too -- an EMPTY snapshot (klangkd restart + orphan reap)
        # sends no frames, so nothing else would clear the stale UI. Isolated
        # like every render: a UI bug must never tear down the transport.
        self.controller.reset()
        try:
            self._refresh()
        except Exception:
            logger.exception("consent-decide render failed")
        auth_close = False
        try:
            while True:
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd else None
                    auth_close = code in (4001, 4002)
                    break
                action, payload = self.controller.apply_frame(raw)
                try:
                    if action == ERROR:
                        self._flash(str(payload))
                    elif action == REVOKE_ACK:
                        # payload = (request_id, ok). A failed revoke (not an
                        # active verdict, wrong workspace, or the sidecar never
                        # acked the drop) leaves the row enforced -- flash so
                        # the decider knows it is still in effect, never silent.
                        # Success needs no flash; the controller already dropped
                        # the row and the refresh re-renders the list without it.
                        _rid, ok = payload  # type: ignore[misc]
                        if not ok:
                            self._flash("revoke failed — still in effect")
                        self._refresh()
                    else:
                        self._refresh()
                except Exception:
                    logger.exception("consent-decide render failed")
        finally:
            ping.cancel()
            try:
                await ping
            except (asyncio.CancelledError, Exception):
                pass
        return auth_close

    async def _ping_loop(self, ws) -> None:
        """Send a liveness ping every ``ping_interval`` to stay registered."""
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                if self._stop:
                    return
                await ws.send(make_ping())
        except asyncio.CancelledError:
            return
        except Exception:
            # Socket closed (ConnectionClosed, etc.); the pump's recv() loop
            # ends at the same time. Swallowed so the task has no unobserved
            # exception.
            return

    # -- rendering ---------------------------------------------------------

    def _refresh(self) -> None:
        """Sync the list to controller state WITHOUT a full rebuild.

        A clear+rebuild every tick flickered badly. Instead we remove only
        resolved/expired rows, repaint the countdown text of survivors in
        place, and append genuinely-new ones. Order is stable (oldest-first
        by requested_at), so we never have to reorder.

        Also keeps the rules screen live when it is the active screen, so its
        countdowns tick and a freshly-arrived ``egress_rules`` frame shows up
        without waiting for its own timer (#2335 slice B).
        """
        # Refresh the rules screen first (if up) so a queue-query hiccup can
        # never starve it -- the WS worker is shared across the switch.
        try:
            screen = self.screen
        except Exception:
            screen = None  # not mounted yet (pre-mount / no screen stack)
        if isinstance(screen, RulesScreen):
            try:
                screen.refresh_rules()
            except Exception:
                logger.exception("consent-decide rules render failed")
        try:
            lv = self.query_one("#requests", ListView)
            status = self.query_one("#status", Static)
            empty = self.query_one("#empty", Static)
        except Exception:
            return  # not mounted yet (pre-mount call)
        ordered = self.controller.ordered()
        current_ids = {req.id for req in ordered}
        focused_id = self._focused_request_id()
        # Drop resolved/expired rows (leave survivors untouched -> no flicker).
        for child in list(lv.children):
            rid = getattr(child, "request_id", None)
            if rid is not None and rid not in current_ids:
                child.remove()
        # Repaint survivors' countdown in place; append only new rows.
        existing = {getattr(c, "request_id", None): c for c in lv.children}
        for req in ordered:
            item = existing.get(req.id)
            if item is None:
                lv.append(self._render_item(req))
            else:
                self._update_item(item, req)
        self._select_by_id(focused_id)
        empty.display = not ordered
        if self._flash_until > time.time():
            status.update(self._flash_msg)
        else:
            conn = "connected" if self._connected else "reconnecting"
            status.update(
                f" {escape(self.workspace_name)}  ·  {conn}  ·  {len(ordered)} held"
            )

    def _host_line(self, req: ConsentRequest) -> str:
        host = req.dest_host
        if req.dest_port is not None:
            host = f"{host}:{req.dest_port}"
        proc = f"  ({req.process_name})" if req.process_name else ""
        secs = int(self.controller.remaining(req))
        # dest_host is server-observed DNS; escape it so a host containing
        # rich markup (e.g. "[red]") renders literally, not as styling.
        return escape(f"{host}{proc}  ({secs}s)")

    def _render_item(self, req: ConsentRequest) -> ListItem:
        # Host line + per-row Allow/Deny (the only submit actions). The duration
        # is chosen once via the global selector above the list (#2328), so the
        # row stays compact.
        item = ListItem(
            Static(self._host_line(req), classes="req-host"),
            Horizontal(
                Button("Allow", id=f"allow-{req.id}", variant="success"),
                Button("Deny", id=f"deny-{req.id}", variant="error"),
            ),
        )
        item.request_id = req.id  # type: ignore[attr-defined]
        return item

    def _update_item(self, item: ListItem, req: ConsentRequest) -> None:
        """Repaint only the host/countdown line of a surviving row."""
        try:
            item.query_one(".req-host", Static).update(self._host_line(req))
        except NoMatches:
            # The row was appended (its ``request_id`` is set synchronously)
            # but its child widgets aren't mounted yet -- a refresh fired in
            # that mount gap. The host line was already set at render time, so
            # skip this repaint; the next tick finds it mounted and refreshes
            # the countdown normally.
            pass

    def _focused_request_id(self) -> str | None:
        child = self.query_one("#requests", ListView).highlighted_child
        if child is None:
            return None
        return getattr(child, "request_id", None)

    def _select_by_id(self, rid: str | None) -> None:
        if rid is None:
            return
        lv = self.query_one("#requests", ListView)
        for index, child in enumerate(lv.children):
            if getattr(child, "request_id", None) == rid:
                lv.index = index
                return

    def _flash(self, message: str, ttl: float = _FLASH_TTL) -> None:
        """Show a transient message in the status line for ``ttl`` seconds.

        The TTL survives the 1s periodic refresh (which would otherwise
        clobber it immediately). Used for verdict send failures and server
        error frames.
        """
        self._flash_msg = f" [red]![/red] {escape(message)}"
        self._flash_until = time.time() + ttl
        # Query the ACTIVE screen (App.query_one searches the app's default
        # screen, not a pushed one): the main queue's ``#status`` or the rules
        # screen's ``#rules-status`` (a revoke can be issued / its ack can
        # arrive while either is up).
        screen = self.screen
        target = (
            "#rules-status" if isinstance(screen, RulesScreen) else "#status"
        )
        screen.query_one(target, Static).update(self._flash_msg)

    # -- actions -----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("dur-"):
            # Selecting a duration does NOT submit -- it only sets the global
            # choice (Allow/Deny submit with it).
            self._select_duration(event.button)
        elif bid.startswith("allow-"):
            self._decide_id(
                bid.removeprefix("allow-"), DECISION_ALLOWED, self._duration
            )
        elif bid.startswith("deny-"):
            self._decide_id(
                bid.removeprefix("deny-"), DECISION_DENIED, self._duration
            )

    def _select_duration(self, button: Button) -> None:
        """Set the global selected duration + highlight it (no submit)."""
        d = getattr(button, "duration", None)
        if d is None:
            return
        self._duration = d
        for b in self.query(".dur-sel"):
            b.remove_class("dur-sel")
        button.add_class("dur-sel")

    def action_allow(self) -> None:
        # `a` key -> the highlighted row (keyboard path). No-op on the rules
        # screen so a/d can't decide a queue row that isn't visible there.
        if isinstance(self.screen, RulesScreen):
            return
        self._decide(DECISION_ALLOWED)

    def action_deny(self) -> None:
        if isinstance(self.screen, RulesScreen):
            return
        self._decide(DECISION_DENIED)

    def action_rules(self) -> None:
        # `r` opens the read-only rules screen (#2335 slice B). Guard against
        # pushing a second one if `r` is pressed while already viewing rules.
        if isinstance(self.screen, RulesScreen):
            return
        self.push_screen(RulesScreen())

    def _decide(self, decision: str) -> None:
        rid = self._focused_request_id()
        if rid is None:
            return
        self._decide_id(rid, decision, self._duration)

    def _decide_id(self, rid: str, decision: str, duration: str) -> None:
        ws = self._ws
        if ws is None:
            self._flash("disconnected — reconnecting")
            return
        # Send on the shared loop via _send_verdict, which awaits the send and
        # flashes on failure (a dropped socket between the check and the send
        # would otherwise silently lose the verdict). Do not optimistically
        # drop the row: a duplicate/no-op verdict must not hide a still-held
        # request (the server's egress_resolved frame removes it).
        asyncio.create_task(self._send_verdict(ws, rid, decision, duration))

    async def _send_verdict(
        self, ws, rid: str, decision: str, duration: str
    ) -> None:
        try:
            await ws.send(make_verdict(rid, decision, duration))
        except Exception:
            self._flash("verdict send failed — reconnecting")


class RulesScreen(Screen):
    """Read-only view of the workspace's in-effect egress decisions.

    The second screen of :class:`ConsentDeciderApp`, pushed from the held-
    request queue with ``r`` and popped with ``q``/``Esc`` (#2335 slice B). It
    reads ``app.controller.rules`` (the latest ``egress_rules`` frame), kept
    live by the app's shared WS pump + 1s refresh, so the WS worker is **not**
    torn down/reconnected on the switch -- only the view changes. This screen
    only displays; revoking a row is #2339 + slice D (#2341).
    """

    CSS = """
    RulesScreen { layout: vertical; }
    #rules-status { padding: 0 1; background: $panel; color: $text-muted; }
    #rules-body { padding: 0 1; height: 1fr; }
    #rules-list-label { padding: 0 1; background: $panel; color: $text-muted; }
    #rules-list { min-height: 3; height: auto; max-height: 12; border: round $primary; }
    #rules-list:focus-within { border: round $accent; }
    #rules-list ListItem { height: 1; }
    """

    BINDINGS = [
        ("escape", "back", "Back"),
        ("q", "back", "Back"),
        ("x", "revoke", "Revoke"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="rules-status")
        with VerticalScroll(id="rules-body"):
            yield Static(id="rules-content")
        # #2341 slice D: a compact, selectable list of the revocable verdicts
        # (allows + denies). The static allow-list is NOT here (it lives in the
        # read-only #rules-content above), so it can never be the focused
        # revoke target -- the scope guard is structural, not a runtime check.
        yield Static(
            "Revoke: focus a rule, press [bold]x[/bold]",
            id="rules-list-label",
        )
        yield ListView(id="rules-list")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_rules()
        # Focus the revoke selector so arrow keys + `x` work immediately.
        self.query_one("#rules-list", ListView).focus()

    def action_back(self) -> None:
        """``q``/``Esc`` returns to the held-request queue."""
        self.app.pop_screen()

    def action_revoke(self) -> None:
        """``x`` revokes the focused revocable rule (#2341 slice D).

        Sends the ``revoke`` frame; the row stays in the list until the
        server's ``revoke_ack`` confirms it (never optimistically -- a still-
        enforced rule must not be hidden). A failed ack flashes and leaves the
        row; the removal-on-success is driven by the ack in :meth:`_pump`.
        """
        app = self.app
        rid = self._focused_rule_id()
        if rid is None:
            return
        ws = app._ws  # type: ignore[attr-defined]
        if ws is None:
            app._flash("disconnected — reconnecting")  # type: ignore[attr-defined]
            return
        asyncio.create_task(self._send_revoke(app, ws, rid))

    async def _send_revoke(self, app, ws, rid: str) -> None:
        try:
            await ws.send(make_revoke(rid))
        except Exception:
            app._flash("revoke send failed — reconnecting")  # type: ignore[attr-defined]

    def _focused_rule_id(self) -> str | None:
        lv = self.query_one("#rules-list", ListView)
        child = lv.highlighted_child
        if child is None:
            return None
        return getattr(child, "rule_id", None)

    def refresh_rules(self) -> None:
        """Re-render the body from the controller's latest ``egress_rules``."""
        try:
            status = self.query_one("#rules-status", Static)
            content = self.query_one("#rules-content", Static)
        except NoMatches:
            return  # not mounted yet
        app = self.app  # ConsentDeciderApp
        controller = app.controller  # type: ignore[attr-defined]
        rules = controller.rules
        # Respect an active flash (e.g. a failed revoke) so the 1s refresh
        # does not clobber it -- mirrors the main screen's status behavior.
        if app._flash_until > time.time():  # type: ignore[attr-defined]
            status.update(app._flash_msg)  # type: ignore[attr-defined]
        else:
            conn = "connected" if app._connected else "reconnecting"  # type: ignore[attr-defined]
            held = len(controller.pending)
            status.update(
                f" {escape(app.workspace_name)}  ·  {conn}  ·  {held} held  ·  rules"  # type: ignore[attr-defined]
            )
        content.update(self._render_body(rules, controller))
        self._rebuild_rule_list(rules)

    def _rebuild_rule_list(self, rules: EgressRules | None) -> None:
        """Rebuild the revoke selector from the controller's rules (#2341).

        Compact rows (no countdown -- the detail body above ticks that). Only
        rebuilt when the set of revocable rule ids changes, so the 1s refresh
        never flickers it. The static allow-list is intentionally absent -> it
        is never a revoke target.
        """
        lv = self.query_one("#rules-list", ListView)
        desired: list[str] = []
        if rules is not None:
            desired += [r.id for r in rules.allowed]
            desired += [r.id for r in rules.denied]
        current = [getattr(c, "rule_id", None) for c in lv.children]
        if current == desired:
            return  # unchanged -> no flicker
        focused = (
            getattr(lv.highlighted_child, "rule_id", None)
            if lv.highlighted_child is not None
            else None
        )
        lv.clear()
        if rules is not None:
            for r in rules.allowed:
                lv.append(self._rule_item(r, DECISION_ALLOWED))
            for r in rules.denied:
                lv.append(self._rule_item(r, DECISION_DENIED))
        if focused is not None:
            # Restore focus to the surviving rule. ``lv.clear()`` schedules an
            # index reset that would clobber a synchronous set, so defer the
            # restore to after the refresh pass lands it.
            def _restore_focus(_lv=lv, _focused=focused) -> None:
                for index, child in enumerate(_lv.children):
                    if getattr(child, "rule_id", None) == _focused:
                        _lv.index = index
                        break

            lv.call_after_refresh(_restore_focus)

    @staticmethod
    def _rule_item(rule: ConsentRule, decision: str) -> ListItem:
        host = rule.dest_host
        if rule.dest_port is not None:
            host = f"{host}:{rule.dest_port}"
        proc = f"  ({rule.process_name})" if rule.process_name else ""
        mark = "allow" if decision == DECISION_ALLOWED else "deny"
        item = ListItem(
            Static(f"{escape(mark)}  {escape(host)}{escape(proc)}")
        )
        item.rule_id = rule.id  # type: ignore[attr-defined]
        return item

    def _render_body(self, rules: EgressRules | None, controller) -> str:
        """Build the grouped rich-markup body (allow-list, allows, denies, pause)."""
        lines: list[str] = []
        n_allowed = len(rules.allowed) if rules else 0
        n_denied = len(rules.denied) if rules else 0

        lines.append("[bold]Static allow-list[/bold]")
        if rules is None:
            lines.append("  [dim](no rules received yet)[/dim]")
        elif rules.allow_list:
            for d in rules.allow_list:
                lines.append(f"  {escape(d)}")
        else:
            lines.append("  [dim](none)[/dim]")
        lines.append("")

        lines.append(f"[bold]Active allows ({n_allowed})[/bold]")
        if rules and rules.allowed:
            for r in rules.allowed:
                lines.append("  " + self._rule_line(r, controller, deny=False))
        else:
            lines.append("  [dim](none)[/dim]")
        lines.append("")

        lines.append(f"[bold]Active denies ({n_denied})[/bold]")
        if rules and rules.denied:
            for r in rules.denied:
                lines.append("  " + self._rule_line(r, controller, deny=True))
        else:
            lines.append("  [dim](none)[/dim]")

        # Pause window (#2332; absent today -> section hidden).
        if rules is not None and rules.paused is not None:
            lines.append("")
            lines.append("[bold]Pause[/bold]")
            rem = controller.pause_remaining(rules)
            if rem is None:
                lines.append(
                    "  [yellow]Filtering paused until restart[/yellow]"
                )
            else:
                lines.append(
                    "  [yellow]Filtering paused "
                    f"(resumes in {_fmt_duration(rem)})[/yellow]"
                )
        return "\n".join(lines)

    @staticmethod
    def _rule_line(rule: ConsentRule, controller, *, deny: bool) -> str:
        host = rule.dest_host
        if rule.dest_port is not None:
            host = f"{host}:{rule.dest_port}"
        proc = f"  ({rule.process_name})" if rule.process_name else ""
        if rule.duration == DURATION_FOREVER:
            label = "forever"
        elif rule.duration == DURATION_TILRESTART:
            label = "until restart"
        else:
            rem = controller.rule_remaining(rule)
            # Guard None before formatting: a timed verdict with a null
            # decided_at or an unknown duration has no countdown. Both allow
            # and deny must degrade the same way (else _fmt_duration(None)
            # raises TypeError on a deny) -- the parser permits these rows, so
            # rendering must too.
            if rem is None:
                label = ""
            elif deny:
                label = f"{_fmt_duration(rem)} left"
            else:
                label = f"expires in {_fmt_duration(rem)}"
        return f"{escape(host)}{escape(proc)}  [dim]{escape(label)}[/dim]"
