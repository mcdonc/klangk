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

| Workspace           | Born in                                    | Owner             | Role in the video                                                                                                                                                                                                                                                |
| ------------------- | ------------------------------------------ | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`demo`**          | Scene 2 (`klangkc create demo`, on camera) | `admin@plope.com` | **Hero.** Kept alive through every scene after. Accumulates: cloned klangk repo + Pi session (Sc 2) → clanker's Flask app `app.py`/`requirements.txt` (Sc 6) → debugged, running app (Sc 6b) → browsed files + Pyramid PDF (Sc 7) → shared with the team (Sc 8). |
| `myproject`         | Scene 3 (`klangkc sandbox myproject`)      | `admin@plope.com` | Self-contained sandbox feature demo. Stays in the list.                                                                                                                                                                                                          |
| `openclaw`          | Scene 4 (`klangkc sandbox openclaw`)       | `admin@plope.com` | Self-contained service feature demo. Stays in the list (green health icon); its Service tab + hosted app are shown in Sc 4.                                                                                                                                      |
| Potemkin workspaces | Pre-seeded (see Accounts below)            | various           | Decorative — fill every account's list so it looks lived-in. Never opened on camera.                                                                                                                                                                             |

**Rules:**

- Whenever a scene says "open a workspace", it means **open the `demo` workspace**.
- **Do not `klangkc rm demo` during the run** — it must survive into the next
  scene. `rm` is mentioned verbally in Sc 2 as the eventual cleanup, not
  executed on `demo`.
- Record the browser arc (Sc 5→6→6b→7→8) **in order**, against the same live
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
      This creates `teammate@`, `designer@`, `reviewer@` (used live in Sc 8) and
      five **Potemkin** workspaces (`Shared Workspace`, `Team Project`,
      `Design Review`, `Teammate Sandbox`, `Design Lab`) spread across the
      accounts. Idempotent — re-run safely.
- [ ] For Sc 8, log `teammate@` into a **second browser session** (incognito /
      separate profile) so two users are live at once. `designer@`/`reviewer@`
      join via WS only (no second window needed).

### Pre-warm the slow, network-dependent installs (do OFF camera)

> Recording a fresh install live is the #1 way to waste a take. These download
> from the internet and can take minutes.

- [ ] **openclaw sandbox:** run `klangkc sandbox openclaw` once off-camera so the
      nvm + Node 24 + openclaw download is done. For the on-camera take you then
      either re-run with `--force` (re-applies config, fast) or just keep the
      workspace and `klangkc restart openclaw` (see Scene 4 reset).
- [ ] **Sandbox scene project:** build a real `.klangk-sandbox.yaml` in a demo
      project dir and run it once off-camera (setup script + mounts verified).
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
klangkc rm myproject       # the sandbox-scene workspace
klangkc rm openclaw        # only if you want a truly fresh Service scene
# then re-run the CLI scenes (2,3,4) to rebuild demo/myproject/openclaw on camera
```

> Because `demo` carries state across scenes, a **mid-arc re-take** (e.g. just
> Sc 7) does NOT need a full reset — re-run from the earliest scene whose state
> it depends on (Sc 6 rebuilds the Flask app) or re-seed just the missing piece
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
  1. `klangkc login admin@plope.com`
  2. `klangkc create demo`
  3. `klangkc shell demo` → show real bash; mention tmux persistence
  4. disconnect → reconnect to prove persistence
  5. `klangkc shell demo -A` (explain `-A` = forward SSH agent, no key copied in)
  6. `git clone git@github.com:mcdonc/klangk.git`
  7. run `pi`, ask it a **simple, reliable** task (see gotcha) → show it work
  8. disconnect with **Enter ~ .** (practice this — it's fiddly on camera)
  9. **do NOT `rm demo`** — narrate `klangkc rm` as the eventual cleanup, but
     keep `demo` alive; every later browser scene depends on it
- **Reset:** `klangkc rm demo && klangkc create demo` — but only for a full
  re-run of the whole arc, since `demo` must survive into Scenes 5–8.
- **Gotchas:**
  - The `pi` interaction is **live and nondeterministic** — re-takes won't match.
    Use a short, predictable prompt; do this scene as one long take; leave dead
    air for the agent to respond (you narrate over it later).
  - `-A` must actually work — test SSH agent forwarding before recording.
  - The `Enter ~ .` escape only triggers right after a newline; fumble it and you
    waste the take. Rehearse.

### Scene 3 — klangkc sandbox: One Command (~1.5 min)

- **On screen:** terminal, `cd`'d into a demo project dir.
- **Pre-roll:** a real `.klangk-sandbox.yaml` in that project (mount project at
  `~/myproject`, bind `.ssh` + `.claude`, a named volume, a setup script). Run it
  once off-camera so the setup script is proven.
- **Action:**
  1. `cat .klangk-sandbox.yaml` — narrate the config
  2. `klangkc sandbox myproject -A` → first run: create + mount + setup + shell
  3. show the project mounted inside; show the second run reconnects instantly
  4. mention: commit the config → any teammate/clone gets the same env
- **Reset:** `klangkc rm myproject` then `klangkc sandbox myproject -A` again.
- **Gotchas:**
  - **Filename is `.klangk-sandbox.yaml` at the project root** — the current
    script text says `.klangk/sandbox.yaml`, which is **wrong**. Fix the script
    (see "Open script-fix" at the bottom) and show the real file on camera.
  - If your setup script is slow/network-y, pre-warm it; show re-connect (2nd
    run) to avoid re-watching the install.

### Scene 4 — Service Workspaces: service-command + auto-start + health (~2 min)

> The showcase scene. Demo via the **openclaw** sandbox (visual: hosted app URL).

- **On screen:** terminal in `sandboxes/openclaw`, then browser, then terminal
  again for `monitor`.
- **Pre-roll:** openclaw **pre-warmed** (Node install done off-camera);
  `KLANGK_ALLOW_AUTOSTART=1` on; `jq` installed; LLM proxy working (so the
  gateway comes up healthy, not red).
- **Action:**
  1. `cd sandboxes/openclaw && cat .klangk-sandbox.yaml` — point at the 3 lines:
     `service-command`, `auto-start: true`, `health-check`
  2. `klangkc sandbox openclaw` (fast if pre-warmed; gateway auto-starts in the
     **Service** tab — that's service-command at work)
  3. browser → workspace → **Service** tab, gateway running
  4. narrate singleton semantics: Ctrl+C stops, up-arrow+Enter restarts, shared
     with everyone who has access
  5. **auto-start:** restart the **whole Klangk server**, show openclaw's
     container booting on its own and the gateway up _before anyone connects_
  6. **health check:** point at the green status icon in the workspace list;
     narrate "exit 0 = healthy, else unhealthy, and you see _why_ (stderr tail)"
  7. terminal: `klangkc monitor --type service_health | jq .` — show live events;
     mention the `-- sh -c '...'` form fires a command (e.g. Slack alert) on change
  8. browser → click the **hosted app** link → land in openclaw's own web UI
- **Reset:**
  - Cleanest re-take that avoids the slow install: **keep** the openclaw workspace
    and `klangkc restart openclaw` (restarts container + gateway). Only
    `klangkc rm openclaw && klangkc sandbox openclaw` if you need truly fresh.
  - The server-restart beat (step 5) is its own clip — restart, trim in edit.
- **Gotchas:**
  - **Never record the first-run install live** — it downloads nvm + Node + the
    app and can stall. Pre-warm, or `--force`/restart.
  - openclaw's gateway binds a port ("contactable by anyone who can reach the
    server"). On `localhost` that's fine; **don't** run this take against a
    public server without auth configured.
  - If health shows **amber/red**, the LLM proxy or config is off — fix before
    recording (green icon is the payoff).
  - Showing the _unhealthy → why_ path requires breaking the service on camera;
    consider skipping it live (the narration covers it) or pre-recording it.

### Scene 5 — Web UI: Workspaces and Terminal (~1 min)

- **On screen:** browser — the workspace list, then **the `demo` workspace**.
- **Pre-roll:** `demo` exists and was created on camera in Scene 2, with the
  cloned klangk repo + a Pi session still in its tmux. `myproject` (Sc 3) and
  `openclaw` (Sc 4, green health icon) are also in the list, plus the Potemkin
  workspaces. This scene is a **continuation** of the CLI — open `demo`, don't
  create anything new.
- **Action:**
  1. land on the workspace list → **open the `demo` workspace** (the hero;
     narrate the others are visible too — `myproject`, `openclaw` still healthy)
  2. show the terminal is the same tmux session from `klangkc shell` — the
     cloned repo / Pi scrollback from Sc 2 are still here (the continuity payoff)
  3. click `+` → create a new terminal tab → rename it
  4. narrate the correction: tabs created here **can be connected to from the
     CLI** with `klangkc shell` — they do NOT auto-"show up" in the CLI; the web
     UI and CLI are two ways into the same sessions
- **Reset:** none (pure navigation).
- **Gotchas:**
  - Open **`demo`**, not openclaw — continuity into Sc 6/6b/7/8 depends on it.
    (openclaw's Service tab was already showcased in Sc 4; here it's just a list
    entry with its health icon.)
  - The Sc 2→5 continuity beat lands hardest if the cloned repo / Pi session are
    genuinely still in `demo`'s terminal — record Sc 5 right after Sc 2's state
    is in place.
  - Get the CLI-connectivity wording right: **"can be connected to from the
    CLI"**, not "shows up in the CLI".

### Scene 6 — AI Agent: clanker (~1.5 min)

- **On screen:** browser → Chat tab, **still in the `demo` workspace** (Sc 5).
- **Pre-roll:** the `demo` workspace from Sc 5, agent **functional** — LLM key
  working, `pi` set up. Test the exact prompt off-camera. clanker's output here
  (`app.py`, `requirements.txt`) must persist for Sc 6b/7 — don't wipe `demo`.
- **Action:**
  1. Chat tab → `@clanker create a simple Flask web app on port 8000 that shows
"Hello from Klangk"` (narrate that clanker is the **built-in agent,
     available only through chat** — you talk to it by @mentioning, not by
     running it yourself. This sets up 6b, where you run Pi directly in the
     terminal because clanker isn't available that way.)
  2. watch clanker create files
  3. narrate the security model: the LLM key **never enters the container** —
     nginx proxy on the host injects it; inside, pi talks to a local proxy URL
  4. **switch to the Terminal tab, type `env`** → show the container's full
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

### Scene 6b — Debugging with Pi (~1.5–2 min)

> The payoff for the agentic model: clanker's app from Scene 6 doesn't run, so
> we hand it to Pi **in the terminal** and debug alongside it. Invents a
> realistic agent failure (missing dependency) and shows Pi + manual inspection
> coexisting in the same workspace.

- **On screen:** browser → Terminal tab (or `klangkc shell`), with Pi running in
  one tab and a plain bash tab open alongside it. (Mention in VO that this works
  with any harness — Pi is what's installed, but you can bring Claude Code,
  Codex, or anything else the same way.)
- **Pre-roll:** the Scene 6 workspace, where clanker has already produced an
  `app.py` **plus** a `requirements.txt` that lists `flask` but was never
  installed. Verify **off camera** that `python app.py` fails with
  `ModuleNotFoundError: No module named 'flask'` — that IS the bug we exploit.
  Have `pi` functional in the container and rehearse the exact prompt once.
- **Action (in order):**
  1. Terminal tab → `python app.py` → show the `ModuleNotFoundError` traceback
     (establish the problem clanker left behind)
  2. open a **new tab**, launch `pi`, ask it: _"clanker's Flask app in app.py
     won't run — figure out why and fix it"_
  3. show Pi reading `app.py`, spotting the missing dep, running
     `pip install -r requirements.txt` (or `pip install flask`), and retrying
  4. **open a second bash tab next to the Pi tab** → `ls`, `cat app.py`,
     `cat requirements.txt` — inspect what the LLM produced (the
     "alongside-the-agent" beat; narrate that you can verify its work yourself)
  5. **success criterion:** open the **hosted-app URL in a new browser tab** →
     the page renders `Hello from Klangk` (Pi's goal was to get clanker's app
     into a state where it opens as a page; the hosted-app URL is proxied
     through Klangk's single port, same as Scene 4). Fall back to
     `curl localhost:8000` only if the hosted URL isn't configured.
- **Reset:** `pip uninstall -y flask` to re-break for another take, or re-run
  Sc 6 (into the same `demo` workspace) so clanker regenerates a known-broken
  `app.py` + `requirements.txt`. Don't spin up a fresh workspace — Sc 7 depends
  on these files living in `demo`.
- **Gotchas:**
  - **Live/nondeterministic** — Pi's exact steps vary take to take. One long
    take, leave dead air while it works, narrate over later. Same discipline as
    Scenes 2 and 6.
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

### Scene 7 — File Browser (~30s)

- **On screen:** browser → Files tab.
- **Pre-roll:** files exist (continuity from Scene 6/6b — the Flask app clanker
  made: `app.py`, `requirements.txt`). Best scene to record right after 6/6b.
  **Also preseed a PDF** into the workspace home before recording so we can show
  inline rendering (see seeding note below).
- **Action:**
  1. browse the home → click a code/text file for a highlighted preview
  2. **click the preseeded PDF → it renders inline** (the `PdfRenderer`; payoff
     beat — Klangk previews rich formats, not just text)
  3. mention drag-drop upload, right-click download/rename/delete
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
  - Record immediately after Scene 6/6b so the Flask app files are present too.
  - Verify the PDF **renders** off-camera before the take (the `PdfRenderer` must
    handle it; a corrupt/minimal PDF may fail to render).
  - The PDF must be seeded **after** the container boots — `seedDemoFile` hits the
    running container via the upload API, so call it post-`openWorkspaceDemo`.

### Scene 8 — Multi-User Collaboration (~1.5 min)

- **On screen:** browser, two sessions side by side (owner + teammate), **in the
  `demo` workspace** (Sc 5–7).
- **Pre-roll:** `teammate@` logged into an incognito/profile window (seeded via
  `demo-seed.ts`); `demo` is shared with them as **Collaborator**. Continuity:
  the Flask app from Sc 6 and the chat history from Sc 6/6b are already in
  `demo` — the team is joining work in progress, not a blank workspace.
- **Action:**
  1. Sharing tab → add teammate as Collaborator (on `demo`)
  2. narrate the four roles (note: **Spectators are read-only now** — can watch
     shared terminals, can't type in them or chat)
  3. right-click a terminal tab → **Share**
  4. teammate's window: the shared tab appears; both see the same output; type
     in one, watch it appear in the other (real pair-programming, not screen
     share)
  5. show presence indicators / viewer count
  6. chat is shared — humans + clanker in the same space
- **Reset:** unshare / re-share, or just redo the clicks.
- **Gotchas:**
  - Two live sessions needed — incognito + normal, or two browser profiles. The
    second user must be a real account (seeded/invited).
  - "Both type" solo is awkward — type in one window, cut to the other reacting.
  - Keep the spectator description consistent with the (fixed) script: read-only.

### Scene 9 — Plugins (~45s)

- **On screen:** plugins config + browser.
- **Pre-roll:** image built with a visual plugin — **celebrate** (confetti) is
  the easy payoff. Optionally mention `git-credential`, `claude-code`.
- **Action:** show the plugins declaration → trigger celebrate (confetti).
- **Reset:** re-trigger confetti.
- **Gotchas:** plugins are **compile-time** (image rebuild) — you can't add one
  live. Build it in ahead of time. Confirm the confetti trigger works.

### Scene 10 — Administration (~30s)

- **On screen:** browser → admin panel.
- **Pre-roll:** admin logged in; a couple seeded users/groups so it looks lived-in.
- **Action:** users & groups → invitations → mention OIDC SSO (Google/GitHub/IdP)
  → note everything is one port (8995) behind nginx.
- **Reset:** none.
- **Gotchas:** avoid showing real emails/PII — use seeded demo accounts.

### Scene 11 — Closing (~30s)

- **On screen:** title card / logo / GitHub link.
- **Action:** VO-only.
- **Reset:** n/a.

---

## Recording workflow (video-first, VO second)

1. **Capture silent video, one clip per scene**, using the resets above. Don't
   aim for perfect narration while recording — you'll VO later.
2. **Leave headroom/tails** on each clip (a beat of nothing before and after) so
   editing and VO alignment have slack. Leave _dead air while the agent works_
   (Scenes 2, 6, 6b) — you'll narrate over it.
3. **Re-take discipline:** if a scene flubs, reset per the block and re-record
   just that clip; don't restart the whole video.
4. **Rough cut** the clips to the ~14-min structure, then **record voiceover**
   in a single quiet session reading `videoscript.md` against the cut.
5. A cheap-ish mic in a quiet room + a pop filter is plenty for VO; record VO as
   a separate audio track and align to picture in the editor.
