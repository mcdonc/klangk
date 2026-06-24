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

| Path                                                         | Time                     | Notes                                                                                                           |
| ------------------------------------------------------------ | ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `flutter build web --release` (warm cache, no plugin change) | **21.3 s**               | compile only; the `flutterbuildweb.sh` task adds plugin import + ~25 MB source-map inlining + cache-bust on top |
| same, after a **plugin-set change**                          | minutes                  | `flutterbuildweb.sh` does `rm -rf .dart_tool/flutter_build` → full cold dart2js compile                         |
| dev server: edit Dart + **browser refresh**                  | **~8.8 s**               | incremental DDC recompile + app boot; **no release build runs**; needs no extension                             |
| dev server: **hot reload** (`r`), debug-connected browser    | **122–135 ms**, stateful | measured via `-d chrome`; same-origin needs the Dart Debug Extension (see finding)                              |
| dev server: **hot restart** (`R`), debug-connected browser   | **249 ms**               | measured via `-d chrome`; same-origin needs the Dart Debug Extension (see finding)                              |

Verified the layered routing is correct: the app loaded from the dev server
through nginx :8995, auto-logged-in (admin2@example.com), rendered the Workspaces
list, and reached `#/workspaces` — i.e. `/api/v1/*` and `/ws` still hit the
backend. A title edit in `workspace_list_page.dart` appeared after a plain
refresh with no `flutter build` invocation. Screenshots in `scratch/`.

### Finding: hot reload needs the browser to be the _debug-connected_ one

Tested empirically (real Chrome, driven via the devtools MCP):

- `flutter run -d web-server` cannot _push_ a hot restart/reload into a browser it
  doesn't control — without the **Dart Debug Extension**, `R` times out
  (`Hot restart ... received 0/1 responses`).
- `flutter run -d chrome` _does_ control its browser, so hot reload/restart land
  with **no extension**: measured **`r` = 122–135 ms** (stateful), **`R` = 249 ms**.
  But that Chrome loads from `:8996` directly, so `Uri.base = :8996` and the app's
  `/api/v1/config` + `/ws` hit the dev server, not the backend (observed:
  `AuthService load config failed: ... <!doctype ...`). **Same-origin breaks.**
- Crucially: with `-d chrome` running, a **separate** same-origin tab opened at
  nginx `:8995` (where the app _does_ work — logged in, workspaces rendered) did
  **not** receive the 122 ms hot reload — DDC reload only patches the
  debug-connected browser, not an independently-loaded tab (verified by
  screenshot). So you cannot "share" one `-d chrome` debug session with a
  same-origin tab.

**Conclusion — pick two of three (extension-free / same-origin / sub-second):**

| Goal                                       | How                                                     | Cost                                     |
| ------------------------------------------ | ------------------------------------------------------- | ---------------------------------------- |
| same-origin **+** extension-free           | `-d web-server` behind nginx, **edit + refresh**        | ~9 s, loses state                        |
| same-origin **+** sub-second hot reload    | `-d web-server` behind nginx **+ Dart Debug Extension** | one-time extension install               |
| extension-free **+** sub-second hot reload | `-d chrome`                                             | **not** same-origin → app's API/WS break |

For klangk's app (which needs API/WS), the realistic choices are the first two.
The shipped Strategy A is the first row. The Dart Debug Extension upgrades it to
the second (sub-second) with no code change.

This pins down **Strategy B**: scripting `R` (via `--machine` or a FIFO) only
helps once the **Dart Debug Extension** is installed (then it's same-origin +
sub-second). The only _extension-free_ no-keypress win is an auto-**browser-
reload** watcher (watch `lib/**` → reload the `:8995` tab → ~9 s incremental
recompile).

## Validation plan (full local)

1. Start podman VM if needed; `devenv processes up --no-tui`; confirm `:8995`.
2. Measure baseline: time a `flutter build web --release`.
3. Start `flutterdevweb`; load `:8995` in Chrome via the devtools MCP
   (admin2@example.com / password).
4. Edit a widget; confirm `R` reflects in the browser in seconds with **no** full
   rebuild; confirm login, `/api/v1/config`, and the WS still work.
5. Record real timings back into this doc. (macOS playwright e2e stays excluded.)

## Container-side iteration (D + F) — validated

The other half of `devenv up` is `klangk:build-workspace-image` (the image where
`pi` / `claude code` / `hermes` run). Two findings:

### D — the image build is already hash-gated (decoupled)

`build-workspace-image.sh` hashes `scripts/build-workspace-image.sh` +
`src/containers/workspace/` + the plugins dir against
`$DEVENV_STATE/klangk/.backend-image-hash` and **skips** when unchanged
(confirmed live: "Image klangk-arm64 is up to date, skipping build."). So
frontend-only work doesn't rebuild the image. Dev mode (Strategy A) additionally
skips `klangk:flutter-build`, so a `KLANGK_WEB_DEV=1` start does neither heavy
build when nothing relevant changed. No change needed here beyond documenting it.

### F — bind-mount extensions to skip the image rebuild ✅ mechanic validated

Today the Dockerfile `COPY`s plugin/builtin **extensions**, **tools**, **hooks**,
and the frequently-edited `klangk-*` scripts / `system-prompt.md` /
`entrypoint.sh` into the image (`src/containers/workspace/Dockerfile:13-60`). Any
edit to those changes the hash → full `podman build`. Pi runs as a long-lived
container (`entrypoint.sh` = `sleep infinity`; sessions via `podman exec`), so the
container doesn't even need rebuilding — just the files refreshed.

**Key insight** (`klangk-setup-clankers.py:64`): Pi auto-discovers
`~/.pi/agent/extensions/` _in addition_ to the baked image dir. So dev extensions
can be bind-mounted **additively** into the user dir — no need to mount over
`/opt/klangk/pi-agent/extensions` (which would _hide_ the baked builtin/plugin
extensions).

Validated against the real `klangk-arm64` image (no rebuild):

```text
podman run -d --userns=keep-id:uid=1000,gid=1000 \
  -v $PWD/scratch/fdev-extensions:/home/klangk/.pi/agent/extensions:ro klangk-arm64
# file visible inside, owned by klangk; host edit -> reflected live in the
# running container with NO rebuild and NO restart.
```

**Proposed implementation (opt-in, separate tested PR):** in `container.py` where
`binds` is assembled (~line 538), when a dev flag is set (e.g.
`KLANGK_WORKSPACE_DEV=1`), append additive read-only mounts:

- host plugin/builtin extensions dir → `/home/klangk/.pi/agent/extensions/` (or a
  second dir added to Pi's `extensions` array in `settings.json`)
- staged tools dir → an extra `PATH` dir (avoid hiding `/opt/klangk/bin`)
- `src/containers/workspace/*.sh` dev copies → `/opt/klangk/bin/*` individually

Then editing an extension/tool/script + reopening the workspace (or re-exec'ing
the agent) picks up changes with **no image rebuild**. Gate it so prod/default is
untouched, mirroring Strategy A. Needs unit tests (container `binds` assembly is
covered) to hold the 100% coverage bar — hence a separate PR, not bundled here.

## Stretch (G) — hermes agent in klangkc mode: investigation + blocker

What I confirmed:

- The workspace image bakes in only **Pi** (`@earendil-works/pi-coding-agent`)
  and **Herdr** (`herdr` 0.6.6, a terminal-based agent runtime). `claude code`
  and `hermes` are not baked — they'd be installed/run _inside_ a workspace.
- Agents talk to the LLM via the **`llm-proxy`** provider (nginx →
  `KLANGK_LLM_BASE_URL` with the server-side key); `klangk-setup-clankers.py`
  wires Pi's `~/.pi/agent/{settings,models}.json` to it using `KLANGK_LLM_MODEL`.
- **MiniMax** is a supported LLM (builtin `minimax-thinking-tags.ts` repairs
  `<think>` tags from MiniMax models). `MINIMAX_API_KEY` is in `.env`.
- **`klangkc sandbox`** reads a `.klangk/` config (`image`, `setup-command`,
  mounts, `forward-agent`) and runs a default command in a workspace container —
  this is the "klangkc mode" surface where an agent build would be driven.
- **"hermes" appears nowhere** in the repo or git history/branches.

**Blocker (needs your input):** I can't "get the hermes agent build running"
without knowing what `hermes` _is_. Which of these?

1. An external agent CLI/binary (give the repo URL / install command), or
2. An npm package (like Pi) to add to the image / a sandbox `setup-command`, or
3. A Pi/Herdr configuration preset, or
4. A MiniMax **model id** named "Hermes" to set as `KLANGK_LLM_MODEL`.

Once identified, the concrete path is: add a `.klangk/` sandbox config (or image
layer) that installs hermes and sets the MiniMax-backed model via the llm-proxy,
then `klangkc sandbox` / `klangkc shell` to run it. I've left this unblocked-but-
unstarted rather than guess.
