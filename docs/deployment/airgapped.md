# Air-Gapped Deployment

This chapter covers deploying the Klangk host container on a network with
no internet access — a disconnected, isolated, or "air-gapped" environment.
The runtime is already close to air-gap-ready (`image_pull_policy=never`,
local auth + JWT, in-container LLM router), but several defaults silently
assume internet reachability. The sections below document the delivery path
and the settings an operator must change.

## Offline image transport

The standard [Updating](docker.md#updating) procedure uses `docker pull`,
which is impossible on a disconnected network. Transport the host image
with `docker save` / `docker load` instead:

**On the connected machine:**

```bash
# Pull and export — pin the version tag so the exact build survives
# transport; export both the version tag and latest so either can be
# referenced on the target.
docker pull ghcr.io/mcdonc/klangk/klangk-host:v1.0
docker save ghcr.io/mcdonc/klangk/klangk-host:v1.0 \
  -o klangk-host-v1.0.tar
```

**On the air-gapped host:**

```bash
docker load -i klangk-host-v1.0.tar
# Verify the image and tag are present:
docker images ghcr.io/mcdonc/klangk/klangk-host
```

Podman equivalents (`podman save` / `podman load`) work identically.

## Package mirrors for workspaces

There is currently no built-in setting that injects pip/uv/npm/cargo
mirror configuration into workspaces. Without it, an agent that tries to
install packages from PyPI, npm, or any other public registry will fail on
a disconnected network.

### Workarounds

1. **Bake mirrors into a custom workspace image.** Build a workspace image
   that pre-configures `~/.config/pip/pip.conf`, `.npmrc`, etc. to point at
   internal mirrors. See [Customizing a Deployment](customizing.md) for the
   image-build procedure.

2. **Run internal mirrors and allow them via egress.** Stand up internal
   PyPI/npm mirrors on the isolated network and add their hostnames to
   `netfilter_default_domains` so workspaces can reach them while egress
   stays default-deny for everything else.

Both approaches require the operator to maintain the mirror infrastructure.

## DNS configuration

When no host resolver is detected, the network sidecar's proxy falls back
to forwarding DNS queries to `1.1.1.1` (Cloudflare) — unreachable on an
isolated network. The workspace's DNS resolution will silently hang.

**Always set `dns_servers` explicitly** in an air-gapped environment:

```yaml
dns_servers: "10.0.0.2,10.0.0.3"
dns_search: "corp.example.com"
```

These values are passed to workspace containers via `podman --dns` /
`--dns-search`. They apply to newly created containers and are reloadable
on SIGHUP.

## LLM provider base URLs

The LLM router's provider short names fill in public internet URLs when
the base URL is omitted:

| Provider       | Default base URL                        |
| -------------- | --------------------------------------- |
| `openai`       | `https://api.openai.com/v1`             |
| `anthropic`    | `https://api.anthropic.com/v1`          |
| `cohere`       | `https://api.cohere.ai/v1`              |
| `mistral`      | `https://api.mistral.ai/v1`             |
| `groq`         | `https://api.groq.com/openai/v1`        |
| `together_ai`  | `https://api.together.xyz/v1`           |
| `deepseek`     | `https://api.deepseek.com/v1`           |
| `fireworks_ai` | `https://api.fireworks.ai/inference/v1` |

On a disconnected network these URLs are unreachable — requests will hang
or time out with no error at configuration time.

**Always spell the full internal base URL** in `llm_models` entries:

```yaml
llm_models:
  - "openai/gpt-4o:https://llm.internal.corp/v1:sk-..."
```

The format is `provider/model:base_url:api_key`. Omitting the `base_url`
segment uses the table above, which is never correct on an air-gapped
network.

## Loading extra workspace images

`allowed_images` controls which workspace images users may select, and
`image_pull_policy: never` (the default) ensures podman never attempts a
registry pull. However, loading extra workspace images onto the host
requires manual steps:

1. On the connected machine, export the image:

   ```bash
   docker save my-custom-workspace:latest -o my-custom-workspace.tar
   ```

2. On the air-gapped host, load the image into the host container's
   podman:

   ```bash
   docker exec klangk podman load -i /path/to/my-custom-workspace.tar
   ```

3. Add the image name to `allowed_images`:

   ```yaml
   allowed_images: "klangk-workspace,my-custom-workspace"
   ```

**Important:** A host container rebuild (e.g. upgrading the klangk image)
loses hand-loaded images. Re-load them after every rebuild, or bake them
into a custom host image.

## Nix in workspaces

Nix workspaces assume access to `cache.nixos.org` for binary substituters.
There is no built-in offline knob or substituter configuration setting.

On a disconnected network, only these approaches are viable:

- Use the pre-built `klangk-workspace-nix` image, which ships with a
  pre-populated nix store. Packages already in the store work; anything
  not baked in will fail to fetch.
- Build a custom workspace image with internal substituters or mirrors
  pre-configured in the nix configuration.

The `nix_seed` setting pre-populates package metadata but does not address
substituter configuration or binary cache access.

## Settings checklist

A recommended `klangkd.yaml` block for an air-gapped environment:

```yaml
# --- LLM ---
# Always specify the full internal base URL (see "LLM provider base URLs")
llm_models:
  - "openai/gpt-4o:https://llm.internal.corp/v1:sk-..."

# --- DNS ---
# Internal resolvers — never rely on the 1.1.1.1 fallback
dns_servers: "10.0.0.2,10.0.0.3"
dns_search: "corp.example.com"

# --- Egress ---
# Allow internal mirrors, registries, and LLM endpoints so workspaces
# can reach them while egress stays default-deny for everything else
netfilter_default_domains:
  - "llm.internal.corp"
  - "pypi.internal.corp"
  - "npm.internal.corp"
  - "registry.internal.corp"

# --- Auth ---
auth_modes: "password"
jwt_secret: "<generate-a-strong-random-secret>"

# --- Images ---
# List any extra images that were manually loaded (see "Loading extra
# workspace images"); the default image_name is added automatically.
# image_pull_policy defaults to "never" — leave it.
allowed_images: "klangk-workspace,klangk-workspace-nix"

# --- Email ---
# Point at an internal relay, or disable invitations if there is no
# mail path on the isolated network
smtp_host: "smtp.internal.corp"
# disable_invites: true

# --- Hosting ---
# The hostname users type on the isolated network (plain HTTP, no TLS
# termination unless an internal CA is configured)
hosting_hostname: "klangk.internal.corp"
```

**Operational note:** JWT lifetimes and lockout windows assume the host
clock is accurate. Ensure NTP or another time-synchronization mechanism is
working on the isolated network — clock drift will cause token validation
failures and incorrect lockout behavior.
