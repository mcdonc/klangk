# Gitea

Authenticate `git clone`, `git pull`, and `git push` from workspace
containers against [Gitea](https://about.gitea.com) (and Forgejo) hosts
through your browser — no personal access token to create or paste
(#3385). The first `git` operation opens an authorization window; you
approve it once, and the session keeps working, refreshing its token on
its own.

Gitea implements the OAuth authorization-code grant with PKCE (S256) —
the browser flow below — and no device flow, so the
[GitHub device flow](features/github-authentication.md) does not apply
to Gitea hosts.

## What you need

- A Gitea or Forgejo instance you can reach from your browser (Gitea
  1.22 or newer for the PKCE behavior described here).
- The **public origin of your klangk deployment** — the address your
  browser uses, e.g. `https://klangk.example.com`. The authorization
  popup returns to this origin.
- Any user account on the Gitea instance. Registering the OAuth
  application is a user-level settings page, so a regular account is
  enough; an instance administrator is not required.
- A Gitea instance that answers on an address workspace containers can
  reach. A Gitea running on the same machine as klangkd binds a real
  interface address (`0.0.0.0` or the host's LAN address): workspace
  containers reach the host through a gateway that maps onto its
  non-loopback side, so a listener bound to `127.0.0.1` refuses their
  connections. Workspaces reach the Gitea address through the normal
  egress path, so it is subject to the same consent or allow-list
  rules as any other destination.

## Register the OAuth application in Gitea

1. Sign in to your Gitea instance and open your avatar menu
   (top right) → **Settings**.
2. Open the **Applications** tab and scroll to **Manage OAuth2
   Applications** (the _OAuth2 Apps_ section, not Access Tokens).
3. Click **Create Application** and fill in:
   - **Application name** — anything you like, e.g. `klangk`.
   - **Redirect URI** — your klangk origin exactly as the browser
     reaches it, e.g. `https://klangk.example.com/`. The authorization
     popup lands back on this origin; the trailing slash is part of the
     URI, so keep it consistent with what you configure below.
   - **Confidential** — leave **off**. klangk acts as a public client
     (PKCE carries the proof); a confidential application fails the
     PKCE challenge on Gitea 1.22 and newer.
   - **Scopes** — leave the application unrestricted for git over
     HTTPS. Gitea's granular scopes apply to its `/api/v1` routes and
     do not cover the git smart-HTTP endpoints, so restricting the
     scopes would only exclude git access.
4. Click **Create Application** and copy the **Client ID** shown on the
   application's page. A public client has no client secret — none is
   needed.

Behind the scenes this registers an OAuth application under
`/user/settings/applications`; the Gitea admin panel under
`Site Administration → Integrations → OAuth2 Applications` lists the
same applications, but you do not need it to create one.

## Configure klangk

The shorthand covers a deployment that clones from one Gitea: set a
bare client ID plus the klangk origin you registered as the redirect.
Deploy-wide via the YAML config — the `features_config` block maps the
feature's environment keys to plain strings:

```yaml
features_config:
  KLANGKWS_FEATURE_GITEA_OAUTH_CLIENT_ID: "<the Client ID from the step above>"
  KLANGKWS_FEATURE_GITEA_OAUTH_REDIRECT_URI: "https://klangk.example.com/"
```

Or as environment variables on the server:

```sh
KLANGKWS_FEATURE_GITEA_OAUTH_CLIENT_ID="<the Client ID>"
KLANGKWS_FEATURE_GITEA_OAUTH_REDIRECT_URI="https://klangk.example.com/"
```

The redirect is set separately from the client ID because it names the
klangk origin — the address the browser returns the authorization popup
to — which no Gitea-side value carries. With both keys set, the first
clone from your Gitea runs the browser flow below; the authorize and
token endpoints are derived from the host being cloned (Gitea serves
them at `/login/oauth/authorize` and `/login/oauth/access_token`). The
block keys can also be the stripped, lowercased short forms
(`gitea_oauth_client_id:` / `gitea_oauth_redirect_uri:`); env wins per
key.

The shorthand serves one Gitea — the instance the client ID was
registered on. Cloning from several instances, or from a Gitea the
browser and the workspace containers reach by different names (the
authorize URL opens in the browser, the token exchange runs in the
container, and the shorthand derives both from the clone host), needs a
provider entry per host instead — the form below also stays available
for a Gitea served under a path prefix or behind nonstandard endpoints.
An explicit `KLANGKWS_FEATURE_OAUTH_PROVIDERS` entry whose `host`
matches the remote always wins over the shorthand. A client ID set
without the redirect leaves the shorthand inactive: the terminal falls
back to the PAT dialog, and `GIT_CREDENTIAL_KLANGK_DEBUG=1` in the
workspace prints a line naming the missing pair.

### The provider entry form

A provider entry spells every field out. Deploy-wide via the YAML
config — the `features_config` block maps the feature's environment
key to the JSON list, kept as one string:

```yaml
features_config:
  KLANGKWS_FEATURE_OAUTH_PROVIDERS: '[{"host": "git.example.com", "flow": "authorization_code_pkce", "client_id": "<the Client ID from the step above>", "authorize_url": "https://git.example.com/login/oauth/authorize", "token_url": "https://git.example.com/login/oauth/access_token", "redirect_uri": "https://klangk.example.com/"}]'
```

Or as an environment variable on the server (the same JSON list,
without the quoting the YAML form needs):

```sh
KLANGKWS_FEATURE_OAUTH_PROVIDERS='[
  {
    "host": "git.example.com",
    "flow": "authorization_code_pkce",
    "client_id": "<the Client ID>",
    "authorize_url": "https://git.example.com/login/oauth/authorize",
    "token_url": "https://git.example.com/login/oauth/access_token",
    "redirect_uri": "https://klangk.example.com/"
  }
]'
```

The `redirect_uri` is the same origin you registered in Gitea. The
block key can also be the stripped, lowercased short form
(`oauth_providers:`) with the same JSON-string value — the form the
[environment reference](reference/environment.md) lists per key.

A Forgejo instance uses the same endpoint paths.

## The first clone

```sh
git clone https://git.example.com/my-org/my-repo.git
```

1. The workspace asks your browser tab: a dialog names the host and an
   authorization window opens at the Gitea sign-in/approval page.
2. Sign in (if the popup did not inherit your session) and click
   **Authorize Application**.
3. The popup closes; the dialog in the workspace follows on its own,
   and the clone proceeds. The username reported to git is `oauth2`
   with the access token as the password — nothing to type.

Every later `git` operation in that tab session reuses the token from
the browser tab's in-memory cache and refreshes it headlessly before it
expires (Gitea's default access-token lifetime is one hour), so the
authorization window appears once per tab session. The cache lives in
the tab's memory: closing or reloading the tab clears it, and the next
clone opens the authorization window again. Cancelling the dialog, or
denying the application on Gitea's approval page, fails the git
operation: git tries any other configured credential helper, then
reports a fatal authentication error.

## What happens under the hood

- The helper inside the container fetches the host's OIDC discovery
  document and confirms the server offers `authorization_code` with
  S256; the provider entry's `flow` field activates the browser flow.
- The token exchange and every refresh run from the workspace container
  straight to `<your Gitea host>` — the same origin the clone itself
  uses — so the flow opens no new outbound destinations. The browser
  performs no HTTP on the container's behalf.
- The authorization code is single-use and useless without the PKCE
  verifier, which never enters the browser relay (it is sent only to
  the token endpoint — that exchange is PKCE); the `state` value binds
  the response to the flow that started it.

See [GitHub HTTPS Authentication](features/github-authentication.md)
for the provider-map reference (per-flow fields, host matching) and the
GitHub/GitLab device flows.
