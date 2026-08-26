"""ASGI middleware owned by the app composition root.

Moved out of ``main.py`` in the #2738 module split; behavior is
unchanged. Two middlewares live here:

- :class:`LiveCORSMiddleware` — CORS that re-reads allowed origins from
  ``app.state`` on each request (SIGHUP-reloadable, #1610).
- :class:`InFlightMiddleware` + :class:`InFlightRequests` — in-flight
  HTTP request counting that backs the quiesce phase of the graceful
  restart/shutdown paths (#2527).

Stack ordering (outermost first, as wired in ``main.build_app``):
``ServerErrorMiddleware`` → no-cache (``static.no_cache_headers``) →
``LiveCORSMiddleware`` → ``InFlightMiddleware`` → ``ExceptionMiddleware``
→ router. CORS sits *outside* the in-flight counter on purpose: a CORS
preflight (OPTIONS) is answered by the CORS layer itself without
reaching the app, so prefights are instant and never counted; every
request that actually reaches the app is counted. The audit (#2738)
confirmed this ordering is correct.
"""

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# --- Live CORS middleware (#1610) ---
# Instead of a static CORSMiddleware, this wrapper re-reads allowed origins
# from app.state.util.cors_origins() on every request so a SIGHUP reload
# of KLANGKD_CORS_ORIGINS takes effect without a process restart.


class LiveCORSMiddleware:
    """CORS middleware that reads allowed origins from app state on each request.

    Delegates to a ``CORSMiddleware`` instance that is rebuilt whenever the
    origin list changes.  The check-and-rebuild is O(1) most of the time
    (pointer comparison of the settings object).
    """

    def __init__(self, app_asgi, *, fastapi_app: FastAPI) -> None:
        self.app = app_asgi
        self._fastapi_app = fastapi_app
        self._last_settings = None
        self._inner: CORSMiddleware | None = None

    def _rebuild_if_needed(self) -> CORSMiddleware:
        current = self._fastapi_app.state.settings
        if current is not self._last_settings or self._inner is None:
            self._last_settings = current
            self._inner = CORSMiddleware(
                self.app,
                allow_origins=self._fastapi_app.state.util.cors_origins(),
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        return self._inner

    async def __call__(self, scope, receive, send):
        inner = self._rebuild_if_needed()
        await inner(scope, receive, send)


class InFlightRequests:
    """In-flight HTTP request counter (#2527 graceful restart/shutdown).

    Backs the quiesce phase of both the SIGHUP restart and the
    TERM/INT shutdown: after new container starts are refused,
    :meth:`wait_for_idle` waits for the request count to reach zero
    before the containers are drained. Not an owned subsystem —
    a plain counter with no app dependency.
    """

    def __init__(self) -> None:
        self.count = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def increment(self) -> None:
        if self.count == 0:
            self._idle.clear()
        self.count += 1

    def decrement(self) -> None:
        self.count = max(0, self.count - 1)
        if self.count == 0:
            self._idle.set()

    async def wait_for_idle(self, timeout: float) -> bool:
        """Wait until no requests are in flight; False on timeout."""
        if self.count == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            return False
        return True


class InFlightMiddleware:
    """Pure-ASGI wrapper counting in-flight ``http`` requests (#2527).

    ``http`` scopes only — a WebSocket connection never "completes", so
    counting it would block the drain quiesce forever. The counter is
    shared via ``app.state.inflight_requests`` so the SIGHUP restart
    and TERM/INT shutdown paths can wait on it.
    """

    def __init__(self, app_asgi, counter: InFlightRequests) -> None:
        self.app = app_asgi
        self.counter = counter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.counter.increment()
        try:
            await self.app(scope, receive, send)
        finally:
            self.counter.decrement()
