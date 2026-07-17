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

- **Packaged `klangkd` now ships and serves the web UI (#1600).** The
  compiled Flutter web build is `force-include`d into the `klangk` wheel at
  `klangk/frontend/`, and the `frontend_dir` default now resolves to that
  in-package location — so `pip install klangk` serves the UI out of the box
  with no checkout or separate build. A missing wheel artifact fails the
  wheel build at hatchling time (`Forced include not found`). When the
  resolved `frontend_dir` is absent at startup, `klangkd` now logs a warning
  instead of silently serving an API-only app. Source-tree deployments
  (devenv, the host container) set `KLANGK_FRONTEND_DIR` to the repo's
  `src/frontend/build/web`. See [Packaged klangkd](../deployment/packaged.md).

- **Option to require consent banner acceptance on every visit (#1544).**
  New setting `login_banner_every_visit` / `KLANGK_LOGIN_BANNER_EVERY_VISIT`
  (default `false`, surfaced on `GET /api/v1/config`). When `true`, the
  login/consent banner must be re-accepted on every fresh app load / login
  — acceptance is held for the session only (in-memory), never persisted.
  When `false` (default), behavior is unchanged: acceptance is cached
  permanently against the banner text hash.

- **`KLANGK_EGRESS_LISTEN`** — the interface nginx binds for the container-
  egress listener, rendered as `listen {egress_listen}:{egress_port};`.
  Defaults to `0.0.0.0` (all interfaces), the only value portable across
  podman network modes — `host.containers.internal` resolves to a netavark/
  pasta virtual gateway that isn't bindable, and the real interface container
  traffic lands on is environment-specific. The all-interfaces bind is gated
  by `CONTAINER_ACL` (deny-all → 403 outside the container subnet) plus the
  `auth_request` workspace-token gate (→ 401 without a valid JWT); pin to a
  specific host IP to tighten further (#1542).

- **`KLANGK_EGRESS_PORT`** — a dedicated container-egress port nginx listens
  on for container→backend traffic (`/llm-proxy`, `/api/v1/browser-delegate`,
  `/api/v1/workspaces/post-chat-message`). Default `8995`. Served in both
  headless and full/browser modes (#1542).

- **`KLANGK_SOCKET`** — the backend UDS path `klangkd` binds. Defaults to
  `<state_dir>/klangk.sock`; override when the default overflows the
  `AF_UNIX` `sun_path` limit. A resolved path exceeding 104 chars fails at
  construction with a diagnostic directing the deployer to shorten
  `KLANGK_SOCKET` or move `KLANGK_STATE_DIR` shallower (#1531, #1542).

- **Config-file keys accept `snake_case` _and_ `kebab-case`:** every
  `klangkd` config-file key may now be written in either form (`jwt_secret`
  or `jwt-secret`, `egress_port` or `egress-port`, etc.) and resolves to the
  same setting. Generalizes the dual-form lookup the OIDC provider dicts
  already had to the whole config file; `snake_case` remains the
  preferred/documented form (#1538).

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
- **klangk nginx now rewrites `$remote_addr` to the real client IP** via the
  realip module (`set_real_ip_from <each KLANGK_TRUSTED_PROXY_CIDRS entry>` +
  `real_ip_header X-Forwarded-For` + `real_ip_recursive on`). Without this,
  `proxy_set_header X-Real-IP $remote_addr` clobbered the real client IP the
  outer proxy forwarded with the proxy's own IP, so the backend's
  `client_is_loopback` / `derive_hosting_info` resolved the proxy IP, not the
  browser's — a regression from stable/1.0 where the customer proxy hit
  uvicorn directly. Suppressed entirely when `KLANGK_REJECT_PROXY_HEADERS` is
  set (#1558).

### Changed

- **`frontend_dir` default moved in-package (#1600).** The default changed
  from the repo-relative `src/frontend/build/web` (which only worked under an
  editable install) to the in-package `klangk/frontend/` shipped in the
  wheel. Source-tree deployments that relied on the old default must now set
  `KLANGK_FRONTEND_DIR` (devenv and the host container already do); packaged
  installs need no action.

- **SIGHUP now reloads configuration (#1587).** Sending `SIGHUP` to
  `klangkd` re-resolves `KlangkSettings` from the environment / YAML
  config file and applies the new values before recycling the runtime.
  Invalid config denies the restart (runtime left on last-known-good,
  reason logged at `ERROR`). Settings bound for the process lifetime
  (`KLANGK_PORT`, `KLANGK_LISTEN`, `KLANGK_DATA_DIR`, `KLANGK_STATE_DIR`)
  are warned but require a full restart to apply. See
  [Process Signals](deployment/signals.md).

- **`KLANGK_CORS_ORIGINS` and `KLANGK_FRONTEND_DIR` are now reloadable
  on SIGHUP (#1610).** CORS origins are served by a live middleware that
  re-reads `KLANGK_CORS_ORIGINS` after every settings swap. A changed
  `KLANGK_FRONTEND_DIR` remounts the Flutter static-files directory
  without a process restart.

- **`KLANGK_PORT` is now the nginx browser port, not uvicorn's bind.** Under
  `klangkd` uvicorn always binds the UDS (`KLANGK_SOCKET`); `KLANGK_PORT` is
  the nginx listener for the browser UI + API + hosted apps. **Unset ⇒
  headless mode** (no browser listener; only the container-egress listener on
  `KLANGK_EGRESS_PORT` is served). Set ⇒ full/browser mode. Suggested value
  `8997` (#1542).

- **`KLANGK_LISTEN` is now a plain browser-interface address** (default
  `127.0.0.1`), rendered as `listen {KLANGK_LISTEN}:{KLANGK_PORT};` only in
  full/browser mode. The polymorphic socket-path meaning is retired (it never
  shipped in a release); the UDS path is now `KLANGK_SOCKET` (#1542).

- **nginx now listens on two separate ports in full/browser mode** — the
  browser listener (`KLANGK_LISTEN`:`KLANGK_PORT`) and the container-egress
  listener (`KLANGK_EGRESS_PORT`) — so ingress and egress traffic can be
  firewalled independently. `KLANGK_EGRESS_PORT` must differ from
  `KLANGK_PORT` (#1542).

- **`KLANGK_FRONTEND_DIR` setting (#1456):** the built Flutter Web UI is
  served from `settings.frontend_dir` (defaults to the repo-relative
  `src/frontend/build/web` computed in `KlangkSettings`; `klangkd`
  deployments override it). Previously the path was hardcoded in `build_app`,
  so installed-package deployments silently skipped mounting the UI.

### Deprecated

- **`KLANGK_NGINX_PORT`** is deprecated; rename to `KLANGK_EGRESS_PORT`. If
  `KLANGK_EGRESS_PORT` is unset, the `KLANGK_NGINX_PORT` value is used as the
  egress port (with a deprecation warning); if both are set,
  `KLANGK_EGRESS_PORT` wins and `KLANGK_NGINX_PORT` is ignored. A future
  release will stop recognizing it (#1542).

### Removed

- **`instance_metadata` DB table / DB-stored instance ID:** the instance
  ID is now a single line of text in `<data_dir>/instance-id`, not a row in
  SQLite. The file lives in `data_dir` (next to `klangk.db`) because it
  _identifies the data_ — its lifetime is tied to the data, not to a process
  run, so it does not belong alongside the per-process PID file / UDS socket
  in `state_dir`. The `instance_metadata` table, the `model/instance.py` module,
  and the `resolve_instance_id_sync()` DB-opening helper are gone; there is
  no migration path (no existing installs). Instance identity is owned by
  `Util` (`app.state.util`): `resolve_instance_id()` writes the file at
  startup, `instance_id()` returns it using the same settings instance as
  every other config-backed helper — no module-level cache/global (#1553).

- **`klangk-instance-id` console script:** the entry point and its
  `_instance_id.py` module are gone. Now that the ID is a file at a fixed
  name (`instance-id`) under `<data_dir>`, every caller reads it directly
  (`Path(data_dir) / "instance-id"`) instead of shelling out to a process
  whose only job was to print that file's contents. The `_ShimAppState`
  fake-`app.state` it needed to reproduce path resolution goes with it
  (#1565).

- **In-container guards on container cleanup:** the
  `/.dockerenv` / `/run/.containerenv` early-return checks in
  `reap_instance_containers()` and `shutdown()` are gone. Both operations are
  scoped by the `klangk.instance` label filter, which already excludes any
  container this klangkd didn't create (unrelated host containers, or
  containers created by an outer klangkd with a different instance ID), so
  the guards protected against an impossible case. A side effect was that
  8 container-cleanup logic tests failed whenever pytest ran inside a
  container (distrobox, CI-in-docker, klangk-in-klangk); the suite is now
  portable across host environments with no test-side patching (#1556).
- **devenv `klangk:kill-containers` task and `scripts.kill-containers`:**
  klangkd now reaps its own instance's leftover containers at startup
  (in `reap_instance_containers`, immediately after `prewarm_podman`),
  removing the need for devenv to shell out to `klangk-instance-id` +
  `podman rm -f` before the backend process starts. The kill now happens
  in every deployment shape (systemd, host-container, bare `klangkd`),
  not just under devenv (#1554).
- **`adopt_orphaned_containers` → `reap_instance_containers`:** the old
  method was effectively a startup reap already (the in-memory registry is
  empty at startup, so every leftover was "untracked" and removed). Renamed
  to reflect what it actually does and dropped the dead tracked-skip branch;
  added the in-container guard (skip when klangkd itself runs in a
  container) (#1554).
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

- **One `klangk` distribution ships the renamed server package `klangkd` and the folded-in client `klangkc` (#1606).** The backend package is renamed `klangk_backend` → `klangkd` and the standalone `klangkc` distribution is retired — the client is promoted to a sibling top-level package under the same source root. One `pip install klangk` yields both `klangkd` (server) and `klangkc` (client); the entrypoint command names are unchanged. The distribution name (`klangk`) is distinct from the import packages (`klangkd` / `klangkc`), like `python-dateutil` → `dateutil`.
  - **Integrators** who `import klangk_backend` (e.g. OIDC login hooks) must update to `import klangkd`.
  - **The `klangkc` PyPI distribution is retired** in favor of `klangk`; the `cli-v*` tag line and `cli-publish.yml` workflow are removed. Both binaries release together off the single `v*` tag line.
  - **Test layout**: tests are split into per-package suites — `src/klangk/klangkd-tests/{tests,e2e-tests}` (server) and `src/klangk/klangkc-tests/{tests,e2e-tests}` (client) — as hyphenated siblings of the package dirs so they don't ship in the wheel. Both unit suites share one `--cov=klangkd --cov=klangkc` 100% gate (run together via `test-backend`).

- **The listen/port settings model is restructured** (#1542):
  - `KLANGK_NGINX_PORT` → rename to `KLANGK_EGRESS_PORT` (deprecated alias
    accepted this release with a warning).
  - `KLANGK_PORT` changes meaning from uvicorn's bind to the nginx browser
    port. Operators who set `KLANGK_PORT` on the old assumption it was the
    (dead) uvicorn bind should review: unset it for headless, or set it to
    the desired browser port.
  - `KLANGK_LISTEN`'s default is `127.0.0.1` (was polymorphic/unused). The
    socket-path meaning never shipped in a release.
  - The host container (`Dockerfile`) now sets `KLANGK_PORT=8997`,
    `KLANGK_EGRESS_PORT=8995`, and publishes both ports (was
    `KLANGK_NGINX_PORT` + one published port).

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

### Fixed

- **The browser-listener container-source deny no longer false-positives
  behind a trusted proxy co-located on klangk's host** (#1546). The
  `location /` deny (#1376) was an inline `deny <ip>; allow all;` list,
  which nginx evaluates against `$remote_addr`. After #1560's realip
  directives rewrite `$remote_addr` to the `X-Forwarded-For` client, a
  trusted outer proxy running on the same host as klangk (whose forwarded
  real client is a host interface IP, e.g. a `10.100.0.0/24` bridge) made
  every proxied browser request land on a denied host IP → 403 for the whole
  UI/API. The deny is now a `geo $realip_remote_addr $container_source { … }`
  block + `if ($container_source) { return 403; }` on the catch-all, keyed on
  the _immediate_ TCP peer (`$realip_remote_addr`, pre-realip) instead of the
  rewritten real client. So: a container connecting directly via pasta NAT is
  still denied (brute-force cap intact); a request through a trusted proxy is
  let through (its peer is the proxy, not a container source) while
  `X-Real-IP`/`X-Forwarded-For` still carry the real client to the backend.
  An upstream proxy on the same host now works out of the box — no
  `KLANGK_CONTAINER_SUBNETS` escape hatch needed.

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
