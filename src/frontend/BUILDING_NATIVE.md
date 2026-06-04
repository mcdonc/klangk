# Building klangk natively (macOS) — experiment notes

Status: spike on branch `macos-native`. Validates the Phase 0 + Phase 3 path
in `scratch/native-plan.md`. Not yet production-ready (see "Known gaps").

## Toolchain — run from a SYSTEM shell, not devenv

Per the plan's constraint, native builds use the system Apple toolchain, NOT
devenv. devenv pollutes `SDKROOT` / `DEVELOPER_DIR` and breaks the
`objective_c` build hook (`xcrun --show-sdk-path`).

Verified working here:
- Flutter 3.44.0 / Dart 3.12.0 (`/opt/homebrew/bin/flutter`)
- Xcode 26.5

Run everything from `/bin/zsh` or `/bin/bash` with a clean env, e.g.:

```sh
env -i HOME="$HOME" PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  TERM=xterm LANG=en_US.UTF-8 bash
cd src/frontend
```

## One-time scaffold

```sh
flutter create --platforms=macos --project-name klangk_frontend .
```

This created `macos/` (Runner Xcode project) plus some template files
(`README.md`, `analysis_options.yaml`, `.metadata`, `.idea/`, and a default
`test/widget_test.dart` — the last one was deleted; it tests a nonexistent
`MyApp` counter template and would break `flutter test`).

### Entitlements (required)

The app is a network *client* (HTTP + WebSocket to the backend). The sandbox
blocks outbound connections without `com.apple.security.network.client`. Added
to both `macos/Runner/DebugProfile.entitlements` and `Release.entitlements`.

## Build & run

```sh
# Build
flutter build macos --debug \
  --dart-define=KLANGK_BACKEND_URL=http://localhost:8997

# Run against a backend on :8997
flutter run -d macos \
  --dart-define=KLANGK_BACKEND_URL=http://localhost:8997
```

NOTE the port: the backend listens on **8997** (`devenv.nix:111`,
`KLANGK_PORT = "8997"`), not `18997` as the draft plan stated. The
`apiBaseUrl` default in `lib/utils/api_base_url.dart` should be revisited to
match (or always be passed via `--dart-define`).

### Linux

`linux/` is scaffolded (GTK/CMake). Build/run on a Linux host (Flutter
desktop does not cross-compile, so it can't be built from macOS):

```sh
flutter run -d linux \
  --dart-define=KLANGK_BACKEND_URL=http://localhost:8997
```

Toolchain: GTK3 dev libs, `clang`, `cmake`, `ninja`, `pkg-config`. The
libghostty native lib is **fetched prebuilt** for x86_64/aarch64 glibc (and
musl) from the libghostty GitHub releases — no Zig compile needed (verified
via `asset_hashes.dart`: `libghostty-x86_64-linux-gnu.so` etc.). If a target
has no prebuilt, install Zig and pass `--define=libghostty.download=compile`.
Not yet built/verified on a Linux host.

## Backend for local dev

The backend stays in devenv. On a clean shell it can also be run via uv:

```sh
cd src/backend
uv run uvicorn klangk_backend.main:app --host 127.0.0.1 --port 8997
```

It auto-creates an admin user on first start (printed to the log).

## Integration tests (the dev-loop payoff)

`integration_test/` runs widget tests inside a real Flutter engine on the
device, so terminal behaviors that need a real desktop process (libghostty
FFI VT, genuine rendering/layout, font loading, paste) are assertable —
unlike the headless `flutter test` VM.

```sh
flutter test -d macos integration_test/ghostty_terminal_test.dart
# or the whole dir:
flutter test -d macos integration_test/
```

On Linux/CI, wrap in a virtual display:

```sh
xvfb-run -a -s '-screen 0 1280x1024x24 +extension GLX' \
  flutter test -d linux integration_test/
```

Gotcha (already handled in the test): `GhosttyTerminal._loadFont` calls
`FontLoader.load()` at runtime, which fires a "system fonts changed" platform
message. On a real engine that can land mid-frame and trip a framework
assertion (`RenderParagraph._scheduleSystemFontsUpdate` requires the idle
phase). It's benign and never fires in the VM suite; the integration test
installs a narrow `FlutterError.onError` filter (`_ignoreSystemFontsAssert`)
that swallows exactly that one and forwards everything else.

## What was verified

- `flutter pub get`, `flutter analyze` (no errors), `flutter test` (335 pass)
  all run outside devenv with the system toolchain.
- `flutter build macos --debug` produces `klangk_frontend.app` with the
  `network.client` entitlement baked in — the libghostty FFI native build
  links cleanly.
- The launched native app reaches the backend: `GET /api/config` → `200`,
  confirming the env-aware URL + sandbox network entitlement work end to end.
- `flutter test -d macos integration_test/ghostty_terminal_test.dart` — all 11
  GhosttyTerminal tests pass in a real macOS engine (render, output, resize,
  right-click paste menu, focused paste routing, plus issue #7 font zoom via
  methods + Cmd/Ctrl shortcuts, and Shift+PgUp/PgDown scrollback paging).

## Known gaps (must fix before this is real)

1. **Bundled plugins are web-only.** `klangk_plugins` aggregates
   `beep` / `celebrate` / `soliplex`, which import `dart:js_interop` and
   `package:web` *unconditionally* (e.g. `beep/klangk/lib/beep.dart:1`). They
   cannot compile for a non-web target and break the kernel build. For this
   spike, `pubspec_overrides.yaml` points `klangk_plugins` at a native-safe
   empty aggregator (`../../native-plugins-stub`, returns `[]`).
   **Real fix:** give each plugin a conditional-import split (web impl vs. a
   native no-op/stub), or generate a native-safe aggregator. This belongs in
   Phase 0 — the plan's "conditional imports everywhere" did not cover the
   plugin packages.
2. **Backend port default (18997 vs 8997)** — see above.
3. **`pubspec_overrides.yaml` is gitignored** and currently points at the
   spike stub. A committed native target needs a non-devenv way to resolve
   `klangk_plugins` (the real, native-safe one).
