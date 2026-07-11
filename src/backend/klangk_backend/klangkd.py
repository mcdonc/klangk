"""``klangkd`` — the klangk server launcher (#1395, #1396).

Loads config (from a YAML file + env vars + built-in defaults, per the
precedence rules in :mod:`klangk_backend.settings`), binds uvicorn (to a
UNIX domain socket when ``KLANGK_LISTEN`` is a path, or a TCP host
otherwise), and owns the nginx child that fronts it.

Usage::

    klangkd                          # requires /etc/klangkd.conf
    klangkd --config /path/to/cfg.yaml
    klangkd --config=none            # env-vars-only (the sole opt-out)

Config-file resolution (three states, no implicit escape):

1. Bare ``klangkd`` → requires ``/etc/klangkd.conf``; missing → error.
2. ``--config=<path>`` → that path required to exist; missing → error.
3. ``--config=none`` → run from env vars + built-in defaults (no file).

See #1392 (the design record), #1395 (config + launcher), and #1396 (UDS +
nginx ownership) for the full rationale.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn

from klangk_backend.settings import (
    classify_listen,
    get_settings,
    resolve_indirection,
    set_config_file,
    validate_at_startup,
)
from klangk_backend.util import set_uds_mode

# The default config-file location — a deployed klangkd finds its config here
# with no args.  See #1395.
DEFAULT_CONFIG_PATH = "/etc/klangkd.conf"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Start the klangk server (config + uvicorn + nginx).",
)


def _resolve_config_path(config: str) -> str:
    """Resolve the ``--config`` value into a path or the 'none' sentinel.

    Returns the path string (which the caller has verified exists), or
    ``"none"`` for the explicit opt-out.  Raises ``typer.BadParameter`` (which
    Typer surfaces as a clean CLI error) on a missing required file.
    """
    if config == "none":
        return "none"
    path = Path(config)
    if not path.is_file():
        raise typer.BadParameter(
            f"Config file not found: {config}",
            param_hint="--config",
        )
    return str(path)


def _default_state_dir() -> str:
    """The built-in state-dir default (env-only fallback, before config load).

    Only used to seed the default in the settings model; once config is loaded,
    ``get_settings().state_dir`` is the source of truth (config file > env >
    this default, with ``file:``/``cmd:`` resolution).
    """
    return (
        os.environ.get("KLANGK_STATE_DIR")
        or os.environ.get("DEVENV_STATE")
        or "/tmp/klangk-state"
    )


@app.command()
def main(  # pragma: no cover
    config: str = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help=(
            "Path to a YAML config file (default: /etc/klangkd.conf). "
            "Use 'none' to run from env vars only (no config file)."
        ),
    ),
) -> None:
    """Start the klangk server (uvicorn + nginx child)."""
    resolved = _resolve_config_path(config)

    # Seed the state_dir default into the env so the settings model can pick
    # it up as the lowest-priority default (config file > env > this). Done
    # before set_config_file so the YAML source sees a consistent env.
    os.environ.setdefault("KLANGK_STATE_DIR", _default_state_dir())

    # Set the config-file path on the settings module before anything reads
    # config. This wires the YAML source into the customise_sources chain.
    set_config_file(resolved)
    # Eagerly validate — a bogus config fails here, before uvicorn starts.
    validate_at_startup()

    # Everything below reads through the typed config (config file > env >
    # defaults, with file:/cmd: resolution), NOT raw os.environ — so a YAML
    # value or a ``file:``/``cmd:`` prefix takes effect the same as an env
    # var (#1394/#1395).
    settings = get_settings()
    state_dir = resolve_indirection(settings.state_dir) or _default_state_dir()
    # Re-pin state_dir into env so the lifespan watchdog (which reads the
    # merged config fresh) sees the same resolved value.
    os.environ["KLANGK_STATE_DIR"] = state_dir

    # ``KLANGK_LISTEN`` is polymorphic (#1422): a socket path or a TCP
    # TCP host or socket path. classify_listen picks the transport from its
    # shape; uvicorn
    # binds accordingly. The deployment shape is *derived* from listen's shape
    # + auth_modes — there is no amalgamated UI-mode setting.
    listen = resolve_indirection(settings.listen) or "127.0.0.1"
    transport = classify_listen(listen)

    # Read ws_max_size through the typed config (config file > env > default,
    # with file:/cmd:), not raw os.environ (#1394/#1395).
    ws_max_size = int(
        resolve_indirection(settings.ws_msg_size_max) or "16777216"
    )

    if transport == "socket":
        # Bind the UDS. A stale socket from a kill -9'd process makes the
        # bind fail with EADDRINUSE — unlink first (the pidfile guard in the
        # lifespan refuses a concurrent klangkd). Ensure the parent dir is
        # private (0700) so only the klangk user can open the socket — the
        # same-uid trust boundary _UDS_MODE relies on.
        try:
            os.unlink(listen)
        except FileNotFoundError:
            pass
        Path(listen).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Arm the internal _UDS_MODE trust flag (util.py): over a UDS,
        # request.client is None, and a None peer is the trusted reverse
        # proxy (same-uid socket access). Set here, from the bind decision —
        # not via a config field (#1422 retired KLANGK_UDS_MODE).
        set_uds_mode(True)
        uvicorn.run(
            "klangk_backend.main:app",
            uds=listen,
            # proxy_headers=False: over a UDS request.client is None; our
            # trust helpers handle header trust via _UDS_MODE. Letting uvicorn
            # also rewrite client would double-resolve.
            proxy_headers=False,
            ws_max_size=ws_max_size,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
    else:
        # TCP bind. LISTEN is the host (no port — port always comes from
        # KLANGK_PORT). No nginx ownership on this path (bare uvicorn /
        # direct-TCP tests); nginx is owned only in the UDS case.
        host = listen
        port = int(resolve_indirection(settings.port) or "8997")
        uvicorn.run(
            "klangk_backend.main:app",
            host=host,
            port=port,
            proxy_headers=False,
            ws_max_size=ws_max_size,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )


if __name__ == "__main__":  # pragma: no cover
    app()
