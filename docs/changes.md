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

- **Per-request duration for egress-consent verdicts (#2328).** A verdict
  now carries a `duration` (`once` | `5m` | `15m` | `1h` | `1d` | `1w` |
  `restart` | `forever`, default `restart`). The `consent-decide` TUI shows a
  per-row duration selector (click to choose; selecting does NOT submit -- only
  Allow/Deny submit with the chosen duration). The sidecar honors it: an allow
  learns the IP for that long (`once` = this connection only, no learn); a deny
  REJECTs (tcp-reset) for that long. `restart` = the workspace container's
  lifetime; `forever` = the workspace's lifetime (persists across container
  restarts via klangkd -- the cross-restart persistence is a follow-up; at the
  sidecar level it maps to a long in-memory TTL). Recorded on the
  `egress_consent` row.

- **Active-egress-decisions snapshot (`egress_rules` frame) (#2338).** The
  consent-decider WebSocket now pushes an `egress_rules` snapshot on connect
  (and refreshes it after each verdict): the workspace's in-effect consent
  verdicts (allows and denies still within their duration) plus its static
  allow-list, for the upcoming rule-management view (#2335). The
  `egress_consent.duration` column is now read back and constrained by a DB
  `CHECK` to the documented duration values (mirroring `DURATIONS`).

- **`klangk consent-decide <workspace>` (#2310).** A live Textual client that
  connects to a workspace's consent-decider stream and shows its held egress
  requests (blocked destinations the network sidecar is holding for a
  verdict), with a countdown to auto-deny. Press `a` to allow (once) or `d` to
  deny; accepting lets that exact held connection proceed while a deny (or the
  countdown hitting zero) fails it. It pings every 15s to stay registered as
  the workspace's live decider and reconnects on drop; while no client is
  attached, held requests auto-deny (fail-closed). Requires terminal access to
  the workspace.

- **Embedded network sidecar image (#2301).** The all-in-one host image
  (`scripts/build-host-image.sh`) now embeds the network sidecar image as a
  tarball and `podman load`s it on first startup, mirroring the workspace
  image. A default host-image deployment with FQDN egress enabled can start
  a workspace with `allowed_domains` without separately building or pulling
  the sidecar.

- **Egress consent recording (#2242).** The network sidecar records every
  blocked destination to the `egress_consent` table: for **static**
  workspaces (the default) as `denied` with no human (`decided_by` NULL),
  immediately; for **interactive** workspaces as a `pending` request a
  human can allow/deny via the consent UI (#2244, not yet wired) before it
  auto-expires (`egress_consent_timeout`, default 30s; rate-limited per
  workspace via `egress_consent_rate_limit`, default 50). The sidecar
  consumes its own NFQUEUE (`-j NFQUEUE --queue-num 5139`; it is the netns
  owner with `NET_ADMIN`) and POSTs each blocked packet's destination to
  klangkd's consent endpoint (workspace-JWT-authenticated via Caddy's
  forward_auth); it also forwards denied DNS queries with their domain
  names (NFQUEUE only carries raw IPs). Static mode is now strictly better
  than the old silent-deny: it records denied attempts for audit/review.
- **`scripts/consent-watch.py`** — a tiny `rich`-based live view of a
  workspace's egress-consent request history (pending/allowed/denied/expired),
  for debugging interactive mode (#2242).
- **`scripts/consent-decide.py`** — an interactive CLI to accept/deny a
  workspace's pending egress-consent requests at runtime (the command-line
  decide flow for #2244); accept marks a request `allowed` and adds the
  destination to the workspace's allow-list (applies on next recreate), deny
  marks it `denied` (#2242).
- **Consent decider registry + `/ws/consent-decider` (#2308).** Interactive
  egress consent is now runtime state: a workspace's blocked egress is held
  for a decision only while a consent decider is registered for it (or
  deploy-wide), over a new decider WebSocket (`consent_decider_timeout`,
  default 45s, reaps silent deciders). **Behavior change:** an `interactive`
  workspace with no decider registered now records blocked egress as a
  static denial instead of queuing it — it needs a live decider to queue.
  The decider client itself lands with #2310.
- **`/ws/egress-sidecar` + consent hold/resolve coordinator (#2311).** The
  network sidecar's blocked-egress path gains a synchronous hold: a
  workspace's blocked egress is held in-flight (pending a human verdict) only
  while a consent decider is registered (#2308); with no decider it is denied
  at once as a static denial (no hold, no queued row). The sidecar connects
  over a new `/ws/egress-sidecar` WebSocket (workspace-JWT auth) and receives
  a verdict per blocked destination; the in-process coordinator fail-closes
  on timeout or shutdown (no leaked allow, no hung connection). **Scope: this
  is the klangkd coordination half** — the sidecar's kernel-level hold
  (suspending DNS queries, deferring NFQUEUE verdicts) + its WS client land in
  a follow-up, and decider fanout/verdict reception with #2244.
- **Decider WebSocket fanout + verdict reception (#2244).** Held egress
  requests now reach a human: when the coordinator creates a hold it broadcasts
  an `egress_request` frame to every live decider for the workspace (and
  deploy-wide) over the `/ws/consent-decider` socket; a decider's `verdict`
  message is fed to `resolve()`, which records the decision, releases the held
  sidecar connection, and broadcasts `egress_resolved` so co-deciders drop it
  (first-decision-wins). A decider connecting mid-flight gets a snapshot of the
  workspace's current pending requests. The #2308 authz gap is closed: a
  workspace-scoped decider now needs `terminal` access to that workspace, a
  deploy-wide decider needs admin (else the socket is closed `4003 Forbidden`),
  and a verdict is honored only for the decider's own workspace. The first
  consumer is the `consent-decide` client (#2310).
- **Sidecar consent gates the connection SYN (#2311, #2324).** The network
  sidecar holds a non-allow-listed connection's SYN (NFQUEUE) pending the
  consent verdict instead of the DNS query: a denied name now _resolves_ (the
  workspace gets the IP) and the first packet to that IP is queued -- `allow`
  learns the IP + lets it proceed, `deny`/timeout/WS-down fail-closes -- a
  denied connection gets a RST via a temporary REJECT (tcp-reset) rule so it
  fails at once (ECONNREFUSED), not after tcp_syn_retries ~127s (a static
  workspace or an unreachable klangkd behaves exactly as before; no hang).
  Gating the SYN gives the human the kernel's connect timeout
  (`tcp_syn_retries` ~127s) instead of a DNS resolver's <=30s `getaddrinfo`
  cap. `KLANGKD_EGRESS_CONSENT_TIMEOUT` and `KLANGKNETWORK_EGRESS_HOLD_TIMEOUT`
  defaults rise 30 -> 120s to use that window; SYN retransmits reuse the cached
  verdict so they don't each re-prompt. Distinct concurrent flows are held in
  parallel (#2329): the NFQUEUE consumer is loop-driven (`get_fd` + `add_reader`)
  - non-blocking (each held SYN is retained + handed to a verdict task), not a
    single blocking thread that serializes flows behind the first. New sidecar
    config: `KLANGKNETWORK_EGRESS_VERDICT_CACHE_TTL` (how long to reuse a SYN verdict,
    default 120s) and `KLANGKNETWORK_EGRESS_REJECT_TTL` (how long a deny keeps its
    REJECT tcp-reset rule so the connection fails fast, default 10s).
    `KLANGKNETWORK_EGRESS_HOLD_LIMIT` is removed (the DNS-path hold bound; the SYN
    path is bounded by the iptables rate-limit). The proxy is asyncio + the sidecar image gains the `websockets`
    dependency; the legacy fire-and-forget POST endpoint is superseded (recording
    happens on the WS path, removal tracked in #2318).
- **FQDN egress allow-list wildcards, per-domain port scoping, and learned-IP
  TTL (#2256).** `allowed_domains` now accepts `*.domain[:port]` wildcards
  (subdomains only — distinct from a bare `domain`, which also matches the
  apex). `host:port` now scopes a learned IP to that single TCP port — a
  **behavior change**: previously a learned IP was reachable on **all** ports
  regardless of the spec's port, so an operator who relied on that (e.g.
  reaching `:22` on an IP resolved under a `:443` spec) will now see that
  traffic blocked; add an explicit spec for the extra port (a bare `host`
  keeps all-ports, unchanged). The sidecar's entrypoint now applies the same
  port scoping to CIDR specs like `10.0.0.0/8:443` (previously stripped).
  Resolved IPs are allow-listed only for the DNS response's TTL — the proxy
  re-resolves on each query and a background sweep removes the rule when the
  TTL elapses, so stale IPs no longer linger. See
  `docs/features/egress-filtering.md`.

- **`klangk-build-nix-seed` + `klangk-load-nix-seed-btrfs` — build/load the
  `/nix` seed from a wheel install (#2225).** Two console scripts (shipped with
  `pip install klangk`) replace the former devenv-only shell scripts.
  `klangk-build-nix-seed` builds the shared nix seed dir — no source tree, no
  devenv: it bundles the seed Dockerfile in the wheel, drives the **configured**
  podman (`KLANGKD_PODMAN_BIN`), and writes `<dir>/nix` + `<dir>/nix.conf`
  (`--update` rebuilds in place, `--no-cache` forces fresh nix/devenv).
  `klangk-load-nix-seed-btrfs <seed-tree> <btrfs-parent>` loads the output into
  a btrfs subvolume for the `btrfs-snapshot` backend (the fuse backend points
  `nix_seed.path` at the dir directly). Both supersede
  `scripts/build-nix-seed.sh` + `scripts/load-nix-seed-btrfs.sh` (the tools
  work in dev too — the console scripts are in the devenv venv, with a
  source-tree Dockerfile fallback + podman resolved from the devenv PATH).

- **Per-workspace "Mount /nix dir" toggle in all create/edit surfaces
  (#2233).** The per-workspace nix toggle is now exposed in the workspace
  edit panel (Flutter) and in the TUI create and edit screens, and is
  labeled "Mount /nix dir" everywhere (the Flutter create dialog's "Nix"
  checkbox is renamed to match). The toggle stays hidden when the server
  has no `nix_seed` backend configured; the underlying setting remains the
  boolean `nix`.

- **Interactive egress consent mode (#2239, #2240, #2241).** Workspaces can
  now set `egress_mode: "interactive"` (API/CLI). In this mode the sidecar
  queues otherwise-blocked packets to its own NFQUEUE consumer (group 5139),
  which forwards each destination to klangkd for consent (#2242); the human
  decide/notify UI lands with #2244. Until then, interactive mode denies
  unmatched traffic exactly like static mode (no prompts; no security gap)
  while the monitor records the attempts. See
  `docs/features/egress-filtering.md`.
- **FQDN egress network sidecar + DNS proxy (#2250, #2253).** New
  `klangk-network-sidecar` image (`src/containers/network/`) that runs a
  FQDN DNS proxy in a `NET_ADMIN` container sharing a filtered workspace's netns.
  It intercepts the workspace's DNS (nat REDIRECT of its configured resolvers),
  applies an allow-list, forwards allowed queries to a distinct upstream, and
  allow-lists the resolved IPs at runtime — solving DNS round-robin (a filtered
  workspace reaches an allowed domain on whatever IP it actually resolves). Part
  of the FQDN egress build (#2250); klangk lifecycle wiring is #2254.

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

- **Shared base `/nix` store seed (the `klangk-build-nix-seed` command)
  (#2200).** A reproducible build step that produces a self-consistent `/nix`
  tree (the store, the nix DB, and a base profile with nix and devenv) and an
  `/etc/nix/nix.conf` (flakes, nix-command, and pre-configured binary caches),
  deployed alongside klangk as a host-side tree rather than baked into a
  workspace image. Run via `klangk-build-nix-seed`. Consumed by #2201.

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

- **New workspaces default to `egress_mode=interactive`.** The default is
  `interactive`, so held egress requests reach a decider out of the box without
  a manual per-workspace flip (`EGRESS_MODE_DEFAULT` in `model/workspaces.py`);
  set a workspace to `static` to opt into silent deny + record instead.

- **Workspace + network sidecar container names and labels (#2286).** A
  workspace and its network sidecar now share a `klangk.workspace=<id>` +
  `klangk.role` label (so one `podman ps --filter label=klangk.workspace=<id>`
  returns the pair), and both container names embed the slugified workspace name
  on a shared `id[:8]` tail so `podman ps | grep <partial-name>` finds both.
  This renames the old write-only labels `klangk.workspace-id` /
  `klangk.network-sidecar` to the shared key, and sidecar removal is now
  label-based (robust to renames/restarts). A renamed workspace keeps its old
  slug until the container restarts; correlation via `klangk.workspace` is
  unaffected.

- **Egress filtering enforcement moved into the network sidecar (#2255).**
  The per-workspace egress ruleset — default-deny OUTPUT, loopback,
  established, the DNS REDIRECT + FQDN proxy, static CIDR allows, the
  backend gateway, IPv6 default-deny, and interactive NFLOG — now lives
  entirely in the network sidecar's netns (which the filtered workspace
  shares via `--network container:`). The create-time OCI hook model
  (`klangk-netfilter.sh` + `netfilter.py`'s annotation/`--hooks-dir` path)
  and the post-start `allow_backend_gateway` nsenter step are removed; the
  sidecar is the only egress model (fail-closed if it can't start). The
  sidecar image (`network_sidecar_image`) defaults to
  `klangk-network-sidecar`, so filtering works out of the box without
  `KLANGKD_NETFILTER_HOOKS_DIR`.

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

- **A reconnecting consent decider no longer re-shows an already-resolved
  request (#2345).** The decider (re)connect snapshot read pending requests
  from the DB, so a request whose `egress_resolved` broadcast was lost on
  the prior (dead) connection could be replayed + linger with no further
  resolve to clear it. The snapshot now intersects DB-pending rows with the
  coordinator's in-memory hold set (`_holds`), which `resolve` pops
  synchronously before the DB write + broadcast -- so a resolved request is
  never replayed, regardless of broadcast loss.

- **Denied egress connections now fail fast reliably (#2345).** A denied
  held connection was meant to return `ECONNREFUSED` at once via a temporary
  `REJECT --reject-with tcp-reset` rule, but the rule often never fired: the
  original SYN, NFQUEUE'd then dropped, left an unconfirmed conntrack entry
  that entangled the kernel's SYN retransmit, so the connection instead hung
  for its full timeout (curl exit 28) instead of being refused (exit 7). The
  network sidecar now forges the RST directly from the queue callback (it gets
  `CAP_NET_RAW`, and `rp_filter` is disabled in its netns), so `connect()` gets
  `ECONNREFUSED` immediately, independent of the race. A SYN retransmit during
  the consent hold no longer spawns a duplicate request. The REJECT rule stays
  as a backstop.
- **`restart`-duration consent verdicts are now reaped when a workspace
  container (re)starts (#2346).** A `restart` verdict means "for the
  container's lifetime" -- the sidecar honors it via an in-memory rule that
  dies on restart, but the `egress_consent` row persisted, so the
  in-effect view (`list_active`) reported stale `restart` allows/denies after
  a restart. They are now deleted on container (re)start (the create path,
  not the already-running reconnect), so the recorded set matches what the
  sidecar actually enforces. `forever`/time-bounded/`once`/pending/static
  rows are left untouched.

- **`consent-decide` no longer crashes when a held request arrives while a
  row is mid-mount (#2327).** `_refresh`'s in-place diff treats a just-appended
  row (its `request_id` is set synchronously) as "existing" on the very next
  refresh, but the row's child widgets mount asynchronously -- so repainting
  it could fire `query_one(".req-host")` before mount and raise `NoMatches`,
  aborting the refresh. The repaint now tolerates the mount gap (the next tick
  finishes it once mounted). Surfaced under rapid concurrent requests.

- **`consent-decide` rows are now compact, so multiple held requests show
  at once (#2327).** Each held-request `ListItem` defaulted to `height:
auto`, which expanded to fill the whole `ListView`, so with two or more
  concurrent requests only the first was visible and the rest lurked below
  the fold (you had to scroll the list to see them). Rows are now fixed at
  two lines (host line + Allow/Deny), so the queue shows every pending
  request without scrolling.

- **Orphaned pending egress-consent requests no longer replay after a
  klangkd restart.** On startup every still-`pending` row is an orphan (its
  in-memory hold died with the prior process), so they're reaped to `expired`
  up front — otherwise the decider snapshot replayed stale requests that could
  never be resolved (the "orphans return" in `consent-decide`).

- **Consent verdict `decided_by` now stores the stable user id, not the
  volatile email (#2244).** `egress_consent.decided_by` is `REFERENCES
users(id)`, so the decider handler passing the decider's email violated the
  foreign key and every verdict failed with an `IntegrityError`. It now
  threads the user id through `_handle_verdict`.

- **Egress-site `forward_auth` no longer breaks WebSocket upgrades
  (#2319, #2322).** Caddy's site-level `forward_auth` copied the WS
  `Upgrade` headers onto its auth subrequest, so uvicorn treated the
  (HTTP) `verify-workspace-token` check _itself_ as a websocket — no ws
  route at that path → the catch-all `StaticFiles` asserted
  `scope["type"] == "http"` → HTTP 500. Every WS through the container-egress
  port (the sidecar's `/ws/egress-sidecar`) failed this way even after #2321
  added the handle. `forward_auth` now takes a `@notWs` matcher so WS
  upgrades skip it; the egress WS endpoint self-authenticates via the
  `Authorization` header.

- **Egress-sidecar WebSocket on the egress port (#2319).** The network
  sidecar's held-egress WS (`/ws/egress-sidecar`) was never added as an
  explicit handle on the container-egress Caddy site, so every sidecar WS
  upgrade fell through to the catch-all `StaticFiles` (which asserts
  `scope["type"] == "http"`) and failed with HTTP 500. The egress site now
  reverse-proxies that WS to the app, and the sidecar sends its workspace JWT
  as an `Authorization: Bearer` header (matching the site's `forward_auth`
  and the legacy consent POST) instead of a `?token=` query param.

- **Sidecar token file retention (#2309).** A periodic sweep now removes
  leftover per-workspace token files under `data_dir/ws-tokens/` once the
  workspace row is gone, so they no longer accumulate one file per workspace
  ever started. The sweep piggybacks on the idle container cleanup loop (at
  most every 5 minutes) and leaves the token in place for any workspace that
  still exists, including stopped ones awaiting a restart.

- **Starting/restarting a workspace with a bad bind mount returns a 400, not
  a 500 (#2157).** A workspace whose extra mount points at a nonexistent host
  path previously raised an unhandled `ValueError` from the volume pre-check,
  surfacing as a 500 traceback. The `/workspaces/{id}/start` and `/restart`
  endpoints now catch it and return HTTP 400 with the message (e.g. "Bind mount
  source does not exist: /path"), matching the create/update endpoints and
  the WebSocket start path. The pre-check stays (it catches typos); it just no
  longer 500s in the HTTP path.

- **Network sidecar start recovers from host-port conflicts (#2293).** Since
  #2291 a filtered workspace publishes its host ports on the network sidecar,
  so a bind conflict there could fail the workspace start with no retry (unlike
  the non-filtered workspace path, which self-heals). The sidecar start now
  shares the workspace path's port-conflict recovery: it removes the stale
  holder and retries with back-off.

- **Stopping a workspace now tears down its network sidecar even when the
  workspace isn't tracked in the in-memory registry (#2286).** A workspace
  started by autostart or a prior klangkd session, then stopped from the TUI
  (or `/stop`, `/delete`, `/restart`), previously left its network sidecar
  running until the next start or the startup reaper. The stop path now takes
  the workspace id directly from the endpoint and removes the sidecar by label.

- **Network sidecar: filtered workspaces can host apps again (#2267).** A
  filtered workspace shares the network sidecar's netns, so `--publish` on the
  workspace itself was silently discarded by podman and configured host ports
  (`KLANGKWS_PORT_MAPPINGS`) vanished with no warning. Host ports are now
  published on the network sidecar instead (it owns the netns), forwarding into
  the shared netns to the workspace's listener. IPv4 clients work; the sidecar
  still kills IPv6 (#2277), so IPv6-only clients to a published port will fail
  until that lands.

- **Network sidecar: no longer leaks across a process restart (#2248
  review).** Reconnecting to a running filtered workspace after a klangkd
  restart now re-tracks its sidecar, so stopping the workspace tears the
  sidecar down. Previously the in-memory tracking was lost on restart and the
  reconnect path didn't restore it, leaving the sidecar (NET_ADMIN + DNS
  proxy) running until the next start or the instance reaper.

- **Network sidecar: stop/start race on the deterministic sidecar name
  (#2265).** A concurrent stop and start of the same filtered workspace could
  race on the sidecar's per-workspace name: the stop removed the sidecar the
  start had just created (leaving the workspace joined to a removed netns),
  or the start collided with a lingering old sidecar and refused to come up.
  The sidecar teardown is now serialized under the workspace lock with a
  re-verify, and the start clears any stale same-named sidecar first.

- **Network sidecar: a filtered workspace now waits for the sidecar's DNS
  proxy to be ready before starting (#2277).** Previously the workspace joined
  the sidecar's netns the instant it was started, before the entrypoint had
  applied `iptables -P OUTPUT DROP` or the proxy had bound — a window of
  unrestricted egress. The sidecar start now polls for the proxy's
  `dns-proxy listening` line and refuses to start the workspace (fail-closed)
  if the proxy exits or never binds.

- **Network sidecar: the DNS proxy no longer dies on a single transient
  failure (#2278).** A failed `iptables` call (learning an IP) or a `sendto`
  to a vanished client used to escape the proxy's main loop and kill PID 1,
  leaving the workspace without DNS while learned allow-rules persisted. The
  learn+respond path now swallows such errors so one bad packet drops only that
  response.

- **`build-workspace-image.sh` no longer rebuilds the workspace image on
  every server restart (#2273).** The image-creation-time "verify the image
  is newer than every source file" check was unreliable (podman inspect
  timestamps don't reflect storage-layer caching / layer reuse) and always
  re-evaluated to "rebuild", defeating the stamp cache. Removed: when the
  stamp hash matches, the build is now skipped and the stamp trusted.
- **Per-workspace egress filtering now actually fires on rootless podman
  (#1365).** The OCI hook ran at the `createContainer` stage but drove
  iptables through `nsenter` on the init pid — at that stage the hook is
  already inside the container network namespace (and pid is 0), so no
  rules were installed and filtered workspaces ran unrestricted. The hook
  now calls `iptables`/`ip6tables` directly. (The former
  `sysctl net.ipv6.conf.*.disable_ipv6=1` was dropped — at createContainer
  it runs before pasta configures the netns and makes pasta's IPv6 address
  setup fail; `ip6tables -P OUTPUT DROP` alone carries the v6 default-deny.)
  DNS resolvers can no
  longer be read from the container's `/etc/resolv.conf` (netavark writes
  it only after the create hooks, and the hook runs in the host mount
  namespace), so the server detects the host's upstream resolvers, passes
  them to the container via `--dns`, and mirrors them in a
  `klangk.netfilter.resolvers` annotation the hook allows on `:53`.
  `--hooks-dir` is also passed to `podman start` (podman 5.x reads it only
  at start). The backend gateway (`host.containers.internal`) is
  allow-listed post-start — the createContainer hook can't read the
  container's `/etc/hosts` (pid=0), so after start the server resolves the
  gateway IP and inserts an `ACCEPT` rule atop the container's `OUTPUT`
  chain. DNS round-robin domains (create-time vs runtime resolution) remain
  a known limitation of IP-pinning.
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

- **Network sidecar: filtered workspaces with `allow_sudo` drop `net_raw` as
  defense-in-depth (#2276).** The primary `SO_MARK`-bypass guard is
  user-namespace isolation — the workspace runs in its own keep-id userns,
  distinct from the one that owns the network sidecar's netns, so its caps are
  not valid there. A filtered+`allow_sudo` workspace additionally drops
  `net_raw` from the bounding set so that if that isolation ever does not hold,
  `sudo`→root still can't mark; the setuid-ping bridge is disabled for such
  workspaces.

- **Network sidecar: filtered workspaces now require a non-empty
  `KLANGKD_USERNS` (egress-stack review).** An empty `KLANGKD_USERNS` made the
  workspace share the network sidecar's user namespace, silently reopening the
  `SO_MARK` egress bypass. A filtered workspace (`allowed_domains` set) now
  refuses to start when `KLANGKD_USERNS` is empty (fail-closed). The default
  (`keep-id:uid=1000,gid=1000`) is unaffected; only deployments that explicitly
  set `KLANGKD_USERNS=""` are impacted.

- **`git-credential-klangk`: secrets redacted from debug output (#1938).**

- **Read-only terminal input whitelist (#1716).** Spectators can no longer
  inject arbitrary escape sequences (including OSC 52 clipboard access).

- **Bumped `pyasn1` to 0.6.4 (CVE-2026-59886, #1730).**

- **Admin seeding is first-boot-only (#1622).** Config can no longer mint or
  reset admins once an admin exists.

- **Proxy denies container source IPs on the catch-all (#1376).** Containers
  can only reach `/llm-proxy/`, `/api/v1/browser-delegate`, and
  `/api/v1/workspaces/post-chat-message`.
