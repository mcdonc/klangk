# Packaged `klangkd` (pip install)

The compiled Flutter Web UI ships **inside the `klangk` wheel** at
`klangk/frontend/`, so a `pip install klangk` deployment serves the UI out of
the box — no separate frontend build or checkout required (#1600).

## Where the UI comes from

`klangkd` resolves the directory it serves the UI from in this order:

1. **`KLANGKD_FRONTEND_DIR`**, if set — point it at any built Flutter web
   directory on the filesystem (#1456). Use this to serve a UI you built
   yourself, or to override the default.
2. **The in-package default** — `<site-packages>/klangk/frontend/`. This is
   the wheel's built-in copy, populated at wheel-build time (see below). For a
   plain `pip install klangk` this is what serves the UI.

If the resolved directory does not exist at startup, `klangkd` logs a warning
and serves an API-only app (the UI is not mounted). This makes a misconfigured
override — or a wheel built without the frontend artifact — obvious rather
than silent.

## Building the wheel (operators releasing klangk)

The Flutter web build (`src/frontend/build/web/`) is gitignored, so it exists
only at wheel-build time. Hatchling `force-include`s it into the wheel under
`klangk/frontend/` (`src/klangk/pyproject.toml`). Produce it **before** building
the wheel:

```bash
scripts/flutterbuildweb.sh        # writes src/frontend/build/web
uv build --project src/klangk     # force-includes it into klangk/frontend/
```

If the artifact is absent at build time, hatchling fails the build
(`Forced include not found`) — the build cannot silently produce a UI-less
wheel.

## Other deployment modes

Not every `klangkd` runs from an installed wheel. The in-package default only
applies when the package is installed non-editable; source-tree deployments set
`KLANGKD_FRONTEND_DIR` explicitly:

| Mode                                | Runs from                    | Frontend dir                                                                                   |
| ----------------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------------- |
| **Packaged** (`pip install klangk`) | installed wheel              | in-package `klangk/frontend/` (default)                                                        |
| **devenv / checkout**               | editable source tree         | `KLANGKD_FRONTEND_DIR` → repo `src/frontend/build/web` (set in `devenv.nix`)                   |
| **Host container**                  | source tree via `PYTHONPATH` | `KLANGKD_FRONTEND_DIR` → `/home/klangk/src/frontend/build/web` (set in the image `Dockerfile`) |

Operators running their own build of the UI (e.g. a custom Flutter build) set
`KLANGKD_FRONTEND_DIR` to that directory in all three modes.
