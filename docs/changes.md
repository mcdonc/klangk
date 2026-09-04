# Changelog

All notable changes to klangk are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and each version's section is also prepended to its GitHub Release notes (see
[Releasing](development/releasing.md)).

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
     Release body. Keep one `## \[<version>]` section per release; unreleased
     changes accumulate under `## \[Unreleased]`. The opening bracket in these
     headings is escaped so the docs build does not parse them as Markdown
     link references (#3142); they render as plain `[...]` text and the
     release workflow matches both the escaped and bare forms. -->

## \[Unreleased]

### Breaking

- **Members can create workspaces by default (#3137).** The seeded
  `create-workspace` Allow on `/workspaces` now targets the `members`
  group (which every new user joins) in addition to `admins`, so a
  stock multi-user deploy is self-service out of the box — bounded by
  the admission/quota controls (`KLANGKD_ADMISSION_*`,
  `max_running_workspaces_per_user`, `volume_quota_per_user`) and
  `allowed_images` / `allowed_mount_roots`. **On upgrade, migration
  m0029 appends the grant after any existing `/workspaces` rows**, so
  existing deployments flip too; a deploy that wants the old
  admin-only posture adds one explicit Deny for `members` (or
  `Authenticated`) ahead of the Allow in the ACL editor. See
  [ACL System](https://klangk.github.io/klangk/reference/acl/).

- **`KLANGKD_PER_HANDLE_HOME` is now a ceiling, not a default (#3135).**
  The flag no longer just pre-selects the home layout for new
  workspaces — it gates whether per-handle homes are permitted at all
  (the same shape `KLANGKD_ALLOW_SUDO` got in #3047). While it is
  `false` (the default), **every** workspace gets the shared
  `/home/klangk` regardless of its stored `per_handle_home` value: a
  stored `true` (including the population migration 0009 backfilled
  for pre-feature workspaces) is inert, clamped at the next
  connect/start and never rewritten — no create/edit request is
  rejected. Operators whose workspaces rely on per-handle homes must
  set `KLANGKD_PER_HANDLE_HOME=true` on upgrade. With the ceiling on,
  behavior is unchanged (workspaces choose either layout; an omitted
  create field stores the flag's value). The _Per-handle home_ toggle
  is hidden in the web dialog/settings panel and the TUI forms while
  the ceiling is off (`per_handle_home_available` on `/config`), and
  the CLI `--per-handle-home` flag cannot raise a workspace past the
  ceiling. Reloadable on SIGHUP; applies to containers started after
  the change. See
  [Workspaces](https://klangk.github.io/klangk/features/workspaces/).

- **Per-workspace sudo is now opt-in (#3046, #3047).** `KLANGKD_ALLOW_SUDO`
  no longer grants passwordless sudo by itself — it is now only the
  ceiling that permits a workspace to opt in. Sudo is on for a workspace
  only when its settings bag stores `allow_sudo: true` (the _Allow sudo_
  toggle, which now defaults to unchecked in the Flutter dialog/settings
  panel and the TUI form, or `klangk create --sudo` / `--sudo` on edit);
  an absent key means locked down, and a `true` can never raise sudo
  past `KLANGKD_ALLOW_SUDO=0`. **On upgrade, existing workspaces with
  no stored `allow_sudo` key lose sudo at their next container start**
  (no bag migration runs) — opt them in with `klangk edit <ws> --sudo`
  if they need it. See [Container
  packages](https://klangk.github.io/klangk/features/container-packages/).

- **Volumes are an admin surface (#2993).** `GET /volumes` now checks
  the new `view-volumes` permission (the admin Volumes tab's listing
  gate), while `POST /volumes` and `DELETE /volumes/{name}` keep
  `manage-volumes` — both seeded Allow for the `admins` group only,
  so non-admin users lose volume list/create/delete access on
  upgrade (migration m0026 replaces the old rows; custom operator
  rows that don't match the seeded shapes survive below the new admin
  rows — re-grant via the ACL editor if a deploy wants self-service
  volumes back). The tab lists and deletes volumes (delete needs
  `manage-volumes`); there is no create surface, and
  `manage-volumes` holders may delete any instance volume. The
  listing now returns the whole inventory — creator provenance,
  using-workspace names, search, and paging — in a
  `{volumes, page, page_size, total}` envelope documented in
  `docs/reference/api-endpoints.md`; `klangk volumes ls` ships with
  it, but external API consumers must read the envelope.

- **Deploy-wide consent decider removed (#2976).** The
  `/ws/consent-decider` handshake without a `?workspace=` param is now
  refused (HTTP 403): consent authority is strictly per-workspace
  (`egress-consent` on `/workspaces/{id}`), and `manage-server-schedule`
  no longer authorizes any consent path. Operators who relied on a
  standing admin decider covering every interactive workspace lose that
  override: a workspace with no connected member decider now reverts to
  its static allow-list. The `klangk shell` popup decider and the web
  workspace page both register a decider whenever the member holds
  `egress-consent`, so interactive workspaces keep working for their own
  members.

- **Deploy capability toggles moved off the images listing (#2994).**
  `GET /api/v1/images` no longer returns `nix_available` /
  `sudo_available` — read them from the new authenticated-only
  `GET /api/v1/config` fields of the same names. Hand-built clients
  reading the toggles off the images response must switch to `/config`.
  Migration `0025` also removes the retired seed's Deny Everyone row on
  `/images` (it gated no route): authenticated users' effective
  permissions on `/images` now include the `view` inherited from `/`,
  visible in `/my-permissions` — informational only.

- **Workspace-sphere permission names (#2946).** Every stored ACE and
  every client that checks a workspace permission must use the new
  specific names: `create-workspace` (on `/workspaces`),
  `edit-workspace`, `delete-workspace`, `duplicate-workspace`,
  `transfer-workspace` (replaces `admin` on the workspace),
  `monitor-workspace`, `export-workspace`, `share-workspace`,
  `share-advanced` (replaces `change-acls`), and `files-view`
  (replaces `files`). Migration m0022 renames the stored ACE rows
  automatically, including per-workspace role groups; `view`,
  `terminal`, `files-download`, `files-write`, and the egress/shared-
  terminal names are unchanged. Scripts and hand-built clients that
  check or grant the old names must rename them the same way.
  Lifecycle control also splits out of `terminal`: `start-workspace`,
  `stop-workspace`, and `restart-workspace` are now checked on their
  own. m0022 grants the trio to every existing workspace's
  `coders-*`/`collaborators-*` role groups (matching the fresh seeds);
  spectators no longer hold lifecycle control — re-grant the trio
  manually if a spectator group should keep it.

- **Self-service surfaces are permission-gated (#2946).** The volumes
  API and the images listing now check `manage-volumes` and
  `view-images` on their own resources — seeded Allow for
  Authenticated (m0023 seeds existing deployments), so default
  behavior is unchanged; a deploy can now deny them per user/group
  via the ACL editor. `GET /users/search` (member-picker type-ahead)
  checks `search-users` on `/users`, also Allow Authenticated by
  default. The LLM proxy is gated separately by its own
  workspace-token requirement (#2959).

- **`/admin/*` API paths moved to first-class resources (#2944).**
  Scripts and clients calling the old `/api/v1/admin/users*`,
  `/api/v1/admin/groups*`, `/api/v1/admin/invitations*`,
  `/api/v1/admin/schedule*`, `/api/v1/admin/events`, or
  `/api/v1/admin/acl*` paths must switch to `/api/v1/users*`,
  `/api/v1/groups*`, `/api/v1/invitations*`, `/api/v1/server/schedule*`,
  `/api/v1/events`, and `/api/v1/acl/*` respectively (the old paths
  404). The CLI, web frontend, e2e suites, and seeds are already
  migrated. The `/admin` `*` wildcard no longer covers these surfaces —
  the walk from the new resources never passes through `/admin` — so
  delegations granted on the old `/admin/users`, `/admin/groups`, …
  sub-resources match nothing anymore: re-grant the `manage-*`
  permission on the new first-class resource. The pre-existing
  `/groups` Allow `create` seed is migrated automatically (m0021); any
  other custom `/groups` rows are left for a manual re-grant.

- **Hand-crafted `admin` ACEs stop matching split routes (#2940).** ACLs
  granting the literal `admin` permission on `/admin` (rather than the
  seeded `*` wildcard) no longer satisfy the per-tab endpoints — grant
  the tab permission (or `*`) instead. Default deployments are
  unaffected. If you granted `container-events` ACEs while running
  main, rename them to `manage-events` (the feature was never in a
  release). Likewise, pre-#2940 `/groups` Allow `create` delegations
  are now inert: group creation is gated by `manage-groups` on
  `/admin/groups`, and that permission covers the whole Groups tab
  (edit, delete, member management) — re-grant accordingly.

- **The seeded admin group is renamed to `admins` (#2934).** Fresh
  installs seed a group named `admins`; upgrading renames the `admin`
  group in place (memberships and ACLs keep pointing at the same
  group). If a group named `admins` already exists (created manually),
  boot fails: stop klangkd, rename that group directly in SQLite
  (`UPDATE groups SET name = 'admins-manual' WHERE name = 'admins';`
  on `<data-dir>/klangk.db`), and restart. OIDC login hooks must
  return `"admins"` from `on_login` **before the first post-upgrade
  login** — an unchanged hook auto-creates a permissionless `admin`
  group and its membership diff-sync strips the user's synced `admins`
  membership at their next login.

- **`enable_ping` is removed; workspaces never hold `CAP_NET_RAW` (#2347).**
  The `KLANGKD_ENABLE_PING` setting is gone (ignored on an existing config)
  and every newly created workspace container launches with
  `--cap-drop net_raw`; unprivileged `ping` inside a workspace no longer
  works (the setuid-ping / `ping_group_range` / `setcap` alternatives all
  fail under rootless podman, #2045). Applies to containers started after
  the upgrade; rebuild the workspace image to also shed the now-useless
  setuid `ping` binary baked into older ones.

- **`GET /api/v1/groups` now returns a paged envelope (#2750).** The
  response is `{groups, page, page_size, total}` (same shape as
  `GET /api/v1/admin/groups`) instead of a bare list that was silently
  truncated at 200 rows. Integrators must read the `groups` key and
  paginate with `page`/`page_size`.

- **`KLANGKD_ALLOW_SUDO` now defaults to on (#2017).** Passwordless sudo
  inside workspace containers is granted by default; operators who want
  the previous locked-down posture must set `KLANGKD_ALLOW_SUDO=0`
  explicitly. The per-workspace lock-down (`allow_sudo: false` in the
  workspace settings bag, `klangk create`/`edit --no-sudo`, or the UI
  toggle) can still opt individual workspaces out. Applies to containers
  started after the change.

- **The `work/` subtree is removed from workspace homes (#2725).** The
  separate shared project directory `/home/work` no longer exists:
  project files live directly in the klangk user's home (`/home/klangk` —
  or `/home/<handle>` under the per-handle layout), which is the working
  directory for shells, exec sessions, and the image's `WORKDIR`. There
  is **no data migration**: existing workspaces keep their files in
  `~/work` — move them up manually (`mv ~/work/* ~`) if you want them at
  the top of the home. Importing an old export archive (with `home/work/`)
  preserves its layout as-is.

- **The default home layout is now shared (#2723).** `KLANGKD_PER_HANDLE_HOME`
  (and the `per_handle_home` setting) now defaults to `false`: new workspaces
  share one `/home/klangk` instead of per-user homes. Existing workspaces are
  unaffected (migration backfilled `true`). Set
  `KLANGKD_PER_HANDLE_HOME=true` — or pass the per-workspace create flag — to
  keep per-handle homes.

- **The chat feature is removed (#2716).** The per-workspace chat panel, the
  `@klangk` agent interaction, the `pi --mode rpc` chat-agent runtime, and the
  `chat` feature flag are gone. `KLANGKD_FEATURES_ENABLE=chat` on an existing
  config is ignored with a startup warning — no operator action required.
  Existing `chat_messages`/`chat_mentions` tables and stale `chat` ACL rows
  remain in the DB as inert leftovers (no destructive migration). The `chat`
  workspace permission is removed from the known-permissions list and the
  sharing UI; the agent user identity (DB row, handle, inactivity-sweep
  exemption) and its ownership of the `service` tmux session are unchanged.
  The agent home is still materialized at container create — now a plain
  `/home/klangk` directory (no `.users/{uid}` symlink indirection; the
  handle is fixed) populated from `/etc/skel`, without chat-agent Pi
  config. `klangk-setup-pi` stays as the generic per-user Pi setup.

- **The agent user is `klangk` (#2718).** The agent's identity is fixed
  (handle `klangk`, email `klangk@example.com`) and matches the container
  UNIX user / shared home. `klangk` is a reserved handle and the agent
  row's handle/email can no longer be changed. The
  `KLANGKWS_FEATURE_CHAT_AGENT_HANDLE`/`EMAIL` feature-config keys are
  removed (stale settings are ignored). Migration `m0008` rewrites the
  agent row and relocates a human user who already held the `klangk`
  handle to a unique alternative. Deployments that customized the agent's
  name lose that customization.

- **Interactive workspaces now require the network sidecar (#2325).** Every
  `egress_mode=interactive` workspace (the default) spawns a network sidecar
  and holds each new outbound host for a consent decision. On upgrade, an
  existing interactive workspace's next start requires a configured
  `network_sidecar_image` and a non-empty `KLANGKD_USERNS`; if either is
  missing it **fails closed** (refuses to start) instead of egressing
  unrestricted. An interactive workspace with `allow_sudo` also had `net_raw`
  dropped (defense-in-depth against the SO_MARK bypass) — since #2347 the
  drop applies to every workspace. Static workspaces with no allow/reject
  lists are otherwise unaffected.

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

### Security

- **WebSocket connections are closed on token revocation (#3152).**
  Logging out, or being evicted by the per-user session limit, now
  immediately closes the live WebSocket connections the revoked token
  authenticated (close code 4001, so clients log out instead of
  reconnect-looping). Previously an established socket kept full
  data-plane access until the next reconnect. Refresh rotation is
  unaffected — a refreshed session keeps its socket.

- **Bounded rate-limit state for the email cooldowns (#3113).** The
  per-address cooldown dicts behind
  `POST /api/v1/auth/forgot-password` and
  `POST /api/v1/auth/resend-verification` are now capped at 10,000
  hashed keys, shed oldest-first, and sweep expired entries only when
  recording. An unauthenticated flood of unique addresses can no longer
  grow process memory or per-request CPU without bound, and no raw
  email strings are retained.

- **Forgot-password no longer leaks account existence via SMTP
  failures or response timing (#3114).** `POST
/api/v1/auth/forgot-password` now answers `"sent"` immediately and
  delivers the email in a background task, logging failures
  server-side. Previously a broken SMTP backend answered 503 for
  existing enabled accounts but 200 for unknown/disabled ones, and the
  inline SMTP round-trip made the existing-enabled path measurably
  slower — both usable as account-existence oracles. Operators should
  watch the server log for reset-email delivery failures instead of
  relying on the HTTP response.

- **Forgot-password rate limiter no longer leaks account existence
  (#3100).** The 60-second per-address cooldown on
  `POST /api/v1/auth/forgot-password` now applies before the account
  lookup, so unknown, disabled, and enabled addresses all answer 429
  identically on a repeated request. Previously only existing enabled
  accounts could be rate-limited, making the cooldown an oracle for
  both account existence and the disabled state.

- **Per-frame gates on the own-terminal and ssh-agent WS commands
  (#3022).** With `join-workspace` as the connect gate (#2975), a
  join-only member could reach frames whose only protection was the
  old terminal-checked handshake: `ssh_agent_start` spawned a socat
  relay in the container, and the own-window frames
  (`terminal_new_window`/`select_window`/`close_window`/
  `rename_window`/`list_windows`) ran tmux against the caller's
  session — which, for a spectator viewing a shared terminal, is a
  grouped session whose windows belong to the whole group (they could
  inject or close the owner's windows). All six frames now refuse
  with a plain `Permission denied` error frame (deliberately not the
  `forbidden` code, which #2891 reserves for connect-level refusals —
  a stamped sub-action denial would dead-end the whole workspace
  page) unless the caller holds `code-in-isolation` (window frames)
  or either `code-in-isolation` or `exec-and-sync` (the agent relay,
  which both session kinds consume). Seeded roles are unaffected:
  every role whose clients send these frames already holds the
  permissions, and the web UI already hid them.

- **Workspace-mount volume-source validation (#3018).** A mount
  source with no `/` that doesn't start with `.` is a named volume,
  and must now be podman-safe to pass workspace create/update mount
  validation (alphanumeric first character, `a-zA-Z0-9_.-` only, at
  most 64 characters — the same rule as the volumes API, #2971),
  returning HTTP 400 on violation. Previously such a source reached
  `podman volume create/inspect` argv verbatim at container start, so
  a leading-dash source was parsed as a podman flag; the check also
  runs at start as defense in depth for workspaces created before the
  gate.

- **Volume name validation (#2971).** `POST /api/v1/volumes` and
  `DELETE /api/v1/volumes/{name}` now reject names that are not
  podman-safe — they must start with an alphanumeric character,
  continue with `a-zA-Z0-9_.-` only, and be at most 64 characters —
  with HTTP 422. Previously any string was appended verbatim to the
  podman command line, so a leading-dash name was parsed as a podman
  flag (on delete, `--all` could remove every unused volume on the
  host).

- **Volume-create conflict check no longer leaks foreign volume names
  (#2973).** `POST /api/v1/volumes` answered 409 for any podman volume
  name that existed on the host, letting a user probe for volumes the
  Klangk instance doesn't manage (other instances', operator-created).
  The 409 is now returned only for volumes labeled with this instance's
  id; other names fall through to the create, which fails podman-side —
  the client sees a bare internal error with no probed name, and
  podman's conflict text reaches only the server log.

- **`/llm-proxy` endpoints now require a workspace JWT (#2959).** The
  backend validates the workspace token itself, mirroring the egress
  proxy's `forward_auth` check. Previously the backend routes were
  unauthenticated, so the proxy (and its upstream API keys) was usable
  from outside a workspace container by any client that could reach the
  backend directly — including anonymously through the browser
  listener's `/llm-proxy/` pass-through. User login tokens are rejected;
  the proxy is usable only from inside workspace containers.
- **Image builds verify third-party inputs (#2063).** Base images (workspace base, python host, Alpine sidecar, Debian FIPS builders + nix-seed sandbox) are now pulled by immutable `@sha256:` digest, with the base-image workflow's auto-PR pinning the digest. The uv and process-compose release tarballs are SHA-256-verified per architecture before extraction (no more `curl | sh` / `curl | tar` pipes), the Pi agent npm tarball is fetched directly and SHA-512-verified, and the NodeSource / GitHub CLI / Caddy apt repo keys are hash-verified before entering a keyring (Caddy's sources list is written inline). Pins live in the Dockerfiles; rotation procedures and known residuals are documented in [Building Images](development/building-images.md).

- **Browser-delegate requests are bound to the caller's workspace
  (#1715).** `/api/v1/browser-delegate` and `/api/v1/browser-delegate/stream`
  now verify that the submitted `browser_id` was registered against the
  same workspace as the caller's workspace token, and return 403
  otherwise. Previously a container holding workspace A's token could
  relay actions (e.g. git-credential prompts, browser fetch) to another
  workspace's browser tab if it learned that tab's browser ID — the
  token provided no workspace boundary on the relay.

- **New `monitor` permission gates health/status reception (#2783,
  #1714).** Observing a workspace's health no longer requires
  `terminal`: `GET /workspaces/{id}/status` and the member-scoped
  `container_status` / `service_health` / `workspace_evicted`
  WebSocket frames now check the dedicated `monitor` permission.
  Every role and share that grants `terminal` also grants `monitor`
  (existing deployments are backfilled by migration 0016), and
  `monitor` can be granted alone for monitoring-only members who
  should observe health without exec/attach access.

- **New `egress-consent` permission gates egress decisions (#2883).**
  Registering a consent decider (the web Network tab, the consent
  banner, `klangk consent-decide`), deciding held requests, revoking
  verdicts, and pausing prompting now require `egress-consent` instead
  of `terminal` — a spectator (watch-only) can no longer decide a
  workspace's egress, and the Network tab and consent banner no longer
  render for members without the permission. Owners, coders, and
  collaborators hold it by default (existing deployments are backfilled
  by migration 0018); pause/unpause no longer additionally require
  `share-terminals`. Members or groups granted only `terminal` — whether
  via a custom ACL or the simple Sharing tab (its grants do not include
  `egress-consent`) — must be granted `egress-consent` explicitly to
  keep deciding; grant it in the Advanced ACL editor. See
  [ACLs](reference/acl.md).

- **Workspace status WebSocket broadcasts are now scoped to workspace
  members (#1714).** `container_status`, `service_health` (including
  the connect-time snapshot), and `workspace_evicted` frames were
  fanned out to every authenticated connection, letting any connected
  client enumerate every workspace's id, running state, health, and
  the bounded `health_message` tail of another tenant's service
  output; they are now delivered only to users holding `monitor` on
  the workspace. A view-only grantee still sees status in the
  workspace list (HTTP), but receives no live deltas; an admin
  watching other tenants' workspaces via `klangk monitor` now sees
  only their own workspaces — intended, and part of the fix.

- **`exec-and-sync` permission gates one-shot command execution and
  `klangk sync` (#2706, #2712).** The one-shot exec channel — `klangk
exec`, and the rsync transport `klangk sync` and `klangk sandbox`
  ride on — now requires the new `exec-and-sync` permission on the
  workspace, enforced server-side at `exec_start`. Both sync directions
  are covered by the same gate: a member without `exec-and-sync` cannot
  run one-shot commands or sync in either direction. Isolated terminals
  still use `code-in-isolation` and are unaffected. Coders and
  collaborators keep `exec-and-sync` (existing workspaces are backfilled
  by migration), so revoking it is an admin choice — remove the
  permission in the ACL editor to stop one-shot exec and bulk sync for a
  member while keeping their terminal access. Custom ACEs that granted
  `code-in-isolation` must add `exec-and-sync` explicitly to keep
  one-shot exec working for those principals. `klangk exec`/`klangk
sync` report a clear permission-denied error.

- **Terminal sharing verified permission-gated (#2709).** As part of
  the #2589 exfiltration-avenue audit, terminal sharing was verified to
  be permission-gated end-to-end (long-standing behavior, unchanged
  this release): share/unshare requires `share-terminals` (owners and
  collaborators by default) and joining another member's shared
  terminal requires `spectate-on-shared-terminals` (all roles); a
  joiner without `code-in-shared-terminals` or `share-terminals`
  joins read-only. The browser hides the Share context-menu action for
  members lacking the permission, the server rejects the commands, and
  private, unshared terminals are unaffected. Revoke the permissions
  per workspace in the ACL editor to stop a member sharing their own
  tabs or watching others'.

- **Workspace export is gated on the workspace, not admin (#2707).**
  `GET /api/v1/workspaces/{id}/export` (and `klangk export`) now
  requires the `export` permission on `/workspaces/{id}` instead of the
  `admin` permission on `/admin`: owners keep exporting their own
  workspaces (the owner wildcard ACE and the seeded `owners-<id>` role
  group both cover `export`), while admins no longer bulk-export
  workspaces they hold no grant on. A Deny `export` ACE on a workspace
  resource, positioned ahead of the wildcard allows, revokes export per
  workspace (rewritable by anyone holding `share` there). See
  [Export & Import](https://github.com/mcdonc/klangk/blob/main/docs/features/export-import.md).

- **Group creation restricted to administrators (#2770).** The default
  ACL no longer grants `create` on `/groups` to every authenticated
  user; it goes to the `admin` group instead, matching workspace
  creation (#2569). Existing deployments whose `/groups` still carries
  exactly the seeded ACE are migrated automatically; if your `/groups`
  entries differ (deliberately loosened or customized), remove the
  Allow `create` → Authenticated users entry via the ACL editor. To
  re-open group creation, add an Allow ACE for `create` on `/groups`
  targeting the `members` group.

- **Workspace creation restricted to administrators (#2569).** `POST
/workspaces`, `POST /workspaces/import`, and workspace duplication
  now require the `create` permission on the `/workspaces` collection
  resource, which defaults to the `admin` group only. Non-admin users
  see the create button hidden and receive 403 if they call the API
  directly. A new built-in `members` group is seeded at startup and
  every new user (registration, invitation, OIDC, admin-created) is
  added to it automatically. To let all members create workspaces, add
  an Allow ACE for `create` on `/workspaces` targeting the `members`
  group via the ACL editor.

- **FIPS host container image + containerized boot gate (#2628).** New
  `src/containers/host/Dockerfile.fips` layers the validated OpenSSL
  FIPS provider onto the docker host image (klangkd's own PBKDF2
  password hashing, JWT HMAC-SHA256, and outbound TLS then run inside
  the validated boundary) and embeds the FIPS workspace image in place
  of the stock one; build with `klangk:build-fips-host-image`, or pull
  the CI-built image from GHCR (`klangk-host-fips`). With
  `KLANGKD_FIPS_MODE` on, a containerized klangkd whose own OpenSSL is
  not FIPS-enforcing now refuses to boot instead of logging a warning
  (a control-host deployment still only warns). Docs:
  [FIPS 140-3 Mode](deployment/fips.md).

- **Login timing equalization (#2618).** Login and resend-verification
  now burn one full password verify even when the account is unknown or
  OIDC-only, so response timing no longer reveals whether an account
  exists.

- **OIDC state cookie hardening (#2573).** The OIDC callback no longer
  trusts the `redirect_uri` stored in the unsigned state cookie when
  exchanging the authorization code with the IdP; it now re-derives the
  value from hosting configuration the same way the login endpoint does.
  The cookie now carries only `state`, `verifier`, and `cli_redirect`.
  Defense-in-depth — not exploitable against a conforming IdP.
- **`KLANGKD_PASSWORD_HISTORY_COUNT` (#2582).** How many **previous**
  passwords to remember per user (default 0 = disabled, max 24). When
  set, password changes, resets, and admin password sets are rejected
  with 400 if the new password matches the current or any remembered
  one; the old hash is retired into history (stored hashed, pruned to
  the window) on every set. `/api/v1/config` advertises the count as
  `password_history_count`.

- **WebSocket error responses (#1718).** Terminal- and shared-terminal
  failure frames sent over the workspace WebSocket no longer include raw
  exception text (which could leak backend paths, image names, or
  tmux/podman internals to the caller). Clients now receive a fixed
  message (e.g. `Failed to create window`); the full exception detail is
  logged server-side instead.

- **OIDC `cli_redirect` userinfo bypass (#2571).** The localhost-only
  guard on the CLI login redirect used prefix matching, so a crafted
  `cli_redirect` like `http://localhost:1@attacker.example/` passed the
  check while actually routing to the attacker's host — a victim
  completing a normal IdP login had their session token redirected to
  it. The target is now parsed and must be plain `http` to `localhost`
  or `127.0.0.1` with a port and no userinfo; anything else falls back to
  the web flow.

- **`/files/content` now requires `files-download` (#2713).** The file
  viewer's text-reader endpoint
  (`GET /api/v1/workspaces/{id}/files/content`) is gated by the
  `files-download` permission like `/files/download` (#2705), closing
  the remaining scripted bulk-read avenue for members with `files`
  alone. Operators who relied on text viewing without download must
  also grant `files-download` (or withhold `files` entirely). The web
  file viewer degrades gracefully: listings and metadata stay visible
  and the content pane reports the denial. See
  [ACLs](reference/acl.md).

### Added

- **All five container images now publish on a release tag (#3140).**
  Pushing `vX.Y.Z` publishes `klangk-host`, `klangk-host-fips`,
  `klangk-workspace`, `klangk-workspace-fips`, and the newly pullable
  `klangk-network-sidecar` to GHCR under that tag, built from the tagged
  commit. Versioned tags only: the floating `:latest` stays owned by the
  continuous builds. See
  [Building Images](development/building-images.md).
- **`/health` now reports the instance id (#3057).** The health endpoint
  returns `{"status": "ok", "instance": "<id>"}` so a caller can confirm
  it reached the intended klangkd (the id is the same one in
  `<data_dir>/instance-id` and the podman `klangk.instance` labels). The
  E2E harnesses use it to detect fixture-server port collisions.
- **Backup and restore docs (#2999).** New "Backup and Restore" reference
  chapter covering the full-site backup set (data dir, labeled podman
  volumes with their labels, env + config file + `file:` secrets,
  customization tree, host bind-mount sources, locally-built images)
  and backup/restore procedures for both the packaged and Docker
  deployments, including the instance-id and volume-label constraints
  that make a restore succeed.
- **`is_admin` flag on `/my-permissions` (#2995).** The response now
  carries an explicit instance-admin flag derived from `admins`-group
  membership (the CLI's `status` and the web app's admin gating read
  it). The `/admin` ACL tree is retired with it: no rows are seeded,
  `/admin` no longer appears in the permission map or the ACL browser,
  and migration m0027 deletes any stored `/admin` rows (they answered
  no check). Because the group's name is now load-bearing, the
  `admins` group can no longer be renamed or deleted (HTTP 400), and
  the name cannot be claimed by another group. See
  [ACL reference](reference/acl.md).
- **`KLANGKD_VOLUME_QUOTA_PER_USER` (#2972).** Per-user cap on
  instance-managed named volumes, enforced at both creation paths:
  the volumes API (a create past the cap returns 429 naming the
  setting) and the workspace-start auto-create of mounted named
  volumes (a clear start error). A per-user lock makes the cap exact
  under concurrent creates. `0` (the default) = unlimited — the
  create path is unchanged when no quota is set. Reloadable on
  SIGHUP.

- **`workspace` filter on `GET /api/v1/events` (#3006).** The Admin →
  Events filter now accepts a workspace name as well as a workspace id:
  the new `workspace` query param matches an exact id or a workspace-name
  substring. The legacy `workspace_id` param keeps its exact-id behavior.

- **Live permission lists in the Sharing tab (#2986).** Each role
  bucket (owners, collaborators, coders, spectators) now lists the
  permissions its group actually holds on the workspace, read live
  from the ACL — post-seed edits made in the Advanced ACL editor are
  reflected on reload. A `*` grant shows as "All permissions". The
  buckets span about three quarters of the screen width.

- **`join-workspace` permission (#2975).** The `workspace_connect`
  gate — opening a workspace at all — now checks `join-workspace`
  instead of `terminal`. `terminal` keeps its name and becomes the
  Terminal-tab visibility signal: a member without it gets no Terminal
  tab, so a custom ACL can grant, e.g., files-only access
  (`join-workspace` + `files-view`). Migration m0024 copies every
  stored `terminal` ACE — Allow and Deny, on any resource the ACL
  ancestor walk consults — to a `join-workspace` sibling inserted
  directly after it, so first-match answers (including Deny-based
  exclusions and collection-level grants) survive the swap unchanged;
  nothing is renamed, and fresh seeds and both share flows (member,
  group) grant `join-workspace` alongside `terminal`.

- **README release badge (#2981).** The README now carries a release
  badge showing the latest `v*` tag, driven by GitHub Releases —
  no manual updates needed.
- **Versioned documentation (#687).** Each release tag now deploys its
  docs as a versioned subdirectory of the `gh-pages` branch (managed by
  mike, with the zensical version selector), instead of overwriting the
  whole site. The docs root redirects to the `latest` version. One-time
  operator action: after the first tagged deploy creates the branch,
  switch the GitHub Pages source to "Deploy from a branch: `gh-pages`".

- **Native YAML integers for `port`, `egress_port`,
  `bridge_timeout_seconds`, and `idle_timeout_seconds` (#2967).** A
  bare YAML integer (`port: 8997`) now parses the same as the quoted
  string form — previously it was rejected at construction unless
  quoted. The deprecated `proxy_port` alias accepts a bare integer
  too. Env vars, quoted strings, and `file:`/`cmd:` indirection are
  unaffected.

- **`KLANGKWS_FEATURE_OAUTH_PROVIDERS` (#432).** JSON list of OAuth
  device-flow providers (`host`, `client_id`, `device_code_url`,
  `token_url`, optional `scope`/`username`) that extends the git-credential
  device flow beyond GitHub to any RFC 8628 provider — self-hosted GitLab
  (17.1+, device flow enabled on the app) and other compliant hosts. A
  matching entry wins over the client-ID shorthands; a new
  `KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID` shorthand covers `gitlab.com`
  the way the existing GitHub one does. The browser dialog now names the
  provider host ("Sign in to gitlab.com"), only `https` verification pages
  are auto-opened, and malformed provider responses fall back to the PAT
  dialog instead of hanging it. See
  [GitHub Authentication](features/github-authentication.md#other-git-hosts-gitlab-self-hosted-provider-map).

- **Granular `/admin` tab permissions (#2940).** The admin endpoints
  split off the monolithic `admin` gate onto one permission per tab:
  `manage-users` (Users), `manage-invitations` (Invitations),
  `manage-groups` (Groups), `manage-server-schedule` (Server), and
  `manage-events` (Events; renamed from `container-events`). Admins are
  unaffected — the seeded `/admin` `*` wildcard covers every name — and
  a whole tab can now be delegated to a non-admin via an `Allow` ACE on
  its sub-resource. `manage-acls` (Access Control browser) is
  root-equivalent: it can rewrite ACLs on any resource including
  `/admin` and `/`, so it is granted only to administrators. See
  [ACLs](reference/acl.md).

- **Container events history API + admin Events tab (#2923).** New
  `GET /api/v1/events` endpoint pages through the
  `container_events` audit table (#2915) newest-first, with an optional
  `workspace_id` filter and a total count, and the admin section gains
  an Events tab rendering it (when, workspace, event, actor, cause,
  container, network namespace). Both are gated on the dedicated
  `manage-events` permission over `/admin/container-events` (renamed
  from `container-events` in #2940 before any release): admins
  hold it via the `/admin` wildcard, and granting it to another
  principal on that resource delegates read-only audit access without
  full admin.

- **Container lifecycle audit trail (#2915).** Every workspace
  container start/stop is now recorded in a new `container_events`
  table with the acting principal (user, agent, or system), the cause
  (api/create/ws_connect/auto_start/crash_restart | stop/restart/delete/
  crash_teardown/idle_timeout/eviction/logout/drain/shutdown), the
  podman container, and its role — workspace or network sidecar
  (sidecar create/teardown lands as system-caused `sidecar_start`/
  `sidecar_stop` rows; workspace rows carry the sidecar container id
  as `network_namespace` for egress-filtered workspaces). Labeled
  containers stopped by the shutdown/drain orphan sweeps, the boot
  reaps, and the sidecar dependent-container teardowns are attributed
  too (by their `klangk.workspace` label). Recording is best-effort
  and never fails the start/stop itself; rows accumulate under
  `data_dir`'s SQLite DB (bounded by the #2924 prune knobs).
- **`KLANGKD_CONTAINER_EVENTS_RETENTION_DAYS` / `KLANGKD_CONTAINER_EVENTS_ROW_CAP`
  (#2924).** Bound the `container_events` audit table (#2915): rows older
  than the retention window (default 90 days) are deleted, and when the
  table exceeds the deploy-wide row cap (default 10000) the oldest rows
  are trimmed keeping the newest. Swept once at startup, then hourly, by
  the consent sweeper's retention pass. Set either to `0` to disable that
  bound. Reloadable on SIGHUP (applies on the next sweep).

- **Super-E2E suite (#2561).** A new pytest suite
  (`src/klangk/klangkd-tests/super-e2e/`, run via `test-super-e2e`)
  exercises the real Docker host container (supervisord + klangkd +
  caddy + nested rootless podman) black-box over its published port:
  auth, workspace lifecycle, file ops, egress filtering + interactive
  consent, shared terminals, health/idle, SIGHUP reload, admin users,
  export/import. The `super-e2e.yml` workflow runs it on demand and on
  release branches; it is not a per-PR gate. See
  [Super-E2E](development/super-e2e.md).

- **`change-acls` permission (#2764).** Raw ACL editing is now gated
  on a dedicated resource-level permission instead of `share`:
  `GET`/`PUT /api/v1/workspaces/{id}/acl` (the Advanced ACL editor) and
  the role-group writes (`POST`/`DELETE`/`PATCH
/api/v1/workspaces/{id}/roles*`, which can mint an `owners-` member)
  require `change-acls`; `PUT /api/v1/acl/resource` additionally
  requires it when the target is an individual workspace. The simple
  sharing surface (member and group shares with the fixed permission
  set) stays on `share`. Owners are covered by their `*` wildcard, and
  migration 0017 backfills `change-acls` onto existing effective
  `share` holders, so workspace-side behavior is unchanged for them.
  Integrators calling `PUT /admin/acl/resource` against individual
  workspaces must now hold `change-acls` there (grant it on the
  workspace, or on `/workspaces` / `/` for deploy-wide coverage). See
  [ACL](reference/acl.md).
- **`fmtk-up` / `fmtk-down` / `fmtk-seed` — one-command fmtk harness (#2881).**
  `devenv shell -- fmtk-up` boots a scratch backend, an origin-splitting
  proxy, a seeded fixture, and a debug `flutter run -d chrome`, then
  prints the VM-service URI for `fmtk` (see AGENTS.md "Inspecting the
  running frontend"). Ctrl-C keeps the backend+proxy for a fast
  re-launch; `fmtk-down` stops them (`--wipe` resets the fixture), and
  `fmtk-seed` re-seeds standalone.

- **Native YAML booleans for string-typed boolean settings (#2796).**
  `allow_sudo`, `allow_autostart`, `disable_registration`,
  `disable_invites`, `disable_tmux`, `prevent_insecure_jwt_secret`,
  `allow_insecure_no_auth`, `reject_proxy_headers`, and `test_mode` now
  accept a bare `true`/`false` in the YAML config file (previously
  rejected at boot unless quoted). Env vars and quoted strings behave
  exactly as before.

- **`fmtk` — flutter-mcp-toolkit CLI in the devenv shell (#2868).** Agents
  can inspect and drive a debug run of the frontend (semantic snapshots,
  widget refs, taps/typing, hot reload, app logs) via the pinned
  `flutter-mcp-toolkit` release binary. The frontend registers the debug-only
  `mcp_toolkit` VM service extensions (release builds are unaffected). See
  AGENTS.md "Inspecting the running frontend" for the workflow.

- **Admission control: `KLANGKD_ADMISSION_MEMORY_ENABLED`,
  `KLANGKD_ADMISSION_MEMORY_MARGIN` (#2525).** Opt-in start-time
  host-capacity check: before a workspace container is created,
  klangkd compares available host memory (`MemAvailable`, plus the
  cgroup limit when klangkd itself is memory-capped; `vm_stat` on
  macOS, capped by the podman machine's configured memory — containers
  run in that VM, whose default 2048 MiB is far below the Mac's RAM)
  against the workspace's resolved memory limit plus a deploy-wide
  reserve (default `1g`). A start that does not fit fails
  fast with a distinguishable 503 / WebSocket error ("host at capacity:
  1.2 GB available, workspace wants 4 GB") instead of deferring the
  failure to the kernel OOM killer. Default off (with the default 8g
  limit, small hosts would be refused every start); skipped when no
  memory limit is configured. Reloadable on SIGHUP.
- **`KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER` (#2525).** Deploy-wide
  cap on concurrently running workspaces per user, checked at start
  time (the k8s ResourceQuota analogue). A user at the cap gets a
  clear "stop a workspace first" 503 / WebSocket error. `0` (the
  default) = unlimited. Reloadable on SIGHUP.
- **`KLANGKD_CLASSIFICATION_BANNER` (#2768).** Deploy-wide default
  classification marking (free text) for the always-visible marking banner
  the Application Security and Development STIG requires ("markings at the
  top and the bottom of screens"). Per-workspace override via the
  `classification_banner` field on `POST`/`PUT /api/v1/workspaces`, `klangk
create`/`edit --classification-banner`, and the create/edit UIs; the
  workspace-created hook can set it like any other attribute. Markings are
  validated (one line, printable, ≤120 chars — control and invisible format
  characters rejected); a malformed `KLANGKD_CLASSIFICATION_BANNER` aborts
  startup / is denied on SIGHUP reload. The web workspace page renders the
  banner pinned at the very top and bottom of the screen (color-coded by
  marking, scaled to stay fully legible — never ellipsized), and the TUI
  workspace detail shows a matching status line; marking edits propagate
  live to the owner, editors, and shared members. With no marking
  configured (the default) no banner is rendered and no screen space is
  reserved. Downloaded/exported files are not marked — the screen banner is
  the scope.
- **`KLANGKD_BROWSER_DELEGATE_ENABLED` (#2710).** Deploy-wide kill
  switch for the browser-delegate bridge (the workspace-token-gated
  `/api/v1/browser-delegate{,/stream}` endpoints that let a container
  drive the user's browser tab — a workspace-data read channel that
  bypasses file permissions). Defaults to `true`; set `false` to return
  403 from both endpoints, stop registering browser tabs for bridge
  routing, stop attaching a browser ID into new terminals (terminals
  already running keep a stale `klangk-browser-id`, but their bridge
  POSTs get the same 403), and advertise
  `browser_delegate_enabled: false` via `/api/v1/config` so the web UI
  stops answering bridge requests. Reloadable on SIGHUP. See
  [Browser Bridge](architecture/browser-bridge.md).

- **`files-write` permission (#2705).** The mutating files endpoints —
  upload (`POST …/files/upload`), rename (`POST …/files/rename`), and
  delete (`DELETE …/files`) — now require the new `files-write`
  permission in addition to `files`. New shares (members, groups,
  coder/collaborator roles) grant it; migration `m0012` grants it
  alongside every Allow `files-download` grant, so existing deployments
  keep current behavior. Without the permission the file viewer hides
  every mutating affordance (drag-and-drop, upload hints, Rename/Delete
  in the context menu) and editor renderers are read-only.

- **`files-download` permission (#2705).** The workspace file-download
  endpoint (`GET /api/v1/workspaces/{id}/files/download`) now requires
  the new `files-download` permission in addition to `files`, so
  download can be withheld from members who can otherwise browse/read
  files in the viewer. New shares (members, groups, coder/collaborator
  roles) grant both permissions; a schema migration mirrors existing
  `files` grants so current behavior is unchanged. Without the
  permission the file viewer hides its download affordances and binary
  renderers (image, PDF, video, spreadsheet) cannot fetch bytes — and
  since #2713 the text reader (`/files/content`) requires the
  permission too. The CLI/TUI expose no file-download affordances.

- **`KLANGKD_WORKSPACE_CREATED_HOOK` (#2762).** New customize-dir hook:
  a deployment-local Python file (point the env var at it, like
  `KLANGKD_OIDC_LOGIN_HOOK`) whose `on_workspace_created(workspace,`
  `actor)` runs after every workspace creation — create, import, and
  duplicate — and may mutate workspace attributes (validated, persisted)
  and rewrite the workspace ACL. Hook failures are logged and never
  fail the create; reloaded on SIGHUP. See
  [Customizing a Deployment](https://mcdonc.github.io/klangk/deployment/customizing/)
  for the API; a commented example ships in
  `customize/custom/hooks/workspace_created.py`.

- **De-noised group lists in the UI (#2752).** The admin Groups tab
  defaults to `source=manual`, with a "Workspace role groups" filter
  chip to include the seeded per-workspace groups. The ACL editor's
  add-entry picker offers manual groups plus the groups already
  referenced by the resource's ACEs — other workspaces' role groups are
  omitted — and the entries table is unchanged. Group (and picker user)
  fetches now walk every page of the paged envelope instead of silently
  truncating at 200 rows, and the picker dropdowns ellipsize long names
  (e.g. UUID-suffixed role groups) instead of overflowing.

- **Group `source` marker and filtering (#2750).** Groups now carry a
  `source` column: `manual` for human-managed groups,
  `workspace-role` for the four role groups seeded per workspace.
  `GET /api/v1/groups` and `GET /api/v1/admin/groups` accept a `source`
  query filter and include it in each row, so pickers can hide the
  machine-generated role-group names. Existing rows are backfilled by a
  schema migration. Role groups are now also rejected as share/ACL
  targets outside their own workspace (HTTP 400), and their names cannot
  be changed (HTTP 400) — the name is the teardown/scope-guard key.

- **Per-workspace sudo lock-down (#2017).** `allow_sudo` in the workspace
  settings bag (set with `klangk create`/`klangk edit`
  `--no-sudo`/`--sudo`, the web and TUI _Allow sudo_ toggles, or
  `PATCH /workspaces/{id}/settings`) locks a single workspace out of
  passwordless sudo even on a deploy with `KLANGKD_ALLOW_SUDO` on.
  `KLANGKD_ALLOW_SUDO` stays the ceiling: a workspace can never grant
  itself sudo on a deploy that forbids it. The rule applies at the next
  container start; the toggle is only shown when the deploy allows sudo
  (`sudo_available` on `/api/v1/images`).

- **Workspace export/import preserves the home layout (#2722).** `workspace.json`
  now carries `per_handle_home`, and import honors the archive's layout even
  when the server's `KLANGKD_PER_HANDLE_HOME` default differs. Archives
  exported before the feature import as per-handle homes.

- **Per-handle home is choosable on every create and edit surface (#2721).**
  The web create dialog and Settings tab, the TUI create/edit screens, and
  `klangk create`/`klangk edit` (`--per-handle-home`/`--shared-home`) all
  send the workspace's home layout. Create forms pre-reflect the server
  default (`KLANGKD_PER_HANDLE_HOME`, surfaced as `default_per_handle_home`
  on `/config`); a flip on an existing workspace applies from the next
  connect/start. See [Workspaces](features/workspaces.md).

- **Service session HOME is always the shared home (#2717).** The
  `service` tmux session now runs with `HOME=/home/klangk` pinned as a
  constant under both home layouts, and `/home/klangk` is created and
  populated from the image skeleton before the session's first login
  shell — including on the server-boot auto-start path, where no user
  ever connects first. This gives the service environment parity with
  member setup: exports written to `/home/klangk/.profile` reach the
  service (with the shared-mutable-state consequence that typed
  commands land in the shared `.bash_history`). The agent-private home
  provisioning is gone; `KLANGKWS_AGENT_HOME` remains baked as the
  constant `/home/klangk`, so sandbox `setup.sh` scripts using
  `export HOME="${KLANGKWS_AGENT_HOME}"` keep working (a no-op on
  shared-home workspaces).
- **`per_handle_home` now selects the home layout at runtime (#2720).**
  A workspace created with `per_handle_home=false` (see
  `KLANGKD_PER_HANDLE_HOME`, #2719) now actually serves the shared
  layout: every connection — and exec sessions, the health-check probe,
  and the `service` tmux session — uses the single shared `/home/klangk`
  (the container user's own home), with no `/home/{handle}` →
  `.users/{user_id}` symlinks and no per-user skeleton population.
  Changing your handle no longer re-links a home on this layout. The
  default (`true`, per-handle homes) is unchanged.
- **`KLANGKD_PER_HANDLE_HOME` (#2719).** Deploy-wide default for the home
  layout of **new** workspaces: `true` (default) = per-handle homes, the
  current behavior; `false` = a shared klangk home. Overridable per
  workspace via the new `per_handle_home` field on `POST /workspaces`
  (and editable later with `PUT /workspaces/{id}` — a flip applies from
  the next connect/start); exposed in `GET /workspaces` payloads;
  duplicates copy it, imports follow the deploy default.
  Reloadable on SIGHUP. See
  [Environment variables](reference/environment.md).
- **Admin page → Server tab (#2684).** Admins can now schedule a server
  stop or recycle from the Admin page: pick an action and either an
  absolute time (date/time pickers) or a delay (`2h`, `90m`, `45s`, or a
  bare number of minutes). Pending schedules list soonest-first with the
  same live countdown clients see, and each can be cancelled with a
  confirm step. The list follows the live `server_schedule` snapshot, so
  changes made by other admins appear immediately. The API remains
  available for scripting; see
  [Server Scheduling](features/server-scheduling.md).

- **TUI status bar on every screen (#2689).** The `server / user /
last login` status line — including live segments such as the
  scheduled stop/recycle countdown, host notices, and reachability
  flags — now renders on every TUI screen (workspace detail, create/edit
  forms, server switch, login), not only the workspaces list. The line
  stays current while you work inside a workspace screen and no longer
  disappears when navigating.
- **`terminal-open-cmd` / `KLANGKC_TERMINAL_OPEN_CMD` (#2685).** New CLI
  setting (klangk.yaml or envvar) that names the command used to open a
  new terminal window, e.g. `konsole -e`. When set, selecting a
  terminal in the TUI spawns `klangk shell` in a new terminal window
  instead of suspending the TUI and taking over the current terminal;
  the TUI stays running, and the window closes on its own when the shell
  disconnects (a holding flag like `--hold` keeps it open if wanted). If
  the command can't be launched, the TUI shows an error and falls back
  to the previous inline behavior. See
  [CLI reference](reference/cli.md).
- **Clearer shell exit (#2685).** `klangk shell` now says how to exit
  ("Exit this shell: press Enter, then ~.") and prints
  `Disconnected from <workspace>.` after a clean disconnect, so tmux's
  `[exited]` line reads as a normal exit instead of a crash. The
  consent-popup wrapper's cleanup no longer sprays
  `no server running on …sock` into the terminal after the shell ends.
- **Scheduled server stop/recycle (#2661).** Admins can schedule a
  server stop or recycle at an absolute time or after a delay
  (`POST /api/v1/server/schedule` with
  `{action: "stop" | "recycle", at | in_seconds}`; list/cancel via
  `GET`/`DELETE` on the same resource). Schedules persist in the DB
  across `klangkd` restarts and fire without anyone connected. A
  **stop** runs the graceful TERM/INT path and the process exits
  (code 0) — the service manager owns what happens next; a **recycle**
  runs the SIGHUP graceful runtime recycle in-process (listener and DB stay
  up) and never exits. In both, workspaces are drained gracefully and
  every connected client sees a live-countdown notification: a banner
  in the Flutter UI (`Server stops at 23:00 (in 1h 12m — workspaces
stop)`) and a `server: stop at 23:00 (in 1h 12m)` status line in the
  TUI. See [Server Scheduling](features/server-scheduling.md).
- **`EX_CONFIG` exit status 78 for deterministic config errors (#2666).**
  When `klangkd` refuses to boot over bad configuration — e.g. a
  `KLANGKD_DEFAULT_PASSWORD` that violates the password policy, password
  mode without a staged password, `auth_modes: none` on a non-loopback
  bind, or a containerized FIPS backend with non-FIPS OpenSSL — it now
  exits with status 78 instead of uvicorn's generic startup-failure
  status, so a first-boot misconfiguration no longer presents as an
  endless restart loop. Supervisors can stop retrying it (systemd:
  `RestartPreventExitStatus=78`). See
  [Process signals](deployment/signals.md) for the exit-status table.
- **Graceful stop on SIGTERM/SIGINT (#2527).** TERM/INT shutdown now
  broadcasts a `host_shutdown` WebSocket event (so clients render
- **Graceful stop on SIGTERM/SIGINT (#2527, #2664).** TERM/INT shutdown
  now broadcasts a `host_shutdown` WebSocket event (so clients render
  "server went away" instead of reconnect-looping), refuses new
  workspace starts, waits up to `KLANGKD_QUIESCE_TIMEOUT` seconds
  (default 15) for in-flight HTTP requests to finish, and drains every
  running workspace through the same graceful path as SIGHUP (terminal
  stop frames + `container_stopped` with reason `host shutdown`)
  before uvicorn's exit sequence runs. A drain failure is logged and
  never blocks the exit; a SIGHUP arriving during shutdown is ignored.
  Clients surface `host_shutdown` / `server_recycle` / `host_started` as
  transient, non-blocking notices (web UI snackbar, TUI status line +
  toast) — auto-reconnect is never visually impeded. Docs:
  [Signals](deployment/signals.md).
- **Graceful SIGHUP restart + `KLANGKD_QUIESCE_TIMEOUT` (#2527,
  #2664).** SIGHUP is now a full graceful restart: new workspace
  starts are refused, in-flight HTTP requests get
  `KLANGKD_QUIESCE_TIMEOUT` seconds (default 15) to finish, running
  workspaces are stopped gracefully (concurrently per workspace, each
  with a 5s podman stop grace); the reloaded config is applied, and
  the runtime recycles (drained workspaces are not restarted — only
  `auto_start` ones return).
  Clients get `server_recycle` events with a `phase` field and a final
  `host_started` broadcast; each phase is logged. Starts stay refused
  until the post-restart container reaps finish, and a failed restart
  logs, attempts a startup recovery, and exits (code 1) if recovery
  fails — the node never lingers half-restarted. Invalid config still
  denies the restart with nothing touched. Docs:
  [Signals](deployment/signals.md).
- **Decommissioning guide (#2593).** New [deployment chapter](deployment/decommissioning.md)
  documenting the decommissioning notification chain (users, admins, integrators,
  infrastructure owners) and the shutdown sequence: workspace export, graceful
  stop, data disposal, and secret revocation.

- **`KLANGKD_FIPS_MODE` (#2570, #2591).** Opt-in FIPS enforcement:
  every workspace container must prove an actively-enforcing OpenSSL
  FIPS provider when klangkd starts or adopts it (distro-agnostic
  probes — provider-aware digest rejection, or an SHA-2-only
  `fips=yes` approved set via the openssl CLI); a container that
  cannot prove it is removed and its start refused. The klangkd
  process's own OpenSSL is probed once at startup and logged for
  audit. A new `klangk:build-fips-image` devenv task builds the FIPS
  workspace image variant. See
  [FIPS 140-3 Mode](deployment/fips.md).

- **Host memory-pressure eviction (#2526).** When memory availability
  stays below `KLANGKD_MEMORY_EVICTION_THRESHOLD_PERCENT` (default 10%)
  for `KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS` polls (default 3 × 10s),
  klangkd gracefully stops the least-recently-active workspace with no
  connected clients — one per poll — until availability recovers to
  `KLANGKD_MEMORY_EVICTION_RECOVERY_PERCENT` (default 15%, hysteresis);
  the stop uses the idle-stop path (state preserved, next connect
  restarts) and emits a `workspace_evicted` WS event. Availability is
  measured platform-aware: `/proc/meminfo` on Linux (plus the cgroup
  limit inside memory-limited containers, e.g. Docker `-m`), and
  `vm_stat`/`sysctl` on macOS. Workspaces with live clients and
  workspaces pinned never-stop (`idle_timeout` 0, e.g. auto-started
  boot services) are never chosen while an idle one exists; on by
  default — disable with `KLANGKD_MEMORY_EVICTION_ENABLED=false`. All
  settings reload on SIGHUP.

- **Crash recovery for workspace containers (#2524).** Unexpectedly-dead
  workspace containers (OOM kill, non-zero exit, external removal) are now
  detected by a liveness sweep, and the death events carry the classified
  cause — an OOM kill names the workspace's effective memory limit (e.g.
  "OOM-killed at 8g memory limit") instead of surfacing as a generic
  death. Set `KLANGKD_CONTAINER_RESTART_ENABLED=true` to also auto-restart
  such workspaces after an exponential backoff (default 5s → 10s → 20s,
  capped at 60s; `KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS`), with at
  most `KLANGKD_CONTAINER_RESTART_MAX_RETRIES` (default `5`) attempts —
  exhaustion leaves a visible `crash-loop` state on
  `GET /workspaces/<id>/status` instead of an infinite restart loop.
  Expected stops (user stop, idle stop, delete, logout) never restart.
  Default off: recovery stays manual.

- **Resend-verification lockout (#2618).** Failed password checks on
  `POST /auth/resend-verification` now count toward the login lockout
  (`KLANGKD_LOGIN_LOCKOUT_*`), keyed like login on the account's email.
  A locked-out account gets `429` there too; a correct check on an
  unverified account clears the counter, matching login semantics.
  The 60s per-email resend cooldown is unchanged.

- **`KLANGKD_INACTIVITY_DISABLE_DAYS` (#2588).** Accounts whose newest
  activity signal — last authenticated API access (tracked per user,
  migration 0005), last login, or creation — is older than the window
  (default `35` days; `0` disables) are disabled by an hourly sweep.
  Login, token refresh, and authenticated requests then fail with
  `403 Account disabled` and live WebSocket connections are closed
  (4001 → client logout) until an admin re-enables the account via
  `PATCH /api/v1/users/{id}`. Admin-group members and the system
  agent are exempt; the setting is reloadable on SIGHUP. See
  [Authentication](features/authentication.md).

- **Last successful login time (#2583).** Every login (password,
  SSO, no-auth, and the auto-login after register/verify/reset/invite
  acceptance) now stamps a `last_login_at` timestamp on the user.
  `GET /auth/me` reports it, the TUI main-screen status bar shows it,
  and `klangk account show` prints it — so users can spot unexpected
  access to their account. Applied as schema migration 0002.

- **Concurrent-logon audit records (#2586).** Each session now records
  the workstation it was established from (effective client IP +
  user agent; applied as schema migration 0004). When a login is
  concurrent with an active session from a different workstation,
  klangkd writes an audit record to the server log — the signal to
  review for shared or stolen credentials. The new
  `GET /api/v1/users/{id}/sessions` endpoint lists a user's
  active sessions with their workstations. See
  [Authentication](features/authentication.md).

- **`KLANGKD_MAX_SESSIONS_PER_USER` (#2585).** New setting that caps
  how many concurrent login sessions a user may have (default `0` = no
  limit). When a new login pushes a user past the cap, the oldest session
  is revoked via the token blocklist (its next HTTP request gets 401;
  its next WebSocket connect is rejected with 4001). Token refresh
  keeps the same slot, and expired sessions never count. Reloadable on
  SIGHUP. See [Authentication](features/authentication.md).

- **Schema migrations (#30).** Schema changes are now applied as
  ordered, once-only migrations recorded in a new `schema_migrations`
  table at startup, instead of ad-hoc `CREATE TABLE IF NOT EXISTS`
  blocks. Migration 0001 adds a `password_history` table (rows cascade
  with their user) ahead of password-reuse prevention (#2582).

- **`KLANGKD_PASSWORD_REQUIRE_{UPPER,LOWER,DIGIT,SPECIAL}` (#2581).**
  Character-class complexity requirements for passwords: each setting is
  the number of characters of that class a password must contain
  (e.g. `2` = at least two uppercase letters), `0` (the default) = no
  requirement. Enforced on registration, password change/reset, invite
  acceptance, and admin set-password; advertised to clients via
  `password_requirements` in `/api/v1/config` for inline validation in
  the web UI and CLI.

- **FIPS 140-3 workspace image (#2570, #2577).** New
  `src/containers/workspace/Dockerfile.fips` variant builds on the
  workspace image with the CMVP-validated OpenSSL 3.1.2 FIPS provider
  (certificate #4985): system OpenSSL, python, and Node.js (including
  the pi coding agent) route through the validated module, non-approved
  algorithms fail closed, and the build verifies activation
  automatically. Docs: [FIPS 140-3 Mode](deployment/fips.md).

- **`KLANGKD_EGRESS_CONSENT_RETENTION_DAYS` / `KLANGKD_EGRESS_CONSENT_ROW_CAP`
  (#2303).** The `egress_consent` table is now bounded on long-lived deploys:
  a retention window (default 30 days; `0` disables) deletes terminal rows
  older than it, and a per-workspace row cap (default 2000; `0` disables)
  trims the oldest rows when a workspace floods decided requests past the
  cap. Verdicts still in effect (`forever`, `tilrestart`, or a timed window
  not yet elapsed) are enforcement state and are never pruned; they leave
  via workspace deletion or the `tilrestart` reap as before. Swept at
  startup and hourly (wall-clock deadline — event traffic never postpones
  it) by the consent monitor; both settings are reloadable on SIGHUP.
- **`KLANGKNETWORK_EGRESS_ACTIVITY_GATE` forwarding (#2514).** The
  sidecar's idle-activity report interval is now honored when set in
  klangkd's environment (forwarded to the sidecar like
  `KLANGKNETWORK_EGRESS_MIN_TTL`). Operators can lower the default 60s
  gate on deploys with short idle timeouts so egress-only workspaces'
  keep-alive bumps stay prompt relative to the check interval.
- **Static reject list on the Net Rules tab (#2503).** The tab now shows
  the workspace's `rejected_domains` (names the network sidecar blocks
  unconditionally, e.g. from a _deny forever_ verdict) in a read-only
  section below the static allow-list. Editing stays in the workspace
  settings panel.
- **Pause egress filtering from the web UI (#2494).** The workspace page's
  **Net Rules** tab now has a pause control (Unpause / Pause 15m / 1h / 1d)
  that silences consent prompts workspace-wide for a window, matching the
  `consent-decide` TUI control (#2332). Requires the same
  `share-terminals` permission as the TUI; the server nacks otherwise.

- **Egress request-flow diagram (#2376).** New "Anatomy of an egressing
  request" page under Architecture: a Mermaid flowchart of a single
  egressing request — DNS gate → NFQUEUE SYN gate → consent loop →
  verdict — plus the host-matching grammar, the allow-vs-deny asymmetry,
  and persistence boundaries. Linked from
  [Egress Filtering](features/egress-filtering.md), whose spec
  grammar bullets were also corrected to the nginx-style scopes
  (bare host = apex only, `.host` = apex + subdomains) of #2377.

- **Interactive egress consent documented (#2247).** The
  [egress filtering](https://klangk.dev/features/egress-filtering) page
  now covers the interactive consent mode end-to-end: egress modes and
  defaults, deciders and held connections, decision durations, pause and
  revoke, the audit trail, operator settings, and the security model.
- **`klangk shell` consent-decider popup (#2383).** Shelling into an
  `interactive`-egress workspace now wraps the shell in a local tmux that
  floats the egress-consent decider over it as a `tmux display-popup`, so
  held egress requests can be acted on without leaving the shell. The shell
  itself is unchanged (the normal container tmux, full window machinery
  incl. the status-bar `+`); the outer local tmux is nearly invisible
  (`C-a` prefix, no status bar, mouse passes through to the container tmux).
  `C-a p` reopens the popup, `q` hides it (decider stays registered), `Q`
  quits for real. Falls back to the plain shell when host tmux < 3.2, stdin
  is not a tty, or `--no-consent-popup` is passed.

- **`klangkd doctor` tmux version check (#2383).** Doctor now reports the
  host tmux version and warns when it is below 3.2 — the minimum for the
  upcoming TUI consent-decider popup over the shell (`tmux display-popup`
  landed in 3.2). Below 3.2 the shell layer will fall back to a plain
  attach, so the check is a warning, not an error.
- **`klangk consent-decide` persistent popup role (#2383).** The decider
  accepts internal `--popup-socket` / `--popup-session` options (set by the
  shell-layer wrapper) for its upcoming persistent role inside a hidden tmux
  session: in that role `q` hides the popup viewer (detaching it, leaving the
  decider registered) and `Q` confirms a real quit. Standalone `q` still
  quits immediately.

- **Egress traffic extends the workspace idle timeout (#2485).** The
  network sidecar now samples real workspace egress — any TCP/UDP traffic,
  including long-lived connections and non-DNS UDP that the existing DNS/SYN
  hooks (#2479) miss — and resets the container's idle timer while bytes are
  flowing, so an egress-only workload is no longer reaped mid-transfer.
  Best-effort and scoped to exclude the sidecar's own control traffic. No new
  config: it reuses `KLANGKNETWORK_EGRESS_ACTIVITY_GATE` (default 60s) as the
  sample cadence.

- **`KLANGKNETWORK_EGRESS_UPSTREAM` — operator-pinnable sidecar DNS upstream (#2424).**
  When set in `klangkd`'s environment, the network sidecar's FQDN proxy forwards
  workspace DNS to this resolver verbatim instead of auto-detecting a host
  resolver. An operator may want every filtered workspace to use a specific
  resolver (e.g. a corporate DNS); the interactive-egress smoketest also uses
  it to point the sidecar at a controlled-DNS test fixture so chosen hostnames
  resolve to single stable test IPs. Absent, behavior is unchanged (auto-detect).
  Mirrors the existing `KLANGKNETWORK_EGRESS_MIN_TTL` / `SWEEP_INTERVAL` forwarding.

- **`KLANGKD_CONTAINER_TMP_SIZE` + `settings.tmp_size` (#2378).** The
  per-workspace `/tmp` tmpfs size is now configurable (e.g. `2g`, `512m`);
  the deploy default stays `2g` (the prior hardcoded value), so existing
  installs are unchanged. Set the env var empty to mount `/tmp` with no
  `size=` option (podman then sizes it at half of RAM). Exposed in the
  Flutter create/settings dialogs and the TUI create/edit form.
- **`egress_mode: "allow"` — default-permit egress (#2406).** A third
  egress mode alongside `static` (default-deny) and `interactive`
  (consent-gated). An `allow` workspace permits every host except names in
  `rejected_domains` (NXDOMAIN'd at the sidecar DNS layer); off-list egress is
  recorded through the consent pipeline for observability and auto-allowed
  with no consent prompt, behaving as if an internal always-allow decider were
  registered. External consent deciders are refused (as with `static`). The
  network sidecar runs when one is configured (for logging + reject-list
  enforcement) but degrades to plain unrestricted egress when filtering isn't
  set up, so it never fail-closes. `klangk sandbox` now creates `allow`-mode
  workspaces instead of the prior static-no-list-unrestricted degenerate case.

- **Egress-mode picker in the TUI + Flutter create/edit dialogs (#2409).** The
  workspace create and edit forms now expose an egress-mode selector
  (allow / static / interactive, default `interactive`), so the mode is
  settable from the clients rather than API-only; a change on a running
  workspace applies on the next start/restart (both clients prompt). The
  `allow` mode itself landed in #2406.

- **`rejected_domains` in the TUI + Flutter workspace dialogs (#2386).** The
  static deny-list is now editable end to end, mirroring `allowed_domains`:
  the TUI create/edit forms (a second list editor in the Netfilter pane, with
  focus-aware Delete/'e' for either list), the Flutter create + settings
  dialogs, and the `klangk create/edit --reject` CLI flags. The shared
  validator rejects CIDR specs for `rejected_domains` up front (NXDOMAIN is
  name-level), matching the API. The list page also badges a workspace whose
  `rejected_domains` is set but netfilter is disabled.

- **`rejected_domains` workspace setting + sidecar enforcement (#2367).**
  The deny counterpart to `allowed_domains`: a persisted, host-only list whose
  names the network sidecar NXDOMAINs unconditionally (no resolution, no SYN,
  no consent prompt), in both static and interactive egress modes, taking
  precedence over the allow-list and consent. The grammar mirrors
  `allowed_domains` (bare = exact apex, `.host` = apex + subdomains, `*.host` =
  subdomains only); CIDR specs are rejected at the API (NXDOMAIN is name-level).
  Configurable via the create/update/clone/import workspace API. In static
  mode a reject-only workspace (no `allowed_domains`) is deny-all
  (fail-closed); the reject list is a useful blocklist alongside an allow-list
  or in interactive mode. Static mode itself is being phased out in favor of
  interactive-everywhere. The
  TUI/Flutter dialogs are a follow-up (#2386); the `forever` **deny** verdict
  that mutates this list at runtime is #2369.
- **Consent rules-management tab in the Flutter workspace (#2387).**
  Interactive-egress workspaces now show a **Rules** tab in the workspace
  IDE tab strip (alongside Files/Terminal/Settings), the Flutter counterpart
  of the TUI `consent-decide` rules screen. It lists the static allow-list,
  active consent allows (with `expires in 5m` / `until restart` / `forever`
  labels), and active denies (with remaining window), all live off the
  existing `egress_rules` stream. Each active verdict has a **Revoke** action
  (confirm → the row leaves once the server acks; a failed ack surfaces an
  error and leaves the rule enforced). It mirrors the TUI exactly; static
  allow-list entries are not revocable from this tab.

- **`forever` egress-consent deny persists across restarts (#2369).** The deny
  counterpart of the forever-allow: a `deny` with `duration=forever` appends
  the host to the workspace's `rejected_domains`, which the sidecar re-reads on
  (re)start and NXDOMAINs unconditionally. The deciding connection still gets
  its immediate in-memory REJECT; the list mutation makes the deny durable.
  Unlike the allow side, a port-less deny (e.g. ICMP) is persisted as a bare
  host -- reject enforcement is name-level, so blocking the whole host is the
  safe unit of a deny. Best-effort (failures swallowed). Revoking must clear
  both the list entry and the audit row (#2370).

- **Pause egress-consent filtering (#2332).** A workspace-level control in
  the `consent-decide` TUI (`Pause: 15m | 1h | 1d | Cancel`) silences ALL
  consent prompts for the workspace for the chosen window: a destination with
  no allow-list rule and no in-effect recorded verdict is auto-allowed (no
  hold) instead of prompting. The pause does not bypass policy --
  `allowed_domains`/`rejected_domains` rules and existing `egress_consent`
  verdicts (a recorded deny still blocks) keep applying. The window
  self-expires (the gate re-checks on every connection), the status line
  shows the remaining time, and a refreshed `egress_rules` frame carries the
  live `paused` window to every decider.

- **`reject_list` in the `egress_rules` frame (#2370, #2340).** The read-only
  rules view (`ConsentCoordinator.rules_frame`) now surfaces the workspace's
  `rejected_domains` alongside the existing `allow_list`, so deciders see the
  static deny-list in the rule-management screen.

- **`forever` egress-consent allow persists across connections and restarts
  (#2368, #2372).** An allow with `duration=forever` allow-lists the host for
  the rest of the session AND across container restarts: the sidecar treats the
  host as live allow-listed (so a later connection that resolves to a
  CDN-rotated IP passes without re-prompting, #2372), and klangkd appends the
  consented `host:port` to `allowed_domains`, which the sidecar re-reads on
  (re)start (#2368). The deciding connection still gets its in-memory ACCEPT
  immediately; the persisted entry is port-scoped (the port the decider was
  shown) and de-duplicated. The `forever` deny counterpart is a follow-up
  (#2369).

- **Static egress mode refuses consent deciders (#2394).** A workspace-scoped
  consent decider connecting to a workspace with `egress_mode = "static"` is
  now refused at registration with a `4003 Forbidden` close
  (`workspace egress mode is static`), so the static/interactive boundary is
  structural rather than only enforced at hold time. Deploy-wide deciders are
  unaffected. The coordinator's existing `_is_interactive` gate remains as
  defense-in-depth.

- **Revoke action in `consent-decide` (#2341).** On the rules screen
  (`r`), focus an active consent allow/deny row and press `x` to revoke it:
  klangkd drops the sidecar rule and marks the verdict spent, so the row
  leaves the list immediately. A failed revoke (sidecar unreachable / no ack)
  flashes `revoke failed — still in effect` and leaves the row enforced — a
  still-active rule is never silently hidden. Static allow-list rows are not
  revocable from this screen (edit them in workspace settings).

- **Read-only rules screen in `consent-decide` (#2340).** Press `r` in the
  `consent-decide` TUI to switch from the held-request queue to a second,
  read-only screen listing every egress decision currently in effect for the
  workspace: the static allow-list, active consent allows (with expiry such
  as `expires in 5m` / `until restart` / `forever`), active denies (with
  remaining deny window), and the pause window when filtering is paused
  (#2332; hidden until that lands). `q`/`Esc` returns to the queue. The
  WebSocket worker stays connected across the switch, so holds keep arriving
  and the list updates live from the `egress_rules` frame (#2338). Revoking a
  row is a separate follow-up (#2339/#2341).

- **Interactive egress-consent banner in the web UI (#2246).** Workspaces
  in `interactive` egress mode now show a banner on the workspace page listing
  pending held egress requests (host:port, process, countdown) with per-row
  Allow/Deny verdict buttons, alongside the `consent-decide`
  TUI; verdicts go live over the `/ws/consent-decider` stream. Server error
  frames, verdict send failures, and verdicts attempted while disconnected
  surface as a transient flash so a rejected/lost verdict is never silent.

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

- **Revoking a consent verdict (#2339).** A decider can revoke an active
  allow/deny so its effect is immediate (not waiting for the duration/restart):
  klangkd pushes a `drop_rule` to the workspace's network sidecar, which drops
  the learned ACCEPT/REJECT rule for the host and acks back, and only then is
  the `egress_consent` row flipped to `revoked` (fail-closed -- a connected
  but unresponsive sidecar leaves the row enforced rather than falsely marking
  it revoked). A `revoke` decision + `revoked_at`/`revoked_by` audit columns are
  added (the `decision` `CHECK` is now generated from `DECISIONS`, like
  `duration`).

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
  consent verdict instead of the DNS query: a denied name now resolves (the
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
  values. See [Configuration](reference/klangkd-config.md).

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
  `klangkd.yaml`. See [LLM Proxy](architecture/llm-proxy.md).

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

- **`klangk sandbox` `copy:` destinations are literal container paths
  (#3118).** Only a leading `~` is special (it expands to
  `/home/{handle}`); no other shell expansion happens — a destination
  like `~other/file` or `$HOME/file` is written as a literal filename,
  and a relative destination resolves against the container user's
  home, not `mount-at`. Previously such destinations were expanded by
  accident (unquoted interpolation let the container's `sh` interpret
  them); spell out the destination path you want. See
  [Sandbox](https://mcdonc.github.io/klangk/features/sandbox/).

- **`KLANGKD_EGRESS_CONSENT_RATE_LIMIT=0`** now means unlimited (#3083).
  Previously `0` denied every interactive consent hold (the pending cap
  compared `>= 0`); it now disables the per-workspace pending cap, matching
  the retention knobs where `0` turns a bound off.

- **Server admin schedule list (#3002).** The explanatory paragraph
  ("A stop exits the server process…") no longer renders above the
  schedule list on the Server admin tab; the list now fills the tab
  directly.

- **Password-field visibility toggle (#2893).** The "eye" toggle on
  password fields (login, settings, invitations, password reset, admin
  user forms) is no longer a Tab stop — Tab moves only between input
  fields. The toggle is now mouse/touch-only: it has no keyboard
  activation path (screen-reader semantics navigation still reaches it).

- **The devenv Flutter toolchain is now 3.47.0 (#2869).** Flutter (and its
  Dart SDK) come from a pinned `nixpkgs-unstable` input while the rest of
  the toolchain stays on `nixos-26.05`. Contributors get Flutter 3.47 /
  Dart 3.13, clearing the Flutter >= 3.44 requirement for the
  flutter-mcp-toolkit integration (#2868).

- **Host image base is `python:3.14-slim` (#2844).** The host and FIPS
  host container images now run CPython 3.14 (digest-pinned, OpenSSL
  3.5). Workspaces are unaffected; operators should rebuild the host
  and FIPS images to pick up the new base.

- **`nix_enabled` — the per-workspace `/nix` feature is now off by default
  (#2560).** A new master switch (`KLANGKD_NIX_ENABLED` / `nix_enabled` in
  `klangkd.yaml`, reloadable on SIGHUP) gates the whole feature. While off,
  the nix toggle is absent from every create/edit surface (web, TUI, CLI),
  the API rejects a new `nix: true` opt-in with a clear error (an edit
  echoing an already-stored value is tolerated), and a workspace start with
  a stored nix flag proceeds without the `/nix` mount (logged once at
  info). Set it with a `nix_seed` block to arm the feature — see
  [Nix workspaces](features/nix.md).

- **Branch coverage in the Python 100% gates (#2834).** Both the
  `klangk` and `klangksidecar` unit suites now measure branch coverage
  (`--cov-branch`) and require every branch outcome exercised, not just
  every line — structurally unreachable arms carry documented
  `# pragma: no branch` comments. The sidecar suite gains a coverage gate
  at all (previously none); contributors adding an `if` with only one
  side tested will now fail the build until the other outcome is tested.

- **`host_restart` WS event renamed to `server_recycle` (#2661).** The
  graceful recycle path (SIGHUP and scheduled `recycle`) now broadcasts
  `server_recycle {phase: draining | recycling}` instead of
  `host_restart`, its `container_stopped` drain reason is `server
recycle`, and the Flutter notice reads "Server recycling…" — matching
  the stop/recycle terminology. Unreleased alongside #2661, so no
  migration concern. The Flutter workspace page no longer raises the
  blocking "Container stopped — click Restart" overlay for a server
  recycle: the server stays up, the WebSocket reconnects (1012), and
  auto-start brings workspaces back, so the reconnect overlay owns the
  gap.

- **Password hashing (#2576).** Passwords are now hashed with
  PBKDF2-HMAC-SHA512 via `hashlib` (600,000 iterations, stored as
  `pbkdf2_sha512$…`) instead of bcrypt, and the `bcrypt` dependency is
  gone. Hashing now routes through the container's OpenSSL, so it is
  FIPS-approvable when the FIPS provider is active (#2570). No migration
  is needed: there are no deployments with stored bcrypt hashes.
- **Numeric config accepts bare numbers (#2603).** All numeric
  `KLANGKD_*` settings (`access_token_hours`, `min_password_length`,
  the `login_lockout_*` trio, `invite_expire_hours`, `port_range_start`,
  `websocket_msg_size_max`, `smtp_port`, `file_upload_size_max`, the
  `health_check_*` floats, `hosted_ports_per_workspace`) now accept a
  bare YAML int/float (`access_token_hours: 48`) as well as quoted
  strings and env vars, and keep `file:`/`cmd:` indirection working.
  Malformed values (bools, floats-for-ints, negatives, out-of-range
  ports) fail at startup naming the field instead of at request time;
  `smtp_use_tls: true` (native bool) is also accepted. Zero remains
  legal only where it already meant "off" (length floor, lockout,
  hosted ports). Explicitly emptying a value (`KLANGKD_X=""`) now
  resolves to the field's default instead of crashing consumers with
  `int(None)`; `port_range_start` is range-checked against the last
  legal host port.

- **Egress rules tab renamed to Network (#2510).** The workspace tab
  previously titled **Net Rules** (originally "Rules", #2457) is now
  titled **Network**. Presentation only; the tab's contents are
  unchanged.
- **`consent-decide` TUI: duration attached to the verdict action (#2511).**
  The global duration-selector row is gone. `a`/`d` (and the row buttons)
  now always send the default duration (`tilrestart`); `A`/`D` open a
  per-request duration picker (Enter sends, Esc cancels). A duration can
  no longer be pre-armed and silently applied to the next Allow/Deny —
  matching the web banner's split Allow/Deny controls.
- **Restyled pause option buttons (#2502).** The Network tab's pause
  controls (Unpause / Pause 15m / 1h / 1d) now share one pill-shaped
  option-button look with equal sizing, pause/play icons, and tooltips;
  the active choice keeps the amber fill. Purely presentational — keys and behavior unchanged.
- **Consent banner per-row duration menus (#2499).** Each row of the
  egress-consent banner has a split Allow/Deny button: a bare click sends
  the verdict with the default duration (until restart), and the attached
  ▾ menu sends it with any other duration (just once, 5 minutes, …,
  forever) in one step. The duration is chosen with the click, never armed
  beforehand, and no longer takes up a button row of its own.
- **`KLANGKD_IDLE_TIMEOUT_SECONDS` default 30m → 60m (#2480).** The workspace container idle timeout now defaults to 3600s (was 1800s). Existing
  deployments get a longer idle window on upgrade unless the env var is already
  set; set `KLANGKD_IDLE_TIMEOUT_SECONDS=1800` to keep the old 30-minute default.
  The auto-computed idle-check interval (`timeout/3`, clamped 10–60s) follows.

- **`klangk sandbox` drops to interactive egress after install, where safe (#2404).**
  A sandbox workspace is still created in `allow` mode so `setup.sh` installs
  proceed; once setup returns, the driver resets `egress_mode` to `interactive`
  and stops the container when the server has a network sidecar and the
  workspace is not auto-start — the next `klangk shell` start then applies
  consent-gated egress. It stays in `allow` (and its service command still
  fires post-setup) for auto-start workspaces (which boot unattended, with no
  decider to consent) and on servers with no sidecar (where interactive is
  fail-closed). `--force` re-setup flips back to `allow` and restarts first
  (deferring the service command until the re-setup completes), so re-running
  setup still egresses freely.
- **Workspace Pi agent hides thinking blocks by default (#2459).** The
  per-user `~/.pi/agent/settings.json` provisioned at first login now sets
  `hideThinkingBlock: true` instead of the previous `defaultThinkingLevel:
"off"`, which had no effect behind the LLM proxy (the model kept thinking
  regardless). Thinking still runs and costs tokens; only its block is hidden
  in the web-terminal TUI, where the Ctrl+T toggle is swallowed by the
  browser. Existing `settings.json` files are left untouched.

- **Network egress UI copy (#2457).** The workspace consent-rules tab is
  now labeled **"Net Rules"** (was "Rules") to disambiguate it, and the
  rejected-domains help text plus its validation error replaced the DNS
  jargon "NXDOMAIN" with plain language (e.g. "Hosts blocked unconditionally
  (never resolved, no consent asked)."). Display-only; no behavior change.

- **Workspace base image is now `debian:trixie-slim`, Node installed via apt (#2432).**
  The workspace container derives from `debian:trixie-slim` (pinned by digest)
  instead of `node:26-slim`, and Node.js 26 + npm are installed explicitly
  from the NodeSource apt repo (`nodejs=26.7.0-1nodesource1`) rather than
  coming from the base image. Node major is unchanged (still 26), so workspace
  behavior is unaffected.

- **Revoking a `forever` verdict retracts its durable list entry (#2370,
  #2339).** Revoking a `forever` allow/deny now removes the host from
  `allowed_domains`/`rejected_domains` (not just the in-memory sidecar rule),
  and the sidecar clears its in-session `_SESSION_HOST_ALLOWS`/`_VERDICT_CACHE` for
  the host -- so the verdict stops taking effect immediately and no longer
  re-applies on the next sidecar restart. The retract is best-effort (failures
  logged + swallowed); the verdict row is marked revoked regardless. A
  statically-configured list entry (set via the API, not a verdict) is only
  affected if it matches the revoked verdict's host, and clears on the next
  restart as with any list edit.

- **Interactive egress consent no longer gated on `allowed_domains` (#2325).**
  A workspace in `egress_mode=interactive` (the default) now always runs the
  FQDN network sidecar and holds every not-yet-approved egress for a consent
  decision, even with an empty `allowed_domains`; `allowed_domains` now means
  "pre-approved, skip consent" rather than the prerequisite for consent to
  exist. Static mode with no lists stays unrestricted. `klangk sandbox` now
  creates its workspace in `static` mode so its automated installs keep
  unrestricted egress.

- **Egress-consent duration token `restart` renamed to `tilrestart` (#2357).**
  The verdict `duration` value `restart` is now `tilrestart` ("until restart")
  everywhere — the model constant, the wire/DB token, the `consent-decide` TUI
  selector, the web consent banner, and the network sidecar. The semantics are
  unchanged (the verdict holds for the workspace container's lifetime and is
  cleared on restart); only the token name changes. `restart` reads ambiguously
  (valid _until_ restart vs. starting _at_ restart), `tilrestart` states the
  window directly.

- **`allowed_domains` / forever-allow host matching is now exact-by-default
  (nginx-style) (#2377).** A bare host (`example.com`) now matches the apex
  **only**; prefix with a dot (`.example.com`) for apex + subdomains (the old
  bare-host behavior); `*.example.com` remains subdomains only. This aligns
  with the firewall mainstream (Cilium `matchName`, Pi-hole / hosts-file, nginx
  `server_name`) and makes a `forever` consent allow least-privilege by default
  — it covers exactly the host the user approved, not its subdomains. No
  migration required (single deployment).

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
  `KLANGK_*` (CLI). See [Environment variables](reference/environment.md).

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

- **`/groups` write endpoints (#2940, closing #2941).** Group
  management consolidates onto `/api/v1/groups` behind
  `manage-groups`: `POST/PATCH/DELETE /api/v1/groups` and the
  `/groups/{id}/members` write/read endpoints are gone (their
  semantics — creator ACL grant on create, ACE cleanup on delete —
  were ported to the admin surface). `GET /api/v1/groups` remains the
  authenticated listing. The seeded `/groups` Allow `create` ACE is no
  longer emitted; rows already in upgraded deployments are inert.
  Scripts calling the removed endpoints must switch to
  `/api/v1/groups`.

- **`KLANGKD_TRUST_OUTER_PROXY` (#2596).** Dead setting removed: it was
  never read by any code (a leftover from the old nginx renderer). No
  operator action needed — the env var was already ignored. For trusted
  outer-proxy setups use `KLANGKD_TRUSTED_PROXY_CIDRS`, which is the
  setting the proxy renderer actually consumes; the host docker-compose
  sample now points there.

- **`scope` removed from egress-consent verdicts (#2356).** The `scope`
  field is gone from the decider WebSocket protocol, the `consent-decide` TUI,
  and the `egress_consent` DB column + CHECK constraint. `duration` is now the
  sole axis for how long a verdict holds (default unchanged: `restart`; `once`
  is a one-connection duration). Operators with an existing DB keep a dead
  `scope` column (harmless, unreferenced); no migration action.

- **`POST /internal/egress-consent/events` (#2318).** The sidecar's
  fire-and-forget consent-event endpoint is gone, superseded by the
  synchronous `/ws/egress-sidecar` hold path (#2311): recording now happens
  when the sidecar holds a destination pending a verdict (the coordinator
  creates the row on `hold()`), so the POST handler + its Caddy handle were
  dead. `KLANGKNETWORK_EGRESS_CONSENT_URL` still drives the sidecar (it now
  only uses the `host:port`; the path is ignored) to derive the WS URL.

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

### Fixed

- **Parallel image-build tasks tolerate transient rootless-podman failures
  (#3168).** `klangk:build-workspace-image` and `klangk:build-network-sidecar`
  run in parallel and are a fresh machine's first rootless podman invocations;
  the concurrent first-time user-namespace initialization intermittently
  aborted one of them with `failed to reexec: Permission denied` before any
  test ran, reding the whole CI job. Their podman calls now go through a
  retry helper that retries exactly that failure signature once — printing
  `podman info` / `podman unshare` diagnostics first so a persistent
  occurrence stays attributable — and passes every other failure through
  untouched.

- **`build-host-image` / `build_wheel.sh` (#3143).** The release wheel is
  now built with `uv build`, which resolves hatchling/hatch-vcs into its own
  isolated build environment instead of transiently installing `build` into
  the shared devenv venv. Previously a concurrent `uv-sync` from another
  devenv shell entry could wipe `pyproject-hooks` mid-build, failing the
  wheel build with `can't open ... _in_process.py: [Errno 2]`.

- **Workspace settings panel name validation (#3130).** Clearing the
  _Name_ field and tapping _Save_ used to fail the whole panel update
  with an opaque `Failed: Error: 422`, blocking every other field from
  saving until a name was re-entered. The blank name is now rejected
  inline on the field (same message as the server's rule, #3110), and
  validation-style API errors render their actual message instead of a
  bare status code (save banner and ownership-transfer snackbar).

- **Sandbox `copy` specs with extra colon segments are rejected (#3119).**
  A `.klangk-sandbox.yaml` `copy:` entry like `notes.txt:~/notes.txt:ro`
  used to silently drop the trailing `:ro` and copy anyway; it now fails
  config load with an `Invalid sandbox config` error. Copy specs are
  strictly `source:dest` — no mount-style `:options` segment, both
  halves non-empty, string entries only.

- **`klangk sandbox` copy destinations are now resolved POSIX-style
  (#3117).** The `mkdir -p` parent for a `copy:` destination was derived
  with the CLI host's path flavor, so on a Windows host a container path
  like `/home/admin/sub dir` came out backslash-mangled and the copy
  landed in the wrong directory inside the container. The parent is now
  computed with POSIX semantics regardless of host platform.

- **`klangk monitor` hook spawn failures are now fatal (#3092).** A
  `--` command that could not be spawned (missing binary, bad path
  component, not executable) used to be misclassified as a network
  error, making the monitor reconnect with backoff forever. It now
  prints the spawn error and exits nonzero; socket-level errors still
  reconnect as before.

- **Corrupt `klangk-state.yaml` no longer bricks the CLI (#3111).** A
  state or config file that is not a mapping, not parseable, or not
  valid UTF-8 now degrades to an empty document (with a one-line
  warning) instead of crashing every command — `klangk login` works as
  the repair flow. The state file and the managed `klangk.yaml` writes
  are atomic now, so an interrupted write can no longer leave a
  truncated file; and an add-server that would rewrite an unparseable
  `klangk.yaml` is refused with a clear message instead of silently
  discarding its contents.

- **Workspace name minimum length is enforced (#3110).** The API now
  rejects empty and whitespace-only workspace names on create, rename,
  duplicate, and import (422 on the JSON bodies, 400 on the import
  archive/request name). Previously `"name": ""` was silently accepted
  on create and rename; `klangk edit` already rejected blank `--name`
  client-side (#3103), and this closes the remaining surfaces (web
  frontend, TUI, direct API callers). Existing blank-named rows, if any,
  are unaffected — renaming away from one remains possible.

- **Exec-stdin failures surface as the command's exit status (#3124).** A
  container-side command that exits before consuming piped stdin (a file
  upload racing a dying container) no longer raises an unhandled
  broken-pipe error out of the files API. The per-call timeout now bounds
  runs that block writing stdin: a payload larger than the pipe buffer
  with a child that never reads used to hang the call forever with the
  timeout un-armed.

- **Malformed `KLANGKD_OIDC_CONFIG` files fail boot with a configuration
  error (#3124).** An empty file, a YAML mapping (e.g. a `providers:`
  wrapper), a syntactically broken file, or an entry with a missing or
  non-string required field used to crash startup with a raw
  TypeError/AttributeError/KeyError/ParserError; all now raise a
  `ConfigurationError` naming the problem (the message carries only the
  line/column mark — the file holds client secrets). Duplicate provider
  ids are rejected too — the second provider used to be silently
  shadowed at login while still showing its own button.

- **`KLANGKD_PORT` / `KLANGKD_EGRESS_PORT` values are validated
  (#3124).** A non-numeric, out-of-range (including the previously
  tolerated `0`), or explicitly emptied value now fails settings
  construction with a named-setting error instead of crashing the
  launcher with a raw `ValueError`; accepted values are stored
  normalized (`" 30"` → `"30"`), so whitespace-prefixed forms can no
  longer slip past the browser/egress port-separation check. Empty
  means unset (headless browser port / the built-in egress default).
  An emptied str-typed boolean setting likewise resolves to its field
  default: `KLANGKD_SMTP_USE_TLS=` keeps TLS on rather than silently
  disabling it, and `KLANGKD_ALLOW_SUDO=` resets the sudo ceiling to
  its default (open) instead of closing it.

- **A failed first-time home skeleton copy retries on the next connect
  (#3124).** A per-handle home left empty by a transient
  `klangk-setup-home` failure used to stay bare forever (no
  `.profile`/`.bashrc`); an empty user directory now re-triggers the
  copy, matching the shared-home behavior.
- **A mistyped workspace-created-hook change no longer fails the
  create (#3123).** A hook assigning a mistyped value (e.g.
  `allowed_domains = [123]` or a non-iterable `mounts`) crashed the
  validation step and 500'd the workspace create/import/duplicate
  request. Per the documented failure semantics the change is now
  logged as invalid and the workspace is returned exactly as created.

- **`KLANGKD_SOCKET` / `KLANGKD_CADDY_ADMIN_SOCKET` changes on SIGHUP
  now warn (#3123).** Both sockets are bound for the life of the
  process (uvicorn's listener, the Caddy child's admin UDS); a reloaded
  value never applied and silently desynced the proxy supervision. The
  reload now logs the same "requires a full process restart" warning
  as `KLANGKD_PORT`/`KLANGKD_LISTEN`.

- **Typo'd CIDR settings no longer wedge the proxy (#3123).** An
  invalid entry in `KLANGKD_TRUSTED_PROXY_CIDRS` or
  `KLANGKD_CONTAINER_SUBNETS` flowed into the Caddyfile, where Caddy
  rejects it at adapt time — the config push failed and the watchdog
  kill/respawn loop left the whole proxy down on a typo. Invalid
  entries are now skipped with a warning (fail-closed: narrower
  egress allowlist / loopback-only proxy trust, and an all-invalid
  container-subnet list denies container egress like a blank one).

- **`klangkd doctor` pacman hints use `-S` (#3123).** The install hint
  for an arch-family host emitted `sudo pacman install <pkg>`; pacman
  has no `install` subcommand, so the hint failed verbatim. It now
  emits `sudo pacman -S <pkg>`.

- **Non-mapping `klangk.yaml` no longer crashes every CLI command
  (#3094).** A config file holding valid YAML that is not a mapping
  (a stray list or a bare string) used to abort every `klangk`
  invocation with `AttributeError`. It is now treated as an empty
  config, so commands run with defaults and a bad alias lookup misses
  instead of crashing.

- **`klangk sandbox` copy/setup paths are now shell-quoted (#3093).** A
  `.klangk-sandbox.yaml` `copy:` destination containing spaces (or a
  `mount-at`/`setup:` path containing spaces or single quotes) no longer
  misdirects the copy or breaks the generated setup command. The setup
  script now runs as `bash <quoted path>` instead of `bash -c '<path>'`;
  the `-c` layer re-parsed the quoted path as a command line and
  word-split it. `setup:` was never documented to accept shell syntax,
  so a script path is all it passes now.

- **Concurrent duplicate registrations and invite accepts return a
  clean 4xx (#3101).** The production email-verification `register`
  path and `accept-invite` only pre-checked for an existing account
  before inserting, so two racing requests made the loser hit a 500;
  they now catch the lost race and return the same 400 as the
  pre-check.
- **One pending invitation per email (#3101).** `POST /invitations`
  now enforces a single pending invitation per address (partial
  unique index, migration 0028 revokes any duplicate pending rows
  created by the old race; history is kept), so concurrent sends can
  no longer mint two pending invitations.
- **Workspace member/group share is atomic and duplicate-safe
  (#3101).** Sharing a workspace with a user or group writes the whole
  permission block in one transaction: a failure no longer leaves a
  partial share, concurrent shares no longer collide on positions, and
  re-sharing an already-shared principal is rejected with 409 instead
  of stacking a duplicate block.
- **Export no longer hides a tar failure behind a 200 (#3101).** A
  missing `tar` binary fails the export with a clean 500, and a tar
  that dies mid-run aborts the download (logged at ERROR with tar's
  stderr) instead of delivering a truncated archive; the CLI removes
  the partial file and reports the interrupted transfer.

- **Cancellation during a consent verdict no longer hangs the egress
  relay** (#3089). A decider disconnect or backend shutdown landing while
  the verdict's DB write was in flight escaped as a `CancelledError`
  before the held connection's Future was resolved — with the hold popped
  and its timeout cancelled, nothing could ever resolve it, so the
  sidecar relay waited forever and the request row lingered pending
  against the workspace's cap. The resolve path now fail-closes the
  Future (deny) and expires the row before letting the cancellation
  propagate.

- **`klangk edit` restart-after-rename crash (#3091).** Restarting a
  running workspace renamed by the same edit (`klangk edit my-ws --name
new-ws …` then answering `y` to the restart prompt) crashed with an
  uncaught `WorkspaceNotFoundError` traceback before the restart could
  fire. The restart now targets the workspace id, the confirmation
  echoes show the new name, and an empty `--name` is rejected instead
  of silently renaming the workspace to the empty string.

- **Consent-popup socket names mangled digits (#3091).** A typo in the
  workspace-id sanitizer's character class (`0189` instead of the range
  `0-9`) replaced digits 2–7 with `-` in the tmux socket and session
  names, mangling nearly every workspace's consent-popup socket and
  letting distinct ids collide on one socket.

- **`klangk shell` / `klangk sandbox` timeout tracebacks (#3091).** A
  container that never reaches the ready state within the wait budget
  now surfaces a one-line timeout message and a nonzero exit instead
  of a raw `TimeoutError` traceback.

- **TUI edit form: rename + "Restart now" now restarts the renamed
  workspace (#3096).** Accepting the post-save restart prompt on a
  renamed workspace called `restart` with the pre-rename name, which
  missed and left the workspace running the old configuration ("Saved,
  but restart failed"). The restart now targets the workspace by id, so
  it resolves after a rename and cannot hit a different workspace that
  happens to share the name.

- **`klangk consent-decide`: picking a duration while disconnected no
  longer strands the picker (#3096).** Submitting a duration from the
  picker with the socket down flashed to the picker screen (which has no
  status bar), raising before the modal dismissed — Enter then did
  nothing and only Esc closed the picker. The flash now lands on the
  queue screen's status line and the modal dismisses normally.

- **TUI edit form: Enter in the /tmp size field now submits (#3096).**
  The edit form's Enter-submit id list omitted the /tmp size input, so
  pressing Enter there was a no-op (the create form already submitted).
  Enter now saves the form from that field as well.

- **Admin user email updates are now validated (#3097).** `PATCH
/api/v1/users/{id}` rejects a malformed address (400 "Must be a valid
  email address") and an address already used by another account (400
  "Email already in use") instead of persisting invalid emails or
  returning a 500 off the `users.email` unique constraint.

- **Workspace rename conflicts now return 409 (#3097).** `PUT
/api/v1/workspaces/{id}` renaming a workspace onto another name the
  owner already holds returns the same 409 "A workspace named … already
  exists" as the create, duplicate, and import paths, instead of a 500.
  Explicitly nulling a non-nullable field (`name`, `setup_state`,
  `egress_mode`) is now a 400 instead of a 500 or a misleading 409.

- **`egress_request` frames now carry one request shape on every delivery
  path (#3082).** A live hold fanned out to deciders carried a request dict
  without `duration`/`revoked_at`/`revoked_by`, while a connect-time replay
  of the same held request carried all columns. Both paths now emit the full
  column set, so clients keying on any field behave identically whether a
  request arrives live or via replay.

- **Stale consent pause no longer auto-allows in static-mode workspaces
  (#3080).** The consent-pause window (`set_consent_pause`, e.g. a 1d
  pause set by a decider) was honored by the egress gate even after the
  workspace's `egress_mode` was switched away from `interactive`, so a
  static (default-deny) workspace auto-allowed every off-list
  destination for the rest of the window. The pause is now applied only
  while the workspace is in interactive mode, and an actual `egress_mode`
  switch clears the stored window (unrelated edits that merely re-send
  the current mode leave a live pause alone) so it cannot resurrect on a
  later switch back.
- **Consent rate-limit wedge on DB errors (#3081).** When a database
  error struck while recording a decider verdict or expiring a timed-out
  request, the row stayed `pending` forever with no live hold — invisible
  to deciders yet counted against the workspace's pending cap
  (`KLANGKD_EGRESS_CONSENT_RATE_LIMIT`), eventually wedging every new
  request into `rate_limited` denials. Both error paths now fail-close
  the verdict and retry expiring the row once; a row that still cannot
  be expired is reaped at the next backend start instead of lingering
  for the retention window.
- **Consent revoke races and shared `forever` entries** (#3083). Two
  deciders revoking the same verdict concurrently no longer show a
  misleading "revoke failed — still in effect" ack on the losing side
  (it is now an idempotent success). Revoking one of two identical
  `forever` verdicts for the same `host:port` no longer retracts the
  durable allow/reject-list entry the surviving verdict still needs
  (it is retracted only when the last `forever` verdict sharing it goes).

- **Malformed client frames no longer drop the WebSocket session
  (#3071).** Any command handler exception now gets an error frame and
  the connection stays up, instead of ending the whole session:
  non-object JSON frames (`[]`, `"x"`, `3`) are rejected as invalid
  frames, invalid base64 in `exec_input`/`ssh_agent_data` is dropped,
  a `terminal_resize` with null/string/oversized dimensions falls back
  to the last known size (previously persisted and poisoning later
  starts), and a dead SSH-agent relay is torn down instead of killing
  the socket. A `PodmanError` during `workspace_connect` now surfaces
  as `Container start failed: …` (matching the restart path, #2676)
  rather than a generic handler-error frame.
- **Crash auto-restart (#3074).** `KLANGKD_CONTAINER_RESTART_ENABLED`
  never restarted a workspace: every crash-detected death was
  misclassified as a user action and left the workspace stopped with no
  crash state on `/status`. Restart now works as documented (backoff,
  bounded retries, crash-loop terminal state), and the death cause is
  recorded for the status API even with restart disabled.
- **`KLANGKD_MEMORY_EVICTION_ENABLED` reload (#3074).** A SIGHUP config
  reload that disables memory-pressure eviction now takes effect on the
  next poll instead of one cycle late, so turning the evictor off under
  sustained pressure cannot evict one more workspace after the reload.
- **Podman create retry and CI-aware bring-up budgets (#3064).** A
  `podman create` that stalls past its budget (120s local, 240s on CI)
  under load is now killed and retried once (idempotent via
  `--replace`), so a stalled create no longer 500s straight into the
  workspace start/connect path. On CI (`CI` env), create/start budgets
  double to 240s and container-readiness widens 60→240s; the CLI's
  WS-connect wait is now overridable via `KLANGKC_WS_CONNECT_TIMEOUT`
  (the e2e suites set it to 240s on CI).
- **Renaming a workspace no longer kicks the TUI detail screen back to the
  list (#3065).** The `workspaces_changed` push fired by the rename can
  reach the open detail screen before the edit form's save dismisses, so
  the reload resolved the workspace by its old name, missed, and was
  treated as a deletion — popping the form and the screen mid-rename. The
  screen now re-resolves the workspace by id on a name miss, adopts the
  new name, and stays mounted; only a genuinely deleted workspace pops.
- **Connects can no longer attach to a torn-down workspace session
  (#3070).** A WebSocket connect that raced the last member's
  disconnect could attach to a session already removed from the
  connection registry: the new member then silently missed every
  workspace broadcast (terminal tabs, shared terminals, lifecycle
  events) until reconnecting, and the orphaned session's background
  token-renewal task and window watcher leaked for the life of the
  process. The registry mapping is now re-verified under the session
  lock on attach: a popped-but-unsuperseded session reclaims its slot,
  and one that a newer session replaced routes the subscriber to the
  replacement.
- **SSH-agent relay and sidecar reconnect robustness (#3069).** The
  SSH-agent output relay no longer kills the whole WebSocket when the
  client falls behind (its `SlowClientError` is now swallowed like other
  socket errors, and a failed relay task can no longer leak its exception
  through disconnect cleanup, which also skipped removing the connection
  from the registry). A reconnecting egress sidecar's registration is now
  identity-guarded, so the stale socket's teardown can no longer drop the
  replacement's registration and leave egress revocations unenforced.
- **Unsharing or deleting a shared terminal no longer kills every
  non-owner tmux session in the container (#3072).** One
  `unshare_window`/`delete_shared_terminal` frame used to run a
  group-unscoped `tmux kill-session` sweep that terminated other
  members' live terminals, the workspace service command, and the
  window watcher (leaving tab-strip sync dead until reconnect). Kicks
  are now per viewer: only the connections actually viewing the
  affected window are disconnected, and viewers of the owner's other
  shared windows keep their sessions. Deleting an agent-owned shared
  terminal also works now — the close targeted a tmux session named
  after the agent's user id instead of the `service` session and
  always failed with "Failed to delete shared terminal".
- **Terminal share/unshare/close no longer hang clients on refusal paths
  (#3057).** The WebSocket handlers for `share_window`, `unshare_window`,
  `join_shared_terminal`, and the own-window commands (`terminal_new_window`,
  `terminal_close_window`, `terminal_rename_window`,
  `terminal_list_windows`, `terminal_select_window`) sent no frame when the
  connection had no attached terminal or workspace session, so a client
  waiting for a confirmation timed out (10s CLI hang in
  `klangk terminal share`/`unshare`); every refusal now answers with an
  `error` frame, and unsharing an already-unshared terminal broadcasts the
  current list so the command exits promptly with a definite outcome.
- **Static egress: off-list DNS names return NXDOMAIN (#3041).** A
  `static` workspace with egress filtering resolved every off-list name
  through the upstream resolver when the consent stack was wired (which is
  every filtered workspace) — a resolution oracle and a DNS exfiltration
  channel that bypassed the allow-list. klangkd now passes the workspace's
  `egress_mode` to the network sidecar explicitly
  (`KLANGKNETWORK_EGRESS_MODE`), and the sidecar refuses off-list queries
  locally in `static` mode. `interactive` and `allow` modes are unchanged
  (they resolve and gate the connection at the SYN). The fix lives in the
  sidecar image: on manual-registry deploys update `network_sidecar_image`
  together with klangkd, or the env var is silently ignored (the all-in-one
  host image embeds both). A mode change on a running workspace takes effect
  at the next container start, like allow-list edits. See
  [Egress filtering](features/egress-filtering.md).

- **Tab-strip sync survives a container restart (#3015).** The
  server's tmux window watcher was built once per workspace session
  and never replaced when the container was recycled while members
  stayed connected, so idle clients' terminal tab strips and the
  shared-terminal list went stale until each user took a terminal
  action or reloaded. A stale or dead watcher is now detected on every
  (re)connect and rebuilt against the current container.
- **Workspace restart now notifies every connected member (#3008).**
  When one member restarted a workspace container, only that member's
  client was told — other members' terminals went dead with no recovery
  until a full page reload. The restart lifecycle events are now
  broadcast to every connection in the workspace; the broadcast
  `container_ready` clears each member's stopped overlay and re-starts
  their terminal automatically.
- **Admin → Events table no longer runs off the screen (#3006).** The
  table columns now share the panel width — long container ids and
  network-namespace names ellipsize with a tooltip carrying the full
  value — instead of laying out wider than the viewport and pushing
  content past the right-hand screen edge.

- **Blank terminal for members joining a running workspace (#3000).**
  Opening a workspace whose container is already running (e.g. a member
  opening a shared workspace after the owner) could leave the Terminal
  tab permanently blank: the one-shot `container_ready` event fired
  before the permission-gated terminal view mounted, so the terminal
  was never started. The WebSocket client now tracks container
  readiness, and a late-mounting terminal view catches up from that
  state.
- **GitHub device-flow gate now normalizes the git host (#2963).**
  With `KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID` configured, a remote
  written as `https://github.com:443/...`, `https://GitHub.com/...`, or
  `https://github.com./...` skipped the device flow and fell back to the
  manual PAT dialog; the credential helper now matches those spellings
  the same way the browser dialog does.
- **Restart affordances gated on `restart-workspace` (#2939).** The
  container-stopped overlay's Restart button and the settings panel's
  "Restart now" action are hidden for members without the permission
  (spectators, custom ACLs) — the server already refused the
  `restart_container` message, so the buttons only surfaced a
  permission-denied error. The pending-restart notice still shows;
  only the action hides. The WebSocket gate matrix is now documented in
  the ACL reference.

- **Git credential dialog hint text (#2954).** The browser-side
  "Git credentials" dialog now derives its field wording from the host
  (case-, port-, and trailing-dot-insensitive): `github.com` keeps
  "GitHub username" and the PAT-flavored token label and placeholder,
  while any other host (gitlab.com, bitbucket.org, gitea, self-hosted)
  shows neutral "Username" / "Token or password" text.

- **GitHub device-flow login runs once per tab session (#2953).**
  With `KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID` configured, every
  authenticated git operation (push, pull, clone) started a fresh
  OAuth device-flow login — the browser tab's credential cache,
  populated by git's `store` after a successful push, was never
  consulted on the device-flow path. The credential helper now checks
  the cache first (new `peek` bridge operation); one login covers
  subsequent github.com operations in that tab, and a rejected token
  still triggers `erase` → a fresh login. Rebuild the workspace image
  and the frontend bundle to pick up the fixed helper and feature.

- **OpenClaw sandbox: every workspace sharing a mount now runs its own
  gateway (#2947).** openclaw 2026.8.1 added a single-instance gateway
  lock under its state dir, which defaulted onto the shared `/openclaw`
  mount — so with several workspaces at one mount only the first
  workspace's gateway could start and every other workspace's health
  check reported `unhealthy` forever. Setup now points
  `OPENCLAW_STATE_DIR` at the per-workspace agent home (own lock and
  state per workspace) while `OPENCLAW_CONFIG_PATH` keeps the shared
  config on the mount.

- **Window watcher start/stop race (#2929).** A quick connect→disconnect
  on a live workspace could land `stop()` inside the tmux control-mode
  watcher's still-in-flight start, leaving the host-side podman exec
  reader and the container-side tmux control session running with
  nothing left to ever stop them. The watcher now records the stop
  request before reading its teardown state, so a racing start tears
  itself down once its exec completes.

- **Workspace session background tasks are now strongly referenced
  (#2913).** The tmux window-watcher start/stop and the debounced window
  re-sync ran as unreferenced fire-and-forget tasks, which CPython may
  garbage-collect mid-execution. When the collected task was the window
  watcher's stop, the per-workspace tmux control-mode teardown never
  completed, leaking the host-side podman exec reader and its
  container-side tmux control client until the container stopped.
- **Memory leak on workspace delete (#2912).** Deleting a workspace
  (or a user, whose workspaces cascade-delete) now drops the daemon's
  per-workspace registry entries — one start/stop lock and one stop-epoch
  counter per workspace id ever started, previously retained for the
  process lifetime. Found by the long-lived-process memory audit (#2911).

- **LLM router: replaced upstream clients are now closed reliably
  (#2928).** Reconfiguring the passthrough LLM provider (e.g. on SIGHUP)
  closed the old upstream HTTP client via an unreferenced background
  task that CPython could garbage-collect mid-execution, leaving its
  connections open; a failing close was also silently dropped. The close
  is now strongly referenced and its failure logged.

- **`klangkd doctor` names the right package for a missing `ip` command
  (#2921).** The warning hint now says `sudo dnf install iproute` on the
  Red Hat family and `sudo apt install iproute2` elsewhere (zypper, apk,
  pacman, brew likewise) instead of `sudo dnf install ip`, which names a
  nonexistent package.

- **Admin users list marks the system agent (#2892).** The built-in
  `klangk@example.com` user now carries a SYSTEM badge and a fixed-identity
  note instead of looking like an ordinary account, and tapping its row no
  longer opens the edit dialog (its email, handle, and password are fixed).
  The group-member, ACL, share-with-user, and transfer-ownership pickers
  omit it as well, matching the server's refusal to make it a principal.

- **Access-revoked restart loop (#2891).** When a user's workspace access
  was revoked (share removed, ACL changed, workspace deleted) while a
  "Container stopped" overlay was up, pressing its Restart button was
  refused forever with no explanation — the overlay and button kept
  reappearing. The server now tags those WebSocket refusals with
  machine-readable `forbidden` / `not_found` codes, and the frontend
  swaps the overlay for an "Access to this workspace has been revoked"
  view with only a "Back to workspaces" action.
- **Documentation audit (#2889).** Every page under `docs/` plus the
  top-level README/CONTRIBUTING/SECURITY was checked against the code it
  describes, and the stale claims found were corrected in the same pass —
  notably: default container CPU/memory/PIDs caps are non-empty since #2042,
  `KLANGKD_AUTH_MODES` never defaults to `both` via OIDC config, `default_user`
  defaults to `<unixuser>@example.com`, malformed
  `KLANGKD_NETFILTER_DEFAULT_DOMAINS` aborts startup since #1939, the upload
  cap env var is `KLANGKD_FILE_UPLOAD_SIZE_MAX`, and the reference tables now
  cover all settings fields and API/WebSocket endpoints (including
  `klangk consent-decide`, `/ws/consent-decider`, `/ws/egress-sidecar`, and
  the `/llm-proxy/*` routes).

- **Files tab is hidden without the `files` permission (#2886).** A
  spectator's workspace page mounted the Files tab unconditionally, so
  opening it failed with `Cannot list this directory: Permission denied`.
  The tab now mounts only for principals whose workspace permissions
  include `files` (or `*`) — the same gate as the Sharing tab — and
  `?file=`/`?dir=` deep-links and terminal path taps no-op instead.

- **Workspace stopped overlay now clears when the container returns on its
  own (#2701).** The blocking "Container stopped — Restart" overlay
  only cleared when the restart came from its own button; if the container
  came back another way (socket reconnect after a server cycle, auto-start,
  a restart initiated from the settings panel), the overlay stayed on top
  of a live terminal. Any `container_ready` event on the socket now
  clears it.

- **`klangk terminal share` / `unshare` / `ls` (#2876).** A server
  refusal — e.g. `Permission denied` for a member without the
  `share-terminals` permission — now prints a one-line error and exits
  nonzero instead of a raw Python traceback. Timeouts name the failing
  wait, and a 4001/4002 handshake rejection reports the session-expired
  hint instead of a stack trace.

- **`klangk exec` (#2876).** Server errors and expired sessions during
  exec now print a one-line message and exit nonzero instead of a raw
  traceback, matching the terminal commands.

- **Unsharing a terminal tab no longer requires the `share-terminals`
  permission (#2875).** A workspace member who shared a terminal tab and
  then lost the permission was stuck: the tab stayed shared (readable by
  every member with spectate access) with no working unshare control.
  The server now always lets a member unshare their own tab — the UI
  affordances stay operable wherever own tabs are visible, and `klangk
terminal unshare` works for any role — since unsharing only reduces
  exposure. Sharing still requires the permission.

- **Boot auto-start no longer ignores a disabled `allow_autostart`
  (#2796).** `KLANGKD_ALLOW_AUTOSTART=false` (any non-truthy non-empty
  string) previously read as _enabled_ on the boot path — workspaces with
  auto-start were started at daemon boot even though every other surface
  (including `/api/v1/config`) reported auto-start off. The same fix
  applies to `KLANGKD_TEST_MODE`: any non-empty value (including
  `false`/`0`) previously auto-verified new registrations; it now parses
  like every other boolean setting.

- **`KLANGKD_SMTP_USE_TLS=yes` no longer disables STARTTLS (#2796).** The
  SMTP toggle was the one str-typed boolean consumer that did not accept
  `yes` as truthy, silently sending mail credentials over plaintext, and
  an explicit `smtp_use_tls:` (null) in YAML crashed at first send. Both
  now parse through the shared boolean setting parse.

- **Files tab: a directory listing failure no longer renders as an
  empty directory (#2766).** Listing an unreadable directory (e.g. a
  home volume root created without world `r-x` under a restrictive
  umask) now returns an error with the underlying message (403 for
  permission denied, 500 otherwise) shown in the viewer, and workspace
  start heals the home volume root to a listable mode.

- **`GET /api/v1/groups` no longer silently truncates at 200 rows
  (#2750).** The endpoint previously fetched a single 200-row page and
  returned it as a bare list; deployments with more groups (easy to hit
  once per-workspace role groups accumulated) lost the tail. It now
  paginates with the standard envelope — see the Breaking note above.

- **TUI server switch and logout now drop the old server's status
  WebSocket (#2704).** Switching servers in the TUI (`c`) left the status
  WS connected to the previous server until that server closed it, so
  live updates and the unreachable/reachability indicator kept tracking a
  server the user had already left. The switch now tears that connection
  down promptly, re-dials the new server immediately (also interrupting a
  pending reconnect backoff), and restarts the status loop when it had
  given up reconnecting — making the give-up screen's "switch server to
  reconnect" promise real. Logout and session expiry tear the connection
  down too, so a logged-out TUI no longer keeps receiving the old
  server's events and a re-login no longer runs two status connections.
- **SIGHUP `KLANGKD_FRONTEND_DIR` reload no longer breaks the restart
  (#2738).** The in-process frontend remount re-registered the
  no-cache HTTP middleware, which Starlette rejects once the app is
  serving — the recycle task then failed and had to run its recovery
  path. The middleware is now registered once at app build; a reload
  also re-resolves the `/branding` mount instead of serving the stale
  directory.
- **Startup config refusals now exit `EX_CONFIG` (78) consistently
  (#2738).** The password-mode admin lockout guard and the agent-handle
  collision guard raised bare `RuntimeError`, which a supervisor would
  restart-loop; they now raise the configuration error the launcher
  maps to `EX_CONFIG`, like every other deterministic boot refusal.
- **E2E container-readiness budget tolerates CI load (#245).**
  Frontend e2e tests running four Playwright workers on one runner
  could exceed the 120s container bring-up budget under contention;
  the budget now doubles to 240s on CI (local runs keep 120s).
- **Hosted-app URLs derived without a request now name the browser listener
  (#2732).** The hostname floor used when no browser request is in hand
  (sandbox setup starts, autostart) was a bare `localhost` — implying port
  80 — and `sandboxes/openclaw/setup.sh` fell back to `localhost:8995`, the
  container-egress port, which serves no `/hosted/` routes. The synthetic
  loopback hostname now carries the configured `KLANGKD_PORT`, so the URL
  printed at the end of a sandbox install is followable as-is.
  `KLANGKD_HOSTING_HOSTNAME` pins and trusted `X-Forwarded-Host` values are
  still used verbatim. Headless servers (no `KLANGKD_PORT`) no longer
  inject `KLANGKWS_PORT_MAPPINGS` / `KLANGKWS_HOSTING_*` at all — setup
  prints a pointer to the workspace page instead of a dead URL, and
  `klangk-hosted-url` errors cleanly.
- **Crash recovery no longer corrupts workspaces that reconnect
  mid-death-detection (#331).** When the crash monitor was handling a
  container's death while a user reconnect recreated the container, the
  fresh container's registry state could be dropped and its network
  sidecar torn down. Death teardown is now keyed to the dead container
  id, so a workspace re-bound to a fresh container is left untouched.
- **Service commands now recover from interrupted startup (#2740).**
  A podman exec killed by CPU load (or a client disconnect) during the
  service-command fire could leave a half-created `service-cmd` tmux
  window that silently suppressed the workspace's service command until
  the container was recreated. Any such half-fire is now cleaned up and
  retried on the next terminal start.
- **Status WebSocket follows a server switch (#2029).** The TUI's live
  status connection kept dialing the previous server after switching
  servers, producing a false "server down" overlay. It now re-reads the
  active server each cycle.
- **Workspace forms validate numeric resource fields (#2029).**
  Non-numeric or non-finite (NaN/Inf) values in the Idle timeout, CPU
  limit, or PIDs limit fields now show an inline field-named error
  instead of crashing the app or failing later at container start.
- **TUI no longer strands a deleted workspace's page behind a dialog
  (#2029).** Deleting a workspace while a dialog is open over its detail
  page now closes both the dialog and the dead page, returning to the
  workspace list.
- **TUI tolerates malformed server payloads (#2029).** A malformed
  `container_status` timestamp or OIDC provider entry degrades
  gracefully instead of crashing, and errors in status-event handling no
  longer masquerade as a lost connection.
- **TUI status-bar refresh is cheaper (#2029).** Refreshing the status
  bar no longer re-reads and re-parses the CLI state file several times
  per event on the UI thread.
- **No stale `live: …` segment in the TUI status bar after a server
  stop/recycle (#2690).** Routine broadcasts (`container_status`,
  `workspaces_changed`, `terminals_changed`, `service_health`) no
  longer write the live status segment — they already have dedicated UI
  surfaces — so the bar no longer shows a raw event name indefinitely
  after a drain cycle, and a pending scheduled stop/recycle countdown
  is no longer clobbered by them.
- **Consent popup shows the held request immediately (#2699).** The
  Allow/Deny row now renders within a few hundred ms of the popup frame
  appearing over a shell. Previously the decider ran its tmux
  `display-popup` subprocesses on the UI event loop, where the call
  blocks until its timeout, delaying the row by seconds while the hold
  countdown burned. The show now runs on a worker thread; a request held
  while the decider was reconnecting also surfaces promptly on the
  reconnect snapshot.
- **Concurrent shells on one interactive-egress workspace now land on their
  selected terminal (#2692).** Selecting a terminal (e.g. from the TUI with
  `terminal-open-cmd`) while another consent-popup shell on the same
  workspace was still open attached to the first shell's terminal instead.
  Abandoned shells also no longer accumulate sessions and popups after the
  terminal window is closed or the client is killed.
- **`klangk shell` selections copy to the local clipboard again (#2694).**
  The container tmux now runs with `set-clipboard external` plus the
  `clipboard` terminal feature, so a copy-mode selection emits OSC 52 to
  the attached client and reaches your terminal's clipboard through the
  WebSocket. `external` (not `on`) keeps container apps from reading the
  CLI user's clipboard through query forwarding. The consent-popup
  wrapper's local tmux runs `set-clipboard on` so it re-emits the inner
  shell's pane-originated OSC 52 to the real terminal — shells in
  external terminals (`KLANGKC_TERMINAL_OPEN_CMD`) and the TUI inline
  shell both work. Previously the escape was written by a helper with no
  controlling terminal and silently went nowhere. Requires a workspace
  image rebuild; workspaces started from an older image keep the old
  behavior until their container is recreated.
- **Idempotent `POST /auth/logout` (#2687).** Logout now returns 200 even
  when the presented token is already expired, revoked, or absent — the
  token being dead is logout's desired end state, not an auth failure.
  Disabled accounts logging out also get 200 (and their token revoked)
  instead of 403. Previously these cases returned 401/403, and clients
  reacting to them produced long request loops in the access log. The
  web client also no longer sends a logout request once its token is
  already cleared.

- **Admin dialogs validate email format before submitting (#2668).** The
  Add User, Edit User, and Invite User dialogs now check the address
  client-side against the same rule the server applies: a malformed value
  shows an inline "Enter a valid email" error and keeps the confirm button
  disabled, instead of surfacing a raw API error. `klangk admin invitations
send` rejects a malformed address locally the same way.

- **Admin route guard (#2669).** An authenticated non-admin visiting
  `/admin/users` directly (typed URL, stale bookmark or redirect) no
  longer sees the dead-end "No admin sections available" page; the
  router now bounces them to the workspace list. Logged-out visitors
  keep the normal login flow, and admins are unaffected.

- **Short image names resolve in fresh checkouts (#286).** devenv now
  exports `CONTAINERS_REGISTRIES_CONF` and seeds a
  `registries.conf` (`unqualified-search-registries = ["docker.io"]`)
  next to the podman signature policy, so image builds that reference
  short names (`alpine:3.21`, `python:3.13-slim`, …) no longer fail with
  `short-name ... did not resolve to an alias and no
containers-registries.conf(5) was found`.

- **Post-login redirect no longer leaks across sessions (#2670).** The
  "return to where you were" URL stashed when a logged-out user hits a
  protected route is now cleared on logout (and on any 401 that ends the
  session), so it can never outlive its session. It is also checked
  against the new session: a non-admin logging in on a browser where an
  admin previously logged out now lands on `/workspaces`, not the admin
  page the previous session was viewing.

- **TUI live status events stopped working entirely (#2612 regression).**
  The mount-time "last login" fetch ran as an exclusive worker in the
  default worker group, which cancelled the just-started status-WS and
  token-refresh loops on every startup — the TUI received no live
  events at all (reachability signals, workspace status changes, and
  the host-shutdown countdown). It now runs in its own worker group.- **TUI status line was invisible on the workspaces screen (#2661).**
  the host-shutdown countdown). It now runs in its own worker group.
- **TUI countdown was truncated off the right edge (#2661).** The
  scheduled stop/recycle countdown is appended to the status line as
  its last segment — past `server`/`user`/`last login` (~76 columns),
  it fell off the right edge of a typical terminal and was invisible.
  The live segment (countdown, host notices) now renders first; the
  static segments follow it.
- **TUI status line was invisible on the workspaces screen (#2661).** The status bar (server, user, live state) has been painted underneath
  the keybind footer since the screens refactor (#1875) — two
  bottom-docked Textual widgets fully overlap, and the later-mounted
  footer wins the row. It now stacks in its own docked container above
  the footer, which also makes the scheduled-host-action countdown
  (`host: shutdown at 23:00 (in 1h 12m)`) visible.(fix(tui): stack the status bar above the keybind footer)

- **Restart button with a still-running container (#2676).** Pressing
  Restart after an unclean host shutdown/restart no longer fails with a
  raw `dependent containers` podman error and a dropped WebSocket: the
  restart now always reads the workspace fresh from the database (so a
  live container is reused, like a reconnect does), a create-path start
  whose lingering network sidecar is pinned by a dependent removes the
  dependent first, and any remaining start failure is reported as an
  error frame with a clear message while the session stays connected.- **Workspace Restart button after a server restart (#2674).** Clicking
  Restart while the browser sat on the container-stopped overlay during a
  host shutdown/restart used to spin forever: the WebSocket had given up
  auto-reconnecting while the server was down, so the restart command was
  silently dropped. The button now reconnects and rejoins the workspace,
  which auto-starts the container, clears the spinner, and reattaches the
  terminal. If the server is still unreachable, the Restart button is
  restored so the user can retry.

- **First terminal load shows the bash prompt (#2671).** On the first
  open of a workspace terminal (fresh container or browser refresh),
  the prompt could be scrolled off the top of the viewport — only a
  blinking cursor on a blank screen — until Enter was pressed. The
  forced initial tmux redraw now uses the client's current terminal
  size instead of a possibly stale start-time size, so the prompt is
  visible immediately on attach.

- **Clean sidecar shutdown when its consent WebSocket is down
  (#2657).** Removing a workspace whose egress sidecar sat in the
  consent reconnect backoff dumped a raw
  `asyncio.exceptions.CancelledError` traceback to the journal and
  aborted teardown mid-way (exit code 1). SIGTERM teardown now
  swallows the cancelled reconnect task and completes every cleanup
  step.

- **Egress sidecar startup log noise on read-only `/proc/sys` (#2656).**
  The sidecar entrypoint's best-effort sysctl writes (disable IPv6,
  `rp_filter=0`) leaked alarming `Read-only file system` shell errors to
  journald on hosts that mount `/proc/sys` read-only in containers. Both
  writes are now fully silent; the egress deny is unaffected (ip6tables
  OUTPUT DROP remains the backstop).

- **Terminal tab state no longer flaps old→new under load (#2653).**
  The window watcher's window-list refresh could land between a
  rename/new/close command and its confirmation, briefly reverting
  the tab strip and shared-terminal list to the pre-command state
  (new → old → new) before the next event re-corrected it. Stale
  in-flight watcher snapshots are now discarded instead of applied.

- **Shared-terminal tab list not updating on rename under load
  (#2651).** Renaming a shared terminal could leave other users' tab
  lists showing the old name indefinitely: the window-watcher's
  debounced re-sync could apply the renamed window list to the session
  state before the rename command's own sync, erasing the change the
  sync's broadcast decision relies on — so the `shared_terminals`
  update was never sent to other workspace members. The update is now
  sent exactly once regardless of which path applies it first.

- **Memory-pressure eviction no longer stops a workspace mid-connect (#2527).**
  A reconnecting workspace's container is tracked from `podman create`
  but has no WebSocket subscriber until `container_ready`, so an armed
  evictor could stop the fresh container under the connecting client
  (seen as a reconnect that immediately fails under sustained memory
  pressure). Workspaces with a start/stop in flight (per-workspace lock
  held) are now skipped by the evictor.

- **`klangk terminal share`/`unshare` blind 10s timeout (#2633 CI
  flake).** The tmux window-watcher's re-sync broadcast the window list
  to clients without updating the in-memory map the share handlers
  read, so a share issued right after a watcher frame (racing the
  terminal-start sync under load) was answered "Window not found" —
  which the CLI's receive loop ignored, timing out after 10 seconds
  with a traceback. The watcher sync now updates the map through the
  same merge as every other sync path, and the terminal commands'
  receive loops surface server `error` frames immediately (#1966
  pattern) instead of waiting out the timeout.

- **Terminal window commands racing a fresh session (#2623).** A
  select/close issued immediately after opening a terminal could fail
  with `can't find session` under load: the session is created
  asynchronously by the attaching process, and the command could run
  first. Window commands now retry that cold-start condition briefly
  (up to ~3s) instead of failing, matching the existing tmux-socket
  startup retry.
- **Spurious "Slow client dropped" disconnects (#2623).** A client
  connecting to a workspace could be abruptly disconnected (WebSocket
  closed with no close frame) when another user's connection was tearing
  down at the same moment. The presence broadcast now discards
  connections that are already closing instead of failing the new
  connection's setup.
- **SSH agent forwarding readiness (#2535).** The `ssh_agent_started`
  event now fires only after the in-container relay socket is actually
  bound, instead of when the relay process was merely spawned. Commands
  issued immediately after starting forwarding (e.g. a scripted
  `ssh-add`) no longer race the relay startup under load.
- **Per-workspace idle timeout ignored on web-UI starts (#2514).** A
  workspace's `idle_timeout` settings override (#864) was only applied
  when started via `POST /workspaces/{id}/start`; a workspace started by
  connecting in the browser silently used the deploy-wide default. The
  override is now applied at the single container-start choke point, so
  every start path honors it (`0` = never idle out still works).
- **Traceback when a terminal open races an idle reap (#2514).** podman's
  "can only create exec sessions on running containers" refusal during a
  terminal open is now treated as the expected container-recycle race it
  is (clean warning + client error frame, #2178), not an ERROR traceback
  in the server log.
- **Add Workspace dialog netfilter section (#2508).** The
  allowed-domains editor now has an "Allowed Domains" title and
  description, matching the rejected-domains editor and the workspace
  settings panel.
- **Login-banner redirect loop on return visits.** Opening the app with
  a saved login token while a login banner was pending (`login_banner_every_visit`
  on, or a banner whose text changed since it was last accepted) hit a
  `/consent` ⇄ `/workspaces` redirect loop instead of showing the banner.
  The banner gate is now terminal while a banner is pending, so the
  consent page renders and accepting it proceeds as before.
- **Rules-screen focus race (#2362).** In `klangk consent-decide`'s rules
  view, two rule-set changes landing within one refresh cycle could drop
  the row highlight to the top of the list, so a subsequent `x` revoked
  the newest rule instead of the focused one. Focus is now remembered
  across rebuilds and restored to the focused rule (or a deterministic
  neighbor if it was revoked elsewhere).
- **Stale pause in the consent-decide TUI after expiry (#2498).** A finite
  pause window (15m/1h/1d) whose time elapsed in an idle workspace no longer
  lingers as `paused 0s` on the pause bar and `Filtering paused (resumes in
0s)` on the rules screen. Both views now clear the pause locally the
  moment the window lapses; indefinite (until-restart) pauses and live
  countdowns still render.

- **Consent-decider 403 storm on refused registration (#2490).** A refused
  decider WebSocket handshake (uvicorn answers every pre-accept close with
  a bare HTTP 403 — e.g. the workspace is no longer `interactive` egress
  mode, or permissions were revoked) now logs the reason, user, workspace,
  and User-Agent in klangkd's log instead of an anonymous 403. The decider
  client refreshes its token and retries once, then falls back to a slow
  60s retry instead of reconnecting every 5s; its status line shows
  `refused — retrying every 60s`, and it self-heals if the refusal cause
  goes away mid-session.

- **Network sidecar containers no longer orphaned on reap/sweep (#2476).**
  A workspace joins its sidecar's netns via `--network container:<sidecar>`,
  so podman refuses to remove a sidecar while its workspace still shares that
  netns ("has dependent containers"), and `rm -f` does not override that.
  klangk creates the sidecar before the workspace, so a bulk removal that did
  not order by role could hit the sidecar first, remove only the workspaces,
  and leave every sidecar running. The startup container reapers and the e2e
  teardown sweep now remove workspace containers before network sidecar
  containers, so both come down in one pass instead of the sidecar lingering
  until a second startup.
- **Egress activity resets the idle timer (#2479).** A workspace whose only
  activity was outbound network egress used to be treated as idle and stopped by
  the idle timeout, because egress (workspace → network sidecar → internet)
  bypasses klangkd and never bumped the container's `last_activity`. The network
  sidecar now sends a flood-gated `{type:activity}` frame to klangkd on every
  DNS query and queued connection SYN (`KLANGKNETWORK_EGRESS_ACTIVITY_GATE`,
  default 60s, jittered to 0.5×–1.0× per send so a fleet of sidecars never herds
  onto a synchronized frame rate — at most one frame per workspace per ~30–60s,
  with the first event after a quiet period forwarding immediately), and klangkd
  bumps the workspace's idle timer on receipt. Reloadable on sidecar restart; no
  operator action on upgrade.

- **A `once` egress-consent deny no longer blocks later same-host connects (#2463).**
  Denying a single connection once used to install a destination-scoped REJECT
  rule that silently rejected every _new_ connection to the same `host:port` for
  the fail-close window (~10s by default), with no re-prompt and no rule in the
  DB. The fail-close REJECT for a `once` deny is now scoped to the denied
  connection's source port, so a new connection (different source port)
  re-prompts as expected; `once` adds no DB rule and nothing keeps denying it.
  Timed/forever denies are unchanged (their destination-scoped REJECT is
  correct — the persisted rule governs re-prompting).

- **Flutter Net Rules page drops expired timed verdicts (#2467).** A timed
  active allow/deny now disappears from the Net Rules view the moment its
  window elapses, instead of lingering with a "0s left" label. The server
  only re-broadcasts the rules snapshot on discrete events (verdict/revoke/
  pause/reconnect), so the client prunes elapsed rules on its 1s tick.

- **Timed egress-consent allows no longer outlive their duration (#2465).** A
  timed `allow` verdict used to keep covering retries past its window via two
  leaks, both now closed. (1) Every re-resolve of the consented host re-learned
  its resolved IP for the response's DNS TTL (often minutes), so the learned
  ACCEPT rule outlived the verdict; the DNS-path learn is now bounded at the
  verdict's remaining window. (2) A consent-verdict rule was floored at the
  learned-IP `MIN_TTL` (30s), so a short verdict (the test-only `5s`) lived
  30s; the consent paths now use the verdict verbatim (no floor). Together a
  timed allow's rule lapses at the verdict and a retry past the window
  re-prompts (the `deny` side already behaved correctly). Static allow-list
  entries are unaffected (their DNS-learn floor is unchanged).

- **Timed egress-consent allows now cover the whole host (#2434).** A timed or
  `until restart` `allow` verdict now allow-lists the consented host for its
  whole duration — the same as `allow forever` already did — instead of only
  the IP resolved at the moment of the decision (`once` stays per-connection).
  This fixes intermittent `ECONNREFUSED` on a connection to a host the user had
  just allowed, when the connection resolved to a different (CDN-rotated) IP
  than the one consented.

- **Timed egress-consent denials now cover the whole host (#2446).** A timed or
  `until restart` `deny` verdict now deny-lists the consented host for its whole
  duration — the deny-side counterpart of the allow fix in #2434 — instead of
  only a short per-IP reject rule. A retry to an already-denied host (including
  a CDN-rotated IP) is now refused without re-prompting the user for a host
  they already denied (`once` stays per-connection); an in-effect `allow` still
  overrides an in-effect deny at the gate.

- **Dead-owner container reap at startup (#2342).** klangkd now stamps each
  workspace + network-sidecar container with the creating daemon's PID
  (`klangk.pid`; the sidecar also gains `klangk.managed=true`), and on startup
  removes any `klangk.managed=true` container whose recorded PID is no longer
  a live process — cleaning up workspaces a crashed or killed klangkd left
  running. Containers without a `klangk.pid` (from an older klangkd, possibly
  still running) are skipped, and a container whose owner is still alive is
  never touched.

- **Workspace export/import now round-trips `egress_mode` (#2402).** The
  export endpoint previously omitted `egress_mode` from the serialized
  metadata, and import did not read it, so a `static` (or `allow`)
  workspace silently imported as the deploy default `interactive` —
  starting a network sidecar and gating egress on consent with no
  indication. Export now serializes `egress_mode`; import restores it,
  validating it against the allowed `EGRESS_MODES` (`static` |
  `interactive` | `allow`) and falling back to the deploy default on an
  unknown or missing value.
- **Off-list egress fails fast when consent is unavailable (#2413).** When
  the network sidecar's WebSocket to klangkd is down (a klangkd restart or a
  proxy hiccup), off-list egress now fails fast with a connection refused
  (forged RST + a short REJECT backstop) instead of hanging for the kernel's
  ~127s SYN-retransmit window. On-list egress is unaffected (learned ACCEPT
  rules sit above the NFQUEUE gate); the temporary deny is bounded by
  `KLANGKNETWORK_EGRESS_REJECT_TTL` (default 10s), so no rule lingers past the
  outage and fresh off-list egress prompts again once the sidecar reconnects.
- **A timed consent `allow` no longer outlives its verdict (#2408).** The
  network sidecar tracked one TTL per learned IP, shared between the consent
  rule's lifetime and the DNS host-mapping's lifetime. A re-resolution (every
  new connection re-queries DNS) bumped that shared TTL to the DNS record's TTL
  (often far longer than the verdict), so a short `allow` (e.g. `5s`) could keep
  its ACCEPT rule for the DNS TTL instead of the consent duration — a
  fail-open. The rule's lifetime (`rule_expire`) is now tracked separately from
  the host mapping (`expire`), so the ACCEPT expires at the verdict while the
  host mapping lives on for naming; latent for `5m+` allows (the verdict
  usually exceeds the DNS TTL) but real for shorter allows or long-DNS-TTL
  hosts.
- **Network sidecar now stops promptly on SIGTERM (#2400).** The sidecar
  runs as PID 1 (`entrypoint.sh` execs `python3 /proxy.py`), and the Linux
  kernel ignores the default SIGTERM disposition for PID-namespace init (a
  handler must be installed), so every sidecar removal fell back to SIGKILL
  after the full 5s `podman stop -t 5` window — and occasionally wedged in
  `Stopping`, leaking a NET_ADMIN container. The sidecar now installs an
  explicit SIGTERM handler that cancels its event loop for a clean teardown
  (closes the consent WebSocket, unbinds NFQUEUE, closes the DNS socket); the
  teardown is bounded so it completes well within the stop timeout rather than
  re-introducing the 5s window. Workspace teardown and klangkd shutdown are no
  longer gated on the 5s SIGKILL fallback per filtered workspace.

- **Rate-limited egress connections now fail fast instead of hanging
  (#2399).** A SYN past the network sidecar's NFQUEUE rate limit previously
  fell through to the OUTPUT DROP policy, so `connect()` hung for
  `tcp_syn_retries` (~127s) / a `curl --max-time` (exit 28) with no consent
  request. The overflow is now REJECTed (tcp-reset) so the connection gets
  ECONNREFUSED at once. Still fail-closed (denied); only the failure mode
  changes. Normal consent holds are unaffected (a new SYN matches NFQUEUE
  under the limit).

- **`consent-decide` duration selector no longer shows two highlighted
  buttons at first render (#2360).** The first duration button ("once")
  grabbed initial focus on mount and rendered with the default focus
  background, so it read as "selected" alongside the real default
  (`restart`). Non-selected duration buttons are now transparent even when
  focused, leaving the selected (`dur-sel`) button as the only one with a
  background.

- **`duration=once` consent verdicts now re-prompt every subsequent
  connection (#2361).** The sidecar's SYN verdict cache was keyed on
  (destination, port), so an allow-once was reused for a later connection to
  the same destination (the 2nd+ `curl` passed without re-prompting). The cache
  - in-flight set are now keyed on the connection (source IP+port + dest), so a
    SYN retransmit still reuses the verdict but a new connection (new source
    port) is a cache miss and re-prompts. Non-`once` persistence is unchanged
    (an allow learns the IP; a deny installs a REJECT rule -- both per-destination
    rules ahead of NFQUEUE).

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
  (HTTP) `verify-workspace-token` check itself as a websocket — no ws
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

- **Host image ships `iproute2` (#2561).** Without it, caddy's
  container-subnet auto-detection failed inside the appliance and fell
  back to denying all RFC1918 peers — a host container run with a
  published port (`docker run -p 8997:8997`) refused every browser/API
  request with 403. Found by the new super-E2E suite.
