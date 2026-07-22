# @klangk/herdr

Installs [herdr](https://github.com/ogulcancelik/herdr) — the terminal-based
agent runtime (persistent sessions, pane API) — into workspace containers.

## What it does

- **on-image-build.sh** — Downloads and installs the herdr binary (pinned to
  v0.6.6) at image build time. Architecture is auto-detected via `uname -m`
  (x86_64 / aarch64).
- **on-shell-init.sh** — Sets up herdr's API socket on every shell open. The
  socket lives on tmpfs (`/tmp`) because virtiofs (macOS) rejects `chmod` on
  sockets, and uses a per-user, random-suffixed directory to prevent
  predictable-path attacks and avoid collisions between concurrent shells.

## How herdr finds its socket

herdr resolves its API socket via the `HERDR_SOCKET_PATH` environment variable,
which `on-shell-init.sh` exports per shell. Because each shell gets its own
socket directory, concurrent terminals do not collide.

## Opt-in

This feature is **not** included in the default `features.yaml`. To enable it,
add it manually:

```yaml
features:
  - name: herdr
    git: https://github.com/mcdonc/klangk.git
    path: features/herdr
    ref: main
```

Without this feature, the base workspace image contains no herdr binary and sets
no `HERDR_SOCKET_PATH`.
