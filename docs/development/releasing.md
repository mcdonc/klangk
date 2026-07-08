# Releasing

Push a semver tag to trigger the `release.yml` workflow:

```bash
devenv shell -- git tag v0.1.0
devenv shell -- git push origin v0.1.0
```

This builds the host image (including workspace and Flutter web), pushes both `klangk-host` and `klangk-workspace` to GHCR tagged with the version (e.g. `v0.1.0`), and creates a GitHub Release. The release body is GitHub's auto-generated notes (PR list + compare link) **with that version's section from [the changelog](../changes.md) prepended**, when one exists. No `:latest` tag is pushed — all images are referenced by explicit version. For patch releases, increment the patch version: `v0.1.1`.

## Gardening the changelog before a tag

`docs/changes.md` is the source of truth for human-authored release notes. Right before tagging, rename the accumulated `## [Unreleased]` section to `## [vX.Y.Z] - YYYY-MM-DD` and add a fresh empty `## [Unreleased]` above it, in its own commit. The release workflow extracts the `## [vX.Y.Z]` section from the checkout at the tag, so the rename must land in (or before) the commit you tag. See `AGENTS.md` for the full maintenance rules (when to add entries, what qualifies).

## CI

The `release.yml` workflow builds and pushes the host image to GHCR, triggered by pushing a version tag matching `v[0-9]*`. The `image-workspace.yml` workflow builds and pushes the workspace image independently on push to `main` (when workspace container files change).
