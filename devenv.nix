{
  pkgs,
  config,
  lib,
  ...
}:
{
  languages.javascript = {
    enable = true;
    npm.enable = true;
    npm.install.enable = true;
    directory = "./src/e2e_tests";
    corepack.enable = false; # disinclude dev version of node, squash warnings
  };
  languages.python = {
    enable = true;
    venv.enable = true;
    uv = {
      enable = true;
      sync.enable = true;
    };
    directory = "./src/backend";
  };

  packages = with pkgs; [
    docker-client
    flutter
    gnutar
    nginx
    xz
    git # HM for "error: Failed to find git" during devenv:git-hooks:install
    sqlite
  ];

  env.PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
  env.PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";

  tasks = {
    "bark:flutter-build" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/flutterbuildweb.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/flutterbuildweb.sh"
        "src/frontend/lib/**"
        "src/frontend/web/**"
        "src/frontend/pubspec.yaml"
        "src/frontend/pubspec.lock"
        "${config.env.BARK_PLUGINS_DIR}/**/*.dart"
        "${config.env.BARK_PLUGINS_DIR}/plugins.lock"
      ];
    };
    "bark:docker-build" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/dockerbuild.sh"
        "src/dockerimage/**"
        "${config.env.BARK_PLUGINS_DIR}/**/*.ts"
        "${config.env.BARK_PLUGINS_DIR}/**/tools/**"
        "${config.env.BARK_PLUGINS_DIR}/plugins.lock"
      ];
    };
  };

  processes = {
    backend = {
      exec = ''
        cd $DEVENV_ROOT/src/backend && exec uvicorn bark_backend.main:app --host 0.0.0.0 --port $BARK_PORT
      '';
      after = [
        "bark:flutter-build"
        "bark:docker-build"
      ];
    };
    nginx = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/nginx.sh"'';
      after = [
        "bark:flutter-build"
        "bark:docker-build"
      ];
    };
  };

  env.SOURCE_DATE_EPOCH = "";
  env.UV_PYTHON = config.devenv.state + "/venv/bin/python";
  # Port defaults use mkOverride 1500 (lower priority than mkDefault/1000).
  # dotenv.enable loads .env values as mkDefault, so .env entries override these.
  # devenv.local.nix with lib.mkForce overrides everything.
  # Priority: devenv.local.nix (mkForce/50) > .env (mkDefault/1000) > these defaults (1500)
  env.BARK_PORT = lib.mkOverride 1500 "8997";
  env.BARK_NGINX_PORT = lib.mkOverride 1500 "8995";
  env.BARK_SOLIPLEX_PORT = lib.mkOverride 1500 "8555";
  env.BARK_DATA_DIR = lib.mkOverride 1500 (builtins.getEnv "HOME" + "/.bark/data");
  env.BARK_PLUGINS_DIR = lib.mkOverride 1500 (builtins.getEnv "HOME" + "/.bark/plugins");
  env.BARK_IMAGE_NAME = lib.mkOverride 1500 "bark-pi";
  env.BARK_INSTANCE_ID = lib.mkOverride 1500 "default";
  dotenv.enable = true;

  scripts.flutterbuildweb.exec = ''exec devenv tasks run bark:flutter-build --refresh-task-cache "$@"'';
  scripts.dockerbuild.exec = ''exec devenv tasks run bark:docker-build --refresh-task-cache "$@"'';
  scripts.pull-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/pull-base-image.sh" "$@"'';
  scripts.push-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/push-base-image.sh" "$@"'';
  scripts.dockerbuild-base.exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild-base.sh" "$@"'';

  scripts.kill-containers.exec = ''
    docker ps -a --filter "label=bark.instance=''${BARK_INSTANCE_ID}" -q | xargs -r docker rm -f
  '';

  scripts.rebuild.exec = ''
    echo "Rebuilding Docker image..."
    dockerbuild
    echo "Rebuilding Flutter..."
    flutterbuildweb
    echo "==> Done"
  '';

  scripts.update-plugins.exec = ''
    cd $DEVENV_ROOT
    python3 scripts/update_plugins.py "$@"
  '';

  # -n auto: run tests in parallel across CPUs (pytest-xdist)
  scripts.test-backend.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/backend/tests -v -n auto "$@"
  '';

  scripts.test-e2e.exec = ''
    cd $DEVENV_ROOT
    devenv tasks run bark:flutter-build bark:docker-build
    cd src/e2e_tests
    npm install --silent
    exec npx playwright test --reporter=list "$@"
  '';

  scripts.test-frontend.exec = ''
    cd $DEVENV_ROOT/src/frontend
    flutter test "$@"
    exit_code=$?
    case "$*" in
      *--coverage*)
        if [ -f coverage/lcov.info ]; then
          python3 $DEVENV_ROOT/scripts/lcov-report.py coverage/lcov.info
        fi
        ;;
    esac
    exit $exit_code
  '';

  # --- Pre-commit hooks ---
  git-hooks.hooks = {
    # Python: ruff lint + format
    ruff-lint = {
      enable = true;
      name = "ruff check";
      entry = "${pkgs.ruff}/bin/ruff check --fix";
      files = "\\.py$";
      language = "system";
      pass_filenames = true;
    };
    ruff-format = {
      enable = true;
      name = "ruff format";
      entry = "${pkgs.ruff}/bin/ruff format";
      files = "\\.py$";
      language = "system";
      pass_filenames = true;
    };
    # Dart
    dart-format = {
      enable = true;
      name = "dart format";
      entry = "dart format";
      files = "\\.dart$";
      language = "system";
      pass_filenames = true;
    };
    # TypeScript / JavaScript / YAML: prettier
    prettier = {
      enable = true;
      settings.write = true;
      excludes = [
        "node_modules/"
        "src/frontend/build/"
        "\\.devenv/"
      ];
    };
    # Nix
    nixfmt.enable = true;
    # Secrets
    trufflehog.enable = true;
    # GitHub Actions
    actionlint.enable = true;
    # Markdown
    markdownlint.enable = true;
    # TOML
    check-toml.enable = true;
    # Shell
    check-executables-have-shebangs.enable = true;
    shellcheck.enable = true;
    shfmt = {
      enable = true;
      settings.indent = 2;
    };
    # YAML lint
    yamllint.enable = true;
  };

  enterShell = ''
    mkdir -p "$BARK_DATA_DIR"

    # Ensure bark_plugins stub exists so flutter pub get works
    # before plugins are fetched (first-time checkout / CI)
    bash "$DEVENV_ROOT/scripts/stub_dart_plugins.sh"

    # Generate prettierignore (not committed)
    cat > "$DEVENV_ROOT/.prettierignore" <<'PRETTIER'
    node_modules/
    src/frontend/build/
    .devenv/
    *.lock
    PRETTIER

    # Generate yamllint config (not committed)
    cat > "$DEVENV_ROOT/.yamllint.yml" <<'YAMLLINT'
    extends: relaxed
    rules:
      line-length:
        max: 200
    YAMLLINT

    # Generate markdownlint config (not committed)
    cat > "$DEVENV_ROOT/.markdownlint.yaml" <<'MDLINT'
    MD013: false
    MD034: false
    MDLINT
  '';

  claude.code.mcpServers = { };
}
