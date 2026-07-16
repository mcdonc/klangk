"""E2E test server launcher (NOT for production).

Builds the app explicitly (``build_app(KlangkSettings(os.environ))``) and passes
the object to uvicorn — no ``module:app`` string import. The composition root
is sealed (#1454); this is the test-only TCP entry point the E2E suites use
instead of bare ``uvicorn klangk_backend.main:app``.

Reads config from env vars (the E2E harness sets them before spawning this
process). Accepts the uvicorn bind options the suites need as CLI args.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from klangk_backend.main import build_app
from klangk_backend.settings import KlangkSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E test server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ws-max-size", type=int, default=16777216)
    parser.add_argument("--ws-ping-interval", type=int, default=20)
    parser.add_argument("--ws-ping-timeout", type=int, default=20)
    parser.add_argument("--config", default=None, help="YAML config file")
    args = parser.parse_args()

    app = build_app(KlangkSettings(os.environ, config_file=args.config))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ws_max_size=args.ws_max_size,
        ws_ping_interval=args.ws_ping_interval,
        ws_ping_timeout=args.ws_ping_timeout,
    )


if __name__ == "__main__":
    main()
