# Releasing

Push a CalVer tag to trigger the `release.yml` workflow:

```bash
git tag v2026.06.10
git push origin v2026.06.10
```

This builds the host image (including workspace and Flutter web), pushes both `klangk-host` and `klangk-workspace` to GHCR tagged with the version (e.g. `v2026.06.10`), and creates a GitHub Release with auto-generated notes. No `:latest` tag is pushed — all images are referenced by explicit version. If you need a second release on the same day, append a suffix: `v2026.06.10.1`.

## CI

The `release.yml` workflow builds and pushes the host image to GHCR, triggered by pushing a CalVer tag. The `image-workspace.yml` workflow builds and pushes the workspace image independently on push to `main` (when workspace container files change).
