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
    directory = "./src/backend";
  };

  packages =
    with pkgs;
    [
      bash # explicit bash for shell scripts (CI /bin/sh may be dash)
      coreutils # GNU du (macOS BSD du lacks -b)
      docker-client
      flutter
      git # "error: Failed to find git" during devenv:git-hooks:install
      gzip
      gnutar
      nginx
      podman
      sqlite.bin
      rsync
    ]
    ++ (if pkgs.stdenv.isDarwin then [ iproute2mac ] else [ iproute2 ]);

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
    "klangk:build-workspace-image" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/build-workspace-image.sh"'';
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
  # generated in enterShell and scripts pass it via --signature-policy.
  # On macOS podman runs in *remote* mode against the VM, which has its own
  # policy and rejects the local-storage --signature-policy flag, so leave
  # this empty there (scripts skip the flag when the var is unset).
  env.KLANGK_SIGNATURE_POLICY = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isDarwin then
      ""
    else
      config.devenv.state + "/klangk/podman/policy.json"
  );
  # Tell podman where to find the signature policy globally (not just via
  # --signature-policy flags in build scripts). Fixes `podman save`, `podman
  # run`, etc. which only check default paths.
  env.CONTAINERS_SIGNATURE_POLICY = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isDarwin then
      ""
    else
      config.devenv.state + "/klangk/podman/policy.json"
  );
  env.KLANGK_INSTANCE_ID = lib.mkOverride 1500 "default";
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

  scripts.run-host-container.exec = ''exec bash "$DEVENV_ROOT/scripts/run-host-container.sh" "$@"'';

  scripts.kill-containers.exec = ''
    ''${KLANGK_PODMAN_BIN:-podman} ps -a \
      --filter "label=klangk.instance=''${KLANGK_INSTANCE_ID}" \
      -q | xargs -r ''${KLANGK_PODMAN_BIN:-podman} rm -f
  '';

  scripts.restart.exec = ''
    echo "Stopping devenv processes..."
    devenv processes down --no-tui 2>/dev/null || true
    sleep 1
    echo "Starting..."
    exec devenv up --no-tui "$@"
  '';

  scripts.rebuild.exec = ''
    echo "Rebuilding backend image..."
    build-workspace-image
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
    devenv tasks run klangk:flutter-build klangk:build-workspace-image
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

    # Podman config lives outside the storage directory so
    # `podman system reset` doesn't delete it.
    _PODMAN_CONF="$DEVENV_STATE/klangk/podman"
    _PODMAN_STORE="$DEVENV_STATE/klangk/containers"
    # KLANGK_PODMAN_STORAGE: custom path for podman image storage.
    # Use an ext4 filesystem (not ZFS) for --userns=keep-id support.
    # ZFS lacks idmapped mounts, causing storage-chown-by-maps to hang.
    _PODMAN_GRAPHROOT="''${KLANGK_PODMAN_STORAGE:-$_PODMAN_STORE/storage}"
    mkdir -p "$_PODMAN_CONF" "$_PODMAN_STORE/run" \
      "$_PODMAN_GRAPHROOT" "$_PODMAN_GRAPHROOT/tmp"

    cat > "$_PODMAN_CONF/storage.conf" <<STORAGE
    [storage]
    driver = "overlay"
    graphroot = "$_PODMAN_GRAPHROOT"
    runroot = "$_PODMAN_STORE/run"
    STORAGE
    export CONTAINERS_STORAGE_CONF="$_PODMAN_CONF/storage.conf"

    # Stage image blob downloads inside the graphroot ("storage" sentinel)
    # instead of the default /var/tmp, which on this host is the root fs.
    cat > "$_PODMAN_CONF/containers.conf" <<CONTAINERS
    [engine]
    image_copy_tmp_dir = "storage"
    CONTAINERS
    export CONTAINERS_CONF="$_PODMAN_CONF/containers.conf"

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
