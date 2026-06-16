# Git credential delegation via browser WebSocket bridge (#237)

## Context

Git operations (push/pull/clone) inside workspace containers currently have no credential support — users must manually paste tokens or configure remotes with embedded credentials. We want to delegate credential acquisition to the user's browser via the existing browser-delegate bridge, so tokens never touch the container disk.

Nothing off-the-shelf fits: existing tools (GCM, git-credential-oauth, git-credential-forwarder) all assume the credential source is on the same machine. Our bridge is remote (browser ↔ WebSocket ↔ container). The credential helper protocol itself is trivial (stdin/stdout text), so the custom piece is small.

**Two access paths need support:**

1. **Browser** — user has a WebSocket connection; bridge is available for HTTPS credential delegation
2. **`klangk shell`** (CLI) — no browser; SSH agent forwarding over WebSocket for SSH-based git ops, plus credential helper fallback for HTTPS

## Issues

| Issue | Title | Dependencies |
|-------|-------|-------------|
| #385 | Plugin configuration system | — |
| #396 | Container-side credential helper script | #397 |
| #397 | Browser-side git-credential plugin (PAT + cache) | #385, #396 |
| #398 | GitHub OAuth device flow | #397, #385 |
| #399 | SSH agent forwarding for `klangk shell` | Independent (phase 2) |

## Architecture

```
Container terminal:
  git push → git-credential-klangk (helper) → curl POST /api/browser-delegate
    → backend wshandler → WebSocket → browser
    → browser_delegate.dart dispatches "git_credential" action
    → frontend shows cached token or triggers OAuth popup
    → response flows back through bridge → helper prints to stdout → git uses it

klangk shell (no browser):
  HTTPS: git push https://... → git-credential-klangk → bridge fails → exit 1
    → git falls through to interactive prompt (user types PAT)
  SSH:   git push git@github.com:... → ssh → SSH_AUTH_SOCK (forwarded)
    → WebSocket relay → klangk shell CLI → local ssh-agent → key used
```

## Components

### 1. `git-credential-klangk` — credential helper script (container)

**File**: `src/containers/workspace/bin/git-credential-klangk`

A shell script installed in the container image at `/usr/local/bin/git-credential-klangk`. Git calls it with `get`, `store`, or `erase` as the first argument.

**`get` flow:**

1. Read stdin (protocol, host, path — git credential helper protocol)
2. POST to bridge: `curl -s -X POST http://${KLANGK_BRIDGE_URL}/api/browser-delegate` with body `{"action": "git_credential", "token": "$KLANGK_BRIDGE_TOKEN", "operation": "get", "protocol": "...", "host": "..."}`
3. If bridge responds with credentials → print `username=...\npassword=...\n` to stdout
4. If bridge fails (no browser, timeout, user cancelled) → exit 1 (git falls through to next helper or prompts)

**`store` / `erase`:** Forward to bridge so frontend can update/clear its cache. Non-critical — exit 0 on failure.

**Fallback for `klangk shell`:** When `KLANGK_BRIDGE_TOKEN` is unset or bridge POST fails, the helper simply exits non-zero. Git falls through to whatever other credential helpers are configured (or prompts interactively). This means `klangk shell` users can:

- Set `GIT_ASKPASS` or use `git credential-store` manually
- Use SSH keys (ssh-agent forwarding — separate issue #185)
- Paste tokens when prompted

### 2. Git configuration in container image

**File**: `src/containers/workspace/Dockerfile` (or `Dockerfile.base`)

```dockerfile
COPY bin/git-credential-klangk /usr/local/bin/git-credential-klangk
RUN chmod +x /usr/local/bin/git-credential-klangk
```

**File**: Container gitconfig (global or system-level)

```
[credential]
    helper = klangk
```

Set as system gitconfig so all users get it. Users can override with their own `.gitconfig`.

### 3. Klangk plugin: `plugins/git-credential/`

In-repo plugin under `plugins/git-credential/`, following the same pattern as `plugins/celebrate/`. Each plugin has two sides:

- **`extension.ts`** — Pi agent extension (container-side). Not needed here — the shell script credential helper replaces this role.
- **`klangk/`** — Dart package (browser-side handler). This is where the credential dialog lives.

**Plugin structure:**

```
plugins/git-credential/
  package.json                    # Plugin metadata (no extension.ts needed)
  klangk/
    pubspec.yaml
    lib/
      klangk_plugin_git_credential.dart  # Barrel export
      plugin.dart                         # ToolPlugin implementation
      credential_cache.dart               # In-memory cache (protocol://host → token)
      credential_dialog.dart              # Flutter dialog widget
      github_device_flow.dart             # GitHub OAuth device flow client
```

**Plugin class** (`plugin.dart`):

```dart
class GitCredentialPlugin extends ToolPlugin {
  @override
  Map<String, ToolHandler> get handlers => {
    'git_credential': _handleCredential,
  };
}
```

**`_handleCredential` dispatches by `operation`** field (`get`, `store`, `erase`):

**`get` operation:**

1. Check in-memory credential cache (keyed by `protocol://host`)
2. If cached → return immediately as JSON `{"username": "...", "password": "..."}`
3. If not cached → show `CredentialDialog` (via `buildOverlay` or direct `showDialog`):
   - "Git needs credentials for `https://github.com`"
   - PAT text field (works with any host)
   - "Sign in with GitHub" button (shown only if host is `github.com` and `client_id` is available)
   - GitHub device flow: show code + verification URL, poll for token
4. Cache result in memory (session-only — not localStorage for security)
5. Return credentials JSON string

**`store` operation:** Update cache (confirms credentials worked).

**`erase` operation:** Remove from cache (credentials were rejected by git).

**Registration**: The plugin import system (`import_plugins.py`) discovers `plugins/git-credential/klangk/` and adds `GitCredentialPlugin` to `createAllPlugins()`.

### 4. Backend: minimal changes

- The bridge itself needs no changes — `"git_credential"` flows through `dispatch_browser_request_to()` as any other action.
- The plugin needs `KLANGK_GITHUB_OAUTH_CLIENT_ID` surfaced to the frontend so it knows whether to show the GitHub OAuth button. This depends on **#385** (plugin configuration system) — the plugin manifest would declare this env var, and the backend would expose it via a config endpoint or pass it through the bridge.
- **If #385 is not yet implemented**, we can bootstrap with a simple `/api/config` public endpoint that returns `{"github_oauth_client_id": "..."}` from the server env. This is the same pattern we'd formalize in #385.

### 5. Dependency: #385 (plugin configuration)

Issue #385 ("Allow plugins to respect/register configuration settings") is a prerequisite for doing this cleanly. The git credential plugin needs:

- `KLANGK_GITHUB_OAUTH_CLIENT_ID` — surfaced to the frontend (for the OAuth device flow button)
- Potentially other provider client IDs in the future (GitLab, Bitbucket)

Without #385, we'd hardcode a `/api/config` endpoint. With #385, the plugin manifest declares its config keys and the system handles the plumbing.

## GitHub OAuth via device flow

The credential dialog includes a "Sign in with GitHub" button alongside the PAT text field. We use GitHub's **OAuth device flow** — it's ideal because:

- No `client_secret` needed (only `client_id`) — safe to embed in frontend
- No redirect URI handling — user clicks a link to `github.com/login/device`, enters a code
- Works even if the Klangk instance has no public URL
- The user is already in a browser, so clicking the link is trivial

### Flow

1. User clicks "Sign in with GitHub" in the credential dialog
2. Frontend calls `POST https://github.com/login/device/code` with `client_id` and `scope=repo`
3. GitHub returns `device_code`, `user_code`, and `verification_uri`
4. Dialog shows: "Enter code **XXXX-YYYY** at github.com/login/device" with a clickable link
5. Frontend polls `POST https://github.com/login/oauth/access_token` with `device_code` every 5s
6. Once user approves in their browser tab, poll returns `access_token`
7. Token cached in memory, returned to credential helper as `password`

### Configuration

Requires a **GitHub OAuth App** `client_id`. Configurable via:

- `KLANGK_GITHUB_OAUTH_CLIENT_ID` env var on the backend
- Passed to frontend via a new `/api/config` endpoint (or similar public config)
- If not configured, the "Sign in with GitHub" button is hidden; only PAT input shown

The `repo` scope is needed for private repo access. For public repos, no scope is needed.

### GitLab / other providers (future)

Device flow is also supported by GitLab and other OAuth providers. The pattern is the same — just different URLs and client IDs. Can be added later without architectural changes.

## `klangk shell`: SSH agent forwarding over WebSocket

The CLI connects to the container via WebSocket (not SSH), so we can't just mount `SSH_AUTH_SOCK`. Instead, we forward the SSH agent protocol over the existing WebSocket tunnel:

### How it works

```
User's machine                    Klangk backend              Container
┌─────────────┐                  ┌──────────────┐           ┌───────────┐
│ ssh-agent    │                  │              │           │           │
│   ↕          │                  │              │           │  socat    │
│ klangk shell │◄── WebSocket ──►│  wshandler   │◄─ pipe ──►│  ↕        │
│ (reads local │   ssh_agent_*   │  (relay)     │           │ SSH_AUTH_ │
│  SSH_AUTH_   │   messages      │              │           │ SOCK      │
│  SOCK)       │                  │              │           │           │
└─────────────┘                  └──────────────┘           └───────────┘
```

1. **`klangk shell` startup**: CLI detects local `SSH_AUTH_SOCK`, sends `{"cmd": "ssh_agent_start"}` over WebSocket
2. **Backend**: Creates a Unix socket inside the container (e.g., `/tmp/.klangk-ssh-agent.sock`) and a relay process. Sets `SSH_AUTH_SOCK` in the terminal environment to point at it.
3. **Relay**: When something in the container reads from the socket, backend forwards the SSH agent protocol bytes over WebSocket as `ssh_agent_data` messages. CLI reads them, forwards to local `ssh-agent`, sends response back.
4. **Result**: `git push git@github.com:...` inside the container uses the user's local SSH keys without any keys being copied.

### Implementation detail

The SSH agent protocol is simple binary framing (4-byte length prefix + message). We don't need to parse it — just relay opaque bytes between the container socket and the CLI's local `SSH_AUTH_SOCK`.

The relay is bidirectional:

- **Container → CLI**: `socat` on container socket → backend reads → WS `ssh_agent_data` → CLI → local agent
- **CLI → Container**: CLI reads agent response → WS `ssh_agent_response` → backend writes to relay → socat → container socket

### Complexity assessment

This is moderate complexity:

- **CLI side**: ~50 lines — read/write local `SSH_AUTH_SOCK`, relay over WS
- **Backend side**: ~80 lines — new WS message types (`ssh_agent_start`, `ssh_agent_data`, `ssh_agent_response`), manage relay process per session
- **Container side**: `socat` is already available or trivially installable. Creates the listening socket.

### Fallback

If local `SSH_AUTH_SOCK` is not set, `klangk shell` skips agent forwarding. Users can still:

- Use the HTTPS credential helper (which falls through to interactive prompt since there's no browser)
- Configure their own git credentials inside the container

## `klangk shell`: HTTPS credential helper fallback

For HTTPS git operations in `klangk shell`, the credential helper (`git-credential-klangk`) detects no `KLANGK_BRIDGE_TOKEN` (or bridge POST fails) and exits non-zero. Git falls through to its default behavior (interactive terminal prompt). This works fine — user just types their PAT at the prompt.

## Phasing

### Phase 1: HTTPS credential helper via browser bridge

- `git-credential-klangk` shell script in container
- Frontend `git_credential` action handler with credential dialog
- PAT text input (works with any git host)
- GitHub OAuth device flow button (if `KLANGK_GITHUB_OAUTH_CLIENT_ID` configured)
- In-memory credential cache (session-only, keyed by protocol://host)
- Dockerfile changes (install helper, set system gitconfig)
- `/api/config` endpoint to expose OAuth client_id to frontend
- Tests

### Phase 2 (future): SSH agent forwarding for `klangk shell`

- CLI-side agent relay (read/write local `SSH_AUTH_SOCK`)
- Backend WS message types (`ssh_agent_start/data/response`)
- Container-side socat listener
- Tests

### Phase 3 (future): Additional OAuth providers

- GitLab, Bitbucket device flow support
- Host → provider auto-detection

## Files to create/modify

### Phase 1

| File | Action | Description |
|------|--------|-------------|
| `src/containers/workspace/bin/git-credential-klangk` | **Create** | Shell script credential helper |
| `src/containers/workspace/Dockerfile` | **Modify** | COPY helper, set system gitconfig |
| `plugins/git-credential/package.json` | **Create** | Plugin metadata |
| `plugins/git-credential/klangk/pubspec.yaml` | **Create** | Dart package manifest |
| `plugins/git-credential/klangk/lib/plugin.dart` | **Create** | ToolPlugin: dispatches get/store/erase |
| `plugins/git-credential/klangk/lib/credential_cache.dart` | **Create** | In-memory cache keyed by protocol://host |
| `plugins/git-credential/klangk/lib/credential_dialog.dart` | **Create** | Flutter dialog: PAT field + GitHub OAuth button |
| `plugins/git-credential/klangk/lib/github_device_flow.dart` | **Create** | GitHub OAuth device flow client |
| `src/backend/klangk_backend/api.py` | **Modify** | `/api/config` endpoint (public, returns github_oauth_client_id) |
| `src/backend/tests/test_api.py` | **Modify** | Test `/api/config` |
| `plugins/git-credential/klangk/test/plugin_test.dart` | **Create** | Unit tests for handler |
| `plugins/git-credential/klangk/test/device_flow_test.dart` | **Create** | Unit tests for GitHub device flow |

### Phase 2

| File | Action | Description |
|------|--------|-------------|
| `src/backend/klangk_backend/cli/client.py` | **Modify** | SSH agent relay in `_ws_shell` |
| `src/backend/klangk_backend/wshandler.py` | **Modify** | `ssh_agent_start/data/response` handlers |
| `src/backend/klangk_backend/terminal.py` | **Modify** | Create agent socket, set `SSH_AUTH_SOCK` |
| `src/containers/workspace/Dockerfile.base` | **Modify** | Ensure `socat` is installed |
| `src/backend/tests/test_wshandler.py` | **Modify** | Tests for agent relay |
| `src/backend/tests/test_cli.py` | **Modify** | Tests for CLI agent forwarding |

## Verification

### Phase 1

1. Open a workspace in the browser, open a terminal
2. `git clone https://github.com/some/private-repo.git` → dialog appears asking for credentials
3. Enter a PAT → clone succeeds → subsequent operations reuse cached token
4. `git push` to a repo where the cached token is invalid → credential erased, re-prompted
5. Open `klangk shell` (no browser) → `git clone https://...` falls through to interactive prompt
6. Backend tests pass, frontend tests pass

### Phase 2

1. Ensure local `ssh-agent` is running with a key added (`ssh-add -l`)
2. `klangk shell myworkspace`
3. Inside container: `ssh -T git@github.com` → "Hi username!" (uses forwarded key)
4. `git clone git@github.com:user/private-repo.git` → succeeds using forwarded SSH key
5. No private keys exist in the container at any point
