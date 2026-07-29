"""``klangkd`` — the klangk server launcher (#1395, #1396, #1645).

Loads config (from a YAML file + env vars + built-in defaults, per the
precedence rules in :mod:`klangk.settings`), binds uvicorn (to a
UNIX domain socket when ``KLANGKD_LISTEN`` is a path, or a TCP host
otherwise), and owns the proxy child (currently nginx) that fronts it.

Usage::

    klangkd                          # resolves <KLANGKD_CONFIG_DIR>/klangkd.yaml;
    #                                # generates it on first run (#1645)
    klangkd --config /path/to/cfg.yaml
    klangkd --config=none            # env-vars-only (the sole opt-out)

Config-file resolution (three states, no implicit escape):

1. Bare ``klangkd`` → resolves ``$KLANGKD_CONFIG_DIR/klangkd.yaml`` (default
   ``~/.config/klangkd/klangkd.yaml``, #1649, #1646). If the file is missing it is
   **generated** as a near-empty template pointing at the docs (#1645) —
   no admin identity or password is emitted. The admin row is seeded at
   runtime: ``default_user`` defaults to ``<unixuser>@example.com`` with
   ``password_hash=None`` in ``none``/``oidc`` mode (no password needed);
   ``password``/``both`` mode requires ``KLANGKD_DEFAULT_PASSWORD`` (fail-fast
   if unset).
2. ``--config=<path>`` → that path required to exist; missing → error.
   Explicit paths are never auto-generated.
3. ``--config=none`` → run from env vars + built-in defaults (no file).

See #1392 (the design record), #1395 (config + launcher), #1396 (UDS +
proxy ownership), and #1645 (first-run generation) for the full rationale.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import typer
import uvicorn

# Import the logger module before settings so its module-level default
# configuration is active during ``KlangkSettings(...)`` construction
# (validators + the file:/cmd: indirection resolver log before any app
# exists). ``build_app``'s ``configure(settings)`` later overrides the
# level from ``KLANGKD_LOG_LEVEL`` (#1467). Imported as a statement (not
# ``from klangk import logger``) so the name ``logger`` stays free for the
# per-module Logger below — ``klangk.logger`` exposes no ``logger`` symbol
# (only ``configure`` / ``configure_defaults``), so the old
# ``from klangk.logger import logger`` in the error paths raised ImportError
# instead of logging and exiting (#1993).
import klangk.logger  # noqa: F401
from klangk import first_run
from klangk.settings import KlangkSettings

logger = logging.getLogger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Start the klangk server (config + uvicorn + proxy).",
)


def _resolve_config_path(config: str | None) -> str:
    """Resolve the ``--config`` value into a path or the 'none' sentinel.

    Three cases, no implicit escape (#1392 / #1645):

    - ``None`` (bare ``klangkd``, no ``--config``) → resolve the default path
      at ``<KLANGKD_CONFIG_DIR>/klangkd.yaml`` (default
      ``~/.config/klangkd/klangkd.yaml``). **Generate on first run** if the
      file doesn't exist (#1645): writes a near-empty template pointing at
      the docs. No admin identity or password is emitted — the admin row
      is seeded at runtime (``default_user`` defaults to
      ``<unixuser>@example.com``; null hash in ``none``/``oidc`` mode,
      ``KLANGKD_DEFAULT_PASSWORD`` required in ``password``/``both``).
    - ``"none"`` → explicit env-only opt-out (no config file).
    - ``"<path>"`` → that path, required to exist. Missing → ``BadParameter``.
      Explicit paths are never auto-generated — generation only fires for
      the implicit default.

    Returns the resolved path string or ``"none"``.  Raises
    ``typer.BadParameter`` (which Typer surfaces as a clean CLI error) on a
    missing explicitly-required file.
    """
    if config is None:
        path = first_run.default_config_path()
        if not os.path.isfile(path):
            try:
                first_run.generate_default_config(path)
            except FileExistsError:
                # Race: another klangkd (e.g. a systemd restart overlap)
                # generated the file between our isfile check and the open.
                # Treat it as "the file is there now" and proceed.
                pass
        return path
    if config == "none":
        return "none"
    path = Path(config)
    if not path.is_file():
        raise typer.BadParameter(
            f"Config file not found: {config}",
            param_hint="--config",
        )
    return str(path)


def _check_pid_preflight(settings: KlangkSettings) -> int | None:
    """Return the PID of a live klangkd for this instance, or ``None``.

    Mirrors :meth:`Util.check_pid_file` but runs *before* the app is
    built so the launcher can abort before touching the UDS (#1837).
    """
    # Read the instance ID the same way Util.resolve_instance_id does,
    # but read-only — don't generate one; if the file is missing there
    # is no running instance to collide with.
    instance_id_path = Path(settings.data_dir) / "instance-id"
    try:
        instance_id = instance_id_path.read_text().strip()
    except (FileNotFoundError, ValueError):
        return None
    if not instance_id:
        return None

    pid_path = Path(settings.state_dir) / f"klangk-{instance_id}.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        # Stale PID file — clean it up.
        pid_path.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid

    if pid == os.getpid():
        return None
    return pid


def _prepend_gnubin_paths() -> None:  # pragma: no cover
    """On macOS, prepend Homebrew gnubin dirs to ``PATH`` (#1947).

    macOS ships BSD ``du`` and ``tar`` whose flags are incompatible with
    klangkd's usage (``du -b``, ``tar --transform``).  Homebrew's
    ``coreutils`` and ``gnu-tar`` install GNU binaries under g-prefixed
    names (``gdu``, ``gtar``) and provide ``libexec/gnubin`` directories
    that shadow the BSD originals.

    This runs once at startup — before any subprocess is spawned — so
    every callsite (including bare ``subprocess.run(["du", ...])`` and
    the ``subprocess_env()``-based podman calls) inherits the fixed PATH.
    No-op on Linux or when ``brew`` is absent.
    """
    if platform.system() != "Darwin":
        return
    brew = shutil.which("brew")
    if not brew:
        return
    try:
        result = subprocess.run(
            [brew, "--prefix"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        prefix = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return
    if not prefix:
        return
    gnubin_dirs = [
        os.path.join(prefix, "opt", "coreutils", "libexec", "gnubin"),
        os.path.join(prefix, "opt", "gnu-tar", "libexec", "gnubin"),
    ]
    existing = [d for d in gnubin_dirs if os.path.isdir(d)]
    if existing:
        os.environ["PATH"] = (
            os.pathsep.join(existing) + os.pathsep + os.environ.get("PATH", "")
        )


@app.callback()
def main(  # pragma: no cover
    ctx: typer.Context,
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to a YAML config file. Bare ``klangkd`` (no --config) "
            "resolves ``$KLANGKD_CONFIG_DIR/klangkd.yaml`` "
            "(default ``~/.config/klangkd/klangkd.yaml``) and generates it "
            "on first run (#1645). Use 'none' to run from env vars only "
            "(no config file)."
        ),
    ),
) -> None:
    """Start the klangk server (config + uvicorn + proxy)."""
    _prepend_gnubin_paths()
    if ctx.invoked_subcommand is not None:
        return  # defer to the subcommand (e.g. ``doctor``)
    resolved = _resolve_config_path(config)

    # Everything below reads through the typed config (config file > env >
    # defaults, with file:/cmd: resolution), NOT raw os.environ — so a YAML
    # value or a ``file:``/``cmd:`` prefix takes effect the same as an env
    # var (#1394/#1395). Construction runs field validators (fail-fast on
    # bogus config) before uvicorn starts.
    settings = KlangkSettings(os.environ, config_file=resolved)

    # uvicorn always binds the UDS at ``settings.socket`` (default
    # ``<state_dir>/klangk.sock``, overridable via ``KLANGKD_SOCKET`` — #1542).
    # ``KLANGKD_PORT`` (unset ⇒ headless, set ⇒ full/browser) drives the proxy's
    # rendered template + listen directives; uvicorn never listens on TCP
    # directly.
    state_dir = settings.state_dir
    os.environ["KLANGKD_STATE_DIR"] = state_dir
    uds_path = settings.socket

    # Read ws_max_size through the typed config (default 16 MiB, #1394/#1395).
    ws_max_size = int(settings.websocket_msg_size_max)

    # Pre-flight PID check: abort *before* touching the UDS so a second
    # klangkd doesn't destroy the first instance's socket (#1837).
    # The lifespan has its own authoritative check, but that runs after
    # uvicorn binds — too late to protect the socket file.
    existing = _check_pid_preflight(settings)
    if existing is not None:
        logger.error(
            "Another klangk instance (PID %d) is already running — "
            "refusing to start",
            existing,
        )
        sys.exit(1)

    # Bind the UDS. A stale socket from a kill -9'd process makes the
    # bind fail with EADDRINUSE — unlink first (the pidfile guard in the
    # lifespan refuses a concurrent klangkd). Ensure the parent dir is
    # private (0700) so only the klangk user can open the socket — the
    # same-uid trust boundary _UDS_MODE relies on.
    try:
        os.unlink(uds_path)
    except FileNotFoundError:
        pass
    Path(uds_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Construct the app explicitly and pass the object to uvicorn (not a
    # ``module:app`` string import). This avoids the module-level
    # ``app = build_app()`` global — there's one ``build_app(settings)`` call,
    # one registry, wired correctly (#1464, #1454).
    from klangk.main import build_app  # noqa: allow-deferred-import

    asgi_app = build_app(settings)
    # Arm the UDS trust flag on the Util instance: over a UDS,
    # request.client is None, and a None peer is the trusted reverse
    # proxy (same-uid socket access). Set here, from the bind decision —
    # not via a config field (#1422 retired KLANGKD_UDS_MODE).
    asgi_app.state.util.set_uds_mode(True)
    try:
        uvicorn.run(
            asgi_app,
            uds=uds_path,
            # proxy_headers=False: over a UDS request.client is None; our
            # trust helpers handle header trust via _UDS_MODE. Letting uvicorn
            # also rewrite client would double-resolve.
            proxy_headers=False,
            ws_max_size=ws_max_size,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
    except OSError as exc:
        logger.error(
            "uvicorn failed to bind UDS at %s: %s — exiting", uds_path, exc
        )
        sys.exit(1)


@app.command()
def doctor(  # pragma: no cover
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show extra detail for each check."
    ),
) -> None:
    """Check for missing dependencies and common misconfigurations."""
    from klangk.doctor import doctor_main  # noqa: allow-deferred-import

    raise SystemExit(doctor_main(verbose=verbose))


if __name__ == "__main__":  # pragma: no cover
    app()
