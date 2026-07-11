# Handoff: #1426 Composition-Root Refactor

## What this is

A multi-slice refactor to eliminate module-level mutable globals from
`klangk_backend` in favor of one composition root: a `build_app(settings)`
factory. The tracking issue is **#1426**. Every actionable slice has its own
issue (see "Issue map" below).

**Current branch**: `issue-1447-klangksettings-env-constructor-for-injectable-env-dicts-1426`
**Current PR**: [#1455](https://github.com/mcdonc/klangk/pull/1455) (OPEN)
**Test state**: 2377 passed, 100% coverage, 0 warnings

## Issue map

| Issue | Title | State |
|-------|-------|-------|
| **#1426** | Tracker: one composition root, no module globals | umbrella |
| **#1447** | Slice 1 — `KlangkSettings(env=...)` + settings threading | in progress (#1455) |
| **#1458** | Slice 1 remainder — delete `_config_file_path` global | next |
| **#1449** | Slice 2 — ContainerRegistry owned instance | |
| **#1450** | Slice 3 — OIDC instance + caches | |
| **#1451** | Slice 4 — Plugins instance | |
| **#1452** | Slice 5 — `DB(settings)`; kill db globals | |
| **#1453** | Slice 6 — freeze `resolve_env_value` at startup | |
| **#1454** | Slice 7 — delete the `main:app` shim | |
| **#1456** | Standalone — `frontend_dir` config setting | |
| **#1457** | Standalone — tests stop monkeypatching `os.environ` | |

The next-step chain after #1455 merges: **#1458** → Slice 1 done →
**#1449** (Slice 2) → #1450 → #1451 → #1452 → #1453 → #1454.

## Slice 1: what's DONE

Two PRs land Slice 1. **#1448 is merged** (on `main`); **#1455 is open**
(this branch).

### PR #1448 (merged) — the constructor

- **`KlangkSettings(env, config_file=None)`** constructor. `env` is
  **required** (`Mapping[str, str]` — production passes `os.environ`,
  tests pass a dict). `config_file` is optional (`str | None`; `None` =
  no config file). Implementation: `_EnvDictSource(EnvSettingsSource)`
  overrides `_load_env_vars()` to run the passed mapping through the
  same `parse_env_vars` normalizer the base uses for `os.environ`.
- **Class-var bridge** (`_env_for_sources`, `_config_file_for_sources`):
  shuttles the env dict / config-file path from `__init__` through the
  classmethod boundary (`settings_customise_sources` runs before `self`
  exists). **Review fixes applied (#1448 review):**
  - Annotated as `ClassVar[...]` (not bare underscore attrs — pydantic
    absorbs those into `__private_attributes__` and they worked only by
    `cls.` vs `self.` accident).
  - Cleanup is `try/finally` around `super().__init__()` (a failed
    construction no longer leaks the env dict onto the class).
- **No init kwargs.** `**values` was dropped — init kwargs are NOT a
  supported config path (lowest-priority source, silently ignored when
  env sets the field).
- **Precedence**: env dict > config file > defaults (earlier sources in
  the pydantic-settings tuple win).

### PR #1455 (open, this branch) — build_app, cache deletion, oidc threading, validator

Four commits on top of the merged #1448:

1. **`oidc.auth_modes(settings)` + predicate threading.**
   `auth_modes()`, `password_login_allowed()`, `local_login_allowed()`,
   `oidc_login_allowed()` now take a `KlangkSettings` arg and read
   `settings.auth_modes` instead of `resolve_env_value("KLANGK_AUTH_MODES")`
   at call time. Production callers obtain settings via `get_settings()`
   (transitional — `get_settings_dep` arrives in commit 2). **This is a
   stepping stone for Slice 3 (#1450)**, where the `settings` param
   becomes `self.settings` on an `OIDC(settings)` class.

2. **`build_app(settings)` composition root + `get_settings_dep` + cache
   machinery deletion.**
   - `build_app()` wraps app construction (FastAPI, CORS, routers,
     exception handlers, WS endpoint, static files) into one factory.
     Sets `app.state.settings = settings`.
   - `get_settings_dep(request)` → `request.app.state.settings` — the
     per-request bridge (zero callers yet; endpoints migrate to it
     incrementally or via subsystem Depends in later slices).
   - Module-level `app = build_app(get_settings())` remains as a shim
     for uvicorn's string import.
   - **Cache machinery deleted**: `_settings_instance`,
     `_settings_env_signature`, `_env_signature()`, `_invalidate_cache()`
     all gone. `get_settings()` is cache-free (constructs a fresh
     `KlangkSettings(os.environ, config_file=_config_file_path)` on
     every call).

3. **`auth_modes` field validator (security fix).**
   A typo'd `KLANGK_AUTH_MODES` (e.g. `passdword`) used to fall through
   to `"none"` — a **silent security downgrade** (`none` freely issues
   an admin token). A pydantic `field_validator` now rejects non-`None`,
   non-empty values outside `{password, oidc, both, none}` at
   construction (`validate_at_startup()` in the lifespan → aborts boot
   before serving traffic). Empty string is treated as unset (`None`),
   preserving the blank-value behavior. `oidc.auth_modes()` simplified
   to `settings.auth_modes` + `None → "none"` fallback (the set check is
   redundant now that the validator guarantees validity).

4. **E2E test fix.** `test_nginx_acl_e2e.py` imported `_invalidate_cache`
   (deleted in commit 2). Removed all 10 lines (5 imports + 5 calls).
   Also fixed invalid `password,oidc` test data in `test_nginx.py`
   (never a valid mode — the new validator catches it) → `both`.

## Slice 1: what REMAINS

Only one item — **#1458** (filed as its own issue):

- **Delete the `_config_file_path` global.** `set_config_file()` /
  `get_config_file()` / `_config_file_path` are still present as a
  transitional fallback. `get_settings()` passes
  `config_file=_config_file_path` explicitly so the coupling is visible.
  Migrate `klangkd` (the one production caller, `klangkd.py:105`) and
  ~15 test callers to pass `config_file=` to the constructor, then
  delete the global + accessors.

After #1458 merges, **#1447 (Slice 1) closes** and **#1449 (Slice 2)**
starts.

## Design decisions (locked in)

- **`env` is required on `KlangkSettings.__init__`** — forces explicit
  config source. `os.environ` is never read unless explicitly passed.
- **`config_file` defaults to `None`** — `None` means "no config file"
  (legitimate common case for tests/scripts). `"none"` is the explicit
  opt-out string.
- **Init kwargs NOT supported** — dropped from signature, not mentioned
  in docstrings (except `config_file`).
- **Precedence**: env dict > config file > defaults.
- **`DB(settings)` not `settings.get_db()`** — settings stays a pure
  config value; engine/PRAGMA/cache concerns live on `DB`.
- **No ContextVar** — Pyramid discipline; all threaded explicitly.
- **`get_settings()` is a cache-free shim** — stays until Slice 7
  (#1454) when its last caller (the `main:app` shim) is gone.
- **`get_settings_dep` is NOT for every endpoint** — its real audience
  is endpoints reading config fields directly. Endpoints delegating to
  subsystem objects (oidc/container/plugins) use per-subsystem Depends
  once those become instances (Slices 2-4).

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

# Full CLI suite (CI-matching):
devenv --quiet -O dotenv.enable:bool false shell -- \
  bash -c 'cd src/cli && python3 -m pytest tests -n auto'
```

CI runs E2E suites (`test-backend-e2e`, `test-cli-e2e`,
`test-frontend-e2e`) that need a container runtime — those can't run
locally without podman. The nginx ACL E2E tests were the catch for the
`_invalidate_cache` import breakage in #1455 (fixed in commit 4).

## Key files and their roles

| File | Role |
|------|------|
| `src/backend/klangk_backend/settings.py` | `KlangkSettings` model + constructor (`_EnvDictSource`, class-var bridges, `auth_modes` validator), `get_settings()` (cache-free shim), `validate_at_startup()`, `_config_file_path` global (dies in #1458), `resolve_env_value/bool` (die in Slice 6 / #1453) |
| `src/backend/klangk_backend/main.py` | `build_app(settings)` composition root, `get_settings_dep`, `lifespan`, `setup_logfire`, `cors_origins`, nginx watchdog globals, `app = build_app(get_settings())` shim (dies in Slice 7 / #1454) |
| `src/backend/klangk_backend/oidc.py` | `auth_modes(settings)`, `*_login_allowed(settings)` — take settings arg (become methods in Slice 3 / #1450); `_providers` / `_discovery_cache` / `_jwks_cache` globals (move onto OIDC instance in Slice 3) |
| `src/backend/klangk_backend/api/auth.py` | Auth endpoints calling `oidc.password_login_allowed(get_settings())` / `oidc.local_login_allowed(get_settings())` |
| `src/backend/klangk_backend/api/__init__.py` | `/config` endpoint calling `oidc.auth_modes(get_settings())`; module-scope `resolve_env_value` constants (migrate to settings in Slice 6) |
| `src/backend/klangk_backend/klangkd.py` | Launcher; calls `set_config_file(resolved)` (dies in #1458), `validate_at_startup()`, `uvicorn.run("klangk_backend.main:app")` |

## Tech debt callout: the class-var bridge

`_env_for_sources` and `_config_file_for_sources` are `ClassVar`s set on
the class in `__init__` before `super().__init__()`, read in the
classmethod `settings_customise_sources`, and cleaned up in a `try/finally`.
This is safe (construction is single-threaded at startup and one-at-a-time
in tests) but not thread-safe and surprising.

A cleaner alternative (not yet verified): stash `env`/`config_file` on a
subclassed `init_settings` source (the one `settings_customise_sources`
argument tied to *this* construction) instead of a class var. That's deeper
pydantic-internals work; leave as a follow-up cleanup. Do **not** treat the
class-var bridge as permanent — it exists because extracting `env` from
`init_settings` inside the classmethod doesn't work (`extra="ignore"` drops
non-field init kwargs).

## Full target `build_app()` shape (the end state across all slices)

Every `app.state` attribute created across the refactor, in slice order.
Each slice creates its subset and leaves the rest for later slices.

```python
def build_app(settings: KlangkSettings) -> FastAPI:
    app = FastAPI(title="Klangk", lifespan=_lifespan(settings))

    # --- Slice 1 (DONE) ---
    app.state.settings = settings

    # --- Slice 2 (#1449) ---
    app.state.container_registry = container.ContainerRegistry(settings)
    #   - IdleMonitor / HealthMonitor / BrowserRouter / PortAllocator become
    #     collaborators of the registry (nested as `.idle`, `.health`, etc.)
    #   - terminal._service_session_locks moves onto the registry
    #   - auto_start_workspaces takes (container_registry, settings) as args
    app.state.nginx_watchdog = NginxWatchdog(settings)
    app.state.connections = ConnectionRegistry()
    #   ^ new lifecycle object: replaces the `wshandler.state` module global.
    #     The WS layer registers connections into it; HealthMonitor broadcasts
    #     health/death frames through it (today this is a lazy `from .wshandler
    #     import state as _ws_state`). It's the one place where a background
    #     actor (monitor, owned by the registry) needs a handle to the set of
    #     live WS connections — different lifecycle than either, so it gets its
    #     own owner on app.state.

    # --- Slice 3 (#1450) ---
    app.state.oidc = oidc.OIDC(settings)
    #   - _providers / _discovery_cache / _jwks_cache move onto the instance
    #   - auth_modes(settings) / *_login_allowed(settings) become methods
    #     (self.settings replaces the settings arg threaded in Slice 1)

    # --- Slice 4 (#1451) ---
    app.state.plugins = plugins.Plugins(settings)
    #   - _declarations / _values move onto the instance

    # --- Slice 5 (#1452) ---
    app.state.db = DB(settings)
    #   - kills data_dir / DB_PATH / engine / ensure_engine / get_db() globals
    #   - model methods take settings (or live on DB; see #1452 scope fence)

    # --- routers become factories closing over instances (not module globals) ---
    app.include_router(api.build_router(settings, app.state.container_registry, app.state.oidc), prefix=API_PREFIX)
    app.include_router(auth.build_router(app.state.oidc), prefix=API_PREFIX)

    # --- WS handler takes instances as explicit args ---
    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await wshandler.handle(websocket, app.state.settings, app.state.container_registry, app.state.oidc, app.state.connections)
    # settings: needed for KLANGKC_DEBUG_SSH_AGENT / KLANGKWS_DEBUG /
    #   KLANGK_BRIDGE_TIMEOUT_SECONDS (read today via resolve_env_value;
    #   migrate to settings in Slice 6 / #1453). Passed from the start so the
    #   WS handler signature doesn't change twice.

    return app
```

### Lifecycle objects on `app.state` (reference)

| Attribute | Slice | Lifecycle | Notes |
|-----------|-------|-----------|-------|
| `settings` | 1 (done) | frozen at startup | `KlangkSettings(os.environ, config_file=...)` |
| `container_registry` | 2 | process lifetime | owns monitors, port allocator, browser router |
| `nginx_watchdog` | 2 | start/stop in lifespan | `.start()` / `.stop()` methods |
| `connections` | 2 | process lifetime (connections register/unregister) | replaces `wshandler.state` global; monitor broadcasts through it |
| `oidc` | 3 | process lifetime | owns providers + discovery/JWKS caches |
| `plugins` | 4 | process lifetime | owns declarations + values |
| `db` | 5 | process lifetime | owns engine + model methods |

### The `connections` lifecycle (new — the one genuinely new owner)

Today `wshandler.state` is a module global holding live WS connections.
`HealthMonitor._send_heartbeats` reaches into it via a lazy import to
broadcast health/death frames. This is the one place where a background
actor (the monitor, owned by the registry) needs to talk to all live
connections — connections come and go on a different lifecycle than the
monitor, so neither can own the other.

Slice 2 promotes `wshandler.state` to a `ConnectionRegistry` instance on
`app.state.connections`. The WS layer registers/unregisters connections
into it; the monitor (and anything else that needs to broadcast) gets a
handle to it. This is the single place in the refactor where "thread it as
a param" requires introducing a **new owner** rather than reusing an
existing one — call it out as a micro-slice within Slice 2.
