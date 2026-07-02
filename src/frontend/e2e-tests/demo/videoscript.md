# Klangk Introduction Video Script (~14 minutes)

> **Production note — continuity.** This video tells one continuous story
> across a single evolving workspace, **`demo`**, created on camera in "The
> CLI" scene and kept alive through every scene after. Each shot builds on the
> state of the last: the repo cloned and the Pi session from the CLI scene are
> still there when we reopen `demo` in the browser; clanker's Flask app
> (`app.py`, `requirements.txt`) is what we debug with Pi and then browse in
> the Files scene; and we bring the team into that same workspace for
> collaboration. Another workspace — `openclaw` (the sandbox + service scene)
> — coexists in the list but is a self-contained feature demo. The list is also
> pre-populated with a few decorative "Potemkin" workspaces per account so it
> looks lived-in (see the shotlist's global setup). Whenever a scene says "open
> a workspace", it means **open the `demo` workspace**.

## Opening — What is Klangk? (1 minute)

Hey everyone. Today I'm going to show you Klangk — an open-source platform for running AI coding agents in sandboxed containers.

I'm sure if you're a programmer or programmer adjacent, you know things are getting a little scary. AI harnesses like Claude Code and Codex are incredibly productive, but they need broad permissions — they read and write your files, and run shell commands. Most harnesses pretend to have "safety" modes but nothing is really safe about them.

Klangk lets you quickly give every project its own isolated Linux container without a lot of ceremony — agents can do whatever they need in there without touching your host system.

If you're a solo developer, you'll probably interact with Klangk mostly via its CLI, which lets you create workspaces, login via an SSH-like interface, mount directories from your home system, copy files between your host system and the container, and use version control like GitHub.

Meanwhile, if you're on a team, you can use Klangk to share workspaces, pair-program in shared terminals, and chat alongside your AI through a web browser.

I'll start by showing the solo workflow from the command line, then move to the web UI for the team features. Let me show you how it works.

## The CLI — Creating Your First Workspace (2 minutes)

_[Screen: local terminal, klangkc already installed]_

The CLI is called `klangkc`. You install it with pip and point it at your Klangk server. Let me log in.

_[Type: klangkc login admin@example.com]_

Now let me create a workspace.

_[Type: klangkc create demo]_

That's it — a fresh Linux container with Python, Node, git, and build tools is now running. Let me drop into it.

_[Type: klangkc shell demo]_

This is an SSH-like connection into an Ubuntu container. I'm in a real bash shell on Linux, regardless of what my local machine is. Under the hood this is backed by tmux, so if I disconnect and reconnect, everything is exactly where I left it — same scrollback, same running processes.

Let me do something useful. I'll clone a repo.

_[Type: klangkc shell demo -A]_

The `-A` flag forwards my local SSH agent into the container, so I can use my GitHub SSH keys without copying any private keys into the container.

_[Type: ssh-add -l]_

See? My keys are here — forwarded from my host, never copied into the container, although this doesnt happen over literal SSH. Instead the forwarding happens over HTTP(S).

_[Type: git clone git@github.com:mcdonc/klangk.git]_

I can also use harnesses like Pi and claude-code inside the container.

_[Type: cd klangk, then: pi -p "In two sentences, what does this codebase do?"]_

There it is. I can work on this project, run tests, use AI agents — all inside the sandbox. My host system is untouched.

And I'm not limited to a single terminal — I can open more than one window into the same workspace. Let me split my screen and connect again, this time to a named window.

_[Split the terminal into two side-by-side panes. In the new pane, type: klangkc shell demo logs]_

Now I've got two terminals open to the same workspace, side by side. The second one connected to a separate, named window — "logs". If I list the files here, it's the same workspace: the repo I cloned is right there.

_[In the second pane, type: ls]_

Each connection is its own independent shell, and a named window like this shows up as a tab in the web UI too. To disconnect either one, I use the SSH-style escape: Enter, tilde, dot.

_[Disconnect the second pane with ~. , then the first]_

The container keeps running. Back at my host prompt, I can see all my workspaces with `klangkc ls` — there's `demo`, the one we just made.

_[Type: klangkc ls]_

And when I'm eventually done with a workspace, `klangkc rm` tears it down and cleans up its files. But I'm going to keep this one around — we'll come back to it from the browser in a minute.

## klangkc sandbox — One Command to Rule Them All (3 minutes)

_[Screen: local terminal, in a project directory]_

Creating workspaces manually is fine, but the real power for solo developers is `klangkc sandbox`. You check a config file into your repo, and then one command sets up everything.

We have a sandbox config for `openclaw` in our repository.

_[Type: cat sandboxes/openclaw/.klangk-sandbox.yaml — point at the mount-at and setup lines]_

Here's what a sandbox config looks like — it mounts the project at a fixed path inside the container and runs a setup script to get it ready. When I run `klangkc sandbox openclaw`, it creates the workspace, mounts everything, and starts the container.

_[Type: klangkc sandbox openclaw sandboxes/openclaw]_

The idea is that you commit this config file to your repo. Any teammate — or future you on a different machine — runs the same command and gets the exact same environment. It's like a Dockerfile for your dev environment, but the container lifecycle is managed for you.

You can connect to the workspace with `klangkc shell`.

_[Type: klangkc shell openclaw]_

But a workspace can also run a **long-lived service**: a dev server, a database, or, in this case, an AI assistant that stays available around the clock. openclaw's config adds three lines under `workspace`: a `service-command` that launches its gateway, `auto-start: true`, and a `health-check` script.

_[Scroll back to the three lines under `workspace:` — service-command, auto-start: true, health-check]_

When I ran `klangkc sandbox openclaw`, the setup installed Node and openclaw, wrote a config that points at the Klangk LLM proxy, and started the gateway automatically. That's the **service command** at work — a per-workspace singleton: it runs once in its own session and is shared with everyone you give access to. And I can see that straight from the command line:

_[Type: klangkc ls — the Status column shows openclaw as healthy, in green]_

The Status column shows `openclaw` as **healthy** — that's the service command running and its health check passing. Everything the CLI knows about the workspace, you see right here.

Now here's what turns this from "a process I left running" into an actual service. Two things.

First, **auto-start**. I've got `KLANGK_ALLOW_AUTOSTART` enabled on the server, so if I restart the whole Klangk server — or it reboots — openclaw's container boots on its own and the gateway is running _before anyone connects_. I don't have to log in and kick it off.

_[Host terminal: devenv processes restart backend. Then: klangkc ls — openclaw's Status goes starting → healthy again, all without anyone connecting]_

Second, **health checks**. A running container only proves the container is alive — it says nothing about the process inside it. So Klangk runs my health-check command inside the container every thirty seconds: exit zero means healthy, anything else is unhealthy — and that status is the very thing lighting up the Status column we just saw. Because it's all surfaced as events, I can watch it live from the command line with `klangkc monitor` — and even run a command on a change, like firing a Slack alert when the service goes down.

_[Type: klangkc monitor --type service_health | jq . — show live events; Ctrl+C to stop]_

The gateway here is also exposed as a **hosted app** — once we switch to the browser I can click straight through to openclaw's own web UI, proxied through Klangk's single port. No separate port to open, no extra auth to wire up. We'll see that in a moment.

So the same sandbox idea — one config file, one command — scales from "my dev environment" up to "a service that's always on."

## The Web UI — Workspaces and Terminal (1 minute)

_[Screen: switch to browser, Klangk web UI]_

Now let me continue in the browser. When you open Klangk on the web, you see the same workspaces you saw from the CLI — including the ones we just made. There's `openclaw` still showing its green health icon, and here's `demo`, the workspace we created a moment ago. Let me open it.

_[Click the demo workspace card on the "Owned by Me" tab]_

This is a continuation of exactly what we were doing. The terminal here is the same tmux session I had from `klangkc shell` — the repo I cloned and the Pi session I ran from the command line are all still here. That's the whole point: the CLI and the web UI are two windows into the same container.

_[Click the "+" next to the terminal tab bar to open a new tab, then double-click the tab name and rename it "scratch"]_

I can create multiple interactive terminal tabs, rename them, close them. And these aren't trapped in the browser — any tab I create here can be connected to from the CLI too, with `klangkc shell`. The web UI and the CLI are just two ways into the same sessions.

But the web UI has features beyond the terminal — files, chat, and collaboration. Let me show those.

## AI Agent — clanker (1.5 minutes)

Still in the `demo` workspace, I'll click over to the Chat tab.

_[Click the Chat tab in the left rail]_

Every workspace comes with a built-in AI agent. By default it's called clanker, and it's available **only through chat** — you talk to it by @mentioning it, not by running it in a terminal yourself.

_[Type: @clanker create a simple Flask web app on port 8000 that shows "Hello from Klangk"]_

The agent runs Pi inside the container. It can read and write files, run shell commands, and answer questions — all confined to this workspace's sandbox.

_[Wait ~10s. clanker's reply appears, followed by tool-call lines as it creates app.py and requirements.txt]_

There it is. Now here's something important about the security model. My LLM API key — the key that talks to the AI provider — never enters the container. Klangk runs an nginx reverse proxy on the host that injects the key into requests. Inside the container, Pi just talks to a local proxy URL. So even if the container were compromised, the API key isn't there.

_[Click the Terminal tab in the left rail, then type: env]_

And I can prove it. Here's the full environment of the container — no API keys, no secrets, nothing to steal. The key only exists on the host, in the proxy.

After an @mention, follow-up messages automatically route to the agent — I don't need to keep @mentioning it. The conversation continues until someone else speaks or I @mention a different user.

One thing worth being clear about: clanker is a **chat agent**, not a coding-agent harness. It does no tool calling, and you can't add skills or prompts to it — it's a fixed, built-in assistant scoped to the workspace. If what you want is a full harness you can extend and drive yourself, that's the next section.

## Debugging with The Pi Harness (~1.5–2 minutes)

> **Production note:** The Pi interaction here is nondeterministic — Pi's exact
> steps vary take to take. This part is **driven by an agent that pretends to be
> a human operating Pi**: it launches `pi`, sends the debug prompt, and reacts to
> Pi's output as a person would (reading the traceback, watching the fix, then
> inspecting files in the second bash tab). Its **goal is to get clanker's Flask
> app into a state where it can be opened in a new browser tab** — i.e. installed
> and running so the hosted-app URL serves the page. The scene culminates in
> doing exactly that. Treat it like the live `pi` beats in Scenes 2 and 6 — one
> long take, leave dead air while Pi works, narrate over later.

_[Screen: same workspace, Terminal tab]_

So clanker built us a Flask app. Let's actually try to run it.

_[Terminal tab (or `klangkc shell demo`): type python app.py]_

`ModuleNotFoundError: No module named 'flask'`. A classic agent mistake — the code is there, but the dependency was never installed. I could fix this by hand, but there's a faster way that shows off something important about Klangk.

This container has Pi as an agent — the same engine that powers clanker — but I can run it right here in the terminal, where I can watch it work and step in alongside it. And you're not limited to Pi — you can also bring your own harness, like Claude Code or Codex, and run it the same way.

_[Click "+" to open a new terminal tab, then type: pi]_

I'll start Pi in its own tab. Now I'll ask it to debug.

_[Type into pi: clanker's Flask app in app.py won't run — figure out why and fix it]_

Pi reads the traceback, sees Flask is missing, and installs it.

_[Watch Pi: it reads app.py, runs pip install -r requirements.txt, then retries python app.py]_

And here's the part I want you to see. While Pi works in its tab, I can open a plain bash tab right next to it and inspect what clanker actually produced.

_[Click "+" to open a second terminal tab next to the Pi tab]_

_[Type, one at a time: ls — cat app.py — cat requirements.txt]_

There's the app clanker wrote, and there's `requirements.txt` — Flask was listed all along, it just never got installed. I can poke around the files myself, double-check Pi's work, run things — all alongside the agent, not instead of it.

_[Browser: open the demo workspace → click the hosted-app button, or paste the URL from klangk-hosted-url 8000 (http://localhost:8995/hosted/<workspace_id>/<host_port>/) → the page renders "Hello from Klangk"]_

There it is. A real running app — broken by one agent, fixed by another, and now
open in my browser — all without leaving the sandbox.

## File Browser (30 seconds)

_[Click the Files tab in the left rail]_

The Files tab gives me a visual file browser. I can see the files clanker created a moment ago — `app.py`, `requirements.txt` — click one for a syntax-highlighted preview, drag and drop to upload, or right-click to download, rename, or delete.

And it previews more than just code. I dropped a PDF in here earlier — the Pyramid web framework documentation — let me open it.

_[Click pyramid-docs.pdf in the file tree — it renders in the viewer pane]_

Klangk renders common formats right in the browser — PDFs, images, spreadsheets, even video — so I can look at them without downloading anything or leaving the workspace.

## Multi-User Collaboration (1.5 minutes)

I've been working solo in the `demo` workspace — the Flask app's there, the chat history's there. Now let me bring the team in.

_[Click the Sharing tab in the left rail]_

I can share this workspace with other users.

_[In the Sharing panel, type teammate@example.com in the add-user field → pick the Collaborator role (people icon) → click Add]_

Klangk has four roles. Owners have full control. Coders get their own terminal and file access but can only watch shared terminals. Collaborators can type in shared terminals alongside the owner. And Spectators are read-only — they can watch shared terminals, but can't type in them or send chat.

_[Right-click a terminal tab → click "Share" — a share badge appears on the tab]_

I can share any terminal tab. When I do, the other user sees it appear in their tab bar. They're looking at the same live terminal — this is real pair programming, not screen sharing. Both of us can type, and we both see the same output.

_[Mouse over the top presence bar and the shared-tab viewer count]_

The UI shows who's connected to each workspace, and shared tabs show a viewer count so you know when someone's watching.

Chat is shared too — everyone in the workspace sees messages in real time, including the AI agent's responses. So you can collaborate with both humans and AI in the same space.

## Plugins (45 seconds)

_[Type: cat customize/plugins.yaml — point at one plugin entry (name, git, path, ref)]_

Klangk has a plugin system. Plugins are git repos that can install system packages at image build time, add CLI tools to the container, extend Pi with new tools, or inject UI widgets into the browser.

_[Browser: in the demo workspace, click the Chat tab → type: @clanker celebrate! → confetti animates over the UI (clanker called the celebrate tool the plugin registered)]_

For example, the "celebrate" plugin lets Pi trigger a confetti animation. The "git-credential" plugin adds a browser-based Git authentication dialog. "claude-code" installs Anthropic's Claude Code agent alongside Pi.

Plugins are declared in a YAML file and fetched automatically.

How do plugins differ from the `klangkc sandbox` setup scripts we saw earlier? They're both ways to customize a workspace, but they make a different trade-off.

The downside of plugins is that they require Klangk itself to be recompiled to pick them up — you can't just add one on the fly. But the payoff is that there's **no startup cost**: a plugin is baked into the image at build time, so every workspace that uses that image is ready to go instantly, with no setup script to run on first creation. The feature is available to all workspaces instantly.

Plugins can also extend the Flutter/Dart app that composes Klangk itself.

## Administration (30 seconds)

_[Browser: click the admin link → click through the Users, Groups, Invitations, and ACL tabs]_

The admin panel lets you manage users and groups, send email invitations, and configure access control. Klangk supports OIDC single sign-on — Google, GitHub, whatever your identity provider is.

Everything runs through a single port — nginx reverse-proxies the API, the frontend, hosted apps, and the LLM proxy all on port 8995.

## Closing (30 seconds)

So that's Klangk. For solo developers: sandboxed containers you manage from the CLI, one-command project setup with `klangkc sandbox`, SSH agent forwarding so your keys just work, and workspaces that can run always-on services with auto-start and health checks. For teams: shared workspaces, pair programming through shared terminals, real-time chat with an AI agent, and role-based access control. All self-hosted, all open source.

Containers auto-stop after an idle timeout to save resources, but your files persist. You can get started with a single Docker command or clone the repo and use devenv for development.

Check it out on GitHub — the link is in the description. Thanks for watching.
