<!-- markdownlint-disable MD013 -->

# Sandbox

`klangk sandbox` creates a workspace using a project-level config
file. It reads `.klangk-sandbox.yaml`, creates the workspace with the
configured image, mounts, and volumes, copies files, and runs the
setup script. Use `klangk shell` afterwards to connect.

`klangk sandbox` is a quality-of-life feature of the
[`klangk` CLI](../reference/cli.md) that combines several of
Klangk's individual features — workspace creation, bind mounts,
file copying, and command execution — into a single step driven by
a config file. Everything it does can be done manually with
`klangk create`, `klangk exec`, and `klangk shell`, but the
sandbox command makes it easy to check a `.klangk-sandbox.yaml` into
your repo so you or your teammates can spin up an identical sandboxed
environment with one command.

This feature is most useful when run with the Klangk server on your own
machine. It requires the `klangk` client program. It is not a feature of the
web UI.

> **Sandboxes vs. plugins.** Sandboxes are a _runtime_ feature: they
> install software and apply configuration scoped to a _particular
> user within a particular workspace_ — not to the workspace image as
> a whole — when the workspace is created, without rebuilding anything.
> By contrast, [plugins](plugins.md) are a _compile-time_ feature that
> bakes software into the workspace image at build time so it needn't
> be installed later — the features they add are available to any user
> in any workspace, but adding or changing a plugin requires rebuilding
> the Klangk image.

## Quick start

Create a `.klangk-sandbox.yaml` in your project root:

```yaml
sandbox:
  mount-at: ~/myproj
```

Then run:

```bash
klangk sandbox myworkspace
klangk shell myworkspace
```

The first command creates a workspace named `myworkspace`, mounts the
sandbox root into the container at `~/myproj`, and runs any setup
script. The second command connects you to an interactive shell.

Run `klangk sandbox myworkspace` again on an existing workspace and
it will error — pass `--force` to re-apply the config and re-run
setup.

## Config file reference

The config file lives at `.klangk-sandbox.yaml` inside your project.
The directory containing `.klangk-sandbox.yaml` is called the **sandbox root** —
it's automatically mounted into the container at the `mount-at`
location. If you do not specify a `mount-at` location, it will be
placed in `~/work`.

### `workspace`

```yaml
workspace:
  image: klangk-workspace
  service-command: openclaw gateway
  auto-start: true
  health-check: /openclaw/bin/healthcheck.sh
```

| Field             | Required | Default              | Description                                                                                                                                 |
| ----------------- | -------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `image`           | no       | server default image | Container image. Must be in the server's allowed images list.                                                                               |
| `service-command` | no       | (none)               | Command to run automatically as the agent identity in the Service terminal tab on first connect. See [Service Command](service-command.md). |
| `auto-start`      | no       | `false`              | Start the container automatically when the Klangk server starts. See [Auto-start](workspaces.md#auto-start).                                |
| `health-check`    | no       | (none)               | Shell command polled inside the container to gauge service health (exit 0 = healthy). See [Health Check](health-check.md).                  |

The workspace name is not in the config file — it's always
specified as a positional argument on the command line.

### `sandbox`

```yaml
sandbox:
  mount-at: ~/klangk
  setup: setup.sh
  setup-timeout: 300
```

| Field           | Required | Default  | Description                                                                                                |
| --------------- | -------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `mount-at`      | no       | `~/work` | Where the sandbox root is mounted inside the container. `~` expands to `/home/{handle}`.                   |
| `setup`         | no       | (none)   | Script to run inside the container after creation. Relative to `mount-at`, or absolute if starts with `/`. |
| `setup-timeout` | no       | `300`    | Maximum seconds the setup script may run before being killed. Set to `0` to disable.                       |

The setup script runs once — on workspace creation, not on
reconnect. It runs as the `klangk` user inside the container. If
`KLANGK_ALLOW_SUDO` is enabled on the server, the script can use
`sudo` for system-level setup (installing packages, etc.).

### `copy`

```yaml
copy:
  - ~/.gitconfig:~/.gitconfig
  - ~/.zshrc:~/.zshrc
```

Files copied from the host into the container home directory. Uses the
same `source:destination` format as mounts. Tilde on the left expands
to the host user's home; tilde on the right expands to the container
user's home (`/home/{handle}`).

Copies happen once during workspace creation, after the default home
skeleton is populated but before the setup script runs. The copied
files become independent of the host originals — changes inside the
container don't affect the host, and vice versa.

### `mounts`

```yaml
mounts:
  - /home/user/data:~/data
  - ~/.claude:~/.claude
  - ~/.ssh:~/.ssh:ro
  - ../sibling-repo:~/sibling-repo
```

Bind mounts from the host into the container. Format:
`source:destination` or `source:destination:options`.

- **Source**: host path. Absolute, or relative to the sandbox root.
  Tilde expands to the host user's home.
- **Destination**: container path. Tilde expands to
  `/home/{handle}`. Relative paths (no `~` or `/` prefix) are
  resolved relative to `mount-at`.
- **Options**: optional, comma-separated. Common options: `ro`
  (read-only), `rw` (read-write, default).

Relative source paths are resolved to absolute paths before being
sent to the server. The server validates all mount sources against
`KLANGK_ALLOWED_MOUNT_ROOTS` if that setting is configured.

The sandbox root mount (at `mount-at`) is implicit — you don't need
to list it here.

Use mounts for files that should stay in sync between host and
container: live dotfile directories (`~/.claude`, `~/.ssh`), sibling
repos, shared data directories.

### `volumes`

```yaml
volumes:
  - klangk-nix:/nix
  - klangk-cache:~/.cache
```

Named podman volumes. Format: `name:destination` or
`name:destination:options`. Volumes persist across container
recreations but aren't tied to a specific host directory. Use them
for caches, package stores, and other data that should survive
container rebuilds but doesn't need to be on the host filesystem.

### Secrets and environment variables

There is no dedicated `env` section. Instead, mount your `.env` file
into the container and source it from your shell or setup script:

```yaml
mounts:
  - .env:~/.env:ro
```

Then in your `~/.profile` (so the service command — running as the
agent — and `klangk exec` see the variables too; see
[The Shell](the-shell.md#startup-files)) or setup script:

```bash
[ -f ~/.env ] && . ~/.env
```

This way, changes to the secrets file on the host take effect on the
next shell session without recreating the workspace.

## Command reference

```text
klangk sandbox WORKSPACE [PATH] [--force]
```

| Argument/Flag | Default | Description                                                             |
| ------------- | ------- | ----------------------------------------------------------------------- |
| `WORKSPACE`   |         | Workspace name (required).                                              |
| `PATH`        | `.`     | Path to the sandbox root (directory containing `.klangk-sandbox.yaml`). |
| `--force`     | `false` | Re-apply config and re-run setup on an existing workspace.              |

### Behavior

**First run** (workspace doesn't exist):

1. Read `.klangk-sandbox.yaml` from the sandbox root
2. Create the workspace with the configured image, mounts, and
   volumes
3. Mount the sandbox root at `mount-at`
4. Copy files listed in `copy` into the container home
5. Run the `setup` script inside the container (if configured)
6. Print a message to run `klangk shell` to connect

**Subsequent runs** (workspace already exists):

- Without `--force`: error with a message to use `--force`
- With `--force`: re-apply config and re-run the copy and setup steps

### Connecting after sandbox

After `klangk sandbox` completes, connect with:

```bash
klangk shell myworkspace
```

To forward your SSH agent into the container:

```bash
klangk shell myworkspace -A
```

If the workspace has a `service-command` configured (e.g.
`openclaw gateway`), that command runs in the workspace's **Service**
terminal tab — as the agent identity, not in your own shell
(see [Where the service command runs](#where-the-service-command-runs-and-how-to-install-for-it)
above). To get an interactive shell alongside it, connect to a
named terminal window:

```bash
klangk shell myworkspace dev
```

This creates a new terminal window called `dev` where you can
work interactively while the service command continues running in
the first window.

The copy and setup steps only run during `klangk sandbox`. On
`klangk shell`, the command connects directly to the existing
workspace. This means:

- **Mounts** are always current (they're live links to host paths).
- **Copied files** reflect the state at creation time. To update
  them, delete the workspace and recreate it, or use `--force`.
- **Setup script changes** are not re-applied automatically. Use
  `--force` to re-run the copy and setup steps on an existing
  workspace.
- **Config changes** (new mounts, different image) require
  deleting and recreating the workspace.

## Setup scripts

The setup script runs inside the container as the `klangk` user. It
has access to everything that's been mounted and copied. The working
directory is the sandbox root (the `mount-at` path).

**Important:** The `klangk` user does not have sudo access by
default. Without it, setup scripts are limited to user-space
operations (installing to `~`, downloading binaries, etc.). To
install system packages with `apt`, install nix, or modify system
files, the server administrator must set `KLANGK_ALLOW_SUDO=true`
in the server's `.env` file.

### Where the service command runs (and how to install for it)

A `service-command` does **not** run in the workspace owner's shell.
It runs as the workspace's **agent** identity, in a dedicated
`service` tmux session whose `$HOME` is the agent's home
(`/home/clanker` by default, exposed as `$KLANGK_AGENT_HOME`) — not
the owner's. The owner interacts with it through the **Service**
terminal tab in the web UI.

This matters for setup scripts: anything the service command needs at
runtime — env exports in `~/.profile`, binaries installed under
`~/.local/bin`, config it reads from `$HOME` — must land in the
**agent's** home, because that's the home whose `~/.profile` the
service session sources. If you write to `~/.profile` while `$HOME`
is still the owner's home (the default when the setup script starts),
the service command will never see those exports.

The simplest fix is to repoint `HOME` at the agent home at the top of
your setup script. After that, every home-relative write in the
script — `~/.profile` appends, `~/.local/bin` links, `~/.pi` config —
lands in the agent's home, which is exactly where the service command
will look:

```bash
#!/bin/bash
set -euo pipefail

# Run the rest of setup as the agent identity: the service command
# runs in the agent's service session ($KLANGK_AGENT_HOME), so install
# everything the service command depends on into THAT home.
export HOME="${KLANGK_AGENT_HOME:-/home/clanker}"

# Now ~/.profile, ~/.local/bin, etc. resolve into the agent's home.
```

> The owner does **not** get tools installed by a sandbox on their own
> PATH. Sandbox-installed services are owned and operated by the agent
> through the Service tab — that is the supported way to manage them
> (e.g. `openclaw onboard`, restarting a gateway). Don't also write
> the same exports to the owner's `~/.profile`; it's not a consumer.

### Example: install nix and devenv

```bash
#!/bin/bash
# setup.sh
set -euo pipefail

# Install nix (requires KLANGK_ALLOW_SUDO=true on the server)
if ! command -v nix &>/dev/null; then
  curl -L https://nixos.org/nix/install | sh -s -- --no-daemon
fi

# Source nix
. ~/.nix-profile/etc/profile.d/nix.sh

# Install devenv
if ! command -v devenv &>/dev/null; then
  nix profile install nixpkgs#devenv
fi
```

### Interrupted setup

If the setup script is interrupted (Ctrl+C, network failure, etc.),
the workspace is left in an inconsistent state. To recover:

- **If your script is idempotent:** re-run with `--force`:
  `klangk sandbox myws --force`
- **If not:** restart the container and try again:
  `klangk restart myws && klangk sandbox myws --force`.
  Or delete the workspace entirely and start over:
  `klangk rm myws && klangk sandbox myws`.

### Tips

- **Make scripts idempotent.** Check if tools are already installed
  before installing them (e.g. `if ! command -v nix`). This makes
  `--force` safe to use after interruptions. If your script isn't
  idempotent, an interrupted setup means you'll need to destroy the
  workspace and recreate it.
- **Use named volumes for large installs.** Mount `/nix` as a named
  volume so the nix store persists across workspace recreations.
- **Keep it fast.** The setup script blocks before you can connect.
  Move slow one-time setup into a volume that persists.

## Example

Klangk ships working sandbox configurations, documented in
[Available Sandboxes](../sandboxes/index.md):

- **[OpenClaw](../sandboxes/openclaw.md)** — the OpenClaw assistant,
  pre-configured for the Klangk LLM proxy, with a `service-command` gateway
  and a `health-check`.
- **[Hermes](../sandboxes/hermes.md)** — the NousResearch Hermes Agent,
  installed per-workspace and routed through the Klangk LLM proxy. Hermes was
  previously a compile-time [plugin](plugins.md); it moved to a runtime
  sandbox so each workspace can configure it independently (and so its
  installer's `bash -i` PATH probe no longer needs an image-build bailout).

A project that needs nix/devenv, custom dotfiles, a data directory,
and SSH access to GitHub:

```yaml
# .klangk-sandbox.yaml
sandbox:
  mount-at: ~/klangk
  setup: setup.sh

workspace:
  service-command: openclaw gateway
  health-check: /openclaw/bin/healthcheck.sh

copy:
  - ~/.gitconfig:~/.gitconfig
  - ~/.zshrc:~/.zshrc

mounts:
  - ~/.claude:~/.claude
  - ~/.ssh:~/.ssh:ro
  - /home/chrism/data:~/data
  - .env:~/.env:ro

volumes:
  - klangk-nix:/nix
  - klangk-cache:~/.cache
```

And a setup script:

```bash
#!/bin/bash
# setup.sh
set -euo pipefail

# Run the rest of setup as the agent identity: the service command
# runs in the agent's service session ($KLANGK_AGENT_HOME), so install
# everything it depends on into THAT home. See
# "Where the service command runs" in sandbox.md.
export HOME="${KLANGK_AGENT_HOME:-/home/clanker}"

# Install nix (single-user, no daemon needed in containers).
if ! nix --version &>/dev/null; then
  rm -rf "$HOME/.local/state/nix" "$HOME/.nix-profile" \
         "$HOME/.nix-defexpr" "$HOME/.nix-channels"
  curl -L https://nixos.org/nix/install | sh -s -- --no-daemon
fi

# Add nix to PATH and enable flakes.
export PATH="$HOME/.nix-profile/bin:$PATH"
mkdir -p ~/.config/nix
grep -q experimental-features ~/.config/nix/nix.conf 2>/dev/null \
  || echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf

# Source nix in every login shell of the agent -- the service command
# (running in the service session) sources the agent's ~/.profile.
# Writing this to ~/.bashrc instead would hide it from non-interactive
# login shells (its interactivity guard returns early). (The health
# check is NOT a ~/.profile consumer -- it runs as a non-login bash -c;
# see health-check.md.) See the-shell.md#startup-files.
# shellcheck disable=SC2016
grep -q nix-profile ~/.profile 2>/dev/null \
  || echo '. "$HOME/.nix-profile/etc/profile.d/nix.sh"' >> ~/.profile

# Install devenv.
if ! command -v devenv &>/dev/null; then
  nix profile install \
    --extra-experimental-features "nix-command flakes" \
    --accept-flake-config "github:cachix/devenv/v2.1.2"
fi
```

Usage:

```bash
cd ~/projects/klangk
klangk sandbox myproj
klangk shell myproj -A
# First sandbox: creates workspace, mounts everything, installs nix
# shell: connects with SSH agent forwarding
# Subsequent sandbox calls: error unless --force
```

## Interaction with server settings

- **`KLANGK_ALLOWED_MOUNT_ROOTS`**: All bind mount sources are
  validated against this list. If your mounts are under `/home` and
  the server allows `/home`, it works. Named volumes bypass this
  check.
- **`KLANGK_IMAGE_NAME` / `KLANGK_ALLOWED_IMAGES`**: The `image`
  field must match one of the server's allowed images.
- **`KLANGK_ALLOW_SUDO`**: Must be enabled on the server for setup
  scripts that need `sudo` (e.g., installing system packages, nix).
