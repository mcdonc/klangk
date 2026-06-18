# SSH Agent Forwarding

When connecting via `klangkc shell`, your local SSH agent is
automatically forwarded into the workspace container. This lets you
use `git push git@github.com:...`, `ssh`, and other SSH-based tools
inside the container using your local SSH keys — without copying any
private keys.

## How it works

`klangkc shell` detects your local `SSH_AUTH_SOCK` environment
variable. If an agent is available, it sets up a relay over the
existing WebSocket tunnel:

1. A Unix socket is created inside the container at a well-known path
2. The socket is bridged to the CLI via socat and the WebSocket
3. `SSH_AUTH_SOCK` is set in the container shell's environment

When something inside the container (e.g., `ssh` or `git`) connects
to the agent socket, the request is relayed to your local SSH agent
and the response is sent back — all over the existing WebSocket
connection.

## Usage

No special flags are needed. If your local SSH agent is running,
forwarding happens automatically:

```bash
# Make sure your agent is running and has keys loaded
ssh-add -l

# Connect — agent forwarding starts automatically
klangkc shell my-workspace

# Inside the container:
ssh-add -l                          # shows your forwarded keys
ssh -T git@github.com               # authenticates with your key
git clone git@github.com:user/repo  # works without any credentials
```

## Requirements

- A running SSH agent on your local machine (`SSH_AUTH_SOCK` must
  be set and point to a valid socket)
- The `klangkc shell` CLI (agent forwarding is not available from
  the web frontend)

## Limitations

- **Sequential connections only**: The relay handles one SSH agent
  connection at a time. This works for typical usage (single `git
push`, `ssh` commands) but may not work correctly with parallel
  SSH operations like `git clone --recurse-submodules -j4`.
- **Web frontend**: Agent forwarding is only available via `klangkc
shell`, not from the browser-based terminal.
- **Session persistence**: The agent socket path is set when the
  terminal starts. If you disconnect and reconnect, the socket is
  recreated at the same path, so existing shells continue to work.

## Troubleshooting

### `ssh-add -l` says "Could not open a connection to your authentication agent"

- Check that `SSH_AUTH_SOCK` is set: `echo $SSH_AUTH_SOCK`
- If empty, your local agent wasn't running when you connected.
  Exit the shell and ensure `ssh-agent` is running locally, then
  reconnect.

### Agent forwarding doesn't work after reconnecting

- If you opened new terminal tabs while disconnected, they may have
  inherited a stale `SSH_AUTH_SOCK`. Open a new tab after
  reconnecting.

### Debugging

Set `KLANGK_DEBUG_SSH_AGENT=1` in your `.env` (or export it in the
shell running the backend) to enable verbose logging of the SSH agent
relay on both the backend and CLI side. The CLI also respects this
variable — export it in the terminal where you run `klangkc shell`.

```bash
# Backend side (in .env or environment)
KLANGK_DEBUG_SSH_AGENT=1

# CLI side
export KLANGK_DEBUG_SSH_AGENT=1
klangkc shell -A my-workspace
```

Log messages are prefixed with `[ssh-agent]` and show data flow
through each stage of the relay.
