# CI

GitHub Actions run automatically on PRs and pushes to main (all also support `workflow_dispatch` for manual triggering):

- **Backend tests** (`.github/workflows/backend-tests.yml`) — triggered by changes to `src/backend/` or `pytest.ini`
- **Frontend tests** (`.github/workflows/frontend-tests.yml`) — triggered by changes to `src/frontend/lib/`, `src/frontend/test/`, or `src/frontend/pubspec.yaml`. Uses `stub_dart_plugins.sh` to create a minimal `klangk_plugins` package so `flutter pub get` works without the full plugin codegen.
- **E2E tests** (`.github/workflows/frontend-e2e-tests.yml`) — merge-gating: runs Chromium + API tests on PRs to main. Requires `KLANGK_LLM_API_KEY`, `KLANGK_LLM_BASE_URL`, and `KLANGK_LLM_MODEL` secrets. Starts `devenv processes up -d` to run nginx (providing the LLM proxy) before running tests. Uploads test results and backend logs as artifacts on failure.
- **Cross-browser E2E** (`.github/workflows/frontend-e2e-cross-browser.yml`) — scheduled every 6 hours and on release branches. Runs Firefox and WebKit in addition to Chromium.
- **Container images** (`.github/workflows/image-workspace-base.yml`, `image-workspace.yml`) — build and push workspace container images
- **Release** (`.github/workflows/release.yml`) — release automation
