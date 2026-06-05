{
  pkgs,
  config,
  lib,
  ...
}:
let
  uvicornCmd = ''
    python3 -m uvicorn klangk_backend.main:app \
       --host 0.0.0.0 \
       --port $KLANGK_PORT \
       --ws-max-size 65536 \
       --ws-ping-interval 20 \
       --ws-ping-timeout 20'';
in
{
  languages.javascript = {
    enable = true;
    npm.enable = true;
    npm.install.enable = true;
    directory = "./src/frontend/e2e-tests";
    # disinclude dev version of node, squash warnings
    corepack.enable = false;
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
    bash # explicit bash for shell scripts (CI /bin/sh may be dash)
    coreutils # GNU du (macOS BSD du lacks -b)
    docker-client
    flutter
    git # "error: Failed to find git" during devenv:git-hooks:install
    gzip
    gnutar
    nginx
    sqlite.bin
    rsync
  ];

  env.PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
  env.PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";

  tasks = {
    "klangk:flutter-build" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/flutterbuildweb.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/flutterbuildweb.sh"
        "src/frontend/lib/**"
        "src/frontend/web/**"
        "src/frontend/pubspec.yaml"
        "src/frontend/pubspec.lock"
        "${config.env.KLANGK_PLUGINS_DIR}/**/*.dart"
        "${config.env.KLANGK_PLUGINS_DIR}/plugins.lock"
      ];
    };
    "klangk:docker-build" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/dockerbuild.sh"
        "src/docker/workspace/**"
        "${config.env.KLANGK_PLUGINS_DIR}/**/*.ts"
        "${config.env.KLANGK_PLUGINS_DIR}/**/tools/**"
        "${config.env.KLANGK_PLUGINS_DIR}/plugins.lock"
      ];
    };
    "klangk:kill-containers" = {
      exec = ''
        if [ ! -f /.dockerenv ]; then
          docker ps -a --filter "label=klangk.instance=''${KLANGK_INSTANCE_ID}" -q | xargs -r docker rm -f
        fi
      '';
    };
    "klangk:kill-port-holders" = {
      exec = ''
        if [ ! -f /.dockerenv ]; then
          for port in $KLANGK_PORT $KLANGK_NGINX_PORT; do
            fuser -k "$port/tcp" 2>/dev/null || true
          done
        fi
      '';
    };
  };

  processes = {
    backend = {
      exec = ''
        cd $DEVENV_ROOT/src/backend && exec ${uvicornCmd}
      '';
      after = [
        "klangk:flutter-build"
        "klangk:docker-build"
        "klangk:kill-containers"
        "klangk:kill-port-holders"
      ];
    };
    nginx = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/nginx.sh"'';
      after = [
        "klangk:flutter-build"
        "klangk:docker-build"
        "klangk:kill-port-holders"
      ];
    };
  };

  env.SOURCE_DATE_EPOCH = "";
  env.UV_PYTHON = config.devenv.state + "/venv/bin/python";
  # Port defaults: mkOverride 1500 (lower priority than mkDefault).
  # dotenv.enable loads .env values as mkDefault, so .env overrides.
  # devenv.local.nix (mkForce/50) > .env (1000) > these (1500)
  env.KLANGK_PORT = lib.mkOverride 1500 "8997";
  env.KLANGK_NGINX_PORT = lib.mkOverride 1500 "8995";
  env.KLANGK_DATA_DIR = lib.mkOverride 1500 (
    config.devenv.root + "/.devenv/state/klangk/data"
  );
  env.KLANGK_PLUGINS_DIR = lib.mkOverride 1500 (
    config.devenv.root + "/.devenv/state/klangk/plugins"
  );
  env.KLANGK_IMAGE_NAME = lib.mkOverride 1500 "klangk";
  env.KLANGK_INSTANCE_ID = lib.mkOverride 1500 "default";
  dotenv.enable = true;

  scripts.flutterbuildweb.exec = ''
    exec devenv tasks run klangk:flutter-build \
      --refresh-task-cache "$@"'';
  scripts.dockerbuild.exec = ''
    exec devenv tasks run klangk:docker-build \
      --refresh-task-cache "$@"'';
  scripts.pull-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/pull-base-image.sh" "$@"'';
  scripts.push-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/push-base-image.sh" "$@"'';
  scripts.dockerbuild-base.exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild-base.sh" "$@"'';
  scripts.dockerbuild-host.exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild-host.sh" "$@"'';
  scripts.trivy-host.exec = ''exec bash "$DEVENV_ROOT/scripts/trivy-host.sh" "$@"'';

  scripts.run-host.exec = ''
    DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
    ENVFILE=$(mktemp)
    trap 'rm -f "$ENVFILE"' EXIT
    env | grep '^KLANGK_' \
      | grep -v '^KLANGK_DATA_DIR=' \
      | grep -v '^KLANGK_PLUGINS_DIR=' \
      | grep -v '^KLANGK_VERSION_FILE=' \
      > "$ENVFILE"
    docker rm -f klangk-host-run 2>/dev/null || true
    exec docker run --name klangk-host-run \
      -p "''${KLANGK_PORT}:''${KLANGK_PORT}" \
      -p "''${KLANGK_NGINX_PORT}:''${KLANGK_NGINX_PORT}" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      --group-add "$DOCKER_GID" \
      --env-file "$ENVFILE" \
      "$@" \
      klangk-host
  '';

  scripts.kill-containers.exec = ''
    docker ps -a \
      --filter "label=klangk.instance=''${KLANGK_INSTANCE_ID}" \
      -q | xargs -r docker rm -f
  '';

  scripts.restart.exec = ''
    echo "Stopping devenv processes..."
    devenv processes down --no-tui 2>/dev/null || true
    sleep 1
    echo "Starting..."
    exec devenv up --no-tui "$@"
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

  # CLI E2E tests: start real server, run klangk commands
  scripts.test-cli-e2e.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/backend/e2e-tests \
      -v -p no:xdist --no-cov "$@"
  '';

  scripts.test-frontend-e2e.exec = ''
    cd $DEVENV_ROOT
    devenv tasks run klangk:flutter-build klangk:docker-build
    cd src/frontend/e2e-tests
    npm install --silent
    exec npx playwright test --reporter=list "$@"
  '';

  scripts.test-frontend.exec = ''
    cd $DEVENV_ROOT/src/frontend
    rm -rf coverage
    flutter test --coverage "$@"
    test_exit=$?
    cov_exit=0
    if [ -f coverage/lcov.info ]; then
      python3 $DEVENV_ROOT/scripts/lcov-report.py coverage/lcov.info
      cov_exit=$?
    fi
    if [ $test_exit -ne 0 ]; then
      echo ""
      echo "FAIL: some tests failed"
      exit 1
    fi
    if [ $cov_exit -ne 0 ]; then
      exit 1
    fi
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
    nixfmt = {
      enable = true;
      settings.width = 80;
    };
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
    mkdir -p "$KLANGK_DATA_DIR"

    # Ensure klangk_plugins stub exists so flutter pub get works
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
