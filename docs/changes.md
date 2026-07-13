# Changelog

All notable changes to klangk are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and each version's section is also prepended to its GitHub Release notes (see
[Releasing](../development/releasing.md)).

Entries use the following conventions:

- **Added** — new features.
- **Changed** — changes to existing functionality.
- **Deprecated** — soon-to-be removed features.
- **Removed** — now removed features.
- **Fixed** — bug fixes.
- **Security** — fixes for vulnerabilities, in lieu of or in addition to a
  dedicated security advisory.

A `Breaking` subsection may appear under any version for changes that require
operators or integrators to act when upgrading.

<!-- The release workflow prepends each released version's section to its GitHub
     Release body. Keep one `## [<version>]` section per release; unreleased
     changes accumulate under `## [Unreleased]`. -->

## [Unreleased]

### Added

- **Construction-time `file:`/`cmd:` resolution:** `KlangkSettings` now
  resolves all `file:`/`cmd:`-prefixed field values once, at construction.
  A dangling reference (e.g. `file:/nonexistent`) fails fast at boot with
  a `ValidationError`, not silently at use time. Callers read
  `settings.field` directly — no per-call `resolve_indirection` wrap
  (#1461).
- **`state_dir` required; `data_dir` / `customize_dir` / `plugins_dir` derive from it:**
  `KLANGK_STATE_DIR` has no default — a missing value fails at construction
  with a `ValidationError` (#1459, #1461). `KLANGK_DATA_DIR` defaults to
  `<KLANGK_STATE_DIR>/data`, `KLANGK_CUSTOMIZE_DIR` to
  `<KLANGK_STATE_DIR>/custom`, and `KLANGK_PLUGINS_DIR` to
  `<KLANGK_STATE_DIR>/plugins` when unset; an explicit value always wins
  (#1461, #1506). `klangkd` no longer mutates `os.environ` to inject a
  `state_dir` default; the field enforces its own requirement (#1459).
- **CLI transport resolver:** `klangkc --server` now accepts a Unix socket
  path (e.g. `/tmp/klangk.sock`) in addition to `http(s)://` URLs. All HTTP
  and WebSocket connections route through a single transport resolver that
  picks UDS or TCP based on the server spec (#1399).
- **Dev config file:** devenv now reads backend config from `klangkd.yaml`
  (gitignored; copied from `klangkd.yaml.example` on first shell entry).
  `.env` / `dotenv.enable` removed; `KLANGK_LISTEN`, `KLANGK_IMAGE_NAME`,
  `KLANGK_CUSTOMIZE_DIR`, `KLANGK_PORT`, `KLANGK_NGINX_PORT` no longer set
  as env vars by devenv (#1399).
- **UDS safe for no-auth mode:** `KLANGK_AUTH_MODES=none` now accepts a UDS
  bind without `KLANGK_ALLOW_INSECURE_NO_AUTH` — socket file permissions
  (0700 parent dir) provide the same trust boundary as loopback (#1399).
- **Direct UDS login:** `client_is_loopback` treats direct UDS connections
  (no nginx proxy) as loopback, so `klangkc login /path/to/sock` works in
  no-auth mode (#1399).
- **Per-test timeout for the Python test suites** — both backend and CLI
  suites now run with `pytest-timeout` (`--timeout=60`). A hanging test
  fails after 60s instead of burning the whole job budget. New
  `pytest-timeout` dev dependency (#1513).

### Changed

- **`resolve_env_value` (KLANGK path) no longer re-resolves `file:`/`cmd:`**
  — the field is already resolved at construction. The function survives for
  plugins' dynamic keys (non-`KLANGK_`, discovered from `package.json`) and
  not-yet-migrated modules; core code should read `app_state.settings.field`
  directly (#1461).
- **Public `resolve_indirection` removed** — the logic is now private
  (`_resolve_indirection`), called only by the model validator and the
  non-`KLANGK_` path of `resolve_env_value` (#1461).
- **Proxy-trust / hosting helpers are instance methods on `Util(app_state)`:**
  `util.py` no longer reads config at import time. `reject_proxy_headers`,
  `trusted_proxy_cidrs`, `peer_trusted`, `connection_peer_is_trusted`,
  `client_is_loopback`, `derive_hosting_info`, `customize_dir`, `cors_origins`,
  and `set_uds_mode` are now methods on `Util`, reading `self.settings` at
  call time. The module globals `_REJECT_PROXY`, `_TRUSTED_PROXY_CIDRS`, and
  `_UDS_MODE` are gone. `klangkd` arms UDS trust via
  `app.state.util.set_uds_mode(True)` after `build_app` (#1503, #1426).

### Removed

- **`scripts/run-host-container.sh`:** retired; the `env | grep '^KLANGK_'`
  env-passthrough mechanism is replaced by mounting a config file (#1417).
- **Headless single-user profile: nginx minimal template on a socket bind**
  (#1398, chunk 5 of #1392). When `KLANGK_LISTEN` is a UNIX socket path,
  the nginx renderer now emits a minimal (headless) template — only the
  container-egress `/llm-proxy` location (with its workspace-token
  `auth_request` gate + `CONTAINER_ACL`) on the single container-egress
  listener, and nothing else: no `location /`, no `/api/v1/*`, no static
  UI, no `/auth/local`. A browser can't reach a UDS and uvicorn exposes no
  browser-facing TCP, so no browser surface is serviceable — the attack
  surface is two channels (operator→UDS, container→llm-proxy) and nothing
  else. Template selection keys off `KLANGK_LISTEN`'s shape alone; the
  `KLANGK_AUTH_MODE` value does not participate (socket ⇒ minimal, TCP ⇒
  full browser template, across all auth values). The TCP path is a strict
  regression guard (byte-for-byte identical output). This makes the
  UDS+none default posture's "eliminate the browser/TCP surface" a real
  property rather than a claim; the default-flip itself is #1400.

- **`test-all` / `test-unit` devenv scripts and concurrency-safe test corpus**
  (#1393). The whole test corpus is now runnable concurrently: every E2E
  harness free-allocates its server port and `KLANGK_PORT_RANGE_START`
  (via a new `klangk_backend.model.free_port` helper) instead of hardcoding
  them, and container teardown is instance-scoped (no more `klangk.managed=true`
  sweeps that nuked other suites' containers). The two unit suites combine
  into one `python -m pytest src/backend/tests src/cli/tests` invocation
  (the root `pyproject.toml` now carries the asyncio + capture config that
  used to conflate them). New `test-all` runs unit + E2E; `test-unit` runs
  the combined unit corpus. E2E tasks dropped the forced `-p no:xdist` —
  opt into parallelism with `-n auto --dist=loadscope`.

- **`KLANGK_AUTH_MODES=none`: no-login single-user (local-dev) mode**
  (#1374). A new `none` auth mode lets the frontend and CLI obtain a token
  for the seeded default user with no password prompt, enabling a frictionless
  single-user dev/test loop and serving as the foundation for a "one binary,
  named deployment profiles" strategy (`local-dev` / `customer-locked` /
  `team`). The server
  auto-creates the default user at startup; `POST /api/v1/auth/local` mints a
  standard JWT for it. The loopback bind (`KLANGK_LISTEN`, #1375) plus an
  nginx per-location `allow 127.0.0.1/::1; deny all` ACL keep `/auth/local`
  unreachable from workspace containers, and the server refuses to start in
  `none` mode on a non-loopback bind unless `KLANGK_ALLOW_INSECURE_NO_AUTH=1`
  is set. The CLI (`klangkc`) auto-logs in on first command run with no prior
  `klangkc login`; the server's auth mode is probed live (not cached) so a
  mode switch takes effect immediately. See [Auth Modes](features/auth-modes.md)
  for the full mode-switching guide.
- **`klangkc admin` command group** (#1374): site-wide administration now
  has a dedicated CLI surface — `admin users ls`, `admin users
set-password <email>` (set a known password for the default user — whose
  password is random unless `KLANGK_DEFAULT_PASSWORD` was set — before
  flipping `none` -> `password`), and `admin invitations send/ls`. The
  top-level `invite`/`invitations` commands moved under `admin invitations`.
- **`klangkc status`** now reports your user id and admin status (derived
  from `/my-permissions`).

### Breaking

- **Direct TCP to uvicorn is gone.** uvicorn now binds only a UNIX socket
  (`<state_dir>/klangk.sock`); nginx proxies to it. Point external proxies at
  `KLANGK_NGINX_PORT` (default 8995), not the old port 8997 (#1400).
- **Default is now headless.** Bare `klangkd` (no `KLANGK_LISTEN` set)
  defaults to UDS + `none` auth — headless, CLI-only. Set
  `KLANGK_LISTEN=127.0.0.1` for the browser UI (#1400).
- **`KLANGK_PORT` is no longer used by klangkd.** uvicorn always binds a UDS;
  the setting is retained only for bare-uvicorn test harnesses (#1400).
- **Devenv default changed to browser-first.** `klangkd.yaml.example` now
  defaults to `listen: 127.0.0.1` + `auth_modes: password`. Delete your local
  `klangkd.yaml` and re-enter `devenv shell` to regenerate it (#1400).
- **Default auth mode is now `none`** (no-login single-user, loopback-bound)
  when `KLANGK_AUTH_MODES` is unset and no OIDC provider is configured
  (#1374). Previously the unset default was `password`. A fresh klangk now
  "just works" locally with no password and is unreachable from the network.
  This is safe by construction — `none` refuses to start on a non-loopback
  bind unless `KLANGK_ALLOW_INSECURE_NO_AUTH=1` — but it is a behavior change
  on upgrade: **set `KLANGK_AUTH_MODES=password` (or `oidc`/`both`) explicitly
  before redeploying if you relied on the old default.** Note: `none` mode is
  not yet supported with the published Docker host image (a published port
  isn't loopback) — the Docker examples set `KLANGK_AUTH_MODES=password`; see
  #1391.
- **OIDC settings no longer change the auth mode (#1419).** Previously, when
  `KLANGK_AUTH_MODES` was unset **and** an OIDC provider was configured, the
  resolved default was silently promoted to `both` (the "OIDC turns auth on"
  rule). That promotion is removed: the unset default is now **always `none`**,
  regardless of OIDC config, and `KLANGK_OIDC_*` settings only take effect
  once the mode is explicitly `oidc` or `both`. **If you relied on OIDC being
  configured implying `both`, set `KLANGK_AUTH_MODES=oidc` (or `both`)
  explicitly before redeploying** — otherwise your server will boot in `none`
  mode (no-login single-user, loopback-bound; safe by construction, but not
  your intended multi-user posture).
- **uvicorn now binds `127.0.0.1` by default** instead of `0.0.0.0`
  (`KLANGK_LISTEN`, new). Workspace containers could previously reach the
  backend directly via `host.containers.internal:$KLANGK_PORT`, bypassing nginx
  and therefore every per-location nginx ACL. nginx remains bound to `0.0.0.0`
  (container-reachable, so hosted apps and remote browsers still work) and
  proxies to uvicorn on the loopback address. Operators who reach the backend
  directly —
  bypassing nginx — must set `KLANGK_LISTEN=0.0.0.0` to restore the old
  behavior. Applies to both the devenv dev server and the host container.
  (#1375)
- **`klangkc invite` moved under the `admin` group** (#1374). The top-level
  `klangkc invite <email>` command is gone, with no backward-compat alias.
  Use `klangkc admin invitations send <email>` (and list with
  `klangkc admin invitations ls`). Site-wide administration — users and
  invitations — now has a dedicated `admin` CLI surface matching the
  `terminal`/`volumes` noun-subgroup convention.
- **`klangkd` binds a UDS; `scripts/nginx.sh` retired** (#1396). uvicorn now
  binds a UNIX domain socket (`$KLANGK_STATE_DIR/klangk.sock`) instead of a
  TCP port when launched via `klangkd` (dev and host container). nginx config
  is rendered by Python (`klangk_backend.nginx`) and nginx is owned as a
  child process of `klangkd`'s lifespan. uvicorn has **no TCP listener in any
  mode** — it is reachable only via the socket, which only same-uid processes
  can open. `scripts/nginx.sh`, the `klangk-resolve-value` console script,
  and the `/home/klangk/bin/nginx` shim are removed. The host container no
  longer publishes `KLANGK_PORT` (8997) — only `KLANGK_NGINX_PORT` (8995).
  `KLANGK_PORT`/`KLANGK_LISTEN` are retained for tests that launch uvicorn
  over TCP directly but are unused under `klangkd`.

### Security

- **nginx now denies container source IPs by default on the catch-all
  `location /`** (#1376). Previously the catch-all was open to container
  source IPs (the host's own IP via pasta NAT), so safety relied on every
  backend endpoint remembering its `Depends(auth)` — a single forgotten
  dependency silently exposed that endpoint to a workspace container's API
  brute-force sweep. nginx now denies the container source subnets on the
  catch-all, so a container can reach only the three endpoints it is known to
  need (`/llm-proxy/`, `/api/v1/browser-delegate`,
  `/api/v1/workspaces/post-chat-message`); every other path is refused at
  nginx with 403. Loopback (local browsers) and other IPs (remote browsers)
  are unaffected. The three container endpoints keep their existing allowlist +
  workspace-token `auth_request`.
