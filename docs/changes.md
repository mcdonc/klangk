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

- **`nix_seed` — per-workspace `/nix` with two backends (#2219, #2220).** The
  per-workspace `/nix` config is now one block — `nix_seed: {type, path}` —
  selecting a backend: `btrfs-snapshot` (a CoW snapshot of a seed btrfs
  subvolume) or `fuse-overlayfs` (the default; a `fuse-overlayfs` overlay of a
  plain-directory seed — works on any filesystem, no privileged helper; needs
  `fuse-overlayfs` + `fusermount3` + `/dev/fuse`). Omit `nix_seed` to disable
  (nix is image-only). The fuse backend suits a bare-metal Linux host; it does
  not work where podman is nested (host-container, macOS — see #2221). See
  `docs/features/nix.md`.
- **`klangkd doctor` verifies GNU `stat` (#2220).** The nix btrfs-snapshot
  backend validates the seed is on btrfs via `stat -f -c %T`; doctor now checks
  GNU coreutils `stat` is on PATH (Linux) so a missing or BSD `stat` surfaces at
  pre-flight. `coreutils` (which provides `stat`) is explicit in `devenv.nix`.

- **Shared base `/nix` store seed (`scripts/build-nix-seed.sh`) (#2200).**
  A reproducible build step that produces a self-consistent `/nix` tree (the
  store, the nix DB, and a base profile with nix and devenv) and an
  `/etc/nix/nix.conf` (flakes, nix-command, and pre-configured binary caches),
  deployed alongside klangk as a host-side tree rather than baked into a
  workspace image. Run via `devenv shell -- build-nix-seed`. Consumed by #2201.

- **Per-workspace `/nix` via btrfs snapshots (#2201, #2202, #2208).** With
  `nix_seed.type: btrfs-snapshot` and a seed btrfs subvolume at `nix_seed.path`,
  a workspace with the
  per-workspace `nix` setting enabled gets a writable, isolated `/nix` as a
  btrfs snapshot of the seed: snapshotted on first start, reused across
  restarts, deleted on workspace delete; the snapshot's `/nix` and `nix.conf`
  are bind-mounted into the container. btrfs needs no privileged helper (a
  non-root user can snapshot a writable subvolume, and the snapshot is reachable
  via the parent mount) — unlike zfs, whose non-root mount is impossible on
  Linux. Requires a btrfs filesystem mounted with `user_subvol_rm_allowed`.

- **Per-workspace nix feature flag (#2202).** A workspace `nix` setting
  (boolean) gates the per-workspace `/nix` mount, settable at create time via a
  "Nix" checkbox in the create-workspace dialog (shown only when the server
  has `nix_seed` configured) or via the API. Workspaces without it are
  unaffected; with no `nix_seed`, nix is image-only.

- **Shared terminals in the TUI (#2164).** The workspace detail screen
  now lists shared terminals visible to you (other users' shared windows
  and the agent's `service` window), below your own terminals. Selecting
  one runs `klangk shell <ws> <handle>:<window>`, joining it via the same
  `join_shared_terminal` path the browser uses. Arrow down from your own
  terminals into the shared list; the `[n]`/`[m]`/`[t]` own-terminal
  keys no-op while the shared list is focused.

- **New terminal from the tmux status bar (#2161).** A clickable "+ new"
  item in the workspace terminal's tmux status bar opens another terminal
  via a native `tmux new-window`, working the same in `klangk shell` and the
  browser. (The new terminal appearing as a Flutter tab is tracked in
  #2171.)

- **`klangkd.yaml` config file (#1645, #1649).** `klangkd` reads configuration
  from `$KLANGKD_CONFIG_DIR/klangkd.yaml` (default
  `~/.config/klangkd/klangkd.yaml`). A template is generated on first run.
  Keys accept both `snake_case` and `kebab-case`; env vars override config-file
  values. See [Configuration](docs/reference/klangkd-config.md).

- **`klangk` TUI (#1746).** Running `klangk` with no subcommand launches an
  interactive terminal UI (Textual). Login, workspace management, terminal
  management, import/export, live container status, and a keyboard cheatsheet.

- **DNS search domains for workspace containers (#2055).** `KLANGKD_DNS_SEARCH`
  (comma-separated) adds `--dns-search` to workspace containers so short
  hostnames resolve on corporate/Tailscale networks. Reloadable on SIGHUP.

- **Unprivileged `ping` in workspace containers (#2045).** Containers ship with
  `CAP_NET_RAW` so `ping` works out of the box under rootless podman. Disable
  with `KLANGKD_ENABLE_PING=false`.

- **`process-compose` in the workspace container (#2049).** The workspace image
  ships `process-compose` v1.120.0 at `/usr/local/bin/process-compose`.

- **`chat` feature (#1976).** The workspace chat tab + clanker agent UI is now a
  compiled-in, opt-in feature. Activate with `KLANGKD_FEATURES_ENABLE=chat`.

- **Feature-contributed workspace tabs (#1975).** Features can contribute tabs
  to the workspace tab strip via `WorkspaceTabPlugin`. Tabs mount only when
  their feature is active.

- **Per-workspace behavioral settings (#864).** Workspaces can override
  `idle_timeout`, `bridge_timeout`, `cpu_limit`, `memory_limit`, and
  `pids_limit` via a JSON `settings` bag. Set at create time or via
  `PATCH /workspaces/{id}/settings`.

- **Container resource limits (#34).** Deploy-wide CPU/memory/PIDs limits:
  `KLANGKD_CONTAINER_CPU_LIMIT` (default `2.0`),
  `KLANGKD_CONTAINER_MEMORY_LIMIT` (default `8g`),
  `KLANGKD_CONTAINER_PIDS_LIMIT` (default `512`). Per-workspace overrides
  via the workspace `settings` bag.

- **Tmux status bar in workspace shells (#1880).** Shells show the workspace
  name, terminal name, and `~.` disconnect hint. Updates live on rename.

- **`klangkd doctor` pre-flight checker (#1612).** Verifies required binaries,
  rootless podman config, and common misconfigurations with actionable hints.

- **`klangk account` CLI (#1753).** `show`, `passwd`, `handle`, `email`
  subcommands for self-service account management from the command line.

- **Restart now button in workspace settings (#1780).** Editing a create-time
  field on a running workspace offers an immediate restart action.

- **Per-workspace network egress filtering (#1365).** `allowed_domains`
  allow-list restricts outbound network via OCI-hook iptables rules. Deploy
  defaults via `KLANGKD_NETFILTER_DEFAULT_DOMAINS`; disable with
  `KLANGKD_NETFILTER_ENABLED=false`.
  See [Egress Filtering](https://klangk.dev/features/egress-filtering).

- **`features_config:` block in `klangkd.yaml` (#1659, #1737).** Feature
  config keys can be set in the config file (env still wins). Accepts both
  the full name (`KLANGKWS_FEATURE_SOLIPLEX_URL`) and the short form
  (`soliplex_url`). `file:`/`cmd:` indirection honored.

- **CLI auto-discovers co-located `klangkd` (#1676).** When no server is
  configured, `klangk` falls back to the local `klangkd` UDS if it exists.
  `klangkd` + `klangk` on the same host "just works" with no `klangk login`.

- **Soliplex ships compiled-in but dormant (#1664).** The Soliplex knowledge-
  base plugin is compiled into the default wheel but not in `DEFAULT_FEATURES`.
  Opt in with `KLANGKD_FEATURES_ENABLE=soliplex`.

- **PyPI publishing on tag push (#1656).** `release.yml` builds the wheel and
  publishes via trusted publishing (OIDC). `pip install klangk` yields a
  working `klangkd` with the Flutter UI served from the wheel.

- **Feature manifest + per-deploy activation (#1655).** The build emits
  `features.json`; `KLANGKD_FEATURES_ENABLE` (comma-separated) controls which
  features are active. Unset uses the manifest defaults.

- **`KLANGKD_CONFIG_DIR` (#1649).** Root for user-edited config paths. Defaults
  to `$XDG_CONFIG_HOME/klangk`. `KLANGKD_CUSTOMIZE_DIR` derives from it.

- **Caddy replaces nginx as the reverse proxy (#1559, #1634, #1642).** Config
  is delivered to Caddy's admin API over a Unix domain socket. No on-disk
  config file. `KLANGKD_PROXY_BIN` overrides the Caddy binary;
  `KLANGKD_CADDY_ADMIN_SOCKET` overrides the admin UDS path.

- **Handles accepted at login (#616).** Login and user-lookup surfaces now
  accept a handle in addition to an email. Brute-force lockout is keyed on
  the resolved user.

- **Packaged `klangkd` serves the web UI (#1600).** `pip install klangk` serves
  the Flutter UI from the in-wheel `klangk/frontend/`. Override with
  `KLANGKD_FRONTEND_DIR`.

- **`KLANGKD_LOG_LEVEL` (#1467).** Centralized, settings-driven log level
  (default `INFO`). Reloadable on SIGHUP. Third-party loggers silenced to
  `WARNING`.

- **Consent banner per-visit mode (#1544).** `KLANGKD_LOGIN_BANNER_EVERY_VISIT`
  (default `false`) requires re-acceptance on every app load.

- **`KLANGKD_EGRESS_LISTEN` (#1542).** Bind address for the container-egress
  listener. Default `0.0.0.0`.

- **LLM proxy with multi-provider routing (#1396, #2046, #2070).** Workspace
  containers access LLMs via `/llm-proxy/`, backed by an in-process litellm
  Router. Passthrough mode for single-provider; explicit model list for multi-
  provider. Configure via `KLANGKD_LLM_MODELS` or `llm-models` in
  `klangkd.yaml`. See [LLM Proxy](docs/architecture/llm-proxy.md).

- **`KLANGKD_EGRESS_PORT` (#1542).** Container-egress port for `/llm-proxy`,
  `/api/v1/browser-delegate`, `/api/v1/workspaces/post-chat-message`. Default
  `8995`.

- **`KLANGKD_SOCKET` (#1531, #1542).** Backend UDS path. Defaults to
  `<state_dir>/klangk.sock`. Paths > 104 chars fail at construction.

- **`file:`/`cmd:` resolution (#1461).** All settings resolve `file:`/`cmd:`
  prefixes at construction. A bad reference fails fast at boot.

- **`KLANGKD_STATE_DIR` required (#1459, #1461).** `KLANGKD_DATA_DIR`,
  `KLANGKD_CUSTOMIZE_DIR` derive from it when unset.

- **CLI transport resolver (#1399).** `klangk --server` accepts UDS paths in
  addition to HTTP URLs. UDS is safe for `none` auth mode without
  `KLANGKD_ALLOW_INSECURE_NO_AUTH`.

### Changed

- **Workspace create/edit forms grouped into sections (#2229).** The
  browser's "New workspace" dialog and the workspace settings panel now
  group fields into the same logical sections the TUI form uses — General,
  Mounts, Environment, Netfilter, Resources, Advanced — in that order, each
  under its own titled pane. A section-nav strip above the fields jumps to a
  section (the edit panel's strip is pinned; the create dialog's scrolls at
  the top). The settings panel's config area is also wider. Field set and
  validation are unchanged.

- **Duplicate terminal tab names allowed (#2192).** Renaming a
  terminal tab to a name another tab already uses is no longer rejected.
  Tab names are display-only; window identity is the tmux window id (`@N`),
  so nothing in klangk relies on names being unique. The same-named shared
  terminal created via the legacy `create_shared_terminal` command is now
  identified by its window id rather than matched by name. `klangk shell`
  now errors on an ambiguous name (listing the candidate `@N` ids) instead
  of silently picking the first, accepts `@N` and `handle:@N` for exact
  targeting, and `klangk terminal ls` shows an `ID` column. `klangk terminal
share`/`unshare` likewise accept `@N` and reject an ambiguous name. The
  TUI's `[n]` new-terminal action no longer invents a sequential `term-N`
  label; it creates the window unnamed, so the server names it `bash` like
  every other new terminal.

- **TUI workspace-detail rows are zebra-striped (#2193).** The
  `id / running / uptime / mounts / …` property block on
  `WorkspaceDetailScreen` now alternates row backgrounds (theme `surface`
  over `background`) for readability; key names are bold and right-aligned,
  and a multi-line value keeps one background across its wrapped lines.

- **`container_pids_limit` default raised to 16384 (#2160).** The
  deploy-wide workspace process-count cap shipped at 512 (#2030), which
  build-heavy workloads (nix/devenv flake evaluation, libgit2 threaded
  packfile/git-cache unpacking) exceed on multi-core hosts, failing with
  `pthread_create` EAGAIN. The default is now 16384; still bounded and
  overridable per-deploy (`klangkd.yaml` / `KLANGKD_CONTAINER_PIDS_LIMIT`)
  or per-workspace (`pids_limit`, #864).

- **Bumped `@earendil-works/pi-coding-agent` to `0.83.0` (#2049).**

- **`none` auth mode unsupported with Docker host image (#1391).** The Docker
  image uses `KLANGKD_AUTH_MODES=password` by default.

- **Clanker agent is opt-in (#1977).** Requires `KLANGKD_FEATURES_ENABLE=chat`
  and `KLANGKWS_FEATURE_CHAT_AGENT_ENABLED=true`.

- **Chat tab is opt-in (#1976).** Dormant unless `KLANGKD_FEATURES_ENABLE`
  includes `chat`.

- **`forward-agent` on by default (#1923, #2000).** New CLI configs ship
  `forward-agent: true`. Set `false` for untrusted workspaces.

- **Environment variables split into four families (#1653).** `KLANGKD_*`
  (server), `KLANGKBUILD_*` (build), `KLANGKWS_*` (container-injected),
  `KLANGK_*` (CLI). See [Environment variables](docs/reference/environment.md).

- **Soliplex vendored locally (#1686).** Declared via `path:` in
  `plugins.yaml`; no network fetch. Still dormant by default.

- **Default features: `beep, bobdobbs, boingball, browser-fetch, celebrate,
git-credential` (#1700).** `pig-latin` removed; `word-count` dormant.

- **CLI/server XDG paths (#1646).** Server: `~/.config/klangkd/` +
  `~/.local/state/klangkd/`. CLI: `~/.config/klangk/klangk.yaml` +
  `~/.local/state/klangk/klangk-state.yaml`.

- **`KLANGKD_STATE_DIR` defaults to `$XDG_STATE_HOME/klangk` (#1644).**

- **CLI renamed `klangkc` → `klangk` (#1615).**

- **`frontend_dir` default moved in-package (#1600).** Source-tree deployments
  must set `KLANGKD_FRONTEND_DIR`.

- **SIGHUP reloads configuration (#1587).** Invalid config is denied; the
  runtime stays on last-known-good. See [Process Signals](deployment/signals.md).

- **`KLANGKD_CORS_ORIGINS` and `KLANGKD_FRONTEND_DIR` reloadable on SIGHUP
  (#1610).**

- **`KLANGKD_PORT` is the proxy browser port (#1542).** Unset = headless mode.
  `KLANGKD_EGRESS_PORT` must differ from `KLANGKD_PORT`.

- **`KLANGKD_LISTEN` (#1542).** Browser-interface bind address (default
  `127.0.0.1`). UDS path is `KLANGKD_SOCKET`.

- **`KLANGKD_FRONTEND_DIR` setting (#1456).** Override where the Flutter UI
  is served from.

### Deprecated

- **`KLANGKD_PROXY_PORT`** — rename to `KLANGKD_EGRESS_PORT`. If both are set,
  `KLANGKD_EGRESS_PORT` wins (#1542).

### Removed

- **`KLANGKD_SSL_CERT_DIR` (#1523).** Use `<KLANGKD_CUSTOMIZE_DIR>/certs/`.

- **`KLANGKD_AGENT_DISABLED`, `KLANGKD_AGENT_EMAIL`, `KLANGKD_AGENT_HANDLE`
  (#1977).** Use `KLANGKWS_FEATURE_CHAT_AGENT_ENABLED` / `_EMAIL` / `_HANDLE`.

- **`customize/build/` directory (#1663).** Fork the repo and edit
  `plugins.yaml` directly.

- **`instance_metadata` DB table (#1553).** Instance ID is now a file at
  `<data_dir>/instance-id`.

- **`klangk-instance-id` console script (#1565).** Read the file directly.

- **`adopt_orphaned_containers` renamed to `reap_instance_containers`
  (#1554).**

- **`KLANGKD_AUTH_MODES=none` (#1374).** No-login single-user mode. The server
  auto-creates the default user; loopback + proxy ACL keep `/auth/local`
  unreachable from containers. See [Auth Modes](features/auth-modes.md).

- **`klangk admin` command group (#1374).** `admin users ls`,
  `admin users set-password`, `admin invitations send/ls`.

- **`klangk status`** now reports user id and admin status.

- **`KLANGKC_DEBUG_SSH_AGENT` (#1522).** Removed along with the debug logging
  it controlled.

- **`claude-code` and `herdr` features (#1658).** Removed. Drop them from
  custom `features.yaml` if present.

### Breaking

- **(#1653)** Environment variables renamed to `KLANGKD_*` / `KLANGKBUILD_*` /
  `KLANGKWS_*` / `KLANGK_*`. Old `KLANGK_*` names are not accepted. Update
  deploy manifests.

- **"Plugin" → "feature" rename (#1658).** `plugins.yaml` → `features.yaml`,
  `plugins/` → `features/`, `update-plugins` → `update-features`, etc.

- **Feature config keys must start with `KLANGKWS_FEATURE_` (#1662).** Rename
  `KLANGKD_GITHUB_OAUTH_CLIENT_ID` → `KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID`,
  `KLANGKBUILD_BOING_SPEED` → `KLANGKWS_FEATURE_BOING_SPEED`, etc.

- **`KLANGKD_CUSTOMIZE_DIR` moved to `<config_dir>/custom` (#1644).** Move
  contents from `<state_dir>/custom` or set `KLANGKD_CUSTOMIZE_DIR` explicitly.

- **One `klangk` distribution (#1606).** `pip install klangk` yields both
  `klangkd` (server) and `klangk` (client). `import klangk_backend` →
  `import klangkd`. The `klangkc` PyPI distribution is retired.

- **Default auth mode is `none` (#1374).** Set `KLANGKD_AUTH_MODES=password`
  explicitly if you relied on the old default. `none` is loopback-bound and
  safe by construction.

- **OIDC settings no longer change the auth mode (#1419).** Set
  `KLANGKD_AUTH_MODES=oidc` (or `both`) explicitly.

- **`klangk invite` → `klangk admin invitations send` (#1374).**

### Fixed

- **Workspace: no more overlapping "Server unreachable" and
  "Session expired" overlays (#2227).** When the WebSocket closed with
  an auth-failure code (4001/4002, session expired), the client also
  flagged itself disconnected, so the workspace's reconnect overlay
  painted on top of the re-login surface. The client now marks the
  disconnect as auth-caused and the workspace suppresses the reconnect
  overlay in that case, leaving only the re-login path.

- **New terminals from the Flutter "+" are named "bash" (#2179).** A
  terminal created from the browser tab-bar "+" was named with a
  consecutive number ("1", "2", …), inconsistent with window 0 ("bash")
  and the tmux status-bar "+". The server's no-name default is now
  "bash" (tmux permits duplicate window names, so the duplicate guard
  that protects explicit names is skipped for the default).

- **TUI workspace detail: long values no longer wrap into the label
  column (#2190).** The detail table was rendered at the screen width,
  but the value panel is narrower (screen chrome), so the panel re-wrapped
  the pre-folded lines and dropped the value column's hanging indent —
  wrapped continuation lines fell back under the labels. The table is now
  rendered at the panel's actual content width (and trailing padding is
  stripped), so every wrapped line stays aligned in the value column.

- **Recycled-container race on terminal start (#2178).** When the
  workspace container was recycled between terminal start and the
  initial window sync, the server logged a full `TerminalError`
  traceback on every occurrence and the client got no initial terminal
  tab list. The "container gone" condition is now detected and handled
  cleanly: a single warning line, the dead session is torn down, and the
  client gets a user-visible error so it can reopen the terminal.

- **Browser terminals over plain HTTP (#2162).** In the browser UI,
  workspace terminals never started (the pane stayed blank, no shell prompt)
  on deployments served over plain HTTP to a non-localhost host — they only
  worked over HTTPS or localhost. Fixed; terminals now start over plain HTTP
  too.

- **Terminal copy over plain HTTP (#2166).** Copying text from a browser
  workspace terminal did nothing on plain-HTTP deployments (the async
  Clipboard API is secure-context-only). Copy now falls back to
  `document.execCommand('copy')` in insecure contexts.

- **Terminal tab strip stays in sync with tmux (#2171, #2161).** A new
  terminal added from the tmux status bar now appears as a tab on every
  connected Flutter client, a closed terminal's tab closes everywhere, and
  switching the active tmux window switches the active Flutter tab. Driven by
  a persistent per-workspace tmux control-mode watcher (no per-tick polling).

- **Flutter "+" no longer steals focus (#2176).** Creating a terminal from
  the Flutter tab-bar "+" keeps focus on the currently-selected tab instead
  of switching to the new one. The active-window follow now ignores a
  brand-new window becoming active (only switches to an existing window).

### Security

- **`git-credential-klangk`: secrets redacted from debug output (#1938).**

- **Read-only terminal input whitelist (#1716).** Spectators can no longer
  inject arbitrary escape sequences (including OSC 52 clipboard access).

- **Bumped `pyasn1` to 0.6.4 (CVE-2026-59886, #1730).**

- **Admin seeding is first-boot-only (#1622).** Config can no longer mint or
  reset admins once an admin exists.

- **Proxy denies container source IPs on the catch-all (#1376).** Containers
  can only reach `/llm-proxy/`, `/api/v1/browser-delegate`, and
  `/api/v1/workspaces/post-chat-message`.
