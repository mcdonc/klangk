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

Sudo is **off by default** for every workspace (#3046/#3047): the
stored per-workspace `allow_sudo` setting is the sole posture source,
and an absent key means locked down. The deploy-wide
`KLANGKD_ALLOW_SUDO` flag is only a ceiling — it permits the _Allow
sudo_ toggle to be checked but grants nothing by itself.

### With sudo enabled

Opt the workspace in (check _Allow sudo_ in the UI, or
`klangk create --sudo` / `klangk edit --sudo`) and the `klangk` user
gets passwordless `sudo` (while the deploy ceiling allows it):

```bash
sudo apt-get update && sudo apt-get install -y <package>
```

The opt-in stores `allow_sudo: true` in the workspace settings; a later
lock-down (`allow_sudo: false`, or just removing the opt-in) applies at
the next container start. A workspace `true` can never grant sudo on a
deploy where `KLANGKD_ALLOW_SUDO` is off.

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
