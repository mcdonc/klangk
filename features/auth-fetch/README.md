# auth-fetch feature

Browser-delegated authenticated fetch for Klangk workspaces. A container
process asks the user's browser to fetch a URL **with the browser's session
credentials**; when the site answers 401 (e.g. an nginx `auth_request` /
oauth2-proxy gate), the feature opens the URL in a popup so the user can
sign in (passkey, IdP redirect dance — whatever the site's login flow is),
polls a headless auth probe until the session cookie exists, then fetches
the original URL and returns the page text to the container.

Generalizes the bridge machinery of git-credential (#3385/#3405) to any
authenticated site — no OAuth client, token or git involved: the browser's
session cookie is the credential, and the browser is the HTTP client.

## Components

### Container side

**`klangk-auth-fetch`** (`tools/klangk-auth-fetch`) is symlinked to
`/usr/local/bin/` by `on-image-build.sh`:

    klangk-auth-fetch https://host:9443/page          # prints the page text
    klangk-auth-fetch https://host:9443/page --json   # {"status", "body", ...}

It POSTs `{action: "auth_fetch", operation: "get", url}` to the
browser-delegate bridge and prints the page body (or `--json` for the full
result envelope) when the browser answers. Exits 1 on error, cancellation
or timeout. `--probe` overrides the auth-probe URL (default:
`<scheme>://<host>:<port>/auth-fetch` — the convention the companion nginx
role serves); `--timeout` bounds the whole interaction (default 900s).

An agent-facing tool (`auth_fetch`) is registered by `extension.ts` with
the same behavior, mirroring browser-fetch.

### Browser side

**`AuthFetchFeature`** (`klangk/lib/feature.dart`) registers the
`auth_fetch` bridge action:

- **`get`** — fetch the URL with credentials. 200 → answer immediately.
  401 → open the URL in a popup (the site's own login flow runs there),
  show a dialog ("waiting for sign-in", with a re-open button and cancel),
  poll the auth probe every 2s; on 200 fetch the original URL once more
  and answer with `{status, body, authenticated: true}` plus the probe's
  identity text when the site supplies one.
- **`cancel`** — dismiss the pending fetch (answers `cancelled`).

Only one fetch may be pending at a time (busy error). A new workspace tab
(session) starts with no cookies, so each tab signs in once per site.

## Site requirements

The target site must serve, alongside its login-gated pages:

- **`GET /auth-fetch`** — the headless probe: plain **401** while
  unauthenticated (no redirect — background fetches must be able to read
  the status), 200 with a body once the session cookie is accepted. The
  companion NixOS role (`pocket-id-sso`) serves exactly this behind
  oauth2-proxy, including CORS (`Access-Control-Allow-Origin` for the
  klangk origin, `Access-Control-Allow-Credentials: true`, OPTIONS
  preflight bypass) and a `SameSite=None` session cookie — required
  because the klangk origin (e.g. `http://localhost:8997`) is cross-site
  from the target host.

## Debugging

Set `AUTH_FETCH_KLANGK_DEBUG=1` in the container to see the CLI's bridge
traffic (redacted) on stderr.
