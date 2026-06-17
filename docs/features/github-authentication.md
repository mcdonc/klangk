# GitHub Authentication

Klangk workspaces can authenticate with GitHub for HTTPS git operations
(push, pull, clone of private repos) using a personal access token (PAT).
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

## Generating a GitHub PAT

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

## Using the credential helper

1. Open a workspace in the browser.
2. Open the terminal.
3. Run a git command that requires authentication, e.g.:

   ```sh
   git clone https://github.com/yourname/private-repo.git
   ```

4. A dialog appears in the browser asking for your **username** and
   **personal access token**:

   ![Git credentials dialog](../assets/github-auth/01-credential-dialog.png)

5. Enter your GitHub username and paste the PAT you generated.
6. Click **Authenticate**.
7. Git proceeds with the operation.

On subsequent git operations to the same host, the cached credentials
are reused automatically — no dialog appears. The cache lasts until you
refresh the page or close the tab.

## How it works

When git needs HTTPS credentials, it calls the `git-credential-klangk`
helper installed in the workspace container. The helper:

1. Reads the current browser ID (which identifies your browser tab).
2. Sends a request through the Klangk backend to your browser tab.
3. The browser-side plugin checks its in-memory cache.
4. On a cache miss, the PAT dialog appears.
5. Your credentials are returned to the helper, which passes them to git.
6. After a successful operation, git tells the helper to cache the
   credentials for future use.

The credential cache is **per-tab** and **in-memory only**. Credentials
are not shared between browser tabs, windows, or browsers — each has
its own independent cache:

- Opening a new tab or window starts with an empty cache.
- Refreshing the page clears the cache (you'll be prompted again).
- Closing the tab clears the cache.
- Two users sharing the same workspace each have their own cache.
- Credentials are never written to disk inside the container.

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
HTTPS with PATs is the recommended authentication method. PATs can be
scoped to specific repositories and revoked easily if compromised.

## Troubleshooting

### Dialog doesn't appear

- Make sure the `git-credential` plugin is installed (check that
  `klangk-browser-id` is on PATH inside the container).
- Verify the browser tab has a WebSocket connection (check for
  errors in the browser console).

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
