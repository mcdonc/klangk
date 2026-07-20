"""``klangkd`` — the klangk server launcher (#1395, #1396, #1645).

Loads config (from a YAML file + env vars + built-in defaults, per the
precedence rules in :mod:`klangk.settings`), binds uvicorn (to a
UNIX domain socket when ``KLANGK_LISTEN`` is a path, or a TCP host
otherwise), and owns the proxy child (currently nginx) that fronts it.

Usage::

    klangkd                          # resolves <KLANGK_CONFIG_DIR>/klangkd.yaml;
    #                                # generates it on first run (#1645)
    klangkd --config /path/to/cfg.yaml
    klangkd --config=none            # env-vars-only (the sole opt-out)

Config-file resolution (three states, no implicit escape):

1. Bare ``klangkd`` → resolves ``$KLANGK_CONFIG_DIR/klangkd.yaml`` (default
   ``~/.config/klangkd/klangkd.yaml``, #1649, #1646). If the file is missing it is
   **generated** as a near-empty template pointing at the docs (#1645) —
   no admin identity or password is emitted. The admin row is seeded at
   runtime: ``default_user`` defaults to ``<unixuser>@example.com`` with
   ``password_hash=None`` in ``none``/``oidc`` mode (no password needed);
   ``password``/``both`` mode requires ``KLANGK_DEFAULT_PASSWORD`` (fail-fast
   if unset).
2. ``--config=<path>`` → that path required to exist; missing → error.
   Explicit paths are never auto-generated.
3. ``--config=none`` → run from env vars + built-in defaults (no file).

See #1392 (the design record), #1395 (config + launcher), #1396 (UDS +
proxy ownership), and #1645 (first-run generation) for the full rationale.
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
from klangk import first_run
from klangk.settings import KlangkSettings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Start the klangk server (config + uvicorn + proxy).",
)


def _resolve_config_path(config: str | None) -> str:
    """Resolve the ``--config`` value into a path or the 'none' sentinel.

    Three cases, no implicit escape (#1392 / #1645):

    - ``None`` (bare ``klangkd``, no ``--config``) → resolve the default path
      at ``<KLANGK_CONFIG_DIR>/klangkd.yaml`` (default
      ``~/.config/klangkd/klangkd.yaml``). **Generate on first run** if the
      file doesn't exist (#1645): writes a near-empty template pointing at
      the docs. No admin identity or password is emitted — the admin row
      is seeded at runtime (``default_user`` defaults to
      ``<unixuser>@example.com``; null hash in ``none``/``oidc`` mode,
      ``KLANGK_DEFAULT_PASSWORD`` required in ``password``/``both``).
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


@app.command()
def main(  # pragma: no cover
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to a YAML config file. Bare ``klangkd`` (no --config) "
            "resolves ``$KLANGK_CONFIG_DIR/klangkd.yaml`` "
            "(default ``~/.config/klangkd/klangkd.yaml``) and generates it "
            "on first run (#1645). Use 'none' to run from env vars only "
            "(no config file)."
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
