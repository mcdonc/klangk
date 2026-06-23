# Sandbox

`klangkc sandbox` creates and connects to a workspace using a
project-level config file. It's the single command for "get me into
this project" — create the workspace on the first run, reconnect on
subsequent runs.

`klangkc sandbox` is a quality-of-life feature of the
[`klangkc` CLI](../reference/cli.md) that combines several of
Klangk's individual features — workspace creation, bind mounts,
file copying, command execution, and shell access — into a single
step driven by a config file. Everything it does can be done manually
with `klangkc create`, `klangkc exec`, and `klangkc shell`, but the
sandbox command makes it easy to check a `.klangk-sandbox.yaml` into
your repo so you or your teammates can spin up an identical sandboxed
environment with one command.

This feature is most useful when run with the Klangk server on your own
machine. It requires the `klangkc` client program. It is not a feature of the
web UI.

## Quick start

Create a `.klangk-sandbox.yaml` in your project root:

```yaml
sandbox:
  mount-at: ~/myproj
```

Then run:

```bash
klangkc sandbox myworkspace
```

This creates a workspace named `myproj`, mounts the sandbox root
into the myworkspace container at `~/myproj`, and drops you into a shell.
Run the same command again to reconnect to the existing workspace.

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
```

| Field   | Required | Default              | Description                                                   |
| ------- | -------- | -------------------- | ------------------------------------------------------------- |
| `image` | no       | server default image | Container image. Must be in the server's allowed images list. |

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

The setup script runs once — on workspace creation, not on reconnect.
It runs as the `klangk` user inside the container. If
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

Then in your `.bashrc` or setup script:

```bash
[ -f ~/.env ] && . ~/.env
```

This way, changes to the secrets file on the host take effect on the
next shell session without recreating the workspace.

## Command reference

```text
klangkc sandbox WORKSPACE [PATH] [--forward-agent/-A] [--force-setup]
```

| Argument/Flag        | Default | Description                                                             |
| -------------------- | ------- | ----------------------------------------------------------------------- |
| `WORKSPACE`          |         | Workspace name (required).                                              |
| `PATH`               | `.`     | Path to the sandbox root (directory containing `.klangk-sandbox.yaml`). |
| `--forward-agent/-A` | `false` | Forward local SSH agent into the container.                             |
| `--force-setup`      | `false` | Re-run copy and setup steps even if the workspace exists.               |

### Behavior

**First run** (workspace doesn't exist):

1. Read `.klangk-sandbox.yaml` from the sandbox root
2. Create the workspace with the configured image, mounts, and
   volumes
3. Mount the sandbox root at `mount-at`
4. Copy files listed in `copy` into the container home
5. Run the `setup` script inside the container (if configured)
6. Connect to the workspace shell

**Subsequent runs** (workspace already exists):

1. Connect to the existing workspace shell

The copy and setup steps only run on first creation. On reconnect,
the command skips straight to the shell — it does not re-copy files
or re-run the setup script. This means:

- **Mounts** are always current (they're live links to host paths).
- **Copied files** reflect the state at creation time. To update
  them, delete the workspace and recreate it.
- **Setup script changes** are not re-applied automatically. Use
  `--force-setup` to re-run the copy and setup steps on an existing
  workspace.
- **Config changes** (new mounts, different image) require
  restarting (`klangkc restart myws`) or deleting and recreating
  the workspace. A warning is shown if the config has changed
  since creation.

## Setup scripts

The setup script runs inside the container as the `klangk` user. It
has access to everything that's been mounted and copied. The working
directory is the sandbox root (the `mount-at` path).

**SSH agent forwarding** is active during setup when `-A` /
`--forward-agent` is used. This means `git clone git@github.com:...`
and other SSH operations work in your setup script without any extra
configuration. SSH host key checking is set to `accept-new` during
setup (new hosts are automatically trusted on first connect).

**Important:** The `klangk` user does not have sudo access by
default. Without it, setup scripts are limited to user-space
operations (installing to `~`, downloading binaries, etc.). To
install system packages with `apt`, install nix, or modify system
files, the server administrator must set `KLANGK_ALLOW_SUDO=true`
in the server's `.env` file.

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

- **If your script is idempotent:** re-run with `--force-setup`:
  `klangkc sandbox myws --force-setup`
- **If not:** restart the container and try again:
  `klangkc restart myws && klangkc sandbox myws --force-setup`.
  Or delete the workspace entirely and start over:
  `klangkc rm myws && klangkc sandbox myws`.

### Tips

- **Make scripts idempotent.** Check if tools are already installed
  before installing them (e.g. `if ! command -v nix`). This makes
  `--force-setup` safe to use after interruptions. If your script
  isn't idempotent, an interrupted setup means you'll need to
  destroy the workspace and recreate it.
- **Use named volumes for large installs.** Mount `/nix` as a named
  volume so the nix store persists across workspace recreations.
- **Keep it fast.** The setup script blocks before you get a shell.
  Move slow one-time setup into a volume that persists.

## Example

See the [examples/](https://github.com/mcdonc/klangk/tree/main/examples)
directory in the klangk repo for working sandbox configurations.

A project that needs nix/devenv, custom dotfiles, a data directory,
and SSH access to GitHub:

```yaml
# .klangk-sandbox.yaml
sandbox:
  mount-at: ~/klangk
  setup: setup.sh

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

# Source nix in non-login shells too.
# shellcheck disable=SC2016
grep -q nix-profile ~/.bashrc 2>/dev/null \
  || echo '. "$HOME/.nix-profile/etc/profile.d/nix.sh"' >> ~/.bashrc

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
klangkc sandbox myproj -A
# First run: creates workspace, mounts everything, installs nix, drops into shell
# Subsequent runs: reconnects to existing workspace
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
