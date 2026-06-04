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

## Backend for local dev

The backend stays in devenv. On a clean shell it can also be run via uv:

```sh
cd src/backend
uv run uvicorn klangk_backend.main:app --host 127.0.0.1 --port 8997
```

It auto-creates an admin user on first start (printed to the log).

## What was verified

- `flutter pub get`, `flutter analyze` (no errors), `flutter test` (335 pass)
  all run outside devenv with the system toolchain.
- `flutter build macos --debug` produces `klangk_frontend.app` with the
  `network.client` entitlement baked in — the libghostty FFI native build
  links cleanly.
- The launched native app reaches the backend: `GET /api/config` → `200`,
  confirming the env-aware URL + sandbox network entitlement work end to end.

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
