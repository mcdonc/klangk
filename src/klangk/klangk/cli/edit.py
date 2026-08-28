"""The `klangk edit` command: interactive workspace settings editor.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import typer

from . import context
from .workspaces import _build_settings, _parse_env_list, _prompt, _SENTINEL
from .mount import validate_allowed_domain_spec, validate_mount_spec


@context.app.command()
def edit(
    workspace: str = typer.Argument(..., help="Workspace name"),
    name: str | None = typer.Option(None, "--name", help="New name"),
    image: str | None = typer.Option(None, "--image", help="Container image"),
    command: str | None = typer.Option(
        None, "--command", "-c", help="Service shell command (use '' to clear)"
    ),
    auto_start: bool | None = typer.Option(
        None,
        "--auto-start/--no-auto-start",
        help="Start container automatically on server boot",
    ),
    per_handle_home: bool | None = typer.Option(
        None,
        "--per-handle-home/--shared-home",
        help=(
            "Home layout (applies from the next connect/start): "
            "per-handle gives each member a private /home/<handle>; "
            "shared puts everyone in /home/klangk"
        ),
    ),
    health_check: str | None = typer.Option(
        None,
        "--health-check",
        help=(
            "Shell command polled inside the container to gauge service "
            "health (exit 0 = healthy). Use '' to clear."
        ),
    ),
    mount: list[str] | None = typer.Option(
        None,
        "--mount",
        help="Mount, repeatable (e.g. /home/me/src:/work/src, nix-vol:/nix)",
    ),
    env: list[str] | None = typer.Option(
        None,
        "--env",
        help="Environment variable, repeatable (e.g. KEY=VALUE)",
    ),
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        help="Allowed egress domain, repeatable (e.g. github.com:443, pypi.org)",
    ),
    reject: list[str] | None = typer.Option(
        None,
        "--reject",
        help=(
            "Rejected egress domain (NXDOMAIN'd), repeatable "
            "(e.g. evil.example.com). CIDR ranges are not supported."
        ),
    ),
    idle_timeout: int | None = typer.Option(
        None,
        "--idle-timeout",
        help="Idle timeout in seconds (0 = never idle out)",
    ),
    cpu_limit: float | None = typer.Option(
        None, "--cpu-limit", help="CPU limit (e.g. 2.0)"
    ),
    memory_limit: str | None = typer.Option(
        None, "--memory-limit", help="Memory limit (e.g. 4g, 512m)"
    ),
    pids_limit: int | None = typer.Option(
        None, "--pids-limit", help="PIDs limit (e.g. 512)"
    ),
    allow_sudo: bool | None = typer.Option(
        None,
        "--sudo/--no-sudo",
        help=(
            "Workspace sudo posture (server-permitting): --no-sudo locks "
            "this workspace down (no passwordless sudo) even when the "
            "server allows it; --sudo reverts to the server default. "
            "Applies when the container is next created"
        ),
    ),
    classification_banner: str | None = typer.Option(
        None,
        "--classification-banner",
        help=(
            "Classification marking shown as a persistent banner on the "
            "workspace page (free text, e.g. UNCLASSIFIED, CUI, SECRET). "
            "Use '' to clear back to the server default"
        ),
    ),
) -> None:
    """Edit workspace settings.

    Without flags, interactively prompts for each field.
    Press Enter to keep the current value.
    """
    context.require_auth()
    client = context._client()
    ws = context.resolve_or_exit(client, workspace)

    has_flags = (
        name is not None
        or image is not None
        or command is not None
        or auto_start is not None
        or per_handle_home is not None
        or health_check is not None
        or isinstance(mount, list)
        or isinstance(env, list)
        or isinstance(allow, list)
        or isinstance(reject, list)
        or idle_timeout is not None
        or cpu_limit is not None
        or memory_limit is not None
        or pids_limit is not None
        or allow_sudo is not None
        or classification_banner is not None
    )
    if not has_flags:
        # Interactive mode
        new_name = _prompt("Name", ws.name)
        new_image = _prompt("Container Image", ws.image)
        new_command = _prompt("Service shell command", ws.service_command)
        new_health_check = _prompt("Health check command", ws.health_check)
        # Plain label like the sibling prompts: Enter keeps the current
        # value (the [(none)] display shows it); typing whitespace clears
        # the override back to the deploy default (#2768).
        new_banner = _prompt("Classification banner", ws.classification_banner)

        # Interactive mount editing loop
        current_mounts = list(ws.mounts or [])
        mounts_changed = False
        while True:
            if current_mounts:
                typer.echo("\nCurrent mounts:")
                for i, m in enumerate(current_mounts, 1):
                    typer.echo(f"  {i}. {m}")
            else:
                typer.echo("\nNo mounts configured.")

            add = input(
                "\nAdd mount (e.g. /host/path:/container/path, or Enter to skip): "
            ).strip()
            if add:
                err = validate_mount_spec(add)
                if err:
                    typer.echo(err)
                    continue
                current_mounts.append(add)
                mounts_changed = True
                continue

            if current_mounts:
                rm = input("Remove mount number (or Enter to skip): ").strip()
                if rm:
                    try:
                        idx = int(rm) - 1
                        if 0 <= idx < len(current_mounts):
                            removed = current_mounts.pop(idx)
                            typer.echo(f"Removed: {removed}")
                            mounts_changed = True
                            continue
                        else:
                            typer.echo("Invalid number.")
                            continue
                    except ValueError:
                        typer.echo("Invalid number.")
                        continue

            break  # both add and remove were skipped

        # Interactive env var editing loop
        current_env = dict(ws.env or {})
        env_changed = False
        while True:
            if current_env:
                typer.echo("\nCurrent environment variables:")
                env_items = list(current_env.items())
                for i, (k, v) in enumerate(env_items, 1):
                    typer.echo(f"  {i}. {k}={v}")
            else:
                typer.echo("\nNo environment variables configured.")

            add = input(
                "\nAdd env var (e.g. KEY=VALUE, or Enter to skip): "
            ).strip()
            if add:
                if "=" not in add:
                    typer.echo("Invalid format, expected KEY=VALUE.")
                    continue
                key, _, value = add.partition("=")
                current_env[key] = value
                env_changed = True
                continue

            if current_env:
                rm = input(
                    "Remove env var number (or Enter to skip): "
                ).strip()
                if rm:
                    try:
                        idx = int(rm) - 1
                        env_items = list(current_env.items())
                        if 0 <= idx < len(env_items):
                            removed_key = env_items[idx][0]
                            del current_env[removed_key]
                            typer.echo(f"Removed: {removed_key}")
                            env_changed = True
                            continue
                        else:
                            typer.echo("Invalid number.")
                            continue
                    except ValueError:
                        typer.echo("Invalid number.")
                        continue

            break  # both add and remove were skipped

        # Interactive allowed-domains editing loop
        current_domains = list(ws.allowed_domains or [])
        domains_changed = False
        while True:
            if current_domains:
                typer.echo("\nAllowed egress domains:")
                for i, d in enumerate(current_domains, 1):
                    typer.echo(f"  {i}. {d}")
            else:
                typer.echo("\nNo egress allowlist (unrestricted networking).")

            add = input(
                "\nAdd domain (e.g. github.com:443, or Enter to skip): "
            ).strip()
            if add:
                err = validate_allowed_domain_spec(add)
                if err:
                    typer.echo(err)
                    continue
                current_domains.append(add)
                domains_changed = True
                continue

            if current_domains:
                rm = input("Remove domain number (or Enter to skip): ").strip()
                if rm:
                    try:
                        idx = int(rm) - 1
                        if 0 <= idx < len(current_domains):
                            removed = current_domains.pop(idx)
                            typer.echo(f"Removed: {removed}")
                            domains_changed = True
                            continue
                        else:
                            typer.echo("Invalid number.")
                            continue
                    except ValueError:
                        typer.echo("Invalid number.")
                        continue

            break  # both add and remove were skipped

        # Interactive rejected-domains editing loop (#2386)
        current_rejected = list(ws.rejected_domains or [])
        rejected_changed = False
        while True:
            if current_rejected:
                typer.echo("\nRejected egress domains (NXDOMAIN'd):")
                for i, d in enumerate(current_rejected, 1):
                    typer.echo(f"  {i}. {d}")
            else:
                typer.echo("\nNo egress denylist.")

            add = input(
                "\nAdd rejected domain (e.g. evil.example.com, or Enter to skip): "
            ).strip()
            if add:
                # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
                err = validate_allowed_domain_spec(add, allow_cidr=False)
                if err:
                    typer.echo(err)
                    continue
                current_rejected.append(add)
                rejected_changed = True
                continue

            if current_rejected:
                rm = input("Remove domain number (or Enter to skip): ").strip()
                if rm:
                    try:
                        idx = int(rm) - 1
                        if 0 <= idx < len(current_rejected):
                            removed = current_rejected.pop(idx)
                            typer.echo(f"Removed: {removed}")
                            rejected_changed = True
                            continue
                        else:
                            typer.echo("Invalid number.")
                            continue
                    except ValueError:
                        typer.echo("Invalid number.")
                        continue

            break  # both add and remove were skipped

        body: dict = {}
        if new_name is not _SENTINEL:
            body["name"] = new_name or ws.name  # don't allow empty name
        if new_image is not _SENTINEL:
            body["image"] = new_image or None
        if new_command is not _SENTINEL:
            body["service_command"] = new_command or None
        if new_health_check is not _SENTINEL:
            body["health_check"] = new_health_check or None
        if new_banner is not _SENTINEL:
            body["classification_banner"] = new_banner or None
        if mounts_changed:
            body["mounts"] = current_mounts or None
        if env_changed:
            body["env"] = current_env or None
        if domains_changed:
            body["allowed_domains"] = current_domains or None
        if rejected_changed:
            body["rejected_domains"] = current_rejected or None
    else:
        # Flags mode — only send provided fields
        body = {}
        if name is not None:
            body["name"] = name
        if image is not None:
            body["image"] = image or None
        if command is not None:
            body["service_command"] = command or None
        if auto_start is not None:
            body["auto_start"] = auto_start
        if per_handle_home is not None:
            # Mutable (#2719); takes effect on the next connect/start —
            # never a restart-prompt field (existing sessions keep their
            # layout until they end).
            body["per_handle_home"] = per_handle_home
        if health_check is not None:
            body["health_check"] = health_check or None
        if classification_banner is not None:
            body["classification_banner"] = classification_banner or None
        if isinstance(mount, list):
            for m in mount:
                err = validate_mount_spec(m)
                if err:
                    context._err.print(f"[red]{err}[/red]")
                    raise typer.Exit(code=1)
            body["mounts"] = mount or None
        if isinstance(env, list):
            body["env"] = _parse_env_list(env) or None
        if isinstance(allow, list):
            for spec in allow:
                err = validate_allowed_domain_spec(spec)
                if err:
                    context._err.print(f"[red]{err}[/red]")
                    raise typer.Exit(code=1)
            body["allowed_domains"] = allow or None
        if isinstance(reject, list):
            for spec in reject:
                # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
                err = validate_allowed_domain_spec(spec, allow_cidr=False)
                if err:
                    context._err.print(f"[red]{err}[/red]")
                    raise typer.Exit(code=1)
            body["rejected_domains"] = reject or None
        edit_settings = _build_settings(
            idle_timeout, cpu_limit, memory_limit, pids_limit, allow_sudo
        )
        if edit_settings:
            # Merge with existing settings so unspecified keys are preserved.
            merged = dict(ws.settings or {})
            merged.update(edit_settings)
            body["settings"] = merged

    if not body:
        typer.echo("No changes.")
        return

    # Detect if any create-time field changed on a running workspace.
    _CREATE_TIME_KEYS = {
        "image",
        "mounts",
        "env",
        "service_command",
        "allowed_domains",
        "rejected_domains",
    }
    restart_needed = ws.running and bool(body.keys() & _CREATE_TIME_KEYS)
    # Settings-bag keys that are baked into the container at create time:
    # nix (the /nix mount, #2233) and allow_sudo (the sudoers rule, #2017).
    # A flip on a running workspace needs a restart to take effect — the
    # TUI and web panel detect the same pair.
    if ws.running and "settings" in body:
        _bag = body["settings"] or {}
        _old = ws.settings or {}
        _defaults = {"nix": False, "allow_sudo": True}
        for _key, _default in _defaults.items():
            if _bag.get(_key, _default) != _old.get(_key, _default):
                restart_needed = True
                break

    resp = client.put(f"/api/v1/workspaces/{ws.id}", json=body)
    if resp.status_code == 404:
        context._err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Updated workspace {ws.name}")

    if restart_needed:
        context._err.print(
            "[yellow]The running container is not affected by this "
            "edit — restart the workspace to apply.[/yellow]"
        )
        answer = input("Restart now? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            client.restart_workspace(ws.name)
            typer.echo(f"Restarted workspace {ws.name}")
