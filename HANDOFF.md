# Handoff: issue #1393 — make the whole test corpus runnable concurrently

**Branch:** `issue-1393-make-the-whole-test-corpus-runnable-concurrently-free-ports`
**Worktree:** `.worktrees/issue-1393-make-the-whole-test-corpus-runnable-concurrently-free-ports`
**Issue:** https://github.com/mcdonc/klangk/issues/1393
**Status:** Core work done, committed, pushed. **Remaining work below.**

> Read this top to bottom before resuming. All commands must be run through
> `devenv --quiet -O dotenv.enable:bool false shell -- ...` (see `AGENTS.md`).

## What's done (committed)

### A. Unit-suite conflation — FIXED ✅

Root cause was **pytest config discovery**, not a fixture clash. When run
together, `python -m pytest src/backend/tests src/cli/tests` resolves
rootdir to the **repo root**, which had no `[tool.pytest.ini_options]`, so
`asyncio_mode` fell back to `strict` → every async fixture (`db`, etc.)
errored with "no plugin or hook that handled it". A second issue: both
per-package configs set `--capture=no` but the root had none, so the
combined run used default `--capture=fd`, replacing stdin with
`DontReadFromInput` → 5 CLI `run_shell` tests failed on `stdin.fileno()`.

**Fix:** `pyproject.toml` (repo root) now has:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
addopts = "--capture=no"
```

No coverage in the root config — the 100% gate stays per-package (each
suite alone still resolves rootdir to `src/backend` or `src/cli`).

**Verified:** `python -m pytest src/backend/tests src/cli/tests -n auto --no-cov`
→ **2818 passed**. Per-suite CI runs still 100% coverage (backend 2343,
cli 477).

### B. Free-port allocation in all E2E harnesses — DONE ✅

Added `free_port()` to `src/backend/klangk_backend/model/ports.py`
(returns an OS-assigned ephemeral port via `socket.bind(("127.0.0.1", 0))`),
exported from `model/__init__.py`, with 2 unit tests in
`src/backend/tests/test_model.py::TestPortAllocations`.

Replaced **every** hardcoded port across all E2E + sandbox suites:

- Backend E2E (`src/backend/e2e-tests/`): `test_agent_home_e2e.py`,
  `test_api_e2e.py` (2 servers), `test_event_fanout.py`,
  `test_health_check_e2e.py`, `test_nginx_acl_e2e.py` (3 servers + the
  old local `_find_free_port` now delegates to `free_port`),
  `test_per_user_home.py`, `test_service_command_shared_e2e.py`,
  `test_sighup_restart_e2e.py`.
- CLI E2E (`src/cli/e2e-tests/`): `test_cli_e2e.py` (session server + 5
  class-scoped servers), `test_monitor_e2e.py`, `test_terminal_windows_e2e.py`.
- Sandbox (`sandboxes/tests/`): `hermes/test_hermes_setup_e2e.py`,
  `openclaw/test_openclaw_setup_e2e.py`.

Both `KLANGK_PORT` (server) and `KLANGK_PORT_RANGE_START` (workspace
container hosted-app range) are now `str(free_port())`.

**Verified:** `test_nginx_acl_e2e.py` (26 tests, starts backend+nginx,
no containers) green. `test_health_check_e2e.py` (2 tests, spawns real
podman containers) green — logs show ports allocated from the ephemeral
range, e.g. `[47363, 47364, ...]`. `TestLogin` from CLI E2E green.

### C. Instance-scoped container cleanup — DONE ✅

The CLI E2E (3 files) and sandbox (2 files) `_stop_server` helpers used
`label=klangk.managed=true` — a cross-suite/cross-worker hazard (one
suite's teardown nuked another's containers). Replaced with
instance-scoped cleanup using `klangk-instance-id` resolved from the
test's `data_dir` (the proven pattern from `test_api_e2e.py`). Backend
E2E already used specific labels — unchanged.

### D. xdist unblocking + single command — DONE ✅

- `devenv.nix`: dropped `-p no:xdist` from `test-cli-e2e`,
  `test-terminal-windows-e2e`, `test-backend-e2e`. Added `test-unit`
  (combined unit corpus) and `test-all` (unit + backend-e2e + cli-e2e).
  E2E runs serially by default with an opt-in comment for
  `-n auto --dist=loadscope`.
- `.github/workflows/sandbox-e2e-tests.yml`: dropped `-p no:xdist`.
- **Verified xdist works on E2E:** `test_nginx_acl_e2e.py -n auto
--dist=loadscope` → 26 passed (module/class-scoped fixtures stay
  cohesive with `loadscope`).

### Changelog

Added an entry under `## [Unreleased] → ### Added` in `docs/changes.md`.

---

## Remaining work (NOT done)

### 1. ⚠️ Warnings cleanup (the task that was interrupted)

The user asked to "fix any warnings you find during test runs." This was
**aborted before any investigation**. Resume by running:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- \
  python -m pytest src/backend/tests src/cli/tests -n auto --no-cov 2>&1 \
  | grep -iE "warning|Warn" | sort | uniq -c | sort -rn
```

Known warnings likely to appear (from the nginx_acl run I did):

- **`PytestRemovedIn10Warning: Class-scoped fixture defined as instance method is deprecated.`**
  Hits `test_nginx_acl_e2e.py` (`TestNginxAclEnforcement`,
  `TestNginxDenyByDefault`, `TestNginxAuthLocalAcl`) — their
  `@pytest.fixture(autouse=True, scope="class")` fixtures are defined as
  instance methods. Fix: add `@staticmethod` and set attrs on `cls`
  (the warning message itself says how). **Check whether other E2E files
  have the same pattern** (`test_cli_e2e.py` class-scoped servers already
  use `@staticmethod` — they're fine).
- There may be a `ResourceWarning` (the combined unit run output
  mentioned "Enable tracemalloc to get traceback where the object was
  allocated" / resource-warnings). Investigate the source.

Run each suite and fix warnings in the files you touch. Re-run with
`-W error` scoped to specific categories to find them fast.

### 2. ⚠️ Full E2E validation not run

I only ran **representative** E2E tests (nginx_acl, health_check, CLI
TestLogin) to validate the free-port + cleanup changes. The **full** E2E
suites were not run end-to-end (they take a long time + spawn many
containers). Before merging, run:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- test-backend-e2e
devenv --quiet -O dotenv.enable:bool false shell -- test-cli-e2e
```

Sandbox E2E (`sandboxes/tests`) needs network + does real installs
(npm/uv + git clone) — run if feasible, otherwise rely on CI.

### 3. ⚠️ `test-all` script not run end-to-end

The `test-all` devenv script is written but not executed (it would run
the full corpus). At minimum smoke-test the unit portion:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- test-unit
```

### 4. Open question from the issue: container concurrency ceiling

The issue asks how many workspace containers can run concurrently before
rootless-podman / resource limits bite. **Not investigated.** The E2E
suites default to serial (no `-n`) so this isn't blocking, but if you
want to enable `-n auto --dist=loadscope` by default for E2E, you'd need
to cap workers (e.g. `-n 4`) or document the ceiling. The
`--dist=loadscope` flag is important: it keeps each module/class-scoped
server fixture on a single worker (otherwise the same server fixture
would be requested by multiple workers and they'd each try to start it).

### 5. PR not opened

The branch is pushed but **no PR exists yet**. After the remaining work
above, open one:

```bash
cat <<'EOF' | devenv --quiet -O dotenv.enable:bool false shell -- \
  gh pr create --base main \
  --head issue-1393-make-the-whole-test-corpus-runnable-concurrently-free-ports \
  --title "fix(#1393): make the whole test corpus runnable concurrently" \
  --body-file -
<body here — note the PR uses --body-file - per AGENTS.md, NOT --body ->
EOF
```

Reference the issue (`Closes #1393`).

---

## Key design decisions (so you don't re-litigate them)

1. **`free_port()` lives in `klangk_backend.model.ports`** — the natural
   home (already has `port_in_use`/`scan_free_ports`). It returns `int`;
   E2E harnesses cast with `str(free_port())` at the env edge. The
   nginx_acl local `_find_free_port` (which returned `str`) is kept as a
   one-line shim `return str(free_port())` rather than rewriting all its
   call sites — feel free to inline it if you prefer.

2. **Root `pyproject.toml` config is intentionally minimal** — no
   coverage, no `--cov-fail-under`. The combined run is a pass/fail
   smoke; the 100% gate stays per-package. This is because the two
   packages have different `--cov=<pkg>` targets and you can't cleanly
   combine them in one `addopts`.

3. **E2E defaults to serial, not `-n auto`** — the issue's acceptance
   criteria allow "each individual suite is explicitly documented as
   serial-only with the reason." I chose serial-by-default-with-opt-in
   because (a) container concurrency ceiling is an open question, and
   (b) `loadscope` is the correct dist mode and users should opt in
   knowingly. The comments in `devenv.nix` explain this.

4. **Instance-scoped cleanup uses `klangk-instance-id`** resolved from
   `data_dir` (not the server's auto-generated ID fetched over HTTP,
   which is what `test_agent_home_e2e.py` does). The `klangk-instance-id`
   CLI is deterministic from `data_dir` and works even if the server is
   already dead at teardown time — more robust. Verified it returns a
   UUID even for a fresh dir.

## Files changed (19 + changelog)

See `git diff --stat` on the branch. The stray `devenv.lock` files that
appeared in `sandboxes/tests/` and `src/cli/e2e-tests/` were **deleted**
(spurious devenv artifacts, not part of the change) — if they reappear
during your test runs, `rm` them before committing.
