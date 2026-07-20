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

- **The CLI now defaults to a co-located `klangkd`'s UDS when no server is
  configured (#1676).** When neither `--server` nor an `active-server` in
  CLI state is set, `klangk` falls back to the default Unix socket a
  same-host `klangkd` binds — `$KLANGK_SOCKET` (plain absolute path),
  `$KLANGK_STATE_DIR/klangk.sock`, or `$XDG_STATE_HOME/klangkd/klangk.sock`
  (typically `~/.local/state/klangkd/klangk.sock`) — but only if that
  socket exists. A single-host `klangkd` + `klangk` now "just works" with
  no prior `klangk login`; hosts with no `klangkd` running keep the
  existing "No server configured" error, and a _stale_ socket (a `klangkd`
  that crashed without unlinking it) now reports "Cannot connect to
  klangkd at <path>" instead of the misleading "Not logged in".
  Operators who relocate the socket via a `file:`/`cmd:` `KLANGK_SOCKET`
  indirection still need a one-time `klangk login` (the CLI can't run the
  cmd / read the file client-side).

- **Soliplex ships as a compiled-in (dormant) feature of the default wheel
  (#1664).** The Soliplex knowledge-base plugin
  (`soliplex/klangk-plugin-soliplex`, maintained by the Soliplex org) is now
  declared in the checked-in `plugins.yaml` as a remote `git:` entry pinned at
  `v0.4` (`f9ad398`). A bare install compiles it in — the Dart UI + the TS
  extension land in the bundle — but it's **not** in `DEFAULT_FEATURES`, so
  on the **frontend** `KLANGK_FEATURES_ENABLE` unset leaves it inactive.
  Operators running a Soliplex server opt in by adding `soliplex` to
  `KLANGK_FEATURES_ENABLE` (composed with the stock set — the canonical
  activation semantics make an explicit value the **exact** active list, not
  additive) instead of forking the repo and rebuilding. This is the first
  real exercise of the "compiled-in ⊋ defaults" design from #1655
  (compiled-in = 8, defaults = 7). Its one config key (`SOLIPLEX_URL`, scope
  `frontend`) is bridged via `/api/v1/config` when active; no
  `container_env_keys` (browser-side feature). `update_plugins.py` gained a
  `--local-only` flag for the scripts test suite (which doesn't have network
  access) to verify the local-plugin contract without cloning — the real
  build (`flutterbuildweb.sh`, `build-workspace-image.sh`) still fetches
  soliplex normally. **Known limitation:** dormancy governs the frontend only;
  the workspace container bundles every compiled-in plugin's `extension.ts`
  and Pi loads them unconditionally, so soliplex's `soliplex_*` tools appear
  in every workspace pi's tool list regardless of `KLANGK_FEATURES_ENABLE`
  (they self-no-op when no Soliplex server is reachable). Workspace-side
  gating is a follow-up.

- **First-run config generation: a bare `klangkd` boots with no config
  file (#1645).** When `klangkd` is invoked with no `--config` and no
  `klangkd.yaml` exists at the resolved path (`$KLANGK_CONFIG_DIR/klangkd.yaml`,
  default `~/.config/klangkd/klangkd.yaml`), a near-empty template is generated
  pointing at the solo docs (#1629) with commented examples for the mode
  transitions. No admin identity or password is emitted — the admin row is
  seeded at runtime: `default_user` defaults to `<unixuser>@example.com`
  (derived from `getpass.getuser()`), with `password_hash=None` in `none`/`oidc`
  mode (the row is load-bearing for `/auth/local` token minting but no
  endpoint checks the hash). `password`/`both` mode requires
  `KLANGK_DEFAULT_PASSWORD` (fail-fast if unset — auto-generate-and-print was
  removed as a lockout footgun for detached deployments). Combined with the
  wheel publish (#1656), `pip install klangkd && klangkd` yields a usable
  solo instance with no config file and no password. The `--config` default
  changed from `/etc/klangkd.yaml` to the XDG config dir — the host container
  and devenv both pass `--config=none` explicitly, so existing deployments are
  unaffected. Existing `klangkd.yaml` files are never overwritten.

- **The `klangk` wheel is now published to PyPI on tag push (#1656).**
  `release.yml` gains a parallel `build-wheel` job that builds the frontend
  (default plugin set from the checked-in `plugins.yaml`) and produces the
  wheel via `scripts/build_wheel.sh`, then publishes it via **trusted
  publishing (OIDC)** — `pypa/gh-action-pypi-publish@release/v1` with
  `permissions.id-token: write` and `environment: pypi`, no API token secret
  (same shape as the deleted `cli-publish.yml` pre-#1606). `pip install
klangk==<tag>` now yields a working `klangkd` with the UI served from the
  in-wheel `klangk/frontend/`. This is the release artifact the pip/uv
  first-run UX (#1607 / #1645) was designed for. Requires a one-time
  trusted-publisher config on the `klangk` PyPI project bound to this repo /
  workflow / environment.

- **Build-pipeline integration tests for the plugin build path (#1666).**
  `scripts/tests/test_build_pipeline.py` runs the real
  `update_plugins.py` + `import_dart_plugins.py` against the checked-in
  `plugins.yaml` and real `plugins/` trees, then asserts on the outputs:
  every declared plugin materializes, the generated Dart aggregator imports
  the expected class names, `features.json` satisfies the runtime's
  manifest contract (shape, scopes, `defaults` / `container_env_keys`
  invariants), and the 7-on-disk / 4-Dart asymmetry is locked. The suite
  runs in `test-backend` CI (broadened path-filter to include `scripts/`,
  `plugins/`, `plugins.yaml`). A frontend e2e spec
  (`src/frontend/e2e-tests/e2e/features.spec.ts`) also asserts the built
  `features.json` is served at `/features.json` and `/api/v1/config`
  surfaces the frontend-scope keys — proving the build → serve → API chain
  end-to-end against a real booted `klangkd`.

- **Feature manifest (`features.json`) + per-deploy activation
  (`KLANGK_FEATURES_ENABLE`) (#1655).** The build emits a single
  `features.json` into the frontend bundle directory (next to `index.html`)
  carrying every compiled-in feature's metadata + a `defaults` list + the
  container-scope env keys. The frontend reads its sibling file for
  per-feature metadata and (when `KLANGK_FEATURES_ENABLE` is unset) the
  stock default-on set. `KLANGK_FEATURES_ENABLE` (comma-separated feature
  names, canonical semantics — any explicit value is **exactly** that list,
  nothing implied; unset → manifest `defaults`; no `*` form) is forwarded via
  `/api/config`, and `main.dart` filters `createAllPlugins()` against the
  active set before registration — a shipped-but-inactive feature's Dart is
  in the monolithic bundle but inert (no app-bar icon, overlay, routes, or
  dispatched tools). This is what lets a single-client feature ship dormant
  in every wheel and turn on only where wanted — no fork, no custom tag,
  no rebuild. Activation is wheel-side only for now; the workspace side
  (TS extensions + tools) stays always-on by design (deferred as future
  work). Compiled-in set ⊇ `defaults` deliberately — the delta is the
  dormant-on-stock-deliver features.

- **`KLANGK_CONFIG_DIR` is the config-tree root (#1649).** The single
  overridable knob for user-edited, durable config paths — the config-tree
  analogue of `KLANGK_STATE_DIR`. Defaults to `$XDG_CONFIG_HOME/klangk` (→
  `~/.config/klangk`, incl. macOS when the var is unset); `KLANGK_CUSTOMIZE_DIR`
  derives from the resolved `config_dir` (like `KLANGK_DATA_DIR` derives from
  `state_dir`). Set this to relocate the config tree with one var instead of
  setting the sub-dir var; `KLANGK_CUSTOMIZE_DIR` still wins over the
  derivation. `KLANGK_PLUGINS_DIR` is **not** a `config_dir` child (its tree
  placement is reworked separately in #1651). No behavior change for
  operators not setting it (the default reproduces the previous inline
  `$XDG_CONFIG_HOME/klangk` root exactly).

- **`KLANGK_CADDY_ADMIN_SOCKET` overrides the Caddy engine's admin-API
  socket path (#1636).** The admin UDS was hardcoded to
  `<state_dir>/caddy-admin.sock` with no override and no length check; a deep
  `KLANGK_STATE_DIR` could push it over the portable `AF_UNIX` `sun_path`
  bound (≤104 chars) and make the Caddy engine unstartable (the admin UDS is
  its only config-delivery path). The new setting mirrors the backend-UDS
  `KLANGK_SOCKET` escape hatch, and the existing length validator now covers
  **both** socket paths — a too-long either one fails at construction with a
  diagnostic naming the offending variable, regardless of engine. Unused by
  the nginx engine.

- **Caddy reverse-proxy engine behind `KLANGK_PROXY_ENGINE=caddy` (#1559).**
  A second, opt-in proxy engine joins the default nginx one: `klangkd`
  renders a **Caddyfile** and pushes it to Caddy's **admin API** over a
  `klangkd`-owned Unix domain socket (`<state_dir>/caddy-admin.sock`, mode
  `0600`) via `POST /load` (`text/caddyfile`) — no on-disk config source of
  truth. Caddy is bootstrapped with `CADDY_ADMIN=unix//…|0600` (empty config
  pinned to `/dev/null`), so the admin endpoint is reachable only by
  `klangkd`. A SIGHUP settings change re-pushes the Caddyfile over the admin
  API (the nginx engine, by contrast, stays stale until a full restart).
  Both engines cover the same surface (two listeners, token gate via
  `forward_auth`/`auth_request`, container-source `remote_ip` matchers,
  body-size limits, UDS upstream, injected LLM `Authorization`, exact-match
  `/auth/local` + `/post-chat-message`, `/llm-proxy/` prefix strip). The
  engine is selected once at process start (restart required to change it —
  SIGHUP logs a non-reloadable warning); the default stays `nginx` until the
  cutover. `caddy` is added to the devenv shell; stock Caddy (no plugins)
  suffices — `caddy-l4` and `fastcaddy` are explicitly out of scope.

- **Handles accepted at login and user-lookup surfaces (#616).** The
  `POST /auth/login` request body is renamed `email` → `identifier` and
  now accepts a **handle** as well as an email everywhere a user is
  identified: the `klangkd` web login page (field relabeled "Email or
  handle", validator no longer requires `@`) and the `klangkc` commands
  `login`, `admin users set-password`, `share`, and `unshare`. Resolution
  dispatches on whether the identifier contains `@` (emails always do,
  handles never do — disjoint namespaces). Login brute-force lockout is
  now keyed on the resolved user's canonical email, so handle and email
  attempts against one account share a single counter. `GET /users/search`
  matches an email **or** handle prefix. Registration and `admin
invitations send` stay email-only (a deliverable address is required);
  `resend-verification` keeps its email-based body (it targets an email
  address). `admin invitations send`'s arg help documents this.

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

- **`KLANGK_LOG_LEVEL` — centralized, settings-driven logging (#1467).**
  Logging is no longer configured as an import-time side-effect of
  `klangk.main` (the `logging.basicConfig(...)` call is gone). It is now
  configured by a dedicated module, `klangk.logger`, with two phases:
  sensible defaults (INFO level, the pre-refactor colored console format,
  and central silencing of chatty third-party loggers) are applied at import,
  so logging is formatted from the very first log call — including during
  `KlangkSettings` construction, which runs before any `app` exists; then
  `build_app()` re-applies the level from the new `log_level` setting
  (`KLANGK_LOG_LEVEL`, default `INFO`; accepts a level name like
  `DEBUG`/`WARNING`/`ERROR`/`CRITICAL` in any case, or a numeric value, and
  rejects garbage at boot). The level is re-applied on a SIGHUP reload (after
  the settings swap, before the subsystem reconfigure loop), so
  `KLANGK_LOG_LEVEL` takes effect without a process restart. Chatty
  third-party loggers (`uvicorn.access`, `sqlalchemy.engine`, `httpx`,
  `httpcore`, `watchfiles`, `asyncio`) are silenced centrally to `WARNING`.

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
- **CLI transport resolver:** `klangk --server` now accepts a Unix socket
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
  (no nginx proxy) as loopback, so `klangk login /path/to/sock` works in
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

- **CLI config/state files renamed + relocated onto the XDG trees; server's
  XDG subdir is now `klangkd` (#1646).** The CLI's two files move:
  `cli.yaml` → `~/.config/klangk/klangk.yaml` (read via the `XDG_CONFIG_HOME`
  var with the documented fallback, was hardcoded) and `state.yaml` →
  `~/.local/state/klangk/klangk-state.yaml` (state, not config — was jammed
  into the config tree). The `~/.klangk-ssh-agent.log` debug log moves to
  `~/.local/state/klangk/klangk-ssh-agent.log` (no longer pollutes `$HOME`).
  The server's XDG subdir changed from `klangk` to `klangkd` (the binary
  name) — distinct from the CLI's `klangk` tree. Different audiences,
  different shapes: the server's state is GB-scale DBs + UDS, the CLI's is
  a few hundred bytes of user tokens; splitting at the filesystem level
  mirrors the code-level isolation rule. New default paths:
  server `~/.config/klangkd/klangkd.yaml` + `~/.local/state/klangkd/`;
  CLI `~/.config/klangk/klangk.yaml` + `~/.local/state/klangk/klangk-state.yaml`.
  **Breaking** (no migration shim): this lands before there's a deployed
  user base with on-disk state worth preserving (#1656 wheel publish just
  landed; #1670 first-run generation is the first time a config file even
  exists). Existing dev installs need manual relocation:
  - CLI config: rename `~/.config/klangk/cli.yaml` → `klangk.yaml`.
  - CLI state: move `~/.config/klangk/state.yaml` → `~/.local/state/klangk/klangk-state.yaml`
    (cached tokens; or just `klangk login` again).
  - Server DB: move `~/.local/state/klangk/data/` → `~/.local/state/klangkd/data/`
    (if running the server outside devenv / the host container, which pin
    `KLANGK_STATE_DIR` explicitly and are unaffected).
    CI doesn't set either env var and runs hermetically, so it's unaffected.

- **The pytest toolchain is now an optional `test` extra, not a runtime
  dependency (#1673).** `src/klangk/pyproject.toml` moves `pytest`,
  `pytest-asyncio`, `pytest-cov`, `pytest-xdist`, and `pytest-timeout` out
  of `dependencies` into `[project.optional-dependencies] test`. A plain
  `pip install klangk` (or `pip install klangk==<tag>` from PyPI) no longer
  pulls in pytest + its transitive deps (pluggy, iniconfig, packaging,
  coverage, execnet — ~a dozen packages / several MB with no runtime role).
  Dev and CI installs opt in explicitly: `pip install klangk[test]`, or
  `uv sync --extra test` (the path the devenv shell and `backend-tests.yml`
  now use). **Integrator action:** if you install `klangk` into an env where
  you also run the test suite, add the `[test]` extra.

- **`KLANGK_PLUGINS_DIR` is gone from every layer (#1660).** The plugin
  declaration list is now the checked-in `plugins.yaml` at the repo root
  (the build-time source of truth, analogue of a committed `package.json` /
  `Cargo.toml`). The materialized payload — fetched/symlinked plugin trees,
  `plugins.lock`, the generated `klangk_plugins` Dart package — is a
  throwaway `mktemp -d` each build script (`flutterbuildweb.sh`,
  `build-workspace-image.sh`, `build-host-image.sh`) owns and cleans up on
  exit. `update_plugins.py` and `import_dart_plugins.py` take the payload
  dir via `--payload-dir` instead of reading `KLANGK_PLUGINS_DIR` from the
  environment. The host image no longer copies plugin trees in (the runtime
  reads `features.json` from the frontend build, not on-disk `package.json`
  files — the workspace image still bakes them in for Pi). The first-run
  `plugins.yaml` template-creation bootstrap is removed; the file is
  source-controlled. Operators who overrode `KLANGK_PLUGINS_DIR` to point
  at a custom declaration should instead edit the checked-in `plugins.yaml`
  (or, for `customize/build/build.sh`, the build script overwrites the
  clone's `plugins.yaml` with `customize/build/plugins.yaml`).

- **`KLANGK_STATE_DIR` now defaults to `$XDG_STATE_HOME/klangk` (#1644).**
  The runtime-state directory (UDS socket, rendered proxy config, pid file,
  DB) defaults to `~/.local/state/klangk` when no explicit value is supplied,
  so `pip install klangkd && klangkd` no longer hard-requires an operator to
  set it. Explicit `KLANGK_STATE_DIR` / config-file values still win (devenv,
  the host container, and production operators who pin it are unaffected).
  `KLANGK_DATA_DIR` derives from `state_dir` as before, so it picks up the
  default too. Construction still fails fast in the genuinely-unconfigured
  case (neither `$XDG_STATE_HOME` nor `$HOME` set), preserving the #1461
  intent. The cross-platform XDG fallback applies on macOS too (vars unset →
  `~/.local/state`).

- **Proxy terminology replaces nginx in code, docs, and env vars (#1430).**
  The reverse proxy klangkd owns and supervises is referred to as "the proxy"
  throughout the codebase rather than by the underlying implementation
  (currently nginx). Renames: the `klangk.nginx` module is now `klangk.proxy`
  (`NginxRenderer`/`NginxWatchdog` → `ProxyRenderer`/`ProxyWatchdog`); the
  settings fields `nginx_bin`/`nginx_port` → `proxy_bin`/`proxy_port` (env
  `KLANGK_NGINX_BIN`/`KLANGK_NGINX_PORT` → `KLANGK_PROXY_BIN`/`KLANGK_PROXY_PORT`);
  `app.state.nginx_watchdog` → `app.state.proxy_watchdog`; the internal
  `_KLANGK_DISABLE_NGINX` test kill switch → `_KLANGK_DISABLE_PROXY`; and the
  `test_nginx*.py` suites → `test_proxy*.py`. The actual `nginx` binary,
  rendered `nginx.conf`, and nginx packages are unchanged — the proxy is still
  implemented with nginx. Operators using `KLANGK_NGINX_BIN` or
  `KLANGK_NGINX_PORT` must rename them to `KLANGK_PROXY_*`.

- **CLI command renamed `klangkc` → `klangk` (#1615).** One `pip install
klangk` now yields `klangk` (client) and `klangkd` (server), matching the
  unified distribution name. The `klangkc` entrypoint is removed; the Typer
  app name, all help/error text, docs, demo scripts, and backend comment
  references are updated to the new name. The Python module (`klangk.cli`)
  and the `klangkc-tests` test directory are unchanged.

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

- **`KLANGK_PROXY_PORT`** is deprecated; rename to `KLANGK_EGRESS_PORT`. If
  `KLANGK_EGRESS_PORT` is unset, the `KLANGK_PROXY_PORT` value is used as the
  egress port (with a deprecation warning); if both are set,
  `KLANGK_EGRESS_PORT` wins and `KLANGK_PROXY_PORT` is ignored. A future
  release will stop recognizing it (#1542, #1430). Renamed from
  `KLANGK_NGINX_PORT` in #1430; the old `KLANGK_NGINX_PORT` name is no longer
  recognized.

### Removed

- **The `@demigodmode/pi-web-agent` Pi extension is no longer installed
  in the workspace image (#1689).** The workspace Dockerfile previously ran
  `pi install npm:@demigodmode/pi-web-agent@1.5.0` alongside the global
  `@earendil-works/pi-coding-agent` install; that step is gone, and the
  extension is no longer listed among the pre-installed Pi extensions in
  the docs. `@earendil-works/pi-coding-agent` is unchanged. Users who want
  the web-agent UI can still `pi install` it at runtime.

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
  is set. The CLI (`klangk`) auto-logs in on first command run with no prior
  `klangk login`; the server's auth mode is probed live (not cached) so a
  mode switch takes effect immediately. See [Auth Modes](features/auth-modes.md)
  for the full mode-switching guide.
- **`klangk admin` command group** (#1374): site-wide administration now
  has a dedicated CLI surface — `admin users ls`, `admin users
set-password <email>` (set a known password for the default user — whose
  password is random unless `KLANGK_DEFAULT_PASSWORD` was set — before
  flipping `none` -> `password`), and `admin invitations send/ls`. The
  top-level `invite`/`invitations` commands moved under `admin invitations`.
- **`klangk status`** now reports your user id and admin status (derived
  from `/my-permissions`).
- **`KLANGKC_DEBUG_SSH_AGENT` env var (#1522):** the debug-only knob that
  enabled verbose `[ssh-agent]` logging on the backend (`SshAgentForwarder`)
  and CLI (the local agent relay) is gone, along with the `log_stderr()`
  socat-stderr relay it spawned and the `~/.local/state/klangk/klangk-ssh-agent.log`
  file handler the CLI wrote. The SSH agent forwarding feature itself is
  unchanged; only the debug scaffolding is removed. The name was also wrong
  (`KLANGKC_` is the CLI prefix, but the backend read it too).

### Breaking

- **`KLANGK_PLUGINS_DIR` retires as a runtime setting (#1655).** The
  runtime no longer scans `$KLANGK_PLUGINS_DIR/*/package.json` for plugin
  config — that presumed materialized source trees on the klangkd host,
  which pip/uv installs never have. The server now reads the build-emitted
  `features.json` (one field — `container_env_keys` — to bridge container
  env vars; the frontend reads the rest). `KLANGK_PLUGINS_DIR` is **removed
  from `KlangkSettings`** with no successor; it stays as a **build-time-only**
  env var consumed by `update_plugins.py` and the image-build scripts (read
  from `os.environ`, not via settings). Operators who set it expecting the
  server to scan it: the server no longer scans anything; the shipped
  `features.json` is the whole runtime truth.

- **`KLANGK_CUSTOMIZE_DIR` relocates from the state tree to the config
  tree (#1644).** It holds user-edited, durable intent (branding, email
  templates), so it defaults to `<config_dir>/custom` (→
  `~/.config/klangk/custom`, deriving from the new `KLANGK_CONFIG_DIR` root —
  #1649) when unset — no longer under `state_dir`. **Operators who relied on
  the old `<state_dir>/custom` default must move their contents** (or set
  `KLANGK_CUSTOMIZE_DIR` explicitly to the old path, which still works — or
  set `KLANGK_CONFIG_DIR` once to relocate it). Explicit overrides are
  unchanged; the host container and shell scripts that set this var are
  unaffected.
  `KLANGK_PLUGINS_DIR` is **not** affected by this change — it stays under
  `<state_dir>/plugins` (as on main). Its tree placement is reworked
  separately in #1651.

- **`KLANGK_PROXY_ENGINE` now defaults to `caddy` (#1634).** The Caddy
  reverse-proxy engine replaces nginx as the default. The rendered proxy
  config is delivered to Caddy's admin API over a `klangkd`-owned Unix
  domain socket (`POST /load`, `text/caddyfile`) instead of being written
  to an `nginx.conf` and applied by `nginx -c` — no on-disk source of
  truth, no reload. **Operators with no `KLANGK_PROXY_ENGINE` set switch
  engines on upgrade.** The nginx engine remains selectable this release
  via `KLANGK_PROXY_ENGINE=nginx` as the escape hatch for a Caddy
  regression — selecting it fires a deprecation warning, and it will be
  removed in a future release (#1642). If you hit a regression, set
  `KLANGK_PROXY_ENGINE=nginx` and file an issue. Otherwise, unset the
  variable (caddy is the default). `klangkd` manages the proxy config
  entirely in both engines — operators never template proxy config or run
  reloads — so the swap is transparent to anyone not overriding
  `KLANGK_PROXY_BIN`.

- **One `klangk` distribution ships the renamed server package `klangkd` and the folded-in client `klangk` (#1606).** The backend package is renamed `klangk_backend` → `klangkd` and the standalone `klangkc` distribution is retired — the client is promoted to a sibling top-level package under the same source root. One `pip install klangk` yields both `klangkd` (server) and `klangk` (client); the entrypoint command names are unchanged. The distribution name (`klangk`) is distinct from the import packages (`klangkd` / `klangk`), like `python-dateutil` → `dateutil`.
  - **Integrators** who `import klangk_backend` (e.g. OIDC login hooks) must update to `import klangkd`.
  - **The `klangkc` PyPI distribution is retired** in favor of `klangk`; the `cli-v*` tag line and `cli-publish.yml` workflow are removed. Both binaries release together off the single `v*` tag line.
  - **Test layout**: tests are split into per-package suites — `src/klangk/klangkd-tests/{tests,e2e-tests}` (server) and `src/klangk/klangkc-tests/{tests,e2e-tests}` (client) — as hyphenated siblings of the package dirs so they don't ship in the wheel. Both unit suites share one `--cov=klangkd --cov=klangk` 100% gate (run together via `test-backend`).

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
- **`klangk invite` moved under the `admin` group** (#1374). The top-level
  `klangk invite <email>` command is gone, with no backward-compat alias.
  Use `klangk admin invitations send <email>` (and list with
  `klangk admin invitations ls`). Site-wide administration — users and
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

- **Default builds skip the soliplex remote plugin (CI unblock, #1691).**
  Every PR triggering `klangk:flutter-build` was failing during
  `flutter pub get`: the soliplex plugin (#1683) pulls `soliplex_client` /
  `soliplex_agent` from `soliplex/frontend.git`, one of which depends on the
  **git** source of `ag_ui` (`ag-ui-protocol/ag-ui`) — and that repo has an
  LFS-tracked fixture (`apps/dojo/e2e/fixtures/test-image.png`) whose object
  went missing on the remote, breaking every clone's smudge filter.
  Workaround: `scripts/flutterbuildweb.sh` and
  `scripts/build-workspace-image.sh` now default to `update_plugins.py
--local-only`, which skips git-sourced plugins (records them in
  `plugins.lock` with `sha: 'skipped'`). Soliplex is dormant by default
  anyway (not in `DEFAULT_FEATURES`), so a default build produces a
  pre-#1683-equivalent bundle with no ag-ui LFS dependency. Release /
  single-client builds that need soliplex compiled in opt in with
  `KLANGK_BUILD_INCLUDE_REMOTE=1`. Proper fix is upstream (consume the
  hosted `ag_ui` from pub.dev instead of the git repo) — tracked in #1691.

- **`pip install klangk` no longer warns `typer 0.27.0 does not provide the
extra 'all'`** (#1679). The declaration was `typer[all]>=0.12.0`, but the
  `all` extra was removed from typer (its constituents `rich`, `shellingham`,
  `colorama` are now unconditional typer runtime deps — `colorama` only on
  Windows). Changed to `typer>=0.12.0`; the deps `[all]` used to pull in are
  still installed transitively, so no functionality is lost.

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

- **Admin seeding is first-boot-only: config can no longer mint or
  reset admins once an admin exists (#1622).** Previously
  `seed_default_user` ran on every boot and created a fresh admin from
  `KLANGK_DEFAULT_USER` / `KLANGK_DEFAULT_PASSWORD` whenever the configured
  email didn't match an existing user — so anyone able to edit
  `klangkd.yaml` (or set the `KLANGK_DEFAULT_*` env vars) could mint or
  reset an admin account by editing those values and restarting, bypassing
  all auth and admin-invite flows. Seeding is now gated on **`admin`-group
  emptiness**: an admin is created from `KLANGK_DEFAULT_*` only when the
  `admin` group has no members (first boot, or after every admin has been
  deleted); once at least one admin exists, startup never creates, renames,
  re-emails, or re-passwords a user regardless of `KLANGK_DEFAULT_*`. This
  also prevents lockout: editing `KLANGK_DEFAULT_USER` and restarting can
  no longer clobber the already-seeded admin's identity. To change the
  admin after first boot, use the normal in-app / `klangkc admin` paths.
  Deployers should still treat `klangkd.yaml` as sensitive (first-boot
  password, LLM keys, JWT secret), but it is no longer a standing
  admin-minting credential.

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

- **Removed unused `adm-zip` devDependency from the frontend e2e-test
  package (#2).** `adm-zip` and `@types/adm-zip` were declared in
  `src/frontend/e2e-tests/package.json` but never imported anywhere in the
  tree; dropping them eliminates the vulnerable `0.5.x` line
  (CVE-2026-39244 / GHSA-xcpc-8h2w-3j85 — crafted ZIP triggers a 4 GB
  memory allocation) flagged by Dependabot. `npm audit` now reports 0
  vulnerabilities; Playwright still compiles all 202 tests. No production
  code depended on the package.
