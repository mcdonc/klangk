# The Shell

Bash is the default shell for all workspace terminals. The system-wide
`/etc/bash.bashrc` handles terminal setup (waiting for container readiness,
running plugin hooks, and launching default commands), then sources your
personal `~/.bashrc`.

Your `~/.bashrc` persists across container restarts (it lives on the
bind-mounted home directory), so any customizations you make are permanent.

## User environment

Each workspace container runs as a single UNIX user (`klangk`), but
Klangk supports multiple workspace members by giving each person their
own `$HOME` directory. When you open a terminal, `$HOME` is set to your
personal directory (e.g., `/home/<handle>/`), so
dotfiles like `.bashrc`, `.gitconfig`, `.vimrc`, and `.zshrc` are
per-user and persist across sessions.

You can customize your environment the same way you would on any UNIX
system — edit dotfiles in `$HOME`, add scripts to `~/bin`, configure
your shell prompt, set up aliases, etc.

!!! warning
Because all workspace members share the same UNIX user, every
member can read and modify every file under `/home` — including
other members' home directories. Do not store secrets or sensitive
data in your home directory if you share the workspace with
untrusted users.

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
