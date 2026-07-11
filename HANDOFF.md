# Handoff: #1426 Composition-Root Refactor

## What this is

This is a large, multi-slice refactor to eliminate module-level mutable globals from `klangk_backend`. The tracking issue is **#1426** (read its full body for the complete design). The worktree is at:

```
/home/chrism/projects/klangk/.worktrees/refactor-1426-composition-root
```

Branch: `refactor/1426-composition-root`, based on `origin/main`.

## What's been done (commit `7040610e`)

**Slice 1, step 1: `KlangkSettings(env=...)` constructor.** Done and tested.

`KlangkSettings` now accepts an optional `env` dict parameter:

```python
KlangkSettings()              # reads os.environ (production default)
KlangkSettings(env={...})     # reads the dict only; os.environ ignored (tests)
```

Implementation: `_EnvDictSource(EnvSettingsSource)` overrides `_load_env_vars()` to run the passed dict through the same `parse_env_vars` normalizer the base uses for `os.environ`. A class-var bridge (`_env_for_sources`) shuttles the env dict from `__init__` through the classmethod boundary (`settings_customise_sources` runs before `self` exists), cleaned up after construction so it doesn't leak.

**Precedence note**: pydantic-settings gives earlier sources in the tuple higher priority. The source order is: `[env_source, yaml_config, init_kwargs]`. So env values win over init kwargs when both provide the same field. This is existing behavior, preserved by the change.

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
