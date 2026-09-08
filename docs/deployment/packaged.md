# Packaged `klangkd` (pip install)

The compiled Flutter Web UI ships **inside the `klangk` wheel** at
`klangk/frontend/`, so a `pip install klangk` deployment serves the UI out of
the box — no separate frontend build or checkout required.

## Where the UI comes from

`klangkd` resolves the directory it serves the UI from in this order:

1. **`KLANGKD_FRONTEND_DIR`**, if set — point it at any built Flutter web
   directory on the filesystem. Use this to serve a UI you built
   yourself, or to override the default.
2. **The in-package default** — `<site-packages>/klangk/frontend/`. This is
   the wheel's built-in copy, populated at wheel-build time (see below). For a
   plain `pip install klangk` this is what serves the UI.

If the resolved directory does not exist at startup, `klangkd` logs a warning
and serves an API-only app (the UI is not mounted). This makes a misconfigured
override — or a wheel built without the frontend artifact — obvious rather
than silent.

## Container images for workspaces

The wheel carries the server, the CLI, and the web UI. Workspace containers
come from **container images**, which live in the local podman image store —
the wheel ships none. A workspace start needs both of these images:

| Image           | Setting (default)                                  | Purpose                                                                                                                                                                                                                             |
| --------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Workspace       | `image_name` (`klangk-workspace`)                  | The environment your code runs in (the container `klangkd` creates per workspace).                                                                                                                                                  |
| Network sidecar | `network_sidecar_image` (`klangk-network-sidecar`) | The FQDN egress-filtering companion. Fresh workspaces default to `egress_mode=interactive`, which makes the sidecar mandatory: a start whose sidecar image is missing refuses to run and the API answers `500` with the pull error. |

Both images publish to GHCR under `ghcr.io/mcdonc/klangk/`, tagged with
immutable version tags only — `vX.Y.Z` release tags and
`YYYY.MM.DD-<commit>` continuous tags (see
[Building Images](../development/building-images.md) for the publishing
conventions). `klangkd` resolves both settings' short names against the local
image store (the workspace image is created with pull policy `never`), so
pull a published tag and retag it onto the short name:

```bash
# Release tags pair with the released wheel of the same version;
# continuous tags track main.
WS_TAG=v2026.06.10
podman pull ghcr.io/mcdonc/klangk/klangk-workspace:$WS_TAG
podman tag  ghcr.io/mcdonc/klangk/klangk-workspace:$WS_TAG klangk-workspace:latest

# Pick the newest continuous tag from
# https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-network-sidecar
SIDECAR_TAG=2026.09.07-49bf897
podman pull ghcr.io/mcdonc/klangk/klangk-network-sidecar:$SIDECAR_TAG
podman tag  ghcr.io/mcdonc/klangk/klangk-network-sidecar:$SIDECAR_TAG klangk-network-sidecar:latest
```

Pick the workspace and sidecar tags built from the same commit as your
wheel when you can: the three components evolve together, and a release
tag (`vX.Y.Z`) matches the wheel cut from that tag.

With a checkout of the repository, the devenv tasks build and tag both
images locally instead:

```bash
devenv shell -- build-workspace-image
devenv shell -- build-network-sidecar
```

Both settings take a fully-qualified reference too
(`KLANGKD_IMAGE_NAME=ghcr.io/mcdonc/klangk/klangk-workspace:v2026.06.10`),
which still requires the image in the local store — pull it first. See the
[configuration reference](../reference/klangkd-config.md) for the full
settings surface.

## Building the wheel (operators releasing klangk)

The Flutter web build (`src/frontend/build/web/`) is gitignored, so it exists
only at wheel-build time. A hatch custom build hook
(`src/klangk/hatch_build_package_data.py`) includes it into the wheel under
`klangk/frontend/`. Produce it **before** building the wheel:

```bash
scripts/flutterbuildweb.sh        # writes src/frontend/build/web
uv build --project src/klangk     # hook includes it into klangk/frontend/
```

If the artifact is absent at build time the hook fails the build
(`Frontend artifact not found`) — the build cannot silently produce a
UI-less wheel. (Editable installs, where the gitignored artifact is absent,
are exempt — the hook only requires it for a non-editable wheel.)

## Other deployment modes

Not every `klangkd` runs from an installed wheel. The in-package default only
applies when the package is installed non-editable; source-tree deployments set
`KLANGKD_FRONTEND_DIR` explicitly:

| Mode                                | Runs from                    | Frontend dir                                                                                   |
| ----------------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------------- |
| **Packaged** (`pip install klangk`) | installed wheel              | in-package `klangk/frontend/` (default)                                                        |
| **devenv / checkout**               | editable source tree         | `frontend_dir` → repo `src/frontend/build/web` (via `klangkd.yaml`)                            |
| **Host container**                  | source tree via `PYTHONPATH` | `KLANGKD_FRONTEND_DIR` → `/home/klangk/src/frontend/build/web` (set in the image `Dockerfile`) |

Operators running their own build of the UI (e.g. a custom Flutter build) set
`KLANGKD_FRONTEND_DIR` to that directory in all three modes.
