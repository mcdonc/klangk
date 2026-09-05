"""E2E: per-client-IP API rate limiting (#3157).

Two servers, each exercising one production path of
``ApiRateLimitMiddleware``:

- **TCP via the managed proxy** (``uds=False``): browser-shaped clients
  hit Caddy on ``KLANGKD_PORT``; Caddy forwards to klangkd's UDS with
  ``X-Real-IP``, and the limit keys on that forwarded IP. Asserts the
  429 + ``Retry-After`` wire shape and that ``/health`` (outside
  ``/api/``) stays unlimited.
- **UDS-direct with explicit X-Real-IP** (the trusted-proxy hop shape):
  two different forwarded IPs get independent budgets.

Run with: devenv shell -- test-backend-e2e test_api_rate_limit_e2e.py
"""

import pytest

from _e2e_server import start_server, stop_server


@pytest.fixture(scope="module")
def proxy_server():
    """A browser-path server: Caddy on a free TCP port, budget 3."""
    server = start_server(
        uds=False,
        KLANGKD_JWT_SECRET="rate-limit-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_API_RATE_LIMIT="3",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def uds_server():
    """A UDS-direct server (trusted same-uid peer), budget 2."""
    server = start_server(
        KLANGKD_JWT_SECRET="rate-limit-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_API_RATE_LIMIT="2",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


class TestThroughProxy:
    def test_over_budget_gets_429_with_retry_after(self, proxy_server):
        client = proxy_server["client"]
        # /api/v1/version needs no auth: exactly the pre-auth surface a
        # scraper hits.
        for _ in range(3):
            r = client.get("/api/v1/version", timeout=10)
            assert r.status_code == 200, r.text
        denied = client.get("/api/v1/version", timeout=10)
        assert denied.status_code == 429
        retry_after = int(denied.headers["retry-after"])
        assert 1 <= retry_after <= 60
        assert "detail" in denied.json()

    def test_health_stays_unlimited_after_trip(self, proxy_server):
        client = proxy_server["client"]
        # The module-scoped server is already over budget from the test
        # above; /health (root router, outside /api/) must not be limited.
        for _ in range(5):
            r = client.get("/health", timeout=10)
            assert r.status_code == 200


class TestPerIpBuckets:
    def test_forwarded_ips_get_independent_budgets(self, uds_server):
        client = uds_server["client"]
        # Same-uid UDS peer == the trusted-proxy hop Caddy uses; the
        # explicit X-Real-IP is the client identity (loopback TCP peer
        # as seen by the proxy).
        for ip in ("198.51.100.1", "198.51.100.2"):
            headers = {"X-Real-IP": ip}
            for _ in range(2):
                r = client.get("/api/v1/version", headers=headers, timeout=10)
                assert r.status_code == 200, (ip, r.text)
            denied = client.get("/api/v1/version", headers=headers, timeout=10)
            assert denied.status_code == 429, ip
