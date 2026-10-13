# klangk-jwt feature

Retrieves the user's klangk web session JWT from the browser via the
browser-delegate bridge (#3554). Dormant by default — activate with
`KLANGKD_FEATURES_ENABLE=klangk-jwt` (composed with the stock set; see
[Feature Activation](../../docs/features/features.md)).

## What it does

A process in a workspace container asks for the token; the browser tab that
owns the terminal answers with the raw JWT string — the `klangk_jwt`
sessionStorage value. The feature only retrieves the string: it never calls
anything with the token, and klangk's own API refuses that token outside the
browser (DPoP binding, #3218). The expected consumer is a separate system
that chooses to accept the klangk session JWT and applies its own
validation policy.

## Consent

By default the tab asks the user to confirm each retrieval (a dialog naming
what is being shared). A Deny answers the caller with `cancelled`. The
deploy turns the prompt off with:

```text
KLANGKWS_FEATURE_KLANGK_JWT_REQUIRE_CONSENT=false
```

(frontend scope — surfaced to the UI as `klangk_jwt_require_consent` via
`GET /api/v1/config`; also settable under `features_config:` as
`klangk_jwt_require_consent`). With the prompt off, the handler returns the
token to any bridge-authenticated caller in the workspace.

## Components

### Container side

**`klangk-jwt`** (`tools/klangk-jwt`) is a Python script at
`/opt/klangk/features/klangk-jwt/tools/`, symlinked to
`/usr/local/bin/` by `on-image-build.sh`. It reads the browser ID and the
workspace token per invocation (`klangk-browser-id`,
`klangk-workspace-token`), POSTs `{"action": "klangk_jwt", ...}` to the
bridge, and prints the JWT on stdout. It blocks while the user decides
(15-minute budget, like the git-credential waits).

### Browser side

The Flutter plugin (`klangk/lib/feature.dart`) registers the `klangk_jwt`
tool handler: read the token, gate on consent when configured, answer with
the raw string.

### Pi extension

`extension.ts` registers the `klangk_jwt` agent tool, which POSTs the same
bridge action and returns the token text. Note the workspace-side dormancy
caveat (see the feature-activation docs): pi loads every compiled-in
feature's `extension.ts`, so the tool appears in the tool list even when
the feature is dormant — a dormant deploy answers with "Unknown action"
and the tool reports that failure.

## Security notes

- The bridge POST authenticates with the workspace token; a browser ID
  bound to another workspace is rejected (#1715), and the whole bridge can
  be disabled per deploy with `KLANGKD_BROWSER_DELEGATE_ENABLED=false`
  (#2710).
- The token dies with the tab (sessionStorage, #3193) and is refused by
  klangk's own API anywhere but this browser (#3218/#3230).
