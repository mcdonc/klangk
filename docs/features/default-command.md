<!-- markdownlint-disable MD013 -->

# Default Command

A workspace can have a **default command** — a shell command that
runs automatically in the first terminal window when the terminal
session starts. This is useful for workspaces that serve a
long-running process like a dev server, AI gateway, or any daemon
that should be running whenever the workspace is in use.

## How it works

When a workspace has a default command configured, the server sends
it as keystrokes into the first tmux window immediately after
creating the terminal session. The command runs as a normal
foreground process inside a bash login shell.

This means:

- **Ctrl+C** stops the process and returns you to the bash prompt
- **Up-arrow + Enter** restarts it
- The terminal scrollback shows the process output
- The experience is identical to typing the command yourself

If no default command is set, the terminal starts with a plain bash
prompt as usual.

## Setting the default command

### Web UI

Set the default command when creating a workspace, or change it
later in the workspace **Settings** tab.

### CLI

```bash
# Set during creation
klangkc create my-workspace --default-command 'openclaw gateway'

# Change later
klangkc edit my-workspace --default-command 'npm run dev'

# Clear it
klangkc edit my-workspace --default-command ''
```

### Sandbox config

In `.klangk-sandbox.yaml`:

```yaml
workspace:
  default-command: openclaw gateway
```

## When does the command run?

The default command runs when the terminal session is created —
typically on the first connection to the workspace after the
container starts. It does **not** re-run on reconnect; if you
disconnect and reconnect, you pick up the tmux session exactly
where you left off (the process may still be running, or you may
be at a bash prompt if it exited).

### Auto-start workspaces

If the workspace has [auto-start](workspaces.md#auto-start) enabled,
the container starts when the Klangk server starts and the default
command begins running immediately — before any user connects. When
you later run `klangkc shell`, you walk up to the service already
running in tmux window 0.

## Shell features

The command is sent as keystrokes into a bash shell, so any shell
syntax works — pipes, redirects, `&&` chains, subshells, etc.:

```yaml
workspace:
  default-command: openclaw gateway 2>&1 | tee /tmp/gateway.log
```

## Use cases

- **Dev servers** — `npm run dev`, `python manage.py runserver`
- **AI agents** — `pi`, `openclaw gateway`
- **Background services** — any daemon you want running by default
- **Project setup** — a command that initializes the environment
  on first terminal open
