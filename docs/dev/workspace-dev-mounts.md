# Workspace dev mounts (edit Pi extensions without an image rebuild)

Opt-in, off by default. Lets you iterate on Pi extensions inside a running
workspace container without the `klangk:build-workspace-image` (`podman build`)
round-trip.

## Why

The workspace image `COPY`s plugin/builtin Pi extensions at build time
(`src/containers/workspace/Dockerfile`). Editing one normally changes the image
hash and forces a full rebuild. But the container is long-lived (`sleep
infinity`; sessions via `podman exec`) and Pi **auto-discovers
`~/.pi/agent/extensions/`** in addition to the baked image dir
(`klangk-setup-clankers.py`). So a host directory mounted there is **additive** —
it doesn't hide the baked builtin/plugin extensions — and host edits show up on
the next workspace open with no rebuild.

## Usage

```bash
export KLANGK_WORKSPACE_DEV=1
# A host dir of Pi extensions (.ts files) to overlay into the workspace:
export KLANGK_WORKSPACE_DEV_EXTENSIONS_DIR=$PWD/src/containers/workspace/builtin-extensions
# then start the stack as usual (devenv processes up / your normal flow)
```

Open a workspace, edit a `.ts` file in that host dir, reopen the workspace (or
restart the agent) — the change is live, no `podman build`.

- Flag accepts `1`, `true`, `yes` (case-insensitive). Anything else = off.
- The mount is **read-only** and mounted at `/home/klangk/.pi/agent/extensions`.
- If the flag is off, the dir is unset, or the dir doesn't exist, behaviour is
  exactly as today (a missing-dir case logs a warning and is skipped).
- Default / production: **unchanged** — `workspace_dev_mounts()` returns `[]`.

## Implementation

`container.py::workspace_dev_mounts()` returns the extra bind specs, appended to
the container `binds`. Validated on the real `klangk-arm64` image: a host edit to
a bind-mounted extension is reflected live in the running container with no
rebuild and no restart, including the nested case under the `/home` mount.

## Scope / follow-ups

This PR covers **extensions** (the common iteration case). Tools
(`/opt/klangk/bin`) and the frequently-edited `klangk-*` scripts could get the
same treatment, but those live outside `/home` and would need either individual
file mounts or a merged dir — left as a follow-up to keep this change small and
additive.
