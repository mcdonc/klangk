# Building Images

## Host Image

```bash
build-host-image
```

This builds everything from source: Flutter web, workspace image
(podman), then the host image (Docker). Tagged locally with `latest`
and a version tag derived from git state (release tag, branch name,
or commit). Only the version tag is pushed to GHCR — `:latest` is
never pushed to the registry. The version is baked into
`/home/klangk/version.json` and served at `GET /api/v1/version`.

## Custom Image with Features

To build a host image with features, CA certificates, or OIDC hooks
baked in, see [Customizing a Deployment](../deployment/customizing.md).

## Scanning

```bash
trivy-host                        # scan host image
trivy-workspace                   # scan workspace image
trivy-host --severity CRITICAL    # critical only
```

### No-fix CVEs (tracking)

Most HIGH/CRITICAL findings in the workspace image have no fixed Debian/Node
package available yet (Trivy status `affected` / `fix_deferred`). These can't
be resolved by an upgrade until upstream ships a patched package, so they are
tracked for awareness and re-scanned periodically.

To render a focused report that separates **upgradeable** findings from the
**no upstream fix yet** set:

```bash
trivy-workspace-report                          # scan + report in one shot
trivy-workspace-report scan.json                # render an existing JSON scan
trivy-workspace --severity CRITICAL,HIGH --format json \
  | trivy-workspace-report -                    # pipe a scan into the report
```

The [`trivy-workspace-scan`](https://github.com/mcdonc/klangk/actions/workflows/trivy-workspace-scan.yml)
workflow automates this: it builds a fresh workspace image weekly (Mondays
06:00 UTC, or on demand via _Run workflow_), scans it, and posts the report
plus raw artifacts to the workflow run. It is **informational** — it never
fails a run — so reviewers check the Actions tab rather than gating CI.

## Image Versioning

**Stock images carry no `:latest` tag in the registry.** The host,
workspace, workspace base, and network sidecar images are pushed only
with an explicit version tag. This prevents confusion when stable
branches would otherwise overwrite `:latest` with an older version.
Consumers always reference a specific version (by checking out the tag
in their fork) or build locally. The FIPS variants are the exception:
their continuous workflows also maintain a floating `:latest` for the
e2e pull path (#2631) — the release path never retags `:latest`.

Locally, `build-workspace-image` tags `klangk-workspace:latest`
(used by the backend at runtime with pull policy `never`) and a
deterministic version tag (`YYYY.MM.DD-<commit>`). Stale version
tags from previous builds are automatically removed so they don't
accumulate. The local `:latest` tag is never pushed to GHCR.

## Release Publishing

Pushing a `v*` tag triggers `release.yml`, which publishes **all five**
images to GHCR under the tag (`vX.Y.Z`), built from the tagged commit:

| Image           | GHCR repo (under `ghcr.io/mcdonc/klangk/`) | Also built continuously by  |
| --------------- | ------------------------------------------ | --------------------------- |
| Host            | `klangk-host`                              | — (release-only)            |
| FIPS host       | `klangk-host-fips`                         | `image-host-fips.yml`       |
| Workspace       | `klangk-workspace`                         | `image-workspace.yml`       |
| FIPS workspace  | `klangk-workspace-fips`                    | `image-workspace-fips.yml`  |
| Network sidecar | `klangk-network-sidecar`                   | `image-network-sidecar.yml` |

The release path owns no build steps: it calls those image workflows via
`workflow_call` with the tag pinned, and each one builds and pushes the way
its continuous runs do (host pair via docker, workspace + sidecar via
podman). The host pair builds in one job, so the tarballs embedded in the
host image come from that job's own workspace + sidecar builds, and the FIPS
host layers that job's FIPS workspace — a release tag pins an auditable
combination rather than a mix of floating tags.

Release tags are immutable and versioned only: the release path never
retags `:latest` (the floating `:latest` on the FIPS images stays owned by
the continuous workflows). See [Releasing](releasing.md) for the full
tag-push procedure.

## Workspace Base Image Pin

The workspace `Dockerfile` pins its base image to an **immutable digest**
via a build `ARG`:

```dockerfile
ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base@sha256:…
FROM $WORKSPACE_BASE_IMAGE
```

The digest references the multi-arch manifest (amd64 + arm64); the build
selects the platform via `--platform`. This means changes to
`Dockerfile.base` on main don't silently affect other branches, and the
registry cannot serve different content for the same reference. The flow:

1. Someone changes `Dockerfile.base` and pushes to main.
2. The `image-workspace-base.yml` workflow builds and pushes the new
   base image with a versioned tag.
3. The same workflow resolves the pushed manifest's digest and
   automatically opens a PR to update the `ARG` default in
   `src/containers/workspace/Dockerfile` to `repo@sha256:…`.
4. A maintainer reviews and merges the PR.

Stable/deploy branches keep their original pinned base digest and
are unaffected. To override at build time:
`--build-arg WORKSPACE_BASE_IMAGE=ghcr.io/.../klangk-workspace-base@sha256:…`.
`pull-base-image` pulls exactly this pinned reference (never a mutable
`:latest`) so the local cache matches what a build consumes.

## Pinned Third-Party Artifacts

Every base image, release tarball, and apt repo key fetched from the
network at image-build time is verified against a hash pinned in the
Dockerfile or pulled by immutable digest — a compromised registry, CDN,
or MITM cannot swap those inputs undetected, and a mismatch fails the
build loudly. (Known residuals beyond that scope are listed under
"Accepted residuals" below.)

| Artifact                                         | Where the pin lives                                                                | Verify/rotate on bump                                                                                                                           |
| ------------------------------------------------ | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Workspace base image                             | `WORKSPACE_BASE_IMAGE` ARG in `src/containers/workspace/Dockerfile`                | automatic — the base-image workflow's auto-PR rewrites the ARG with the new `repo@digest`                                                       |
| Pi agent npm tarball                             | `PI_AGENT_SHA512` in `src/containers/workspace/Dockerfile`                         | `npm view @earendil-works/pi-coding-agent@<ver> dist.integrity` (base64 sha512 → hex)                                                           |
| uv                                               | `UV_SHA256_AMD64` / `UV_SHA256_ARM64` in `src/containers/workspace/Dockerfile`     | `sha256sum` of each arch tarball, or the `.sha256` sidecars in the GitHub release                                                               |
| process-compose                                  | `PROCESS_COMPOSE_SHA256_AMD64` / `_ARM64` in `src/containers/workspace/Dockerfile` | `sha256sum` of each arch tarball                                                                                                                |
| Debian base (workspace, FIPS builders, nix-seed) | digest in `src/containers/workspace/Dockerfile.base` (pre-existing)                | `docker buildx imagetools inspect debian:trixie-slim (read the Digest: line)`; keep the three aligned builders in sync                          |
| python host base                                 | digest in `src/containers/host/Dockerfile`                                         | `docker buildx imagetools inspect python:3.14-slim (read the Digest: line)`                                                                     |
| Alpine (network sidecar)                         | digest in `src/containers/network/Dockerfile`                                      | `docker buildx imagetools inspect alpine:3.21 (read the Digest: line)`                                                                          |
| NodeSource repo key                              | `NODESOURCE_KEY_SHA256` in `Dockerfile.base`                                       | `sha256sum` of the fetched `gpgkey/nodesource-repo.gpg.key` (after cross-checking the new key's fingerprint against NodeSource's docs)          |
| GitHub CLI repo key                              | `GITHUBCLI_KEYRING_SHA256` in `Dockerfile.base`                                    | `sha256sum` of the fetched `githubcli-archive-keyring.gpg` (fingerprint in the Dockerfile comment)                                              |
| Caddy repo key (Cloudsmith)                      | `CADDY_REPO_KEY_SHA256` in `src/containers/host/Dockerfile`                        | `sha256sum` of the fetched `gpg.key` (fingerprint in the Dockerfile comment; cross-check <https://cloudsmith.io/~caddy/repos/stable/pub-keys/>) |

Notes:

- The apt **sources lists** (NodeSource, GitHub CLI, Caddy) are written
  inline in the Dockerfiles, not fetched — the GPG key is the only
  network-sourced trust input, and apt's own signature verification then
  covers the package indexes and `.deb`s. Caddy itself is intentionally
  not version-pinned so rebuilds pick up security patches; its integrity
  rests on the pinned repo key.
- Deps of the Pi agent tarball are still resolved by npm at build time
  (integrity-checked by npm against registry metadata, as usual).
- `scripts/tests/test_supply_chain_pins.py` holds contract tests asserting
  the pins exist; removing one without updating that file fails CI.

### Accepted residuals

Not everything fetched at build time is hash-verified. What remains
trusted through TLS plus the source's own integrity mechanisms, and why
(called out explicitly so the posture above is not
over-read):

- **PyPI/npm dependency closures.** `pip install` of the klangk wheel's
  deps (host image, network sidecar) and npm's resolution of the Pi
  agent tarball's deps rely on the registry's metadata-bound integrity
  records. Pinning the full transitive closure would require
  `--require-hashes` lockfiles maintained outside this repo's wheels.
- **nix-seed sandbox** (`src/containers/nix-seed/Dockerfile`): the nix
  installer is still piped to `sh` and the devenv flake ref is mutable.
  It is a build sandbox whose output is a content-addressed `/nix` store;
  it is never baked into a shipped image. A malicious installer would
  have to compromise the machine that computes those store addresses.
- **`ssh-keyscan github.com`** in `Dockerfile.base` bakes host keys via
  TOFU at build time rather than pinning GitHub's published
  fingerprints.
- **`dist-smoke-test.sh`** builds a throwaway `node:22-slim` (mutable
  tag) smoke image — dev-only, never shipped.
