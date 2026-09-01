"""E2E tests for the in-process LLM proxy endpoints (#2072).

Starts a fake OpenAI-compatible upstream (a tiny HTTP server that
returns canned responses), then starts a real klangkd with
``KLANGKD_LLM_MODELS`` pointing at it, and verifies that
``/llm-proxy/models`` and ``/llm-proxy/chat/completions`` work
end-to-end through the in-process litellm Router.

Run with: devenv shell -- test-backend-e2e -k test_llm_proxy_e2e
"""

import httpx
import pytest

from klangk.model import free_port
from _e2e_server import start_server, stop_server
from _fake_llm import start_fake_llm, stop_fake_llm


def _ws_headers(client, workspace_id="e2e-llm"):
    """Auth headers with a workspace JWT minted by the server's own
    test-mode endpoint — the token class the egress caddy's forward_auth
    forwards and the backend gate now requires (#2959)."""
    resp = client.get(
        f"/api/v1/test/workspace-token/{workspace_id}", timeout=10
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture(scope="module")
def fake_llm():
    """Start a fake OpenAI-compatible LLM server on a free port."""
    fake = start_fake_llm()
    yield fake
    stop_fake_llm(fake)


@pytest.fixture(scope="module")
def server(fake_llm):
    """Start a real klangkd with LLM models pointing at the fake upstream."""
    model_entry = f"openai/fake-model:{fake_llm['url']}/v1:dummy-key"
    srv = start_server(
        KLANGKD_JWT_SECRET="llm-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        KLANGKD_LLM_MODELS=model_entry,
    )
    yield srv
    stop_server(srv)


class TestLLMProxyE2E:
    def test_unauthenticated_rejected(self, server):
        """#2959: no token → 401 (backend-side, not just the egress gate)."""
        resp = server["client"].get("/llm-proxy/models", timeout=10)
        assert resp.status_code == 401

    def test_user_jwt_rejected(self, server):
        """#2959: a logged-in user's JWT is not a workspace token → 401."""
        login = server["client"].post(
            "/api/v1/auth/login",
            json={"identifier": "test@example.com", "password": "testpass"},
            timeout=10,
        )
        assert login.status_code == 200
        user_jwt = login.json()["access_token"]
        resp = server["client"].get(
            "/llm-proxy/models",
            headers={"Authorization": f"Bearer {user_jwt}"},
            timeout=10,
        )
        assert resp.status_code == 401

    def test_models_endpoint_returns_configured_model(self, server):
        resp = server["client"].get(
            "/llm-proxy/models",
            headers=_ws_headers(server["client"]),
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "fake-model" in ids

    def test_chat_completions_proxies_to_upstream(self, server):
        resp = server["client"].post(
            "/llm-proxy/chat/completions",
            headers=_ws_headers(server["client"]),
            json={
                "model": "fake-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
            timeout=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        assert "Hello from" in content

    def test_chat_completions_returns_usage(self, server):
        resp = server["client"].post(
            "/llm-proxy/chat/completions",
            headers=_ws_headers(server["client"]),
            json={
                "model": "fake-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=30,
        )
        assert resp.status_code == 200
        usage = resp.json()["usage"]
        assert usage["prompt_tokens"] == 5
        assert usage["total_tokens"] == 10

    def test_models_empty_when_no_models_configured(self):
        """Start a server with no LLM models and verify empty response."""
        srv = start_server(
            KLANGKD_JWT_SECRET="llm-e2e-none",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        try:
            resp = srv["client"].get(
                "/llm-proxy/models",
                headers=_ws_headers(srv["client"]),
                timeout=10,
            )
            assert resp.status_code == 200
            assert resp.json()["data"] == []
        finally:
            stop_server(srv)


class TestLLMProxyPassthroughE2E:
    """Passthrough mode: single wildcard entry → discover + forward."""

    @pytest.fixture(scope="class")
    @staticmethod
    def passthrough_stack(fake_llm, tmp_path_factory):
        """Start klangkd with a wildcard model pointing at the fake LLM."""
        cfg_dir = tmp_path_factory.mktemp("llm-pt")
        cfg_file = cfg_dir / "klangkd.yaml"
        cfg_file.write_text(
            "llm-models:\n"
            "  - model_name: '*'\n"
            "    litellm_params:\n"
            f"      api_base: '{fake_llm['url']}/v1'\n"
            "      api_key: dummy-key\n"
        )
        srv = start_server(
            config=str(cfg_file),
            KLANGKD_JWT_SECRET="llm-pt-e2e",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
        )
        yield srv
        stop_server(srv)

    def test_models_discovers_upstream(self, passthrough_stack):
        """GET /llm-proxy/models queries the upstream and returns its models."""
        resp = passthrough_stack["client"].get(
            "/llm-proxy/models",
            headers=_ws_headers(passthrough_stack["client"]),
            timeout=10,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "fake-model" in ids

    def test_chat_completions_forwards_verbatim(self, passthrough_stack):
        """POST /llm-proxy/chat/completions forwards the model name as-is."""
        resp = passthrough_stack["client"].post(
            "/llm-proxy/chat/completions",
            headers=_ws_headers(passthrough_stack["client"]),
            json={
                "model": "fake-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
            timeout=30,
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "Hello from" in content


class TestLLMProxyEgressHopE2E:
    """The full production hop: caddy egress listener → backend gate.

    The other classes hit the backend directly, and the caddy ACL suite
    uses an echo upstream — neither proves that a request through the
    egress listener carries the workspace JWT all the way into the
    backend's ``require_workspace_token`` gate (#2959). A caddy-side
    regression that stopped forwarding ``Authorization`` would 401 every
    in-container LLM call while both suites stayed green; this closes
    that gap.

    Real klangkd in TCP mode renders its own caddy (browser + egress
    listeners). ``KLANGKD_CONTAINER_SUBNETS=127.0.0.1`` makes the test's
    loopback source a container source so the egress ACL allows it (the
    same trick the caddy ACL suite uses with the host IP).
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def stack(fake_llm):
        egress_port = free_port()
        model_entry = f"openai/fake-model:{fake_llm['url']}/v1:dummy-key"
        srv = start_server(
            uds=False,
            KLANGKD_JWT_SECRET="llm-hop-e2e",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            LOGFIRE_TOKEN="",
            KLANGKD_LLM_MODELS=model_entry,
            KLANGKD_EGRESS_PORT=str(egress_port),
            KLANGKD_CONTAINER_SUBNETS="127.0.0.1",
        )
        yield {"client": srv["client"], "egress_port": egress_port}
        stop_server(srv)

    def test_tokenless_rejected_by_forward_auth(self, stack):
        """No token → the egress forward_auth verifier 401s it."""
        resp = httpx.get(
            f"http://127.0.0.1:{stack['egress_port']}/llm-proxy/models",
            timeout=10,
        )
        assert resp.status_code == 401

    def test_workspace_token_survives_the_hop(self, stack):
        """Workspace token → passes the ACL, forward_auth, AND the
        backend gate; the model list comes back (200)."""
        headers = _ws_headers(stack["client"], "ws-hop")
        resp = httpx.get(
            f"http://127.0.0.1:{stack['egress_port']}/llm-proxy/models",
            headers=headers,
            timeout=10,
        )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert "fake-model" in ids
