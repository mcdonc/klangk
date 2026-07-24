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

- **Bare `klangk` (no subcommand) launches an interactive textual TUI on a
  real terminal (#1746).** The TUI is the foundation of the terminal client:
  in-TUI login (local/password, with no-auth auto-login and OIDC hand-off to
  `klangk login`), live server switching, and a
  live workspace/container status feed over the existing WebSocket. Subcommands
  are unchanged; in non-interactive contexts (pipes, CI) bare `klangk` still
  prints help. `textual` is now a runtime dependency.

- **The `klangk` TUI now lists workspaces and manages them in-app
  (#1747).** The home screen is a two-page list (Owned by me / Shared to
  me) that refreshes from the live WebSocket status feed. Selecting a
  workspace opens a detail screen (running/health, image, command, mounts,
  env, owner) with Restart, Duplicate, and Delete actions — each guarded by
  a confirmation. The detail screen mirrors live status (running/health)
  from `container_status`/`service_health` broadcasts; Duplicate prompts
  for a new name (server requires one); Delete is a yes/no confirm that
  returns to the list. All user-facing text is rendered as rich `Text`
  (never markup-parsed), so workspace names or messages containing bracket
  characters can't crash the TUI; list/detail load errors degrade to a
  graceful message. Volume cleanup is deferred.

- **The `klangk` TUI detail screen now lists the workspace's terminals and
  lets you delete them (#1747).** The detail page enumerates the terminals
  you own (fetched over the workspace WebSocket) and adds a Delete-key
  binding to remove the selected one; the last terminal is protected, as
  in Flutter. Selecting a terminal is wired for a future `klangk shell`
  step. The workspace-list page is now titled "Klangk: Workspaces".

- **The `features_config:` block now accepts the stripped, lowercased key form
  (`soliplex_url`) in addition to the full declared name
  (`KLANGKWS_FEATURE_SOLIPLEX_URL`) (#1737).** The short form matches the key the
  frontend receives via `GET /api/v1/config`; the full name still works, and env
  (`KLANGKWS_FEATURE_*`) still wins per key — so the YAML reads the way operators
  naturally write it instead of being silently ignored.

- **Plugin-declared config values are now resolvable from `klangkd.yaml`
  (#1659).** A new `features_config:` block supplies values for the keys
  the build emits into `features.json` (`container_env_keys` + the
  per-feature `config` blocks) — a second source alongside the server's
  environment, so long-lived deploy config (OAuth client IDs, RAG
  endpoints) can live in the committed config file instead of env.
  Precedence per key: **env** > **`features_config:`** > **plugin-declared
  default**; env stays the per-invocation override, the block carries the
  durable value, the plugin default is the floor. `file:`/`cmd:` prefixes
  are honored on values in the block too (consistent with how the resolver
  treats env values); unlike top-level `KLANGK_*` fields, a bad reference
  here does not abort boot — it logs and falls through to the default
  (same as a broken env ref). The block is read at boot and on `SIGHUP`
  (reloadable). Builds on #1655's key-set bridge with no change to the
  bridge itself — only `resolve_dynamic_config`'s source set widened.
  See [Configuration File](docs/reference/klangkd-config.md).

- **The CLI now defaults to a co-located `klangkd`'s UDS when no server is
  configured (#1676).** When neither `--server` nor an `active-server` in
  CLI state is set, `klangk` falls back to the default Unix socket a
  same-host `klangkd` binds — `$KLANGKD_SOCKET` (plain absolute path),
  `$KLANGKD_STATE_DIR/klangk.sock`, or `$XDG_STATE_HOME/klangkd/klangk.sock`
  (typically `~/.local/state/klangkd/klangk.sock`) — but only if that
  socket exists. A single-host `klangkd` + `klangk` now "just works" with
  no prior `klangk login`; hosts with no `klangkd` running keep the
  existing "No server configured" error, and a _stale_ socket (a `klangkd`
  that crashed without unlinking it) now reports "Cannot connect to
  klangkd at `<path>` instead of the misleading "Not logged in".
  Operators who relocate the socket via a `file:`/`cmd:` `KLANGKD_SOCKET`
  indirection still need a one-time `klangk login` (the CLI can't run the
  cmd / read the file client-side).

- **Soliplex ships as a compiled-in (dormant) feature of the default wheel
  (#1664).** The Soliplex knowledge-base plugin
  (`soliplex/klangk-plugin-soliplex`, maintained by the Soliplex org) is now
  declared in the checked-in `plugins.yaml` as a remote `git:` entry pinned at
  `v0.4` (`f9ad398`). A bare install compiles it in — the Dart UI + the TS
  extension land in the bundle — but it's **not** in `DEFAULT_FEATURES`, so
  on the **frontend** `KLANGKD_FEATURES_ENABLE` unset leaves it inactive.
  Operators running a Soliplex server opt in by adding `soliplex` to
  `KLANGKD_FEATURES_ENABLE` (composed with the stock set — the canonical
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
  in every workspace pi's tool list regardless of `KLANGKD_FEATURES_ENABLE`
  (they self-no-op when no Soliplex server is reachable). Workspace-side
  gating is a follow-up.

- **First-run config generation: a bare `klangkd` boots with no config
  file (#1645).** When `klangkd` is invoked with no `--config` and no
  `klangkd.yaml` exists at the resolved path (`$KLANGKD_CONFIG_DIR/klangkd.yaml`,
  default `~/.config/klangkd/klangkd.yaml`), a near-empty template is generated
  pointing at the solo docs (#1629) with commented examples for the mode
  transitions. No admin identity or password is emitted — the admin row is
  seeded at runtime: `default_user` defaults to `<unixuser>@example.com`
  (derived from `getpass.getuser()`), with `password_hash=None` in `none`/`oidc`
  mode (the row is load-bearing for `/auth/local` token minting but no
  endpoint checks the hash). `password`/`both` mode requires
  `KLANGKD_DEFAULT_PASSWORD` (fail-fast if unset — auto-generate-and-print was
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
  (`KLANGKD_FEATURES_ENABLE`) (#1655).** The build emits a single
  `features.json` into the frontend bundle directory (next to `index.html`)
  carrying every compiled-in feature's metadata + a `defaults` list + the
  container-scope env keys. The frontend reads its sibling file for
  per-feature metadata and (when `KLANGKD_FEATURES_ENABLE` is unset) the
  stock default-on set. `KLANGKD_FEATURES_ENABLE` (comma-separated feature
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

- **`KLANGKD_CONFIG_DIR` is the config-tree root (#1649).** The single
  overridable knob for user-edited, durable config paths — the config-tree
  analogue of `KLANGKD_STATE_DIR`. Defaults to `$XDG_CONFIG_HOME/klangk` (→
  `~/.config/klangk`, incl. macOS when the var is unset); `KLANGKD_CUSTOMIZE_DIR`
  derives from the resolved `config_dir` (like `KLANGKD_DATA_DIR` derives from
  `state_dir`). Set this to relocate the config tree with one var instead of
  setting the sub-dir var; `KLANGKD_CUSTOMIZE_DIR` still wins over the
  derivation. `KLANGKBUILD_PLUGINS_DIR` is **not** a `config_dir` child (its tree
  placement is reworked separately in #1651). No behavior change for
  operators not setting it (the default reproduces the previous inline
  `$XDG_CONFIG_HOME/klangk` root exactly).

- **`KLANGKD_CADDY_ADMIN_SOCKET` overrides the Caddy engine's admin-API
  socket path (#1636).** The admin UDS was hardcoded to
  `<state_dir>/caddy-admin.sock` with no override and no length check; a deep
  `KLANGKD_STATE_DIR` could push it over the portable `AF_UNIX` `sun_path`
  bound (≤104 chars) and make the Caddy engine unstartable (the admin UDS is
  its only config-delivery path). The new setting mirrors the backend-UDS
  `KLANGKD_SOCKET` escape hatch, and the existing length validator now covers
  **both** socket paths — a too-long either one fails at construction with a
  diagnostic naming the offending variable, regardless of engine. Unused by
  the nginx engine.

- **Caddy reverse-proxy engine behind `KLANGKD_PROXY_ENGINE=caddy` (#1559).**
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
  (devenv, the host container) set `KLANGKD_FRONTEND_DIR` to the repo's
  `src/frontend/build/web`. See [Packaged klangkd](../deployment/packaged.md).

- **`KLANGKD_LOG_LEVEL` — centralized, settings-driven logging (#1467).**
  Logging is no longer configured as an import-time side-effect of
  `klangk.main` (the `logging.basicConfig(...)` call is gone). It is now
  configured by a dedicated module, `klangk.logger`, with two phases:
  sensible defaults (INFO level, the pre-refactor colored console format,
  and central silencing of chatty third-party loggers) are applied at import,
  so logging is formatted from the very first log call — including during
  `KlangkSettings` construction, which runs before any `app` exists; then
  `build_app()` re-applies the level from the new `log_level` setting
  (`KLANGKD_LOG_LEVEL`, default `INFO`; accepts a level name like
  `DEBUG`/`WARNING`/`ERROR`/`CRITICAL` in any case, or a numeric value, and
  rejects garbage at boot). The level is re-applied on a SIGHUP reload (after
  the settings swap, before the subsystem reconfigure loop), so
  `KLANGKD_LOG_LEVEL` takes effect without a process restart. Chatty
  third-party loggers (`uvicorn.access`, `sqlalchemy.engine`, `httpx`,
  `httpcore`, `watchfiles`, `asyncio`) are silenced centrally to `WARNING`.

- **Option to require consent banner acceptance on every visit (#1544).**
  New setting `login_banner_every_visit` / `KLANGKD_LOGIN_BANNER_EVERY_VISIT`
  (default `false`, surfaced on `GET /api/v1/config`). When `true`, the
  login/consent banner must be re-accepted on every fresh app load / login
  — acceptance is held for the session only (in-memory), never persisted.
  When `false` (default), behavior is unchanged: acceptance is cached
  permanently against the banner text hash.

- **`KLANGKD_EGRESS_LISTEN`** — the interface nginx binds for the container-
  egress listener, rendered as `listen {egress_listen}:{egress_port};`.
  Defaults to `0.0.0.0` (all interfaces), the only value portable across
  podman network modes — `host.containers.internal` resolves to a netavark/
  pasta virtual gateway that isn't bindable, and the real interface container
  traffic lands on is environment-specific. The all-interfaces bind is gated
  by `CONTAINER_ACL` (deny-all → 403 outside the container subnet) plus the
  `auth_request` workspace-token gate (→ 401 without a valid JWT); pin to a
  specific host IP to tighten further (#1542).

- **`KLANGKD_EGRESS_PORT`** — a dedicated container-egress port nginx listens
  on for container→backend traffic (`/llm-proxy`, `/api/v1/browser-delegate`,
  `/api/v1/workspaces/post-chat-message`). Default `8995`. Served in both
  headless and full/browser modes (#1542).

- **`KLANGKD_SOCKET`** — the backend UDS path `klangkd` binds. Defaults to
  `<state_dir>/klangk.sock`; override when the default overflows the
  `AF_UNIX` `sun_path` limit. A resolved path exceeding 104 chars fails at
  construction with a diagnostic directing the deployer to shorten
  `KLANGKD_SOCKET` or move `KLANGKD_STATE_DIR` shallower (#1531, #1542).

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
  `KLANGKD_STATE_DIR` has no default — a missing value fails at construction
  with a `ValidationError` (#1459, #1461). `KLANGKD_DATA_DIR` defaults to
  `<KLANGKD_STATE_DIR>/data`, `KLANGKD_CUSTOMIZE_DIR` to
  `<KLANGKD_STATE_DIR>/custom`, and `KLANGKBUILD_PLUGINS_DIR` to
  `<KLANGKD_STATE_DIR>/plugins` when unset; an explicit value always wins
  (#1461, #1506). `klangkd` no longer mutates `os.environ` to inject a
  `state_dir` default; the field enforces its own requirement (#1459).
- **CLI transport resolver:** `klangk --server` now accepts a Unix socket
  path (e.g. `/tmp/klangk.sock`) in addition to `http(s)://` URLs. All HTTP
  and WebSocket connections route through a single transport resolver that
  picks UDS or TCP based on the server spec (#1399).
- **Dev config file:** devenv now reads backend config from `klangkd.yaml`
  (gitignored; copied from `klangkd.yaml.example` on first shell entry).
  `.env` / `dotenv.enable` removed; `KLANGKD_LISTEN`, `KLANGKD_IMAGE_NAME`,
  `KLANGKD_CUSTOMIZE_DIR`, `KLANGKD_PORT`, `KLANGKD_NGINX_PORT` no longer set
  as env vars by devenv (#1399).
- **UDS safe for no-auth mode:** `KLANGKD_AUTH_MODES=none` now accepts a UDS
  bind without `KLANGKD_ALLOW_INSECURE_NO_AUTH` — socket file permissions
  (0700 parent dir) provide the same trust boundary as loopback (#1399).
- **Direct UDS login:** `client_is_loopback` treats direct UDS connections
  (no nginx proxy) as loopback, so `klangk login /path/to/sock` works in
  no-auth mode (#1399).
- **Per-test timeout for the Python test suites** — both backend and CLI
  suites now run with `pytest-timeout` (`--timeout=60`). A hanging test
  fails after 60s instead of burning the whole job budget. New
  `pytest-timeout` dev dependency (#1513).
- **klangk nginx now rewrites `$remote_addr` to the real client IP** via the
  realip module (`set_real_ip_from <each KLANGKD_TRUSTED_PROXY_CIDRS entry>` +
  `real_ip_header X-Forwarded-For` + `real_ip_recursive on`). Without this,
  `proxy_set_header X-Real-IP $remote_addr` clobbered the real client IP the
  outer proxy forwarded with the proxy's own IP, so the backend's
  `client_is_loopback` / `derive_hosting_info` resolved the proxy IP, not the
  browser's — a regression from stable/1.0 where the customer proxy hit
  uvicorn directly. Suppressed entirely when `KLANGKD_REJECT_PROXY_HEADERS` is
  set (#1558).

### Changed

- **Environment variables are now split into four prefixed families
  (#1653).** The single `KLANGK_` prefix is repointed at the component each
  var targets:
  - `klangkd` server/operator settings → `KLANGKD_*` (e.g. `KLANGKD_PORT`,
    `KLANGKD_JWT_SECRET`, `KLANGKD_LLM_BASE_URL`, `KLANGKD_STATE_DIR`);
  - build/dev-shell/image-build knobs → `KLANGKBUILD_*` (e.g.
    `KLANGKBUILD_HOST_IMAGE`, `KLANGKBUILD_PLATFORM`, `KLANGKBUILD_VARIANT`);
  - vars the server **injects into workspace containers** (the agent's
    runtime contract) → `KLANGKWS_*` (e.g. `KLANGKWS_BRIDGE_URL`,
    `KLANGKWS_LLM_PROXY_URL`, `KLANGKWS_WORKSPACE_ID`, `KLANGKWS_PORT_MAPPINGS`,
    `KLANGKWS_FEATURE_*`);
  - the `klangk` CLI client's own vars stay `KLANGK_*` (the event-hook
    contract `KLANGK_EVENT*`/`KLANGK_HEALTHY`/…, plus the co-located-UDS
    reads `KLANGK_SOCKET`/`KLANGK_STATE_DIR`).
    Dual-purpose vars (a `klangkd` setting **and** container-injected) carry
    both names: the server reads `KLANGKD_LLM_MODEL` as its config and injects
    `KLANGKWS_LLM_MODEL` into the container. The websocket debug flag
    `KLANGKWS_DEBUG` → `KLANGKD_WEBSOCKET_DEBUG`, and the WS message-size
    var/field `WS_MSG_SIZE_MAX` → `WEBSOCKET_MSG_SIZE_MAX` (CLI
    `KLANGK_WEBSOCKET_MSG_SIZE_MAX`, server `KLANGKD_WEBSOCKET_MSG_SIZE_MAX`).
    See [Environment variables](docs/reference/environment.md).

- **The Soliplex knowledge-base plugin is now vendored into the repo
  under `plugins/soliplex/` (#1686).** `plugins.yaml` declares it via a local
  `path:` entry instead of the remote `git:`/`ref:` fetch from
  `soliplex/klangk-plugin-soliplex` pinned at `v0.4` (#1664); the build
  materializes it by symlink like the other plugins, with no network fetch.
  A side effect of retiring the remote fetch: a default build now compiles
  soliplex **in** again — it had been skipped by default since #1691 (the
  plugin's transitive `ag_ui` git dep carries an LFS-tracked fixture,
  `apps/dojo/e2e/fixtures/test-image.png`, that unauthenticated CI can't
  fetch). The build now exports `GIT_LFS_SKIP_SMUDGE=1` so the dep resolves
  without the LFS object — only its Dart source is needed. Soliplex is still dormant (not in `DEFAULT_FEATURES`); opt in
  with `KLANGKD_FEATURES_ENABLE=soliplex`. The git-sourced-plugin ability is
  unchanged — `update_plugins.py` still handles `git:`/`ref:` entries, and
  the build scripts' `KLANGKBUILD_BUILD_INCLUDE_REMOTE` gate stays as the generic
  remote-plugin policy (a no-op now that no plugin is git-sourced).

- **Default active-feature set is now `beep, bobdobbs, boingball,
browser-fetch, celebrate, git-credential` (#1700).** `DEFAULT_FEATURES`
  (`scripts/import_dart_plugins.py`) now ships `bobdobbs` (a compiled-in Dart
  plugin promoted from the optional set) and drops `pig-latin` and `word-count`
  from the default-on list. `plugins.yaml` is aligned: `bobdobbs` is added and
  `pig-latin` removed entirely (no longer compiled in — its source tree stays
  in the repo as an opt-in `path:` entry); `word-count` stays compiled in but is
  now dormant (activate with `KLANGKD_FEATURES_ENABLE=word-count`). This breaks
  the prior "compiled-in == defaults" invariant — `word-count` joins `soliplex`
  as a compiled-in-but-dormant feature.

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
    `KLANGKD_STATE_DIR` explicitly and are unaffected).
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

- **`KLANGKBUILD_PLUGINS_DIR` is gone from every layer (#1660).** The plugin
  declaration list is now the checked-in `plugins.yaml` at the repo root
  (the build-time source of truth, analogue of a committed `package.json` /
  `Cargo.toml`). The materialized payload — fetched/symlinked plugin trees,
  `plugins.lock`, the generated `klangk_plugins` Dart package — is a
  throwaway `mktemp -d` each build script (`flutterbuildweb.sh`,
  `build-workspace-image.sh`, `build-host-image.sh`) owns and cleans up on
  exit. `update_plugins.py` and `import_dart_plugins.py` take the payload
  dir via `--payload-dir` instead of reading `KLANGKBUILD_PLUGINS_DIR` from the
  environment. The host image no longer copies plugin trees in (the runtime
  reads `features.json` from the frontend build, not on-disk `package.json`
  files — the workspace image still bakes them in for Pi). The first-run
  `plugins.yaml` template-creation bootstrap is removed; the file is
  source-controlled. Operators who overrode `KLANGKBUILD_PLUGINS_DIR` to point
  at a custom declaration should instead edit the checked-in `plugins.yaml`
  (in their fork — see #1663).

- **`KLANGKD_STATE_DIR` now defaults to `$XDG_STATE_HOME/klangk` (#1644).**
  The runtime-state directory (UDS socket, rendered proxy config, pid file,
  DB) defaults to `~/.local/state/klangk` when no explicit value is supplied,
  so `pip install klangkd && klangkd` no longer hard-requires an operator to
  set it. Explicit `KLANGKD_STATE_DIR` / config-file values still win (devenv,
  the host container, and production operators who pin it are unaffected).
  `KLANGKD_DATA_DIR` derives from `state_dir` as before, so it picks up the
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
  `KLANGKD_NGINX_BIN`/`KLANGKD_NGINX_PORT` → `KLANGKD_PROXY_BIN`/`KLANGKD_PROXY_PORT`);
  `app.state.nginx_watchdog` → `app.state.proxy_watchdog`; the internal
  `_KLANGKD_DISABLE_NGINX` test kill switch → `_KLANGKBUILD_DISABLE_PROXY`; and the
  `test_nginx*.py` suites → `test_proxy*.py`. The actual `nginx` binary,
  rendered `nginx.conf`, and nginx packages are unchanged — the proxy is still
  implemented with nginx. Operators using `KLANGKD_NGINX_BIN` or
  `KLANGKD_NGINX_PORT` must rename them to `KLANGKD_PROXY_*`.

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
  `KLANGKD_FRONTEND_DIR` (devenv and the host container already do); packaged
  installs need no action.

- **SIGHUP now reloads configuration (#1587).** Sending `SIGHUP` to
  `klangkd` re-resolves `KlangkSettings` from the environment / YAML
  config file and applies the new values before recycling the runtime.
  Invalid config denies the restart (runtime left on last-known-good,
  reason logged at `ERROR`). Settings bound for the process lifetime
  (`KLANGKD_PORT`, `KLANGKD_LISTEN`, `KLANGKD_DATA_DIR`, `KLANGKD_STATE_DIR`)
  are warned but require a full restart to apply. See
  [Process Signals](deployment/signals.md).

- **`KLANGKD_CORS_ORIGINS` and `KLANGKD_FRONTEND_DIR` are now reloadable
  on SIGHUP (#1610).** CORS origins are served by a live middleware that
  re-reads `KLANGKD_CORS_ORIGINS` after every settings swap. A changed
  `KLANGKD_FRONTEND_DIR` remounts the Flutter static-files directory
  without a process restart.

- **`KLANGKD_PORT` is now the nginx browser port, not uvicorn's bind.** Under
  `klangkd` uvicorn always binds the UDS (`KLANGKD_SOCKET`); `KLANGKD_PORT` is
  the nginx listener for the browser UI + API + hosted apps. **Unset ⇒
  headless mode** (no browser listener; only the container-egress listener on
  `KLANGKD_EGRESS_PORT` is served). Set ⇒ full/browser mode. Suggested value
  `8997` (#1542).

- **`KLANGKD_LISTEN` is now a plain browser-interface address** (default
  `127.0.0.1`), rendered as `listen {KLANGKD_LISTEN}:{KLANGKD_PORT};` only in
  full/browser mode. The polymorphic socket-path meaning is retired (it never
  shipped in a release); the UDS path is now `KLANGKD_SOCKET` (#1542).

- **nginx now listens on two separate ports in full/browser mode** — the
  browser listener (`KLANGKD_LISTEN`:`KLANGKD_PORT`) and the container-egress
  listener (`KLANGKD_EGRESS_PORT`) — so ingress and egress traffic can be
  firewalled independently. `KLANGKD_EGRESS_PORT` must differ from
  `KLANGKD_PORT` (#1542).

- **`KLANGKD_FRONTEND_DIR` setting (#1456):** the built Flutter Web UI is
  served from `settings.frontend_dir` (defaults to the repo-relative
  `src/frontend/build/web` computed in `KlangkSettings`; `klangkd`
  deployments override it). Previously the path was hardcoded in `build_app`,
  so installed-package deployments silently skipped mounting the UI.

### Deprecated

- **`KLANGKD_PROXY_PORT`** is deprecated; rename to `KLANGKD_EGRESS_PORT`. If
  `KLANGKD_EGRESS_PORT` is unset, the `KLANGKD_PROXY_PORT` value is used as the
  egress port (with a deprecation warning); if both are set,
  `KLANGKD_EGRESS_PORT` wins and `KLANGKD_PROXY_PORT` is ignored. A future
  release will stop recognizing it (#1542, #1430). Renamed from
  `KLANGKD_NGINX_PORT` in #1430; the old `KLANGKD_NGINX_PORT` name is no longer
  recognized.

### Removed

- **The `customize/build/` directory is gone — fork the repo to add custom
  plugins (#1663).** With the plugin declaration list now checked in as
  `plugins.yaml` at the repo root (#1660), the `customize/build/build.sh`
  workflow (clone klangk, overlay `customize/build/plugins.yaml`, build) is
  redundant. The simpler, standard path is to fork klangk and edit the
  checked-in `plugins.yaml` directly, then run `scripts/build-host-image.sh`.
  `customize/build/build.sh` and `customize/build/plugins.yaml` are removed;
  everything else under `customize/` (`custom/`, `data/`, `mount/`,
  `docker-compose.yml`, `README.md`) stays — those are runtime-config
  concerns. The example `docker-compose.yml` now references the stock
  `klangk-host` image (override the `image:` line with your fork's build).
  `KLANGKD_REF` / `KLANGKD_REPO` (formerly consumed by `build.sh`) are gone;
  set `KLANGKBUILD_VARIANT` / `KLANGKBUILD_HOST_IMAGE` in the environment when running
  `scripts/build-host-image.sh`.

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
  (#1398, chunk 5 of #1392). When `KLANGKD_LISTEN` is a UNIX socket path,
  the nginx renderer now emits a minimal (headless) template — only the
  container-egress `/llm-proxy` location (with its workspace-token
  `auth_request` gate + `CONTAINER_ACL`) on the single container-egress
  listener, and nothing else: no `location /`, no `/api/v1/*`, no static
  UI, no `/auth/local`. A browser can't reach a UDS and uvicorn exposes no
  browser-facing TCP, so no browser surface is serviceable — the attack
  surface is two channels (operator→UDS, container→llm-proxy) and nothing
  else. Template selection keys off `KLANGKD_LISTEN`'s shape alone; the
  `KLANGKD_AUTH_MODE` value does not participate (socket ⇒ minimal, TCP ⇒
  full browser template, across all auth values). The TCP path is a strict
  regression guard (byte-for-byte identical output). This makes the
  UDS+none default posture's "eliminate the browser/TCP surface" a real
  property rather than a claim; the default-flip itself is #1400.

- **`test-all` / `test-unit` devenv scripts and concurrency-safe test corpus**
  (#1393). The whole test corpus is now runnable concurrently: every E2E
  harness free-allocates its server port and `KLANGKD_PORT_RANGE_START`
  (via a new `klangk_backend.model.free_port` helper) instead of hardcoding
  them, and container teardown is instance-scoped (no more `klangk.managed=true`
  sweeps that nuked other suites' containers). The two unit suites combine
  into one `python -m pytest src/backend/tests src/cli/tests` invocation
  (the root `pyproject.toml` now carries the asyncio + capture config that
  used to conflate them). New `test-all` runs unit + E2E; `test-unit` runs
  the combined unit corpus. E2E tasks dropped the forced `-p no:xdist` —
  opt into parallelism with `-n auto --dist=loadscope`.

- **`KLANGKD_AUTH_MODES=none`: no-login single-user (local-dev) mode**
  (#1374). A new `none` auth mode lets the frontend and CLI obtain a token
  for the seeded default user with no password prompt, enabling a frictionless
  single-user dev/test loop and serving as the foundation for a "one binary,
  named deployment profiles" strategy (`local-dev` / `customer-locked` /
  `team`). The server
  auto-creates the default user at startup; `POST /api/v1/auth/local` mints a
  standard JWT for it. The loopback bind (`KLANGKD_LISTEN`, #1375) plus an
  nginx per-location `allow 127.0.0.1/::1; deny all` ACL keep `/auth/local`
  unreachable from workspace containers, and the server refuses to start in
  `none` mode on a non-loopback bind unless `KLANGKD_ALLOW_INSECURE_NO_AUTH=1`
  is set. The CLI (`klangk`) auto-logs in on first command run with no prior
  `klangk login`; the server's auth mode is probed live (not cached) so a
  mode switch takes effect immediately. See [Auth Modes](features/auth-modes.md)
  for the full mode-switching guide.
- **`klangk admin` command group** (#1374): site-wide administration now
  has a dedicated CLI surface — `admin users ls`, `admin users
set-password <email>` (set a known password for the default user — whose
  password is random unless `KLANGKD_DEFAULT_PASSWORD` was set — before
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

- **The `claude-code` and `herdr` features have been removed.** The
  `features/claude-code/` and `features/herdr/` trees (and their docs /
  mentions) are gone. Neither was declared in the default `features.yaml`,
  so a stock build is unaffected; deployments that opted into either via a
  custom `features.yaml` entry should drop the entry. (#1658)

### Breaking

- **(#1653)** The environment-variable rename in _Changed_ is a clean break —
  there has been no stable release exposing these as a public operator
  contract (the project is pre-2.0; v1.0.x are patch-level previews), so old
  `KLANGK_*` names are **not** accepted alongside the new prefixes. Operators
  and deploy manifests (`docker-compose.yml`, `devenv.nix`, `.env`,
  `klangkd.yaml`) must update server/build vars to the new prefixes; CLI
  event-hook scripts keep `KLANGK_*`. The only intentional `KLANGK_*`
  survivors are the CLI client's own vars (event-hook contract + the
  co-located-UDS reads `KLANGK_SOCKET`/`KLANGK_STATE_DIR`).

- **The term "plugin" is retired in favor of "feature" across the codebase,
  build, and docs (#1658).** The activation unit is now "feature" everywhere
  except the external `klangk_plugin_api` package and `ToolPlugin` base class
  (kept — external package). Operator/integrator-visible: `plugins.yaml` →
  `features.yaml`, `plugins/` → `features/`, `update-plugins` →
  `update-features` (and codegen scripts `*_plugins.*` → `*_features.*`),
  workspace image path `/opt/klangk/plugins/` → `/opt/klangk/features/`,
  `GET /api/v1/version` field `"plugins"` → `"features"`, generated Dart
  package `klangk_plugins` → `klangk_features` (`createAllPlugins` →
  `createAllFeatures`) and each feature's package `klangk_plugin_<name>` →
  `klangk_feature_<name>`. `KLANGKD_FEATURES_ENABLE`/`features.json` are
  unchanged (already "feature" by #1655); the retired `KLANGKBUILD_PLUGINS_DIR`
  stays retired under its historical name; the env-var prefix remains
  `KLANGK_*` (the `KLANGK_*` → `KLANGKD_*` rename is #1653, not yet landed).

- **The Soliplex plugin's config key is renamed `SOLIPLEX_URL` →
  `KLANGKWS_FEATURE_SOLIPLEX_URL` (#1686).** Same `KLANGKWS_FEATURE_` namespace
  as the other plugin keys (#1662); the rename was deferred from #1702
  because soliplex was a remote plugin skipped by the build guard. Now that
  it's vendored local, the build guard would reject the unprefixed
  `SOLIPLEX_URL`, so the rename lands here. Operators who set `SOLIPLEX_URL`
  (only reachable on installs that built soliplex in via
  `KLANGKBUILD_BUILD_INCLUDE_REMOTE=1` and activated it) must set
  `KLANGKWS_FEATURE_SOLIPLEX_URL` instead. The frontend `/api/config` key is
  unchanged at `soliplex_url` (strip prefix + lowercase suffix), so Dart/UI
  consumers need no change.

- **Plugin-declared config keys must now start with `KLANGKWS_FEATURE_`**
  (#1662). The prefix is the plugin-config namespace: every server setting
  is `KLANGK_<SETTING>` (no `FEATURE_` infix), so the prefix alone guarantees
  a plugin can never declare a key that collides with a server secret, path,
  or infra field (`KLANGKD_JWT_SECRET`, `KLANGKD_DATA_DIR`, …) — no denylist /
  reserved set needed. Non-`KLANGKWS_FEATURE_` environment poison (`PATH`,
  `HOME`, `LD_PRELOAD`, …) is rejected by the same rule. Enforced at both
  layers: the build emitter (`import_dart_plugins.py`) raises on an
  unprefixed key, and the runtime resolver (`klangk.plugins`) skips one in
  a stale manifest with a warning. **Existing plugins must rename their
  declared keys:** `KLANGKD_GITHUB_OAUTH_CLIENT_ID` →
  `KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID` (git-credential),
  `KLANGKBUILD_BOING_SPEED` → `KLANGKWS_FEATURE_BOING_SPEED` (boingball). The
  container env var keeps the full prefixed name
  (`KLANGKWS_FEATURE_*=<value>`); the frontend `/api/config` key is the
  lowercased suffix after the prefix (e.g. `boing_speed=2.5`, not
  `klangk_feature_boing_speed=2.5`). Operators who set the renamed env
  vars must update their config. **Blocked on #1686:** the remote soliplex
  v0.4 plugin still declares the unprefixed `SOLIPLEX_URL`; this change
  must land together with (or after) #1686, which vendors + renames
  soliplex.

- **`KLANGKBUILD_PLUGINS_DIR` retires as a runtime setting (#1655).** The
  runtime no longer scans `$KLANGKBUILD_PLUGINS_DIR/*/package.json` for plugin
  config — that presumed materialized source trees on the klangkd host,
  which pip/uv installs never have. The server now reads the build-emitted
  `features.json` (one field — `container_env_keys` — to bridge container
  env vars; the frontend reads the rest). `KLANGKBUILD_PLUGINS_DIR` is **removed
  from `KlangkSettings`** with no successor; it stays as a **build-time-only**
  env var consumed by `update_plugins.py` and the image-build scripts (read
  from `os.environ`, not via settings). Operators who set it expecting the
  server to scan it: the server no longer scans anything; the shipped
  `features.json` is the whole runtime truth.

- **`KLANGKD_CUSTOMIZE_DIR` relocates from the state tree to the config
  tree (#1644).** It holds user-edited, durable intent (branding, email
  templates), so it defaults to `<config_dir>/custom` (→
  `~/.config/klangk/custom`, deriving from the new `KLANGKD_CONFIG_DIR` root —
  #1649) when unset — no longer under `state_dir`. **Operators who relied on
  the old `<state_dir>/custom` default must move their contents** (or set
  `KLANGKD_CUSTOMIZE_DIR` explicitly to the old path, which still works — or
  set `KLANGKD_CONFIG_DIR` once to relocate it). Explicit overrides are
  unchanged; the host container and shell scripts that set this var are
  unaffected.
  `KLANGKBUILD_PLUGINS_DIR` is **not** affected by this change — it stays under
  `<state_dir>/plugins` (as on main). Its tree placement is reworked
  separately in #1651.

- **`KLANGKD_PROXY_ENGINE` now defaults to `caddy` (#1634).** The Caddy
  reverse-proxy engine replaces nginx as the default. The rendered proxy
  config is delivered to Caddy's admin API over a `klangkd`-owned Unix
  domain socket (`POST /load`, `text/caddyfile`) instead of being written
  to an `nginx.conf` and applied by `nginx -c` — no on-disk source of
  truth, no reload. **Operators with no `KLANGKD_PROXY_ENGINE` set switch
  engines on upgrade.** The nginx engine remains selectable this release
  via `KLANGKD_PROXY_ENGINE=nginx` as the escape hatch for a Caddy
  regression — selecting it fires a deprecation warning, and it will be
  removed in a future release (#1642). If you hit a regression, set
  `KLANGKD_PROXY_ENGINE=nginx` and file an issue. Otherwise, unset the
  variable (caddy is the default). `klangkd` manages the proxy config
  entirely in both engines — operators never template proxy config or run
  reloads — so the swap is transparent to anyone not overriding
  `KLANGKD_PROXY_BIN`.

- **One `klangk` distribution ships the renamed server package `klangkd` and the folded-in client `klangk` (#1606).** The backend package is renamed `klangk_backend` → `klangkd` and the standalone `klangkc` distribution is retired — the client is promoted to a sibling top-level package under the same source root. One `pip install klangk` yields both `klangkd` (server) and `klangk` (client); the entrypoint command names are unchanged. The distribution name (`klangk`) is distinct from the import packages (`klangkd` / `klangk`), like `python-dateutil` → `dateutil`.
  - **Integrators** who `import klangk_backend` (e.g. OIDC login hooks) must update to `import klangkd`.
  - **The `klangkc` PyPI distribution is retired** in favor of `klangk`; the `cli-v*` tag line and `cli-publish.yml` workflow are removed. Both binaries release together off the single `v*` tag line.
  - **Test layout**: tests are split into per-package suites — `src/klangk/klangkd-tests/{tests,e2e-tests}` (server) and `src/klangk/klangkc-tests/{tests,e2e-tests}` (client) — as hyphenated siblings of the package dirs so they don't ship in the wheel. Both unit suites share one `--cov=klangkd --cov=klangk` 100% gate (run together via `test-backend`).

- **The listen/port settings model is restructured** (#1542):
  - `KLANGKD_NGINX_PORT` → rename to `KLANGKD_EGRESS_PORT` (deprecated alias
    accepted this release with a warning).
  - `KLANGKD_PORT` changes meaning from uvicorn's bind to the nginx browser
    port. Operators who set `KLANGKD_PORT` on the old assumption it was the
    (dead) uvicorn bind should review: unset it for headless, or set it to
    the desired browser port.
  - `KLANGKD_LISTEN`'s default is `127.0.0.1` (was polymorphic/unused). The
    socket-path meaning never shipped in a release.
  - The host container (`Dockerfile`) now sets `KLANGKD_PORT=8997`,
    `KLANGKD_EGRESS_PORT=8995`, and publishes both ports (was
    `KLANGKD_NGINX_PORT` + one published port).

- **Direct TCP to uvicorn is gone.** uvicorn now binds only a UNIX socket
  (`<state_dir>/klangk.sock`); nginx proxies to it. Point external proxies at
  `KLANGKD_NGINX_PORT` (default 8995), not the old port 8997 (#1400).
- **Default is now headless.** Bare `klangkd` (no `KLANGKD_LISTEN` set)
  defaults to UDS + `none` auth — headless, CLI-only. Set
  `KLANGKD_LISTEN=127.0.0.1` for the browser UI (#1400).
- **`KLANGKD_PORT` is no longer used by klangkd.** uvicorn always binds a UDS;
  the setting is retained only for bare-uvicorn test harnesses (#1400).
- **Devenv default changed to browser-first.** `klangkd.yaml.example` now
  defaults to `listen: 127.0.0.1` + `auth_modes: password`. Delete your local
  `klangkd.yaml` and re-enter `devenv shell` to regenerate it (#1400).
- **Default auth mode is now `none`** (no-login single-user, loopback-bound)
  when `KLANGKD_AUTH_MODES` is unset and no OIDC provider is configured
  (#1374). Previously the unset default was `password`. A fresh klangk now
  "just works" locally with no password and is unreachable from the network.
  This is safe by construction — `none` refuses to start on a non-loopback
  bind unless `KLANGKD_ALLOW_INSECURE_NO_AUTH=1` — but it is a behavior change
  on upgrade: **set `KLANGKD_AUTH_MODES=password` (or `oidc`/`both`) explicitly
  before redeploying if you relied on the old default.** Note: `none` mode is
  not yet supported with the published Docker host image (a published port
  isn't loopback) — the Docker examples set `KLANGKD_AUTH_MODES=password`; see
  #1391.
- **OIDC settings no longer change the auth mode (#1419).** Previously, when
  `KLANGKD_AUTH_MODES` was unset **and** an OIDC provider was configured, the
  resolved default was silently promoted to `both` (the "OIDC turns auth on"
  rule). That promotion is removed: the unset default is now **always `none`**,
  regardless of OIDC config, and `KLANGKD_OIDC_*` settings only take effect
  once the mode is explicitly `oidc` or `both`. **If you relied on OIDC being
  configured implying `both`, set `KLANGKD_AUTH_MODES=oidc` (or `both`)
  explicitly before redeploying** — otherwise your server will boot in `none`
  mode (no-login single-user, loopback-bound; safe by construction, but not
  your intended multi-user posture).
- **uvicorn now binds `127.0.0.1` by default** instead of `0.0.0.0`
  (`KLANGKD_LISTEN`, new). Workspace containers could previously reach the
  backend directly via `host.containers.internal:$KLANGKD_PORT`, bypassing nginx
  and therefore every per-location nginx ACL. nginx remains bound to `0.0.0.0`
  (container-reachable, so hosted apps and remote browsers still work) and
  proxies to uvicorn on the loopback address. Operators who reach the backend
  directly —
  bypassing nginx — must set `KLANGKD_LISTEN=0.0.0.0` to restore the old
  behavior. Applies to both the devenv dev server and the host container.
  (#1375)
- **`klangk invite` moved under the `admin` group** (#1374). The top-level
  `klangk invite <email>` command is gone, with no backward-compat alias.
  Use `klangk admin invitations send <email>` (and list with
  `klangk admin invitations ls`). Site-wide administration — users and
  invitations — now has a dedicated `admin` CLI surface matching the
  `terminal`/`volumes` noun-subgroup convention.
- **`klangkd` binds a UDS; `scripts/nginx.sh` retired** (#1396). uvicorn now
  binds a UNIX domain socket (`$KLANGKD_STATE_DIR/klangk.sock`) instead of a
  TCP port when launched via `klangkd` (dev and host container). nginx config
  is rendered by Python (`klangk_backend.nginx`) and nginx is owned as a
  child process of `klangkd`'s lifespan. uvicorn has **no TCP listener in any
  mode** — it is reachable only via the socket, which only same-uid processes
  can open. `scripts/nginx.sh`, the `klangk-resolve-value` console script,
  and the `/home/klangk/bin/nginx` shim are removed. The host container no
  longer publishes `KLANGKD_PORT` (8997) — only `KLANGKD_NGINX_PORT` (8995).
  `KLANGKD_PORT`/`KLANGKD_LISTEN` are retained for tests that launch uvicorn
  over TCP directly but are unused under `klangkd`.

### Fixed

- **The nginx proxy engine stays up under a plain `systemctl start` with no
  operator log workaround (#1550).** nginx's `access_log` directive has no
  `stdout` keyword, so it always `open(2)`s its destination by path; under
  systemd fd 1 is a Unix socket to journald (`ls /proc/<pid>/fd/1` →
  `socket:[N]`) that can't be re-opened, so the legacy `access_log
/dev/stdout;` failed with `ENXIO` and nginx exited at config parse.
  `ProxyRenderer` now probes fd 1: when it's reopenable (devenv, interactive,
  pipe-to-file, containers without journald) it keeps `access_log
/dev/stdout;`; when it's a socket (systemd journal) it emits `access_log
syslog:server=unix:/dev/log;` — the converged journald route that works on
  NixOS, Ubuntu 24.04+, and every other systemd host, since `/dev/log` is
  created and serviced by the core `systemd-journald-dev-log.socket` unit (no
  rsyslog needed), so access logs land in the journal next to klangkd's own.
  A `state_dir` file is the last-resort fallback for the rare
  socket-stdout-but-no-`/dev/log` case. `error_log stderr;` is unchanged
  (nginx special-cases the bare `stderr` token to inherited fd 2). This
  makes a default `StandardOutput=journal` unit work, retiring the
  `StandardOutput=append:...` workaround from #1546.

- **The Caddy proxy engine's child coexists with any other Caddy on the host
  and binds its admin UDS from the first moment, on any Caddy version
  (#1709).** Two version-robustness bugs were fixed. (1) The watchdog spawned
  `caddy run --config /dev/null` relying on the `CADDY_ADMIN` env var to set
  the admin address, but `CADDY_ADMIN` only lands in Caddy >= 2.7
  (caddy#5317) and klangkd runs the host's system Caddy
  (`shutil.which("caddy")`), so on older Caddy the env var was ignored, the
  empty config fell back to the default `localhost:2019`, and klangkd
  crash-looped polling a UDS that never appeared — failing on any host that
  also runs Caddy (a system `caddy.service`, a sibling reverse proxy, another
  klangkd). The spawn now passes a minimal initial Caddyfile (`{ admin
unix//<sock> }`) via `--config`; the `admin` global option has been honored
  since Caddy v2.0. (2) The admin address no longer carries a `|0600` mode
  suffix — that syntax is only honored on Caddy >= 2.8 and on older Caddy is
  folded into the socket _path_ (creating `caddy-admin.sock|0600` instead of
  `caddy-admin.sock`), which broke the admin poll on the older system Caddy
  the dist-smoke gate installs. The owner-only mode (#1559) is now enforced
  by the watchdog via `os.chmod` after the bind (version-independent). (3)
  The admin directive now sets `origins localhost` explicitly — older Caddy
  (<2.11) defaults the unix-socket admin's allowed origins to empty and 403s
  the `Host: localhost` klangkd sends, breaking `POST /load`. (4) The full
  global block (`persist_config off` + the `servers { trusted_proxies ... }`
  option + `trusted_proxies_strict`) is emitted only when the caddy binary
  actually supports it — klangkd probes the binary at startup (`caddy adapt`
  on a representative block) rather than trusting a version map. Ubuntu 24.04's
  apt caddy (2.6.2) predates `persist_config` and `servers/trusted_proxies`
  and would reject the whole config; on such older caddy klangkd falls back to
  a minimal global block (admin + auto_https only — caddy autosaves harmlessly
  and `{client_ip}` resolves the immediate peer, fine without an outer proxy).
  klangkd now runs on both the devenv's current caddy and that older system
  caddy.

- **The nginx proxy engine no longer returns 500 for `/llm-proxy/*`
  requests (or the other container-egress POST endpoints) with a body
  larger than the in-memory buffer (#1682).** With the default
  `proxy_request_buffering on`, nginx spills an oversize request body to
  `client_body_temp_path` — a directory that, under the keep-id user
  namespace, is owned by a different uid than the nginx worker, so any
  spill raised EACCES → 500. The container-egress locations
  (`/llm-proxy/`, `/api/v1/browser-delegate`,
  `/api/v1/workspaces/post-chat-message`) now set
  `proxy_request_buffering off`, streaming the request body straight to
  the upstream (sidestepping the temp dir entirely — matching caddy's
  `reverse_proxy`, which is why only nginx was affected). The LLM
  block's `resolver` now also sets `ipv6=off`, which changes upstream
  resolution for `/llm-proxy/` to IPv4-only (AAAA records suppressed) so
  hosts without IPv6 egress stop logging `Network is unreachable` per
  request; IPv6-only LLM upstreams would need a future setting.

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
  `KLANGKBUILD_BUILD_INCLUDE_REMOTE=1`. Proper fix is upstream (consume the
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
  `KLANGKD_CONTAINER_SUBNETS` escape hatch needed.

### Security

- **Read-only ("spectate") terminal input is now a strict whitelist of
  the protocol responses tmux needs to initialize, instead of "any ESC
  byte" (#1716).** The old gate let a read-only joiner pass any string
  beginning with `ESC`, so a spectator could inject arbitrary CSI/DCS/OSC
  sequences into the shared terminal — including **OSC 52 clipboard
  read/write**, which can exfiltrate or overwrite the owner's clipboard in
  terminals that support it. Only the terminal-protocol responses tmux's
  attach handshake needs now pass: DA1/DA2/DA3 device-attribute responses,
  the DSR cursor-position report, OSC 10/11/12/4 color reports, XTVERSION,
  and XTGETTCAP; user typing and every other escape sequence (title sets,
  size queries, DCS/tmux passthrough) is dropped. The size guard also now
  runs before the whitelist check, so an oversized read-only message is
  rejected without scanning it.

- **Bumped `pyasn1` 0.6.3 → 0.6.4 to fix CVE-2026-59886 / GHSA-hm4w-wwcw-mr6r
  (#1730, dependabot #4 / #3).** "Uncontrolled resource consumption when
  converting decoded REAL values" — a denial-of-service via crafted ASN.1
  REAL values. `pyasn1` is reached at runtime through `python-jose` and
  `rsa`, both on the JWT/OIDC auth path (`auth.py`, `oidc.py`), i.e. an
  attacker-reachable surface. It is a purely transitive dependency with no
  direct pin in `pyproject.toml`, so the lock bump alone closes both
  dependabot alerts with no code change.

- **Admin seeding is first-boot-only: config can no longer mint or
  reset admins once an admin exists (#1622).** Previously
  `seed_default_user` ran on every boot and created a fresh admin from
  `KLANGKD_DEFAULT_USER` / `KLANGKD_DEFAULT_PASSWORD` whenever the configured
  email didn't match an existing user — so anyone able to edit
  `klangkd.yaml` (or set the `KLANGKD_DEFAULT_*` env vars) could mint or
  reset an admin account by editing those values and restarting, bypassing
  all auth and admin-invite flows. Seeding is now gated on **`admin`-group
  emptiness**: an admin is created from `KLANGKD_DEFAULT_*` only when the
  `admin` group has no members (first boot, or after every admin has been
  deleted); once at least one admin exists, startup never creates, renames,
  re-emails, or re-passwords a user regardless of `KLANGKD_DEFAULT_*`. This
  also prevents lockout: editing `KLANGKD_DEFAULT_USER` and restarting can
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
