"""Forge proxy: host-held forge tokens, allow-listed git/API proxy, bridge policy."""

import json
import time
import types

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk import forge_proxy as fp
from klangk.api import forge_proxy as routes
from klangk.auth import Auth
from _helpers import make_settings

HOST = "forge.test"
PROVIDER = {
    "host": HOST,
    "flow": "authorization_code_pkce",
    "client_id": "cid",
    "authorize_url": f"https://{HOST}/login/oauth/authorize",
    "token_url": f"https://{HOST}/login/oauth/access_token",
    "redirect_uri": "http://localhost:8997/",
}


@pytest.fixture
def providers(monkeypatch):
    def set_(entries):
        monkeypatch.setenv(fp.PROVIDERS_KEY, json.dumps(entries))

    set_([PROVIDER])
    return set_


def _app(env=None):
    settings = make_settings(
        env={
            "KLANGKD_FORGE_PROXY_HOSTS": HOST,
            "KLANGKD_FORGE_PROXY_ALLOWED_REPOS": "*",
            "KLANGKD_FORGE_PROXY_FEATURES": "read,git-read,git-push,issues,issue-edit,site-create,collaborators",
            **(env or {}),
        }
    )
    state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.auth = Auth(state)
    app.state.forge_proxy = fp.ForgeProxy(app)
    app.include_router(routes.router)
    return app


def _ws(app, workspace_id="ws1"):
    token = app.state.auth.create_workspace_token(workspace_id)
    return {"Authorization": f"Bearer {token}"}


def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )


class Upstream:
    """A fake forge: records requests, answers by (method, path)."""

    def __init__(self):
        self.requests = []
        self.answers = {}

    def on(self, method, path, status=200, body=None, headers=None):
        self.answers[(method, path)] = (status, body, headers or {})

    def handler(self, request):
        self.requests.append(request)
        status, body, headers = self.answers.get(
            (request.method, request.url.path), (404, {"message": "nope"}, {})
        )
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **headers}
        else:
            data = body or b""
        # A real byte stream, as a server sends: the proxy streams it raw.
        return httpx.Response(
            status, headers=headers, stream=httpx.ByteStream(data)
        )

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def upstream(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(routes, "http_client_for", lambda app: up.client())
    return up


def _authorized(app, workspace_id="ws1", expires_at=0.0, refresh="r1"):
    app.state.forge_proxy._tokens[(workspace_id, HOST)] = fp.ForgeToken(
        access_token="forge-at",
        refresh_token=refresh,
        expires_at=expires_at,
        login="me",
    )


# --- pure policy -------------------------------------------------------------


class TestPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "/a/%2e%2e/b",
            "/a\\b",
            "//a",
            "/a/./b",
            "/a/../b",
            "/a/\x01",
            "x/y",
            "/a/\x7f",
        ],
    )
    def test_ambiguous_paths_rejected(self, path):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.validate_raw_path(path)
        assert e.value.status == 400

    def test_clean_path_ok(self):
        assert (
            fp.validate_raw_path("/o/r.git/info/refs") == "/o/r.git/info/refs"
        )


class TestRoutes:
    def test_git_refs(self):
        r = fp.match_route(
            "GET", "/o/r.git/info/refs", "service=git-upload-pack"
        )
        assert (r.kind, r.owner, r.repo, r.query) == (
            "git-refs",
            "o",
            "r",
            "service=git-upload-pack",
        )

    def test_git_refs_bad_service(self):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.match_route("GET", "/o/r.git/info/refs", "service=other")
        assert e.value.status == 400

    def test_receive_pack(self):
        assert (
            fp.match_route("POST", "/o/r.git/git-receive-pack", "").kind
            == "git-receive"
        )

    def test_issue_routes(self):
        assert (
            fp.match_route("POST", "/api/v1/repos/o/r/issues", "").kind
            == "issue-create"
        )
        assert (
            fp.match_route(
                "POST", "/api/v1/repos/o/r/issues/3/comments", ""
            ).kind
            == "comment-create"
        )
        assert (
            fp.match_route("PATCH", "/api/v1/repos/o/r/issues/3", "").kind
            == "issue-state"
        )
        assert fp.match_route("GET", "/api/v1/user", "").kind == "api-read"

    def test_allowed_query_rebuilt(self):
        r = fp.match_route(
            "GET", "/api/v1/repos/o/r/issues", "state=open&limit=2"
        )
        assert r.query == "state=open&limit=2"

    @pytest.mark.parametrize(
        "query", ["access_token=x", "sudo=admin", "state=a&state=b"]
    )
    def test_bad_query(self, query):
        with pytest.raises(fp.ForgeProxyError):
            fp.match_route("GET", "/api/v1/repos/o/r/issues", query)

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/v1/admin/users"),
            ("DELETE", "/api/v1/repos/o/r"),
            ("GET", "/api/v1/user/applications/oauth2"),
            ("PATCH", "/api/v1/repos/o/r"),
            ("POST", "/api/v1/repos/o/r/issues/0/comments"),
        ],
    )
    def test_not_allowed(self, method, path):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.match_route(method, path, "")
        assert e.value.status == 403

    def test_empty_repo_name(self):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.match_route(
                "GET", "/o/.git/info/refs", "service=git-upload-pack"
            )
        assert e.value.status == 403


class TestBodies:
    def test_issue_create(self):
        out = fp.validate_json_body(
            "issue-create", b'{"title":"t","body":"b"}'
        )
        assert json.loads(out) == {"title": "t", "body": "b"}

    def test_git_body_untouched(self):
        assert fp.validate_json_body("git-upload", b"\x00pack") == b"\x00pack"

    @pytest.mark.parametrize(
        "kind,body,status",
        [
            ("issue-create", b'{"title":"t","assignees":"x"}', 400),
            ("issue-create", b'{"title":"t","ref":"main"}', 400),
            ("issue-labels", b'{"labels":[0]}', 400),
            ("issue-labels", b'{"labels":[true]}', 400),
            ("issue-labels", b'{"labels":[""]}', 400),
            ("issue-create", b'{"body":"b"}', 400),
            ("issue-create", b"not json", 400),
            ("issue-create", b"[1]", 400),
            ("issue-create", b'{"title":"a","title":"b"}', 400),
            ("issue-create", b'{"title":1}', 400),
            ("issue-state", b'{"state":"locked"}', 400),
            (
                "comment-create",
                b'{"body":"' + b"x" * fp.MAX_JSON_BODY + b'"}',
                413,
            ),
        ],
    )
    def test_rejected(self, kind, body, status):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.validate_json_body(kind, body)
        assert e.value.status == status


class TestProviderConfig:
    def test_not_proxied(self, providers):
        with pytest.raises(fp.ForgeProxyError) as e:
            _app().state.forge_proxy.provider("other.test")
        assert e.value.status == 404

    @pytest.mark.parametrize(
        "entries",
        [
            "not json",
            {"host": HOST},
            ["x", {"host": "else.test"}],
            [dict(PROVIDER, flow="device_code")],
            [dict(PROVIDER, token_url="http://forge.test/t")],
            [dict(PROVIDER, redirect_uri="http://evil/")],
            [dict(PROVIDER, client_id="")],
        ],
    )
    def test_unusable_entries(self, monkeypatch, entries):
        monkeypatch.setenv(
            fp.PROVIDERS_KEY,
            entries if isinstance(entries, str) else json.dumps(entries),
        )
        with pytest.raises(fp.ForgeProxyError):
            _app().state.forge_proxy.provider(HOST)

    def test_found_with_port_and_case(self, providers):
        assert (
            _app().state.forge_proxy.provider("FORGE.test:443")["client_id"]
            == "cid"
        )

    def test_empty_hosts(self, providers):
        app = _app({"KLANGKD_FORGE_PROXY_HOSTS": ""})
        assert app.state.forge_proxy.proxied_hosts() == set()

    def test_host_of_url(self):
        assert fp._host_of_url("https://user@FORGE.TEST.:443/x") == HOST
        assert fp._host_of_url("https://other.test/") == "other.test"
        assert fp._host_of_url("http://[::1") == ""

    def test_providers_unset_or_not_a_list(self, monkeypatch):
        monkeypatch.delenv(fp.PROVIDERS_KEY, raising=False)
        f = _app().state.forge_proxy
        assert f._provider_entries() == []
        monkeypatch.setenv(fp.PROVIDERS_KEY, "{}")
        assert f._provider_entries() == []

    def test_no_proxied_hosts_passes_everything(self, providers):
        f = _app({"KLANGKD_FORGE_PROXY_HOSTS": ""}).state.forge_proxy
        assert (
            f.bridge_policy(
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "x",
                    "verification_uri": "https://%66orge.test/",
                }
            )
            == "pass"
        )


class TestTransactions:
    def test_start_claim_once(self, providers):
        f = _app().state.forge_proxy
        txn_id = f.start("ws1", HOST)
        with pytest.raises(fp.ForgeProxyError):
            f.claim("ws2", txn_id)
        txn = f.claim("ws1", txn_id)
        url = f.authorize_url(txn)
        assert "code_challenge_method=S256" in url and txn.verifier not in url
        with pytest.raises(fp.ForgeProxyError):
            f.claim("ws1", txn_id)

    def test_expired_txn_pruned(self, providers, monkeypatch):
        f = _app().state.forge_proxy
        txn_id = f.start("ws1", HOST)
        later = time.time() + fp.TXN_TTL_SECONDS + 1
        monkeypatch.setattr(fp.time, "time", lambda: later)
        with pytest.raises(fp.ForgeProxyError):
            f.claim("ws1", txn_id)

    def test_scope_and_query_in_authorize_url(self, providers):
        providers(
            [
                dict(
                    PROVIDER,
                    scope="read:user",
                    authorize_url=PROVIDER["authorize_url"] + "?x=1",
                )
            ]
        )
        f = _app().state.forge_proxy
        url = f.authorize_url(f.claim("ws1", f.start("ws1", HOST)))
        assert "scope=read%3Auser" in url and "?x=1&" in url


@pytest.mark.asyncio
class TestCompleteAndRefresh:
    async def test_complete_stores_token_host_side(self, providers):
        up = Upstream()
        up.on(
            "POST",
            "/login/oauth/access_token",
            body={
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
            },
        )
        up.on("GET", "/api/v1/user", body={"login": "me"})
        f = _app().state.forge_proxy
        txn = f.claim("ws1", f.start("ws1", HOST))
        async with up.client() as http:
            assert await f.complete(txn, "code", txn.state, http) == "me"
        sent = up.requests[0].content.decode()
        assert "code_verifier=" + txn.verifier in sent
        assert f.status("ws1", HOST) == {"connected": True, "login": "me"}
        assert txn.txn_id not in f._txns

    async def test_state_mismatch(self, providers):
        f = _app().state.forge_proxy
        txn = f.claim("ws1", f.start("ws1", HOST))
        with pytest.raises(fp.ForgeProxyError):
            await f.complete(txn, "code", "wrong", None)
        assert txn.txn_id not in f._txns

    @pytest.mark.parametrize(
        "token_answer,user_answer",
        [
            ((400, {"error": "bad"}), None),
            ((200, b"html"), None),
            ((200, {"no": "token"}), None),
            ((200, {"access_token": "at"}), (403, {})),
            ((200, {"access_token": "at"}), (200, b"html")),
            ((200, {"access_token": "at"}), (200, {"login": ""})),
        ],
    )
    async def test_exchange_failures(
        self, providers, token_answer, user_answer
    ):
        up = Upstream()
        up.on("POST", "/login/oauth/access_token", *token_answer)
        if user_answer:
            up.on("GET", "/api/v1/user", *user_answer)
        f = _app().state.forge_proxy
        txn = f.claim("ws1", f.start("ws1", HOST))
        async with up.client() as http:
            with pytest.raises(fp.ForgeProxyError) as e:
                await f.complete(txn, "code", txn.state, http)
        assert e.value.status == 502
        assert f.status("ws1", HOST)["connected"] is False

    async def test_access_token_missing(self, providers):
        with pytest.raises(fp.ForgeProxyError) as e:
            await _app().state.forge_proxy.access_token("ws1", HOST, None)
        assert e.value.status == 401

    async def test_refresh_rotates(self, providers):
        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        up = Upstream()
        up.on(
            "POST",
            "/login/oauth/access_token",
            body={
                "access_token": "at2",
                "refresh_token": "r2",
                "expires_in": 60,
            },
        )
        async with up.client() as http:
            assert (
                await app.state.forge_proxy.access_token("ws1", HOST, http)
            ).access_token == "at2"
        tok = app.state.forge_proxy._tokens[("ws1", HOST)]
        assert tok.refresh_token == "r2" and tok.login == "me"

    async def test_refresh_keeps_old_refresh_token(self, providers):
        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        up = Upstream()
        up.on(
            "POST", "/login/oauth/access_token", body={"access_token": "at2"}
        )
        async with up.client() as http:
            await app.state.forge_proxy.access_token("ws1", HOST, http)
        assert (
            app.state.forge_proxy._tokens[("ws1", HOST)].refresh_token == "r1"
        )

    async def test_refresh_rejected_forgets(self, providers):
        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        up = Upstream()
        up.on(
            "POST",
            "/login/oauth/access_token",
            400,
            {"error": "invalid_grant"},
        )
        async with up.client() as http:
            with pytest.raises(fp.ForgeProxyError) as e:
                await app.state.forge_proxy.access_token("ws1", HOST, http)
        assert e.value.status == 401
        assert ("ws1", HOST) not in app.state.forge_proxy._tokens

    async def test_no_refresh_token_forgets(self, providers):
        app = _app()
        _authorized(app, expires_at=time.time() + 10, refresh="")
        with pytest.raises(fp.ForgeProxyError):
            await app.state.forge_proxy.access_token("ws1", HOST, None)
        assert ("ws1", HOST) not in app.state.forge_proxy._tokens


class TestBridgePolicy:
    def test_policy(self, providers):
        f = _app().state.forge_proxy
        auth_url = f"https://{HOST}/login/oauth/authorize?client_id=x"
        cases = {
            "pass": [
                {"action": "fetch"},
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "github.com",
                },
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "github.com",
                    "verification_uri": "https://github.com/login/device",
                },
            ],
            "txn": [
                {
                    "action": "git_credential",
                    "operation": "auth_flow_start",
                    "txn_id": "t",
                }
            ],
            "refuse": [
                {"action": "git_credential", "operation": "get", "host": HOST},
                {
                    "action": "git_credential",
                    "operation": "peek",
                    "host": "FORGE.TEST",
                },
                {
                    "action": "git_credential",
                    "operation": "auth_flow_start",
                    "host": "github.com",
                    "authorize_url": auth_url,
                },
                # Percent-encoded host: browsers decode it, urlsplit does not.
                {
                    "action": "git_credential",
                    "operation": "auth_flow_start",
                    "host": "other.test",
                    "authorize_url": "https://%66orge.test/login/oauth/authorize",
                },
                {
                    "action": "git_credential",
                    "operation": "auth_flow_start",
                    "host": "github.com",
                    "authorize_url": "https://github.com/x",
                },
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "x",
                    "verification_uri": f"https://{HOST}/device",
                },
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "x",
                    "verification_uri": "https://%66orge.test/verify",
                },
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "x",
                    "verification_uri_complete": "https://a@github.com/x",
                },
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "x",
                    "verification_uri": "https://fürge.test/x",
                },
                {
                    "action": "git_credential",
                    "operation": "get",
                    "host": "x",
                    "verification_uri": "https://[::1/x",
                },
            ],
        }
        for expected, payloads in cases.items():
            for payload in payloads:
                assert f.bridge_policy(payload) == expected, payload


@pytest.mark.asyncio
class TestRelayAuthorization:
    async def _relay(self, f, answer, txn_id=None):
        txn_id = txn_id or f.start("ws1", HOST)
        sent = []

        async def dispatch(request, timeout):
            sent.append(request)
            return answer

        up = Upstream()
        up.on("POST", "/login/oauth/access_token", body={"access_token": "at"})
        up.on("GET", "/api/v1/user", body={"login": "me"})
        async with up.client() as http:
            out = await f.relay_authorization(
                "ws1",
                {"action": "git_credential", "txn_id": txn_id},
                dispatch,
                http,
            )
        return out, sent

    async def test_code_consumed_not_relayed(self, providers):
        f = _app().state.forge_proxy
        txn_id = f.start("ws1", HOST)
        state = f._txns[txn_id].state
        out, sent = await self._relay(
            f,
            {
                "status": "ok",
                "result": json.dumps({"code": "the-code", "state": state}),
            },
            txn_id,
        )
        assert "the-code" not in json.dumps(out)
        assert json.loads(out["result"]) == {
            "status": "connected",
            "host": HOST,
            "login": "me",
        }
        assert sent[0]["authorize_url"].startswith(PROVIDER["authorize_url"])

    @pytest.mark.parametrize(
        "answer",
        [
            {"error": "cancelled"},
            {"status": "ok", "result": json.dumps({"error": "cancelled"})},
            "not a dict",
            {"status": "ok", "result": "{not json"},
            {"status": "ok", "result": json.dumps([1])},
        ],
    )
    async def test_browser_errors(self, providers, answer):
        f = _app().state.forge_proxy
        out, _ = await self._relay(f, answer)
        assert "error" in json.loads(out["result"])
        assert f._txns == {}

    async def test_provider_gone_clears_txn(self, providers, monkeypatch):
        f = _app().state.forge_proxy
        txn_id = f.start("ws1", HOST)
        monkeypatch.setenv(fp.PROVIDERS_KEY, "[]")
        with pytest.raises(fp.ForgeProxyError):
            await self._relay(f, {}, txn_id)
        assert f._txns == {}


@pytest.mark.asyncio
class TestRouter:
    async def test_requires_workspace_token(self, providers):
        async with _client(_app()) as c:
            assert (
                await c.get(f"/forge-proxy/{HOST}/api/v1/user")
            ).status_code == 401

    async def test_start_and_status(self, providers):
        app = _app()
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/_klangk/oauth/start", headers=_ws(app)
            )
            assert set(r.json()) == {"txn_id"}
            r = await c.get(
                f"/forge-proxy/{HOST}/_klangk/status", headers=_ws(app)
            )
            assert r.json() == {"connected": False, "login": None}
            r = await c.post(
                "/forge-proxy/other.test/_klangk/oauth/start", headers=_ws(app)
            )
            assert r.status_code == 404
            r = await c.get(
                "/forge-proxy/other.test/_klangk/status", headers=_ws(app)
            )
            assert r.status_code == 404

    async def test_forward_injects_token_and_filters_headers(
        self, providers, upstream
    ):
        app = _app()
        _authorized(app)
        upstream.on(
            "GET",
            "/api/v1/user",
            body={"login": "me"},
            headers={
                "Set-Cookie": "s=1",
                "X-Total-Count": "1",
                "Location": "https://x",
            },
        )
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user",
                headers={
                    **_ws(app),
                    "Cookie": "c=1",
                    "Sudo": "admin",
                    "Accept": "application/json",
                },
            )
        assert r.status_code == 200 and r.json() == {"login": "me"}
        sent = upstream.requests[0]
        assert sent.headers["authorization"] == "Bearer forge-at"
        assert "cookie" not in sent.headers and "sudo" not in sent.headers
        assert "set-cookie" not in r.headers and "location" not in r.headers
        assert r.headers["x-total-count"] == "1"

    async def test_forward_with_query_and_body(self, providers, upstream):
        app = _app()
        _authorized(app)
        upstream.on("POST", "/api/v1/repos/o/r/issues", 201, {"number": 7})
        upstream.on("GET", "/api/v1/repos/o/r/issues", body=[])
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues",
                headers=_ws(app),
                json={"title": "t", "body": "b"},
            )
            assert r.status_code == 201
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues?state=open",
                headers=_ws(app),
            )
            assert r.status_code == 200
        assert upstream.requests[1].url.query == b"state=open"

    @pytest.mark.parametrize(
        "method,path,status",
        [
            ("GET", "/api/v1/admin/users", 403),
            ("GET", "/api/v1/repos/o/r/issues?access_token=x", 400),
            ("POST", "/api/v1/repos/o/r/issues", 400),
        ],
    )
    async def test_refusals(self, providers, upstream, method, path, status):
        app = _app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.request(
                method,
                f"/forge-proxy/{HOST}{path}",
                headers=_ws(app),
                content=b"{}",
            )
        assert r.status_code == status
        assert upstream.requests == []

    async def test_unproxied_host(self, providers, upstream):
        app = _app()
        async with _client(app) as c:
            r = await c.get(
                "/forge-proxy/other.test/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 404

    async def test_needs_auth(self, providers, upstream):
        app = _app()
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 401 and "forge-auth" in r.json()["detail"]

    async def test_upstream_401_forgets(self, providers, upstream):
        app = _app()
        _authorized(app)
        upstream.on("GET", "/api/v1/user", 401, {})
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 401
        assert ("ws1", HOST) not in app.state.forge_proxy._tokens

    async def test_upstream_redirect_not_followed(self, providers, upstream):
        app = _app()
        _authorized(app)
        upstream.on(
            "GET", "/api/v1/user", 302, b"", {"Location": "https://evil/"}
        )
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 502 and "location" not in r.headers

    async def test_upstream_unreachable(self, providers, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(
            routes,
            "http_client_for",
            lambda app: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        )
        app = _app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 502

    async def test_upstream_body_failure_truncates(
        self, providers, monkeypatch
    ):
        class Broken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"part"
                raise httpx.ReadError("reset")

        monkeypatch.setattr(
            routes,
            "http_client_for",
            lambda app: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=Broken())
                )
            ),
        )
        app = _app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 200 and r.content == b"part"

    async def test_token_refresh_failure_closes_client(
        self, providers, upstream
    ):
        app = _app()
        _authorized(app, expires_at=time.time() + 5, refresh="")
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
        assert r.status_code == 401

    async def test_raw_path_outside_prefix(self, providers, upstream):
        """A raw path that does not carry the routed prefix (a proxy
        normalized it differently) is refused, never re-rooted."""
        app = _app()
        _authorized(app)
        req = types.SimpleNamespace(
            app=app,
            method="GET",
            scope={"raw_path": b"/elsewhere", "query_string": b""},
        )
        resp = await routes.forward(HOST, "api/v1/user", req, "ws1")
        assert resp.status_code == 400


class TestSslContext:
    def test_default(self):
        assert routes.ssl_context(make_settings(env={})) is not None

    def test_custom_ca(self, tmp_path):
        import certifi

        pem = tmp_path / "ca.pem"
        pem.write_text(open(certifi.where()).read())
        settings = make_settings(env={"KLANGKD_FORGE_PROXY_CA_CERT": str(pem)})
        assert routes.ssl_context(settings) is not None

    @pytest.mark.asyncio
    async def test_http_client(self):
        app = _app()
        client = routes.http_client_for(app)
        assert client.follow_redirects is False
        await client.aclose()


@pytest.mark.asyncio
async def test_patch_forward(providers, upstream):
    app = _app()
    _authorized(app)
    upstream.on(
        "PATCH", "/api/v1/repos/o/r/issues/2", 201, {"state": "closed"}
    )
    async with _client(app) as c:
        r = await c.patch(
            f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues/2",
            headers=_ws(app),
            json={"state": "closed"},
        )
    assert r.status_code == 201


def test_unwrap_dict_result():
    assert fp._unwrap({"status": "ok", "result": {"code": "c"}}) == {
        "code": "c"
    }


def test_container_flows_pass_when_no_forge_is_proxied(providers):
    f = _app({"KLANGKD_FORGE_PROXY_HOSTS": ""}).state.forge_proxy
    assert (
        f.bridge_policy(
            {
                "action": "git_credential",
                "operation": "auth_flow_start",
                "host": "github.com",
                "authorize_url": "https://github.com/x",
            }
        )
        == "pass"
    )


class TestCapsAndCleanup:
    def test_pending_txn_cap(self, providers):
        f = _app().state.forge_proxy
        for _ in range(fp.MAX_PENDING_TXNS_PER_WORKSPACE):
            f.start("ws1", HOST)
        with pytest.raises(fp.ForgeProxyError) as e:
            f.start("ws1", HOST)
        assert e.value.status == 429
        f.start("ws2", HOST)

    @pytest.mark.asyncio
    async def test_txn_expired_at_completion(self, providers, monkeypatch):
        f = _app().state.forge_proxy
        txn = f.claim("ws1", f.start("ws1", HOST))
        later = time.time() + fp.TXN_TTL_SECONDS + 1
        monkeypatch.setattr(fp.time, "time", lambda: later)
        with pytest.raises(fp.ForgeProxyError) as e:
            await f.complete(txn, "code", txn.state, None)
        assert e.value.status == 403

    @pytest.mark.asyncio
    async def test_dispatch_exception_clears_txn(self, providers):
        f = _app().state.forge_proxy
        txn_id = f.start("ws1", HOST)

        async def boom(request, timeout):
            raise TimeoutError

        with pytest.raises(TimeoutError):
            await f.relay_authorization("ws1", {"txn_id": txn_id}, boom, None)
        assert f._txns == {}

    @pytest.mark.asyncio
    async def test_browser_error_sanitized(self, providers):
        f = _app().state.forge_proxy
        txn_id = f.start("ws1", HOST)

        async def dispatch(request, timeout):
            return {"error": "<script>secret detail</script>"}

        out = await f.relay_authorization(
            "ws1", {"txn_id": txn_id}, dispatch, None
        )
        assert json.loads(out["result"]) == {"error": "authorization failed"}

    @pytest.mark.asyncio
    async def test_transport_error_on_exchange(self, providers):
        def boom(request):
            raise httpx.ConnectError("down")

        f = _app().state.forge_proxy
        txn = f.claim("ws1", f.start("ws1", HOST))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(boom)
        ) as http:
            with pytest.raises(fp.ForgeProxyError) as e:
                await f.complete(txn, "code", txn.state, http)
        assert e.value.status == 502

    @pytest.mark.asyncio
    async def test_malformed_json_token_answer(self, providers):
        up = Upstream()
        up.on(
            "POST",
            "/login/oauth/access_token",
            200,
            b"{not json",
            {"Content-Type": "application/json"},
        )
        f = _app().state.forge_proxy
        txn = f.claim("ws1", f.start("ws1", HOST))
        async with up.client() as http:
            with pytest.raises(fp.ForgeProxyError):
                await f.complete(txn, "code", txn.state, http)

    @pytest.mark.asyncio
    async def test_refresh_transport_error_forgets(self, providers):
        def boom(request):
            raise httpx.ConnectError("down")

        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(boom)
        ) as http:
            with pytest.raises(fp.ForgeProxyError) as e:
                await app.state.forge_proxy.access_token("ws1", HOST, http)
        assert e.value.status == 401


@pytest.mark.asyncio
class TestRefreshConcurrency:
    async def test_concurrent_requests_refresh_once(self, providers):
        import asyncio

        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        calls = []

        async def handler(request):
            calls.append(1)
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                json={"access_token": f"at{len(calls)}", "expires_in": 3600},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            toks = await asyncio.gather(
                *[
                    app.state.forge_proxy.access_token("ws1", HOST, http)
                    for _ in range(5)
                ]
            )
        assert len(calls) == 1
        assert {t.access_token for t in toks} == {"at1"}

    async def test_waiter_sees_forgotten_token(self, providers):
        import asyncio

        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        f = app.state.forge_proxy
        tok = f._tokens[("ws1", HOST)]
        lock = f._locks.setdefault(("ws1", HOST), asyncio.Lock())
        await lock.acquire()
        task = asyncio.ensure_future(f.access_token("ws1", HOST, None))
        await asyncio.sleep(0)
        f.forget_token("ws1", HOST, tok)
        lock.release()
        with pytest.raises(fp.ForgeProxyError) as e:
            await task
        assert e.value.status == 401

    async def test_stale_refresh_does_not_clobber_new_authorization(
        self, providers
    ):
        app = _app()
        _authorized(app, expires_at=time.time() + 10)
        f = app.state.forge_proxy
        old = f._tokens[("ws1", HOST)]
        newer = fp.ForgeToken("new-at", "new-rt", 0.0, "me")
        f._tokens[("ws1", HOST)] = newer
        up = Upstream()
        up.on(
            "POST", "/login/oauth/access_token", body={"access_token": "stale"}
        )
        async with up.client() as http:
            served = await f._refresh(("ws1", HOST), old, http)
        assert served is newer
        assert f._tokens[("ws1", HOST)] is newer
        f.forget_token("ws1", HOST, old)
        assert f._tokens[("ws1", HOST)] is newer


@pytest.mark.asyncio
class TestBodyLimits:
    async def test_json_body_too_large(self, providers, upstream):
        app = _app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues",
                headers=_ws(app),
                content=b"x" * (fp.MAX_JSON_BODY + 1),
            )
        assert r.status_code == 413 and upstream.requests == []

    async def test_git_body_streamed_and_capped(
        self, providers, upstream, monkeypatch
    ):
        app = _app()
        _authorized(app)
        upstream.on("POST", "/o/r.git/git-upload-pack", 200, b"PACK")
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/o/r.git/git-upload-pack",
                headers=_ws(app),
                content=b"0032want",
            )
            assert r.status_code == 200 and r.content == b"PACK"
            monkeypatch.setattr(fp, "MAX_GIT_BODY", 4)
            r = await c.post(
                f"/forge-proxy/{HOST}/o/r.git/git-upload-pack",
                headers=_ws(app),
                content=b"0032want",
            )
            assert r.status_code == 413


@pytest.mark.asyncio
async def test_refresh_after_forget_is_401(providers):
    app = _app()
    _authorized(app, expires_at=time.time() + 10)
    f = app.state.forge_proxy
    old = f._tokens.pop(("ws1", HOST))
    up = Upstream()
    up.on("POST", "/login/oauth/access_token", body={"access_token": "late"})
    async with up.client() as http:
        with pytest.raises(fp.ForgeProxyError) as e:
            await f._refresh(("ws1", HOST), old, http)
    assert e.value.status == 401


# --- repository allow-list and push inspection --------------------------------

ZERO = "0" * 40
OID = "a" * 40


def _pkt(text: str) -> bytes:
    data = text.encode()
    return b"%04x" % (len(data) + 4) + data


def _push(*commands: str, pack: bytes = b"PACKDATA") -> bytes:
    lines = [
        _pkt(c + ("\0report-status" if i == 0 else ""))
        for i, c in enumerate(commands)
    ]
    return b"".join(lines) + b"0000" + pack


class TestRepoRules:
    def test_unset_means_no_limit(self):
        assert fp.parse_repo_rules("") == []
        assert fp.parse_repo_rules(" , ") == []

    def test_parse(self):
        rules = fp.parse_repo_rules(
            "Owner/Repo.git, team/*:ro, bad, a/b/c, x/y:rw"
        )
        assert rules == [
            fp.RepoRule("owner", "repo", False),
            fp.RepoRule("team", "*", True),
        ]

    def test_set_but_unusable_allows_nothing(self):
        assert fp.parse_repo_rules("nonsense") == []
        route = fp.match_route("GET", "/api/v1/repos/o/r", "")
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.check_repo(route, [])
        assert e.value.status == 403

    @pytest.mark.parametrize(
        "method,path,query,allowed",
        [
            ("GET", "/api/v1/repos/Owner/Repo/issues", "", True),
            ("POST", "/owner/repo.git/git-receive-pack", "", True),
            ("GET", "/api/v1/repos/team/any", "", True),
            (
                "GET",
                "/team/any.git/info/refs",
                "service=git-upload-pack",
                True,
            ),
            (
                "GET",
                "/team/any.git/info/refs",
                "service=git-receive-pack",
                False,
            ),
            ("POST", "/team/any.git/git-receive-pack", "", False),
            ("POST", "/api/v1/repos/team/any/issues", "", False),
            ("GET", "/api/v1/repos/other/repo", "", False),
            ("GET", "/api/v1/user", "", True),
        ],
    )
    def test_check(self, method, path, query, allowed):
        rules = fp.parse_repo_rules("owner/repo, team/*:ro")
        route = fp.match_route(method, path, query)
        if allowed:
            fp.check_repo(route, rules)
        else:
            with pytest.raises(fp.ForgeProxyError) as e:
                fp.check_repo(route, rules)
            assert e.value.status == 403

    def test_parse_issues_mode(self):
        rules = fp.parse_repo_rules("Team/Template:issues")
        assert rules == [fp.RepoRule("team", "template", True, issues=True)]

    @pytest.mark.parametrize(
        "method,path,query,allowed",
        [
            ("GET", "/api/v1/repos/team/template", "", True),
            ("GET", "/api/v1/repos/team/template/issues", "", True),
            ("POST", "/api/v1/repos/team/template/issues", "", True),
            (
                "POST",
                "/api/v1/repos/team/template/issues/3/comments",
                "",
                True,
            ),
            ("PATCH", "/api/v1/repos/team/template/issues/3", "", True),
            ("POST", "/team/template.git/git-receive-pack", "", False),
            (
                "GET",
                "/team/template.git/info/refs",
                "service=git-receive-pack",
                False,
            ),
            (
                "GET",
                "/team/template.git/info/refs",
                "service=git-upload-pack",
                True,
            ),
            (
                "PUT",
                "/api/v1/repos/team/template/collaborators/svc",
                "",
                False,
            ),
        ],
    )
    def test_issues_mode(self, method, path, query, allowed):
        rules = fp.parse_repo_rules("team/template:issues")
        route = fp.match_route(method, path, query)
        if allowed:
            fp.check_repo(route, rules)
        else:
            with pytest.raises(fp.ForgeProxyError) as e:
                fp.check_repo(route, rules)
            assert e.value.status == 403

    def test_exact_rule_wins_over_wildcard(self):
        rules = fp.parse_repo_rules("team/*:ro, team/docs")
        route = fp.match_route("POST", "/api/v1/repos/team/docs/issues", "")
        fp.check_repo(route, rules)

    def test_unset_refuses_repo_routes(self):
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(fp.match_route("GET", "/api/v1/repos/o/r", ""), [])


class TestPushInspection:
    def test_create_and_update_pass(self):
        body = _push(
            f"{ZERO} {OID} refs/heads/new", f"{OID} {'b' * 40} refs/heads/main"
        )
        end = fp.push_commands_end(body)
        assert body[end:] == b"PACKDATA"
        fp.check_push_commands(body, end)

    def test_shallow_line_passes(self):
        body = _pkt(f"shallow {OID}") + _push(f"{ZERO} {OID} refs/heads/x")
        fp.check_push_commands(body, fp.push_commands_end(body))

    def test_delete_refused(self):
        body = _push(f"{OID} {ZERO} refs/heads/main")
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.check_push_commands(body, fp.push_commands_end(body))
        assert e.value.status == 403

    def test_sha256_delete_refused(self):
        body = _push(f"{'c' * 64} {'0' * 64} refs/heads/main")
        with pytest.raises(fp.ForgeProxyError):
            fp.check_push_commands(body, fp.push_commands_end(body))

    @pytest.mark.parametrize(
        "line", ["push-cert", f"{OID} {OID}", "garbage here now"]
    )
    def test_unknown_command_refused(self, line):
        body = _push(line)
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.check_push_commands(body, fp.push_commands_end(body))
        assert e.value.status == 400

    def test_incomplete_needs_more(self):
        assert fp.push_commands_end(_pkt(f"{ZERO} {OID} refs/heads/x")) is None
        assert fp.push_commands_end(b"00") is None

    @pytest.mark.parametrize("head", [b"zzzz", b"0002"])
    def test_malformed_length(self, head):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.push_commands_end(head + b"rest")
        assert e.value.status == 400


@pytest.mark.asyncio
class TestRouterPolicy:
    def _app(self, repos="owner/repo, team/*:ro"):
        return _app(env={"KLANGKD_FORGE_PROXY_ALLOWED_REPOS": repos})

    async def test_disallowed_repo_api_and_git(self, providers, upstream):
        app = self._app()
        _authorized(app)
        async with _client(app) as c:
            for path in (
                "/api/v1/repos/other/repo/issues",
                "/other/repo.git/info/refs?service=git-upload-pack",
            ):
                r = await c.get(f"/forge-proxy/{HOST}{path}", headers=_ws(app))
                assert r.status_code == 403
        assert upstream.requests == []

    async def test_read_only_repo(self, providers, upstream):
        app = self._app()
        _authorized(app)
        upstream.on("GET", "/team/docs.git/info/refs", 200, b"refs")
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/team/docs.git/info/refs?service=git-upload-pack",
                headers=_ws(app),
            )
            assert r.status_code == 200
            r = await c.get(
                f"/forge-proxy/{HOST}/team/docs.git/info/refs?service=git-receive-pack",
                headers=_ws(app),
            )
            assert r.status_code == 403
        assert len(upstream.requests) == 1

    async def test_push_create_streams_whole_body(self, providers, upstream):
        app = self._app()
        _authorized(app)
        upstream.on("POST", "/owner/repo.git/git-receive-pack", 200, b"ok")
        body = _push(f"{ZERO} {OID} refs/heads/x")
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/owner/repo.git/git-receive-pack",
                headers=_ws(app),
                content=body,
            )
        assert r.status_code == 200
        assert upstream.requests[0].read() == body

    async def test_push_delete_refused(self, providers, upstream):
        app = self._app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/owner/repo.git/git-receive-pack",
                headers=_ws(app),
                content=_push(f"{OID} {ZERO} refs/heads/x"),
            )
        assert r.status_code == 403 and upstream.requests == []

    async def test_push_refusals(self, providers, upstream, monkeypatch):
        app = self._app()
        _authorized(app)
        url = f"/forge-proxy/{HOST}/owner/repo.git/git-receive-pack"
        async with _client(app) as c:
            r = await c.post(
                url,
                headers={**_ws(app), "Content-Encoding": "gzip"},
                content=_push(f"{ZERO} {OID} refs/heads/x"),
            )
            assert r.status_code == 415
            r = await c.post(
                url,
                headers=_ws(app),
                content=_pkt(f"{ZERO} {OID} refs/heads/x"),
            )
            assert r.status_code == 400
            monkeypatch.setattr(fp, "MAX_PUSH_COMMANDS", 8)
            r = await c.post(
                url,
                headers=_ws(app),
                content=_pkt(f"{ZERO} {OID} refs/heads/x"),
            )
            assert r.status_code == 413
        assert upstream.requests == []

    async def test_push_pack_capped(self, providers, upstream, monkeypatch):
        app = self._app()
        _authorized(app)
        upstream.on("POST", "/owner/repo.git/git-receive-pack", 200, b"ok")
        body = _push(f"{ZERO} {OID} refs/heads/x", pack=b"P" * 64)

        async def chunked():
            yield body[:60]
            yield body[60:]

        monkeypatch.setattr(fp, "MAX_GIT_BODY", len(body) - 1)
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/owner/repo.git/git-receive-pack",
                headers=_ws(app),
                content=chunked(),
            )
        assert r.status_code == 413


# --- site creation: template generate and collaborator add -------------------


GEN = "/api/v1/repos/tmpl/site-template/generate"
GEN_BODY = {"owner": "me", "name": "new-site", "git_content": True}


class TestSiteCreationPolicy:
    def test_routes(self):
        r = fp.match_route("POST", GEN, "")
        assert (r.kind, r.owner, r.repo) == (
            "repo-generate",
            "tmpl",
            "site-template",
        )
        r = fp.match_route(
            "PUT", "/api/v1/repos/me/new-site/collaborators/build_svc", ""
        )
        assert (r.kind, r.owner, r.repo, r.user) == (
            "collaborator-add",
            "me",
            "new-site",
            "build_svc",
        )
        assert fp.is_write(r)

    def test_generate_body(self):
        out = fp.validate_json_body(
            "repo-generate",
            json.dumps(
                {**GEN_BODY, "private": True, "description": "d"}
            ).encode(),
        )
        assert json.loads(out)["private"] is True

    @pytest.mark.parametrize(
        "kind,body",
        [
            ("repo-generate", {"owner": "me"}),
            ("repo-generate", {**GEN_BODY, "webhooks": True}),
            ("repo-generate", {**GEN_BODY, "git_content": "yes"}),
            ("repo-generate", {**GEN_BODY, "private": 1}),
            ("repo-generate", {**GEN_BODY, "name": "../x"}),
            ("repo-generate", {**GEN_BODY, "owner": "a b"}),
            ("collaborator-add", {"permission": "write"}),
            ("collaborator-add", {"permission": True}),
            ("collaborator-add", {}),
            ("issue-state", {"state": "merged"}),
        ],
    )
    def test_bad_bodies(self, kind, body):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.validate_json_body(kind, json.dumps(body).encode())
        assert e.value.status == 400

    def test_parse_names(self):
        assert fp.parse_names(" A/B , ,c ") == {"a/b", "c"}
        assert fp.parse_names("") == set()

    def test_check_template(self):
        route = fp.match_route("POST", GEN, "")
        fp.check_template(route, {"tmpl/site-template"})
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.check_template(route, {"other/template"})
        assert e.value.status == 403

    @pytest.mark.parametrize(
        "owner,login,owners,rules,ok",
        [
            ("me", "Me", set(), None, True),
            ("team", "me", {"team"}, None, True),
            ("team", "me", set(), None, False),
            ("me", "", set(), None, False),
            ("me", "me", set(), "me/*", True),
            ("me", "me", set(), "me/*:ro", False),
            ("me", "me", set(), "other/*", False),
        ],
    )
    def test_check_new_repo(self, owner, login, owners, rules, ok):
        body = {"owner": owner, "name": "new-site"}
        parsed = fp.parse_repo_rules(rules or "*")
        if ok:
            fp.check_new_repo(body, login, owners, parsed)
        else:
            with pytest.raises(fp.ForgeProxyError) as e:
                fp.check_new_repo(body, login, owners, parsed)
            assert e.value.status == 403

    @pytest.mark.parametrize(
        "path,login,ok",
        [
            ("/api/v1/repos/me/r/collaborators/Build_Svc", "ME", True),
            ("/api/v1/repos/other/r/collaborators/build_svc", "me", False),
            ("/api/v1/repos/me/r/collaborators/someone", "me", False),
            ("/api/v1/repos/me/r/collaborators/build_svc", "", False),
        ],
    )
    def test_check_collaborator(self, path, login, ok):
        route = fp.match_route("PUT", path, "")
        if ok:
            fp.check_collaborator(route, login, {"build_svc"})
        else:
            with pytest.raises(fp.ForgeProxyError) as e:
                fp.check_collaborator(route, login, {"build_svc"})
            assert e.value.status == 403


@pytest.mark.asyncio
class TestSiteCreationRouter:
    def _app(self, **env):
        base = {
            "KLANGKD_FORGE_PROXY_ALLOWED_REPOS": "me/*, ops/registry",
            "KLANGKD_FORGE_PROXY_TEMPLATE_REPOS": "tmpl/site-template",
            "KLANGKD_FORGE_PROXY_COLLABORATOR_ACCOUNTS": "build_svc",
        }
        return _app(env={**base, **env})

    async def test_generate_and_collaborator(self, providers, upstream):
        app = self._app()
        _authorized(app)
        upstream.on("POST", GEN, 201, {"full_name": "me/new-site"})
        upstream.on(
            "PUT", "/api/v1/repos/me/new-site/collaborators/build_svc", 204
        )
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}{GEN}", headers=_ws(app), json=GEN_BODY
            )
            assert r.status_code == 201
            r = await c.put(
                f"/forge-proxy/{HOST}/api/v1/repos/me/new-site/collaborators/build_svc",
                headers=_ws(app),
                json={"permission": "read"},
            )
            assert r.status_code == 204
        assert json.loads(upstream.requests[0].read()) == GEN_BODY
        assert (
            upstream.requests[1].headers["authorization"] == "Bearer forge-at"
        )

    @pytest.mark.parametrize(
        "method,path,body,env",
        [
            # Template not listed.
            ("POST", "/api/v1/repos/tmpl/other/generate", GEN_BODY, {}),
            # Generating into someone else's account.
            ("POST", GEN, {**GEN_BODY, "owner": "victim"}, {}),
            # Owner allowed, but the new repo is outside the allow-list.
            (
                "POST",
                GEN,
                {**GEN_BODY, "owner": "team"},
                {"KLANGKD_FORGE_PROXY_TEMPLATE_OWNERS": "team"},
            ),
            # Collaborator on a repository the user does not own.
            (
                "PUT",
                "/api/v1/repos/ops/registry/collaborators/build_svc",
                {"permission": "read"},
                {},
            ),
            # An account not in the collaborator list.
            (
                "PUT",
                "/api/v1/repos/me/new-site/collaborators/mallory",
                {"permission": "read"},
                {},
            ),
        ],
    )
    async def test_refused(self, providers, upstream, method, path, body, env):
        app = self._app(**env)
        _authorized(app)
        async with _client(app) as c:
            r = await c.request(
                method,
                f"/forge-proxy/{HOST}{path}",
                headers=_ws(app),
                json=body,
            )
        assert r.status_code == 403
        assert upstream.requests == []

    async def test_write_permission_refused(self, providers, upstream):
        app = self._app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.put(
                f"/forge-proxy/{HOST}/api/v1/repos/me/new-site/collaborators/build_svc",
                headers=_ws(app),
                json={"permission": "admin"},
            )
        assert r.status_code == 400 and upstream.requests == []

    async def test_generate_refused_when_no_templates(
        self, providers, upstream
    ):
        app = self._app(KLANGKD_FORGE_PROXY_TEMPLATE_REPOS="")
        _authorized(app)
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}{GEN}", headers=_ws(app), json=GEN_BODY
            )
        assert r.status_code == 403 and upstream.requests == []


class TestFailClosed:
    def test_unset_allows_no_repository(self):
        route = fp.match_route("GET", "/api/v1/repos/o/r", "")
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.check_repo(route, fp.parse_repo_rules(""))
        assert e.value.status == 403

    def test_account_route_passes_without_rules(self):
        fp.check_repo(fp.match_route("GET", "/api/v1/user", ""), [])

    @pytest.mark.parametrize("raw", ["*", "*/*"])
    def test_star_allows_everything(self, raw):
        rules = fp.parse_repo_rules(raw)
        assert rules == [fp.RepoRule("*", "*", False)]
        fp.check_repo(
            fp.match_route("POST", "/a/b.git/git-receive-pack", ""), rules
        )

    def test_star_modes_and_specific_rules_win(self):
        rules = fp.parse_repo_rules("*:issues, me/*")
        push = fp.match_route("POST", "/other/x.git/git-receive-pack", "")
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(push, rules)
        fp.check_repo(
            fp.match_route("POST", "/api/v1/repos/other/x/issues", ""), rules
        )
        fp.check_repo(
            fp.match_route("POST", "/me/x.git/git-receive-pack", ""), rules
        )
        assert fp.parse_repo_rules("*:ro") == [fp.RepoRule("*", "*", True)]

    @pytest.mark.asyncio
    async def test_router_refuses_when_unset(self, providers, upstream):
        app = _app(env={"KLANGKD_FORGE_PROXY_ALLOWED_REPOS": ""})
        _authorized(app)
        upstream.on("GET", "/api/v1/user", body={"login": "me"})
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r", headers=_ws(app)
            )
            assert r.status_code == 403
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
            )
            assert r.status_code == 200


class TestIssueAssignees:
    @pytest.mark.parametrize(
        "kind,body",
        [
            ("issue-state", {"assignees": ["dev.one"]}),
            ("issue-state", {"state": "closed", "assignees": []}),
            ("issue-create", {"title": "t", "assignees": ["a", "b"]}),
        ],
    )
    def test_allowed(self, kind, body):
        out = fp.validate_json_body(kind, json.dumps(body).encode())
        assert json.loads(out) == body

    @pytest.mark.parametrize(
        "kind,body",
        [
            ("issue-state", {}),
            ("issue-state", {"ref": "main"}),
            ("issue-state", {"assignees": "someone"}),
            ("issue-state", {"assignees": [1]}),
            ("issue-state", {"assignees": ["a/b"]}),
            ("issue-state", {"assignees": ["a"] * 11}),
        ],
    )
    def test_refused(self, kind, body):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.validate_json_body(kind, json.dumps(body).encode())
        assert e.value.status == 400

    def test_issues_rule_allows_edit_but_not_push(self):
        rules = fp.parse_repo_rules("team/*:issues")
        fp.check_repo(
            fp.match_route("PATCH", "/api/v1/repos/team/r/issues/3", ""), rules
        )
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(
                fp.match_route("POST", "/team/r.git/git-receive-pack", ""),
                rules,
            )


class TestIssueSurface:
    @pytest.mark.parametrize(
        "kind,body",
        [
            ("issue-state", {"title": "t", "body": "b", "milestone": 2}),
            (
                "issue-create",
                {"title": "t", "labels": [3, "bug"], "milestone": 1},
            ),
            ("issue-labels", {"labels": ["bug", 7]}),
        ],
    )
    def test_allowed(self, kind, body):
        assert (
            json.loads(fp.validate_json_body(kind, json.dumps(body).encode()))
            == body
        )

    def test_too_many_labels(self):
        with pytest.raises(fp.ForgeProxyError):
            fp.validate_json_body(
                "issue-labels",
                json.dumps({"labels": list(range(1, 22))}).encode(),
            )

    @pytest.mark.parametrize(
        "method,path,kind",
        [
            ("GET", "/api/v1/repos/o/r/labels", "api-read"),
            ("GET", "/api/v1/repos/o/r/milestones", "api-read"),
            ("GET", "/api/v1/repos/o/r/assignees", "api-read"),
            ("GET", "/api/v1/repos/o/r/collaborators", "api-read"),
            ("POST", "/api/v1/repos/o/r/issues/4/labels", "issue-labels"),
            ("PUT", "/api/v1/repos/o/r/issues/4/labels", "issue-labels"),
        ],
    )
    def test_routes(self, method, path, kind):
        assert fp.match_route(method, path, "").kind == kind

    def test_issues_rule_allows_labels(self):
        rules = fp.parse_repo_rules("team/*:issues")
        fp.check_repo(
            fp.match_route("PUT", "/api/v1/repos/team/r/issues/1/labels", ""),
            rules,
        )


class TestPassthrough:
    def test_off_by_default(self):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.match_route("GET", "/api/v1/orgs/x", "")
        assert e.value.status == 403

    @pytest.mark.parametrize(
        "method,path,kind,owner",
        [
            ("GET", "/api/v1/orgs/team/teams", "api-pass-read", ""),
            (
                "DELETE",
                "/api/v1/repos/o/r/issues/3/labels/2",
                "api-pass-write",
                "o",
            ),
            ("POST", "/api/v1/repos/o/r", "api-pass-write", "o"),
        ],
    )
    def test_routes(self, method, path, kind, owner):
        r = fp.match_route(method, path, "page=2", passthrough=True)
        assert (r.kind, r.owner, r.query) == (kind, owner, "page=2")

    def test_listed_routes_keep_their_kind(self):
        r = fp.match_route("GET", "/api/v1/repos/o/r", "", passthrough=True)
        assert r.kind == "api-read"

    @pytest.mark.parametrize(
        "method,path,query,status",
        [
            ("GET", "/api/v1/user", "access_token=x", 400),
            ("GET", "/api/v1/orgs/x", "Sudo=admin", 400),
            ("OPTIONS", "/api/v1/orgs/x", "", 405),
            ("GET", "/o/r/raw/main/file", "", 403),
        ],
    )
    def test_refusals(self, method, path, query, status):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.match_route(method, path, query, passthrough=True)
        assert e.value.status == status

    def test_repo_rules_still_apply(self):
        rules = fp.parse_repo_rules("team/*:issues")
        route = fp.match_route(
            "DELETE", "/api/v1/repos/team/r/issues/3", "", passthrough=True
        )
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(route, rules)
        other = fp.match_route(
            "GET", "/api/v1/repos/other/r/topics", "", passthrough=True
        )
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(other, rules)

    @pytest.mark.asyncio
    async def test_router_forwards_any_api_call(self, providers, upstream):
        app = _app(env={"KLANGKD_FORGE_PROXY_FEATURES": "*"})
        _authorized(app)
        upstream.on("DELETE", "/api/v1/repos/o/r/issues/3/labels/2", 204, b"")
        upstream.on("GET", "/api/v1/orgs/team/teams", body=[])
        async with _client(app) as c:
            r = await c.delete(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues/3/labels/2",
                headers=_ws(app),
            )
            assert r.status_code == 204
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/orgs/team/teams", headers=_ws(app)
            )
            assert r.status_code == 200
        assert (
            upstream.requests[0].headers["authorization"] == "Bearer forge-at"
        )

    @pytest.mark.asyncio
    async def test_router_allowlist_mode_refuses(self, providers, upstream):
        app = _app()
        _authorized(app)
        async with _client(app) as c:
            r = await c.delete(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues/3",
                headers=_ws(app),
            )
        assert r.status_code == 403 and upstream.requests == []


class TestFeatures:
    def test_parse(self):
        assert fp.parse_features("") == set()
        assert fp.parse_features(" Read , issues, bogus") == {"read", "issues"}
        assert fp.parse_features("*") == set(fp.FEATURES)

    @pytest.mark.parametrize(
        "method,path,query,feature",
        [
            (
                "GET",
                "/o/r.git/info/refs",
                "service=git-upload-pack",
                "git-read",
            ),
            (
                "GET",
                "/o/r.git/info/refs",
                "service=git-receive-pack",
                "git-push",
            ),
            ("POST", "/o/r.git/git-receive-pack", "", "git-push"),
            ("POST", "/api/v1/repos/o/r/issues", "", "issues"),
            ("PATCH", "/api/v1/repos/o/r/issues/2", "", "issue-edit"),
            ("GET", "/api/v1/repos/o/r", "", "read"),
        ],
    )
    def test_route_feature(self, method, path, query, feature):
        assert fp.route_feature(fp.match_route(method, path, query)) == feature

    def test_locked_down_by_default(self):
        route = fp.match_route("GET", "/api/v1/user", "")
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.check_feature(route, fp.parse_features(""))
        assert e.value.status == 403

    @pytest.mark.asyncio
    async def test_router_default_refuses_everything(
        self, providers, upstream
    ):
        app = _app(env={"KLANGKD_FORGE_PROXY_FEATURES": ""})
        _authorized(app)
        async with _client(app) as c:
            for method, path in (
                ("GET", "/api/v1/user"),
                ("GET", "/o/r.git/info/refs?service=git-upload-pack"),
                ("POST", "/api/v1/repos/o/r/issues"),
            ):
                r = await c.request(
                    method,
                    f"/forge-proxy/{HOST}{path}",
                    headers=_ws(app),
                    content=b'{"title":"t"}',
                )
                assert r.status_code == 403
        assert upstream.requests == []

    @pytest.mark.asyncio
    async def test_router_read_only_features(self, providers, upstream):
        app = _app(env={"KLANGKD_FORGE_PROXY_FEATURES": "read,git-read"})
        _authorized(app)
        upstream.on("GET", "/api/v1/user", body={"login": "me"})
        async with _client(app) as c:
            assert (
                await c.get(
                    f"/forge-proxy/{HOST}/api/v1/user", headers=_ws(app)
                )
            ).status_code == 200
            r = await c.post(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r/issues",
                headers=_ws(app),
                json={"title": "t"},
            )
            assert r.status_code == 403


class TestCodexFindings:
    @pytest.mark.parametrize(
        "path", ["/api/v1/repos/o/r#", "/api/v1/repos/o/r?x"]
    )
    def test_delimiters_in_path_rejected(self, path):
        with pytest.raises(fp.ForgeProxyError) as e:
            fp.match_route("DELETE", path, "", passthrough=True)
        assert e.value.status == 400

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/api/v1/user/repos"),
            ("GET", "/api/v1/repositories/42"),
            ("GET", "/api/v1/repos/search"),
            ("GET", "/api/v1/orgs/team/repos"),
        ],
    )
    def test_unscoped_passthrough_needs_star(self, method, path):
        route = fp.match_route(method, path, "", passthrough=True)
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(route, fp.parse_repo_rules("me/*"))
        fp.check_repo(route, fp.parse_repo_rules("*"))

    def test_unscoped_passthrough_respects_star_mode(self):
        route = fp.match_route(
            "POST", "/api/v1/user/repos", "", passthrough=True
        )
        with pytest.raises(fp.ForgeProxyError):
            fp.check_repo(route, fp.parse_repo_rules("*:ro"))


@pytest.mark.asyncio
class TestErrorBodies:
    async def test_api_error_body_replaced(self, providers, upstream):
        app = _app()
        _authorized(app)
        upstream.on(
            "GET",
            "/api/v1/repos/o/r",
            500,
            {"message": "token forge-at rejected"},
        )
        async with _client(app) as c:
            r = await c.get(
                f"/forge-proxy/{HOST}/api/v1/repos/o/r", headers=_ws(app)
            )
        assert r.status_code == 500
        assert "forge-at" not in r.text and r.json()["detail"].endswith(
            "(500)"
        )

    async def test_git_error_body_kept(self, providers, upstream):
        app = _app()
        _authorized(app)
        upstream.on(
            "POST", "/o/r.git/git-upload-pack", 403, b"denied by forge"
        )
        async with _client(app) as c:
            r = await c.post(
                f"/forge-proxy/{HOST}/o/r.git/git-upload-pack",
                headers=_ws(app),
                content=b"0000",
            )
        assert r.status_code == 403 and r.content == b"denied by forge"
