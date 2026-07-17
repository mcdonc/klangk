"""``klangkd`` — the klangk server launcher (#1395, #1396).

Loads config (from a YAML file + env vars + built-in defaults, per the
precedence rules in :mod:`klangk.settings`), binds uvicorn (to a
UNIX domain socket when ``KLANGK_LISTEN`` is a path, or a TCP host
otherwise), and owns the proxy child (currently nginx) that fronts it.

Usage::

    klangkd                          # requires /etc/klangkd.conf
    klangkd --config /path/to/cfg.yaml
    klangkd --config=none            # env-vars-only (the sole opt-out)

Config-file resolution (three states, no implicit escape):

1. Bare ``klangkd`` → requires ``/etc/klangkd.conf``; missing → error.
2. ``--config=<path>`` → that path required to exist; missing → error.
3. ``--config=none`` → run from env vars + built-in defaults (no file).

See #1392 (the design record), #1395 (config + launcher), and #1396 (UDS +
proxy ownership) for the full rationale.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn

# Import the logger module before settings so its module-level default
# configuration is active during ``KlangkSettings(...)`` construction
# (validators + the file:/cmd: indirection resolver log before any app
# exists). ``build_app``'s ``configure(settings)`` later overrides the
# level from ``KLANGK_LOG_LEVEL`` (#1467).
from klangk import logger  # noqa: F401
from klangk.settings import KlangkSettings

# The default config-file location — a deployed klangkd finds its config here
# with no args.  See #1395.
DEFAULT_CONFIG_PATH = "/etc/klangkd.conf"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Start the klangk server (config + uvicorn + proxy).",
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
    """Start the klangk server (uvicorn + proxy child)."""
    resolved = _resolve_config_path(config)

    # Everything below reads through the typed config (config file > env >
    # defaults, with file:/cmd: resolution), NOT raw os.environ — so a YAML
    # value or a ``file:``/``cmd:`` prefix takes effect the same as an env
    # var (#1394/#1395). Construction runs field validators (fail-fast on
    # bogus config) before uvicorn starts.
    settings = KlangkSettings(os.environ, config_file=resolved)

    # uvicorn always binds the UDS at ``settings.socket`` (default
    # ``<state_dir>/klangk.sock``, overridable via ``KLANGK_SOCKET`` — #1542).
    # ``KLANGK_PORT`` (unset ⇒ headless, set ⇒ full/browser) drives the proxy's
    # rendered template + listen directives; uvicorn never listens on TCP
    # directly.
    state_dir = settings.state_dir
    os.environ["KLANGK_STATE_DIR"] = state_dir
    uds_path = settings.socket

    # Read ws_max_size through the typed config (default 16 MiB, #1394/#1395).
    ws_max_size = int(settings.ws_msg_size_max)

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
    # not via a config field (#1422 retired KLANGK_UDS_MODE).
    asgi_app.state.util.set_uds_mode(True)
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


if __name__ == "__main__":  # pragma: no cover
    app()
