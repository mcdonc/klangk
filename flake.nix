# Nix flake for running klangk (server + client) with all runtime
# dependencies on PATH.
#
# Quick start (creates a venv and pip-installs klangk on first run):
#
#   nix run github:mcdonc/klangk                # run klangkd
#   nix run github:mcdonc/klangk#klangk         # run klangk CLI
#   nix develop github:mcdonc/klangk            # shell with all deps
#
# The flake provides Python + every runtime binary klangkd needs (podman,
# caddy, tmux, etc.) via Nix.  The klangk wheel itself is pip-installed
# into a persistent venv at ~/.local/share/klangk/venv on first launch —
# Nix handles the system deps, pip handles the Python deps.
#
# Supports x86_64-linux, aarch64-linux, x86_64-darwin, aarch64-darwin.
# See #1948.
{
  description = "klangk — multi-user Pi coding agent (server + client)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };

        python = pkgs.python312;

        # Runtime binaries that klangkd shells out to (podman, caddy, etc.).
        # The list mirrors the klangkd doctor checks (#1612).
        runtimeDeps =
          with pkgs;
          [
            caddy
            coreutils # GNU du (macOS BSD du lacks -b)
            git
            gnutar # macOS ships BSD tar; GNU tar required
            gzip
            openssl
            podman
            rsync
            sqlite
            tmux
          ]
          ++ lib.optionals stdenv.hostPlatform.isLinux [
            # Rootless podman prerequisites — macOS uses podman machine
            # instead of user namespaces.
            fuse-overlayfs
            slirp4netns
            shadow # newuidmap / newgidmap
          ];

        runtimePath = pkgs.lib.makeBinPath runtimeDeps;

        # Wrapper script: ensures a venv with klangk installed exists at
        # ~/.local/share/klangk/venv, then execs the target binary.
        # On first run (or after `--upgrade`), pip installs/upgrades the
        # wheel from PyPI.  Subsequent runs just exec into the existing
        # venv — no network needed.
        mkLauncher =
          binName:
          pkgs.writeShellScriptBin binName ''
            set -euo pipefail
            export PATH="${runtimePath}:${python}/bin:$PATH"

            KLANGK_DATA="''${XDG_DATA_HOME:-$HOME/.local/share}/klangk"
            VENV="$KLANGK_DATA/venv"

            if [ ! -f "$VENV/bin/${binName}" ]; then
              echo "klangk: creating venv and installing klangk from PyPI..." >&2
              mkdir -p "$KLANGK_DATA"
              ${python}/bin/python3 -m venv "$VENV"
              "$VENV/bin/pip" install --upgrade pip >/dev/null 2>&1
              "$VENV/bin/pip" install klangk
              echo "klangk: installed." >&2
            fi

            # Pass --upgrade to force a pip upgrade of the wheel.
            if [ "''${1:-}" = "--upgrade" ]; then
              shift
              echo "klangk: upgrading klangk from PyPI..." >&2
              "$VENV/bin/pip" install --upgrade klangk
              echo "klangk: upgraded." >&2
            fi

            exec "$VENV/bin/${binName}" "$@"
          '';

        klangkd = mkLauncher "klangkd";
        klangk = mkLauncher "klangk";

        # Combined package with both binaries.
        klangkPkg = pkgs.symlinkJoin {
          name = "klangk";
          paths = [
            klangkd
            klangk
          ];
        };
      in
      {
        packages.default = klangkPkg;
        packages.klangkd = klangkd;
        packages.klangk = klangk;

        apps.default = {
          type = "app";
          program = "${klangkd}/bin/klangkd";
        };

        apps.klangk = {
          type = "app";
          program = "${klangk}/bin/klangk";
        };

        # Dev shell with all runtime deps + Python — useful for
        # contributors who want to pip-install klangk in editable mode
        # or run klangkd doctor.
        devShells.default = pkgs.mkShell {
          packages = runtimeDeps ++ [ python ];
          shellHook = ''
            echo "klangk dev shell — Python ${python.version}, all runtime deps on PATH."
            echo "  pip install klangk    # install from PyPI"
            echo "  klangkd              # start the server"
          '';
        };
      }
    );
}
