"""Shared ``typer`` flag definitions for the workspace commands.

The create and edit commands expose the same repeatable/resource-limit
flags with the same help text; declaring each option once keeps the two
signatures from drifting (#2904, #3048). Each ``Annotated`` alias below
carries the full option definition — annotation, flag spelling, help —
so a command signature only spells the parameter name and its ``None``
default (``mount: MountOption = None``). Reusing one definition across
commands is safe: typer copies the ``OptionInfo`` per command when it
builds the click parameters and never mutates the original.
"""

from __future__ import annotations

from typing import Annotated

import typer

MountOption = Annotated[
    list[str] | None,
    typer.Option(
        "--mount",
        help="Mount, repeatable (e.g. /home/me/src:/work/src, nix-vol:/nix)",
    ),
]
EnvOption = Annotated[
    list[str] | None,
    typer.Option(
        "--env",
        help="Environment variable, repeatable (e.g. KEY=VALUE)",
    ),
]
AllowOption = Annotated[
    list[str] | None,
    typer.Option(
        "--allow",
        help=(
            "Allowed egress domain, repeatable (e.g. github.com:443, pypi.org)"
        ),
    ),
]
RejectOption = Annotated[
    list[str] | None,
    typer.Option(
        "--reject",
        help=(
            "Rejected egress domain (NXDOMAIN'd), repeatable"
            " (e.g. evil.example.com). CIDR ranges are not supported."
        ),
    ),
]
IdleTimeoutOption = Annotated[
    int | None,
    typer.Option(
        "--idle-timeout",
        help="Idle timeout in seconds (0 = never idle out)",
    ),
]
CpuLimitOption = Annotated[
    float | None,
    typer.Option("--cpu-limit", help="CPU limit (e.g. 2.0)"),
]
MemoryLimitOption = Annotated[
    str | None,
    typer.Option("--memory-limit", help="Memory limit (e.g. 4g, 512m)"),
]
PidsLimitOption = Annotated[
    int | None,
    typer.Option("--pids-limit", help="PIDs limit (e.g. 512)"),
]
SudoOption = Annotated[
    bool | None,
    typer.Option(
        "--sudo/--no-sudo",
        help=(
            "Workspace sudo posture (server-permitting): off unless the "
            "workspace opts in with --sudo. --no-sudo locks it down "
            "(no passwordless sudo). Applies when the container is next "
            "created"
        ),
    ),
]
ClassificationBannerOption = Annotated[
    str | None,
    typer.Option(
        "--classification-banner",
        help=(
            "Classification marking shown as a persistent banner on the "
            "workspace page (free text, e.g. UNCLASSIFIED, CUI, SECRET). "
            "Empty or omitted = the server default "
            "(KLANGKD_CLASSIFICATION_BANNER)"
        ),
    ),
]
