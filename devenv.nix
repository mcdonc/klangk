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
       --no-access-log \
       --ws-max-size ''${KLANGK_WS_MSG_SIZE_MAX:-16777216} \
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
    directory = ".";
  };

  packages =
    with pkgs;
    [
      bash # explicit bash for shell scripts (CI /bin/sh may be dash)
      coreutils # GNU du (macOS BSD du lacks -b)
      docker-client
      expect
      flutter
      git # "error: Failed to find git" during devenv:git-hooks:install
      gzip
      gnutar
      nginx
      podman
      sqlite.bin
      rsync
      twine
      zensical
    ]
    ++ (
      if pkgs.stdenv.isDarwin then
        [ iproute2mac ]
      else
        [
          iproute2
          su
          util-linux
        ]
    );

  # Nix playwright-driver.browsers has revision numbers that may not match
  # the npm @playwright/test package (nix backports browser security patches).
  # enterShell runs setup-playwright-browsers.sh to create a symlink farm
  # that bridges the two.
  env.NIX_PLAYWRIGHT_BROWSERS = pkgs.playwright-driver.browsers;
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
    "klangk:build-workspace-image" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/build-workspace-image.sh"'';
      after = [ "klangk:update-plugins" ];
      showOutput = true;
    };
    "klangk:kill-containers" = {
      exec = ''
        if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
          ''${KLANGK_PODMAN_BIN:-podman} ps -a --filter "label=klangk.instance=''${KLANGK_INSTANCE_ID}" -q \
            | xargs -r ''${KLANGK_PODMAN_BIN:-podman} rm -f
        fi
      '';
    };
    "klangk:kill-port-holders" = {
      exec = ''
        if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
          for port in $KLANGK_PORT $KLANGK_NGINX_PORT; do
            fuser -k "$port/tcp" 2>/dev/null || true
          done
        fi
      '';
    };
    "klangk:init-plugins" = {
      exec = ''
        if [ ! -f "${config.env.KLANGK_PLUGINS_DIR}/plugins.yaml" ]; then
          cd $DEVENV_ROOT
          python3 scripts/update_plugins.py
        fi
      '';
      before = [ "klangk:update-plugins" ];
      showOutput = true;
    };
    "klangk:update-plugins" = {
      exec = ''
        cd $DEVENV_ROOT
        bash scripts/stub_dart_plugins.sh
        exec python3 scripts/update_plugins.py
      '';
      before = [ "klangk:flutter-build" ];
      showOutput = true;
      execIfModified = [
        "${config.env.KLANGK_PLUGINS_DIR}/plugins.yaml"
      ];
    };
  };

  processes = {
    backend = {
      exec = ''
        cd $DEVENV_ROOT/src/backend && exec ${uvicornCmd}
      '';
      after = [
        "klangk:flutter-build"
        "klangk:build-workspace-image"
        "klangk:kill-containers"
        "klangk:kill-port-holders"
      ];
    };
    nginx = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/nginx.sh"'';
      after = [
        "klangk:flutter-build"
        "klangk:build-workspace-image"
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
  env.KLANGK_IMAGE_NAME = lib.mkOverride 1500 "klangk-workspace";
  # Rootless podman from nix (Linux) has no default policy.json; it is
  # generated in enterShell. CONTAINERS_SIGNATURE_POLICY tells podman
  # where to find it.  On macOS podman runs in *remote* mode against
  # the VM, which has its own policy, so leave this empty there.
  env.CONTAINERS_SIGNATURE_POLICY = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isDarwin then
      ""
    else
      config.devenv.state + "/klangk/podman/policy.json"
  );
  env.KLANGK_INSTANCE_ID = lib.mkOverride 1500 "default";
  env.KLANGK_VERSION_FILE = config.devenv.state + "/klangk/version.json";
  # Docker build platform for klangk images. On Linux, default to the host
  # architecture so arm64 machines build/run natively instead of under amd64
  # emulation. The published GHCR base (klangk-workspace-base:latest) is
  # multi-arch (amd64 + arm64), so we default to the host's native
  # architecture on all platforms. Override in .env to force a specific arch.
  env.KLANGK_PLATFORM = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isAarch64 then "linux/arm64" else "linux/amd64"
  );
  dotenv.enable = true;

  scripts.flutterbuildweb.exec = ''exec bash "$DEVENV_ROOT/scripts/flutterbuildweb.sh" "$@"'';
  scripts.build-workspace-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-workspace-image.sh" "$@"'';
  scripts.pull-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/pull-base-image.sh" "$@"'';
  scripts.push-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/push-base-image.sh" "$@"'';
  scripts.build-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-base-image.sh" "$@"'';
  scripts.build-host-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-host-image.sh" "$@"'';
  scripts.trivy-host.exec = ''exec bash "$DEVENV_ROOT/scripts/trivy-host.sh" "$@"'';
  scripts.trivy-workspace.exec = ''exec bash "$DEVENV_ROOT/scripts/trivy-workspace.sh" "$@"'';

  scripts.run-host-container.exec = ''exec bash "$DEVENV_ROOT/scripts/run-host-container.sh" "$@"'';

  scripts.kill-containers.exec = ''
    ''${KLANGK_PODMAN_BIN:-podman} ps -a \
      --filter "label=klangk.instance=''${KLANGK_INSTANCE_ID}" \
      -q | xargs -r ''${KLANGK_PODMAN_BIN:-podman} rm -f
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

  # CLI unit tests
  scripts.test-cli.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/cli/tests -v -n auto "$@"
  '';

  # CLI E2E tests: start real server, run klangkc commands
  scripts.test-cli-e2e.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/cli/e2e-tests \
      -v -p no:xdist --no-cov "$@"
  '';

  scripts.test-terminal-windows-e2e.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/cli/e2e-tests/test_terminal_windows_e2e.py \
      -v -p no:xdist --no-cov "$@"
  '';

  # Backend E2E tests: start real server, run backend E2E tests
  scripts.test-backend-e2e.exec = ''
    cd $DEVENV_ROOT
    exec python -m pytest src/backend/e2e-tests \
      -v -p no:xdist --no-cov "$@"
  '';

  scripts.test-frontend-e2e.exec = ''
    cd $DEVENV_ROOT
    devenv tasks run klangk:flutter-build klangk:build-workspace-image
    cd src/frontend/e2e-tests
    npm install --silent
    # Re-create the playwright browser symlink farm now that browsers.json
    # exists (enterShell may have skipped it if node_modules was missing).
    source "$DEVENV_ROOT/scripts/setup-playwright-browsers.sh"
    exec npx playwright test --reporter=list "$@"
  '';

  # API fuzz test: start an isolated server, send random requests
  scripts.test-fuzz-api.exec = ''
    cd $DEVENV_ROOT
    exec python scripts/fuzz-api.py "$@"
  '';

  scripts.test-frontend.exec = ''
    cd $DEVENV_ROOT/src/frontend
    rm -rf coverage

    # macOS only: flutter compiles the objective_c native FFI (a transitive
    # dep via the flterm/libghostty terminal stack) during `flutter test`.
    # dart's native_toolchain_c resolves the macOS SDK by running
    # `xcrun --sdk macosx --show-sdk-path`. The first xcrun on PATH is the
    # nix `xcbuild` shim, which only resolves the SDK when DEVELOPER_DIR is
    # set -- but flutter strips DEVELOPER_DIR from the native-assets hook, so
    # that xcrun fails and its error string is fed to clang as -isysroot,
    # producing "'Foundation/Foundation.h' file not found".
    #
    # Fix: prepend scripts/xcrun-shim (which delegates to the system
    # /usr/bin/xcrun) to PATH. The system xcrun resolves the SDK via
    # xcode-select state with no env at all (returns the system MacOSX SDK,
    # which includes the frameworks); the nix clang-wrapper compiles
    # objective-c against that SDK fine.
    if [ "$(uname -s)" = "Darwin" ] && [ -x /usr/bin/xcrun ]; then
      export PATH="$DEVENV_ROOT/scripts/xcrun-shim:$PATH"
    fi

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

  scripts.build-docs.exec = ''
    cd $DEVENV_ROOT
    exec zensical build "$@"
  '';

  scripts.serve-docs.exec = ''
    cd $DEVENV_ROOT
    exec zensical serve --dev-addr 0.0.0.0:9111 "$@"
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
    # Deferred imports
    deferred-imports = {
      enable = true;
      name = "deferred-imports";
      entry = "python3 scripts/check_deferred_imports.py";
      files = "\\.py$";
      language = "system";
      pass_filenames = true;
    };
  };

  enterShell = ''
    # Create playwright browser symlink farm (nix revisions → npm revisions)
    source "$DEVENV_ROOT/scripts/setup-playwright-browsers.sh"

    mkdir -p "$KLANGK_DATA_DIR"

    # Generate version file (used by update_plugins.py and /version endpoint)
    mkdir -p "$(dirname "$KLANGK_VERSION_FILE")"
    bash "$DEVENV_ROOT/scripts/generate-version.sh" > "$KLANGK_VERSION_FILE"

    # Podman uses its default storage (~/.local/share/containers/).
    # To customize, create ~/.config/containers/storage.conf.
    # See docs/reference/podman.md.
    _PODMAN_CONF="$DEVENV_STATE/klangk/podman"
    mkdir -p "$_PODMAN_CONF"
    if [ ! -f "$_PODMAN_CONF/policy.json" ]; then
      echo '{"default": [{"type": "insecureAcceptAnything"}]}' \
        > "$_PODMAN_CONF/policy.json"
    fi

    # On macOS, podman requires a VM; init and start it if needed.
    if [ "$(uname)" = "Darwin" ]; then
      if ! podman machine list --format '{{.Name}}' 2>/dev/null | grep -q .; then
        echo "Initializing podman machine..."
        podman machine init
      fi
      if ! podman machine info 2>/dev/null | grep -q "Running"; then
        echo "Starting podman machine..."
        podman machine start || true
      fi
    fi


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
