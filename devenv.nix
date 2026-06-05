{
  pkgs,
  config,
  lib,
  ...
}:
let
  isContainer = config.container.isBuilding;
  isDev = !isContainer;

  # The local venv, pre-built by `uv sync` during devenv shell.
  # Contains bin/ (uvicorn, klangk, etc.) and lib/python3.13/site-packages/.
  # Requires `--impure` for container builds since the venv is outside the nix store.
  # Must run `devenv shell` at least once before building the container.
  venvCopy =
    if isContainer && builtins.pathExists ./.devenv/state/venv then
      builtins.path {
        path = ./.devenv/state/venv;
        name = "venv";
      }
    else
      null;

  uvicornCmd = "python3 -m uvicorn klangk_backend.main:app --host 0.0.0.0 --port $KLANGK_PORT --ws-max-size 65536 --ws-ping-interval 20 --ws-ping-timeout 20";

  # Version info baked into the container image at build time.
  # Uses builtins.getEnv (impure) so the build command can pass values:
  #   KLANGK_BUILD_COMMIT=$(git rev-parse --short HEAD) \
  #   KLANGK_BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  #   NIX_CONFIG="pure-eval = false" devenv container build processes
  buildCommit = builtins.getEnv "KLANGK_BUILD_COMMIT";
  buildTimestamp = builtins.getEnv "KLANGK_BUILD_TIMESTAMP";
  versionJson = pkgs.writeText "version.json" (
    builtins.toJSON {
      commit = if buildCommit != "" then buildCommit else "unknown";
      built_at = if buildTimestamp != "" then buildTimestamp else "unknown";
    }
  );

  # Home directory contents for the container image.
  # Laid out as: bin/{nginx,klangk}, data/, src/{backend,frontend}, version.json
  containerHome = pkgs.runCommand "klangk-home" { } ''
    mkdir -p $out/bin $out/data $out/src/backend $out/src/frontend/build

    # Backend source code
    cp -r ${./src/backend/klangk_backend} $out/src/backend/klangk_backend
    cp ${./src/backend/pyproject.toml} $out/src/backend/pyproject.toml

    # Pre-built Flutter web output (must run flutter build web first)
    if [ -d ${./src/frontend/build/web} ]; then
      cp -r ${./src/frontend/build/web} $out/src/frontend/build/web
    fi

    # Startup scripts
    cp ${./scripts/nginx.sh} $out/bin/nginx
    chmod +x $out/bin/nginx

    cat > $out/bin/klangk <<'SCRIPT'
    #!/usr/bin/env bash
    exec ${uvicornCmd}
    SCRIPT
    chmod +x $out/bin/klangk

    # Version info
    cp ${versionJson} $out/version.json
  '';
in
{
  languages.javascript = {
    enable = isDev;
    npm.enable = isDev;
    npm.install.enable = isDev;
    directory = "./src/frontend/e2e-tests";
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

  packages =
    with pkgs;
    [
      bash # explicit bash for shell scripts (CI /bin/sh may be dash)
      docker-client
      coreutils # GNU du (macOS BSD du lacks -b)
      gzip
      gnutar
      nginx
      xz
      sqlite
      rsync
    ]
    ++ lib.optionals isDev [
      flutter
      git # HM for "error: Failed to find git" during devenv:git-hooks:install
    ];

  env.PLAYWRIGHT_BROWSERS_PATH = if isContainer then "" else pkgs.playwright-driver.browsers;
  env.PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = if isContainer then "" else "true";

  tasks = lib.mkIf isDev {
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
        "src/docker/**"
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

  processes =
    if isContainer then
      {
        # Processes aren't used directly in the container (startupCommand handles it)
        # but nix still evaluates them, so provide no-ops.
        backend.exec = "true";
        nginx.exec = "true";
      }
    else
      {
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
  env.PYTHONPATH =
    if isContainer then
      lib.mkForce "/env/src/backend:${
        if venvCopy != null then toString venvCopy else ""
      }/lib/python3.13/site-packages"
    else
      lib.mkDefault "";
  env.UV_PYTHON = config.devenv.state + "/venv/bin/python";
  # Port defaults use mkOverride 1500 (lower priority than mkDefault/1000).
  # dotenv.enable loads .env values as mkDefault, so .env entries override these.
  # devenv.local.nix with lib.mkForce overrides everything.
  # Priority: devenv.local.nix (mkForce/50) > .env (mkDefault/1000) > these defaults (1500)
  env.KLANGK_PORT = lib.mkOverride 1500 "8997";
  env.KLANGK_NGINX_PORT = lib.mkOverride 1500 "8995";
  env.KLANGK_DATA_DIR = lib.mkOverride 1500 (
    if isContainer then "/env/data" else config.devenv.root + "/.devenv/state/klangk/data"
  );
  env.KLANGK_PLUGINS_DIR = lib.mkOverride 1500 (
    if isContainer then "/env/app/plugins" else config.devenv.root + "/.devenv/state/klangk/plugins"
  );
  env.KLANGK_IMAGE_NAME = lib.mkOverride 1500 "klangk";
  env.KLANGK_INSTANCE_ID = lib.mkOverride 1500 "default";
  env.KLANGK_VERSION_FILE = if isContainer then "/env/version.json" else lib.mkOverride 1500 "";
  dotenv.enable = isDev;
  dotenv.disableHint = isContainer;

  scripts.flutterbuildweb.exec = ''exec devenv tasks run klangk:flutter-build --refresh-task-cache "$@"'';
  scripts.dockerbuild.exec = ''exec devenv tasks run klangk:docker-build --refresh-task-cache "$@"'';
  scripts.pull-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/pull-base-image.sh" "$@"'';
  scripts.push-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/push-base-image.sh" "$@"'';
  scripts.dockerbuild-base.exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild-base.sh" "$@"'';
  scripts.dockerbuild-host.exec = ''exec bash "$DEVENV_ROOT/scripts/dockerbuild-host.sh" "$@"'';

  scripts.kill-containers.exec = ''
    docker ps -a --filter "label=klangk.instance=''${KLANGK_INSTANCE_ID}" -q | xargs -r docker rm -f
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

  # CLI E2E tests: start real server, run klangk commands, need Docker
  scripts.test-cli-e2e.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/backend/e2e-tests -v -p no:xdist --no-cov "$@"
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

  # --- Pre-commit hooks (dev only) ---
  git-hooks.hooks = lib.mkIf isDev {
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
    mkdir -p "$KLANGK_DATA_DIR"
  ''
  + lib.optionalString isDev ''
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

  containers.processes =
    let
      # Symlinks from $HOME/{bin,src,version.json} to the nix store derivation,
      # so the home directory is clean (no hash-prefixed dirs).
      homeSymlinks = pkgs.runCommand "klangk-symlinks" { } ''
        mkdir -p $out/env
        ln -s ${containerHome}/bin $out/env/bin
        ln -s ${containerHome}/src $out/env/src
        ln -s ${containerHome}/version.json $out/env/version.json
      '';
    in
    {
      name = "klangk-host";
      copyToRoot = [ ];
      startupCommand = "$HOME/bin/nginx & exec $HOME/bin/klangk";
      layers = [
        {
          copyToRoot = [ homeSymlinks ];
          perms = [
            {
              path = homeSymlinks;
              regex = "/env";
              mode = "0755";
              uid = 1000;
              gid = 1000;
              uname = "user";
              gname = "user";
            }
          ];
        }
      ];
    };

  claude.code.mcpServers = { };
}
