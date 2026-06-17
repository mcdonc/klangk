# CLI

Klangk provides a CLI for terminal-based access to the same containers:

```bash
klangk login admin@example.com        # authenticate (prompts for password)
klangk list                             # list workspaces
klangk create my-project                # create a workspace
klangk create my-project --mount ~/src:/home/klangk/work/src          # with bind mount
klangk create my-project --mount nix-store:/nix           # with named volume
klangk create my-project --env KLANGK_SKILLS=stats,rdkit    # with env vars
klangk edit my-project                  # interactive edit (name, image, command, mounts, env)
klangk edit my-project --env FOO=bar    # set env var via flag
klangk dup my-project my-copy           # duplicate a workspace
klangk shell my-project                 # drop into bash inside the container
klangk exec my-project ls /home/klangk/work         # run a command in the container
klangk sync ~/src my-project:/home/klangk/work      # sync files to/from the container
klangk rm my-project                # delete a workspace
klangk export my-project            # export workspace to my-project.tar.gz (admin only)
klangk export my-project -o bak.tar.gz  # export to specific file
klangk import bak.tar.gz            # import workspace from archive
klangk import bak.tar.gz --name new-name  # import with a different name
klangk terminals my-project         # list all terminals (own + shared)
klangk share my-project bash        # share a terminal with workspace members
klangk unshare my-project bash      # stop sharing a terminal
klangk invite user@example.com      # send an invitation email (admin only)
klangk invitations                  # list all invitations (admin only)
klangk images                       # list available container images
klangk volumes ls                   # list your podman volumes
klangk volumes create nix-store     # create a named volume (owned by you)
klangk volumes rm nix-store         # delete a volume (must be yours)
```

The CLI connects to the running Klangk backend over HTTP + WebSocket — it works locally and against remote servers.

## Terminal behavior differences

`klangk shell` provides the same tmux-based terminal as the web frontend, but clipboard behavior differs:

- **Web frontend**: Text selections auto-copy to the system clipboard via the browser bridge. Mouse wheel scrolls through scrollback. No extra setup needed.
- **CLI (`klangk shell`)**: Text selections auto-copy to the system clipboard via [OSC 52](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html#h3-Operating-System-Commands), which requires your terminal emulator to support it. Mouse wheel scrollback works. Native text selection (viewport-only) is available via **Shift+drag**.

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

If your terminal does not support OSC 52, tmux selections will still be captured in the tmux paste buffer but will not automatically appear on your system clipboard. Consider switching to a terminal emulator that supports OSC 52 for the best `klangk shell` experience.
