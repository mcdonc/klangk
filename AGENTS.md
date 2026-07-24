# Klangk agent instructions

Project-specific guidance for coding agents working in this repo.

## Prefix most commands with `devenv --quiet shell --`

Klangk uses [devenv](https://devenv.sh) (Nix-based) for its dev environment. **Every command that touches the project toolchain must be run through the devenv shell**, including git. The toolchain — Python venv, Node, Dart/Flutter, podman, pre-commit hooks, etc. — only exists inside the shell.

```bash
devenv --quiet -- git commit -m "..."
devenv --quiet -- pytest
```

The flags: `--quiet` suppresses noisy devenv output; `-O dotenv.enable:bool false` prevents devenv from loading `.env` (which can interfere with test environments that set their own env vars via monkeypatch). `shell --` launches an ephemeral shell with the full environment, runs the command, and exits — this is the pattern agents should use for one-off commands. (`devenv shell` with no `--` drops into an interactive shell; not useful for non-interactive agents.) This applies to **all** commands: builds, tests, lint, `git`, `podman`, `flutter`, `gh`.

A long-running interactive `devenv up` (backend + proxy + workspace image build) is a human-facing workflow; agents generally don't run it. If you need the backend up for something, ask.

## Running tests (match CI)

Always run the test suites **the way CI runs them**. The exact invocations
are:

```bash
# Python (single klangk package, server + CLI)
devenv --quiet -O dotenv.enable:bool false shell -- python -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests -v -n auto

# Frontend
devenv --quiet -O dotenv.enable:bool false shell -- flutter test --coverage
```

`-n auto` (pytest-xdist) is **not optional** for the Python suite — it's how
CI runs it, and it is the difference between a real and a bogus coverage
number. The `conftest.py` sets `COVERAGE_CORE=sysmon` specifically so that
code executed inside SQLAlchemy's greenlet context is tracked **in each
xdist worker**; without `-n` (a single-process run) that tracking
under-counts and you'll see a false ~93% total with heavy files like
`api/auth.py` reported at ~55%. Run with `-n auto` and coverage matches CI
(100%, every module). Don't try to "reproduce" a coverage drop from a
single-process run — re-run with `-n auto` first.

The 100% coverage gate is enforced on the `klangk` package; a new code
path with no test will fail the build. When iterating
fast on one file you can scope with `-k` / a path and add `--no-cov`, but
re-run the full suite **with** coverage (and `-n auto`) before committing.

## `app` ownership rule

State objects (owned subsystems constructed in `build_app` as
`app.state.X = X(app)`) take **only `app`** and cache **only
`self.app`**. Never cache a subobject of `app` on an instance,
and never pass one into a constructor:

```python
# DO
workspaces = Workspaces(app)
podman = Podman(app)
# read live at call time:
path = self.app.state.settings.data_dir

# DON'T — caches a stale snapshot that survives a settings reload / swap
self.settings = app.state.settings
self.podman = app.state.podman
self.secret = app.state.settings.jwt_secret
PortAllocator(self)          # pass app_state, not self
Podman(app.state.settings)   # pass app_state, not a subobject
```

Settings-derived values (`jwt_secret`, `data_dir`, `image_name`, …) are read
live off `self.app.state.settings.<field>` — typically via `@property` — not
materialized into instance attributes at construction. This is what makes a
runtime swap (the SIGHUP config reload, #1587) propagate without per-subsystem
`reconfigure()` boilerplate. Cached subobject references silently keep the old
value after a swap and are a recurring source of stale-config bugs (#1608).

## Process manager: devenv 2.x native (not process-compose)

`devenv processes up` / `devenv up` use **devenv 2.x's built-in process manager**,
not process-compose. Consequences when debugging a managed stack:

- `devenv processes list|status|logs|restart <NAME>` work without a separate
  `process-compose` daemon running — there is no `process-compose` binary or
  socket to look for. `ps` will **not** show a `process-compose` process; the
  manager is devenv itself.
- A crashed process is restarted by devenv's own supervisor (the journal shows
  `Process exited (Failure), restarting` / `Restarted (attempt N)`), and after
  enough attempts the whole `devenv processes up` invocation exits.
- On hosts that run the stack under systemd, the unit's `ExecStart` is
  `devenv processes up` (foreground, `DEVENV_TUI=false`); a crash loop in one
  process takes the unit down. Debug by running the suspect process directly
  under the devenv shell (bypassing the supervisor) to see its real stderr.

## CLI subpackage isolation (`klangk.cli`)

Code in `src/klangk/klangk/cli/` (the `klangk` client) must **not** import
anything from the rest of the `klangk` package — only stdlib, third-party
deps, and sibling modules within `cli/` itself (`from .config import ...`,
`from .transport import ...`). The CLI is a standalone client that ships in
the same wheel but runs in the user's environment against a remote backend;
it has no access to the server's `app.state`, settings, or process-local
singletons.

Practical consequence: anything centralized on the server side as an
`app.state.*` object (e.g. logging via a `Logger(app)` state object) is
**not** shared with the CLI — the client keeps its own setup and reaches the
backend only over HTTP/WebSocket. Don't refactor `cli/` to import a shared
helper from `klangk.*`; duplicate the small bit of logic instead, or put the
shared code somewhere both can import without crossing the boundary.

## Changelog (`docs/changes.md`)

`docs/changes.md` is the single source of truth for human-authored release notes,
formatted as [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). It has two
rendering surfaces:

- **Docs site** — the whole file renders as one page at `/changes/`, sidebar entry
  "Changelog" (nav is in `zensical.toml`). Includes the `## [Unreleased]`
  section, so in-flight work is visible.
- **Release tab** — when a `v*` tag is pushed, `release.yml` checks out the code
  **at the tag**, extracts that version's `## [<version>]` section, and prepends it
  to GitHub's auto-generated notes (PR list + compare link).

### When to add an entry

Add a bullet under `## [Unreleased]` **in the same PR that introduces the change**
(not as an afterthought, not after merge). Use the matching subsection:

- **Added** — new feature, config var, CLI flag, endpoint.
- **Changed** — change to existing behavior, default, or signature.
- **Deprecated** — soon-to-be-removed.
- **Removed** — now removed.
- **Fixed** — notable bug fix.
- **Security** — vulnerability fix.
- **Breaking** — sub-section under any version for changes requiring operator/integrator
  action on upgrade. Call out the migration.

Each entry: one line, reference the issue/PR (`(#1375)`), enough context that an
operator skimming the changelog understands the impact.

Add an entry for anything an **operator, integrator, or end user** would notice:
new/changed config or defaults, behavior changes, security fixes, notable fixes,
new features.

**Skip** entries for: pure internal refactors (moving code between modules,
renaming internal classes/variables, restructuring state objects), test/CI/doc
churn with no user-visible effect, and dependency bumps that don't change
behavior. Internal architecture changes (e.g. "X is now a class instead of free
functions", "Y now takes app instead of app_state") are invisible to users and
create noise — do not add changelog entries for them.

### When to garden for a release

Right before pushing the tag — do this as its own commit on `main`:

1. Rename `## [Unreleased]` → `## [vX.Y.Z] - YYYY-MM-DD`
   (today's date). The `v` prefix and bracket form **must match the tag exactly**;
   the `- YYYY-MM-DD` date suffix is optional but conventional. The workflow matches the section
   heading as a prefix, so `## [v1.0.5] - 2026-07-07` matches tag `v1.0.5`.
2. Insert a fresh, empty `## [Unreleased]` heading directly above it.
3. Commit, e.g. `chore(changelog): cut vX.Y.Z`.
4. Tag and push: `devenv --quiet -O dotenv.enable:bool false shell -- git tag vX.Y.Z && devenv --quiet -O dotenv.enable:bool false shell -- git push origin vX.Y.Z`.

**Critical sequencing:** `release.yml` checks out `docs/changes.md` at the tagged
commit, so the `[Unreleased]` → `[vX.Y.Z]` rename **must land in (or before) the
commit you tag**. If you tag a commit that still has the changes under
`[Unreleased]`, the workflow finds no `## [vX.Y.Z]` section and the release body
falls back to pure auto-generated notes — the human-authored section is silently lost.

### After a release

Nothing to do in `docs/changes.md` itself — the `[Unreleased]` heading you created
at cut time is already in place for the next cycle's entries. Just start adding new
entries under it.

## Demo video recording

Before **every** full recording run (CLI + browser scenes, or a re-run of just
the browser half), you MUST first destroy the hero account so all its
workspaces + containers cascade-delete with it. This is the only reliable way
to get a clean slate — a prior interrupted run or a browser-only re-run leaves
stale workspaces/tabs that corrupt the continuity later scenes assume.

Do it as an explicit step 0 before `record-cli.sh`, using the seed's reset
(which deletes the hero via `DELETE /admin/users/<id>` → cascades, then
recreates the hero + Potemkin workspaces):

```bash
devenv --quiet -O dotenv.enable:bool false shell -- node --experimental-strip-types \
  src/frontend/e2e-tests/demo/demo-seed.ts --reset
```

`record-cli.sh`'s Scene 2 prep also calls `--reset`, but do NOT rely on that
alone — run the destroy consciously and explicitly every time.
