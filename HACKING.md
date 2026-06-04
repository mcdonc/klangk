# Hacking on Klangk

## Prerequisites

- Docker daemon running
- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (or run `./bootstrap`)

## Getting Started

```bash
git clone git@github.com:mcdonc/klangk.git
cd klangk

# Create .env from the example
cp -n .env.example .env

# Edit .env with your credentials
cat > .env << 'EOF'
KLANGK_LLM_API_KEY=your-api-key-here
# Any OpenAI-compatible provider (or http://localhost:11434/v1 for self-hosted)
KLANGK_LLM_BASE_URL=https://ollama.com/v1
# Any model available on your provider
KLANGK_LLM_MODEL=gemma4:31b
KLANGK_JWT_SECRET=change-this-to-a-random-secret
KLANGK_DEFAULT_USER=admin@example.com
# Omit to generate a random password on first run
# KLANGK_DEFAULT_PASSWORD=admin
EOF

# Install Nix and devenv (if not already installed)
./bootstrap
```

## Entering the Dev Shell

All commands below assume you're inside the devenv shell:

```bash
devenv shell
```

This puts all project tools (Python, Flutter, Dart, Node, Docker CLI, etc.) on your PATH.

## Starting the Dev Environment

```bash
devenv processes up --no-tui
```

This builds the Docker image and Flutter web app on first run (via `execIfModified`), starts nginx, the FastAPI backend, and watches for file changes. Open http://localhost:8995.

## Running Tests

```bash
# Backend unit tests (Python, pytest, parallel)
test-backend

# Frontend unit tests (Dart, flutter test, 100% coverage required)
test-frontend

# CLI E2E tests (starts real server + Docker containers)
test-cli-e2e

# Flutter E2E tests (Playwright, needs flutter build + docker build)
test-frontend-e2e

# Run a specific E2E test
cd src/frontend/e2e-tests && npx playwright test --project=chromium --no-deps --grep "test name"
```

Backend tests for Python changes, frontend tests for Dart changes. Run E2E tests before committing cross-cutting changes.

## Environment Variables

`$DEVENV_STATE` refers to `<project root>/.devenv/state` — this is where devenv stores runtime data.

All settings can be overridden in `.env`. Defaults (where appropriate) are provided in `devenv.nix` at low priority so `.env` values take precedence.

**`file:` prefix:** Any env var can be prefixed with `file:` to read the value from a file at runtime (e.g. `KLANGK_JWT_SECRET=file:/run/secrets/jwt`). The file contents are stripped of leading/trailing whitespace. This works with secret management tools like agenix/sops that write decrypted secrets to files. If the file cannot be read, an error is logged and the value is treated as unset.

| Variable                        | Default                              | Description                                                                                                                                                 |
| ------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KLANGK_NGINX_PORT`             | `8995`                               | **Primary access point** — nginx reverse proxy port (UI, API, WebSocket, hosted apps)                                                                       |
| `KLANGK_PORT`                   | `8997`                               | Backend (FastAPI/uvicorn) port — proxied through nginx, not accessed directly                                                                               |
| `KLANGK_DATA_DIR`               | `$DEVENV_STATE/klangk/data`          | Database, workspaces, Pi sessions                                                                                                                           |
| `KLANGK_PLUGINS_DIR`            | `$DEVENV_STATE/klangk/plugins`       | Fetched plugins (outside repo for `execIfModified`)                                                                                                         |
| `KLANGK_IMAGE_NAME`             | `klangk`                             | Docker image name for workspace containers                                                                                                                  |
| `KLANGK_INSTANCE_ID`            | `default`                            | Instance identifier for multi-instance deployments on the same host — isolates containers, names, and cleanup                                               |
| `KLANGK_DNS_SERVERS`            |                                      | Comma-separated DNS server IPs for containers (e.g., `100.100.100.100,8.8.8.8` for Tailscale MagicDNS). If unset, containers use Docker's default DNS.      |
| `KLANGK_HOSTING_HOSTNAME`       | (auto-derived)                       | Hostname for hosted app URLs. Behind a reverse proxy: uses `X-Forwarded-Host` as-is. Direct access: uses `Host` header with `KLANGK_NGINX_PORT` substituted |
| `KLANGK_HOSTING_PROTO`          | (from `X-Forwarded-Proto` or `http`) | Protocol for user-facing app URLs. Auto-derived from request headers if not set                                                                             |
| `KLANGK_HOSTING_BASE_PATH`      | (from `X-Forwarded-Prefix` or empty) | Base path prefix for user-facing app URLs (e.g., `/klangk`). Auto-derived from nginx `X-Forwarded-Prefix` header if not set                                 |
| `KLANGK_IDLE_TIMEOUT_SECONDS`   | `1800`                               | Container idle timeout in seconds (check interval auto-computed as timeout/3, clamped 10–60s)                                                               |
| `KLANGK_LOGIN_LOCKOUT_WINDOW`   | `300`                                | Time window in seconds for counting failed login attempts.                                                                                                  |
| `KLANGK_LOGIN_LOCKOUT_FAILURES` | `0`                                  | Number of failed login attempts before a lockout. Default `0` (disabled).                                                                                   |
| `KLANGK_LOGIN_LOCKOUT_DURATION` | `900`                                | Duration of lockout in seconds (only relevant when `KLANGK_LOGIN_LOCKOUT_FAILURES` > 0).                                                                    |
| `KLANGK_LLM_API_KEY`            |                                      | LLM provider API key                                                                                                                                        |
| `KLANGK_LLM_BASE_URL`           |                                      | LLM API URL (any OpenAI-compatible provider)                                                                                                                |
| `KLANGK_LLM_MODEL`              |                                      | LLM model name                                                                                                                                              |
| `KLANGK_JWT_SECRET`             |                                      | JWT signing secret                                                                                                                                          |
| `KLANGK_DEFAULT_USER`           |                                      | Auto-seeded admin email on startup                                                                                                                          |
| `KLANGK_DEFAULT_PASSWORD`       |                                      | Auto-seeded password on startup (omit to generate random; supports `file:` prefix)                                                                          |
| `KLANGK_MIN_PASSWORD_LENGTH`    | `4`                                  | Minimum password length                                                                                                                                     |
| `KLANGK_DISABLE_REGISTRATION`   |                                      | Set to any non-empty value to block new user signups and hide the registration link in the UI                                                               |
| `KLANGK_SMTP_HOST`              |                                      | SMTP server hostname (if set, uses SMTP; otherwise uses sendmail)                                                                                           |
| `KLANGK_SMTP_PORT`              | `587`                                | SMTP server port                                                                                                                                            |
| `KLANGK_SMTP_USER`              |                                      | SMTP auth username                                                                                                                                          |
| `KLANGK_SMTP_PASSWORD`          |                                      | SMTP auth password                                                                                                                                          |
| `KLANGK_SMTP_FROM`              |                                      | Email sender address (falls back to SMTP_USER, then noreply@localhost)                                                                                      |
| `KLANGK_SMTP_USE_TLS`           | `true`                               | Use STARTTLS for SMTP                                                                                                                                       |
| `KLANGK_SENDMAIL_PATH`          | `sendmail`                           | Path to sendmail binary (used when KLANGK_SMTP_HOST is not set)                                                                                             |
| `KLANGK_LOGIN_BANNER_TITLE`     |                                      | Title shown on the consent banner page (e.g., company name). If empty, no title is displayed.                                                               |
| `KLANGK_LOGIN_BANNER`           |                                      | Consent banner text shown before login. Blocks all access until accepted. Supports `file:` prefix. If empty, no banner is shown.                            |
| `LOGFIRE_TOKEN`                 |                                      | Pydantic Logfire write token (opt-in)                                                                                                                       |
| `LOGFIRE_BASE_URL`              | `https://logfire-api.pydantic.dev`   | Logfire API base URL (for self-hosted instances)                                                                                                            |
| `LOGFIRE_ENVIRONMENT`           |                                      | Logfire environment tag (e.g., `production`, `staging`) — filters traces in the dashboard.                                                                  |

### Ports

- `KLANGK_NGINX_PORT` (default `8995`): **Primary access point** — nginx serves UI, API, WebSocket, and proxies hosted app URLs directly to container ports
- `KLANGK_PORT` (default `8997`): Backend (FastAPI/uvicorn)
- `9000+`: User app ports (5 per workspace, mapped to container ports 8000-8004)

## Branch Protection

`main` requires a PR with 4 passing checks before merge:

- `test-backend`
- `test-frontend`
- `test-cli-e2e`
- `test-flutter-e2e`

All 4 run automatically on PRs. You can bypass as repo admin.

## Project Layout

```text
src/
  backend/             # FastAPI app
    tests/             # Backend unit tests
    e2e-tests/         # CLI E2E tests
  frontend/            # Flutter web app
    test/              # Frontend unit tests
    e2e-tests/         # Playwright E2E tests
  docker/              # Dockerfile, entrypoint, system prompt
  bridge/              # @klangk/bridge npm package
plugins/               # Built-in plugins (celebrate, beep, etc.)
scripts/               # Build and utility scripts
devenv.nix             # devenv configuration
```

## All Shell Commands

Inside `devenv shell`, these commands are available:

| Command             | Description                           |
| ------------------- | ------------------------------------- |
| `test-backend`      | Run backend unit tests                |
| `test-frontend`     | Run frontend unit tests with coverage |
| `test-cli-e2e`      | Run CLI E2E tests                     |
| `test-frontend-e2e` | Run Flutter E2E tests (all browsers)  |
| `flutterbuildweb`   | Rebuild Flutter web only              |
| `dockerbuild`       | Rebuild Docker image only             |
| `update-plugins`    | Fetch plugins from plugins.yaml       |

## Plugin System

All plugins live in `$KLANGK_PLUGINS_DIR/<name>/` directories (defaults to `.devenv/state/klangk/plugins/`). A plugin can contain:

- `extension.ts` — Pi extension with `pi.registerTool()`. Copied to `src/docker/extensions/` at build time.
- `klangk/` — Optional Dart package for client-side browser actions:
  - `klangk/pubspec.yaml` — Package definition, depends on `klangk_plugin_api` (git)
  - `klangk/lib/plugin.dart` — Class extending `ToolPlugin` with action handlers
  - `klangk/lib/*.dart` — Supporting Dart files (widgets, utilities)
- `tools/` — Server-side scripts. Everything in this subdirectory is copied to `/opt/klangk/plugin-tools/<name>/` in the Docker image.

A plugin needs at minimum an `extension.ts`. The `klangk/` subdirectory is only needed for client-side browser actions (e.g., celebrate, beep, authenticated fetch) that are dispatched via the browser bridge.

### Build integration

- `scripts/import_dart_plugins.py` scans `$KLANGK_PLUGINS_DIR/*/klangk/` for plugin Dart packages and generates `$KLANGK_PLUGINS_DIR/.dart/` (the `klangk_plugins` package with path deps and `createAllPlugins()`)
- `dockerbuild` stages `extension.ts` and `tools/` files from all plugins into `$KLANGK_PLUGINS_DIR/.docker/` and passes them via named Docker build contexts (`plugin-extensions`, `plugin-tools`)
- `flutterbuildweb` runs the codegen before compiling
- `stub_dart_plugins.sh` creates a minimal stub at `$KLANGK_PLUGINS_DIR/.dart/` so `flutter pub get` works before plugins are fetched (runs automatically at devenv shell startup via `enterShell`; skips if `pubspec_overrides.yaml` already exists)
- Both build tasks are triggered automatically by `devenv up` via `execIfModified`

### Adding a plugin

For local development, create files directly in `$KLANGK_PLUGINS_DIR`:

1. Create `$KLANGK_PLUGINS_DIR/<name>/extension.ts` with `pi.registerTool()`
2. For client-side browser actions, add `klangk/pubspec.yaml` (depends on `klangk_plugin_api`) and `klangk/lib/plugin.dart` extending `ToolPlugin`
3. For server-side scripts, add files in `$KLANGK_PLUGINS_DIR/<name>/tools/`
4. `devenv up` rebuilds automatically when `$KLANGK_PLUGINS_DIR` changes

For remote plugins, add an entry to `$KLANGK_PLUGINS_DIR/plugins.yaml` and run `update-plugins` to fetch it.

### Plugin management

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

## Data

- All data stored in `$KLANGK_DATA_DIR` (defaults to `$DEVENV_STATE/klangk/data`)
- SQLite database: `klangk.db` (users, workspaces, port allocations, token blocklist, login attempts)
- Workspace files: `workspaces/<user-id>/home/<workspace-id>/work/` (inside the `/home/klangk` bind mount)
- Persistent home: `workspaces/<user-id>/home/<workspace-id>/` (mounted as `/home/klangk` — dotfiles, bash history, Pi sessions)
- Database persists across restarts and rebuilds

## SSH Agent Forwarding

If you have an SSH agent running on the host (`ssh-agent` or 1Password/Secretive), Klangk automatically forwards it into workspace containers. This means `git clone`, `git push`, and `ssh` work inside containers without copying your private keys — the keys never leave the host.

```bash
# On the host, add your key to the agent (if not already)
ssh-add ~/.ssh/id_ed25519

# Inside a container, git/ssh just works
git clone git@github.com:yourorg/private-repo.git
```

For extra security, use `ssh-add -c` to require confirmation on the host for each SSH operation, preventing malicious code from silently using your key.

## CLI Access

Klangk provides a CLI for terminal-based access to the same containers:

```bash
klangk login admin@example.com        # authenticate (prompts for password)
klangk list                             # list workspaces
klangk create my-project                # create a workspace
klangk create my-project --mount ~/src:/home/klangk/work/src          # with bind mount
klangk create my-project --mount nix-store:/nix           # with named volume
klangk create my-project --env KLANGK_SKILLS=stats,rdkit    # with env vars
klangk edit my-project                  # interactive edit (name, image, command, mounts, env)
klangk edit my-project --env FOO=bar    # set env var via flag
klangk dup my-project my-copy           # duplicate a workspace
klangk shell my-project                 # drop into bash inside the container
klangk exec my-project ls /home/klangk/work         # run a command in the container
klangk sync ~/src my-project:/home/klangk/work      # sync files to/from the container
klangk rm my-project                # delete a workspace
klangk export my-project            # export workspace to my-project.tar.gz (admin only)
klangk export my-project -o bak.tar.gz  # export to specific file
klangk import bak.tar.gz            # import workspace from archive
klangk import bak.tar.gz --name new-name  # import with a different name
klangk volumes ls                   # list Docker volumes
klangk volumes create nix-store     # create a named volume
klangk volumes rm nix-store         # delete a volume
```

The CLI connects to the running Klangk backend over HTTP + WebSocket — it works locally and against remote servers.

## Workspace Export/Import

Workspaces can be exported as `.tar.gz` archives and imported to create new workspaces. The archive contains:

- `workspace.json` — metadata (name, image, default command, mounts, env vars, num_ports)
- `home/` — the workspace's home directory tree (files, dotfiles, virtualenvs, Pi sessions, bash history)

**Export** (admin only): `klangk export <workspace>` downloads the archive via `GET /workspaces/{id}/export`. The tarball is built on the server using a temp file to avoid memory pressure on large workspaces.

**Import**: `klangk import <archive>` uploads the archive via `POST /workspaces/import`. The server streams the upload to a temp file, extracts metadata, creates the workspace, and extracts the home directory. Invalid images or mounts from the archive are silently dropped. Use `--name` to override the workspace name from the archive.

System-level packages (apt installs, etc.) are not included — those belong in custom Docker images.

## Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` via [git-hooks.nix](https://github.com/cachix/git-hooks.nix):

- **ruff check --fix** — Python linting with auto-fix
- **ruff format** — Python formatting
- **dart format** — Dart formatting
- **nixfmt** — Nix formatting
- **prettier** — TypeScript, JavaScript, and YAML formatting
- **yamllint** — YAML linting

Hooks are installed automatically when entering the devenv shell.
