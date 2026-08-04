"""E2E tests for the in-process LLM proxy endpoints (#2072).

Starts a fake OpenAI-compatible upstream (a tiny HTTP server that
returns canned responses), then starts a real klangkd with
``KLANGKD_LLM_MODELS`` pointing at it, and verifies that
``/llm-proxy/models`` and ``/llm-proxy/chat/completions`` work
end-to-end through the in-process litellm Router.

Run with: devenv shell -- test-backend-e2e -k test_llm_proxy_e2e
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest

from klangk.model import free_port
from _e2e_server import start_server, stop_server


class _FakeLLMHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible handler."""

    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "fake-model",
                            "object": "model",
                            "owned_by": "test",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            request_body = (
                json.loads(self.rfile.read(length)) if length else {}
            )
            model = request_body.get("model", "unknown")
            body = json.dumps(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": f"Hello from {model}!",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 5,
                        "total_tokens": 10,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # suppress request logs


@pytest.fixture(scope="module")
def fake_llm():
    """Start a fake OpenAI-compatible LLM server on a free port."""
    port = free_port()
    httpd = HTTPServer(("127.0.0.1", port), _FakeLLMHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": port, "url": f"http://127.0.0.1:{port}"}
    httpd.shutdown()


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
    def test_models_endpoint_returns_configured_model(self, server):
        resp = server["client"].get("/llm-proxy/models", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "fake-model" in ids

    def test_chat_completions_proxies_to_upstream(self, server):
        resp = server["client"].post(
            "/llm-proxy/chat/completions",
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
            resp = srv["client"].get("/llm-proxy/models", timeout=10)
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
        resp = passthrough_stack["client"].get("/llm-proxy/models", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "fake-model" in ids

    def test_chat_completions_forwards_verbatim(self, passthrough_stack):
        """POST /llm-proxy/chat/completions forwards the model name as-is."""
        resp = passthrough_stack["client"].post(
            "/llm-proxy/chat/completions",
            json={
                "model": "fake-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
            timeout=30,
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "Hello from" in content
