# Container Packages

Every workspace runs inside a container built from
`debian:trixie-slim` (Debian 13). The image ships a curated set of packages
so common development tasks work out of the box.

> **Note:** This page documents the default `klangk-workspace` image.
> If your deployment uses a custom container image, the available
> packages may differ.

## Language Runtimes

| Runtime        | Source                                 | Notes                              |
| -------------- | -------------------------------------- | ---------------------------------- |
| **Node.js 26** | NodeSource apt repo (`nodejs` package) | Includes `npm`; pinned to `26.7.0` |
| **Python 3**   | `python3` system package               | `pip` and `venv` included          |
| **Bash**       | Default shell                          | `/bin/sh` is symlinked to bash     |
| **Zsh**        | `zsh` system package                   | Available but not the default      |

## Build Tools

- `build-essential` — gcc, g++, make, libc headers
- `git`
- `curl`, `wget`
- `unzip`, `zip`, `xz-utils`

## Editors

- `vim`
- `nano`
- `emacs-nox`

## CLI Utilities

| Tool             | Description                                |
| ---------------- | ------------------------------------------ |
| `gh`             | GitHub CLI                                 |
| `jq`             | JSON processor                             |
| `sqlite3`        | SQLite shell                               |
| `ripgrep` (`rg`) | Fast recursive grep                        |
| `fd`             | Fast file finder (symlinked from `fdfind`) |
| `fzf`            | Fuzzy finder                               |
| `bat`            | `cat` with syntax highlighting             |
| `tree`           | Directory tree listing                     |
| `htop`           | Interactive process viewer                 |
| `tmux`           | Terminal multiplexer (backs terminal tabs) |
| `supervisor`     | Process manager (`supervisord`)            |
| `less`           | Pager                                      |
| `rsync`          | File synchronization                       |
| `file`           | File type detection                        |

## Networking / Debugging

- `openssh-client`
- `net-tools`, `iproute2`
- `iputils-ping` (installed for diagnostics, but `ping` cannot work: the
  workspace container never holds `CAP_NET_RAW` — #2347)
- `telnet`
- `lsof`
- `strace`, `ltrace`
- `procps` (ps, top, free, etc.)

## AI Agents

- **Pi** (`@earendil-works/pi-coding-agent`) — terminal-based coding
  agent; see [AI Coding Harnesses](ai-coding-harnesses.md)

## Process Supervisors

- **process-compose** (`/usr/local/bin/process-compose`) — standalone
  process supervisor for running a managed set of processes inside the
  workspace container. See the
  [process-compose docs](https://f1bonacc1.github.io/process-compose/).
- `supervisor` (supervisord) — Python-based supervisor, installed via apt
  in the base image.

## Installing Additional Packages

By default the `klangk` user has passwordless `sudo` (the deploy-wide
`KLANGKD_ALLOW_SUDO` defaults to on).

### With sudo enabled

With sudo on (the default), the `klangk` user can install packages
normally:

```bash
sudo apt-get update && sudo apt-get install -y <package>
```

A workspace can also be locked down individually below the deploy
default (sudo-disabled) even on a sudo-enabled server — the owner (or a
member with edit permission on the workspace) sets `allow_sudo: false` in
the workspace settings (`klangk edit --no-sudo`, or the _Allow sudo_
toggle in the workspace settings UI — which defaults to unchecked, so a
workspace created without touching the toggle is locked down; the
deploy-wide default still applies to workspaces created via the API with
no `allow_sudo` key, #3046). The lock-down applies at the next
container start.

### Without sudo

When sudo is disabled — the administrator sets `KLANGKD_ALLOW_SUDO=0`, or
the workspace is individually locked down (see below) — you can still:

- Install **Node packages** globally or locally with `npm install`
- Create a **Python virtual environment** and pip-install into it:

  ```bash
  python3 -m venv ~/.venv
  source ~/.venv/bin/activate
  pip install <package>
  ```

- Download standalone binaries into `~/bin` (which you can add to
  `$PATH` in your `~/.bashrc`)

Packages installed inside a running container are lost when the
container restarts. To make system-level packages permanent, build a
custom container image (see the [Customizing](../deployment/customizing.md)
guide).
