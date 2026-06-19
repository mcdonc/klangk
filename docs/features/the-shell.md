# The Shell

Bash is the default shell for all workspace terminals. The system-wide
`/etc/bash.bashrc` handles terminal setup (waiting for container readiness,
running plugin hooks, and launching default commands), then sources your
personal `~/.bashrc`.

Your `~/.bashrc` persists across container restarts (it lives on the
bind-mounted home directory), so any customizations you make are permanent.

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
