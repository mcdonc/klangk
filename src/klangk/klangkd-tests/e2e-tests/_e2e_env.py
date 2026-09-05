"""Shared hermetic env helper for E2E test suites (#1526).

Every E2E suite that launches a subprocess (``runtestserver.py``, ``klangkd``,
or ``klangk``) must build the child's env from :func:`clean_env`, **not**
``{**os.environ, ...}``. A stray ``KLANGKD_*`` var in the CI runner's env
(or one leaked by a prior test) silently becomes the child's config and can
change test results — ``clean_env`` strips all config-affecting prefixes so
the child sees only what the test explicitly sets.

Strips (case-insensitive prefix match): ``KLANGK``, ``_KLANGK``,
``KLANGKC``, ``LOGFIRE``. OS-essential vars (``PATH``, ``HOME``, Nix-specific
``LOCALE_ARCHIVE`` / ``NIX_LD`` / etc.) are preserved so the subprocess can
actually run.
"""

from __future__ import annotations

import os
from subprocess import Popen


def close_popen_pipes(proc: Popen) -> None:
    """Close a Popen's captured stdout/stderr pipes.

    E2E server fixtures capture the child's combined output on a
    ``stdout=PIPE`` pipe to surface its log on failure. The
    ``_io.BufferedReader`` that backs ``proc.stdout`` is *not* closed by
    ``proc.kill()/wait()``; leaving it open leaks an fd until GC, which
    surfaces as ``ResourceWarning: unclosed file`` in the suite's
    warnings summary (#1493). Call this at the end of every fixture's
    teardown after the log has been drained.
    """
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


# Prefixes whose vars are stripped: any env var starting with one of these
# (case-insensitive) is config or debug state that must not leak from the
# ambient env into a test subprocess.
_STRIP_PREFIXES = ("KLANGK", "_KLANGK", "KLANGKC", "LOGFIRE")

# Build-infra vars that locate *artifacts the test must use* (the workspace
# container image, the version stamp) — their values are produced by devenv's
# ``klangk:build-workspace-image`` task, not by any test, and every E2E server
# subprocess needs the real ones. These are forwarded from the ambient env
# deliberately (not stripped) so the server finds the built image.
# They are not test config — overriding one in a ``clean_env(...)`` call
# still wins.
#
# KLANGKD_FRONTEND_DIR is NOT here. Devenv no longer exports it (#1788; it's
# a klangkd.yaml setting), so each env-only launcher sets it explicitly
# (_e2e_server.start_server, global-setup.ts, run-demo-backend.sh). Forwarding
# a stray ambient value would risk pointing at a stale/wrong build.
_INFRA_VARS = (
    "KLANGKD_IMAGE_NAME",
    "KLANGKD_VERSION_FILE",
)


def clean_env(**overrides: str) -> dict[str, str]:
    """Return a hermetic env dict for a test subprocess.

    Starts from ``os.environ`` with every config-affecting var stripped, then
    applies ``overrides`` (the test's explicit KLANGKD_* / LOGFIRE_* keys).
    The baseline includes ``_KLANGKD_DISABLE_PROXY=1`` and
    ``KLANGKD_AUTH_MODES=password`` (the E2E default — most suites exercise the
    password auth flow); pass ``KLANGKD_AUTH_MODES="none"`` in overrides to
    opt into no-auth mode.

    Tests should call::

        env = clean_env(
            KLANGKD_PORT=port,
            KLANGKD_DATA_DIR=data_dir,
            ...
        )
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(_STRIP_PREFIXES)
    }
    # Forward the build-infra vars (image / features / version stamp) so the
    # server subprocess finds the artifacts devenv built. See _INFRA_VARS.
    for name in _INFRA_VARS:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    # If the caller pinned HOME, also pin XDG_CONFIG_HOME / XDG_STATE_HOME to
    # under it — unless the caller set them explicitly. Without this, the
    # inherited runner env (GitHub Actions ubuntu runners set
    # XDG_CONFIG_HOME=/home/runner/.config in /etc/environment; the value is
    # not re-expanded per-process) leaks past the HOME override, and any code
    # that correctly reads the XDG var (per spec) writes outside the tmpdir
    # the test set up. The CLI's config/state resolution (post-#1646) and the
    # server's (post-#1644) both honor these vars.
    if "HOME" in overrides and "XDG_CONFIG_HOME" not in overrides:
        env["XDG_CONFIG_HOME"] = f"{overrides['HOME']}/.config"
    if "HOME" in overrides and "XDG_STATE_HOME" not in overrides:
        env["XDG_STATE_HOME"] = f"{overrides['HOME']}/.local/state"
    # E2E baseline defaults.
    env["_KLANGKD_DISABLE_PROXY"] = "1"
    env.setdefault("KLANGKD_AUTH_MODES", "password")
    # #3157: per-client-IP API rate limiting is default-on in production
    # (300/60s). E2E suites generate machine-speed request bursts from one
    # loopback IP that would trip it nondeterministically; the limit itself
    # is exercised explicitly (test_api_rate_limit_e2e.py boots its own
    # server with a tiny budget).
    env.setdefault("KLANGKD_API_RATE_LIMIT", "0")
    # #3064: child CLI/TUI processes get the widened WS-connect wait on CI
    # to match the bring-up budgets (the strip above removes any ambient
    # value, so this stamp is the only way it reaches the child).
    if os.environ.get("CI"):
        env.setdefault("KLANGKC_WS_CONNECT_TIMEOUT", "240")
    env.update(overrides)
    return env


def ci_budget(default: float, ci: float) -> float:
    """Load-aware E2E budget, widened on CI (#3064).

    The four E2E suites share one runner VM; under that storage/IO
    contention a real podman bring-up (create/start/readiness) can
    outrun a tight local-dev budget — the observed failures were
    client-side 60s caps blowing while unrelated tests passed. Same
    shape as the frontend's container-ready doubling (#2745). Local
    runs keep the snappier default.
    """
    return ci if os.environ.get("CI") else default
