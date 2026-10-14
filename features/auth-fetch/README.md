# auth-fetch feature

Browser-delegated authenticated fetch for Klangk workspaces. A container
process asks the user's browser to fetch a URL **with the browser's session
credentials**; when the site answers 401 (an nginx `auth_request` /
oauth2-proxy gate), the feature opens the URL itself in a popup so the user
can sign in (passkey, IdP redirect dance — whatever the site's login flow
is), polls the site's stock auth endpoint until the session cookie exists,
then fetches the original URL and returns the page text to the container.

Generalizes the bridge machinery of git-credential (#3385/#3405) to any
authenticated site — no OAuth client, token or git involved: the browser's
session cookie is the credential, and the browser is the HTTP client.

The site needs no klangk-specific pages or endpoints — only stock
nginx/oauth2-proxy configuration plus two standard settings that let a
cross-origin browser fetch work (see [Site requirements](#site-requirements)).
Admit the target's origin in the klangk frontend CSP with the
csp_connect_origins setting (`KLANGKD_CSP_CONNECT_ORIGINS` env or the
`csp_connect_origins:` key in `klangkd.yaml`, reloadable on SIGHUP) —
the feature declares no config key of its own.

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
`<scheme>://<host>:<port>/oauth2/auth` — the oauth2-proxy auth endpoint);
`--timeout` bounds the whole interaction (default 900s).

An agent-facing tool (`auth_fetch`) is registered by `extension.ts` with
the same behavior, mirroring browser-fetch.

### Browser side

**`AuthFetchFeature`** (`klangk/lib/feature.dart`) registers the
`auth_fetch` bridge action:

- **`get`** — fetch the URL with credentials. 200 → answer immediately.
  401 → open the URL itself in a popup (the site's own login flow runs
  there; after it completes the browser sits on the requested page),
  show a dialog ("waiting for sign-in", with a re-open button and
  cancel), poll the auth probe every 2s; on 2xx fetch the original URL
  once more and answer with `{status, body, authenticated: true}`.
- **`cancel`** — dismiss the pending fetch (answers `cancelled`).

Only one fetch may be pending at a time (busy error). A new workspace tab
(session) starts with no cookies, so each tab signs in once per site.

## Site requirements

The target site is expected to be nginx gated by oauth2-proxy
(`auth_request`), with the standard nginx integration — nothing
klangk-branded. Beyond stock, two settings are required because the klangk
web app is a _different site_ from the target and its background fetches
must carry and read cross-site responses:

- **CORS on the gated vhost** — `Access-Control-Allow-Origin` set to the
  klangk origin, `Access-Control-Allow-Credentials: true` (plus a GET/OPTIONS
  method list; serve `204` on `OPTIONS` so preflights bypass auth). Without
  it the browser refuses the tab's credentialed fetch and the feature
  reports "probe unreachable".
- **`SameSite=None` session cookie** — oauth2-proxy started with
  `--cookie-samesite=none` (cookies are already `Secure` behind HTTPS).
  The default `Lax` cookie never rides a cross-site fetch, so the probe
  would keep answering 401 even after a successful popup login.

The probe itself is stock: `GET /oauth2/auth` (the endpoint every
canonical nginx `auth_request` integration already proxies) answers plain
`401` while unauthenticated and `2xx` once the session cookie is accepted
— statuses a background fetch can read. `--probe` overrides it for sites
gated differently.

## Debugging

Set `AUTH_FETCH_KLANGK_DEBUG=1` in the container to see the CLI's bridge
traffic (redacted) on stderr.
