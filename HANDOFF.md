# Handoff: #1426 Composition-Root Refactor

## What this is

A multi-slice refactor to eliminate module-level mutable globals from
`klangk_backend` in favor of one composition root: a `build_app(settings)`
factory. The tracking issue is **#1426**.

**Current branch**: `issue-1449-slice-2-promote-containerregistry-to-an-owned-instance-1426`
**Current PR**: [#1466](https://github.com/mcdonc/klangk/pull/1466) (Slice 2a — CI running)
**Test state**: 2374 passed, 100% coverage, 0 warnings

## Where we are

**Slice 1 is DONE** (merged: #1448, #1455, #1460). **Slice 2 is in progress**
(PR #1466 implements Slice 2a). The four Slice 2 sub-issues (#1462–#1465)
decompose the original Slice 2 (#1449) into independently-shippable PRs.

## Suggested ordering for remaining work

The 7 original slices (#1447–#1454) had a fixed linear chain. The refactor
has since spawned standalone issues (#1456–#1469) that interleave. This is
the suggested order, grouped by theme, with dependencies noted:

### Phase 1: finish Slice 2 (container subsystem)
1. **#1462 / PR #1466 (Slice 2a)** — `ContainerRegistry(settings)` +
   build_app wiring + nginx/podman settings threading. **In progress.**
2. **#1463 (Slice 2b)** — `NginxWatchdog` class → `app.state.nginx_watchdog`.
   Depends on 2a (the nginx renderer settings params land in #1466; the
   watchdog absorbs `_prepare_nginx`'s `get_settings()` call).
3. **#1464 (Slice 2c)** — `ConnectionRegistry` (promote `wshandler.state`).
   Depends on 2a. Can run in parallel with 2b.
4. **#1465 (Slice 2d)** — migrate ~660 test references off `container.registry`
   + `wshandler.state` module globals. Depends on 2a + 2c. Closes Slice 2.

### Phase 2: the remaining subsystem instances (linear)
5. **#1450 (Slice 3)** — `OIDC(settings)` instance + caches. Absorbs the
   `auth_modes(settings)` param (Slice 1 stepping stone → `self.settings`).
6. **#1451 (Slice 4)** — `Plugins(settings)` instance.
7. **#1452 (Slice 5)** — `DB(settings)`; kill db globals.

### Phase 3: env-read retirement + cleanup (can interleave with Phase 2)
8. **#1461** — resolve `file:`/`cmd:` at construction (model validator);
   delete `resolve_indirection` + its ~22 call-site wraps. Pairs with #1453.
9. **#1453 (Slice 6)** — freeze `resolve_env_value`/`resolve_env_bool`
   (132 call sites). The mechanical env-read retirement. After this, no
   code reads env at call time except inside `KlangkSettings`.
10. **#1454 (Slice 7)** — delete the `main:app` shim. Last step — `get_settings()`
    dies when its last caller (the shim) is gone.

### Phase 4: test cleanup (parallel, any time)
11. **#1457** — tests stop monkeypatching `os.environ`; construct
    `KlangkSettings(env={...})` directly. ~421 `setenv`/`delenv` calls
    across 17 files. Pairs naturally with each subsystem slice (migrate
    the tests that touch that subsystem).

### Standalone (independent, any time)
- **#1456** — `frontend_dir` config setting (replace hardcoded repo path).
- **#1459** — `state_dir` field default (no `os.environ.setdefault` mutation).
- **#1467** — centralize logging configuration (no import-time `basicConfig`).
- **#1468** — `Podman(settings)` class (cohesion; 20 functions, no state).
- **#1469** — nginx renderer class (fold into NginxWatchdog or standalone
  `NginxRenderer`; collapses the 10 `settings` params added in #1466).
  Best done with or after #1463 (2b).

## Issue map

| Issue | Title | State |
|-------|-------|-------|
| **#1426** | Tracker: one composition root, no module globals | umbrella |
| #1447 | Slice 1 — `KlangkSettings(env=...)` + settings threading | ✅ done |
| #1458 | Slice 1 remainder — delete `_config_file_path` global | ✅ done |
| **#1449** | Slice 2 — ContainerRegistry owned instance (umbrella) | in progress |
| **#1462** | Slice 2a — ContainerRegistry(settings) + nginx/podman | **in progress (#1466)** |
| #1463 | Slice 2b — NginxWatchdog class | next |
| #1464 | Slice 2c — ConnectionRegistry | next (parallel with 2b) |
| #1465 | Slice 2d — migrate test refs off globals | after 2a+2c |
| #1468 | Slice 2 — `Podman(settings)` class | part of Slice 2 |
| #1469 | Slice 2 — nginx renderer class | part of Slice 2 |
| #1450 | Slice 3 — OIDC instance + caches | |
| #1451 | Slice 4 — Plugins instance | |
| #1452 | Slice 5 — `DB(settings)`; kill db globals | |
| #1453 | Slice 6 — freeze `resolve_env_value` at startup | |
| #1454 | Slice 7 — delete the `main:app` shim | |
| #1456 | Standalone — `frontend_dir` config setting | |
| #1457 | Standalone — tests stop monkeypatching `os.environ` | |
| #1459 | Standalone — `state_dir` field default (no env mutation) | |
| #1461 | Standalone — resolve `file:`/`cmd:` at construction | |
| #1467 | Standalone — centralize logging configuration | |

## Slice 1: DONE (reference)

Three PRs landed Slice 1:

- **#1448 (merged)** — `KlangkSettings(env, config_file=None)` constructor.
  `env` is required (`Mapping[str, str]`). `config_file` optional. Class-var
  bridge (`ClassVar`-annotated, `try/finally` cleanup) for the classmethod
  boundary. No init kwargs. Precedence: env > config file > defaults.
- **#1455 (merged)** — `build_app(settings)` composition root,
  `get_settings_dep`, oidc settings threading, `auth_modes` field validator
  (rejects typos → no silent security downgrade to no-auth mode), cache
  machinery deletion (`get_settings()` is cache-free).
- **#1460 (merged)** — deleted `_config_file_path` global + dead
  `validate_at_startup()`. `klangkd` passes `config_file=` to the constructor
  directly. `get_settings()` returns `KlangkSettings(os.environ)`.

## Slice 2a (PR #1466): what's in this branch

- **`ContainerRegistry(settings)`** — takes `KlangkSettings | None = None`.
  `build_app()` constructs `app.state.container_registry`. Module-level
  `container.registry` stays as a transitional shim (constructed with
  `get_settings()`) — dies in Slice 2d (#1465).
- **Service-session locks** — `terminal._service_session_locks` stays
  physically in `terminal.py` (circular import), but the registry exposes
  `get/clear/prune_service_session_lock` methods that delegate to terminal's
  functions. Dict moves fully in Slice 2d.
- **nginx.py settings threading** — all renderer functions take `settings`
  (required on entry points: `render_config`, `write_config`, `find_nginx_bin`;
  required on internal helpers). `get_settings()` removed from `nginx.py`.
- **podman.py** — `_podman_bin()` keeps plain `get_settings()` (no pointless
  indirection; threading settings through subprocess wrappers is #1468's job).
- **test_nginx.py rewrite** — constructs `KlangkSettings(env={...})` directly,
  no `_set()`/`_settings()`/`_clean_env` fixture, no `os.environ` mutation.

## Design decisions (locked in)

- **`env` is required on `KlangkSettings.__init__`** — forces explicit
  config source. `os.environ` is never read unless explicitly passed.
- **`config_file` defaults to `None`** — `None` means "no config file"
  (legitimate common case for tests/scripts). `"none"` is the explicit
  opt-out string.
- **Init kwargs NOT supported** — dropped from signature.
- **Precedence**: env dict > config file > defaults.
- **`DB(settings)` not `settings.get_db()`** — settings stays a pure
  config value; engine/PRAGMA/cache concerns live on `DB`.
- **No ContextVar** — Pyramid discipline; all threaded explicitly.
- **`get_settings()` is a cache-free shim** — stays until Slice 7
  (#1454) when its last caller (the `main:app` shim) is gone.
- **`get_settings_dep` is for every endpoint that needs settings** — no
  either/or. Per-subsystem Depends (when they arrive in later slices) are
  additive, not alternative.
- **Tests construct `KlangkSettings(env={...})` directly** — no
  `monkeypatch.setenv` reacharound, no `os.environ` mutation. Each test
  specifies the config it wants via explicit `KLANGK_*` keys.
- **Two motivations for classes** (both legitimate): (1) mutable globals
  to eliminate (container, oidc, plugins, db — the original #1426 plan);
  (2) cohesion — shared dep repeated across signatures (nginx, podman —
  #1468/#1469, emerged from reviewing the settings-threading).

## How to run tests

```bash
# Always use this prefix (devenv project):
devenv --quiet -O dotenv.enable:bool false shell --

# Full backend suite (CI-matching, with coverage + xdist):
devenv --quiet -O dotenv.enable:bool false shell -- \
  bash -c 'cd src/backend && python3 -m pytest tests -n auto'

# Settings/main tests only (fast iteration, no coverage):
devenv --quiet -O dotenv.enable:bool false shell -- \
  bash -c 'cd src/backend && python3 -m pytest tests/test_settings.py tests/test_main.py -o addopts="" -q'
```

CI runs E2E suites (`test-backend-e2e`, `test-cli-e2e`, `test-frontend-e2e`)
that need a container runtime — can't run locally without podman.

## Tech debt callout: the class-var bridge

`_env_for_sources` and `_config_file_for_sources` are `ClassVar`s set on
the class in `__init__` before `super().__init__()`, read in the classmethod
`settings_customise_sources`, and cleaned up in `try/finally`. Safe
(construction is single-threaded) but not thread-safe and surprising.
A cleaner alternative: stash `env`/`config_file` on a subclassed
`init_settings` source instead of a class var — deeper pydantic-internals
work, leave as follow-up. Exists because extracting `env` from
`init_settings` inside the classmethod doesn't work (`extra="ignore"` drops
non-field init kwargs).

## Full target `build_app()` shape (end state across all slices)

```python
def build_app(settings: KlangkSettings) -> FastAPI:
    app = FastAPI(title="Klangk", lifespan=_lifespan(settings))

    # --- Slice 1 (DONE) ---
    app.state.settings = settings

    # --- Slice 2 (#1449: 2a done in #1466, 2b/2c/2d remaining) ---
    app.state.container_registry = container.ContainerRegistry(settings)
    app.state.nginx_watchdog = NginxWatchdog(settings)
    app.state.connections = ConnectionRegistry()

    # --- Slice 3 (#1450) ---
    app.state.oidc = oidc.OIDC(settings)

    # --- Slice 4 (#1451) ---
    app.state.plugins = plugins.Plugins(settings)

    # --- Slice 5 (#1452) ---
    app.state.db = DB(settings)

    # --- Standalone follow-ups (#1468/#1469) ---
    app.state.podman = podman.Podman(settings)
    # (nginx renderer folds into NginxWatchdog or becomes NginxRenderer)

    # --- routers become factories closing over instances ---
    app.include_router(api.build_router(settings, app.state.container_registry, app.state.oidc), prefix=API_PREFIX)
    app.include_router(auth.build_router(app.state.oidc), prefix=API_PREFIX)

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await wshandler.handle(websocket, app.state.settings, app.state.container_registry, app.state.oidc, app.state.connections)

    return app
```

### Lifecycle objects on `app.state` (reference)

| Attribute | Slice | Lifecycle | Notes |
|-----------|-------|-----------|-------|
| `settings` | 1 (done) | frozen at startup | `KlangkSettings(os.environ)` |
| `container_registry` | 2a (done) | process lifetime | owns monitors, port allocator, browser router |
| `nginx_watchdog` | 2b | start/stop in lifespan | `.start()` / `.stop()` methods |
| `connections` | 2c | process lifetime (register/unregister) | replaces `wshandler.state`; monitor broadcasts through it |
| `oidc` | 3 | process lifetime | owns providers + discovery/JWKS caches |
| `plugins` | 4 | process lifetime | owns declarations + values |
| `db` | 5 | process lifetime | owns engine + model methods |
| `podman` | follow-up | process lifetime | cohesion class (no mutable state) |
