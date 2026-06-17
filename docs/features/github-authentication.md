# GitHub Authentication

Klangk workspaces can authenticate with GitHub for HTTPS git operations
(push, pull, clone of private repos). Two methods are available:

- **Sign in with GitHub** (recommended) — one-click OAuth device flow,
  no token management required. Requires admin configuration.
- **Personal access token (PAT)** — manual token entry, always available
  as a fallback.

When git needs credentials, a dialog appears in your browser tab — no
need to paste tokens into the terminal.

## Setup

The `git-credential` plugin must be included in your plugins list. If
you're using the default plugin set, it's already there. Otherwise, add
it to your `plugins.yaml`:

```yaml
plugins:
  - name: git-credential
    git: git@github.com:mcdonc/klangk.git
    path: plugins/git-credential
    ref: main
```

Then run `update-plugins` and rebuild the workspace image (`devenv up`
will do both automatically).

## Sign in with GitHub (recommended)

When the admin has configured GitHub OAuth (see
[Admin setup](#admin-setup-creating-a-github-oauth-app) below), the
credential dialog shows a **Sign in with GitHub** button:

![Credential dialog with GitHub sign-in](../assets/github-auth/02-credential-dialog-with-github.png)

### How it works

1. Open a workspace and run a git command that requires authentication:

   ```sh
   git push
   ```

2. A dialog appears with a **Sign in with GitHub** button. Click it.

3. The dialog displays a one-time code and a link to GitHub:

   ![Device flow code entry](../assets/github-auth/03-device-flow-code.png)

4. Open the GitHub link in a new tab, enter the code, and authorize the
   app.

5. The dialog detects authorization automatically and git proceeds. No
   token to copy or manage.

The OAuth token is cached in memory for the browser session. Subsequent
git operations reuse it without prompting.

### Scopes

The device flow requests the `repo` scope, which grants read/write
access to repositories you can access on GitHub. The token is scoped to
the OAuth App — it cannot access organization resources unless the
organization has approved the app.

## Using a personal access token

If GitHub OAuth is not configured, or if you prefer to manage tokens
manually, the dialog shows username and PAT fields:

![Git credentials dialog](../assets/github-auth/01-credential-dialog.png)

### Generating a GitHub PAT

The credential helper needs a **fine-grained personal access token**
with repository access. To create one:

1. Go to <https://github.com/settings/tokens?type=beta> (or navigate to
   **Settings > Developer settings > Personal access tokens > Fine-grained tokens**).
2. Click **Generate new token**.
3. Give it a descriptive name (e.g. "Klangk workspace").
4. Set an expiration. GitHub allows up to 1 year.
5. Under **Repository access**, choose either:
   - **All repositories** — if you want to push/pull from any repo.
   - **Only select repositories** — pick the specific repos you need.
6. Under **Permissions > Repository permissions**, grant:
   - **Contents**: Read and write (required for push/pull).
   - **Metadata**: Read-only (required by GitHub for all fine-grained tokens).
7. Click **Generate token**.
8. **Copy the token immediately** — GitHub will not show it again.

The token starts with `github_pat_` (fine-grained) or `ghp_` (classic).
Both formats work. Classic tokens also work but fine-grained tokens are
recommended because they can be scoped to specific repositories.

### Using the PAT

1. Open a workspace and run a git command that requires authentication.
2. Enter your GitHub username and paste the PAT.
3. Click **Authenticate**.

On subsequent git operations to the same host, the cached credentials
are reused automatically — no dialog appears. The cache lasts until you
refresh the page or close the tab.

## Admin setup: creating a GitHub OAuth App

To enable "Sign in with GitHub" for your Klangk instance, you need to
create a GitHub OAuth App and set one environment variable.

1. Go to **GitHub > Settings > Developer settings > OAuth Apps**.
2. Click **New OAuth App** (or **Register a new application**).
3. Fill in the form:
   - **Application name**: e.g. "Klangk — My Instance"
   - **Homepage URL**: your Klangk instance URL (e.g.
     `https://klangk.example.com`)
   - **Authorization callback URL**: use your instance URL (e.g.
     `https://klangk.example.com`). The device flow does not use
     redirects, but GitHub requires this field.
4. Check **Enable Device Flow** on the registration form.
5. Click **Register application**.
6. Copy the **Client ID** (you do not need the client secret — the
   device flow is designed for public clients).
7. Set the environment variable in your deployment:

   ```sh
   KLANGK_GITHUB_OAUTH_CLIENT_ID=Ov23li...
   ```

8. Restart Klangk. The "Sign in with GitHub" button will now appear in
   the credential dialog.

**Important**: this must be an **OAuth App**, not a GitHub App. The
device authorization grant is only available on OAuth Apps.

If `KLANGK_GITHUB_OAUTH_CLIENT_ID` is not set, the "Sign in with
GitHub" button is hidden and only the PAT form is shown.

## Credential cache

The cache is **per-tab** and **in-memory only**:

- Each browser tab has its own `GitCredentialPlugin` instance with its
  own cache. Credentials entered in tab A are not available in tab B.
- Refreshing the page clears the cache (new plugin instance).
- Closing the tab clears the cache.
- The cache is keyed by `protocol://host` (e.g. `https://github.com`).

## Multiple browser tabs

If you have two browser tabs open on the same workspace, the credential
dialog appears in whichever tab you most recently clicked into. Both
tabs share the same terminal session, but each maintains its own
credential cache. Switching tabs and running git will prompt for
credentials again if that tab's cache is empty.

## SSH alternative

If you prefer SSH authentication over HTTPS, you can configure your git
remotes to use SSH URLs (`git@github.com:...`) instead. The credential
helper only activates for HTTPS URLs.

Note that there is currently no way to keep your GitHub private key
secure in a Klangk instance — any SSH key placed in the container is
accessible to anyone with access to the workspace. For this reason,
HTTPS with PATs or OAuth is the recommended authentication method.

## Troubleshooting

### Dialog doesn't appear

- Make sure the `git-credential` plugin is installed (check that
  `klangk-browser-id` is on PATH inside the container).
- Verify the browser tab has a WebSocket connection (check for
  errors in the browser console).

### "Sign in with GitHub" button not shown

- Verify `KLANGK_GITHUB_OAUTH_CLIENT_ID` is set in the environment.
- Check that the OAuth App has **Enable Device Flow** turned on.
- Restart Klangk after setting the variable.

### Device flow code expired

The code is valid for 15 minutes. If it expires before you authorize,
click **Try again** to get a new code.

### Credentials rejected

- Verify your PAT hasn't expired.
- Check that the token has **Contents: Read and write** permission.
- For fine-grained tokens, verify the target repository is included.
- Try generating a new token.

### Debug output

Run with debug logging to see the credential helper's activity:

```sh
export GIT_CREDENTIAL_KLANGK_DEBUG=1
git push
```

This prints the browser ID, the bridge request/response, and any
errors to stderr.
