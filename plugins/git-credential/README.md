# git-credential plugin

Browser-delegated git credential helper for Klangk workspaces. When git
needs HTTPS credentials (e.g. `git push`), a dialog appears in the
user's browser tab asking for a username and personal access token (PAT).
Credentials are cached in memory for the browser session.

## Components

### Container side

**`git-credential-klangk`** (`tools/git-credential-klangk`) is a Python
script installed to `/opt/klangk/bin/` in the workspace image. Git calls
it automatically because `on-image-build.sh` sets
`git config --system credential.helper klangk` at image build time.

Git invokes the helper with one of three operations:

- **`get`** — git needs credentials. The helper reads the current
  browser ID via `klangk-browser-id`, then POSTs to the backend's
  `/api/browser-delegate` endpoint. The backend routes the request to
  the correct browser tab over WebSocket.
- **`store`** — git confirms that credentials worked. The helper
  forwards to the bridge so the browser plugin can cache them.
- **`erase`** — git reports that credentials were rejected. The helper
  forwards to the bridge so the browser plugin can clear its cache.

If no browser is connected (e.g. `klangk shell` without a browser),
the helper exits non-zero and git falls through to its next configured
credential helper or prompts interactively.

### Browser side

**`GitCredentialPlugin`** (`klangk/lib/plugin.dart`) is a Dart plugin
that runs in the Flutter web app. It registers a handler for the
`git_credential` bridge action.

On a `get` request:

1. Check the in-memory credential cache (keyed by `protocol://host`).
2. If cached, return credentials immediately — no dialog shown.
3. If not cached, show a modal dialog asking for username and PAT.
4. Wait for the user to submit or cancel.
5. Return credentials (or an error if cancelled) as the bridge response.

On a `store` request, the plugin adds the credentials to its cache.
On an `erase` request, the plugin removes them.

### Image build hook

**`on-image-build.sh`** runs `git config --system credential.helper klangk`
at image build time so git finds the helper without per-user configuration.

## Protocol

The full flow for `git push` over HTTPS:

```text
git push
  → git calls: git-credential-klangk get
    → reads browser ID from klangk-browser-id (tmux env)
    → POST /api/browser-delegate
        { action: "git_credential",
          browser_id: "<uuid>",
          operation: "get",
          protocol: "https",
          host: "github.com" }
    → backend resolves browser_id to a WebSocket connection
    → sends browser_request to that browser tab
    → GitCredentialPlugin._handleGet() runs in the browser
      → cache hit? return cached credentials
      → cache miss? show PAT dialog, wait for user
    → browser sends browser_response with credentials
    → backend returns HTTP response to the helper
    → helper prints username=.../password=... to stdout
  → git authenticates with the credentials
  → push succeeds
  → git calls: git-credential-klangk store
    → POST /api/browser-delegate { operation: "store", username, password }
    → plugin caches credentials for future requests
```

If authentication fails, git calls `erase` instead of `store`, and the
plugin removes any cached credentials for that host.

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
4. Tab B shows the PAT dialog.
5. After you enter credentials, tab B caches them independently.

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
and the raw bridge response.
