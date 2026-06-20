# Development

## Running Tests

```bash
# Backend unit tests (Python, pytest, parallel)
test-backend

# Frontend unit tests (Dart, flutter test, 100% coverage required)
test-frontend

# Backend E2E tests (starts real server + podman containers)
test-backend-e2e

# Flutter E2E tests (Playwright, needs flutter build + podman build)
test-frontend-e2e

# Run a specific E2E test
cd src/frontend/e2e-tests && npx playwright test --project=chromium --no-deps --grep "test name"
```

Backend tests for Python changes, frontend tests for Dart changes. Run E2E tests before committing cross-cutting changes.

## Project Layout

```text
src/
  backend/             # FastAPI app
    tests/             # Backend unit tests
    e2e-tests/         # Backend E2E tests
  frontend/            # Flutter web app
    test/              # Frontend unit tests
    e2e-tests/         # Playwright E2E tests
  containers/
    host/              # Host container (Dockerfile, entrypoint)
    workspace/         # Workspace container (Dockerfile, base, entrypoint)
  bridge/              # @klangk/bridge npm package
plugins/               # Built-in plugins (celebrate, beep, etc.)
scripts/               # Build and utility scripts
devenv.nix             # devenv configuration
```

## Shell Commands

Inside `devenv shell`, these commands are available:

| Command                 | Description                           |
| ----------------------- | ------------------------------------- |
| `test-backend`          | Run backend unit tests                |
| `test-frontend`         | Run frontend unit tests with coverage |
| `test-backend-e2e`      | Run backend E2E tests                 |
| `test-frontend-e2e`     | Run Flutter E2E tests (all browsers)  |
| `flutterbuildweb`       | Rebuild Flutter web only              |
| `build-workspace-image` | Rebuild workspace image (podman)      |
| `build-base-image`      | Rebuild workspace base image          |
| `build-host-image`      | Build host container image            |
| `run-host-container`    | Run host container locally            |
| `trivy-host`            | Scan host image for vulnerabilities   |
| `update-plugins`        | Fetch plugins from plugins.yaml       |

## Branch Protection

`main` requires a PR with 4 passing checks before merge:

- `test-backend`
- `test-frontend`
- `test-backend-e2e`
- `test-frontend-e2e`

All 4 run automatically on PRs. You can bypass as repo admin.

## Build Architecture (amd64 / arm64)

All workspace image builds (`build-workspace-image`, `build-base-image`) use podman and build for `$KLANGK_PLATFORM`, which `devenv.nix` defaults to the host architecture (`linux/arm64` on Apple Silicon, `linux/amd64` elsewhere). This means images build and run natively instead of under QEMU emulation. The host container (`build-host-image`) still uses Docker. Override per-shell via `.env`:

```bash
KLANGK_PLATFORM=linux/amd64   # force amd64 even on an arm64 host
```

Building the **workspace** image natively requires a **base** image with a matching variant. The base (`ghcr.io/mcdonc/klangk/klangk-workspace-base`) is published as a multi-arch manifest (amd64 + arm64) by `push-base-image`, so `pull-base-image` automatically gets the right variant for the host.
