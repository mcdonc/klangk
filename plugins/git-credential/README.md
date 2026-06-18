# git-credential plugin

Browser-delegated git credential helper for Klangk workspaces. When git
needs HTTPS credentials (e.g. `git push`), the helper either runs the
GitHub OAuth device flow (if configured) or shows a PAT dialog in the
user's browser tab. Credentials are cached in memory for the browser
session.

## Components

### Container side

**`git-credential-klangk`** (`tools/git-credential-klangk`) is a Python
script installed to `/opt/klangk/bin/` in the workspace image. Git calls
it automatically because `on-image-build.sh` sets
`git config --system credential.helper klangk` at image build time.

Git invokes the helper with one of three operations:

- **`get`** — git needs credentials. If `KLANGK_GITHUB_OAUTH_CLIENT_ID`
  is set and the host is `github.com`, the helper runs the GitHub device
  flow: it requests a code from GitHub, sends it to the browser for
  display, and polls GitHub for the token. If the device flow is not
  available or fails, the helper falls back to the bridge-based PAT
  dialog.
- **`store`** — git confirms that credentials worked. The helper
  forwards to the bridge so the browser plugin can cache them.
- **`erase`** — git reports that credentials were rejected. The helper
  forwards to the bridge so the browser plugin can clear its cache.

If no browser is connected (e.g. `klangkc shell` without a browser),
the helper exits non-zero and git falls through to its next configured
credential helper or prompts interactively.

### Browser side

**`GitCredentialPlugin`** (`klangk/lib/plugin.dart`) is a Dart plugin
that runs in the Flutter web app. It registers a handler for the
`git_credential` bridge action.

The plugin handles these operations:

- **`get`** — check the in-memory credential cache. On a hit, return
  credentials immediately. On a miss, show a modal PAT dialog and wait
  for the user to submit or cancel.
- **`store`** / **`erase`** — update or clear the credential cache.
- **`device_flow_show`** — display the GitHub device flow code and
  verification link, and auto-open the GitHub authorization page in a
  popup window.
- **`device_flow_done`** — dismiss the device flow display.
- **`device_flow_error`** — show an error message in the device flow
  display.

### Image build hook

**`on-image-build.sh`** runs `git config --system credential.helper klangk`
at image build time so git finds the helper without per-user configuration.

## Protocol

### GitHub device flow (when configured)

```text
git push (to github.com)
  → git calls: git-credential-klangk get
    → KLANGK_GITHUB_OAUTH_CLIENT_ID is set, host is github.com
    → POST https://github.com/login/device/code (from container)
    → GitHub returns device_code, user_code, verification_uri
    → POST /api/browser-delegate { operation: "device_flow_show",
        user_code, verification_uri }
    → browser shows code dialog, opens GitHub auth page in popup
    → helper polls POST https://github.com/login/oauth/access_token
    → user authorizes in popup
    → poll returns access_token
    → POST /api/browser-delegate { operation: "device_flow_done" }
    → browser dismisses code dialog
    → helper prints username=x-access-token / password=<token>
  → git authenticates with the token
  → push succeeds
  → git calls: git-credential-klangk store
    → POST /api/browser-delegate { operation: "store", username, password }
    → plugin caches credentials for future requests
```

The access token never passes through the backend or browser — it goes
directly from GitHub to the container helper to git's stdout.

### PAT dialog fallback

```text
git push (to any host, or github.com without device flow)
  → git calls: git-credential-klangk get
    → POST /api/browser-delegate { operation: "get", host: "..." }
    → browser plugin checks cache
      → cache hit: return cached credentials
      → cache miss: show PAT dialog, wait for user
    → browser sends browser_response with credentials
    → helper prints username=.../password=... to stdout
  → git authenticates
  → push succeeds
  → git calls: git-credential-klangk store
    → plugin caches credentials
```

If authentication fails, git calls `erase` instead of `store`, and the
plugin removes any cached credentials for that host.

## Configuration

The plugin declares one config variable in `package.json`:

- **`KLANGK_GITHUB_OAUTH_CLIENT_ID`** (scope: `container`) — GitHub
  OAuth App client ID. When set, the device flow activates for
  `github.com` hosts. No client secret needed.

## Credential cache

The cache is **per-tab** and **in-memory only**:

- Each browser tab has its own `GitCredentialPlugin` instance with its
  own cache. Credentials entered in tab A are not available in tab B.
- Refreshing the page clears the cache (new plugin instance).
- Closing the tab clears the cache.
- The cache is keyed by `protocol://host` (e.g. `https://github.com`).

## Multi-tab behavior

Two browser tabs viewing the same workspace share the same tmux session
and the same container terminal. The browser ID in the tmux environment
determines which tab receives bridge requests.

When you click into a terminal, the frontend sends a `browser_reattach`
message that updates the browser ID to the active tab. So bridge
requests always route to whichever tab you last interacted with.

If tab A has cached credentials and you switch to tab B:

1. `git push` in the terminal runs the credential helper.
2. `klangk-browser-id` returns tab B's browser ID (set by
   `browser_reattach` when you clicked into the terminal on tab B).
3. The bridge routes to tab B's plugin, which has an empty cache.
4. Tab B shows the PAT dialog (or device flow code display).
5. After authentication, tab B caches credentials independently.

Each tab maintains its own credential cache. There is no cross-tab
credential sharing.

## Debugging

Set `GIT_CREDENTIAL_KLANGK_DEBUG=1` in the container terminal to see
the helper's stderr output:

```sh
export GIT_CREDENTIAL_KLANGK_DEBUG=1
git push
```

This prints the bridge URL, browser ID, credential input from git,
device flow status, and the raw bridge response.
