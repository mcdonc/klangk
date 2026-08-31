"""Migration 0019: the ``container_events`` audit table (#2915).

Persists every klangk-managed container start/stop with the acting
principal (``actor_type`` user/agent/system + ``actor_id``), the
``cause``, and the podman correlation ids: the ``container_id``, its
``container_role`` ('workspace' | 'network-sidecar', CHECK-constrained),
and — on workspace rows — the ``network_namespace``: the network sidecar
container whose netns the workspace shares (NULL for unfiltered
workspaces and on sidecar rows, which ARE the netns owner).

Written at the lifecycle choke points — workspace rows at
``ContainerRegistry.start_container`` / ``stop_and_remove_container``
(plus the failed-start backstop, the labeled sweep stops, and the boot
reaps), sidecar rows at ``start_network_sidecar`` creation and the
label-based removal choke point (plus the dependent-container forced
removals). All writes are best-effort: a failed audit write is logged,
never fatal to the start/stop itself. Retention/bounding is a deliberate
follow-up (#2924), so the table grows without bound for now.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS container_events (
            id INTEGER PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            event TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            cause TEXT NOT NULL,
            container_id TEXT,
            container_role TEXT NOT NULL DEFAULT 'workspace'
                CHECK (container_role IN ('workspace', 'network-sidecar')),
            network_namespace TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_container_events_ws_time
            ON container_events (workspace_id, created_at DESC)
        """
    )


migration = Migration(19, "0019_container_events", apply)
