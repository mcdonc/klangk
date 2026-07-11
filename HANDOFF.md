# HANDOFF — issue #1399: CLI transport resolver (UDS or URL for `--server`)

Status: **investigation complete, no code written yet.** This document is
the full picture so a fresh agent can implement without re-reading the whole
CLI. Delete this file before opening the PR.

- Issue: https://github.com/mcdonc/klangk/issues/1399 (chunk 6 of 7 in #1392)
- Worktree: `.worktrees/issue-1399-cli-transport-resolver-klangkc-server-accepts-socket-path-or`
- Branch: `issue-1399-cli-transport-resolver-klangkc-server-accepts-socket-path-or` (off `origin/main` @ `2d4e914e`)
- Scope: **client-only.** No server changes.

## Goal (one line)

`klangkc login --server` (and therefore every CLI command) must accept
**either** an absolute Unix socket path **or** an `http(s)://` URL. One
transport resolver picks UDS vs TCP and routes **every** outbound call
(HTTP requests + WebSocket connections) through it.

## Detection rule (from the issue)

- `http://...` or `https://...` prefix → **TCP** (unchanged behavior).
- Anything else → **UDS**, and the path **must be absolute**. A
  relative/bare value is an error ("socket path must be absolute").
- No `unix:`/`file:` scheme — the _absence_ of an http(s) scheme is the
  signal.

**Incremental rollout:** the issue asks to ship a **guesser first**
(try-is-it-a-file/socket/has-scheme, pick; **warn on every guess**), then
tighten to the strict rule. The strict rule is a restriction, so it breaks
nothing the guesser accepted correctly. (See "Open design question" below
on whether to ship guesser or strict first — the issue's wording slightly
contradicts its own acceptance criteria.)

## Verified facts (from a throwaway script, not committed)

Installed: `httpx==0.28.1`, `websockets==16.0`.

- **httpx UDS works:** `httpx.HTTPTransport(uds=path)` →
  `httpx.Client(transport=..., base_url="http://localhost")` → `c.get("/api/v1/config")`
  returns 200 from a uvicorn server bound to the socket. Confirmed.
- **websockets UDS works:** open a preconnected socket
  (`socket.socket(AF_UNIX); sock.connect(path)`), then
  `websockets.connect("ws://localhost/ws", sock=sock, ...)`. The `sock=`
  kwarg is the supported hook (documented in `connect`'s docstring: "You
  may set `sock` to provide a preexisting TCP socket"). Note the socket
  must be a **connected** AF_UNIX socket, not a path. The URI still needs
  a host (use a dummy like `localhost` / `unix`); the real transport is
  the socket.

## Current architecture (what exists today)

Everything assumes TCP. Two modules carry all outbound calls:

### `src/cli/klangkc/auth.py` — HTTP auth calls (6 module-level httpx calls)

| #   | function                    | call                                                          | line |
| --- | --------------------------- | ------------------------------------------------------------- | ---- |
| 1   | `fetch_config`              | `httpx.get(f"{server_url}/api/v1/config", timeout=5.0)`       | 36   |
| 2   | `local_login`               | `httpx.post(f"{server_url}/api/v1/auth/local", timeout=15.0)` | 53   |
| 3   | `login` (token-reuse probe) | `httpx.get(f"{server_url}/api/v1/workspaces", ...)`           | 196  |
| 4   | `login` (password)          | `httpx.post(f"{server_url}/api/v1/auth/login", ...)`          | 271  |
| 5   | `refresh_token`             | `httpx.post(f"{server_url}/api/v1/auth/refresh", ...)`        | 309  |
| 6   | `logout`                    | `httpx.post(f"{server_url}/api/v1/auth/logout", ...)`         | 347  |

`server_url` here is the raw `--server` string (URL today).

### `src/cli/klangkc/client.py` — KlangkClient + WS session code

HTTP (3 sites; `KlangkClient` methods all funnel through `_request`):

| #   | function             | call                                                        | line |
| --- | -------------------- | ----------------------------------------------------------- | ---- |
| 7   | `request_with_retry` | `httpx.request(method, url, timeout=timeout, **kwargs)`     | 131  |
| 8   | `export_workspace`   | `httpx.stream("GET", f"{self.server_url}/.../export", ...)` | 546  |
| 9   | `import_workspace`   | `httpx.post(f"{self.server_url}/.../import", ...)`          | 610  |

WebSocket (3 connect sites; all `websockets.connect(f"{ws_url}?token={token}", max_size=...)`):

| #   | function        | line |
| --- | --------------- | ---- |
| W1  | `ws_shell`      | 767  |
| W2  | `ws_exec`       | 1518 |
| W3  | `ws_exec_piped` | 1544 |

**Token-refresh-via-WS wrinkle (W1 only):** inside `ws_shell`, after
connecting, it derives an HTTP base URL from the WS URL for the
session's self-healing refresh (client.py ~line 995):

```python
_http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
if _http_url.endswith("/ws"):
    _http_url = _http_url[:-3]
# passed as server_url= into TerminalSession; on a 4002 close,
# stdout_loop calls _refresh_token / _server_mode_is_none / _local_login
# with that server_url.
```

For UDS this derivation is **wrong** — it must yield a value the HTTP
resolver recognizes as UDS again (i.e. the socket path), not a dummy host.

### `src/cli/klangkc/main.py` — command entrypoints + `build_ws_url`

`build_ws_url(server_url)` at **line 905** is the single HTTP→WS
converter today:

```python
def build_ws_url(server_url: str) -> str:
    if server_url.startswith("http://"):
        return server_url.replace("http://", "ws://") + "/ws"
    elif server_url.startswith("https://"):
        return server_url.replace("https://", "wss://") + "/ws"
    return f"ws://{server_url}/ws"
```

Callers of `build_ws_url`: `shell` (~995), `exec_cmd` (~1995), `monitor`
(~1289), `sandbox` (~1448), `_resolve_workspace_and_url` (~1331, used by
`terminal ls`, `share_terminal`, `unshare_terminal`).

WS connect sites in main.py (5):

| #   | function                  | line |
| --- | ------------------------- | ---- |
| W4  | `monitor_connection`      | 1090 |
| W5  | `sandbox_setup_only`      | 1529 |
| W6  | `terminals` (terminal ls) | 1608 |
| W7  | `share_terminal`          | 1740 |
| W8  | `unshare_terminal`        | 1821 |

**Total: 9 HTTP call sites + 8 WS connect sites = 17 places, all currently
hardcoded TCP.** Centralizing is the point of the issue.

### `src/cli/klangkc/config.py`

`CLIConfig.resolve_server(name_or_url)` (line ~67) maps a config alias to
its `url` and is applied to `--server` in `main.py:app_callback` (~93) and
`login_cmd` (~187) **before** `server_url()` is set. `ServerEntry.url` is
`str`. A socket path can be stored as the alias's `url` unchanged (it's
just a string); no schema change needed, but the resolver must run _after_
alias resolution, on whatever string comes out.

## THE design decision (read this first)

There is a hard tension between the issue's "one resolver, no per-call-site
branching" and the **existing test mocking surface**:

- Tests patch the **module-level functions**: `httpx.get`, `httpx.post`,
  `httpx.request`, `httpx.stream` (37 patch sites), and `websockets.connect`
  (38 patch sites). All tests use TCP/server URLs.
- `httpx.Client(...).get(...)` does **NOT** go through the module-level
  `httpx.get`. So if the resolver returns an `httpx.Client` for _every_
  call, the 37 httpx patches stop intercepting → ~all HTTP tests break and
  need rewriting.

**Recommended resolution (preserves the mocking surface):** make the
resolver a **pair of thin helper functions**, not a shared Client object.
The TCP path delegates to the exact module functions tests already patch;
only the UDS path constructs a Client/socket.

```python
# in a new src/cli/klangkc/transport.py  (the "single resolver")

def resolve_transport(server_spec: str) -> ServerTransport:
    """Decide UDS vs TCP from the server spec string.
    Returns (is_uds, uds_path, base_url, ws_uri). Raises ValueError on a
    relative/non-http(s) value (strict rule) — or warns + guesses (rollout)."""

def http_request(server_spec, method, path, **kwargs) -> httpx.Response:
    """TCP → httpx.request(...)  (module fn, tests patch this).
       UDS  → pooled httpx.Client(transport=HTTPTransport(uds=path)).request(...)."""

# + a ws_connect(...) async context-manager helper:
async def ws_connect(server_spec, ws_path, *, token, **kwargs):
    """TCP → websockets.connect(uri, **kw)            (tests patch this).
       UDS  → open AF_UNIX socket, connect(path), websockets.connect(uri, sock=sock, **kw)."""
```

Every call site swaps its `httpx.X(...)` / `websockets.connect(...)` for
the matching helper, passing the **raw server spec string** (so the helper
can re-derive transport each time — no stale cached decision). That is
"one resolver, used everywhere" with **zero** transport branching at call
sites, and **zero** test-mock breakage on the TCP path (which is what every
test exercises). UDS paths get fresh, dedicated unit tests.

> Alternative considered: a global pooled `httpx.Client`. Rejected — breaks
> the 37 module-level patches and risks connection-pool surprises. Only
> pool _inside_ the UDS arm if needed.

## Concrete steps

1. **New module `src/cli/klangkc/transport.py`** with
   `resolve_transport` + `http_request` + `ws_connect` (see shape above).
   - `resolve_transport`: returns a small dataclass. Detection: prefix
     `http://`/`https://` → TCP; absolute path → UDS; else error (or
     guess-and-warn for the rollout phase).
   - `http_request`: TCP arm calls `httpx.request` (so `patch("httpx.request")`
     / `patch("klangkc.client.httpx.request")` keep working); UDS arm builds
     `httpx.HTTPTransport(uds=path)` + `httpx.Client` and `.request`s.
     The full URL for UDS is `http://localhost` + path (dummy host — the
     socket is the transport, host is irrelevant to a UDS server).
   - `ws_connect`: TCP arm is a thin passthrough to `websockets.connect`
     (preserve patchability). UDS arm: `socket.socket(AF_UNIX).connect(path)`
     then `websockets.connect(uri, sock=sock, **kw)`. Make it usable as
     `async with ws_connect(...) as ws:` (return the connect()'s
     context manager, or a small async-CM wrapper).
   - Build the WS URI inside the helper from the spec + a `ws_path`
     ("/ws"), so call sites stop hand-building `f"{ws_url}?token=..."`.

2. **Re-route HTTP call sites** (auth.py 1–6, client.py 7–9) through
   `http_request`. Each keeps passing the raw server spec + the API path.
   For client.py, `KlangkClient._request` already centralizes its own
   calls, so only `request_with_retry` + `export_workspace` + `import_workspace`
   change; the retry/refresh logic above them is untouched.

3. **Re-route WS call sites** (client.py W1–W3, main.py W4–W8) through
   `ws_connect`. `build_ws_url` either becomes a delegate to the resolver
   or is replaced by passing the raw spec into `ws_connect` (prefer the
   latter — fewer string round-trips, fixes the `_http_url` wrinkle
   directly: the WS helper can also hand back the HTTP refresh URL as the
   raw spec so token-refresh-on-4002 works over UDS).

4. **Fix the `ws_shell` token-refresh URL** (client.py ~995): for UDS,
   `_http_url` must be the socket spec, not a dummy host. Cleanest: have
   `ws_connect` / the resolver return the canonical HTTP server spec for
   refresh, and pass _that_ into `TerminalSession.server_url`.

5. **Tests (100% coverage gate, run with `-n auto`):**
   - Existing TCP tests must pass **unchanged** (that's the point of the
     delegating-helper design). Spot-check `TestBuildWsUrl`
     (test_cli_main.py ~2882), the `request_with_retry` tests
     (test_cli.py ~913), and the monitor `ws_url` assertion
     (test_cli_main.py ~3625: `args[1] == "ws://localhost:8995/ws"`).
     If `build_ws_url`'s signature/return changes, update these — but keep
     TCP behavior identical.
   - **Add new unit tests for the resolver:** TCP detection, UDS detection,
     absolute-path enforcement / guesser warning, and that `http_request`/
     `ws_connect` invoke `httpx.request`/`websockets.connect` on the TCP
     path (mock the module fns) while constructing the UDS transport on the
     UDS path (mock `httpx.HTTPTransport`/`socket`). The issue's acceptance
     criteria are effectively these tests.
   - Optional but high-value: one integration test spinning up a uvicorn
     server on a UDS (use the pattern from the throwaway script that's
     described under "Verified facts") and proving a full request +
     websocket round-trip. See `src/cli/tests/test_cli_integration.py` for
     the existing real-server test style.
   - **Run:** `devenv --quiet -O dotenv.enable:bool false shell -- python
-m pytest src/cli/tests -v -n auto` (must stay 100%).

6. **Changelog:** add an **Added** bullet under `## [Unreleased]` in
   `docs/changes.md` per AGENTS.md (this is user-visible: `--server`
   accepts a socket path).

## Open design question to confirm with the issue author

The issue's **Acceptance criteria** are phrased as the **strict** rule
("any non-http(s) value connects over UDS"), but the **Design** section
says "ship a guesser first, warn on every guess, then tighten." These
conflict: the strict rule has no guessing. **Recommendation:** ship the
**strict** rule (absolute-path-or-URL) directly — it's simpler, matches
the acceptance criteria exactly, and the "relative socket path" error
message is self-documenting. The guesser is only worth the complexity if
real-world `--server` values turn out to be ambiguous. Flag this in the PR
description if you go strict-first.

## Things that should NOT change

- Server-side anything (client-only issue).
- `KlangkClient`'s public method signatures or the retry/refresh flow
  (`_request`, `_try_refresh`, `_headers`, 401-retry) — only the leaf
  transport call inside them moves into the helper.
- The `websockets` `sock=` approach is confirmed working in 16.0; do not
  switch to a different WS-UDS mechanism.
- The on-the-wire protocol (token in `?token=`, `/ws` path, same JSON
  frames) — UDS is just a different transport for the same endpoints.

## Quick orientation commands

```bash
# all HTTP call sites
grep -rn 'httpx\.\(get\|post\|request\|stream\|put\|delete\)' src/cli/klangkc/
# all WS connect sites
grep -rn 'websockets.connect' src/cli/klangkc/
# run the suite (100% gate, must use -n auto)
devenv --quiet -O dotenv.enable:bool false shell -- python -m pytest src/cli/tests -v -n auto
```
