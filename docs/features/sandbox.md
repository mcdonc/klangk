# Sandbox

`klangkc sandbox` creates and connects to a workspace using a
project-level config file. It's the single command for "get me into
this project" — create the workspace on the first run, reconnect on
subsequent runs.

Klangk's containerization system exists to let you safely work on
projects that use AI harnesses with wide permissions. The `sandbox`
command makes this easy: check a `.klangk/sandbox.yaml` into your
repo that describes what the workspace needs, and anyone on the team
can spin up an identical sandboxed environment with one command.

## Quick start

Create a `.klangk/sandbox.yaml` in your sandbox root:

```yaml
workspace:
  name: my-project
```

Then run:

```bash
klangkc sandbox
```

This creates a workspace named `my-project`, mounts the project
directory into the container at `~/work`, and drops you into a shell.
Run the same command again to reconnect to the existing workspace.

## Config file reference

The config file lives at `.klangk/sandbox.yaml` inside your project.
The directory containing `.klangk/` is called the **sandbox root** —
it's automatically mounted into the container.

### `workspace`

```yaml
workspace:
  name: klangk
  image: klangk-workspace
```

| Field   | Required | Default              | Description                                                   |
| ------- | -------- | -------------------- | ------------------------------------------------------------- |
| `name`  | no       | directory name       | Workspace name. Overrideable via `--name`.                    |
| `image` | no       | server default image | Container image. Must be in the server's allowed images list. |

### `sandbox`

```yaml
sandbox:
  mount_at: ~/klangk
  setup: .klangk/setup.sh
```

| Field      | Required | Default  | Description                                                                                                |
| ---------- | -------- | -------- | ---------------------------------------------------------------------------------------------------------- |
| `mount_at` | no       | `~/work` | Where the sandbox root is mounted inside the container. `~` expands to `/home/{handle}`.                   |
| `setup`    | no       | (none)   | Script to run inside the container after creation. Relative to `mount_at`, or absolute if starts with `/`. |

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
  `/home/{handle}`.
- **Options**: optional, comma-separated. Common options: `ro`
  (read-only), `rw` (read-write, default).

Relative source paths are resolved to absolute paths before being
sent to the server. The server validates all mount sources against
`KLANGK_ALLOWED_MOUNT_ROOTS` if that setting is configured.

The sandbox root mount (at `mount_at`) is implicit — you don't need
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
klangkc sandbox [PATH] [--name NAME] [--forward-agent/-A]
```

| Argument/Flag        | Default  | Description                                                 |
| -------------------- | -------- | ----------------------------------------------------------- |
| `PATH`               | `.`      | Path to the sandbox root (directory containing `.klangk/`). |
| `--name`             | (config) | Override the workspace name from the config file.           |
| `--forward-agent/-A` | `false`  | Forward local SSH agent into the container.                 |

### Behavior

**First run** (workspace doesn't exist):

1. Read `.klangk/sandbox.yaml` from the sandbox root
2. Create the workspace with the configured image, mounts, and
   volumes
3. Mount the sandbox root at `mount_at`
4. Copy files listed in `copy` into the container home
5. Run the `setup` script inside the container (if configured)
6. Connect to the workspace shell

**Subsequent runs** (workspace already exists):

1. Connect to the existing workspace shell

If you need to recreate the workspace (e.g., after changing the
config), delete it first with `klangkc rm` and run `sandbox` again.

## Setup scripts

The setup script runs inside the container as the `klangk` user. It
has access to everything that's been mounted and copied. The working
directory is the sandbox root (the `mount_at` path).

**Important:** The `klangk` user does not have sudo access by
default. Without it, setup scripts are limited to user-space
operations (installing to `~`, downloading binaries, etc.). To
install system packages with `apt`, install nix, or modify system
files, the server administrator must set `KLANGK_ALLOW_SUDO=true`
in the server's `.env` file.

### Example: install nix and devenv

```bash
#!/bin/bash
# .klangk/setup.sh
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

### Tips

- **Make scripts idempotent.** Check if tools are already installed
  before installing them. The script runs once on creation, but if
  you delete and recreate the workspace, it runs again.
- **Use named volumes for large installs.** Mount `/nix` as a named
  volume so the nix store persists across workspace recreations.
- **Keep it fast.** The setup script blocks before you get a shell.
  Move slow one-time setup into a volume that persists.

## Worked example

A project that needs nix/devenv, custom dotfiles, a data directory,
and SSH access to GitHub:

```yaml
# .klangk/sandbox.yaml
workspace:
  name: klangk
  image: klangk-workspace

sandbox:
  mount_at: ~/klangk
  setup: .klangk/setup.sh

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
# .klangk/setup.sh
set -euo pipefail

if ! command -v nix &>/dev/null; then
  curl -L https://nixos.org/nix/install | sh -s -- --no-daemon
fi

. ~/.nix-profile/etc/profile.d/nix.sh

if ! command -v devenv &>/dev/null; then
  nix profile install nixpkgs#devenv
fi
```

Usage:

```bash
cd ~/projects/klangk
klangkc sandbox -A
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
