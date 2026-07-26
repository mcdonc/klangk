# Per-workspace network egress filtering

Klangk can restrict which external hosts a workspace container may reach,
so a deployment running AI agents or untrusted code isn't an open
exfiltration vector. The filter is **opt-in** per workspace _and_ per
deploy: a workspace with no `allowed_domains` keeps unrestricted outbound
networking exactly as before.

The mechanism uses OCI `createContainer` hooks — there is no proxy, no TLS
interception, and no microVM. Each workspace that declares an allow-list
gets iptables rules injected into its network namespace before its process
starts.

## How it works

1. A workspace carries an `allowed_domains` list (`host` or `host:port`
   specs).
2. On container start, if the deploy has enabled netfilter, the backend
   passes `--annotation klangk.netfilter.rules=<host:port,...>` and
   `--hooks-dir <dir>` to `podman create` (see the caveat below on
   `--hooks-dir` overriding default hook dirs).
3. The OCI hook (`klangk-netfilter.sh`, materialized by the backend into the
   hooks dir) fires at `createContainer` time, reads the annotation from the
   container state, resolves each host to IPs, and installs an iptables
   ruleset in the container's network namespace (via `nsenter` on the init
   pid).
4. The default `OUTPUT` policy is `DROP`; loopback, established
   connections, **DNS to the container's configured resolvers only**
   (read from its `/etc/resolv.conf`, not a blanket `udp/tcp 53` allow),
   the backend gateway (`host.containers.internal`, resolved from the
   container's `/etc/hosts`), and the resolved allowed destinations are
   `ACCEPT`ed. Everything else is dropped.

The hook runs **before** the container process starts, so the ruleset is in
place and immutable before any user code runs — `CAP_NET_ADMIN` is dropped
by the runtime before the container entrypoint executes.

## Enabling it (operator)

Netfilter is **armed by default**. At startup klangkd materializes the hook
script (`klangk-netfilter.sh`) and its config (`klangk-netfilter.json`)
into a hooks directory and registers the OCI `createContainer` hook — no
configuration is required for the common case.

1. Ensure `iptables`, `getent`, and `nsenter` are available where the OCI
   runtime executes (the host, or the Docker-in-Docker outer container —
   _not_ the workspace image). The documented DinD deployment already has
   `CAP_SYS_ADMIN` + `seccomp=unconfined`, which provides the necessary
   privileges.
2. The hooks dir defaults to `<state_dir>/oci-hooks`
   (`KLANGKD_STATE_DIR`/`oci-hooks`). Override
   `KLANGKD_NETFILTER_HOOKS_DIR` only when the OCI runtime can't see
   `state_dir` — a split runtime, a DinD outer container, or a
   `podman machine` CoreOS VM (where it must be inside the VM, since
   `podman machine` does not bind-mount arbitrary host paths the way
   Docker Desktop does):

   ```bash
   export KLANGKD_NETFILTER_HOOKS_DIR=/var/lib/klangk/netfilter-hooks
   ```

3. Restart klangkd. The log shows
   `Netfilter egress filtering enabled: OCI hooks installed in <dir>`.

To **disable** netfilter entirely (e.g. an environment without
`iptables`/`nsenter`, or where the hook can't be granted `CAP_NET_ADMIN`),
set `KLANGKD_NETFILTER_ENABLED=false` (or YAML `netfilter_enabled: false`).
When disabled, `enabled()` reports false, `--hooks-dir` is never passed,
and workspaces with `allowed_domains` fail open with a loud warning
(#1769). (#1774)

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

Entries use the same `host` / `host:port` spec as a workspace allow-list
(`host` allows all ports; `host:port` allows a single TCP port,
1–65535) and are validated server-side at startup. A malformed value
logs a warning and falls back to "no default" rather than aborting the
server (#1772). Read at boot and on SIGHUP (reloadable).

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

- `host` allows all ports to that host.
- `host:port` allows a single TCP port (port must be 1–65535).
- Each entry is validated server-side; malformed entries are rejected with
  HTTP 400.
- An empty list (or `null`) **inherits the deploy-wide default**
  (`KLANGKD_NETFILTER_DEFAULT_DOMAINS`); if no default is configured, the
  workspace is **unrestricted**. There is currently no per-workspace opt-out
  into truly-unrestricted egress when a deploy default is set — clear the
  default server-side to permit unrestricted workspaces.

A restart of the workspace container applies the change to a running
workspace (the ruleset is set at create time).

## Fail-open behavior

If a workspace declares `allowed_domains` but netfilter is **not armed**
on the server — disabled via `KLANGKD_NETFILTER_ENABLED=false`, the hooks
dir unwritable, or the hook not installed/current (#1771) — the workspace
starts **unrestricted** and the server logs a loud warning. The
`allowed_domains` value is still persisted, so it takes effect the moment
netfilter is armed. The workspace's Settings panel and list row also badge
the gap (#1769), so the user who set the list sees it — not just operator
logs. This is deliberate: a misconfigured deploy degrades to the
unrestricted baseline rather than making workspaces unusable, but the
warning makes the gap visible.

## Common service domain lists

When building an `allowed_domains` list, you need to know which hosts and
ports a service requires. The tables below cover the most-requested
services; the same research pattern (check the provider's firewall /
proxy docs, then test) works for any service.

> **Note:** Hostnames are resolved to IPs at container-create time. If a
> service's IPs change (common with CDN-fronted domains), the container
> must be **restarted** to pick up the new addresses. See the
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
addition to git typically needs a broader set. Because klangk's
netfilter resolves at container-create time and doesn't support
wildcards, list the specific subdomains you use:

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
you use (klangk's netfilter doesn't support wildcards):

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

A self-hosted Ollama configured as `KLANGKD_LLM_BASE_URL` is proxied
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

- **DNS resolution at creation time — restart on IP change.** iptables
  matches IPs, so hostnames are resolved once when the container is
  created. If a service rotates IPs (common with CDNs like Fastly,
  CloudFront, and Cloudflare), access may break without warning.
  **Restart the workspace container** to re-resolve hostnames and
  update the iptables rules. Mitigation: allow a port without pinning a
  host, or allow a CIDR range (a possible future enhancement; the
  initial implementation is `host`/`host:port` only).
- **DNS is pinned to resolvers, not blocked entirely.** Outbound `:53` is
  accepted only to the nameservers in the container's `/etc/resolv.conf`,
  so a workspace cannot talk to an arbitrary host on port 53. This does
  **not** prevent DNS tunneling through those permitted resolvers to
  attacker-controlled domains (data can still be encoded in DNS queries).
  Treat the filter as an egress allow-list, not a complete anti-exfiltration
  guarantee against DNS-based channels.
- **Ruleset immutability depends on the runtime capability set.** The
  hook installs the iptables rules before the container entrypoint starts,
  and a filtered workspace also has `NET_ADMIN` dropped explicitly
  (`--cap-drop NET_ADMIN`). `NET_ADMIN` is already absent from podman's
  default capability set, so this is a no-op under defaults and defense
  in depth against an operator override. It is **not** a hard guarantee:
  running the workspace `--privileged`, adding `--cap-add NET_ADMIN`, or a
  permissive seccomp profile hands the entrypoint `iptables -F OUTPUT`,
  which flushes the ruleset and lets it exfiltrate freely. Do not run
  filtered workspaces privileged or grant `NET_ADMIN`.
- **`--hooks-dir` overrides podman's default hook dirs.** Podman's
  `--hooks-dir` flag _replaces_ (does not append to) the default OCI hook
  search paths, so passing only klangk's hooks dir for a filtered
  workspace would silently disable every _other_ `createContainer` hook
  an operator relies on (monitoring, secrets injection, GPU, corporate
  integrations). To avoid that, a filtered container passes klangk's hooks
  dir **and** the two standard default dirs
  (`/usr/share/containers/oci/hooks.d`, `/etc/containers/oci/hooks.d`),
  preserving operator hooks. Podman tolerates a dir that doesn't exist (it
  simply finds no hooks there). Limitation: a _non-standard_ hooks dir
  configured only via `containers.conf` is still clobbered by an explicit
  `--hooks-dir`; unrestricted workspaces are unaffected (the flag isn't
  passed). See #1770.
- **Port granularity.** The initial implementation supports `host` and
  `host:port`. CIDR ranges and port-only rules may follow.
- **`macOS` hosts.** The `createContainer` hook runs inside the
  container's Linux network namespace, never the macOS (XNU) kernel, so
  `iptables` availability is not host-dependent. For the DinD deployment
  there is no macOS-specific concern. For a native-on-mac deployment
  driving `podman machine`, ensure the `--hooks-dir` path and
  `klangk-netfilter.sh` are resolvable from inside the CoreOS VM.

## References

- [Podman maintainer discussion on OCI hooks for iptables][podman-disc]
- [Working OCI hooks + iptables implementation][jerabaul29]
- [OCI runtime spec — hooks][oci-hooks]

[podman-disc]: https://github.com/containers/podman/discussions/27099
[jerabaul29]: https://github.com/jerabaul29/2025_podman_iptable_rules
[oci-hooks]: https://github.com/opencontainers/runtime-spec/blob/main/config.md#posix-platform-hooks
