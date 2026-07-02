# Recording / Shot Checklist — Klangk Intro Video

Companion to `videoscript.md`. Worked **silent video first, voiceover second**,
so every scene below is about capturing clean screen footage per scene, with room
left in the pacing for narration later.

- **Target runtime:** ~13.5 min (matches `videoscript.md` timings; Scene 6b adds ~1.5–2 min).
- **Edit unit:** one clip per scene (easy to re-take a single flubbed scene).
- **Scope:** silent screen capture now → record VO against the cut → align.

---

## 0. Before you record — global setup (do once)

### Continuity & workspace map — read this first

This video is **one continuous story across a single evolving workspace**,
not a series of independent demos. State accumulates shot to shot:

| Workspace           | Born in                                    | Owner               | Role in the video                                                                                                                                                                                                                                                |
| ------------------- | ------------------------------------------ | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`demo`**          | Scene 2 (`klangkc create demo`, on camera) | `admin@example.com` | **Hero.** Kept alive through every scene after. Accumulates: cloned klangk repo + Pi session (Sc 2) → clanker's Flask app `app.py`/`requirements.txt` (Sc 5) → debugged, running app (Sc 5b) → browsed files + Pyramid PDF (Sc 6) → shared with the team (Sc 7). |
| `openclaw`          | Scene 3 (`klangkc sandbox openclaw`)       | `admin@example.com` | Self-contained sandbox + service feature demo. Stays in the list (green health icon); its Service tab + hosted app are shown in Sc 3.                                                                                                                            |
| Potemkin workspaces | Pre-seeded (see Accounts below)            | various             | Decorative — fill every account's list so it looks lived-in. Never opened on camera.                                                                                                                                                                             |

**Rules:**

- Whenever a scene says "open a workspace", it means **open the `demo` workspace**.
- **Do not `klangkc rm demo` during the run** — it must survive into the next
  scene. `rm` is mentioned verbally in Sc 2 as the eventual cleanup, not
  executed on `demo`.
- Record the browser arc (Sc 4→5→5b→6→7) **in order**, against the same live
  `demo` container, so clanker's files / chat history / running app carry
  forward.
- **Implementation note (code):** the Playwright scenes currently each spin up
  their _own_ fresh workspace + user (`webui-demo`, `clanker-demo`, `files-demo`,
  `collab-demo`). To realize continuity they must be refactored to share **one**
  hero workspace (`demo`) owned by `admin@plope.com`, seeding state forward
  between scenes instead of `ensureFreshWorkspace`-ing each time. See
  `demo-helpers.ts` (`ensureFreshWorkspace` / `openWorkspaceDemo`).

### Demo environment

- [ ] Klangk server running locally; reach it at `http://localhost:8995`.
- [ ] `KLANGK_ALLOW_AUTOSTART=1` set on the server (required for the
      Service-Workspaces scene; otherwise the option is hidden).
- [ ] **Real LLM key configured** for the proxy. The clanker chat scene AND the
      openclaw scene both depend on the LLM proxy actually working. Test it.
- [ ] `jq` installed locally (for the `klangkc monitor | jq .` beat).

### Accounts & Potemkin workspaces (seed once, before recording)

- [ ] **Owner / on-camera user:** `admin@plope.com` (the server's
      `KLANGK_DEFAULT_USER`). The hero `demo` workspace is owned by this account
      and created **on camera** in Scene 2 — do not pre-seed `demo`.
- [ ] **Run the seed script once** to create the supporting cast + decorative
      workspaces so every list looks populated:
      `bash
KLANGK_DEMO_ADMIN_EMAIL=admin@plope.com \
devenv shell -- node --experimental-strip-types \
    src/frontend/e2e-tests/demo/demo-seed.ts
`
      This creates `teammate@`, `designer@`, `reviewer@` (used live in Sc 7) and
      five **Potemkin** workspaces (`Shared Workspace`, `Team Project`,
      `Design Review`, `Teammate Sandbox`, `Design Lab`) spread across the
      accounts. Idempotent — re-run safely.
- [ ] For Sc 7, log `teammate@` into a **second browser session** (incognito /
      separate profile) so two users are live at once. `designer@`/`reviewer@`
      join via WS only (no second window needed).

### Pre-warm the slow, network-dependent installs (do OFF camera)

> Recording a fresh install live is the #1 way to waste a take. These download
> from the internet and can take minutes.

- [ ] **openclaw sandbox:** run `klangkc sandbox openclaw` once off-camera so the
      nvm + Node 24 + openclaw download is done. For the on-camera take you then
      either re-run with `--force` (re-applies config, fast) or just keep the
      workspace and `klangkc restart openclaw` (see Scene 3 reset).
- [ ] **Sandbox scene project:** build a real `.klangk-sandbox.yaml` in a demo
      project dir and run it once off-camera (setup script + mounts verified).
      (openclaw now serves double duty — it's both the sandbox AND the service
      demo in Scene 3.)
- [ ] **A repo to clone** in the CLI scene: clone Klangk itself —
      `git@github.com:mcdonc/klangk.git`. Verify `ssh-add -l` shows your key
      so `-A` works.

### Recording tooling & hygiene

- [ ] Recorder: OBS (or equivalent). Capture a fixed window or region, not
      fullscreen with the menubar.
- [ ] **Font size up** in both terminal and browser (legibility on small
      screens). Bump terminal font to ~18–20pt.
- [ ] **Do Not Disturb** on (kill notifications, Slack popups, calendar toasts).
- [ ] Clean browser: hide bookmarks bar, blank new-tab, neutral wallpaper.
- [ ] **No real secrets on screen:** use the demo account, fake emails, and never
      show a token/key. The LLM proxy keeps the key off-screen by design — good
      thing to say, not show.
- [ ] Decide talking-head vs. title-card for Opening/Closing (VO-only scenes).

### Master reset (between full re-runs)

```bash
klangkc rm demo            # hero workspace — wipes the accumulated state
klangkc rm openclaw        # only if you want a truly fresh sandbox/service scene
# then re-run the CLI scenes (2,3) to rebuild demo/openclaw on camera
```

> Because `demo` carries state across scenes, a **mid-arc re-take** (e.g. just
> Sc 6) does NOT need a full reset — re-run from the earliest scene whose state
> it depends on (Sc 5 rebuilds the Flask app) or re-seed just the missing piece
> (e.g. drop the Pyramid PDF back in via `seedDemoFile`). Only full re-runs wipe
> `demo`.

---

## Per-scene shot list

> Each block: **what the viewer sees → pre-roll → action → reset → gotchas.**
> Scene numbers match the headings in `videoscript.md`.

### Scene 1 — Opening: What is Klangk? (~1 min)

- **On screen:** title card / logo, or talking head. Minimal screen content.
- **Pre-roll:** none.
- **Action:** VO-only; this is narration over a static/title shot.
- **Reset:** n/a.
- **Gotchas:** none — record VO to picture after the cut is locked.

### Scene 2 — CLI: Creating Your First Workspace (~2 min)

- **On screen:** local terminal, `klangkc` installed, clean prompt.
- **Pre-roll:** confirm `ssh-add -l` lists your key; repo to clone is
  `git@github.com:mcdonc/klangk.git`; have `pi` functional inside a workspace
  (test the agent prompt once off-camera).
- **Action (in order):**
  1. `klangkc login admin@example.com`
  2. `klangkc create demo`
  3. `klangkc shell demo` → show real bash; mention tmux persistence
  4. disconnect → reconnect to prove persistence
  5. `klangkc shell demo -A` (explain `-A` = forward SSH agent, no key copied in)
  6. `git clone git@github.com:mcdonc/klangk.git`
  7. run `pi`, ask it a **simple, reliable** task (see gotcha) → show it work
  8. **split the host tmux pane side-by-side** → in the new pane run
     `klangkc shell demo logs` (a **named window** — connects to a separate
     terminal, "logs", not the active one) → `ls` to show it's the same
     workspace (the cloned repo is visible). Narrate: two CLI terminals, one
     workspace, each its own independent shell.
  9. disconnect **both** panes with **Enter ~ .** (second pane first, then the
     first) — practice the escape, it's fiddly on camera
  10. `klangkc ls` — back at the host prompt, show the workspace list (`demo`
      is there; narrate this is how you see everything at a glance)
  11. **do NOT `rm demo`** — narrate `klangkc rm` as the eventual cleanup, but
      keep `demo` alive; every later browser scene depends on it
- **Reset:** `klangkc rm demo && klangkc create demo` — but only for a full
  re-run of the whole arc, since `demo` must survive into Scenes 4–7.
- **Gotchas:**
  - The `pi` interaction is **live and nondeterministic** — re-takes won't match.
    Use a short, predictable prompt; do this scene as one long take; leave dead
    air for the agent to respond (you narrate over it later).
  - `-A` must actually work — test SSH agent forwarding before recording.
  - The `Enter ~ .` escape only triggers right after a newline; fumble it and you
    waste the take. Rehearse.
  - The split-pane beat connects to a **named** window (`logs`) — the driver
    splits the recorder's tmux session and runs `klangkc shell demo logs` in the
    new pane. The split itself is a tmux control call, so it never appears as
    typed text in the recording. Both panes share one workspace; disconnect each
    independently.

### Scene 3 — klangkc sandbox: one command, dev env to always-on service (~3 min)

> The showcase scene. Demo the sandbox concept AND the service features
> (service-command, auto-start, health) in one continuous, **CLI-only** flow
> via the **openclaw** sandbox. The hosted-app payoff is deferred to the
> browser (Scene 4); here it is a narration-only tease. No browser beats.

- **On screen:** local terminal, at the klangk repo root.
- **Pre-roll:** openclaw **pre-warmed** (Node install done off-camera);
  `KLANGK_ALLOW_AUTOSTART=1` on; `jq` installed; LLM proxy working (so the
  gateway comes up healthy, not red). Confirm `klangkc ls` shows a **Status**
  column before recording (it must, post-#1207).
- **Action (all CLI — no browser):**
  1. `cat sandboxes/openclaw/.klangk-sandbox.yaml` — narrate the config: mounts
     the project at a fixed path and runs a setup script. Then point at the 3
     service lines under `workspace:`: `service-command`, `auto-start: true`,
     `health-check`.
  2. `klangkc sandbox openclaw sandboxes/openclaw` — creates the workspace,
     mounts everything, runs setup, starts the container (fast if pre-warmed).
     The gateway auto-starts in its service session — that is service-command at
     work; no browser needed to prove it.
  3. `klangkc shell openclaw` — connect, show the project mounted inside; then
     disconnect with **Enter ~ .**
  4. narrate the sandbox idea: commit the config → any teammate or future-you
     runs the same command and gets the exact same env (a Dockerfile for your
     dev environment, lifecycle managed for you).
  5. narrate the service-command concept: a per-workspace singleton — it runs
     once in its own session and is shared with everyone who has access.
     (Scroll back to the three `workspace:` lines while you say this.)
  6. `klangkc ls` — the **Status** column shows `openclaw` as **healthy**
     (green). Narrate: the service command is running and its health check is
     passing — everything the CLI knows about the workspace, right here.
  7. **auto-start:** in the host terminal run `devenv processes restart backend`,
     then `klangkc ls` again — `openclaw`'s Status goes `starting` → `healthy`
     without anyone connecting. Narrate: with `KLANGK_ALLOW_AUTOSTART` on, the
     container and gateway boot on their own after a server restart.
  8. **health check:** `klangkc monitor --type service_health | jq .` — show
     live events (a `service_health` frame arrives immediately on connect,
     thanks to the snapshot-on-connect fix #1210, so you don't wait on a
     transition). Narrate: exit 0 = healthy, anything else = unhealthy, and you
     see _why_; **Ctrl+C** to stop. Mention the `-- sh -c '...'` form fires a
     command (e.g. a Slack alert) on change.
  9. **hosted-app tease (narration only):** the gateway is also exposed as a
     hosted app — once we switch to the browser (Scene 4) we can click straight
     through to openclaw's own web UI, proxied through Klangk's single port.
     **Do NOT open the browser here.**
- **Reset:**
  - Cleanest re-take that avoids the slow install: **keep** the openclaw
    workspace and `klangkc restart openclaw` (restarts container + gateway).
    Only `klangkc rm openclaw && klangkc sandbox openclaw` if you need truly
    fresh.
  - The server-restart beat (step 7) is its own clip — restart, trim in edit.
- **Gotchas:**
  - **Never record the first-run install live** — it downloads nvm + Node + the
    app and can stall. Pre-warm, or `--force`/restart.
  - openclaw's gateway binds a port ("contactable by anyone who can reach the
    server"). On `localhost` that's fine; **don't** run this take against a
    public server without auth configured.
  - If `klangkc ls` shows `unhealthy` / a stuck `starting`, the LLM proxy or
    config is off — fix before recording (green Status is the payoff).
  - Showing the _unhealthy → why_ path requires breaking the service on camera;
    consider skipping it live (the narration covers it) or pre-recording it.
  - This scene is **CLI-only** — resist cutting to the browser. The hosted-app
    click-through lands in Scene 4; here it is just a verbal tease.

### Scene 4 — Web UI: Workspaces and Terminal (~1 min)

- **On screen:** browser — the workspace list, then **the `demo` workspace**.
- **Pre-roll:** `demo` exists and was created on camera in Scene 2, with the
  cloned klangk repo + a Pi session still in its tmux. `openclaw` (Sc 3, green
  health icon) is also in the list, plus the Potemkin workspaces. This scene is
  a **continuation** of the CLI — open `demo`, don't create anything new.
- **Action:**
  1. land on the workspace list (narrate: the same workspaces you saw from the
     CLI — `openclaw` still showing its green health icon, plus `demo` from
     Sc 2)
  2. **hosted-app payoff (from the Sc 3 tease):** click the **openclaw**
     workspace → **Service** tab → click **"Open hosted app"** → land in
     openclaw's own web UI. Narrate: proxied through Klangk's single port, no
     separate port to open, no extra auth to wire up. Close/return to the list.
  3. **open the `demo` workspace** (the hero for the rest of the browser arc)
  4. show the terminal is the same tmux session from `klangkc shell` — the
     cloned repo / Pi scrollback from Sc 2 are still here (the continuity payoff)
  5. click `+` next to the terminal tab bar → a new tab opens → double-click
     its name → rename it "scratch"
  6. narrate the correction: tabs created here **can be connected to from the
     CLI** with `klangkc shell` — they do NOT auto-"show up" in the CLI; the web
     UI and CLI are two ways into the same sessions
- **Reset:** none (pure navigation).
- **Gotchas:**
  - The hosted-app beat (step 2) opens **openclaw**; the rest of the scene
    (steps 3–6) opens **`demo`**. Don't confuse the two on camera — openclaw is
    just the hosted-app payoff, then switch to `demo` for the terminal arc.
    (openclaw's service/health was already shown via `klangkc ls` + `monitor` in
    the CLI-only Sc 3; the Service tab here is only for the hosted-app
    click-through.)
  - The Sc 2→4 continuity beat lands hardest if the cloned repo / Pi session
    are genuinely still in `demo`'s terminal — record Sc 4 right after Sc 2's
    state is in place.
  - Get the CLI-connectivity wording right: **"can be connected to from the
    CLI"**, not "shows up in the CLI".

### Scene 5 — AI Agent: clanker (~1.5 min)

- **On screen:** browser → Chat tab, **still in the `demo` workspace** (Sc 4).
- **Pre-roll:** the `demo` workspace from Sc 4, agent **functional** — LLM key
  working, `pi` set up. Test the exact prompt off-camera. clanker's output here
  (`app.py`, `requirements.txt`) must persist for Sc 5b/6 — don't wipe `demo`.
- **Action:**
  1. Chat tab → `@clanker create a simple Flask web app on port 8000 that shows
"Hello from Klangk"` (narrate that clanker is the **built-in agent,
     available only through chat** — you talk to it by @mentioning, not by
     running it yourself. This sets up 5b, where you run Pi directly in the
     terminal because clanker isn't available that way.)
  2. wait ~10s → clanker's reply + tool-call lines appear as it creates
     `app.py` / `requirements.txt`
  3. narrate the security model: the LLM key **never enters the container** —
     nginx proxy on the host injects it; inside, pi talks to a local proxy URL
  4. **click the Terminal tab in the left rail, type `env`** → show the container's full
     environment with **no API keys / no secrets** (proves the claim in step 3;
     narrate: "the key only exists on the host, in the proxy")
  5. narrate: after the @mention, follow-ups auto-route to clanker until someone
     else speaks or you @mention another user
- **Reset:** hard — clanker mutated `demo`. To re-take just this scene, manually
  delete `app.py`/`requirements.txt` (and `pip uninstall flask`) so the next
  clanker run is clean; only `klangkc rm demo && klangkc create demo` for a full
  arc re-run (then redo Sc 2's clone/pi first).
- **Gotchas:**
  - **Live/nondeterministic** like Scene 2's `pi` beat — one long take, simple
    prompt, leave silence while it works (narrate over later).
  - Needs a working key — if the proxy 401s, the scene dies. Test first.
  - For the `env` beat (step 4): make sure the container env genuinely contains
    **no secrets** before recording — check nothing in the image or startup
    script exports a token/key. The whole point is a clean `env` output.

### Scene 5b — Debugging with Pi (~1.5–2 min)

> The payoff for the agentic model: clanker's app from Scene 5 doesn't run, so
> we hand it to Pi **in the terminal** and debug alongside it. Invents a
> realistic agent failure (missing dependency) and shows Pi + manual inspection
> coexisting in the same workspace.

- **On screen:** browser → Terminal tab (or `klangkc shell`), with Pi running in
  one tab and a plain bash tab open alongside it. (Mention in VO that this works
  with any harness — Pi is what's installed, but you can bring Claude Code,
  Codex, or anything else the same way.)
- **Pre-roll:** the Scene 5 workspace, where clanker has already produced an
  `app.py` **plus** a `requirements.txt` that lists `flask` but was never
  installed. Verify **off camera** that `python app.py` fails with
  `ModuleNotFoundError: No module named 'flask'` — that IS the bug we exploit.
  Have `pi` functional in the container and rehearse the exact prompt once.
- **Action (in order):**
  1. Terminal tab (or `klangkc shell demo`) → type `python app.py` → show the
     `ModuleNotFoundError` traceback (establish the problem clanker left behind)
  2. click `+` to open a **new terminal tab** → launch `pi`, ask it: _"clanker's
     Flask app in app.py won't run — figure out why and fix it"_
  3. watch Pi: reads `app.py`, spots the missing dep, runs
     `pip install -r requirements.txt` (or `pip install flask`), retries the app
  4. **click `+` for a second terminal tab next to the Pi tab** → type, one at
     a time: `ls`, `cat app.py`, `cat requirements.txt` — inspect what the LLM
     produced (the "alongside-the-agent" beat; narrate that you can verify its
     work yourself)
  5. **success criterion:** browser → open the `demo` workspace → click the
     hosted-app button (or paste the URL from `klangk-hosted-url 8000`:
     `http://localhost:8995/hosted/<workspace_id>/<host_port>/`) → the page
     renders `Hello from Klangk`. Fall back to `curl localhost:8000` only if the
     hosted URL isn't configured.
- **Reset:** `pip uninstall -y flask` to re-break for another take, or re-run
  Sc 5 (into the same `demo` workspace) so clanker regenerates a known-broken
  `app.py` + `requirements.txt`. Don't spin up a fresh workspace — Sc 6 depends
  on these files living in `demo`.
- **Gotchas:**
  - **Live/nondeterministic** — Pi's exact steps vary take to take. One long
    take, leave dead air while it works, narrate over later. Same discipline as
    Scenes 2 and 5.
  - If typing into the **browser** terminal is flaky (FocusNode focus), drive
    this scene via `klangkc shell` / a real tmux session instead — far more
    reliable for live agent interaction than the web terminal.
  - Make sure the _initial_ failure is the missing dep, not something Pi caused
    on a prior take — reset `flask` between takes.
  - The "inspect" bash tab is the key visual: keep Pi's tab and the bash tab
    both visible (side by side, or quick cuts) so the viewer sees agent + human
    working in parallel.
  - The **hosted-app URL must be live** for the payoff beat — the app has to
    bind the expected container port and be mapped to a host port via
    `KLANGK_PORT_MAPPINGS`. Verify the URL opens to the page **off camera**
    before recording; if it's not set up, fall back to `curl localhost:8000`
    (weaker payoff) or cut this beat.
  - This scene is driven by a **pretend-human agent** (see the production note in
    `videoscript.md`) — it operates Pi and reacts to its output as a person
    would. Treat its takes as live/nondeterministic; do one long take and
    narrate over the dead air later.

### Scene 6 — File Browser (~30s)

- **On screen:** browser → Files tab.
- **Pre-roll:** files exist (continuity from Scene 5/5b — the Flask app clanker
  made: `app.py`, `requirements.txt`). Best scene to record right after 5/5b.
  **Also preseed a PDF** into the workspace home before recording so we can show
  inline rendering (see seeding note below).
- **Action:**
  1. browse the home → click `app.py` (or `requirements.txt`) for a
     syntax-highlighted preview
  2. **click `pyramid-docs.pdf` in the file tree → it renders inline** (the
     `PdfRenderer`; payoff beat — Klangk previews rich formats, not just text)
  3. narrate drag-drop upload (drag a file onto the tree), right-click a file →
     Download / Rename / Delete
- **Reset:** none (pure navigation). Re-seed the PDF if it was deleted.
- **Seeding the PDF (pre-roll):** the Pyramid web framework docs PDF is shipped
  in the demo dir at **`assets/pyramid-docs.pdf`** (5.4 MB, a real, richly
  rendered PDF — reads great on camera). Seed it into `demo`'s home via the
  `seedDemoFile` helper against an **absolute** container path (the upload API's
  `validate_path` rejects non-absolute paths, `files.py:20`; container home is
  `/home/work`, `Dockerfile.base:80`):

  ```ts
  import { readFileSync } from "fs";
  import path from "path";
  const pdf = readFileSync(
    path.join(__dirname, "..", "assets", "pyramid-docs.pdf"),
  );
  await seedDemoFile(
    request,
    ws.id,
    "/home/work/pyramid-docs.pdf",
    pdf,
    headers,
    "application/pdf",
  );
  ```

  Do this **after** the container is up (after `openWorkspaceDemo` with
  `waitForTerminal: true`).

- **Gotchas:**
  - Record immediately after Scene 5/5b so the Flask app files are present too.
  - Verify the PDF **renders** off-camera before the take (the `PdfRenderer` must
    handle it; a corrupt/minimal PDF may fail to render).
  - The PDF must be seeded **after** the container boots — `seedDemoFile` hits the
    running container via the upload API, so call it post-`openWorkspaceDemo`.

### Scene 7 — Multi-User Collaboration (~1.5 min)

- **On screen:** browser, two sessions side by side (owner + teammate), **in the
  `demo` workspace** (Sc 4–6).
- **Pre-roll:** `teammate@` logged into an incognito/profile window (seeded via
  `demo-seed.ts`); `demo` is shared with them as **Collaborator**. Continuity:
  the Flask app from Sc 5 and the chat history from Sc 5/5b are already in
  `demo` — the team is joining work in progress, not a blank workspace.
- **Action:**
  1. click the **Sharing** tab in the left rail → type `teammate@example.com`
     in the add-user field → pick the **Collaborator** role (people icon) →
     click **Add**
  2. narrate the four roles (note: **Spectators are read-only now** — can watch
     shared terminals, can't type in them or chat)
  3. right-click a terminal tab → click **Share** — a share badge appears
  4. teammate's window: the shared tab appears; both see the same output; type
     in one, watch it appear in the other (real pair-programming, not screen
     share)
  5. hover the top **presence bar** and the shared-tab **viewer count**
  6. chat is shared — humans + clanker in the same space
- **Reset:** unshare / re-share, or just redo the clicks.
- **Gotchas:**
  - Two live sessions needed — incognito + normal, or two browser profiles. The
    second user must be a real account (seeded/invited).
  - "Both type" solo is awkward — type in one window, cut to the other reacting.
  - Keep the spectator description consistent with the (fixed) script: read-only.

### Scene 8 — Plugins (~45s)

- **On screen:** plugins config + browser.
- **Pre-roll:** image built with a visual plugin — **celebrate** (confetti) is
  the easy payoff. `customize/plugins.yaml` is already declared with `celebrate`
  (plus `beep`, `pig-latin`, `word-count`, `browser-fetch`, `bobdobbs`); confirm
  the image was rebuilt so `celebrate`'s Pi tool is live. Optionally mention
  `git-credential` (browser Git auth dialog).
- **Action:**
  1. terminal: `cat customize/plugins.yaml` — narrate the declaration format
     (one entry per plugin: `name`, `git`, `path`, `ref`); point at the
     `celebrate` entry
  2. browser → `demo` workspace → **Chat** tab → type `@clanker celebrate!` →
     confetti animates over the UI (clanker called the `celebrate` tool the
     plugin registered with Pi)
- **Reset:** re-trigger confetti.
- **Gotchas:** plugins are **compile-time** (image rebuild) — you can't add one
  live. Build it in ahead of time. Confirm the confetti trigger works.

### Scene 9 — Administration (~30s)

- **On screen:** browser → admin panel.
- **Pre-roll:** admin logged in; a couple seeded users/groups so it looks lived-in.
- **Action:** click the **admin** link → click through the **Users**, **Groups**,
  **Invitations**, and **ACL** tabs → narrate OIDC SSO (Google / GitHub / IdP)
  → note everything is proxied through the single nginx port (8995).
- **Reset:** none.
- **Gotchas:** avoid showing real emails/PII — use seeded demo accounts.

### Scene 10 — Closing (~30s)

- **On screen:** title card / logo / GitHub link.
- **Action:** VO-only.
- **Reset:** n/a.

---

## Recording workflow (video-first, VO second)

1. **Capture silent video, one clip per scene**, using the resets above. Don't
   aim for perfect narration while recording — you'll VO later.
2. **Leave headroom/tails** on each clip (a beat of nothing before and after) so
   editing and VO alignment have slack. Leave _dead air while the agent works_
   (Scenes 2, 5, 5b) — you'll narrate over it.
3. **Re-take discipline:** if a scene flubs, reset per the block and re-record
   just that clip; don't restart the whole video.
4. **Rough cut** the clips to the ~14-min structure, then **record voiceover**
   in a single quiet session reading `videoscript.md` against the cut.
5. A cheap-ish mic in a quiet room + a pop filter is plenty for VO; record VO as
   a separate audio track and align to picture in the editor.
