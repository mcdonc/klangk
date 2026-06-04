# Plugins: per-platform conditional compilation — plan

Status: draft. Companion to `native-plan.md`. Personal notes, not tracked.

## Goal

Make every klangk Dart plugin compile and run on **native** (macOS/Linux/
Windows) *and* **web** from one source tree, so the native build drops the
`native-plugins-stub/` workaround and `createAllPlugins()` returns the real
plugin set on every target.

## Why this exists

The `macos-native` spike (commit 9ea4b74) found that `flutter build macos`
fails in the Dart kernel step because several plugins import browser-only
libraries unconditionally:

```
.../soliplex/klangk/lib/plugin.dart:1  Error: Dart library 'dart:js_interop' is not available
.../beep/klangk/lib/beep.dart:1        Error: Dart library 'dart:js_interop' is not available
.../web-1.1.1/...                       (cascades from the above)
```

`klangk_plugins` (the generated aggregator) imports every plugin's
`plugin.dart` unconditionally, so one web-only import poisons the whole
native compile. The spike sidesteps this with an empty aggregator; this plan
is the real fix.

## Audit (current state)

Canonical sources: `plugins/<name>/klangk/lib/`. `soliplex` is external
(fetched via `plugins.lock`) and lives only under `$KLANGK_PLUGINS_DIR`.

| Plugin     | Web-only imports                              | Native status | Effort |
|------------|-----------------------------------------------|---------------|--------|
| bobdobbs   | none                                          | compiles      | none   |
| celebrate  | none (pure Flutter `confetti.dart`)           | compiles      | none   |
| beep       | `beep.dart`: `dart:js_interop` (Web Audio)    | broken        | small  |
| itstime    | `plugin.dart`: `dart:js_interop` (DOM video)  | broken        | medium |
| soliplex   | `plugin.dart` + `soliplex_tools.dart`: `dart:js_interop`, `dart:js_interop_unsafe`, `package:web` (localStorage OAuth + JS window globals) | broken | adopt upstream pkgs |

So only **3 of 5** need work; beep/itstime are self-contained effects, and
soliplex is best solved by *adopting* `soliplex_client` rather than porting
the hand-rolled code (see its section).

## The pattern (klangk already uses it)

Mirror `src/frontend/lib/utils/web_helpers_stub.dart` /
`web_helpers_web.dart`: keep `plugin.dart` platform-agnostic (Flutter +
`klangk_plugin_api` only), and push every `dart:js_interop` / `package:web`
call behind a conditional import with two implementations and one shared API:

```dart
// effect.dart — the only file plugin.dart imports for platform bits
export 'effect_stub.dart'
    if (dart.library.js_interop) 'effect_web.dart';
```

- `effect_stub.dart` — native (and VM-test) implementation. No browser imports.
- `effect_web.dart` — current browser implementation (js_interop / package:web).

Rule after this change: **no `plugin.dart` imports `dart:js_interop`,
`dart:js_interop_unsafe`, `dart:html`, or `package:web` directly.** Add a CI
grep that fails if one does (see Phase 4).

## Per-plugin work

### beep (small)
- New `lib/beep_stub.dart`: `void playBeep({double frequency, int durationMs})`
  — native no-op (or `SystemSound.play(SystemSoundType.alert)` for a real
  desktop beep).
- Rename current `beep.dart` → `beep_web.dart` (the js_interop Web Audio code).
- New `beep.dart` = the conditional `export` shim above.
- `plugin.dart` is unchanged (already imports `beep.dart`).

### itstime (medium)
The effect *is* a DOM `<video>` overlay injected via `eval`. Two options:
- **Native-faithful**: implement the overlay as a Flutter widget using
  `video_player` (or `package:media_kit` for desktop) playing the bundled
  `assets/itstime.mp4`, returned from `buildOverlay`. Web keeps the DOM path.
- **Native-degraded (cheaper)**: native stub shows a simple full-screen
  Flutter overlay (image/text) and no video; web keeps the DOM video.
- Either way: split the `_createVideo`/`_destroyVideo` js_interop into
  `itstime_overlay_stub.dart` / `_web.dart`; `plugin.dart` calls the shared
  API and stops importing `dart:js_interop`.
- Recommend native-degraded first (unblocks the build); upgrade to
  `video_player` later if desktop video is wanted.

### soliplex — adopt upstream packages, don't hand-roll (was "large")

**Decision (2026-06-04):** klangk and soliplex are the same team, and the
driving goal is the native *dev loop*, not a merged product. So: **keep
klangk as the host app and consume soliplex's published packages** — do NOT
fork `soliplex/frontend` to embed klangk, and do NOT keep hand-rolling the
soliplex client inside the plugin. (Fork-and-embed was considered and
rejected: it inherits soliplex's native infra but pays a re-skin + perpetual
-fork tax and inverts product ownership; a monorepo merge is overkill for a
dev-loop goal.)

`soliplex/frontend` is itself a multi-platform Flutter app (macos/linux/
windows/ios/android) and already ships the pieces klangk's plugin reinvents:

- **`packages/soliplex_client`** — pure-Dart, transport-injectable client:
  `auth/` (OIDC discovery + `token_refresh_service`), `http/`
  (`authenticated_http_client`, `refreshing_http_client`, `token_refresher`,
  `agui_stream_client` for SSE, `http_transport` abstraction), `api/`
  (`fetch_auth_providers`, `soliplex_api`, AG-UI mappers).
- **`packages/soliplex_client_native`** — `cupertino_http` (URLSession)
  transport for macOS/iOS, xhr for web, behind `create_platform_client_io`
  / `_stub` (the same conditional-import pattern). Native HTTP for free.
- Their app's `pubspec.yaml` shows native auth is already done the right way:
  `flutter_appauth` (RFC 8252: system browser + PKCE + native redirect
  capture) + `flutter_secure_storage` (Keychain). The token-in-query
  popup-poll dance is only soliplex's *web* path.

Because their native app authenticates against the same backend with
`flutter_appauth`, **the server already supports the standard
authorization-code redirect for native clients** — so no loopback-server
workaround and no server-side `return_to` allowlist change is needed.

What the klangk plugin keeps vs. delegates:

| klangk plugin today (web-only, hand-rolled)            | replace with                                   |
|--------------------------------------------------------|------------------------------------------------|
| `SoliplexClient` (listRooms/queryRoom + SSE parsing)   | `soliplex_client` `soliplex_api` + `agui_stream_client` |
| `_getAccessToken` / `_tryRefreshToken` / discovery     | `soliplex_client` `token_refresh_service` + `oidc_discovery` |
| `getAuthSystems` (`/api/login`)                        | `soliplex_client` `fetch_auth_providers`       |
| `localStorage` token store                             | `flutter_secure_storage` (Keychain)            |
| `popupLogin` popup + `popup.location.href` polling     | `flutter_appauth` (system browser, PKCE)       |
| raw `package:http`                                     | injected transport: `cupertino_http` desktop / xhr web |

After this, the plugin shrinks to two klangk-specific things:
1. the **tool-handler wrapper** (`soliplex_list_rooms`/`soliplex_query`
   handlers → `soliplex_api`), and
2. the **auth overlay widget** (the "Connect to Soliplex" UI), now driving
   `flutter_appauth` instead of the popup.

Both lose their `dart:js_interop` / `package:web` imports — the platform code
now lives inside `soliplex_client_native`'s conditional transport — so the
plugin compiles natively with **no klangk-side stub**.

Dependency wiring (same-team git deps, mirroring how soliplex_client pulls
`ag_ui`):

```yaml
dependencies:
  soliplex_client:
    git: { url: https://github.com/soliplex/frontend.git, path: packages/soliplex_client }
  soliplex_client_native:
    git: { url: https://github.com/soliplex/frontend.git, path: packages/soliplex_client_native }
  flutter_appauth: ^12.0.0
  flutter_secure_storage: ^10.3.0
```

soliplex's plugin source is fetched (not in `plugins/`), so this lands in the
**soliplex-side plugin package** + a `plugins.lock` bump. Since it's the same
team, any small gaps in `soliplex_client`'s public surface can be fixed
upstream rather than worked around.

### shared baseUrl fix (applies to all plugins, not just soliplex)
The plugins call `'$baseUrl/api/config'` using `klangk_plugin_api`'s
path-only `baseUrl`, which is hostless on native (the same bug fixed for the
app in commit 9ea4b74, but the fix only covered the frontend, not the
package). **Make `baseUrl` in `klangk_plugin_api` itself env-aware on
native** (consult `KLANGK_BACKEND_URL` when `!kIsWeb`); then the frontend's
`lib/utils/api_base_url.dart` becomes a thin re-export (or is dropped) and
plugins resolve the backend correctly with no per-plugin change.

## Phased plan

### Phase 1 — in-repo plugins (beep, itstime)
- Apply the conditional-import split to `plugins/beep` and `plugins/itstime`.
- bobdobbs / celebrate: no change (add a regression note that they're
  native-clean).
- Verify: `flutter test` (VM, non-web) imports each plugin without throwing;
  `flutter build macos` compiles with these four plugins present.

### Phase 2 — codegen + stub awareness
- `scripts/import_dart_plugins.py` already emits an unconditional
  `createAllPlugins()` — keep it, now that each plugin compiles everywhere.
- Optionally teach it to read a `platforms:` hint from each plugin's
  `pubspec.yaml` (e.g. `klangk: { platforms: [web] }`) and emit a
  conditionally-empty entry for web-only plugins that haven't been ported yet
  (bridges Phase 3 without breaking native).
- `scripts/stub_dart_plugins.sh` stays as the first-checkout/CI fallback;
  no change needed.

### Phase 3 — soliplex (adopt upstream packages)
- Rebuild the soliplex plugin on `soliplex_client` + `soliplex_client_native`
  + `flutter_appauth` + `flutter_secure_storage` per the soliplex section;
  bump `plugins.lock`.
- Land the `klangk_plugin_api` env-aware `baseUrl` fix first (it unblocks
  `_getSoliplexUrl` and any other plugin hitting the backend on native).
- Interim: if soliplex must ship on web before the rebuild, have the
  generator exclude it from the native aggregator (Phase 2 hint) rather than
  stubbing all plugins.

### Phase 4 — guardrail
- CI check (runs in the existing Ubuntu frontend job):
  ```sh
  ! grep -rEl "dart:js_interop|dart:html|package:web" \
      $KLANGK_PLUGINS_DIR/*/klangk/lib/plugin.dart
  ```
  Fails if any plugin's top-level `plugin.dart` reintroduces a browser import.
- Add `flutter build macos` (or `flutter build linux` in CI) over the full
  real plugin set so a future web-only plugin can't silently rebreak native.

## Open questions

1. **Does `soliplex_client`'s public surface cover the two klangk tool
   handlers** (list rooms, query-a-room-via-AG-UI) without reaching into
   `src/`? If not, expose the needed entry points upstream (same team).
2. **`flutter_appauth` config**: issuer / clientId / scopes / redirect
   (custom scheme vs. loopback) — read these from soliplex's own app `lib/`
   once the GitHub API rate limit resets, and reuse verbatim.
3. **itstime native: real video or degraded overlay?** Default: degraded
   first; revisit once a desktop video dep is chosen.
4. **Does anything external call soliplex's web window globals**
   (`soliplexShowTokens`/etc.)? If not, drop them on native entirely rather
   than web-gating (they're debug helpers).
5. **flutter_secure_storage vs. shared_preferences for soliplex tokens?**
   Prefer `flutter_secure_storage` to match soliplex's app (Keychain); note
   klangk's own JWT still uses `shared_preferences` (see `native-plan.md` Q2).
