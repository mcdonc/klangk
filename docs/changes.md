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

### Breaking

- **uvicorn now binds `127.0.0.1` by default** instead of `0.0.0.0`
  (`KLANGK_LISTEN`, new). Workspace containers could previously reach the
  backend directly via `host.containers.internal:$KLANGK_PORT`, bypassing nginx
  and therefore every per-location nginx ACL. nginx remains bound to `0.0.0.0`
  (container-reachable, so soliplex and hosted apps still work) and proxies to
  uvicorn on the loopback address. Operators who reach the backend directly —
  bypassing nginx — must set `KLANGK_LISTEN=0.0.0.0` to restore the old
  behavior. Applies to both the devenv dev server and the host container.
  (#1375)

### Added

- **Optional IPv6 ingress** (`KLANGK_NGINX_ENABLE_IPV6`, new). Set to `1` to
  add `listen [::]:${KLANGK_NGINX_PORT}` for dual-stack ingress, so IPv6-only
  clients can reach the app. Off by default: an unconditional IPv6 listen
  crashes nginx on kernels with IPv6 compiled out (`ipv6.disable=1` on the
  kernel cmdline). nginx→uvicorn stays IPv4 loopback internally (clients never
  see that hop), so this works independently of the backend bind address. The
  container ACL also now auto-detects IPv6 host addresses (in addition to
  IPv4), so a container reaching nginx over IPv6 is allowed on the container
  endpoints instead of hitting `deny all`; `::1` is never denied on the
  catch-all, mirroring the `127.0.0.1` treatment. (#1385)

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
