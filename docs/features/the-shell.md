# The Shell

Bash is the default shell for all workspace terminals. Two system files
set up the environment before your personal `~/.bashrc` runs:

- `/etc/profile.d/klangk-*.sh` — environment exports (`PATH=/opt/klangk/bin`,
  `EDITOR`) sourced by **every login shell**, interactive or not. This is
  why one-shot commands like the workspace health check (`bash -lc`) and
  `klangkc exec` (also `bash -lc`, #1041) still find `pi`, `herdr`, and
  the `klangk-*` helpers.
- `/etc/bash.bashrc` — interactive-shell setup: waits for container
  readiness, runs `on-shell-init` plugin hooks, and (via the default
  command) launches the workspace's configured service.

Your `~/.bashrc` persists across container restarts (it lives on the
bind-mounted home directory), so any customizations you make are permanent.

## Users and the single-UNIX-user design

Workspace containers run as a single UNIX user called `klangk`. There
are no separate UNIX accounts for each workspace member — everyone
runs as the same uid.

This is intentional. Collaboration between actual UNIX users is
invariably painful: file ownership mismatches, group permission
headaches, umask defaults that lock teammates out, setgid directory
hacks that half-work. Every project that tries to make multi-user
file sharing work on UNIX ends up with a pile of permission workarounds.

By running everything as a single UNIX user, Klangk sidesteps all of
this. Every workspace member can read and write every file without
fighting permissions. Collaboration just works.

### Per-user home directories

Even though everyone shares the same UNIX user, each workspace member
gets their own `$HOME` directory. When you open a terminal, `$HOME` is
set to `/home/<handle>/` — a symlink that points to
`.users/<user-id>/` on the bind-mounted home volume.

This means:

- Your dotfiles (`.bashrc`, `.gitconfig`, `.vimrc`) are yours alone
- Your bash history is separate from other members'
- Your AI agent config (`.pi/agent/`, `.claude/`) is per-user
- The shared project directory at `/home/work/` is accessible to everyone

See [Handles](handles.md) for how handles are assigned and how they
relate to your home directory path.

### The tradeoff

Because all workspace members share the same UNIX user, every member
can read and modify every file under `/home` — including other members'
home directories and dotfiles. This is the cost of frictionless
collaboration. Use separate workspaces for collaboration with different
groups of differently trusted users, or for solo work where you need
full privacy.

!!! warning
Do not store secrets or sensitive data in your home directory if
you share the workspace with untrusted users.

## Customizing your environment

You can customize your shell the same way you would on any UNIX
system. The key question is _which file_ to put a change in, because
Klangk runs commands in several contexts and not all of them source the
same startup files (see [Startup files](#startup-files) below):

- Edit `~/.profile` for **environment exports** (PATH additions,
  `OPENCLAW_HOME`, tool-manager setup like nvm/asdf) that every shell
  — including non-interactive ones like the health check — must see.
- Edit `~/.bashrc` for **interactive niceties** (aliases, prompt
  customization) that only matter in a terminal you're typing into.
- Add scripts to `~/bin`
- Configure `~/.gitconfig`, `~/.vimrc`, etc.

All changes persist across container restarts.

## Startup files

Klangk runs in-container commands in a few different ways, and each
sources a different set of startup files. Getting this right matters:
an environment export buried below `~/.bashrc`'s interactivity guard is
invisible to the health check, so a check like `openclaw health` reports
perpetually unhealthy even though the service is fine (#1087).

### Convention

| File                                        | Purpose                                                                                                              | Sourced by                                                                              |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `/etc/profile.d/klangk-*.sh`                | system-wide defaults (klangk `PATH`, `EDITOR`)                                                                       | every login shell                                                                       |
| `~/.profile`                                | **per-user environment exports** (PATH additions, tool homes, nvm/asdf) — anything non-interactive commands must see | login shells: interactive terminals, the default command, the health check (`bash -lc`) |
| `~/.bashrc` (below the interactivity guard) | interactive niceties (aliases, prompt)                                                                               | interactive non-login bash shells; also chained from `~/.profile` for login shells      |

**Rule of thumb:** if a non-interactive command (health check, a
script) needs it, it goes in `~/.profile`. If it only matters when
you're at a prompt, it goes in `~/.bashrc`.

### Which code path sources what

| In-container command                         | How Klangk runs it                            | Sources `~/.profile`?                                              |
| -------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------ |
| Interactive terminal                         | `tmux new-session` (login shell) or `bash -l` | yes                                                                |
| `default_command` (the `default-cmd` window) | login shell (tmux window 0)                   | yes                                                                |
| Workspace health check                       | `bash -lc` (a login shell, #1087)             | yes                                                                |
| `klangkc exec` (default)                     | `bash -lc` (a login shell, #1041)             | yes                                                                |
| `klangkc exec --raw` / `klangkc sync`        | raw command (no shell)                        | no — programmatic transports (rsync) must not source startup files |

This is why workspace setup scripts (`sandboxes/*/setup.sh`) persist
their env exports to `~/.profile` rather than `~/.bashrc`: the exports
must be visible to the health check and the default command, both of
which are login shells that source `~/.profile`. `~/.bashrc`'s
interactivity guard (`case $- in *i*) ;; *) return`) hides its body
from those non-interactive login shells.

## Using zsh instead

Zsh is installed but not the default shell. To switch, add the following
to your `~/.bashrc` inside the workspace:

```bash
if [ -x /usr/bin/zsh ] && [ -z "$ZSH_STARTED" ]; then
    export ZSH_STARTED=1
    exec zsh
fi
```

This lets the bash startup complete normally (plugin hooks, default
command handling), then replaces bash with zsh. The `ZSH_STARTED` guard
prevents infinite loops. Your `~/.zshrc` will be sourced as usual.
