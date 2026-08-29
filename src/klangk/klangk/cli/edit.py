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

# Body fields baked into the container at create time: changing one on a
# running workspace does nothing until the container is restarted.
CREATE_TIME_KEYS = {
    "image",
    "mounts",
    "env",
    "service_command",
    "allowed_domains",
    "rejected_domains",
}

# Settings-bag keys baked into the container at create time: nix (the
# /nix mount, #2233) and allow_sudo (the sudoers rule, #2017). A flip on
# a running workspace needs a restart to take effect — the TUI and web
# panel detect the same pair.
CREATE_TIME_SETTINGS_DEFAULTS = {"nix": False, "allow_sudo": True}


def edit_list_interactively(
    items: list[str],
    *,
    header: str,
    empty_note: str,
    add_prompt: str,
    remove_what: str,
    validate,
) -> bool:
    """Add/remove loop for a repeatable string list; True if changed."""
    changed = False
    while True:
        if items:
            typer.echo(f"\n{header}:")
            for i, item in enumerate(items, 1):
                typer.echo(f"  {i}. {item}")
        else:
            typer.echo(f"\n{empty_note}")

        add = input(add_prompt).strip()
        if add:
            err = validate(add)
            if err:
                typer.echo(err)
                continue
            items.append(add)
            changed = True
            continue

        if items:
            rm = input(
                f"Remove {remove_what} number (or Enter to skip): "
            ).strip()
            if rm:
                try:
                    idx = int(rm) - 1
                    if 0 <= idx < len(items):
                        removed = items.pop(idx)
                        typer.echo(f"Removed: {removed}")
                        changed = True
                        continue
                    else:
                        typer.echo("Invalid number.")
                        continue
                except ValueError:
                    typer.echo("Invalid number.")
                    continue

        break  # both add and remove were skipped
    return changed


def edit_env_interactively(env: dict) -> bool:
    """Add/remove loop for the env-var map; True if changed."""
    changed = False
    while True:
        if env:
            typer.echo("\nCurrent environment variables:")
            env_items = list(env.items())
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
            env[key] = value
            changed = True
            continue

        if env:
            rm = input("Remove env var number (or Enter to skip): ").strip()
            if rm:
                try:
                    idx = int(rm) - 1
                    env_items = list(env.items())
                    if 0 <= idx < len(env_items):
                        removed_key = env_items[idx][0]
                        del env[removed_key]
                        typer.echo(f"Removed: {removed_key}")
                        changed = True
                        continue
                    else:
                        typer.echo("Invalid number.")
                        continue
                except ValueError:
                    typer.echo("Invalid number.")
                    continue

        break  # both add and remove were skipped
    return changed


def has_any_flags(
    *,
    name: str | None,
    image: str | None,
    command: str | None,
    auto_start: bool | None,
    per_handle_home: bool | None,
    health_check: str | None,
    mount: list[str] | None,
    env: list[str] | None,
    allow: list[str] | None,
    reject: list[str] | None,
    idle_timeout: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    pids_limit: int | None,
    allow_sudo: bool | None,
    classification_banner: str | None,
) -> bool:
    scalars = (
        name,
        image,
        command,
        auto_start,
        per_handle_home,
        health_check,
        idle_timeout,
        cpu_limit,
        memory_limit,
        pids_limit,
        allow_sudo,
        classification_banner,
    )
    lists = (mount, env, allow, reject)
    return any(v is not None for v in scalars) or any(
        isinstance(v, list) for v in lists
    )


def prompted_edit_fields(ws):
    """Prompt for the scalar fields (Enter keeps the current value).

    Returns (name, image, command, health_check, banner) — each either
    the typed value or the ``_SENTINEL`` meaning "unchanged".
    """
    new_name = _prompt("Name", ws.name)
    new_image = _prompt("Container Image", ws.image)
    new_command = _prompt("Service shell command", ws.service_command)
    new_health_check = _prompt("Health check command", ws.health_check)
    # Plain label like the sibling prompts: Enter keeps the current
    # value (the [(none)] display shows it); typing whitespace clears
    # the override back to the deploy default (#2768).
    new_banner = _prompt("Classification banner", ws.classification_banner)
    return (
        new_name,
        new_image,
        new_command,
        new_health_check,
        new_banner,
    )


def edit_lists_interactively(ws):
    """Run the four list editors against the workspace's current lists.

    Returns (mounts, mounts_changed, env, env_changed, domains,
    domains_changed, rejected, rejected_changed).
    """
    current_mounts = list(ws.mounts or [])
    mounts_changed = edit_list_interactively(
        current_mounts,
        header="Current mounts",
        empty_note="No mounts configured.",
        add_prompt="\nAdd mount (e.g. /host/path:/container/path, or Enter to skip): ",
        remove_what="mount",
        validate=validate_mount_spec,
    )

    current_env = dict(ws.env or {})
    env_changed = edit_env_interactively(current_env)

    current_domains = list(ws.allowed_domains or [])
    domains_changed = edit_list_interactively(
        current_domains,
        header="Allowed egress domains",
        empty_note="No egress allowlist (unrestricted networking).",
        add_prompt="\nAdd domain (e.g. github.com:443, or Enter to skip): ",
        remove_what="domain",
        validate=validate_allowed_domain_spec,
    )

    current_rejected = list(ws.rejected_domains or [])
    # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2386).
    rejected_changed = edit_list_interactively(
        current_rejected,
        header="Rejected egress domains (NXDOMAIN'd)",
        empty_note="No egress denylist.",
        add_prompt="\nAdd rejected domain (e.g. evil.example.com, or Enter to skip): ",
        remove_what="rejected domain",
        validate=lambda spec: validate_allowed_domain_spec(
            spec, allow_cidr=False
        ),
    )
    return (
        current_mounts,
        mounts_changed,
        current_env,
        env_changed,
        current_domains,
        domains_changed,
        current_rejected,
        rejected_changed,
    )


def interactive_edit_body(ws) -> dict:
    """Prompt for each field (Enter keeps the current value) and build the
    PATCH body from the answers."""
    (
        new_name,
        new_image,
        new_command,
        new_health_check,
        new_banner,
    ) = prompted_edit_fields(ws)
    (
        current_mounts,
        mounts_changed,
        current_env,
        env_changed,
        current_domains,
        domains_changed,
        current_rejected,
        rejected_changed,
    ) = edit_lists_interactively(ws)

    body = prompted_body_fields(
        ws, new_name, new_image, new_command, new_health_check, new_banner
    )
    body.update(
        edited_list_body(
            current_mounts,
            mounts_changed,
            current_env,
            env_changed,
            current_domains,
            domains_changed,
            current_rejected,
            rejected_changed,
        )
    )
    return body


def prompted_body_fields(
    ws, new_name, new_image, new_command, new_health_check, new_banner
) -> dict:
    """Body fields from the scalar prompts (sentinel = leave unchanged)."""
    body: dict = {}
    if new_name is not _SENTINEL:
        body["name"] = new_name or ws.name  # don't allow empty name
    # A cleared (whitespace) answer maps to None on the wire (#2768).
    clearable = {
        "image": new_image,
        "service_command": new_command,
        "health_check": new_health_check,
        "classification_banner": new_banner,
    }
    for key, value in clearable.items():
        if value is not _SENTINEL:
            body[key] = value or None
    return body


def edited_list_body(
    current_mounts,
    mounts_changed,
    current_env,
    env_changed,
    current_domains,
    domains_changed,
    current_rejected,
    rejected_changed,
) -> dict:
    """Body fields from the interactive list editors (changed lists only)."""
    body: dict = {}
    if mounts_changed:
        body["mounts"] = current_mounts or None
    if env_changed:
        body["env"] = current_env or None
    if domains_changed:
        body["allowed_domains"] = current_domains or None
    if rejected_changed:
        body["rejected_domains"] = current_rejected or None
    return body


def validated_specs_or_exit(values: list[str], validate) -> list[str]:
    """Validate each repeatable-flag value; exit(1) on the first bad one."""
    for v in values:
        err = validate(v)
        if err:
            context._err.print(f"[red]{err}[/red]")
            raise typer.Exit(code=1)
    return values


def flag_scalar_body(
    *,
    name: str | None,
    image: str | None,
    command: str | None,
    auto_start: bool | None,
    per_handle_home: bool | None,
    health_check: str | None,
    classification_banner: str | None,
) -> dict:
    """Body fields for the scalar (non-repeatable) flags."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    # '' clears these back to the deploy default (None on the wire).
    clearable = {
        "image": image,
        "service_command": command,
        "health_check": health_check,
        "classification_banner": classification_banner,
    }
    for key, value in clearable.items():
        if value is not None:
            body[key] = value or None
    if auto_start is not None:
        body["auto_start"] = auto_start
    if per_handle_home is not None:
        # Mutable (#2719); takes effect on the next connect/start —
        # never a restart-prompt field (existing sessions keep their
        # layout until they end).
        body["per_handle_home"] = per_handle_home
    return body


def flag_list_body(
    ws,
    *,
    mount: list[str] | None,
    env: list[str] | None,
    allow: list[str] | None,
    reject: list[str] | None,
    idle_timeout: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    pids_limit: int | None,
    allow_sudo: bool | None,
) -> dict:
    """Body fields for the repeatable list flags and the settings bag."""
    body: dict = {}
    if isinstance(mount, list):
        validated_specs_or_exit(mount, validate_mount_spec)
        body["mounts"] = mount or None
    if isinstance(env, list):
        body["env"] = _parse_env_list(env) or None
    if isinstance(allow, list):
        validated_specs_or_exit(allow, validate_allowed_domain_spec)
        body["allowed_domains"] = allow or None
    if isinstance(reject, list):
        # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
        validated_specs_or_exit(
            reject,
            lambda spec: validate_allowed_domain_spec(spec, allow_cidr=False),
        )
        body["rejected_domains"] = reject or None
    merged = merged_flag_settings(
        ws, idle_timeout, cpu_limit, memory_limit, pids_limit, allow_sudo
    )
    if merged:
        body["settings"] = merged
    return body


def merged_flag_settings(
    ws,
    idle_timeout: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    pids_limit: int | None,
    allow_sudo: bool | None,
) -> dict | None:
    """Flag-provided settings merged over the existing bag so unspecified
    keys are preserved; None when no settings flag was given (the key is
    then omitted entirely)."""
    edit_settings = _build_settings(
        idle_timeout, cpu_limit, memory_limit, pids_limit, allow_sudo
    )
    if not edit_settings:
        return None
    merged = dict(ws.settings or {})
    merged.update(edit_settings)
    return merged


def flag_edit_body(
    ws,
    *,
    name: str | None,
    image: str | None,
    command: str | None,
    auto_start: bool | None,
    per_handle_home: bool | None,
    health_check: str | None,
    classification_banner: str | None,
    mount: list[str] | None,
    env: list[str] | None,
    allow: list[str] | None,
    reject: list[str] | None,
    idle_timeout: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    pids_limit: int | None,
    allow_sudo: bool | None,
) -> dict:
    """Build the PATCH body from only the flags the user provided."""
    body = flag_scalar_body(
        name=name,
        image=image,
        command=command,
        auto_start=auto_start,
        per_handle_home=per_handle_home,
        health_check=health_check,
        classification_banner=classification_banner,
    )
    body.update(
        flag_list_body(
            ws,
            mount=mount,
            env=env,
            allow=allow,
            reject=reject,
            idle_timeout=idle_timeout,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            pids_limit=pids_limit,
            allow_sudo=allow_sudo,
        )
    )
    return body


def restart_needed(ws, body: dict) -> bool:
    """True if any create-time field changed on a running workspace."""
    if ws.running and bool(body.keys() & CREATE_TIME_KEYS):
        return True
    if ws.running and "settings" in body:
        bag = body["settings"] or {}
        old = ws.settings or {}
        for key, default in CREATE_TIME_SETTINGS_DEFAULTS.items():
            if bag.get(key, default) != old.get(key, default):
                return True
    return False


def apply_edit(client, ws, body: dict, restart: bool) -> None:
    resp = client.put(f"/api/v1/workspaces/{ws.id}", json=body)
    if resp.status_code == 404:
        context._err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Updated workspace {ws.name}")

    if restart:
        context._err.print(
            "[yellow]The running container is not affected by this "
            "edit — restart the workspace to apply.[/yellow]"
        )
        answer = input("Restart now? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            client.restart_workspace(ws.name)
            typer.echo(f"Restarted workspace {ws.name}")


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

    if has_any_flags(
        name=name,
        image=image,
        command=command,
        auto_start=auto_start,
        per_handle_home=per_handle_home,
        health_check=health_check,
        mount=mount,
        env=env,
        allow=allow,
        reject=reject,
        idle_timeout=idle_timeout,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        pids_limit=pids_limit,
        allow_sudo=allow_sudo,
        classification_banner=classification_banner,
    ):
        body = flag_edit_body(
            ws,
            name=name,
            image=image,
            command=command,
            auto_start=auto_start,
            per_handle_home=per_handle_home,
            health_check=health_check,
            classification_banner=classification_banner,
            mount=mount,
            env=env,
            allow=allow,
            reject=reject,
            idle_timeout=idle_timeout,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            pids_limit=pids_limit,
            allow_sudo=allow_sudo,
        )
    else:
        body = interactive_edit_body(ws)

    if not body:
        typer.echo("No changes.")
        return

    apply_edit(client, ws, body, restart_needed(ws, body))
