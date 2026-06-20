# The Shell

Bash is the default shell for all workspace terminals. The system-wide
`/etc/bash.bashrc` handles terminal setup (waiting for container readiness,
running plugin hooks, and launching default commands), then sources your
personal `~/.bashrc`.

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
system:

- Edit `~/.bashrc` for aliases, prompt customization, PATH changes
- Add scripts to `~/bin`
- Configure `~/.gitconfig`, `~/.vimrc`, etc.

All changes persist across container restarts.

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
