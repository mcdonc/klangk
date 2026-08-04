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

- **`klangkd.yaml` config file (#1645, #1649).** `klangkd` reads
  configuration from a YAML file at `$KLANGKD_CONFIG_DIR/klangkd.yaml`
  (default `~/.config/klangkd/klangkd.yaml`). A template is generated on
  first run if the file doesn't exist. Keys accept both `snake_case` and
  `kebab-case`. Environment variables override config-file values.
  See [Configuration](docs/reference/klangkd-config.md).

- **`klangk` TUI (#1746).** Running `klangk` with no subcommand launches an
  interactive terminal UI (built on Textual). Features: in-TUI login
  (password and OIDC hand-off), live server switching, a workspace list with
  filter/sort, workspace create/edit/duplicate/delete forms, workspace
  import/export with progress, terminal management (list, rename, delete),
  live container status via WebSocket, per-workspace quick actions (start,
  stop, restart), a keyboard cheatsheet (`?`), and a custom dark theme
  matching the web UI. `textual` is a runtime dependency.

- **Configurable DNS search domains for workspace containers (#2055).**
  A new deploy setting `dns_search` (env `KLANGKD_DNS_SEARCH`, comma-separated)
  is passed to workspace containers via podman `--dns-search`, so short
  hostnames that rely on a search suffix (e.g. `db` → `db.corp.example` on a
  corporate or Tailscale network) resolve inside containers. It pairs with
  the existing `dns_servers` / `--dns` plumbing (nameservers vs. the `search`
  line of `/etc/resolv.conf`). Read live off settings (reloadable on SIGHUP);
  applies to newly-created containers. Unset → podman's default search
  behavior (no change).

- **Unprivileged `ping` enabled in workspace containers (#2045).**
  Workspaces now ship with `CAP_NET_RAW` granted to the container so
  `iputils ping` works out of the box. The ping-socket sysctl
  (`net.ipv4.ping_group_range`) and a `setcap`'d ping binary — the
  least-privilege alternatives — are both rejected under rootless podman
  (the sysctl write fails with `EINVAL` at container start; file
  capabilities are ignored in a user namespace), so the capability is the
  only path that works in klangk's rootless deployment. Its real cost is
  low rootless: the container lives in a private netns behind pasta/slirp
  with no shared L2 bridge, so the cap grants neither bridge sniffing nor
  ARP spoofing — only raw-socket crafting inside the container's own netns.
  A deploy setting `enable_ping` (default `true`, env `KLANGKD_ENABLE_PING`)
  turns it off for locked-down deploys.
- **`process-compose` supervisor installed in the workspace container (#2049).**
  The workspace image now ships the `process-compose` binary at
  `/usr/local/bin/process-compose` (arch-aware build, pinned to `v1.120.0`),
  so a managed set of processes can be run inside the container. The base
  image's `supervisor` (supervisord) is unaffected.
- **`chat` feature — workspace chat surface extracted into a compiled-in,
  opt-in feature (#1976).** The chat tab + clanker agent UI moved out of the
  host frontend into `features/chat/`, registered as a workspace tab via the
  feature-tab framework (#1975). To carry the chat's rich integration
  (unread badge, mention highlight, mark-read-on-view, focus-on-select)
  across the package boundary, the tab framework gained a strip-badge channel
  and a visibility hook (`klangk_plugin_api` v0.5.0:
  `WorkspaceTabPlugin.badge` + `.setVisible`). Declares
  `KLANGKWS_FEATURE_CHAT_AGENT_ENABLED` / `_EMAIL` / `_HANDLE` (scope `both`).

- **Feature-contributed workspace tabs (#1975).** A feature can now
  contribute a workspace tab (mounted in the workspace tab strip) by
  declaring a `WorkspaceTabPlugin` (title + icon + builder), separately
  from `ToolPlugin`. A feature package may declare either or both — e.g. a
  `chat` feature contributes both a chat tab and agent tool handlers. Tabs
  mount only when their feature is active (the existing
  `KLANGKD_FEATURES_ENABLE` active-set filter). Bumps the
  `klangk_plugin_api` dependency to `v0.3.0`, which adds the new
  `WorkspaceTabPlugin` / `WorkspaceTabRegistry` API.

- **Per-workspace behavioral settings via a JSON `settings` bag (#864).**
  A workspace may now override several deploy-wide tuning knobs on a
  per-workspace basis, with the precedence **workspace override > deploy
  default > none**. The overrides live in a single JSON `settings` column
  on the workspace (one column, one migration, one resolution path) rather
  than one column per setting. This release wires up:
  `idle_timeout`, `bridge_timeout`, `cpu_limit`, `memory_limit`, and
  `pids_limit`. Resource-limit overrides are applied as-is with no
  clamping (#34) — a creator may go larger _or_ smaller than the deploy
  default, and are **not bounded by `KLANGKD_CONTAINER_*`** (an owner who
  sets an override escapes the deploy-wide budget). An `idle_timeout` of
  `0` means "never idle out" (pin the workspace alive), matching the
  auto_start boot pin; the other limits must be strictly positive.
  Set them at create time (`POST /workspaces`), via full replace
  (`PUT /workspaces/{id}` with a `settings` field), or via partial merge
  (`PATCH /workspaces/{id}/settings`, where a `null` value deletes a key
  and reverts it to the deploy default). Unknown setting names and
  malformed values are rejected at the API boundary (HTTP 400).

- **Container resource limits (CPU / memory / PIDs, #34).** Deploy-wide
  limits cap every workspace container: `KLANGKD_CONTAINER_CPU_LIMIT`
  (default `2.0`), `KLANGKD_CONTAINER_MEMORY_LIMIT` (default `8g`),
  `KLANGKD_CONTAINER_PIDS_LIMIT` (default `512`). Per-workspace overrides
  via the workspace `settings` bag. Set a field to empty to disable that
  cap.
- **Tmux status bar in workspace shells (#1880).** Shells display a status
  bar at the bottom showing the workspace name, current terminal name, and
  the `~.` disconnect hint. The workspace name updates live on rename.

- **Account self-service from the CLI (#1753).** A new
  `klangk account` group (`show`, `passwd`, `handle`, `email`) changes your
  password, handle, or email from the command line. It mirrors the Flutter
  `SettingsPage`: current `@handle` + email from `GET /api/v1/auth/me`,
  client-side checks (handle lowercase `[a-z0-9._-]+` charset, email
  format, password minimum read from `/api/v1/config`), and password
  confirmation for handle/email changes (plus a confirm dialog for the
  handle, which affects your terminal home directory). The CLI re-keys your
  cached token under the new email after a change — the JWT subject is your
  user id, so the token stays valid; only the key it's filed under changes.

- **Flutter workspace settings: the restart-needed notice offers a
  "Restart now" button (#1780).** Editing a create-time field (image,
  service command, mounts, env, or allowed domains) on a _running_ workspace
  shows a "restart to apply" notice; it now includes a Restart now action
  that triggers the restart immediately (routed through the workspace page,
  which owns the in-flight indicator), rather than being informational only.

- **Per-workspace network egress filtering (#1365).** Workspaces can
  declare an `allowed_domains` allow-list (`host`, `host:port`, or IPv4
  CIDR specs) to restrict outbound network to specific destinations. An
  OCI hook installs a default-deny iptables ruleset in the container's
  network namespace before the process starts — no proxy, no TLS
  interception. IPv6 is disabled inside filtered containers. Deploy-wide
  defaults via `KLANGKD_NETFILTER_DEFAULT_DOMAINS`; disable entirely with
  `KLANGKD_NETFILTER_ENABLED=false`. Configurable via the workspace
  Settings panel or the `allowed_domains` API field. See
  [Egress Filtering](https://klangk.dev/features/egress-filtering).

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

- **Caddy replaces nginx as the reverse proxy (#1559, #1634, #1642).**
  The reverse proxy is now Caddy (previously nginx). Config is delivered to
  Caddy's admin API over a Unix domain socket — no on-disk config file, no
  reload command. `KLANGKD_PROXY_BIN` overrides the Caddy binary.
  `KLANGKD_CADDY_ADMIN_SOCKET` overrides the admin UDS path (default
  `<state_dir>/caddy-admin.sock`). The proxy surface is unchanged: two
  listeners (browser + container egress), workspace JWT validation,
  container-source IP ACL, LLM proxy, hosted app proxy.

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

- **`KLANGKD_EGRESS_LISTEN`** — the interface the proxy binds for the
  container-egress listener. Defaults to `0.0.0.0` (all interfaces). The
  all-interfaces bind is gated by the container-source IP ACL plus the
  workspace-token gate; pin to a specific host IP to tighten further
  (#1542).

- **LLM proxy with multi-provider routing (#1396, #2046, #2070).**
  Workspace containers access LLMs via `/llm-proxy/`, backed by an in-process
  litellm Router that routes requests to one or more providers by model name.
  Single-provider setups use passthrough mode (`model_name: '*'`) for automatic
  model discovery; multi-provider setups list models explicitly with per-model
  credentials. Configure via `KLANGKD_LLM_MODELS` (env) or `llm-models` in
  `klangkd.yaml`. `KLANGKD_LLM_API_KEY` is the default key for models that
  don't specify their own. See
  [LLM Proxy](docs/architecture/llm-proxy.md).

- **`KLANGKD_EGRESS_PORT`** — the container-egress port the proxy listens on
  for container→backend traffic (`/llm-proxy`, `/api/v1/browser-delegate`,
  `/api/v1/workspaces/post-chat-message`). Default `8995` (#1542).

- **`KLANGKD_SOCKET`** — the backend UDS path `klangkd` binds. Defaults to
  `<state_dir>/klangk.sock`; override when the default overflows the
  `AF_UNIX` `sun_path` limit. A resolved path exceeding 104 chars fails at
  construction with a diagnostic directing the deployer to shorten
  `KLANGKD_SOCKET` or move `KLANGKD_STATE_DIR` shallower (#1531, #1542).

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
- **UDS safe for no-auth mode:** `KLANGKD_AUTH_MODES=none` now accepts a UDS
  bind without `KLANGKD_ALLOW_INSECURE_NO_AUTH` — socket file permissions
  (0700 parent dir) provide the same trust boundary as loopback (#1399).
- **Direct UDS login:** `client_is_loopback` treats direct UDS connections
  (no proxy) as loopback, so `klangk login /path/to/sock` works in
  no-auth mode (#1399).
- **Per-test timeout for the Python test suites** — both backend and CLI
  suites now run with `pytest-timeout` (`--timeout=60`). A hanging test
  fails after 60s instead of burning the whole job budget. New
  `pytest-timeout` dev dependency (#1513).

### Changed

- **Bumped `@earendil-works/pi-coding-agent` in the workspace image from
  `0.79.9` to `0.83.0` (#2049).** The in-container coding agent is now the
  latest published release.
- **`none` auth mode is declared an unsupported configuration with the
  published Docker host image (#1391).** `none` (no-login single-user,
  loopback-only) is safe only when solely the operator's loopback can reach
  `/auth/local`; a `docker run -p` published port is network-reachable, so
  the image cannot use it — the bind-safety gate refuses a non-loopback
  bind, and even with `KLANGKD_ALLOW_INSECURE_NO_AUTH=1` the proxy
  `/auth/local` ACL denies the port-forwarded request with `403`. **The
  Docker host image uses `KLANGKD_AUTH_MODES=password`** (or `oidc`/`both`)
  by default; all Docker examples pin it. For a no-login single-user
  experience, run klangk locally (devenv or the bare binary) instead of the
  published image. This replaces the previous "until #1391 lands"
  placeholder language in the Docker docs.
- **The clanker agent is now opt-in (off by default) (#1977).** The
  `pi --mode rpc` agent subprocess spawns only when the `chat` feature is
  active (`KLANGKD_FEATURES_ENABLE`) **and** the operator enables it via
  `KLANGKWS_FEATURE_CHAT_AGENT_ENABLED`. Previously the agent was on by
  default, disableable via `KLANGKD_AGENT_DISABLED`. `/api/v1/config` now
  reports `chat_agent_enabled` (bool) so the UI can hide `@clanker` and
  the agent controls when it's off.

- **The workspace chat tab is now opt-in (#1976).** The chat surface moved
  into the compiled-in `chat` feature, which is **dormant by default** — the
  chat tab no longer appears unless the deploy sets
  `KLANGKD_FEATURES_ENABLE=chat`. (Previously the chat tab was always present,
  permission-gated.) `klangk_plugin_api` bumped to v0.5.0 (adds
  `WorkspaceTabPlugin.badge` + `.setVisible`).

- **`forward-agent` is on by default in generated `klangk.yaml` (#1923,
  #2000).** A freshly created config — written eagerly on any CLI invocation
  (`ensure_config`, #2000), or on first `klangk login` — ships an active
  `forward-agent: true` so a workspace can use the operator's loaded SSH keys
  (e.g. `git push`) without extra setup. Set `forward-agent: false` (globally
  or per-server) to disable it for an untrusted workspace: while forwarded,
  anyone who can reach the agent socket on the remote host can authenticate
  as you for the session. Existing configs are unchanged.

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

- **`KLANGKD_PORT` is the proxy browser port.** Uvicorn binds the UDS
  (`KLANGKD_SOCKET`); `KLANGKD_PORT` is the proxy listener for the browser
  UI + API + hosted apps. **Unset ⇒ headless mode** (only the container-
  egress listener on `KLANGKD_EGRESS_PORT`). Set ⇒ full/browser mode.
  `KLANGKD_EGRESS_PORT` must differ from `KLANGKD_PORT` (#1542).

- **`KLANGKD_LISTEN`** — the browser-interface bind address (default
  `127.0.0.1`). The UDS path is `KLANGKD_SOCKET` (#1542).

- **`KLANGKD_FRONTEND_DIR` setting (#1456):** the built Flutter Web UI is
  served from `settings.frontend_dir` (defaults to the repo-relative
  `src/frontend/build/web` computed in `KlangkSettings`; `klangkd`
  deployments override it). Previously the path was hardcoded in `build_app`,
  so installed-package deployments silently skipped mounting the UI.

### Deprecated

- **`KLANGKD_PROXY_PORT`** is deprecated; rename to `KLANGKD_EGRESS_PORT`. If
  both are set, `KLANGKD_EGRESS_PORT` wins (#1542).

### Removed

- **`KLANGKD_SSL_CERT_DIR` is removed (#1523).** Custom CA certificates now
  have a single canonical location: drop `.pem`/`.crt` files into
  `<KLANGKD_CUSTOMIZE_DIR>/certs/`. Operators who set `KLANGKD_SSL_CERT_DIR`
  should move those certs into `<customize_dir>/certs/`. The resolver already
  fell back to that path; the env var is removed with no compat shim.

- **`KLANGKD_AGENT_DISABLED`, `KLANGKD_AGENT_EMAIL`, `KLANGKD_AGENT_HANDLE`
  are removed (#1977).** The clanker agent's enable flag and identity now
  live in the chat feature's config keys (`KLANGKWS_FEATURE_CHAT_AGENT_ENABLED`,
  `_EMAIL`, `_HANDLE`) — set via env or the `features_config:` block of
  `klangkd.yaml`. Operators who customized `KLANGKD_AGENT_*` should move those
  values to the feature-config keys. See [Chat](../features/chat.md).

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
  standard JWT for it. The loopback bind (`KLANGKD_LISTEN`, #1375) plus a
  proxy per-location ACL keep `/auth/local`
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

- **One `klangk` distribution ships the renamed server package `klangkd` and the folded-in client `klangk` (#1606).** The backend package is renamed `klangk_backend` → `klangkd` and the standalone `klangkc` distribution is retired — the client is promoted to a sibling top-level package under the same source root. One `pip install klangk` yields both `klangkd` (server) and `klangk` (client); the entrypoint command names are unchanged. The distribution name (`klangk`) is distinct from the import packages (`klangkd` / `klangk`), like `python-dateutil` → `dateutil`.
  - **Integrators** who `import klangk_backend` (e.g. OIDC login hooks) must update to `import klangkd`.
  - **The `klangkc` PyPI distribution is retired** in favor of `klangk`; the `cli-v*` tag line and `cli-publish.yml` workflow are removed. Both binaries release together off the single `v*` tag line.
  - **Test layout**: tests are split into per-package suites — `src/klangk/klangkd-tests/{tests,e2e-tests}` (server) and `src/klangk/klangkc-tests/{tests,e2e-tests}` (client) — as hyphenated siblings of the package dirs so they don't ship in the wheel. Both unit suites share one `--cov=klangkd --cov=klangk` 100% gate (run together via `test-backend`).

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
  an unsupported configuration with the published Docker host image (a
  published port isn't loopback) — the Docker image uses
  `KLANGKD_AUTH_MODES=password`; see #1391.
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
- **`klangk invite` moved under the `admin` group** (#1374). The top-level
  `klangk invite <email>` command is gone, with no backward-compat alias.
  Use `klangk admin invitations send <email>` (and list with
  `klangk admin invitations ls`). Site-wide administration — users and
  invitations — now has a dedicated `admin` CLI surface matching the
  `terminal`/`volumes` noun-subgroup convention.

### Fixed

- **Duplicate `klangkd` launch no longer spams the running instance's log
  with ERROR "Another klangk instance is already running" lines (#2021).**
  The _losing_ (second) process still reports why it exits, but now
  de-duplicated against the live winner PID: the first collision logs once,
  and a service supervisor's restart loop of the loser stays quiet for
  retries against the same winner instead of emitting one ERROR per retry
  into the shared log stream. A _different_ winner PID (a restart) is
  reported fresh. The _winning_ (first) process never reaches the refusal
  path, so it never logs this — independent of whether stderr is a TTY.

- **`klangkd` no longer crash-loops when a second instance starts against
  the same config (#1993).** The pre-flight guard that refuses a duplicate
  instance tried to log via a nonexistent `logger` symbol
  (`from klangk.logger import logger` — `klangk.logger` exposes only
  `configure` / `configure_defaults`), raising `ImportError` before the
  graceful `sys.exit(1)`. The process crashed and restarted under the
  supervisor (and the already-running instance printed the same traceback)
  instead of exiting cleanly. The launcher now uses a per-module
  `logging.getLogger(__name__)` and logs + exits as intended.

- **Chat read paths now require the `chat` permission (#1976).** When the
  chat surface moved into the (deploy-wide) `chat` feature tab, the
  per-user `_hasPerm('chat')` gate that hid the panel was lost. The send
  path was already gated, but chat history (sent on connect) and
  `chat_load_more` were not — so a user without `chat` could read full
  history + paginate older messages. Both read paths now check `_has_perm
("chat")` and deny without it (the frontend tab is still visible to such
  a user but receives no data).

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

### Security

- **`git-credential-klangk`: redact secrets from debug output (#1938).**
  When `GIT_CREDENTIAL_KLANGK_DEBUG` is set, the helper previously logged
  the bridge response and the git credential input verbatim to stderr,
  either of which can carry the user's password/PAT. Debug logs now mask
  values for sensitive keys (`password`, `token`, `access_token`,
  `secret`). The credential is still delivered to git over stdout as
  before — only the stderr debug trace is redacted.

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

- **The proxy denies container source IPs by default on the catch-all**
  (#1376). A container can reach only the three endpoints it needs
  (`/llm-proxy/`, `/api/v1/browser-delegate`,
  `/api/v1/workspaces/post-chat-message`); every other path is refused
  with 403. Loopback (local browsers) and other IPs (remote browsers)
  are unaffected.

- **Removed unused `adm-zip` devDependency from the frontend e2e-test
  package (#2).** `adm-zip` and `@types/adm-zip` were declared in
  `src/frontend/e2e-tests/package.json` but never imported anywhere in the
  tree; dropping them eliminates the vulnerable `0.5.x` line
  (CVE-2026-39244 / GHSA-xcpc-8h2w-3j85 — crafted ZIP triggers a 4 GB
  memory allocation) flagged by Dependabot. `npm audit` now reports 0
  vulnerabilities; Playwright still compiles all 202 tests. No production
  code depended on the package.
