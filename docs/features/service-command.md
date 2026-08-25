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
window that lives in a standalone `service` tmux session owned by the
workspace's agent identity (not any user's session), and is **never**
re-run for other users who open the workspace.

The session's `$HOME` is **always `/home/klangk`** — the shared home,
under both home layouts — and its shell is a bash login shell, so it
sources `/home/klangk/.profile`. That directory is populated from the
image skeleton before the session is created, on every fresh container
(including server boot for auto-start workspaces, before any user
connects). Environment setup a member writes to `/home/klangk/.profile`
therefore reaches the service — but note that under the default
per-handle layout a member's interactive shell reads _their own_
home's `.profile`, not the shared one; see
[The Shell](the-shell.md) for the distinction.

Because the service session and (under the shared home layout) every
member share one `/home/klangk`, they also share its mutable state: a
command typed into the service tab lands in the shared `.bash_history`,
and shell state (environment, cwd history) is visible across sessions.

The command is sent as keystrokes into the bash login shell, so:

- **Ctrl+C** stops the process and returns to the bash prompt
- **Up-arrow + Enter** restarts it
- The terminal scrollback shows the process output
- The experience is identical to typing the command yourself

If no service command is set, no `service-cmd` window is created.

### Who can see and control it

Because the command is a shared workspace service:

- Every member sees `service-cmd` in the shared terminal list (the
  owner and users granted a workspace role alike — **coders** /
  **collaborators**).
  [Read-only spectators](terminal.md#shared-terminals) can view it.
- The window is **shared by definition**: nobody has to reshare
  it manually, and it remains visible even after every member
  disconnects.

Anyone who can write to the shared window (members with the
`code-in-shared-terminals` permission) can stop or restart the
service via Ctrl+C / up-arrow / Enter — everyone joined sees the same
output.

## Setting the service command

### Web UI

Set the service command when creating a workspace, or change it
later in the workspace **Settings** tab.

### CLI

`klangk create` accepts `--command`/`-c` to set the service command at
creation time. On an existing workspace, use `klangk edit`:

```bash
# Set it when creating the workspace
klangk create my-workspace --command 'npm run dev'

# Set or change it on an existing workspace
klangk edit my-workspace --command 'npm run dev'

# Clear it
klangk edit my-workspace --command ''
```

### Sandbox config

In `.klangk-sandbox.yaml`:

```yaml
workspace:
  service-command: openclaw gateway
```

## When does the command run?

The service command runs whenever a **fresh container** is created
for the workspace — at workspace creation, on `klangk restart`, at
server boot (for [auto-start](#auto-start-workspaces) workspaces), or
on the first connection that starts the container. It does **not**
re-run on reconnect; if you disconnect and reconnect, you pick up the
tmux session exactly where you left off (the process may still be
running, or you may be at a bash prompt if it exited).

### Auto-start workspaces

If the workspace has [auto-start](workspaces.md#auto-start) enabled,
the container also starts when the Klangk server starts (boot), so
the service is already running before any user connects. When you
later run `klangk shell`, you walk up to the service already
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
