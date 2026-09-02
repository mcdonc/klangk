"""The `klangk edit` command: interactive workspace settings editor.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import typer

from . import context
from .options import (
    ALLOW_OPTION,
    CPU_LIMIT_OPTION,
    ENV_OPTION,
    IDLE_TIMEOUT_OPTION,
    MEMORY_LIMIT_OPTION,
    MOUNT_OPTION,
    PIDS_LIMIT_OPTION,
    REJECT_OPTION,
)
from .workspaces import build_settings, parse_env_list, prompt, SENTINEL
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
CREATE_TIME_SETTINGS_DEFAULTS = {"nix": False, "allow_sudo": False}


def print_list_editor_state(items, header, empty_note) -> None:
    """Show the numbered current list (or the empty note)."""
    if items:
        typer.echo(f"\n{header}:")
        for i, item in enumerate(items, 1):
            typer.echo(f"  {i}. {item}")
    else:
        typer.echo(f"\n{empty_note}")


def prompt_list_add(items, add_prompt, validate) -> bool | None:
    """One add prompt: True added / False invalid (retry) / None skipped."""
    add = input(add_prompt).strip()
    if not add:
        return None
    err = validate(add)
    if err:
        typer.echo(err)
        return False
    items.append(add)
    return True


def prompt_list_remove(items, remove_what) -> bool | None:
    """One remove prompt: True removed / False invalid (retry) / None skipped."""
    rm = input(f"Remove {remove_what} number (or Enter to skip): ").strip()
    if not rm:
        return None
    try:
        idx = int(rm) - 1
    except ValueError:
        typer.echo("Invalid number.")
        return False
    if not 0 <= idx < len(items):
        typer.echo("Invalid number.")
        return False
    typer.echo(f"Removed: {items.pop(idx)}")
    return True


def next_list_outcome(items, *, add_prompt, remove_what, validate):
    """One add/remove interaction: True/False (changed / retry) or None (done)."""
    outcome = prompt_list_add(items, add_prompt, validate)
    if outcome is None and items:
        outcome = prompt_list_remove(items, remove_what)
    return outcome


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
        print_list_editor_state(items, header, empty_note)
        outcome = next_list_outcome(
            items,
            add_prompt=add_prompt,
            remove_what=remove_what,
            validate=validate,
        )
        if outcome is None:
            return changed
        if outcome:
            changed = True


def print_env_state(env) -> None:
    """Show the numbered current env vars (or the empty note)."""
    if env:
        typer.echo("\nCurrent environment variables:")
        env_items = list(env.items())
        for i, (k, v) in enumerate(env_items, 1):
            typer.echo(f"  {i}. {k}={v}")
    else:
        typer.echo("\nNo environment variables configured.")


def prompt_env_add(env) -> bool | None:
    """One add prompt: True added / False invalid (retry) / None skipped."""
    add = input("\nAdd env var (e.g. KEY=VALUE, or Enter to skip): ").strip()
    if not add:
        return None
    if "=" not in add:
        typer.echo("Invalid format, expected KEY=VALUE.")
        return False
    key, _, value = add.partition("=")
    env[key] = value
    return True


def prompt_env_remove(env) -> bool | None:
    """One remove prompt: True removed / False invalid (retry) / None skipped."""
    rm = input("Remove env var number (or Enter to skip): ").strip()
    if not rm:
        return None
    try:
        idx = int(rm) - 1
    except ValueError:
        typer.echo("Invalid number.")
        return False
    env_items = list(env.items())
    if not 0 <= idx < len(env_items):
        typer.echo("Invalid number.")
        return False
    removed_key = env_items[idx][0]
    del env[removed_key]
    typer.echo(f"Removed: {removed_key}")
    return True


def next_env_outcome(env):
    """One add/remove interaction: True/False (changed / retry) or None (done)."""
    outcome = prompt_env_add(env)
    if outcome is None and env:
        outcome = prompt_env_remove(env)
    return outcome


def edit_env_interactively(env: dict) -> bool:
    """Add/remove loop for the env-var map; True if changed."""
    changed = False
    while True:
        print_env_state(env)
        outcome = next_env_outcome(env)
        if outcome is None:
            return changed
        if outcome:
            changed = True


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
    the typed value or the ``SENTINEL`` meaning "unchanged".
    """
    new_name = prompt("Name", ws.name)
    new_image = prompt("Container Image", ws.image)
    new_command = prompt("Service shell command", ws.service_command)
    new_health_check = prompt("Health check command", ws.health_check)
    # Plain label like the sibling prompts: Enter keeps the current
    # value (the [(none)] display shows it); typing whitespace clears
    # the override back to the deploy default (#2768).
    new_banner = prompt("Classification banner", ws.classification_banner)
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


def clearable_body(clearable: dict, skip) -> dict:
    """Body fields for clearable overrides; a falsy value maps to None
    on the wire ('' clears back to the deploy default, #2768). *skip* is
    the not-given marker filtered out (``SENTINEL`` for prompts, ``None``
    for flags)."""
    return {
        key: value or None
        for key, value in clearable.items()
        if value is not skip
    }


def prompted_body_fields(
    ws, new_name, new_image, new_command, new_health_check, new_banner
) -> dict:
    """Body fields from the scalar prompts (sentinel = leave unchanged)."""
    body: dict = {}
    if new_name is not SENTINEL:
        body["name"] = new_name or ws.name  # don't allow empty name
    # A cleared (whitespace) answer maps to None on the wire (#2768).
    body.update(
        clearable_body(
            {
                "image": new_image,
                "service_command": new_command,
                "health_check": new_health_check,
                "classification_banner": new_banner,
            },
            skip=SENTINEL,
        )
    )
    return body


def changed_list_body(changed: bool, key: str, current) -> dict:
    """Body field for one interactively edited list (only when changed)."""
    if not changed:
        return {}
    return {key: current or None}


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
    body.update(changed_list_body(mounts_changed, "mounts", current_mounts))
    body.update(changed_list_body(env_changed, "env", current_env))
    body.update(
        changed_list_body(domains_changed, "allowed_domains", current_domains)
    )
    body.update(
        changed_list_body(
            rejected_changed, "rejected_domains", current_rejected
        )
    )
    return body


def validated_specs_or_exit(values: list[str], validate) -> list[str]:
    """Validate each repeatable-flag value; exit(1) on the first bad one."""
    for v in values:
        err = validate(v)
        if err:
            context.err.print(f"[red]{err}[/red]")
            raise typer.Exit(code=1)
    return values


def override_body(overrides: dict) -> dict:
    """Body fields for the non-None overrides."""
    return {
        key: value for key, value in overrides.items() if value is not None
    }


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
    body: dict = override_body(
        {
            "name": name,
            # Mutable (#2719); takes effect on the next connect/start —
            # never a restart-prompt field (existing sessions keep their
            # layout until they end).
            "per_handle_home": per_handle_home,
            "auto_start": auto_start,
        }
    )
    # '' clears these back to the deploy default (None on the wire).
    body.update(
        clearable_body(
            {
                "image": image,
                "service_command": command,
                "health_check": health_check,
                "classification_banner": classification_banner,
            },
            skip=None,
        )
    )
    return body


def list_flag_body(value, key: str, validate) -> dict:
    """Body for one repeatable list flag; {} when the flag was not given."""
    if not isinstance(value, list):
        return {}
    validated_specs_or_exit(value, validate)
    return {key: value or None}


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
    body.update(list_flag_body(mount, "mounts", validate_mount_spec))
    if isinstance(env, list):
        body["env"] = parse_env_list(env) or None
    body.update(
        list_flag_body(allow, "allowed_domains", validate_allowed_domain_spec)
    )
    # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
    body.update(
        list_flag_body(
            reject,
            "rejected_domains",
            lambda spec: validate_allowed_domain_spec(spec, allow_cidr=False),
        )
    )
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
    edit_settings = build_settings(
        idle_timeout, cpu_limit, memory_limit, pids_limit, allow_sudo
    )
    if not edit_settings:
        return None
    merged = dict(ws.settings or {})
    merged.update(edit_settings)
    return merged


def create_time_setting_changed(
    bag: dict, old: dict, key: str, default
) -> bool:
    """True when a create-time settings key differs from its old value."""
    return bag.get(key, default) != old.get(key, default)


def settings_changed(ws, body: dict) -> bool:
    """True when a create-time settings key (nix / allow_sudo) changed."""
    if "settings" not in body:
        return False
    bag = body["settings"] or {}
    old = ws.settings or {}
    return any(
        create_time_setting_changed(bag, old, key, default)
        for key, default in CREATE_TIME_SETTINGS_DEFAULTS.items()
    )


def restart_needed(ws, body: dict) -> bool:
    """True if any create-time field changed on a running workspace."""
    if not ws.running:
        return False
    if body.keys() & CREATE_TIME_KEYS:
        return True
    return settings_changed(ws, body)


def apply_edit(client, ws, body: dict, restart: bool) -> None:
    resp = client.put(f"/api/v1/workspaces/{ws.id}", json=body)
    if resp.status_code == 404:
        context.err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Updated workspace {ws.name}")

    if restart:
        context.err.print(
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
    mount: list[str] | None = MOUNT_OPTION,
    env: list[str] | None = ENV_OPTION,
    allow: list[str] | None = ALLOW_OPTION,
    reject: list[str] | None = REJECT_OPTION,
    idle_timeout: int | None = IDLE_TIMEOUT_OPTION,
    cpu_limit: float | None = CPU_LIMIT_OPTION,
    memory_limit: str | None = MEMORY_LIMIT_OPTION,
    pids_limit: int | None = PIDS_LIMIT_OPTION,
    allow_sudo: bool | None = typer.Option(
        None,
        "--sudo/--no-sudo",
        help=(
            "Workspace sudo posture (server-permitting): off unless the "
            "workspace opts in with --sudo. --no-sudo locks it down "
            "(no passwordless sudo). Applies when the container is next "
            "created"
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
    client = context.client()
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
    else:
        body = interactive_edit_body(ws)

    if not body:
        typer.echo("No changes.")
        return

    apply_edit(client, ws, body, restart_needed(ws, body))
