# Releasing

Push a semver tag to trigger the `release.yml` workflow:

```bash
devenv shell -- git tag v0.1.0
devenv shell -- git push origin v0.1.0
```

The workflow runs these jobs:

- **Image jobs (`sidecar-image`, `workspace-image`, `workspace-fips-image`,
  `host-images`)** — call the image workflows (`image-network-sidecar.yml`,
  `image-workspace.yml`, `image-workspace-fips.yml`, `image-host-fips.yml`)
  via `workflow_call` with the tag pinned, so all **five** images publish
  from the tagged commit under the version tag (`v0.1.0`): `klangk-host`,
  `klangk-host-fips`, `klangk-workspace`, `klangk-workspace-fips`, and
  `klangk-network-sidecar`. There are no inline build steps here — the
  called workflows build and push exactly the way their continuous runs
  do (host pair via docker, workspace + sidecar via podman). The host
  pair is built in one job, so the tarballs embedded in the host image
  are that job's own workspace + sidecar builds, and the FIPS host layers
  that job's FIPS workspace — the auditable combination a release tag
  pins. No `:latest` tag is pushed — the floating `:latest` tags stay
  owned by the continuous workflows, and all release images are
  referenced by explicit version.
- **`create-release`** — after all five images are published, creates the
  GitHub Release. The release body is GitHub's auto-generated notes (PR
  list + compare link) **with that version's section from
  [the changelog](../changes.md) prepended**, when one exists.
- **`build-wheel`** — builds the `klangk` wheel (with the default-feature-set
  frontend baked in) and publishes it to PyPI, so `pip install klangk==<tag>`
  yields a working `klangkd` with the UI served from the in-wheel
  `klangk/frontend/`. Runs in parallel with the image jobs.

For patch releases, increment the patch version: `v0.1.1`.

## PyPI publishing

The `build-wheel` job publishes via **trusted publishing (OIDC)** — no API
token. `pypa/gh-action-pypi-publish@release/v1` negotiates an OIDC token
from GitHub Actions and presents it to PyPI, which validates it against the
trusted-publisher config on the `klangk` PyPI project.

**One-time setup (PyPI side):** the `klangk` PyPI project must have a
trusted publisher configured for:

- PyPI project: `klangk` (the distribution name)
- GitHub repo: `mcdonc/klangk`
- Workflow filename: `.github/workflows/release.yml`
- Environment name: `pypi` (the job's `environment:`)

With that in place, no secret is needed on the GitHub side — the publish is
authenticated purely via OIDC attestation. `skip-existing: true` makes the
upload idempotent on re-runs of the same tag (PyPI refuses re-upload of the
same filename).

The wheel is built by `scripts/build_wheel.sh`, which runs `uv build
--package klangk --wheel` — uv resolves hatchling/hatch-vcs into its own
cached, isolated build environment, so the shared devenv venv is never
touched (#3143). The hatch
build hook (`src/klangk/hatch_build_package_data.py`) includes the Flutter
web build at `klangk/frontend/` and **requires** it for non-editable wheel
builds — so the `build-wheel` job runs `klangk:flutter-build` first (which
runs `flutterbuildweb.sh` against the checked-in `features.yaml`).

To build a wheel locally for testing (after running `flutterbuildweb.sh`):

```bash
devenv shell -- bash scripts/build_wheel.sh
# produces src/klangk/dist/klangk-<version>-py3-none-any.whl
```

## Gardening the changelog before a tag

`docs/changes.md` is the source of truth for human-authored release notes. Right before tagging, rename the accumulated `## \[Unreleased]` section to `## \[vX.Y.Z] - YYYY-MM-DD` and add a fresh empty `## \[Unreleased]` above it, in its own commit. The opening bracket in version headings is escaped (`\[`) so the docs build does not parse them as Markdown link references — they render as plain `[...]` (#3142), and the release workflow matches both the escaped and bare forms. The release workflow extracts the `## [vX.Y.Z]` section from the checkout at the tag, so the rename must land in (or before) the commit you tag. See `AGENTS.md` for the full maintenance rules (when to add entries, what qualifies).

## CI

The `release.yml` workflow publishes all five container images to GHCR (host,
FIPS host, workspace, FIPS workspace, network sidecar — each under the
version tag) plus the `klangk` wheel to PyPI, triggered by pushing a version
tag matching `v[0-9]*`. The `image-workspace.yml`, `image-workspace-fips.yml`,
`image-host-fips.yml`, and `image-network-sidecar.yml` workflows also build
and push their images independently on push to `main` (when their inputs
change), under `<calver>-<commit>` tags (plus a floating `:latest` on the
FIPS images).
