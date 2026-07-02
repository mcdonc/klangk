# Klangk Intro Video — demo scene scripts

Scripts that drive the klangk web UI **and** the `klangkc` CLI through each scene
of the intro video (`videoscript.md`), recording video you can voice over and cut
together in DaVinci Resolve. This is **not** part of the CI test suite — it's a
recording harness, separate from `../e2e/` (the real Playwright config explicitly
does not match this directory).

There are two recording halves, because browsers and terminals need different
recorders:

- **Web-UI scenes** (5, 6, 6b, 7, 8, 9, 10) — driven by **Playwright**
  against the Flutter web app (`record-demo.sh` wraps the Xvfb + ffmpeg
  capture). See "Running a web-UI scene" below.
- **CLI scenes** (2, 3, 4) — host-terminal work (`klangkc shell`, `git clone`,
  `klangkc sandbox`) that Playwright can't drive. Driven by **`record-terminal.sh`**
  - `cli_demo.py` (Xvfb + xterm + tmux + ffmpeg). See "CLI terminal scenes"
    below.

## What's here

```text
videoscript.md          # the narration script (scene list)
shotlist.md             # per-scene recording checklist + resets + gotchas

# Web-UI harness (Playwright)
playwright.demo.config.ts   # Playwright config: headed, video:on, 1 worker
record-demo.sh         # Xvfb + ffmpeg recorder wrapper for the web-UI scenes
run-demo.sh            # convenience runner
demo-helpers.ts        # Flutter-coordinate primitives + pacing + auth + WS
demo-seed.ts           # one-time: seed users + Potemkin workspaces
scenes/
  scene-05-web-ui.ts          # workspaces + terminal (continuation of the CLI)
  scene-06-clanker-chat.ts    # live @clanker take (re-run until you like it)
  scene-07-files.ts           # file browser + PDF inline render
  scene-08-collaboration.ts   # 4 humans + clanker in one chat (2 recordings)
  scene-10-admin.ts           # admin panel tour

# CLI harness (tmux + ffmpeg)
record-terminal.sh     # Xvfb + xterm + tmux + ffmpeg recorder harness
cli_demo.py            # stdlib-only Python "expect" driver + the CLI scenes
recordings/            # CLI scene output .mp4 files (gitignored)

# Misc
tsconfig.json          # local type-check config (run tsc --noEmit)
assets/                # static demo assets (e.g. pyramid-docs.pdf for scene 7)
```

Scene files are named `scene-*.ts` (not `*.spec.ts`); the demo config sets
`testMatch: /scene-.*\.ts$/` so Playwright discovers them (its default glob
would otherwise skip them). Run them via the `playwright` command defined in
`devenv.nix` (see "Running a web-UI scene" below).

## Prerequisites

1. **Demo server running.** Start your real klangk normally (the one with your
   real LLM key). It should answer on `http://localhost:8995`.

2. **Auto-start enabled** (for scene 4, later): `.env` has
   `KLANGK_ALLOW_AUTOSTART=1` — confirmed.

3. **A working LLM key.** Scenes 6 + 8 invoke clanker live; if the proxy 401s,
   those takes die. Test the key first.

4. **`.env` copied here** (done once): the server reads it; the scripts read
   `KLANGK_DEFAULT_USER` to find the seeded admin. `.env` is gitignored.

5. **Seed once:**

   ```bash
   devenv shell -- node --experimental-strip-types src/frontend/e2e-tests/demo/demo-seed.ts
   ```

   Creates the `teammate@example.com` account (and is idempotent — safe to
   re-run). Override the teammate email / password with
   `KLANGK_DEMO_TEAMMATE_EMAIL` / `KLANGK_DEMO_PASSWORD`.

6. **e2e node_modules installed** (Playwright + ws): the wrapper needs
   `src/frontend/e2e-tests/node_modules/`. If missing, run
   `cd src/frontend/e2e-tests && npm install`.

## Running a web-UI scene

The config is **headed** (you'll SEE each click resolve as coordinates dial in)
and records video. Run from the **worktree root** (devenv needs `devenv.nix`):

```bash
# one scene, by grep on its title
devenv shell -- playwright test \
  --config=src/frontend/e2e-tests/demo/playwright.demo.config.ts -g clanker

# all scenes, in order
devenv shell -- playwright test \
  --config=src/frontend/e2e-tests/demo/playwright.demo.config.ts

# discover only (don't run)
devenv shell -- playwright test \
  --config=src/frontend/e2e-tests/demo/playwright.demo.config.ts --list
```

> **Run from the worktree root** (where `devenv.nix` lives), wrapped in
> `devenv shell --`. Use the **`playwright`** command defined in `devenv.nix`
> — it resolves to the local binary pinned to `@playwright/test@1.59.1`.
> Do **not** use `npx playwright`, which grabs a newer cached version (1.61.x)
> and fails with "two different versions of @playwright/test".

Knobs (env vars):

| Var                          | Default                    | Effect                                                          |
| ---------------------------- | -------------------------- | --------------------------------------------------------------- |
| `KLANGK_TEST_URL`            | `http://localhost:8995`    | the demo server to point at                                     |
| `KLANGK_DEMO_HEADLESS`       | unset                      | set `=1` for a quick headless dry check                         |
| `KLANGK_DEMO_SLOWMO`         | `50`                       | ms slowMo between actions (bump for slower, readable clicks)    |
| `KLANGK_DEMO_AGENT_WAIT`     | `60000`                    | how long to hold for clanker's live reply before the scene ends |
| `KLANGK_DEMO_PASSWORD`       | `demopass123`              | password for freshly-registered demo accounts                   |
| `KLANGK_DEMO_ADMIN_PASSWORD` | `$KLANGK_DEFAULT_PASSWORD` | the seeded admin's password (seed + admin scene)                |
| `KLANGK_DEMO_TEAMMATE_EMAIL` | `teammate@example.com`     | the collaborator account                                        |

## Output: web-UI video files

Playwright writes `.webm` recordings under the default test-results dir next to
each scene. Scene 08 produces **two** (owner + teammate). DaVinci Resolve imports
`.webm` directly; if yours doesn't, transcode:

```bash
for f in path/to/*.webm; do ffmpeg -i "$f" "${f%.webm}.mov"; done
```

## How it works (Flutter gotcha)

The frontend is Flutter Web → it renders to `<canvas>` inside `<flutter-view>`,
so **CSS selectors don't work.** Every interaction is a **coordinate click on
`flutter-view`** plus keyboard typing, and state is verified via page title.
`demo-helpers.ts` reuses the proven primitives from `../e2e/helpers.ts`
(`flutterClick`, tab coordinates, `terminalType`) and adds demo pacing + auth.

For the flaky bits (right-click → Share popup, chat mention autocomplete),
scenes drive state via **WebSocket commands** instead of pixel clicks — the same
reliable approach the existing `docs-*-screenshots.spec.ts` suite uses.

## Re-take workflow

Each browser scene creates a **fresh workspace** with a stable name, so a re-take
starts clean while the on-screen name stays the same. The **live-agent scenes
(6, 8)** are nondeterministic — re-run until you like what clanker produced, keep
that recording, and trim dead air in DaVinci (or narrate over it).

## CLI terminal scenes (`record-terminal.sh` + `cli_demo.py`)

The CLI scenes (2, 3, 4) drive a real terminal — `klangkc login`, `klangkc
create`, `klangkc shell`, `klangkc sandbox`, `git clone`, `klangkc monitor` —
which Playwright can't touch. The recorder scripts a terminal and captures it as
a **true 1080p** `.mp4` with no manual recording.

### How it works

The recorder **decouples _driving_ the terminal from _recording_ it** — the
same insight the web-UI harness (`record-demo.sh`) uses to beat Playwright's
~800×450 video cap. Four layers:

1. **Xvfb** — a virtual X display at the canvas size (default 1920×1080).
2. **xterm** — a real terminal emulator on that display, big legible font
   (default _DejaVu Sans Mono_ 22 pt, black background), attached to a tmux
   session.
3. **tmux** — owns the pty. xterm _displays_ the session; the driver _writes_
   to it (`send-keys`) and reads it (`capture-pane`). It's also the persistence
   layer (`klangkc shell` is tmux-backed, so this matches the real UX).
4. **ffmpeg** (`x11grab`) — captures the whole Xvfb display into a true
   full-resolution `.mp4`.

The driver (`cli_demo.py`) scripts the scene: typing commands (optionally one
character at a time for a "live typing" look), pressing Enter, and waiting for
expected output before continuing.

### Why not pexpect?

The plan (#1201) suggested _pexpect_ as the driver. It's the right **idea** —
an expect-style loop of _send a command, wait for output, respond_ — but it
can't drive a **displayed** terminal: `pexpect.spawn()` takes the **master**
side of the pty, so no terminal emulator can render that session live (xterm
would need to be the master). For a demo video you need a pty multiplexer in
the middle: one client renders it (xterm → Xvfb → ffmpeg) while another
scripts it. **tmux is that multiplexer**, and its `send-keys`/`capture-pane`
pair gives the same `send`/`expect` primitives without taking the pty hostage.
So the driver is **stdlib-only** (no `pexpect`, no `pip install` — runs on any
`python3`). Verified empirically: pexpect drove a shell headless fine, but no
terminal emulator could render it.

### Running a CLI scene

From the worktree root:

```bash
# Built-in smoke-test scene — NO klangk server required:
devenv shell -- src/frontend/e2e-tests/demo/record-terminal.sh \
    python3 src/frontend/e2e-tests/demo/cli_demo.py --scene demo

# A real CLI scene against a live server (start klangk on :8995 first):
devenv shell -- src/frontend/e2e-tests/demo/record-terminal.sh \
    python3 src/frontend/e2e-tests/demo/cli_demo.py --scene scene_2
```

Output: `recordings/recording-<timestamp>.mp4` at 1920×1080. Re-run a scene
until you like the take; edit the `.mp4`s together (and voice over) in DaVinci
Resolve.

### Writing a scene

A scene is a function `scene_<name>(t: Term)` in `cli_demo.py`. The `Term` API:

| Method                               | Effect                                             |
| ------------------------------------ | -------------------------------------------------- |
| `t.type(text, per_char=0.03)`        | type text (typewriter effect with `per_char`)      |
| `t.enter()`                          | press Enter                                        |
| `t.run(cmd, expect="$", timeout=30)` | type a command + Enter, then wait for `expect`     |
| `t.expect(text, timeout=30)`         | block until `text` appears in the pane             |
| `t.pause(seconds)`                   | hold for a beat (let output land / read on camera) |
| `t.clear()`                          | clear the screen                                   |

Register it in the `SCENES` dict and run with `--scene <name>`. The built-in
`demo` scene is a no-server smoke test — copy it as a template. `scene_2` /
`scene_3` / `scene_4` are skeletons for the real intro-video CLI scenes (they
need a live server); flesh them out against your demo server.

Camera-readability tips: **typewriter the commands** (`per_char≈0.02–0.04`) but
let bulky output paste normally; use `--key-delay` (pause after each Enter) to
avoid a blurred machine-gun look; bump `KLANGK_DEMO_FONT_SIZE` (e.g. `28`) if
text is small; drive flaky/async things (a service coming up) with
`t.expect(..., timeout=)` so the take doesn't race.

### CLI recorder knobs (env vars)

| Variable                   | Default                                              | Effect                        |
| -------------------------- | ---------------------------------------------------- | ----------------------------- |
| `KLANGK_DEMO_WIDTH`        | `1920`                                               | canvas width (px)             |
| `KLANGK_DEMO_HEIGHT`       | `1080`                                               | canvas height (px)            |
| `KLANGK_DEMO_FONT`         | `DejaVu Sans Mono`                                   | xterm font family             |
| `KLANGK_DEMO_FONT_SIZE`    | `22`                                                 | terminal font size (pt)       |
| `KLANGK_DEMO_FPS`          | `30`                                                 | capture framerate             |
| `KLANGK_DEMO_CRF`          | `20`                                                 | x264 quality (lower = better) |
| `KLANGK_DEMO_DISPLAY`      | `97`                                                 | Xvfb display number           |
| `KLANGK_DEMO_TMUX_SESSION` | `klangk-demo`                                        | tmux session name             |
| `KLANGK_DEMO_OUTPUT`       | `src/frontend/e2e-tests/demo/recordings/...<ts>.mp4` | output path                   |
| `KLANGK_DEMO_PROMPT`       | `klangk$` (colored)                                  | `PS1` for the session shell   |
| `KLANGK_DEMO_TYPEWRITER`   | `0`                                                  | default per-char delay (s)    |
| `KLANGK_DEMO_KEY_DELAY`    | `0.4`                                                | default pause after Enter (s) |

Requirements (all present on NixOS / via devenv): `Xvfb`, `xterm`, `tmux`,
`ffmpeg`, `ffprobe`, `xdotool`. The driver needs only `python3` (stdlib).

## Type-checking

```bash
devenv shell -- bash -lc 'cd src/frontend/e2e-tests/demo && tsc --noEmit -p tsconfig.json'
```

(The `node_modules` symlink points at `../node_modules` so `@playwright/test`
and `ws` resolve without a separate install.)

## Status / TODO

- [x] scaffolding: config, helpers, seed (users + Potemkin workspaces)
- [x] CLI recorder: `record-terminal.sh` + `cli_demo.py` (landed via #1204)
- [x] web-UI scenes 05, 06, 07, 08, 10 (Web UI, clanker, Files, Collab, Admin)
- [ ] CLI scenes 02, 03, 04 — flesh out the `cli_demo.py` `scene_2/3/4` skeletons
      against the demo server (and the continuity model in `shotlist.md`)
- [ ] scene 6b — debug-with-Pi terminal scene (drives `pi` in a tab; agent-driven)
- [ ] scene 09 — Plugins
- [ ] continuity refactor — unify web-UI scenes onto one shared `demo` workspace
      owned by `admin@plope.com`, seeding state forward (see `shotlist.md`)
