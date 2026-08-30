# Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` via [git-hooks.nix](https://github.com/cachix/git-hooks.nix):

- **actionlint** — GitHub Actions workflow linting
- **check-executables-have-shebangs** — ensures executable scripts have a shebang line
- **deferred-imports** — flags non-module-scope imports
- **check-toml** — TOML syntax validation
- **dart format** — Dart formatting
- **markdownlint** — Markdown linting
- **nixfmt** — Nix formatting
- **prettier** — TypeScript, JavaScript, and YAML formatting
- **ruff format** — Python formatting
- **ruff check --fix** — Python linting with auto-fix
- **xenon** — Python complexity gate (rank B; see AGENTS.md)
- **binary-integrity** — protects generated binary artifacts from drift
- **shellcheck** — shell script linting
- **shfmt** — shell script formatting
- **trufflehog** — secret scanning
- **yamllint** — YAML linting

Hooks are installed automatically when entering the devenv shell.
