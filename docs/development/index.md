# Development

## Running Tests

```bash
# Backend unit tests (Python, pytest, parallel)
test-backend

# CLI unit tests
test-cli

# Frontend unit tests (Dart, flutter test, 100% coverage required)
test-frontend

# Backend E2E tests (starts real server + podman containers)
test-backend-e2e

# CLI E2E tests (starts real server, runs klangk commands)
test-cli-e2e

# Frontend E2E tests (Playwright, needs flutter build + podman build)
test-frontend-e2e

# Run a specific frontend E2E test
test-frontend-e2e --project=chromium --no-deps -g "test name"
```

Backend tests for Python changes, CLI tests for CLI changes, frontend
tests for Dart changes. Run E2E tests before committing cross-cutting
changes.

## Shell Commands

Inside `devenv shell`, these commands are available:

| Command                  | Description                                 |
| ------------------------ | ------------------------------------------- |
| `test-backend`           | Run Python unit tests (server + client)     |
| `test-cli`               | Run CLI unit tests only (subset)            |
| `test-frontend`          | Run frontend unit tests with coverage       |
| `test-backend-e2e`       | Run backend E2E tests                       |
| `test-cli-e2e`           | Run CLI E2E tests                           |
| `test-frontend-e2e`      | Run frontend E2E tests (Playwright)         |
| `flutterbuildweb`        | Rebuild Flutter web only                    |
| `build-workspace-image`  | Rebuild workspace image (podman)            |
| `build-base-image`       | Rebuild workspace base image                |
| `build-host-image`       | Build host container image                  |
| `run-host-container`     | Run host container locally                  |
| `trivy-host`             | Scan host image for vulnerabilities         |
| `trivy-workspace`        | Scan workspace image for vulnerabilities    |
| `trivy-workspace-report` | Scan + report no-fix CVEs (or render JSON)  |
| `update-features`        | Fetch features from features.yaml           |
| `kill-containers`        | Stop and remove all klangk containers       |
| `restart`                | Rebuild images and restart devenv processes |
| `rebuild`                | Rebuild workspace image and Flutter web     |
| `serve-docs`             | Serve docs locally for preview              |
| `build-docs`             | Build docs for deployment                   |

## Branch Protection

`main` requires a PR with passing checks before merge:

- `test-backend`
- `test-frontend`
- `test-backend-e2e`
- `test-frontend-e2e`

CLI, CLI E2E, and cross-browser E2E checks also run on PRs but are
not required for merge. You can bypass as repo admin.

## Build Architecture (amd64 / arm64)

All workspace image builds (`build-workspace-image`, `build-base-image`) use podman and build for `$KLANGKBUILD_PLATFORM`, which `devenv.nix` defaults to the host architecture (`linux/arm64` on Apple Silicon, `linux/amd64` elsewhere). This means images build and run natively instead of under QEMU emulation. The host container (`build-host-image`) still uses Docker. Override per-shell via `.env`:

```bash
KLANGKBUILD_PLATFORM=linux/amd64   # force amd64 even on an arm64 host
```

Building the **workspace** image natively requires a **base** image with a matching variant. The base (`ghcr.io/mcdonc/klangk/klangk-workspace-base`) is published as a multi-arch manifest (amd64 + arm64) by `push-base-image`, so `pull-base-image` automatically gets the right variant for the host.
