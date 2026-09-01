# git-credential feature

Browser-delegated git credential helper for Klangk workspaces. When git
needs HTTPS credentials (e.g. `git push`), the helper either runs an
OAuth device flow (GitHub via the shorthand client ID, or any RFC 8628
provider — GitLab, Gitea, self-hosted — via `KLANGKWS_FEATURE_OAUTH_PROVIDERS`)
or shows a PAT dialog in the user's browser tab. Credentials are cached
in memory for the browser session.

## Components

### Container side

**`git-credential-klangk`** (`tools/git-credential-klangk`) is a Python
script at `/opt/klangk/features/git-credential/tools/`, symlinked to
`/usr/local/bin/` by `on-image-build.sh`. Git calls it automatically
because the same hook sets `git config --system credential.helper klangk`.

Git invokes the helper with one of three operations:

- **`get`** — git needs credentials. If an OAuth device-flow provider is
  configured for the host — a `KLANGKWS_FEATURE_OAUTH_PROVIDERS` entry,
  or `KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID` for `github.com` — the
  helper runs that provider's device flow: it requests a code from the
  provider, sends it to the browser for display, and polls the provider
  for the token. If the device flow is not available or fails, the helper
  falls back to the bridge-based PAT dialog.
- **`store`** — git confirms that credentials worked. The helper
  forwards to the bridge so the browser feature can cache them.
- **`erase`** — git reports that credentials were rejected. The helper
  forwards to the bridge so the browser feature can clear its cache.

If no browser is connected (e.g. `klangk shell` without a browser),
the helper exits non-zero and git falls through to its next configured
credential helper or prompts interactively.

### Browser side

**`GitCredentialFeature`** (`klangk/lib/feature.dart`) is a Dart feature
that runs in the Flutter web app. It registers a handler for the
`git_credential` bridge action.

The feature handles these operations:

- **`get`** — check the in-memory credential cache. On a hit, return
  credentials immediately. On a miss, show a modal PAT dialog and wait
  for the user to submit or cancel.
- **`store`** / **`erase`** — update or clear the credential cache.
- **`device_flow_show`** — display the device flow code and verification
  link for the provider host, and auto-open the provider's authorization
  page in a popup window.
- **`device_flow_done`** — dismiss the device flow display.
- **`device_flow_error`** — show an error message in the device flow
  display.

### Image build hook

**`on-image-build.sh`** runs `git config --system credential.helper klangk`
at image build time so git finds the helper without per-user configuration.

## Protocol

### GitHub device flow (when configured)

```text
git push (to a host with a configured provider, e.g. github.com)
  → git calls: git-credential-klangk get
    → provider resolved from KLANGKWS_FEATURE_OAUTH_PROVIDERS
      (or the github.com shorthand)
    → POST <provider device_code_url> (from container)
    → provider returns device_code, user_code, verification_uri
    → POST /api/v1/browser-delegate { operation: "device_flow_show",
        user_code, verification_uri, host }
    → browser shows code dialog ("Sign in to <host>"), opens the
      provider's authorization page in a popup
    → helper polls POST <provider token_url>
    → user authorizes in popup
    → poll returns access_token
    → POST /api/v1/browser-delegate { operation: "device_flow_done" }
    → browser dismisses code dialog
    → helper prints username=<provider username> / password=<token>
  → git authenticates with the token
  → push succeeds
  → git calls: git-credential-klangk store
    → POST /api/v1/browser-delegate { operation: "store", username, password }
    → feature caches credentials for future requests
```

The poll loop (authorization_pending / slow_down / expired_token /
access_denied) is RFC 8628 standard, so any compliant provider works.

The access token never passes through the backend or browser — it goes
directly from the provider to the container helper to git's stdout.

### PAT dialog fallback

```text
git push (to any host, or github.com without device flow)
  → git calls: git-credential-klangk get
    → POST /api/v1/browser-delegate { operation: "get", host: "..." }
    → browser feature checks cache
      → cache hit: return cached credentials
      → cache miss: show PAT dialog, wait for user
    → browser sends browser_response with credentials
    → helper prints username=.../password=... to stdout
  → git authenticates
  → push succeeds
  → git calls: git-credential-klangk store
    → feature caches credentials
```

If authentication fails, git calls `erase` instead of `store`, and the
feature removes any cached credentials for that host.

## Configuration

The feature declares three config variables in `package.json` (all
`container` scope):

- **`KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID`** — GitHub OAuth App client
  ID, a shorthand that expands to a stock `github.com` provider entry
  (endpoints `https://github.com/login/device/code` and
  `https://github.com/login/oauth/access_token`, scope `repo`, username
  `x-access-token`). No client secret needed.
- **`KLANGKWS_FEATURE_GITLAB_OAUTH_CLIENT_ID`** — the same shorthand for
  `gitlab.com` (endpoints `https://gitlab.com/oauth/authorize_device`
  and `https://gitlab.com/oauth/token`, scope
  `read_repository write_repository`, username `oauth2`). Needs GitLab
  17.1+ with the device flow enabled on the OAuth application.
- **`KLANGKWS_FEATURE_OAUTH_PROVIDERS`** — JSON list of provider entries
  that activates the device flow for any host — self-hosted GitLab,
  other RFC 8628 providers, or overrides of the stock entries. Each
  entry:

  ```json
  {
    "host": "gitlab.example.com",
    "client_id": "abc123",
    "device_code_url": "https://gitlab.example.com/oauth/authorize_device",
    "token_url": "https://gitlab.example.com/oauth/token",
    "scope": "read_repository write_repository",
    "username": "oauth2"
  }
  ```

  Required: `host`, `client_id`, `device_code_url`, `token_url`. Optional:
  `scope` (omitted from the code request when empty) and `username`
  (defaults to `oauth2`; GitHub uses `x-access-token`). Entries must use
  the bare host: matching normalizes the credential host (case, explicit
  port, trailing dot) and tolerates a `www.` prefix on it, but never
  matches by suffix (`github.com.evil.com` is not `github.com`). When a
  shorthand and a map entry both define a provider for the same host,
  the `KLANGKWS_FEATURE_OAUTH_PROVIDERS` entry wins.

All three may be set deploy-wide (server env or the `features_config:`
block of `klangkd.yaml`, short keys `github_oauth_client_id` /
`gitlab_oauth_client_id` / `oauth_providers`), per workspace (the
workspace `env` map), or ad hoc in a shell. See the docs site's
[GitHub Authentication](../../docs/features/github-authentication.md)
page for the walkthrough.

Providers known to support RFC 8628 device flow: GitHub (OAuth Apps only,
not GitHub Apps) and GitLab (17.1+, with the device flow enabled on the
OAuth application). Gitea has no device flow in any release yet
(go-gitea/gitea#27309); Atlassian/Bitbucket has no public device
authorization endpoint. When those change, extend `STOCK_PROVIDERS` in
`tools/git-credential-klangk` — until then such hosts use the PAT dialog.

## Credential cache

The cache is **per-tab** and **in-memory only**:

- Each browser tab has its own `GitCredentialFeature` instance with its
  own cache. Credentials entered in tab A are not available in tab B.
- Refreshing the page clears the cache (new feature instance).
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
3. The bridge routes to tab B's feature, which has an empty cache.
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
