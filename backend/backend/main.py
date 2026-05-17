"""Bark backend: FastAPI app with HTTP + WebSocket endpoints."""

import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import container_manager, user_store
from .api import router
from .ws_handler import handle_websocket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _seed_default_user() -> None:
    """Create default user if it doesn't exist."""
    import bcrypt

    username = os.environ.get("BARK_DEFAULT_USER", "admin")
    password = os.environ.get("BARK_DEFAULT_PASSWORD", "admin")
    existing = await user_store.get_user_by_username(username)
    if existing is None:
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await user_store.create_user(username, password_hash)
        logger.info("Created default user '%s'", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await user_store.init_db()
    await _seed_default_user()
    container_manager.start_cleanup_loop()
    logger.info("Bark backend started")
    yield
    await container_manager.shutdown()
    logger.info("Bark backend stopped")


app = FastAPI(title="Bark", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


# --- Hosted app proxy ---

_proxy_client: httpx.AsyncClient | None = None


async def _get_proxy_client() -> httpx.AsyncClient:  # pragma: no cover
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(follow_redirects=True)
    return _proxy_client


@app.api_route(
    "/hosted/{workspace_id}/{port:int}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_hosted_app(
    workspace_id: str,
    port: int,
    path: str,
    request: Request,
):
    """Proxy requests to apps running in workspace containers (no auth required)."""
    upstream_url = f"http://127.0.0.1:{port}/{path}"
    logger.info("Proxying %s %s -> %s", request.method, request.url.path, upstream_url)
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    client = await _get_proxy_client()
    body = await request.body()
    try:
        req = client.build_request(
            method=request.method,
            url=upstream_url,
            headers={
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "authorization", "connection")
            },
            content=body if body else None,
        )
        resp = await client.send(req, stream=True)
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="App is not running on this port")
    except httpx.RemoteProtocolError:
        raise HTTPException(status_code=502, detail="App did not send a valid response")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="App did not respond in time")

    excluded = {"transfer-encoding", "connection", "keep-alive"}
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}

    async def stream_body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        content=stream_body(),
        status_code=resp.status_code,
        headers=headers,
    )


# --- WebSocket ---


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):  # pragma: no cover
    await handle_websocket(ws)


# --- Static files (Flutter Web) ---
# Must be last so API routes take priority


def setup_static_files(app: FastAPI, frontend_dir: Path) -> None:
    """Mount Flutter Web static files and add no-cache middleware."""
    static_app = StaticFiles(directory=str(frontend_dir), html=True)

    @app.middleware("http")
    async def add_no_cache_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith((".html", ".js")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.mount("/", static_app, name="frontend")


_frontend_dir = Path(__file__).parent.parent.parent / "frontend" / "build" / "web"
if _frontend_dir.exists():  # pragma: no cover
    setup_static_files(app, _frontend_dir)
