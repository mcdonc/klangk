# Releasing

Push a semver tag to trigger the `release.yml` workflow:

```bash
git tag v0.1.0
git push origin v0.1.0
```

This builds the host image (including workspace and Flutter web), pushes both `klangk-host` and `klangk-workspace` to GHCR tagged with the version (e.g. `v0.1.0`), and creates a GitHub Release with auto-generated notes. No `:latest` tag is pushed — all images are referenced by explicit version. For patch releases, increment the patch version: `v0.1.1`.

## CI

The `release.yml` workflow builds and pushes the host image to GHCR, triggered by pushing a version tag matching `v[0-9]*`. The `image-workspace.yml` workflow builds and pushes the workspace image independently on push to `main` (when workspace container files change).
