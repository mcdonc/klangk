"""A fake OpenAI-compatible upstream for LLM-proxy E2E tests.

Shared by ``test_llm_proxy_e2e.py`` (backend-level) and
``test_llm_proxy_in_workspace_e2e.py`` (full container path). Serves
``/v1/models`` and ``/v1/chat/completions`` with canned responses;
every completion echoes the model name in its content so callers can
assert the proxy forwarded the request verbatim.
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from klangk.model import free_port


class FakeLLMHandler(BaseHTTPRequestHandler):
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


def start_fake_llm() -> dict:
    """Start the fake upstream on a free loopback port.

    Returns ``{"port", "url", "httpd"}``; stop with :func:`stop_fake_llm`.
    """
    port = free_port()
    httpd = HTTPServer(("127.0.0.1", port), FakeLLMHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return {"port": port, "url": f"http://127.0.0.1:{port}", "httpd": httpd}


def stop_fake_llm(fake: dict) -> None:
    fake["httpd"].shutdown()
