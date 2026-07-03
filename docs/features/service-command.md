<!-- markdownlint-disable MD013 -->

# Service Command

A workspace can have a **service command** — a shell command that
runs automatically in a dedicated terminal window when the workspace
is opened. This is useful for workspaces that serve a long-running
process like a dev server, AI gateway, or any daemon that should be
running whenever the workspace is in use.

## How it works

The service command runs as a **per-workspace singleton** — like a
global service. It starts exactly once, in a dedicated `service-cmd`
tmux window that lives in the **workspace owner's** terminal session,
and is **never** re-run for other users who open the workspace.

The command is sent as keystrokes into a bash login shell, so:

- **Ctrl+C** stops the process and returns to the bash prompt
- **Up-arrow + Enter** restarts it
- The terminal scrollback shows the process output
- The experience is identical to typing the command yourself

If no service command is set, no `service-cmd` window is created.

### Who can see and control it

Because the command is a shared workspace service:

- The **owner** sees `service-cmd` as one of their own terminal tabs.
- Other users granted a workspace role (**coders** / **collaborators**)
  see it as a **shared terminal** they can open and join.
  [Read-only spectators](terminal.md#shared-terminals) can view it.
- The window is **shared by definition**: the owner never has to reshare
  it manually, and it remains visible even after the owner disconnects.

Anyone who can write to the shared window (the owner, plus users with
the `code-in-shared-terminals` permission) can stop or restart the
service via Ctrl+C / up-arrow / Enter — everyone joined sees the same
output.

## Setting the service command

### Web UI

Set the service command when creating a workspace, or change it
later in the workspace **Settings** tab.

### CLI

`klangkc create` accepts `--command`/`-c` to set the service command at
creation time. On an existing workspace, use `klangkc edit`:

```bash
# Set it when creating the workspace
klangkc create my-workspace --command 'npm run dev'

# Set or change it on an existing workspace
klangkc edit my-workspace --command 'npm run dev'

# Clear it
klangkc edit my-workspace --command ''
```

### Sandbox config

In `.klangk-sandbox.yaml`:

```yaml
workspace:
  service-command: openclaw gateway
```

## When does the command run?

The service command runs when the terminal session is created —
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
running in the `service-cmd` tab. Visitors who open the workspace see
it as a shared terminal without any action from the owner.

## Shell features

The command is sent as keystrokes into a bash shell, so any shell
syntax works — pipes, redirects, `&&` chains, subshells, etc.:

```yaml
workspace:
  service-command: openclaw gateway 2>&1 | tee /tmp/gateway.log
```

## Use cases

- **Dev servers** — `npm run dev`, `python manage.py runserver`
- **AI agents** — `pi`, `openclaw gateway`
- **Background services** — any daemon you want running by default
- **Project setup** — a command that initializes the environment
  on first terminal open
