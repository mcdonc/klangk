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

- **`KLANGK_AUTH_MODES=none`: no-login single-user (local-dev) mode**
  (#1374). A new `none` auth mode lets the frontend and CLI obtain a token
  for the seeded default user with no password prompt, enabling a frictionless
  single-user dev/test loop (including the soliplex browser-delegate flow)
  and serving as the foundation for a "one binary, named deployment
  profiles" strategy (`local-dev` / `customer-locked` / `team`). The server
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

- **Default auth mode is now `none`** (no-login single-user, loopback-bound)
  when `KLANGK_AUTH_MODES` is unset and no OIDC provider is configured
  (#1374). Previously the unset default was `password`. A fresh klangk now
  "just works" locally with no password and is unreachable from the network.
  This is safe by construction — `none` refuses to start on a non-loopback
  bind unless `KLANGK_ALLOW_INSECURE_NO_AUTH=1` — but it is a behavior change
  on upgrade: **set `KLANGK_AUTH_MODES=password` (or `oidc`/`both`) explicitly
  before redeploying if you relied on the old default.** When an OIDC
  provider is configured (`KLANGK_OIDC_CONFIG`), the unset default stays
  `both`, unchanged. Note: `none` mode is not yet supported with the published
  Docker host image (a published port isn't loopback) — the Docker examples
  set `KLANGK_AUTH_MODES=password`; see #1391.
- **uvicorn now binds `127.0.0.1` by default** instead of `0.0.0.0`
  (`KLANGK_LISTEN`, new). Workspace containers could previously reach the
  backend directly via `host.containers.internal:$KLANGK_PORT`, bypassing nginx
  and therefore every per-location nginx ACL. nginx remains bound to `0.0.0.0`
  (container-reachable, so soliplex and hosted apps still work) and proxies to
  uvicorn on the loopback address. Operators who reach the backend directly —
  bypassing nginx — must set `KLANGK_LISTEN=0.0.0.0` to restore the old
  behavior. Applies to both the devenv dev server and the host container.
  (#1375)
- **`klangkc invite` moved under the `admin` group** (#1374). The top-level
  `klangkc invite <email>` command is gone, with no backward-compat alias.
  Use `klangkc admin invitations send <email>` (and list with
  `klangkc admin invitations ls`). Site-wide administration — users and
  invitations — now has a dedicated `admin` CLI surface matching the
  `terminal`/`volumes` noun-subgroup convention.

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
