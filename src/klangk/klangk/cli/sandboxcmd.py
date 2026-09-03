"""The `klangk sandbox` command and its copy/setup helpers.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import io
import json
import shlex
import sys
from pathlib import Path

import typer
import websockets

from .client import exec_on_ws, workspace_ws, WorkspaceNotFoundError
from . import context
from .sandbox import (
    build_all_mounts,
    build_copy_pairs,
    expand_container_path,
    load_sandbox_config,
    resolve_setup_command,
)


def resolve_workspace_and_url(
    workspace_name: str,
) -> tuple:
    """Resolve a workspace by name and return (ws, server_spec, token)."""
    context.require_auth()
    client = context.client()
    ws = context.resolve_or_exit(client, workspace_name)
    return (ws, context.server_url(), context.session_token())


async def copy_sandbox_files(ws, config, sandbox_root, handle) -> None:
    """Copy the configured files into the container home (cat via exec)."""
    for host_path, container_dest in build_copy_pairs(
        config, sandbox_root, handle
    ):
        src = Path(host_path)
        if not src.exists():
            context.err.print(
                f"[yellow]Warning: copy source {host_path} not"
                f" found, skipping[/yellow]"
            )
            continue
        context.err.print(f"  [dim]copy:[/dim] {host_path} → {container_dest}")
        parent = str(Path(container_dest).parent)
        # #3093: quote the paths — a copy destination containing
        # spaces must round-trip into the sh -c string intact.
        stdout_buf = io.BytesIO()
        exit_code = await exec_on_ws(
            ws,
            [
                "sh",
                "-c",
                f"mkdir -p {shlex.quote(parent)}"
                f" && cat > {shlex.quote(container_dest)}",
            ],
            stdin=io.BytesIO(src.read_bytes()),
            stdout=stdout_buf,
        )
        if exit_code != 0:
            context.err.print(
                f"[yellow]Warning: copy to {container_dest}"
                f" failed (exit {exit_code})[/yellow]"
            )


def setup_warning(exit_code: int, timeout) -> None:
    """Warn on a failed or timed-out setup script."""
    if exit_code == 124:
        context.err.print(f"[yellow]Setup timed out after {timeout}s[/yellow]")
    elif exit_code != 0:
        context.err.print(
            f"[yellow]Setup exited with code {exit_code}[/yellow]"
        )


async def sandbox_setup(ws, config, sandbox_root, handle):
    """Copy files and run setup script on an open WebSocket.

    Called once after workspace creation, before the shell starts.
    The caller has already connected and called wait_container_ready.

    Returns the setup script's exit code, or ``None`` if no setup
    command was configured (in which case there is nothing to fail).
    """
    await copy_sandbox_files(ws, config, sandbox_root, handle)

    # Run setup script — stream output to stderr in real time.
    setup_cmd = resolve_setup_command(config, handle)
    if setup_cmd:
        mount_at = expand_container_path(config.mount_at, handle)
        context.err.print(f"[dim]setup:[/dim] {setup_cmd}")
        # Set GIT_SSH_COMMAND so SSH accepts new host keys automatically.
        # Setup runs non-interactively (no TTY), so SSH cannot prompt the
        # user for host-key confirmation; without this, git-over-SSH hangs
        # indefinitely waiting for input that will never arrive.
        shell_cmd = (
            "export GIT_SSH_COMMAND="
            "'ssh -o StrictHostKeyChecking=accept-new'"
            f" && cd {shlex.quote(mount_at)}"
            f" && bash -c {shlex.quote(setup_cmd)}"
        )
        timeout = config.setup_timeout or None
        exit_code = await exec_on_ws(
            ws,
            ["sh", "-c", shell_cmd],
            stdout=sys.stderr.buffer,
            timeout=timeout,
        )
        setup_warning(exit_code, timeout)
        return exit_code
    return None


def reuse_or_refuse_workspace(client, workspace: str, force: bool):
    """The existing-workspace path: refuse without --force, else
    re-apply config."""
    ws = client.resolve_workspace(workspace)
    if not force:
        context.err.print(
            f"[red]Workspace [bold]{workspace}[/bold] already"
            " exists.[/red] Pass [bold]--force[/bold] to re-apply"
            " config and re-run setup."
        )
        raise typer.Exit(code=1)
    context.err.print(
        f"Workspace [bold]{workspace}[/bold] exists, re-applying config..."
    )
    return ws


def create_sandbox_workspace(client, workspace, config, sandbox_root, handle):
    """Create the sandbox workspace (``allow`` egress so setup.sh's
    installs proceed — #2325/#2406/#2404; see the original inline
    notes)."""
    all_mounts = build_all_mounts(config, sandbox_root, handle)
    context.err.print(f"Creating workspace [bold]{workspace}[/bold]...")
    ws = client.create_workspace(
        workspace,
        image=config.image,
        service_command=config.service_command,
        auto_start=config.auto_start,
        mounts=all_mounts,
        setup_state="pending"
        if resolve_setup_command(config, handle)
        else None,
        health_check=config.health_check,
        # #2325 / #2406 / #2404: a sandbox is an automated install
        # context (setup.sh runs npm/git/... that need unrestricted
        # outbound network). Default workspaces are interactive (hold
        # every egress for consent), which would block the install with
        # no decider present. Create the sandbox workspace in ``allow``
        # mode so its egress is default-permit (installs proceed), with
        # off-list destinations recorded through the consent pipeline
        # for observability and rejected_domains still enforced.
        # sandbox_setup_only resets egress_mode back to ``interactive``
        # and stops the container once setup.sh returns (#2404), so the
        # next start is consent-gated. Allow mode degrades to plain
        # unrestricted when the server has no network sidecar
        # configured, so the sandbox keeps working everywhere.
        egress_mode="allow",
    )
    return ws


def reallow_workspace_for_setup(client, ws_id: str) -> None:
    """#2404: on --force re-setup the workspace may have been reset to
    'interactive' by a prior sandbox run (sandbox_setup_only resets it
    after setup). setup.sh needs unrestricted egress, and egress_mode only
    takes effect at container start -- so flip back to 'allow' and restart
    before re-running setup. Also reset setup_state to 'pending' so the
    restart's create choke point DEFERS the service command until the
    re-setup completes -- otherwise /restart reads the stale 'complete'
    from the prior run and fires the service against half-reinstalled
    state."""
    client.update_workspace(ws_id, egress_mode="allow", setup_state="pending")
    client.restart_workspace_by_id(ws_id)


def run_sandbox_setup(
    surl, token, workspace, ws_id, config, sandbox_root, handle, client
) -> None:
    """Connect and run the sandbox's setup.sh (via sandbox_setup_only)."""
    context.err.print(f"Connecting to [bold]{workspace}[/bold] for setup...")
    try:
        asyncio.run(
            sandbox_setup_only(
                surl,
                token,
                ws_id,
                config,
                sandbox_root,
                handle,
                max_size=context.ws_max_size(),
                client=client,
            )
        )
    except websockets.InvalidStatus as e:
        if e.response.status_code in (4001, 4002):
            context.err.print(
                "[red]Session expired.[/red] Run"
                " [bold]klangk login[/bold] to re-authenticate."
            )
            raise typer.Exit(code=1) from None
        raise
    except ConnectionError as e:
        context.err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    except asyncio.TimeoutError as e:
        context.err.print(f"[red]{context.timeout_detail(e)}[/red]")
        raise typer.Exit(code=1) from None


def load_config_or_exit(sandbox_root: Path):
    """Load .klangk-sandbox.yaml or exit with the standard error."""
    try:
        return load_sandbox_config(sandbox_root)
    except FileNotFoundError as e:
        context.err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    except ValueError as e:
        context.err.print(f"[red]Invalid sandbox config:[/red] {e}")
        raise typer.Exit(code=1) from None


def needs_setup(created: bool, force: bool) -> bool:
    """A fresh create or a --force re-run both require the setup pass."""
    return created or force


def maybe_reallow(force: bool, created: bool, client, ws_id: str) -> None:
    """On a --force re-setup, flip back to allow and restart (#2404)."""
    if force and not created:
        reallow_workspace_for_setup(client, ws_id)


@context.app.command()
def sandbox(
    workspace: str = typer.Argument(help="Workspace name"),
    path: str = typer.Argument(
        ".",
        help="Path to sandbox root (directory containing .klangk/)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-apply config and re-run setup on an existing workspace",
    ),
) -> None:
    """Create a sandbox workspace from .klangk-sandbox.yaml.

    Creates the workspace with the configured image, mounts, and
    volumes, copies files, and runs the setup script.  Use
    ``klangk shell`` afterwards to connect.
    """
    # require_auth (not a raw token check) so none-mode auto-login
    # (#1374) and the stale-UDS hint (#1676) apply to sandbox too (#3090).
    context.require_auth()

    sandbox_root = Path(path).resolve()
    config = load_config_or_exit(sandbox_root)

    client = context.client()
    handle = client.get_handle()
    surl = context.server_url()
    token = context.session_token()
    created = False

    # Check if workspace already exists.
    try:
        ws = reuse_or_refuse_workspace(client, workspace, force)
    except WorkspaceNotFoundError:
        ws = create_sandbox_workspace(
            client, workspace, config, sandbox_root, handle
        )
        created = True

    if needs_setup(created, force):
        maybe_reallow(force, created, client, ws.id)
        run_sandbox_setup(
            surl, token, workspace, ws.id, config, sandbox_root, handle, client
        )

    context.err.print(
        f"[green]Done.[/green] Run [bold]klangk shell"
        f" {workspace}[/bold] to connect."
    )


async def mark_setup_state(client, workspace_id: str, new_state: str) -> None:
    """Best-effort setup_state update with a warning on failure (#1033)."""
    if client is None:
        return
    try:
        await asyncio.to_thread(
            client.set_setup_state, workspace_id, new_state
        )
    except Exception as e:
        context.err.print(
            f"[yellow]Warning: could not mark setup_state"
            f" = {new_state}: {e}[/yellow]"
        )


async def decide_egress_reset(config, client) -> tuple[bool, str | None]:
    """#2404: choose the post-install egress posture.

    The workspace was created in 'allow' so setup.sh could egress without
    a decider; now drop to the safe 'interactive' default -- but only when
    that is safe and meaningful:

      - auto-start workspaces boot unattended (no decider connected),
        and interactive DENIES egress with no decider, so their service
        command could never reach its upstream. Leave them in allow.
      - interactive is fail-closed: start_container refuses to start
        unfiltered when no network sidecar is configured. So only reset
        when the server can actually enforce it (netfilter_enabled).

    When neither skip applies, reset egress_mode to interactive and
    stop the container; egress_mode is applied by the sidecar at
    container START (not on the live container), so the stop forces the
    next start -- the user's `klangk shell` -- up in interactive mode.
    The service command is not fired in that case: it re-fires at the
    create choke point on that next start. Returns (reset, skipped_reason).
    """
    if config.auto_start:
        return False, "auto-start workspace boots unattended"
    if client is None:
        return False, None
    try:
        cfg = await asyncio.to_thread(client.config)
    except Exception as e:
        cfg = {}
        context.err.print(
            "[yellow]Warning: could not read server config"
            f" ({e}); leaving workspace in allow[/yellow]"
        )
    if not cfg.get("netfilter_enabled"):
        return False, "no network sidecar on this server"
    return True, None


async def reset_egress_and_stop(client, workspace_id: str) -> None:
    """Drop to the safe interactive default and stop the container.

    egress_mode applies at the next container START (not on the live
    container), so the stop forces the next `klangk shell` up in
    consent-gated mode. The service command is NOT fired here -- the
    container is being stopped, and it re-fires at the create choke
    point on that next start.
    """
    try:
        await asyncio.to_thread(
            client.update_workspace,
            workspace_id,
            egress_mode="interactive",
        )
    except Exception as e:
        context.err.print(
            "[yellow]Warning: could not reset egress_mode"
            f" to interactive: {e}[/yellow]"
        )
    try:
        await asyncio.to_thread(client.stop_workspace_by_id, workspace_id)
    except Exception as e:
        context.err.print(
            f"[yellow]Warning: could not stop container"
            f" after setup: {e}[/yellow]"
        )
    context.err.print(
        "[dim]Workspace stopped; egress reset to interactive"
        " for the next start.[/dim]"
    )


async def wait_for_terminal_start(ws, timeout: float) -> None:
    """Wait (bounded) for the terminal to start so the command actually
    runs before we disconnect. Other messages are ignored."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            break
        if json.loads(raw).get("type") == "terminal_started":
            break


async def fire_service_command(
    ws, config, setup_ok: bool, timeout: float = 30
) -> None:
    """Fire the service command via terminal_start so it's up (#1033:
    deferred until setup_state is complete, then launched in the
    dedicated service-cmd tmux window). Skipped on setup failure -- the
    service command's prerequisites are not met.
    """
    if not (config.service_command and setup_ok):
        return
    await ws.send(
        json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
    )
    await wait_for_terminal_start(ws, timeout)


def setup_succeeded(exit_code) -> bool:
    """No setup command (None) or a zero exit — both count as success."""
    return exit_code is None or exit_code == 0


async def post_setup_actions(
    ws,
    config,
    setup_ok: bool,
    client,
    workspace_id: str,
    reset_to_interactive: bool,
    skipped_reason,
) -> None:
    """Stop-and-reset or keep-allow-and-fire, per the posture decision."""
    if reset_to_interactive and client is not None:
        await reset_egress_and_stop(client, workspace_id)
    else:
        # Stay in allow (auto-start, no sidecar, or no client to
        # decide). The container keeps running, so fire the service
        # command now so it's up.
        await fire_service_command(ws, config, setup_ok)
        if client is not None:
            context.err.print(
                f"[dim]Workspace left in allow mode ({skipped_reason}).[/dim]"
            )


async def sandbox_setup_only(
    server_spec,
    token,
    workspace_id,
    config,
    sandbox_root,
    handle,
    max_size=None,
    client=None,
):
    """Connect to workspace, run setup, then disconnect (no shell).

    After setup.sh returns, marks the workspace's ``setup_state``
    (#1033): ``complete`` on success (or when no setup command is
    configured), ``failed`` otherwise. Then (#2404) chooses the
    post-install egress posture: drop the create-time ``allow`` back to
    the safe ``interactive`` default and stop the container -- but only
    when that is safe and meaningful. ``interactive`` is fail-closed
    without a network sidecar (``start_container`` refuses to start
    unfiltered), and it DENIES egress when no consent decider is
    connected, so an auto-start service workspace left interactive
    could never boot healthy. So the reset is skipped (workspace stays
    in ``allow``) for auto-start workspaces or when the server has no
    sidecar; otherwise ``egress_mode`` is reset to ``interactive`` and
    the container is stopped, so the next ``klangk shell`` start applies
    consent-gated egress. When the workspace stays in ``allow`` the
    container keeps running, so the service command is fired via
    ``terminal_start`` after setup (deferred until ``setup_state`` is
    complete, #1033); when the container is stopped it instead re-fires
    at the create choke point on the next start.
    """
    async with workspace_ws(
        server_spec, token, workspace_id, max_size=max_size
    ) as ws:
        # Re-enter 'pending' before running setup (#1033). On first
        # create the workspace is already 'pending', but on --force
        # re-setup it may be 'complete'/'failed'; either way this is
        # idempotent and ensures a visitor during (re-)setup is blocked
        # from firing the service command prematurely.
        await mark_setup_state(client, workspace_id, "pending")
        exit_code = await sandbox_setup(ws, config, sandbox_root, handle)

        # Mark setup_state before anything else (#1033). 'complete'
        # when setup ran and returned 0, or when there was no setup
        # command at all (nothing to fail); 'failed' otherwise.
        setup_ok = setup_succeeded(exit_code)
        await mark_setup_state(
            client, workspace_id, "complete" if setup_ok else "failed"
        )

        reset_to_interactive, skipped_reason = await decide_egress_reset(
            config, client
        )
        await post_setup_actions(
            ws,
            config,
            setup_ok,
            client,
            workspace_id,
            reset_to_interactive,
            skipped_reason,
        )
