"""Periodic WebSocket idle sweep for the session idle timeout (#3151).

A login session nobody is using must not survive on proactive token
refreshes alone. The
HTTP half is enforced at the refresh seam (``Auth._reject_idle_session``);
this loop is the WebSocket half: a socket that stops sending frames past
the window is closed by the server (4001 → client logout), so a quiet WS
cannot keep a session alive past the timeout. Inbound frames — including
the frontend's 60-second heartbeat — reset the connection's idle clock,
so a browser the user is actually watching stays connected.

The sweep interval adapts to the window so termination lands within the
window regardless of how small an operator sets it. Unarmed (window 0)
sweeps return immediately.
"""

from __future__ import annotations

import logging

from klangk.interval import IntervalWorker

logger = logging.getLogger(__name__)

#: Sweep cadence bounds (seconds): fast enough that a window as small as
#: one minute still terminates well inside it, slow enough that an armed
#: deploy with idle connections pays a handful of dict reads per sweep.
MIN_SWEEP_INTERVAL = 5.0
MAX_SWEEP_INTERVAL = 60.0


class SessionIdleMonitor(IntervalWorker):
    """Close WebSocket connections quiet past the idle window (#3151).

    Constructed once in :func:`klangk.main.build_app` and stored on
    ``app.state.session_idle_monitor``; started in the lifespan and
    stopped on shutdown. Owns only ``app`` — the window is read live so
    a SIGHUP reload applies on the next sweep.
    """

    log_label = "session-idle: WebSocket idle sweep"

    @property
    def interval(self) -> float:
        """A third of the shortest window any user can have (the general
        setting, or the privileged one when shorter), clamped 5–60s."""
        settings = self.app.state.settings
        window = settings.session_idle_timeout_minutes
        privileged = settings.privileged_session_idle_timeout_minutes
        if 0 < privileged < window:
            window = privileged
        window_secs = window * 60
        if window_secs <= 0:
            return MAX_SWEEP_INTERVAL
        return min(
            MAX_SWEEP_INTERVAL, max(MIN_SWEEP_INTERVAL, window_secs / 3)
        )

    async def sweep(self) -> None:
        """One sweep: close every connection idle past its owner's window."""
        if self.app.state.settings.session_idle_timeout_minutes <= 0:
            return
        sockets = self.app.state.sockets
        closed = await sockets.close_idle_connections(
            self.app.state.auth.idle_window_minutes_for_user
        )
        if closed:
            logger.info(
                "session idle timeout: closed %d idle WebSocket(s)", closed
            )
