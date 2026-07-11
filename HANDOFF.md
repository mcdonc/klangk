# Handoff: #1426 Composition-Root Refactor

## What this is

This is a large, multi-slice refactor to eliminate module-level mutable globals from `klangk_backend`. The tracking issue is **#1426** (read its full body for the complete design).

Branch: `refactor/1426-composition-root`, based on `origin/main`.

## What's been done (commit `7040610e`)

**Slice 1, step 1: `KlangkSettings(env=...)` constructor.** Done and tested.

`KlangkSettings` now accepts an optional `env` dict parameter:

```python
KlangkSettings()              # reads os.environ (production default)
KlangkSettings(env={...})     # reads the dict only; os.environ ignored (tests)
```

Implementation: `_EnvDictSource(EnvSettingsSource)` overrides `_load_env_vars()` to run the passed dict through the same `parse_env_vars` normalizer the base uses for `os.environ`. A class-var bridge (`_env_for_sources`) shuttles the env dict from `__init__` through the classmethod boundary (`settings_customise_sources` runs before `self` exists), cleaned up after construction so it doesn't leak.

**Known tech debt — the class-var bridge (`_env_for_sources`, `_config_file_for_sources`).** This is nasty but works: `__init__` sets a class var, `super().__init__()` reads it in the classmethod, then `__init__` resets it to `None`. It's safe because construction is single-threaded (startup + tests construct one at a time), but it's not thread-safe and it's surprising. A cleaner alternative to investigate: stash `env`/`config_file` on a subclassed `init_settings` source (the one `settings_customise_sources` argument tied to *this* construction) instead of a class var. That's deeper pydantic-internals work and wasn't verified under time pressure; leave as a follow-up cleanup. Do **not** treat the class-var bridge as permanent.

**Precedence note**: env > config file > defaults. (pydantic-settings gives earlier sources in the tuple higher priority. Init kwargs / field overrides are not a supported configuration path — don't use them.)

**Tests**: 57 settings tests pass (6 new `TestEnvConstructor` tests). Full suite: 2354 pass, 1 pre-existing flaky WS test fails under `-n auto` (passes in isolation), 0 warnings from new tests.

## What remains for Slice 1

The rest of Slice 1 (see #1426 for acceptance criteria):

1. **`build_app(settings)` factory.** Wrap the module-level `app = FastAPI()` + `add_middleware` + `include_router` in `main.py` into a `build_app()` function. Keep `main:app` as a shim (`app = build_app(get_settings())`) so uvicorn's string import still works. The factory takes `settings: KlangkSettings` and stores it on `app.state.settings`.

2. **`get_settings_dep` FastAPI dependency.** Add a per-request dependency that reads `request.app.state.settings`:

   ```python
   def get_settings_dep(request: Request) -> KlangkSettings:
       return request.app.state.settings
   ```

3. **Thread `settings` into `oidc.auth_modes()` and the `*_login_allowed` predicates.** These currently call `get_settings()` internally. Change them to take a `settings: KlangkSettings` arg. Update all callers (the FastAPI auth dependencies in `api/auth.py`, the `/config` endpoint in `api/__init__.py`, and startup callers).

4. **Drop the cache machinery.** Delete `_settings_instance`, `_settings_env_signature`, `_env_signature()`, `_invalidate_cache()`, and the env-change detection in `get_settings()`. `get_settings()` either goes away entirely or becomes a test-only constructor.

5. **Migrate tests.** Tests that use `monkeypatch.setenv` + `_invalidate_cache()` should migrate to `KlangkSettings(env={...})` where practical. Some tests may keep monkeypatch if they test code paths that still call `resolve_env_value` (those retire in Slice 6).

6. **Update `validate_at_startup()`** to construct `KlangkSettings(os.environ)` instead of calling `get_settings()`.

## How to run tests

```bash
# Always use this prefix (devenv project):
devenv --quiet -O dotenv.enable:bool false shell --

# Settings tests only (fast iteration):
devenv --quiet -O dotenv.enable:bool false shell -- bash -c 'cd src/backend && python3 -m pytest tests/test_settings.py -o addopts="" -q'

# Full backend suite (CI-matching, with coverage):
devenv --quiet -O dotenv.enable:bool false shell -- bash -c 'cd src/backend && python3 -m pytest tests -n auto'

# Quick run without coverage (for fast iteration):
devenv --quiet -O dotenv.enable:bool false shell -- bash -c 'cd src/backend && python3 -m pytest tests/test_settings.py tests/test_main.py -o addopts="" -q'
```

## Key files and their roles

| File                                         | Role                                                                                                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/backend/klangk_backend/settings.py`     | `KlangkSettings` model, `get_settings()` singleton (to be removed), `validate_at_startup()`, `resolve_env_value/bool` (Slice 6)                       |
| `src/backend/klangk_backend/main.py`         | Lifespan, `app = FastAPI()`, `setup_logfire`, `cors_origins`, nginx watchdog. The `build_app()` factory goes here.                                    |
| `src/backend/klangk_backend/oidc.py`         | `auth_modes()`, `password_login_allowed()`, `oidc_login_allowed()`, `local_login_allowed()` — the per-request predicates that need settings threading |
| `src/backend/klangk_backend/api/auth.py`     | Auth dependencies (`require_auth`, etc.) that call `oidc.auth_modes()`                                                                                |
| `src/backend/klangk_backend/api/__init__.py` | The `/config` endpoint that reads `oidc.auth_modes()`                                                                                                 |
| `src/backend/klangk_backend/klangkd.py`      | The launcher entry point; calls `validate_at_startup()` and `uvicorn.run("klangk_backend.main:app")`                                                  |

## Design rules (from #1426)

- **No implicit lookups.** All threaded explicitly as constructor/param args — no `ContextVar`, no module global, no `app.state`-as-registry. The lifespan closes over the instances it constructs and passes them into the background actors and WS/router factories.
- **ASGI app is the only global.** Everything else is an instance owned by `app.state` or an explicit constructor arg.
- **`app.state.x = ...` only inside `build_app()` or the lifespan.**
- **`KlangkSettings(os.environ)`** in production; `KlangkSettings(env={...})` in tests.
- **100% coverage maintained** at every commit.
- **Warnings in tests you touched must be squashed** before pushing.

## Remaining slices (beyond Slice 1)

See #1426 for the full design. Summary:

- **Slice 2**: promote `ContainerRegistry` to `app.state.container_registry`; move `_service_session_locks`; make `IdleMonitor`/`HealthMonitor`/`BrowserRouter`/`PortAllocator` take `settings` in constructors; `auto_start_workspaces` becomes a `ContainerRegistry` method; `NginxWatchdog` → `app.state.nginx_watchdog`; `wshandler.state` → `ConnectionRegistry`.
- **Slice 3**: `OIDC` instance + caches → `app.state.oidc`.
- **Slice 4**: `Plugins` instance → `app.state.plugins`.
- **Slice 5**: `DB(settings)` — kill `data_dir`/`DB_PATH`/`get_db()` globals in `model/db.py`.
- **Slice 6**: freeze `resolve_env_value` at startup (132 call sites).
- **Slice 7**: delete the `main:app` shim.

## Full target `build_app()` shape (the end state across all slices)

Every `app.state` attribute created across the refactor, in slice order. The
HANDOFF model should understand this is the target — each slice creates its
subset and leaves the rest for later slices.

```python
def build_app(settings: KlangkSettings) -> FastAPI:
    app = FastAPI(title="Klangk", lifespan=_lifespan(settings))

    # --- Slice 1 ---
    app.state.settings = settings

    # --- Slice 2 ---
    app.state.container_registry = container.ContainerRegistry(settings)
    #   - IdleMonitor / HealthMonitor / BrowserRouter / PortAllocator become
    #     collaborators of the registry (nested as `.idle`, `.health`, etc.)
    #   - terminal._service_session_locks moves onto the registry
    #   - auto_start_workspaces becomes a ContainerRegistry method
    app.state.nginx_watchdog = NginxWatchdog(settings)
    app.state.connections = ConnectionRegistry()
    #   ^ new lifecycle object: replaces the `wshandler.state` module global.
    #     The WS layer registers connections into it; HealthMonitor broadcasts
    #     health/death frames through it (today this is a lazy `from .wshandler
    #     import state as _ws_state`). It's the one place where a background
    #     actor (monitor, owned by the registry) needs a handle to the set of
    #     live WS connections — different lifecycle than either, so it gets its
    #     own owner on app.state.

    # --- Slice 3 ---
    app.state.oidc = oidc.OIDC(settings)
    #   - _providers / _discovery_cache / _jwks_cache move onto the instance

    # --- Slice 4 ---
    app.state.plugins = plugins.Plugins(settings)
    #   - _declarations / _values move onto the instance

    # --- Slice 5 ---
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
    #   migrate to settings in Slice 6). Passed from the start so the WS
    #   handler signature doesn't change twice.

    return app
```

### Lifecycle objects on `app.state` (reference)

| Attribute | Slice | Lifecycle | Notes |
|-----------|-------|-----------|-------|
| `settings` | 1 | frozen at startup | `KlangkSettings(os.environ, config_file=...)` |
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
