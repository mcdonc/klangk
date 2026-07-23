# Klangk Introduction Video Script (~14 minutes)

## Production — read this first

**Target runtime:** ~14 min. **Edit unit:** one clip per scene (easy to
re-take a single flubbed scene). **Workflow:** silent screen capture first →
record VO against the cut → align.

### Continuity & workspace map

One continuous story across a single evolving workspace, **`demo`**, created
on camera in Scene 2 and kept alive through every scene after. State
accumulates shot to shot:

| Workspace  | Born in                                   | Owner               | Role in the video                                                                                                                                                                                                                                                   |
| ---------- | ----------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`demo`** | Scene 2 (`klangk create demo`, on camera) | `admin@example.com` | **Hero.** Kept alive through every scene after. Accumulates: cloned klangk repo + Pi session (Sc 2) → clanker's Flask app `app.py`/`requirements.txt` (Sc 5) → debugged, running app (Sc 5b) → browsed files + Pyramid PDF (Sc 6) → shared with the team (Sc 7/7b). |
| `openclaw` | Scene 3 (`klangk sandbox openclaw`)       | `admin@example.com` | Self-contained sandbox + service feature demo. Stays in the list (green health icon); its Service tab + hosted app are shown in Sc 3b/4.                                                                                                                            |
| Potemkin   | Pre-seeded (see Accounts below)           | various             | Decorative — fill every account's list so it looks lived-in. Never opened on camera.                                                                                                                                                                                |

**Rules:**

- Whenever a scene says "open a workspace", it means **open the `demo` workspace**.
- **Do not `klangk rm demo` during the run** — it must survive into the next
  scene. `rm` is mentioned verbally in Sc 2 as the eventual cleanup, not
  executed on `demo`.
- Record the browser arc (Sc 4→5→5b→6→7→7b) **in order**, against the same live
  `demo` container, so clanker's files / chat history / running app carry
  forward.
- **Code note:** the Playwright scenes currently each spin up their _own_ fresh
  workspace + user. To realize continuity they must be refactored to share
  **one** hero workspace (`demo`) owned by `admin@example.com`, seeding state
  forward between scenes instead of `ensureFreshWorkspace`-ing each time. See
  `demo-helpers.ts`.

### Demo environment

- [ ] Klangk server running locally; reach it at `http://localhost:8995`.
- [ ] `KLANGK_ALLOW_AUTOSTART=1` set on the server (required for the
      Sc 3/3b service scene).
- [ ] **Real LLM key configured** for the proxy. The clanker chat scene AND the
      openclaw scene both depend on the LLM proxy actually working. Test it.
- [ ] `jq` installed locally (for the `klangk monitor | jq .` beat).
- [ ] For Sc 3b's unhealthy beat: `KLANGK_HEALTH_CHECK_INTERVAL=10` set (snappier
      flip on camera; product default is 30s). Needs a **full** backend restart
      (not SIGHUP) — set it off camera, before recording.

### Interaction on camera (recording rules)

These apply to **every** browser/web-UI scene. They keep the recording honest
— the viewer should see a real user driving the UI with the mouse, not a
script firing synthetic events.

- **Every UI action is a mouse click / movement.** Buttons, tabs, cards, menu
  items, dialog submission — all driven with visible mouse movement
  (`mouseClick` / `mouseClickRight`, which glide the cursor to the target). The
  viewer watches the cursor travel and click. This is the whole point of an
  intro video.
- **Keyboard is for typing text only** — terminal commands, a file name, the
  word "scratch". It is **never** used to emulate an action: no `Ctrl+A` /
  `Cmd+A` to select, no `Enter` / `Escape` to submit or dismiss a dialog, no
  `Tab` to move focus. Those are actions; they get a mouse click. (Select-all
  in a field is a **triple-click** — three rapid clicks with no move
  between, else it doesn't register. Submit is a click on the OK button.)
- **Don't add beats the script doesn't call for.** A scene shows exactly what
  its narration describes — no extra clicking around to "fill time" or
  "tour" UI the narration hasn't introduced. In particular: **do not click
  through the nav tabs (Files/Chat/Sharing/Settings) in Scene 4.** The line
  "the web UI has features beyond the terminal — files, chat, and
  collaboration. Let me show those" is a **hand-off to Scenes 5/6/7**, not an
  instruction to click through them here. (Scene 5 opens by clicking the Chat
  tab — touring it in Sc 4 spoils that reveal.)
- **Don't type commands just to produce output.** Where the script's point is
  continuity (the terminal already shows prior state from the CLI), show the
  existing scrollback — don't type a fresh `echo`/`ls` that overwrites or
  crowds it. Type into the terminal only when the narration calls for a
  command.

### Accounts & Potemkin workspaces (seed once, before recording)

- [ ] **On-camera user:** `admin@example.com` (password `adminpass`). The hero
      `demo` workspace is created **on camera** in Scene 2 — do not pre-seed it.
- [ ] **Run the seed script once** for the supporting cast + decorative
      workspaces (creates `teammate@`, `designer@`, `reviewer@` used live in
      Sc 7, plus five Potemkin workspaces; idempotent — re-run safely):

  ```bash
  KLANGK_DEMO_ADMIN_EMAIL=admin@example.com \
  devenv shell -- node --experimental-strip-types \
      src/frontend/e2e-tests/demo/demo-seed.ts
  ```

- [ ] For Sc 7/7b, the supporting cast (`teammate@`, `designer@`, `reviewer@`)
      is seeded as Collaborators on `demo`; each scene records **one** browser
      window and sidechannels the other party over WS (see Sc 7/7b production
      notes). No second live browser window is needed during recording.

### Pre-warm the slow, network-dependent installs (do OFF camera)

> Recording a fresh install live is the #1 way to waste a take.

- [ ] **openclaw sandbox:** run `klangk sandbox openclaw` once off-camera so the
      nvm + Node 24 + openclaw download is done. On-camera, keep the workspace
      and `klangk restart openclaw` (fast — install guards fire because
      `/openclaw` is a mount).
- [ ] **A repo to clone** in the CLI scene: `git@github.com:mcdonc/klangk.git`.
      Verify `ssh-add -l` shows your key so `-A` works.

### Recording tooling & hygiene

- [ ] Recorder: capture a fixed window/region (not fullscreen with the menubar).
      **1080p minimum.**
- [ ] **Font size up** in both terminal and browser (legibility) — terminal
      ~18–20pt.
- [ ] **Do Not Disturb** on (kill notifications, Slack popups, calendar toasts).
- [ ] Clean browser: hide bookmarks bar, blank new-tab, neutral wallpaper.
- [ ] **No real secrets on screen:** demo account, fake emails, never a token.
      The LLM proxy keeps the key off-screen by design — good to say, not show.
- [ ] **CLI scenes** are driven by `cli_demo.py` (xterm+tmux+ffmpeg via
      `record-terminal.sh`). Cursor must not blink (`xterm cursorBlink:false` +
      `PROMPT_COMMAND='printf "\033[2 q"'`). Plain `host $` host prompt; venv on
      PATH for `klangk`. ≥5s pauses between commands (`KLANGK_DEMO_HOLD`,
      default 5).

### Master reset (between full re-runs)

```bash
klangk rm demo            # hero workspace — wipes the accumulated state
klangk rm openclaw        # only if you want a truly fresh sandbox/service scene
# then re-run Sc 2, 3 to rebuild demo/openclaw on camera
```

> A mid-arc re-take (e.g. just Sc 6) does NOT need a full reset — re-run from
> the earliest scene whose state it depends on, or re-seed just the missing
> piece (e.g. drop the Pyramid PDF back in via `seedDemoFile`).

### Recording workflow (video-first, VO second)

1. **Capture silent video, one clip per scene.** Don't aim for perfect narration
   while recording — you'll VO later.
2. **Leave headroom/tails** on each clip. Leave _dead air while the agent works_
   (Scenes 2, 5, 5b) — you'll narrate over it.
3. **Re-take discipline:** reset per the scene's note and re-record just that
   clip; don't restart the whole video.
4. **Rough cut** the clips to the ~14-min structure, then **record voiceover**
   in a single quiet session reading this script against the cut.
5. A cheap-ish mic in a quiet room + a pop filter is plenty for VO.

---

## Scene 1 — Opening — What is Klangk? (1 minute)

Hey everyone. Today I'm going to show you Klangk — an open-source platform for running AI coding agents in sandboxed containers.

I'm sure if you're a programmer or programmer adjacent, you know things are getting a little scary. AI coding harnesses are incredibly productive, but they need broad permissions — they read and write your files, and run shell commands. Most harnesses pretend to have "safety" modes but nothing is really safe about them.

Klangk lets you quickly give every project its own isolated Linux container without a lot of ceremony — agents can do whatever they need in there without touching your host system.

If you're a solo developer, you'll probably interact with Klangk mostly via its CLI, which lets you create workspaces, login via an SSH-like interface, mount directories from your home system, copy files between your host system and the container, and use version control like GitHub.

Meanwhile, if you're on a team, you can use Klangk to share workspaces, pair-program in shared terminals, and chat alongside your AI through a web browser.

Klangk can be run as a Docker container or using raw hardware on MacOS or
Linux.

I'll start by showing a solo workflow from the command line, then move to the web UI for the team features. Let me show you how it works.

> **Production —** _on screen:_ title card / logo, or talking head; minimal screen
> content. _reset:_ n/a. _gotchas:_ VO-only — record to picture after the cut is
> locked.

## Scene 2 — The CLI — Creating Your First Workspace (2 minutes)

_[Screen: local terminal, klangk already installed]_

The CLI is called `klangk`. You install it with pip and point it at your Klangk server. Let me log in.

_[Type: klangk login admin@example.com]_

Now let me create a workspace.

_[Type: klangk create demo]_

That's it — a fresh Linux container with Python, Node, git, and build tools is now running. Let me drop into it.

_[Type: klangk shell demo]_

This is an SSH-like connection into an Ubuntu container. I'm in a real bash shell on Linux, regardless of what my local machine is. Under the hood this is backed by tmux, so if I disconnect and reconnect, everything is exactly where I left it — same scrollback, same running processes.

Let me do something useful. I'll clone a repo.

_[Type: klangk shell demo -A]_

The `-A` flag forwards my local SSH agent into the container, so I can use my GitHub SSH keys without copying any private keys into the container.

_[Type: ssh-add -l]_

See? My keys are here — forwarded from my host, never copied into the container, although this doesnt happen over literal SSH. Instead the forwarding happens over HTTP(S).

_[Type: git clone git@github.com:mcdonc/klangk.git]_

I can also use harnesses like Pi inside the container.

_[Type: cd klangk, then: pi -p "In two sentences, what does this codebase do?"]_

There it is. I can work on this project, run tests, use AI agents — all inside the sandbox. My host system is untouched.

And I'm not limited to a single terminal — I can open more than one window into the same workspace. But first, let me grab this container's identity so I can prove a point in a moment.

_[Type: hostname — shows the container ID. Then: echo "$(hostname)" > ~/containername]_

Now let me split my screen and connect again, this time to a named window.

_[Split the terminal into two horizontally split panes. In the new (bottom) pane, type: klangk shell demo terminal2]_

Now I've got two terminals open to the same workspace, on top of each other. The second one connected to a separate, named window — "terminal2". And here's the proof that it's the same container:

_[In the second pane, type: cat containername — shows the same container ID]_

Same hostname — because both terminals share one container. Each connection is its own independent shell, and a named window like this shows up as a tab in the web UI too. To disconnect either one, I use the SSH-style escape: Enter, tilde, dot.

_[Disconnect the second pane with ~. , then the first]_

The container keeps running. Back at my host prompt, I can see all my workspaces with `klangk ls` — there's `demo`, the one we just made.

_[Type: klangk ls]_

And when I'm eventually done with a workspace, `klangk rm` tears it down and cleans up its files. But I'm going to keep this one around — we'll come back to it from the browser in a minute.

> **Production —** _on screen:_ local terminal, `klangk` installed, clean
> prompt. _pre-roll:_ confirm `ssh-add -l` lists your key; repo is
> `git@github.com:mcdonc/klangk.git`; have `pi` functional in a workspace (test
> the prompt once off-camera). _reset:_ `klangk rm demo && klangk create demo`
> — but only for a full arc re-run, since `demo` must survive into Sc 4–7.
> _gotchas:_ the `pi` interaction is **live/nondeterministic** — one long take,
> leave dead air, narrate over later; `-A` must actually work (test first); the
> `Enter ~ .` escape only fires right after a newline — rehearse it; the
> split-pane beat connects to a **named** window (`logs`) via a tmux control call
> (it never appears as typed text).

## Scene 3 — klangk sandbox — One Command to Rule Them All (~1.5 minutes)

_[Screen: local terminal, in a project directory]_

Creating workspaces manually is fine, but the real power for solo developers is `klangk sandbox`. You check a config file into your repo, and then one command sets up everything.

We have a sandbox config for `openclaw` in our repository.

_[Type: cat sandboxes/openclaw/.klangk-sandbox.yaml — point at the mount-at and setup lines]_

Here's what a sandbox config looks like — it mounts the project at a fixed path inside the container and runs a setup script to get it ready. When I run `klangk sandbox openclaw`, it creates the workspace, mounts everything, and starts the container.

_[Type: klangk sandbox openclaw sandboxes/openclaw]_

The idea is that you commit this config file to your repo. Any teammate — or future you on a different machine — runs the same command and gets the exact same environment. It's like a Dockerfile for your dev environment, but the container lifecycle is managed for you.

You can connect to the workspace with `klangk shell`.

_[Type: klangk shell openclaw]_

> **Production —** _on screen:_ local terminal at the klangk repo root. _pre-roll:_
> openclaw **pre-warmed** (Node install done off-camera); `jq` installed; LLM proxy
> working (so the gateway comes up healthy, not red); confirm `klangk ls` shows a
> **Status** column (post-#1207). _reset:_ keep the openclaw workspace and
> `klangk restart openclaw` (re-installs are slow); only `rm && sandbox` for a
> truly fresh take. _gotchas:_ **never record the first-run install live**;
> CLI-only — the hosted app is Scene 4.

## Scene 3b — Long Lived Services (~2.5 minutes)

_[Screen: same terminal, continuing — the openclaw workspace from Scene 3 is now running]_

But a workspace can also run a **long-lived service**: a dev server, a database, or, in this case, an AI assistant that stays available around the clock. openclaw's config adds three lines under `workspace`: a `service-command` that launches its gateway, `auto-start: true`, and a `health-check` script.

_[Scroll back to the three lines under `workspace:` — service-command, auto-start: true, health-check]_

When I ran `klangk sandbox openclaw`, the setup installed Node and openclaw, wrote a config that points at the Klangk LLM proxy, and started the gateway automatically. That's the **service command** at work — a per-workspace singleton: it runs once in its own session and is shared with everyone you give access to. And I can see that straight from the command line:

_[Type: klangk ls — the Status column shows openclaw as healthy, in green]_

The Status column shows `openclaw` as **healthy** — the service command is running and its health check is passing. Everything the CLI knows about the workspace, you see right here.

I can even attach to the service command itself — here's the gateway running live.

_[Split the terminal horizontally. In the new pane: klangk shell openclaw clanker:service-cmd — joins the service command's session; gateway logs stream]_

Now here's what turns this from "a process I left running" into an actual service. First, **health checks**. A running container only proves the container is alive — it says nothing about the process inside it. So Klangk runs my health-check command inside the container every ten seconds: exit zero means healthy, anything else is unhealthy — and that status is the very thing lighting up the Status column. Because it's all surfaced as events, I can watch it live from the command line with `klangk monitor`:

_[In the top pane: klangk monitor --type service_health | jq . — a healthy frame arrives immediately on connect]_

There — healthy. And I can even run a command on a change, like firing a Slack alert when the service goes down. So what happens when the service actually breaks?

_[In the bottom pane: Ctrl+C — kills the gateway; its logs stop]_

_[The top pane's monitor emits an unhealthy event within a few seconds]_

There it went **unhealthy** — and I can see exactly why. That's the difference between "the container is up" and "the service is working."

_[Ctrl+C the monitor; close the bottom pane; back to a single terminal]_

Second, **auto-start and recovery**. I've got `KLANGK_ALLOW_AUTOSTART` enabled on the server, so if the server reboots, openclaw's container boots on its own and the gateway is running _before anyone connects_. And the same thing happens any time the container is recreated — the service command re-fires on every fresh container create. So I can show you right now with a per-workspace restart, without taking the whole server down:

_[Host terminal: klangk restart openclaw — a per-workspace restart. Then: watch -n 3 klangk ls — openclaw's Status goes starting → healthy again as the service command re-fires on the fresh container]_

The gateway here is also exposed as a **hosted app** — once we switch to the browser I can click straight through to openclaw's own web UI, proxied through Klangk's single port. No separate port to open, no extra auth to wire up. We'll see that in a moment.

So the same sandbox idea — one config file, one command — scales from "my dev environment" up to "a service that's always on."

> **Production —** _on screen:_ same terminal, continuing — openclaw is up and
> healthy. _pre-roll:_ carries over from Sc 3 (openclaw pre-warmed, autostart on,
> `jq` + LLM proxy working); **`KLANGK_HEALTH_CHECK_INTERVAL=10`** set (snappier
> unhealthy flip; product default 30s; needs a **full** backend restart, off
> camera). _mechanic (the unhealthy beat):_ two-pane split (horizontal
> divider) — bottom pane `klangk shell openclaw clanker:service-cmd` (joins the
> service command; gateway logs stream), top pane `klangk monitor --type
service_health | jq .` (shows a healthy frame immediately via
> snapshot-on-connect). Ctrl+C the **bottom** pane kills the gateway; the
> **top** pane emits `"healthy": false` within ≤ interval (the next health
> check). Then Ctrl+C the monitor, kill the bottom pane, and recover with
> `klangk restart openclaw` (the service command re-fires on the fresh
> container — #1244/#1246). _reset:_ to re-run, `klangk restart openclaw`
> again to get back to healthy before re-breaking.
> _gotchas:_ the unhealthy flip is silent dead air (≤10s at interval=10) — trim in
> edit; the gateway binds a port, so **localhost only**; per-workspace restart
> (not a full-server SIGHUP), so the `demo` workspace and the rest of the
> recording are untouched — scene 3b no longer needs to be recorded last; CLI-only.

## Scene 4 — The Web UI — Workspaces, Terminal, and Hosted Apps (1 minute)

_[Screen: switch to browser, Klangk web UI]_

Now let me continue in the browser. When you open Klangk on the web, you see the same workspaces you saw from the CLI — including the ones we just made. There's `openclaw` still showing its green health icon, and here's `demo`, the workspace we created a moment ago.

First, that hosted app I teased a moment ago. Let me click into `openclaw` and open its Service tab.

_[Click the openclaw workspace, then the Service tab, show the output of the service running]_

There's Openclaw running. We know it's running on port 8000, so let's find out which hosted URL that translates to in klangk.

-[Click the bash tab, type `klangk-hosted-url 8000` into it to display the URL that openclaw is istening on]\_

There's the URL, lets open it in another browser instance.

_[openclaw's own web UI loads]_

There — openclaw's own web UI, proxied through Klangk's single port. No separate port to open, no extra auth to wire up. Let me go back and open `demo`.

_[Return to the workspace list, click the demo workspace card on the "Owned by Me" tab]_

This is a continuation of exactly what we were doing. The terminal here is the same tmux session I had from `klangk shell` — the repo I cloned and the Pi session I ran from the command line are all still here. That's the whole point: the CLI and the web UI are two windows into the same container.

_[Click the "+" next to the terminal tab bar to open a new tab, then double-click the tab name and rename it "scratch"]_

I can create multiple interactive terminal tabs, rename them, close them. And these aren't trapped in the browser — any tab I create here can be connected to from the CLI too, with `klangk shell`. The web UI and the CLI are just two ways into the same sessions.

But the web UI has features beyond the terminal — files, chat, and collaboration. Let me show those.

> **Production —** _on screen:_ browser — workspace list, then the `demo`
> workspace. _pre-roll:_ `demo` from Sc 2 (cloned repo + Pi session in its tmux);
> `openclaw` (Sc 3/3b, green health icon) in the list; Potemkin workspaces seeded.
> _reset:_ none (pure navigation). _gotchas:_ the hosted-app beat opens
> **openclaw**; the rest opens **demo** — don't confuse them; the Sc 2→4
> continuity lands hardest if the cloned repo / Pi scrollback are genuinely still
> in `demo`'s terminal (record Sc 4 right after Sc 2's state is in place); wording
> is "tabs created here **can be connected to from the CLI**", not "show up in the
> CLI". **Do NOT tour the nav tabs** (Files/Chat/Sharing/Settings) here, and **do
> NOT type `echo`/`ls` to manufacture output** — the terminal-continuity beat is
> **showing the existing CLI scrollback**, not adding to it. (See "Interaction on
> camera" above.) The rename beat is right-click the new tab → "Rename" →
> triple-click-select-all in the field → type "scratch" → click OK (all mouse;
> no `Ctrl+A`, no `Enter`).

## Scene 5 — AI Agent — clanker (1.5 minutes)

Still in the `demo` workspace, I'll click over to the Chat tab.

_[Click the Chat tab in the left rail]_

Every workspace comes with a built-in AI agent. By default it's called clanker, and it's available **only through chat** — you talk to it by @mentioning it, not by running it in a terminal yourself.

_[Type: @clanker what is my hostname]_

The agent runs Pi inside the container. It can read and write files, run shell commands, and answer questions — all confined to this workspace's sandbox.

_[Wait ~30s. clanker's reply appears]_

There it is. Now here's something important about the security model. My LLM API key — the key that talks to the AI provider — never enters the container. Klangk runs a reverse proxy (nginx) on the host that injects the key into requests. Inside the container, Pi just talks to a local proxy URL. So even if the container were compromised, the API key isn't there.

_[Click the Terminal tab in the left rail, then type: env, wait for 10 seconds]_

And I can prove it. Here's the full environment of the container — no API keys, no secrets, nothing to steal. The key only exists on the host, in the proxy.

One thing worth being clear about: clanker is a **chat agent**, not a coding-agent harness. It does no tool calling, and you can't add skills or prompts to it — it's a fixed, built-in assistant scoped to the workspace. If what you want is a full harness you can extend and drive yourself, that's the next section.

> **Production —** _on screen:_ browser → Chat tab, still in `demo` (Sc 4).
> _pre-roll:_ agent functional (LLM key working); test the exact prompt
> off-camera. This is a read-only Q&A — clanker answers in chat, creates no
> files, so `demo` is left untouched (the Flask app for Sc 5b/6 is built by pi
> in Sc 5b). _reset:_ none — re-run freely. _gotchas:_ **live/nondeterministic**
> — one long take, leave dead air; needs a working key (proxy 401 kills the
> scene); clanker is a chat-only agent (no tool calling), so an uptime-style
> question is answered from its training data, not by running a command —
> verify the response is acceptable before keeping the take.

## Scene 5b — Debugging with The Pi Harness (~2.5 minutes)

> **Production note:** The Pi interaction here is nondeterministic — Pi's exact
> steps vary take to take. This part is **driven by an agent that pretends to be
> a human operating Pi**: it launches `pi`, sends the debug prompt, and reacts to
> Pi's output as a person would (reading the traceback, watching the fix, then
> inspecting files in the second bash tab). Its **goal is to get pi's Flask
> app into a state where it can be opened in a new browser tab** — i.e. installed
> and running so the hosted-app URL serves the page. The scene culminates in
> doing exactly that. Treat it like the live `pi` beats in Scenes 2 and 6 — one
> long take, leave dead air while Pi works, narrate over later.
>
> Note that the pi session files are in ~/.pi within the container and those contain the conversation with Pi. While you're recording the session you can also use "podman exec" or "klangk exec" and tmux to capture the conversation and respond interactively.

_[Screen: same workspace, Terminal tab]_

Let's use pi to build us an application.

_[Open "pi", type "please build me a Flask hello world application that listens on port 8000. Only write the files — don't install anything." within it]_

Let's actually try to run it.

_[Open the "scratch" terminal tab by mousing to it and clicking it, then type "python3 app.py" into it]_

`ModuleNotFoundError: No module named 'flask'`. A classic agent mistake — the code is there, but the dependency was never installed. I could fix this by hand, but there's a faster way that shows off something important about Klangk.

This container has Pi as an agent — the same engine that powers clanker — but I can run it right here in the terminal, where I can watch it work and step in alongside it.

_[Navigate back to the bash tab where pi is still running]_

_[Type into pi: the Flask app in app.py fails with ModuleNotFoundError when I run `python3 app.py` — install flask so the app runs. You don't need to run the app.]_

Pi reads the traceback, sees Flask is missing, and installs it.

_[Watch Pi: it runs `pip install flask`]_

And here's the part I want you to see. While Pi works in its tab, I can open another bash tab right next to it and inspect what pi actually produced.

_[Go back to the scratch tab]_

_[Type, one at a time: ls — cat app.py — cat requirements.txt]_

There's the app pi wrote, and there's `requirements.txt` — Flask was listed all along, it just never got installed. I can poke around the files myself, double-check Pi's work — all alongside the agent, not instead of it.

Let's run the application now.

_[Navigate to the terminal2 tab and type `python3 app.py`]_

It serves. Now let me open it as a real app in the browser.

_[Open a new browser tab at the hosted URL: the page renders "Hello from Klangk", then return to the workspace]_

There it is. A real running app — written by an agent, broken on first run,
and fixed by the same agent once I pointed it at the error — now
open in my browser, all without leaving the sandbox.

> **Production —** _on screen:_ browser → Terminal tab. THREE terminal tabs
> are open (from Sc 2 + Sc 4): `bash` (pi lives here the whole scene — it is
> never exited), `terminal2` (where the fixed app is run for the reveal), and
> `scratch` (first run attempt → ModuleNotFoundError, then file inspection).
> _pre-roll:_ `demo` workspace present with those three tabs; `pi` functional.
> The Flask app is built on-camera by Pi (the prompt constrains it to write
> files only, so it writes `app.py` + `requirements.txt` but does not
> pip-install), so the `ModuleNotFoundError` happens live in the scratch tab.
> pi is then asked (still in its bash tab) to install flask. The reveal runs
> the app in `terminal2` and opens the hosted URL in a temporary browser tab.
> _reset:_ the scene cleans up a prior take off-camera (`fuser -k 8000/tcp`,
> `rm app.py requirements.txt`, `rm -rf .venv`). _gotchas:_
> **live/nondeterministic**; pi must not be exited between the build and debug
> prompts (we tab away to scratch/terminal2 instead); the app must listen on
> **8000** (the first hosted port — `KLANGK_PORT_MAPPINGS=8000:9065,...`).

## Scene 6 — File Browser (30 seconds)

_[Click the Files tab in the left rail]_

The Files tab gives me a visual file browser. I can see the files clanker created a moment ago — `app.py`, `requirements.txt` — click one for a syntax-highlighted preview, drag and drop to upload, or right-click to download, rename, or delete.

And it previews more than just code. I dropped a PDF in here earlier — the Pyramid web framework documentation — let me open it.

_[Click pyramid-docs.pdf in the file tree — it renders in the viewer pane]_

Klangk renders common formats right in the browser — PDFs, images, spreadsheets, even video — so I can look at them without downloading anything or leaving the workspace.

> **Production —** _on screen:_ browser → Files tab. _pre-roll:_ files from Sc
> 5/5b (`app.py`, `requirements.txt`); record right after 5/5b; **seed the Pyramid
> PDF** (`assets/pyramid-docs.pdf`) into `demo`'s home via `seedDemoFile` against
> an **absolute** container path (`/home/work/pyramid-docs.pdf`) AFTER the
> container boots. _reset:_ none; re-seed the PDF if deleted. _gotchas:_ verify
> the PDF renders off-camera (`PdfRenderer` must handle it); seed after
> `openWorkspaceDemo` (the upload API needs the running container).

## Scene 7 — Collaboration: The Owner's View (~1.5 minutes)

> **Two-scene collaboration arc.** Real multi-user collaboration is shot as
> **two separate single-window recordings** — this scene (the owner,
> `admin@example.com`) and **Scene 7b** (the teammate, `teammate@example.com`)
> — that tell the **same conversation twice, each from one side**. The other
> party's half is driven by a **WebSocket sidechannel** (terminal input into
> the shared terminal; chat messages sent as the other user), so each recording
> shows a coherent solo view. The two clips are then **intercut in the edit**
> (DaVinci) — owner shares → cut to teammate seeing the shared tab appear;
> owner types → cut to teammate watching it land; owner @mentions clanker → cut
> to teammate's reaction. Combined edited length ~1.5–2 min (the two overlap,
> they don't add).
>
> Recording one window at a time sidesteps the prior approach's failure: two
> browser windows fighting for the foreground under the matchbox/Xvfb recorder,
> so the wrong window (the teammate's) was captured instead of the owner's.

_[Screen: browser, the `demo` workspace, the owner's single window. The Flask app from Sc 5b and the chat history from Sc 5 are still here.]_

I've been working solo in the `demo` workspace. Now let me bring the team in.

_[Click the Sharing tab in the left rail]_

I can share this workspace with other users. Klangk has four roles: **Owners** have full control; **Coders** get their own terminal and file access but can only watch shared terminals; **Collaborators** can type in shared terminals alongside the owner; and **Spectators** are read-only — they can watch shared terminals, but can't type or send chat.

_[In the Sharing panel, the teammate is already listed as a Collaborator]_

I've already added my teammate as a Collaborator. Now let me share a terminal so we can pair-program.

_[Right-click the scratch terminal tab → Share — a share badge (cell-tower icon) appears on the tab]_

When I share a terminal tab, the other user sees it appear in their tab bar. They're looking at the same live terminal — this is real pair programming, not screen sharing. Both of us can type, and we both see the same output.

_[Type: echo 'owner typing here' — the line echoes back]_

I can type...

_[The teammate's line appears in the same shared terminal: echo 'teammate typing back' — echoed back]_

...and so can they. One terminal, both of us writing to it.

_[Mouse over the shared-tab viewer count — shows 1 viewer]_

The tab shows a viewer count so I know when someone's watching.

Chat is shared too — everyone in the workspace sees messages in real time, including the AI agent's responses. Let me switch to the Chat tab.

_[Click the Chat tab]_

_[A message from designer@example.com appears: "hey, can we add a simple landing page?"]_

_[A message from reviewer@example.com appears: "yeah — minimal, just a headline and a button"]_

_[Type into the chat box: @clanker scaffold a simple Flask landing page with a headline and a button, then click Send]_

_[Wait up to ~120s for clanker's reply — leave dead air, narrate over later]_

_[A message from teammate@example.com appears: "nice — let's wire that button up next"]_

So you can collaborate with both humans and AI in the same space.

> **Production —** _on screen:_ browser, **single window** — the owner's view of
> `demo`. _pre-roll:_ `demo` shared with `teammate@` as Collaborator (seeded);
> Flask app + chat history from Sc 5/5b present; `designer@`/`reviewer@` seeded.
> _sidechannels (the teammate + designer + reviewer halves of the conversation,
> driven over WS so the owner's solo recording shows a live conversation):_
>
> - **teammate** WS: `join_shared_terminal` (the scratch window the owner
>   shared) → send `terminal_input` `echo 'teammate typing back'\r` (writes to
>   the shared pty; appears in the owner's shared terminal) timed ~2s after the
>   owner's line echoes; later send `chat_send` "nice — let's wire that button up
>   next" ~3s after clanker's reply lands.
> - **designer** WS: `chat_send` "hey, can we add a simple landing page?".
> - **reviewer** WS: `chat_send` "yeah — minimal, just a headline and a button".
>
> _visible (owner, real mouse + keyboard):_ Sharing tab tour; right-click the
> scratch tab → Share (badge appears); type `echo 'owner typing here'`; open
> Chat; type the `@clanker` prompt + click Send. _timing:_ the sidechannel beats must land at
> the _same cadence_ as Scene 7b's mirrored beats so the two clips intercut
> cleanly — keep a shared beat sheet with offsets. _reset:_ unshare / re-share.
> _gotchas:_ share the **scratch** tab (a plain shell), never the `bash` tab
> where pi is still alive from Sc 5b — typing into pi pollutes its context ahead
> of Sc 8; the @-autocomplete must close before Send (trailing space, then click
> the Send button — no `Enter`); verify off-camera that the `terminal_input`
> sidechannel actually lands in the shared pty (the joiner's session must be
> non-read-only — Collaborators satisfy this).

## Scene 7b — Collaboration: The Teammate's View (~1.5 minutes)

> **The mirror of Scene 7** — the same conversation, recorded from the
> **teammate's** single window. The owner's half is sidechanneled. Cut this
> against Scene 7 in the edit (see the arc note in Sc 7).

_[Screen: browser, the `demo` workspace, the teammate's single window. The teammate logs in and opens the workspace the owner just shared with them.]_

My teammate has shared this workspace with me. When I open it, I can see everything the owner's been working on.

_[A shared terminal tab appears in the tab bar — click it to join]_

Here's a terminal the owner shared. I'll join it.

_[The owner's line appears in the shared terminal: echo 'owner typing here' — echoed back]_

The owner's already typing — I can see it live.

_[Type: echo 'teammate typing back' — echoed back]_

And I can type right back. Same terminal, same output, both of us writing.

_[Click the Chat tab]_

_[designer@example.com: "hey, can we add a simple landing page?"]_
_[reviewer@example.com: "yeah — minimal, just a headline and a button"]_
_[owner@example.com: "@clanker scaffold a simple Flask landing page with a headline and a button"]_

The whole team's in the chat — including the owner asking the AI to build something.

_[Wait for clanker's reply]_

_[Type into the chat box: nice — let's wire that button up next, then click Send]_

So I'm pair-programming with the owner and talking to the team and the AI — all in one shared workspace.

> **Production —** _on screen:_ browser, **single window** — the teammate's view
> of `demo`. _pre-roll:_ the owner has shared the workspace + shared the scratch
> terminal before this recording starts (via the Sc 7 setup, or a fresh
> sidechannel setup). _sidechannels (the owner + designer + reviewer halves):_
>
> - **owner** WS: `share_window` (scratch) before the teammate's view loads;
>   send `terminal_input` `echo 'owner typing here'\r` ~2s after the teammate
>   joins the shared terminal; send `chat_send` "@clanker scaffold a simple Flask
>   landing page with a headline and a button" timed with the beat.
> - **designer** + **reviewer** WS: their `chat_send` lines, at the same cadence
>   as Sc 7.
>
> _visible (teammate, real mouse + keyboard):_ log in + open `demo`; click the
> shared tab to join; type `echo 'teammate typing back'`; open Chat; type the
> reaction + click Send. _gotchas:_ the teammate is a **Collaborator** — 3 nav
> tabs only (Terminal / Files / Chat; no Sharing, no Settings) — that is
> correct, not a bug; clanker fires once per recording (each take triggers its
> own reply — fine, you only keep one side's clanker beat in the cut); keep the
> conversation text and cadence identical to Sc 7 so the intercut lines up.

## Scene 8 — Features (45 seconds)

Klangk has a feature system. Features are git repos that can install system packages at image build time, add CLI tools to the container, extend Pi with new tools, or inject UI widgets into the browser. Features can also extend the Flutter/Dart app that composes Klangk itself.

_[Browser: in the demo workspace, type pi and wait for the pi interactive tool to come up. type boingball! into pi → a bouncing ball animates over the UI (pi called the boingball tool the feature registered). Hold on the animation for ~30 seconds]_

For example, the "boingball" feature lets Pi trigger a bouncing ball amimation. The "git-credential" feature adds a browser-based Git authentication dialog. The "celebrate" feature shows confetti.

Features are declared in a YAML file and fetched automatically.

How do features differ from the `klangk sandbox` setup scripts we saw earlier? They're both ways to customize a workspace, but they make a different trade-off.

The downside of features is that they require Klangk itself to be recompiled to pick them up — you can't just add one on the fly. But the payoff is that there's **no startup cost**: a feature is baked into the image at build time, so every workspace that uses that image is ready to go instantly, with no setup script to run on first creation. The feature is available to all workspaces instantly.

> **Production —** _on screen:_ features config + browser. _pre-roll:_ image built
> with a visual feature — **boingball** is the easy payoff;
> `customize/features.yaml` declares it (plus beep, pig-latin, etc.); confirm the
> image was rebuilt so `boingball`'s Pi tool is live. _reset:_ re-trigger boingball.
> _gotchas:_ features are **compile-time** (image rebuild) — you can't add one
> live; build it in ahead of time.

## Scene 9 — Administration (30 seconds)

_[Browser: click the admin link → click through the Users, Groups, Invitations, and ACL tabs, 5 seconds shown apiece]_

The admin panel lets you manage users and groups, send email invitations, and configure global access control. Klangk supports OIDC single sign-on — Google, GitHub, whatever your identity provider is.

> **Production —** _on screen:_ browser → admin panel. _pre-roll:_ admin logged
> in; a couple seeded users/groups so it looks lived-in. _reset:_ none.
> _gotchas:_ no real PII — use seeded demo accounts.

## Scene 10 — Closing (30 seconds)

So that's some of Klangk. For solo developers: sandboxed containers you manage from the CLI, one-command project setup with `klangk sandbox`, SSH agent forwarding so your keys just work, and workspaces that can run always-on services with auto-start and health checks. For teams: shared workspaces, pair programming through shared terminals, real-time chat with an AI agent, and role-based access control. All self-hosted, all open source.

Most containers auto-stop after an idle timeout to save resources, but your files persist. You can get started with a single Docker command or clone the repo and use devenv for development.

Check it out on GitHub — the link is in the description. Thanks for watching.

> **Production —** _on screen:_ title card / logo / GitHub link. _action:_ VO-only.
> _reset:_ n/a.
