# CLI

Klangk provides a CLI for terminal-based access to the same containers:

```bash
klangkc login admin@example.com        # authenticate (prompts for password)
klangkc ls                               # list workspaces
klangkc create my-project                # create a workspace
klangkc create my-project --mount ~/src:/home/klangk/work/src          # with bind mount
klangkc create my-project --mount nix-store:/nix           # with named volume
klangkc create my-project --env KLANGK_SKILLS=stats,rdkit    # with env vars
klangkc edit my-project                  # interactive edit (name, image, command, mounts, env)
klangkc edit my-project --env FOO=bar    # set env var via flag
klangkc dup my-project my-copy           # duplicate a workspace
klangkc shell my-project                 # drop into bash inside the container
klangkc exec my-project ls /home/klangk/work         # run a command in the container
klangkc sync ~/src my-project:/home/klangk/work      # sync files to/from the container
klangkc rm my-project                # delete a workspace
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
