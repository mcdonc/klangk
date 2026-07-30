# SSH Agent Forwarding

By default, `klangk shell` forwards your local SSH agent into the workspace
container. This lets you use `git push git@github.com:...`, `ssh`, and other
SSH-based tools inside the container using your local SSH keys — without
copying any private keys.

## How it works

When agent forwarding is enabled (it is **on by default**), `klangk shell`
checks for a local `SSH_AUTH_SOCK` and sets up a relay over the existing
WebSocket tunnel:

1. A Unix socket is created inside the container at a well-known path
2. The socket is bridged to the CLI via socat and the WebSocket
3. `SSH_AUTH_SOCK` is set in the container shell's environment

When something inside the container (e.g., `ssh` or `git`) connects
to the agent socket, the request is relayed to your local SSH agent
and the response is sent back over the existing WebSocket connection.

## Usage Inside the Klangk Container

A plain `klangk shell` already forwards your agent — no flag needed:

```bash
# Make sure your agent is running and has keys loaded
ssh-add -l

# Connect (agent forwarding is on by default)
klangk shell my-workspace

# Inside the container:
ssh-add -l                          # shows your forwarded keys
ssh -T git@github.com               # authenticates with your key
git clone git@github.com:user/repo  # works without any credentials
```

Override the default per invocation with `--forward-agent` (`-A`) or
`--no-forward-agent`, or persistently via the `forward-agent` key in
`klangk.yaml` (set it to `false` to disable it for a workspace you don't
trust):

```yaml
# forward-agent is on by default. Disable it globally:
# forward-agent: false

# Or disable it for a specific untrusted server:
servers:
  prod:
    url: https://klangk.example.com
    forward-agent: false
```

The resolution priority is:

1. CLI flag (`--forward-agent` / `--no-forward-agent`) — highest
2. Per-server setting in `klangk.yaml`
3. Global setting in `klangk.yaml`
4. Default: `true` (a freshly generated `klangk.yaml` sets
   `forward-agent: true`)

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
