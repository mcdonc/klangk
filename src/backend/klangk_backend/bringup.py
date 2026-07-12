"""Container bring-up: provision agent home, fire the service command.

This is the single choke point reached after
``ContainerRegistry.start_container`` creates a *fresh* container
(status ``"created"``). ``container`` cannot import ``agent``
directly (``agent`` already imports ``container``), so the bring-up
step lives here to keep that boundary clean.

``ensure_service_session`` is idempotent (per-container lock +
window-exists check), so calling this on every fresh create is safe:
after the first fire it is a no-op. The create-time deferral for
workspaces whose ``setup.sh`` has not run yet is handled by gating on
``setup_state`` -- the CLI sandbox driver marks such workspaces
``"pending"`` at create, and the fire lands later once setup
completes and the WS connect path runs.
"""

from . import agent, terminal


async def bringup(
    workspace_id: str,
    container_id: str,
    service_command: str | None,
    setup_state: str | None,
    app_state=None,
) -> None:
    """Provision the agent home and fire the service command.

    Called at the single choke point: every freshly-created container.
    Idempotent via :func:`terminal.ensure_service_session`.
    """
    agent_home = await agent.ensure_agent_home(workspace_id, container_id)
    if not service_command:
        return
    await terminal.ensure_service_session(
        container_id,
        agent_home,
        service_command,
        setup_state=setup_state,
        app_state=app_state,
    )
