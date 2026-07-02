# Klangk Introduction Video Script (~14 minutes)

> **Production note — continuity.** This video tells one continuous story
> across a single evolving workspace, **`demo`**, created on camera in "The
> CLI" scene and kept alive through every scene after. Each shot builds on the
> state of the last: the repo cloned and the Pi session from the CLI scene are
> still there when we reopen `demo` in the browser; clanker's Flask app
> (`app.py`, `requirements.txt`) is what we debug with Pi and then browse in
> the Files scene; and we bring the team into that same workspace for
> collaboration. Two other workspaces — `myproject` (the sandbox scene) and
> `openclaw` (the service-workspace scene) — coexist in the list but are
> self-contained feature demos. The list is also pre-populated with a few
> decorative "Potemkin" workspaces per account so it looks lived-in (see the
> shotlist's global setup). Whenever a scene says "open a workspace", it means
> **open the `demo` workspace**.

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

_[Type: klangkc login admin@plope.com]_

Now let me create a workspace.

_[Type: klangkc create demo]_

That's it — a fresh Linux container with Python, Node, git, and build tools is now running. Let me drop into it.

_[Type: klangkc shell demo]_

This is an SSH-like connection into the container. I'm in a real bash shell on Linux, regardless of what my local machine is. Under the hood this is backed by tmux, so if I disconnect and reconnect, everything is exactly where I left it — same scrollback, same running processes.

Let me do something useful. I'll clone a repo.

_[Type: klangkc shell demo -A]_
_[Explain -A flag]_

The `-A` flag forwards my local SSH agent into the container, so I can use my GitHub SSH keys without copying any private keys into the container.

_[Type: git clone git@github.com:mcdonc/klangk.git]_

I can also use harnesses like Pi and claude-code inside the container.

_[Type: pi and communicate with the agent, ask it to audit my codebase]_

There it is. I can work on this project, run tests, use AI agents — all inside the sandbox. My host system is untouched.

To disconnect, I use the SSH-style escape: Enter, tilde, dot. The container keeps running.

_[Disconnect with ~.]_

And when I'm eventually done with a workspace, `klangkc rm` tears it down and cleans up its files. But I'm going to keep this one around — we'll come back to it from the browser in a minute.

## klangkc sandbox — One Command to Rule Them All (1.5 minutes)

_[Screen: local terminal, in a project directory]_

Creating workspaces manually is fine, but the real power for solo developers is `klangkc sandbox`. You check a config file into your repo, and then one command sets up everything.

_[Show .klangk-sandbox.yaml]_

Here's what a sandbox config looks like. It says: mount my project at `~/myproject` inside the container, bind-mount my `.ssh` and `.claude` directories so my keys and Claude config are available, use a named volume for the nix store so it persists across rebuilds, and run a setup script on first creation.

_[Type: klangkc sandbox myproject -A]_

First run: it creates the workspace, mounts everything, runs the setup script, and drops me into a shell. The `-A` forwards my SSH agent so git works immediately.

_[Show being inside the container with the project mounted]_

Now next time I run the same command, it just reconnects — no setup, straight to my shell.

The idea is that you commit this config file to your repo. Any teammate — or future you on a different machine — runs `klangkc sandbox myproject` and gets the exact same environment. It's like a Dockerfile for your dev environment, but the container lifecycle is managed for you.

## Service Workspaces — Service Command, Auto-Start, and Health Checks (2 minutes)

_[Screen: local terminal, in the klangk repo, sandboxes/openclaw]_

So far every workspace has been an interactive shell — you connect, you type. But a workspace can also run a **long-lived service**: a dev server, a database, or, in this case, an AI assistant that stays available around the clock. Let me show this with one of the example sandboxes we ship — openclaw, a personal AI assistant.

Remember the sandbox config from a moment ago? openclaw's adds three lines under `workspace`.

_[Show sandboxes/openclaw/.klangk-sandbox.yaml]_

A `service-command` that launches the openclaw gateway, `auto-start: true`, and a `health-check` script. Let me create it.

_[Type: klangkc sandbox openclaw]_

First run installs Node and openclaw, writes a config that points at the Klangk LLM proxy, and starts the gateway automatically in its own Service tab — that's the **service command** at work. It's a per-workspace singleton: it runs once in a dedicated window and is shared with everyone you give access to. I can Ctrl+C to stop it, up-arrow and Enter to restart it, and the scrollback sticks around.

_[Show the gateway running in the Service tab in the web UI]_

Now here's what turns this from "a process I left running" into an actual service. Two things.

First, **auto-start**. I've got `KLANGK_ALLOW_AUTOSTART` enabled on the server, so if I restart the whole Klangk server — or it reboots — openclaw's container boots on its own and the gateway is running _before anyone connects_. I don't have to log in and kick it off.

_[Restart the server, show openclaw coming back up on its own]_

Second, **health checks**. A running container only proves the container is alive — it says nothing about the process inside it. So Klangk runs my health-check command inside the container every thirty seconds: exit zero means healthy, anything else is unhealthy. The status shows up live as a colored icon in the workspace list, and if it goes unhealthy Klangk shows me _why_ — the tail of the check's output, not just a red light.

_[Point at the health icon in the workspace list]_

And because it's all surfaced as events, I can watch from the command line with `klangkc monitor` — even run a command on a change, like firing a Slack alert when the service goes down.

_[Type: klangkc monitor --type service_health | jq .]_

The gateway here is also exposed as a hosted app, so I can click straight through to openclaw's own web UI right from the browser, proxied through Klangk's single port.

_[Click the hosted app link]_

So the same sandbox idea — one config file, one command — scales from "my dev environment" up to "a service that's always on."

## The Web UI — Workspaces and Terminal (1 minute)

_[Screen: switch to browser, Klangk web UI]_

Now let me continue in the browser. When you open Klangk on the web, you see the same workspaces you saw from the CLI — including the ones we just made. There's `myproject`, there's `openclaw` still showing its green health icon, and here's `demo`, the workspace we created a moment ago. Let me open it.

_[Open the demo workspace]_

This is a continuation of exactly what we were doing. The terminal here is the same tmux session I had from `klangkc shell` — the repo I cloned and the Pi session I ran from the command line are all still here. That's the whole point: the CLI and the web UI are two windows into the same container.

_[Click "+" to create a new terminal tab, rename it]_

I can create multiple interactive terminal tabs, rename them, close them. And these aren't trapped in the browser — any tab I create here can be connected to from the CLI too, with `klangkc shell`. The web UI and the CLI are just two ways into the same sessions.

But the web UI has features beyond the terminal — files, chat, and collaboration. Let me show those.

## AI Agent — clanker (1.5 minutes)

Still in the `demo` workspace, I'll click over to the Chat tab.

_[Click Chat tab]_

Every workspace comes with a built-in AI agent. By default it's called clanker, and it's available **only through chat** — you talk to it by @mentioning it, not by running it in a terminal yourself.

_[Type: @clanker create a simple Flask web app on port 8000 that shows "Hello from Klangk"]_

The agent runs Pi inside the container. It can read and write files, run shell commands, and answer questions — all confined to this workspace's sandbox.

_[Wait for response, show the agent creating files]_

There it is. Now here's something important about the security model. My LLM API key — the key that talks to the AI provider — never enters the container. Klangk runs an nginx reverse proxy on the host that injects the key into requests. Inside the container, Pi just talks to a local proxy URL. So even if the container were compromised, the API key isn't there.

_[Switch to Terminal tab, type: env]_

And I can prove it. Here's the full environment of the container — no API keys, no secrets, nothing to steal. The key only exists on the host, in the proxy.

After an @mention, follow-up messages automatically route to the agent — I don't need to keep @mentioning it. The conversation continues until someone else speaks or I @mention a different user.

## Debugging with Pi (~1.5–2 minutes)

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

_[Open a terminal tab, type: python app.py]_

`ModuleNotFoundError: No module named 'flask'`. A classic agent mistake — the code is there, but the dependency was never installed. I could fix this by hand, but there's a faster way that shows off something important about Klangk.

This container has Pi as an agent — the same engine that powers clanker — but I can run it right here in the terminal, where I can watch it work and step in alongside it. And you're not limited to Pi — you can also bring your own harness, like Claude Code or Codex, and run it the same way.

_[Open a new terminal tab, type: pi]_

I'll start Pi in its own tab. Now I'll ask it to debug.

_[Type into pi: clanker's Flask app in app.py won't run — figure out why and fix it]_

Pi reads the traceback, sees Flask is missing, and installs it.

_[Show Pi working: reading app.py, running pip install -r requirements.txt, retrying the app]_

And here's the part I want you to see. While Pi works in its tab, I can open a plain bash tab right next to it and inspect what clanker actually produced.

_[Open a second bash tab alongside the Pi tab]_

_[Type: ls — then cat app.py — then cat requirements.txt]_

There's the app clanker wrote, and there's `requirements.txt` — Flask was listed all along, it just never got installed. I can poke around the files myself, double-check Pi's work, run things — all alongside the agent, not instead of it.

_[Confirm the fix: open the hosted-app URL in a new browser tab → "Hello from Klangk" loads as a page]_

There it is. A real running app — broken by one agent, fixed by another, and now
open in my browser — all without leaving the sandbox.

## File Browser (30 seconds)

_[Click Files tab]_

The Files tab gives me a visual file browser. I can see the files clanker created a moment ago — `app.py`, `requirements.txt` — click one for a syntax-highlighted preview, drag and drop to upload, or right-click to download, rename, or delete.

And it previews more than just code. I dropped a PDF in here earlier — the Pyramid web framework documentation — let me open it.

_[Click the preseeded PDF — it renders inline]_

Klangk renders common formats right in the browser — PDFs, images, spreadsheets, even video — so I can look at them without downloading anything or leaving the workspace.

## Multi-User Collaboration (1.5 minutes)

I've been working solo in the `demo` workspace — the Flask app's there, the chat history's there. Now let me bring the team in.

_[Click Sharing tab on the workspace]_

I can share this workspace with other users.

_[Add a user, assign "Collaborator" role]_

Klangk has four roles. Owners have full control. Coders get their own terminal and file access but can only watch shared terminals. Collaborators can type in shared terminals alongside the owner. And Spectators are read-only — they can watch shared terminals, but can't type in them or send chat.

_[Right-click terminal tab, click "Share"]_

I can share any terminal tab. When I do, the other user sees it appear in their tab bar. They're looking at the same live terminal — this is real pair programming, not screen sharing. Both of us can type, and we both see the same output.

_[Show presence indicators]_

The UI shows who's connected to each workspace, and shared tabs show a viewer count so you know when someone's watching.

Chat is shared too — everyone in the workspace sees messages in real time, including the AI agent's responses. So you can collaborate with both humans and AI in the same space.

## Plugins (45 seconds)

_[Show plugins.yaml or plugin directory]_

Klangk has a plugin system. Plugins are git repos that can install system packages at image build time, add CLI tools to the container, extend Pi with new tools, or inject UI widgets into the browser.

_[Show a plugin demo — e.g., celebrate confetti]_

For example, the "celebrate" plugin lets Pi trigger a confetti animation. The "git-credential" plugin adds a browser-based Git authentication dialog. "claude-code" installs Anthropic's Claude Code agent alongside Pi.

Plugins are declared in a YAML file and fetched automatically.

How do plugins differ from the `klangkc sandbox` setup scripts we saw earlier? They're both ways to customize a workspace, but they make a different trade-off.

The downside of plugins is that they require Klangk itself to be recompiled to pick them up — you can't just add one on the fly. But the payoff is that there's **no startup cost**: a plugin is baked into the image at build time, so every workspace that uses that image is ready to go instantly, with no setup script to run on first creation. The feature is available to all workspaces instantly.

Plugins can also extend the Flutter/Dart app that composes Klangk itself.

## Administration (30 seconds)

_[Show admin panel briefly]_

The admin panel lets you manage users and groups, send email invitations, and configure access control. Klangk supports OIDC single sign-on — Google, GitHub, whatever your identity provider is.

Everything runs through a single port — nginx reverse-proxies the API, the frontend, hosted apps, and the LLM proxy all on port 8995.

## Closing (30 seconds)

So that's Klangk. For solo developers: sandboxed containers you manage from the CLI, one-command project setup with `klangkc sandbox`, SSH agent forwarding so your keys just work, and workspaces that can run always-on services with auto-start and health checks. For teams: shared workspaces, pair programming through shared terminals, real-time chat with an AI agent, and role-based access control. All self-hosted, all open source.

Containers auto-stop after an idle timeout to save resources, but your files persist. You can get started with a single Docker command or clone the repo and use devenv for development.

Check it out on GitHub — the link is in the description. Thanks for watching.
