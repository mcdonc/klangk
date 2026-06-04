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
| soliplex   | `plugin.dart` + `soliplex_tools.dart`: `dart:js_interop`, `dart:js_interop_unsafe`, `package:web` (localStorage OAuth + JS window globals) | broken | large |

So only **3 of 5** need work, and 2 of those are self-contained effects.

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

### soliplex (large)
Two distinct web couplings:
1. **OAuth token storage** in `localStorage` (`soliplex_tools.dart`). Replace
   with a platform-neutral store: `shared_preferences` works on web *and*
   desktop and is already a frontend dep. Define a small `TokenStore`
   interface; one `shared_preferences` impl serves all platforms (preferred —
   removes the split entirely), or keep `localStorage` on web behind a
   conditional if exact web-storage parity matters.
2. **JS window globals** (`plugin.dart` registers `soliplexClearTokens`,
   `soliplexShowTokens`, `soliplexVersion` on `window` via
   `js_interop_unsafe`). These exist so external/host JS can call into the
   plugin — meaningless on desktop. Move registration into
   `soliplex_bridge_stub.dart` (native no-op) / `_web.dart` (current
   behavior). `plugin.dart` calls `registerSoliplexBridge()` and drops the
   js_interop imports.
- Since soliplex is fetched (not in `plugins/`), this change lands in the
  **upstream soliplex plugin repo**, then a `plugins.lock` bump. Coordinate
  with whoever owns it; until then soliplex stays excluded from native via
  the aggregator (see Phase 3).

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

### Phase 3 — soliplex (external)
- Port soliplex upstream per the "large" section, then bump `plugins.lock`.
- Interim: if soliplex must ship on web before the port, have the generator
  exclude it from the native aggregator (Phase 2 hint) rather than stubbing
  all plugins.

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

1. **shared_preferences vs. keep localStorage on web for soliplex?** Default:
   single `shared_preferences` store everywhere — simpler, and tokens already
   aren't expected to be shared with host JS on desktop. Confirm no external
   web consumer depends on the exact `localStorage` keys.
2. **itstime native: real video or degraded overlay?** Default: degraded
   first; revisit once a desktop video dep is chosen.
3. **Does the host page actually call soliplex's window globals?** If nothing
   external calls `soliplexShowTokens`/etc., they can be dropped entirely
   rather than web-gated.
