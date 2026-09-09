# Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` via [git-hooks.nix](https://github.com/cachix/git-hooks.nix):

- **actionlint** — GitHub Actions workflow linting
- **check-executables-have-shebangs** — ensures executable scripts have a shebang line
- **deferred-imports** — flags non-module-scope imports
- **check-toml** — TOML syntax validation
- **dart format (verify)** — fails on Dart files that are not canonically formatted instead of rewriting them; run `dart format` yourself before committing. The hook skips files whose package has no `.dart_tool` yet (run `flutter pub get` there); the frontend CI workflow checks formatting for `src/frontend` and every feature package after `pub get`
- **markdownlint** — Markdown linting
- **nixfmt** — Nix formatting
- **prettier** — TypeScript, JavaScript, and YAML formatting
- **ruff format** — Python formatting
- **ruff check --fix** — Python linting with auto-fix
- **xenon** — Python complexity gate (rank A; see AGENTS.md)
- **binary-integrity** — protects generated binary artifacts from drift
- **shellcheck** — shell script linting
- **shfmt** — shell script formatting
- **trufflehog** — secret scanning
- **yamllint** — YAML linting

Hooks are installed automatically when entering the devenv shell.
