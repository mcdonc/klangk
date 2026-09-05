"""ASGI middleware owned by the app composition root.

Moved out of ``main.py`` in the #2738 module split; behavior is
unchanged. Three middlewares live here:

- :class:`LiveCORSMiddleware` — CORS that re-reads allowed origins from
  ``app.state`` on each request (SIGHUP-reloadable, #1610).
- :class:`InFlightMiddleware` + :class:`InFlightRequests` — in-flight
  HTTP request counting that backs the quiesce phase of the graceful
  restart/shutdown paths (#2527).
- :class:`ApiRateLimitMiddleware` — per-client-IP request budget on
  ``/api/*`` routes (429 + ``Retry-After``, #3157).

Stack ordering (outermost first, as wired in ``main.build_app``):
``ServerErrorMiddleware`` → no-cache (``static.no_cache_headers``) →
``LiveCORSMiddleware`` → ``InFlightMiddleware`` →
``ApiRateLimitMiddleware`` → ``ExceptionMiddleware`` → router. CORS sits
*outside* the in-flight counter on purpose: a CORS preflight (OPTIONS) is
answered by the CORS layer itself without reaching the app, so prefights
are instant and never counted; every request that actually reaches the app
is counted. The audit (#2738) confirmed this ordering is correct. The
rate limiter sits innermost so a rejected request is still counted as
in-flight by the quiesce counter (it completes immediately) and its 429
still receives CORS headers from the CORS layer outside it (#3157).
"""

import asyncio
import json
import logging
import math
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers

logger = logging.getLogger(__name__)


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


# --- Per-client-IP API rate limiting (#3157) ---

# Sliding-window length, seconds. Matches the window the docs advertise
# (``KLANGKD_API_RATE_LIMIT`` requests per 60s per client IP).
RATE_LIMIT_WINDOW_SECONDS = 60

# Default per-IP budget when ``KLANGKD_API_RATE_LIMIT`` is unset/None.
RATE_LIMIT_DEFAULT = 300

# Upper bound on tracked per-IP windows. Same shape as the login/resend
# cooldown accounting in api/auth.py: legitimate traffic stays far below
# it (distinct client IPs within one window); a flood of unique source IPs
# stops growing the dict at the cap, degrading the limit to the most
# recent entries instead of exhausting memory (#3157).
RATE_LIMIT_MAX_ENTRIES = 10_000


class ApiRateLimitMiddleware:
    """Pure-ASGI per-client-IP request budget on ``/api/*`` routes (#3157).

    Enforced in the backend process, not the fronting proxy: the long tail
    of the API surface (workspace enumeration, token refresh, scraping)
    gets one fixed 60s window per client IP — ``KLANGKD_API_RATE_LIMIT``
    requests per window (default 300, 0 disables). Over-budget requests
    are answered 429 + ``Retry-After`` with the FastAPI ``{"detail": …}``
    error shape. Static assets, ``/ws`` upgrades, ``/hosted/*``, and the
    health endpoints never consume budget: only ``http`` scopes whose
    path starts with ``/api/`` are counted.

    Keying uses ``app.state.util.effective_client_ip()`` — the same
    proxy-trust-aware resolver the auth surface uses (forwarded headers
    honored only from a trusted peer), so the budget is correct both
    bare and behind the managed Caddy / an outer proxy. A request with
    no resolvable client (``None``) bypasses the budget: there is no key
    to attribute it to.

    Budget and window state: the budget is read live off
    ``app.state.settings`` on every request, so a SIGHUP settings swap
    changes the limit without a restart (the in-flight window keeps its
    start time). State is a process-local dict — klangkd is a single
    uvicorn process — bounded per ``RATE_LIMIT_MAX_ENTRIES``. Not a
    ``BaseHTTPMiddleware`` subclass: this is a pure ``__call__`` wrapper
    (no body-buffering, no task-group overhead).
    """

    def __init__(self, app_asgi, *, fastapi_app: FastAPI) -> None:
        self.app = app_asgi
        self._fastapi_app = fastapi_app
        # ip -> (window_start_monotonic, requests_in_window, warned).
        # Insertion-ordered; the first-seen entry is shed first when the
        # cap is hit. "warned" latches the once-per-window denial log.
        self._windows: dict[str, tuple[float, int, bool]] = {}

    def _budget(self) -> int:
        """Effective per-IP budget (live off settings; 0 disables)."""
        raw = self._fastapi_app.state.settings.api_rate_limit
        return RATE_LIMIT_DEFAULT if raw is None else raw

    def _client_key(self, scope: dict) -> str | None:
        """The proxy-trust-aware client IP for an ASGI http scope."""
        client = scope.get("client")
        client_host = client[0] if client else None
        return self._fastapi_app.state.util.effective_client_ip(
            Headers(scope=scope), client_host
        )

    def _retry_after(self, scope: dict) -> int:
        """0 = allow; otherwise the seconds until the caller's window resets."""
        budget = self._budget()
        if budget <= 0 or not scope["path"].startswith("/api/"):
            return 0
        key = self._client_key(scope)
        if key is None:
            return 0
        return self._window_retry_after(key, budget)

    def _window_retry_after(self, key: str, budget: int) -> int:
        """Record one request for *key*; 0 to allow, else seconds to wait."""
        now = time.monotonic()
        start, count, warned = self._windows.get(key, (now, 0, False))
        if now - start >= RATE_LIMIT_WINDOW_SECONDS:
            start, count, warned = now, 0, False
        if count >= budget:
            remaining = start + RATE_LIMIT_WINDOW_SECONDS - now
            retry = max(1, math.ceil(remaining))
            self._warn_once(key, start, count, warned, retry)
            return retry
        self._record(key, start, count, now)
        return 0

    def _warn_once(
        self, key: str, start: float, count: int, warned: bool, retry: int
    ) -> None:
        """Log the first denial of a window (once per IP per window, so a
        scraper hammering away cannot log-flood; the latch resets with
        the window). The one line is the operator's only signal that real
        users are being throttled — e.g. behind a misconfigured outer
        proxy funneling every client into one bucket."""
        if warned:
            return
        logger.warning(
            "per-client-IP API rate limit exceeded for %s;"
            " retrying in %ss (KLANGKD_API_RATE_LIMIT)",
            key,
            retry,
        )
        self._windows[key] = (start, count, True)

    def _record(self, key: str, start: float, count: int, now: float) -> None:
        """Insert the incremented window, pruning expired/oldest entries
        so a unique-IP flood cannot grow the dict unboundedly. Shedding
        can evict a live window, granting that IP a fresh budget on its
        next request — accepted: reaching the cap needs ~RATE_LIMIT_MAX_ENTRIES
        distinct client IPs within one 60s window, and every key is
        source-IP or trusted-proxy derived, so the reset costs an attacker
        nothing it didn't already have (a fresh bucket per new IP)."""
        self._prune_expired(now)
        while len(self._windows) >= RATE_LIMIT_MAX_ENTRIES:
            del self._windows[next(iter(self._windows))]
        self._windows[key] = (start, count + 1, False)

    def _prune_expired(self, now: float) -> None:
        """Drop windows whose 60s window has closed."""
        for key, (start, _, _) in list(self._windows.items()):
            if now - start >= RATE_LIMIT_WINDOW_SECONDS:
                del self._windows[key]

    async def _reject(self, send, retry_after: int) -> None:
        """Send the 429 (FastAPI error shape + Retry-After)."""
        body = json.dumps({"detail": "Too many requests; retry later"}).encode(
            "ascii"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        retry_after = self._retry_after(scope)
        if retry_after == 0:
            await self.app(scope, receive, send)
            return
        await self._reject(send, retry_after)


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
