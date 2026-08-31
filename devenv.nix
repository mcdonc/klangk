{
  pkgs,
  config,
  lib,
  inputs,
  ...
}:
let
  # Flutter 3.47 from unstable (#2869): the pinned 26.05 channel ships
  # 3.41.9, too old for mcp_toolkit (>= 3.44) that the flutter-mcp-toolkit
  # integration (#2868) depends on. Pulled as a self-contained closure
  # (flutter + its dart SDK); the rest of the toolchain stays on 26.05.
  flutterUnstable =
    inputs.nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system}.flutter;
  # flutter-mcp-toolkit CLI (`fmtk`, #2868): lets agents inspect and drive
  # a debug-run of the frontend (semantic snapshots, taps/typing, hot
  # reload, logs). Not in nixpkgs and not on pub.dev — upstream ships
  # prebuilt Dart AOT binaries via GitHub Releases only. Pinned here as a
  # fixed-hash fetchurl derivation; plain glibc ELF on Linux (no patchelf
  # needed) and ad-hoc Mach-O on macOS. See AGENTS.md "Inspecting the
  # running frontend" for the workflow.
  flutterMcpToolkitVersion = "5.1.0";
  flutterMcpToolkit = pkgs.stdenv.mkDerivation {
    pname = "flutter-mcp-toolkit";
    version = flutterMcpToolkitVersion;
    src = pkgs.fetchurl {
      url = "https://github.com/Arenukvern/mcp_flutter/releases/download/v${flutterMcpToolkitVersion}/flutter_mcp_${flutterMcpToolkitVersion}_${
        if pkgs.stdenv.isDarwin then "darwin-arm64" else "linux-x64"
      }.tar.gz";
      hash =
        if pkgs.stdenv.isDarwin then
          "sha256-L8QhIYwJZz/X+lnQYY6ftW2qQD66cWGoutwdY0wGZaI="
        else
          "sha256-+3sC3DaXvWxgqvFpaqKwr1rS19JcCBPZ8zNC5gK8ko0=";
    };
    sourceRoot = ".";
    dontConfigure = true;
    dontBuild = true;
    # Dart AOT executables are ELF + an appended snapshot blob; strip would
    # truncate the snapshot and leave a bare dartaotruntime ("not an AOT
    # snapshot" on every invocation).
    dontStrip = true;
    installPhase = ''
      install -Dm555 -t $out/bin flutter_mcp_${flutterMcpToolkitVersion}_*/bin/*
    '';
  };
  # jscpd token-clone scanner (#2904): 5.x ships a prebuilt Rust binary
  # via platform-specific npm packages (esbuild-style), so it is not in
  # nixpkgs; pin the binary per platform with fixed hashes (the fmtk
  # pattern). One pinned version keeps clone reports reproducible across
  # contributors and CI. The `klangk:jscpd` task runs it over the backend.
  jscpdBinaryVersion = "5.0.16";
  jscpd = pkgs.stdenv.mkDerivation {
    pname = "jscpd";
    version = jscpdBinaryVersion;
    src = pkgs.fetchurl {
      url =
        if pkgs.stdenv.isDarwin then
          "https://registry.npmjs.org/jscpd-darwin-"
          + (if pkgs.stdenv.hostPlatform.darwinArch == "arm64" then "arm64" else "x64")
          + "/-/jscpd-darwin-"
          + (if pkgs.stdenv.hostPlatform.darwinArch == "arm64" then "arm64" else "x64")
          + "-${jscpdBinaryVersion}.tgz"
        else
          "https://registry.npmjs.org/jscpd-linux-x64-gnu/-/jscpd-linux-x64-gnu-${jscpdBinaryVersion}.tgz";
      hash =
        if pkgs.stdenv.isDarwin then
          (
            if pkgs.stdenv.hostPlatform.darwinArch == "arm64" then
              "sha256-vntXwMkns8HqtHwVzxthzun0tpRAe755YKB5k4c3Wqg="
            else
              "sha256-X2hK+EAgrXGRLUymdo5qgTuWoFQ3U0cz9J064UFQppM="
          )
        else
          "sha256-+6PhbDzUn0e4sQgsUs/kF0C5HlMOixZvlNolO9a4VdI=";
    };
    sourceRoot = ".";
    dontConfigure = true;
    dontBuild = true;
    dontStrip = true;
    installPhase = ''
      install -Dm555 -t $out/bin package/bin/jscpd
    '';
  };
  # klangkd binds a UDS and owns the Caddy reverse proxy as a child
  # (#1396, #1642); the old two-process layout (uvicorn + scripts/nginx.sh)
  # is collapsed into this single entry. Dev config lives in klangkd.yaml
  # (gitignored);
  # seeded from klangkd.yaml.devenv on first shell entry if missing.
  # Task, process, and script exec environments do NOT include the
  # uv-managed venv (.devenv/state/venv) on PATH — `python3` resolves to
  # the bare nix interpreter from languages.python.package, which carries
  # none of the project's dependencies. On 3.13 the ambient interpreter
  # happened to see some propagated nix packages (pyyaml, via pyaml), which
  # masked this for update-features; pinning 3.14 made it fail loudly.
  # Every exec that needs project deps invokes this interpreter explicitly.
  venvPython = config.devenv.state + "/venv/bin/python";
  backendCmd = ''
    ${venvPython} -m klangk.main --config="$DEVENV_ROOT/klangkd.yaml"
  '';
  featuresDir = config.devenv.root + "/.devenv/state/klangk/features";
  dataDir = config.devenv.root + "/.devenv/state/klangk/data";
  versionFile = config.devenv.state + "/klangk/version.json";
  # Browser (ingress) and container-egress ports — the proxy listens on both
  # (#1542). kill-port-holders frees both before startup.
  browserPort = "8997";
  egressPort = "8995";
  # Pytest plugin that surfaces the captured stdout/stderr of a subprocess
  # (podman) whose CalledProcessError fails a test or fixture setup — the e2e
  # helpers run podman with capture_output=True, so the runtime's actual error
  # ("error running container: ...") is otherwise invisible in CI logs. Not
  # loaded by default: test-backend-e2e exports PYTEST_PLUGINS + PYTHONPATH to
  # activate it only when KLANGK_E2E_VERBOSE_PODMAN=1 is set in the calling
  # environment (the self-hosted nix CI runner sets it in the workflow).
  podmanStderrPlugin = pkgs.writeTextDir "klangk_podman_stderr.py" ''
    """Print captured podman output when a CalledProcessError fails a test.

    Uses pytest_runtest_makereport (not pytest_exception_interact) because the
    latter does not fire inside xdist workers, and these suites run -n 2.
    """

    import subprocess

    import pytest


    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(item, call):
        rep = yield
        if not rep.failed or call.excinfo is None:
            return rep
        exc = call.excinfo.value
        if not isinstance(exc, subprocess.CalledProcessError):
            return rep
        # Append to the longrepr rather than writing to the terminal writer:
        # under xdist the worker's stdout is not forwarded at makereport time,
        # but the (serialized) longrepr is.
        extra = ["", "~" * 30 + " podman exit " + str(exc.returncode) + " " * 30]
        for label, data in (
            ("stdout", getattr(exc, "stdout", None)),
            ("stderr", getattr(exc, "stderr", None)),
        ):
            if data:
                extra.append("[podman {}] {}".format(label, data.strip()))
        rep.longrepr = "{}\n{}".format(rep.longrepr, "\n".join(extra))
        return rep
  '';
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
    # Pinned to the channel's python314 rather than the `python3` alias — the
    # toolchain version is a project decision (#2844), not an accident of
    # whichever minor the pinned nixpkgs channel defaults to (26.05 → 3.13).
    package = pkgs.python314;
    venv.enable = true;
    uv = {
      enable = true;
      # sync.enable left off: its gate only fingerprints the root
      # pyproject.toml (a bare [tool.uv.workspace] stub here), so it silently
      # skips and the venv goes stale. klangk:uv-sync below owns dependency sync.
    };
    directory = ".";
  };

  packages =
    with pkgs;
    [
      bash # explicit bash for shell scripts (CI /bin/sh may be dash)
      coreutils # GNU du -b + stat -f -c %T (macOS BSD lacks both)
      docker-client
      expect
      flutterMcpToolkit
      flutterUnstable
      git # "error: Failed to find git" during devenv:git-hooks:install
      gzip
      gnutar
      caddy # reverse-proxy engine (Caddy, sole engine in 2.X, #1559/#1642)
      jscpd # token-clone scanner (#2904), pinned rust binary (see above)
      podman
      ruff
      sqlite.bin
      rsync
      twine
      xenon # cyclomatic-complexity gate tool (#2828): same pinned build as the hook
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
          matchbox # kiosk WM for the demo video recorder (record-demo.sh)
          fuse-overlayfs # rootless per-workspace /nix via the plain-dir seed backend (#2219)
        ]
    );

  # Point Playwright at the nix-provided browsers. @playwright/test
  # (src/frontend/e2e-tests/package.json) is pinned to the version whose browser
  # revisions match these builds, so Playwright resolves each browser under
  # PLAYWRIGHT_BROWSERS_PATH on its own — playwright.config.ts no longer hardcodes
  # the revision numbers (#2182). SKIP_BROWSER_DOWNLOAD keeps `npm install` from
  # fetching its own (mismatched) copy; the nix build is the source of truth.
  # The enterShell block below fails fast if the @playwright/test chromium
  # revision ever drifts from the nix build (e.g. a nixpkgs playwright-driver
  # bump) — the symptom otherwise is a confusing "Executable doesn't exist" at
  # test time.
  env.PLAYWRIGHT_BROWSERS_PATH = pkgs.playwright-driver.browsers;
  env.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
  env.PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";

  tasks = {
    # WORKAROUND: devenv's languages.python.uv.sync gate only hashes the root
    # pyproject.toml (a bare [tool.uv.workspace] stub), so dependency changes in
    # workspace members or captured in uv.lock never invalidate the checksum and
    # `uv sync` is silently skipped -- the venv goes stale (e.g.
    # "ModuleNotFoundError: No module named 'jinja2'" at backend startup).
    #
    # Hooked into devenv:enterShell (runs before every shell, process, and test
    # activation), so deps stay current for `devenv shell`, `devenv test`,
    # pre-commit hooks, AND `devenv processes up` -- not just the backend process.
    # `after` pins ordering: devenv must create the venv
    # (devenv:python:virtualenv) first, or it would `rm -rf` our freshly-synced
    # deps on a cold / interpreter-changed venv.
    #
    # Deliberately NO execIfModified gate: the gate would fingerprint only the
    # source files (uv.lock + pyproject.toml), which is a necessary-but-not-
    # sufficient condition for a correct venv. devenv:python:virtualenv can wipe
    # the venv (cold start, interpreter bump) independent of any source change,
    # leaving an empty venv that the gate happily skips -- reproducing the
    # "No module named uvicorn" startup crash on `devenv processes up`. `uv sync`
    # is the source of truth: on a current venv it's a ~0.1s no-op
    # (resolve+check, no installs), so running it unconditionally is cheaper
    # than getting the gate right. Remove this whole task once devenv hashes
    # uv.lock AND re-runs sync after virtualenv recreation upstream
    # (poetry/npm/pnpm/yarn/bun already hash their lock files; uv is the lone
    # exception).
    "klangk:uv-sync" = {
      exec = ''
        cd "$DEVENV_ROOT"
        # --extra test: pull the pytest toolchain (klangk[test]) so the
        # venv runs the suite. Without it, uv sync installs only the
        # runtime deps (pytest* live in the ``test`` optional-dependency
        # extra, #1673) and `devenv test` / `pytest` blow up with
        # ModuleNotFoundError.
        uv sync --extra test -p "$UV_PYTHON"
      '';
      after = [ "devenv:python:virtualenv" ];
      before = [ "devenv:enterShell" ];
    };
    # The complexity gate as a one-word task (#2828): the exact hook
    # invocation over the exact hook file set, so `devenv shell -- xenon`
    # (ad hoc) and `pre-commit run xenon` (staged) can't drift. Note the
    # hook grades the STAGED subset; this task grades the whole tree —
    # stricter or equal, never more lenient.
    "klangk:xenon" = {
      exec = "xenon --max-absolute B --max-modules B --max-average B $(git ls-files 'src/klangk/klangk/*.py' 'src/klangksidecar/klangksidecar/*.py' 'scripts/*.py')";
    };
    # Token-clone scan of the backend (#2904): the same invocation the
    # consolidation issues used (--min-tokens 70). Advisory only — the
    # residual clones (frozen migrations, cross-boundary signatures,
    # pydantic field lists) are deliberate; see #2904 for the rationale.
    "klangk:jscpd" = {
      exec = "jscpd src/klangk/klangk --min-tokens 70 --silent";
    };
    "klangk:flutter-build" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/flutterbuildweb.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/flutterbuildweb.sh"
        "src/frontend/lib/**"
        "src/frontend/web/**"
        "src/frontend/pubspec.yaml"
        "src/frontend/pubspec.lock"
        # Key on feature *source* (checked-in), not the materialized payload —
        # flutterbuildweb.sh materializes into its own tempdir (#1660).
        "features/**/*.dart"
        "features.yaml"
      ];
    };
    "klangk:build-workspace-image" = {
      exec = ''exec bash "$DEVENV_ROOT/scripts/build-workspace-image.sh"'';
      after = [ "klangk:update-features" ];
      showOutput = true;
    };
    "klangk:build-network-sidecar" = {
      after = [ "klangk:update-features" ];
      exec = ''exec bash "$DEVENV_ROOT/scripts/build-network-sidecar.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/build-network-sidecar.sh"
        "src/containers/network/**"
        # The klangksidecar wheel (#2450): only the import package + its
        # pyproject (dep pins + wheel metadata) affect what gets baked into
        # the image. tests/ is excluded from the wheel automatically, and
        # uv.lock is not consulted by ``uv build`` -- keying on it would
        # rebuild the image on every unrelated workspace dep bump.
        "src/klangksidecar/klangksidecar/**"
        "src/klangksidecar/pyproject.toml"
      ];
    };
    # FIPS workspace image variant (#2570): validated-3.1.2-module wrapper
    # over the stock workspace image. Not in the backend's ``after`` — it's
    # an opt-in variant (KLANGKD_IMAGE_NAME=klangk-workspace-fips); building
    # it on every dev startup would add minutes for nobody. Rebuild keying
    # is the script + Dockerfile.fips only (the base image is an ARG, and
    # its own task already rebuilds when IT changes).
    "klangk:build-fips-image" = {
      after = [ "klangk:build-workspace-image" ];
      exec = ''exec bash "$DEVENV_ROOT/scripts/build-fips-image.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/build-fips-image.sh"
        "src/containers/workspace/Dockerfile.fips"
      ];
    };
    # FIPS host container variant (#2628): validated module + FIPS
    # workspace tar layered onto the stock host image. Opt-in like the
    # workspace variant; rebuilding on dev startup would rebuild the
    # whole host image chain for nobody. Rebuild keying is the script +
    # host Dockerfile.fips only.
    "klangk:build-fips-host-image" = {
      # No klangk:build-host-image task exists (the host image builds via
      # the `build-host-image` script — it needs docker + the flutter web
      # bundle, not part of task startup); the script checks for it and
      # fails with build instructions when absent.
      after = [ "klangk:build-fips-image" ];
      exec = ''exec bash "$DEVENV_ROOT/scripts/build-fips-host-image.sh"'';
      showOutput = true;
      execIfModified = [
        "scripts/build-fips-host-image.sh"
        "src/containers/host/Dockerfile.fips"
      ];
    };
    "klangk:kill-port-holders" = {
      exec = ''
        if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
          for port in ${browserPort} ${egressPort}; do
            fuser -k "$port/tcp" 2>/dev/null || true
          done
        fi
      '';
    };
    "klangk:update-features" = {
      exec = ''
        cd $DEVENV_ROOT
        bash scripts/stub_dart_features.sh
        exec ${venvPython} scripts/update_features.py --payload-dir "${featuresDir}"
      '';
      before = [ "klangk:flutter-build" ];
      showOutput = true;
      execIfModified = [
        # The declaration lives at the repo root now (#1660); the materialized
        # payload under ``featuresDir`` is derived from it + features/*/.
        "features.yaml"
        "features/**/*.dart"
        "features/*/package.json"
        "features/*/klangk/pubspec.yaml"
      ];
    };
  };

  processes = {
    backend = {
      exec = ''
        cd $DEVENV_ROOT/src/klangk && exec ${backendCmd}
      '';
      after = [
        "klangk:flutter-build"
        "klangk:build-workspace-image"
        "klangk:build-network-sidecar"
        "klangk:kill-port-holders"
      ];
    };
  };

  env.SOURCE_DATE_EPOCH = "";
  env.UV_PYTHON = venvPython;

  # --- Devenv-only env vars (used by shell hooks and scripts, NOT by the
  # backend — backend config lives in klangkd.yaml). ---

  # Rootless podman from nix (Linux) ships no default policy.json, so a
  # build/pull fails with "no policy.json file found". enterShell generates a
  # permissive one at this path, and the build/pull scripts consume this var
  # and pass it to podman via `--signature-policy`. NOTE: podman's
  # build/pull/push path does NOT read an env var for the policy (the
  # --signature-policy flag, which sets SystemContext.SignaturePolicyPath, is
  # the only way to point it at a non-default file). On macOS podman runs in
  # *remote* mode against the VM, which has its own policy, so leave this empty
  # there.
  env.CONTAINERS_SIGNATURE_POLICY = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isDarwin then
      ""
    else
      config.devenv.state + "/klangk/podman/policy.json"
  );
  # Same story for registries.conf (#286): rootless podman from nix ships
  # none, so any build whose Dockerfile uses a short image name (alpine:3.21,
  # python:3.14-slim, debian:trixie-slim, node:26-slim) fails with "short-name
  # ... did not resolve to an alias and no containers-registries-conf(5) was
  # found". Unlike the policy, podman DOES read this env var directly (no CLI
  # flag needed), so every podman invocation in the shell — including ones
  # that don't go through scripts/_podman_common.sh — picks it up. enterShell
  # seeds the file; _podman_common.sh re-creates it as a safety net for direct
  # script invocation (podman hard-fails when the var points at a missing
  # file). On macOS podman builds inside its VM, which ships its own
  # registries.conf, so leave this empty there.
  env.CONTAINERS_REGISTRIES_CONF = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isDarwin then
      ""
    else
      config.devenv.state + "/klangk/podman/registries.conf"
  );
  env.KLANGKD_VERSION_FILE = versionFile;
  # state_dir / frontend_dir live in klangkd.yaml (cmd:-indirected to
  # $DEVENV_STATE / $DEVENV_ROOT) — see klangkd.yaml.devenv. IMAGE_NAME and
  # VERSION_FILE stay as env because the build scripts
  # (build-workspace-image.sh, build-host-image.sh) read them and don't parse
  # the YAML (#1788).
  # Docker build platform for klangk images. On Linux, default to the host
  # architecture so arm64 machines build/run natively instead of under amd64
  # emulation. The published GHCR base (klangk-workspace-base, pulled by the
  # digest pinned in the workspace Dockerfile, #2063) is
  # multi-arch (amd64 + arm64), so we default to the host's native
  # architecture on all platforms. Override via devenv.local.nix.
  env.KLANGKBUILD_PLATFORM = lib.mkOverride 1500 (
    if pkgs.stdenv.hostPlatform.isAarch64 then "linux/arm64" else "linux/amd64"
  );
  # NOTE: KLANGKD_IMAGE_NAME is deliberately NOT set via env.* — the
  # devenv env.* export would clobber an externally exported value, and
  # running the stack against a variant image (e.g. the FIPS build, #2570)
  # must be possible with `KLANGKD_IMAGE_NAME=... devenv shell -- ...`
  # without editing nix. The default is seeded in enterShell with the
  # "default only when unset" pattern; a devenv.local.nix env.* override
  # still wins (exported before enterShell runs).
  env.KLANGKD_NETWORK_SIDECAR_IMAGE = lib.mkOverride 1500 "klangk-network-sidecar";

  scripts.flutterbuildweb.exec = ''exec bash "$DEVENV_ROOT/scripts/flutterbuildweb.sh" "$@"'';
  scripts.build-workspace-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-workspace-image.sh" "$@"'';
  scripts.build-fips-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-fips-image.sh" "$@"'';
  scripts.build-fips-host-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-fips-host-image.sh" "$@"'';
  scripts.pull-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/pull-base-image.sh" "$@"'';
  scripts.push-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/push-base-image.sh" "$@"'';
  scripts.build-base-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-base-image.sh" "$@"'';
  scripts.build-host-image.exec = ''exec bash "$DEVENV_ROOT/scripts/build-host-image.sh" "$@"'';
  # Live, rich-rendered view of a workspace's egress-consent history (#2242).
  # The dev klangkd's DB (klangk.db) lives under $dataDir.
  scripts.consent-watch.exec = ''
    exec ${venvPython} "$DEVENV_ROOT/scripts/consent-watch.py" --data-dir "${dataDir}" "$@"
  '';
  # Interactive accept/deny of a workspace's pending consent requests (#2242).
  scripts.consent-decide.exec = ''
    exec ${venvPython} "$DEVENV_ROOT/scripts/consent-decide.py" --data-dir "${dataDir}" "$@"
  '';
  scripts.trivy-host.exec = ''exec bash "$DEVENV_ROOT/scripts/trivy-host.sh" "$@"'';
  scripts.trivy-workspace.exec = ''exec bash "$DEVENV_ROOT/scripts/trivy-workspace.sh" "$@"'';
  scripts.trivy-workspace-report.exec = ''
    cd $DEVENV_ROOT
    if [ "$#" -eq 0 ]; then
      echo "Scanning workspace image and rendering no-fix report..." >&2
      exec bash "$DEVENV_ROOT/scripts/trivy-workspace.sh" --severity CRITICAL,HIGH --format json \
        | ${venvPython} "$DEVENV_ROOT/scripts/trivy-report-nofix.py" -
    fi
    exec ${venvPython} "$DEVENV_ROOT/scripts/trivy-report-nofix.py" "$@"'';

  scripts.update-features.exec = ''
    cd $DEVENV_ROOT
    ${venvPython} scripts/update_features.py "$@"
  '';

  # -n auto: run tests in parallel across CPUs (pytest-xdist)
  # Runs both unit suites (server + client) in one invocation. The single
  # --cov gate covers the klangk package (#1606). The two
  # dirs share rootdir = src/klangk (the pyproject there carries addopts).
  scripts.test-backend.exec = ''
    cd $DEVENV_ROOT
    # scripts/tests (the build-pipeline contract tests) rides along so a
    # local run catches guard-test breakage the way CI's separate
    # "build-pipeline tests" step does (#2629) — the two klangk suites
    # alone missed it.
    exec ${venvPython} -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests scripts/tests \
      -v -n auto "$@"
  '';

  # CLI unit tests only — scoped run for iterating on the client without
  # the server corpus (#1606).
  scripts.test-cli.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangk/klangkc-tests/tests -v -n auto "$@"
  '';

  # Network sidecar unit suite — the standalone klangksidecar package
  # (#2450). Separate invocation (not folded into test-backend) because it
  # has its own rootdir (src/klangksidecar/pyproject.toml) with its own
  # coverage gate (branch coverage, 100% — #2834; the PID-1-only entry
  # paths carry pragmas, exercised instead by the real-podman e2e). CI
  # mirrors this via .github/workflows/sidecar-tests.yml.
  scripts.test-sidecar.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangksidecar/tests -v -n auto "$@"
  '';

  # Both unit suites, no coverage gate — the fast "does it all pass?"
  # smoke.
  scripts.test-unit.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests \
      -v -n auto --no-cov "$@"
  '';

  # Scoped run: re-run only tests whose coverage touches changed source
  # lines (pytest-testmon). Inert on CI — CI runs the full suite via
  # test-backend; this is the local tight-loop accelerator (#2288).
  # First run on a clean tree baselines the line->test map into
  # src/klangk/.testmondata (~the same cost as test-unit); after that, a
  # one-file edit re-runs just the affected subset (~10s vs ~60s). Delete
  # src/klangk/.testmondata after a large refactor / branch switch to
  # re-baseline.
  scripts.testmon.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests \
      -v -n auto --no-cov --testmon "$@"
  '';

  # Fast local pre-push gate (#2727): diff the working tree against the
  # merge-base with origin/main and run only the suites whose area
  # changed (testmon for the klangk package, scripts/tests for the build
  # path, sidecar unit, flutter unit). CI stays authoritative — the full
  # test-backend / test-frontend runs are the pre-merge check, not the
  # pre-push one. Logic lives in scripts/test-push.sh (TEST_PUSH_BASE
  # overrides the base ref).
  scripts.test-push.exec = ''exec bash "$DEVENV_ROOT/scripts/test-push.sh"'';

  # CLI E2E tests: start real server, run klangk commands.
  # Free-allocated ports + instance-scoped cleanup (#1393) make xdist
  # safe with --dist=loadscope. Capped at 2 workers to limit podman
  # contention (TUI and terminal-windows tests are latency-sensitive). #2059
  scripts.test-cli-e2e.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangk/klangkc-tests/e2e-tests \
      -v --no-cov -n 2 --dist=loadscope "$@"
  '';

  scripts.test-terminal-windows-e2e.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangk/klangkc-tests/e2e-tests/test_terminal_windows_e2e.py \
      -v --no-cov "$@"
  '';

  # Backend E2E tests: start real server, run backend E2E tests.
  # Free-allocated ports + instance-scoped cleanup (#1393) make xdist
  # safe with --dist=loadscope. Capped at 2 workers: higher counts
  # cause flaky failures in container-heavy tests (ssh-agent,
  # service-command) due to podman resource contention. #2059
  scripts.test-backend-e2e.exec = ''
    cd $DEVENV_ROOT
    if [ -n "''${KLANGK_E2E_VERBOSE_PODMAN:-}" ]; then
      export PYTEST_PLUGINS=klangk_podman_stderr
      export PYTHONPATH="${podmanStderrPlugin}''${PYTHONPATH:+:$PYTHONPATH}"
    fi
    exec ${venvPython} -m pytest src/klangk/klangkd-tests/e2e-tests \
      -v --no-cov -n 2 --dist=loadscope "$@"
  '';

  # Super-E2E (#2561): black-box feature tests against the real Docker
  # host appliance (supervisord + klangkd + nested rootless podman).
  # Requires docker + a built host image (`build-host-image`; override
  # the image with KLANGK_SUPER_E2E_IMAGE). Serial on purpose: one
  # appliance serves the whole session — xdist workers would each boot
  # their own and starve the runner.
  scripts.test-super-e2e.exec = ''
    cd $DEVENV_ROOT
    exec ${venvPython} -m pytest src/klangk/klangkd-tests/super-e2e \
      -v --no-cov "$@"
  '';

  # Run the whole corpus as concurrently as is safe (#1393): the unit
  # suites combine into one parallel invocation (test-unit), then the
  # e2e suites run with xdist (--dist=loadscope, 2 workers each).
  # Requires podman + a built workspace image for the e2e steps
  # (klangk:build-workspace-image). Passes through args to the e2e
  # invocations only.
  scripts.test-all.exec = ''
    cd $DEVENV_ROOT
    set -e
    echo "=== unit (server + client, parallel) ==="
    ${venvPython} -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests \
      -v -n auto --no-cov "$@"
    echo "=== sidecar unit ==="
    ${venvPython} -m pytest src/klangksidecar/tests -v -n auto --no-cov "$@"
    echo "=== server e2e ==="
    ${venvPython} -m pytest src/klangk/klangkd-tests/e2e-tests \
      -v --no-cov -n 2 --dist=loadscope "$@"
    echo "=== client e2e ==="
    ${venvPython} -m pytest src/klangk/klangkc-tests/e2e-tests \
      -v --no-cov -n 2 --dist=loadscope "$@"
    echo "=== all green ==="
  '';

  scripts.test-frontend-e2e.exec = ''
    cd $DEVENV_ROOT
    devenv tasks run klangk:flutter-build klangk:build-workspace-image
    cd src/frontend/e2e-tests
    npm install --silent

    # Fail fast if @playwright/test's browser revisions don't match the nix
    # playwright-driver.browsers (PLAYWRIGHT_BROWSERS_PATH) — otherwise the
    # suite fails per-test with "Executable doesn't exist". See #2182.
    if [ -n "''${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
      _pwrev=$(python3 -c "import json;print(next(b['revision'] for b in json.load(open('node_modules/playwright-core/browsers.json'))['browsers'] if b['name']=='chromium'))" 2>/dev/null || true)
      if [ -n "$_pwrev" ] && [ ! -d "$PLAYWRIGHT_BROWSERS_PATH/chromium-$_pwrev" ]; then
        echo "ERROR: @playwright/test expects chromium-$_pwrev, which is not under PLAYWRIGHT_BROWSERS_PATH ($PLAYWRIGHT_BROWSERS_PATH)." >&2
        echo "Bump @playwright/test in package.json to the version matching nixpkgs playwright-driver.browsers, then 'npm install'. See #2182." >&2
        exit 1
      fi
    fi

    exec npx playwright test --reporter=list "$@"
  '';

  # Bare `playwright` command that always uses the LOCAL binary pinned in
  # src/frontend/e2e-tests/package.json (@playwright/test 1.59.1). Use this
  # instead of `npx playwright`, which resolves to a newer cached version
  # (1.60.x) and fails with "two different versions of @playwright/test".
  # All extra args are forwarded. e.g.
  #   devenv shell -- playwright test \
  #     --config=src/frontend/e2e-tests/demo/playwright.demo.config.ts -g clanker
  scripts.playwright.exec = ''
    local_pw="$DEVENV_ROOT/src/frontend/e2e-tests/node_modules/.bin/playwright"
    if [ ! -x "$local_pw" ]; then
      echo "error: local Playwright not found at $local_pw" >&2
      echo "       run 'cd src/frontend/e2e-tests && npm install' first" >&2
      exit 1
    fi
    exec "$local_pw" "$@"
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

  # fmtk harness (#2881): one command to a debug frontend driven against a
  # live backend — scratch klangkd + origin-splitting caddy + `flutter run
  # --debug -d chrome` + the fixture matrix, printing the VM-service URI.
  # See scripts/fmtk-up.sh and AGENTS.md "Inspecting the running frontend".
  scripts.fmtk-up.exec = ''exec bash "$DEVENV_ROOT/scripts/fmtk-up.sh"'';
  # Stop the harness services fmtk-up keeps alive (backend + proxy);
  # --wipe also deletes the scratch state.
  scripts.fmtk-down.exec = ''exec bash "$DEVENV_ROOT/scripts/fmtk-down.sh" "$@"'';
  # Idempotent fixture seeding (also run by fmtk-up): sharer/acler/viewer
  # users + the fmtk-verify workspace against a running backend.
  scripts.fmtk-seed.exec = ''exec ${venvPython} "$DEVENV_ROOT/scripts/fmtk-seed.py" "$@"'';

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
    # Python: cyclomatic-complexity gate (#2794). Blocks must stay rank B
    # or better (complexity <= 10) — the F/E/D legacy blocks were refactored
    # down (#2800-#2803, #2808-#2814), the C-ranked ones in the B-ratchet
    # (#2818-#2842), so the excludes are gone — every production .py file is
    # checked (klangkd + CLI under src/klangk/klangk/, the network sidecar
    # under src/klangksidecar/klangksidecar/, and scripts/).
    # Module and codebase averages are also gated at B (#2846): --max-modules
    # grades each staged module on its own average, so partial staging cannot
    # cause false failures; --max-average used to flap pre-ratchet (a global
    # average over only the staged files), but with every block <= 10 no
    # subset's average can exceed 10, so B-level flapping is impossible. That
    # returns only if averages are ratcheted to A — then grade the full tree
    # (pass_filenames = false) instead of the staged subset.
    xenon = {
      enable = true;
      name = "xenon";
      entry = "${pkgs.xenon}/bin/xenon --max-absolute B --max-modules B --max-average B";
      files = "^src/klangk/klangk/.*\\.py$|^src/klangksidecar/klangksidecar/.*\\.py$|^scripts/.*\\.py$";
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
        # Jinja2 email templates: prettier doesn't understand {% %}/{{ }} and
        # corrupts them (breaks expressions across lines). See #1165.
        "email_templates/"
        # Deployer copies of the above (customize/ template tree).
        "customize/custom/email-templates/"
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
    # Guard against UTF-8-lossy rewrites that corrupt binary assets (#1734):
    # a text-mode find-and-replace (errors='replace') collapses invalid bytes
    # to U+FFFD and destroys wasm/font/image files (the bundled libghostty
    # wasm + a font were mangled by the "plugin"->"feature" sweep, crashing
    # WebAssembly.instantiate at app boot and hanging every e2e test). Runs on
    # every commit (always_run) and inspects staged-vs-HEAD itself, so it sets
    # pass_filenames=false and ignores the files/types filters.
    binary-integrity = {
      enable = true;
      name = "binary-integrity";
      entry = "python3 scripts/check_binary_integrity.py";
      language = "system";
      pass_filenames = false;
      always_run = true;
    };
  };

  enterShell = ''
    # Default workspace image name — here (not env.*) so an externally
    # exported KLANGKD_IMAGE_NAME survives into the shell (#2570 FIPS
    # variant runs `KLANGKD_IMAGE_NAME=klangk-workspace-fips devenv shell`).
    export KLANGKD_IMAGE_NAME="''${KLANGKD_IMAGE_NAME:-klangk-workspace}"

    if [ ! -f "$DEVENV_ROOT/klangkd.yaml" ]; then
      cp "$DEVENV_ROOT/klangkd.yaml.devenv" "$DEVENV_ROOT/klangkd.yaml"
      echo "Created klangkd.yaml from klangkd.yaml.devenv — edit it to taste."
    fi
    # Ensure the frontend_dir key exists in an existing klangkd.yaml. devenv
    # used to export KLANGKD_FRONTEND_DIR; it now lives in klangkd.yaml (#1788),
    # but enterShell only seeds the file when missing, so older checkouts lack
    # the key and klangkd would fall back to the absent in-package default
    # (API-only). Append it (non-clobbering) if the key is absent.
    if [ -f "$DEVENV_ROOT/klangkd.yaml" ] \
      && ! grep -qE '^[[:space:]]*frontend_dir:' "$DEVENV_ROOT/klangkd.yaml"; then
      printf '\nfrontend_dir: "cmd:echo $DEVENV_ROOT/src/frontend/build/web"\n' \
        >> "$DEVENV_ROOT/klangkd.yaml"
      echo "Added frontend_dir to klangkd.yaml (#1788)."
    fi

    mkdir -p "${dataDir}"

    # Generate version file (used by update_features.py and /version endpoint)
    mkdir -p "$(dirname "${versionFile}")"
    bash "$DEVENV_ROOT/scripts/generate-version.sh" > "${versionFile}"

    # Podman uses its default storage (~/.local/share/containers/).
    # To customize, create ~/.config/containers/storage.conf.
    # See docs/reference/podman.md.
    _PODMAN_CONF="$DEVENV_STATE/klangk/podman"
    mkdir -p "$_PODMAN_CONF"
    if [ ! -f "$_PODMAN_CONF/policy.json" ]; then
      echo '{"default": [{"type": "insecureAcceptAnything"}]}' \
        > "$_PODMAN_CONF/policy.json"
    fi
    if [ ! -f "$_PODMAN_CONF/registries.conf" ]; then
      echo 'unqualified-search-registries = ["docker.io"]' \
        > "$_PODMAN_CONF/registries.conf"
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


    # Ensure klangk_features stub exists so flutter commands work immediately
    # in any shell session (not just after devenv up). The script is idempotent
    # and skips if pubspec_overrides.yaml already exists.
    bash "$DEVENV_ROOT/scripts/stub_dart_features.sh"

    # Generate prettierignore (not committed)
    cat > "$DEVENV_ROOT/.prettierignore" <<'PRETTIER'
    node_modules/
    src/frontend/build/
    .devenv/
    *.lock
    # Jinja2 email templates — prettier corrupts {% %}/{{ }} syntax. See #1165.
    email_templates/
    # Deployer copies of the above (customize/ template tree).
    customize/custom/email-templates/
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
    MD024:
      # Allow Keep a Changelog's repeated per-version section headings
      # (### Fixed / ### Changed under each ## version).
      siblings_only: true
    MD034: false
    MD060: false
    MDLINT

    # Playwright browser drift guard (#2182): @playwright/test's expected
    # chromium revision must exist under the nix PLAYWRIGHT_BROWSERS_PATH, or
    # every browser-launching test fails with "Executable doesn't exist". Only
    # checked once node_modules is present, so a first shell entry is unaffected.
    # test-frontend-e2e hard-fails on the same drift; this is the early warning.
    if [ -n "''${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
      _pwbj="$DEVENV_ROOT/src/frontend/e2e-tests/node_modules/playwright-core/browsers.json"
      if [ -f "$_pwbj" ]; then
        _pwrev=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(next(b['revision'] for b in d['browsers'] if b['name']=='chromium'))" "$_pwbj" 2>/dev/null || true)
        if [ -n "$_pwrev" ] && [ ! -d "$PLAYWRIGHT_BROWSERS_PATH/chromium-$_pwrev" ]; then
          echo "WARNING (@playwright/test vs nix browsers): @playwright/test expects chromium-$_pwrev, which is not under PLAYWRIGHT_BROWSERS_PATH ($PLAYWRIGHT_BROWSERS_PATH). Bump @playwright/test in src/frontend/e2e-tests/package.json to the version matching nixpkgs playwright-driver.browsers, then 'cd src/frontend/e2e-tests && npm install'. See #2182." >&2
        fi
      fi
    fi
  '';

  claude.code.mcpServers = { };
}
