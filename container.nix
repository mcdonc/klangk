# Container build configuration for the klangk-host image.
#
# Build: dockerbuild-host (or scripts/dockerbuild-host.sh)
# Run:   docker run -v <data>:/env/data -v /var/run/docker.sock:/var/run/docker.sock \
#          --group-add <docker-gid> -e KLANGK_DEFAULT_USER=... klangk-host:latest
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
  # Requires `--impure` for container builds since the venv is outside the nix store.
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
  # --- Container-specific env overrides ---

  env.PYTHONPATH =
    if isContainer then
      lib.mkForce "/env/src/backend:${
        if venvCopy != null then toString venvCopy else ""
      }/lib/python3.13/site-packages"
    else
      lib.mkDefault "";

  env.KLANGK_DATA_DIR = lib.mkOverride 1500 (
    if isContainer then "/env/data" else config.devenv.root + "/.devenv/state/klangk/data"
  );
  env.KLANGK_PLUGINS_DIR = lib.mkOverride 1500 (
    if isContainer then "/env/app/plugins" else config.devenv.root + "/.devenv/state/klangk/plugins"
  );
  env.KLANGK_VERSION_FILE = if isContainer then "/env/version.json" else lib.mkOverride 1500 "";

  dotenv.disableHint = isContainer;

  # No-op processes for container (nix still evaluates them).
  processes = lib.mkIf isContainer {
    backend.exec = "true";
    nginx.exec = "true";
  };

  # --- Container image definition ---

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
}
