{ pkgs, config, lib, ... }: {
  languages.dart = {
    enable = true;
    package = pkgs.flutter;
  };
  languages.javascript = {
    enable = true;
    npm.enable = true;
    npm.install.enable = true;
    directory = "./tests/playwright";
  };
  languages.python = {
    enable = true;
    venv.enable = true;
    uv = {
      enable = true;
      sync.enable = true;
    };
    directory = "./backend";
  };

  packages = with pkgs; [
    docker-client
    nginx
  ];

  tasks = {
    "bark:flutter-build" = {
      exec = "flutterbuildweb";
      showOutput = true;
      execIfModified = [
        "scripts/flutterbuildweb.sh"
        "frontend/lib"
        "frontend/web"
        "frontend/pubspec.yaml"
        "frontend/pubspec.lock"
        "${config.env.BARK_PLUGINS_DIR}/**/*.dart"
        "${config.env.BARK_PLUGINS_DIR}/plugins.lock"
      ];
    };
    "bark:docker-build" = {
      exec = "dockerbuild";
      showOutput = true;
      execIfModified = [
        "scripts/dockerbuild.sh"
        "docker/Dockerfile"
        "docker/entrypoint.sh"
        "docker/*.md"
        "docker/builtin-extensions/*.ts"
        "${config.env.BARK_PLUGINS_DIR}/**/*.ts"
        "${config.env.BARK_PLUGINS_DIR}/**/tools/**"
        "${config.env.BARK_PLUGINS_DIR}/plugins.lock"
      ];
    };
  };

  processes = {
    backend = {
      exec = ''
        cd $DEVENV_ROOT/backend && exec uvicorn backend.main:app --host 0.0.0.0 --port $BARK_PORT
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

  scripts.flutterbuildweb.exec = ''exec bash "$DEVENV_ROOT/scripts/flutterbuildweb.sh" "$@"'';
  scripts.dockerbuild.exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild.sh" "$@"'';

  scripts.rebuild.exec = ''
    echo "Rebuilding Bark..."
    echo "==> Docker image"
    dockerbuild
    echo "==> Flutter web"
    flutterbuildweb
    echo "==> Done"
  '';

  scripts.update-plugins.exec = ''
    cd $DEVENV_ROOT
    python3 scripts/update_plugins.py "$@"
  '';

  scripts.test-backend.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest tests/unit/backend -v "$@"
  '';

  scripts.test-e2e.exec = ''
    cd $DEVENV_ROOT/tests/playwright
    exec npx playwright test "$@"
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
        "frontend/build/"
        "\\.devenv/"
      ];
    };
    # YAML lint
    yamllint.enable = true;
  };

  enterShell = ''
    mkdir -p "$BARK_DATA_DIR"

    # Generate prettierignore (not committed)
    cat > "$DEVENV_ROOT/.prettierignore" <<'PRETTIER'
    node_modules/
    frontend/build/
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
  '';
}
