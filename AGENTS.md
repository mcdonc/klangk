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

## CI runners (stock by default, nix opt-in)

The five E2E workflows (backend, CLI, frontend, cross-browser, sandbox)
default to **stock GitHub-hosted runners** (`ubuntu-latest`); on stock
runners the `devenv-setup` action installs nix + devenv + podman/uidmap per
run. To run a suite on the self-hosted NixOS runner (label `nix`, the
klangk-ci VM — toolchain from the host config), trigger it via
`workflow_dispatch` and set the `runner` input to `nix`; push/PR/schedule
triggers always use the stock default (#2772).

## Running tests (match CI)

Always run the test suites **the way CI runs them**. The exact invocations
are:

```bash
# Python (single klangk package, server + CLI)
devenv --quiet -O dotenv.enable:bool false shell -- python -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests -v -n auto

# Build-pipeline contract tests (scripts/tests — a separate CI step; not
# part of the suite above, so run them too when touching scripts/, the
# image Dockerfiles, or anything in the build path — #2629)
devenv --quiet -O dotenv.enable:bool false shell -- python -m pytest scripts/tests -v

# Frontend
devenv --quiet -O dotenv.enable:bool false shell -- flutter test --coverage
```

`-n auto` (pytest-xdist) is **not optional** for the Python suite — it's how
CI runs it, and it is the difference between a real and a bogus coverage
number. The conftests pin `COVERAGE_CORE=sysmon` (the toolchain is Python
3.14, #2844): sys.monitoring measures branches there and tracks
greenlet-executed code (SQLAlchemy's async engine) natively; without `-n`
(a single-process run) that tracking under-counts and you'll see a false
~93% total with heavy files like `api/auth.py` reported at ~55%. Run with
`-n auto` and coverage matches CI (100%, every module). Don't try to
"reproduce" a coverage drop from a single-process run — re-run with
`-n auto` first.

The 100% gate is **full branch coverage** (`--cov-branch`, #2834): every
branch outcome must be exercised, not just every line — an `if` needs both
its true and false outcomes tested (or a `# pragma: no branch` comment with
a justification for structurally unreachable arms). No `concurrency`
option in the coverage config: sysmon does not support it, and on 3.14 it
is not needed. The gate is enforced on the `klangk` and `klangksidecar`
packages; a new code path — or a new branch outcome —
with no test will fail the build.

### Rapid iteration: use the `testmon` task

For edit/test loops, **default to the `testmon` task** instead of the full
suite. `pytest-testmon` (in the `test` extra) selects only the tests whose
coverage touches your changed lines, so a typical local change re-runs in
~10s vs. the full ~60s:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- testmon
```

It baselines the line→test map into `src/klangk/.testmondata` on the first
clean-tree run, then re-runs just the affected subset. Reach for it
whenever the change is localized to already-covered code — that's most
edits to an existing module.

Fall back to the full suite (or re-baseline) when testmon can under-select:

- **Broad changes** — a large refactor, a branch switch, or edits to shared
  `conftest.py` / fixtures / base classes. testmon can't tell that a
  changed fixture affects tests that don't import it by line coverage, so
  delete `src/klangk/.testmondata` to re-baseline, or just run the full
  suite.
- **New code path** with no prior coverage is never selected — write the
  test first, then iterate with testmon.
- `-k <name>` / a single path plus `--no-cov` is an escape hatch for one or
  two tests, not a default.

`--no-cov` in the task is intentional: a scoped run exercises only a
fraction of the package, so the 100% gate does not apply to it.

### Before push: the scoped `test-push` gate

For a routine push at the end of a focused session (a workon branch,
say), `testmon`-style selection is enough — CI runs the authoritative
full suite on the same hardware moments later. Use the scoped gate:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- test-push
```

It diffs the working tree against the merge-base with `origin/main` and
runs only the suites whose area changed (`testmon` for `src/klangk/`,
`scripts/tests` for `scripts/` + `src/containers/`, sidecar unit, flutter
unit). Skipped areas are safe to skip because CI re-runs everything.

### Before merge: CI green against the latest push

The merge gate is CI passing on the latest pushed commit (after the
rebase), not a local re-run of the full suite. testmon (and `test-push`
generally) is a **local accelerator only** — CI runs the authoritative
full suite with `-n auto` and coverage, the same hardware, moments
later. A local `test-backend` / `test-frontend` run is optional: use it
when you want a coverage signal locally or don't trust a scoped run's
"passing", not as a required pre-merge step
(`devenv --quiet -O dotenv.enable:bool false shell -- test-backend`).

Operational notes: `.testmondata` is per-worktree (rootdir-relative) and
gitignored; concurrent `testmon` runs in the same worktree serialize on
sqlite's busy-lock (a brief stall, not corruption).

### Do not run E2E tests locally "to make sure"

Do **not** run the E2E suites (`test-backend-e2e`, `test-cli-e2e`) locally
as a pre-commit sanity check. The CI runner is a VM on the same machine, so
a local E2E run duplicates exactly the same work CI will do moments later —
it wastes time without adding signal. Run targeted E2E tests locally only
when actively debugging a specific E2E failure (with `-k <test_name>`).

## Verifying behavior empirically (avoid repro loops)

When you need to confirm a runtime behavior empirically, **add a temporary
test to the existing pytest suite and run it** — do not write a standalone
script (`asyncio.run(...)`, `if __name__ == "__main__"`, a one-off driver)
that instantiates app/framework components directly.

The harness exists to provide the setup production code depends on: the
autouse fixtures stub background workers and fake the HTTP/WS backends; the
conftests pin `COVERAGE_CORE=sysmon` for greenlet-safe branch coverage;
`run_test()` owns the textual event
loop. A standalone script skips all of that, so it either **hangs** (commonly:
`run_test()` context-exit waits forever for a real on-mount worker making real
network calls to a fake URL) or exercises behavior that doesn't match
production.

This bites hardest for the textual TUI: `MainScreen` spawns real
`_status_loop` and `_token_refresh_loop` workers on mount. Outside the pytest
harness (whose autouse `_stub_tui_bg_workers` fixture stubs them) `run_test()`
teardown hangs forever. To verify TUI behavior, add a temp test to
`test_tui.py` using the existing helpers (`_real_status_loop`,
`_fast_reconnect`, `_ws`, `_authed_state`), run it the CI way, then delete it.

- Prefer reading the code and reasoning; reach for a temp test only when a
  claim genuinely needs proof.

**If a command you launched hangs or emits no output for a long time, do not
re-run the identical command.** Diagnose first — check whether a child process
is blocked (on teardown, on a network call, on a missing stub). Re-running a
hung command verbatim just burns another cycle on the same hang. Figure out
the cause or switch approaches; a re-run is only justified after you have
changed something that should make it succeed.

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

## Raw SQL containment (`klangk.model`)

Database access — SQL string literals and any `.execute()`, `.executemany()`,
`.executescript()`, `.fetchone()`, or `.fetchall()` call — belongs **only inside
`src/klangk/klangk/model/`**, the data-access layer. Code anywhere else in the
backend (`api/`, `lifecycle.py`, `workspaces.py`, `wshandler/`, …) goes through
the model-layer API (`app.state.model.users.*`,
`app.state.model.workspaces.*`, …) (#3068). The two sanctioned multi-step
patterns that do open a transaction outside `model/` pass an owned connection
_into_ a model helper rather than writing SQL: the register route
(`api/auth.py`) and the admin invite route (`api/admin.py`) both use
`app.state.model.transaction() as db` + `insert_unverified_user(db, …)` —
no SQL literal at the call site. (Shelling out to the `sqlite3` CLI, e.g. the
doctor probe, is not DB access.) The `klangk.cli` subpackage never touches the
DB at all (see its isolation rule below).

Check before committing:

```bash
rg '\.execute\(|\.executemany\(|\.executescript\(|\.fetchone\(|\.fetchall\(' \
  src/klangk/klangk -g '*.py' --glob '!**/model/**' --glob '!**/cli/**'
```

should come back empty.

## Naming: avoid leading underscores

Do not start module names, function/method names, class names, or
globals with an underscore unless absolutely necessary, or unless it is
important to signal that the object must not be imported (private API).
Prefer plain names; modules that must sort by number can use a letter
prefix (`m0001_password_history.py`, not `_0001_password_history.py` —
leading-digit names cannot be imported with plain `import` syntax
anyway).

## Environment variable naming

Env vars use a **category prefix** formed by concatenating `KLANGK` with the
category word and **no underscore**, then a single underscore before the field
name: `KLANGK<WORD>_<FIELD>`. Existing categories: `KLANGKD_` (daemon),
`KLANGKWS_` (in-workspace runtime), `KLANGKBUILD_` (build tooling), `KLANGKC_`
(CLI), `KLANGKNETWORK_` (the network sidecar).

**Never insert an underscore between `KLANGK` and the category word.** The first
underscore must come _after_ the full category word. `KLANGK_NETWORK_UPSTREAM`
is wrong (it parses ambiguously — is the category `KLANGK` or `KLANGK_NETWORK`?);
use `KLANGKNETWORK_EGRESS_UPSTREAM`. The network sidecar handles both ingress
and egress, so its vars further sub-namespace by subsystem —
`KLANGKNETWORK_EGRESS_*` (the DNS-filtering proxy + OUTPUT ruleset) vs a future
`KLANGKNETWORK_INGRESS_*` (host port publishing); a shared tool path like
`KLANGKNETWORK_IPTABLES` stays at the category level. When you add a new family,
pick one category word and concatenate it onto `KLANGK` so `grep -E
'^KLANGK<WORD>_'` matches the whole family exactly. Single-letter categories
are fine too (`KLANGKD` = daemon, `KLANGKC` = CLI).

## Python complexity gate (xenon)

Every function, method, and block in `src/klangk/klangk/**/*.py`,
`src/klangksidecar/klangksidecar/**/*.py`, and
`scripts/**/*.py` must be xenon rank **A or better** (cyclomatic complexity
≤ 5), and the per-module and codebase **averages** must also stay rank A
(≤ 5). The gate runs as the `xenon` pre-commit hook defined in `devenv.nix`
(`--max-absolute A --max-modules A --max-average A`); on every commit that
stages a graded `.py` file it grades the **full tree** (`pass_filenames =
false` — a staged subset's average can exceed 5 while the whole tree
passes, so partial grading would flap). Nothing in CI enforces it yet —
the hook is the only gate, so do not bypass it with `--no-verify`.

Check locally before committing:

```bash
devenv --quiet -O dotenv.enable:bool false shell -- pre-commit run xenon --all-files
```

Keep new functions and extracted helpers small; the established patterns
are extract-function and dispatch tables (see `_EDITOR_BUTTON_HANDLERS` in
`cli/tui/screens/workspace_form.py`). When extending a block already near
the limit, extract a helper instead of growing it. Never add noqa-style
escapes or re-widen the gate to make a commit pass.

The legacy F/E/D blocks were refactored down to C
(#2800–#2803, #2808–#2814), the C blocks to B
(#2817, #2818–#2842), the module and codebase averages under the B
gate (#2846), and finally every block and average to A (#2845, T1–T31).
New code lands at rank **A** (≤ 5) from the start — the gate will not
let anything looser through.

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

## TUI spatial navigation (no focus traps)

The textual TUI must use **spatial navigation** — arrow keys move focus
between logical areas (tab strips, lists, form fields) without requiring
Tab/Shift-Tab to cross boundaries. Never create **focus traps** (a
composite widget that swallows all keys and won't release focus without
Tab). Specifically:

- Down from a tab strip or section header enters the list/pane below it.
- Up from the first row of a list returns focus to the tab strip above.
- Left/Right move between sibling columns or tab pages.
- Tab/Shift-Tab still works as a fallback, but arrows must always be
  sufficient to reach any element in reading order (top-to-bottom,
  left-to-right).
- When implementing a new screen or widget, add the key bridges that let
  arrows cross its boundaries (see `WorkspaceListView.action_cursor_up`
  and `MainScreen.on_key` for the pattern).

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

Each entry must be **2–4 sentences max**. Lead with the **bold setting or feature
name**, then the issue number in parens. State what changed and what operators
need to know. Link to docs if they exist. Do not include internal justification
("because X was broken"), migration history, implementation notes (module names,
internal APIs, code paths), or test infrastructure detail. Only explain old
behavior if the operator must act (Breaking section).

Example:

```markdown
- **`KLANGKD_DNS_SEARCH` (#2055).** Comma-separated DNS search domains
  passed to workspace containers via `--dns-search`. Reloadable on SIGHUP.
```

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

## Inspecting the running frontend (`fmtk` / flutter-mcp-toolkit)

The devenv ships the `fmtk` CLI (flutter-mcp-toolkit, a pinned release
derivation in `devenv.nix`, #2868). It lets an agent inspect and drive a
debug run of the frontend: semantic snapshots, widget details, taps and
typing, hot reload, app logs and errors. pi has no MCP client, so CLI mode
is the interface — there is no MCP server wiring to maintain.

Workflow — use the harness (issue #2881), from the repo root:

```bash
devenv --quiet shell -- fmtk-up
```

It boots a scratch klangkd (127.0.0.1:8998, own state under
`.devenv/state/fmtk`, admin@example.com/admin123abc), an origin-splitting
caddy on 127.0.0.1:8124 (`/api/*` + `/ws` to the backend, everything else
to the flutter dev server on 8125), the fixture, and
`flutter run --debug -d chrome` — then prints the VM-service `ws://` URI
and a ready-to-paste fmtk prefix. Run devenv from the repo **root**
(src/frontend has its own devenv.lock without flutter, so `devenv shell`
there fails with `flutter: not found`).

Launch speed: Ctrl-C stops only the flutter run — the backend, proxy,
and seeded state stay up, so the next `fmtk-up` reuses them (skipping
backend boot, proxy boot, and `pub get`) and is ready in roughly the
flutter compile time alone. `fmtk-down` stops the kept services
(`--wipe` also deletes the scratch state; `fmtk-up --fresh` is
`fmtk-down --wipe` + launch).

The harness exists because the frontend is same-origin only: `baseUrl`
derives from the page origin (klangk-plugin-api `backend_url`), so a
debug app loaded straight from the flutter dev server calls its own
origin for `/api/v1/...` and gets 404s. The caddy proxy plus the
`CHROME_EXECUTABLE` wrapper (`scripts/fmtk-chrome.sh`, which rewrites the
opened URL to the proxy origin) make the debug run reach the backend.

The fixture (seeded by `fmtk-up`, or standalone via
`devenv --quiet shell -- fmtk-seed`) covers every role bucket of the
`fmtk-verify` workspace's Sharing panel — password `fmtk-Pass123!`:

- `fmtk-admin@example.com` — `admins` group member and workspace owner:
  everything visible (Sharing tab with role buckets AND the Advanced ACL
  editor).
- `fmtk-collaborator@example.com` — collaborators bucket member.
- `fmtk-coder@example.com` — coders bucket member.
- `fmtk-spectator@example.com` — spectators bucket member; NO Sharing
  tab.

Each fixture opens the workspace page: fmtk-admin via its owner wildcard,
the role members because every role group carries `join-workspace` — the
WS `workspace_connect` gate requires it (#2975), so a member whose only
grants omit `join-workspace` cannot open the workspace page at all
("Error: Permission denied" before any tab renders) — keep that in mind
when inventing synthetic permission sets. The Terminal tab itself mounts
only for `terminal` holders (spectators included — their tab hosts the
shared terminals they watch).

With the harness up (from another shell, repo root):

```bash
URI=<the ws:// URI fmtk-up printed>
devenv --quiet -O dotenv.enable:bool false shell -- fmtk doctor --vm-service-uri $URI
devenv --quiet -O dotenv.enable:bool false shell -- fmtk exec --name semantic_snapshot --vm-service-uri $URI --args '{}'
devenv --quiet -O dotenv.enable:bool false shell -- fmtk exec --name tap_widget --vm-service-uri $URI --args '{"ref": "s_1"}'
devenv --quiet -O dotenv.enable:bool false shell -- fmtk exec --name enter_text --vm-service-uri $URI --args '{"ref": "s_2", "text": "fmtk-admin@example.com"}'
```

Snapshot nodes carry `ref`s (`s_0`, `s_1`, …) usable in `tap_widget`,
`enter_text`, `fill_form`, and friends. `fmtk capabilities` lists all
commands; `fmtk schema --name <command>` prints each command's arg schema
(`get_recent_logs` takes no `limit` — pass `'{}'`). `get_app_errors`,
`hot_reload_flutter`, and `evaluate_dart_expression` are the other
high-signal ones (though `evaluate_dart_expression` can't import
packages, so it can't drive GoRouter — for hash-route navigation use the
Chrome tab's CDP: the port is the one on the running Chrome's
`remote-debugging-port=` flag, which flutter chooses; `fmtk-up` prints
it, and `/json/list` on that port finds the page target).

UI-driving notes (verified against the harness):

- Log in via the two `textField`s + the `Log In` button; the app lands
  on `/workspaces`. Workspaces you own sit under "Owned by Me"; shared
  ones under the "Shared with Me" segment — tap the segment first, then
  the workspace card.
- The app bar's right edge holds, in order: the email chip (navigates to
  Settings), an admin icon (only for `admins`-group members), and the
  logout icon (rightmost). In snapshots these appear as unlabeled
  `button`s — pick by bounds (top-right corner), not label.
- Icon-only tabs and icon buttons also show up as unlabeled `tappable`/
  `button` nodes; identify them by order (the tab strip reads
  Terminal, Files, Network, Sharing, Settings) or by bounds.
- Material dialogs do not always close on the escape key — tap their
  `Cancel` button instead.

Driving the terminal (verified against the harness):

- The terminal is a canvas (flterm): semantic snapshots show the tab
  strip (the own-terminal tab, the "+" new-terminal button) but never
  the terminal content, and `enter_text` needs an editable ref, so it
  cannot type there. `get_screenshots` also does not work on this
  target (app-owned capture, needs the permission bridge).
- Key-by-key typing is unreliable: `press_key` dispatches lowercase
  letters, `Enter`, and `Space`, but rejects `/`, `-`, `.`, `_`, and
  uppercase letters (`unknown_key`) — and even a dispatched `Enter`
  may not submit a line to the PTY.
- The reliable path is `evaluate_dart_expression` with
  `libraryUri: package:klangk_frontend/terminal/ghostty_terminal.dart`
  (the pubspec name, not the repo name). Walk the element tree for the
  live `GhosttyTerminalState`, then:
  - `st._terminal.sendText('echo hi && whoami\n')` — types AND
    executes (raw input, not bracketed paste); `paste()` only puts
    text on the input line without submitting it.
  - `st._terminal.createFormatter(format: FormatterFormat.plain,
unwrap: true, trim: true).format()` — dumps the visible buffer
    (command, output, prompt, tmux status bar); wrap in
    `try/finally { f.dispose(); }`. This is the substitute for
    screenshots.
  - `st.widget.wsClient` exposes `connected`, `terminalWindows`
    (own PTYs; 0 for spectators), and `sharedTerminals`.
- Caveats: `sendText` returns success even with no PTY behind the
  view (spectator) — always confirm execution by reading the buffer
  output. The container must be running
  (`POST /api/v1/workspaces/{id}/start`, poll `running`); commands
  otherwise go nowhere. `debug_dump_focus_tree` showing
  `ghostty-terminal [PRIMARY FOCUS]` is a good sanity check before
  typing.
- Own-terminal UI (the tab + "+") is gated on `code-in-isolation`:
  owners, coders, and collaborators get an own terminal; spectators
  get none (`terminalWindows` is 0 and `sendText` is inert).

The app side is the debug-only `mcp_toolkit` bootstrap in
`src/frontend/lib/main.dart` — it registers the `ext.mcp.toolkit.*` VM
service extensions, is const-folded out of release builds, and is inert in
widget tests. Pixel screenshots on web additionally ride Chrome CDP
(fmtk auto-discovers it; `--web-browser-debugging-port` overrides);
semantic snapshots and interactions need no CDP.

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

## Worktrees

- When asked to create a worktree, put the worktree inside the repository
  root's `.worktrees` subdirectory. When using a worktree, do not commit
  anything to the main branch or use the main repository to commit anything —
  all commits go on the worktree's own branch within the worktree.
- Worktrees should have a directory name no longer than 16 characters.

## Pull request titles

- Every PR created to address a GitHub issue must include that issue's
  number in the PR title, as a parenthesized suffix — e.g.
  `Add DNS search domains (#2055)`. GitHub appends the PR's own number
  at squash-merge time, so the issue number in the title is what ties
  the PR back to its originating issue in lists, history, and
  notifications.
- This applies to every PR opened from a `gh issue` (via `/workon`,
  `/stackon`, or directly) — check the title before `gh pr create`.

## Stacked PRs (retarget before deleting the base branch)

- A PR merges into its **base** branch. When a PR is stacked on another
  open PR (base = that PR's head branch, not `main`), the base PR must
  merge first — merging the stacked PR first squashes its commits into
  the side branch, never landing them on `main` independently.
- **Before merging the base PR (or letting any cleanup delete its
  branch), retarget every PR based on it to `main`**
  (`gh pr edit <stacked#> --base main`, then rebase its branch onto the
  post-merge `main` and force-push). Deleting a PR's branch closes any
  PR based on it — GitHub does not retarget automatically — and a closed
  PR can be neither reopened (base gone) nor retargeted (closed). The
  only recovery is restoring the deleted branch at its merge SHA,
  reopening, retargeting, and deleting it again (see the #3087/#3095
  sequence). The `/merge` skill deletes the merged PR's branch as
  cleanup, so do the retarget before running it.
