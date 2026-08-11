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
from dataclasses import dataclass

import websockets
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Footer, Header, ListItem, ListView, Static

from ..auth import refresh_token
from ..transport import ws_connect

logger = logging.getLogger(__name__)

# Decision/scope values mirror the server (model/egress_consent.py); the CLI
# is isolated from the server package, so they are duplicated here (#2309 rule).
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
SCOPE_ONCE = "once"
# Duration tokens mirror the server (model/egress_consent.py); duplicated here
# per CLI isolation. Ordered for the TUI selector; default is `restart` (#2328).
DURATION_ONCE = "once"
DURATION_5M = "5m"
DURATION_15M = "15m"
DURATION_1H = "1h"
DURATION_1D = "1d"
DURATION_1W = "1w"
DURATION_RESTART = "restart"
DURATION_FOREVER = "forever"
DURATIONS = (
    DURATION_ONCE,
    DURATION_5M,
    DURATION_15M,
    DURATION_1H,
    DURATION_1D,
    DURATION_1W,
    DURATION_RESTART,
    DURATION_FOREVER,
)
DURATION_DEFAULT = DURATION_RESTART

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
PONG = "pong"
ERROR = "error"  # server rejected a verdict; payload = message str
IGNORED = "ignore"  # non-JSON / unknown frame


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


def make_verdict(
    request_id: str, decision: str, duration: str = DURATION_DEFAULT
) -> str:
    """Build an outbound verdict frame (JSON string) for a held request."""
    return json.dumps(
        {
            "type": "verdict",
            "request_id": request_id,
            "decision": decision,
            "scope": SCOPE_ONCE,
            "duration": duration,
        }
    )


def make_ping() -> str:
    """Build an outbound liveness ping frame (JSON string)."""
    return json.dumps({"type": "ping"})


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

    def reset(self) -> None:
        """Drop all pending requests.

        Called at the start of each (re)connection: the server's snapshot
        that immediately follows is authoritative for currently-held
        requests, so rows that resolved while we were disconnected -- and
        thus never sent us an ``egress_resolved`` -- must not linger as
        ``(0s)`` ghosts after a reconnect or a klangkd restart.
        """
        self.pending.clear()


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
    #duration-selector Button { width: auto; min-width: 0; height: 1; border: none; padding: 0 1; }
    .dur-sel { background: $accent; color: $background; }
    """

    BINDINGS = [
        ("a", "allow", "Allow"),
        ("d", "deny", "Deny"),
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
        # Global duration selector (default `restart`): click to choose; selecting
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
        """
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
                f" {self.workspace_name}  ·  {conn}  ·  {len(ordered)} held"
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
        self.query_one("#status", Static).update(self._flash_msg)

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
        # `a` key -> the highlighted row (keyboard path).
        self._decide(DECISION_ALLOWED)

    def action_deny(self) -> None:
        self._decide(DECISION_DENIED)

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
