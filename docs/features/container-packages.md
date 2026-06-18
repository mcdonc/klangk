# Container Packages

Every workspace runs inside a container built from
`node:26-slim` (Debian). The image ships a curated set of packages
so common development tasks work out of the box.

## Language Runtimes

| Runtime        | Source                      | Notes                          |
| -------------- | --------------------------- | ------------------------------ |
| **Node.js 26** | Base image (`node:26-slim`) | Includes `npm`                 |
| **Python 3**   | `python3` system package    | `pip` and `venv` included      |
| **Bash**       | Default shell               | `/bin/sh` is symlinked to bash |

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
| `less`           | Pager                                      |
| `rsync`          | File synchronization                       |
| `file`           | File type detection                        |

## Networking / Debugging

- `openssh-client`
- `net-tools`, `iproute2`
- `iputils-ping`
- `telnet`
- `lsof`
- `strace`, `ltrace`
- `procps` (ps, top, free, etc.)

## AI Agents

- **Pi** (`@earendil-works/pi-coding-agent`) — terminal-based coding
  agent; see [AI Coding Harnesses](ai-coding-harnesses.md)
- **Claude Code** (`@anthropic-ai/claude-code`)
- **Herdr** — terminal agent runtime for persistent sessions and pane
  management

## Installing Additional Packages

By default the `klangk` user does **not** have root access.

### With sudo enabled

If the administrator sets `KLANGK_ALLOW_SUDO=1` (see
[Environment Variables](../reference/environment.md)), the `klangk`
user gets passwordless `sudo`. You can then install packages normally:

```bash
sudo apt-get update && sudo apt-get install -y <package>
```

### Without sudo

When sudo is disabled, you can still:

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
custom container image (see the `customize/` directory in the
repository root).
