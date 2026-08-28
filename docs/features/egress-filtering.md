# Per-workspace network egress filtering

Klangk can restrict which external hosts a workspace container may reach,
so a deployment running AI agents or untrusted code isn't an open
exfiltration vector. Each workspace picks its posture with an **egress
mode** (`egress_mode`, default `interactive` for new workspaces):

- **`interactive`** — start closed and consent as the workspace works:
  each first-time destination is **held** for a human allow/deny decision
  (Little-Snitch style), with `allowed_domains` entries acting as
  pre-approvals that skip the prompt. See
  [Interactive egress consent](#interactive-egress-consent).
- **`static`** — the classic allow-list: declare `allowed_domains` up
  front; everything else is denied and recorded for audit. No prompting,
  ever.
- **`allow`** — default-permit with a deny-list: every host is reachable
  except names in `rejected_domains`; off-list egress is recorded and
  auto-allowed with no prompt (#2406).

A `static` workspace with no `allowed_domains` (and no deploy-wide
default) keeps unrestricted outbound networking exactly as before; an
`interactive` workspace is always filtered, even with empty lists
(#2325).

The mechanism uses a **network sidecar** — a small NET_ADMIN container
that shares the filtered workspace's network namespace and owns its
egress ruleset. Every filtered workspace (interactive mode, or any mode
declaring `allowed_domains`/`rejected_domains`) runs behind the sidecar,
which default-denies outbound traffic and allow-lists only the approved
destinations (resolved at runtime by a DNS proxy, so DNS round-robin is
handled).

A step-by-step diagram of a single egressing request — DNS gate, SYN
gate at NFQUEUE, the consent loop, and the verdict — is in
[Anatomy of an egressing request](../architecture/egress-flow.md).

## How it works

1. A workspace carries an `allowed_domains` list (`host`, `host:port`,
   `.host`/`*.domain` wildcards, or IPv4 CIDR specs — see the [API](#api)
   section for the full grammar).
2. On container start, if the workspace is filtered (`egress_mode` is
   `interactive`, or any mode declares `allowed_domains`/
   `rejected_domains`), the backend starts a **network sidecar** container
   (`klangk-net-<ws-id>`,
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
6. In interactive mode the ruleset additionally queues every non-approved
   outbound TCP SYN to the sidecar's own NFQUEUE, where it is **held
   pending a consent verdict** — the full loop is in
   [Interactive egress consent](#interactive-egress-consent). The queue is
   rate-limited (`--limit 5/sec --limit-burst 20`); SYNs past the limit
   are REJECTed (fast refusal) rather than queued.

The ruleset is in place before the workspace process starts, and the
workspace lacks `CAP_NET_ADMIN` so it cannot flush the ruleset.

## Consent recording (all modes)

Every egress decision that reaches klangkd — **denied _and_ allowed** —
is recorded to the `egress_consent` table, regardless of `egress_mode`
(#2242, #2304 — auditing needs no opt-in setting):

- **static** -> recorded `denied` immediately, `decided_by` NULL (policy,
  no human). Static mode is strictly better than silent deny: every
  denied attempt is logged for audit/review (`scripts/consent-watch.py`
  shows a live view on the server host). One row per (workspace, host,
  port); repeats don't spam.
- **interactive** -> recorded `pending`, then decided by a human (below),
  or auto-expired (`expired` — deliberately distinct from a human deny)
  when no decider answers within `egress_consent_timeout`.
- **allow** -> off-list destinations recorded `allowed`, `decided_by`
  NULL (#2406) — a log of everything the workspace actually reached.

The DNS layer itself is audited too (#2304): the sidecar's DNS proxy —
which sees every FQDN egress attempt, allowed or not — reports each
outcome it decides over its consent WebSocket, recorded as one
`decided_by`-NULL policy row per host: `allowed` for every allow-listed
(or in-session-consented) resolution that resolves + learns, `denied`
for every reject-listed name NXDOMAIN'd. A paused-mode auto-allow with
no prior verdict is recorded the same way (a replayed in-effect verdict
is not — its human row already exists). Interactive off-list queries
report nothing at the DNS layer — their decision point is the connection
SYN, whose human or policy verdict is already recorded above.

The audit frames are best-effort and session-bounded: one frame per
host per WebSocket session on the sidecar, capped at 4096 hosts (past
the cap the sidecar logs once and stops reporting new hosts until the
WS reconnects), and a lost frame re-reports on that host's next
resolution. Per-connection decisions that never reach klangkd —
session-host and cached-verdict auto-allows at NFQUEUE, and WS-outage
fail-close denies — are not recorded (per-connection flow audit is the
follow-up scope). The rows are bounded by the same retention/cap sweep
as everything else.

The table is bounded (#2303): a retention window
(`KLANGKD_EGRESS_CONSENT_RETENTION_DAYS`, default 30 days) deletes
terminal rows older than it, and a per-workspace cap
(`KLANGKD_EGRESS_CONSENT_ROW_CAP`, default 2000) trims the oldest rows
when a workspace floods decided requests past the cap. Verdicts still
in effect (`forever`, `tilrestart`, or a timed window not yet elapsed)
are enforcement state and are never pruned — they leave via workspace
deletion or the `tilrestart` reap. The consent monitor sweeps at
startup and then hourly on a wall-clock deadline (event traffic never
postpones it); both settings are SIGHUP-reloadable — a reload applies on
the next sweep.

## Interactive egress consent

Instead of declaring the full allow-list up front, an interactive
workspace **starts closed and prompts on each new destination**: the
first connection to a host nobody has approved yet is held mid-`connect()`
and surfaces as a consent request a human can allow or deny for a chosen
duration. The allow-list is built interactively as the workspace actually
reaches out; `allowed_domains` entries (and a deploy-wide default) act as
pre-approvals that skip the prompt (#2325). Requests are per-workspace —
there is no per-process attribution.

### A workspace is interactive only while a decider is connected

`egress_mode = "interactive"` is the opt-in, but interactivity is
**runtime state** (#2308): a workspace's off-list egress is actually held
only while **at least one live consent decider** is connected for it (or
deploy-wide). A decider is a connected client — the `klangk
consent-decide` TUI, the consent popup inside `klangk shell`, or the web
UI — and its WebSocket connection _is_ the registration; liveness is
driven by client pings, and a decider silent for
`KLANGKD_CONSENT_DECIDER_TIMEOUT` (default 45s) is reaped. The moment the
last decider disconnects, the workspace reverts to deny-and-record: with
nobody to ask, off-list connections fail fast (an immediate TCP refusal,
not a hang) and are recorded.

A `static` workspace refuses decider registration outright, so the
static/interactive boundary is structural, not just behavioral (#2394).

### How a hold works

1. The sidecar's DNS proxy resolves every query. Allow-listed names (and
   names covered by an in-effect verdict) resolve and have their IPs
   learned as usual; `rejected_domains` names get NXDOMAIN. Any **other**
   name resolves normally, but the proxy records the IP-to-name mapping
   and installs no allow rule (#2324) — resolution succeeding does _not_
   mean egress is permitted.
2. The first packet (the TCP SYN) to a non-approved IP is queued to the
   sidecar's NFQUEUE. The connection now **stalls inside `connect()`** —
   that stall is the hold, and the decision window it affords the human
   is the kernel's connect timeout (~127s).
3. The sidecar relays the destination to klangkd over its
   `/ws/egress-sidecar` WebSocket — the hostname, when DNS taught it one —
   and klangkd persists a pending request and fans it out to every live
   decider as an `egress_request` frame.
4. The first verdict (or the timeout) resolves the hold. **Allow**
   accepts the SYN and learns the IP for the verdict's duration;
   **deny / timeout / error** forges a TCP RST so `connect()` fails at
   once (`ECONNREFUSED`), with a short-lived REJECT rule as backstop.

The hold is bounded server-side by `KLANGKD_EGRESS_CONSENT_TIMEOUT`
(default 120s, sized to the kernel's connect timeout): a request no
decider answers expires to a deny. If the sidecar's WebSocket to klangkd
is down, off-list connects fail fast rather than hang. Nothing is ever
left pending forever.

### Deciding

- **`klangk consent-decide <workspace>`** — a standalone TUI decider
  (#2310): a live queue of held requests (host:port and a countdown),
  allow/deny per row with the duration chosen at the action — bare
  `a`/`d` (or the row buttons) send the default `tilrestart`, `A`/`D`
  open a duration picker first (#2511) — a rules screen (`r`) with revoke
  (`x`) (#2340, #2341), and pause controls (#2332).
- **`klangk shell`** — shelling into an interactive workspace wraps the
  shell in a local tmux that floats the decider over it as a popup: a
  held request pops up without leaving the shell (`C-a p` reopens it;
  skip the wrapper with `--no-consent-popup`) (#2383). Disconnect with
  the SSH-style escape — press **Enter**, then **~**, then **.** — after
  which the CLI prints `Disconnected from <workspace>.` and the wrapper
  cleans up; the `[exited]` line that may follow is tmux confirming the
  outer session ended, not an error.
- **Web UI** — the workspace page shows a consent banner with per-row
  allow/deny split buttons: a bare click uses the default duration
  (until restart), and the attached ▾ menu sends the verdict with any
  other duration (#2246, #2499), plus a **Network** tab
  listing the in-effect rules with revoke actions.
- **Deploy-wide** — an admin may connect a decider without a workspace
  scope (via the `/ws/consent-decider` WebSocket) to decide for every
  interactive workspace on the deploy. It receives newly-created holds
  live, but no replay of holds that predate its connection (those simply
  time out).

Several deciders may be connected at once (two CLI sessions and the web
UI, say): each pending request is fanned out to all of them and the first
decision wins (#2244).

**Authorization.** A workspace-scoped decider needs `terminal` access to
the workspace (owner, member, or spectator); a deploy-wide decider needs
admin. A verdict can only decide a request inside the decider's own
workspace. Pausing prompting (below) additionally requires
`share-terminals` (owner or collaborator).

### Decision durations

A verdict carries a duration, chosen with the allow/deny action (default
`tilrestart`, #2328): bare `a`/`d` in the TUI — or a plain button click
on the web banner — send the default; the TUI's `A`/`D` picker (#2511)
chooses any other duration in the same step.

| Duration                          | Meaning                                                                                                                                                                                                                         |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `once`                            | This one connection only; the next connection to the same host:port prompts again (#2361).                                                                                                                                      |
| `5m` / `15m` / `1h` / `1d` / `1w` | Timed: allowed (or denied) for the window, then the destination re-prompts.                                                                                                                                                     |
| `tilrestart`                      | The workspace container's lifetime — the sidecar's in-memory rules; cleared on restart (#2346).                                                                                                                                 |
| `forever`                         | The workspace's lifetime: an **allow** is appended to `allowed_domains` as `host:port` (#2368); a **deny** to `rejected_domains` (#2369). The sidecar re-reads both lists on start, so the verdict survives container restarts. |

A timed or `forever` verdict is **host-scoped, not IP-scoped**: every IP
the host resolves to — including CDN rotations — is covered for the
duration, so a user is not re-prompted for a domain they already decided
(#2372, #2446). Decisions persist to the workspace only; there is
currently no promotion of a verdict to a deploy-wide list.

### Pause

A decider can pause prompting workspace-wide for 15m / 1h / 1d (#2332):
while paused, off-list egress is auto-allowed per-connection with no
prompt and no hold — a relief valve for a build or crawl that would
otherwise flood the queue. A recorded deny still blocks while paused.
The pause self-expires; the decider shows the remaining window.

### Rules view, revoke, audit

Every request and verdict is recorded in the `egress_consent` table:
destination (host, port), `requested_at`, the `decision`, its `duration`,
`decided_at`/`decided_by` (the deciding user; NULL means policy decided,
not a person), and — for a revoke — `revoked_at`/`revoked_by`. A timeout
is recorded `expired`, distinguishable from a human deny.

The rules view (the `r` screen of `consent-decide`, the web UI's Net
Rules tab) shows this live over the `egress_rules` stream (#2338): the
static allow-list and reject-list, every in-effect verdict with its
remaining window, and the pause state. An in-effect verdict can be
**revoked** from there (#2339, #2341): the sidecar drops its rule at
once, the row is marked revoked, and a `forever` verdict's durable list
entry is retracted so it does not re-apply on the next restart (#2370).
`scripts/consent-watch.py` renders the raw table live on the server host.

### Operator notes

- **No host-side setup.** Interactive mode needs no kernel logging, no
  `/dev/kmsg` access, no host iptables and no `nsenter`: the sidecar
  consumes its own NFQUEUE inside the network namespace it owns, and
  klangkd coordinates everything over authenticated WebSockets. (An
  earlier design tailed kernel LOG lines; NFQUEUE replaced it — blocked
  packets generate no kernel log volume at all.)
- **Settings** (all SIGHUP-reloadable): `KLANGKD_EGRESS_CONSENT_TIMEOUT`
  (default 120s — how long a request stays pending before auto-deny),
  `KLANGKD_EGRESS_CONSENT_RATE_LIMIT` (default 50 pending requests per
  workspace — at the cap, new holds are denied at once),
  `KLANGKD_CONSENT_DECIDER_TIMEOUT` (default 45s — decider liveness; a
  crashed or half-open decider is reaped within roughly 1.5x that,
  reverting its workspaces to deny-and-record), and the table-bounding
  pair `KLANGKD_EGRESS_CONSENT_RETENTION_DAYS` (default 30) /
  `KLANGKD_EGRESS_CONSENT_ROW_CAP` (default 2000) — see
  [Consent recording](#consent-recording-all-modes); a reload applies on
  the next sweep.
- **Flood bounds.** The consent queue is rate-limited in the ruleset
  (5 SYNs/sec, burst 20) — packets past the limit are REJECTed, never
  queued — and request rows are deduplicated per destination, so a
  flooding workload cannot overwhelm the decider or spam the table.
- **klangkd restart** denies every in-flight hold and expires orphaned
  pending rows at startup; a **sidecar-to-klangkd outage** makes off-list
  connects fail fast. Both are fail-closed.
- **Idle timeout.** Egress activity (DNS queries, consented SYNs, byte
  counts) bumps the workspace idle timer, so an egress-only workload is
  not reaped mid-transfer (#2479, #2485).
- **`egress_mode` changes apply at the next container start** (#2409).
- **`klangk sandbox`** runs its automated install in `allow` mode (no
  decider is watching), then resets the workspace to `interactive`
  (#2404).

### Security model

- The workspace container stays unprivileged: no `NET_ADMIN`, no
  `NET_RAW`. It cannot flush the ruleset, cannot forge the packet mark
  that exempts the DNS proxy's upstream forwards, and cannot reach the
  sidecar's queue or control socket.
- All enforcement — the default-DROP policy, the DNS redirect, the
  consent queue — lives in the sidecar's own network namespace, installed
  by the sidecar itself. Nothing egress-related executes on the host or
  in a privileged context beyond the sidecar's existing `NET_ADMIN`.
- **Authentication.** The sidecar authenticates to klangkd with the
  workspace's own JWT (bind-mounted read-only and re-read on rotation);
  the token is workspace-scoped, so a workspace cannot forge events for
  another workspace. Deciders authenticate with a user JWT and are
  authorization-checked per scope (terminal access / admin).
- **Fail-closed throughout.** No decider -> fast deny. Decider too slow
  -> timeout deny. klangkd restart -> all holds denied. Sidecar link
  down -> fast deny. Queue overflow -> reject. Internal error -> deny.
  No code path silently allows.
- The [DNS caveats](#caveats) below still apply: this is an egress
  allow-list, not a complete anti-exfiltration guarantee (DNS tunneling
  through the proxy remains possible).

## Enabling it (operator)

Egress filtering is **available by default**: `network_sidecar_image`
ships with a default (`klangk-network-sidecar`), so a filtered
workspace — one declaring `allowed_domains`/`rejected_domains`, or in
`interactive` mode — is filtered out of the box; no configuration is
required for the common case.

1. Make the sidecar image available to podman:
   - **All-in-one host image** (`scripts/build-host-image.sh`): nothing to
     do — the sidecar image is embedded as a tarball in the host image and
     `podman load`ed on first startup (#2301).
   - **Dev**: the `klangk:build-network-sidecar` devenv task builds it from
     `src/containers/network/`.
   - **Other deploys**: publish the image to your registry and point
     `KLANGKD_NETWORK_SIDECAR_IMAGE` at it if you don't use the default name.
2. Restart klangkd. A filtered workspace (`egress_mode: interactive`, or
   one declaring `allowed_domains`/`rejected_domains`) now starts behind
   the sidecar.

To **disable** egress filtering entirely, set
`KLANGKD_NETFILTER_ENABLED=false` (or YAML `netfilter_enabled: false`).
When disabled, a filtered workspace (`egress_mode: interactive`, or one
declaring `allowed_domains`/`rejected_domains`) **fails to
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

Entries use the same grammar as a workspace allow-list (`host` /
`.domain` / `*.domain` / `host:port` / IPv4 CIDR — see the [API](#api)
section; #1935, #2377) and are validated server-side at startup.
A malformed value logs a warning and falls back to "no default" rather
than aborting the server (#1772). Read at boot and on SIGHUP
(reloadable).

**Application.** The default is a **pre-fill, not a server-side
merge**: the browser's create-workspace dialog (and any client reading
it from the authenticated `/config` payload) starts its Netfilter list
from this default, and the workspace persists whatever the creator
submits. A workspace's own `allowed_domains` — pre-filled or hand-
written — is exactly what is enforced; an empty list in `interactive`
mode means "prompt for everything". (The TUI create form does not
pre-fill yet — #1931.)

## Configuring a workspace

Set `allowed_domains` via the workspace **Settings** panel (an
"Allowed Domains" list editor under Mounts / Environment Variables), the
CLI, or the API. The **egress mode** is set alongside it: an "Egress
mode" selector in the Netfilter pane of the `klangk create` / `klangk
edit` TUI forms (`interactive (ask first)` / `static (deny + record)` /
`allow (default-permit)`), the same selector in the browser's
create-workspace dialog and settings panel, or `"egress_mode"` in the
API body below. There is no flags-mode CLI option for it — use the
interactive form or the API. A mode change takes effect at the next
container start (#2409).

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
  -d '{"allowed_domains": ["github.com:443", "pypi.org", "registry.npmjs.org"],
       "egress_mode": "interactive"}'
```

`egress_mode` is `"static"`, `"interactive"` (the default for new
workspaces), or `"allow"` (#2239, #2406); like `allowed_domains`, it
applies at the next container start.

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
- An empty list (or `null`) means "no pre-approvals": in `interactive`
  mode every new destination prompts; in `static` mode the workspace is
  **unrestricted** (nothing to filter on); in `allow` mode everything
  except `rejected_domains` is permitted. The deploy-wide default
  (`KLANGKD_NETFILTER_DEFAULT_DOMAINS`) is only a create-dialog pre-fill
  — see above.

A restart of the workspace container applies the change to a running
workspace (the ruleset is set at create time).

## Fail-closed behavior

If a workspace is filtered — it declares `allowed_domains`/
`rejected_domains`, **or** its `egress_mode` is `interactive` — but
egress filtering is **not available** on the server (disabled via
`KLANGKD_NETFILTER_ENABLED=false`, or the sidecar image unset/cleared),
the workspace **refuses to start** rather than running unrestricted.
Silently ignoring an allow-list (or the interactive opt-in) would
disable a security control the user explicitly requested (#2254 review
B2, #2325). The settings are still persisted, so they take effect the
moment filtering is re-enabled.

An `allow`-mode workspace with no lists degrades to unrestricted
instead of failing to start — it asked for permissiveness, not
lockdown (#2406). A `static` workspace **without** lists always starts
unrestricted, regardless of the filtering setting.

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
addition to git typically needs a broader set. Because host matching is
exact by default (#2377), use `.github.com`-style leading-dot entries
(apex + subdomains) or list the specific subdomains you use:

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

A domain without a port (e.g. `github.com`) allows **all** ports to that
exact host (and `.github.com` all ports to it and its subdomains). This
is convenient for quick iteration but less restrictive than pinning
individual ports.

[gh-ssh-443]: https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port
[gh-firewall]: https://docs.github.com/en/get-started/using-github/allowing-access-to-githubs-services-from-a-restricted-network
[gh-meta]: https://api.github.com/meta

### Deploy-wide default: all common services

The following `klangkd.yaml` snippet sets the deploy-wide default to
the union of every service listed above. Workspaces created from the
browser's create dialog (which pre-fills its Netfilter list with this
default) start with all of them as pre-approvals; the creator trims the
list from there.

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
  workspace that genuinely needs IPv6 egress cannot use the filter —
  clear `allowed_domains` (and the deploy-wide default) and set
  `egress_mode` to `static` or `allow` to run it unrestricted.
- **`0.0.0.0/0` matches all IPv4 — don't use it to "disable" the
  filter.** A `/0` CIDR (e.g. `0.0.0.0/0`) is a valid spec but the
  ACCEPT rule it emits matches the entire IPv4 space, so it effectively
  runs the workspace unrestricted while looking like a real rule. The
  server logs a loud warning whenever a `/0` CIDR appears in an allow-list
  (workspace or deploy default). If you genuinely want unrestricted
  egress, set `egress_mode` to `static` or `allow` with an empty list (or
  set `KLANGKD_NETFILTER_ENABLED=false`) — those are the documented,
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
- **Interactive mode denies off-list egress with no decider connected.**
  Interactivity is runtime state (#2308): headless or agent-driven
  workspaces (auto-started services, CI jobs) that must egress
  unattended need a connected decider — or `static`/`allow` mode.
- **A held connection blocks until decided (or it times out).** The hold
  window is `KLANGKD_EGRESS_CONSENT_TIMEOUT` (default 120s, sized to the
  kernel's connect timeout); tools with their own shorter connect
  timeouts may give up before a human answers.
- **Off-list names resolve in interactive mode.** The consent gate is the
  connection SYN, not the DNS query (#2324), so a successful
  `getaddrinfo` does not mean the connection is permitted.
- **Only `forever` verdicts survive a restart.** `tilrestart` and timed
  verdicts live in the sidecar's in-memory rules and die with the
  container; `once` is per-connection by definition (#2346).
- **Pausing consent auto-allows.** While a pause window (#2332) is open,
  every destination without a recorded deny is permitted
  per-connection — treat a pause as a temporary hole, not a hardening
  step.

## References

- [FQDN egress via a DNS-proxy sidecar][fqdn-sidecar]
- [Interactive (Little-Snitch-style) egress consent][interactive-epic]

[fqdn-sidecar]: https://github.com/mcdonc/klangk/issues/2250
[interactive-epic]: https://github.com/mcdonc/klangk/issues/2239
