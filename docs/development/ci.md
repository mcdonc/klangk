# CI

GitHub Actions run automatically on PRs to main and pushes to release
branches. All workflows also support `workflow_dispatch` for manual
triggering.

## Tests

| Workflow                | File                             | Trigger                                        |
| ----------------------- | -------------------------------- | ---------------------------------------------- |
| **Python Tests**        | `backend-tests.yml`              | PRs; pushes to `release/**`                    |
| **Sidecar Tests**       | `sidecar-tests.yml`              | PRs; pushes to `release/**`                    |
| **Frontend Tests**      | `frontend-tests.yml`             | Changes to `src/frontend/lib/`, `test/`        |
| **E2E: Backend Tests**  | `backend-e2e-tests.yml`          | Changes to `src/klangk/`, containers           |
| **E2E: CLI Tests**      | `cli-e2e-tests.yml`              | Changes to `src/klangk/`, containers           |
| **E2E: Frontend Tests** | `frontend-e2e-tests.yml`         | Changes to `src/klangk/`, `src/frontend/`      |
| **E2E: Sandbox Tests**  | `sandbox-e2e-tests.yml`          | PRs (stock runners; `nix` opt-in via dispatch) |
| **E2E: Cross-Browser**  | `frontend-e2e-cross-browser.yml` | Scheduled (every 6 hours), release branches    |
| **E2E: Super (host)**   | `super-e2e.yml`                  | Manual, release branches                       |
| **API Fuzz**            | `fuzz-check.yml`                 | PRs; pushes to `release/**`                    |
| **macOS Smoke**         | `macos-smoke.yml`                | PRs; pushes to `release/**`                    |

Unit tests (Python, frontend) run with `pip install` or `flutter test`
and do not require devenv. The Python suite covers both the `klangkd`
(server) and `klangk` (client) packages from one `pip install -e src/klangk`;
E2E tests use `devenv shell` with the full environment (podman, workspace
image, the proxy). The six E2E workflows default to stock GitHub-hosted
runners; the self-hosted NixOS runner is an opt-in via `workflow_dispatch`.

## Security

| Workflow                  | File                        | Description                                      |
| ------------------------- | --------------------------- | ------------------------------------------------ |
| **CodeQL**                | `codeql.yml`                | GitHub code scanning for vulnerabilities         |
| **Python Deps Audit**     | `python-deps-audit.yml`     | pip-audit of the locked Python dependency set    |
| **Daily Fuzz**            | `fuzz-daily.yml`            | Scheduled API fuzzing (daily, 06:00 UTC)         |
| **Workspace Image Scan**  | `trivy-workspace-scan.yml`  | Scheduled Trivy scan (Mondays, 06:00 UTC)        |
| **CDK Host Pentest**      | `cdk-host-pentest.yml`      | Scheduled pentest of the host image (daily)      |
| **CDK Workspace Pentest** | `cdk-workspace-pentest.yml` | Scheduled pentest of the workspace image (daily) |

## Container images

| Workflow                       | File                       | Description                             |
| ------------------------------ | -------------------------- | --------------------------------------- |
| **Build Workspace Base Image** | `image-workspace-base.yml` | Build and push the base workspace image |
| **Build Workspace Image**      | `image-workspace.yml`      | Build and push the workspace image      |
| **Build FIPS Workspace Image** | `image-workspace-fips.yml` | Build and push the FIPS workspace image |
| **Build FIPS Host Image**      | `image-host-fips.yml`      | Build and push the FIPS host image      |

## Release and publishing

| Workflow        | File             | Trigger                 | Description                                                                              |
| --------------- | ---------------- | ----------------------- | ---------------------------------------------------------------------------------------- |
| **Release**     | `release.yml`    | Push a `v*` tag         | Build and publish host container **and** the `klangk` wheel to PyPI (trusted publishing) |
| **Dist Smoke**  | `dist-smoke.yml` | Manual                  | Verify the to-be-published wheel serves a working login page before tagging              |
| **Deploy Docs** | `docs.yml`       | Push a `v*` tag, manual | Deploy versioned docs to GitHub Pages via the gh-pages branch                            |

Releases are cut by pushing a `v*` tag; see
[Releasing](releasing.md) for the full procedure.
