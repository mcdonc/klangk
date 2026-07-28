# Nix flake for running klangk (server + client) with all runtime
# dependencies on PATH.
#
# Quick start:
#
#   nix run github:mcdonc/klangk                # run klangkd
#   nix run github:mcdonc/klangk#klangk         # run klangk CLI
#   nix develop github:mcdonc/klangk            # shell with all deps
#
# Everything — the klangk Python package itself *and* every Python
# dependency — is built by Nix from nixpkgs. There is no venv and no
# pip install: `nix run` produces a working klangkd with no network
# access on first run, and `nix build` produces a self-contained
# closure. Nix also provides the system binaries klangkd shells out to
# (podman, caddy, tmux, git, GNU tar, …).
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

        # nixpkgs' pythonPackages, with the check phase disabled on the
        # direct deps. Some of them (notably fastapi) ship heavy
        # test-only toolchains (inline-snapshot, dirty-equals, …) whose
        # own transitive deps (pint → uncertainties → scipy) aren't in the
        # binary cache for the current nixpkgs-unstable revision and rebuild
        # from source — where scipy hits a known flaky hypothesis test.
        # Skipping the check phase drops that whole subtree from the build
        # closure; the runtime deps are unaffected. (See #1948.)
        pythonPackages = pkgs.python312Packages.overrideScope (
          final: prev: {
            fastapi = prev.fastapi.overridePythonAttrs (_: {
              doCheck = false;
            });
            starlette = prev.starlette.overridePythonAttrs (_: {
              doCheck = false;
            });
            pydantic-settings = prev.pydantic-settings.overridePythonAttrs (_: {
              doCheck = false;
            });
            typer = prev.typer.overridePythonAttrs (_: {
              doCheck = false;
            });
            httpx = prev.httpx.overridePythonAttrs (_: {
              doCheck = false;
            });
            textual = prev.textual.overridePythonAttrs (_: {
              doCheck = false;
            });
            sqlalchemy = prev.sqlalchemy.overridePythonAttrs (_: {
              doCheck = false;
            });
            logfire = prev.logfire.overridePythonAttrs (_: {
              doCheck = false;
            });
            # Safety net: if scipy still ends up in the closure (a new dep
            # adds it), skip its flaky test suite rather than fail the build.
            scipy = prev.scipy.overridePythonAttrs (_: {
              doCheck = false;
            });
          }
        );

        # Runtime binaries that klangkd shells out to (podman, caddy, etc.).
        # The list mirrors the klangkd doctor checks (#1612).
        runtimeBins =
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

        # klangk built from this flake's source via Nix's Python tooling.
        # All Python deps are resolved from nixpkgs — no venv, no pip,
        # no network access at build or install time. The resulting
        # derivation exposes the `klangkd` and `klangk` entry points
        # directly; we wrap them with the runtime binaries below.
        klangkRaw = pythonPackages.buildPythonApplication {
          pname = "klangk";
          # The release version comes from git tags via hatch-vcs (see
          # src/klangk/pyproject.toml). The flake's source filter strips
          # .git, so hatch-vcs can't compute a version here — postPatch
          # below rewrites pyproject.toml to pin a static dev version.
          # The canonical versioned artifact is the PyPI wheel built in
          # the release workflow.
          version = "0.0.0.dev0";
          pyproject = true;
          src = ./src/klangk;

          build-system = [
            pythonPackages.hatchling
            pythonPackages.hatch-vcs
          ];

          # Python runtime deps — resolved entirely from nixpkgs, mirroring
          # the `dependencies` list in src/klangk/pyproject.toml. Extras
          # (`uvicorn[standard]`, `logfire[fastapi]`) are spelled out
          # explicitly since nixpkgs' pythonPackages uses individual
          # packages rather than PEP 508 extras.
          dependencies = with pythonPackages; [
            fastapi
            pydantic-settings
            typer
            aiosqlite
            sqlalchemy
            bcrypt
            python-jose
            cryptography
            python-multipart
            starlette
            aiosmtplib
            jinja2
            logfire
            pyyaml
            httpx
            textual
            # uvicorn[standard] extras:
            uvicorn
            httptools
            uvloop
            watchfiles
            websockets
            # logfire[fastapi] extras:
            opentelemetry-api
            opentelemetry-sdk
            opentelemetry-instrumentation-fastapi
          ];

          # The flake's source filter strips .git (so hatch-vcs can't
          # derive a version) and the gitignored Flutter web build (so
          # the hatch_build_frontend.py hook would refuse to build a
          # UI-less wheel, #1600). Patch pyproject.toml to:
          #   1. pin a static version, and
          #   2. drop the frontend build hook reference.
          # The resulting klangkd serves UI from KLANGKD_FRONTEND_DIR
          # (pointed at a separately built frontend) or runs UI-less;
          # the PyPI wheel remains the canonical UI-shipping artifact.
          postPatch =
            let
              dropFrontendHook = pkgs.writeScript "drop-frontend-hook.py" ''
                import re
                import sys
                from pathlib import Path

                p = Path(sys.argv[1])
                text = p.read_text()
                # Pin a static version (replaces `dynamic = ["version"]`).
                text = text.replace(
                    'dynamic = ["version"]',
                    'version = "0.0.0.dev0"',
                )
                # Drop the frontend build hook section. Matches the section
                # header and its `path = ...` line; comments above it are
                # left in place (harmless orphan).
                text = re.sub(
                    r'\n\[tool\.hatch\.build\.hooks\.custom\]\n'
                    r'path = "hatch_build_frontend\.py"\n',
                    '\n',
                    text,
                )
                p.write_text(text)
              '';
            in
            ''
              ${pythonPackages.python.interpreter} ${dropFrontendHook} pyproject.toml
            '';

          # No tests in this build — they need the [test] extra (pytest et
          # al) and a different invocation (see AGENTS.md). Run them via
          # devenv instead.
          doCheck = false;

          pythonImportsCheck = [ "klangk" ];
        };

        # Wrap the entry points so the runtime binaries klangkd shells
        # out to (podman, caddy, tmux, …) are on PATH. buildPythonApplication
        # already produces klangkd/klangk scripts; we re-wrap via
        # makeWrapper to augment PATH.
        klangkPkg = pkgs.symlinkJoin {
          name = "klangk-${klangkRaw.version}";
          paths = [ klangkRaw ];
          buildInputs = [ pkgs.makeWrapper ];
          postBuild = ''
            for bin in klangkd klangk; do
              wrapProgram "$out/bin/$bin" \
                --prefix PATH : ${pkgs.lib.makeBinPath runtimeBins}
            done
          '';
        };
      in
      {
        packages.default = klangkPkg;
        packages.klangkd = klangkPkg;
        packages.klangk = klangkPkg;

        apps.default = {
          type = "app";
          program = "${klangkPkg}/bin/klangkd";
        };

        apps.klangk = {
          type = "app";
          program = "${klangkPkg}/bin/klangk";
        };

        devShells.default = pkgs.mkShell {
          packages =
            runtimeBins
            ++ [ pkgs.python312 ]
            ++ (with pythonPackages; [
              hatchling
              hatch-vcs
            ]);
          shellHook = ''
            echo "klangk dev shell — Python ${pkgs.python312.version}, all runtime deps on PATH."
            echo "  pip install -e src/klangk   # editable install for hacking"
            echo "  klangkd                     # start the server"
          '';
        };
      }
    );
}
