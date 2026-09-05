"""Unit tests for the per-client-IP API rate-limit middleware (#3157).

Covers the pure-ASGI middleware directly (scope/send capture) and once
through a real FastAPI router (httpx ASGITransport) for the wire shape of
the 429. Runtime enforcement against a real klangkd is covered by the
backend E2E suite (``test_api_rate_limit_e2e.py``).
"""

import json
import types

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import klangk.middleware as mw
from klangk.middleware import ApiRateLimitMiddleware
from klangk.util import Util

from _helpers import make_settings


# ---------------------------------------------------------------------------
# Harness: build the middleware the way build_app does (fastapi_app ref,
# real Util for the proxy-trust keying), call it with hand-built scopes.
# ---------------------------------------------------------------------------


async def _noop_app(scope, receive, send):
    """Inner ASGI app that records it was reached."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class _Capture:
    """send-capture for asserting on raw ASGI response messages."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def headers(self):
        return {
            k.decode().lower(): v.decode()
            for k, v in self.messages[0]["headers"]
        }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _scope(path="/api/v1/version", client=("127.0.0.1", 49999), headers=None):
    """A minimal http ASGI scope (only what the middleware reads)."""
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [
            (k.lower().encode(), v.encode())
            for k, v in (headers or {}).items()
        ],
        "client": client,
    }


def _middleware(budget="1", **env):
    """ApiRateLimitMiddleware wired like build_app (real settings + Util)."""
    settings = make_settings({**env, "KLANGKD_API_RATE_LIMIT": budget})
    fastapi_app = FastAPI()
    fastapi_app.state.settings = settings
    fastapi_app.state.util = Util(fastapi_app)
    return ApiRateLimitMiddleware(_noop_app, fastapi_app=fastapi_app)


async def _call(m, scope):
    capture = _Capture()
    await m(scope, _receive, capture)
    return capture


class TestPassThrough:
    async def test_non_http_scope_passes_through(self):
        """WebSocket scopes bypass the budget (never limited)."""
        m = _middleware()
        seen = {}

        async def inner_app(scope, receive, send):
            seen["reached"] = True

        m.app = inner_app
        await m({"type": "websocket", "path": "/ws"}, _receive, _Capture())
        assert seen["reached"] is True

    async def test_missing_client_bypasses_budget(self):
        """A request with no resolvable client (bare UDS peer, no
        forwarded headers) has no key to attribute — allowed through."""
        m = _middleware(budget="1")
        for _ in range(3):
            capture = await _call(m, _scope(client=None))
        assert capture.status == 200

    async def test_non_api_paths_never_consume_budget(self):
        """Static assets, /ws, /hosted/*, /health, /llm-proxy/ — anything
        not under /api/ is unlimited."""
        m = _middleware(budget="1")
        for path in (
            "/health",
            "/",
            "/assets/main.dart.js",
            "/ws",
            "/hosted/ws1/",
            "/llm-proxy/v1/chat",
            "/apiversion",
        ):
            for _ in range(3):
                capture = await _call(m, _scope(path=path))
            assert capture.status == 200, path


class TestBudget:
    async def test_allows_under_budget(self):
        m = _middleware(budget="3")
        for _ in range(3):
            capture = await _call(m, _scope())
        assert capture.status == 200

    async def test_denies_over_budget_with_429_and_retry_after(self):
        m = _middleware(budget="2")
        assert (await _call(m, _scope())).status == 200
        assert (await _call(m, _scope())).status == 200
        denied = await _call(m, _scope())
        assert denied.status == 429
        retry_after = int(denied.headers["retry-after"])
        assert 1 <= retry_after <= 60
        body = json.loads(denied.messages[1]["body"].decode("utf-8"))
        assert "detail" in body

    async def test_window_rollover_resets_the_count(self):
        """After the 60s window closes the same IP gets a fresh budget."""
        m = _middleware(budget="1")
        assert (await _call(m, _scope())).status == 200
        assert (await _call(m, _scope())).status == 429
        # Age the open window past the window length.
        for key, (start, count, warned) in list(m._windows.items()):
            m._windows[key] = (start - 61.0, count, warned)
        assert (await _call(m, _scope())).status == 200

    async def test_retry_after_reflects_window_remainder(self):
        m = _middleware(budget="1")
        await _call(m, _scope())
        # 59s elapsed: exactly 1s of window left -> Retry-After: 1.
        for key, (start, count, warned) in list(m._windows.items()):
            m._windows[key] = (start - 59.0, count, warned)
        denied = await _call(m, _scope())
        assert denied.status == 429
        assert denied.headers["retry-after"] == "1"

    async def test_zero_disables_limiting(self):
        m = _middleware(budget="0")
        for _ in range(10):
            capture = await _call(m, _scope())
        assert capture.status == 200

    async def test_none_budget_means_default(self):
        """Defensive parity: a None attribute (the field never surfaces
        None in practice) falls back to the documented default."""
        fastapi_app = FastAPI()
        fastapi_app.state.settings = types.SimpleNamespace(api_rate_limit=None)
        fastapi_app.state.util = Util(fastapi_app)
        m = ApiRateLimitMiddleware(_noop_app, fastapi_app=fastapi_app)
        assert m._budget() == mw.RATE_LIMIT_DEFAULT

    async def test_budget_read_live_off_settings(self):
        """Swapping app.state.settings (what SIGHUP does) changes the
        limit on the very next request — no restart, no re-wiring."""
        m = _middleware(budget="1")
        assert (await _call(m, _scope())).status == 200
        assert (await _call(m, _scope())).status == 429
        m._fastapi_app.state.settings = make_settings(
            {"KLANGKD_API_RATE_LIMIT": "5"}
        )
        assert (await _call(m, _scope())).status == 200


class TestPerIpKeying:
    async def test_distinct_ips_get_independent_budgets(self):
        m = _middleware(budget="1")
        first = await _call(m, _scope(client=("203.0.113.1", 1)))
        second = await _call(m, _scope(client=("203.0.113.2", 2)))
        assert first.status == second.status == 200
        assert (
            await _call(m, _scope(client=("203.0.113.1", 1)))
        ).status == 429

    async def test_trusted_forwarded_ip_is_the_key(self):
        """A loopback peer (the managed Caddy hop, trusted by default)
        keys on X-Real-IP — two forwarded IPs are two buckets."""
        m = _middleware(budget="1")
        a = await _call(m, _scope(headers={"X-Real-IP": "198.51.100.1"}))
        b = await _call(m, _scope(headers={"X-Real-IP": "198.51.100.2"}))
        assert a.status == b.status == 200
        c = await _call(m, _scope(headers={"X-Real-IP": "198.51.100.1"}))
        assert c.status == 429

    async def test_untrusted_forwarded_ip_is_ignored(self):
        """A direct (non-proxy) peer cannot rotate buckets by spoofing
        X-Real-IP — the key stays the peer address."""
        m = _middleware(budget="1")
        peer = ("203.0.113.9", 9)
        assert (
            await _call(
                m, _scope(client=peer, headers={"X-Real-IP": "198.51.100.1"})
            )
        ).status == 200
        denied = await _call(
            m, _scope(client=peer, headers={"X-Real-IP": "198.51.100.2"})
        )
        assert denied.status == 429
        # The spoofed header never created a second bucket.
        assert set(m._windows) == {"203.0.113.9"}


class TestBoundedState:
    async def test_cap_sheds_oldest_entries(self, monkeypatch):
        """A flood of unique source IPs stops growing the dict at the cap
        (same eviction shape as api/auth.py's cooldown accounting)."""
        monkeypatch.setattr(mw, "RATE_LIMIT_MAX_ENTRIES", 2)
        m = _middleware(budget="100")
        for i in range(4):
            await _call(m, _scope(client=(f"203.0.113.{i}", i)))
        assert len(m._windows) == 2

    async def test_closed_windows_are_pruned_on_record(self):
        """Recording for a new IP drops other IPs' expired windows."""
        m = _middleware(budget="100")
        await _call(m, _scope(client=("203.0.113.1", 1)))
        for key, (start, count, warned) in list(m._windows.items()):
            m._windows[key] = (start - 120.0, count, warned)
        await _call(m, _scope(client=("203.0.113.2", 2)))
        assert set(m._windows) == {"203.0.113.2"}


class TestObservability:
    async def test_denial_logs_once_per_window(self, caplog):
        """The first denial of a window warns; further denials stay
        silent (a scraper must not be able to log-flood)."""
        m = _middleware(budget="1")
        with caplog.at_level("WARNING", logger="klangk.middleware"):
            await _call(m, _scope())  # allowed
            assert "rate limit" not in caplog.text
            await _call(m, _scope())  # first denial
            assert "rate limit exceeded" in caplog.text
            assert "127.0.0.1" in caplog.text
            caplog.clear()
            await _call(m, _scope())  # further denials: silent
            await _call(m, _scope())
            assert caplog.text == ""

    async def test_denial_log_latch_resets_with_window(self, caplog):
        """A fresh window can warn again (once per window)."""
        m = _middleware(budget="1")
        with caplog.at_level("WARNING", logger="klangk.middleware"):
            await _call(m, _scope())
            await _call(m, _scope())  # warn
            for key, (start, count, warned) in list(m._windows.items()):
                m._windows[key] = (start - 61.0, count, warned)
            await _call(m, _scope())  # allowed (fresh window)
            caplog.clear()
            await _call(m, _scope())  # deny again
            assert caplog.text.count("rate limit exceeded") == 1


# ---------------------------------------------------------------------------
# Through a real FastAPI router (the wire shape clients see).
# ---------------------------------------------------------------------------


class TestThroughRouter:
    async def test_429_through_real_router(self):
        app = FastAPI()
        app.state.settings = make_settings({"KLANGKD_API_RATE_LIMIT": "2"})
        app.state.util = Util(app)

        @app.get("/api/v1/version")
        async def version():
            return {"version": "dev"}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        app.add_middleware(ApiRateLimitMiddleware, fastapi_app=app)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(2):
                r = await client.get("/api/v1/version")
                assert r.status_code == 200
            denied = await client.get("/api/v1/version")
            assert denied.status_code == 429
            assert 1 <= int(denied.headers["retry-after"]) <= 60
            assert "detail" in denied.json()
            # Outside /api/ — unlimited regardless of the trip above.
            r = await client.get("/health")
            assert r.status_code == 200

    async def test_429_carries_cors_headers(self):
        """The limiter sits inside LiveCORS, so a cross-origin caller's
        429 must still carry CORS headers (otherwise the browser turns
        it into an opaque error with no Retry-After to read)."""
        from klangk.middleware import LiveCORSMiddleware

        app = FastAPI()
        app.state.settings = make_settings(
            {
                "KLANGKD_API_RATE_LIMIT": "1",
                "KLANGKD_CORS_ORIGINS": "http://cors.example",
            }
        )
        app.state.util = Util(app)

        @app.get("/api/v1/version")
        async def version():
            return {"version": "dev"}

        app.add_middleware(ApiRateLimitMiddleware, fastapi_app=app)
        app.add_middleware(LiveCORSMiddleware, fastapi_app=app)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/api/v1/version")
            denied = await client.get(
                "/api/v1/version",
                headers={"Origin": "http://cors.example"},
            )
        assert denied.status_code == 429
        assert (
            denied.headers["access-control-allow-origin"]
            == "http://cors.example"
        )
        assert "retry-after" in denied.headers


# ---------------------------------------------------------------------------
# build_app wiring: the middleware is in the stack, innermost of the three.
# ---------------------------------------------------------------------------


class TestBuildAppWiring:
    def test_middleware_stack_order(self):
        from klangk.main import build_app

        app = build_app(make_settings({}))
        names = [m.cls.__name__ for m in app.user_middleware]
        # user_middleware[0] is outermost: LiveCORS → InFlight → rate limit.
        assert names.index("LiveCORSMiddleware") < names.index(
            "InFlightMiddleware"
        )
        assert names.index("InFlightMiddleware") < names.index(
            "ApiRateLimitMiddleware"
        )
