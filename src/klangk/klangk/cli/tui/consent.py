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
import subprocess
import time
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version as _pkg_version

import websockets
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    ListItem,
    ListView,
    OptionList,
    Static,
)

from ..auth import refresh_token
from ..shell_popup import (
    DEFAULT_POPUP_SIZE,
    hidden_has_client,
    outer_clients,
    show_popup_argv,
)
from ..transport import ws_connect

logger = logging.getLogger(__name__)

# Decision values mirror the server (model/egress_consent.py); the CLI
# is isolated from the server package, so they are duplicated here (#2309 rule).
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
# Duration tokens mirror the server (model/egress_consent.py); duplicated here
# per CLI isolation. Ordered for the duration picker; default is `tilrestart`
# (#2328).
DURATION_ONCE = "once"
# Test-only short duration (#2363, subsumed by #2392): recognized for in-effect
# math (a `5s` verdict in a rules snapshot counts down correctly) and for
# programmatic/test callers, but NOT offered in the human-facing picker
# (see SELECTABLE_DURATIONS below).
DURATION_5S = "5s"
DURATION_5M = "5m"
DURATION_15M = "15m"
DURATION_1H = "1h"
DURATION_1D = "1d"
DURATION_1W = "1w"
DURATION_TILRESTART = "tilrestart"
DURATION_FOREVER = "forever"
# Full set the CLI recognizes (mirrors the server's DURATIONS, incl. the
# test-only 5s). Drives in-effect/countdown math, not the picker.
DURATIONS = (
    DURATION_ONCE,
    DURATION_5S,
    DURATION_5M,
    DURATION_15M,
    DURATION_1H,
    DURATION_1D,
    DURATION_1W,
    DURATION_TILRESTART,
    DURATION_FOREVER,
)
# Human-facing durations: every duration a user can pick (the `A`/`D`
# duration picker). The test-only 5s is NOT offered (#2487) -- it's not meant
# for end users -- but stays recognized for in-effect/countdown math and
# programmatic/test callers (it remains in DURATIONS above).
SELECTABLE_DURATIONS = tuple(d for d in DURATIONS if d != DURATION_5S)
DURATION_DEFAULT = DURATION_TILRESTART

# Seconds each *timed* duration adds to ``decided_at`` (mirror of the server's
# ``_DURATION_SECONDS``; duplicated per CLI isolation). ``once`` is consumed by
# the single connection and ``tilrestart``/``forever`` have no fixed expiry, so
# they are absent -- a rule with one of those (or None) has no countdown.
_DURATION_SECONDS = {
    DURATION_5S: 5,
    DURATION_5M: 300,
    DURATION_15M: 900,
    DURATION_1H: 3600,
    DURATION_1D: 86400,
    DURATION_1W: 604800,
}


def fmt_duration(secs: float) -> str:
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

# After two consecutive 403 refusals the decider retries at this fixed slow
# interval (#2490): bounded log spam (1/min vs the old 1/5s storm), but the
# decider still self-heals if the workspace flips back to interactive (or
# permissions are restored) mid-session instead of staying dead until the
# shell is restarted.
REFUSED_RETRY_INTERVAL = 60.0

# Popup-show retry (#2699 review): how many times a worker re-attempts a
# show that targeted nothing (no outer client found — e.g. a contended tmux
# server timing out ``list-clients``), and how long it waits between
# attempts. Without a retry, requests that arrived during the failed
# attempt's dedupe window would never get a popup (holds auto-deny unseen).
POPUP_SHOW_ATTEMPTS = 3
POPUP_SHOW_RETRY_DELAY = 1.0


# Sent as the WS handshake User-Agent so klangkd's refusal log (#2490) can
# attribute a 403 to this client (vs a browser or anything else).
def user_agent() -> str:
    try:
        return f"klangk-consent-decide/{_pkg_version('klangk')}"
    except PackageNotFoundError:  # running from source, not installed
        return "klangk-consent-decide/dev"


USER_AGENT = user_agent()

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
PAUSE_ACK = (  # server replied to a pause/unpause (#2332); payload=(ok, until)
    "pause_ack"
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


def make_pause(duration: str) -> str:
    """Build an outbound pause frame (JSON string) -- silence prompts (#2332).

    ``duration`` is one of ``"15m"``/``"1h"``/``"1d"``. While paused, a
    destination with no allow-list rule and no in-effect verdict is
    auto-allowed (no hold); a recorded deny still blocks. The server replies
    with a ``pause_ack`` and broadcasts a refreshed ``egress_rules`` frame.
    """
    return json.dumps({"type": "pause", "duration": duration})


def make_unpause() -> str:
    """Build an outbound unpause frame (JSON string) -- resume prompting (#2332)."""
    return json.dumps({"type": "unpause"})


def build_detach_command(socket_path: str, session: str) -> list[str]:
    """tmux argv to detach clients attached to the decider's hidden session.

    In the persistent popup role (#2383) the decider runs inside a hidden
    tmux session and a popup *viewer* attaches to it. Detaching every client
    of *session* on local socket *socket_path* hides that viewer (the only
    client) while the decider process inside the session keeps running -- so
    the held-request stream stays open and the decider stays registered.
    Returns the argv only; the caller runs it and tolerates failure (a stale
    session or no viewer attached is not an error).
    """
    return ["tmux", "-S", socket_path, "detach-client", "-s", session]


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

    def _apply_request(self, msg: dict) -> tuple[str, object]:
        req = _parse_request(msg.get("request"))
        if req is None:
            return IGNORED, None
        self.pending[req.id] = req
        return ADDED, req

    def _apply_resolved(self, msg: dict) -> tuple[str, object]:
        rid = msg.get("request_id")
        if isinstance(rid, str):
            self.pending.pop(rid, None)
        return RESOLVED, (rid, msg.get("decision"))

    def _apply_rules(self, msg: dict) -> tuple[str, object]:
        rules = _parse_rules(msg)
        if rules is None:
            return IGNORED, None
        self.rules = rules
        return RULES, rules

    def _apply_revoke_ack(self, msg: dict) -> tuple[str, object]:
        # #2341: server confirmed a revoke. On success drop the row from the
        # cached snapshot (idempotent -- the server also pushes a refreshed
        # ``egress_rules``); on failure leave it enforced. ``request_id`` may
        # be absent on a malformed frame -> rid None, no mutation.
        rid = msg.get("request_id")
        ok = bool(msg.get("ok"))
        if ok and isinstance(rid, str) and self.rules is not None:
            self.rules = replace(
                self.rules,
                allowed=tuple(r for r in self.rules.allowed if r.id != rid),
                denied=tuple(r for r in self.rules.denied if r.id != rid),
            )
        return REVOKE_ACK, (
            rid if isinstance(rid, str) else None,
            ok,
        )

    def _apply_pause_ack(self, msg: dict) -> tuple[str, object]:
        # #2332: server replied to a pause/unpause. The pause window itself
        # arrives in the subsequent refreshed ``egress_rules`` frame (the
        # server broadcasts one on every pause/unpause); this ack only
        # signals success/failure so the view can flash on a nack. ``until``
        # is None for an unpause or a failed pause.
        until = msg.get("until")
        return PAUSE_ACK, (
            bool(msg.get("ok")),
            float(until)
            if isinstance(until, (int, float)) and not isinstance(until, bool)
            else None,
        )

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
            return self._apply_request(msg)
        if mtype == "egress_resolved":
            return self._apply_resolved(msg)
        if mtype == "egress_rules":
            return self._apply_rules(msg)
        if mtype == "revoke_ack":
            return self._apply_revoke_ack(msg)
        if mtype == "pause_ack":
            return self._apply_pause_ack(msg)
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

    def pause_expired(self, rules: EgressRules) -> bool:
        """Whether a finite pause window has elapsed (#2498).

        The server reverts to prompting at the real expiry but only
        re-broadcasts ``egress_rules`` on the next discrete event
        (verdict/revoke/pause/reconnect) -- never on natural expiry -- so an
        idle workspace must prune the stale pause locally, off the injected
        clock, or the views keep claiming prompts are suppressed ("paused
        0s") after holds have actually resumed. An indefinite pause
        (``until`` None, "until restart") and a not-paused workspace never
        expire. Mirrors the web client's ``isPauseExpired`` (#2497) and the
        timed-verdict prune of #2467.
        """
        paused = rules.paused
        if paused is None or paused.until is None:
            return False
        return paused.until <= self._clock()

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


def _parse_rule_rows(raw) -> list[ConsentRule]:
    """Parse one frame's rule rows, skipping rows that fail to parse.

    Newest-decided-first (matches backend list_active ORDER BY decided_at
    DESC); rows with no decided_at sort last. Stable for ties.
    """
    rows = [r for r in (_parse_rule(o) for o in raw or []) if r]
    rows.sort(key=_rule_sort_key)
    return rows


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
    return EgressRules(
        workspace_id=wid,
        allow_list=allow_list,
        allowed=tuple(_parse_rule_rows(msg.get("allowed"))),
        denied=tuple(_parse_rule_rows(msg.get("denied"))),
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

    # The command palette's "^p palette" Footer entry is noise in the small
    # consent popup -- disable it there (#2383).
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: vertical; }
    #status { padding: 0 1; background: $panel; color: $text-muted; }
    /* #queue holds the request list + empty state and fills the space between
    the status line and the pause bar, so the pause bar pins to just
    above the Footer (#2383). */
    #queue { height: 1fr; }
    #requests { height: 1fr; }
    #requests ListItem { height: 2; }
    #empty { padding: 1 2; color: $text-muted; }
    .req-host { color: $text; }
    #requests Button { height: 1; border: none; padding: 0 1; }
    /* #2332 pause control bar: a compact top-bar that silences ALL prompts for
    the workspace for a window. Flagged yellow so it reads as "filtering off". */
    #pause-bar { height: 1; background: $panel; }
    #pause-bar Static { width: auto; padding: 0 1; color: $text-muted; }
    #pause-bar Button { width: auto; min-width: 0; height: 1; border: none; padding: 0 1; background: transparent; }
    #pause-bar Button:focus { background: transparent; }
    #pause-bar .pause-active { background: $warning; color: $background; }
    #pause-bar .pause-active:focus { background: $warning; color: $background; }
    """

    BINDINGS = [
        ("a", "allow", "Allow"),
        ("A", "allow_duration", "Allow…"),
        ("d", "deny", "Deny"),
        ("D", "deny_duration", "Deny…"),
        ("r", "rules", "Rules"),
        # q, Q, Escape, and Ctrl-A all hide the viewer in persistent mode (the
        # decider is persistent — it never quits on a key, only when the shell
        # ends) and all quit in standalone (#2383). Ctrl-A lets "C-a p" close
        # the popup while it's open: the outer C-a p binding can't fire then
        # (the popup captures input), but C-a passes through to the decider.
        ("q", "q_key", "Quit"),
        ("Q", "q_key", "Quit"),
        ("escape", "q_key", "Quit"),
        ("ctrl+a", "q_key", "Quit"),
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
        popup_socket: str | None = None,
        popup_session: str | None = None,
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
        # #2490: set once registration has been refused (HTTP 403) twice --
        # the reconnect loop has dropped to its slow fixed interval and the
        # status line says so instead of "reconnecting". Cleared when a
        # connect succeeds again (self-heal).
        self._refused = False
        self._flash_msg = ""
        self._flash_until = 0.0
        # #2332 pause window the user last requested (None = Unpaused). Drives
        # which pause button is highlighted; the live countdown is shown next
        # to the buttons (#2383).
        self._pause_duration: str | None = None
        # Persistent popup role (#2383): the decider runs inside a hidden
        # tmux session a popup viewer attaches to. Set (with popup_socket)
        # when launched by the shell-layer wrapper; None for standalone use.
        self.popup_socket = popup_socket
        self.popup_session = popup_session
        # The in-flight popup-show task (#2699): one show at a time, so a
        # burst of held requests (snapshot or rapid-fire) reuses the show
        # already opening instead of piling up tmux subprocesses.
        self._show_popup_task: asyncio.Task | None = None
        # The in-flight viewer-hide task (#2699 review): same off-loop
        # treatment for the ``q``/``Esc`` detach path.
        self._hide_task: asyncio.Task | None = None

    @property
    def _persistent(self) -> bool:
        """True when running inside the hidden popup session (#2383)."""
        return self.popup_session is not None

    def _apply_bindings(self) -> None:
        """Install persistent vs standalone keybindings, then refresh Footer.

        ``q``, ``Q``, ``Escape`` and ``Ctrl-A`` all hide the viewer in
        persistent mode (the decider is persistent — it never quits on a key,
        only when the shell ends) and all quit in standalone. In persistent
        mode the Footer advertises the shell wrapper's ``C-a p`` toggle
        (``Ctrl-A`` is the close half — the popup captures input while open,
        so the outer ``C-a p`` binding can't fire then); ``q``/``Q``/``Esc``
        are active but hidden. In standalone the Footer shows ``q Quit``.
        """
        if self._persistent:
            self.BINDINGS = [
                Binding("a", "allow", "Allow"),
                Binding("A", "allow_duration", "Allow…"),
                Binding("d", "deny", "Deny"),
                Binding("D", "deny_duration", "Deny…"),
                Binding("r", "rules", "Rules"),
                Binding(
                    "ctrl+a",
                    "q_key",
                    "Hide/Show",
                    key_display="Ctrl-a p",
                    show=True,
                ),
                Binding("q", "q_key", "Hide", show=False),
                Binding("Q", "q_key", "Hide", show=False),
                Binding("escape", "q_key", "Hide", show=False),
            ]
        else:
            self.BINDINGS = [
                Binding("a", "allow", "Allow"),
                Binding("A", "allow_duration", "Allow…"),
                Binding("d", "deny", "Deny"),
                Binding("D", "deny_duration", "Deny…"),
                Binding("r", "rules", "Rules"),
                Binding("q", "q_key", "Quit", show=True),
                Binding("Q", "q_key", "Quit", show=False),
                Binding("escape", "q_key", "Quit", show=False),
                Binding("ctrl+a", "q_key", "Quit", show=False),
            ]
        self.refresh_bindings()

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="queue"):
            yield ListView(id="requests")
            yield Static("No held requests — connected, waiting.", id="empty")
        # #2332 pause control: silences ALL prompts for the workspace for a
        # window. Pinned just above the Footer; the active window is
        # highlighted and its countdown shows next to the buttons (#2383).
        yield Horizontal(
            *self._pause_buttons(),
            Static("", id="pause-countdown"),
            id="pause-bar",
        )
        yield Footer()

    def _pause_buttons(self) -> list[Button]:
        """Unpaused / Paused 15m / 1h / 1d (#2332, restyled #2383).

        ``pause_duration`` is None for Unpaused (clears a pause) or a window
        token; the button matching the user's last request is highlighted via
        ``pause-active`` (Unpaused is active when nothing is paused).
        """
        btns: list[Button] = []
        for label, dur in (
            ("Unpause", None),
            ("Pause 15m", DURATION_15M),
            ("Pause 1h", DURATION_1H),
            ("Pause 1d", DURATION_1D),
        ):
            b = Button(
                label, id="pause-none" if dur is None else f"pause-{dur}"
            )
            b.pause_duration = dur  # type: ignore[attr-defined]
            btns.append(b)
        return btns

    def on_mount(self) -> None:
        self.title = f"consent-decide · {self.workspace_name}"
        self._apply_bindings()
        self.run_worker(
            self._ws_loop, exclusive=True, group="ws", exit_on_error=False
        )
        self.set_interval(1.0, self._refresh)

    # -- WS worker ---------------------------------------------------------

    async def _rotate_token(self) -> None:
        """Refresh the JWT and adopt it when a fresh one comes back."""
        new = await self._refresh_token()
        if new:
            self.token = new

    async def _recover_after_refusal(self, refusals: int) -> int:
        """Handle a refused (403) handshake; returns the new refusal count.

        First refusal: maybe just an expired token (its 4002 close code is
        lost pre-accept) -- refresh and retry fast once. Repeated refusal:
        registration cannot succeed right now (see _ws_loop) -- log once,
        then fall back to the slow interval, not a tight loop and not a
        dead stop (#2490 review: a mid-session flip back to interactive
        must self-heal without restarting the shell)."""
        refusals += 1
        if refusals >= 2:
            if not self._refused:
                self._refused = True
                logger.warning(
                    "consent-decide: registration refused (403) "
                    "repeatedly; retrying every %.0fs",
                    REFUSED_RETRY_INTERVAL,
                )
            delay = REFUSED_RETRY_INTERVAL
        else:
            delay = self._backoff(1)
        await self._rotate_token()
        await asyncio.sleep(delay)
        self._refresh()
        return refusals

    async def _ws_loop(self) -> None:
        """Connect -> pump -> reconnect loop, until :attr:`_stop` is set.

        On any error (connect failure, mid-stream drop) the connection is
        dropped and, after backoff, re-attempted. An auth-related close
        (expired/invalid token, 4001/4002) refreshes the JWT first so the
        next attempt authenticates (mirrors ``monitor``). While disconnected
        the decider is deregistered server-side, so in-flight holds auto-deny
        on their own timeout (fail-closed) -- never silently allowed.

        A refused handshake (HTTP 403, #2490) is different: the server
        closed *before* accept (authz, egress mode, vanished workspace --
        or an expired token, since the pre-accept close code never reaches
        us and uvicorn answers every refusal with 403). The first refusal
        refreshes the JWT and retries fast (recovering the expired-token
        case); once refusals pile up (the counter resets only on a
        successful connect) the loop backs off to a fixed slow interval
        (:data:`REFUSED_RETRY_INTERVAL`) instead of stopping -- bounded
        log spam, but the decider still self-heals if the refusal cause
        goes away mid-session (workspace flipped back to interactive,
        permissions restored).
        """
        attempt = 0
        refusals = 0
        while not self._stop:
            auth_close = False
            refused = False
            try:
                async with ws_connect(
                    self.server_url,
                    token=self.token,
                    max_size=self.max_size,
                    path="/ws/consent-decider",
                    query={"workspace": self.workspace_id},
                    user_agent_header=USER_AGENT,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    attempt = 0
                    refusals = 0
                    if self._refused:
                        # Healed: the refusal cause went away -- back to
                        # normal connected/reconnect reporting.
                        self._refused = False
                    self._refresh()
                    auth_close = await self._pump(ws)
            except websockets.InvalidStatus as e:
                if e.response.status_code == 403:
                    refused = True
                else:
                    # A proxy/gateway error (502/503...) is transient -- retry.
                    logger.debug("consent-decide ws handshake failed: %s", e)
            except Exception as e:  # noqa: BLE001
                logger.debug("consent-decide ws dropped: %s", e)
            finally:
                self._ws = None
                self._connected = False
            if refused:
                refusals = await self._recover_after_refusal(refusals)
                continue
            if self._stop:
                break
            if auth_close:
                await self._rotate_token()
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
                self._react(action, payload)
        finally:
            ping.cancel()
            try:
                await ping
            except (asyncio.CancelledError, Exception):
                pass
        return auth_close

    def _react(self, action: str, payload) -> None:
        """React to one parsed frame outcome (isolated render; see _pump)."""
        if action == ADDED:
            # A held request arrived: surface it as the popup over the
            # shell (no-op in standalone, skipped if already shown).
            # Scheduled OFF the event loop (#2699): the show is
            # synchronous tmux subprocess work and must never gate
            # the render below — inline, the Allow/Deny row only
            # appeared ~seconds after the popup wrapper, because a
            # `display-popup` blocks until dismissed and always
            # outlives its 3 s timeout. The task starts on the next
            # loop tick, so in practice the row paints before the
            # viewer attaches (worst case it shows a tick later —
            # never seconds).
            self._schedule_popup_show()
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
            elif action == PAUSE_ACK:
                # payload = (ok, until). A failed pause/unpause flashes;
                # success is reflected by the refreshed egress_rules
                # frame the server broadcasts (no flash needed).
                ok, _until = payload  # type: ignore[misc]
                if not ok:
                    self._flash("pause failed")
                self._refresh()
            else:
                self._refresh()
        except Exception:
            logger.exception("consent-decide render failed")

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

    def _refresh_rules_screen(self) -> None:
        """Keep the rules screen live when it is the active screen (#2335
        slice B); refreshed first so a queue-query hiccup can never starve
        it -- the WS worker is shared across the switch."""
        try:
            screen = self.screen
        except Exception:
            screen = None  # not mounted yet (pre-mount / no screen stack)
        if isinstance(screen, RulesScreen):
            try:
                screen.refresh_rules()
            except Exception:
                logger.exception("consent-decide rules render failed")

    def _sync_request_rows(self, lv, ordered) -> list[str]:
        """Drop resolved/expired rows, repaint survivors in place, append
        new ones (stable order). Returns the ids of the appended rows."""
        current_ids = {req.id for req in ordered}
        # Drop resolved/expired rows (leave survivors untouched -> no flicker).
        for child in list(lv.children):
            rid = getattr(child, "request_id", None)
            if rid is not None and rid not in current_ids:
                child.remove()
        # Repaint survivors' countdown in place; append only new rows.
        existing = {getattr(c, "request_id", None): c for c in lv.children}
        new_ids: list[str] = []
        for req in ordered:
            item = existing.get(req.id)
            if item is None:
                lv.append(self._render_item(req))
                new_ids.append(req.id)
            else:
                self._update_item(item, req)
        return new_ids

    def _apply_focus_policy(
        self, new_ids, current_ids, focused_id, focused_index
    ) -> None:
        """Focus policy (#2383): a newly-arrived hold grabs focus; else keep
        focus on the previously-focused survivor; else (the focused hold was
        just resolved) move focus to the hold above it (or the new top) so
        deciding doesn't strand the user on an unfocused list."""
        if new_ids:
            self._select_by_id(new_ids[0])
        elif focused_id is not None and focused_id in current_ids:
            self._select_by_id(focused_id)
        elif focused_id is not None:
            self._select_index((focused_index or 0) - 1)

    def _status_line(self, ordered) -> str:
        """The connection/status line (flash message takes precedence)."""
        if self._connected:
            conn = "connected"
        elif self._refused:
            conn = f"refused — retrying every {int(REFUSED_RETRY_INTERVAL)}s"
        else:
            conn = "reconnecting"
        return (
            f" {escape(self.workspace_name)}  ·  {conn}"
            f"  ·  {len(ordered)} held"
        )

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
        self._refresh_rules_screen()
        try:
            lv = self.query_one("#requests", ListView)
            status = self.query_one("#status", Static)
            empty = self.query_one("#empty", Static)
        except Exception:
            return  # not mounted yet (pre-mount call)
        ordered = self.controller.ordered()
        current_ids = {req.id for req in ordered}
        focused_id = self._focused_request_id()
        focused_index = lv.index  # to refocus above a resolved hold
        new_ids = self._sync_request_rows(lv, ordered)
        self._apply_focus_policy(
            new_ids, current_ids, focused_id, focused_index
        )
        empty.display = not ordered
        if self._flash_until > time.time():
            status.update(self._flash_msg)
        else:
            status.update(self._status_line(ordered))
        # Pause state + countdown live on the pause bar (next to the buttons).
        self._refresh_pause_highlight()

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
        # Host line + per-row Allow/Deny (the only submit actions). Bare
        # Allow/Deny (button or `a`/`d`) sends the default duration
        # (`tilrestart`); `A`/`D` open the per-row duration picker first
        # (#2511) -- the duration travels with the action, never armed
        # beforehand.
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

    def _select_index(self, index: int) -> None:
        """Focus the request row at *index*, clamped to the list bounds."""
        lv = self.query_one("#requests", ListView)
        if not lv.children:
            return
        lv.index = max(0, min(index, len(lv.children) - 1))

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
        if bid.startswith("pause-"):
            self._handle_pause_button(event.button)
        elif bid.startswith("allow-"):
            self._decide_id(
                bid.removeprefix("allow-"),
                DECISION_ALLOWED,
                DURATION_DEFAULT,
            )
        elif bid.startswith("deny-"):
            self._decide_id(
                bid.removeprefix("deny-"), DECISION_DENIED, DURATION_DEFAULT
            )

    def _handle_pause_button(self, button: Button) -> None:
        """Send a pause (window token) or unpause (Unpaused) frame (#2332)."""
        ws = self._ws
        if ws is None:
            self._flash("disconnected — reconnecting")
            return
        dur = getattr(button, "pause_duration", None)
        # Track the user's request so the matching button stays highlighted.
        self._pause_duration = dur
        if dur is None:
            asyncio.create_task(self._send_unpause(ws))
        else:
            asyncio.create_task(self._send_pause(ws, dur))

    async def _send_pause(self, ws, duration: str) -> None:
        try:
            await ws.send(make_pause(duration))
        except Exception:
            self._flash("pause send failed — reconnecting")

    async def _send_unpause(self, ws) -> None:
        try:
            await ws.send(make_unpause())
        except Exception:
            self._flash("unpause send failed — reconnecting")

    def _refresh_pause_highlight(self) -> None:
        """Highlight the pause button matching the user's last request + show
        the live countdown next to the buttons (#2332, restyled #2383).

        The server's pause frame carries only ``until`` (not which window),
        so the active button is the one matching the user's last pause/unpause
        request (``self._pause_duration``); Unpaused is active when nothing is
        paused. The exact remaining window comes from the rules frame. A
        finite window that has elapsed is treated as not paused (#2498).
        """
        rules = self.controller.rules
        expired = rules is not None and self.controller.pause_expired(rules)
        if expired:
            # #2498: the finite window elapsed locally -- the workspace is
            # effectively unpaused, so clear the stale countdown and fall
            # back to the Unpaused highlight instead of freezing at "paused
            # 0s" until the next server frame. Forget the requested window
            # too, so a post-expiry re-broadcast (paused=None) can't
            # re-light the stale Pause button.
            self._pause_duration = None
        target = self._pause_duration
        for b in self.query("#pause-bar Button"):
            b.set_class(
                getattr(b, "pause_duration", None) == target, "pause-active"
            )
        countdown = self.query_one("#pause-countdown", Static)
        if rules is not None and rules.paused is not None and not expired:
            rem = self.controller.pause_remaining(rules)
            countdown.update(
                "paused until restart"
                if rem is None
                else f"paused {fmt_duration(rem)}"
            )
        else:
            countdown.update("")

    def _on_queue_screen(self) -> bool:
        """True when the held-request queue is the active screen.

        ``a``/``d``/``A``/``D`` must be inert on the rules screen (no row is
        visible there) and while the duration picker modal is up (a stray
        keypress through the modal must not decide a row behind it).
        """
        return not isinstance(self.screen, (RulesScreen, DurationPickerScreen))

    def action_allow(self) -> None:
        # `a` key -> the highlighted row, with the default duration
        # (`tilrestart`): the common case stays one keypress (#2511).
        if not self._on_queue_screen():
            return
        self._decide(DECISION_ALLOWED)

    def action_deny(self) -> None:
        if not self._on_queue_screen():
            return
        self._decide(DECISION_DENIED)

    def action_allow_duration(self) -> None:
        """``A``: allow the highlighted row with a picked duration (#2511)."""
        self._open_duration_picker(DECISION_ALLOWED)

    def action_deny_duration(self) -> None:
        """``D``: deny the highlighted row with a picked duration (#2511)."""
        self._open_duration_picker(DECISION_DENIED)

    def _open_duration_picker(self, decision: str) -> None:
        if not self._on_queue_screen():
            return
        rid = self._focused_request_id()
        if rid is None:
            return
        host = next(
            (r.dest_host for r in self.controller.ordered() if r.id == rid),
            rid,
        )
        self.push_screen(DurationPickerScreen(rid, decision, host))

    def action_rules(self) -> None:
        # `r` opens the read-only rules screen (#2335 slice B). Guard against
        # pushing a second one if `r` is pressed while already viewing rules.
        if isinstance(self.screen, RulesScreen):
            return
        self.push_screen(RulesScreen())

    def action_q_key(self) -> None:
        """``q``/``Q``/``Esc``: hide the popup viewer (persistent) or quit.

        Persistent mode runs the decider inside a hidden tmux session; ``q``,
        ``Q``, and ``Escape`` detach the viewer so the always-on decider is
        never quit by a key (it dies only when the shell ends). Standalone
        quits as before. (On the rules screen ``Esc`` still returns to the
        queue — that screen's own binding takes precedence.) Reopen the popup
        with the shell wrapper's reopen key. The detach itself is scheduled
        off the event loop (:meth:`_schedule_viewer_hide`) — it is the same
        blocking-tmux-subprocess failure mode the show path fixed (#2699).
        """
        if self._persistent:
            self._schedule_viewer_hide()
        else:
            self.exit()

    def _schedule_viewer_hide(self) -> None:
        """Schedule :meth:`_hide_viewer` on a worker thread, once at a time.

        ``_hide_viewer`` is a synchronous ``tmux detach-client`` subprocess
        (3 s timeout); run inline from the key action a contended tmux
        server froze the UI for up to 3 s on ``q``/``Esc`` — the identical
        failure mode the show path fixed (#2699). Deduplicated like the
        show: ``q`` mashing reuses the in-flight detach (a second detach of
        an already-detaching viewer is pointless).
        """
        if not self.popup_socket or not self.popup_session:
            return  # standalone: nothing to detach
        task = self._hide_task
        if task is not None and not task.done():
            return  # a detach is already in flight
        self._hide_task = asyncio.create_task(
            asyncio.to_thread(self._hide_viewer)
        )

    def _hide_viewer(self) -> None:
        """Detach the popup viewer so it hides; the decider stays registered.

        No-op when not running under a popup (standalone `consent-decide`).
        A failed/stale detach is swallowed -- the decider keeps running
        either way, and the viewer is reopened with the reopen key.
        Blocking by design (see :meth:`_schedule_viewer_hide`): callers on
        the event loop must go through the scheduler, which runs this on a
        worker thread.
        """
        sock = self.popup_socket
        sess = self.popup_session
        if not sock or not sess:
            return
        try:
            subprocess.run(
                build_detach_command(sock, sess),
                capture_output=True,
                timeout=3,
            )
        except Exception:  # noqa: BLE001
            logger.debug("consent-decide viewer detach failed")

    def _schedule_popup_show(self) -> None:
        """Schedule :meth:`_show_popup` on a worker thread, once at a time.

        The show is synchronous tmux work — two ``list-clients`` queries
        plus a ``display-popup`` subprocess that **blocks until the popup
        is dismissed** (it always outlives its 3 s timeout, then is killed;
        the popup itself stays up). Called inline from the async pump this
        froze the UI event loop for the full timeout, so the held-request
        row only rendered seconds after the popup wrapper appeared
        (#2699). Off the loop, the row renders immediately (the render is
        not gated on the show) and the popup wrapper follows within one
        tmux round-trip.

        Deduplicated: while one show is in flight, later ADDED frames skip
        — the popup shows the whole held-request queue, not one request,
        so the in-flight show already covers them. A show that targeted
        nothing is retried inside the worker (see
        :meth:`_popup_show_worker`); once the worker ends, the slot frees
        so the next held request schedules a fresh one.
        """
        if not self.popup_socket or not self.popup_session:
            return  # standalone: no popup to show (skip the thread entirely)
        task = self._show_popup_task
        if task is not None and not task.done():
            return  # a show is already in flight; it covers this request too
        self._show_popup_task = asyncio.create_task(self._popup_show_worker())

    async def _popup_show_worker(self) -> None:
        """Run :meth:`_show_popup` off the event loop; never raises (#2699).

        Retries a show that targeted nothing while holds remain pending
        (#2699 review): a failed first attempt (contended tmux server,
        ``list-clients`` timeout) must not strand requests that arrived
        during the attempt's dedupe window — without a retry they would
        sit unpopup'd until the next ADDED frame or a reconnect, and
        auto-deny unseen. Bounded by ``POPUP_SHOW_ATTEMPTS``; once the
        worker ends the slot frees and the next ADDED frame schedules a
        fresh one. :meth:`_show_popup` swallows its subprocess errors;
        this guard keeps the fire-and-forget task from surfacing anything
        (an unretrieved task exception would just log noise).
        """
        try:
            for _ in range(POPUP_SHOW_ATTEMPTS):
                shown = await asyncio.to_thread(self._show_popup)
                if shown or not self.controller.pending:
                    return
                await asyncio.sleep(POPUP_SHOW_RETRY_DELAY)
        except Exception:  # noqa: BLE001
            logger.exception("consent-decide popup show failed")

    def _show_popup(self) -> bool:
        """Show the popup on the user's shell client when a request arrives.

        No-op when not running under a popup (standalone ``consent-decide``).
        Skipped when the popup is already open (the hidden session has a
        viewer client attached). A failed show is swallowed -- the decider
        keeps running and the user can reopen with the shell's reopen key.
        Blocking by design (see :meth:`_schedule_popup_show`): callers on
        the event loop must go through the scheduler, which runs this on a
        worker thread.

        Returns True when the popup is (or was just made) visible on at
        least one shell client — including the already-open case. A
        per-client ``display-popup`` that hits its timeout still counts as
        shown: the call blocks while the popup is open, so the timeout
        means it opened and stayed. False means nothing could be targeted
        (standalone, or no outer client was found — e.g. a contended tmux
        server timing out ``list-clients``); the worker retries so holds
        that arrived meanwhile are not stranded.
        """
        sock = self.popup_socket
        sess = self.popup_session
        if not sock or not sess:
            return False
        if hidden_has_client(sock, sess):
            return True  # popup already open
        w, h = DEFAULT_POPUP_SIZE
        clients = outer_clients(sock, sess)
        for client in clients:
            try:
                subprocess.run(
                    show_popup_argv(sock, sess, client, w=w, h=h),
                    capture_output=True,
                    timeout=3,
                )
            except Exception:  # noqa: BLE001
                logger.debug("consent-decide popup show failed")
        return bool(clients)

    def _decide(self, decision: str) -> None:
        rid = self._focused_request_id()
        if rid is None:
            return
        self._decide_id(rid, decision, DURATION_DEFAULT)

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


class DurationPickerScreen(ModalScreen[None]):
    """Per-row duration picker (#2511): pick a duration for one verdict.

    The TUI analogue of the web banner's split Allow/Deny ▾ menu
    (post-#2499 design): the duration is chosen **with** the action, never
    armed beforehand -- `a`/`d` (or a row button) send the default
    (`tilrestart`) directly; `A`/`D` open this picker for the highlighted
    row first. Enter on an option submits that row's verdict with the chosen
    duration; ``Esc``/``q`` dismiss without sending anything. A modal screen
    (centered overlay) because Textual cannot anchor a widget to a specific
    ListView row -- the row is named in the title instead.
    """

    CSS = """
    DurationPickerScreen { align: center middle; background: transparent; }
    #picker-panel {
        width: auto; max-width: 64; height: auto;
        border: round $accent; background: $panel; padding: 0 1;
    }
    #picker-title { padding: 0 1; color: $text; }
    #picker-durations { width: 24; height: auto; max-height: 12; }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]

    def __init__(self, request_id: str, decision: str, host: str) -> None:
        super().__init__()
        self.request_id = request_id
        self.decision = decision
        self.host = host

    @property
    def _verb(self) -> str:
        return "Allow" if self.decision == DECISION_ALLOWED else "Deny"

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-panel"):
            yield Static(
                f"{self._verb} {escape(self.host)} — pick a duration",
                id="picker-title",
            )
            yield OptionList(*SELECTABLE_DURATIONS, id="picker-durations")
            yield Static("Enter sends · Esc cancels", id="picker-hint")

    def on_mount(self) -> None:
        ol = self.query_one("#picker-durations", OptionList)
        # The default duration starts highlighted, so Enter alone repeats
        # the bare-`a` outcome; arrows then Enter pick any other duration.
        ol.highlighted = SELECTABLE_DURATIONS.index(DURATION_DEFAULT)
        ol.focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Enter: submit the row's verdict with the chosen duration."""
        duration = SELECTABLE_DURATIONS[event.option_index]
        app = self.app
        if isinstance(app, ConsentDeciderApp):
            app._decide_id(self.request_id, self.decision, duration)
        self.dismiss(None)

    def action_cancel(self) -> None:
        """``Esc``/``q``: dismiss without sending anything."""
        self.dismiss(None)


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

    # #2362: the last highlighted revoke target (id + position), remembered
    # from ``ListView.Highlighted`` events. ``lv.clear()`` inside a rebuild
    # resets the highlight to None; without this memory a second rebuild
    # bursting within the same refresh cycle would capture "no focus" and
    # drop the restore, leaving the highlight on index 0 -- the newest rule
    # -- so a subsequent ``x`` would revoke the wrong row.
    _last_focused_rule_id: str | None = None
    _last_focused_rule_index: int = 0

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Track the highlighted revoke target (#2362).

        The None event (posted when ``clear()`` or a reset drops the
        highlight) is ignored so the memory keeps the decider's last real
        focus through rebuild bursts.
        """
        if event.item is None:
            return
        self._last_focused_rule_id = getattr(event.item, "rule_id", None)
        for index, child in enumerate(event.list_view.children):
            if child is event.item:
                self._last_focused_rule_index = index
                break

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

    def _capture_focused_rule_id(self, lv) -> str | None:
        """The focused rule id before ``lv.clear()`` resets the highlight.

        When ``highlighted_child`` is already None (a first rebuild in the
        same refresh cycle cleared it and its deferred restore has not
        landed), fall back to the id remembered from the last
        ``Highlighted`` event (:attr:`_last_focused_rule_id`), which a
        ``clear()`` cannot clobber -- so two rule-set changes inside one
        refresh cycle still restore the focused row (#2362)."""
        focused = (
            getattr(lv.highlighted_child, "rule_id", None)
            if lv.highlighted_child is not None
            else None
        )
        if focused is None:
            # Burst window: a prior rebuild in this same refresh cycle
            # cleared the highlight and its restore has not landed yet --
            # the remembered id is the decider's actual focus (#2362).
            focused = self._last_focused_rule_id
        return focused

    def _rebuild_rule_list(self, rules: EgressRules | None) -> None:
        """Rebuild the revoke selector from the controller's rules (#2341).

        Compact rows (no countdown -- the detail body above ticks that). Only
        rebuilt when the set of revocable rule ids changes, so the 1s refresh
        never flickers it. The static allow-list is intentionally absent -> it
        is never a revoke target.

        Focus survives the rebuild (#2362): the focused rule id is captured
        *before* ``lv.clear()`` resets the highlight, and restored once the
        rebuild lands. The capture never reads None-through-a-burst: when
        ``highlighted_child`` is already None (a first rebuild in the same
        refresh cycle cleared it and its deferred restore has not landed), it
        falls back to the id remembered from the last ``Highlighted`` event
        (:attr:`_last_focused_rule_id`), which a ``clear()`` cannot clobber.
        So two rule-set changes inside one refresh cycle -- an ``egress_rules``
        refresh plus a ``revoke_ack``, or two near-simultaneous verdicts --
        still restore the row the decider focused, never silently index 0
        (the newest rule) of a reordered list. A subsequent ``x`` then
        revokes the intended row.
        """
        lv = self.query_one("#rules-list", ListView)
        desired: list[str] = []
        if rules is not None:
            desired += [r.id for r in rules.allowed]
            desired += [r.id for r in rules.denied]
        current = [getattr(c, "rule_id", None) for c in lv.children]
        if current == desired:
            return  # unchanged -> no flicker
        focused = self._capture_focused_rule_id(lv)
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
                        return
                # The focused rule left the snapshot (revoke ack elsewhere /
                # expiry): fall to a deterministic neighbor -- the old focus
                # position clamped to the new list -- never silently index 0
                # of a reordered list.
                if _lv.children:
                    _lv.index = min(
                        self._last_focused_rule_index, len(_lv.children) - 1
                    )

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

    @staticmethod
    def _allow_list_section(rules: EgressRules | None) -> list[str]:
        """The static allow-list block (raw host specs, no countdown)."""
        lines = ["[bold]Static allow-list[/bold]"]
        if rules is None:
            lines.append("  [dim](no rules received yet)[/dim]")
        elif rules.allow_list:
            for d in rules.allow_list:
                lines.append(f"  {escape(d)}")
        else:
            lines.append("  [dim](none)[/dim]")
        return lines

    def _rules_section(
        self, title: str, rules: EgressRules | None, controller, *, deny: bool
    ) -> list[str]:
        """One active-verdicts block (allows or denies) with its count."""
        rows = (rules.denied if deny else rules.allowed) if rules else []
        lines = [f"[bold]{title} ({len(rows)})[/bold]"]
        if rows:
            for r in rows:
                lines.append("  " + self._rule_line(r, controller, deny=deny))
        else:
            lines.append("  [dim](none)[/dim]")
        return lines

    @staticmethod
    def _pause_section(rules: EgressRules | None, controller) -> list[str]:
        """The pause-window block (#2332; absent -> nothing). A finite
        window that already elapsed renders nothing (#2498): the 1s
        refresh clears the stale section locally until the next frame
        confirms the server's post-expiry state."""
        if (
            rules is None
            or rules.paused is None
            or controller.pause_expired(rules)
        ):
            return []
        lines = ["", "[bold]Pause[/bold]"]
        rem = controller.pause_remaining(rules)
        if rem is None:
            lines.append("  [yellow]Filtering paused until restart[/yellow]")
        else:
            lines.append(
                "  [yellow]Filtering paused "
                f"(resumes in {fmt_duration(rem)})[/yellow]"
            )
        return lines

    def _render_body(self, rules: EgressRules | None, controller) -> str:
        """Build the grouped rich-markup body (allow-list, allows, denies, pause)."""
        lines: list[str] = []
        lines += self._allow_list_section(rules)
        lines.append("")
        lines += self._rules_section(
            "Active allows", rules, controller, deny=False
        )
        lines.append("")
        lines += self._rules_section(
            "Active denies", rules, controller, deny=True
        )
        lines += self._pause_section(rules, controller)
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
            # and deny must degrade the same way (else fmt_duration(None)
            # raises TypeError on a deny) -- the parser permits these rows, so
            # rendering must too.
            if rem is None:
                label = ""
            elif deny:
                label = f"{fmt_duration(rem)} left"
            else:
                label = f"expires in {fmt_duration(rem)}"
        return f"{escape(host)}{escape(proc)}  [dim]{escape(label)}[/dim]"
