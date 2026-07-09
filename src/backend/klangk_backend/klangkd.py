"""``klangkd`` — the klangk server launcher (#1395).

A thin console script that loads config (from a YAML file + env vars + built-in
defaults, per the precedence rules in :mod:`klangk_backend.settings`) and
starts uvicorn.  In this chunk it does **not** own nginx or bind a UDS — it's
purely the config-loading + uvicorn-launching entry point, validating the
config subsystem on the current TCP transport.

Usage::

    klangkd                          # requires /etc/klangkd.conf
    klangkd --config /path/to/cfg.yaml
    klangkd --config=none            # env-vars-only (the sole opt-out)

Config-file resolution (three states, no implicit escape):

1. Bare ``klangkd`` → requires ``/etc/klangkd.conf``; missing → error.
2. ``--config=<path>`` → that path required to exist; missing → error.
3. ``--config=none`` → run from env vars + built-in defaults (no file).

See #1392 (the design record) and #1395 (this chunk) for the full rationale.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn

from klangk_backend.settings import set_config_file, validate_at_startup

# The default config-file location — a deployed klangkd finds its config here
# with no args.  See #1395.
DEFAULT_CONFIG_PATH = "/etc/klangkd.conf"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Start the klangk server (config + uvicorn).",
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
    """Start the klangk server."""
    resolved = _resolve_config_path(config)

    # Set the config-file path on the settings module before anything reads
    # config.  This wires the YAML source into the customise_sources chain.
    set_config_file(resolved)
    # Eagerly validate — a bogus config fails here, before uvicorn starts.
    settings = validate_at_startup()

    # Launch uvicorn on the configured host/port (still TCP in this chunk;
    # UDS arrives in #1396).  Read the bind from the merged settings so a
    # config-file value takes effect.
    host = os.environ.get("KLANGK_LISTEN") or settings.listen or "127.0.0.1"
    port_str = os.environ.get("KLANGK_PORT") or "8997"
    port = int(port_str)

    uvicorn.run(
        "klangk_backend.main:app",
        host=host,
        port=port,
        # Match the flags devenv.nix / supervisord pass today, so behavior
        # is unchanged when klangkd replaces them (#1396 will revisit these).
        proxy_headers=False,
        ws_max_size=int(os.environ.get("KLANGK_WS_MSG_SIZE_MAX", "16777216")),
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
