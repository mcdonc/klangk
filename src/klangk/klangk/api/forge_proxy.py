"""Forge proxy endpoints: allow-listed git and API access with host-held tokens.

Mounted at ``/forge-proxy/`` (outside ``/api/v1/``) and reached by
containers through the egress caddy, like ``/llm-proxy/``. Every route
requires a **workspace JWT**; the forge token for that workspace is
injected here and never returned. See :mod:`klangk.forge_proxy`.
"""

import logging
import ssl

import certifi
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import forge_proxy as fp
from .common import require_workspace_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/forge-proxy",
    tags=["forge-proxy"],
    dependencies=[Depends(require_workspace_token)],
)

_TIMEOUT = httpx.Timeout(30.0, read=300.0)


def ssl_context(settings) -> ssl.SSLContext:
    """Default trust plus the optional forge CA (host-side only)."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    if settings.forge_proxy_ca_cert:
        ctx.load_verify_locations(cafile=settings.forge_proxy_ca_cert)
    return ctx


def http_client_for(app) -> httpx.AsyncClient:
    """An HTTP client for forge calls: no redirects, forge CA trust."""
    return httpx.AsyncClient(
        timeout=_TIMEOUT,
        verify=ssl_context(app.state.settings),
        follow_redirects=False,
    )


def http_client(request: Request) -> httpx.AsyncClient:
    return http_client_for(request.app)


async def _read_capped(request: Request, limit: int) -> bytes:
    """The request body, refused once it exceeds ``limit`` bytes."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise fp.ForgeProxyError(413, "request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _capped_stream(request: Request, limit: int):
    """Stream a git pack upstream without buffering; stop past ``limit``."""
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise fp.ForgeProxyError(413, "request body too large")
        yield chunk


def _error(exc: fp.ForgeProxyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.post("/{host}/_klangk/oauth/start")
async def oauth_start(
    host: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """Begin a host-side authorization; returns only an opaque txn_id."""
    try:
        txn_id = request.app.state.forge_proxy.start(workspace_id, host)
    except fp.ForgeProxyError as exc:
        return _error(exc)
    return {"txn_id": txn_id}


@router.get("/{host}/_klangk/status")
async def oauth_status(
    host: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """Whether this workspace has a forge authorization (no secrets)."""
    try:
        return request.app.state.forge_proxy.status(workspace_id, host)
    except fp.ForgeProxyError as exc:
        return _error(exc)


@router.get("/{host}/{path:path}")
async def forward_get(
    host: str,
    path: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """GET through the forge proxy (allow-listed reads, git refs)."""
    return await forward(host, path, request, workspace_id)


@router.post("/{host}/{path:path}")
async def forward_post(
    host: str,
    path: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """POST through the forge proxy (git packs, issue and comment create)."""
    return await forward(host, path, request, workspace_id)


@router.put("/{host}/{path:path}")
async def forward_put(
    host: str,
    path: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """PUT through the forge proxy (read-only collaborator add)."""
    return await forward(host, path, request, workspace_id)


@router.delete("/{host}/{path:path}")
async def forward_delete(
    host: str,
    path: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """DELETE through the forge proxy (passthrough mode only)."""
    return await forward(host, path, request, workspace_id)


@router.patch("/{host}/{path:path}")
async def forward_patch(
    host: str,
    path: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_token),
):
    """PATCH through the forge proxy (issue state only)."""
    return await forward(host, path, request, workspace_id)


async def forward(host: str, path: str, request: Request, workspace_id: str):
    """Forward one allow-listed request with the workspace's forge token."""
    proxy = request.app.state.forge_proxy
    try:
        route = _route(proxy, host, request)
        body = await _body(route, request)
        client = http_client(request)
        token = await _token(proxy, workspace_id, host, client)
        await _check_actor(proxy, route, body, token, client)
    except fp.ForgeProxyError as exc:
        return _error(exc)
    upstream = client.build_request(
        request.method,
        _upstream_url(proxy, host, route),
        headers=_request_headers(request, token),
        content=body,
    )
    resp = await _send(client, upstream, request.method, route)
    if isinstance(resp, JSONResponse):
        return resp
    logger.info(
        "forge-proxy: workspace=%s host=%s %s %s %s/%s -> %s",
        workspace_id,
        host,
        request.method,
        route.kind,
        route.owner,
        route.repo,
        resp.status_code,
    )
    refused = await _refused(resp, client, proxy, workspace_id, host, token)
    return (
        refused
        or await _sanitized(resp, client, route)
        or _stream(resp, client)
    )


async def _sanitized(resp, client, route: fp.Route):
    """A local error in place of a forge error body on an API route.

    The forge's own error text never reaches the container, so a body that
    echoes request credentials cannot leak them. Git routes keep the forge's
    body: git clients print it as the failure reason.
    """
    if resp.status_code < 400 or route.kind.startswith("git-"):
        return None
    await resp.aclose()
    await client.aclose()
    return JSONResponse(
        status_code=resp.status_code,
        content={"detail": f"forge refused the request ({resp.status_code})"},
    )


def _route(proxy, host: str, request: Request) -> fp.Route:
    """The allow-listed route for the raw (undecoded) request path."""
    proxy.provider(host)
    raw = request.scope.get("raw_path", b"").decode("latin-1")
    prefix = f"/forge-proxy/{host}"
    if not raw.startswith(prefix + "/"):
        raise fp.ForgeProxyError(400, "path not allowed")
    route = fp.match_route(
        request.method,
        raw[len(prefix) :],
        request.scope.get("query_string", b"").decode("latin-1"),
        passthrough=proxy.passthrough(),
    )
    proxy.check_route(route)
    return route


async def _body(route: fp.Route, request: Request):
    """A capped stream for git packs; a validated JSON body otherwise."""
    if route.kind == "git-receive":
        return await _guarded_push(request)
    if route.kind.startswith("git-"):
        return _capped_stream(request, fp.MAX_GIT_BODY)
    if route.kind.startswith("api-pass-"):
        return await _read_capped(request, fp.MAX_PASS_BODY)
    return fp.validate_json_body(
        route.kind, await _read_capped(request, fp.MAX_JSON_BODY)
    )


async def _guarded_push(request: Request):
    """Read a push's ref updates, refuse ref deletion, then stream the rest."""
    encoding = (request.headers.get("content-encoding") or "identity").lower()
    if encoding != "identity":
        raise fp.ForgeProxyError(415, "compressed push requests not allowed")
    chunks = request.stream().__aiter__()
    head = await _push_head(chunks)
    return _replay(head, chunks, fp.MAX_GIT_BODY)


async def _push_head(chunks) -> bytes:
    """Bytes up to and including the flush-pkt ending the ref updates."""
    buf = b""
    end = None
    while end is None:
        chunk = await anext(chunks, None)
        if chunk is None:
            raise fp.ForgeProxyError(400, "malformed push request")
        buf += chunk
        if len(buf) > fp.MAX_PUSH_COMMANDS:
            raise fp.ForgeProxyError(413, "push commands too large")
        end = fp.push_commands_end(buf)
    fp.check_push_commands(buf, end)
    return buf


async def _replay(head: bytes, chunks, limit: int):
    """Stream the already-read head, then the rest of the body, capped."""
    yield head
    size = len(head)
    async for chunk in chunks:
        size += len(chunk)
        if size > limit:
            raise fp.ForgeProxyError(413, "request body too large")
        yield chunk


async def _token(proxy, workspace_id: str, host: str, client):
    try:
        return await proxy.access_token(workspace_id, host, client)
    except BaseException:
        await client.aclose()
        raise


async def _check_actor(proxy, route: fp.Route, body, token, client) -> None:
    """Account-dependent policy; closes the client when it refuses."""
    try:
        proxy.check_actor(route, body, token.login)
    except fp.ForgeProxyError:
        await client.aclose()
        raise


def _upstream_url(proxy, host: str, route: fp.Route) -> str:
    url = f"{proxy.origin(host)}{route.path}"
    return f"{url}?{route.query}" if route.query else url


def _request_headers(request: Request, token) -> dict:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() in fp.FORWARD_REQUEST_HEADERS
    }
    headers["Authorization"] = f"Bearer {token.access_token}"
    return headers


async def _send(client, upstream, method: str, route: fp.Route):
    """The upstream response, or an error response (client closed)."""
    try:
        return await client.send(upstream, stream=True)
    except fp.ForgeProxyError as exc:
        await client.aclose()
        return _error(exc)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning(
            "forge-proxy: %s %s failed: %s",
            method,
            route.kind,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=502, content={"detail": "forge unreachable"}
        )


async def _refused(resp, client, proxy, workspace_id: str, host: str, token):
    """An error response for an upstream redirect or 401, else None."""
    if not (300 <= resp.status_code < 400 or resp.status_code == 401):
        return None
    await resp.aclose()
    await client.aclose()
    if resp.status_code == 401:
        proxy.forget_token(workspace_id, host, token)
        return _error(fp.ForgeProxyError(401, fp._needs_auth(host)))
    return JSONResponse(
        status_code=502, content={"detail": "forge redirected the request"}
    )


def _stream(resp, client) -> StreamingResponse:
    out_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() in fp.FORWARD_RESPONSE_HEADERS
    }

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            # Headers are already sent: end the body early (git and JSON
            # clients see a truncated response) instead of a server error.
            logger.warning(
                "forge-proxy: upstream body failed: %s", type(exc).__name__
            )
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(), status_code=resp.status_code, headers=out_headers
    )
