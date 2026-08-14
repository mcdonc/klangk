# Per-workspace network egress filtering

Klangk can restrict which external hosts a workspace container may reach,
so a deployment running AI agents or untrusted code isn't an open
exfiltration vector. The filter is **opt-in** per workspace _and_ per
deploy: a workspace with no `allowed_domains` keeps unrestricted outbound
networking exactly as before.

The mechanism uses a **network sidecar** — a small NET_ADMIN container
that shares the filtered workspace's network namespace and owns its
egress ruleset. Each
workspace that declares an allow-list runs behind the sidecar, which
default-denies outbound traffic and allow-lists only the declared
destinations (resolved at runtime by a DNS proxy, so DNS round-robin is
handled). A workspace **without** an allow-list keeps unrestricted
outbound networking exactly as before.

A step-by-step diagram of a single egressing request — DNS gate, SYN
gate at NFQUEUE, the consent loop, and the verdict — is in
[Anatomy of an egressing request](../architecture/egress-flow.md).

## How it works

1. A workspace carries an `allowed_domains` list (`host`, `.host`,
   `host:port`, `*.domain[:port]` wildcards, or IPv4 CIDR specs — see the
   [API](#api) section for the full grammar).
2. On container start, if the workspace declares `allowed_domains`, the
   backend starts a **network sidecar** container (`klangk-net-<ws-id>`,
   from the `network_sidecar_image` image, which defaults to
   `klangk-network-sidecar`) with `--cap-add NET_ADMIN` and
   `--dns 1.1.1.1`, then starts the workspace with
   `--network container:<sidecar>` so the two share a network namespace.
   The workspace container itself is unprivileged.
3. The sidecar's entrypoint installs the egress ruleset in the shared
   netns: a default `DROP` OUTPUT policy, loopback (by destination),
   established connections, a mark-scoped allow for the DNS proxy's
   upstream forwards, static CIDR allows, and the backend gateway
   (`host.containers.internal` on the klangkd port — a `/etc/hosts` entry
   the FQDN proxy can't learn, so it's allow-listed statically).
4. DNS is redirected: a `nat OUTPUT REDIRECT` sends all `:53` traffic
   (except the proxy's own marked forwards) to the sidecar's FQDN DNS
   proxy. The proxy resolves each query against a real upstream, and
   **allow-lists resolved IPs at runtime** — so a domain whose IPs rotate
   (CDN, DNS round-robin) stays reachable without a container restart,
   and a denied domain returns NXDOMAIN. This replaces the create-time
   IP-pinning the old OCI hook model used (#2255). A resolved IP is allowed
   **only for the DNS response's TTL**; the proxy re-resolves on the next
   query and a background sweep removes the allow-rule once the TTL
   elapses, so stale IPs do not linger (#2256). The workspace is told
   its resolver is `1.1.1.1` (a placeholder) — the `:53` traffic is
   `REDIRECT`ed to the proxy before it ever leaves, so `1.1.1.1` is never
   actually reached; the proxy forwards to a _different_ detected upstream
   (loop avoidance).
5. **IPv6 egress is default-denied** — the sidecar sets
   `ip6tables -P OUTPUT DROP`, so the v4 allow-list cannot be bypassed
   over IPv6 (#1936). `allowed_domains` therefore accepts only hostnames
   and IPv4 addresses (no `[ipv6]` literals), and AAAA records returned by
   DNS are ignored.

The ruleset is in place before the workspace process starts, and the
workspace lacks `CAP_NET_ADMIN` so it cannot flush the ruleset.

## Egress consent recording (#2242)

The network sidecar records every blocked destination to the
`egress_consent` table, regardless of `egress_mode`:

- **static** (the default) -> recorded as `denied`, `decided_by` NULL (no
  human), immediately. Static mode is strictly better than the old silent-deny:
  it logs every denied attempt for audit/review (`scripts/consent-watch.py`
  shows a live view). One row per (workspace, host, port); repeats don't spam.
- **interactive** -> recorded as `pending`, then a human can `allow`/`deny` it
  via the consent UI (**#2244, not yet wired**) before it auto-expires
  (`egress_consent_timeout`, default 30s; rate-limited per workspace via
  `egress_consent_rate_limit`, default 50).

The sidecar consumes its own NFQUEUE (`-j NFQUEUE --queue-num 5139`; it is the
netns owner with `NET_ADMIN`) and POSTs each blocked packet's destination to
klangkd's consent endpoint (workspace-JWT-authenticated via Caddy's
`forward_auth`); it also forwards denied DNS queries with their domain names
(NFQUEUE only carries raw IPs). The workspace JWT is bind-mounted into the
sidecar and refreshed on rotation, so it never goes stale.

A workspace sets `egress_mode` to `"interactive"` (default `"static"`) via the
API or CLI.

> **Current status:** static recording works end-to-end (deny + record). The
> interactive decide/notify UI that lets a human actually allow/deny a pending
> request **is not wired yet** (#2244) -- until then interactive requests simply
> expire. Do not enable interactive mode expecting real-time consent prompts.

## Enabling it (operator)

Egress filtering is **available by default**: `network_sidecar_image`
ships with a default (`klangk-network-sidecar`), so a workspace that
declares `allowed_domains` is filtered out of the box — no configuration
is required for the common case.

1. Make the sidecar image available to podman:
   - **All-in-one host image** (`scripts/build-host-image.sh`): nothing to
     do — the sidecar image is embedded as a tarball in the host image and
     `podman load`ed on first startup (#2301).
   - **Dev**: the `klangk:build-network-sidecar` devenv task builds it from
     `src/containers/network/`.
   - **Other deploys**: publish the image to your registry and point
     `KLANGKD_NETWORK_SIDECAR_IMAGE` at it if you don't use the default name.
2. Restart klangkd. A workspace that declares `allowed_domains` now starts
   behind the sidecar.

To **disable** egress filtering entirely, set
`KLANGKD_NETFILTER_ENABLED=false` (or YAML `netfilter_enabled: false`).
When disabled, a workspace that declares `allowed_domains` **fails to
start** (fail-closed) rather than running unrestricted — see
[Fail-closed behavior](#fail-closed-behavior).

### Deploy-wide default allow-list

Set a deploy-wide allow-list applied to **every workspace that doesn't
declare its own** `allowed_domains` (#1365) — e.g. to permit a curated
set (package registries, a git host) by default across the whole deploy:

```bash
export KLANGKD_NETFILTER_DEFAULT_DOMAINS=github.com:443,pypi.org,registry.npmjs.org
```

…or, durably in the YAML config file (`klangkd --config`):

```yaml
netfilter_default_domains:
  - github.com:443
  - pypi.org
  - registry.npmjs.org
```

Entries use the same `host` / `host:port` / IPv4 CIDR spec as a
workspace allow-list (`host` allows all ports; `host:port` allows a
single TCP port, 1–65535; `10.0.0.0/8` allows a whole subnet, optional
`:port` to scope it — #1935) and are validated server-side at startup.
A malformed value logs a warning and falls back to "no default" rather
than aborting the server (#1772). Read at boot and on SIGHUP
(reloadable).

**Override semantics.** A workspace with a non-empty `allowed_domains`
**replaces** the default (it does _not_ merge); a workspace with an empty
list (or `null`) **inherits** the default; if no default is configured,
the workspace is unrestricted. There is currently **no per-workspace
opt-out** into truly-unrestricted egress when a default is set — clear
the default server-side to permit unrestricted workspaces. The Flutter
create-workspace dialog pre-fills its Netfilter list with this default
as a starting set (the TUI does not yet — #1931).

## Configuring a workspace

Set `allowed_domains` via the workspace **Settings** panel (an
"Allowed Domains" list editor under Mounts / Environment Variables), the
CLI, or the API.

### CLI

Use the `--allow` flag (repeatable) on `klangk create` or `klangk edit`:

```bash
# At creation time
klangk create my-project --allow github.com:443 --allow pypi.org

# On an existing workspace (flags mode)
klangk edit my-project --allow github.com:443 --allow registry.npmjs.org

# Interactive mode — prompts to add/remove domains one at a time
klangk edit my-project
```

In interactive mode, `klangk edit` shows the current allowlist (if any),
then prompts to add or remove entries. Domains are validated before they
are accepted.

### API

```bash
curl -X PUT https://klangkd/api/v1/workspaces/<id> \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"allowed_domains": ["github.com:443", "pypi.org", "registry.npmjs.org"]}'
```

- `host` allows all ports to that host **only** — the apex, exactly
  (`github.com` does not cover `api.github.com`) (#2377).
- `.host` allows all ports to that host **and its subdomains**
  (`.github.com` covers both `github.com` and `api.github.com`).
- `host:port` / `.host:port` allow a single TCP port (1–65535) with the
  same apex-only / apex-plus-subdomains distinction.
- `*.domain` allows **subdomains only, not the apex** — `*.pypi.org`
  matches `downloads.pypi.org`-style subdomains but not `pypi.org`
  itself. Append a port to scope it (`*.pypi.org:443`). The three forms
  (bare, leading dot, `*.`) let you allow the apex only, apex plus
  subdomains, or subdomains without the apex (#2256, #2377).
  Matching always respects the dot boundary, so `evilexample.com` never
  matches an `example.com` spec.
- `10.0.0.0/8` allows an entire IPv4 subnet (CIDR notation); append a
  port to scope it, e.g. `10.0.0.0/8:443`. A CIDR is installed as a
  single iptables `-d <ip>/<plen>` rule and is **not** DNS-resolved, so
  it is the stable choice for a private range whose individual hosts you
  don't want to enumerate (#1935).
- Resolved IPs (for `host`/`*.domain` specs) are allowed **only for the
  DNS response's TTL**; the sidecar's proxy re-resolves on each query and
  drops the allow-rule once the TTL elapses, so a stale IP is not reachable
  indefinitely (#2256).
- IPv6 literals and IPv6 CIDRs (e.g. `[::1]`, `[2001:db8::1]:443`,
  `2001:db8::/32`) are **not** accepted — IPv6 egress is default-denied
  inside filtered containers, so a v6 destination is neither reachable
  nor enforceable (#1936).
- Each entry is validated server-side; malformed entries are rejected with
  HTTP 400.
- An empty list (or `null`) **inherits the deploy-wide default**
  (`KLANGKD_NETFILTER_DEFAULT_DOMAINS`); if no default is configured, the
  workspace is **unrestricted**. There is currently no per-workspace opt-out
  into truly-unrestricted egress when a deploy default is set — clear the
  default server-side to permit unrestricted workspaces.

A restart of the workspace container applies the change to a running
workspace (the ruleset is set at create time).

## Fail-closed behavior

If a workspace declares `allowed_domains` but egress filtering is **not
available** on the server — disabled via `KLANGKD_NETFILTER_ENABLED=false`,
or the sidecar image unset/cleared — the workspace **refuses to start**
rather than running unrestricted. Silently ignoring an allow-list would
disable a security control the user explicitly requested (#2254 review
B2). The `allowed_domains` value is still persisted, so it takes effect
the moment filtering is re-enabled.

A workspace **without** `allowed_domains` always starts unrestricted,
regardless of the filtering setting.

## Common service domain lists

When building an `allowed_domains` list, you need to know which hosts and
ports a service requires. The tables below cover the most-requested
services; the same research pattern (check the provider's firewall /
proxy docs, then test) works for any service.

> **Note:** Hostnames are resolved to IPs **at runtime** by the sidecar's
> DNS proxy, so a service whose IPs change (common with CDN-fronted domains)
> stays reachable without restarting the container. See the
> [Caveats](#caveats) section.

### GitHub (SSH git operations)

For `git clone git@github.com:…` / `git push` over SSH:

| Entry           | Purpose                |
| --------------- | ---------------------- |
| `github.com:22` | Standard SSH transport |

If upstream firewalls block port 22, GitHub offers an SSH-over-HTTPS
fallback on `ssh.github.com:443` (requires `~/.ssh/config` stanza —
see [Using SSH over the HTTPS port][gh-ssh-443]).

### GitHub (HTTPS git + API)

For `git clone https://github.com/…` and the `gh` CLI:

| Entry                | Purpose                                           |
| -------------------- | ------------------------------------------------- |
| `github.com:443`     | HTTPS clone / push / pull, web UI                 |
| `api.github.com:443` | REST & GraphQL API (`gh` CLI, credential helpers) |

If you also need raw file views, release-asset downloads, or Git LFS:

| Entry                                       | Purpose                     |
| ------------------------------------------- | --------------------------- |
| `raw.githubusercontent.com:443`             | Raw file content            |
| `objects.githubusercontent.com:443`         | Git LFS objects             |
| `github-releases.githubusercontent.com:443` | Release-asset downloads     |
| `codeload.github.com:443`                   | Archive / tarball downloads |

### GitHub (full development)

A workspace that uses the web UI, GitHub Packages, or Copilot in
addition to git typically needs a broader set. Use `.github.com`-style
leading-dot entries (apex + subdomains) or list the specific subdomains
you use:

| Entry                                       | Purpose                 |
| ------------------------------------------- | ----------------------- |
| `github.com:22`                             | SSH git                 |
| `github.com:443`                            | HTTPS git, web UI       |
| `api.github.com:443`                        | API                     |
| `ssh.github.com:443`                        | SSH-over-HTTPS fallback |
| `raw.githubusercontent.com:443`             | Raw file views          |
| `objects.githubusercontent.com:443`         | LFS objects             |
| `github-releases.githubusercontent.com:443` | Release assets          |
| `codeload.github.com:443`                   | Archives                |
| `ghcr.io:443`                               | Container registry      |
| `npm.pkg.github.com:443`                    | npm packages            |
| `pypi.pkg.github.com:443`                   | PyPI packages           |

GitHub warns that their domain list is not comprehensive and IP ranges
change over time — see [Allowing access to GitHub's services from a
restricted network][gh-firewall] and the live
[`/meta` API endpoint][gh-meta].

### Debian / Ubuntu (apt)

The default klangk workspace image is Debian Trixie. For `apt update`
and `apt install`:

| Entry                           | Purpose                           |
| ------------------------------- | --------------------------------- |
| `deb.debian.org:80`             | Main Debian archive (Fastly CDN)  |
| `deb.debian.org:443`            | Main archive over HTTPS           |
| `security.debian.org:80`        | Security updates                  |
| `security.debian.org:443`       | Security updates over HTTPS       |
| `cdn-fastly.deb.debian.org:80`  | Fastly CDN backend (CNAME target) |
| `cdn-fastly.deb.debian.org:443` | Fastly CDN backend over HTTPS     |
| `cdn-aws.deb.debian.org:443`    | CloudFront CDN backend            |

For GPG key operations (`apt-key`, `gpg --recv-keys`):

| Entry                        | Purpose                       |
| ---------------------------- | ----------------------------- |
| `keyserver.ubuntu.com:80`    | Primary keyserver (HTTP)      |
| `keyserver.ubuntu.com:443`   | Primary keyserver (HTTPS)     |
| `keyserver.ubuntu.com:11371` | HKP (HTTP Keyserver Protocol) |

**Ubuntu** workspaces replace the Debian entries with:

| Entry                          | Purpose                       |
| ------------------------------ | ----------------------------- |
| `archive.ubuntu.com:80`        | Main Ubuntu archive           |
| `archive.ubuntu.com:443`       | Main archive over HTTPS       |
| `security.ubuntu.com:80`       | Security updates              |
| `security.ubuntu.com:443`      | Security updates over HTTPS   |
| `ppa.launchpadcontent.net:443` | PPA downloads (if using PPAs) |

Port 80 is important — apt's default Debian/Ubuntu configuration
uses HTTP. The packages themselves are GPG-verified regardless of
transport.

### PyPI

| Entry                        | Purpose           |
| ---------------------------- | ----------------- |
| `pypi.org:443`               | Package index     |
| `files.pythonhosted.org:443` | Package downloads |

### npm

| Entry                    | Purpose                  |
| ------------------------ | ------------------------ |
| `registry.npmjs.org:443` | Package index + tarballs |

### Nix

For `nix build`, `nix develop`, `nix-shell`, and `nix-env -i` (the
binary cache, channels, and source tarballs):

| Entry                    | Purpose                                 |
| ------------------------ | --------------------------------------- |
| `cache.nixos.org:443`    | Default binary cache (Fastly CDN → S3)  |
| `channels.nixos.org:443` | Channel metadata, flake registry        |
| `releases.nixos.org:443` | Nix release tarballs, installer scripts |
| `tarballs.nixos.org:443` | Source tarballs for packages (fetchurl) |

Nix flakes typically pull inputs from GitHub, so the GitHub HTTPS
entries above are needed too (`github.com:443`, `api.github.com:443`,
`codeload.github.com:443`, `raw.githubusercontent.com:443`).

The CDN backends behind `*.nixos.org` are Fastly and S3. If your
firewall resolves CNAMEs or inspects SNI you may also need:

| Entry                            | Purpose                       |
| -------------------------------- | ----------------------------- |
| `nix-cache.s3.amazonaws.com:443` | S3 origin for cache.nixos.org |

If using [Cachix](https://cachix.org) binary caches, add each cache
you use:

| Entry                          | Purpose                       |
| ------------------------------ | ----------------------------- |
| `cachix.org:443`               | Main service                  |
| `devenv.cachix.org:443`        | devenv cache (example)        |
| `nix-community.cachix.org:443` | nix-community cache (example) |

All Nix traffic is HTTPS (port 443). No HTTP or non-standard ports
are required.

### GitLab

| Entry                     | Purpose            |
| ------------------------- | ------------------ |
| `gitlab.com:22`           | SSH git            |
| `gitlab.com:443`          | HTTPS git, web UI  |
| `registry.gitlab.com:443` | Container registry |

Self-managed GitLab instances use their own hostname instead of
`gitlab.com`.

### Bitbucket

| Entry                   | Purpose           |
| ----------------------- | ----------------- |
| `bitbucket.org:22`      | SSH git           |
| `bitbucket.org:443`     | HTTPS git, web UI |
| `api.bitbucket.org:443` | REST API          |

### Codeberg

| Entry              | Purpose           |
| ------------------ | ----------------- |
| `codeberg.org:22`  | SSH git           |
| `codeberg.org:443` | HTTPS git, web UI |

### Canonical Launchpad

| Entry                          | Purpose               |
| ------------------------------ | --------------------- |
| `launchpad.net:443`            | Web UI, API           |
| `git.launchpad.net:22`         | Git over SSH          |
| `git.launchpad.net:443`        | Git over HTTPS        |
| `ppa.launchpadcontent.net:443` | PPA package downloads |

### Anthropic API (Claude Code)

| Entry                       | Purpose                        |
| --------------------------- | ------------------------------ |
| `api.anthropic.com:443`     | Claude API (chat, completions) |
| `statsig.anthropic.com:443` | Feature flags / telemetry      |
| `sentry.io:443`             | Error reporting                |

### OpenAI API (Codex, ChatGPT)

| Entry                              | Purpose                      |
| ---------------------------------- | ---------------------------- |
| `api.openai.com:443`               | Chat, completions, Codex API |
| `cdn.openai.com:443`               | Static assets                |
| `openaiapi-site.azureedge.net:443` | CDN edge (Azure)             |

### Zencoder (z.ai)

| Entry                  | Purpose                                   |
| ---------------------- | ----------------------------------------- |
| `api.z.ai:443`         | LLM API (`/api/coding/paas/v4` base path) |
| `auth.zencoder.ai:443` | Authentication                            |

### OpenRouter

| Entry               | Purpose                               |
| ------------------- | ------------------------------------- |
| `openrouter.ai:443` | LLM API (`/api/v1` base path), web UI |

### Ollama

| Entry                  | Purpose                                  |
| ---------------------- | ---------------------------------------- |
| `cloud.ollama.com:443` | Ollama cloud API (direct from container) |

A self-hosted Ollama configured in `KLANGKD_LLM_MODELS` is proxied
through the backend's `/llm-proxy/` endpoint and needs no netfilter
entry. `cloud.ollama.com` is for workspaces that contact the Ollama
cloud service directly (e.g. from user code or an agent).

### Bare-domain shortcut

A domain without a port (e.g. `github.com`) allows **all** ports to
that host. This is convenient for quick iteration but less restrictive
than pinning individual ports.

[gh-ssh-443]: https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port
[gh-firewall]: https://docs.github.com/en/get-started/using-github/allowing-access-to-githubs-services-from-a-restricted-network
[gh-meta]: https://api.github.com/meta

### Deploy-wide default: all common services

The following `klangkd.yaml` snippet applies the union of every service
listed above as the deploy-wide default. Paste it into your
`klangkd.yaml` and every workspace that doesn't declare its own
`allowed_domains` inherits this list. A workspace that sets its own
list **overrides** (replaces) the default entirely.

Remove services you don't need — the tighter the list, the smaller the
attack surface.

```yaml
# Deploy-wide egress allow-list (union of all common services).
# Each workspace that does NOT set its own allowed_domains inherits this.
# A workspace with its own list overrides it completely.
netfilter_default_domains:
  # --- GitHub (SSH + HTTPS + API + assets) ---
  - github.com:22
  - github.com:443
  - api.github.com:443
  - ssh.github.com:443
  - codeload.github.com:443
  - raw.githubusercontent.com:443
  - objects.githubusercontent.com:443
  - github-releases.githubusercontent.com:443
  - ghcr.io:443
  - npm.pkg.github.com:443
  - pypi.pkg.github.com:443
  # --- GitLab ---
  - gitlab.com:22
  - gitlab.com:443
  - registry.gitlab.com:443
  # --- Bitbucket ---
  - bitbucket.org:22
  - bitbucket.org:443
  - api.bitbucket.org:443
  # --- Codeberg ---
  - codeberg.org:22
  - codeberg.org:443
  # --- Canonical Launchpad ---
  - launchpad.net:443
  - git.launchpad.net:22
  - git.launchpad.net:443
  - ppa.launchpadcontent.net:443
  # --- Debian apt ---
  - deb.debian.org:80
  - deb.debian.org:443
  - security.debian.org:80
  - security.debian.org:443
  - cdn-fastly.deb.debian.org:80
  - cdn-fastly.deb.debian.org:443
  - cdn-aws.deb.debian.org:443
  # --- Ubuntu apt ---
  - archive.ubuntu.com:80
  - archive.ubuntu.com:443
  - security.ubuntu.com:80
  - security.ubuntu.com:443
  # --- GPG keyservers ---
  - keyserver.ubuntu.com:80
  - keyserver.ubuntu.com:443
  - keyserver.ubuntu.com:11371
  # --- PyPI ---
  - pypi.org:443
  - files.pythonhosted.org:443
  # --- npm ---
  - registry.npmjs.org:443
  # --- Nix ---
  - cache.nixos.org:443
  - channels.nixos.org:443
  - releases.nixos.org:443
  - tarballs.nixos.org:443
  - nix-cache.s3.amazonaws.com:443
  - cachix.org:443
  - devenv.cachix.org:443
  - nix-community.cachix.org:443
  # --- LLM providers ---
  - api.anthropic.com:443
  - statsig.anthropic.com:443
  - sentry.io:443
  - api.openai.com:443
  - cdn.openai.com:443
  - openaiapi-site.azureedge.net:443
  - api.z.ai:443
  - auth.zencoder.ai:443
  - openrouter.ai:443
  - cloud.ollama.com:443
```

## Caveats

- **IPv6 egress is default-denied — IPv4 egress only.** The sidecar sets
  `ip6tables -P OUTPUT DROP`, so the allow-list can't be bypassed over v6
  (#1936). Hostnames resolve to IPv4 only (AAAA records are ignored), and
  `[ipv6]:port` literals are rejected by the validator. Trade-off: a
  workspace that genuinely needs IPv6 egress cannot use the filter — clear
  `allowed_domains` (and the deploy-wide default) to run it unrestricted.
- **`0.0.0.0/0` matches all IPv4 — don't use it to "disable" the
  filter.** A `/0` CIDR (e.g. `0.0.0.0/0`) is a valid spec but the
  ACCEPT rule it emits matches the entire IPv4 space, so it effectively
  runs the workspace unrestricted while looking like a real rule. The
  server logs a loud warning whenever a `/0` CIDR appears in an allow-list
  (workspace or deploy default). If you genuinely want unrestricted
  egress, leave `allowed_domains` empty (or set
  `KLANGKD_NETFILTER_ENABLED=false`) — those are the documented,
  obvious ways to opt out (#1935).
- **Hostnames resolve at runtime — no restart needed on IP change.** The
  sidecar's DNS proxy resolves each query against a real upstream and
  allow-lists the IP at runtime, so a service that rotates IPs (CDNs like
  Fastly, CloudFront, Cloudflare) stays reachable without restarting the
  container. Static CIDR ranges (`10.0.0.0/8`) are installed as a single
  stable `-d <ip>/<plen>` rule with no resolution (#1935).
- **A CNAME can widen egress to an attacker-steerable IP.**
  The proxy allow-lists every A record in a response, including those reached
  via a CNAME chain. A `host:port` spec scopes a learned IP to that one TCP
  port; a bare `host` allows all ports; and a learned IP expires with the DNS
  response's TTL (#2256). But within that port/TTL window, if an allowed
  domain CNAMEs to a host an attacker controls (or to a shared CDN frontend
  IP), that IP becomes reachable for the spec's ports. Prefer `host:port`
  specs and avoid allow-listing domains whose CNAME targets you don't control
  (#2279).
- **Dropping a domain from a running workspace does not revoke already-
  resolved IPs.** A resolved IP is allow-listed for the DNS response's TTL
  and the sidecar's proxy drops the rule once the TTL elapses, so stale IPs
  do not persist indefinitely (#2256). But the allow-list a running sidecar
  enforces is fixed at start — removing a domain from `allowed_domains` does
  not revoke egress to IPs the workspace already resolved; recreate the
  workspace (or wait for TTL expiry) to fully revoke (#2281).
- **DNS is redirected to the sidecar's proxy, not blocked.** Outbound
  `:53` is `REDIRECT`ed to the sidecar's FQDN DNS proxy, which resolves
  against a real upstream and allow-lists the IPs at runtime; a denied
  domain returns NXDOMAIN. This does **not** prevent DNS tunneling through
  the proxy to attacker-controlled domains (data can still be encoded in
  DNS queries). Treat the filter as an egress allow-list, not a complete
  anti-exfiltration guarantee against DNS-based channels.
- **Ruleset immutability depends on the runtime capability set.** The
  sidecar installs the iptables rules in the shared netns, and the
  workspace container is unprivileged (no `NET_ADMIN`). It is **not** a
  hard guarantee: running the workspace `--privileged`, adding
  `--cap-add NET_ADMIN`, or a permissive seccomp profile hands the
  entrypoint `iptables -F OUTPUT`, which flushes the ruleset and lets it
  exfiltrate freely. Do not run filtered workspaces privileged or grant
  `NET_ADMIN`.
- **Port granularity.** A spec allows either all ports (`host`, or a
  CIDR like `10.0.0.0/8`) or a single TCP port (`host:port`, CIDR with
  `:port`). Port-only rules (allow a port to any host) are not
  supported — that would be an exfiltration channel.
- **macOS hosts.** On macOS, podman runs inside a CoreOS VM via
  `podman machine`. The network sidecar runs as a normal container in
  that VM (it owns its netns + iptables), so egress filtering works the
  same way as on Linux — no host `iptables`/`nsenter` is needed.

## References

- [FQDN egress via a DNS-proxy sidecar][fqdn-sidecar]

[fqdn-sidecar]: https://github.com/mcdonc/klangk/issues/2250
