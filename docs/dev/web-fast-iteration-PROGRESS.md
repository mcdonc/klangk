# PROGRESS — web fast-iteration (autonomous run, 2026-06-23)

Running log so you can see direction + decisions while away. Newest at top.

## Decisions locked (from your answers)

- Scope: **layered** — hot reload for UI; release path stays the pi/bridge validation path.
- Integration: **opt-in** (`KLANGK_WEB_DEV=1`), default `devenv up` unchanged.
- Autonomy: implement on branch `feat/flutter-web-dev-mode`, push, open **draft PR**.
- Validation: **full local** (podman VM, image build, real Chrome via devtools MCP). No macOS playwright e2e.
- Stretch: hermes agent build in klangkc mode; minimax key is in `.env`.

## Key technical anchor

`ws_client.dart` derives WS + HTTP base from `Uri.base`. Keep browser on nginx :8995 →
all API/WS/bridge unchanged; nginx only forks by path in dev mode.

## Log

- [setup] Branched `feat/flutter-web-dev-mode` off main@1783784. Pre-existing WIP
  (main.py, flutterbuildweb.sh, test_oidc.py) left uncommitted — will NOT enter my PR.
- [doc] Wrote docs/dev/web-fast-iteration.md (scenarios + strategy menu).
- [impl] Strategy A done: scripts/flutterdevweb.sh (new), scripts/nginx.sh dev
  profile, devenv.nix (flutterdevweb script + KLANGK_WEB_DEV_PORT=8996 + skip
  flutter-build in dev). bash -n + nix parse clean.
- [validate] nginx dev config generates + passes `nginx -t`. Routing confirmed:
  `location = /ws` -> backend; `location /api/` -> backend; `location /` -> dev
  server :8996; bridge locations unchanged -> backend. Podman VM started.
- [validate] Surgical stack up (backend :8997 + nginx-dev :8995 + flutterdevweb
  :8996). Confirmed: nginx :8995/ serves the LIVE dev-server bundle
  (`<title>Klangk</title>`, flutter_bootstrap), :8995/api/v1/config -> backend 200.
  Flutter 3.41.6 web-server EXPOSES `r` Hot reload + `R` Hot restart. First DDC
  serve ~15s.
- [VALIDATED ✅] End-to-end in real Chrome via nginx :8995:
  - App loads from the live dev server, auto-logged-in as admin2@example.com,
    Workspaces list renders, API + WS work (it reached #/workspaces). So the
    layered routing (/ -> dev server, /api+/ws -> backend) is correct and the
    pi/bridge path is untouched.
  - Edited `workspace_list_page.dart` app-bar title -> 'Workspaces HOTRELOAD-OK',
    did a plain **browser refresh**: the change appeared in ~8.8s via incremental
    DDC recompile. NO `flutter build web --release` ran. (Edit since reverted.)
    Screenshots: scratch/devmode-before.png, scratch/devmode-after.png.
- [FINDING ⚠️] `flutter run -d web-server` hot restart (`R`) / hot reload (`r`)
  needs a debug-connected browser — i.e. the **Dart Debug Extension** — or it
  times out ("received 0/1 responses"). My headless MCP Chrome has no extension,
  so true hot reload couldn't be auto-triggered. BUT the extension-free path
  (edit + refresh = incremental recompile, ~9s) is the real daily win and needs
  no extension. Implication for Strategy B: for web-server, an auto-**browser-
  reload** watcher (livereload-style) beats trying to script `R`, unless we adopt
  `-d chrome` (flutter-controlled Chrome has the debug client, but loads from
  :8996 directly -> breaks same-origin API/WS). Documenting both.
- [next] Capture baseline `flutter build web --release` time -> compute speedup;
  fold findings into the doc; commit my files; push; draft PR. Then loop on
  Strategy B (reload watcher), D/F (bind-mount extensions), and hermes/klangkc.
