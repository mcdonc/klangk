# Creating Plugins

All plugins live in `$KLANGK_PLUGINS_DIR/<name>/` directories (defaults to `.devenv/state/klangk/plugins/`). A plugin can contain any combination of:

- `extension.ts` — Pi extension with `pi.registerTool()`. Symlinked as `<plugin-name>.ts` into the workspace image at build time.
- `skills/` — Pi skill directories (each containing a `SKILL.md`). Symlinked as `<plugin-name>-<skill>/` into the image.
- `prompts/` — Pi prompt templates (`.md` files). Symlinked as `<plugin-name>-<file>.md` into the image.
- `klangk/` — Dart package for client-side browser actions:
  - `klangk/pubspec.yaml` — Package definition, depends on `klangk_plugin_api` (git)
  - `klangk/lib/plugin.dart` — Class extending `ToolPlugin` with action handlers
  - `klangk/lib/*.dart` — Supporting Dart files (widgets, utilities)
- `tools/` — Server-side scripts and commands. Available at `/opt/klangk/plugins/<name>/tools/` in the container. Plugins that need a tool on `PATH` should symlink it in their `on-image-build.sh` hook.
- `on-image-build.sh` — Lifecycle hook: runs at image build time (see [Lifecycle Hooks](#lifecycle-hooks))
- `on-entrypoint.sh` — Lifecycle hook: runs at container start
- `on-shell-init.sh` — Lifecycle hook: runs on every shell open

No single component is required — a plugin can be an extension + Dart UI, just lifecycle hooks + tools, or any combination. The `klangk/` subdirectory is only needed for client-side browser actions (e.g., celebrate, beep, authenticated fetch) that are dispatched via the [browser bridge](../architecture/browser-bridge.md).

## Build Integration

- `scripts/import_dart_plugins.py` scans `$KLANGK_PLUGINS_DIR/*/klangk/` for plugin Dart packages and generates `$KLANGK_PLUGINS_DIR/.dart/` (the `klangk_plugins` package with path deps and `createAllPlugins()`)
- `build-workspace-image` copies entire plugin directories into `$KLANGK_PLUGINS_DIR/.docker/plugins/` and passes them as a single `plugins` build context. The Dockerfile copies them to `/opt/klangk/plugins/` and symlinks discoverable pieces (extensions, skills, prompts, tools) into central directories.
- `flutterbuildweb` runs the codegen before compiling
- `stub_dart_plugins.sh` creates a minimal stub at `$KLANGK_PLUGINS_DIR/.dart/` so `flutter pub get` works before plugins are fetched (runs automatically as part of the `klangk:update-plugins` task; skips if `pubspec_overrides.yaml` already exists)
- Plugins are fetched automatically on `devenv up`: `klangk:init-plugins` creates `plugins.yaml` on first run, then `klangk:update-plugins` fetches plugins when `plugins.yaml` changes

## Adding a Plugin

The easiest way to develop a plugin locally is to add a `path` entry
to `plugins.yaml` pointing at your plugin directory:

```yaml
plugins:
  - name: my-plugin
    path: /home/user/projects/my-plugin
```

This creates a symlink in `$KLANGK_PLUGINS_DIR` pointing at your
local directory, so edits are reflected immediately without
re-fetching. Paths support `~`, `$ENV_VARS`, and relative paths
(resolved from the `plugins.yaml` directory). Run `update-plugins` or
restart `devenv up` to apply.

Alternatively, create files directly in `$KLANGK_PLUGINS_DIR`:

1. Create `$KLANGK_PLUGINS_DIR/<name>/extension.ts` with `pi.registerTool()`
2. For client-side browser actions, add `klangk/pubspec.yaml` (depends on `klangk_plugin_api`) and `klangk/lib/plugin.dart` extending `ToolPlugin`
3. For server-side scripts, add files in `$KLANGK_PLUGINS_DIR/<name>/tools/`
4. `devenv up` rebuilds automatically when `$KLANGK_PLUGINS_DIR` changes

For remote plugins, add an entry with a `git` key to `$KLANGK_PLUGINS_DIR/plugins.yaml` and run `update-plugins` to fetch it.

## Lifecycle Hooks

Plugins can include shell scripts at their root that run automatically at specific points in the container lifecycle. All hooks are optional.

### `on-image-build.sh`

Runs once at **image build time** via `RUN` in the Dockerfile. Use for system-level configuration that applies to all users and persists in the image.

- Runs as root
- No runtime environment variables available (only build-time values)
- Examples: `git config --system`, installing system packages, writing config files

```bash
#!/usr/bin/env bash
# plugins/git-credential/on-image-build.sh
set -e
git config --system credential.helper klangk
```

### `on-entrypoint.sh`

Runs once per **container start** from the entrypoint, before any shell opens. Use for setup that depends on runtime environment variables but only needs to happen once.

- Runs as the container's initial user (root inside the user namespace, mapped to the host user outside)
- Runtime environment variables are available (`KLANGK_WORKSPACE_ID`, etc.)
- Examples: writing runtime config files, one-time service initialization

### `on-shell-init.sh`

Runs on **every shell open** from `bash.bashrc`. Use for per-user, per-session setup.

- Runs as the `klangk` user
- User environment is available (`HOME`, `KLANGK_USER_ID`, etc.)
- Runs after `klangk-setup-clankers` (Pi agent config)
- Keep it fast — this runs on every new terminal tab and window
- Examples: per-user symlinks, session-specific env setup

### Execution Order

Hooks execute **alphabetically by plugin name**. Within each plugin, only the relevant hook for the current lifecycle phase runs. If ordering between plugins matters, use numeric prefixes on plugin directory names (e.g., `00-core`, `50-git-credential`).

### Container Layout

The entire plugin directory is available in the container. Hooks (and any support scripts or assets they need) can be referenced via their plugin directory:

```text
/opt/klangk/plugins/
  git-credential/
    on-image-build.sh
    helper-script.sh        # accessible to hooks via $SCRIPT_DIR
  some-other-plugin/
    extension.ts            # symlinked to /opt/klangk/pi-agent/extensions/some-other-plugin.ts
    skills/
      my-skill/
        SKILL.md            # symlinked to /opt/klangk/pi-agent/skills/some-other-plugin-my-skill/
    prompts/
      setup.md              # symlinked to /opt/klangk/pi-agent/prompts/some-other-plugin-setup.md
    on-entrypoint.sh
    on-shell-init.sh
```

## Plugin Configuration

Plugins can declare configuration settings (environment variables) in their `package.json`. The system reads these declarations at build time and resolves values from the server environment at runtime.

### Declaring Config Keys

Add a `klangk.config` section to your plugin's `package.json`:

```json
{
  "name": "@klangk/my-plugin",
  "klangk": {
    "config": {
      "MY_PLUGIN_URL": {
        "description": "URL for the my-plugin backend",
        "default": "http://localhost:8080",
        "scope": "frontend"
      },
      "MY_PLUGIN_API_KEY": {
        "description": "API key for my-plugin",
        "default": "",
        "scope": "container"
      }
    }
  }
}
```

Each key in the `config` object is an environment variable name. Fields:

| Field         | Required | Description                                                |
| ------------- | -------- | ---------------------------------------------------------- |
| `description` | No       | Human-readable description of the setting                  |
| `default`     | No       | Default value if the env var is not set (defaults to `""`) |
| `scope`       | No       | Where the value is delivered (defaults to `"container"`)   |

### Scopes

The `scope` field controls where the resolved value is made available:

- **`container`** — Injected as an environment variable into workspace containers at startup. Available to Pi extensions via `process.env.VAR_NAME` and to any process running in the container.
- **`frontend`** — Included in the `GET /api/config` response as a lowercased key (e.g., `MY_PLUGIN_URL` → `my_plugin_url`). Available to Dart plugins in the browser.
- **`both`** — Delivered to both containers and the frontend.

### Setting Values

Values come from the server environment — admins set them in `.env` or as system environment variables, the same as all other Klangk configuration:

```bash
# .env
MY_PLUGIN_URL=https://my-plugin.example.com
MY_PLUGIN_API_KEY=sk-abc123
```

If an environment variable is not set, the `default` from the plugin manifest is used.

### How It Works

1. **Startup**: The backend scans `$KLANGK_PLUGINS_DIR/*/package.json` for `klangk.config` entries and resolves each declared key from the server environment (with fallback to declared defaults).
2. **Container creation**: Keys with `scope: "container"` or `"both"` are injected as env vars into workspace containers alongside system env vars like `KLANGK_BRIDGE_URL`.
3. **Frontend requests**: Keys with `scope: "frontend"` or `"both"` are included in the `GET /api/config` response. Dart plugins can fetch this endpoint to discover their configuration.

### Example: Accessing Config in a Dart Plugin

```dart
import 'package:http/http.dart' as http;
import 'dart:convert';

// In your plugin's initialization:
final resp = await http.get(Uri.parse('$baseUrl/api/config'));
final config = jsonDecode(resp.body) as Map<String, dynamic>;
final myUrl = config['my_plugin_url'] as String? ?? '';
```

### Example: Accessing Config in a Pi Extension

```typescript
// Pi extensions run in the container — values are env vars
const MY_URL = process.env.MY_PLUGIN_URL;
const API_KEY = process.env.MY_PLUGIN_API_KEY;
```
