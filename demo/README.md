# Klangk intro video — CLI terminal recorder

A reproducible, re-takeable recorder for the **CLI/terminal scenes** of the
klangk intro video (#1082). It scripts a real terminal — `klangkc login`,
`klangkc create`, `klangkc shell`, `klangkc sandbox`, `git clone`,
`klangkc monitor`, … — and captures it as a **true 1080p** `.mp4`, with no
manual recording.

This is the terminal half of the intro-video tooling. The web-UI scenes are
driven separately by the Playwright harness (`src/frontend/e2e-tests/demo/`,
WIP); this directory handles what Playwright can't — an interactive shell.

```text
record-terminal.sh   # Xvfb + xterm + tmux + ffmpeg recorder harness
cli_demo.py          # stdlib-only Python "expect" driver + the scenes
README.md            # this file
recordings/          # output .mp4 files (gitignored)
```

## Quick start

From the worktree root (no devenv wrapper needed — the tools are on the system
PATH on NixOS):

```bash
# Built-in smoke-test scene — NO klangk server required:
./demo/record-terminal.sh python3 demo/cli_demo.py --scene demo

# A real CLI scene against a live server (start klangk on :8995 first):
./demo/record-terminal.sh python3 demo/cli_demo.py --scene scene_2
```

You'll get `demo/recordings/recording-<timestamp>.mp4` at 1920×1080. Re-run a
scene until you like the take; edit the `.mp4`s together (and voice over) in
DaVinci Resolve.

## How it works

The recorder **decouples _driving_ the terminal from _recording_ it** — the
same insight the web-UI harness uses to beat Playwright's ~800×450 video cap.
Four layers:

1. **Xvfb** — a virtual X display at the canvas size you ask for (default
   1920×1080).
2. **xterm** — a real terminal emulator on that display, big legible font
   (default _DejaVu Sans Mono_ 22 pt, black background), attached to a tmux
   session.
3. **tmux** — owns the pty. xterm _displays_ the session; the driver _writes_
   to it (`send-keys`) and reads it (`capture-pane`). It's also the persistence
   layer (`klangkc shell` is tmux-backed, so this matches the real UX).
4. **ffmpeg** (`x11grab`) — captures the whole Xvfb display into a true
   full-resolution `.mp4`.

The driver (`cli_demo.py`) then scripts the scene: typing commands (optionally
one character at a time for a "live typing" look), pressing Enter, and waiting
for expected output before continuing.

## Why not pexpect? (driver rationale)

The plan (#1201) suggested _pyexpect_ (pexpect) as the driver. It's the right
**idea** — an expect-style loop of _send a command, wait for output, respond_ —
but it can't drive a **displayed** terminal:

> `pexpect.spawn()` takes the **master** side of the pty it creates. A terminal
> emulator (xterm) must _itself_ be the pty master to render the session. You
> can't have both pexpect and xterm as master of the same pty — so if pexpect
> owns it, xterm has nothing to attach to and nothing is ever drawn to the
> screen that ffmpeg captures.

For a _headless_ expect loop (drive a CLI, scrape its output) pexpect is ideal,
and that's exactly how `klangkc`'s own e2e tests drive the CLI elsewhere. But a
demo video needs the terminal **rendered**. That needs a pty multiplexer in the
middle: one client renders it (xterm → Xvfb → ffmpeg) while another scripts
it. **tmux is that multiplexer**, and its `send-keys`/`capture-pane` pair gives
the same `send`/`expect` primitives pexpect provides — without taking the pty
hostage.

So the driver is a **stdlib-only** Python script (no `pexpect`, no `pip install`
— runs on any `python3`) that implements an expect-style `Term` class on
`tmux send-keys`/`capture-pane`. This was verified empirically: pexpect drove a
shell headless just fine, but no terminal emulator could render that session.

## Writing a scene

A scene is a function `scene_<name>(t: Term)` in `cli_demo.py`. The `Term` API:

| Method                               | Effect                                             |
| ------------------------------------ | -------------------------------------------------- |
| `t.type(text, per_char=0.03)`        | type text (typewriter effect with `per_char`)      |
| `t.enter()`                          | press Enter                                        |
| `t.run(cmd, expect="$", timeout=30)` | type a command + Enter, then wait for `expect`     |
| `t.expect(text, timeout=30)`         | block until `text` appears in the pane             |
| `t.pause(seconds)`                   | hold for a beat (let output land / read on camera) |
| `t.clear()`                          | clear the screen                                   |

Register it in the `SCENES` dict and run it with `--scene <name>`. The built-in
`demo` scene is a no-server smoke test — copy it as a template. `scene_2` /
`scene_3` / `scene_4` are skeletons for the real intro-video CLI scenes (they
need a live server); flesh them out against your demo server.

Tips for a scene that reads well on camera:

- **Typewriter the commands** (`per_char≈0.02–0.04`) so viewers see them being
  typed, but **paste bulky output** by letting commands run normally.
- **`--key-delay`** (pause after each Enter) avoids a blurred machine-gun look.
- Set `KLANGK_DEMO_FONT_SIZE` higher (e.g. `28`) if the text is too small.
- Drive flaky/async things (a service coming up) with `t.expect(..., timeout=)`
  so the take doesn't race.

## Knobs (env vars)

| Variable                   | Default                              | Effect                        |
| -------------------------- | ------------------------------------ | ----------------------------- |
| `KLANGK_DEMO_WIDTH`        | `1920`                               | canvas width (px)             |
| `KLANGK_DEMO_HEIGHT`       | `1080`                               | canvas height (px)            |
| `KLANGK_DEMO_FONT`         | `DejaVu Sans Mono`                   | xterm font family             |
| `KLANGK_DEMO_FONT_SIZE`    | `22`                                 | terminal font size (pt)       |
| `KLANGK_DEMO_FPS`          | `30`                                 | capture framerate             |
| `KLANGK_DEMO_CRF`          | `20`                                 | x264 quality (lower = better) |
| `KLANGK_DEMO_DISPLAY`      | `97`                                 | Xvfb display number           |
| `KLANGK_DEMO_TMUX_SESSION` | `klangk-demo`                        | tmux session name             |
| `KLANGK_DEMO_OUTPUT`       | `demo/recordings/recording-<ts>.mp4` | output path                   |
| `KLANGK_DEMO_PROMPT`       | `klangk$` (colored)                  | `PS1` for the session shell   |
| `KLANGK_DEMO_TYPEWRITER`   | `0`                                  | default per-char delay (s)    |
| `KLANGK_DEMO_KEY_DELAY`    | `0.4`                                | default pause after Enter (s) |

## Requirements

All present on NixOS / via devenv (no installs needed): `Xvfb`, `xterm`,
`tmux`, `ffmpeg`, `ffprobe`, `xdotool`. The driver needs only `python3`
(stdlib). For higher resolution, set `KLANGK_DEMO_WIDTH`/`HEIGHT` (e.g. `2560`
`1440` for 1440p).

## Related

- **#1082** — the intro-video umbrella this belongs to.
- **#1201** — the issue this tool closes.
- `src/frontend/e2e-tests/demo/record-demo.sh` (WIP) — the analogous
  Xvfb+ffmpeg recorder for the **web-UI** scenes; this directory mirrors its
  "decouple driving from recording" approach for the terminal.
