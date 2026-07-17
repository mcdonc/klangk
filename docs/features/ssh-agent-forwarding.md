# SSH Agent Forwarding

When connecting via `klangk shell -A`, your local SSH agent is
forwarded into the workspace container. This lets you use
`git push git@github.com:...`, `ssh`, and other SSH-based tools
inside the container using your local SSH keys — without copying any
private keys.

## How it works

When `--forward-agent` (`-A`) is enabled (via CLI flag or config
file), `klangk shell` checks for a local `SSH_AUTH_SOCK` and sets
up a relay over the existing WebSocket tunnel:

1. A Unix socket is created inside the container at a well-known path
2. The socket is bridged to the CLI via socat and the WebSocket
3. `SSH_AUTH_SOCK` is set in the container shell's environment

When something inside the container (e.g., `ssh` or `git`) connects
to the agent socket, the request is relayed to your local SSH agent
and the response is sent back over the existing WebSocket connection.

## Usage Inside the Klangk Container

Pass `-A` (or `--forward-agent`) to enable forwarding:

```bash
# Make sure your agent is running and has keys loaded
ssh-add -l

# Connect with agent forwarding
klangk shell -A my-workspace

# Inside the container:
ssh-add -l                          # shows your forwarded keys
ssh -T git@github.com               # authenticates with your key
git clone git@github.com:user/repo  # works without any credentials
```

To enable forwarding by default, add `forward-agent: true` to your
CLI config file (`~/.config/klangk/cli.yaml`):

```yaml
# Enable for all servers
forward-agent: true

# Or enable/disable per server
servers:
  local:
    url: http://localhost:8995
    forward-agent: true
  prod:
    url: https://klangk.example.com
    forward-agent: false
```

The resolution priority is:

1. CLI flag (`--forward-agent` / `--no-forward-agent`) — highest
2. Per-server setting in `cli.yaml`
3. Global setting in `cli.yaml`
4. Default: `false`

See [CLI Configuration](../reference/cli.md#configuration) for full
config file documentation.

## Requirements

- A running SSH agent on your local machine (`SSH_AUTH_SOCK` must
  be set and point to a valid socket)
- The `klangk shell` CLI (agent forwarding is not available from
  the web frontend)

## Limitations

- **Sequential connections only**: The relay handles one SSH agent
  connection at a time. This works for typical usage (single `git
push`, `ssh` commands) but may not work correctly with parallel
  SSH operations like `git clone --recurse-submodules -j4`.
- **Web frontend**: Agent forwarding is only available via `klangk
shell`, not from the browser-based terminal.

## Session persistence

The agent socket path is set when the terminal starts. If you
disconnect and reconnect with `-A`, the socket is recreated at the
same path, so existing shells continue to work.

## Troubleshooting

### `ssh-add -l` says "Could not open a connection to your authentication agent"

- Check that `SSH_AUTH_SOCK` is set: `echo $SSH_AUTH_SOCK`
- If empty, either `-A` was not passed or your local agent wasn't
  running when you connected. Exit the shell, ensure `ssh-agent` is
  running locally, and reconnect with `klangk shell -A`.

### Agent forwarding doesn't work after reconnecting

- Reconnect with `-A` to restart the relay. The socket path does
  not change, so existing terminal tabs will work once the relay is
  re-established.

### Debugging

Set `KLANGKC_DEBUG_SSH_AGENT=1` to enable verbose logging of the SSH
agent relay. On the backend, messages go to the server log. On the
CLI, messages are written to `~/.klangk-ssh-agent.log` (to avoid
corrupting the terminal display).

```bash
# Backend side (in .env or environment)
KLANGKC_DEBUG_SSH_AGENT=1

# CLI side
export KLANGKC_DEBUG_SSH_AGENT=1
klangk shell -A my-workspace
# In another terminal:
tail -f ~/.klangk-ssh-agent.log
```

Log messages are prefixed with `[ssh-agent]` and show data flow
through each stage of the relay.
