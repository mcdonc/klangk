# Plugin System

All plugins live in `$KLANGK_PLUGINS_DIR/<name>/` directories (defaults to `.devenv/state/klangk/plugins/`). A plugin can contain:

- `extension.ts` — Pi extension with `pi.registerTool()`. Copied to `src/containers/extensions/` at build time.
- `klangk/` — Optional Dart package for client-side browser actions:
  - `klangk/pubspec.yaml` — Package definition, depends on `klangk_plugin_api` (git)
  - `klangk/lib/plugin.dart` — Class extending `ToolPlugin` with action handlers
  - `klangk/lib/*.dart` — Supporting Dart files (widgets, utilities)
- `tools/` — Server-side scripts. Everything in this subdirectory is copied to `/opt/klangk/plugin-tools/<name>/` in the workspace image.

A plugin needs at minimum an `extension.ts`. The `klangk/` subdirectory is only needed for client-side browser actions (e.g., celebrate, beep, authenticated fetch) that are dispatched via the [browser bridge](../architecture/browser-bridge.md).

## Build Integration

- `scripts/import_dart_plugins.py` scans `$KLANGK_PLUGINS_DIR/*/klangk/` for plugin Dart packages and generates `$KLANGK_PLUGINS_DIR/.dart/` (the `klangk_plugins` package with path deps and `createAllPlugins()`)
- `build-workspace-image` stages `extension.ts` and `tools/` files from all plugins into `$KLANGK_PLUGINS_DIR/.docker/` and passes them via named build contexts (`plugin-extensions`, `plugin-tools`)
- `flutterbuildweb` runs the codegen before compiling
- `stub_dart_plugins.sh` creates a minimal stub at `$KLANGK_PLUGINS_DIR/.dart/` so `flutter pub get` works before plugins are fetched (runs automatically at devenv shell startup via `enterShell`; skips if `pubspec_overrides.yaml` already exists)
- Both build tasks are triggered automatically by `devenv up` via `execIfModified`

## Adding a Plugin

For local development, create files directly in `$KLANGK_PLUGINS_DIR`:

1. Create `$KLANGK_PLUGINS_DIR/<name>/extension.ts` with `pi.registerTool()`
2. For client-side browser actions, add `klangk/pubspec.yaml` (depends on `klangk_plugin_api`) and `klangk/lib/plugin.dart` extending `ToolPlugin`
3. For server-side scripts, add files in `$KLANGK_PLUGINS_DIR/<name>/tools/`
4. `devenv up` rebuilds automatically when `$KLANGK_PLUGINS_DIR` changes

For remote plugins, add an entry to `$KLANGK_PLUGINS_DIR/plugins.yaml` and run `update-plugins` to fetch it.

## Plugin Management

Run `update-plugins` to fetch plugins. On first run it creates a `plugins.yaml` template with the default plugins. Plugins are declared in `$KLANGK_PLUGINS_DIR/plugins.yaml`. Each entry requires `name` and `git`; `path` and `ref` are optional:

```yaml
plugins:
  - name: celebrate
    git: git@github.com:mcdonc/klangk.git
    path: plugins/celebrate
    ref: main
  - name: beep
    git: git@github.com:mcdonc/klangk.git
    path: plugins/beep
    ref: main
```

- `update-plugins` — fetches all plugins listed in `plugins.yaml`, resolves git refs to commit SHAs, writes `plugins.lock`
- `update-plugins <name>` — fetch/update a single plugin by name
- `plugins.lock` — records resolved commit SHAs for reproducible builds
- Local plugin development: drop a directory into `$KLANGK_PLUGINS_DIR` directly — the build system treats it the same as a fetched plugin
- `execIfModified` watches `$KLANGK_PLUGINS_DIR` to trigger rebuilds when plugin content or the lockfile changes
