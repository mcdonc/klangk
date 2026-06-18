# klangkc

CLI client for [Klangk](https://github.com/mcdonc/klangk), a multi-user
containerized development environment.

## Installation

```bash
pip install klangkc
```

Requires Python 3.12+.

## Quick start

```bash
klangkc login admin@example.com        # authenticate
klangkc ls                             # list workspaces
klangkc create my-project              # create a workspace
klangkc shell my-project               # drop into a shell
```

## Features

- `shell` — interactive terminal session inside a workspace container
- `exec` — run a command in a container
- `sync` — sync files to/from a container
- `create` / `rm` / `edit` / `dup` — manage workspaces
- `export` / `import` — archive and restore workspaces
- `terminals` / `share` / `unshare` — shared terminal management
- `volumes` — manage named volumes
- `invite` / `invitations` — user invitation management

## Documentation

Full documentation: https://mcdonc.github.io/klangk/reference/cli/
