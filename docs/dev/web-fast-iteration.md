# Faster web-frontend iteration in klangk

Status: WIP on branch `feat/flutter-web-dev-mode`. Author: autonomous session, 2026-06-23.

## The problem

Day-to-day we start the stack with:

```bash
devenv processes up --no-tui     # daemon, reachable at http://localhost:8995/
```

On every start, two heavy build steps run before the `backend`/`nginx` processes
come up (devenv.nix `tasks`):

1. **`klangk:flutter-build`** → `scripts/flutterbuildweb.sh` → a full
   `flutter build web --release` (dart2js AOT compile, `--no-minify-js`,
   `--source-maps` + a ~25 MB source-map inlining pass). It re-runs whenever any
   of these change (`execIfModified`):
   - `scripts/flutterbuildweb.sh`
   - `src/frontend/lib/**`
   - `src/frontend/web/**`
   - `src/frontend/pubspec.yaml` / `pubspec.lock`
   - `$KLANGK_PLUGINS_DIR/**/*.dart`, `plugins.lock`
2. **`klangk:build-workspace-image`** → `scripts/build-workspace-image.sh` → a
   `podman build` of the workspace image where `pi`, `claude code`, and the
   `hermes` agent run. Hash-gated over `src/containers/workspace/` + the plugins
   dir, so it _skips_ when nothing changed — but the first build, and any
   extension/tool/hook edit, pays the full image build.

The pain: a one-line Dart UI edit (`lib/**`) invalidates step 1 and forces a
**full release recompile** before you can see the change. Flutter supports hot
reload / hot restart, but **only through `flutter run`** (the DDC dev compiler) —
the `flutter build web --release` path has no incremental/dev server, so we never
get it.

## Why hot reload isn't "just turn it on"

`pi` (and `claude code` / `hermes`) run _inside_ the workspace container and their
extensions call back **through the Flutter app** via the
`/api/v1/browser-delegate` bridge (nginx `:8995` → backend `:8997` → the frontend
served at the nginx origin). So the dev server can't live on some random origin —
it has to stay **same-origin** with the bridge or the container→frontend path
breaks.

Key enabling fact (`src/frontend/lib/ws/ws_client.dart:28,157`): the frontend
derives **both** its WebSocket URL (`{scheme}://{host}:{port}{base}/ws`) and its
HTTP API base from `Uri.base` — i.e. _whatever origin the browser loaded from_.
The backend mounts the built assets as a `StaticFiles` catch-all at `/` and the
API under `/api/v1` (`main.py:326,359`), WS at `/ws` (`main.py:332`).

**Consequence:** if the browser keeps loading from nginx `:8995`, every API/WS/
bridge call still targets `:8995`. We only need nginx to fork **by path** in dev
mode:

```text
/api/   + /ws   ->  backend   127.0.0.1:8997   (unchanged behaviour)
/  + assets + DWDS ->  flutter run dev server   127.0.0.1:$KLANGK_WEB_DEV_PORT
```

That keeps the pi/bridge flow byte-for-byte identical to today (it never touches
the dev server), which is exactly the "layered" scope we chose: hot reload for
everyday UI work, release build still the path used to validate the
pi/extension/bridge flow.

## Six representative change scenarios

| #          | Change                                              | Today (`devenv up` release path)                                                            | With Strategy A (dev server)                                                               |
| ---------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 1          | Tweak a widget (color/padding) in `lib/**`          | Full `flutter build web --release` + map inline, then reload                                | `r`/`R` → DDC incremental recompile of changed module (seconds); hot reload may keep state |
| 2          | Change provider/state logic in `lib/**`             | Full release rebuild + state lost                                                           | Hot restart (`R`) recompiles incrementally; state reset only                               |
| 3          | Add a new Dart file / `go_router` route             | Full release rebuild                                                                        | Hot restart picks it up; new top-level classes need restart not reload                     |
| 4          | `pubspec.yaml` dependency add/bump                  | `flutter pub get` + full rebuild                                                            | Must stop/restart `flutter run` (pub get + cold start) — no hot path either way            |
| 5          | Plugin set change (`plugins.yaml` / plugin `.dart`) | update-plugins + import + **cache wipe** (`rm -rf .dart_tool/flutter_build`) + full rebuild | Restart `flutter run` + re-import; cache wipe defeats incrementality regardless            |
| 6          | `web/` asset / `index.html` / manifest              | Full release rebuild + cache-bust `sed`                                                     | Static assets served live; `index.html` template change needs restart                      |
| (baseline) | Backend-only Python change                          | Not in `execIfModified` → no flutter rebuild; uvicorn reload                                | identical                                                                                  |

Takeaway: scenarios **1–3** (the overwhelming majority of frontend work) go from
a full release compile to a seconds-long incremental recompile. **4–6** are
inherently cold-ish in both modes, so the dev server's value there is smaller —
honesty matters: we are not claiming hot reload for dependency or plugin-set
changes.

## Strategy menu (big picture)

Ordered roughly by value-to-effort. **A is the concrete foundation**; the rest are
experiments to layer on.

### A — Layered, opt-in hot-reload dev server ★ foundation

- New `scripts/flutterdevweb.sh`: `flutter run -d web-server
--web-port=$KLANGK_WEB_DEV_PORT --web-hostname=127.0.0.1` (DDC, incremental).
- `scripts/nginx.sh` grows a **dev profile** under `KLANGK_WEB_DEV=1` that routes
  `/api/` + `/ws` → backend and everything else → the dev server.
- `devenv.nix`: in dev mode, **skip** `klangk:flutter-build` (the dev server owns
  compilation) and expose a `flutterdevweb` script (+ optionally a `frontend-dev`
  process).
- Default `devenv processes up --no-tui` is **unchanged** (release build).
- Trigger reload: press `r`/`R` in the `flutter run` console (Strategy B removes
  the keypress).

### B — Auto-reload watcher (revised after measurement)

Goal: no manual keypress / manual refresh. Two variants:

- **Browser-reload watcher (extension-free, preferred):** watch
  `src/frontend/lib/**`; on save, tell the open tab at :8995 to reload (e.g. a
  tiny injected websocket livereload client, or driving Chrome via the DevTools
  Protocol). Reload triggers the incremental DDC recompile — the ~9 s path that
  needs no extension.
- **`flutter run --machine` + `app.restart`:** true hot restart with no keypress,
  but on the web-server device it still needs the Dart Debug Extension connected
  (same limitation as pressing `R`). Adopt once the extension is standard in the
  dev setup.
  Build only after A is proven (it is).

### C — Faster release build (for those who keep the build-then-serve model)

`flutter build web` is always dart2js (no DDC), so it can't be incremental, but we
can offer a "fast build" variant: drop `--source-maps` (skips the 25 MB
map-inlining pass) and tune `--minify`. Minor win vs A; useful for CI smoke /
quick prod-like checks.

### D — Decouple the two `devenv up` build steps

Frontend iteration shouldn't trigger a workspace image rebuild and vice-versa.
Make dev mode short-circuit `klangk:flutter-build`, and document the existing
hash-gates so people understand when `build-workspace-image` actually re-runs.

### F — Bind-mount extensions instead of COPY-at-build (pi / claude-code / hermes speed)

`build-workspace-image.sh` stages plugin `extension.ts` / `tools` / `hooks` into
build-contexts and `COPY`s them into the image — so editing an extension forces a
full image rebuild. If we instead **bind-mount** the staged plugin dir into the
running container, extension/tool/hook edits take effect on container restart with
**no image rebuild**. This is the container-side analogue of hot reload and the
main lever for fast `pi`/`hermes` iteration.

### E — (declined) Replace the default with hot reload

Recorded for completeness; we chose opt-in to keep the release path as the
default and as the pi/bridge validation path.

### G — Stretch: hermes agent build in `klangkc` mode

Containers run `pi`, `claude code`, and the `hermes` agent. Goal: get the hermes
agent **build** running under `klangkc`. A minimax API key was added to `.env` for
this. Investigated in the loop; findings + blockers tracked in `PROGRESS.md`.

## Measured results (2026-06-23, macOS arm64, Flutter 3.41.6)

Validated against a live stack (backend :8997 + nginx-dev :8995 + `flutter run
-d web-server` :8996), driving real Chrome through nginx :8995.

| Path                                                         | Time                         | Notes                                                                                                           |
| ------------------------------------------------------------ | ---------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `flutter build web --release` (warm cache, no plugin change) | **21.3 s**                   | compile only; the `flutterbuildweb.sh` task adds plugin import + ~25 MB source-map inlining + cache-bust on top |
| same, after a **plugin-set change**                          | minutes                      | `flutterbuildweb.sh` does `rm -rf .dart_tool/flutter_build` → full cold dart2js compile                         |
| dev server: edit Dart + **browser refresh**                  | **~8.8 s**                   | incremental DDC recompile + app boot; **no release build runs**; needs no extension                             |
| dev server: **hot reload/restart** (`r`/`R`)                 | sub-second–seconds, stateful | requires the **Dart Debug Extension** on the web-server device (see finding below)                              |

Verified the layered routing is correct: the app loaded from the dev server
through nginx :8995, auto-logged-in (admin2@example.com), rendered the Workspaces
list, and reached `#/workspaces` — i.e. `/api/v1/*` and `/ws` still hit the
backend. A title edit in `workspace_list_page.dart` appeared after a plain
refresh with no `flutter build` invocation. Screenshots in `scratch/`.

### Finding: hot reload needs a debug-connected browser

`flutter run -d web-server` cannot _push_ a hot restart/reload into a browser it
doesn't control — it relies on the **Dart Debug Extension** to relay the debug
connection. Without it, `R` times out (`Hot restart ... received 0/1 responses`).
Two usable modes:

- **Extension-free (default, recommended to start):** edit + refresh the tab at
  nginx :8995. DDC recompiles incrementally (~9 s here) and the app reboots.
  Same-origin, so API/WS/bridge all work.
- **With the Dart Debug Extension:** install it in Chrome, open :8995, and `r`/`R`
  give true stateful hot reload (sub-second). Still same-origin via nginx.
- **`-d chrome` is NOT used** here: it launches a Chrome it controls (so hot
  reload works out of the box) but pointed at :8996 directly, which makes
  `Uri.base` = :8996 and breaks the app's API/WS calls. Same-origin via nginx is
  worth more than out-of-the-box hot reload.

This reframes **Strategy B**: for the web-server device, the no-keypress win is an
auto-**browser-reload** watcher (watch `lib/**` → tell the tab to reload →
incremental recompile), which works _without_ the extension. Scripting `R` only
helps once the extension is installed.

## Validation plan (full local)

1. Start podman VM if needed; `devenv processes up --no-tui`; confirm `:8995`.
2. Measure baseline: time a `flutter build web --release`.
3. Start `flutterdevweb`; load `:8995` in Chrome via the devtools MCP
   (admin2@example.com / password).
4. Edit a widget; confirm `R` reflects in the browser in seconds with **no** full
   rebuild; confirm login, `/api/v1/config`, and the WS still work.
5. Record real timings back into this doc. (macOS playwright e2e stays excluded.)
