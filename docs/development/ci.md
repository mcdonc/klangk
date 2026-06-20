# CI

GitHub Actions run automatically on PRs to main and pushes to release
branches. All workflows also support `workflow_dispatch` for manual
triggering.

## Tests

| Workflow                | File                             | Trigger                                     |
| ----------------------- | -------------------------------- | ------------------------------------------- |
| **Backend Tests**       | `backend-tests.yml`              | Changes to `src/backend/`                   |
| **CLI Tests**           | `cli-tests.yml`                  | Changes to `src/cli/`                       |
| **Frontend Tests**      | `frontend-tests.yml`             | Changes to `src/frontend/lib/`, `test/`     |
| **E2E: Backend Tests**  | `backend-e2e-tests.yml`          | Changes to `src/backend/`, containers       |
| **E2E: CLI Tests**      | `cli-e2e-tests.yml`              | Changes to `src/cli/`, `src/backend/`       |
| **E2E: Frontend Tests** | `frontend-e2e-tests.yml`         | Changes to `src/backend/`, `src/frontend/`  |
| **E2E: Cross-Browser**  | `frontend-e2e-cross-browser.yml` | Scheduled (every 6 hours), release branches |

Unit tests (backend, CLI, frontend) run with `pip install` or
`flutter test` and do not require devenv. E2E tests use
`devenv shell` with the full environment (podman, workspace image,
nginx).

## Security

| Workflow   | File         | Description                              |
| ---------- | ------------ | ---------------------------------------- |
| **CodeQL** | `codeql.yml` | GitHub code scanning for vulnerabilities |

## Container images

| Workflow                       | File                       | Description                             |
| ------------------------------ | -------------------------- | --------------------------------------- |
| **Build Workspace Base Image** | `image-workspace-base.yml` | Build and push the base workspace image |
| **Build Workspace Image**      | `image-workspace.yml`      | Build and push the workspace image      |

## Release and publishing

| Workflow                | File              | Trigger           | Description                       |
| ----------------------- | ----------------- | ----------------- | --------------------------------- |
| **Release**             | `release.yml`     | Manual            | Build and publish host container  |
| **Publish CLI to PyPI** | `cli-publish.yml` | Push `cli-v*` tag | Build and publish klangkc to PyPI |
| **Deploy Docs**         | `docs.yml`        | Manual            | Deploy docs to GitHub Pages       |
