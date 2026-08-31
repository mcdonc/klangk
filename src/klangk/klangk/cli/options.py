"""Shared ``typer.Option`` declarations for the workspace commands.

The create and edit commands expose the same repeatable flags with the
same help text; declaring each option once keeps the two signatures from
drifting (#2904). Reusing one ``OptionInfo`` across commands is safe —
typer reads it when building each command's click parameters and never
mutates it.
"""

from __future__ import annotations

import typer

MOUNT_OPTION = typer.Option(
    None,
    "--mount",
    help="Mount, repeatable (e.g. /home/me/src:/work/src, nix-vol:/nix)",
)
ENV_OPTION = typer.Option(
    None,
    "--env",
    help="Environment variable, repeatable (e.g. KEY=VALUE)",
)
ALLOW_OPTION = typer.Option(
    None,
    "--allow",
    help="Allowed egress domain, repeatable (e.g. github.com:443, pypi.org)",
)
REJECT_OPTION = typer.Option(
    None,
    "--reject",
    help=(
        "Rejected egress domain (NXDOMAIN'd), repeatable "
        "(e.g. evil.example.com). CIDR ranges are not supported."
    ),
)
IDLE_TIMEOUT_OPTION = typer.Option(
    None,
    "--idle-timeout",
    help="Idle timeout in seconds (0 = never idle out)",
)
CPU_LIMIT_OPTION = typer.Option(
    None, "--cpu-limit", help="CPU limit (e.g. 2.0)"
)
MEMORY_LIMIT_OPTION = typer.Option(
    None, "--memory-limit", help="Memory limit (e.g. 4g, 512m)"
)
PIDS_LIMIT_OPTION = typer.Option(
    None, "--pids-limit", help="PIDs limit (e.g. 512)"
)
