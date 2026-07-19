# Releasing

Push a semver tag to trigger the `release.yml` workflow:

```bash
devenv shell -- git tag v0.1.0
devenv shell -- git push origin v0.1.0
```

The workflow runs two jobs in parallel:

- **`build-and-release`** — builds the host image (including workspace and
  Flutter web), pushes both `klangk-host` and `klangk-workspace` to GHCR
  tagged with the version (e.g. `v0.1.0`), and creates a GitHub Release. The
  release body is GitHub's auto-generated notes (PR list + compare link)
  **with that version's section from [the changelog](../changes.md)
  prepended**, when one exists. No `:latest` tag is pushed — all images are
  referenced by explicit version.
- **`build-wheel`** — builds the `klangk` wheel (with the default-plugin-set
  frontend baked in) and publishes it to PyPI, so `pip install klangk==<tag>`
  yields a working `klangkd` with the UI served from the in-wheel
  `klangk/frontend/` (#1656).

For patch releases, increment the patch version: `v0.1.1`.

## PyPI publishing

The `build-wheel` job publishes via **trusted publishing (OIDC)** — no API
token. `pypa/gh-action-pypi-publish@release/v1` negotiates an OIDC token
from GitHub Actions and presents it to PyPI, which validates it against the
trusted-publisher config on the `klangk` PyPI project.

**One-time setup (PyPI side):** the `klangk` PyPI project must have a
trusted publisher configured for:

- PyPI project: `klangk` (the distribution name, #1606)
- GitHub repo: `mcdonc/klangk`
- Workflow filename: `.github/workflows/release.yml`
- Environment name: `pypi` (the job's `environment:`)

With that in place, no secret is needed on the GitHub side — the publish is
authenticated purely via OIDC attestation. `skip-existing: true` makes the
upload idempotent on re-runs of the same tag (PyPI refuses re-upload of the
same filename).

The wheel is built by `scripts/build_wheel.sh`, which installs
`python-build` transiently (it's not a declared dependency of the runtime
venv) and runs `python3 -m build --wheel` from `src/klangk/`. The hatch
build hook (`src/klangk/hatch_build_frontend.py`) force-includes the Flutter
web build at `klangk/frontend/` and **requires** it for non-editable wheel
builds — so the `build-wheel` job runs `klangk:flutter-build` first (which
runs `flutterbuildweb.sh` against the checked-in `plugins.yaml`, #1660).

To build a wheel locally for testing (after running `flutterbuildweb.sh`):

```bash
devenv shell -- bash scripts/build_wheel.sh
# produces src/klangk/dist/klangk-<version>-py3-none-any.whl
```

## Gardening the changelog before a tag

`docs/changes.md` is the source of truth for human-authored release notes. Right before tagging, rename the accumulated `## [Unreleased]` section to `## [vX.Y.Z] - YYYY-MM-DD` and add a fresh empty `## [Unreleased]` above it, in its own commit. The release workflow extracts the `## [vX.Y.Z]` section from the checkout at the tag, so the rename must land in (or before) the commit you tag. See `AGENTS.md` for the full maintenance rules (when to add entries, what qualifies).

## CI

The `release.yml` workflow builds and pushes the host image to GHCR + the wheel to PyPI, triggered by pushing a version tag matching `v[0-9]*`. The `image-workspace.yml` workflow builds and pushes the workspace image independently on push to `main` (when workspace container files change).
