# Extension-free auto-reload (Firefox-friendly)

Opt-in, off by default. Builds on the hot-reload dev mode
(`docs/dev/web-fast-iteration.md`). Gives a **hands-free** edit→see-it loop in
**any browser** (Firefox, Safari, Chrome) with **no Dart Debug Extension**.

## Why this exists (the important caveat)

`flutter run -d web-server` compiles the app at **startup** and on `r`/`R` hot
reload/restart. Hot reload/restart require a _debug-connected_ browser — i.e. the
**Chrome-only Dart Debug Extension** (without it, `R` times out:
`Hot restart … received 0/1 responses`). A plain **browser reload does NOT
trigger recompilation** — verified empirically: editing a widget and refreshing
the tab (even a hard refresh) shows the _old_ code; only after restarting the dev
server does the change appear.

So the only way to pick up a source edit **without the extension** is to **restart
the dev server**. This watcher automates that.

## What it does

With `KLANGK_WEB_DEV_RELOAD=1`, `scripts/flutterdevweb.sh` launches
`scripts/flutter_reload_server.py` (a supervisor) instead of `flutter` directly.
The supervisor:

1. owns the `flutter run -d web-server` process,
2. watches `src/frontend/lib/**` and `web/**`,
3. on a save (debounced), **restarts** the dev server — the warm `.dart_tool`
   cache makes this an incremental recompile, not a cold build,
4. once it's serving again, pushes a Server-Sent Events `reload` to browsers,
5. an `EventSource` client — injected into the served HTML by nginx's dev profile
   via `sub_filter` — calls `location.reload()`.

Same-origin throughout (served via nginx `:8995`), so API/WS/bridge keep working.

## Usage

```bash
# terminal 1 — backend + nginx (dev routing + livereload injection), release build skipped
KLANGK_WEB_DEV=1 KLANGK_WEB_DEV_RELOAD=1 devenv processes up --no-tui
# terminal 2 — supervisor (owns flutter + the livereload SSE server)
KLANGK_WEB_DEV=1 KLANGK_WEB_DEV_RELOAD=1 devenv shell -- flutterdevweb
```

Open the app at `http://localhost:8995/` in **any** browser, edit a `.dart` file,
save — the tab reloads itself a few seconds later showing the change. No keypress,
no extension.

## Measured cost (macOS arm64, Flutter 3.41.6)

|                                                            | time                                   |
| ---------------------------------------------------------- | -------------------------------------- |
| dev-server **warm restart** (per save, this watcher)       | **~12 s** (measured 11.8 s)            |
| dev-server cold first start                                | ~12–15 s                               |
| `flutter build web --release` (the old per-change cost)    | ~21 s (minutes on a plugin-set change) |
| hot reload `r` / restart `R` **with** Dart Debug Extension | ~0.1–0.3 s, stateful (Chrome only)     |

So this is faster than the release build and fully hands-free, but it's a full
restart (app state resets) — slower than extension hot reload. Pick this for
Firefox / no-extension; pick the Dart Debug Extension for the fastest loop in
Chrome.

## Knobs

- `KLANGK_WEB_DEV_RELOAD=1` — enable (requires `KLANGK_WEB_DEV=1`).
- `KLANGK_WEB_DEV_RELOAD_PORT` — SSE server port (default 8994).
- Default / production: untouched — none of this runs unless the flag is set.

## How the pieces fit

- `scripts/flutter_reload_server.py` — the supervisor + SSE server (stdlib only).
- `scripts/nginx.sh` — when `KLANGK_WEB_DEV_RELOAD=1`: a `/__livereload` proxy to
  the SSE server + `sub_filter` injection of the `EventSource` client.
- `scripts/flutterdevweb.sh` — execs the supervisor when the flag is set.
