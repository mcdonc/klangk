# CLI

`klangkc` is the command-line client for Klangk. It lets you manage
workspaces, connect to container shells, sync files, and administer
users — all from your terminal without needing the web UI. It also
supports [sandboxing projects](../features/sandbox.md) from a config
file and [SSH agent forwarding](../features/ssh-agent-forwarding.md)
for using your local SSH keys inside containers.

![klangkc --help](../assets/cli-help.png)

## Installation

Install `klangkc` from PyPI:

```bash
pip install klangkc
```

Requires Python 3.12+.

## Usage

```bash
klangkc login admin@example.com        # authenticate (prompts for password)
klangkc ls                               # list workspaces
klangkc create my-project                # create a workspace
klangkc create my-project --mount ~/src:/home/klangk/work/src          # with bind mount
klangkc create my-project --mount nix-store:/nix           # with named volume
klangkc create my-project --env FOO=bar                      # with env vars
klangkc edit my-project                  # interactive edit (name, image, command, mounts, env)
klangkc edit my-project --env FOO=bar    # set env var via flag
klangkc dup my-project my-copy           # duplicate a workspace
klangkc shell my-project                 # drop into bash inside the container
klangkc sandbox myws                     # create/reconnect from .klangk/sandbox.yaml
klangkc sandbox myws ~/projects/myapp    # specify sandbox root explicitly
klangkc sandbox myws --force-setup       # re-run copy and setup on existing workspace
klangkc exec my-project ls /home/klangk/work         # run a command in the container
klangkc sync ~/src my-project:/home/klangk/work      # sync files to/from the container
klangkc rm my-project                # delete a workspace
klangkc restart my-project           # restart the container for a workspace (owner only)
klangkc export my-project            # export workspace to my-project.tar.gz (admin only)
klangkc export my-project -o bak.tar.gz  # export to specific file
klangkc import bak.tar.gz            # import workspace from archive
klangkc import bak.tar.gz --name new-name  # import with a different name
klangkc terminals my-project         # list all terminals (own + shared)
klangkc share my-project bash        # share a terminal with workspace members
klangkc unshare my-project bash      # stop sharing a terminal
klangkc invite user@example.com      # send an invitation email (admin only)
klangkc invitations                  # list all invitations (admin only)
klangkc images                       # list available container images
klangkc volumes ls                   # list your podman volumes
klangkc volumes create nix-store     # create a named volume (owned by you)
klangkc volumes rm nix-store         # delete a volume (must be yours)
```

The CLI connects to the running Klangk backend over HTTP + WebSocket — it works locally and against remote servers.

## Exiting the shell

To disconnect from `klangkc shell`, use the SSH-style escape sequence:
**Enter**, **~**, **.** (three keystrokes in sequence).

1. Press **Enter** to make sure you're at the beginning of a new line.
   The escape sequence is only recognized immediately after a newline.
2. Press **~** (tilde). Nothing visible happens yet — the CLI is
   waiting to see if the next character completes the escape.
3. Press **.** (period). The connection closes immediately and you're
   returned to your local shell.

If you type **~** and then any key other than **.**, the tilde and
that key are both sent to the remote shell as normal input. This
means **~** only has special meaning right after Enter — you can use
tildes freely in commands and text without triggering the escape.

> **Note:** Closing your terminal window or pressing **Ctrl+C** will
> also end the session, but the escape sequence is the clean way to
> disconnect without interrupting a running process inside the
> container.

## Terminal behavior differences

`klangkc shell` provides the same tmux-based terminal as the web frontend, but clipboard behavior differs:

- **Web frontend**: Text selections auto-copy to the system clipboard via the browser bridge. Mouse wheel scrolls through scrollback. No extra setup needed.
- **CLI (`klangkc shell`)**: Text selections auto-copy to the system clipboard via [OSC 52](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h3-Operating-System-Commands), which requires your terminal emulator to support it. Mouse wheel scrollback works. Native text selection (viewport-only) is available via **Shift+drag**.

### OSC 52 terminal support

The following terminal emulators support OSC 52 clipboard integration (auto-copy from tmux selections will work):

| Terminal         | OSC 52 support |
| ---------------- | -------------- |
| iTerm2           | Yes            |
| kitty            | Yes            |
| alacritty        | Yes            |
| WezTerm          | Yes            |
| foot             | Yes            |
| Windows Terminal | Yes            |
| Konsole          | Yes (22.04+)   |
| xterm            | Yes            |
| GNOME Terminal   | No             |
| Tilix            | No             |
| MATE Terminal    | No             |
| Terminator       | No             |

If your terminal does not support OSC 52, tmux selections will still be captured in the tmux paste buffer but will not automatically appear on your system clipboard. Consider switching to a terminal emulator that supports OSC 52 for the best `klangkc shell` experience.
